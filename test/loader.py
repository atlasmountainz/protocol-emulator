# SPDX-License-Identifier: Apache-2.0
"""Testbench side of the program loader (docs/loading.md): drives LOAD / STROBE / data pins."""
import random

from cocotb.triggers import ClockCycles

LOAD = 0x20    # uio[5]
STROBE = 0x10  # uio[4]
RAM_WORDS = 256
SCHED_BYTE = 2 * RAM_WORDS    # index of the schedule register: the byte after the 512 program bytes


def sched_byte(sched):
    """Pack a schedule (docs/isa.md, 'Timing and the schedule'): slot s is in bits 2s+1:2s.
    Any four values 0..3 are packed, legal or not: the hardware decides what to do with them."""
    assert len(sched) == 4 and all(0 <= t <= 3 for t in sched), f"bad schedule {sched}"
    return sched[0] | sched[1] << 2 | sched[2] << 4 | sched[3] << 6


async def _send_byte(dut, byte, rng):
    dut.ui_in.value = byte
    await ClockCycles(dut.clk, rng.randint(3, 6) if rng else 3)
    dut.uio_in.value = LOAD | STROBE
    await ClockCycles(dut.clk, rng.randint(3, 12) if rng else 3)
    dut.uio_in.value = LOAD
    await ClockCycles(dut.clk, rng.randint(3, 6) if rng else 3)


async def load_program(dut, words, rng=None, sched=None):
    """Load `words` (<= RAM_WORDS, low byte of each word first) into the program memory.

    With `sched` (four thread numbers 0..3, slot 0 first) the program must be the full RAM_WORDS words:
    one more byte, the 513th (byte index 512), is sent after the 512 program bytes with the same strobe
    sequence, and the chip takes it as the thread schedule, sched[0] | sched[1] << 2 | sched[2] << 4 |
    sched[3] << 6 (an illegal schedule is ignored by the chip, which then keeps the default 0 1 2 3).
    Without `sched` the schedule register is not written and keeps its default.

    Leaves LOAD high and the CPU held in reset; the caller releases it by lowering LOAD (and
    sets whatever pin values the program should start with). With `rng`, the host is sloppy:
    random setup times and strobe widths, to check that only the strobe's rising edge counts.
    """
    assert len(words) <= RAM_WORDS
    assert sched is None or len(words) == RAM_WORDS, \
        "the schedule is byte 512: send the whole 256-word image before it"
    dut.ui_in.value = 0
    dut.uio_in.value = LOAD
    await ClockCycles(dut.clk, 4)
    for w in words:
        for byte in (w & 0xFF, w >> 8):
            await _send_byte(dut, byte, rng)
    if sched is not None:
        await _send_byte(dut, sched_byte(sched), rng)
