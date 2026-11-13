# SPDX-License-Identifier: Apache-2.0
"""Tests of the static timing analyzer (tools/timing.py): is the abstract interpreter sound, and are the
numbers it proves the numbers the machine produces?

Pure Python, no simulator:  python3 -m pytest test/test_timing.py -q   (from the repo root)

The oracle is test/isa_model.py, the reference model that the RTL is differentially tested against (every
register, every clock edge, thousands of random programs), so "analyzer agrees with the model" is
"analyzer agrees with the RTL" one step removed. test/test_timing_rtl.py closes that step directly.

  1. every one of the 65536 instruction words: the pins it writes (and the values) are exactly those the
     model writes, for known and for unknown operands;
  2. random states, randomly blurred into "unknown" values: the model's next state, its cost in turns and
     its pin writes are always among those the abstract step allows;
  3. random programs run on the model with random pin inputs: every time between two pin writes that
     happens lies inside the [min, max] the analyzer computes for the pair of events;
  4. the specs in firmware/timing/ hold, and broken firmware makes them fail (the checks can fail);
  5. a few programs whose timing is worked out by hand.
"""
import random
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(ROOT / "tools"))
import asm  # noqa: E402
import timing  # noqa: E402
from isa_model import Model, gen_program  # noqa: E402

NOP = 0xF400


# ----------------------------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------------------------

def thread0_model(words):
    """Model with only thread 0 running (the reset state), default schedule."""
    return Model(list(words))


def set_state(m, pc, a, x, c, sp, stack, mem):
    m.pc[0], m.a[0], m.x[0], m.c[0], m.sp[0] = pc, a, x, c, sp
    m.stack[0] = list(stack)
    m.mem[0] = list(mem)
    m.wait[0] = 0


def written(m_a, m_b, w, pins):
    """Execute word `w` at pc 0 on two models whose output registers start as complements: the bits where
    they agree afterwards are exactly the ones the instruction wrote. Returns (out, od) as sets of
    (pin, value) with value the pin level (open drain: 1 = released)."""
    m_a.pins_out, m_b.pins_out = 0x00, 0xFF
    m_a.od_pull, m_b.od_pull = 0x00, 0xFF
    m_a.execute(0, pins)
    m_b.execute(0, pins)
    out = {(p, (m_a.pins_out >> p) & 1) for p in range(8)
           if ((m_a.pins_out ^ m_b.pins_out) >> p) & 1 == 0}
    od = {(p, 1 - ((m_a.od_pull >> p) & 1)) for p in range(8)
          if ((m_a.od_pull ^ m_b.od_pull) >> p) & 1 == 0}
    return out, od


def effect_sets(effects):
    out = {(p, v) for k, p, v in effects if k == "out"}
    od = {(p, v) for k, p, v in effects if k == "od"}
    return out, od


def blur(rng, values, p):
    """Replace each value by None (unknown) with probability p."""
    return tuple(None if rng.random() < p else v for v in values)


def covers(abs_state, conc):
    """abs_state (components int|None) describes the concrete state `conc`."""
    return all(a is None or a == c for a, c in zip(abs_state, conc))


def flat(state):
    """(pc, a, x, c, sp, stack, mem) -> flat tuple for covers()."""
    pc, a, x, c, sp, stk, mem = state
    return (pc, a, x, c, sp) + tuple(stk) + tuple(mem)


# ----------------------------------------------------------------------------------------------
# 1. pin writes of every instruction word
# ----------------------------------------------------------------------------------------------

@pytest.mark.parametrize("a,c", [(0xA5, 1), (0x5A, 0), (0x00, 1), (0xFF, 0)])
def test_every_word_writes_exactly_the_modelled_pins(a, c):
    """All 65536 words: with known A and C the abstract effects equal the model's writes exactly."""
    for w in range(1 << 16):
        m_a, m_b = thread0_model([w]), thread0_model([w])
        for m in (m_a, m_b):
            set_state(m, 0, a, 0x23, c, 0, (0,) * 4, tuple(range(16)))
        out, od = written(m_a, m_b, w, 0xA55A)
        st = timing.initial_state(0, a, 0x23, c)
        st = st[:6] + (tuple(range(16)),)
        succ = timing.successors(st, w, 0, False)
        eff_out, eff_od = effect_sets(succ[0][2])
        assert eff_out == out and eff_od == od, f"word {w:#06x}: model wrote {out} {od}, analyzer says {eff_out} {eff_od}"


