# SPDX-License-Identifier: Apache-2.0
"""ISA v2 on the real chip: hand-written programs that use the new instructions, loaded through the pin
loader (docs/loading.md), with the results read from the output pins.

Run with:  cd test && make COCOTB_TEST_MODULES=test_isa_v2_rtl

How a program reports a result. `RES` copies A to the push-pull outputs (`OUTA`) and then pulses
open-drain pad 0 (`ODRV 0 0`, `ODRV 0 1`); the testbench samples `uo_out` at every rising edge of
`uio_oe[0]`, so any number of results can be checked in one program, and the last one is also left on
`uo_out`. The expected values are literals worked out by hand from docs/isa.md, not computed by the
reference model (test/test_isa_v2_model.py also runs these very programs on the model, as a cross-check
of the tables below).

The schedule tests load a program together with a thread schedule (byte 512) and measure how many clocks
a thread takes per instruction: 4 when it owns one of the four slots, 2 when it owns two (slots two
clocks apart); an illegal schedule must leave the default, and a thread that is not in the schedule never
runs, although SPAWN marks it running.
"""
import random
import sys
from pathlib import Path

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, RisingEdge

from loader import RAM_WORDS, load_program

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import asm  # noqa: E402

CLK_NS = 20          # 50 MHz
HALT = 0xF000

RES = "OUTA\nODRV 0 0\nODRV 0 1"      # report A on uo_out, then pulse pad 0 (uio_oe[0])
STROBE = "ODRV 0 0\nODRV 0 1"         # the pulse alone (the program has put its result on uo_out itself)


# ======================================================================================
# pure helpers (shared with the model cross-check in test_isa_v2_model.py)
# ======================================================================================

class Prog:
    """A test program and what the pins must show.

    expect       values of uo_out at each strobe (rising edge of uio_oe[0]), in order
    final_uo     uo_out when the program has finished (None: do not check)
    oe_seq       the successive distinct values of uio_oe, starting with the first sample (None: not checked)
    extra_turns  turns spent in WAITs and loops, to size the observation window
    """

    def __init__(self, name, src, expect=None, final_uo=None, oe_seq=None, raw=None, extra_turns=0,
                 ui=0, uio=0, sched=None, window=None):
        self.name, self.src, self.raw = name, src, raw or {}
        self.expect, self.final_uo, self.oe_seq = expect, final_uo, oe_seq
        self.ui, self.uio, self.sched = ui, uio, sched
        self.words = assemble(src, self.raw)
        n = len(asm.assemble(src)[0])
        self.window = window or 4 * (n + extra_turns) + 400


def assemble(src, raw=None):
    """Assemble `src`, patch raw words in ({index: word}) and pad to the full 256 words with HALT."""
    words, _ = asm.assemble(src)
    for i, w in (raw or {}).items():
        words[i] = w
    assert len(words) <= RAM_WORDS, f"{len(words)} words"
    return words + [HALT] * (RAM_WORDS - len(words))


def strobe_values(uo, oe):
    """uo_out at every rising edge of uio_oe[0]."""
    return [uo[i] for i in range(1, len(oe)) if oe[i] & 1 and not oe[i - 1] & 1]


def distinct_runs(seq):
    out = []
    for v in seq:
        if not out or out[-1] != v:
            out.append(v)
    return out


def rise_clocks(uo, bit):
    return [i for i in range(1, len(uo)) if (uo[i] >> bit) & 1 and not (uo[i - 1] >> bit) & 1]


def change_clocks(uo, bit):
    return [i for i in range(1, len(uo)) if ((uo[i] ^ uo[i - 1]) >> bit) & 1]


def check_prog(prog, uo, oe):
    """Compare the sampled pins with what the program must show; returns a list of error strings."""
    errs = []
    if prog.expect is not None:
        got = strobe_values(uo, oe)
        if got != prog.expect:
            bad = next((i for i, (g, e) in enumerate(zip(got, prog.expect)) if g != e), min(len(got), len(prog.expect)))
            errs.append(f"{prog.name}: results {[hex(v) for v in got]}, expected {[hex(v) for v in prog.expect]} "
                        f"(first difference at result {bad})")
    if prog.final_uo is not None and uo[-1] != prog.final_uo:
        errs.append(f"{prog.name}: final uo_out {uo[-1]:#x}, expected {prog.final_uo:#x}")
    if prog.oe_seq is not None and distinct_runs(oe) != prog.oe_seq:
        errs.append(f"{prog.name}: uio_oe went through {[hex(v) for v in distinct_runs(oe)]}, "
                    f"expected {[hex(v) for v in prog.oe_seq]}")
    return errs


# ======================================================================================
# the programs
# ======================================================================================

