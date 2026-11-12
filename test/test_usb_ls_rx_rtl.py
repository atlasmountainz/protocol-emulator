# SPDX-License-Identifier: Apache-2.0
"""The low-speed USB receiver (firmware/examples/usb_ls_rx.asm) on the chip: RTL, and the gate-level netlist in CI.

Run with:  cd test && make COCOTB_TEST_MODULES=test_usb_ls_rx_rtl

Pins only, so it also runs on the gate-level netlist. The program is loaded over the pin loader with the thread
schedule 0,1,0,1 (byte 512), then a USB waveform (usb_ls.py: 666.67 ns bits, bit rate off by some ppm, edge jitter,
D+ / D- skew) is driven on ui[0] / ui[1] clock by clock. The uio pads read back the chip's open-drain pull-downs
(wired with pull-ups), as on a board, which the firmware needs for its internal EVENT flag on uio[2]. The testbench
records the report: uo[7:0] when BYTE (uio[0]) is released again, and a packet ends when STATUS (uio[1]) is. The
same scenarios run on the instruction-set model in test_usb_ls_rx_model.py.
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
import usb_ls as u  # noqa: E402

FIRMWARE = Path(__file__).resolve().parent.parent / "firmware" / "examples" / "usb_ls_rx.asm"
SCHEDULE = (0, 1, 0, 1)
PULLUPS = 0xCF                  # uio[7:6] and uio[3:0] pulled up; uio[5:4] are the loader pins and stay low
LEAD = 300
TOKEN, DATAP, HAND = 0x40, 0xC0, 0x80


def firmware_words():
    words, _ = asm.assemble(FIRMWARE.read_text())
    assert len(words) <= RAM_WORDS
    return words + [0xF000] * (RAM_WORDS - len(words))


async def boot(dut):
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
    await load_program(dut, firmware_words(), None, SCHEDULE)
    dut.ui_in.value = 0x02                              # idle J: D+ low, D- high
    dut.uio_in.value = PULLUPS                          # LOAD low: the program starts
    await ClockCycles(dut.clk, 6)


async def receive(dut, bursts, ppm=0, jitter=0.0, skew=0, rng=None, tail=500):
    """Drive the bursts, return [(bytes, status)] as reported."""
    bus = u.Bus([(s + LEAD, sy) for s, sy in bursts], ppm=ppm, jitter=jitter, skew=skew, rng=rng)
    packets, cur = [], []
    prev_pads = PULLUPS
    for c in range(bus.end_clock() + tail):
        dp, dm = bus.level(c)
        dut.ui_in.value = dp | (dm << 1)
        await RisingEdge(dut.clk)
        pulled = int(dut.uio_oe.value) & ~int(dut.uio_out.value) & 0xFF
        pads = PULLUPS & ~pulled
        dut.uio_in.value = pads
        rose = pads & ~prev_pads
        if rose & 1:
            cur.append(int(dut.uo_out.value))
        if rose & 2:
            packets.append((bytes(cur), int(dut.uo_out.value)))
            cur = []
        prev_pads = pads
    assert not cur, f"bytes reported without a STATUS: {bytes(cur).hex()}"
    return packets


def schedule_bursts(packets, rng, ppm=0, gap=(60, 300)):
    bursts, t = [], 0
    for p in packets:
        syms = p if isinstance(p, list) else u.packet_symbols(p)
        bursts.append((t, syms))
        t += int(len(syms) * u.CLOCKS_PER_BIT * (1 + ppm * 1e-6)) + rng.randrange(*gap)
    return bursts


def good(packet):
    return bytes(packet), (packet[0] & 3) << 6


GET_DESCRIPTOR = bytes.fromhex("c38006000100004000dd94")


@cocotb.test()
async def test_usb_ls_rx_known_packets(dut):
    """SETUP / GET_DESCRIPTOR / ACK / IN / NAK / SOF, nominal bit rate, and one data packet needing a stuffed bit
    in every byte."""
    await boot(dut)
    pk = [u.token(u.SETUP, 0, 0), GET_DESCRIPTOR, u.handshake(u.ACK), u.token(u.IN, 1, 0), u.handshake(u.NAK),
          u.sof(0x2A5), u.data_packet(u.DATA1, b"\xff" * 8)]
    got = await receive(dut, schedule_bursts(pk, random.Random(1)))
    assert got == [good(p) for p in pk], [(b.hex(), hex(s)) for b, s in got]


@cocotb.test()
async def test_usb_ls_rx_rate_limits_jitter_skew(dut):
    """The low-speed limits: bit rate -1.5 % and +1.5 %, edge jitter +-1 clock, D+ / D- skew of 2 clocks."""
    for ppm, seed in ((-15000, 2), (15000, 3)):
        await boot(dut)
        rng = random.Random(seed)
        pk = [u.token(u.OUT, 9, 2), u.data_packet(u.DATA0, bytes(rng.randrange(256) for _ in range(8))),
              u.handshake(u.STALL), u.data_packet(u.DATA1, b"\x7f\xfe\x00\xff")]
        got = await receive(dut, schedule_bursts(pk, rng, ppm), ppm=ppm, jitter=1.0, skew=2, rng=rng)
        assert got == [good(p) for p in pk], (ppm, [(b.hex(), hex(s)) for b, s in got])


@cocotb.test()
async def test_usb_ls_rx_errors_are_reported_and_the_receiver_recovers(dut):
    """Wrong CRC16 / CRC5 / PID check, a stuffing violation: flagged, and the next good packet is still received."""
    await boot(dut)
    data = u.data_packet(u.DATA0, bytes(range(8)))
    bad_data = bytearray(data)
    bad_data[4] ^= 0x08
    tok = u.token(u.SETUP, 5, 1)
    bad_tok = bytearray(tok)
    bad_tok[2] ^= 0x80
    bad_pid = bytes([tok[0] ^ 0x10]) + tok[1:]
    violation = u.nrzi(u.to_bits(bytes([0x80, 0x2D, 0xFF, 0xFF])))      # seven 1s in a row
    last = u.token(u.IN, 3, 3)
    bursts, t = [], 0
    for syms in (u.packet_symbols(bytes(bad_data)), u.packet_symbols(bytes(bad_tok)), u.packet_symbols(bad_pid),
                 violation, u.packet_symbols(last)):
        bursts.append((t, syms))
        t += int(len(syms) * u.CLOCKS_PER_BIT) + 250
    got = await receive(dut, bursts)
    assert [s for _, s in got[:3]] == [DATAP | 4, TOKEN | 4, TOKEN | 2], got
    assert got[3][1] & 0x20
    assert got[4] == good(last)
    assert len(got) == 5