def test_unknown_operands_only_blur_the_values():
    """With A and C unknown the same pins are written; the value is the model's or unknown."""
    for w in range(1 << 16):
        st = timing.initial_state(0, None, 0x23, None)
        try:
            succ = timing.successors(st, w, 0, False)
        except timing.AnalysisError:
            assert (w >> 12) == 0xF and ((w >> 8) & 0xF) == 0xD       # JMPA / CALLA with an unknown A
            continue
        eff = succ[0][2]
        m_a, m_b = thread0_model([w]), thread0_model([w])
        for m in (m_a, m_b):
            set_state(m, 0, 0xC3, 0x23, 1, 0, (0,) * 4, (0,) * 16)
        out, od = written(m_a, m_b, w, 0)
        assert {p for k, p, v in eff if k == "out"} == {p for p, v in out}, f"word {w:#06x}"
        assert {p for k, p, v in eff if k == "od"} == {p for p, v in od}, f"word {w:#06x}"
        for k, p, v in eff:
            if v is not None:
                assert (p, v) in (out if k == "out" else od), f"word {w:#06x}: known value {v} for {k} {p} is wrong"


# ----------------------------------------------------------------------------------------------
# 2. the abstract step is a sound over-approximation of the model's step
# ----------------------------------------------------------------------------------------------

def test_abstract_step_covers_the_model():
    rng = random.Random(0xA11CE)
    seen_ops = set()
    for trial in range(60000):
        w = rng.getrandbits(16) if trial % 2 else (rng.choice([0x6, 0x7, 0x9, 0xB, 0xC, 0xF, 0x4, 0xA, 0x3, 0xE]) << 12 | rng.getrandbits(12))
        pc = rng.randrange(256)
        a, x, c = rng.randrange(256), rng.randrange(256), rng.randrange(2)
        sp = rng.randrange(4)
        stack = [rng.randrange(256) for _ in range(4)]
        mem = [rng.randrange(256) for _ in range(16)]
        pins = rng.getrandbits(16)
        mb_full = rng.randrange(2)

        m = thread0_model([0] * 256)
        m.rom[pc] = w
        set_state(m, pc, a, x, c, sp, stack, mem)
        m.mb_full, m.mb = mb_full, rng.randrange(256)
        pre = (pc, a, x, c, sp, tuple(stack), tuple(mem))
        m.execute(0, pins)
        post = (m.pc[0], m.a[0], m.x[0], m.c[0], m.sp[0]) + tuple(m.stack[0]) + tuple(m.mem[0])
        halted = not m.act[0]
        cost = 1 + m.wait[0]

        p = rng.choice([0.0, 0.15, 0.5, 1.0])
        # blur: a, x, c, stack entries, memory bytes (pc and sp stay known: they are control state)
        ab = (pc, *blur(rng, (a, x, c), p), sp, blur(rng, stack, p), blur(rng, mem, p))
        try:
            succs = timing.successors(ab, w, 0, False)
        except timing.AnalysisError:
            assert (w >> 12) == 0xF and ((w >> 8) & 0xF) == 0xD    # only a computed jump with unknown A refuses
            continue
        ok = False
        for ns, c2, eff in succs:
            if ns is None:
                is_ret = (w >> 12) == 0xF and ((w >> 8) & 0xF) in (1, 7)
                if halted or is_ret:                 # HALT, or a return to a caller the analysis does not know
                    ok = True
                continue
            if halted or c2 != cost:
                continue
            if covers(flat(ns), post):
                ok = True
        assert ok, f"word {w:#06x} pre {pre} blur {ab}: no abstract successor covers {post} cost {cost}"
        seen_ops.add(w >> 12)
    assert seen_ops == set(range(16))