def items_to_programs(name, items, max_words=200, ui=0, uio=0):
    """Pack (code, expected) items into programs of at most `max_words` words. Each `code` ends by
    reporting one result (RES / STROBE) and may use item-local labels."""
    progs, cur, words = [], [], 0
    for item in items + [None]:
        code, exp = item if item is not None else (None, None)
        n = len(asm.assemble(code)[0]) if code is not None else 0
        if code is None or (cur and words + n > max_words):
            if cur:
                src = "\n".join(c for c, _ in cur) + "\nHALT"
                progs.append(Prog(f"{name}-{len(progs) + 1}", src, expect=[e for _, e in cur],
                                  final_uo=cur[-1][1], ui=ui, uio=uio))
            cur, words = [], 0
        if code is not None:
            cur.append((code, exp))
            words += n
    return progs


def acc(code, expected):
    return (code + "\n" + RES, expected)


# C is read out with `LDA 0; ADC 0` (A = C; LDA leaves the flags alone). Each entry: code leaving the result in
# A, expected A.
ALU_ITEMS = [acc(c, e) for c, e in [
    # ADD: result, carry set, carry clear
    ("LDA 0x7F\nADD 0x01", 0x80),
    ("LDA 0xFF\nADD 0x01\nLDA 0\nADC 0", 0x01),
    ("LDA 0x7F\nADD 0x01\nLDA 0\nADC 0", 0x00),
    ("LDA 0xFF\nADD 0xFF", 0xFE),
    # SUB: result, borrow set, borrow clear
    ("LDA 0x00\nSUB 0x01", 0xFF),
    ("LDA 0x00\nSUB 0x01\nLDA 0\nADC 0", 0x01),
    ("LDA 0x80\nSUB 0x01\nLDA 0\nADC 0", 0x00),
    ("LDA 0x7F\nSUB 0xFF", 0x80),
    # CMP: unsigned, A unchanged
    ("LDA 0x0F\nCMP 0x10\nLDA 0\nADC 0", 0x01),
    ("LDA 0x10\nCMP 0x10\nLDA 0\nADC 0", 0x00),
    ("LDA 0xFF\nCMP 0x7F\nLDA 0\nADC 0", 0x00),
    ("LDA 0x7F\nCMP 0xFF\nLDA 0\nADC 0", 0x01),
    ("LDA 0xA5\nCMP 0x01", 0xA5),
    # AND / OR / XOR
    ("LDA 0xF0\nAND 0x3C", 0x30),
    ("LDA 0xF0\nOR 0x0F", 0xFF),
    ("LDA 0xFF\nXOR 0xAA", 0x55),
    # ADC: 16-bit add 0x12FF + 0x0101, carry out, carry in 1 and 0
    ("LDA 0xFF\nADD 0x01\nLDA 0x12\nADC 0x01", 0x14),
    ("LDA 0xFF\nADD 0x01\nLDA 0xFF\nADC 0x00\nLDA 0\nADC 0", 0x01),
    ("LDA 0xFF\nADD 0x01\nLDA 0xFF\nADC 0xFF", 0xFF),
    ("LDA 0x7F\nADD 0x01\nLDA 0xFF\nADC 0xFF", 0xFE),
    # SBC: 16-bit subtract 0x1000 - 0x0001, borrow out, no borrow out, borrow in 0
    ("LDA 0x00\nSUB 0x01\nLDA 0x10\nSBC 0x00", 0x0F),
    ("LDA 0x00\nSUB 0x01\nLDA 0x00\nSBC 0x00\nLDA 0\nADC 0", 0x01),
    ("LDA 0x00\nSUB 0x01\nLDA 0x80\nSBC 0x7F\nLDA 0\nADC 0", 0x00),
    ("LDA 0x00\nSUB 0x01\nLDA 0x80\nSBC 0x7F", 0x00),
    ("LDA 0x05\nSUB 0x01\nLDA 0x09\nSBC 0x04", 0x05),
    # ROR / ROL through the carry
    ("LDA 0x01\nSHR\nLDA 0x02\nROR", 0x81),
    ("LDA 0x01\nSHR\nLDA 0x03\nROR\nLDA 0\nADC 0", 0x01),
    ("LDA 0x01\nSHR\nLDA 0x80\nROL", 0x01),
    ("LDA 0x01\nSHR\nLDA 0x80\nROL\nLDA 0\nADC 0", 0x01),
    ("LDA 0x00\nSUB 0x00\nLDA 0x41\nROL", 0x82),
    # SWAP, INC, DEC, NOT
    ("LDA 0xA5\nSWAP", 0x5A),
    ("LDA 0xFF\nINC", 0x00),
    ("LDA 0x7F\nINC", 0x80),
    ("LDA 0x00\nDEC", 0xFF),
    ("LDA 0x80\nDEC", 0x7F),
    ("LDA 0x3C\nNOT", 0xC3),
    # INC / DEC / logic leave the carry alone (even when INC wraps)
    ("LDA 0x00\nSUB 0x01\nLDA 0xFF\nINC\nLDA 0\nADC 0", 0x01),
    ("LDA 0x00\nSUB 0x00\nLDA 0xFF\nINC\nLDA 0\nADC 0", 0x00),
    ("LDA 0x00\nSUB 0x00\nLDA 0x00\nDEC\nLDA 0\nADC 0", 0x00),
    ("LDA 0x00\nSUB 0x01\nLDA 0xF0\nAND 0x3C\nLDA 0\nADC 0", 0x01),
    # X: TAX, TXA, XCHG
    ("LDA 0x9C\nTAX\nLDA 0\nTXA", 0x9C),
    ("LDA 0x11\nLDX 0x22\nXCHG", 0x22),
    ("LDA 0x11\nLDX 0x22\nXCHG\nTXA", 0x11),
    # ADDX / SUBX with carry and borrow
    ("LDA 0xF0\nLDX 0x20\nADDX", 0x10),
    ("LDA 0xF0\nLDX 0x20\nADDX\nLDA 0\nADC 0", 0x01),
    ("LDA 0x01\nLDX 0x01\nADDX\nLDA 0\nADC 0", 0x00),
    ("LDA 0x10\nLDX 0x20\nSUBX", 0xF0),
    ("LDA 0x10\nLDX 0x20\nSUBX\nLDA 0\nADC 0", 0x01),
    ("LDA 0x20\nLDX 0x10\nSUBX\nLDA 0\nADC 0", 0x00),
    # ANDX / ORX / XORX
    ("LDA 0xF0\nLDX 0x3C\nANDX", 0x30),
    ("LDA 0xA0\nLDX 0x05\nORX", 0xA5),
    ("LDA 0xFF\nLDX 0xAA\nXORX", 0x55),
    # CMPX, XADD (no flag), XCMP
    ("LDA 0x05\nLDX 0x06\nCMPX\nLDA 0\nADC 0", 0x01),
    ("LDA 0x06\nLDX 0x05\nCMPX\nLDA 0\nADC 0", 0x00),
    ("LDA 0x80\nLDX 0x7F\nCMPX\nLDA 0\nADC 0", 0x00),
    ("LDX 0xF0\nXADD 0x20\nTXA", 0x10),
    ("LDX 0xFF\nXADD 0x01\nTXA", 0x00),
    ("LDA 0x00\nSUB 0x01\nLDX 0xFF\nXADD 0x01\nLDA 0\nADC 0", 0x01),
    ("LDA 0x00\nSUB 0x00\nLDX 0xFF\nXADD 0x01\nLDA 0\nADC 0", 0x00),
    ("LDX 0x05\nXCMP 0x06\nLDA 0\nADC 0", 0x01),
    ("LDX 0x06\nXCMP 0x06\nLDA 0\nADC 0", 0x00),
]]

