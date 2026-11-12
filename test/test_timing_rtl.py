# SPDX-License-Identifier: Apache-2.0
"""The timing analyzer (tools/timing.py) against the RTL: every interval the chip really produces lies in
the range the analyzer proved.

Run with:  cd test && make COCOTB_TEST_MODULES=test_timing_rtl        (RTL only: it looks inside the CPU)

For each timing spec in firmware/timing/ the firmware runs on the real RTL (ROM firmware from reset, the
examples loaded over the pins with their schedule) against hostile or random inputs: random MISO and slave
behaviour, a stretching I2C slave that sometimes NACKs, a 1-Wire device, random UART data. At every clock
the testbench notes which instruction the thread executes (pc, A, X, C from inside the CPU). From the words
and registers it derives which pins that instruction wrote, finds every occurrence of the spec's events, and
for each check of the spec compares what happened with what the analyzer says:

    min <= (clocks between the two events) / (clocks per turn) <= max     (and in the exact set, when finite)

Only pairs inside one run of the analysed code count (the analysis starts at the spec's `start` and ends at
a HALT or the return from it), and, for specs that assume `nonblocking`, pairs in which the thread was held
up by the channel are left out, as the analysis assumes. This closes the chain
    formal proofs (one instruction per turn, isolation) -> RTL == model (random differential test)
    -> analyzer == model (test/test_timing.py) -> analyzer ranges contain the RTL's intervals (here).
"""
import random
import sys
from pathlib import Path

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, RisingEdge, Timer

from loader import load_program, RAM_WORDS

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
import timing  # noqa: E402
from test_onewire_jtag import OneWireSlave, make_rom  # noqa: E402
import usb_ls  # noqa: E402

SPECS = ROOT / "firmware" / "timing"
HALT = 0xF000


# ----------------------------------------------------------------------------------------------
# collecting a trace of the executed instructions of one thread
# ----------------------------------------------------------------------------------------------

async def collect(dut, thread, clocks, trace):
    """Append (time, pc, A, X, C) to `trace` for every instruction thread `thread` executes, `time` being
    the number of the clock edge that executes it. Sampled just after each edge: the execute stage then
    holds what the next edge executes."""
    cpu = dut.user_project.u_cpu
    for k in range(clocks):
        await RisingEdge(dut.clk)
        await Timer(1, unit="ns")
        if int(cpu.e_valid.value) and int(cpu.e_thr.value) == thread \
                and ((int(cpu.wait_q.value) >> (12 * thread)) & 0xFFF) == 0:
            trace.append((k + 1,
                          (int(cpu.pc_q.value) >> (8 * thread)) & 0xFF,
                          (int(cpu.a_q.value) >> (8 * thread)) & 0xFF,
                          (int(cpu.x_q.value) >> (8 * thread)) & 0xFF,
                          (int(cpu.c_q.value) >> thread) & 1))


def hit(alts, pc, eff):
    for alt in alts:
        if alt[0] == "at":
            if pc == alt[1]:
                return True
            continue
        for k, p, v in eff:
            if k == alt[0] and p == alt[1] and (alt[2] is None or v == alt[2]):
                return True
    return False


