#!/usr/bin/env python3
"""Static timing analysis of protocol-emulator firmware: protocol timing as a proof, not a measurement.

Usage: python3 tools/timing.py SPEC [SPEC ...] [--verbose]

Why this is possible. The processor is a deterministic barrel machine: a thread runs one instruction
per turn of its slot(s), a turn is a fixed number of clocks (4 by default, 2 for a thread that owns two
slots), a `WAIT n` takes 1 + n turns and nothing else takes longer. No other thread, interrupt or
arbitration can make a thread late (formal/isolation.v proves the isolation, formal/pipeline.v the
one-instruction-per-turn pipeline). The distance in time between two instructions of one thread is
therefore a function of the path the thread took between them, and nothing else.

What this tool does. It explores every path the firmware can take (inputs are free: every pin is
unknown, every `JPIN` goes both ways) with an abstract interpreter of the instruction set that keeps A, X,
C and the 16 data bytes as known numbers where they are known and as "unknown" where they depend on input.
From the resulting graph it computes, for two named pin events, the shortest and the longest time between
an occurrence of the first and the next occurrence of the second. A check is then a comparison with a
number taken from the protocol standard, and it either holds for every possible execution or it does not
(with the path that breaks it). A bound that depends on the outside world (a spin on an input) is
reported as unbounded, never guessed.

Spec file (one statement per line, '#' starts a comment):

    program FILE.asm [FILE.asm ...]   assembly sources, concatenated like tools/build_rom.sh (relative to the spec)
    thread N                          the hardware thread that runs the code (default 0)
    sched a,b,c,d                     schedule (default 0,1,2,3); gives the clocks per turn of that thread
    clock_mhz F                       default 50
    start LABEL|N                     entry address
    init A=3 X=? C=0                  initial registers ('?' = unknown; default: all 0 as after SPAWN / reset)
    mem zero|unknown                  initial data memory (default unknown)
    nonblocking                       SEND / RECV complete at once (default: they may block for ever)
    event NAME = out P | out P=V | pull P | release P | od P | at LABEL[+N]   (alternatives: a | b)
    check A -> B [avoid E] == 8.64us | >= 4.7us | <= 50ns | in [3.9us, 5us] | in [200ns, 500ns] [550ns, 850ns]
    report A -> B                     print the range without a verdict

Events: `out P` is any instruction that writes push-pull output P (PIN, OUT, OUTC, OUTA); `out P=V` only
when it writes the value V; `pull P` / `release P` / `od P` the same for open-drain pad P; `at L` is the
execution of the instruction at address / label L. The time from event A to B is measured from an
execution of A to the *next* execution of B (A may equal B: consecutive occurrences).
Units: clk, turn, ns, us, ms.

Soundness. An unknown value is treated as either; an event that depends on an unknown value (`out 0=1`
written from an unknown carry) counts as "may" for minimums and is ignored for maximums, so every reported
range contains every real execution. test/test_timing.py checks the abstract interpreter against
test/isa_model.py on random programs and checks the ranges against RTL simulation traces.
"""
import argparse
import heapq
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import asm  # noqa: E402

UNKNOWN = None
DEFAULT_SCHED = (0, 1, 2, 3)
MAX_NODES = 3_000_000


class AnalysisError(Exception):
    pass


# ---------------------------------------------------------------------------------------------
# Abstract state: (pc, a, x, c, sp, stack(4), mem(16)); a / x / c / stack entries / mem bytes are an
# int or None (unknown).
# ---------------------------------------------------------------------------------------------

def initial_state(pc=0, a=0, x=0, c=0, mem_known=False):
    return (pc, a, x, c, 0, (None,) * 4, (0,) * 16 if mem_known else (None,) * 16)


def _cond(ext, a, x, c):
    """Value of a branch condition: True / False / None (unknown)."""
    if ext == 1:
        return None if c is None else c == 1
    if ext == 2:
        return None if c is None else c == 0
    if ext in (3, 4, 5, 6):
        if a is None:
            return None
        return {3: a == 0, 4: a != 0, 5: a >= 0x80, 6: a < 0x80}[ext]
    if ext in (7, 8):
        if a is None or x is None:
            return None
        return (a == x) if ext == 7 else (a != x)
    return False                         # 9..15 are NOPs