# ----------------------------------------------------------------------------------------------
# 3. random programs: observed times lie inside the analysed ranges
# ----------------------------------------------------------------------------------------------

def sanitize(words):
    """Keep what one thread can do alone: no channel, no SPAWN of another thread."""
    out = []
    for w in words:
        op, ext = w >> 12, (w >> 8) & 0xF
        if op == 0xF and (ext in (5, 6) or ext in (9, 10, 11)):
            w = NOP
        out.append(w)
    return out


def run_with_writes(words, seed, clocks=6000):
    """Run thread 0 alone on the model with random inputs; return [(edge, set((kind, pin)))]: the pins
    each executed instruction wrote (found with the complementary-shadow trick of written())."""
    rng = random.Random(seed)
    a, b = Model(list(words)), Model(list(words))
    hold = rng.choice([1, 2, 5, 20, 100])
    pins = rng.getrandbits(16)
    events = []
    for edge in range(clocks):
        if edge % hold == 0:
            pins = rng.getrandbits(16)
        b.pins_out, b.od_pull = (~a.pins_out) & 0xFF, (~a.od_pull) & 0xFF
        a.clock(pins)
        b.clock(pins)
        ev = set()
        for p in range(8):
            if ((a.pins_out ^ b.pins_out) >> p) & 1 == 0:
                ev.add(("out", p))
            if ((a.od_pull ^ b.od_pull) >> p) & 1 == 0:
                ev.add(("od", p))
        # the shadow starts as the complement and a step without a write keeps it complementary, so the
        # bits in ev are exactly the writes of this edge's instruction
        if ev:
            events.append((edge, ev))
    return events


def test_observed_times_are_inside_the_analysed_ranges():
    analysed = checked = 0
    for seed in range(1, 120):
        words = sanitize(gen_program(seed))
        start = timing.initial_state(0, 0, 0, 0, mem_known=True)
        start = start[:5] + ((0, 0, 0, 0),) + start[6:]          # the model resets the stack to zeros
        try:
            g = timing.Graph(words, start, 0, False, max_nodes=40000)
        except timing.AnalysisError:
            continue
        analysed += 1
        events = run_with_writes(words, seed)
        rng = random.Random(seed)
        kinds = sorted({k for _, ev in events for k in ev})
        if len(kinds) < 1:
            continue
        cache = {}

        def bounds(e, f):
            if (e, f) not in cache:
                frm = g.nodes_where([(e[0], e[1], None)], False)
                to_may = g.nodes_where([(f[0], f[1], None)], False)
                mn = g.min_time(frm, to_may)
                mx = g.max_time(frm, to_may)[0]
                cache[(e, f)] = (mn[0] if mn else None, mx)
            return cache[(e, f)]

        for _ in range(8):
            e, f = rng.choice(kinds), rng.choice(kinds)
            # observed: each occurrence of e and the next later occurrence of f
            for i, (edge_i, ev_i) in enumerate(events):
                if e not in ev_i:
                    continue
                nxt = next((edge_j for edge_j, ev_j in events[i + 1:] if f in ev_j), None)
                if nxt is None:
                    continue
                dt = nxt - edge_i
                assert dt % 4 == 0
                turns = dt // 4
                mn, mx = bounds(e, f)
                assert mn is not None, f"seed {seed}: {f} followed {e} in the model but the analyzer finds no path"
                assert turns >= mn, f"seed {seed}: {e}->{f} took {turns} turns, analyzer minimum {mn}"
                assert mx == "inf" or mx is None or turns <= mx, \
                    f"seed {seed}: {e}->{f} took {turns} turns, analyzer maximum {mx}"
                checked += 1
    assert analysed >= 60, f"only {analysed} random programs were analysable"
    assert checked >= 500, f"only {checked} observed intervals were compared"