# (branch, setup, taken?)
BRANCH_CASES = [
    ("JC", "LDA 0\nSUB 1", True), ("JC", "LDA 0\nSUB 0", False), ("JC", "LDA 255\nADD 1", True),
    ("JC", "LDA 1\nADD 1", False),
    ("JNC", "LDA 0\nSUB 1", False), ("JNC", "LDA 0\nSUB 0", True), ("JNC", "LDA 255\nADD 1", False),
    ("JNC", "LDA 1\nADD 1", True),
    ("JZ", "LDA 0", True), ("JZ", "LDA 1", False), ("JZ", "LDA 0x80", False), ("JZ", "LDA 0xFF\nADD 1", True),
    ("JNZ", "LDA 0", False), ("JNZ", "LDA 1", True), ("JNZ", "LDA 0x80", True), ("JNZ", "LDA 0xFF\nADD 1", False),
    ("JMI", "LDA 0x80", True), ("JMI", "LDA 0xFF", True), ("JMI", "LDA 0x7F", False), ("JMI", "LDA 0", False),
    ("JPL", "LDA 0x7F", True), ("JPL", "LDA 0", True), ("JPL", "LDA 0x80", False), ("JPL", "LDA 0xFF", False),
    ("JEQ", "LDX 5\nLDA 5", True), ("JEQ", "LDX 5\nLDA 6", False), ("JEQ", "LDX 0\nLDA 0", True),
    ("JEQ", "LDX 0xFF\nLDA 0xFF", True), ("JEQ", "LDX 0\nLDA 0x80", False), ("JEQ", "LDX 0x80\nLDA 0", False),
    ("JNE", "LDX 5\nLDA 5", False), ("JNE", "LDX 5\nLDA 6", True), ("JNE", "LDX 0\nLDA 0", False),
    ("JNE", "LDX 0xFF\nLDA 0xFF", False), ("JNE", "LDX 0\nLDA 0x80", True), ("JNE", "LDX 0x80\nLDA 0", True),
]


