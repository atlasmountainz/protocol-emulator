# SPDX-License-Identifier: Apache-2.0
"""I2C target example (firmware/examples/i2c_target.asm, docs/examples-i2c-target.md).

Run with:  cd test && make COCOTB_TEST_MODULES=test_i2c_target
(the firmware is about 180 words, so it needs the 256-word program memory)

A Python I2C master drives SCL (uio[1]) and SDA (uio[0]) through the pins. SDA is open-drain: the wire is low
if the master or the chip (uio_oe[0]) pulls it low. The master checks, for every bit, that the chip leaves SDA
alone when it is the master's turn and that data it sends is stable before SCL rises. Set I2C_TARGET_ASM to
test a modified copy of the firmware (used for the mutation check).
"""
import os
import sys
from pathlib import Path

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, RisingEdge

from loader import RAM_WORDS, load_program

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import asm  # noqa: E402

FIRMWARE = Path(os.environ.get(
    "I2C_TARGET_ASM", Path(__file__).resolve().parent.parent / "firmware" / "examples" / "i2c_target.asm"))
ADDR = 0x2A
SU_DAT = 5          # clocks (100 ns): a bit the chip sends must be stable this long before SCL rises


def reg(i):
    """Register file: reg[i] = i XOR 0x5A (read only)."""
    return (i & 15) ^ 0x5A


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
# (tLOW 1.3 us, tHIGH 0.6 us, tHD;STA / tSU;STA / tSU;STO 0.6 us, tBUF 1.3 us).
STD = Timing("100k", low=250, high=250, hd=10, quick=True)
FAST = Timing("400k", low=65, high=60, hd=8, su_sta=30, hd_sta=30, su_sto=30, buf=65)
SPEEDS = [cocotb.Param(value=STD, name="100k"), cocotb.Param(value=FAST, name="400k")]


class Bus:
    """The wire, plus an I2C master. Every clock, the pad inputs are set from the master and the chip."""

    def __init__(self, dut, t):
        self.dut, self.t = dut, t
        self.scl = self.msda = 1
        self.errors = []
        self.chip_low_seen = 0

    def chip_low(self):
        return int(self.dut.uio_oe.value) & 1

    def wire(self):
        return self.msda & (0 if self.chip_low() else 1)

    async def run(self):
        while True:
            await RisingEdge(self.dut.clk)
            oe = int(self.dut.uio_oe.value)
            if oe & 0xFE:
                self.errors.append(f"chip drives pins other than SDA: uio_oe={oe:#04x}")
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

    def check(self, idle=True):
        """No protocol errors so far; with idle=True the chip must also have released the bus."""
        assert not self.errors, "bus errors:\n" + "\n".join(self.errors[:10])
        if idle:
            assert int(self.dut.uio_oe.value) & 0xFF == 0, "chip must release SDA when the bus is idle"


def firmware_words():
    words, _ = asm.assemble(FIRMWARE.read_text())
    assert len(words) <= RAM_WORDS, f"{len(words)} words do not fit the {RAM_WORDS}-word program memory"
    return words + [0xF000] * (RAM_WORDS - len(words))


async def bring_up(t):
    """Reset, load the firmware, release it with an idle bus. Returns the Bus."""
    dut = cocotb.top
    cocotb.start_soon(Clock(dut.clk, 20, unit="ns").start())
    dut.ena.value = 1
    dut.ui_in.value = 0
    dut.uio_in.value = 0
    dut.rst_n.value = 0
    await ClockCycles(dut.clk, 10)
    dut.rst_n.value = 1
    await ClockCycles(dut.clk, 5)
    await load_program(dut, firmware_words())
    dut.uio_in.value = 0b11            # LOAD low; SDA and SCL idle high
    await ClockCycles(dut.clk, 6)
    bus = Bus(dut, t)
    cocotb.start_soon(bus.run())
    await ClockCycles(dut.clk, 60)
    return bus


def uo(bus):
    return int(bus.dut.uo_out.value)


@cocotb.test()
@cocotb.parametrize(speed=SPEEDS)
async def test_pointer_write_then_repeated_start_read(dut, speed):
    """START, addr+W, pointer, repeated START, addr+R, four bytes, NACK on the last, STOP."""
    bus = await bring_up(speed)
    assert await bus.write(3, stop=False) == [True, True]
    ack, got = await bus.read(4)
    assert ack and got == [reg(3), reg(4), reg(5), reg(6)], f"got {[hex(x) for x in got]}"
    bus.check()


