"""Reference model of the four-thread protocol-emulator processor, written from docs/isa.md (ISA v2).

Used for differential testing: random programs run on this model and on the RTL, and every
architectural register (including the per-thread data memory) is compared after every clock edge.
"""
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import asm  # noqa: E402  (the real assembler, so the encodings are cross-checked too)

HALT_WORD = 0xF000
OUT_RESET = 0x01
DEFAULT_SCHEDULE = (0, 1, 2, 3)

# Condition field (bits 11:8) of opcode 0x4. Condition 0 is JMP; 9..15 are undefined (never taken).
BRANCH_NAMES = {1: "JC", 2: "JNC", 3: "JZ", 4: "JNZ", 5: "JMI", 6: "JPL", 7: "JEQ", 8: "JNE"}


def valid_schedule(s):
    """A schedule is four slots naming a thread each; no two cyclically adjacent slots may name
    the same thread (slot 3 and slot 0 follow each other too). Equal threads two apart are fine."""
    s = tuple(s)
    return (len(s) == 4 and all(isinstance(t, int) and 0 <= t <= 3 for t in s)
            and all(s[i] != s[(i + 1) % 4] for i in range(4)))


def effective_schedule(s):
    """What the hardware runs when `s` is loaded: `s` itself if it is legal, else the default."""
    return tuple(s) if valid_schedule(s) else DEFAULT_SCHEDULE


