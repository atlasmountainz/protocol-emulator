# SPDX-License-Identifier: Apache-2.0

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, RisingEdge

CLKS_PER_BIT = 434  # must match project.v


async def reset(dut):
    dut.ena.value = 1
    dut.ui_in.value = 0
    dut.uio_in.value = 0
    dut.rst_n.value = 0
    await ClockCycles(dut.clk, 10)
    dut.rst_n.value = 1
    await ClockCycles(dut.clk, 2)


async def send_and_capture(dut, byte):
    """Pulse start with `byte` on ui_in, then sample the TX line mid-bit."""
    dut.ui_in.value = byte
    dut.uio_in.value = 1  # start pin high
    await ClockCycles(dut.clk, 1)
    dut.uio_in.value = 0

    # TX goes low (start bit) within a couple of clocks
    for _ in range(5):
        await RisingEdge(dut.clk)
        if int(dut.uo_out.value) & 1 == 0:
            break
    else:
        raise AssertionError("TX never went low after start")

    # Move to the middle of the start bit, then sample once per bit time
    await ClockCycles(dut.clk, CLKS_PER_BIT // 2)
    start_bit = int(dut.uo_out.value) & 1
    assert start_bit == 0, "start bit should be 0"

    value = 0
    for i in range(8):
        await ClockCycles(dut.clk, CLKS_PER_BIT)
        value |= (int(dut.uo_out.value) & 1) << i

    await ClockCycles(dut.clk, CLKS_PER_BIT)
    stop_bit = int(dut.uo_out.value) & 1
    assert stop_bit == 1, "stop bit should be 1"
    return value


@cocotb.test()
async def test_idle_high(dut):
    cocotb.start_soon(Clock(dut.clk, 20, unit="ns").start())
    await reset(dut)
    await ClockCycles(dut.clk, 50)
    assert int(dut.uo_out.value) & 1 == 1, "TX must idle high"
    assert (int(dut.uo_out.value) >> 1) & 1 == 0, "busy must be low when idle"


@cocotb.test()
async def test_send_bytes(dut):
    cocotb.start_soon(Clock(dut.clk, 20, unit="ns").start())
    await reset(dut)

    for byte in (0x55, 0xA3, 0x00, 0xFF, 0x41):
        got = await send_and_capture(dut, byte)
        dut._log.info(f"sent 0x{byte:02X}, received 0x{got:02X}")
        assert got == byte, f"expected 0x{byte:02X}, got 0x{got:02X}"
        # We sampled mid stop bit, so busy must still be high; it should clear
        # within the rest of the stop bit (plus a little slack).
        assert (int(dut.uo_out.value) >> 1) & 1 == 1, "busy should still be high mid stop bit"
        for _ in range(CLKS_PER_BIT):
            await RisingEdge(dut.clk)
            if (int(dut.uo_out.value) >> 1) & 1 == 0:
                break
        else:
            raise AssertionError("busy never cleared after the stop bit")
        assert int(dut.uo_out.value) & 1 == 1, "TX must return to idle high"
