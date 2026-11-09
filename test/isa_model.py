"""Reference model of the four-thread protocol-emulator processor, written from docs/isa.md.

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
    """Architectural model of the four-thread processor (docs/isa.md), plus the 2-flop input
    synchroniser. One call to clock() is one rising edge.

    Schedule: clock edge c >= 1 executes one instruction of thread (c-1) % 4, provided that thread
    was running before edge c-1 (the instruction was fetched at edge c-1). Nothing about the RTL's
    pipeline registers appears here.
    """

    def __init__(self, rom):
        self.rom = rom + [HALT_WORD] * (256 - len(rom))
        self.cov = {}
        self.reset()

    def reset(self):
        self.pc = [0] * 4
        self.a = [0] * 4
        self.x = [0] * 4
        self.c = [0] * 4
        self.wait = [0] * 4
        self.sp = [0] * 4
        self.stack = [[0] * 4 for _ in range(4)]
        self.act = [1, 0, 0, 0]
        self.mb = 0
        self.mb_full = 0
        self.pins_out = OUT_RESET
        self.od_pull = 0
        self.sync1 = self.sync2 = 0
        self.cycle = 0
        self.fetched_running = 0       # was the thread fetched at the previous edge running?

    def hit(self, name):
        self.cov[name] = self.cov.get(name, 0) + 1

    def clock(self, pins_driven):
        """One rising edge with rst_n high. `pins_driven` = {uio_in, ui_in} as 16 bits."""
        pins = self.sync2                      # what the threads see this cycle
        self.sync2, self.sync1 = self.sync1, pins_driven & 0xFFFF
        c = self.cycle
        runs_now = self.fetched_running
        self.fetched_running = self.act[c % 4]  # state before this edge's updates
        self.cycle += 1
        if c == 0 or not runs_now:
            return
        self.execute((c - 1) % 4, pins)

    def execute(self, t, pins):
        if self.wait[t]:
            self.wait[t] -= 1
            return
        w = self.rom[self.pc[t]]
        op, ext = w >> 12, (w >> 8) & 0xF
        pc = self.pc[t]
        pc_next = (pc + 1) & 0xFF
        bit = lambda n: (pins >> n) & 1
        spawn = None

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
            self.wait[t] = w & 0xFFF
            self.hit("WAIT")
        elif op == 0x4:
            pc_next = w & 0xFF
            self.hit("JMP")
        elif op == 0x6:
            self.a[t] = w & 0xFF
            self.hit("LDA")
        elif op == 0x7:
            self.c[t], self.a[t] = self.a[t] & 1, self.a[t] >> 1
            self.hit("SHR")
        elif op == 0x8:
            p = (w >> 8) & 7
            self.pins_out = (self.pins_out & ~(1 << p)) | (self.c[t] << p)
            self.hit("OUTC")
        elif op == 0x9:
            self.x[t] = w & 0xFF
            self.hit("LDX")
        elif op == 0xA:
            old = self.x[t]
            self.x[t] = (old - 1) & 0xFF
            if old != 1:
                pc_next = w & 0xFF
                self.hit("DJNZ_taken")
            else:
                self.hit("DJNZ_exit")
        elif op == 0xB:
            self.a[t] = pins & 0xFF
            self.hit("IN")
        elif op == 0xC:
            self.c[t], self.a[t] = (self.a[t] >> 7) & 1, (self.a[t] << 1) & 0xFF
            self.hit("SHL")
        elif op == 0xD:
            self.a[t] = (self.a[t] & 0xFE) | bit((w >> 7) & 0xF)
            self.hit("INB")
        elif op == 0xE:
            self.stack[t][self.sp[t]] = pc_next
            self.sp[t] = (self.sp[t] + 1) & 3
            pc_next = w & 0xFF
            self.hit("CALL")
        else:                                  # op == 0xF, extended family
            if ext == 0x0:
                self.act[t] = 0
                pc_next = pc
                self.hit("HALT")
            elif ext == 0x1:
                self.sp[t] = (self.sp[t] - 1) & 3
                pc_next = self.stack[t][self.sp[t]]
                self.hit("RET")
            elif ext == 0x2:
                p, v = w & 7, (w >> 3) & 1
                self.od_pull = (self.od_pull & ~(1 << p)) | ((1 - v) << p)
                self.hit("ODRV")
            elif ext == 0x3:
                p = w & 7
                self.od_pull = (self.od_pull & ~(1 << p)) | ((1 - self.c[t]) << p)
                self.hit("ODRVC")
            elif ext == 0x4:
                self.hit("NOP")
            elif ext == 0x5:                   # SEND: A -> channel, waits while it is full
                if self.mb_full:
                    pc_next = pc
                    self.hit("SEND_block")
                else:
                    self.mb, self.mb_full = self.a[t], 1
                    self.hit("SEND_ok")
            elif ext == 0x6:                   # RECV: channel -> A, waits while it is empty
                if not self.mb_full:
                    pc_next = pc
                    self.hit("RECV_block")
                else:
                    self.a[t], self.mb_full = self.mb, 0
                    self.hit("RECV_ok")
            elif 0x8 <= ext <= 0xB:            # SPAWN thread, addr
                target = ext - 0x8
                if target == t or not self.act[target]:
                    spawn = (target, w & 0xFF)
                    self.hit("SPAWN_self" if target == t else "SPAWN_start")
                else:
                    self.hit("SPAWN_ignored")
            else:
                self.hit("EXT_undefined")
        self.pc[t] = pc_next
        if spawn:
            s, addr = spawn
            self.pc[s], self.a[s], self.x[s], self.c[s] = addr, 0, 0, 0
            self.wait[s], self.sp[s], self.act[s] = 0, 0, 1

    def state(self):
        """Everything the RTL exposes, in the order of STATE_NAMES."""
        out = []
        for t in range(4):
            out += [self.pc[t], self.a[t], self.x[t], self.c[t], self.wait[t], self.act[t],
                    self.sp[t]]
        return tuple(out) + (self.pins_out, self.od_pull, self.mb, self.mb_full)


STATE_NAMES = tuple(f"{n}{t}" for t in range(4)
                    for n in ("pc", "a", "x", "c", "wait", "act", "sp")) + (
    "pins_out", "od_pull", "mb", "mb_full")


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
        (2, lambda: "HALT"),
        (1, lambda: f"SPAWN {r.randrange(4)} {addr()}"),
        (1, lambda: "SEND"),
        (1, lambda: "RECV"),
    ]
    weights = [w for w, _ in makers]
    if seed % 3 == 0:     # channel-heavy programs: lots of SEND / RECV, few HALTs
        weights = [6 if m is makers[-1] or m is makers[-2] else (0 if m is makers[-4] else w)
                   for (w, _), m in zip(makers, makers)]
    # Start the other three threads at random places first, then random code (shared by all).
    lines = [f"SPAWN {t} {addr()}" for t in (1, 2, 3)]
    lines += [r.choices(makers, weights)[0][1]() for _ in range(n)]
    words, _ = asm.assemble("\n".join(lines))
    return words


def write_hex(words, path):
    rom = words + [HALT_WORD] * (256 - len(words))
    Path(path).write_text("\n".join(f"{w:04X}" for w in rom) + "\n")