def successors(st, w, thread, nonblocking):
    """All possible next states of the instruction word `w` executed in state `st`.

    Returns a list of (next_state | None, cost_in_turns, effects). A next state of None means the
    thread ends (HALT, or a return to an unknown caller: the end of the analysed routine).
    `effects` is a tuple of ('out', pin, value) / ('od', pin, value) with value 0, 1 or None (unknown);
    for 'od' the value is the pad level (0 = pulled low, 1 = released).
    """
    pc, a, x, c, sp, stk, mem = st
    op, ext, imm = w >> 12, (w >> 8) & 0xF, w & 0xFF
    pcn = (pc + 1) & 0xFF
    eff = ()
    cost = 1

    def nxt(npc=pcn, a=a, x=x, c=c, sp=sp, stk=stk, mem=mem):
        return (npc, a, x, c, sp, stk, mem)

    def bit(v, i):
        return None if v is None else (v >> i) & 1

    if op in (0x0, 0x5):                                    # JPIN p v addr: the pin is free
        tgt = (((w >> 11) & 1) << 7) | (w & 0x7F)
        return [(nxt(npc=tgt), 1, ()), (nxt(), 1, ())]
    if op == 0x1:                                           # PIN p v
        return [(nxt(), 1, (("out", (w >> 8) & 7, w & 1),))]
    if op == 0x2:                                           # OUT imm
        return [(nxt(), 1, tuple(("out", p, (imm >> p) & 1) for p in range(8)))]
    if op == 0x3:                                           # WAIT n
        return [(nxt(), 1 + (w & 0xFFF), ())]
    if op == 0x4:                                           # JMP and the conditional branches
        if ext == 0:
            return [(nxt(npc=imm), 1, ())]
        t = _cond(ext, a, x, c)
        out = []
        if t is None or t:
            out.append((nxt(npc=imm), 1, ()))
        if t is None or not t:
            out.append((nxt(), 1, ()))
        return out
    if op == 0x6:                                           # LDA and the immediate ALU operations
        if ext == 0:
            return [(nxt(a=imm), 1, ())]
        if ext == 1:
            if a is None:
                return [(nxt(a=None, c=None), 1, ())]
            s = a + imm
            return [(nxt(a=s & 0xFF, c=s >> 8), 1, ())]
        if ext == 2:
            if a is None:
                return [(nxt(a=None, c=None), 1, ())]
            return [(nxt(a=(a - imm) & 0xFF, c=int(a < imm)), 1, ())]
        if ext == 3:
            return [(nxt(a=0 if imm == 0 else (None if a is None else a & imm)), 1, ())]
        if ext == 4:
            return [(nxt(a=0xFF if imm == 0xFF else (None if a is None else a | imm)), 1, ())]
        if ext == 5:
            return [(nxt(a=None if a is None else a ^ imm), 1, ())]
        if ext == 6:
            return [(nxt(c=None if a is None else int(a < imm)), 1, ())]
        if ext == 7:
            if a is None or c is None:
                return [(nxt(a=None, c=None), 1, ())]
            s = a + imm + c
            return [(nxt(a=s & 0xFF, c=s >> 8), 1, ())]
        if ext == 8:
            if a is None or c is None:
                return [(nxt(a=None, c=None), 1, ())]
            return [(nxt(a=(a - imm - c) & 0xFF, c=int(a < imm + c)), 1, ())]
        return [(nxt(), 1, ())]                             # undefined: NOP
    if op == 0x7:                                           # SHR and the A / X operations
        if ext == 0:
            return [(nxt(a=None if a is None else a >> 1, c=bit(a, 0)), 1, ())]
        if ext == 1:
            na = None if (a is None or c is None) else (c << 7) | (a >> 1)
            return [(nxt(a=na, c=bit(a, 0)), 1, ())]
        if ext == 2:
            na = None if (a is None or c is None) else ((a << 1) & 0xFF) | c
            return [(nxt(a=na, c=bit(a, 7)), 1, ())]
        if ext == 3:
            return [(nxt(a=None if a is None else (a + 1) & 0xFF), 1, ())]
        if ext == 4:
            return [(nxt(a=None if a is None else (a - 1) & 0xFF), 1, ())]
        if ext == 5:
            return [(nxt(a=None if a is None else (~a) & 0xFF), 1, ())]
        if ext == 6:
            return [(nxt(c=None if (a is None or x is None) else int(a < x)), 1, ())]
        if ext == 7:
            return [(nxt(a=None if a is None else ((a << 4) | (a >> 4)) & 0xFF), 1, ())]
        if ext == 8:
            return [(nxt(x=a), 1, ())]
        if ext == 9:
            return [(nxt(a=x), 1, ())]
        if ext == 10:
            return [(nxt(a=x, x=a), 1, ())]
        if a is None or x is None:                          # ADDX SUBX ANDX ORX XORX
            known_c = ext in (13, 14, 15)
            return [(nxt(a=0 if (ext == 13 and (a == 0 or x == 0)) else None,
                         c=c if known_c else None), 1, ())]
        if ext == 11:
            s = a + x
            return [(nxt(a=s & 0xFF, c=s >> 8), 1, ())]
        if ext == 12:
            return [(nxt(a=(a - x) & 0xFF, c=int(a < x)), 1, ())]
        return [(nxt(a={13: a & x, 14: a | x, 15: a ^ x}[ext]), 1, ())]
    if op == 0x8:                                           # OUTC p
        return [(nxt(), 1, (("out", (w >> 8) & 7, c),))]
    if op == 0x9:                                           # LDX / XADD / XCMP
        if ext == 0:
            return [(nxt(x=imm), 1, ())]
        if ext == 1:
            return [(nxt(x=None if x is None else (x + imm) & 0xFF), 1, ())]
        if ext == 2:
            return [(nxt(c=None if x is None else int(x < imm)), 1, ())]
        return [(nxt(), 1, ())]
    if op == 0xA:                                           # DJNZ
        if x is None:
            return [(nxt(npc=imm), 1, ()), (nxt(), 1, ())]
        nx = (x - 1) & 0xFF
        return [(nxt(npc=imm, x=nx), 1, ())] if x != 1 else [(nxt(x=nx), 1, ())]
    if op == 0xB:                                           # IN INH OUTA ODRVA
        if ext in (0, 1):
            return [(nxt(a=None), 1, ())]
        if ext == 2:
            return [(nxt(), 1, tuple(("out", p, bit(a, p)) for p in range(8)))]
        if ext == 3:
            return [(nxt(), 1, tuple(("od", p, bit(a, p)) for p in range(8)))]
        return [(nxt(), 1, ())]
    if op == 0xC:                                           # SHL
        if ext == 0:
            return [(nxt(a=None if a is None else (a << 1) & 0xFF, c=bit(a, 7)), 1, ())]
        return [(nxt(), 1, ())]
    if op == 0xD:                                           # INB p
        return [(nxt(a=None), 1, ())]
    if op == 0xE:                                           # CALL
        ns = list(stk)
        ns[sp] = pcn
        return [(nxt(npc=imm, sp=(sp + 1) & 3, stk=tuple(ns)), 1, ())]
    # op == 0xF: extended family
    if ext == 0x0:
        return [(None, 1, ())]                              # HALT
    if ext in (0x1, 0x7):                                   # RET / RETL imm
        nsp = (sp - 1) & 3
        na = imm if ext == 0x7 else a
        if stk[nsp] is None:
            return [(None, 1, ())]                          # return to an unknown caller: end of region
        return [(nxt(npc=stk[nsp], sp=nsp, a=na), 1, ())]
    if ext == 0x2:                                          # ODRV p v
        return [(nxt(), 1, (("od", w & 7, (w >> 3) & 1),))]
    if ext == 0x3:                                          # ODRVC p
        return [(nxt(), 1, (("od", w & 7, c),))]
    if ext == 0x4:
        return [(nxt(), 1, ())]                             # NOP
    if ext == 0x5:                                          # SEND
        return [(nxt(), 1, ())] if nonblocking else [(nxt(), 1, ()), (nxt(npc=pc), 1, ())]
    if ext == 0x6:                                          # RECV
        return ([(nxt(a=None), 1, ())] if nonblocking
                else [(nxt(a=None), 1, ()), (nxt(npc=pc), 1, ())])
    if 0x8 <= ext <= 0xB:                                   # SPAWN t addr
        if ext - 0x8 == thread:                             # restart of the running thread
            return [(nxt(npc=imm, a=0, x=0, c=0, sp=0), 1, ())]
        return [(nxt(), 1, ())]
    if ext == 0xC:                                          # data memory
        off = w & 0xF
        indexed, store = (w >> 6) & 1, (w >> 7) & 1
        if indexed and x is None:
            if store:
                return [(nxt(mem=(None,) * 16), 1, ())]
            return [(nxt(a=None), 1, ())]
        addr = ((x + off) & 15) if indexed else off
        if store:
            nm = list(mem)
            nm[addr] = a
            return [(nxt(mem=tuple(nm)), 1, ())]
        return [(nxt(a=mem[addr]), 1, ())]
    if ext == 0xD:                                          # JMPA / CALLA
        if a is None:
            raise AnalysisError(f"address {pc}: computed jump with an unknown A (cannot follow it)")
        if w & 1:
            ns = list(stk)
            ns[sp] = pcn
            return [(nxt(npc=a, sp=(sp + 1) & 3, stk=tuple(ns)), 1, ())]
        return [(nxt(npc=a), 1, ())]
    return [(nxt(), 1, ())]                                 # 0xE, 0xF: NOP