# ----------------------------------------------------------------------------------------------
# 4. the specs of the shipped firmware, and broken firmware
# ----------------------------------------------------------------------------------------------

SPECS = sorted((ROOT / "firmware" / "timing").glob("*.timing"))


@pytest.mark.parametrize("spec", SPECS, ids=[s.name for s in SPECS])
def test_shipped_timing_specs_hold(spec):
    lines = []
    failures = timing.run_spec(spec, out=lines.append)
    assert failures == 0, "\n".join(lines)


def write_variant(tmp_path, src, edit, spec_text):
    text = (ROOT / "firmware" / src).read_text()
    assert edit[0] in text, f"edit target {edit[0]!r} not found"
    (tmp_path / "prog.asm").write_text(text.replace(edit[0], edit[1], 1))
    (tmp_path / "x.timing").write_text(spec_text)
    return tmp_path / "x.timing"


def test_uart_one_wrong_wait_is_caught(tmp_path):
    spec = write_variant(tmp_path, "uart_tx.asm", ("        WAIT 104\n        DJNZ bit", "        WAIT 103\n        DJNZ bit"),
                         "program prog.asm\nstart uart_send\ninit A=? X=? C=?\nevent TX = out 0\n"
                         "check TX -> TX == 8.64us\n")
    lines = []
    assert timing.run_spec(spec, out=lines.append) == 1, "\n".join(lines)


def test_uart_data_dependent_path_is_caught(tmp_path):
    """A bit that takes one turn longer when it is a 1 makes the bit time depend on the data."""
    spec = write_variant(tmp_path, "uart_tx.asm", ("        DJNZ bit", "        JNC skip_d\n        NOP\nskip_d: DJNZ bit"),
                         "program prog.asm\nstart uart_send\ninit A=? X=? C=?\nevent TX = out 0\n"
                         "check TX -> TX == 8.64us\n")
    lines = []
    assert timing.run_spec(spec, out=lines.append) == 1, "\n".join(lines)


def test_i2c_missing_hold_delay_is_caught(tmp_path):
    spec_text = (ROOT / "firmware" / "timing" / "i2c_master.timing").read_text().replace("../", "")
    for src in (ROOT / "firmware").glob("*.asm"):          # the spec lists the whole ROM program
        (tmp_path / src.name).write_text(src.read_text())
    master = (tmp_path / "i2c_master.asm").read_text()
    stop_fix = "i2c_stop:\n        WAIT 1                ; SDA must not change within 300 ns of SCL falling (UM10204)\n"
    assert stop_fix in master
    (tmp_path / "i2c_master.asm").write_text(master.replace(stop_fix, "i2c_stop:\n"))
    (tmp_path / "x.timing").write_text(spec_text)
    lines = []
    assert timing.run_spec(tmp_path / "x.timing", out=lines.append) == 1, "\n".join(lines)


# ----------------------------------------------------------------------------------------------
# 5. hand-computed programs
# ----------------------------------------------------------------------------------------------

def times(tmp_path, asm_text, events, frm, to, sched="0,1,2,3", thread=0, avoid=None, init="A=? X=? C=?"):
    (tmp_path / "p.asm").write_text(asm_text)
    lines = ["program p.asm", f"thread {thread}", f"sched {sched}", "start s", f"init {init}"]
    lines += [f"event {k} = {v}" for k, v in events.items()]
    lines.append(f"report {frm} -> {to}" + (f" avoid {avoid}" if avoid else ""))
    (tmp_path / "p.timing").write_text("\n".join(lines))
    spec = timing.Spec(tmp_path / "p.timing")
    words, labels = asm.assemble("\n".join(p.read_text() for p in spec.programs))
    st = timing.initial_state(labels["s"], 0, 0, 0)
    g = timing.Graph(words, st, 0, False)
    ev = {k: [("at", labels[a[1]]) if a[0] == "at" else a for a in alts] for k, alts in spec.events.items()}
    f, t = g.nodes_where(ev[frm], False), g.nodes_where(ev[to], False)
    mn = g.min_time(f, t)
    mx = g.max_time(f, g.nodes_where(ev[to], True))[0]
    return mn[0], mx, g.times_set(f, g.nodes_where(ev[to], True), t)


