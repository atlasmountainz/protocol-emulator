# SPDX-License-Identifier: Apache-2.0
"""Fast SPI master examples (firmware/examples/spi_master_fast.asm and spi_master_turbo.asm,
docs/examples-fast-and-memory.md).

Run with:  cd test && make COCOTB_TEST_MODULES=test_spi_master_fast

The program is loaded through the real pin loader together with a thread schedule (byte 512), then driven
like the ROM SPI master: a byte on ui[7:0], START (uio[0]) high, a slave on MISO (uio[1]). The slave model
watches uo[3:0] every clock, records a timestamp for every edge, and the checks below compare those with
exact expected values per schedule: SCK period, high and low time, MOSI setup and hold against the SCK rising
edge, CS_N setup and hold, BUSY. Set SPI_MASTER_FAST_ASM / SPI_MASTER_TURBO_ASM to run the tests against a
modified copy of the firmware (the mutation tests at the end do that in Python and must all be caught).
"""
import os
import random
import sys
from pathlib import Path

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, RisingEdge

from loader import RAM_WORDS, load_program

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import asm  # noqa: E402

EXAMPLES = Path(__file__).resolve().parent.parent / "firmware" / "examples"
FIRMWARE = Path(os.environ.get("SPI_MASTER_FAST_ASM", EXAMPLES / "spi_master_fast.asm"))
TURBO = Path(os.environ.get("SPI_MASTER_TURBO_ASM", EXAMPLES / "spi_master_turbo.asm"))

START = 0x01            # uio_in bit 0
MISO = 0x02             # uio_in bit 1
CS_N, SCK, MOSI, BUSY = 0x01, 0x02, 0x04, 0x08     # uo_out bits

FAST = (0, 1, 0, 2)     # thread 0 owns slots 0 and 2: one instruction every 2 clocks
OTHER_FAST = (0, 1, 0, 1)   # threads 0 and 1 both run one instruction every 2 clocks
DEFAULT = None          # no schedule byte sent: the default 0 1 2 3, one instruction every 4 clocks

# Exact expected values in clocks (20 ns). With the fast schedule one turn of thread 0 is 2 clocks, with the
# default one 4, and the fast program is the same, so every duration is scaled. The turbo program (two threads in
# lock step) needs 0,1,0,1 or the default schedule.
#   period      rising edge to rising edge, every bit of the frame
#   high, low   SCK high / low time
#   setup       last MOSI change to the rising edge it is sampled by (smallest over the frame)
#   hold        rising edge to the next MOSI change (smallest over the frame)
#   cs_setup    CS_N falling to the first rising edge;  cs_hold  last falling edge to CS_N rising
#   busy_lead   BUSY rising to CS_N falling
EXPECT = {
    ("fast", "fast"):      dict(period=10, high=4, low=6, setup=2, hold=8, cs_setup=6, cs_hold=6, busy_lead=2),
    ("fast", "default"):   dict(period=20, high=8, low=12, setup=4, hold=16, cs_setup=12, cs_hold=12, busy_lead=4),
    ("turbo", "fast"):     dict(period=6, high=2, low=4, setup=1, hold=5, cs_setup=10, cs_hold=6, busy_lead=2),
    ("turbo", "default"):  dict(period=12, high=4, low=8, setup=3, hold=9, cs_setup=20, cs_hold=12, busy_lead=4),
}


def expectation(sched, prog="fast"):
    return EXPECT[(prog, "default" if sched is DEFAULT else "fast")]


def firmware_words(text=None, path=None):
    words, _ = asm.assemble((path or FIRMWARE).read_text() if text is None else text)
    assert len(words) <= RAM_WORDS, f"{len(words)} words do not fit the {RAM_WORDS}-word program memory"
    return words + [0xF000] * (RAM_WORDS - len(words))


def bits(byte):
    return [(byte >> (7 - i)) & 1 for i in range(8)]


def to_byte(bs):
    v = 0
    for b in bs:
        v = (v << 1) | b
    return v