# ---------------------------------------------------------------------------------------------
# The graph of abstract states
# ---------------------------------------------------------------------------------------------

class Graph:
    """Reachable abstract states. Node i: state[i], pcs[i], effects of executing its instruction,
    and succ[i] = [(node, turns)]: the time from the execution of this node's instruction to the
    execution of the successor's."""

    def __init__(self, words, start_state, thread=0, nonblocking=False, max_nodes=MAX_NODES):
        rom = list(words) + [0xF000] * (256 - len(words))
        self.words = rom
        self.state, self.pcs, self.effects, self.succ = [start_state], [start_state[0]], [()], [None]
        index = {start_state: 0}
        todo = [0]
        while todo:
            i = todo.pop()
            st = self.state[i]
            w = rom[st[0]]
            outs = successors(st, w, thread, nonblocking)
            edges = []
            eff = ()
            for ns, cost, ef in outs:
                eff = ef or eff
                if ns is None:
                    continue
                j = index.get(ns)
                if j is None:
                    j = len(self.state)
                    if j >= max_nodes:
                        raise AnalysisError(
                            f"more than {max_nodes} abstract states: the firmware keeps too much "
                            "known data (try `nonblocking`, a smaller region, or start later)")
                    index[ns] = j
                    self.state.append(ns)
                    self.pcs.append(ns[0])
                    self.effects.append(())
                    self.succ.append(None)
                    todo.append(j)
                edges.append((j, cost))
            self.effects[i] = eff
            self.succ[i] = edges
        self.n = len(self.state)

    # -- events ---------------------------------------------------------------------------------
    def nodes_where(self, alts, must):
        """Node ids that execute one of the event alternatives `alts` ("may" occurrence by default,
        "must" occurrence if requested)."""
        res = set()
        for ev in alts:
            kind = ev[0]
            if kind == "at":
                res |= {i for i in range(self.n) if self.pcs[i] == ev[1]}
                continue
            for i in range(self.n):
                for (k, p, v) in self.effects[i]:
                    if k != kind or p != ev[1]:
                        continue
                    if ev[2] is None or v == ev[2] or (v is None and not must):
                        res.add(i)
        return res

    # -- shortest / longest time between two events ------------------------------------------------
    def min_time(self, frm, to, avoid=frozenset()):
        """Smallest number of turns from an execution of a node in `frm` to a later one in `to`
        (the first `to` after it). Returns (turns, path) or None if no such pair exists."""
        dist, prev, heap = {}, {}, []
        for u in frm:
            for v, c in self.succ[u]:
                if v in avoid:
                    continue
                if c < dist.get(v, 1 << 60):
                    dist[v], prev[v] = c, (u, True)          # True: reached straight from a `frm` node
                    heapq.heappush(heap, (c, v))
        done = set()
        while heap:
            d, v = heapq.heappop(heap)
            if v in done or d > dist[v]:
                continue
            done.add(v)
            if v in to:
                path = [v]
                while True:
                    u, direct = prev[path[-1]]
                    path.append(u)
                    if direct:
                        break
                return d, path[::-1]
            for w, c in self.succ[v]:
                if w in avoid:
                    continue
                if d + c < dist.get(w, 1 << 60):
                    dist[w], prev[w] = d + c, (v, False)
                    heapq.heappush(heap, (d + c, w))
        return None

    def max_time(self, frm, to, avoid=frozenset()):
        """Largest number of turns from an execution of a node in `frm` to the next execution of a node
        in `to`. Returns (turns | 'inf', witness path, never) where `never` says that some path from
        `frm` never reaches `to`; (None, ...) if no path reaches it at all."""
        # region reachable from the successors of `frm` without going beyond a `to` node
        region, stack = set(), []
        for u in frm:
            for v, _ in self.succ[u]:
                if v not in region and v not in avoid:
                    region.add(v)
                    stack.append(v)
        while stack:
            v = stack.pop()
            if v in to:
                continue
            for w, _ in self.succ[v]:
                if w not in region and w not in avoid:
                    region.add(w)
                    stack.append(w)
        # which region nodes can still reach `to`
        radj = {}
        for v in region:
            if v in to:
                continue
            for w, c in self.succ[v]:
                radj.setdefault(w, []).append(v)
        can = {v for v in region if v in to}
        stack = list(can)
        while stack:
            w = stack.pop()
            for v in radj.get(w, ()):
                if v not in can:
                    can.add(v)
                    stack.append(v)
        never = any(v not in can for v in region)
        if not can:
            return None, [], never
        # longest path by iterative DFS (cycle => unbounded)
        WHITE, GREY, BLACK = 0, 1, 2
        color, best, arg = {}, {}, {}
        inf = False
        for root in can:
            if color.get(root, WHITE) != WHITE:
                continue
            stk = [(root, iter(() if root in to else self.succ[root]))]
            color[root] = GREY
            while stk:
                v, it = stk[-1]
                adv = False
                for w, c in it:
                    if w not in can:
                        continue
                    cw = color.get(w, WHITE)
                    if cw == GREY:
                        inf = True
                    elif cw == WHITE:
                        color[w] = GREY
                        stk.append((w, iter(() if w in to else self.succ[w])))
                        adv = True
                        break
                if adv:
                    continue
                stk.pop()
                color[v] = BLACK
                if v in to:
                    best[v] = 0
                    continue
                b, a_ = None, None
                for w, c in self.succ[v]:
                    if w in can and w in best and (b is None or c + best[w] > b):
                        b, a_ = c + best[w], w
                best[v] = b if b is not None else 0
                arg[v] = a_
        if inf:
            return "inf", [], never
        top, topn, first = None, None, None
        for u in frm:
            for v, c in self.succ[u]:
                if v in can and v in best and (top is None or c + best[v] > top):
                    top, topn, first = c + best[v], v, u
        if top is None:
            return None, [], never
        path, v = [first, topn], topn
        while v in arg and arg[v] is not None and v not in to:
            v = arg[v]
            path.append(v)
        return top, path, never


    def times_set(self, frm, to_must, to_may, cap=200000, avoid=frozenset()):
        """The set of all possible numbers of turns from an execution of `frm` to the next execution of
        a `to` event (a "may" node can be the next occurrence or can be passed over). Returns None if the
        set is infinite (a loop that can repeat for ever before the event) or larger than `cap`."""
        region, stack = set(), []
        for u in frm:
            for v, _ in self.succ[u]:
                if v not in region and v not in avoid:
                    region.add(v)
                    stack.append(v)
        while stack:
            v = stack.pop()
            if v in to_must:
                continue
            for w, _ in self.succ[v]:
                if w not in region and w not in avoid:
                    region.add(w)
                    stack.append(w)
        radj = {}
        for v in region:
            if v in to_must:
                continue
            for w, _ in self.succ[v]:
                radj.setdefault(w, []).append(v)
        can = {v for v in region if v in to_may or v in to_must}
        stack = list(can)
        while stack:
            w = stack.pop()
            for v in radj.get(w, ()):
                if v not in can:
                    can.add(v)
                    stack.append(v)
        color, sets = {}, {}
        for root in can:
            if color.get(root, 0):
                continue
            stk = [(root, iter(() if root in to_must else self.succ[root]))]
            color[root] = 1
            while stk:
                v, it = stk[-1]
                adv = False
                for w, c in it:
                    if w not in can:
                        continue
                    cw = color.get(w, 0)
                    if cw == 1:
                        return None                       # a loop before the event: unbounded
                    if cw == 0:
                        color[w] = 1
                        stk.append((w, iter(() if w in to_must else self.succ[w])))
                        adv = True
                        break
                if adv:
                    continue
                stk.pop()
                color[v] = 2
                acc = {0} if (v in to_must or v in to_may) else set()
                if v not in to_must:
                    for w, c in self.succ[v]:
                        if w in can:
                            acc |= {c + t for t in sets[w]}
                if len(acc) > cap:
                    return None
                sets[v] = acc
        res = set()
        for u in frm:
            for v, c in self.succ[u]:
                if v in can:
                    res |= {c + t for t in sets[v]}
        return res


