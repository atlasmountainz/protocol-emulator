# SPDX-License-Identifier: Apache-2.0
"""The I2C bus model shared by the I2C target tests (test_i2c_target_regs.py) and the three-protocol test
(test_multi.py): an I2C master that drives SCL (uio[1]) and SDA (uio[0]) through the pins, with the checks on the
target, and a reference model of the 16 registers. See test_i2c_target_regs.py for the details."""
from cocotb.triggers import ClockCycles, RisingEdge

ADDR = 0x2A
SU_DAT = 5          # clocks (100 ns): a bit the chip sends must be stable this long before SCL rises
TVD_FM = 45         # clocks (0.9 us): the Fast-mode limit for SDA valid after SCL low (tVD;DAT, tVD;ACK)
TVD_SM = 172        # clocks (3.45 us): the Standard-mode limit

DEFAULT = None      # schedule 0 1 2 3: one turn = 4 clocks
FASTSCHED = (0, 1, 0, 2)   # thread 0: one turn = 2 clocks


class Timing:
    """Master timings in 20 ns clocks."""

    def __init__(self, name, low, high, hd=8, su_sta=None, hd_sta=None, su_sto=None, buf=None, quick=False):
        self.name, self.low, self.high, self.hd = name, low, high, hd
        self.quick = quick            # slow bus: the long tests use a smaller sample of their cases
        self.su_sta = su_sta if su_sta is not None else high
        self.hd_sta = hd_sta if hd_sta is not None else high
        self.su_sto = su_sto if su_sto is not None else high
        self.buf = buf if buf is not None else low

    def khz(self):
        return 1e6 / ((self.low + self.high) * 20)


# Standard mode, 100 kHz: SCL period 500 clocks. Fast mode, 400 kHz: 125 clocks, with the spec minimums
# (tLOW 1.3 us, tHIGH 0.6 us, tHD;STA / tSU;STA / tSU;STO 0.6 us, tBUF 1.3 us); the long tests use more than
# the minimum for tHIGH (60 clocks) like test_i2c_target.py.
STD = Timing("100k", low=250, high=250, hd=10, quick=True)
FAST = Timing("400k", low=65, high=60, hd=8, su_sta=30, hd_sta=30, su_sto=30, buf=65)