class Slave:
    """SPI slave, mode 0, MSB first, on MISO (uio[1]), plus the START pin (uio[0]).

    The slave presents bit 7 when it sees CS_N fall and the next bit when it sees SCK fall, `delay` clocks
    later than the earliest it could (delay 0: the pad changes 1 clock = 20 ns after SCK fell at the chip's
    pin). It records uo[7:0] and uio_oe for every clock: samples[i] is what the chip drove during clock i,
    so a difference between samples i-1 and i is an edge and the distance between two edges is exact.
    """

    def __init__(self, dut, tx, tx2=0x00, delay=0, mode=0):
        self.dut, self.tx, self.tx2, self.delay = dut, tx, tx2, delay
        self.mode = mode           # extra uio_in bits held high (the ROM master's mode pin uio[7])
        self.start = 0
        self.miso = 0
        self.samples = []
        self.oe_seen = 0
        self.pending = []          # (clock, miso value)
        self.drive()

    def drive(self):
        self.dut.uio_in.value = (MISO if self.miso else 0) | (START if self.start else 0) | self.mode

    def set_start(self, v):
        self.start = v
        self.drive()

    def present(self, idx):
        byte = self.tx if idx < 8 else self.tx2
        return (byte >> (7 - (idx % 8))) & 1

    async def run(self):
        idx = 0
        prev = 0x01
        while True:
            await RisingEdge(self.dut.clk)
            now = len(self.samples)
            o = int(self.dut.uo_out.value)
            self.samples.append(o)
            self.oe_seen |= int(self.dut.uio_oe.value)
            if (prev & CS_N) and not (o & CS_N):                 # CS_N asserted: bit 7
                idx = 0
                self.pending.append((now + self.delay, self.present(0)))
            if not (o & CS_N):
                if (prev & SCK) and not (o & SCK):               # falling edge: next bit
                    idx += 1
                    self.pending.append((now + self.delay, self.present(idx)))
            due = [p for p in self.pending if p[0] <= now]
            if due:
                self.pending = [p for p in self.pending if p[0] > now]
                self.miso = due[-1][1]
                self.drive()
            prev = o


def edges(samples, mask):
    """[(clock index, new level)] for every change of the uo bit `mask`."""
    return [(i, 1 if samples[i] & mask else 0) for i in range(1, len(samples))
            if bool(samples[i] & mask) != bool(samples[i - 1] & mask)]


def analyse(samples):
    """Cut the recorded waveform into frames (CS_N low) and return one dict per frame."""
    cs, sck, mosi, busy = (edges(samples, m) for m in (CS_N, SCK, MOSI, BUSY))
    falls = [i for i, v in cs if v == 0]
    rises = [i for i, v in cs if v == 1]
    assert len(falls) == len(rises), "CS_N is still low"
    frames = []
    for k, (f, r) in enumerate(zip(falls, rises)):
        assert f < r
        s_rise = [i for i, v in sck if v == 1 and f < i < r]
        s_fall = [i for i, v in sck if v == 0 and f < i < r]
        m_chg = [i for i, v in mosi if f - 4 <= i <= r]
        b_rise = [i for i, v in busy if v == 1 and i < f][-1:]
        b_fall = [i for i, v in busy if v == 0 and i > r][:1]
        # MOSI level when the rising edges happen (the slave samples it there)
        sampled = [1 if samples[i] & MOSI else 0 for i in s_rise]
        frames.append(dict(cs_fall=f, cs_rise=r, rises=s_rise, falls=s_fall, mosi_changes=m_chg, busy_rise=b_rise,
                           busy_fall=b_fall, mosi=sampled, prev_cs_rise=rises[k - 1] if k else None))
    return frames


