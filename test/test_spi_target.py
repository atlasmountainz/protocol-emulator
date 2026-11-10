# SPDX-License-Identifier: Apache-2.0
"""Tests for firmware/examples/spi_target.asm (docs/examples-spi-target.md).

Run with:  cd test && make COCOTB_TEST_MODULES=test_spi_target

A Python SPI master drives SCK / MOSI / CS_N through ui_in and samples MISO on uo_out[0]; a
bit-level reference model predicts every MISO bit of every frame, so truncated frames (CS_N
rising in the middle of a byte) are checked as well as complete ones.
"""
import os
import random
import sys
from pathlib import Path

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles

from loader import load_program

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
from load_image import build_image  # noqa: E402

# SPI_TARGET_ASM selects another source (used for the mutation check in the doc)
FIRMWARE = Path(os.environ.get("SPI_TARGET_ASM", Path(__file__).resolve().parent.parent
                                / "firmware" / "examples" / "spi_target.asm"))

# Pins (docs/examples-spi-target.md)
SCK, MOSI, CS_N, MODE3 = 0x01, 0x02, 0x04, 0x08    # ui_in bits; MODE3 is not read by the firmware
JEDEC_ID = [0xC2, 0x20, 0x16]
RING = 32                                          # the status counter wraps at 32


def table(i):
    return (i * 7 + 3) & 0xFF


def to_bits(data):
    return [(b >> (7 - k)) & 1 for b in data for k in range(8)]


def to_byte(bits):
    v = 0
    for b in bits:
        v = (v << 1) | b
    return v


def to_bytes(bits):
    return [to_byte(bits[i:i + 8]) for i in range(0, len(bits) - 7, 8)]


class Reference:
    """What the target must answer. `frame(tx_bits)` returns the expected MISO bit for every
    clocked bit and counts the command if its 8 bits were all clocked."""

    def __init__(self):
        self.count = 0

    def frame(self, tx):
        out = [0] * len(tx)             # MISO is low while the command byte is clocked in
        if len(tx) < 8:
            return out
        cmd = to_byte(tx[:8])
        self.count = (self.count + 1) % RING
        for i in range(8, len(tx)):
            j, k = divmod(i, 8)         # response byte j (0 = command byte), bit k
            if cmd == 0x9F:
                byte = JEDEC_ID[j - 1] if j <= 3 else 0xFF
            elif cmd == 0x03:
                # j == 1 is the address byte (MISO stays low); then the table, wrapping at 8
                byte = 0x00 if j == 1 else table(((to_byte(tx[8:16]) & 7) + j - 2) & 7)
            elif cmd == 0x05:
                byte = self.count if j == 1 else 0xFF
            else:
                byte = 0xFF
            out[i] = (byte >> (7 - k)) & 1
        return out