@cocotb.test()
@cocotb.parametrize(speed=SPEEDS)
async def test_every_pointer_value(dut, speed):
    """All 16 pointer values select the right register (the pointer tree), the high nibble is ignored."""
    bus = await bring_up(speed)
    for p in ((0, 5, 10, 15) if speed.quick else range(16)):
        assert await bus.write(p, stop=False) == [True, True]
        ack, got = await bus.read(1)
        assert ack and got == [reg(p)], f"pointer {p}: got {[hex(x) for x in got]}"
    for p in ((0xF3,) if speed.quick else (0xF3, 0x80, 0x7C)):
        assert await bus.write(p, stop=False) == [True, True]
        ack, got = await bus.read(1)
        assert got == [reg(p & 15)], f"pointer byte {p:#04x}: got {[hex(x) for x in got]}"
    bus.check()


@cocotb.test()
@cocotb.parametrize(speed=SPEEDS)
async def test_read_auto_increment_and_wrap(dut, speed):
    """ACKed reads walk through the registers, 15 wraps to 0; the last (NACKed) byte also moves the pointer."""
    bus = await bring_up(speed)
    first, n = (12, 8) if speed.quick else (0, 20)
    assert await bus.write(first, stop=False) == [True, True]
    ack, got = await bus.read(n)
    assert ack and got == [reg(first + i) for i in range(n)], f"got {[hex(x) for x in got]}"
    # the pointer survived the STOP: read on without writing a pointer
    nxt = (first + n) & 15
    ack, got = await bus.read(3)
    assert ack and got == [reg(nxt + i) for i in range(3)], f"got {[hex(x) for x in got]}"
    bus.check()


@cocotb.test()
@cocotb.parametrize(speed=SPEEDS)
async def test_wrong_address_is_not_acknowledged(dut, speed):
    """Every address that differs from 0x2A in any bit, read or write, gets a NACK and no reaction."""
    bus = await bring_up(speed)
    others = [0x00, 0x7F, 0x2B, 0x29, 0x28, 0x2E, 0x22, 0x3A, 0x0A, 0x6A, 0x50, 0x15]
    others += [ADDR ^ (1 << b) for b in range(7)]
    if speed.quick:
        others = others[::4]
    assert await bus.write(5, stop=False) == [True, True]
    ack, got = await bus.read(1)          # pointer is 6 afterwards
    assert got == [reg(5)]
    for a in others:
        assert a != ADDR
        assert await bus.write(None, addr=a) == [False], f"address {a:#04x} (write) was acknowledged"
        ack, got = await bus.read(1, addr=a)
        assert not ack and got == [], f"address {a:#04x} (read) was acknowledged"
        bus.check()
    # still alive, pointer untouched
    ack, got = await bus.read(2)
    assert ack and got == [reg(6), reg(7)], f"got {[hex(x) for x in got]}"
    bus.check()


@cocotb.test()
@cocotb.parametrize(speed=SPEEDS)
async def test_write_data_goes_to_outputs_and_moves_pointer(dut, speed):
    """Written data bytes appear on uo[7:0] (the last one stays), are ACKed, and advance the pointer."""
    bus = await bring_up(speed)
    assert uo(bus) == 0x00, "uo must be 0 after reset"
    data = [0xA5, 0x3C, 0xFF, 0x00, 0x81]
    assert await bus.write(2, data) == [True] * (2 + len(data))
    assert uo(bus) == 0x81, f"uo shows {uo(bus):#04x}, last byte written was 0x81"
    # pointer was 2, five bytes written: 7. Read from there without a pointer write.
    ack, got = await bus.read(2)
    assert ack and got == [reg(7), reg(8)], f"got {[hex(x) for x in got]}"
    assert uo(bus) == 0x81, "reads must not change uo"
    for d in (0x01, 0x80, 0x5A, 0xA5, 0xFE, 0x7F):      # every bit pattern edge
        assert await bus.write(0, [d]) == [True, True, True]
        assert uo(bus) == d, f"wrote {d:#04x}, uo shows {uo(bus):#04x}"
    bus.check()