def check_frame(fr, samples, send, recv, exp, label=""):
    """Compare one frame with the protocol and with the exact timing in `exp`. Returns the measurements."""
    tag = f"{label}: " if label else ""
    # ---- protocol: 16 SCK pulses, byte 1 = what was on ui, byte 2 = what the slave sent in byte 1
    assert len(fr["rises"]) == 16 and len(fr["falls"]) == 16, \
        f"{tag}{len(fr['rises'])} rising / {len(fr['falls'])} falling SCK edges, expected 16"
    first, second = to_byte(fr["mosi"][:8]), to_byte(fr["mosi"][8:])
    assert first == send, f"{tag}MOSI byte 1 is {first:#04x}, expected {send:#04x}"
    assert second == recv, f"{tag}MOSI byte 2 is {second:#04x}, the slave sent {recv:#04x}"
    # ---- SCK: strictly alternating, starts with a rising edge, ends low
    seq = sorted([(i, 1) for i in fr["rises"]] + [(i, 0) for i in fr["falls"]])
    assert [v for _, v in seq] == [1, 0] * 16, f"{tag}SCK edges do not alternate"
    # ---- SCK period / high / low: the same for all 16 bits, exactly
    periods = [b - a for a, b in zip(fr["rises"], fr["rises"][1:])]
    highs = [f - r for r, f in zip(fr["rises"], fr["falls"])]
    lows = [r - f for f, r in zip(fr["falls"], fr["rises"][1:])]
    assert periods == [exp["period"]] * 15, f"{tag}SCK periods {sorted(set(periods))} clocks, expected {exp['period']}"
    assert highs == [exp["high"]] * 16, f"{tag}SCK high times {sorted(set(highs))}, expected {exp['high']}"
    assert lows == [exp["low"]] * 15, f"{tag}SCK low times {sorted(set(lows))}, expected {exp['low']}"
    # ---- MOSI: changes only while SCK is low, and never closer to a rising edge than 1 clock (checked exactly)
    setups, holds = [], []
    for i in fr["mosi_changes"]:
        assert not (samples[i] & SCK) and not (samples[i - 1] & SCK), f"{tag}MOSI changed while SCK was high (clock {i})"
    for r in fr["rises"]:
        before = [i for i in fr["mosi_changes"] if i <= r]
        after = [i for i in fr["mosi_changes"] if i > r]
        if before:
            setups.append(r - before[-1])
        if after:
            holds.append(after[0] - r)
    assert all(s >= 1 for s in setups), f"{tag}MOSI setup {min(setups)} clocks: must be stable 1 clock before SCK rises"
    assert all(h >= 1 for h in holds), f"{tag}MOSI hold {min(holds)} clocks: must stay 1 clock after SCK rises"
    # ---- CS_N and BUSY
    cs_setup = fr["rises"][0] - fr["cs_fall"]
    cs_hold = fr["cs_rise"] - fr["falls"][-1]
    assert cs_setup == exp["cs_setup"], f"{tag}CS_N falls {cs_setup} clocks before the first SCK edge, expected {exp['cs_setup']}"
    assert cs_hold == exp["cs_hold"], f"{tag}CS_N rises {cs_hold} clocks after the last SCK fall, expected {exp['cs_hold']}"
    assert fr["busy_rise"] and fr["cs_fall"] - fr["busy_rise"][0] == exp["busy_lead"], \
        f"{tag}BUSY must rise {exp['busy_lead']} clocks before CS_N falls"
    assert fr["busy_fall"] and fr["busy_fall"][0] > fr["cs_rise"], f"{tag}BUSY must stay high until CS_N is released"
    # ---- the other pins: SCK is low whenever CS_N is high, MOSI low after the frame, uo[7:4] never used
    lo, hi = fr["cs_rise"], fr["cs_rise"] + 6
    assert all(not (samples[i] & SCK) for i in range(lo, hi)), f"{tag}SCK high while CS_N high"
    assert not (samples[lo + 6] & MOSI) and (samples[lo + 6] & CS_N), f"{tag}MOSI must be low after the frame"
    return dict(setup=min(setups) if setups else None, hold=min(holds) if holds else None,
                period=periods[0], high=highs[0], low=lows[0], cs_setup=cs_setup, cs_hold=cs_hold)


_CLOCK = None


def start_clock(dut):
    """One 50 MHz clock per test, however many times the chip is booted inside it."""
    global _CLOCK
    if _CLOCK is None or _CLOCK.done():
        _CLOCK = cocotb.start_soon(Clock(dut.clk, 20, unit="ns").start())


async def boot(dut, words, sched, delay=0, tx=0x00):
    """Reset, load the program (and the schedule), release it with START low. Returns the slave."""
    start_clock(dut)
    dut.ena.value = 1
    dut.ui_in.value = 0
    dut.uio_in.value = 0
    dut.rst_n.value = 0
    await ClockCycles(dut.clk, 10)
    dut.rst_n.value = 1
    await ClockCycles(dut.clk, 5)
    await load_program(dut, words, sched=sched)
    slave = Slave(dut, tx, delay=delay)
    dut.uio_in.value = 0                       # LOAD low: the CPU starts at address 0
    slave.task = cocotb.start_soon(slave.run())
    await ClockCycles(dut.clk, 40)
    return slave


