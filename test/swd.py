# SPDX-License-Identifier: Apache-2.0
"""A model of an ARM SWD debug port (the target side of the wire), a reference host, and a UART decoder.

Used by test_swd_model.py (instruction-set model) and test_swd_rtl.py (the chip) to test firmware/examples/swd.asm.

The wire, as modelled here (ARM Debug Interface v5, serial wire protocol version 1):
  * Every bit takes one SWCLK cycle, LSB first. A bit the host drives is taken by the target on the rising edge
    that ends its cycle; a bit the target drives is put on the line on the rising edge that starts its cycle.
  * Request (8 bits from the host): start 1, APnDP, RnW, A2, A3, parity of those four, stop 0, park 1.
  * One turnaround cycle, then ACK (3 bits from the target): 1 = OK, 2 = WAIT, 4 = FAULT.
  * Read:  32 data bits and a parity bit from the target, then a turnaround cycle.
    Write: a turnaround cycle, then 32 data bits and a parity bit from the host.
    The parity bit is 1 if the 32 data bits have an odd number of ones. After WAIT or FAULT there is no data.
  * Line reset: at least 50 cycles with SWDIO high, then at least 2 idle cycles (low). The first transaction
    after it must read DPIDR, or the port stops answering until the next line reset.
  * A port that powers up in JTAG mode needs the 16-bit sequence 0xE79E (LSB first) between two line resets
    (the model takes it only as the 16 cycles that follow a line reset).
The cycle order (request, turnaround, ACK, data, turnaround / turnaround, data) is the one of the CMSIS-DAP
reference probe. This model was written from the specification as remembered, not copied from a real target.
"""

OK, WAIT, FAULT = 1, 2, 4
JTAG_TO_SWD = 0xE79E
DPIDR_READ = 0xA5


def parity32(v):
    return bin(v & 0xFFFFFFFF).count("1") & 1


def request(apndp, rnw, addr):
    """The request byte (transmitted LSB first) for register address addr (0, 4, 8 or 12)."""
    a2, a3 = (addr >> 2) & 1, (addr >> 3) & 1
    par = apndp ^ rnw ^ a2 ^ a3
    return 1 | apndp << 1 | rnw << 2 | a2 << 3 | a3 << 4 | par << 5 | 0 << 6 | 1 << 7