# ---------------------------------------------------------------------------------------------
# Spec files
# ---------------------------------------------------------------------------------------------

UNIT_NS = {"ns": 1.0, "us": 1e3, "ms": 1e6}


class Spec:
    def __init__(self, path):
        self.path = Path(path)
        self.programs, self.thread, self.sched = [], 0, DEFAULT_SCHED
        self.clock_mhz, self.start, self.init = 50.0, 0, {"A": 0, "X": 0, "C": 0}
        self.mem_zero, self.nonblocking = False, False
        self.events, self.checks, self.reports = {}, [], []
        self.parse()

    def err(self, n, msg):
        raise SystemExit(f"{self.path}:{n}: {msg}")

    def parse(self):
        for n, raw in enumerate(self.path.read_text().splitlines(), 1):
            line = raw.split("#")[0].strip()
            if not line:
                continue
            m = re.match(r"(\w+)\s*(.*)$", line)
            kw, rest = m.group(1), m.group(2).strip()
            if kw == "program":
                self.programs += [self.path.parent / p for p in rest.split()]
            elif kw == "thread":
                self.thread = int(rest)
            elif kw == "sched":
                self.sched = tuple(int(v) for v in rest.split(","))
            elif kw == "clock_mhz":
                self.clock_mhz = float(rest)
            elif kw == "start":
                self.start = rest
            elif kw == "init":
                for item in rest.split():
                    k, v = item.split("=")
                    self.init[k.upper()] = None if v == "?" else int(v, 0)
            elif kw == "mem":
                self.mem_zero = rest == "zero"
            elif kw == "nonblocking":
                self.nonblocking = True
            elif kw == "event":
                m2 = re.match(r"(\w+)\s*=\s*(.*)$", rest)
                if not m2:
                    self.err(n, "event NAME = ...")
                self.events[m2.group(1)] = self.event(m2.group(2).strip(), n)
            elif kw in ("check", "report"):
                m2 = re.match(r"(\w+)\s*->\s*(\w+)\s*(.*?)(?:\s+avoid\s+(\w+))?\s*$", rest)
                if not m2:
                    self.err(n, f"{kw} A -> B ... [avoid E]")
                a, b, cond, av = m2.groups()
                for e in (a, b, av):
                    if e is not None and e not in self.events:
                        self.err(n, f"unknown event '{e}'")
                (self.checks if kw == "check" else self.reports).append((a, b, cond.strip(), n, av))
            else:
                self.err(n, f"unknown statement '{kw}'")

    def event(self, text, n):
        """An event is one or more alternatives separated by '|'."""
        alts = []
        for part in text.split("|"):
            m = re.match(r"\s*(out|od|pull|release|at)\s+(\S+?)(?:=([01]))?\s*$", part)
            if not m:
                self.err(n, f"bad event '{part.strip()}'")
            kind, arg, val = m.groups()
            if kind == "at":
                alts.append(("at", arg))
                continue
            pin = int(arg)
            if kind == "pull":
                alts.append(("od", pin, 0))
            elif kind == "release":
                alts.append(("od", pin, 1))
            else:
                alts.append((kind, pin, None if val is None else int(val)))
        return alts

    def clocks_per_turn(self):
        k = sum(1 for s in self.sched if s == self.thread)
        if k == 0:
            raise SystemExit(f"{self.path}: thread {self.thread} is not in schedule {self.sched}")
        return 4 // k

    def condition(self, text, n):
        """Parse '== 8.64us', '>= 4.7us', 'in [a, b]', 'in [a, b] [c, d]' into a list of windows
        (lo_ns, hi_ns), None = open ended, and the text."""
        def val(s):
            m = re.match(r"([0-9.]+)\s*(clk|clocks?|turns?|ns|us|ms)$", s.strip())
            if not m:
                self.err(n, f"bad value '{s}' (number + clk|turn|ns|us|ms)")
            u = m.group(2)
            num = float(m.group(1))
            if u.startswith("clk") or u.startswith("clock"):
                return num * 1000.0 / self.clock_mhz
            if u.startswith("turn"):
                return num * self.clocks_per_turn() * 1000.0 / self.clock_mhz
            return num * UNIT_NS[u]
        text = text.strip()
        if not text:
            return [(None, None)], ""
        m = re.match(r"in\s*(.*)$", text)
        if m:
            wins = re.findall(r"\[([^\]]*),([^\]]*)\]", m.group(1))
            if not wins:
                self.err(n, f"bad condition '{text}'")
            return [(val(a), val(b)) for a, b in wins], text
        m = re.match(r"(==|>=|<=|>|<)\s*(.*)$", text)
        if not m:
            self.err(n, f"bad condition '{text}'")
        op, v = m.group(1), val(m.group(2))
        eps = 1e-6
        return [{"==": (v - eps, v + eps), ">=": (v - eps, None), "<=": (None, v + eps),
                 ">": (v + eps, None), "<": (None, v - eps)}[op]], text