async def transfer(dut, slave, send, recv, start_clocks=8, settle=12):
    """One transfer: byte on ui, START for `start_clocks` clocks, slave replies `recv`. Returns `settle` clocks
    after BUSY has gone low."""
    slave.tx = recv
    dut.ui_in.value = send
    slave.set_start(1)
    await ClockCycles(dut.clk, start_clocks)
    slave.set_start(0)
    for _ in range(3000):
        await RisingEdge(dut.clk)
        if slave.samples[-1] & BUSY:
            break
    else:
        raise AssertionError("BUSY never rose after START")
    for _ in range(3000):
        await RisingEdge(dut.clk)
        if not (slave.samples[-1] & BUSY):
            break
    else:
        raise AssertionError("BUSY never fell")
    await ClockCycles(dut.clk, settle)


async def run_transfers(dut, words, sched, pairs, delay=0, label="", prog="fast"):
    """Boot, run the (send, recv) pairs, check every frame against the model of the protocol. Returns the
    slave and the analysed frames."""
    slave = await boot(dut, words, sched, delay=delay)
    try:
        exp = expectation(sched, prog)
        for n, (send, recv) in enumerate(pairs):
            before = len(analyse(slave.samples))
            await transfer(dut, slave, send, recv)
            frames = analyse(slave.samples)
            assert len(frames) == before + 1, f"{label}transfer {n}: {len(frames) - before} frames, expected exactly one"
        frames = analyse(slave.samples)
        setups, holds = [], []
        for n, (fr, (send, recv)) in enumerate(zip(frames, pairs)):
            m = check_frame(fr, slave.samples, send, recv, exp, label=f"{label}transfer {n} ({send:#04x}/{recv:#04x})")
            for key, lst in (("setup", setups), ("hold", holds)):
                if m[key] is not None:
                    lst.append(m[key])
        assert slave.oe_seen == 0, f"{label}the program must not drive any uio pad (uio_oe seen {slave.oe_seen:#04x})"
        assert all(s & 0xF0 == 0 for s in slave.samples), f"{label}uo[7:4] must never be driven high"
        assert min(setups) == exp["setup"], f"{label}smallest MOSI setup {min(setups)}, expected {exp['setup']}"
        assert min(holds) == exp["hold"], f"{label}smallest MOSI hold {min(holds)}, expected {exp['hold']}"
        return slave, frames
    finally:
        slave.task.cancel()


PAIRS = [(0x3A, 0xC5), (0x00, 0xFF), (0xFF, 0x00), (0xA5, 0x5A), (0x5A, 0xA5), (0x01, 0x80), (0x80, 0x01),
         (0xAA, 0x55), (0x55, 0xAA), (0x0F, 0xF0), (0xF0, 0x0F), (0x7E, 0x81)]


@cocotb.test()
async def test_fast_schedule_5mhz(dut):
    """Schedule 0,1,0,2: 16 bits of 10 clocks. SCK period 200 ns (5 MHz), high 80 ns, low 120 ns."""
    slave, frames = await run_transfers(dut, firmware_words(), FAST, PAIRS)
    fr = frames[0]
    period = (fr["rises"][-1] - fr["rises"][0]) / 15
    dut._log.info(f"schedule 0,1,0,2: SCK period {period * 20:.0f} ns = {1e3 / (period * 20):.3f} MHz, "
                  f"high {(fr['falls'][0] - fr['rises'][0]) * 20} ns, low {(fr['rises'][1] - fr['falls'][0]) * 20} ns, "
                  f"CS_N to first edge {(fr['rises'][0] - fr['cs_fall']) * 20} ns, "
                  f"last fall to CS_N {(fr['cs_rise'] - fr['falls'][-1]) * 20} ns, "
                  f"frame {(fr['cs_rise'] - fr['cs_fall']) * 20} ns")
    assert period == 10


