# SPDX-License-Identifier: Apache-2.0
"""Directed tests of the ISA v2 reference model (test/isa_model.py) and of the assembler's v2 encodings.

Pure Python, no simulator:  python3 -m pytest test/test_isa_v2_model.py -q   (from the repo root)

Every expected value below is a literal worked out by hand from docs/isa.md; none is computed with the
same expression as the model. The model is the oracle of the RTL differential test, so it must be right.

Timing convention (docs/isa.md, 'Timing and the schedule'; isa_model.Model): at clock edge c the fetch
stage looks at thread sched[c % 4] and samples that thread's running flag before edge c's updates; the
instruction fetched at edge c executes at edge c+1. With the default schedule thread 0 therefore executes
at edges 1, 5, 9, ..., thread 1 at edges 2, 6, 10, ..., and so on.
"""
import sys
import zlib
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "tools"))
import asm  # noqa: E402
from isa_model import (DEFAULT_SCHEDULE, STATE_NAMES, Model, effective_schedule,  # noqa: E402
                       gen_program, gen_schedule, valid_schedule)

DEFAULT = (0, 1, 2, 3)


# ----------------------------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------------------------

class TracedModel(Model):
    """Model that also records every executed turn: log[i] = (edge, thread, pc, A, X, C) with the
    registers of that thread AFTER the turn; pin_log = (edge, uo_out) at every change of the pins."""

    def __init__(self, rom, sched=DEFAULT):
        self.log = []
        self.pin_log = []
        super().__init__(rom, sched)

    def execute(self, t, pins):
        pc = self.pc[t]
        super().execute(t, pins)
        self.log.append((self.cycle - 1, t, pc, self.a[t], self.x[t], self.c[t]))

    def clock(self, pins_driven):
        before = self.pins_out
        super().clock(pins_driven)
        if self.pins_out != before:
            self.pin_log.append((self.cycle - 1, self.pins_out))


def words_of(src, raw=None):
    words, _ = asm.assemble(src)
    for i, w in (raw or {}).items():
        words[i] = w
    return words


def run(src, sched=DEFAULT, pins=0, edges=None, raw=None, max_edges=20000):
    """Assemble `src` and clock the model: for `edges` edges, or (default) until every thread has halted."""
    m = TracedModel(words_of(src, raw), sched)
    if edges is not None:
        m.run(edges, pins)
        return m
    for _ in range(max_edges):
        m.clock(pins)
        if not any(m.act):
            return m
    raise AssertionError("program did not halt")


def carry_prefix(cin):
    """Instructions that leave C = cin (and A = 0)."""
    return "LDA 0\nSUB 1\n" if cin else "LDA 0\nSUB 0\n"


def acc_case(a, x, cin, op):
    """Run `op` with A = a, X = x, C = cin; returns the model after the program has halted."""
    return run(f"{carry_prefix(cin)}LDX {x}\nLDA {a}\n{op}\nHALT")


# ----------------------------------------------------------------------------------------------
# assembler
# ----------------------------------------------------------------------------------------------

ENCODINGS = [
    # version 1, unchanged
    ("PIN 3 1", 0x1301), ("OUT 0xA5", 0x20A5), ("WAIT 0x123", 0x3123), ("JMP 5", 0x4005),
    ("JPIN 3 1 0x85", 0x5985), ("JPIN 3 0 0x85", 0x0985), ("LDA 0x12", 0x6012), ("SHR", 0x7000),
    ("OUTC 3", 0x8300), ("LDX 0x12", 0x9012), ("DJNZ 7", 0xA007), ("IN", 0xB000), ("SHL", 0xC000),
    ("INB 9", 0xD480), ("CALL 0x12", 0xE012), ("HALT", 0xF000), ("RET", 0xF100),
    ("ODRV 2 1", 0xF20A), ("ODRVC 5", 0xF305), ("NOP", 0xF400), ("SEND", 0xF500), ("RECV", 0xF600),
    ("SPAWN 2 0x34", 0xFA34),
    # branches 0100 cccc aaaa aaaa
    ("JC 5", 0x4105), ("JNC 5", 0x4205), ("JZ 5", 0x4305), ("JNZ 5", 0x4405), ("JMI 5", 0x4505),
    ("JPL 5", 0x4605), ("JEQ 5", 0x4705), ("JNE 5", 0x4805), ("JNE 255", 0x48FF),
    # accumulator with an immediate 0110 oooo iiii iiii
    ("ADD 0x12", 0x6112), ("SUB 0x12", 0x6212), ("AND 0x12", 0x6312), ("OR 0x12", 0x6412),
    ("XOR 0x12", 0x6512), ("CMP 0x12", 0x6612), ("ADC 0x12", 0x6712), ("SBC 0x12", 0x6812),
    ("SBC 255", 0x68FF),
    # accumulator and X 0111 ffff ---- ----
    ("ROR", 0x7100), ("ROL", 0x7200), ("INC", 0x7300), ("DEC", 0x7400), ("NOT", 0x7500),
    ("CMPX", 0x7600), ("SWAP", 0x7700), ("TAX", 0x7800), ("TXA", 0x7900), ("XCHG", 0x7A00),
    ("ADDX", 0x7B00), ("SUBX", 0x7C00), ("ANDX", 0x7D00), ("ORX", 0x7E00), ("XORX", 0x7F00),
    # X with an immediate 1001 oooo iiii iiii
    ("XADD 0x12", 0x9112), ("XCMP 0x12", 0x9212),
    # wide pin access 1011 ffff ---- ----
    ("INH", 0xB100), ("OUTA", 0xB200), ("ODRVA", 0xB300),
    # data memory and computed jumps
    ("LDM 5", 0xFC05), ("LDMX 5", 0xFC45), ("STM 5", 0xFC85), ("STMX 5", 0xFCC5),
    ("LDM 15", 0xFC0F), ("STMX 15", 0xFCCF), ("JMPA", 0xFD00), ("CALLA", 0xFD01),
    ("RETL 0x34", 0xF734), ("RETL 255", 0xF7FF),
]


@pytest.mark.parametrize("line,word", ENCODINGS)
def test_encoding(line, word):
    assert asm.assemble(line)[0] == [word]


@pytest.mark.parametrize("line", ["ADD 256", "ADD -1", "SBC 256", "CMP 300", "XADD 256", "XCMP -1",
                                  "RETL 256", "LDM 16", "STM 16", "LDMX 16", "STMX 16", "JC 256",
                                  "JNE 256", "JEQ -1"])
def test_operand_out_of_range(line):
    with pytest.raises(SystemExit) as e:
        asm.assemble(line)
    assert "out of range" in str(e.value)