class SpiMaster:
    """SPI master model. Times are in clocks (20 ns at 50 MHz). SCK is high for `hi` clocks and
    low for `lo` clocks per bit. MOSI changes with the SCK falling edge (zero hold) and MISO is
    sampled `miso_setup` clocks before the SCK rising edge. Mode 0 idles SCK low; mode 3 idles
    it high (and, as the mode pin, also drives ui_in[3])."""

    def __init__(self, dut, period=50, mode=0, hi=None, cs_setup=None, cs_hold=None,
                 cs_gap=None, miso_setup=2):
        self.dut, self.mode = dut, mode
        self.hi = period // 2 if hi is None else hi
        self.lo = period - self.hi
        # First-edge setup, last-edge hold and CS_N-high gap: at least a bit time unless given.
        self.cs_setup = period if cs_setup is None else cs_setup
        self.cs_hold = period if cs_hold is None else cs_hold
        self.cs_gap = period if cs_gap is None else cs_gap
        self.miso_setup = miso_setup
        # The firmware polls every 2nd slot (8 clocks): lengthen the CS_N setup time by 0..7 clocks
        # from frame to frame so that every phase of the clock grid against the firmware is hit.
        self.rotate_phase = True
        self.frames = 0
        self.sck, self.mosi, self.cs = (1 if mode == 3 else 0), 0, 1
        self.drive()

    def drive(self):
        self.dut.ui_in.value = (SCK * self.sck | MOSI * self.mosi | CS_N * self.cs
                                | (MODE3 if self.mode == 3 else 0))

    async def wait(self, n):
        if n > 0:
            await ClockCycles(self.dut.clk, n)

    def miso(self):
        return int(self.dut.uo_out.value) & 1

    async def frame(self, tx):
        """Clock the bit list `tx` out with CS_N low; return the sampled MISO bits."""
        rx = []
        setup = self.cs_setup + (self.frames % 8 if self.rotate_phase else 0)
        self.frames += 1
        self.cs = 0
        if self.mode == 0:
            self.mosi = tx[0] if tx else 0
        self.drive()
        if self.mode == 3:
            await self.wait(setup)
        for i, bit in enumerate(tx):
            self.mosi, self.sck = bit, 0     # falling edge (already low in mode 0 for bit 0)
            self.drive()
            low = setup if (i == 0 and self.mode == 0) else self.lo
            await self.wait(low - self.miso_setup)
            rx.append(self.miso())
            await self.wait(self.miso_setup)
            self.sck = 1                     # rising edge: the target samples MOSI, we sample MISO
            self.drive()
            if i < len(tx) - 1 or self.mode == 0:
                await self.wait(self.hi)
        if self.mode == 0:
            self.sck = 0                     # last edge in mode 0 is the falling edge
            self.drive()
        await self.wait(self.cs_hold)
        self.cs, self.mosi = 1, 0
        self.drive()
        await self.wait(self.cs_gap)
        return rx

    async def xfer(self, tx_bytes, total_bytes=None):
        """Full bytes in, full bytes out (tx padded with 0x00 to total_bytes)."""
        total = total_bytes or len(tx_bytes)
        data = list(tx_bytes) + [0x00] * (total - len(tx_bytes))
        return to_bytes(await self.frame(to_bits(data)))


async def boot(dut, **master_args):
    """Reset, load the SPI target into the program memory, release it. Returns the master."""
    cocotb.start_soon(Clock(dut.clk, 20, unit="ns").start())
    dut.ena.value = 1
    dut.ui_in.value = CS_N
    dut.uio_in.value = 0
    dut.rst_n.value = 0
    await ClockCycles(dut.clk, 10)
    dut.rst_n.value = 1
    await load_program(dut, build_image([str(FIRMWARE)]))
    master = SpiMaster(dut, **master_args)           # CS_N high, SCK idle
    dut.uio_in.value = 0                             # LOAD low: the CPU starts
    await ClockCycles(dut.clk, 40)
    return master


async def check(master, ref, tx, what):
    """Clock `tx` (a bit list) in one frame and compare every MISO bit with the model."""
    want = ref.frame(tx)
    got = await master.frame(tx)
    bad = [i for i in range(len(tx)) if got[i] != want[i]]
    assert not bad, (f"{what}: MISO differs at bits {bad[:4]} "
                     f"(got bytes {[hex(b) for b in to_bytes(got)]}, "
                     f"want {[hex(b) for b in to_bytes(want)]})")


async def check_bytes(master, ref, tx_bytes, total, what):
    await check(master, ref, to_bits(list(tx_bytes) + [0] * (total - len(tx_bytes))), what)


async def battery(master, ref):
    """Every command, once or more, in one go (the status counter must stay in step)."""
    await check_bytes(master, ref, [0x9F], 5, "JEDEC ID")
    await check_bytes(master, ref, [0x9F], 9, "JEDEC ID, read on past the ID")
    for addr in (0x00, 0x01, 0x07, 0x0F, 0x10, 0xA3, 0xFE, 0x5C):
        await check_bytes(master, ref, [0x03, addr], 2 + 20, f"READ from {addr:#04x}")
    await check_bytes(master, ref, [0x05], 4, "status")
    await check_bytes(master, ref, [0x05], 2, "status again")
    for cmd in (0x00, 0xFF, 0x9E, 0x1F, 0x83, 0x01, 0x04, 0x06, 0x07, 0x13, 0x02, 0x0B, 0xAB, 0x90):
        await check_bytes(master, ref, [cmd], 4, f"unknown command {cmd:#04x}")
    await check_bytes(master, ref, [0x05], 2, "status after unknown commands")
    await check_bytes(master, ref, [0x03, 0x04], 18, "READ wrapping past entry 7")