def branch_items():
    items = []
    for i, (cond, setup, taken) in enumerate(BRANCH_CASES):
        code = f"{setup}\n{cond} t{i}\nOUT 0x11\nJMP e{i}\nt{i}: OUT 0x22\ne{i}:\n{STROBE}"
        items.append((code, 0x22 if taken else 0x11))
    return items


# direct, indexed and wrapped data memory accesses
MEMORY_SRC = "\n".join(
    [f"LDA {17 * i}\nSTM {i}" for i in range(16)]                      # mem[i] = 17 * i
    + ["LDM 0\n" + RES, "LDM 5\n" + RES, "LDM 15\n" + RES,
       "LDX 14\nLDA 0xAB\nSTMX 5",                                     # (14 + 5) & 15 = 3
       "LDX 15\nLDA 0xCD\nSTMX 15",                                    # (15 + 15) & 15 = 14
       "LDX 0xF1\nLDA 0xEF\nSTMX 3",                                   # (0xF1 + 3) & 15 = 4
       "LDM 3\n" + RES, "LDM 14\n" + RES, "LDM 4\n" + RES,
       "LDX 14\nLDMX 5\n" + RES, "LDX 13\nLDMX 1\n" + RES, "LDX 0xF0\nLDMX 4\n" + RES,
       "LDX 0\nLDMX 0\n" + RES, "LDX 7\nLDMX 9\n" + RES,               # mem[0] and (7 + 9) & 15 = 0
       "LDA 0x12\nSTM 6\nLDX 1\nLDA 0\nLDMX 5\n" + RES,                # (1 + 5) = 6 -> 0x12
       "HALT"])
MEMORY_EXPECT = [0x00, 0x55, 0xFF,       # direct reads
                 0xAB, 0xCD, 0xEF,       # where the wrapped writes went
                 0xAB, 0xCD, 0xEF,       # the wrapped reads
                 0x00, 0x00,             # mem[0] still 0; mem[0] again via (7 + 9) & 15
                 0x12]

TWO_THREADS_SRC = f"""
        SPAWN 1, t1
        LDA 0x11
        STM 3
        RECV
{RES}
        RECV
{RES}
        LDM 3
{RES}
        HALT
t1:     WAIT 10
        LDM 3
        SEND
        LDA 0x22
        STM 3
        LDM 3
        SEND
        HALT
"""

FOUR_THREADS_SRC = f"""
        SPAWN 1, u1
        SPAWN 2, u2
        SPAWN 3, u3
        LDA 0x60
        STM 0
        WAIT 40
        LDM 0
{RES}
        RECV
{RES}
        RECV
{RES}
        RECV
{RES}
        HALT
u1:     LDA 0x61
        STM 0
        WAIT 60
        LDM 0
        SEND
        HALT
u2:     LDA 0x62
        STM 0
        WAIT 100
        LDM 0
        SEND
        HALT
u3:     LDA 0x63
        STM 0
        WAIT 140
        LDM 0
        SEND
        HALT
"""

# memory is kept by HALT and SPAWN: thread 1 writes, halts and is started again
KEPT_SRC = f"""
        SPAWN 1, a1
        WAIT 30
        SPAWN 1, a2
        RECV
{RES}
        HALT
a1:     LDA 0x77
        STM 0
        HALT
a2:     LDM 0
        SEND
        HALT
"""

# memory is cleared by the reset that every load starts with: the first program leaves values in mem[7] of all
# four threads, the second one (loaded afterwards) reads them back and must see zeros
DIRTY_SRC = """
        SPAWN 1, d1
        SPAWN 2, d2
        SPAWN 3, d3
        LDA 0x5C
        STM 7
        HALT
d1:     LDA 0xA3
        STM 7
        HALT
d2:     LDA 0xB4
        STM 7
        HALT
d3:     LDA 0xC5
        STM 7
        HALT
"""
CLEAN_SRC = f"""
        SPAWN 1, c1
        SPAWN 2, c2
        SPAWN 3, c3
        LDM 7
{RES}
        RECV
{RES}
        RECV
{RES}
        RECV
{RES}
        HALT
c1:     LDM 7
        SEND
        HALT
c2:     WAIT 20
        LDM 7
        SEND
        HALT
c3:     WAIT 40
        LDM 7
        SEND
        HALT
"""

