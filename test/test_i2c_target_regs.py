# SPDX-License-Identifier: Apache-2.0
"""I2C target with 16 read/write registers in the data memory (firmware/examples/i2c_target_regs.asm,
docs/examples-fast-and-memory.md).

Run with:  cd test && make COCOTB_TEST_MODULES=test_i2c_target_regs

A Python I2C master drives SCL (uio[1]) and SDA (uio[0]) through the pins. This is the bus model of
test_i2c_target.py (SDA is open-drain: the wire is low if the master or the chip pulls it low; the master checks,
for every bit, that the chip leaves SDA alone when it is the master's turn and that data it sends is stable before
SCL rises) with three more checks on the target: it never drives anything but SDA (so it never stretches the
clock), it never changes SDA while SCL is high, and every change of SDA follows the falling edge of SCL within the
Fast-mode limit of 0.9 us (tVD;DAT / tVD;ACK). A reference model of the registers predicts every byte read.
Set I2C_TARGET_REGS_ASM to test a modified copy of the firmware (the mutation test does that in Python).
"""
import os
import random
import sys
from pathlib import Path

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, RisingEdge

from loader import RAM_WORDS, load_program

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import asm  # noqa: E402

FIRMWARE = Path(os.environ.get(
    "I2C_TARGET_REGS_ASM", Path(__file__).resolve().parent.parent / "firmware" / "examples" / "i2c_target_regs.asm"))
from i2c_target_bus import (ADDR, SU_DAT, TVD_FM, TVD_SM, DEFAULT, FASTSCHED, Timing, STD, FAST, at_period,  # noqa: E402,F401
                            Regs, Bus, pattern, write_check, read_check, set_pointer_read)

SPEEDS = [cocotb.Param(value=STD, name="100k"), cocotb.Param(value=FAST, name="400k")]


def firmware_words(text=None):
    words, _ = asm.assemble(FIRMWARE.read_text() if text is None else text)
    assert len(words) <= RAM_WORDS, f"{len(words)} words do not fit the {RAM_WORDS}-word program memory"
    return words + [0xF000] * (RAM_WORDS - len(words))


_CLOCK = None


def start_clock(dut):
    """One 50 MHz clock per test, however many times the chip is booted inside it."""
    global _CLOCK
    if _CLOCK is None or _CLOCK.done():
        _CLOCK = cocotb.start_soon(Clock(dut.clk, 20, unit="ns").start())


async def bring_up(t, sched=DEFAULT, words=None):
    """Reset, load the firmware (and the schedule), release it with an idle bus. Returns the Bus."""
    dut = cocotb.top
    start_clock(dut)
    dut.ena.value = 1
    dut.ui_in.value = 0
    dut.uio_in.value = 0
    dut.rst_n.value = 0
    await ClockCycles(dut.clk, 10)
    dut.rst_n.value = 1
    await ClockCycles(dut.clk, 5)
    await load_program(dut, firmware_words() if words is None else words, sched=sched)
    dut.uio_in.value = 0b11            # LOAD low; SDA and SCL idle high
    await ClockCycles(dut.clk, 6)
    bus = Bus(dut, t)
    bus.task = cocotb.start_soon(bus.run())
    await ClockCycles(dut.clk, 60)
    return bus


# ---------------------------------------------------------------------------------------------------------------

@cocotb.test()
@cocotb.parametrize(speed=SPEEDS)
async def test_registers_start_at_zero(dut, speed):
    """The data memory is cleared by the load: all 16 registers read 0, the pointer is 0, uo[7:0] is 0."""
    bus = await bring_up(speed)
    assert int(dut.uo_out.value) == 0x00
    ack, got = await bus.read(16)
    assert ack and got == [0] * 16, f"got {[hex(x) for x in got]}"
    bus.check(tvd=TVD_FM)


