# SPDX-License-Identifier: Apache-2.0
"""The SWD host (firmware/examples/swd.asm) on the instruction-set model, against the debug-port model of swd.py.
Pure Python, no simulator:  cd test && python3 -m pytest test_swd_model.py

The firmware runs on the default schedule. SWDIO is the open-drain pad uio[0] with a pull-up: the level both ends
see is low if the chip or the target pulls it low. The report is decoded from the UART on uo[0].
The same scenarios run on the RTL in test_swd_rtl.py.
"""
import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import asm  # noqa: E402
import swd  # noqa: E402
from isa_model import Model  # noqa: E402

FIRMWARE = Path(__file__).resolve().parent.parent / "firmware" / "examples" / "swd.asm"


def firmware_words(text=None):
    words, _ = asm.assemble(FIRMWARE.read_text() if text is None else text)
    assert len(words) <= 256
    return words


def run(words, target, clocks=110000, **wire_opts):
    """Run the firmware against the target; return (report bytes, wire, model)."""
    m = Model(list(words))
    wire, uart = swd.Wire(target, **wire_opts), swd.UartRx()
    for c in range(clocks):
        m.clock(wire.level() << 8)
        wire.step(c, (m.pins_out >> 2) & 1, m.od_pull & 1)
        uart.step(c, m.pins_out & 1)
        assert m.od_pull & ~1 == 0, "the chip pulled a pad other than SWDIO"
    assert not uart.errors, uart.errors
    return uart.bytes, wire, m


def report(dpidr, ctrl_readback=0xF0000000):
    return ([1] + list(dpidr.to_bytes(4, "little")) + [0] + [1] + [1]
            + [1] + list(ctrl_readback.to_bytes(4, "little")) + [0])


# ------------------------------------------------------------------ the target model itself, without firmware
def test_target_model_with_the_reference_host():
    t = swd.SwdTarget(dpidr=0x0BC11477)
    out = swd.reference_host(t, [("r", 0xA5), ("w", 0x81, 0x1E), ("w", 0xA9, 0x50000000), ("r", 0x8D)])
    assert out == [(1, (0x0BC11477, True)), (1, None), (1, None), (1, (0xF0000000, True))]
    assert t.abort == 0x1E and t.ctrl == 0x50000000 and not t.errors and t.line_resets == 2


def test_target_model_needs_the_select_sequence_and_dpidr_first():
    t = swd.SwdTarget()
    assert swd.reference_host(t, [("r", 0xA5)], select=False) == [(7, None)]
    t = swd.SwdTarget()
    assert swd.reference_host(t, [("r", 0x8D), ("r", 0xA5)]) == [(7, None), (7, None)]


def test_request_bytes():
    assert swd.request(0, 1, 0) == 0xA5 and swd.request(0, 0, 0) == 0x81
    assert swd.request(0, 0, 4) == 0xA9 and swd.request(0, 1, 4) == 0x8D


# ------------------------------------------------------------------ the firmware
@pytest.mark.parametrize("seed", range(6))
def test_power_up_sequence(seed):
    rng = random.Random(seed)
    dpidr = rng.getrandbits(32) | 1
    t = swd.SwdTarget(dpidr=dpidr)
    got, wire, m = run(firmware_words(), t)
    assert got == report(dpidr), [hex(b) for b in got]
    assert not wire.errors and not t.errors, (wire.errors, t.errors)
    assert [(k, r, a) for k, r, a, _ in t.log] == [("read", 0xA5, 1), ("write", 0x81, 1), ("write", 0xA9, 1),
                                                    ("read", 0x8D, 1)]
    assert t.abort == 0x1E and t.ctrl == 0x50000000 and t.line_resets == 2
    # 2 line resets of 56, the 16-bit select sequence, 8 idle, 4 transactions of 46 cycles and 8 idle each
    assert wire.rises == 56 * 2 + 16 + 8 + 4 * (46 + 8)
    assert not m.act[0], "thread 0 should have halted"


def test_both_parities_of_dpidr():
    for dpidr in (0x00000001, 0x00000003, 0xFFFFFFFF, 0x7FFFFFFF, 0x80000001, 0x2BA01477):
        got, wire, _ = run(firmware_words(), swd.SwdTarget(dpidr=dpidr))
        assert got == report(dpidr) and not wire.errors


def test_a_target_that_is_already_in_swd_mode():
    got, wire, _ = run(firmware_words(), swd.SwdTarget(needs_select=False))
    assert got == report(0x2BA01477) and not wire.errors


def test_nobody_there():
    """Only the pull-up: ACK reads as 7, the program reports it and stops, SWCLK stays low."""
    got, wire, m = run(firmware_words(), None, clocks=40000)
    assert got == [7]
    assert wire.rises == 56 * 2 + 16 + 8 + 8 + 1 + 3 + 1 + 8 and not m.act[0] and wire.swclk == 0


def test_dead_target():
    t = swd.SwdTarget(dead=True)
    got, wire, _ = run(firmware_words(), t, clocks=40000)
    assert got == [7] and not wire.errors