class Model:
    """Architectural model of the four-thread processor (docs/isa.md, version 2), plus the 2-flop
    input synchroniser. One call to clock() is one rising edge.

    Schedule: `sched[s]` is the thread that owns slot s. At clock edge c the fetch stage looks at
    thread sched[c % 4]; the instruction fetched there is executed at edge c+1. So edge c >= 1
    executes one instruction of thread sched[(c-1) % 4], provided that thread was running when it
    was fetched (before edge c-1's updates). `sched` must be legal (see valid_schedule); pass
    effective_schedule(loaded_schedule) to model what the hardware does with an illegal one.
    Nothing about the RTL's pipeline registers appears here.
    """

    def __init__(self, rom, sched=DEFAULT_SCHEDULE):
        assert valid_schedule(sched), f"illegal schedule {sched}: use effective_schedule()"
        self.rom = rom + [HALT_WORD] * (256 - len(rom))
        self.sched = tuple(sched)
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
        self.mem = [[0] * 16 for _ in range(4)]    # per-thread data memory, cleared by reset only
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
        self.fetched_running = self.act[self.sched[c % 4]]   # state before this edge's updates
        self.cycle += 1
        if c == 0 or not runs_now:
            return
        self.execute(self.sched[(c - 1) % 4], pins)

    def run(self, n, pins=0):
        """Convenience for directed tests: n edges with constant input pins."""
        for _ in range(n):
            self.clock(pins)

    def execute(self, t, pins):
        if self.wait[t]:
            self.wait[t] -= 1
            return
        w = self.rom[self.pc[t]]
        op, ext = w >> 12, (w >> 8) & 0xF
        imm = w & 0xFF
        pc = self.pc[t]
        pc_next = (pc + 1) & 0xFF
        bit = lambda n: (pins >> n) & 1
        spawn = None
        a, x, cy = self.a[t], self.x[t], self.c[t]    # written back at the end

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
        elif op == 0x4:                        # JMP / conditional branches on C, A, X
            if ext == 0:
                pc_next = imm
                self.hit("JMP")
            elif ext in BRANCH_NAMES:
                take = {1: cy == 1, 2: cy == 0, 3: a == 0, 4: a != 0, 5: a >= 0x80,
                        6: a < 0x80, 7: a == x, 8: a != x}[ext]
                if take:
                    pc_next = imm
                self.hit(BRANCH_NAMES[ext] + ("_taken" if take else "_fall"))
            else:
                self.hit("BR_undefined")       # NOP
        elif op == 0x6:                        # LDA and the ALU operations with an immediate
            cin = cy
            if ext == 0:
                a = imm
                self.hit("LDA")
            elif ext == 1:
                s = a + imm
                a, cy = s & 0xFF, s >> 8
                self.hit("ADD"); self.hit(f"ADD_c{cy}")
            elif ext == 2:
                cy, a = int(a < imm), (a - imm) & 0xFF
                self.hit("SUB"); self.hit(f"SUB_b{cy}")
            elif ext == 3:
                a &= imm
                self.hit("AND")
            elif ext == 4:
                a |= imm
                self.hit("OR")
            elif ext == 5:
                a ^= imm
                self.hit("XOR")
            elif ext == 6:
                cy = int(a < imm)
                self.hit("CMP"); self.hit(f"CMP_b{cy}")
            elif ext == 7:
                s = a + imm + cin
                a, cy = s & 0xFF, s >> 8
                self.hit("ADC"); self.hit(f"ADC_c{cy}"); self.hit(f"ADC_cin{cin}")
            elif ext == 8:
                cy, a = int(a < imm + cin), (a - imm - cin) & 0xFF
                self.hit("SBC"); self.hit(f"SBC_b{cy}"); self.hit(f"SBC_cin{cin}")
            else:
                self.hit("ACC_undefined")      # NOP
        elif op == 0x7:                        # SHR and the accumulator / X operations
            cin = cy
            if ext == 0:
                cy, a = a & 1, a >> 1
                self.hit("SHR")
            elif ext == 1:
                cy, a = a & 1, (cin << 7) | (a >> 1)
                self.hit("ROR"); self.hit(f"ROR_cin{cin}"); self.hit(f"ROR_cout{cy}")
            elif ext == 2:
                cy, a = a >> 7, ((a << 1) & 0xFF) | cin
                self.hit("ROL"); self.hit(f"ROL_cin{cin}"); self.hit(f"ROL_cout{cy}")
            elif ext == 3:
                if a == 0xFF:
                    self.hit("INC_wrap")
                a = (a + 1) & 0xFF
                self.hit("INC")
            elif ext == 4:
                if a == 0:
                    self.hit("DEC_wrap")
                a = (a - 1) & 0xFF
                self.hit("DEC")
            elif ext == 5:
                a = (~a) & 0xFF
                self.hit("NOT")
            elif ext == 6:
                cy = int(a < x)
                self.hit("CMPX"); self.hit(f"CMPX_b{cy}")
            elif ext == 7:
                a = ((a << 4) | (a >> 4)) & 0xFF
                self.hit("SWAP")
            elif ext == 8:
                x = a
                self.hit("TAX")
            elif ext == 9:
                a = x
                self.hit("TXA")
            elif ext == 10:
                a, x = x, a
                self.hit("XCHG")
            elif ext == 11:
                s = a + x
                a, cy = s & 0xFF, s >> 8
                self.hit("ADDX"); self.hit(f"ADDX_c{cy}")
            elif ext == 12:
                cy, a = int(a < x), (a - x) & 0xFF
                self.hit("SUBX"); self.hit(f"SUBX_b{cy}")
            elif ext == 13:
                a &= x
                self.hit("ANDX")
            elif ext == 14:
                a |= x
                self.hit("ORX")
            else:
                a ^= x
                self.hit("XORX")
        elif op == 0x8:
            p = (w >> 8) & 7
            self.pins_out = (self.pins_out & ~(1 << p)) | (cy << p)
            self.hit("OUTC")
        elif op == 0x9:                        # LDX, XADD, XCMP
            if ext == 0:
                x = imm
                self.hit("LDX")
            elif ext == 1:
                x = (x + imm) & 0xFF
                self.hit("XADD")
            elif ext == 2:
                cy = int(x < imm)
                self.hit("XCMP"); self.hit(f"XCMP_b{cy}")
            else:
                self.hit("XIMM_undefined")     # NOP
        elif op == 0xA:
            old = x
            x = (old - 1) & 0xFF
            if old != 1:
                pc_next = imm
                self.hit("DJNZ_taken")
            else:
                self.hit("DJNZ_exit")
        elif op == 0xB:                        # IN, INH, OUTA, ODRVA
            if ext == 0:
                a = pins & 0xFF
                self.hit("IN")
            elif ext == 1:
                a = (pins >> 8) & 0xFF
                self.hit("INH")
            elif ext == 2:
                self.pins_out = a
                self.hit("OUTA")
            elif ext == 3:
                self.od_pull = (~a) & 0xFF     # a 0 bit pulls the pad low, a 1 bit releases it
                self.hit("ODRVA")
            else:
                self.hit("WIDE_undefined")     # NOP
        elif op == 0xC:
            cy, a = (a >> 7) & 1, (a << 1) & 0xFF   # bits 11:8 are ignored, as in version 1
            self.hit("SHL")
        elif op == 0xD:
            a = (a & 0xFE) | bit((w >> 7) & 0xF)
            self.hit("INB")
        elif op == 0xE:
            self.stack[t][self.sp[t]] = pc_next
            self.sp[t] = (self.sp[t] + 1) & 3
            pc_next = imm
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
                self.od_pull = (self.od_pull & ~(1 << p)) | ((1 - cy) << p)
                self.hit("ODRVC")
            elif ext == 0x4:
                self.hit("NOP")
            elif ext == 0x5:                   # SEND: A -> channel, waits while it is full
                if self.mb_full:
                    pc_next = pc
                    self.hit("SEND_block")
                else:
                    self.mb, self.mb_full = a, 1
                    self.hit("SEND_ok")
            elif ext == 0x6:                   # RECV: channel -> A, waits while it is empty
                if not self.mb_full:
                    pc_next = pc
                    self.hit("RECV_block")
                else:
                    a, self.mb_full = self.mb, 0
                    self.hit("RECV_ok")
            elif ext == 0x7:                   # RETL imm8: A = imm8, then return
                a = imm
                self.sp[t] = (self.sp[t] - 1) & 3
                pc_next = self.stack[t][self.sp[t]]
                self.hit("RETL")
            elif 0x8 <= ext <= 0xB:            # SPAWN thread, addr
                target = ext - 0x8
                if target == t or not self.act[target]:
                    spawn = (target, imm)
                    self.hit("SPAWN_self" if target == t else "SPAWN_start")
                else:
                    self.hit("SPAWN_ignored")
            elif ext == 0xC:                   # data memory: bit 7 = store, bit 6 = X-indexed
                off = w & 0xF
                indexed, store = (w >> 6) & 1, (w >> 7) & 1
                if indexed and x + off > 15:
                    self.hit(("STMX" if store else "LDMX") + "_wrap")
                addr = ((x + off) & 15) if indexed else off
                if store:
                    self.mem[t][addr] = a
                else:
                    a = self.mem[t][addr]
                self.hit(("STM" if store else "LDM") + ("X" if indexed else ""))
            elif ext == 0xD:                   # JMPA / CALLA: computed jump to A
                if w & 1:
                    self.stack[t][self.sp[t]] = pc_next
                    self.sp[t] = (self.sp[t] + 1) & 3
                    self.hit("CALLA")
                else:
                    self.hit("JMPA")
                pc_next = a
            else:
                self.hit("EXT_undefined")      # 0xE, 0xF: NOP
        self.a[t], self.x[t], self.c[t] = a, x, cy
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
        out += [self.pins_out, self.od_pull, self.mb, self.mb_full]
        for t in range(4):
            out += self.mem[t]
        return tuple(out)