def fmt_ns(ns):
    if ns >= 1000:
        return f"{ns / 1000:.4g} us"
    return f"{ns:.4g} ns"


class Analysis:
    """A spec loaded and analysed: the assembled program, the graph of abstract states, the resolved
    events. `query` gives the range of times between two events."""

    def __init__(self, path):
        spec = self.spec = Spec(path)
        text = "\n".join(p.read_text() for p in spec.programs)
        self.words, self.labels = asm.assemble(text)
        self.rev = {}
        for k, v in self.labels.items():
            self.rev.setdefault(v, k)
        self.start = self.addr(spec.start) if isinstance(spec.start, str) else spec.start
        self.events = {k: [("at", self.addr(e[1])) if e[0] == "at" else e for e in alts]
                       for k, alts in spec.events.items()}
        st = initial_state(self.start, spec.init["A"], spec.init["X"], spec.init["C"], spec.mem_zero)
        self.g = Graph(self.words, st, spec.thread, spec.nonblocking)
        self.cpt = spec.clocks_per_turn()
        self.ns_per_turn = self.cpt * 1000.0 / spec.clock_mhz

    def addr(self, a):
        m = re.fullmatch(r"(\w+?)([+-]\d+)?", a)
        base, off = (m.group(1), int(m.group(2) or 0)) if m else (a, 0)
        if base in self.labels:
            return self.labels[base] + off
        try:
            return int(base, 0) + off
        except ValueError:
            raise SystemExit(f"{self.spec.path}: unknown label '{a}'")

    def label(self, pc):
        return f"{pc}" + (f" ({self.rev[pc]})" if pc in self.rev else "")

    def describe(self, path):
        pcs = [self.g.pcs[i] for i in path]
        short, last, run = [], None, 0
        for p in pcs:
            if p == last:
                run += 1
                continue
            if run:
                short[-1] += f"x{run + 1}"
                run = 0
            short.append(self.label(p))
            last = p
        if run:
            short[-1] += f"x{run + 1}"
        return " > ".join(short[:40]) + (" ..." if len(short) > 40 else "")

    def query(self, a, b, av=None, want_set=False):
        """Times in turns from event `a` to the next event `b` (not through event `av`).
        Returns None if `b` never follows `a`, else a dict: min, max (int or 'inf'), set (or None),
        never (some executions never reach b), min_path, max_path."""
        g, ev = self.g, self.events
        frm = g.nodes_where(ev[a], False)
        avoid = g.nodes_where(ev[av], False) if av else frozenset()
        mn = g.min_time(frm, g.nodes_where(ev[b], False), avoid) if frm else None
        if mn is None:
            return None
        to_must, to_may = g.nodes_where(ev[b], True), g.nodes_where(ev[b], False)
        mx, mxpath, never = g.max_time(frm, to_must, avoid)
        tset = g.times_set(frm, to_must, to_may, avoid=avoid) if want_set else None
        return {"min": mn[0], "max": "inf" if mx is None else mx, "set": tset, "never": never,
                "min_path": mn[1], "max_path": mxpath}