@pytest.mark.parametrize("line", ["JMPA 1", "CALLA 0", "INH 1", "OUTA 1", "ODRVA 1", "ROR 1", "SWAP 2",
                                  "ADD", "LDM", "RETL", "JC", "XADD"])
def test_operand_count(line):
    with pytest.raises(SystemExit) as e:
        asm.assemble(line)
    assert "operand" in str(e.value)


def test_labels_work_for_new_branches():
    words, labels = asm.assemble("start: JC end\nJNZ start\nend: JMPA")
    assert labels == {"start": 0, "end": 2}
    assert words == [0x4102, 0x4400, 0xFD00]


# Golden checksums of the words the version 1 assembler produced for the existing firmware (taken before
# the v2 mnemonics were added): (number of words, CRC32 of the little-endian bytes).
V1_FIRMWARE = {
    "rom": (["main", "uart_tx", "spi_master", "i2c_master", "i2c_monitor", "echo"], 158, 0x55AFECD0),
    "i2c_target": (["examples/i2c_target"], 181, 0x84A5FB57),
    "jtag": (["examples/jtag"], 96, 0xFEE88503),
    "midi_tx": (["examples/midi_tx"], 48, 0x5B26C938),
    "onewire": (["examples/onewire"], 103, 0x3EC73ED8),
    "ps2_rx": (["examples/ps2_rx"], 64, 0xC97B09C2),
    "spi_target": (["examples/spi_target"], 252, 0xC1A6CD64),
    "uart_rx": (["examples/uart_rx"], 29, 0x8912A972),
    "ws2812": (["examples/ws2812"], 90, 0x55B68177),
}


@pytest.mark.parametrize("name", sorted(V1_FIRMWARE))
def test_v1_firmware_encodes_as_before(name):
    parts, n, crc = V1_FIRMWARE[name]
    text = "\n".join((ROOT / "firmware" / f"{p}.asm").read_text() for p in parts)
    words, _ = asm.assemble(text)
    assert len(words) == n
    assert zlib.crc32(b"".join(w.to_bytes(2, "little") for w in words)) == crc


# ----------------------------------------------------------------------------------------------
# ALU with an immediate
# ----------------------------------------------------------------------------------------------

# (A, imm, expected A, expected C)
ADD_CASES = [
    (0x00, 0x00, 0x00, 0), (0x00, 0x01, 0x01, 0), (0x7F, 0x01, 0x80, 0), (0x7F, 0x7F, 0xFE, 0),
    (0x7F, 0x80, 0xFF, 0), (0x80, 0x80, 0x00, 1), (0x80, 0xFF, 0x7F, 1), (0xFF, 0x01, 0x00, 1),
    (0xFF, 0xFF, 0xFE, 1), (0xFF, 0x00, 0xFF, 0), (0x01, 0xFF, 0x00, 1),
]
SUB_CASES = [
    (0x00, 0x00, 0x00, 0), (0x00, 0x01, 0xFF, 1), (0x01, 0x01, 0x00, 0), (0x01, 0x00, 0x01, 0),
    (0x80, 0x01, 0x7F, 0), (0x7F, 0x80, 0xFF, 1), (0x7F, 0xFF, 0x80, 1), (0x80, 0xFF, 0x81, 1),
    (0xFF, 0x7F, 0x80, 0), (0xFF, 0xFF, 0x00, 0), (0x80, 0x80, 0x00, 0), (0x00, 0xFF, 0x01, 1),
]
# (A, imm, expected C); A is unchanged
CMP_CASES = [
    (0x00, 0x00, 0), (0x00, 0x01, 1), (0x01, 0x00, 0), (0x7F, 0x80, 1), (0x80, 0x7F, 0),
    (0xFF, 0xFF, 0), (0xFE, 0xFF, 1), (0x00, 0xFF, 1), (0xFF, 0x00, 0), (0x80, 0x80, 0),
]
# (A, imm, carry in, expected A, expected C)
ADC_CASES = [
    (0x00, 0x00, 0, 0x00, 0), (0x00, 0x00, 1, 0x01, 0), (0xFF, 0x00, 1, 0x00, 1),
    (0xFF, 0x00, 0, 0xFF, 0), (0xFF, 0xFF, 1, 0xFF, 1), (0xFF, 0xFF, 0, 0xFE, 1),
    (0x7F, 0x80, 1, 0x00, 1), (0x7F, 0x80, 0, 0xFF, 0), (0x7F, 0x00, 1, 0x80, 0),
    (0x80, 0x7F, 1, 0x00, 1), (0x01, 0x01, 1, 0x03, 0), (0x80, 0x80, 1, 0x01, 1),
]
SBC_CASES = [
    (0x00, 0x00, 0, 0x00, 0), (0x00, 0x00, 1, 0xFF, 1), (0x01, 0x00, 1, 0x00, 0),
    (0x00, 0xFF, 1, 0x00, 1), (0x00, 0xFF, 0, 0x01, 1), (0xFF, 0xFF, 1, 0xFF, 1),
    (0xFF, 0xFF, 0, 0x00, 0), (0x80, 0x7F, 1, 0x00, 0), (0x80, 0x80, 1, 0xFF, 1),
    (0x7F, 0x80, 0, 0xFF, 1), (0x05, 0x03, 1, 0x01, 0), (0x80, 0x7F, 0, 0x01, 0),
]


@pytest.mark.parametrize("cin", [0, 1])
@pytest.mark.parametrize("a,imm,ea,ec", ADD_CASES)
def test_add(a, imm, ea, ec, cin):                  # the old carry does not matter for ADD
    m = acc_case(a, 0, cin, f"ADD {imm}")
    assert (m.a[0], m.c[0]) == (ea, ec)


@pytest.mark.parametrize("cin", [0, 1])
@pytest.mark.parametrize("a,imm,ea,ec", SUB_CASES)
def test_sub(a, imm, ea, ec, cin):
    m = acc_case(a, 0, cin, f"SUB {imm}")
    assert (m.a[0], m.c[0]) == (ea, ec)


@pytest.mark.parametrize("cin", [0, 1])
@pytest.mark.parametrize("a,imm,ec", CMP_CASES)
def test_cmp(a, imm, ec, cin):
    m = acc_case(a, 7, cin, f"CMP {imm}")
    assert (m.a[0], m.x[0], m.c[0]) == (a, 7, ec)


@pytest.mark.parametrize("a,imm,cin,ea,ec", ADC_CASES)
def test_adc(a, imm, cin, ea, ec):
    m = acc_case(a, 0, cin, f"ADC {imm}")
    assert (m.a[0], m.c[0]) == (ea, ec)