@cocotb.test()
async def test_default_schedule_same_program(dut):
    """The same program with the default schedule (no byte 512): it just runs at half the rate, 2.5 MHz."""
    slave, frames = await run_transfers(dut, firmware_words(), DEFAULT, PAIRS)
    fr = frames[0]
    dut._log.info(f"default schedule: SCK period {(fr['rises'][1] - fr['rises'][0]) * 20} ns "
                  f"({1e3 / ((fr['rises'][1] - fr['rises'][0]) * 20):.2f} MHz)")


@cocotb.test()
async def test_schedule_0101(dut):
    """Schedule 0,1,0,1 (threads 2 and 3 never run, thread 1 has nothing to do): the same 5 MHz."""
    await run_transfers(dut, firmware_words(), OTHER_FAST, PAIRS[:6])


@cocotb.test()
async def test_random_bytes(dut):
    """60 random byte pairs at 5 MHz."""
    rng = random.Random(7)
    pairs = [(rng.randrange(256), rng.randrange(256)) for _ in range(60)]
    await run_transfers(dut, firmware_words(), FAST, pairs)


@cocotb.test()
async def test_slave_output_delay_margin(dut):
    """How late may the slave present its next MISO bit? The program samples MISO at the rising edge, 6 clocks
    (120 ns) after the falling edge: a pad that changes up to 5 clocks (100 ns) after SCK fell is fine, one
    that needs 6 is not (delay = extra clocks beyond the earliest 1 clock, so delay 4 is the last one that works)."""
    for delay in range(0, 5):
        await run_transfers(dut, firmware_words(), FAST, PAIRS[:4], delay=delay, label=f"delay {delay}: ")
    failed = False
    try:
        await run_transfers(dut, firmware_words(), FAST, PAIRS[:4], delay=5)
    except AssertionError:
        failed = True
    assert failed, "a slave that needs 6 clocks after SCK fell must not work at 5 MHz"
    # default schedule: twice the room
    for delay in (0, 4, 9):
        await run_transfers(dut, firmware_words(), DEFAULT, PAIRS[:3], delay=delay, label=f"default, delay {delay}: ")


@cocotb.test()
async def test_start_handshake_and_ui_sampling(dut):
    """START is level-triggered like in the ROM master: held high through the transfer it gives exactly one frame,
    BUSY stays high until START is released, the byte on ui is read once at the start (changing it afterwards
    does nothing), and START may be a short pulse."""
    slave = await boot(dut, firmware_words(), FAST)
    exp = expectation(FAST)
    slave.tx = 0x96
    dut.ui_in.value = 0xB4
    slave.set_start(1)
    await ClockCycles(dut.clk, 6)
    dut.ui_in.value = 0x00                     # too late to matter
    await ClockCycles(dut.clk, 400)            # START stays high for the whole frame and long after it
    frames = analyse(slave.samples)
    assert len(frames) == 1, f"{len(frames)} frames while START was held, expected 1"
    assert slave.samples[-1] & BUSY, "BUSY must stay high while START is held"
    assert (slave.samples[-1] & 0x0F) == (CS_N | BUSY), f"idle level while START is held: {slave.samples[-1]:#04x}"
    slave.set_start(0)
    await ClockCycles(dut.clk, 12)
    assert not (slave.samples[-1] & BUSY), "BUSY must fall once START is low"
    assert (slave.samples[-1] & 0x0F) == CS_N
    assert len(analyse(slave.samples)) == 1
    check_frame(analyse(slave.samples)[0], slave.samples, 0xB4, 0x96, exp)
    # a pulse of 3 clocks is enough to start a transfer; the host re-arms START in the very clock it sees BUSY low
    await transfer(dut, slave, 0x2C, 0xD3, start_clocks=3, settle=0)
    await transfer(dut, slave, 0x71, 0x4E, start_clocks=3, settle=0)
    frames = analyse(slave.samples)
    assert len(frames) == 3
    check_frame(frames[1], slave.samples, 0x2C, 0xD3, exp)
    check_frame(frames[2], slave.samples, 0x71, 0x4E, exp)
    gap = frames[2]["cs_fall"] - frames[1]["cs_rise"]
    dut._log.info(f"CS_N high between frames: {gap} clocks ({gap * 20} ns) when the host raises START in the clock "
                  f"after BUSY fell")
    assert gap == 18, f"CS_N high time between frames changed: {gap} clocks"