def test_counted_loop(tmp_path):
    # PIN 0 1 ; LDX 3 ; loop: WAIT 4 ; DJNZ loop ; PIN 0 0
    # PIN(1) LDX(1) then 3 x (WAIT 4 = 5, DJNZ = 1) = 18 turns, then the second PIN: 1 + 1 + 18 = 20 turns
    prog = "s: PIN 0 1\n LDX 3\nl: WAIT 4\n DJNZ l\n PIN 0 0\n HALT\n"
    mn, mx, ts = times(tmp_path, prog, {"UP": "out 0=1", "DOWN": "out 0=0"}, "UP", "DOWN")
    assert (mn, mx, ts) == (20, 20, {20})


def test_branch_on_unknown_gives_both_times(tmp_path):
    # JNC takes 1 turn either way but the long path has an extra instruction
    prog = "s: PIN 0 1\n JPIN 0 1 a\n NOP\n NOP\n JMP b\na: NOP\nb: PIN 0 0\n HALT\n"
    mn, mx, ts = times(tmp_path, prog, {"UP": "out 0=1", "DOWN": "out 0=0"}, "UP", "DOWN")
    assert (mn, mx, ts) == (3, 5, {3, 5})


def test_forget_is_sound_on_random_programs():
    """`forget` (make registers / memory unknown at an address) may only widen a result. For random programs and
    random places to forget at, every range computed with it contains the range computed without it, and the
    observed times of real runs are still inside."""
    compared = 0
    for seed in range(1, 60):
        words = sanitize(gen_program(seed))
        start = timing.initial_state(0, 0, 0, 0, mem_known=True)
        start = start[:5] + ((0, 0, 0, 0),) + start[6:]
        rng = random.Random(seed)
        forget = {rng.randrange(len(words)): set(rng.sample(["A", "X", "C", "MEM"], rng.randrange(1, 5)))
                  for _ in range(rng.randrange(1, 6))}
        try:
            exact = timing.Graph(words, start, 0, False, max_nodes=40000)
            blurred = timing.Graph(words, start, 0, False, max_nodes=40000, forget=forget)
        except timing.AnalysisError:
            continue
        kinds = sorted({(e[0], e[1]) for eff in exact.effects for e in eff})
        for _ in range(6):
            if not kinds:
                break
            e, f = rng.choice(kinds), rng.choice(kinds)
            res = []
            for g in (exact, blurred):
                frm = g.nodes_where([(e[0], e[1], None)], False)
                to = g.nodes_where([(f[0], f[1], None)], False)
                mn = g.min_time(frm, to)
                mx = g.max_time(frm, to)[0]
                res.append((mn[0] if mn else None, float("inf") if mx == "inf" else mx))
            (mn0, mx0), (mn1, mx1) = res
            if mn0 is None or mx0 is None:
                continue                                  # the second event never follows the first
            assert mn1 is not None and mn1 <= mn0, (seed, e, f, res)
            assert mx1 is not None and mx1 >= mx0, (seed, e, f, res)
            compared += 1
    assert compared > 100, compared


def test_forget_directive(tmp_path):
    """Without `forget` the loop count is known (20 turns); forgetting X at the loop makes it anything from one
    pass (8 turns) upwards: the exact answer is inside, and the directive is parsed from the spec."""
    prog = "s: PIN 0 1\n LDX 3\nl: WAIT 4\n DJNZ l\n PIN 0 0\n HALT\n"
    (tmp_path / "p.asm").write_text(prog)
    (tmp_path / "p.timing").write_text("program p.asm\nstart s\nforget X at l\nevent UP = out 0=1\n"
                                       "event DOWN = out 0=0\nreport UP -> DOWN\n")
    an = timing.Analysis(tmp_path / "p.timing")
    q = an.query("UP", "DOWN")
    assert q["min"] == 8 and q["max"] == "inf", q
    (tmp_path / "bad.timing").write_text("program p.asm\nstart s\nforget Q at l\n")
    with pytest.raises(SystemExit):
        timing.Spec(tmp_path / "bad.timing")