@cocotb.test()
async def test_commands_mode0_1mhz(dut):
    """All commands, SPI mode 0, 1 MHz (50 clocks per bit)."""
    master = await boot(dut, period=50, mode=0)
    await battery(master, Reference())


@cocotb.test()
async def test_commands_mode3_1mhz(dut):
    """The same battery in SPI mode 3 (SCK idles high; ui[3] = 1, which the firmware ignores)."""
    master = await boot(dut, period=50, mode=3)
    await battery(master, Reference())


# ---- Timing sweeps ---------------------------------------------------------------------------

# The commands whose first response bit is the tightest: the target has only the time between the
# 8th (or 16th) SCK rising edge and the next rising edge to decide and launch bit 7.
CRITICAL = [([0x9F], 5), ([0x03, 0x00], 5), ([0x03, 0x01], 5), ([0x05], 3), ([0x9E], 3), ([0x04], 3)]


async def sweep(master, ref, reps=8):
    """Each critical command `reps` times in a row: the setup time grows by one clock per frame,
    so the 8 repetitions put the clock edges at all 8 phases of the firmware's 2-slot poll."""
    for tx, total in CRITICAL:
        for rep in range(reps):
            await check_bytes(master, ref, tx, total, f"{tx[0]:#04x} phase {rep}")


async def light_battery(master, ref):
    """A short run through every command, for the slow rates."""
    await check_bytes(master, ref, [0x9F], 6, "JEDEC ID")
    for addr in (0x00, 0x05, 0xFB):
        await check_bytes(master, ref, [0x03, addr], 2 + 10, f"READ from {addr:#04x}")
    await check_bytes(master, ref, [0x05], 3, "status")
    await check_bytes(master, ref, [0xAB], 3, "unknown command")
    await check_bytes(master, ref, [0x05], 2, "status again")


@cocotb.test()
async def test_rates(dut):
    """From 250 kHz up to the limit, both SPI modes. 38 clocks per bit (1.316 MHz) is the rate the
    firmware is specified for, 37 clocks (1.351 MHz) is the last one that works with the master's
    40 ns MISO setup (docs/examples-spi-target.md)."""
    for period in (200, 100, 50, 40, 38, 37):
        for mode in (0, 3):
            master = await boot(dut, period=period, mode=mode)
            ref = Reference()
            if period >= 100:
                await light_battery(master, ref)
            else:
                await sweep(master, ref)
                await light_battery(master, ref)
            dut._log.info(f"mode {mode}, {period} clocks per bit ({50e6 / period / 1e6:.3f} MHz): ok")


@cocotb.test()
async def test_clock_duty_cycle(dut):
    """The limits are on each phase separately: SCK low >= 17 clocks (MISO launch), high >= 16."""
    for hi, lo, reps in ((20, 17, 8), (16, 22, 8), (16, 60, 3), (60, 17, 3)):
        for mode in (0, 3):
            master = await boot(dut, period=hi + lo, hi=hi, mode=mode)
            ref = Reference()
            await sweep(master, ref, reps=reps)
            dut._log.info(f"mode {mode}, SCK high {hi} / low {lo} clocks: ok")


# ---- CS_N framing ----------------------------------------------------------------------------

@cocotb.test()
async def test_cs_reframing(dut):
    """CS_N rising in the middle of a command, an address byte, a response byte, or exactly on a
    byte boundary resets the bit counter: the next frame starts with a fresh command. After each
    truncated frame a status read checks that only commands whose 8 bits were all clocked in
    were counted."""
    for mode in (0, 3):
        master = await boot(dut, period=44, mode=mode)
        ref = Reference()
        frames = {
            "ID": to_bits([0x9F, 0, 0, 0, 0]),
            "READ": to_bits([0x03, 0x05, 0, 0, 0, 0]),
            "status": to_bits([0x05, 0, 0]),
            "unknown": to_bits([0x9E, 0, 0, 0]),
        }
        for name, bits in frames.items():
            cuts = list(range(1, 13)) + [15, 16, 17, 23, 24, 25, 31, 32, 33, 39, 40]
            for n in [c for c in cuts if c < len(bits)]:
                await check(master, ref, bits[:n], f"{name} cut after {n} bits")
                # the bits of this frame must not leak into the next one
                await check_bytes(master, ref, [0x05], 2, f"status after {name} cut at {n}")
        dut._log.info(f"mode {mode}: re-framing ok")