@pytest.mark.parametrize("n", [1, 3, 15])
def test_wait_is_retried(n):
    """WAIT on the ABORT write, n times: the request is repeated and the report is that of a good run."""
    t = swd.SwdTarget(waits={1: n})
    got, wire, _ = run(firmware_words(), t, clocks=110000 + n * 8000)
    assert got == report(0x2BA01477), [hex(b) for b in got]
    assert not wire.errors and not t.errors
    assert sum(1 for _, _, a, _ in t.log if a == swd.WAIT) == n and t.ctrl == 0x50000000


def test_wait_for_ever_gives_up_after_16_tries():
    t = swd.SwdTarget(waits={2: 10**6})
    got, wire, m = run(firmware_words(), t, clocks=250000)
    assert got == [1, 0x77, 0x14, 0xA0, 0x2B, 0, 1, 2], [hex(b) for b in got]
    assert sum(1 for _, _, a, _ in t.log if a == swd.WAIT) == 16
    assert not wire.errors and not m.act[0]


def test_fault_is_reported_and_ends_the_program():
    t = swd.SwdTarget(fault_at=2)
    got, wire, m = run(firmware_words(), t)
    assert got == [1, 0x77, 0x14, 0xA0, 0x2B, 0, 1, 4]
    assert not wire.errors and not m.act[0] and t.ctrl == 0


@pytest.mark.parametrize("which,expect_at", [(0, 5), (3, 13)])
def test_parity_error_in_read_data_is_flagged(which, expect_at):
    t = swd.SwdTarget(bad_parity_at=which)
    got, wire, _ = run(firmware_words(), t)
    want = report(0x2BA01477)
    want[expect_at] = 1
    assert got == want and not wire.errors


@pytest.mark.parametrize("value", [0x1F, 0x01, 0x80, 0xFF, 0x00])
def test_write_parity_is_computed_for_other_data(value):
    """The two values the program writes both have an even number of ones. Other values of the ABORT byte give
    both parities; the target checks the parity bit and keeps the value only if it is right."""
    text = FIRMWARE.read_text().replace("LDA  0x1E             ; ABORT", f"LDA  {value:#04x}             ; ABORT")
    assert text != FIRMWARE.read_text() or value == 0x1E
    t = swd.SwdTarget()
    got, wire, _ = run(firmware_words(text), t)
    assert got == report(0x2BA01477) and not t.errors and not wire.errors
    assert t.abort == value


def test_timing_seen_by_the_target():
    """SWCLK high exactly 48 clocks (12 turns), low at least 68, SWDIO set up 56 clocks before the rising edge."""
    m = Model(firmware_words())
    t = swd.SwdTarget()
    wire = swd.Wire(t)
    highs, lows, setups, last, lastchg, prev_low = set(), set(), set(), None, None, 0
    for c in range(60000):
        m.clock(wire.level() << 8)
        swclk, host_low = (m.pins_out >> 2) & 1, m.od_pull & 1
        if host_low != prev_low:
            lastchg, prev_low = c, host_low
        if swclk != wire.swclk and last is not None:
            (lows if swclk else highs).add(c - last)
            if swclk and lastchg is not None:
                setups.add(c - lastchg)
        if swclk != wire.swclk:
            last = c
        wire.step(c, swclk, host_low)
    assert highs == {48}, highs
    assert min(lows) == 68, sorted(lows)[:5]
    assert min(setups) == 56, sorted(setups)[:5]


# ------------------------------------------------------------------ do the tests have teeth?
MUTANTS = [
    ("the select sequence is wrong", "LDA  0x9E             ; JTAG-to-SWD", "LDA  0x9D             ; JTAG-to-SWD"),
    ("only 48 clocks of line reset", "LDA  7\n        STM  9", "LDA  6\n        STM  9"),
    ("no turnaround before write data", "JNZ  sw_fail\n        CALL sw_trn           ; turnaround\n        LDM  4",
     "JNZ  sw_fail\n        LDM  4"),
    ("write parity always 0", "        AND  1\n        RET", "        AND  0\n        RET"),
    ("SWDIO set too late", "ODRVC 0               ; SWDIO = the bit (0 pulls low, 1 lets go)\n        WAIT 12",
     "WAIT 11\n        ODRVC 0\n        NOP"),
    ("target bits shifted in at the wrong end", "        LDM  10\n        ROR", "        LDM  10\n        ROL"),
    ("target bit read after the rising edge", "        INB  8                ; SWDIO, read back from the pad, just before the rising edge\n        PIN  2 1",
     "        PIN  2 1\n        INB  8"),
    ("read data sampled a cycle late", "        CALL sw_rbit\n        CALL sw_rbit\n        CALL sw_rbit          ; A = ACK << 5",
     "        CALL sw_rbit\n        CALL sw_rbit\n        CALL sw_rbit\n        CALL sw_trn"),
]


@pytest.mark.parametrize("name,old,new", MUTANTS, ids=[m[0] for m in MUTANTS])
def test_mutant_is_caught(name, old, new):
    text = FIRMWARE.read_text()
    assert text.count(old) == 1, name
    text = text.replace(old, new)
    if name == "write parity always 0":                 # needs data with an odd number of ones to show
        text = text.replace("LDA  0x1E             ; ABORT", "LDA  0x1F             ; ABORT")
    t = swd.SwdTarget()
    got, wire, _ = run(firmware_words(text), t)
    good = got == report(0x2BA01477) and not wire.errors and not t.errors and t.ctrl == 0x50000000
    assert not good, f"mutant '{name}' passes"