def verify(an, trace, log):
    """Compare every check / report of the analysis with what the trace shows. Returns pairs compared."""
    spec, cpt = an.spec, an.cpt
    words = an.words + [HALT] * (256 - len(an.words))
    ent = []                                   # (time, pc, effects, word)
    for t, pc, a, x, c in trace:
        w = words[pc]
        st = (pc, a, x, c, 0, (None,) * 4, (None,) * 16)
        eff = timing.successors(st, w, spec.thread, True)[0][2]
        ent.append((t, pc, eff, w))
    n = len(ent)
    blocked = [False] * n                      # SEND / RECV that did not complete: the same pc comes again
    for i in range(n - 1):
        w = ent[i][3]
        if w >> 12 == 0xF and ((w >> 8) & 0xF) in (5, 6) and ent[i + 1][1] == ent[i][1]:
            blocked[i] = True
    aid, active, depth, act = 0, False, 0, []
    for t, pc, eff, w in ent:
        if not active and pc == an.start:
            active, depth, aid = True, 0, aid + 1
        act.append(aid if active else -1)
        if active:
            op, ext = w >> 12, (w >> 8) & 0xF
            if op == 0xE or (op == 0xF and ext == 0xD and w & 1):
                depth += 1
            elif op == 0xF and ext in (1, 7):
                if depth == 0:
                    active = False
                else:
                    depth -= 1
            elif op == 0xF and ext == 0:
                active = False
    total = 0
    for kind, items in (("check", spec.checks), ("report", spec.reports)):
        for a, b, cond, ln, av in items:
            q = an.query(a, b, av, want_set=True)
            if q is None:
                continue
            ea, eb = an.events[a], an.events[b]
            ev_av = an.events[av] if av else None
            seen = lo = hi = 0
            lo, hi = 1 << 60, 0
            for i in range(n):
                if act[i] < 0 or not hit(ea, ent[i][1], ent[i][2]):
                    continue
                j = i + 1
                while j < n and act[j] == act[i]:
                    if spec.nonblocking and blocked[j - 1]:
                        break
                    if ev_av is not None and hit(ev_av, ent[j][1], ent[j][2]):
                        break
                    if hit(eb, ent[j][1], ent[j][2]):
                        dt = ent[j][0] - ent[i][0]
                        assert dt % cpt == 0, f"{spec.path.name} {a}->{b}: {dt} clocks is not whole turns"
                        turns = dt // cpt
                        assert turns >= q["min"], \
                            f"{spec.path.name} {a}->{b}: RTL took {turns} turns, analyzer minimum {q['min']}"
                        assert q["max"] == "inf" or turns <= q["max"], \
                            f"{spec.path.name} {a}->{b}: RTL took {turns} turns, analyzer maximum {q['max']}"
                        assert q["set"] is None or turns in q["set"], \
                            f"{spec.path.name} {a}->{b}: RTL took {turns} turns, not in {sorted(q['set'])[:10]}"
                        seen += 1
                        lo, hi = min(lo, turns), max(hi, turns)
                        break
                    j += 1
            if seen:
                mx = q["max"]
                log.info(f"{spec.path.name}: {a} -> {b}{' avoid ' + av if av else ''}: {seen} intervals, "
                         f"RTL {lo}..{hi} turns, analyzer {q['min']}..{mx}")
            total += seen
    return total


# ----------------------------------------------------------------------------------------------
# running a firmware on the chip
# ----------------------------------------------------------------------------------------------

async def start_clock(dut):
    task = getattr(dut, "_clk_task", None)
    if task is None or task.done():
        dut._clk_task = cocotb.start_soon(Clock(dut.clk, 20, unit="ns").start())


async def reset_rom(dut, uio):
    await start_clock(dut)
    dut.ena.value = 1
    dut.rst_n.value = 0
    dut.ui_in.value = 0
    dut.uio_in.value = uio
    await ClockCycles(dut.clk, 6)
    dut.rst_n.value = 1


async def boot_loaded(dut, an, sched=None, uio=0, on_release=None):
    """Reset, load the program over the pins; `on_release()` runs just before LOAD goes low (the CPU starts
    a few clocks later), so that a trace started there sees the very first instruction."""
    await start_clock(dut)
    dut.ena.value = 1
    dut.rst_n.value = 0
    dut.ui_in.value = 0
    dut.uio_in.value = 0
    await ClockCycles(dut.clk, 10)
    dut.rst_n.value = 1
    await ClockCycles(dut.clk, 2)
    words = an.words + [HALT] * (RAM_WORDS - len(an.words))
    await load_program(dut, words, None, sched)
    if on_release:
        on_release()
    dut.uio_in.value = uio                       # LOAD low: the program starts
    await ClockCycles(dut.clk, 6)


async def wait_clocks(dut, n, hold=None):
    await ClockCycles(dut.clk, n)


async def drive_start(dut, rng, transfers, hold_clks, gap=200, mode=0, miso=False, bits=0xFF):
    """START (uio[0]) high for hold_clks with a random byte on ui, low for gap, `transfers` times; with
    `miso`, uio[1] (MISO) is random and changes every few clocks."""
    for _ in range(transfers):
        dut.ui_in.value = rng.getrandbits(8) & bits
        left, uio_miso = hold_clks, 0
        while left > 0:
            step = min(left, rng.choice([1, 2, 3, 7, 20, 40]))
            if miso:
                uio_miso = rng.getrandbits(1) << 1
            dut.uio_in.value = mode | 1 | uio_miso
            await ClockCycles(dut.clk, step)
            left -= step
        dut.uio_in.value = mode
        await ClockCycles(dut.clk, gap)