def test_spin_loop_is_unbounded(tmp_path):
    prog = "s: PIN 0 1\nw: JPIN 0 0 w\n PIN 0 0\n HALT\n"
    mn, mx, ts = times(tmp_path, prog, {"UP": "out 0=1", "DOWN": "out 0=0"}, "UP", "DOWN")
    assert mn == 2 and mx == "inf" and ts is None


def test_two_slot_thread_turn_is_two_clocks(tmp_path):
    (tmp_path / "p.asm").write_text("s: PIN 0 1\n WAIT 9\n PIN 0 0\n HALT\n")
    (tmp_path / "p.timing").write_text(
        "program p.asm\nthread 0\nsched 0,1,0,2\nstart s\nevent UP = out 0=1\nevent DOWN = out 0=0\n"
        "check UP -> DOWN == 400ns\n")        # PIN + WAIT 9 (10 turns) = 11 turns x 2 clocks x 20 ns = 440 ns
    lines = []
    assert timing.run_spec(tmp_path / "p.timing", out=lines.append) == 1       # 440 ns, not 400
    (tmp_path / "p.timing").write_text(
        "program p.asm\nthread 0\nsched 0,1,0,2\nstart s\nevent UP = out 0=1\nevent DOWN = out 0=0\n"
        "check UP -> DOWN == 440ns\n")
    assert timing.run_spec(tmp_path / "p.timing", out=lines.append) == 0, "\n".join(lines)


def test_illegal_or_missing_thread_is_refused(tmp_path):
    (tmp_path / "p.asm").write_text("s: HALT\n")
    (tmp_path / "p.timing").write_text("program p.asm\nthread 3\nsched 0,1,0,2\nstart s\n")
    with pytest.raises(SystemExit):
        timing.run_spec(tmp_path / "p.timing", out=lambda *_: None)


# ----------------------------------------------------------------------------------------------
# 5. the analysed program is the ROM that is built into the chip
# ----------------------------------------------------------------------------------------------

def _rom_sources():
    """The firmware files tools/build_rom.sh assembles into src/rom.v, in order."""
    import re
    script = (ROOT / "tools" / "build_rom.sh").read_text()
    line = next(ln for ln in script.splitlines() if "tools/asm.py" in ln and "src/rom.v" in ln)
    return [ROOT / f for f in re.findall(r"firmware/[\w./-]+\.asm", line)]


def test_rom_v_is_the_assembled_firmware():
    """src/rom.v is generated; if someone edits the firmware and forgets to run build_rom.sh the timing
    results (which are about the assembled firmware) would no longer be about the ROM in the chip."""
    import re
    srcs = _rom_sources()
    assert len(srcs) >= 6 and srcs[0].name == "main.asm"
    words, _ = asm.assemble("\n".join(p.read_text() for p in srcs))
    rom = {int(a): int(d, 16) for a, d in
           re.findall(r"8'd(\d+): data = 16'h([0-9A-Fa-f]{4});", (ROOT / "src" / "rom.v").read_text())}
    assert len(words) == len(rom) == 162, f"{len(words)} words assembled, {len(rom)} in src/rom.v"
    assert [rom[i] for i in range(len(words))] == words, "src/rom.v is stale: run sh tools/build_rom.sh"


@pytest.mark.parametrize("spec", ["i2c_master", "spi_master", "uart_tx"])
def test_rom_specs_analyse_the_rom_program(spec):
    """The specs that claim to be about the ROM use exactly the files of build_rom.sh, in the same order."""
    s = timing.Spec(ROOT / "firmware" / "timing" / f"{spec}.timing")
    assert [p.resolve() for p in s.programs] == [p.resolve() for p in _rom_sources()]