STATE_NAMES = tuple(f"{n}{t}" for t in range(4)
                    for n in ("pc", "a", "x", "c", "wait", "act", "sp")) + (
    "pins_out", "od_pull", "mb", "mb_full") + tuple(f"mem{t}_{i}" for t in range(4) for i in range(16))


def _undefined_word(r, kind):
    """A word with an undefined sub-opcode (docs/isa.md): the hardware must execute it as NOP."""
    if kind == 0:
        return 0x4000 | (r.randrange(9, 16) << 8) | r.randrange(256)    # branch condition 9..15
    if kind == 1:
        return 0x6000 | (r.randrange(9, 16) << 8) | r.randrange(256)    # accumulator op 9..15
    if kind == 2:
        return 0x9000 | (r.randrange(3, 16) << 8) | r.randrange(256)    # X op 3..15
    if kind == 3:
        return 0xB000 | (r.randrange(4, 16) << 8) | r.randrange(256)    # wide pin op 4..15
    return 0xF000 | (r.randrange(0xE, 0x10) << 8) | r.randrange(256)    # extended sub-opcode 0xE, 0xF


def _idioms():
    """Straight-line snippets with known outcomes (each returns assembly lines for base index b).
    gen_program puts a random selection of them at the start of the program, where thread 0 runs
    them once, so that every carry / borrow / branch outcome is reached no matter where the random
    control flow of the rest of the program ends up. Their results are checked like everything else:
    against the model, on every clock edge."""
    def skip(cond, setup, b):          # setup; Jcond over one instruction (taken, then not taken)
        return setup + [f"{cond} {b + len(setup) + 2}", "INC", "NOP"]

    ids = []
    for cond, taken, fall in (("JC", ["LDA 255", "ADD 1"], ["LDA 1", "ADD 1"]),
                              ("JNC", ["LDA 1", "ADD 1"], ["LDA 255", "ADD 1"]),
                              ("JZ", ["LDA 0"], ["LDA 1"]), ("JNZ", ["LDA 1"], ["LDA 0"]),
                              ("JMI", ["LDA 128"], ["LDA 127"]), ("JPL", ["LDA 127"], ["LDA 128"]),
                              ("JEQ", ["LDA 7", "LDX 7"], ["LDA 7", "LDX 8"]),
                              ("JNE", ["LDA 7", "LDX 8"], ["LDA 7", "LDX 7"])):
        ids.append(lambda b, cond=cond, taken=taken, fall=fall:
                   skip(cond, taken, b) + skip(cond, fall, b + len(taken) + 3))
    ids += [
        # ADD carry set / clear, ADC with carry in 1 and 0, 16-bit style chain
        lambda b: ["LDA 255", "ADD 1", "ADD 1", "LDA 255", "ADD 1", "ADC 255", "ADC 0", "ADC 5"],
        # SUB / SBC borrow set / clear, SBC with borrow in 1 and 0
        lambda b: ["LDA 0", "SUB 1", "SBC 0", "SBC 255", "SUB 0", "LDA 5", "SBC 5"],
        lambda b: ["LDA 5", "CMP 6", "CMP 5", "CMP 4", "CMP 0", "CMP 255"],
        # CMPX, XCMP, ADDX (carry set and clear), SUBX (borrow set and clear), XADD
        lambda b: ["LDA 5", "LDX 6", "CMPX", "XCHG", "CMPX", "LDX 200", "LDA 100", "ADDX", "SUBX",
                   "XCMP 201", "XCMP 5", "LDA 1", "ADDX", "SUBX", "XADD 100", "XADD 200", "XCMP 0"],
        lambda b: ["LDA 255", "INC", "DEC", "DEC", "NOT", "SWAP", "AND 15", "OR 128", "XOR 255"],
        # ROR / ROL with carry in 0 / 1 and carry out 0 / 1
        lambda b: ["LDA 3", "SHR", "ROR", "ROR", "ROR", "ROR", "LDA 192", "SHL", "ROL", "ROL", "ROL",
                   "ROL"],
        # data memory: direct, indexed, wrap of (X + off) & 15
        lambda b: ["LDA 165", "STM 3", "LDA 0", "LDM 3", "LDX 15", "LDA 90", "STMX 3", "LDMX 3",
                   "LDX 14", "LDMX 5", "LDA 9", "STMX 2", "LDX 0", "LDMX 15", "LDM 2", "LDM 15"],
        lambda b: ["LDA 240", "OUTA", "ODRVA", "INH", "IN", "TAX", "ANDX", "ORX", "XORX", "TXA",
                   "LDA 255", "ODRVA"],
        # DJNZ: loop three times, then exit; and exit on the first pass
        lambda b: ["LDX 3", "NOP", f"DJNZ {b + 1}", "LDX 1", f"DJNZ {b}"],
        # CALLA into a RETL table, then JMPA over the table
        lambda b: ["LDA %d" % (b + 4), "CALLA", f"JMP {b + 7}", "NOP", "RETL 66", "NOP", "NOP",
                   "LDA %d" % (b + 10), "JMPA", "NOP", "NOP"],
        # undefined sub-opcodes are NOPs
        lambda b: ["RAW0", "RAW1", "RAW2", "RAW3", "RAW4", "RAW4"],
    ]
    return ids