@pytest.mark.parametrize("a,imm,cin,ea,ec", SBC_CASES)
def test_sbc(a, imm, cin, ea, ec):
    m = acc_case(a, 0, cin, f"SBC {imm}")
    assert (m.a[0], m.c[0]) == (ea, ec)


@pytest.mark.parametrize("cin", [0, 1])
@pytest.mark.parametrize("op,a,imm,ea", [
    ("AND", 0xF0, 0x3C, 0x30), ("AND", 0xFF, 0x00, 0x00), ("AND", 0xAA, 0x55, 0x00),
    ("AND", 0xFF, 0xFF, 0xFF), ("OR", 0xF0, 0x0F, 0xFF), ("OR", 0x00, 0x00, 0x00),
    ("OR", 0xA0, 0x05, 0xA5), ("XOR", 0xFF, 0xAA, 0x55), ("XOR", 0x55, 0x55, 0x00),
    ("XOR", 0x0F, 0xF0, 0xFF), ("XOR", 0x80, 0x01, 0x81)])
def test_logic_immediate_keeps_carry(op, a, imm, ea, cin):
    m = acc_case(a, 0, cin, f"{op} {imm}")
    assert (m.a[0], m.c[0]) == (ea, cin)


@pytest.mark.parametrize("cin", [0, 1])
def test_lda_ldx_keep_carry(cin):
    m = run(f"{carry_prefix(cin)}LDA 0x99\nLDX 0x88\nHALT")
    assert (m.a[0], m.x[0], m.c[0]) == (0x99, 0x88, cin)


def test_adc_chain_16_bit_add():
    # 0x12FF + 0x0101 = 0x1400, no carry out; the low byte is stored in mem[0], the high byte in mem[1]
    m = run("LDA 0xFF\nADD 0x01\nSTM 0\nLDA 0x12\nADC 0x01\nSTM 1\nHALT")
    assert (m.mem[0][0], m.mem[0][1], m.c[0]) == (0x00, 0x14, 0)
    # 0xFFFF + 0x0001 = 0x0000 with carry out
    m = run("LDA 0xFF\nADD 0x01\nSTM 0\nLDA 0xFF\nADC 0x00\nSTM 1\nHALT")
    assert (m.mem[0][0], m.mem[0][1], m.c[0]) == (0x00, 0x00, 1)
    # 0x7F80 + 0x0080 = 0x8000
    m = run("LDA 0x80\nADD 0x80\nSTM 0\nLDA 0x7F\nADC 0x00\nSTM 1\nHALT")
    assert (m.mem[0][0], m.mem[0][1], m.c[0]) == (0x00, 0x80, 0)


def test_sbc_chain_16_bit_subtract():
    # 0x1000 - 0x0001 = 0x0FFF
    m = run("LDA 0x00\nSUB 0x01\nSTM 0\nLDA 0x10\nSBC 0x00\nSTM 1\nHALT")
    assert (m.mem[0][0], m.mem[0][1], m.c[0]) == (0xFF, 0x0F, 0)
    # 0x0000 - 0x0001 = 0xFFFF with borrow out
    m = run("LDA 0x00\nSUB 0x01\nSTM 0\nLDA 0x00\nSBC 0x00\nSTM 1\nHALT")
    assert (m.mem[0][0], m.mem[0][1], m.c[0]) == (0xFF, 0xFF, 1)
    # 0x0200 - 0x0180 = 0x0080: low 00-80 borrows, high 02-01-1 = 0
    m = run("LDA 0x00\nSUB 0x80\nSTM 0\nLDA 0x02\nSBC 0x01\nSTM 1\nHALT")
    assert (m.mem[0][0], m.mem[0][1], m.c[0]) == (0x80, 0x00, 0)


def test_16_bit_compare_with_cmp_and_sbc():
    # 0x0100 < 0x00FF? no. Low bytes first: CMP sets C = 00 < FF = 1, then high: 01 - 00 - 1 = 0, no borrow
    m = run("LDA 0x00\nCMP 0xFF\nLDA 0x01\nSBC 0x00\nHALT")
    assert (m.a[0], m.c[0]) == (0x00, 0)
    # 0x00FF < 0x0100? yes: CMP: FF < 00 no, C = 0; high: 00 - 01 - 0 borrows
    m = run("LDA 0xFF\nCMP 0x00\nLDA 0x00\nSBC 0x01\nHALT")
    assert (m.a[0], m.c[0]) == (0xFF, 1)


# ----------------------------------------------------------------------------------------------
# accumulator and X, no operand
# ----------------------------------------------------------------------------------------------

def test_shr_shl_unchanged():
    m = run("LDA 0x81\nSHR\nHALT")
    assert (m.a[0], m.c[0]) == (0x40, 1)
    m = run("LDA 0x81\nSHL\nHALT")
    assert (m.a[0], m.c[0]) == (0x02, 1)
    m = run("LDA 0x40\nSHL\nHALT")
    assert (m.a[0], m.c[0]) == (0x80, 0)


def test_ror_rol_through_carry_sequence():
    m = run("LDA 0x03\nSHR\nROR\nROR\nROR\nLDA 0xC0\nSHL\nROL\nROL\nROL\nHALT")
    # (A, C) after each instruction
    assert [(e[3], e[5]) for e in m.log if e[1] == 0] == [
        (0x03, 0), (0x01, 1),                          # LDA, SHR
        (0x80, 1), (0xC0, 0), (0x60, 0),               # ROR x3
        (0xC0, 0), (0x80, 1),                          # LDA keeps C, SHL
        (0x01, 1), (0x03, 0), (0x06, 0),               # ROL x3
        (0x06, 0)]                                     # HALT


@pytest.mark.parametrize("a,cin,ea,ec", [
    (0x00, 0, 0x00, 0), (0x00, 1, 0x80, 0), (0x01, 0, 0x00, 1), (0x01, 1, 0x80, 1),
    (0xFF, 0, 0x7F, 1), (0xFF, 1, 0xFF, 1), (0xAA, 0, 0x55, 0), (0x55, 1, 0xAA, 1)])
def test_ror(a, cin, ea, ec):
    m = acc_case(a, 0, cin, "ROR")
    assert (m.a[0], m.c[0]) == (ea, ec)


@pytest.mark.parametrize("a,cin,ea,ec", [
    (0x00, 0, 0x00, 0), (0x00, 1, 0x01, 0), (0x80, 0, 0x00, 1), (0x80, 1, 0x01, 1),
    (0xFF, 0, 0xFE, 1), (0xFF, 1, 0xFF, 1), (0x55, 0, 0xAA, 0), (0xAA, 1, 0x55, 1)])