@cocotb.test()
@cocotb.parametrize(speed=SPEEDS)
async def test_write_all_registers_and_read_them_back(dut, speed):
    """One write transaction fills all 16 registers (pointer 0, 16 bytes); then read back as a full sweep, as 16
    single reads (pointer write, repeated START, 1 byte) and as two halves; twice, with different patterns."""
    bus = await bring_up(speed)
    ref = Regs()
    for seed in (1, 2):
        data = pattern(seed)
        assert len(set(data)) == 16 and 0 not in data
        await write_check(bus, ref, 0, data)
        assert int(dut.uo_out.value) == data[-1], "uo shows the last byte written"
        await set_pointer_read(bus, ref, 0, 16)                      # full sweep, one transaction
        for r in ((0, 7, 15) if speed.quick else range(16)):         # single registers
            await set_pointer_read(bus, ref, r, 1)
        await set_pointer_read(bus, ref, 8, 8)                       # upper half
        await set_pointer_read(bus, ref, 0, 8)                       # lower half
        assert ref.mem == data
    bus.check(tvd=TVD_FM)


@cocotb.test()
@cocotb.parametrize(speed=SPEEDS)
async def test_read_from_the_middle_with_wrap(dut, speed):
    """Reads that start in the middle auto-increment and wrap from 15 to 0; the pointer carries on after STOP."""
    bus = await bring_up(speed)
    ref = Regs()
    data = pattern(3)
    await write_check(bus, ref, 0, data)
    await set_pointer_read(bus, ref, 12, 8)          # 12 13 14 15 0 1 2 3
    await read_check(bus, ref, 3, "continued: ")     # 4 5 6 (the pointer survived the STOP)
    await set_pointer_read(bus, ref, 15, 3)          # 15 0 1
    await set_pointer_read(bus, ref, 14, 1)
    await read_check(bus, ref, 1, "next: ")          # 15
    await read_check(bus, ref, 2, "wrapped: ")       # 0 1
    if not speed.quick:
        await set_pointer_read(bus, ref, 5, 35)      # more than two full turns
    bus.check(tvd=TVD_FM)


@cocotb.test()
@cocotb.parametrize(speed=SPEEDS)
async def test_write_wraps_and_only_touches_its_registers(dut, speed):
    """A write that starts at 14 and is 5 bytes long changes registers 14, 15, 0, 1, 2 and no others."""
    bus = await bring_up(speed)
    ref = Regs()
    await write_check(bus, ref, 0, pattern(4))
    await write_check(bus, ref, 14, [0x11, 0x22, 0x33, 0x44, 0x55])
    assert ref.mem[14] == 0x11 and ref.mem[15] == 0x22 and ref.mem[0] == 0x33 and ref.mem[2] == 0x55
    await set_pointer_read(bus, ref, 0, 16)
    assert int(dut.uo_out.value) == 0x55
    # a write continues from where the last one stopped, without a new pointer byte? No: every write starts with
    # its own pointer byte. START + addr+W + STOP is a probe and changes nothing.
    assert await bus.write(None) == [True]
    await set_pointer_read(bus, ref, 3, 2)
    bus.check(tvd=TVD_FM)


@cocotb.test()
@cocotb.parametrize(speed=SPEEDS)
async def test_pointer_semantics(dut, speed):
    """Only the low nibble of the pointer byte counts; a write that stops after the pointer byte just moves the
    pointer; the pointer is left after the last byte written; reads start where the last transaction ended."""
    bus = await bring_up(speed)
    ref = Regs()
    await write_check(bus, ref, 0, pattern(5))
    for p in ((0xF3, 0x80) if speed.quick else (0xF3, 0x80, 0x7C, 0x1F, 0xFF)):
        await set_pointer_read(bus, ref, p, 2)
    await write_check(bus, ref, 6, [])                       # pointer only
    await read_check(bus, ref, 2, "after pointer-only write: ")
    await write_check(bus, ref, 9, [0xC3, 0x3C])             # pointer 9 -> 11 after two bytes
    await read_check(bus, ref, 2, "after data bytes: ")
    await write_check(bus, ref, 0x2D, [0x99])                # high nibble ignored: register 13
    assert ref.mem[13] == 0x99
    await set_pointer_read(bus, ref, 13, 1)
    bus.check(tvd=TVD_FM)