def gen_program(seed):
    """Random assembly program. Every mnemonic is exercised; branch targets are random.

    Roughly one program in five is version-1 only (the original instruction mix). The others mix in
    the version-2 instructions, undefined sub-opcodes (which must behave as NOP) and, in most of them,
    a prelude of short snippets with known outcomes (carry / borrow / branch taken and not taken,
    memory wraparound, CALLA into a RETL table, ...). Programs built this way never contain a 0xCxxx
    word with nonzero bits 11:8 (docs/isa.md is ambiguous about those).
    """
    r = random.Random(seed)
    n = r.randint(40, 253)       # + 3 SPAWN lines must still fit in 256 words
    cur = [3]                    # index of the line being generated

    def addr():
        # Mostly near the branch (forward: the program makes progress through its code; backward:
        # loops), sometimes anywhere, including the HALT-filled tail.
        p = r.random()
        if p < 0.60:
            tgt = cur[0] + r.randrange(1, 16)
        elif p < 0.70:
            tgt = cur[0] - r.randrange(0, 12)
        else:
            tgt = r.randrange(n + 8)
        return min(max(tgt, 0), 255)

    def imm():                   # corner values are common, so carries and borrows happen
        return r.choice([0, 1, 2, 0x7F, 0x80, 0xFF, 0xFE, 0x55, 0xAA, r.randrange(256),
                         r.randrange(256), r.randrange(256)])

    # (name, weight, generator). Version 1 first (the original mix), then version 2.
    v1 = [
        ("PIN", 8, lambda: f"PIN {r.randrange(8)} {r.randrange(2)}"),
        ("OUT", 3, lambda: f"OUT {r.randrange(256)}"),
        ("WAIT", 8, lambda: f"WAIT {r.choice([0, 0, 1, 2, 3, 5, r.randrange(40)])}"),
        ("JMP", 5, lambda: f"JMP {addr()}"),
        ("JPIN", 14, lambda: f"JPIN {r.randrange(16)} {r.randrange(2)} {addr()}"),
        ("LDA", 5, lambda: f"LDA {imm() if r.random() < 0.5 else r.randrange(256)}"),
        ("SHR", 5, lambda: "SHR"),
        ("OUTC", 6, lambda: f"OUTC {r.randrange(8)}"),
        ("LDX", 5, lambda: f"LDX {r.choice([0, 1, 2, 3, 8, 0x7F, 0x80, 0xFF, r.randrange(256)])}"),
        ("DJNZ", 6, lambda: f"DJNZ {addr()}"),
        ("IN", 4, lambda: "IN"),
        ("SHL", 5, lambda: "SHL"),
        ("INB", 6, lambda: f"INB {r.randrange(16)}"),
        ("CALL", 6, lambda: f"CALL {addr()}"),
        ("RET", 5, lambda: "RET"),
        ("ODRV", 6, lambda: f"ODRV {r.randrange(8)} {r.randrange(2)}"),
        ("ODRVC", 5, lambda: f"ODRVC {r.randrange(8)}"),
        ("NOP", 2, lambda: "NOP"),
        ("HALT", 2, lambda: "HALT"),
        ("SPAWN", 1, lambda: f"SPAWN {r.randrange(4)} {addr()}"),
        ("SEND", 2, lambda: "SEND"),
        ("RECV", 2, lambda: "RECV"),
    ]
    v2 = [(m, 3, (lambda m=m: f"{m} {addr()}")) for m in
          ("JC", "JNC", "JZ", "JNZ", "JMI", "JPL", "JEQ", "JNE")]
    v2 += [(m, 3, (lambda m=m: f"{m} {imm()}")) for m in
           ("ADD", "SUB", "AND", "OR", "XOR", "CMP", "ADC", "SBC")]
    v2 += [(m, 2, (lambda m=m: m)) for m in
           ("ROR", "ROL", "INC", "DEC", "NOT", "CMPX", "SWAP", "TAX", "TXA", "XCHG", "ADDX",
            "SUBX", "ANDX", "ORX", "XORX")]
    v2 += [(m, 2, (lambda m=m: f"{m} {imm()}")) for m in ("XADD", "XCMP")]
    v2 += [(m, 2, (lambda m=m: m)) for m in ("INH", "OUTA", "ODRVA")]
    v2 += [(m, 3, (lambda m=m: f"{m} {r.randrange(16)}")) for m in ("LDM", "STM", "LDMX", "STMX")]
    v2 += [("JMPA", 1, lambda: "JMPA"), ("CALLA", 1, lambda: "CALLA"),
           ("RETL", 2, lambda: f"RETL {imm()}"),
           ("UNDEF", 4, lambda: f"RAW{r.randrange(5)}")]

    weight = {name: w for name, w, _ in v1}
    gens = {name: g for name, _, g in v1}
    v2_scale = 0 if seed % 5 == 2 else (2 if seed % 4 == 1 else 1)   # pure v1 / v2-heavy / mixed
    for name, w, g in v2:
        weight[name] = w * v2_scale
        gens[name] = g
    if seed % 3 == 0:     # channel-heavy programs: lots of SEND / RECV, no HALTs
        weight["SEND"] = weight["RECV"] = 8
        weight["HALT"] = 0
    names = list(weight)
    weights = [weight[k] for k in names]

    # Start the other three threads at random places first, ...
    lines = []
    for t in (1, 2, 3):
        cur[0] = len(lines)
        lines.append(f"SPAWN {t} {addr()}")
    # ... then (version 2 programs) the prelude, then random code (shared by all threads).
    if v2_scale and r.random() < 0.8:
        idioms = [f for f in _idioms() if r.random() < 0.55]
        r.shuffle(idioms)
        for f in idioms:
            lines += f(len(lines))
        lines += ["LDA 90", "SEND", "RECV"]    # last: if another thread steals the byte this blocks
    n = min(max(n, len(lines) - 3 + 20), 253)
    while len(lines) < n + 3:
        cur[0] = len(lines)
        lines.append(gens[r.choices(names, weights)[0]]())
    raw = {}
    for i, line in enumerate(lines):
        if line.startswith("RAW"):             # undefined sub-opcode: assembled as NOP, patched below
            raw[i], lines[i] = _undefined_word(r, int(line[3:])), "NOP"
    words, _ = asm.assemble("\n".join(lines))
    for i, word in raw.items():
        words[i] = word
    return words