def test_rol(a, cin, ea, ec):
    m = acc_case(a, 0, cin, "ROL")
    assert (m.a[0], m.c[0]) == (ea, ec)


def test_16_bit_shift_right_with_ror():
    # 0x0301 >> 1 = 0x0180: SHR the high byte (bit 0 goes to C), ROR the low byte (C comes in at bit 7)
    m = run("LDA 0x03\nSHR\nSTM 1\nLDA 0x01\nROR\nSTM 0\nHALT")
    assert (m.mem[0][1], m.mem[0][0], m.c[0]) == (0x01, 0x80, 1)


@pytest.mark.parametrize("cin", [0, 1])
@pytest.mark.parametrize("a,ea", [(0xFF, 0x00), (0x7F, 0x80), (0x00, 0x01), (0x41, 0x42)])
def test_inc(a, ea, cin):
    m = acc_case(a, 0, cin, "INC")
    assert (m.a[0], m.c[0]) == (ea, cin)


@pytest.mark.parametrize("cin", [0, 1])
@pytest.mark.parametrize("a,ea", [(0x00, 0xFF), (0x80, 0x7F), (0x01, 0x00), (0x42, 0x41)])
def test_dec(a, ea, cin):
    m = acc_case(a, 0, cin, "DEC")
    assert (m.a[0], m.c[0]) == (ea, cin)


@pytest.mark.parametrize("cin", [0, 1])
@pytest.mark.parametrize("a,ea", [(0x00, 0xFF), (0xFF, 0x00), (0x0F, 0xF0), (0xA5, 0x5A), (0x80, 0x7F)])
def test_not(a, ea, cin):
    m = acc_case(a, 0, cin, "NOT")
    assert (m.a[0], m.c[0]) == (ea, cin)


@pytest.mark.parametrize("cin", [0, 1])
@pytest.mark.parametrize("a,ea", [(0xA5, 0x5A), (0x12, 0x21), (0x0F, 0xF0), (0x80, 0x08), (0xFF, 0xFF),
                                  (0x00, 0x00)])
def test_swap(a, ea, cin):
    m = acc_case(a, 0, cin, "SWAP")
    assert (m.a[0], m.c[0]) == (ea, cin)


@pytest.mark.parametrize("a,x,ec", [(5, 6, 1), (6, 5, 0), (5, 5, 0), (0, 0xFF, 1), (0xFF, 0, 0),
                                    (0x7F, 0x80, 1), (0x80, 0x7F, 0)])
@pytest.mark.parametrize("cin", [0, 1])
def test_cmpx(a, x, ec, cin):
    m = acc_case(a, x, cin, "CMPX")
    assert (m.a[0], m.x[0], m.c[0]) == (a, x, ec)


@pytest.mark.parametrize("x,imm,ec", [(5, 6, 1), (6, 6, 0), (7, 6, 0), (0, 255, 1), (255, 0, 0),
                                      (0x7F, 0x80, 1), (0x80, 0x7F, 0)])
@pytest.mark.parametrize("cin", [0, 1])
def test_xcmp(x, imm, ec, cin):
    m = acc_case(0x33, x, cin, f"XCMP {imm}")
    assert (m.a[0], m.x[0], m.c[0]) == (0x33, x, ec)


@pytest.mark.parametrize("cin", [0, 1])
@pytest.mark.parametrize("x,imm,ex", [(0xF0, 0x20, 0x10), (0xFF, 0x01, 0x00), (0x7F, 0x01, 0x80),
                                      (0x00, 0x00, 0x00), (0x10, 0xF0, 0x00), (0x01, 0xFF, 0x00)])
def test_xadd_has_no_flag(x, imm, ex, cin):
    m = acc_case(0x33, x, cin, f"XADD {imm}")
    assert (m.a[0], m.x[0], m.c[0]) == (0x33, ex, cin)


def test_tax_txa_xchg():
    m = run("LDA 0x9C\nTAX\nHALT")
    assert (m.a[0], m.x[0]) == (0x9C, 0x9C)
    m = run("LDA 0x44\nLDX 0x33\nTXA\nHALT")
    assert (m.a[0], m.x[0]) == (0x33, 0x33)
    for cin in (0, 1):
        m = acc_case(0x11, 0x22, cin, "XCHG")
        assert (m.a[0], m.x[0], m.c[0]) == (0x22, 0x11, cin)


@pytest.mark.parametrize("a,x,ea,ec", [(0xF0, 0x20, 0x10, 1), (0x01, 0x01, 0x02, 0), (0xFF, 0x01, 0x00, 1),
                                       (0x7F, 0x01, 0x80, 0), (0x80, 0x80, 0x00, 1), (0xFF, 0xFF, 0xFE, 1),
                                       (0x00, 0x00, 0x00, 0)])
@pytest.mark.parametrize("cin", [0, 1])
def test_addx(a, x, ea, ec, cin):
    m = acc_case(a, x, cin, "ADDX")
    assert (m.a[0], m.x[0], m.c[0]) == (ea, x, ec)


@pytest.mark.parametrize("a,x,ea,ec", [(0x10, 0x20, 0xF0, 1), (0x20, 0x10, 0x10, 0), (0x55, 0x55, 0x00, 0),
                                       (0x00, 0x01, 0xFF, 1), (0x80, 0x7F, 0x01, 0), (0x7F, 0x80, 0xFF, 1),
                                       (0xFF, 0x00, 0xFF, 0)])
@pytest.mark.parametrize("cin", [0, 1])
def test_subx(a, x, ea, ec, cin):
    m = acc_case(a, x, cin, "SUBX")
    assert (m.a[0], m.x[0], m.c[0]) == (ea, x, ec)


@pytest.mark.parametrize("cin", [0, 1])
@pytest.mark.parametrize("op,a,x,ea", [("ANDX", 0xF0, 0x3C, 0x30), ("ANDX", 0xFF, 0x00, 0x00),
                                       ("ORX", 0xA0, 0x05, 0xA5), ("ORX", 0x00, 0x00, 0x00),
                                       ("XORX", 0xFF, 0xAA, 0x55), ("XORX", 0x55, 0x55, 0x00)])
def test_logic_with_x_keeps_carry(op, a, x, ea, cin):
    m = acc_case(a, x, cin, op)
    assert (m.a[0], m.x[0], m.c[0]) == (ea, x, cin)