@cocotb.test()
async def test_idle_state_and_pins(dut):
    """After loading: CS_N high, SCK, MOSI, BUSY low, nothing else driven, no uio pad driven. Idle forever."""
    slave = await boot(dut, firmware_words(), FAST)
    await ClockCycles(dut.clk, 300)
    assert set(slave.samples[-200:]) == {0x01}, f"idle outputs {set(slave.samples[-200:])}"
    assert slave.oe_seen == 0
    assert int(dut.uio_out.value) == 0


@cocotb.test()
async def test_rom_master_for_comparison(dut):
    """The built-in ROM SPI master (uio[7] high before reset), measured with the same slave model: the baseline of
    the speed-up. 12 turns of 4 clocks per bit: SCK period 960 ns (1.04 MHz), high 400 ns, low 560 ns."""
    start_clock(dut)
    dut.ena.value = 1
    dut.ui_in.value = 0
    dut.uio_in.value = 0x80
    dut.rst_n.value = 0
    await ClockCycles(dut.clk, 10)
    dut.rst_n.value = 1
    slave = Slave(dut, 0, mode=0x80)
    slave.task = cocotb.start_soon(slave.run())
    await ClockCycles(dut.clk, 60)
    await transfer(dut, slave, 0x3A, 0xC5)
    (fr,) = analyse(slave.samples)
    assert to_byte(fr["mosi"][:8]) == 0x3A and to_byte(fr["mosi"][8:]) == 0xC5
    periods = {b - a for a, b in zip(fr["rises"], fr["rises"][1:])}
    highs = {f - r for r, f in zip(fr["rises"], fr["falls"])}
    dut._log.info(f"ROM master: SCK period {periods} clocks, high {highs}, CS_N to first edge "
                  f"{fr['rises'][0] - fr['cs_fall']} clocks, last fall to CS_N {fr['cs_rise'] - fr['falls'][-1]} clocks")
    assert periods == {48} and highs == {20}
    assert fr["rises"][0] - fr["cs_fall"] == 40 and fr["cs_rise"] - fr["falls"][-1] == 20
    setups = [r - max(i for i in fr["mosi_changes"] if i <= r) for r in fr["rises"]
              if any(i <= r for i in fr["mosi_changes"])]
    holds = [min(i for i in fr["mosi_changes"] if i > r) - r for r in fr["rises"]
             if any(i > r for i in fr["mosi_changes"])]
    dut._log.info(f"ROM master: MOSI setup {min(setups)} clocks, hold {min(holds)} clocks")
    assert min(setups) == 16 and min(holds) == 32


# ---- the two-thread variant: 8.33 MHz ----------------------------------------------------------------------

@cocotb.test()
async def test_turbo_8mhz(dut):
    """spi_master_turbo.asm, schedule 0,1,0,1: SCK period 120 ns (8.33 MHz), high 40 ns, low 80 ns, MOSI setup 20 ns."""
    slave, frames = await run_transfers(dut, firmware_words(path=TURBO), OTHER_FAST, PAIRS, prog="turbo")
    fr = frames[0]
    period = (fr["rises"][-1] - fr["rises"][0]) / 15
    dut._log.info(f"turbo, schedule 0,1,0,1: SCK period {period * 20:.0f} ns = {1e3 / (period * 20):.3f} MHz, "
                  f"high {(fr['falls'][0] - fr['rises'][0]) * 20} ns, low {(fr['rises'][1] - fr['falls'][0]) * 20} ns, "
                  f"CS_N to first edge {(fr['rises'][0] - fr['cs_fall']) * 20} ns, "
                  f"last fall to CS_N {(fr['cs_rise'] - fr['falls'][-1]) * 20} ns")
    assert period == 6


@cocotb.test()
async def test_turbo_default_schedule(dut):
    """The same two-thread program with the default schedule (the lock step survives): half the rate, 4.17 MHz."""
    slave, frames = await run_transfers(dut, firmware_words(path=TURBO), DEFAULT, PAIRS, prog="turbo")
    fr = frames[0]
    dut._log.info(f"turbo, default schedule: SCK period {(fr['rises'][1] - fr['rises'][0]) * 20} ns")