@cocotb.test()
@cocotb.parametrize(speed=SPEEDS)
async def test_back_to_back_transactions(dut, speed):
    """Transactions of every kind one after the other, with only the minimum bus-free time in between."""
    bus = await bring_up(speed)
    # after reset the pointer is 0
    ack, got = await bus.read(2)
    assert ack and got == [reg(0), reg(1)]
    assert await bus.write(9, [0x11]) == [True] * 3          # pointer 9 -> 10 after the data byte
    ack, got = await bus.read(1)
    assert ack and got == [reg(10)]
    assert await bus.write(1, stop=False) == [True, True]    # write + repeated START + read + STOP
    ack, got = await bus.read(2)
    assert ack and got == [reg(1), reg(2)]
    assert await bus.write(None) == [True]                   # address probe: START, addr+W, STOP
    ack, got = await bus.read(1)
    assert ack and got == [reg(3)], "an address probe must not change the pointer"
    assert await bus.write(14, [0x22, 0x33], stop=False) == [True] * 4    # write, repeated START, write
    assert await bus.write(7, [0x44]) == [True] * 3
    assert uo(bus) == 0x44
    ack, got = await bus.read(1)
    assert ack and got == [reg(8)]
    ack, got = await bus.read(1)                              # read again right away
    assert ack and got == [reg(9)]
    bus.check()


@cocotb.test()
@cocotb.parametrize(speed=SPEEDS)
async def test_nack_ends_read_and_bus_is_released(dut, speed):
    """After the master's NACK the target stops driving SDA (so a STOP or START can follow) whatever the next
    bit would have been, and the pointer has moved on."""
    bus = await bring_up(speed)
    for p in (0x00, 0x0E):             # every register value starts with a 0 bit: a target still driving would block the STOP
        assert await bus.write(p, stop=False) == [True, True]
        ack, got = await bus.read(1)
        assert ack and got == [reg(p)]
        bus.check()
        ack, got = await bus.read(1)
        assert ack and got == [reg(p + 1)], "the NACKed byte still advances the pointer"
    bus.check()


@cocotb.test()
@cocotb.parametrize(speed=SPEEDS)
async def test_other_devices_traffic_is_ignored(dut, speed):
    """Bytes that look like our address inside another device's transaction, and other STARTs: no reaction."""
    bus = await bring_up(speed)
    assert await bus.write(4, stop=False) == [True, True]
    ack, got = await bus.read(1)
    assert got == [reg(4)]                       # pointer 5 now
    await bus.start()
    assert await bus.write_byte(0x50 << 1) is False
    for b in (ADDR << 1, (ADDR << 1) | 1, 0x54, 0x55):
        assert await bus.write_byte(b) is False   # nobody there; bytes equal to our address+R/W
    await bus.start()                             # repeated START towards another address
    assert await bus.write_byte(0x51 << 1 | 1) is False
    await bus.stop()
    ack, got = await bus.read(2)
    assert ack and got == [reg(5), reg(6)], f"got {[hex(x) for x in got]}"
    assert uo(bus) == 0x00
    bus.check()


@cocotb.test()
async def test_fast_sda_changes_after_scl_falls(dut):
    """A master that changes SDA only 1 clock (20 ns) after SCL falls must not look like START / STOP."""
    bus = await bring_up(Timing("400k-hd1", low=65, high=60, hd=1, su_sta=30, hd_sta=30, su_sto=30, buf=65))
    for ptr, data in ((0x05, [0x00, 0xFF, 0x00, 0x7F, 0x80]), (0x0A, [0xFF, 0x00, 0xFF, 0x80, 0x01])):
        assert await bus.write(ptr, data) == [True] * (2 + len(data))
        assert uo(bus) == data[-1]
        assert await bus.write(ptr, stop=False) == [True, True]
        ack, got = await bus.read(5)
        assert ack and got == [reg(ptr + i) for i in range(5)]
    bus.check()


@cocotb.test()
async def test_speed_margin_588khz(dut):
    """Margin over Fast mode: SCL period 85 clocks (588 kHz, 1.5x of 400 kHz) still works. The limit is about
    660 kHz: a data byte needs 16 slots (64 clocks) for the ACK and the uo[7:0] update. Not an I2C mode."""
    bus = await bring_up(Timing("588k", low=45, high=40, hd=6, su_sta=25, hd_sta=25, su_sto=25, buf=45))
    assert await bus.write(5, [0xA5, 0x3C, 0xC3]) == [True] * 5
    assert uo(bus) == 0xC3
    assert await bus.write(2, stop=False) == [True, True]
    ack, got = await bus.read(10)
    assert ack and got == [reg(2 + i) for i in range(10)], f"got {[hex(x) for x in got]}"
    assert await bus.write(None, addr=0x2B) == [False]
    bus.check()
