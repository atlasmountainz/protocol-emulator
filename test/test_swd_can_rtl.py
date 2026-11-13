# SPDX-License-Identifier: Apache-2.0
"""The SWD host (firmware/examples/swd.asm) and the CAN receiver (firmware/examples/can_rx.asm) on the chip: RTL,
and the gate-level netlist in CI.

Run with:  cd test && make COCOTB_TEST_MODULES=test_swd_can_rtl

Pins only, so it also runs on the gate-level netlist. Each program is loaded over the pin loader, then the other end
of the wire is modelled clock by clock: for SWD the debug-port model of swd.py on SWCLK (uo[2]) and the open-drain
SWDIO (uio[0]), with the report decoded from the UART on uo[0]; for CAN the bus model of can.py on RXD (ui[0]), with
the chip's TXD (uio[3]) fed back onto the bus through a transceiver loop delay. The uio pads read back the chip's
open-drain pull-downs (wired with pull-ups). The same scenarios, and many more, run on the instruction-set model in
test_swd_model.py and test_can_model.py.
"""
import random
import sys
from pathlib import Path

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, RisingEdge

from loader import RAM_WORDS, load_program

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import asm  # noqa: E402
import can  # noqa: E402
import swd  # noqa: E402

EXAMPLES = Path(__file__).resolve().parent.parent / "firmware" / "examples"
PULLUPS = 0xCF                  # uio[7:6] and uio[3:0] pulled up; uio[5:4] are the loader pins and stay low


def words_of(name):
    words, _ = asm.assemble((EXAMPLES / name).read_text())
    assert len(words) <= RAM_WORDS
    return words + [0xF000] * (RAM_WORDS - len(words))


async def boot(dut, name, sched, ui=0):
    task = getattr(dut, "_clk_task", None)             # one clock for the whole module, however often we boot
    if task is None or task.done():
        dut._clk_task = cocotb.start_soon(Clock(dut.clk, 20, unit="ns").start())
    dut.ena.value = 1
    dut.rst_n.value = 0
    dut.ui_in.value = 0
    dut.uio_in.value = 0
    await ClockCycles(dut.clk, 10)
    dut.rst_n.value = 1
    await ClockCycles(dut.clk, 2)
    await load_program(dut, words_of(name), None, sched)
    dut.ui_in.value = ui
    dut.uio_in.value = PULLUPS                          # LOAD low: the program starts


def pulled(dut):
    return int(dut.uio_oe.value) & ~int(dut.uio_out.value) & 0xFF


# ------------------------------------------------------------------------------------------ SWD
async def run_swd(dut, target, clocks):
    await boot(dut, "swd.asm", None)
    wire, uart = swd.Wire(target), swd.UartRx()
    for c in range(clocks):
        dut.uio_in.value = (PULLUPS & ~1) | wire.level()
        await RisingEdge(dut.clk)
        low = pulled(dut)
        assert low & ~1 == 0, f"the chip pulls uio pads {low:#04x}"
        uo = int(dut.uo_out.value)
        wire.step(c, (uo >> 2) & 1, low & 1)
        uart.step(c, uo & 1)
    assert not uart.errors, uart.errors
    return uart.bytes, wire


@cocotb.test()
async def test_swd_power_up_with_wait_and_parity_error(dut):
    """Line reset, JTAG-to-SWD, read DPIDR, write ABORT and CTRL/STAT, read CTRL/STAT. The target answers WAIT
    three times to one request and sends one wrong parity bit."""
    t = swd.SwdTarget(dpidr=0x0BC11477, waits={2: 3}, bad_parity_at=3)
    got, wire = await run_swd(dut, t, 130000)
    want = [1, 0x77, 0x14, 0xC1, 0x0B, 0, 1, 1, 1, 0x00, 0x00, 0x00, 0xF0, 1]
    assert got == want, [hex(b) for b in got]
    assert not wire.errors and not t.errors, (wire.errors, t.errors)
    assert t.abort == 0x1E and t.ctrl == 0x50000000