@cocotb.test()
async def test_turbo_random_bytes_and_slave_delay(dut):
    """60 random byte pairs at 8.33 MHz. The slave has 40 ns after the SCK fall to present MISO: a pad that changes
    1 clock after the fall (delay 0) or 2 clocks after it (delay 1) works, 3 clocks (delay 2) does not."""
    rng = random.Random(8)
    pairs = [(rng.randrange(256), rng.randrange(256)) for _ in range(60)]
    await run_transfers(dut, firmware_words(path=TURBO), OTHER_FAST, pairs, prog="turbo")
    await run_transfers(dut, firmware_words(path=TURBO), OTHER_FAST, PAIRS[:6], delay=1, prog="turbo", label="delay 1: ")
    try:
        await run_transfers(dut, firmware_words(path=TURBO), OTHER_FAST, PAIRS[:6], delay=2, prog="turbo")
    except AssertionError:
        pass
    else:
        raise AssertionError("a slave that needs 3 clocks after SCK fell must not work at 8.33 MHz")


@cocotb.test()
async def test_turbo_needs_threads_in_lock_step(dut):
    """With the schedule 0,1,0,2 thread 1 runs at half the speed of thread 0: the lock step is lost and the frame is
    wrong. (This is why the program says it needs 0,1,0,1 or the default schedule.)"""
    try:
        await run_transfers(dut, firmware_words(path=TURBO), FAST, PAIRS[:4], prog="turbo")
    except AssertionError:
        pass
    else:
        raise AssertionError("the turbo program must not work with schedule 0,1,0,2")


# ---- mutation checks: each change below keeps the program 'nearly right' and must be caught -------------------

def mutate(text, old, new, count=1):
    assert text.count(old) >= 1, f"mutation pattern not found: {old!r}"
    return text.replace(old, new, count) if count else text.replace(old, new)


def bit_block(j):
    return f"bit{j}:\n        PIN  1 1\n        INB  9\n        PIN  1 0\n        SHL\n        OUTC 2\n"


MUTATIONS = {
    # data path
    "MISO sampled 3 instructions late (after the next MOSI bit is out)":
        lambda t: mutate(t, bit_block(3), "bit3:\n        PIN  1 1\n        PIN  1 0\n        SHL\n        OUTC 2\n        INB  9\n"),
    "SHL after OUTC in one bit (MOSI shows the previous bit)":
        lambda t: mutate(t, bit_block(5), "bit5:\n        PIN  1 1\n        INB  9\n        PIN  1 0\n        OUTC 2\n        SHL\n"),
    "bit 12 skips its SHL (echo byte is wrong)":
        lambda t: mutate(t, bit_block(12), "bit12:\n        PIN  1 1\n        INB  9\n        PIN  1 0\n        OUTC 2\n"),
    # timing that keeps the data right
    "next MOSI bit launched 2 clocks after SCK rises (hold ok on paper, but MOSI moves while SCK is high)":
        lambda t: mutate(t, bit_block(9), "bit9:\n        SHL\n        PIN  1 1\n        OUTC 2\n        INB  9\n        PIN  1 0\n"),
    "extra NOP in bit 7 (one SCK period 12 clocks instead of 10)":
        lambda t: mutate(t, bit_block(7), bit_block(7).replace("        SHL\n", "        NOP\n        SHL\n")),
    "CS_N hold removed (WAIT 1 deleted)":
        lambda t: mutate(t, "        WAIT 1", ""),
    "CS_N released together with the last bit (no hold at all, MOSI low skipped)":
        lambda t: mutate(mutate(t, "        WAIT 1                ; CS hold: 3 turns from the last falling edge to CS_N\n", ""),
                         "        PIN  2 0              ; MOSI low\n", ""),
    "BUSY never raised":
        lambda t: mutate(t, "        PIN  3 1              ; busy\n", ""),
    # protocol
    "mode 1 (SCK idles high)":
        lambda t: mutate(t, "        PIN  1 0              ; SCK idle low\n", "        PIN  1 1\n"),
    "CS_N high before the last falling edge is finished (bit 15 loses its fall)":
        lambda t: mutate(t, "bit15:\n        PIN  1 1\n        INB  9\n        PIN  1 0\n", "bit15:\n        PIN  1 1\n        INB  9\n"),
}