def gen_schedule(seed):
    """Random four-slot schedule for the loader's byte 512 (see docs/isa.md, 'Timing and the
    schedule'). Mostly legal ones, including the default, (0,1,0,2), (0,1,0,1), (3,2,3,0) and
    random ones; about one in eight is ILLEGAL (a thread owns two adjacent slots), which the
    hardware must ignore (it runs the default schedule), see effective_schedule()."""
    r = random.Random(seed * 7919 + 13)
    if r.random() < 1 / 8:
        while True:
            s = tuple(r.randrange(4) for _ in range(4))
            if not valid_schedule(s):
                return s
    named = [(0, 1, 2, 3), (0, 1, 0, 2), (0, 1, 0, 1), (3, 2, 3, 0), (0, 2, 0, 3), (1, 0, 3, 0),
             (0, 3, 2, 1), (1, 0, 1, 2)]
    if r.random() < 0.4:
        return r.choice(named)
    while True:
        s = tuple(r.randrange(4) for _ in range(4))
        # a schedule without thread 0 never runs anything (thread 0 is the only one running at reset)
        if valid_schedule(s) and (0 in s or r.random() < 0.05):
            return s


def write_hex(words, path):
    rom = words + [HALT_WORD] * (256 - len(words))
    Path(path).write_text("\n".join(f"{w:04X}" for w in rom) + "\n")