@cocotb.test()
async def test_swd_nobody_there_and_fault(dut):
    got, wire = await run_swd(dut, None, 40000)
    assert got == [7] and wire.swclk == 0
    t = swd.SwdTarget(fault_at=1)
    got, wire = await run_swd(dut, t, 60000)
    assert got == [1, 0x77, 0x14, 0xA0, 0x2B, 0, 4] and not wire.errors


# ------------------------------------------------------------------------------------------ CAN
LEAD = 1200
ACK_EARLY, ACK_LATE = 10, 14


async def run_can(dut, frames, ppm=0, jitter=0.0, rng=None, listen=0, loop=5, tail=1500):
    await boot(dut, "can_rx.asm", (0, 1, 0, 1), ui=1 | (listen << 7))
    bursts, t, slots = [], LEAD, []
    scale = 1 + ppm * 1e-6
    for f in frames:
        w = f.wire() if isinstance(f, can.Frame) else f
        slots.append(t + f.ack_index * can.BIT_CLOCKS * scale if isinstance(f, can.Frame) else None)
        bursts.append((t, w))
        t += int(len(w) * can.BIT_CLOCKS * scale) + 1
    bus = can.Bus(bursts, ppm=ppm, jitter=jitter, rng=rng)
    prev, cur, out, acks, low_at = 0, [], [], [], None
    echo = [0] * (loop + 1)
    for c in range(bus.end_clock() + tail):
        low = pulled(dut)
        echo.append((low >> 3) & 1)
        dut.ui_in.value = (bus.level(c) & (1 - echo[-1 - loop])) | (listen << 7)
        dut.uio_in.value = PULLUPS & ~low
        await RisingEdge(dut.clk)
        low = pulled(dut)
        assert low & 0xB0 == 0, f"the chip pulls uio pads {low:#04x}"
        fell, rose = low & ~prev, prev & ~low
        if rose & 1:
            cur.append(int(dut.uo_out.value))
        if rose & 2:
            out.append((bytes(cur), int(dut.uo_out.value)))
            cur = []
        if fell & 8:
            low_at = c
        if rose & 8:
            acks.append((low_at, c))
        prev = low
    return out, acks, slots


@cocotb.test()
async def test_can_frames_are_received_and_acknowledged(dut):
    """Data and remote frames back to back, one with a stuff bit after the CRC, bit rate 1 % off, jittered edges:
    reported exactly, and acknowledged with one dominant bit in the ACK slot."""
    rng = random.Random(21)
    fr = [can.Frame(0x123, b"\x11\x22\x33"), can.Frame(0x2AA, rtr=1, dlc=4), can.Frame(0, bytes(8)),
          can.Frame(0x7FF, b"\xff" * 8)]
    seed = 1000
    while not fr[-1].stuff_after_crc:                   # add one frame whose CRC ends with five equal bits
        f = can.random_frame(random.Random(seed))
        seed += 1
        if f.stuff_after_crc:
            fr.append(f)
    out, acks, slots = await run_can(dut, fr, ppm=10000, jitter=2.0, rng=rng)
    assert [(b.hex(), s) for b, s in out] == [(f.report().hex(), 0) for f in fr]
    assert len(acks) == len(fr)
    for (lo, hi), slot in zip(acks, slots):
        assert hi - lo == can.BIT_CLOCKS and -ACK_EARLY <= lo - slot <= ACK_LATE, (lo - slot, hi - lo)


@cocotb.test()
async def test_can_errors_and_listen_only(dut):
    """A wrong CRC and an error flag are reported and not acknowledged, the next frame is; in listen-only mode
    TXD is never driven."""
    good = can.Frame(0x456, b"\xde\xad\xbe\xef")
    bad = can.Frame(0x321, b"\x01\x02\x03\x04", crc_xor=0x40)
    flag = can.Frame(0x111, b"\x01\x02").stuffed[:25] + [0] * 6 + [1] * 11
    out, acks, slots = await run_can(dut, [bad, good, flag, good])
    assert [s for _, s in out] == [1, 0, 2, 0] and out[1][0] == good.report() == out[3][0]
    assert len(acks) == 2 and all(hi - lo == can.BIT_CLOCKS for lo, hi in acks)
    out, acks, _ = await run_can(dut, [good, good], listen=1)
    assert out == [(good.report(), 0)] * 2 and acks == []