@cocotb.test()
@cocotb.parametrize(speed=SPEEDS)
async def test_repeated_start_in_every_place(dut, speed):
    """Repeated START after the address, after the pointer, after data bytes, between a read and a write."""
    bus = await bring_up(speed)
    ref = Regs()
    await write_check(bus, ref, 0, pattern(15))
    # write ptr, data, repeated START, write ptr, data, repeated START, read
    assert await bus.write(2, [0xA1, 0xA2], stop=False) == [True] * 4
    ref.write(2, [0xA1, 0xA2])
    assert await bus.write(10, [0xB1], stop=False) == [True] * 3
    ref.write(10, [0xB1])
    await read_check(bus, ref, 3, "read after repeated START: ")
    # repeated START straight after the address (no pointer byte), then a normal write
    await bus.start()
    assert await bus.write_byte(ADDR << 1) is True
    assert await bus.write(4, [0xC7], stop=False) == [True] * 3
    ref.write(4, [0xC7])
    await read_check(bus, ref, 2, "after address-only START: ")
    # read, repeated START, read (continues), then write
    ack, got = await bus.read(1, stop=False)
    assert ack and got == ref.read(1)
    ack, got = await bus.read(2, stop=False)
    assert ack and got == ref.read(2)
    assert await bus.write(1, [0xD9]) == [True] * 3
    ref.write(1, [0xD9])
    await set_pointer_read(bus, ref, 0, 16)
    bus.check(tvd=TVD_FM)


@cocotb.test()
@cocotb.parametrize(speed=SPEEDS)
async def test_wrong_address_is_not_acknowledged(dut, speed):
    """Every address that differs from 0x2A in any bit, read or write, gets a NACK and no reaction."""
    bus = await bring_up(speed)
    ref = Regs()
    await write_check(bus, ref, 0, pattern(7))
    others = [0x00, 0x7F, 0x2B, 0x29, 0x28, 0x2E, 0x22, 0x3A, 0x0A, 0x6A, 0x50, 0x15]
    others += [ADDR ^ (1 << b) for b in range(7)]
    if speed.quick:
        others = others[::4]
    await set_pointer_read(bus, ref, 5, 1)          # pointer is 6 afterwards
    for a in others:
        assert a != ADDR
        assert await bus.write(None, addr=a) == [False], f"address {a:#04x} (write) was acknowledged"
        ack, got = await bus.read(1, addr=a)
        assert not ack and got == [], f"address {a:#04x} (read) was acknowledged"
        bus.check()
    # the registers and the pointer are untouched
    await read_check(bus, ref, 2, "after wrong addresses: ")
    await set_pointer_read(bus, ref, 0, 16)
    bus.check(tvd=TVD_FM)


@cocotb.test()
@cocotb.parametrize(speed=SPEEDS)
async def test_other_devices_traffic_is_ignored(dut, speed):
    """Bytes that look like our address or a pointer inside another device's transaction: no reaction."""
    bus = await bring_up(speed)
    ref = Regs()
    await write_check(bus, ref, 0, pattern(8))
    await set_pointer_read(bus, ref, 4, 1)           # pointer 5 now
    await bus.start()
    assert await bus.write_byte(0x50 << 1) is False
    for b in (ADDR << 1, (ADDR << 1) | 1, 0x54, 0x55, 0x03, 0xFF):
        assert await bus.write_byte(b) is False      # nobody there
    await bus.start()                                # repeated START towards another address
    assert await bus.write_byte(0x51 << 1 | 1) is False
    await bus.stop()
    await read_check(bus, ref, 2, "after another device's traffic: ")
    assert int(dut.uo_out.value) == pattern(8)[15]
    bus.check(tvd=TVD_FM)