LOOKUP_ITEMS = [(f"LDA {i}\nADD tab\nCALLA\n{RES}", v) for i, v in enumerate([0x10, 0x21, 0x42, 0x84])]
LOOKUP_SRC = "\n".join(c for c, _ in LOOKUP_ITEMS) + """
        LDA tj
        JMPA
        OUT 0x11
        JMP e1
tj:     OUT 0x22
e1:
""" + STROBE + """
        LDA 2
        ADD jt
        JMPA
jt:     JMP a0
        JMP a1
        JMP a2
a0:     OUT 0xA0
        JMP e2
a1:     OUT 0xA1
        JMP e2
a2:     OUT 0xA2
e2:
""" + STROBE + """
        HALT
tab:    RETL 0x10
        RETL 0x21
        RETL 0x42
        RETL 0x84
"""
LOOKUP_EXPECT = [0x10, 0x21, 0x42, 0x84, 0x22, 0xA2]

# input pins: ui = 0x3C (input pins 0-7), uio = 0xCA (input pins 8-15; bit 5 is LOAD, which must be low)
WIDE_UI, WIDE_UIO = 0x3C, 0xCA
WIDE_SRC = f"""
        WAIT 4
        IN
{RES}
        INH
{RES}
        INH
        XOR 0xFF
{RES}
        HALT
"""
WIDE_EXPECT = [0x3C, 0xCA, 0x35]      # IN, INH, INH ^ 0xFF

# ODRVA: a 0 bit pulls the pad low (uio_oe bit = 1), a 1 bit releases it. 0xA6 = 1010 0110 -> pads 0, 3, 4, 6 pulled
# (0x59); 0xF0 -> 0x0F; 0x00 -> all pulled (0xFF); ODRV 3 1 releases pad 3 (0xF7), ODRV 5 1 pad 5 (0xD7);
# ODRVA 0xFF replaces all eight at once: everything released (0x00).
ODRVA_SRC = """
        LDA 0xA6
        ODRVA
        LDA 0xF0
        ODRVA
        LDA 0x00
        ODRVA
        ODRV 3 1
        ODRV 5 1
        LDA 0xFF
        ODRVA
        HALT
"""
ODRVA_OE_SEQ = [0x00, 0x59, 0x0F, 0xFF, 0xF7, 0xD7, 0x00]      # first sample (reset value), then each change

# undefined sub-opcodes are NOPs: none of these words may change A or X or jump anywhere
UNDEFINED_WORDS = [0x4900, 0x4A05, 0x4F00, 0x6900, 0x6FFF, 0x9300, 0x9F55, 0xB400, 0xBF12, 0xFE00, 0xFFFF]
UNDEF_SRC = ("LDA 0x55\nLDX 0x66\n" + "NOP\n" * len(UNDEFINED_WORDS) + RES + "\nTXA\n" + RES + "\nOUT 0x77\nHALT")
UNDEF_RAW = {2 + i: w for i, w in enumerate(UNDEFINED_WORDS)}


def group_programs():
    ui, uio = WIDE_UI, WIDE_UIO
    return {
        "alu": items_to_programs("alu", ALU_ITEMS),
        "branches": items_to_programs("branch", branch_items()),
        "memory": [Prog("memory", MEMORY_SRC, expect=MEMORY_EXPECT, final_uo=0x12)],
        "memory-threads": [
            Prog("two-threads", TWO_THREADS_SRC, expect=[0x00, 0x22, 0x11], final_uo=0x11, extra_turns=40),
            Prog("four-threads", FOUR_THREADS_SRC, expect=[0x60, 0x61, 0x62, 0x63], final_uo=0x63,
                 extra_turns=200),
            Prog("kept", KEPT_SRC, expect=[0x77], final_uo=0x77, extra_turns=40),
            Prog("dirty", DIRTY_SRC, extra_turns=5),
            Prog("clean", CLEAN_SRC, expect=[0x00, 0x00, 0x00, 0x00], final_uo=0x00, extra_turns=80),
        ],
        "jumps": [Prog("lookup", LOOKUP_SRC, expect=LOOKUP_EXPECT, final_uo=0xA2, extra_turns=10)],
        "wide": [
            Prog("wide-in", WIDE_SRC, expect=WIDE_EXPECT, final_uo=0x35, ui=ui, uio=uio, extra_turns=10),
            Prog("odrva", ODRVA_SRC, oe_seq=ODRVA_OE_SEQ),
        ],
        "undefined": [Prog("undefined", UNDEF_SRC, expect=[0x55, 0x66], final_uo=0x77, raw=UNDEF_RAW)],
    }


# ---- schedule test programs. Thread t toggles pin t + 1 of uo_out; `turns` = instructions per period ---------