@cocotb.test()
async def test_cs_toggled_without_clocks(dut):
    """Empty frames (CS_N pulses with no SCK) change nothing, including the status counter."""
    for mode in (0, 3):
        master = await boot(dut, period=50, mode=mode)
        ref = Reference()
        for _ in range(5):
            await check(master, ref, [], "empty frame")
        await check_bytes(master, ref, [0x05], 2, "status after empty frames")
        await check_bytes(master, ref, [0x9F], 5, "ID after empty frames")


@cocotb.test()
async def test_minimum_cs_timing(dut):
    """The CS_N limits quoted in the doc: setup 20, hold 12, high 32 clocks (the setup time is
    swept up by 0..7 clocks from frame to frame, the others are exact)."""
    for period in (50, 38):
        for mode in (0, 3):
            master = await boot(dut, period=period, mode=mode, cs_setup=20, cs_hold=12, cs_gap=32)
            ref = Reference()
            await sweep(master, ref, reps=8)
            for cmd in (0x06, 0x9F, 0x03, 0x05, 0xAB):      # command only, CS_N rises at once
                await check_bytes(master, ref, [cmd], 1, f"command {cmd:#04x} alone")
            await check_bytes(master, ref, [0x05], 2, "status counts the command-only frames")
            dut._log.info(f"mode {mode}, {period} clocks per bit, minimum CS_N timing: ok")


@cocotb.test()
async def test_idle_pins(dut):
    """MISO is low whenever CS_N is high; no other output pin is ever driven."""
    master = await boot(dut, period=50, mode=0)
    ref = Reference()
    for tx in ([0x9F], [0x03, 0x02], [0x05]):
        await check_bytes(master, ref, tx, 6, f"{tx[0]:#04x}")
        await ClockCycles(dut.clk, 40)
        assert int(dut.uo_out.value) == 0, f"uo_out = {int(dut.uo_out.value):#04x} while idle"
        assert int(dut.uio_oe.value) == 0, "uio must stay inputs"
        assert int(dut.uio_out.value) == 0


# ---- Random frames ---------------------------------------------------------------------------

@cocotb.test()
async def test_random_frames(dut):
    """Random commands, random truncation points, random CS_N timing, both modes."""
    for mode, period, seed in ((0, 44, 1), (3, 40, 2)):
        rng = random.Random(seed)
        master = await boot(dut, period=period, mode=mode)
        ref = Reference()
        for n in range(60):
            master.cs_setup = rng.randint(20, 70)
            master.cs_hold = rng.randint(12, 70)
            master.cs_gap = rng.randint(32, 90)
            cmd = rng.choice([0x9F, 0x03, 0x05, 0x03, 0x9F, rng.randrange(256), rng.randrange(256)])
            data = [cmd] + [rng.randrange(256) for _ in range(rng.randint(0, 11))]
            if cmd == 0x03 and len(data) > 1:
                data[1] = rng.randrange(256)
            bits = to_bits(data)
            if rng.random() < 0.4:
                bits = bits[:rng.randint(1, len(bits))]
            await check(master, ref, bits, f"random frame {n} ({[hex(b) for b in data]}, {len(bits)} bits)")
        await check_bytes(master, ref, [0x05], 2, "final status")
        dut._log.info(f"mode {mode}: 60 random frames ok")


@cocotb.test()
async def test_status_counter_full_cycle(dut):
    """The status byte takes every value of the counter's ring: 1, 2, ... 31, 0, 1, ... with one
    step per command, whichever command it is. Every ring position is read at least once."""
    master = await boot(dut, period=50, mode=0)
    seen = []
    for n in range(1, 70):
        if n % 3 == 0:
            await master.xfer([0x9F], 2)             # not a status read, but it is a command
            continue
        got = (await master.xfer([0x05], 2))[1]
        assert got == n % RING, f"command {n}: status {got}, want {n % RING}"
        seen.append(got)
    assert set(seen) == set(range(RING)), f"ring positions never read: {set(range(RING)) - set(seen)}"