@cocotb.test()
@cocotb.parametrize(speed=SPEEDS)
async def test_nack_ends_read_and_bus_is_released(dut, speed):
    """After the master's NACK the target stops driving SDA (so a STOP or START can follow) whatever the next bit
    would have been, and the pointer has moved on. Registers whose value ends in a 0 bit are the hard case."""
    bus = await bring_up(speed)
    ref = Regs()
    await write_check(bus, ref, 0, [0x00, 0xFE, 0x80, 0x01, 0xFF] + pattern(9)[5:])
    for p in (0, 1, 2, 3, 4):
        await set_pointer_read(bus, ref, p, 1)
        bus.check()
        await read_check(bus, ref, 1, "the NACKed byte moved the pointer: ")
    bus.check(tvd=TVD_FM)


@cocotb.test()
@cocotb.parametrize(speed=SPEEDS)
async def test_back_to_back_transactions_and_uo(dut, speed):
    """Transactions of every kind one after the other with only the minimum bus-free time in between; uo[7:0]
    always shows the last byte written (and reads do not change it)."""
    bus = await bring_up(speed)
    ref = Regs()
    assert int(dut.uo_out.value) == 0
    await read_check(bus, ref, 2, "first: ")
    await write_check(bus, ref, 9, [0x11])
    assert int(dut.uo_out.value) == 0x11
    await read_check(bus, ref, 1)
    assert int(dut.uo_out.value) == 0x11, "reads must not change uo"
    await set_pointer_read(bus, ref, 1, 2)
    assert await bus.write(None) == [True]
    await read_check(bus, ref, 1, "after a probe: ")
    assert await bus.write(14, [0x22, 0x33], stop=False) == [True] * 4
    ref.write(14, [0x22, 0x33])
    await write_check(bus, ref, 7, [0x44])
    assert int(dut.uo_out.value) == 0x44
    for d in (0x01, 0x80, 0x5A, 0xA5, 0xFE, 0x7F, 0x00, 0xFF):      # every bit-pattern edge reaches uo and memory
        await write_check(bus, ref, 0, [d])
        assert int(dut.uo_out.value) == d, f"wrote {d:#04x}, uo shows {int(dut.uo_out.value):#04x}"
        await set_pointer_read(bus, ref, 0, 1)
    bus.check(tvd=TVD_FM)


@cocotb.test()
async def test_random_transactions(dut):
    """A few hundred random bytes in random transactions at 400 kHz against the reference model."""
    rng = random.Random(11)
    bus = await bring_up(FAST)
    ref = Regs()
    for n in range(40):
        kind = rng.choice(["w", "w", "r", "pr", "pr", "probe", "bad"])
        if kind == "w":
            ptr = rng.randrange(256)
            await write_check(bus, ref, ptr, [rng.randrange(256) for _ in range(rng.randint(0, 6))])
        elif kind == "r":
            await read_check(bus, ref, rng.randint(1, 20), f"op {n}: ")
        elif kind == "pr":
            await set_pointer_read(bus, ref, rng.randrange(256), rng.randint(1, 20))
        elif kind == "probe":
            assert await bus.write(None) == [True]
        else:
            a = rng.choice([x for x in range(128) if x != ADDR])
            assert await bus.write(None, addr=a) == [False]
        assert int(dut.uo_out.value) == ref.uo, f"op {n}: uo {int(dut.uo_out.value):#04x}, model {ref.uo:#04x}"
    await set_pointer_read(bus, ref, 0, 16)
    bus.check(tvd=TVD_FM)


@cocotb.test()
async def test_fast_sda_changes_after_scl_falls(dut):
    """A master that changes SDA only 1 clock (20 ns) after SCL falls must not look like START / STOP."""
    bus = await bring_up(Timing("400k-hd1", low=65, high=60, hd=1, su_sta=30, hd_sta=30, su_sto=30, buf=65))
    ref = Regs()
    for ptr, data in ((0x05, [0x00, 0xFF, 0x00, 0x7F, 0x80]), (0x0A, [0xFF, 0x00, 0xFF, 0x80, 0x01])):
        await write_check(bus, ref, ptr, data)
        await set_pointer_read(bus, ref, ptr, 5)
    bus.check(tvd=TVD_FM)