async def i2c_bus(dut, rng, mode, start_hold, rounds):
    """A hostile I2C bus: the pads read back the wired-AND of the chip's pull-downs and a slave that
    sometimes stretches SCL and drives SDA low at random (so ACK and NACK paths both occur)."""
    stretch = slave_sda = 0
    for _ in range(rounds):
        for phase_start in (1, 0):
            left = start_hold if phase_start else 300
            while left > 0:
                await RisingEdge(dut.clk)
                left -= 1
                oe = int(dut.uio_oe.value)
                if rng.random() < 0.02:
                    stretch = rng.random() < 0.3
                if rng.random() < 0.05:
                    slave_sda = rng.random() < 0.6
                scl = 0 if ((oe >> 2) & 1 or stretch) else 1
                sda = 0 if ((oe >> 3) & 1 or slave_sda) else 1
                dut.uio_in.value = mode | phase_start | (scl << 2) | (sda << 3)


async def run_scenario(dut, spec_name, clocks, setup, drivers, log_name=None, sched=None, loaded=False, uio0=0):
    an = timing.Analysis(SPECS / spec_name)
    rng = random.Random(hash(spec_name) & 0xFFFF)
    trace, tasks = [], []

    def go():
        tasks.append(cocotb.start_soon(collect(dut, an.spec.thread, clocks, trace)))
        tasks.extend(cocotb.start_soon(d(dut, rng)) for d in drivers)

    if loaded:
        await boot_loaded(dut, an, sched, uio0, on_release=go)
    else:
        await reset_rom(dut, uio0)
        go()
    await ClockCycles(dut.clk, clocks)
    for t in tasks:
        t.cancel()
    assert len(trace) > 50, f"{spec_name}: only {len(trace)} instructions traced"
    pairs = verify(an, trace, dut._log)
    dut._log.info(f"{spec_name}: {len(trace)} instructions traced, {pairs} intervals compared with the analysis")
    assert pairs >= 30, f"{spec_name}: only {pairs} intervals compared"
    return an, trace


# ----------------------------------------------------------------------------------------------
# the tests
# ----------------------------------------------------------------------------------------------

@cocotb.test()
async def test_uart_tx_rom(dut):
    """The ROM UART transmitter: every TX write is 432 clocks after the previous one, for random bytes."""
    async def drv(dut, rng):
        await drive_start(dut, rng, 10, 5000)
    await run_scenario(dut, "uart_tx.timing", 60000, None, [drv])


@cocotb.test()
async def test_spi_master_rom(dut):
    async def drv(dut, rng):
        await drive_start(dut, rng, 8, 1400, mode=0x80, miso=True)
    await run_scenario(dut, "spi_master.timing", 14000, None, [drv], uio0=0x80)


@cocotb.test()
async def test_i2c_master_rom(dut):
    """The ROM I2C master against a stretching, randomly ACKing / NACKing slave: every standard-mode check
    of the spec, on every transaction that happens."""
    async def drv(dut, rng):
        await i2c_bus(dut, rng, 0x40, 70000, 3)
    await run_scenario(dut, "i2c_master.timing", 230000, None, [drv], uio0=0x40)


@cocotb.test()
async def test_ws2812_loaded(dut):
    await run_scenario(dut, "ws2812.timing", 45000, None, [], loaded=True)


@cocotb.test()
async def test_spi_master_fast_loaded(dut):
    """The 5 MHz SPI master on the fast schedule 0,1,0,2: a turn is 2 clocks."""
    async def drv(dut, rng):
        await drive_start(dut, rng, 8, 500, miso=True)
    await run_scenario(dut, "spi_master_fast.timing", 8000, None, [drv], sched=(0, 1, 0, 2), loaded=True)


@cocotb.test()
async def test_midi_tx_loaded(dut):
    async def drv(dut, rng):
        await drive_start(dut, rng, 4, 17000, bits=0x7F)
    await run_scenario(dut, "midi_tx.timing", 80000, None, [drv], loaded=True, uio0=0)