ZOO1_SRC = """
        SPAWN 1, z1
        SPAWN 2, z2
        SPAWN 3, z3
z0:     PIN 1 1
        PIN 1 0
        JMP z0
z1:     PIN 2 1
        CALL f1
        PIN 2 0
        JMP z1
f1:     RET
z2:     PIN 3 1
        LDA f2
        CALLA
        PIN 3 0
        JMP z2
f2:     RETL 7
z3:     PIN 4 1
        LDA z3b
        JMPA
        NOP
z3b:    PIN 4 0
        LDA z3
        JMPA
"""
ZOO1_TURNS = {0: 3, 1: 5, 2: 6, 3: 6}      # PIN PIN JMP / PIN CALL RET PIN JMP / PIN LDA CALLA RETL PIN JMP /
#                                            PIN LDA JMPA PIN LDA JMPA
ZOO1_WINDOW = 2000

ZOO2_SRC = """
        SPAWN 1, y1
        SPAWN 2, y2
        SPAWN 3, y3
y0:     PIN 1 1
        LDA 0
        JZ y0b
        NOP
y0b:    PIN 1 0
        LDA 1
        JNZ y0
y1:     PIN 2 1
        WAIT 4
        PIN 2 0
        WAIT 4
        JMP y1
y2:     PIN 3 1
        LDX 0
y2w:    DJNZ y2w
        PIN 3 0
        JMP y2
y3:     PIN 4 1
        PIN 4 0
        JMP y3
"""
# PIN LDA JZ(taken) PIN LDA JNZ(taken) = 6 / PIN WAIT4 (5 turns) PIN WAIT4 (5 turns) JMP = 13 /
# PIN LDX 256 x DJNZ PIN JMP = 260 / PIN PIN JMP = 3
ZOO2_TURNS = {0: 6, 1: 13, 2: 260, 3: 3}
ZOO2_WINDOW = 4600

SPACING_SRC = "\n".join(f"PIN 1 {(i + 1) % 2}" for i in range(120)) + "\nHALT"     # PIN 1 1, PIN 1 0, ... 120 changes
SPACING_WINDOW = 800

# (schedule, clocks per instruction of each thread: 4 with one slot, 2 with two slots, None if not scheduled)
SCHEDULE_CASES = {
    "default": ((0, 1, 2, 3), {0: 4, 1: 4, 2: 4, 3: 4}),
    "0102": ((0, 1, 0, 2), {0: 2, 1: 4, 2: 4, 3: None}),
    "0101": ((0, 1, 0, 1), {0: 2, 1: 2, 2: None, 3: None}),
    "0203": ((0, 2, 0, 3), {0: 2, 1: None, 2: 4, 3: 4}),
    "1010": ((1, 0, 1, 0), {0: 2, 1: 2, 2: None, 3: None}),
    "2030": ((2, 0, 3, 0), {0: 2, 1: None, 2: 4, 3: 4}),
    "3230": ((3, 2, 3, 0), {0: 4, 1: None, 2: 4, 3: 2}),
}
# a thread owns two adjacent slots (slot 3 and slot 0 are adjacent too): the chip ignores these and runs 0 1 2 3
ILLEGAL_SCHEDULES = [(0, 0, 1, 2), (0, 1, 2, 0), (1, 2, 3, 3), (1, 1, 1, 1), (3, 3, 3, 3)]

# One-instruction DJNZ loop (LDX 0: 256 turns), WAIT n (1 + n turns) and taken branches cost one turn each.
# PIN(1) LDX(1) DJNZ x256 PIN(1) = the first fall is 258 turns after the first rise; WAIT 9 is 10 turns, so PIN 1 1 is
# 11 turns after the fall; the next PIN 1 0 is 1 turn later.
TIMING_SRC = "PIN 1 1\nLDX 0\nL: DJNZ L\nPIN 1 0\nWAIT 9\nPIN 1 1\nPIN 1 0\nHALT"
TIMING_TURNS = [258, 11, 1]
TIMING_WINDOW = 1400
TIMING_CASES = [((0, 1, 2, 3), 4), ((0, 1, 0, 1), 2), ((0, 1, 0, 2), 2)]


def check_period(uo, thread, turns, cpt, label):
    """Thread `thread` toggles pin thread + 1 with `turns` instructions per period. With `cpt` clocks per
    instruction (None: the thread never runs) the period must be turns * cpt clocks, every time."""
    rises = rise_clocks(uo, thread + 1)
    if cpt is None:
        if rises or any((v >> (thread + 1)) & 1 for v in uo):
            return [f"{label}: thread {thread} must never run, but pin {thread + 1} toggled"]
        return []
    diffs = [b - a for a, b in zip(rises, rises[1:])]
    if len(rises) < 3 or set(diffs) != {turns * cpt}:
        return [f"{label}: thread {thread} period {sorted(set(diffs))} clocks over {len(rises)} rises, "
                f"expected {turns} instructions x {cpt} clocks = {turns * cpt}"]
    return []