@cocotb.test()
@cocotb.parametrize(speed=SPEEDS)
async def test_target_timing_and_no_clock_stretching(dut, speed):
    """The chip never drives SCL (no clock stretching at 100 or 400 kHz), changes SDA only while SCL is low, and
    each change comes at most 0.9 us after SCL fell. Reports the measured worst case."""
    bus = await bring_up(speed)
    ref = Regs()
    await write_check(bus, ref, 0, pattern(10))
    await set_pointer_read(bus, ref, 0, 16)
    await write_check(bus, ref, 5, [0xFF, 0x00])
    await set_pointer_read(bus, ref, 5, 4)
    bus.check(tvd=TVD_FM)
    assert bus.sda_changes > 50 and bus.vd, "the monitor saw no SDA changes"
    dut._log.info(f"{speed.name}: SDA changes {bus.sda_changes}; SCL fall to SDA change: "
                  f"min {min(bus.vd)}, max {max(bus.vd)} clocks ({max(bus.vd) * 20} ns), limit {TVD_FM} (900 ns)")


@cocotb.test()
async def test_fast_schedule_400k_and_1mhz(dut):
    """The program does not depend on the schedule: with 0,1,0,2 (twice as fast) it still works at 400 kHz, and
    now also at 1 MHz (Fast-mode Plus speed; the master's START / STOP times are scaled down with it, see the doc)."""
    for t in (FAST, at_period(50)):
        bus = await bring_up(t, sched=FASTSCHED)
        ref = Regs()
        await write_check(bus, ref, 0, pattern(11))
        await set_pointer_read(bus, ref, 0, 16)
        await set_pointer_read(bus, ref, 11, 7)
        await write_check(bus, ref, 14, [1, 2, 3])
        assert await bus.write(None, addr=0x2B) == [False]
        await set_pointer_read(bus, ref, 12, 6)
        bus.check(tvd=TVD_FM)
        bus.task.cancel()


SWEEP = {
    "default": (DEFAULT, [125, 100, 90, 85, 80, 75, 70, 65, 60]),
    "fast": (FASTSCHED, [125, 60, 50, 45, 40, 35, 30, 25]),
}


async def sweep_ops(bus, ref):
    await write_check(bus, ref, 2, [0xA5, 0x5A, 0xFF, 0x00, 0x81])
    await set_pointer_read(bus, ref, 0, 8)
    assert await bus.write(None, addr=0x2B) == [False]
    await set_pointer_read(bus, ref, 14, 4)
    assert await bus.write(1, [0x3C], stop=False) == [True] * 3
    ref.write(1, [0x3C])
    await read_check(bus, ref, 3, "after repeated START: ")
    assert int(bus.dut.uo_out.value) == ref.uo


async def try_period(sched, period):
    """Run the sweep operations at SCL period `period` clocks. Returns None if everything passed, else why not."""
    t = at_period(period)
    bus = await bring_up(t, sched=sched)
    try:
        await sweep_ops(bus, Regs())
        bus.check()
        return None, max(bus.vd)
    except (AssertionError, IndexError, ValueError) as e:
        return str(e).splitlines()[0][:90], max(bus.vd) if bus.vd else None
    finally:
        bus.task.cancel()


# Slowest "must work" SCL period (clocks) and a period far beyond the limit that must fail, per schedule. The limits
# come from the START detection (up to 3 polls, then a re-check, all before the master pulls SCL low again: tHD;STA
# is half of SCL high here) and from the time to react to SCL falling and rising.
SWEEP_LIMITS = {"default": (70, 60), "fast": (35, 25)}


