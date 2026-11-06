"""Cycle-accurate reference model of the protocol-emulator CPU, written from docs/isa.md.

Used for differential testing: random programs run on this model and on the RTL, and every
architectural register is compared after every clock edge.
"""
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import asm  # noqa: E402  (the real assembler, so the encodings are cross-checked too)

HALT_WORD = 0xF000
OUT_RESET = 0x01


class Model:
    """State and one-clock step. Matches src/cpu.v plus the 2-flop input synchroniser."""

    def __init__(self, rom):
        self.rom = rom + [HALT_WORD] * (256 - len(rom))
        self.cov = {}
        self.reset()

    def reset(self):
        self.pc = self.a = self.x = self.c = self.wait = self.sp = 0
        self.halted = 0
        self.stack = [0] * 4
        self.pins_out = OUT_RESET
        self.od_pull = 0
        self.sync1 = self.sync2 = 0

    def hit(self, name):
        self.cov[name] = self.cov.get(name, 0) + 1

    def clock(self, pins_driven):
        """One rising edge with rst_n high. `pins_driven` = {uio_in, ui_in} as 16 bits."""
        pins = self.sync2                      # what the CPU sees this cycle
        self.sync2, self.sync1 = self.sync1, pins_driven & 0xFFFF
        if self.wait:
            self.wait -= 1
            return
        if self.halted:
            return
        w = self.rom[self.pc]
        op, ext = w >> 12, (w >> 8) & 0xF
        pc_next = (self.pc + 1) & 0xFF
        bit = lambda n: (pins >> n) & 1

        if op in (0x0, 0x5):                   # JPIN p v addr
            pin, tgt = (w >> 7) & 0xF, (((w >> 11) & 1) << 7) | (w & 0x7F)
            want = 1 if op == 0x5 else 0
            name = f"JPIN{want}"
            if bit(pin) == want:
                pc_next = tgt
                self.hit(name + "_taken")
            else:
                self.hit(name + "_fall")
        elif op == 0x1:
            p = (w >> 8) & 7
            self.pins_out = (self.pins_out & ~(1 << p)) | ((w & 1) << p)
            self.hit("PIN")
        elif op == 0x2:
            self.pins_out = w & 0xFF
            self.hit("OUT")
        elif op == 0x3:
            self.wait = w & 0xFFF
            self.hit("WAIT")
        elif op == 0x4:
            pc_next = w & 0xFF
            self.hit("JMP")
        elif op == 0x6:
            self.a = w & 0xFF
            self.hit("LDA")
        elif op == 0x7:
            self.c, self.a = self.a & 1, self.a >> 1
            self.hit("SHR")
        elif op == 0x8:
            p = (w >> 8) & 7
            self.pins_out = (self.pins_out & ~(1 << p)) | (self.c << p)
            self.hit("OUTC")
        elif op == 0x9:
            self.x = w & 0xFF
            self.hit("LDX")
        elif op == 0xA:
            old = self.x
            self.x = (self.x - 1) & 0xFF
            if old != 1:
                pc_next = w & 0xFF
                self.hit("DJNZ_taken")
            else:
                self.hit("DJNZ_exit")
        elif op == 0xB:
            self.a = pins & 0xFF
            self.hit("IN")
        elif op == 0xC:
            self.c, self.a = (self.a >> 7) & 1, (self.a << 1) & 0xFF
            self.hit("SHL")
        elif op == 0xD:
            self.a = (self.a & 0xFE) | bit((w >> 7) & 0xF)
            self.hit("INB")
        elif op == 0xE:
            self.stack[self.sp] = pc_next
            self.sp = (self.sp + 1) & 3
            pc_next = w & 0xFF
            self.hit("CALL")
        else:                                  # op == 0xF, extended family
            if ext == 0x0:
                self.halted = 1
                pc_next = self.pc
                self.hit("HALT")
            elif ext == 0x1:
                self.sp = (self.sp - 1) & 3
                pc_next = self.stack[self.sp]
                self.hit("RET")
            elif ext == 0x2:
                p, v = w & 7, (w >> 3) & 1
                self.od_pull = (self.od_pull & ~(1 << p)) | ((1 - v) << p)
                self.hit("ODRV")
            elif ext == 0x3:
                p = w & 7
                self.od_pull = (self.od_pull & ~(1 << p)) | ((1 - self.c) << p)
                self.hit("ODRVC")
            elif ext == 0x4:
                self.hit("NOP")
            else:
                self.hit("EXT_undefined")
        self.pc = pc_next

    def state(self):
        return (self.pc, self.a, self.x, self.c, self.wait, self.halted, self.sp,
                self.pins_out, self.od_pull)


STATE_NAMES = ("pc", "a", "x", "c", "wait_cnt", "halted", "sp", "pins_out", "od_pull")


def gen_program(seed):
    """Random assembly program. Every mnemonic is exercised; branch targets are random."""
    r = random.Random(seed)
    n = r.randint(40, 256)

    def addr():
        return r.randrange(n + 8)              # sometimes lands in the HALT-filled tail

    makers = [
        (8, lambda: f"PIN {r.randrange(8)} {r.randrange(2)}"),
        (3, lambda: f"OUT {r.randrange(256)}"),
        (8, lambda: f"WAIT {r.choice([0, 0, 1, 2, 3, 5, r.randrange(40)])}"),
        (5, lambda: f"JMP {addr()}"),
        (14, lambda: f"JPIN {r.randrange(16)} {r.randrange(2)} {addr()}"),
        (5, lambda: f"LDA {r.randrange(256)}"),
        (5, lambda: "SHR"),
        (6, lambda: f"OUTC {r.randrange(8)}"),
        (5, lambda: f"LDX {r.choice([0, 1, 2, 3, 8, r.randrange(256)])}"),
        (6, lambda: f"DJNZ {addr()}"),
        (4, lambda: "IN"),
        (5, lambda: "SHL"),
        (6, lambda: f"INB {r.randrange(16)}"),
        (6, lambda: f"CALL {addr()}"),
        (5, lambda: "RET"),
        (6, lambda: f"ODRV {r.randrange(8)} {r.randrange(2)}"),
        (5, lambda: f"ODRVC {r.randrange(8)}"),
        (2, lambda: "NOP"),
        (1, lambda: "HALT"),
    ]
    weights = [w for w, _ in makers]
    lines = [r.choices(makers, weights)[0][1]() for _ in range(n)]
    words, _ = asm.assemble("\n".join(lines))
    return words


def write_hex(words, path):
    rom = words + [HALT_WORD] * (256 - len(words))
    Path(path).write_text("\n".join(f"{w:04X}" for w in rom) + "\n")