@cocotb.test()
async def test_onewire_loaded(dut):
    """The 1-Wire master against a behavioural 1-Wire device (reset, presence, READ ROM)."""
    an = timing.Analysis(SPECS / "onewire.timing")
    slave = OneWireSlave(dut, make_rom([0x28, 0xFF, 0x64, 0x1E, 0x8F, 0x2B, 0x6C]))
    trace, tasks = [], []
    await boot_loaded(dut, an, None, 0x01, on_release=lambda: tasks.extend(
        [cocotb.start_soon(slave.run()), cocotb.start_soon(collect(dut, 0, 300000, trace))]))
    await ClockCycles(dut.clk, 300000)
    for t in tasks:
        t.cancel()
    assert not slave.errors, slave.errors
    pairs = verify(an, trace, dut._log)
    dut._log.info(f"onewire.timing: {len(trace)} instructions traced, {pairs} intervals compared")
    assert pairs >= 150, pairs


USB_PULLUPS = 0xCF      # uio[7:6] and uio[3:0] pulled up; uio[5:4] are the loader pins and stay low


async def usb_bus(dut, rng, packets=16):
    """A low-speed USB host: random tokens, data packets and handshakes back to back, bit rate off by up to
    +-1.5 %, edge jitter, D+ / D- skew; the uio pads read back the chip's open-drain pull-downs."""
    bursts, t = [], 0
    for i in range(packets):
        kind = ["data", "token", "data", "hs", "token"][i % 5]        # the first one has a wrong CRC16
        if kind == "token":
            p = usb_ls.token(rng.choice([usb_ls.OUT, usb_ls.IN, usb_ls.SETUP]), rng.randrange(128), rng.randrange(16))
        elif kind == "hs":
            p = usb_ls.handshake(rng.choice(usb_ls.HANDSHAKES))
        else:
            p = usb_ls.data_packet(rng.choice(usb_ls.DATA), bytes(rng.choice([0xFF, 0, rng.randrange(256)])
                                                                  for _ in range(rng.randrange(9))))
        if kind != "hs" and (i == 0 or i % 5 == 4):                   # a wrong CRC: the handler's other branch
            b = bytearray(p)
            b[rng.randrange(1, len(b))] ^= 1 << rng.randrange(8)
            p = bytes(b)
        syms = usb_ls.packet_symbols(p)
        bursts.append((t, syms))
        t += int(len(syms) * usb_ls.CLOCKS_PER_BIT * 1.01) + rng.randrange(20, 300)
    bus = usb_ls.Bus([(s + 300, sy) for s, sy in bursts], ppm=rng.choice([-15000, -5000, 0, 8000, 15000]),
                     jitter=1.5, skew=rng.choice([0, 2]), rng=rng)
    for c in range(bus.end_clock() + 400):
        dp, dm = bus.level(c)
        dut.ui_in.value = dp | (dm << 1)
        await RisingEdge(dut.clk)
        pulled = int(dut.uio_oe.value) & ~int(dut.uio_out.value) & 0xFF
        dut.uio_in.value = USB_PULLUPS & ~pulled


@cocotb.test()
async def test_usb_ls_rx_thread0_loaded(dut):
    """The low-speed USB receiver's window schedule (13 turns after an edge, 8 after the last poll without one,
    17 per bit) on the chip, against random traffic."""
    await run_scenario(dut, "usb_ls_rx.timing", 62000, None, [usb_bus], sched=(0, 1, 0, 1), loaded=True,
                       uio0=USB_PULLUPS)


@cocotb.test()
async def test_usb_ls_rx_thread1_loaded(dut):
    """Thread 1 of the same receiver: every message handled within the proven 69 turns, strobes 160 ns."""
    await run_scenario(dut, "usb_ls_rx_thread1.timing", 62000, None, [usb_bus], sched=(0, 1, 0, 1), loaded=True,
                       uio0=USB_PULLUPS)


@cocotb.test()
async def test_a_wrong_cost_model_is_caught(dut):
    """The cross-check can fail: with the cost of WAIT n made 1 + n - 1 the analysis no longer contains
    what the RTL does."""
    real = timing.successors

    def bad(st, w, thread, nonblocking):
        out = real(st, w, thread, nonblocking)
        if w >> 12 == 0x3 and (w & 0xFFF) > 0:
            return [(ns, c - 1, e) for ns, c, e in out]
        return out
    timing.successors = bad
    try:
        failed = False
        try:
            async def drv(dut, rng):
                await drive_start(dut, rng, 3, 5000)
            await run_scenario(dut, "uart_tx.timing", 15000, None, [drv])
        except AssertionError:
            failed = True
        assert failed, "a wrong cost for WAIT was not noticed"
    finally:
        timing.successors = real