@cocotb.test()
async def test_scl_rate_sweep(dut):
    """How fast an SCL does the program follow? Logs the result for every rate and asserts the rates quoted in the
    doc: the default schedule works up to a 70-clock SCL period (714 kHz) and not at 60 (833 kHz); the fast schedule
    0,1,0,2 works up to 35 clocks (1.43 MHz) and not at 25 (2 MHz). The master's timings scale with the period."""
    for name, (sched, periods) in SWEEP.items():
        ok_limit, bad = SWEEP_LIMITS[name]
        for period in periods:
            why, vd = await try_period(sched, period)
            t = at_period(period)
            dut._log.info(f"sweep {name} schedule, SCL period {period} clocks ({t.khz():.0f} kHz, low {t.low}, high {t.high}): "
                          f"{'PASS' if why is None else 'FAIL ' + why}, max SCL-fall-to-SDA {vd} clocks")
            if period >= ok_limit:
                assert why is None, f"{name} schedule must work at {t.khz():.0f} kHz: {why}"
            if period == bad:
                assert why is not None, f"{name} schedule unexpectedly works at {t.khz():.0f} kHz: update the doc"


@cocotb.test()
async def test_start_time_is_the_limit_of_the_default_schedule(dut):
    """At 65 and 60 clocks per SCL period the sweep above fails only because its START / STOP times are short (half
    a high time: 15 clocks, 300 ns). With the 0.6 us (30 clocks) that Fast-mode asks for, the default schedule still
    follows 769 and 833 kHz; at 55 clocks (909 kHz) it fails whatever the START time. So the limit of the default
    schedule is the time the program needs to see a (repeated) START, then the SCL period itself."""
    for period, sta, works in ((65, 30, True), (65, 14, False), (60, 30, True), (60, 14, False), (55, 30, False)):
        base = at_period(period)
        t = Timing("x", low=base.low, high=base.high, hd=base.hd, su_sta=sta, hd_sta=sta, su_sto=sta, buf=base.low)
        bus = await bring_up(t)
        try:
            await sweep_ops(bus, Regs())
            bus.check()
            why = None
        except (AssertionError, IndexError, ValueError) as e:
            why = str(e).splitlines()[0][:80]
        finally:
            bus.task.cancel()
        dut._log.info(f"default schedule, period {period} clocks, START / STOP time {sta} clocks: "
                      f"{'PASS' if why is None else 'FAIL ' + why}")
        assert (why is None) == works, f"period {period}, START time {sta}: expected {'pass' if works else 'a failure'}, got {why}"


async def exercise(bus, ref, tvd=TVD_FM):
    """A compact run through every feature, used by the mutation test."""
    await write_check(bus, ref, 0, pattern(12))
    await set_pointer_read(bus, ref, 0, 16)
    await set_pointer_read(bus, ref, 13, 5)
    await read_check(bus, ref, 2, "continued: ")
    await write_check(bus, ref, 14, [0x81, 0x42, 0x24])
    assert await bus.write(3, [0x5A], stop=False) == [True] * 3
    ref.write(3, [0x5A])
    await read_check(bus, ref, 2, "after a repeated START: ")
    assert await bus.write(None, addr=0x2B) == [False]
    assert await bus.write(None, addr=0x6A) == [False]
    await write_check(bus, ref, 0xF2, [0xE7])
    assert int(bus.dut.uo_out.value) == ref.uo, f"uo {int(bus.dut.uo_out.value):#04x}, model {ref.uo:#04x}"
    await set_pointer_read(bus, ref, 0, 16)
    ack, got = await bus.read(1)                      # NACK at the end of a byte that ends in a 0 bit
    assert ack and got == ref.read(1)
    await read_check(bus, ref, 1, "after a NACKed byte: ")
    bus.check(tvd=tvd)


# ---- mutation checks: each change keeps the program 'nearly right' and must be caught -------------------------

def mutate(text, old, new):
    assert text.count(old) == 1, f"mutation pattern {old!r} found {text.count(old)} times"
    return text.replace(old, new)