def check_spacing(uo, cpt, label):
    ch = change_clocks(uo, 1)
    diffs = [b - a for a, b in zip(ch, ch[1:])]
    if len(ch) < 100 or set(diffs) != {cpt}:
        return [f"{label}: thread 0 instruction spacing {sorted(set(diffs))} clocks over {len(ch)} changes, "
                f"expected {cpt}"]
    return []


# ======================================================================================
# testbench side
# ======================================================================================

async def boot(dut):
    cocotb.start_soon(Clock(dut.clk, CLK_NS, unit="ns").start())
    dut.ena.value = 1
    dut.ui_in.value = 0
    dut.uio_in.value = 0
    dut.rst_n.value = 0
    await ClockCycles(dut.clk, 10)


async def start_prog(dut, words, sched=None, ui=0, uio=0, rng=None):
    """Reset the chip, load program (and schedule) over the pins, release LOAD with the given pin values."""
    dut.rst_n.value = 0
    dut.ui_in.value = 0
    dut.uio_in.value = 0
    await ClockCycles(dut.clk, 10)
    dut.rst_n.value = 1
    await ClockCycles(dut.clk, 2)
    await load_program(dut, words, rng, sched)
    dut.ui_in.value = ui          # LOAD low and the pins the program expects, at the same time
    dut.uio_in.value = uio


async def capture(dut, clocks):
    """uo_out and uio_oe on `clocks` consecutive rising edges."""
    uo, oe = [], []
    for _ in range(clocks):
        await RisingEdge(dut.clk)
        uo.append(int(dut.uo_out.value))
        oe.append(int(dut.uio_oe.value))
    return uo, oe


async def run_prog(dut, prog, rng=None):
    await start_prog(dut, prog.words, prog.sched, prog.ui, prog.uio, rng)
    uo, oe = await capture(dut, prog.window)
    return check_prog(prog, uo, oe)


async def run_group(dut, group):
    await boot(dut)
    errs = []
    for prog in group_programs()[group]:
        errs += await run_prog(dut, prog)
    assert not errs, "\n".join(errs)


@cocotb.test()
async def test_alu(dut):
    """ADD SUB AND OR XOR CMP ADC SBC ROR ROL INC DEC NOT SWAP TAX TXA XCHG ADDX SUBX ANDX ORX XORX CMPX XADD
    XCMP: results and carry / borrow, 16-bit chains, flags that must not change."""
    await run_group(dut, "alu")


@cocotb.test()
async def test_conditional_branches(dut):
    """JC JNC JZ JNZ JMI JPL JEQ JNE, each taken and not taken."""
    await run_group(dut, "branches")


@cocotb.test()
async def test_data_memory(dut):
    """LDM STM LDMX STMX: all 16 bytes, and the wraparound of (X + off) & 15."""
    await run_group(dut, "memory")


@cocotb.test()
async def test_data_memory_is_per_thread(dut):
    """Every thread has its own 16 bytes; HALT and SPAWN keep them, a load (reset) clears them."""
    await run_group(dut, "memory-threads")


@cocotb.test()
async def test_computed_jumps_and_lookup_table(dut):
    """CALLA into a table of RETL (lookup), JMPA, and a JMPA jump table."""
    await run_group(dut, "jumps")


@cocotb.test()
async def test_wide_pin_access(dut):
    """IN, INH (the uio pads read back), OUTA, ODRVA."""
    await run_group(dut, "wide")


@cocotb.test()
async def test_undefined_subopcodes_are_nop(dut):
    """Words with undefined sub-opcodes execute as NOP."""
    await run_group(dut, "undefined")


# ---- schedules ------------------------------------------------------------------------------------------------

def check_timing(uo, cpt, label):
    ch = change_clocks(uo, 1)
    diffs = [b - a for a, b in zip(ch, ch[1:])]
    if len(ch) != 4 or diffs != [n * cpt for n in TIMING_TURNS]:
        return [f"{label}: intervals {diffs} clocks over {len(ch)} changes, expected "
                f"{[n * cpt for n in TIMING_TURNS]}"]
    return []