def test_djnz_counts_with_x_as_before():
    # LDX 3: the loop body runs three times; LDX 0 would loop 256 times
    m = run("LDX 3\nLDA 0\nL: INC\nDJNZ L\nHALT")
    assert (m.a[0], m.x[0]) == (3, 0)
    m = run("LDX 0\nLDA 0\nL: INC\nDJNZ L\nHALT")
    assert (m.a[0], m.x[0]) == (0, 0)       # 256 increments wrap A back to 0


# ----------------------------------------------------------------------------------------------
# conditional branches
# ----------------------------------------------------------------------------------------------

# (setup lines, branch taken?) per condition. The setup is the same in both pairs of cases where it can be.
BRANCH_CASES = {
    "JC": [("LDA 0\nSUB 1", True), ("LDA 0\nSUB 0", False), ("LDA 255\nADD 1", True), ("LDA 1\nADD 1", False)],
    "JNC": [("LDA 0\nSUB 1", False), ("LDA 0\nSUB 0", True), ("LDA 255\nADD 1", False), ("LDA 1\nADD 1", True)],
    "JZ": [("LDA 0", True), ("LDA 1", False), ("LDA 0x80", False), ("LDA 0xFF\nADD 1", True)],
    "JNZ": [("LDA 0", False), ("LDA 1", True), ("LDA 0x80", True), ("LDA 0xFF\nADD 1", False)],
    "JMI": [("LDA 0x80", True), ("LDA 0xFF", True), ("LDA 0x7F", False), ("LDA 0", False)],
    "JPL": [("LDA 0x7F", True), ("LDA 0", True), ("LDA 0x80", False), ("LDA 0xFF", False)],
    "JEQ": [("LDX 5\nLDA 5", True), ("LDX 5\nLDA 6", False), ("LDX 0\nLDA 0", True),
            ("LDX 0xFF\nLDA 0xFF", True), ("LDX 0\nLDA 0x80", False), ("LDX 0x80\nLDA 0", False)],
    "JNE": [("LDX 5\nLDA 5", False), ("LDX 5\nLDA 6", True), ("LDX 0\nLDA 0", False),
            ("LDX 0xFF\nLDA 0xFF", False), ("LDX 0\nLDA 0x80", True), ("LDX 0x80\nLDA 0", True)],
}


@pytest.mark.parametrize("cond,setup,taken", [(c, s, t) for c, cases in BRANCH_CASES.items()
                                              for s, t in cases])
def test_conditional_branch(cond, setup, taken):
    src = f"{setup}\n{cond} yes\nOUT 0x11\nHALT\nyes: OUT 0x22\nHALT"
    m = run(src)
    assert m.pins_out == (0x22 if taken else 0x11)
    n = len(setup.split("\n"))                      # the branch is instruction number n
    thread0 = [e for e in m.log if e[1] == 0]
    assert thread0[n][3:] == thread0[n - 1][3:]     # a branch changes no register
    assert thread0[n][2] == n                       # ... and executed at pc = n
    assert thread0[n + 1][2] == (n + 3 if taken else n + 1)   # layout: n setup, branch, OUT, HALT, yes:


def test_branch_target_is_the_address_field():
    # the target is the full 8-bit address field: 200 is in the HALT-filled tail, so the program stops there
    m = run("LDA 255\nADD 1\nJC 200\nOUT 0x11\nHALT")
    assert m.pins_out == 0x01
    assert [e for e in m.log if e[1] == 0][-1][2] == 200


@pytest.mark.parametrize("word", [0x4900, 0x4A05, 0x4F00, 0x4FFF])
def test_undefined_branch_condition_never_branches(word):
    m = run("LDA 0x55\nLDX 0x66\nNOP\nOUT 0x77\nHALT", raw={2: word})
    assert (m.a[0], m.x[0], m.pins_out) == (0x55, 0x66, 0x77)


@pytest.mark.parametrize("word", [0x6900, 0x69FF, 0x6F12, 0x9300, 0x9377, 0x9F01, 0xB400, 0xB5FF, 0xBF00,
                                  0xFE00, 0xFE12, 0xFF00, 0xFFFF])
def test_undefined_subopcodes_are_nop(word):
    m = run("LDA 0x55\nLDX 0x66\nLDA 0\nSUB 1\nNOP\nOUT 0x77\nHALT", raw={4: word})
    assert (m.a[0], m.x[0], m.c[0], m.pins_out) == (0xFF, 0x66, 1, 0x77)
    assert [e for e in m.log if e[1] == 0][4][3:] == (0xFF, 0x66, 1)     # the word itself changed nothing


# ----------------------------------------------------------------------------------------------
# data memory
# ----------------------------------------------------------------------------------------------

def test_memory_direct_all_sixteen_bytes():
    src = "\n".join(f"LDA {i * 17}\nSTM {i}" for i in range(16)) + "\nLDM 5\nHALT"
    m = run(src)
    assert m.mem[0] == [0x00, 0x11, 0x22, 0x33, 0x44, 0x55, 0x66, 0x77,
                        0x88, 0x99, 0xAA, 0xBB, 0xCC, 0xDD, 0xEE, 0xFF]
    assert m.a[0] == 0x55
    assert m.mem[1] == m.mem[2] == m.mem[3] == [0] * 16
    m = run("LDA 0x12\nSTM 15\nLDA 0\nLDM 15\nHALT")
    assert (m.a[0], m.mem[0][15], m.mem[0][0]) == (0x12, 0x12, 0)


@pytest.mark.parametrize("cin", [0, 1])
def test_memory_instructions_keep_flags_and_x(cin):
    m = run(f"{carry_prefix(cin)}LDX 9\nLDA 0xE7\nSTM 4\nSTMX 4\nLDM 4\nLDMX 4\nHALT")
    assert (m.a[0], m.x[0], m.c[0]) == (0xE7, 9, cin)


def test_memory_indexed_with_wraparound():
    m = run("LDX 14\nLDA 0xAB\nSTMX 5\n"           # (14 + 5) & 15 = 3
            "LDX 15\nLDA 0xCD\nSTMX 15\n"          # (15 + 15) & 15 = 14
            "LDX 0xF1\nLDA 0xEF\nSTMX 3\n"         # (0xF1 + 3) & 15 = 4: only the low bits of X count
            "LDX 2\nLDA 0\nLDMX 1\nTAX\nHALT")     # (2 + 1) = 3 -> 0xAB
    expected = [0] * 16
    expected[3], expected[14], expected[4] = 0xAB, 0xCD, 0xEF
    assert m.mem[0] == expected
    assert m.a[0] == 0xAB