class SwdTarget:
    """The debug port. Call rising(level) on every rising SWCLK edge with the level of SWDIO as driven by the
    host (1 when the host has let go). `drive` is what the target puts on the line: None, 0 or 1.

    Options: dpidr; needs_select (powers up in JTAG mode); waits = {transaction index: number of WAIT answers};
    fault_at = index of the transaction answered with FAULT; bad_parity_at = index of the read whose parity bit
    is inverted; dead = never answers. Transaction indices count the requests the port accepted, from 0.
    """

    def __init__(self, dpidr=0x2BA01477, needs_select=True, waits=None, fault_at=None, bad_parity_at=None,
                 dead=False):
        self.dpidr, self.dead = dpidr, dead
        self.swd = not needs_select
        self.waits = dict(waits or {})
        self.fault_at, self.bad_parity_at = fault_at, bad_parity_at
        self.drive = None
        self.state = "locked"          # locked | reset | idle | req | xfer
        self.ones = 0
        self.sr16 = 0
        self.idle = 0
        self.need_dpidr = True
        self.bits = []
        self.plan = []                 # what to do on the next rising edges
        self.index = 0                 # transactions answered with something other than WAIT
        self.ctrl = 0
        self.select = 0
        self.abort = None
        self.log = []                  # (kind, request, ack, value)
        self.errors = []
        self.line_resets = 0
        self.since_reset = 10**9

    # ---- registers
    def read_reg(self, apndp, addr):
        if apndp:
            return 0
        if addr == 0:
            return self.dpidr
        if addr == 4:
            return self.ctrl | ((self.ctrl & 0x50000000) << 1)      # the ACK bits follow the requests
        return 0

    def write_reg(self, apndp, addr, value):
        if apndp:
            return
        if addr == 0:
            self.abort = value
        elif addr == 4:
            self.ctrl = value & 0x50000000
        elif addr == 8:
            self.select = value

    # ---- the wire
    def rising(self, level):
        if self.plan:                                   # inside a transaction: the plan says what each edge does
            step = self.plan.pop(0)
            step(level)
            return
        if self.drive is not None:
            self.errors.append("target left driving outside a transaction")
            self.drive = None
        self.ones = self.ones + 1 if level else 0
        self.sr16 = (self.sr16 >> 1) | (level << 15)
        if self.ones >= 50:
            if self.state != "reset":
                self.line_resets += 1
            self.state, self.need_dpidr, self.idle, self.since_reset = "reset", True, 0, 0
            return
        self.since_reset += 1
        if self.sr16 == JTAG_TO_SWD and self.since_reset == 16:     # only straight after a line reset
            self.swd = True
        if self.state == "reset":                       # the first low cycle after the reset
            self.state, self.idle = "idle", 1
        elif self.state == "idle":
            if level:
                if self.need_dpidr and self.idle < 2:
                    self.state = "locked"               # fewer than 2 idle cycles after a line reset
                else:
                    self.state, self.bits = "req", [1]
            else:
                self.idle += 1
        elif self.state == "req":
            self.bits.append(level)
            if len(self.bits) == 8:
                self.got_request(sum(b << i for i, b in enumerate(self.bits)))

    def got_request(self, req):
        start, apndp, rnw = req & 1, (req >> 1) & 1, (req >> 2) & 1
        addr = ((req >> 3) & 3) << 2
        par, stop, park = (req >> 5) & 1, (req >> 6) & 1, (req >> 7) & 1
        valid = start and par == (apndp ^ rnw ^ ((req >> 3) & 1) ^ ((req >> 4) & 1)) and not stop and park
        if not valid or not self.swd or self.dead or (self.need_dpidr and req != DPIDR_READ):
            self.state = "locked"                       # protocol error: silent until the next line reset
            return
        self.need_dpidr = False
        if self.waits.get(self.index, 0) > 0:
            self.waits[self.index] -= 1
            ack = WAIT
        elif self.fault_at == self.index:
            ack = FAULT
        else:
            ack = OK
        if ack != WAIT:
            self.index += 1
        me = self.index - 1

        def put(v):
            return lambda level: setattr(self, "drive", v)

        def release(level):                             # the turnaround cycle starts: let go of the line
            self.drive = None

        def end(level):                                 # the turnaround cycle ends: the line is the host's again
            self.state, self.idle = "idle", 0

        plan = [put((ack >> i) & 1) for i in range(3)]
        if ack != OK:
            self.log.append(("read" if rnw else "write", req, ack, None))
            plan += [release, end]
        elif rnw:
            value = self.read_reg(apndp, addr)
            p = parity32(value) ^ (1 if self.bad_parity_at == me else 0)
            self.log.append(("read", req, ack, value))
            plan += [put((value >> i) & 1) for i in range(32)] + [put(p), release, end]
        else:
            got = []

            def skip(level):
                pass

            def take(level):
                got.append(level)

            def last(level):
                value = sum(b << i for i, b in enumerate(got))
                if level != parity32(value):
                    self.errors.append(f"write of {value:#010x}: wrong parity bit from the host")
                else:
                    self.write_reg(apndp, addr, value)
                self.log.append(("write", req, ack, value))
                self.state, self.idle = "idle", 0

            plan += [release, skip] + [take] * 32 + [last]
        self.plan = plan
        self.state = "xfer"