async def check_schedule(dut, sched, cpt, rng=None):
    """Load ZOO1 / ZOO2 / SPACING with `sched` (None: no schedule byte) and check the clocks per instruction of
    every thread: `cpt[t]` = 4, 2, or None (thread t never runs). The caller has called boot()."""
    errs = []
    for name, src, turns, window in (("zoo1", ZOO1_SRC, ZOO1_TURNS, ZOO1_WINDOW),
                                     ("zoo2", ZOO2_SRC, ZOO2_TURNS, ZOO2_WINDOW)):
        await start_prog(dut, assemble(src), sched, rng=rng)
        uo, _ = await capture(dut, window)
        for t in range(4):
            errs += check_period(uo, t, turns[t], cpt[t], f"{name} sched={sched}")
    await start_prog(dut, assemble(SPACING_SRC), sched, rng=rng)
    uo, _ = await capture(dut, SPACING_WINDOW)
    errs += check_spacing(uo, cpt[0], f"spacing sched={sched}")
    assert not errs, "\n".join(errs)


@cocotb.test()
async def test_schedule_default_loaded_explicitly(dut):
    """0 1 2 3: every thread one instruction per 4 clocks."""
    await boot(dut)
    await check_schedule(dut, *SCHEDULE_CASES["default"])


@cocotb.test()
async def test_schedule_0102(dut):
    """0 1 0 2: thread 0 owns slots 0 and 2, so it runs every 2 clocks (twice as fast); threads 1 and 2 as
    before; thread 3 is not scheduled, so it never runs although thread 0 spawns it."""
    await boot(dut)
    await check_schedule(dut, *SCHEDULE_CASES["0102"])


@cocotb.test()
async def test_schedule_0101(dut):
    """0 1 0 1: threads 0 and 1 run every 2 clocks; threads 2 and 3 never run. Sloppy host timing."""
    await boot(dut)
    await check_schedule(dut, *SCHEDULE_CASES["0101"], rng=random.Random(11))


@cocotb.test()
async def test_schedule_0203(dut):
    """0 2 0 3: thread 0 fast; threads 2 and 3 normal; thread 1 never runs."""
    await boot(dut)
    await check_schedule(dut, *SCHEDULE_CASES["0203"])


@cocotb.test()
async def test_schedule_1010(dut):
    """1 0 1 0: thread 0 owns the odd slots (and so starts on an odd clock); both threads 0 and 1 are fast."""
    await boot(dut)
    await check_schedule(dut, *SCHEDULE_CASES["1010"])


@cocotb.test()
async def test_schedule_2030(dut):
    """2 0 3 0: thread 0 owns slots 1 and 3 (fast); threads 2 and 3 have one slot each; thread 1 never runs."""
    await boot(dut)
    await check_schedule(dut, *SCHEDULE_CASES["2030"])


@cocotb.test()
async def test_schedule_3230(dut):
    """3 2 3 0: thread 3 owns two slots, so it is the fast one; thread 0 has one slot (4 clocks); thread 1 never
    runs."""
    await boot(dut)
    await check_schedule(dut, *SCHEDULE_CASES["3230"])


@cocotb.test()
async def test_illegal_schedule_leaves_the_default(dut):
    """A thread that owns two adjacent slots (also slot 3 and slot 0) is ignored by the chip: it keeps 0 1 2 3."""
    await boot(dut)
    for bad in ILLEGAL_SCHEDULES:
        await check_schedule(dut, bad, SCHEDULE_CASES["default"][1], rng=random.Random(sum(bad)))


@cocotb.test()
async def test_no_schedule_byte_means_the_default(dut):
    """512 bytes without the 513th: default schedule (even right after a test that loaded a fast one)."""
    await boot(dut)
    await check_schedule(dut, *SCHEDULE_CASES["0101"])
    await check_schedule(dut, None, SCHEDULE_CASES["default"][1])


@cocotb.test()
async def test_schedule_without_thread_0_runs_nothing(dut):
    """At reset only thread 0 runs; with a schedule that never gives it a clock nothing ever happens."""
    await boot(dut)
    await start_prog(dut, assemble("PIN 1 1\nPIN 2 1\nSPAWN 1, 0\nHALT"), (1, 2, 1, 3))
    uo, _ = await capture(dut, 400)
    assert set(uo) == {0x01}, f"uo_out changed: {sorted(set(uo))}"


@cocotb.test()
async def test_branch_and_wait_timing_on_a_fast_schedule(dut):
    """A one-instruction DJNZ loop, WAIT n (1 + n turns) and taken branches cost one turn each, so the pulse of
    PIN 1 1 ... PIN 1 0 lasts 258 turns, etc. (see TIMING_SRC): 4 clocks per turn on one slot, 2 on two slots."""
    await boot(dut)
    errs = []
    for sched, cpt in TIMING_CASES:
        await start_prog(dut, assemble(TIMING_SRC), sched)
        uo, _ = await capture(dut, TIMING_WINDOW)
        errs += check_timing(uo, cpt, f"timing sched={sched}")
    assert not errs, "\n".join(errs)