def test_memory_indexed_reads_wrap():
    prog = ("LDX 14\nLDA 0xAB\nSTMX 5\n"            # mem[3] = 0xAB
            "LDX 15\nLDA 0xCD\nSTMX 15\n"           # mem[14] = 0xCD
            "LDX 0xF1\nLDA 0xEF\nSTMX 3\n"          # mem[4] = 0xEF
            "LDX 14\nLDMX 5\nTAX\nLDA 0\n"          # A = mem[(14+5)&15 = 3] = 0xAB, X = 0xAB
            "LDX 13\nLDMX 1\nTAX\nLDA 0\n"          # A = mem[(13+1)&15 = 14] = 0xCD
            "LDX 0xF0\nLDMX 4\nHALT")               # A = mem[(0xF0+4)&15 = 4] = 0xEF
    m = run(prog)
    xs = [e[4] for e in m.log if e[1] == 0 and e[2] in (11, 15)]   # X after the two TAX
    assert xs == [0xAB, 0xCD]
    assert m.a[0] == 0xEF


def test_memory_is_private_to_each_thread():
    m = run("SPAWN 1, 8\nLDA 0x11\nSTM 3\nWAIT 40\nLDM 3\nLDM 4\nHALT\nNOP\n"
            "LDM 3\nTAX\nLDA 0x22\nSTM 3\nSTM 4\nHALT")
    assert m.a[0] == 0x00                           # thread 0's last LDM 4 saw its own (empty) byte 4
    assert [e for e in m.log if e[1] == 0 and e[2] == 4][0][3] == 0x11     # LDM 3 still 0x11
    assert [e for e in m.log if e[1] == 1 and e[2] == 8][0][3] == 0x00     # thread 1 saw an empty byte 3
    expected0, expected1 = [0] * 16, [0] * 16
    expected0[3] = 0x11
    expected1[3] = expected1[4] = 0x22
    assert m.mem == [expected0, expected1, [0] * 16, [0] * 16]


def test_memory_survives_halt_and_spawn_and_is_cleared_by_reset():
    m = run("SPAWN 1, 6\nWAIT 20\nSPAWN 1, 9\nWAIT 20\nHALT\nNOP\n"
            "LDA 0x77\nSTM 0\nHALT\n"
            "LDM 0\nSTM 1\nHALT")
    # thread 1 wrote 0x77, halted, was started again (A = X = C = 0) and still read 0x77
    assert m.mem[1][:2] == [0x77, 0x77]
    assert m.a[1] == 0x77
    m.reset()
    assert m.mem == [[0] * 16 for _ in range(4)]
    assert m.state() == Model([]).state()


def test_self_spawn_keeps_memory():
    # thread 0 stores 0x5A, restarts itself at pc 4 (A = 0 after the restart) and reads it back
    m = run("LDA 0x5A\nSTM 7\nSPAWN 0, 4\nHALT\nLDM 7\nHALT")
    assert (m.a[0], m.mem[0][7]) == (0x5A, 0x5A)


# ----------------------------------------------------------------------------------------------
# JMPA / CALLA / RETL
# ----------------------------------------------------------------------------------------------

@pytest.mark.parametrize("index,value", [(0, 0x10), (1, 0x21), (2, 0x42), (3, 0x84)])
def test_calla_retl_lookup_table(index, value):
    m = run(f"LDA {index}\nADD 6\nCALLA\nOUTA\nHALT\nNOP\n"
            "RETL 0x10\nRETL 0x21\nRETL 0x42\nRETL 0x84")
    assert m.pins_out == value
    assert m.sp[0] == 0                               # CALLA pushed, RETL popped
    thread0 = [e[2] for e in m.log if e[1] == 0]
    assert thread0 == [0, 1, 2, 6 + index, 3, 4]      # LDA ADD CALLA RETL(index) OUTA HALT


def test_retl_leaves_x_and_carry_and_replaces_a():
    m = run("LDA 0\nSUB 1\nLDX 0x66\nLDA 8\nCALLA\nHALT\nNOP\nNOP\nRETL 0x42")
    assert (m.a[0], m.x[0], m.c[0]) == (0x42, 0x66, 1)


def test_jmpa_computed_jump():
    m = run("LDA 4\nJMPA\nOUT 0x11\nHALT\nOUT 0x22\nHALT")
    assert m.pins_out == 0x22
    assert m.sp[0] == 0
    m = run("LDA 200\nJMPA")                          # 200 is in the HALT-filled tail
    assert [e[2] for e in m.log if e[1] == 0] == [0, 1, 200]
    # a jump table: ADD base; JMPA into a run of JMPs (entries at pcs 4, 5, 6 jump to 10, 11, 12)
    m = run("LDA 2\nADD 4\nJMPA\nNOP\nJMP 10\nJMP 11\nJMP 12\nNOP\nNOP\nNOP\nOUT 0xA0\nHALT\nOUT 0xB0\nHALT")
    assert m.pins_out == 0xB0                         # index 2 -> A = 6 -> `JMP 12` -> `OUT 0xB0`


def test_calla_and_call_share_the_return_stack():
    # CALL f; f: CALLA to g; g: RETL 7 (returns into f); f: RET (returns to main)
    m = run("CALL 3\nOUTA\nHALT\nLDA 6\nCALLA\nRET\nRETL 7")
    assert m.pins_out == 0x07 and m.sp[0] == 0
    assert [e[2] for e in m.log if e[1] == 0] == [0, 3, 4, 6, 5, 1, 2]


# ----------------------------------------------------------------------------------------------
# wide pin access
# ----------------------------------------------------------------------------------------------

PINS = 0xA55A      # uio_in = 0xA5 (input pins 8-15), ui_in = 0x5A (input pins 0-7)


def test_in_inh_outa_odrva():
    # The first NOP gives the 2-flop synchroniser time: the first instruction runs at edge 1.
    m = run("NOP\nIN\nTAX\nINH\nOUTA\nHALT", pins=PINS)
    assert (m.x[0], m.a[0], m.pins_out) == (0x5A, 0xA5, 0xA5)
    m = run("NOP\nLDA 0xF0\nODRVA\nHALT", pins=PINS)
    assert m.od_pull == 0x0F                           # 0 bits pull the pad low, 1 bits release it
    m = run("NOP\nLDA 0xFF\nODRVA\nHALT")
    assert m.od_pull == 0x00
    m = run("NOP\nLDA 0x00\nODRVA\nHALT")
    assert m.od_pull == 0xFF
    m = run("NOP\nLDA 0xA6\nODRVA\nHALT")              # 1010 0110: pads 0, 3, 4, 6 are pulled low
    assert m.od_pull == 0x59