def run_spec(path, verbose=False, out=print):
    an = Analysis(path)
    spec, g = an.spec, an.g
    cpt, ns_per_turn = an.cpt, an.ns_per_turn
    out(f"{spec.path.name}: {len(an.words)} words, thread {spec.thread} at {ns_per_turn:g} ns per turn "
        f"({cpt} clocks), {g.n} abstract states")
    failures = 0

    def inside(t, wins):
        return any((lo is None or t >= lo) and (hi is None or t <= hi) for lo, hi in wins)

    for kind, items in (("check", spec.checks), ("report", spec.reports)):
        for a, b, cond, n, av in items:
            wins, ctext = spec.condition(cond, n)
            name = f"{a} -> {b}" + (f" (not through {av})" if av else "")
            q = an.query(a, b, av, want_set=(len(wins) > 1 or kind == "report"))
            if q is None:
                out(f"  {'FAIL' if kind == 'check' else 'n/a '}  {name}: the second event never follows the first")
                failures += kind == "check"
                continue
            mn, mx, tset = q["min"], q["max"], q["set"]
            tmin = mn * ns_per_turn
            if mx == "inf":
                tmax = float("inf")
                rng = f"{fmt_ns(tmin)} .. unbounded (depends on the inputs)"
            else:
                tmax = mx * ns_per_turn
                rng = f"{fmt_ns(tmin)}" if mx == mn else f"{fmt_ns(tmin)} .. {fmt_ns(tmax)}"
                if mx == mn:
                    rng += f"  (exact: {mn} turns = {mn * cpt} clocks)"
            if tset is not None and len(tset) > 1:
                vals = sorted(tset)
                shown = ", ".join(fmt_ns(t * ns_per_turn) for t in vals[:8]) + (" ..." if len(vals) > 8 else "")
                rng += f"  possible: {{{shown}}}"
            if len(wins) > 1:
                ok = tset is not None and all(inside(t * ns_per_turn, wins) for t in tset)
            else:
                lo, hi = wins[0]
                ok = (lo is None or tmin >= lo) and (hi is None or tmax <= hi)
            tag = "report" if kind == "report" else ("PASS  " if ok else "FAIL  ")
            out(f"  {tag} {name} {ctext}: {rng}")
            if q["never"] and kind == "check":
                out("         note: some executions never reach the second event (end of the analysed code)")
            if not ok and kind == "check":
                failures += 1
                out(f"         shortest path: {an.describe(q['min_path'])}")
                if mx != "inf":
                    out(f"         longest path:  {an.describe(q['max_path'])}")
            elif verbose:
                out(f"         shortest path: {an.describe(q['min_path'])}")
                if mx != "inf":
                    out(f"         longest path:  {an.describe(q['max_path'])}")
    return failures


def main():
    ap = argparse.ArgumentParser(description="static timing analysis of protocol-emulator firmware")
    ap.add_argument("specs", nargs="+")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    bad = sum(run_spec(s, args.verbose) for s in args.specs)
    print("all timing checks hold" if not bad else f"{bad} timing check(s) FAILED")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