def rolled_loop_version(text):
    """A 'neater' version of the program with a DJNZ loop instead of 16 copies: correct data, but a sixth
    instruction per bit (12 clocks, 4.2 MHz) and a different MOSI timing."""
    head = text[:text.index("bit0:")]
    return head + """        LDX  16
bitloop:
        PIN  1 1
        INB  9
        PIN  1 0
        SHL
        OUTC 2
        DJNZ bitloop
        WAIT 1
        PIN  0 1
        PIN  2 0
spi_rel:
        JPIN 8 1 spi_rel
        PIN  3 0
        JMP  spi_idle
"""


def strip_comments(text):
    """The program without comments and trailing blanks, so that mutation patterns see only instructions."""
    return "\n".join(line.split(";")[0].rstrip() for line in text.splitlines() if line.split(";")[0].strip()) + "\n"


def nth_replace(text, old, new, n):
    """Replace the n-th (0-based) occurrence of `old`."""
    parts = text.split(old)
    assert len(parts) > n + 1, f"{old!r} occurs only {len(parts) - 1} times"
    return old.join(parts[:n + 1]) + new + old.join(parts[n + 1:])


TURBO_MUTATIONS = {
    "thread 1 started one instruction too early (one NOP after SPAWN)":
        lambda t: mutate(t, "        SPAWN 1 data\n        NOP\n        NOP\n", "        SPAWN 1 data\n        NOP\n"),
    "thread 1 started one instruction too late (an extra NOP after SPAWN)":
        lambda t: mutate(t, "        SPAWN 1 data\n        NOP\n        NOP\n", "        SPAWN 1 data\n        NOP\n        NOP\n        NOP\n"),
    "thread 0 loses its NOP in bit 5 (the lock step slips by one instruction)":
        lambda t: nth_replace(t, "        NOP\n        PIN  1 1\n", "        PIN  1 1\n", 5),
    "SHL and OUTC swapped in bit 9 of thread 1 (MOSI shows the previous bit)":
        lambda t: nth_replace(t, "        SHL\n        OUTC 2\n        INB  9\n", "        OUTC 2\n        SHL\n        INB  9\n", 9),
    "MISO not read in bit 2 (INB replaced by a NOP)":
        lambda t: nth_replace(t, "        SHL\n        OUTC 2\n        INB  9\n", "        SHL\n        OUTC 2\n        NOP\n", 2),
    "MISO read after the next MOSI bit in bit 6 (INB, SHL, OUTC order): sampled a bit time too late":
        lambda t: nth_replace(t, "        SHL\n        OUTC 2\n        INB  9\n", "        INB  9\n        SHL\n        OUTC 2\n", 6),
}


@cocotb.test()
async def test_mutations_are_caught(dut):
    """Every mutant must fail at least one of the exact checks at 5 MHz (turbo mutants: at 8.33 MHz); the original
    passes the same run."""
    text = FIRMWARE.read_text()
    await run_transfers(dut, firmware_words(text), FAST, PAIRS[:10], label="original: ")
    mutants = dict(MUTATIONS)
    mutants["bit loop rolled up with DJNZ (still correct, but 4.2 MHz)"] = rolled_loop_version
    survivors = []
    for name, f in mutants.items():
        mutated = f(text)                      # a pattern that is not in the program fails the test right here
        try:
            await run_transfers(dut, firmware_words(mutated), FAST, PAIRS[:10], label=f"[{name}] ")
        except (AssertionError, IndexError, ValueError) as e:
            dut._log.info(f"caught: {name}: {str(e).splitlines()[0][:110]}")
        else:
            survivors.append(name)
    ttext = strip_comments(TURBO.read_text())
    await run_transfers(dut, firmware_words(ttext), OTHER_FAST, PAIRS[:10], label="original turbo: ", prog="turbo")
    for name, f in TURBO_MUTATIONS.items():
        mutated = f(ttext)
        try:
            await run_transfers(dut, firmware_words(mutated), OTHER_FAST, PAIRS[:10], label=f"[{name}] ", prog="turbo")
        except (AssertionError, IndexError, ValueError) as e:
            dut._log.info(f"caught: {name}: {str(e).splitlines()[0][:110]}")
        else:
            survivors.append(name)
    assert not survivors, f"mutants that passed all checks: {survivors}"
