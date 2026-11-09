# SPDX-License-Identifier: Apache-2.0
"""Testbench side of the program loader (docs/loading.md): drives LOAD / STROBE / data pins."""
import random

from cocotb.triggers import ClockCycles

LOAD = 0x20    # uio[5]
STROBE = 0x10  # uio[4]
RAM_WORDS = 256


async def load_program(dut, words, rng=None):
    """Load `words` (<= RAM_WORDS, low byte of each word first) into the program memory.

    Leaves LOAD high and the CPU held in reset; the caller releases it by lowering LOAD (and
    sets whatever pin values the program should start with). With `rng`, the host is sloppy:
    random setup times and strobe widths, to check that only the strobe's rising edge counts.
    """
    assert len(words) <= RAM_WORDS
    dut.ui_in.value = 0
    dut.uio_in.value = LOAD
    await ClockCycles(dut.clk, 4)
    for w in words:
        for byte in (w & 0xFF, w >> 8):
            dut.ui_in.value = byte
            await ClockCycles(dut.clk, rng.randint(3, 6) if rng else 3)
            dut.uio_in.value = LOAD | STROBE
            await ClockCycles(dut.clk, rng.randint(3, 12) if rng else 3)
            dut.uio_in.value = LOAD
            await ClockCycles(dut.clk, rng.randint(3, 6) if rng else 3)