MUTATIONS = {
    "wrong address bit (0x56 instead of 0x54)":
        lambda t: mutate(t, "XOR  0x54", "XOR  0x56"),
    "write goes to register 0 whatever the pointer (STM instead of STMX)":
        lambda t: mutate(t, "STMX 0                ; reg[pointer] = byte", "STM  0"),
    "write stores one register too high (STMX 1)":
        lambda t: mutate(t, "STMX 0                ; reg[pointer] = byte", "STMX 1"),
    "pointer advances by 2 after a written byte":
        lambda t: mutate(t, "XADD 1                ; pointer + 1", "XADD 2"),
    "pointer does not advance after the first byte of a read":
        lambda t: mutate(t, "        LDMX 0                ; A = first register\n        XADD 1\n",
                         "        LDMX 0                ; A = first register\n"),
    "read of the 2nd and later bytes repeats the register (no XADD in the ACK path)":
        lambda t: mutate(t, "        LDMX 0                ; ACK: the next register, loaded while the ACK clock finishes\n        XADD 1\n",
                         "        LDMX 0                ; ACK\n"),
    "pointer byte ignored (TAX removed)":
        lambda t: mutate(t, "        TAX                   ; X = pointer (the memory only looks at its low 4 bits)\n", ""),
    "read sends LSB first (SHR instead of SHL in txbit)":
        lambda t: mutate(t, "        SHL                   ; C = next bit\n        ODRVC 0", "        SHR\n        ODRVC 0"),
    "SDA not released for the master's ACK/NACK after a read byte":
        lambda t: mutate(t, "        ODRV 0 1              ; release SDA: the master's ACK / NACK\n", ""),
    "NACK from the master not recognised (read goes on)":
        lambda t: mutate(t, "        JPIN 8 1 idle         ; SDA high = NACK", "        NOP                   ; SDA high = NACK"),
    "repeated START not recognised after a data byte":
        lambda t: mutate(t, "        JMP  a_phase          ; (repeated) START", "        JMP  idle"),
    "STOP not recognised after a data byte (target stays in the write loop)":
        lambda t: mutate(t, "        JPIN 8 1 r7_stp       ; SDA rose", "        NOP                   ; SDA rose"),
    "write data byte is not acknowledged":
        lambda t: mutate(t, "        OUTA                  ; uo[7:0] = byte\n        CALL ack\n", "        OUTA\n        CALL ack_none\n")
        + "\nack_none:\n        JPIN 9 1 ack_none\nan_h:\n        JPIN 9 0 an_h\nan_l:\n        JPIN 9 1 an_l\n        RET\n",
    "ACK delayed by 21 turns (84 clocks at the default schedule: fine at 100 kHz, too late at 400 kHz)":
        lambda t: mutate(t, "ack:\n        JPIN 9 1 ack\n        ODRV 0 0\n", "ack:\n        JPIN 9 1 ack\n        WAIT 20\n        ODRV 0 0\n"),
    "uo shows the next register instead of the byte just written (LDMX before OUTA)":
        lambda t: mutate(t, "        OUTA                  ; uo[7:0] = byte\n",
                         "        LDMX 0\n        OUTA\n"),
}


@cocotb.test()
async def test_mutations_are_caught(dut):
    """The original passes the compact run at 400 kHz; every mutant must fail it (the 'slow ACK' one passes at
    100 kHz and fails at 400 kHz)."""
    text = FIRMWARE.read_text()
    bus = await bring_up(FAST, words=firmware_words(text))
    await exercise(bus, Regs())
    bus.task.cancel()
    survivors = []
    for name, f in MUTATIONS.items():
        mutated = f(text)
        bus = await bring_up(FAST, words=firmware_words(mutated))
        try:
            await exercise(bus, Regs())
        except (AssertionError, IndexError, ValueError) as e:
            dut._log.info(f"caught: {name}: {str(e).splitlines()[0][:100]}")
        else:
            survivors.append(name)
        finally:
            bus.task.cancel()
    assert not survivors, f"mutants that passed all checks: {survivors}"
    # the slow ACK is legal at 100 kHz: the same mutant passes there (so the 400 kHz run is what caught it)
    slow = [f for n, f in MUTATIONS.items() if n.startswith("ACK delayed")][0](text)
    bus = await bring_up(STD, words=firmware_words(slow))
    await exercise(bus, Regs(), tvd=TVD_SM)
    bus.task.cancel()
