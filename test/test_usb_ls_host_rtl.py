# SPDX-License-Identifier: Apache-2.0
"""The low-speed USB host (firmware/examples/usb_ls_host.asm) on the chip: RTL, and the gate-level netlist in CI.

Run with:  cd test && make COCOTB_TEST_MODULES=test_usb_ls_host_rtl

Pins only. The program is loaded over the pin loader with the schedule 0,1,0,1; then, every clock, the chip's uio
pads (output enable and value) and the device model of usb_host_bus.py make the levels of D+ and D-, which go back
on uio_in[1:0]. The device model decodes what the host sends with the reference model, checks the bus timing
(every transition on the 666.67 ns grid within 26.7 ns, EOPs, keep-alives, the bus reset, no contention) and
answers each SETUP + DATA0 with a handshake, which the chip must show on uo[7:0]. The same scenarios, and many more,
run on the instruction-set model in test_usb_ls_host_model.py.
"""
import sys
from pathlib import Path

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, RisingEdge

from loader import RAM_WORDS, load_program
import test_usb_ls_host_model as tm

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import asm  # noqa: E402
import usb_ls as u  # noqa: E402
from usb_host_bus import HostBus  # noqa: E402


async def run(dut, text, bus, clocks):
    task = getattr(dut, "_clk_task", None)
    if task is None or task.done():
        dut._clk_task = cocotb.start_soon(Clock(dut.clk, 20, unit="ns").start())
    words, _ = asm.assemble(text)
    words += [0xF000] * (RAM_WORDS - len(words))
    dut.ena.value = 1
    dut.rst_n.value = 0
    dut.ui_in.value = 0
    dut.uio_in.value = 0
    await ClockCycles(dut.clk, 10)
    dut.rst_n.value = 1
    await ClockCycles(dut.clk, 2)
    await load_program(dut, words, None, tm.SCHEDULE)
    dp, dm, shown = 0, 1, []
    for c in range(clocks):
        dut.uio_in.value = dp | (dm << 1) | 0xCC            # LOAD (uio[5]) low: the program runs
        await RisingEdge(dut.clk)
        oe, out = int(dut.uio_oe.value), int(dut.uio_out.value)
        assert oe & ~3 == 0, f"the chip drives uio pads {oe:#04x}"
        dp, dm = bus.step(c, oe, out)
        uo = int(dut.uo_out.value)
        if not shown or shown[-1] != uo:
            shown.append(uo)
    return shown


@cocotb.test()
async def test_usb_host_enumeration_request_acknowledged(dut):
    """Bus reset, keep-alives, SETUP + DATA0 GET_DESCRIPTOR(device) to address 0, twice; the device ACKs."""
    text = tm.FIRMWARE.read_text().replace("h_rlen: LDX  64", "h_rlen: LDX  1")
    bus = HostBus(reset_min=tm.SHORT_RESET, delay_bits=3.0, ppm=-15000)
    shown = await run(dut, text, bus, 2 * tm.FRAME + 16_000)
    tm.check_wire(bus, 2)
    assert len(bus.resets) == 1
    assert shown[-1] == 0xD2, [hex(v) for v in shown]


@cocotb.test()
async def test_usb_host_stuffing_and_nak(dut):
    """A request of all 1s (a stuffed bit after every six) to address 0x55, endpoint 9; the device NAKs at +1.5 %."""
    text = tm.source(payload=b"\xff" * 8, addr=0x55, ep=9)
    bus = HostBus(answer=u.NAK, reset_min=tm.SHORT_RESET, delay_bits=5.5, ppm=15000)
    shown = await run(dut, text, bus, tm.FRAME + 16_000)
    tm.check_wire(bus, 1, b"\xff" * 8, 0x55, 9)
    assert shown[-1] == 0x5A, [hex(v) for v in shown]