def test_outa_replaces_all_pins_and_odrva_replaces_all_pulls():
    m = run("OUT 0xFF\nLDA 0x3C\nOUTA\nODRV 0 0\nODRV 7 0\nLDA 0xFF\nODRVA\nHALT")
    assert m.pins_out == 0x3C
    assert m.od_pull == 0x00


def test_pin_reset_value_and_outc_odrvc_unchanged():
    m = run("HALT")
    assert (m.pins_out, m.od_pull) == (0x01, 0x00)
    m = run("LDA 0\nSUB 1\nOUTC 6\nODRVC 2\nHALT")      # C = 1: pin 6 high, pad 2 released (not pulled)
    assert (m.pins_out, m.od_pull) == (0x41, 0x00)
    m = run("LDA 0\nSUB 0\nOUTC 0\nODRVC 2\nHALT")      # C = 0: pin 0 low, pad 2 pulled low
    assert (m.pins_out, m.od_pull) == (0x00, 0x04)


def test_channel_and_spawn_still_work():
    m = run("SPAWN 1, 4\nLDA 0x42\nSEND\nHALT\nRECV\nOUTA\nHALT")
    assert m.pins_out == 0x42 and m.mb_full == 0


# ----------------------------------------------------------------------------------------------
# schedule
# ----------------------------------------------------------------------------------------------

def test_valid_schedule_rules():
    ok = [(0, 1, 2, 3), (0, 1, 0, 2), (0, 1, 0, 1), (3, 2, 3, 0), (1, 0, 1, 0), (2, 3, 2, 3)]
    bad = [(0, 0, 1, 2), (1, 2, 2, 3), (1, 2, 3, 3), (0, 1, 2, 0), (1, 1, 1, 1), (0, 1, 1, 0),
           (0, 1, 0, 0), (3, 3, 3, 3), (0, 1, 2), (0, 1, 2, 3, 0), (0, 1, 2, 4), (0, 1, 2, -1)]
    assert all(valid_schedule(s) for s in ok)
    assert not any(valid_schedule(s) for s in bad)
    # (k-1)^4 + (k-1) proper colourings of a 4-cycle with k = 4 colours
    assert sum(valid_schedule((a, b, c, d)) for a in range(4) for b in range(4)
               for c in range(4) for d in range(4)) == 84
    for s in ok:
        assert effective_schedule(s) == s
    for s in bad[:8]:
        assert effective_schedule(s) == (0, 1, 2, 3)
    assert DEFAULT_SCHEDULE == (0, 1, 2, 3)


def test_model_refuses_an_illegal_schedule():
    with pytest.raises(AssertionError):
        Model([0xF000], (0, 0, 1, 2))


SPAWN_PROG = "SPAWN 1, 4\nNOP\nNOP\nHALT\nNOP\nNOP\nHALT"     # thread 0: pcs 0-3, thread 1: pcs 4-6


def test_default_schedule_timing():
    m = run(SPAWN_PROG)
    assert [(e[0], e[1], e[2]) for e in m.log] == [
        (1, 0, 0), (5, 0, 1), (6, 1, 4), (9, 0, 2), (10, 1, 5), (13, 0, 3), (14, 1, 6)]


def test_schedule_0101_runs_both_threads_every_two_clocks():
    m = run(SPAWN_PROG, sched=(0, 1, 0, 1))
    # thread 1 is spawned by the instruction at edge 1; its first fetch with the flag set is edge 3
    assert [(e[0], e[1], e[2]) for e in m.log] == [
        (1, 0, 0), (3, 0, 1), (4, 1, 4), (5, 0, 2), (6, 1, 5), (7, 0, 3), (8, 1, 6)]


def test_schedule_0102_and_a_thread_that_is_not_scheduled():
    m = run("SPAWN 1, 6\nSPAWN 2, 8\nSPAWN 3, 10\nHALT\nNOP\nNOP\nNOP\nHALT\nNOP\nHALT\nNOP\nHALT",
            sched=(0, 1, 0, 2), edges=40)
    assert [(e[0], e[1], e[2]) for e in m.log] == [
        (1, 0, 0), (3, 0, 1), (5, 0, 2), (6, 1, 6), (7, 0, 3), (8, 2, 8), (10, 1, 7), (12, 2, 9)]
    # thread 3 was started (its flag is set) but never gets a clock
    assert m.act == [0, 0, 0, 1] and m.pc[3] == 10 and m.a[3] == m.x[3] == m.c[3] == 0


def test_schedule_3203_thread_zero_waits_for_its_slot():
    # slots 0, 1, 2, 3 -> threads 3, 2, 3, 0: thread 0 owns slot 3 only (first execute at edge 4),
    # thread 3 owns slots 0 and 2 and runs every second clock once it is started
    m = run("SPAWN 3, 3\nHALT\nNOP\nINC\nJMP 3", sched=(3, 2, 3, 0), edges=14)
    assert [(e[0], e[1], e[2]) for e in m.log] == [
        (4, 0, 0), (7, 3, 3), (8, 0, 1), (9, 3, 4), (11, 3, 3), (13, 3, 4)]


@pytest.mark.parametrize("sched,expected_a", [((0, 1, 2, 3), 25), ((3, 2, 3, 0), 25), ((0, 1, 0, 1), 50),
                                              ((0, 1, 0, 2), 50), ((0, 2, 0, 3), 50), ((1, 0, 1, 0), 50),
                                              ((2, 0, 3, 0), 50)])
def test_thread_zero_speed(sched, expected_a):
    # `L: INC; JMP L` for edges 0..199, one INC per two turns.
    # One slot (default, or (3,2,3,0) where thread 0 owns slot 3): executes at edges 1, 5, ..., 197 (or
    # 4, 8, ..., 196) = 50 (49) turns, 25 INCs. Two slots two clocks apart: executes at edges 1, 3, ..., 199
    # (or 2, 4, ..., 198) = 100 (99) turns, 50 INCs.
    m = run("L: INC\nJMP L", sched=sched, edges=200)
    assert m.a[0] == expected_a