def at_period(period):
    """A master at SCL period `period` clocks, low 52 % / high 48 %, START / STOP timing = half a high time."""
    low = round(period * 0.52)
    high = period - low
    return Timing(f"{1e6 / (period * 20):.0f}k", low=low, high=high, hd=max(1, min(8, low // 8)),
                  su_sta=high // 2, hd_sta=high // 2, su_sto=high // 2, buf=low)


class Regs:
    """Reference model of the target: 16 registers, a pointer, and what uo[7:0] shows."""

    def __init__(self):
        self.mem = [0] * 16
        self.ptr = 0
        self.uo = 0

    def write(self, ptr, data=()):
        if ptr is not None:
            self.ptr = ptr & 15
        for d in data:
            self.mem[self.ptr] = d
            self.ptr = (self.ptr + 1) & 15
            self.uo = d

    def read(self, n):
        out = []
        for _ in range(n):
            out.append(self.mem[self.ptr])
            self.ptr = (self.ptr + 1) & 15
        return out


class Bus:
    """The wire, plus an I2C master and the monitors on the target. Every clock the pad inputs are set."""

    def __init__(self, dut, t):
        self.dut, self.t = dut, t
        self.scl = self.msda = 1
        self.errors = []
        self.cycle = 0
        self.chip_prev = 0
        self.scl_prev = 1
        self.scl_fall = None
        self.vd = []                  # clocks from the SCL fall to each change of SDA by the chip
        self.sda_changes = 0

    def chip_low(self):
        return int(self.dut.uio_oe.value) & 1

    def wire(self):
        return self.msda & (0 if self.chip_low() else 1)

    async def run(self):
        while True:
            await RisingEdge(self.dut.clk)
            self.cycle += 1
            oe = int(self.dut.uio_oe.value)
            if oe & 0xFE:
                self.errors.append(f"chip drives pins other than SDA (clock stretching?): uio_oe={oe:#04x}")
            chip = oe & 1
            if self.scl_prev == 1 and self.scl == 0:
                self.scl_fall = self.cycle
            if chip != self.chip_prev:
                self.sda_changes += 1
                if self.scl_prev and self.scl:
                    self.errors.append(f"clock {self.cycle}: chip changed SDA while SCL was high")
                elif self.scl_fall is not None and not self.scl:
                    self.vd.append(self.cycle - self.scl_fall)
            self.chip_prev, self.scl_prev = chip, self.scl
            self.dut.uio_in.value = (self.scl << 1) | self.wire()

    async def wait(self, n):
        await ClockCycles(self.dut.clk, n)

    async def bit_out(self, bit):
        """The master sends one bit (SCL low on entry and exit). Returns the wire level at the end of SCL high."""
        t = self.t
        await self.wait(t.hd)
        self.msda = bit
        await self.wait(t.low - t.hd)
        self.scl = 1
        await self.wait(t.high - 1)
        w = self.wire()
        await self.wait(1)
        self.scl = 0
        return w

    async def bit_in(self):
        """The master releases SDA and reads one bit. The bit must be valid SU_DAT clocks before SCL rises and
        must not move while SCL is high."""
        t = self.t
        await self.wait(t.hd)
        self.msda = 1
        await self.wait(t.low - t.hd - SU_DAT)
        early = self.wire()
        await self.wait(SU_DAT)
        self.scl = 1
        await self.wait(t.high - 1)
        late = self.wire()
        await self.wait(1)
        self.scl = 0
        if early != late:
            self.errors.append("chip changed SDA late: not valid SU_DAT clocks before SCL rose")
        return late

    async def start(self):
        """START, or a repeated START when SCL is low on entry."""
        t = self.t
        if self.scl == 0:
            await self.wait(t.hd)
            self.msda = 1
            await self.wait(t.low - t.hd)
            self.scl = 1
        self.msda = 1
        await self.wait(t.su_sta)
        self.msda = 0
        await self.wait(t.hd_sta)
        self.scl = 0

    async def stop(self):
        t = self.t
        await self.wait(t.hd)
        self.msda = 0
        await self.wait(t.low - t.hd)
        self.scl = 1
        await self.wait(t.su_sto)
        self.msda = 1
        await self.wait(t.buf)

    async def write_byte(self, b):
        """Send a byte, return True if the chip acknowledged it."""
        for i in range(7, -1, -1):
            bit = (b >> i) & 1
            if await self.bit_out(bit) != bit:
                self.errors.append(f"chip pulled SDA low during bit {i} of {b:#04x}")
        return await self.bit_in() == 0

    async def read_byte(self, ack):
        v = 0
        for _ in range(8):
            v = (v << 1) | await self.bit_in()
        level = 0 if ack else 1
        if await self.bit_out(level) != level:
            self.errors.append("chip held SDA low during the master's ACK/NACK bit")
        return v

    # ---- whole transactions
    async def write(self, ptr, data=(), addr=ADDR, stop=True):
        """START, addr+W, pointer, data bytes. Returns the list of ACKs."""
        await self.start()
        acks = [await self.write_byte(addr << 1)]
        if ptr is not None:
            acks.append(await self.write_byte(ptr))
            for d in data:
                acks.append(await self.write_byte(d))
        if stop:
            await self.stop()
        return acks

    async def read(self, n, addr=ADDR, stop=True):
        """(Repeated) START, addr+R, n bytes with ACK after all but the last. Returns (ack, bytes)."""
        await self.start()
        ack = await self.write_byte((addr << 1) | 1)
        got = []
        if ack:
            for k in range(n):
                got.append(await self.read_byte(ack=(k != n - 1)))
        if stop:
            await self.stop()
        return ack, got

    def check(self, idle=True, tvd=None):
        """No protocol errors so far; with idle=True the chip must also have released the bus; with `tvd` every
        change of SDA by the chip came at most that many clocks after SCL fell."""
        assert not self.errors, "bus errors:\n" + "\n".join(self.errors[:10])
        if idle:
            assert int(self.dut.uio_oe.value) & 0xFF == 0, "chip must release SDA when the bus is idle"
        if tvd is not None and self.vd:
            assert max(self.vd) <= tvd, f"SDA changed {max(self.vd)} clocks ({max(self.vd) * 20} ns) after SCL fell, limit {tvd}"



def pattern(seed):
    """16 different non-zero register values (an odd multiplier makes them distinct) with all kinds of bit patterns."""
    v = [((i * 0x35 + seed * 0x6B + 0x11) & 0xFF) ^ 0xA5 for i in range(16)]
    assert len(set(v)) == 16 and 0 not in v, f"pattern({seed}) has a zero or a repeated value"
    return v


async def write_check(bus, ref, ptr, data, stop=True):
    acks = await bus.write(ptr, data, stop=stop)
    assert acks == [True] * (1 + (ptr is not None) + (len(data) if ptr is not None else 0)), f"ACKs {acks}"
    ref.write(ptr, data)


async def read_check(bus, ref, n, what=""):
    ack, got = await bus.read(n)
    want = ref.read(n)
    assert ack and got == want, f"{what}read {n}: got {[hex(x) for x in got]}, want {[hex(x) for x in want]}"


async def set_pointer_read(bus, ref, ptr, n):
    """write pointer, repeated START, read n bytes."""
    assert await bus.write(ptr, stop=False) == [True, True]
    ref.write(ptr)
    ack, got = await bus.read(n)
    want = ref.read(n)
    assert ack and got == want, f"pointer {ptr:#04x}, read {n}: got {[hex(x) for x in got]}, want {[hex(x) for x in want]}"