class Wire:
    """SWCLK / SWDIO between a host and a SwdTarget, clock by clock, with the checks a real target relies on.

    step(clock, swclk, host_low) is called once per system clock with the host's SWCLK and whether the host pulls
    SWDIO low; level() is what both ends see on SWDIO. Checks (in system clocks): SWCLK high and low for at least
    `min_phase`; a host-driven SWDIO stable for `setup` clocks before and `hold` clocks after every rising edge;
    the host never pulls the line low while the target drives it.
    """

    def __init__(self, target, min_phase=40, setup=40, hold=8):
        self.t = target
        self.min_phase, self.setup, self.hold = min_phase, setup, hold
        self.swclk, self.host_low = 0, 0
        self.last_edge, self.last_change, self.last_rise = None, -10**9, -10**9
        self.rises = 0
        self.errors = []

    def level(self):
        if self.host_low or (self.t is not None and self.t.drive == 0):
            return 0
        return 1

    def step(self, clock, swclk, host_low):
        t = self.t
        if host_low != self.host_low:
            if clock - self.last_rise < self.hold:
                self.errors.append(f"clock {clock}: SWDIO changed {clock - self.last_rise} clocks after SWCLK rose")
            self.last_change = clock
        if host_low and t is not None and t.drive is not None:
            self.errors.append(f"clock {clock}: the host pulls SWDIO low while the target drives it")
        self.host_low = host_low
        if swclk != self.swclk:
            if self.last_edge is not None and clock - self.last_edge < self.min_phase:
                self.errors.append(f"clock {clock}: SWCLK phase of only {clock - self.last_edge} clocks")
            self.last_edge = clock
            if swclk:
                if clock - self.last_change < self.setup:
                    self.errors.append(f"clock {clock}: SWDIO changed {clock - self.last_change} clocks before "
                                       "SWCLK rose")
                self.last_rise = clock
                self.rises += 1
                if t is not None:
                    t.rising(self.level())
            self.swclk = swclk


class UartRx:
    """8N1 receiver for the chip's report on uo[0]: 432 clocks per bit (115200 baud at 50 MHz)."""

    def __init__(self, clocks_per_bit=432):
        self.cpb = clocks_per_bit
        self.prev, self.start = 1, None
        self.bytes = []
        self.errors = []

    def step(self, clock, tx):
        if self.start is None:
            if self.prev and not tx:
                self.start, self.val = clock, 0
        else:
            k, r = divmod(clock - self.start, self.cpb)
            if r == self.cpb // 2:
                if k == 0 and tx:
                    self.errors.append("UART: start bit not low")
                elif 1 <= k <= 8:
                    self.val |= tx << (k - 1)
                elif k == 9:
                    if not tx:
                        self.errors.append("UART: stop bit not high")
                    self.bytes.append(self.val)
                    self.start = None
        self.prev = tx


def reference_host(target, ops, select=True):
    """A plain bit-level SWD host in Python (the cycle order of the CMSIS-DAP probe), used to test the target
    model without any firmware. ops = [("r", request) | ("w", request, value)]; returns [(ack, value or None)]."""
    def bit_out(b):
        target.rising(b)

    def bit_in():
        v = 1 if target.drive is None else target.drive
        target.rising(1)
        return v

    def reset():
        for _ in range(56):
            bit_out(1)

    reset()
    if select:
        for i in range(16):
            bit_out((JTAG_TO_SWD >> i) & 1)
        reset()
    for _ in range(8):
        bit_out(0)
    out = []
    for op in ops:
        req = op[1]
        for i in range(8):
            bit_out((req >> i) & 1)
        target.rising(1)                                # turnaround
        ack = sum(bit_in() << i for i in range(3))
        value = None
        if ack == OK and op[0] == "r":
            value = sum(bit_in() << i for i in range(32))
            par = bit_in()
            value = (value, par == parity32(value))
            target.rising(1)                            # turnaround
        elif ack == OK:
            target.rising(1)                            # turnaround
            for i in range(32):
                bit_out((op[2] >> i) & 1)
            bit_out(parity32(op[2]))
        else:
            target.rising(1)
        for _ in range(8):
            bit_out(0)
        out.append((ack, value))
    return out