@pytest.mark.parametrize("sched,period", [((0, 1, 2, 3), 12), ((0, 1, 0, 1), 6), ((0, 1, 0, 2), 6)])
def test_pin_toggle_period(sched, period):
    # 'PIN 1 1; PIN 1 0; JMP L' is three instructions: 3 turns = 12 clocks, or 6 clocks on a fast schedule
    m = run("L: PIN 1 1\nPIN 1 0\nJMP L", sched=sched, edges=120)
    rises = [e for e, v in m.pin_log if v & 2]
    assert rises[:3] == [1, 1 + period, 1 + 2 * period]
    falls = [e for e, v in m.pin_log if not v & 2]
    assert falls[:2] == [1 + period // 3, 1 + period + period // 3]


def test_taken_branches_cost_nothing_extra():
    # LDX 3; (NOP; DJNZ) x3; OUT 0x5A: OUT is the 8th instruction (index 7), executed on turn 8
    prog = "LDX 3\nL: NOP\nDJNZ L\nOUT 0x5A\nHALT"
    assert run(prog).pin_log == [(1 + 4 * 7, 0x5A)]
    assert run(prog, sched=(0, 1, 0, 1)).pin_log == [(1 + 2 * 7, 0x5A)]
    assert run(prog, sched=(0, 1, 0, 2)).pin_log == [(1 + 2 * 7, 0x5A)]


def test_wait_takes_one_plus_n_turns():
    # WAIT 2 is turn 1, then two waiting turns, so the OUT is on turn 4
    prog = "WAIT 2\nOUT 7\nHALT"
    assert run(prog).pin_log == [(1 + 4 * 3, 7)]
    assert run(prog, sched=(0, 1, 0, 1)).pin_log == [(1 + 2 * 3, 7)]


def test_call_ret_calla_retl_cost_one_turn_on_a_fast_schedule():
    prog = "CALL 4\nOUT 3\nHALT\nNOP\nRET"
    m = run(prog, sched=(0, 1, 0, 1))
    assert [(e[0], e[2]) for e in m.log] == [(1, 0), (3, 4), (5, 1), (7, 2)]
    m = run("LDA 5\nCALLA\nOUT 3\nHALT\nNOP\nRETL 9", sched=(0, 1, 0, 2))
    assert [(e[0], e[2]) for e in m.log] == [(1, 0), (3, 1), (5, 5), (7, 2), (9, 3)]
    assert m.a[0] == 9


def test_invalid_schedule_runs_the_default_one():
    for bad in [(0, 0, 1, 2), (0, 1, 2, 0), (1, 2, 3, 3), (3, 3, 3, 3)]:
        sched = effective_schedule(bad)
        assert sched == (0, 1, 2, 3)
        m = run(SPAWN_PROG, sched=sched)
        assert [(e[0], e[1], e[2]) for e in m.log] == [
            (1, 0, 0), (5, 0, 1), (6, 1, 4), (9, 0, 2), (10, 1, 5), (13, 0, 3), (14, 1, 6)]


def test_schedule_without_thread_zero_runs_nothing():
    m = run("OUT 0x55\nHALT", sched=(1, 2, 1, 3), edges=100)
    assert m.log == [] and m.pins_out == 0x01 and m.act == [1, 0, 0, 0]


def test_two_threads_count_independently_on_a_fast_schedule():
    # thread 0: INC, JMP; thread 1 (spawned at edge 1, executes at edges 4, 6, ...): XADD 2, JMP
    m = run("SPAWN 1, 4\nL: INC\nJMP L\nNOP\nM: XADD 2\nJMP M", sched=(0, 1, 0, 1), edges=60)
    # thread 0: turns at edges 1, 3, ..., 59 = 30 turns (SPAWN, then INC / JMP x14, INC) = 15 INCs
    # thread 1: turns at edges 4, 6, ..., 58 = 28 turns = 14 XADDs = 28
    assert (m.a[0], m.x[0]) == (15, 0)
    assert (m.a[1], m.x[1]) == (0, 28)


# ----------------------------------------------------------------------------------------------
# model plumbing: state vector, schedule generator, program generator
# ----------------------------------------------------------------------------------------------

def test_state_vector_layout():
    assert len(STATE_NAMES) == 4 * 7 + 4 + 64
    assert STATE_NAMES[28:32] == ("pins_out", "od_pull", "mb", "mb_full")
    assert STATE_NAMES[32] == "mem0_0" and STATE_NAMES[32 + 16 + 5] == "mem1_5" and STATE_NAMES[-1] == "mem3_15"
    m = run("LDA 0x31\nSTM 5\nSPAWN 2, 4\nHALT\nLDA 0x42\nSTM 9\nHALT")
    st = dict(zip(STATE_NAMES, m.state()))
    assert st["mem0_5"] == 0x31 and st["mem2_9"] == 0x42 and st["mem1_5"] == 0 and st["mem3_9"] == 0
    assert st["a2"] == 0x42 and st["act2"] == 0 and st["pins_out"] == 0x01


def test_gen_schedule_distribution():
    scheds = [gen_schedule(s) for s in range(1, 401)]
    assert all(len(s) == 4 and all(isinstance(t, int) and 0 <= t <= 3 for t in s) for s in scheds)
    invalid = [s for s in scheds if not valid_schedule(s)]
    assert 25 <= len(invalid) <= 80                   # about one in eight
    for want in [(0, 1, 2, 3), (0, 1, 0, 2), (0, 1, 0, 1), (3, 2, 3, 0)]:
        assert want in scheds
    assert gen_schedule(7) == gen_schedule(7)
    assert any(effective_schedule(s) != s for s in scheds)


def test_gen_program_fits_and_is_deterministic():
    for seed in range(1, 120):
        words = gen_program(seed)
        assert 43 <= len(words) <= 256
        assert all(0 <= w <= 0xFFFF for w in words)
    assert gen_program(5) == gen_program(5) and gen_program(5) != gen_program(6)


def test_generators_exercise_every_required_coverage_counter():
    """The same stimulus as test/random_test.py, on the model alone: every counter that
    test/random_run.py demands must be hit within its default 40 seeds."""
    import random
    from random_run import REQUIRED
    total = {}
    for seed in range(1, 41):
        rng = random.Random(seed ^ 0x5EED)
        model = Model(gen_program(seed), effective_schedule(gen_schedule(seed)))
        hold = rng.choice([1, 1, 2, 5, 20])
        ui = uio = 0
        for cyc in range(6000):
            if cyc % hold == 0:
                ui, uio = rng.getrandbits(8), rng.getrandbits(8) & 0xDF
            model.clock((uio << 8) | ui)
        for k, v in model.cov.items():
            total[k] = total.get(k, 0) + v
    assert [k for k in REQUIRED if total.get(k, 0) == 0] == []


def test_loader_packs_the_schedule_byte():
    pytest.importorskip("cocotb")
    from loader import SCHED_BYTE, sched_byte
    assert sched_byte((0, 1, 2, 3)) == 0xE4
    assert sched_byte((0, 1, 0, 1)) == 0x44
    assert sched_byte((0, 1, 0, 2)) == 0x84
    assert sched_byte((3, 2, 3, 0)) == 0x3B
    assert sched_byte((0, 0, 1, 2)) == 0x90          # illegal, but still packed as given
    assert SCHED_BYTE == 512
