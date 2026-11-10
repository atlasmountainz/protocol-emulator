# SPDX-License-Identifier: Apache-2.0
"""Tests for firmware/examples/onewire.asm and firmware/examples/jtag.asm.

Run with:  cd test && make COCOTB_TEST_MODULES=test_onewire_jtag

Each program is loaded through the real pin loader (test/loader.py) and driven by a behavioural
device model written in Python:
  * OneWireSlave   a 1-Wire ROM device. It checks every pulse the master makes against the
                   standard-speed timing windows, decodes the command and serves the ROM bits.
  * JtagChain      a chain of IEEE 1149.1 TAPs (16-state FSM, IR, IDCODE and BYPASS), which checks
                   TCK speed and TMS/TDI setup and hold, and records the scans the master does.
The UART output (uo[0]) is decoded strictly: every edge must sit on a multiple of 432 clocks.

Environment (used by the mutation checks): OW_ASM / JTAG_ASM replace the firmware file under test.
"""
import os
import sys
from pathlib import Path

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, RisingEdge

from loader import load_program

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
from load_image import build_image  # noqa: E402

EXAMPLES = Path(__file__).resolve().parent.parent / "firmware" / "examples"
OW_ASM = os.environ.get("OW_ASM", str(EXAMPLES / "onewire.asm"))
JTAG_ASM = os.environ.get("JTAG_ASM", str(EXAMPLES / "jtag.asm"))

US = 50                 # clocks per microsecond (50 MHz)
CLKS_PER_BIT = 432      # UART, 115200 baud


# ------------------------------------------------------------------------------ common helpers

async def boot(dut, asm_file, uio=0x0F, ui=0):
    """Reset, load the program over the pins, release LOAD. `uio` is what the pads read at start."""
    task = getattr(dut, "_clk_task", None)
    if task is None or task.done():          # one clock per test; tests cancel their tasks at the end
        dut._clk_task = cocotb.start_soon(Clock(dut.clk, 20, unit="ns").start())
    dut.ena.value = 1
    dut.ui_in.value = 0
    dut.uio_in.value = 0
    dut.rst_n.value = 0
    await ClockCycles(dut.clk, 10)
    dut.rst_n.value = 1
    await ClockCycles(dut.clk, 2)
    words = build_image([asm_file])
    assert sum(1 for w in words if w != 0xF000) <= 128, "examples are kept within 128 words"
    await load_program(dut, words)
    dut.ui_in.value = ui
    dut.uio_in.value = uio
    await ClockCycles(dut.clk, 6)


def pads_high(dut):
    """uio[3:1] are released-pad scratch lines: pulled up, pulled low only by the chip."""
    return (~int(dut.uio_oe.value)) & 0x0E


async def uart_monitor(dut, frames, errors):
    """Decode 8N1 frames on uo[0]. Exact timing: within a frame TX may only change at multiples of
    432 clocks after the start edge. frames gets (start_clock, byte)."""
    cycle = 0
    prev = 1
    while True:
        await RisingEdge(dut.clk)
        cycle += 1
        cur = int(dut.uo_out.value) & 1
        if prev == 1 and cur == 0:
            start = cycle
            samples = [0]
            for _ in range(10 * CLKS_PER_BIT - 1):
                await RisingEdge(dut.clk)
                cycle += 1
                samples.append(int(dut.uo_out.value) & 1)
            bits = []
            for b in range(10):
                chunk = samples[b * CLKS_PER_BIT:(b + 1) * CLKS_PER_BIT]
                if len(set(chunk)) != 1:
                    errors.append(f"UART bit {b} of frame at {start} is not stable for {CLKS_PER_BIT} clocks")
                bits.append(chunk[0])
            if bits[0] != 0 or bits[9] != 1:
                errors.append(f"bad start/stop bits {bits}")
            frames.append((start, sum(bits[1 + i] << i for i in range(8))))
            cur = samples[-1]
        prev = cur


async def wait_for(dut, cond, limit):
    for _ in range(limit):
        await RisingEdge(dut.clk)
        if cond():
            return
    raise AssertionError("timeout waiting for the expected output")


def crc8(data):
    """Dallas / Maxim CRC8: x^8 + x^5 + x^4 + 1, reflected, init 0."""
    crc = 0
    for byte in data:
        for i in range(8):
            fb = (crc ^ (byte >> i)) & 1
            crc >>= 1
            if fb:
                crc ^= 0x8C
    return crc


