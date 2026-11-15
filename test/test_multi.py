# SPDX-License-Identifier: Apache-2.0
"""Three protocols at the same time (firmware/examples/multi.asm, docs/examples-multi.md).

Run with:  cd test && make COCOTB_TEST_MODULES=test_multi

One program image runs a WS2812 LED strip driver (threads 0 and 1, uo[2]), an I2C target with 16 registers
(thread 2, SDA uio[0], SCL uio[1]) and a UART status line (thread 3, uo[0]) together. The test drives the chip
through its pins only, with all three checked at once:
  * the I2C master of test_i2c_target_regs.py runs random register writes and reads against a reference model,
    with its bus checks (the chip drives only SDA, never changes it while SCL is high, answers in time);
  * the WS2812 waveform is decoded and must be exactly what the stand-alone program produces: the same pixels
    and the same pulse widths to the clock (16 / 36 clocks high, 64 clocks per bit), however busy the I2C bus is;
  * every UART frame must sit exactly on the 432-clock bit grid and the lines must read "UP 00", "UP 01", ...
So the threads really run side by side without disturbing each other's timing, on the RTL (and on the gate-level
netlist in CI).
"""
import random
import sys
from pathlib import Path

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, RisingEdge

from loader import RAM_WORDS, load_program
import test_i2c_target_regs as itr
import test_leds_keyboard_midi as lkm

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import asm  # noqa: E402

FIRMWARE = Path(__file__).resolve().parent.parent / "firmware" / "examples" / "multi.asm"


def words():
    w, _ = asm.assemble(FIRMWARE.read_text())
    assert len(w) <= RAM_WORDS
    return w + [0xF000] * (RAM_WORDS - len(w))


async def record(dut, edges, stray):
    """Edges of uo[2] (WS2812); any change of a uo pin other than uo[0] (UART) and uo[2] is stray."""
    cycle, prev = 0, int(dut.uo_out.value)
    while True:
        await RisingEdge(dut.clk)
        cycle += 1
        cur = int(dut.uo_out.value)
        if (cur ^ prev) & ~0b101 & 0xFF:
            stray.append((cycle, prev, cur))
        if (cur ^ prev) & 4:
            edges.append((cycle, (cur >> 2) & 1))
        prev = cur


async def boot(dut, timing):
    itr.start_clock(dut)
    dut.ena.value = 1
    dut.ui_in.value = 0
    dut.uio_in.value = 0
    dut.rst_n.value = 0
    await ClockCycles(dut.clk, 10)
    dut.rst_n.value = 1
    await ClockCycles(dut.clk, 5)
    await load_program(dut, words())
    edges, stray = [], []
    cocotb.start_soon(record(dut, edges, stray))
    uart = lkm.UartMonitor(dut, 432, pin=0)
    cocotb.start_soon(uart.run())
    dut.uio_in.value = 0b11                 # LOAD low (the program starts); SDA and SCL idle high
    bus = itr.Bus(dut, timing)
    bus.task = cocotb.start_soon(bus.run())
    await ClockCycles(dut.clk, 200)
    return bus, edges, stray, uart


async def i2c_traffic(bus, ref, rng, done):
    """Random register traffic until `done` is set: writes, reads, pointer + repeated START + read."""
    n = 0
    while not done[0]:
        op = rng.randrange(3)
        if op == 0:
            ptr = rng.randrange(16)
            await itr.write_check(bus, ref, ptr, [rng.randrange(256) for _ in range(rng.randrange(1, 5))])
        elif op == 1:
            await itr.read_check(bus, ref, rng.randrange(1, 5))
        else:
            await itr.set_pointer_read(bus, ref, rng.randrange(16), rng.randrange(1, 4))
        n += 1
    return n


def check_ws2812(edges, stray, frames_wanted):
    assert not stray, f"uo pins other than uo[0] and uo[2] changed: {stray[:3]}"
    frames, gaps, stats = lkm.decode_ws2812(edges)
    full = [f for f in frames if len(f) == lkm.PIXELS * 3 and "partial" not in f]
    assert len(full) >= frames_wanted, f"only {len(full)} complete frames"
    for k in range(frames_wanted):
        assert full[k] == lkm.expected_frame(k), f"frame {k} wrong"
    # exactly the stand-alone waveform: constant to the clock, whatever the other threads do
    assert set(stats["t0h"]) == {16 * lkm.CLK_NS}, sorted(set(stats["t0h"]))
    assert set(stats["t1h"]) == {36 * lkm.CLK_NS}, sorted(set(stats["t1h"]))
    assert set(stats["period"]) == {64 * lkm.CLK_NS}, sorted(set(stats["period"]))
    assert all(g is None or g >= lkm.RESET_MIN for g in gaps)
    return len(full)


def check_uart(uart, lines_wanted):
    assert not uart.errors, uart.errors[:5]
    text = bytes(uart.bytes).decode("ascii")
    lines = text.split("\r\n")
    complete = lines[:-1]
    assert len(complete) >= lines_wanted, repr(text)
    assert complete == [f"UP {i:02X}" for i in range(len(complete))], repr(text)
    return len(complete)


@cocotb.test()
async def test_three_protocols_at_once_400k(dut):
    """WS2812 + I2C target at 400 kHz with random traffic + UART status lines, all checked at the same time."""
    bus, edges, stray, uart = await boot(dut, itr.FAST)
    ref, rng, done = itr.Regs(), random.Random(1), [False]
    traffic = cocotb.start_soon(i2c_traffic(bus, ref, rng, done))
    await ClockCycles(dut.clk, 3 * 27_500 + 10_000)       # three LED frames and the start of a fourth
    done[0] = True
    n = await traffic
    bus.check(tvd=itr.TVD_FM)
    frames = check_ws2812(edges, stray, 3)
    lines = check_uart(uart, 1)
    dut._log.info(f"{n} I2C transactions, {frames} LED frames, {lines} UART lines, all at the same time")


@cocotb.test()
async def test_three_protocols_at_once_100k_all_registers(dut):
    """The same at 100 kHz: all 16 registers written and read back while the LEDs and the UART run."""
    bus, edges, stray, uart = await boot(dut, itr.STD)
    ref = itr.Regs()
    data = itr.pattern(3)
    await itr.write_check(bus, ref, 0, data)
    await itr.set_pointer_read(bus, ref, 0, 16)
    await itr.set_pointer_read(bus, ref, 9, 10)
    rest = 3 * 27_500 + 10_000 - bus.cycle
    if rest > 0:
        await ClockCycles(dut.clk, rest)
    bus.check(tvd=itr.TVD_SM)
    check_ws2812(edges, stray, 3)
    check_uart(uart, 1)