def make_rom(payload7):
    return list(payload7) + [crc8(payload7)]


# ------------------------------------------------------------------------------ 1-Wire model

class OneWireSlave:
    """1-Wire ROM device on uio[0] (open drain, external pull-up), standard speed.

    The test calls run() once per simulation. Timing rules checked on the master (all in us):
      reset low 480..960; no falling edge for 480 us after the reset pulse ends (tRSTH);
      slot (falling edge to falling edge) >= 60; recovery (release to next falling edge) >= 1;
      write-1: low 1..15; write-0: low 60..120; read slot: low 1..15.
    The device answers READ ROM (0x33) only.  `sample_us` is when the device samples a write slot
    (anything in 15..60 is legal); `hold_us` is how long it keeps the line low for a 0 in a read
    slot, counted from the falling edge (15 is the worst case a master has to cope with).
    """

    def __init__(self, dut, rom, present=True, pres_delay_us=30, pres_width_us=120, sample_us=35,
                 hold_us=15, stuck_low=False):
        self.dut, self.rom = dut, list(rom)
        self.present, self.stuck_low = present, stuck_low
        self.pres_delay, self.pres_width = pres_delay_us * US, pres_width_us * US
        self.sample, self.hold = sample_us * US, hold_us * US
        self.errors = []
        self.log = []                  # protocol events, for the tests to look at
        self.cmd = None
        self.rom_bits_served = 0
        self.resets = 0
        self.slots_after_reset = 0
        self.min_slot = None
        self.pending_bit = self.pending_fall = None

    def err(self, msg):
        self.errors.append(msg)

    def rom_bit(self, n):
        return (self.rom[n // 8] >> (n % 8)) & 1

    async def run(self):
        dut = self.dut
        now = 0
        prev_low = 0
        t_fall = t_rise = t_prev_fall = None
        t_reset_end = None
        pres_on = pres_off = None
        hold_until = -1
        state = "idle"          # idle -> reset_seen -> cmd -> rom -> done
        nbits = 0
        cmd = 0
        sample_at = None
        sample_val = None
        while True:
            await RisingEdge(dut.clk)
            now += 1
            oe = int(dut.uio_oe.value)
            mlow = oe & 1
            # --- master edges
            if mlow and not prev_low:                      # falling edge
                t_fall = now
                if t_rise is not None and now - t_rise < 1 * US and state in ("cmd", "rom"):
                    self.err(f"recovery {(now - t_rise) / US:.2f} us < 1 us")
                if t_prev_fall is not None and state in ("cmd", "rom") and now - t_prev_fall < 60 * US:
                    self.err(f"slot {(now - t_prev_fall) / US:.2f} us < 60 us")
                if t_prev_fall is not None and state in ("cmd", "rom"):
                    s = now - t_prev_fall
                    self.min_slot = s if self.min_slot is None else min(self.min_slot, s)
                if state == "reset_seen":
                    if now - t_reset_end < 480 * US:
                        self.err(f"first slot {(now - t_reset_end) / US:.1f} us after reset, tRSTH < 480 us")
                    state = "cmd"
                    nbits = cmd = 0
                if state == "cmd":
                    sample_at = now + self.sample
                elif state == "rom":
                    if self.rom_bits_served >= 64:
                        self.err("master started a 65th read slot")
                    else:
                        if self.rom_bit(self.rom_bits_served) == 0:
                            hold_until = now + self.hold
                        self.rom_bits_served += 1
                elif state == "done":
                    self.err("slot after the ROM code was read")
                if not (self.present or self.stuck_low) and state != "idle":
                    self.err("master kept talking to an absent device")
                t_prev_fall = now if state in ("cmd", "rom") else None
                if state in ("cmd", "rom"):
                    self.slots_after_reset += 1
            if prev_low and not mlow:                      # release
                low = now - t_fall
                t_rise = now
                if state in ("idle", "reset_seen") and low >= 100 * US:
                    # a reset pulse
                    if not 480 * US <= low <= 960 * US:
                        self.err(f"reset low {low / US:.1f} us outside 480..960")
                    self.resets += 1
                    t_reset_end = now
                    state = "reset_seen"
                    if self.present:
                        pres_on, pres_off = now + self.pres_delay, now + self.pres_delay + self.pres_width
                    self.cmd = None
                    self.rom_bits_served = 0
                    t_prev_fall = None
                elif state == "cmd":
                    pass       # checked when the bit is sampled below
                elif state == "rom":
                    if not 1 * US <= low <= 15 * US:
                        self.err(f"read slot low {low / US:.2f} us outside 1..15")
                elif state in ("idle", "reset_seen"):
                    self.err(f"stray low pulse of {low / US:.2f} us")
            # --- device samples a write slot
            if state == "cmd" and sample_at is not None and now == sample_at:
                sample_val = 0 if (int(dut.uio_oe.value) & 1) else 1
                cmd |= sample_val << nbits
                nbits += 1
                sample_at = None
                self.pending_bit = sample_val
                self.pending_fall = t_fall
            if state == "cmd" and prev_low and not mlow and nbits > 0 and self.pending_fall == t_fall:
                low = now - t_fall
                if self.pending_bit == 1 and not 1 * US <= low <= 15 * US:
                    self.err(f"write-1 low {low / US:.2f} us outside 1..15")
                if self.pending_bit == 0 and not 60 * US <= low <= 120 * US:
                    self.err(f"write-0 low {low / US:.2f} us outside 60..120")
                self.pending_fall = None
            if state == "cmd" and nbits == 8 and sample_at is None and self.pending_fall is None:
                self.cmd = cmd
                self.log.append(("cmd", cmd))
                if cmd == 0x33:
                    state = "rom"
                else:
                    self.err(f"unexpected command {cmd:#04x}")
                    state = "done"
            if state == "rom" and self.rom_bits_served >= 64 and prev_low and not mlow:
                state = "done"
            prev_low = mlow
            # --- what the device drives
            slow = (pres_on is not None and pres_on <= now < pres_off) or now < hold_until or self.stuck_low
            bus = 0 if (mlow or slow) else 1
            dut.uio_in.value = pads_high(dut) | bus


@cocotb.test()
async def test_crc8_reference(dut):
    """The Python CRC helper gives the CRC of the widely quoted Maxim iButton example ROM
    (02 1C B8 01 00 00 00 -> A2), and the CRC of a message including its CRC byte is zero."""
    assert crc8([0x02, 0x1C, 0xB8, 0x01, 0x00, 0x00, 0x00]) == 0xA2
    assert crc8(make_rom([1, 2, 3, 4, 5, 6, 7])) == 0


async def run_onewire(dut, slave, nframes, settle_clks=30000, limit=400000):
    await boot(dut, OW_ASM)
    tasks = [cocotb.start_soon(slave.run())]
    frames, uerr = [], []
    tasks.append(cocotb.start_soon(uart_monitor(dut, frames, uerr)))
    try:
        await wait_for(dut, lambda: len(frames) >= nframes, limit)
        await ClockCycles(dut.clk, settle_clks)       # nothing more may follow
    finally:
        for t in tasks:
            t.cancel()
    assert not uerr, uerr
    assert not slave.errors, slave.errors
    return [b for _, b in frames], frames


@cocotb.test()
async def test_onewire_good_crc(dut):
    """READ ROM from a good device: 8 bytes + status 0. Run with the two extremes of the legal
    device timing (earliest/shortest presence, latest/longest presence; early/late write sampling)."""
    for kw, payload in ((dict(pres_delay_us=15, pres_width_us=60, sample_us=15, hold_us=15),
                         [0x28, 0xFF, 0x64, 0x1E, 0x8F, 0x2B, 0x6C]),
                        (dict(pres_delay_us=60, pres_width_us=240, sample_us=59, hold_us=15),
                         [0x01, 0x80, 0x00, 0xA5, 0x5A, 0xFF, 0x7E])):
        rom = make_rom(payload)
        slave = OneWireSlave(dut, rom, **kw)
        got, frames = await run_onewire(dut, slave, 9)
        assert got == rom + [0x00], f"{[hex(b) for b in got]} != {[hex(b) for b in rom + [0]]}"
        assert slave.cmd == 0x33 and slave.resets == 1 and slave.rom_bits_served == 64
        assert slave.slots_after_reset == 8 + 64
        dut._log.info(f"1-Wire {kw}: ROM {[hex(b) for b in got]}, min slot "
                      f"{slave.min_slot / US:.1f} us")


@cocotb.test()
async def test_onewire_crc_error(dut):
    """A device whose ROM CRC is wrong (corrupt data byte, corrupt CRC byte, a single bit in the
    last data byte): all 8 bytes are still reported, status 1."""
    base = [0x10, 0xEC, 0x3B, 0x91, 0x00, 0x08, 0x00]
    good = make_rom(base)
    for name, rom in (("data byte", good[:2] + [good[2] ^ 0x04] + good[3:]),
                      ("crc byte", good[:7] + [good[7] ^ 0x80]),
                      ("last data bit", good[:6] + [good[6] ^ 0x01] + good[7:])):
        slave = OneWireSlave(dut, rom)
        got, _ = await run_onewire(dut, slave, 9)
        assert got == rom + [0x01], f"{name}: {[hex(b) for b in got]}"


@cocotb.test()
async def test_onewire_crc_patterns(dut):
    """Corner-case ROM contents for the firmware CRC: all-zero payload (CRC 0), all-ones payload, and
    the all-zero payload with a CRC byte of 1 (the register ends non-zero)."""
    for rom, status in ((make_rom([0] * 7), 0), (make_rom([0xFF] * 7), 0), ([0] * 7 + [1], 1)):
        slave = OneWireSlave(dut, rom)
        got, _ = await run_onewire(dut, slave, 9, settle_clks=2000)
        assert got == rom + [status], f"{[hex(b) for b in got]}"


@cocotb.test()
async def test_onewire_no_device(dut):
    """Nobody on the bus: after the reset pulse the line stays high, the master reports 2 and stops."""
    slave = OneWireSlave(dut, [0] * 8, present=False)
    got, frames = await run_onewire(dut, slave, 1, settle_clks=60000, limit=100000)
    assert got == [0x02], got
    assert slave.resets == 1 and slave.slots_after_reset == 0


@cocotb.test()
async def test_onewire_stuck_low(dut):
    """A shorted bus (always low) looks like a presence pulse that never ends: status 2."""
    slave = OneWireSlave(dut, [0] * 8, stuck_low=True)
    got, _ = await run_onewire(dut, slave, 1, settle_clks=20000, limit=100000)
    assert got == [0x02], got


# ------------------------------------------------------------------------------ JTAG model

TCK, TMS, TDI = 2, 3, 4          # uo[2], uo[3], uo[4]

NEXT = {  # state -> (next if TMS=0, next if TMS=1)
    "TLR": ("RTI", "TLR"), "RTI": ("RTI", "SelDR"),
    "SelDR": ("CapDR", "SelIR"), "CapDR": ("ShDR", "Ex1DR"), "ShDR": ("ShDR", "Ex1DR"),
    "Ex1DR": ("PauDR", "UpdDR"), "PauDR": ("PauDR", "Ex2DR"), "Ex2DR": ("ShDR", "UpdDR"),
    "UpdDR": ("RTI", "SelDR"),
    "SelIR": ("CapIR", "TLR"), "CapIR": ("ShIR", "Ex1IR"), "ShIR": ("ShIR", "Ex1IR"),
    "Ex1IR": ("PauIR", "UpdIR"), "PauIR": ("PauIR", "Ex2IR"), "Ex2IR": ("ShIR", "UpdIR"),
    "UpdIR": ("RTI", "SelDR"),
}


class Tap:
    """One device: IR of `irlen` bits, IDCODE (default instruction after reset) and BYPASS (all ones)."""

    def __init__(self, idcode, irlen, idcode_op):
        self.idcode, self.irlen, self.idcode_op = idcode, irlen, idcode_op
        self.bypass_op = (1 << irlen) - 1
        self.ir = self.idcode_op
        self.ir_shift = 0
        self.dr, self.drlen = 0, 1

    def reset(self):
        self.ir = self.idcode_op

    def tdo(self, state):
        if state == "ShDR":
            return self.dr & 1
        if state == "ShIR":
            return self.ir_shift & 1
        return 1

    def capture_dr(self):
        if self.ir == self.idcode_op:
            self.dr, self.drlen = self.idcode, 32
        else:
            self.dr, self.drlen = 0, 1       # BYPASS (and any unknown instruction)

    def shift(self, state, tdi):
        if state == "ShDR":
            self.dr = (self.dr >> 1) | (tdi << (self.drlen - 1))
        elif state == "ShIR":
            self.ir_shift = (self.ir_shift >> 1) | (tdi << (self.irlen - 1))


class JtagChain:
    """Devices in series: TDI -> devices[0] -> ... -> devices[-1] -> TDO. TMS/TCK are common.

    Behaviour as in IEEE 1149.1: TMS and TDI are sampled on the rising edge of TCK, TDO changes on
    the falling edge. Checks TCK period/pulse widths (master must be at 1 MHz or slower) and that TMS
    and TDI never change while TCK is high and are stable >= 3 clocks before the rising edge.
    """

    def __init__(self, dut, devices, tdo_stuck=None):
        self.dut, self.devs = dut, devices
        self.tdo_stuck = tdo_stuck         # None, 0 or 1: no device at all, TDO tied to a level
        self.state = "TLR"
        self.errors = []
        self.scans = []                    # [kind, number of TCK edges taken in the shift state]
        self.trace = []                    # (state before the edge, tms)
        self.edges = 0
        self.min_period = self.min_high = self.min_low = None
        self.tdo = 1
        self.tdi_seen = []

    def err(self, m):
        self.errors.append(m)

    async def run(self):
        dut = self.dut
        now = 0
        prev_tck = 0
        prev_tms = prev_tdi = None
        t_change = -100
        t_rise = t_fall = None
        while True:
            await RisingEdge(dut.clk)
            now += 1
            o = int(dut.uo_out.value)
            tck, tms, tdi = (o >> TCK) & 1, (o >> TMS) & 1, (o >> TDI) & 1
            if (tms, tdi) != (prev_tms, prev_tdi):
                if tck and prev_tms is not None:
                    self.err(f"TMS/TDI changed while TCK was high (clock {now})")
                t_change = now
                prev_tms, prev_tdi = tms, tdi
            if tck and not prev_tck:                          # rising edge
                if now - t_change < 3:
                    self.err(f"TMS/TDI setup {now - t_change} clocks < 3 (clock {now})")
                if t_rise is not None:
                    p = now - t_rise
                    self.min_period = p if self.min_period is None else min(self.min_period, p)
                    if p < 50:
                        self.err(f"TCK period {p} clocks < 1 us")
                    low = now - t_fall
                    self.min_low = low if self.min_low is None else min(self.min_low, low)
                    if low < 20:
                        self.err(f"TCK low {low} clocks < 400 ns")
                t_rise = now
                self.edge(tms, tdi)
            if prev_tck and not tck:                          # falling edge
                t_fall = now
                if now - t_rise < 20:
                    self.err(f"TCK high {now - t_rise} clocks < 400 ns")
                high = now - t_rise
                self.min_high = high if self.min_high is None else min(self.min_high, high)
                self.update_tdo()
            prev_tck = tck
            dut.ui_in.value = self.tdo
            dut.uio_in.value = pads_high(dut) | 1

    def update_tdo(self):
        if self.tdo_stuck is not None:
            self.tdo = self.tdo_stuck
        else:
            self.tdo = self.devs[-1].tdo(self.state)

    def edge(self, tms, tdi):
        self.edges += 1
        self.trace.append((self.state, tms))
        st = self.state
        # shift registers move on the edge taken in a shift state (before the state changes)
        if st in ("ShDR", "ShIR") and self.tdo_stuck is None:
            outs = [d.tdo(st) for d in self.devs]
            for i, d in enumerate(self.devs):
                d.shift(st, tdi if i == 0 else outs[i - 1])
            self.tdi_seen.append((st, tdi))
        if st in ("ShDR", "ShIR"):
            if self.scans and self.scans[-1][2] == "open" and self.scans[-1][0] == st[2:]:
                self.scans[-1][1] += 1
        nxt = NEXT[st][tms]
        if nxt in ("ShDR", "ShIR") and st not in ("ShDR", "ShIR"):
            self.scans.append([nxt[2:], 0, "open"])
            # a scan is open until the TAP leaves the shift state
        if st in ("ShDR", "ShIR") and nxt != st and nxt not in ("ShDR", "ShIR"):
            if self.scans and self.scans[-1][2] == "open":
                self.scans[-1][2] = "closed"
        self.state = nxt
        if nxt == "TLR":
            for d in self.devs:
                d.reset()
        if nxt == "CapDR":
            for d in self.devs:
                d.capture_dr()
        if nxt == "CapIR":
            for d in self.devs:
                d.ir_shift = 0b01        # IEEE 1149.1: IR captures ...01
        if nxt == "UpdIR":
            for d in self.devs:
                d.ir = d.ir_shift

    def closed_scans(self):
        return [(k, n) for k, n, s in self.scans]


def jtag_expect(idcode, nchain):
    b = [(idcode >> (8 * i)) & 0xFF for i in range(4)]
    return b + [0x00, nchain]


async def run_jtag(dut, chain, nframes=6, limit=600000, settle=20000):
    await boot(dut, JTAG_ASM)
    tasks = [cocotb.start_soon(chain.run())]
    frames, uerr = [], []
    tasks.append(cocotb.start_soon(uart_monitor(dut, frames, uerr)))
    try:
        await wait_for(dut, lambda: len(frames) >= nframes, limit)
        await ClockCycles(dut.clk, settle)
    finally:
        for t in tasks:
            t.cancel()
    assert not uerr, uerr
    assert not chain.errors, chain.errors[:5]
    return [b for _, b in frames]


@cocotb.test()
async def test_jtag_single_device(dut):
    """One device with a 4-bit IR. IDCODE read out (LSB first), IR loaded with BYPASS, chain length 1."""
    dev = Tap(idcode=0x1BA00477, irlen=4, idcode_op=0b0010)
    chain = JtagChain(dut, [dev])
    got = await run_jtag(dut, chain)
    assert got == jtag_expect(0x1BA00477, 1), [hex(b) for b in got]
    assert dev.ir == dev.bypass_op, "IR must hold BYPASS (all ones) after the IR scan"
    assert chain.closed_scans() == [("DR", 32), ("IR", 32), ("DR", 2)], chain.closed_scans()
    assert chain.state == "RTI", f"master must leave the TAP in Run-Test/Idle, is in {chain.state}"
    # TMS sequence of the whole session, exactly
    tms = [t for _, t in chain.trace]
    expect = ([1] * 5 + [0, 1, 0, 0] + [0] * 31 + [1] + [1, 1, 1, 0, 0] + [0] * 31 + [1]
              + [1, 1, 0, 0] + [0] * 1 + [1, 1, 0])
    assert tms == expect, f"TMS sequence differs:\n{tms}\n{expect}"
    # IDCODE scan: TDI quiet (0) ; IR scan shifted ones ; marker is a single 1
    ir_tdi = [v for s, v in chain.tdi_seen if s == "ShIR"]
    assert ir_tdi == [1] * 32
    dr_tdi = [v for s, v in chain.tdi_seen if s == "ShDR"]
    assert dr_tdi[:32] == [0] * 32 and dr_tdi[32:] == [1, 0], dr_tdi[32:]
    dut._log.info(f"TCK min period {chain.min_period * 20} ns, high {chain.min_high * 20} ns, "
                  f"low {chain.min_low * 20} ns")


@cocotb.test()
async def test_jtag_two_device_chain(dut):
    """Two devices (4-bit and 5-bit IR, different IDCODEs): the IDCODE of the one next to TDO comes
    out, both IRs are loaded with BYPASS, and the chain is measured as 2 devices."""
    d0 = Tap(idcode=0x4BA00477, irlen=4, idcode_op=0b0010)
    d1 = Tap(idcode=0x06414041, irlen=5, idcode_op=0b00001)
    chain = JtagChain(dut, [d0, d1])
    got = await run_jtag(dut, chain)
    assert got == jtag_expect(0x06414041, 2), [hex(b) for b in got]
    assert d0.ir == 0b1111 and d1.ir == 0b11111
    assert chain.closed_scans() == [("DR", 32), ("IR", 32), ("DR", 3)], chain.closed_scans()
    assert chain.state == "RTI"


@cocotb.test()
async def test_jtag_longer_chains(dut):
    """Three and five BYPASS devices (mixed IR lengths, 3+4+8 and 2+3+4+5+6 = at most 32 bits)."""
    for irs, n in (((3, 4, 8), 3), ((2, 3, 4, 5, 6), 5)):
        devs = [Tap(idcode=0x10000001 + (i << 12) + (i << 28), irlen=l, idcode_op=1) for i, l in enumerate(irs)]
        chain = JtagChain(dut, devs)
        got = await run_jtag(dut, chain)
        assert got == jtag_expect(devs[-1].idcode, n), [hex(b) for b in got]
        assert all(d.ir == d.bypass_op for d in devs)
        assert chain.closed_scans()[-1] == ("DR", n + 1)


@cocotb.test()
async def test_jtag_dead_tdo(dut):
    """TDO tied low (nothing connected): IDCODE bit 0 is 0 -> status 1; no marker ever returns -> 0xFF."""
    chain = JtagChain(dut, [Tap(0, 4, 2)], tdo_stuck=0)
    got = await run_jtag(dut, chain, settle=3000)
    assert got == [0, 0, 0, 0, 0x01, 0xFF], [hex(b) for b in got]
    assert not chain.errors
