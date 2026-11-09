# SPDX-License-Identifier: Apache-2.0
"""Example firmware that the chip was not built with: WS2812 LEDs, a PS/2 keyboard receiver and MIDI.

Run with:  cd test && make COCOTB_TEST_MODULES=test_leds_keyboard_midi
Every program is assembled from firmware/examples/*.asm and loaded into the program memory through
the real pin loader (docs/loading.md), exactly as a host would do it after fabrication.
"""
import os
import random
import sys
from pathlib import Path

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, RisingEdge
from cocotb.utils import get_sim_time

from loader import load_program

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))
from load_image import build_image  # noqa: E402

EXAMPLES = ROOT / "firmware" / "examples"
FIRMWARE = ROOT / "firmware"

# FW_OVERRIDE_<NAME>=path lets the mutation checks (see docs/examples-leds-keyboard-midi.md) swap in a
# modified copy of a source file without touching the real one.
def fw(name, base=EXAMPLES):
    return os.environ.get("FW_OVERRIDE_" + name.upper().replace(".", "_"), str(base / name))


CLK_NS = 20          # 50 MHz
SLOT = 4             # clocks per instruction slot


def ns(clocks):
    return clocks * CLK_NS


async def start_program(dut, sources, ui=0, uio=0, rng=None):
    """Reset the chip, load the program(s) over the pins, release LOAD with the given pin values."""
    cocotb.start_soon(Clock(dut.clk, CLK_NS, unit="ns").start())
    dut.ena.value = 1
    dut.ui_in.value = 0
    dut.uio_in.value = 0
    dut.rst_n.value = 0
    await ClockCycles(dut.clk, 10)
    dut.rst_n.value = 1
    await ClockCycles(dut.clk, 2)
    await load_program(dut, build_image(sources), rng)
    dut.ui_in.value = ui          # LOAD low and the pins the program expects, at the same time
    dut.uio_in.value = uio
    await ClockCycles(dut.clk, 6)


def bit_of(dut_signal, n):
    return (int(dut_signal.value) >> n) & 1


# ======================================================================================
# WS2812
# ======================================================================================

# The rainbow ring in firmware/examples/ws2812.asm: nine colours (R, G, B), eight pixels per frame.
RING = [(0xFF, 0x00, 0x00), (0xFF, 0xAA, 0x00), (0xAA, 0xFF, 0x00), (0x00, 0xFF, 0x00),
        (0x00, 0xFF, 0xAA), (0x00, 0xAA, 0xFF), (0x00, 0x00, 0xFF), (0xAA, 0x00, 0xFF),
        (0xFF, 0x00, 0xAA)]
PIXELS = 8

# WS2812 timing windows from the task (nanoseconds)
T0H_MIN, T0H_MAX = 220, 380
T1H_MIN, T1H_MAX = 580, 1000
PERIOD_MIN, PERIOD_MAX = 1050, 1450
# WS2812B datasheet low times: T0L 0.85 +-0.15 us, T1L 0.45 +-0.15 us
T0L_MIN, T0L_MAX = 700, 1000
T1L_MIN, T1L_MAX = 300, 600
RESET_MIN = 50_000
GAP_THRESHOLD = 2_000     # a low longer than this is a latch gap, not a bit


async def record_edges(dut, pin, edges, stray):
    """Append (clock_index, new_level) for every change of uo[pin]; flag any change on the other pins."""
    cycle = 0
    prev = int(dut.uo_out.value)
    while True:
        await RisingEdge(dut.clk)
        cycle += 1
        cur = int(dut.uo_out.value)
        if (cur ^ prev) & ~(1 << pin) & 0xFF:
            stray.append((cycle, prev, cur))
        if bit_of(dut.uo_out, pin) != (prev >> pin) & 1:
            edges.append((cycle, bit_of(dut.uo_out, pin)))
        prev = cur


def decode_ws2812(edges):
    """Turn the edge list into frames. Returns (frames, gaps, stats): frames[i] is a list of bytes,
    gaps[i] the low time in ns between frame i and i+1, stats the measured pulse widths."""
    assert edges and edges[0][1] == 1, "the first edge must be a rising edge"
    rises = [c for c, lvl in edges if lvl == 1]
    falls = [c for c, lvl in edges if lvl == 0]
    n = min(len(rises), len(falls))
    assert all(r < f for r, f in zip(rises[:n], falls[:n])), "rise/fall out of order"
    bits, frames, gaps, stats = [], [], [], {"t0h": [], "t1h": [], "t0l": [], "t1l": [], "period": []}
    cur = []
    for i in range(n):
        high = ns(falls[i] - rises[i])
        if T0H_MIN <= high <= T0H_MAX:
            bit = 0
        elif T1H_MIN <= high <= T1H_MAX:
            bit = 1
        else:
            raise AssertionError(f"pulse {i}: high time {high} ns is neither T0H nor T1H")
        (stats["t1h"] if bit else stats["t0h"]).append(high)
        cur.append(bit)
        if i + 1 < len(rises):
            low = ns(rises[i + 1] - falls[i])
            period = ns(rises[i + 1] - rises[i])
            if low > GAP_THRESHOLD:
                frames.append(cur)
                gaps.append(low)
                cur = []
            else:
                stats["period"].append(period)
                (stats["t1l"] if bit else stats["t0l"]).append(low)
    if len(rises) > len(falls):
        pass            # a frame is in progress at the end of the capture: ignore it
    else:
        frames.append(cur)     # the capture ended right after a falling edge; keep it as partial
        gaps.append(None)
    out = []
    for fr in frames:
        usable = len(fr) - len(fr) % 8
        out.append([int("".join(str(b) for b in fr[k:k + 8]), 2) for k in range(0, usable, 8)]
                   + (["partial"] if len(fr) % 8 else []))
    return out, gaps, stats


def expected_frame(k):
    """Frame k as the GRB byte stream: frame k starts at ring colour (8k mod 9)."""
    out = []
    for i in range(PIXELS):
        r, g, b = RING[(PIXELS * k + i) % len(RING)]
        out += [g, r, b]
    return out


@cocotb.test()
async def test_ws2812_waveform_and_pixels(dut):
    """Measure every pulse of four frames against the WS2812 timing windows, decode the bits and check
    the pixel bytes (GRB, MSB first) and the rotation from frame to frame."""
    await start_program(dut, [fw("ws2812.asm")])
    edges, stray = [], []
    cocotb.start_soon(record_edges(dut, 2, edges, stray))
    await ClockCycles(dut.clk, 4 * 27_500 + 6_000)     # four frames of about 546 us, plus the start of a fifth

    assert not stray, f"uo pins other than uo[2] changed: {stray[:3]}"
    frames, gaps, stats = decode_ws2812(edges)
    full = [f for f in frames if len(f) == PIXELS * 3 and "partial" not in f]
    dut._log.info(f"{len(edges)} edges, {len(frames)} frames, gaps(ns) {gaps}")
    for name in ("t0h", "t1h", "t0l", "t1l", "period"):
        v = stats[name]
        dut._log.info(f"{name}: n={len(v)} min={min(v)} max={max(v)} ns")

    # Pulse widths against the spec windows (decode_ws2812 already classified every high pulse)
    assert all(T0H_MIN <= v <= T0H_MAX for v in stats["t0h"])
    assert all(T1H_MIN <= v <= T1H_MAX for v in stats["t1h"])
    assert all(PERIOD_MIN <= v <= PERIOD_MAX for v in stats["period"]), f"bit period out of range: {sorted(set(stats['period']))}"
    assert all(T0L_MIN <= v <= T0L_MAX for v in stats["t0l"]), f"T0L out of range: {sorted(set(stats['t0l']))}"
    assert all(T1L_MIN <= v <= T1L_MAX for v in stats["t1l"]), f"T1L out of range: {sorted(set(stats['t1l']))}"
    # The barrel makes the waveform exact: the same widths every time, byte boundaries included.
    assert set(stats["t0h"]) == {16 * CLK_NS} and set(stats["t1h"]) == {36 * CLK_NS}, \
        f"high times not constant: {sorted(set(stats['t0h']))} {sorted(set(stats['t1h']))}"
    assert set(stats["period"]) == {64 * CLK_NS}, f"bit period not constant: {sorted(set(stats['period']))}"

    # Latch gap
    real_gaps = [g for g in gaps if g is not None]
    assert len(real_gaps) >= 4, f"expected at least four latch gaps, got {gaps}"
    assert all(g >= RESET_MIN for g in real_gaps), f"reset gap below 50 us: {real_gaps}"
    # (the gap is set by the feeder thread's WAIT and moves by a few slots with the code path it took)
    assert max(real_gaps) - min(real_gaps) < 1_000, f"latch gap not steady: {real_gaps}"
    assert max(real_gaps) < 1_000_000, "latch gap absurdly long"

    # Pixels
    assert len(full) >= 4, f"expected at least four complete frames, got {[len(f) for f in frames]}"
    for k in range(4):
        assert full[k] == expected_frame(k), \
            f"frame {k}: got {[f'{b:02X}' for b in full[k]]}, expected {[f'{b:02X}' for b in expected_frame(k)]}"
    assert full[1] != full[0], "the rainbow must move from frame to frame"


# ======================================================================================
# Shared: a bit-accurate UART monitor (used for the PS/2 scan codes and for MIDI)
# ======================================================================================

def now(dut):
    """Current simulation time in clocks."""
    return int(get_sim_time("ns")) // CLK_NS


class UartMonitor:
    """8N1 receiver on uo[pin]. A frame starts at the falling edge; every later edge in the frame must
    lie on the bit grid (a multiple of `bit_clks` from the start edge, exactly); the bits are sampled
    in the middle. Records (start_clock, byte) and any problem in `errors`."""

    def __init__(self, dut, bit_clks, pin=0):
        self.dut, self.bit_clks, self.pin = dut, bit_clks, pin
        self.frames, self.errors = [], []

    @property
    def bytes(self):
        return [b for _, b in self.frames]

    async def run(self):
        dut, bc = self.dut, self.bit_clks
        prev = 1
        while True:
            await RisingEdge(dut.clk)
            cur = bit_of(dut.uo_out, self.pin)
            if not (prev == 1 and cur == 0):
                prev = cur
                continue
            t0 = now(dut)
            last, samples = 0, {}
            for i in range(1, 10 * bc):
                await RisingEdge(dut.clk)
                v = bit_of(dut.uo_out, self.pin)
                t = now(dut) - t0
                if v != last and t % bc:
                    self.errors.append(f"frame at clock {t0}: edge at +{t} is off the {bc}-clock bit grid")
                last = v
                if t % bc == bc // 2:
                    samples[t // bc] = v
            if samples.get(0) != 0:
                self.errors.append(f"frame at clock {t0}: start bit not low")
            if samples.get(9) != 1:
                self.errors.append(f"frame at clock {t0}: stop bit not high")
            byte = sum(samples.get(1 + k, 0) << k for k in range(8))
            self.frames.append((t0, byte))
            prev = last


# ======================================================================================
# PS/2 keyboard
# ======================================================================================

US = 50   # clocks per microsecond (20 ns clock)


def sint(sig):
    v = sig.value
    return int(v) if v.is_resolvable else 0


class Ps2Bus:
    """The two open-collector PS/2 lines. The keyboard model releases or pulls them (dev_clk, dev_data);
    the chip can pull CLK low with open-drain uio[0] (the inhibit). ui[0] and uio[0] see the wired-AND
    of both, ui[1] sees DATA. Also records how long, and when, the chip held CLK low."""

    def __init__(self, dut):
        self.dut = dut
        self.dev_clk, self.dev_data = 1, 1
        self.net_clk = 1
        self.host_low_runs = []      # (start_clock, length_in_clocks) of every inhibit
        self._run = 0
        self._start = 0

    async def run(self):
        dut = self.dut
        while True:
            await RisingEdge(dut.clk)
            pull = sint(dut.uio_oe) & 1
            if pull:
                if not self._run:
                    self._start = now(dut)
                self._run += 1
            elif self._run:
                self.host_low_runs.append((self._start, self._run))
                self._run = 0
            self.net_clk = self.dev_clk & (0 if pull else 1)
            dut.ui_in.value = self.net_clk | (self.dev_data << 1)
            dut.uio_in.value = self.net_clk          # the same net also enters on uio[0]


def odd_parity_bit(byte):
    return 1 if bin(byte).count("1") % 2 == 0 else 0


class Ps2Keyboard:
    """Keyboard model: sends 11-bit frames. `low`/`high` give the next CLK low/high time in clocks;
    DATA changes at least 5 us after the rising edge and at least 5 us before the next falling edge."""

    IDLE = 50 * US           # bus must have been idle (CLK and DATA high) for 50 us before a frame

    def __init__(self, dut, bus, rng):
        self.dut, self.bus, self.rng = dut, bus, rng
        self.start_fall_clocks = []    # when the start bit's falling edge happened, per frame
        self.stop_fall_clocks = []     # when the stop bit's falling edge happened, per completed frame

    async def wait_idle(self, extra=0, idle=None):
        need = (self.IDLE if idle is None else idle) + extra
        count = 0
        while count < need:
            await RisingEdge(self.dut.clk)
            count = count + 1 if (self.bus.net_clk and self.bus.dev_data) else 0

    async def send(self, byte, low, high, parity_ok=True, stop_ok=True, start_ok=True, extra_idle=0, idle=None):
        """Returns True if the frame was completed, False if the host inhibited the clock part way.
        `idle` overrides the 50 us the bus must have been quiet (a stress case when shorter)."""
        bus, dut = self.bus, self.dut
        await self.wait_idle(extra_idle, idle)
        bits = ([0 if start_ok else 1] + [(byte >> i) & 1 for i in range(8)]
                + [odd_parity_bit(byte) ^ (0 if parity_ok else 1), 1 if stop_ok else 0])
        bus.dev_data = bits[0]
        await ClockCycles(dut.clk, 10 * US)          # data set up well before the first falling edge
        for i, b in enumerate(bits):
            if not bus.net_clk:                       # host is holding CLK low: stay off the bus
                bus.dev_clk, bus.dev_data = 1, 1
                return False
            bus.dev_clk = 0
            lo = low()
            if i == 0:
                self.start_fall_clocks.append(now(dut))
            if i == 10:
                self.stop_fall_clocks.append(now(dut))
            await ClockCycles(dut.clk, lo)
            bus.dev_clk = 1
            hi = high()
            if i == 10:                               # the frame is complete; the host may inhibit from now on
                await ClockCycles(dut.clk, 5 * US)
                bus.dev_data = 1
                return True
            await ClockCycles(dut.clk, 3 * US)
            if not bus.net_clk:                       # the host pulled CLK low right after the pulse
                bus.dev_clk, bus.dev_data = 1, 1
                return False
            # DATA may change from 5 us after the rising edge to 5 us before the next falling edge
            change = self.rng.randint(5 * US, max(5 * US, hi - 5 * US))
            await ClockCycles(dut.clk, change - 3 * US)
            bus.dev_data = bits[i + 1]
            await ClockCycles(dut.clk, hi - change)
        raise AssertionError("unreachable")


def fixed(clocks):
    return lambda: clocks


def jittery(lo=30 * US, hi=50 * US, rng=None):
    return lambda: rng.randint(lo, hi)


async def ps2_setup(dut, rng):
    await start_program(dut, [fw("ps2_rx.asm")], ui=0x03, uio=0x01, rng=rng)
    bus = Ps2Bus(dut)
    cocotb.start_soon(bus.run())
    mon = UartMonitor(dut, 432)
    cocotb.start_soon(mon.run())
    await ClockCycles(dut.clk, 40)
    return bus, mon, Ps2Keyboard(dut, bus, rng)


async def ps2_run(dut, kbd, mon, frames, low, high, gap_extra=lambda: 0, idle=None):
    """Send (byte, kwargs) frames, then wait for the UART to drain. Returns the completion flags."""
    results = []
    for byte, kw in frames:
        results.append(await kbd.send(byte, low, high, extra_idle=gap_extra(), idle=idle, **kw))
    await ClockCycles(dut.clk, 110 * US + 4320)       # a UART frame is 4320 clocks
    return results


def check_uart_latency(kbd, mon, max_clocks=20 * US):
    """Each UART frame must start soon after the stop bit's falling edge (the firmware is event driven)."""
    for (t0, byte), stop in zip(mon.frames, kbd.stop_fall_clocks):
        assert 0 < t0 - stop <= max_clocks, f"UART frame {byte:#04x} started {t0 - stop} clocks after the stop bit"
    kbd.dut._log.info("UART start after the stop bit's falling edge (us): "
                      + ", ".join(f"{(t0 - stop) / US:.2f}" for (t0, _), stop in zip(mon.frames, kbd.stop_fall_clocks)))


@cocotb.test()
async def test_ps2_make_break_10khz(dut):
    """A key press and release at 10 kHz (50 us low, 50 us high): make 1C, break F0 1C, then E0 75."""
    rng = random.Random(1)
    bus, mon, kbd = await ps2_setup(dut, rng)
    codes = [0x1C, 0xF0, 0x1C, 0xE0, 0x75]
    res = await ps2_run(dut, kbd, mon, [(c, {}) for c in codes], fixed(50 * US), fixed(50 * US),
                        gap_extra=lambda: rng.randint(0, 200 * US))
    assert all(res), "keyboard frame aborted"
    assert not mon.errors, mon.errors
    assert mon.bytes == codes, f"UART got {[hex(b) for b in mon.bytes]}"
    assert not bus.host_low_runs, f"the firmware inhibited the keyboard on good frames: {bus.host_low_runs}"
    check_uart_latency(kbd, mon)


@cocotb.test()
async def test_ps2_16k7_back_to_back(dut):
    """The fastest allowed clock (16.7 kHz: 30 us low, 30 us high), bytes whose single bits / parity
    cover every position, and frames squeezed together (20 us of idle bus, far less than a real keyboard
    leaves) so that each next frame starts while the UART is still sending the previous byte: the
    receiver thread must not be held up by the UART thread."""
    rng = random.Random(2)
    bus, mon, kbd = await ps2_setup(dut, rng)
    codes = [0x01, 0x80, 0xAA, 0x55, 0x00, 0xFF, 0x7F, 0x2D]
    res = await ps2_run(dut, kbd, mon, [(c, {}) for c in codes], fixed(30 * US), fixed(30 * US), idle=20 * US)
    assert all(res), "keyboard frame aborted"
    assert not mon.errors, mon.errors
    assert mon.bytes == codes, f"UART got {[hex(b) for b in mon.bytes]}"
    overlaps = [s < t0 + 10 * 432 for (t0, _), s in zip(mon.frames, kbd.start_fall_clocks[1:])]
    assert all(overlaps) and len(overlaps) == len(codes) - 1, \
        f"every frame should start while the UART is busy: {overlaps}"
    assert not bus.host_low_runs, f"the firmware inhibited the keyboard on good frames: {bus.host_low_runs}"
    check_uart_latency(kbd, mon)


@cocotb.test()
async def test_ps2_clock_jitter(dut):
    """Random clock phases (30..50 us each, so 10..16.7 kHz and everything between), random DATA change
    times inside the allowed window, random idle gaps."""
    rng = random.Random(3)
    bus, mon, kbd = await ps2_setup(dut, rng)
    codes = [rng.randrange(256) for _ in range(6)]
    res = await ps2_run(dut, kbd, mon, [(c, {}) for c in codes], jittery(rng=rng), jittery(rng=rng),
                        gap_extra=lambda: rng.randint(0, 300 * US))
    assert all(res), "keyboard frame aborted"
    assert not mon.errors, mon.errors
    assert mon.bytes == codes, f"UART got {[hex(b) for b in mon.bytes]}, sent {[hex(b) for b in codes]}"
    assert not bus.host_low_runs
    check_uart_latency(kbd, mon)


@cocotb.test()
async def test_ps2_errors(dut):
    """Bad parity, a missing stop bit and a start bit that is 1 each give the error marker 0xFE (and not
    the data), the host then holds CLK low for 100 us or more, and good frames afterwards still work."""
    rng = random.Random(4)
    bus, mon, kbd = await ps2_setup(dut, rng)
    lo, hi = fixed(30 * US), fixed(30 * US)
    seq = [
        (0x1C, {}),
        (0x2B, {"parity_ok": False}),
        (0xF0, {}),
        (0x5A, {"stop_ok": False}),
        (0x1C, {}),
        (0xC3, {"start_ok": False}),
        (0xAA, {}),
    ]
    res = await ps2_run(dut, kbd, mon, seq, lo, hi)
    assert not mon.errors, mon.errors
    assert mon.bytes == [0x1C, 0xFE, 0xF0, 0xFE, 0x1C, 0xFE, 0xAA], f"UART got {[hex(b) for b in mon.bytes]}"
    assert res == [True, True, True, True, True, False, True], f"frame completion {res}"
    runs = bus.host_low_runs
    assert len(runs) == 3, f"expected three inhibits, got {runs}"
    for start, length in runs:
        assert 100 * US <= length <= 140 * US, f"inhibit lasted {length / US:.1f} us"
    dut._log.info("inhibit lengths (us): " + ", ".join(f"{length / US:.2f}" for _, length in runs))


# ======================================================================================
# MIDI
# ======================================================================================

MIDI_BIT = 1600       # 31250 baud at 50 MHz: exactly 1600 clocks
MIDI_FRAME = 10 * MIDI_BIT
MIDI_START_PIN = 0x01     # uio_in[0]
MIDI_PLAY_PIN = 0x02      # uio_in[1]


async def midi_setup(dut):
    await start_program(dut, [fw("midi_tx.asm")])
    mon = UartMonitor(dut, MIDI_BIT)
    cocotb.start_soon(mon.run())
    await ClockCycles(dut.clk, 40)
    return mon


async def watch_pin(dut, pin, edges):
    """Append (clock, new_level) for every change of uo[pin], on the same time base as UartMonitor."""
    prev = bit_of(dut.uo_out, pin)
    while True:
        await RisingEdge(dut.clk)
        cur = bit_of(dut.uo_out, pin)
        if cur != prev:
            edges.append((now(dut), cur))
            prev = cur


def parse_midi(stream):
    """Minimal MIDI parser with running status. Returns a list of (kind, channel, data1, data2)."""
    msgs, status, data = [], None, []
    for b in stream:
        if b & 0x80:
            assert b in (0x80, 0x90), f"unexpected status byte {b:#04x}"
            assert not data, f"status byte {b:#04x} inside a message"
            status = b
        else:
            assert status is not None, "data byte with no status (no running status yet)"
            data.append(b)
            if len(data) == 2:
                msgs.append(({0x80: "off", 0x90: "on"}[status & 0xF0], status & 0x0F, data[0], data[1]))
                data = []
    assert not data, "message cut short"
    return msgs


@cocotb.test()
async def test_midi_raw_bytes(dut):
    """A byte on ui[7:0] plus the START strobe goes out as one 31250 baud 8N1 frame (bit edges exactly on
    the 1600-clock grid); START is level triggered, so holding it sends the byte only once."""
    mon = await midi_setup(dut)
    await ClockCycles(dut.clk, 300)
    assert not mon.frames and bit_of(dut.uo_out, 0) == 1, "TX must idle high and quiet"
    assert bit_of(dut.uo_out, 1) == 0, "BUSY must be low when idle"

    sent = []
    for byte, hold in [(0x90, 8), (0x3C, 8), (0x00, 8), (0xFF, 8), (0xA5, 8), (0x81, 2 * MIDI_FRAME)]:
        n0 = len(mon.frames)
        dut.ui_in.value = byte
        dut.uio_in.value = MIDI_START_PIN
        await ClockCycles(dut.clk, hold)
        if hold <= 8:                 # a short strobe: BUSY is high in the middle of the frame
            dut.uio_in.value = 0
            await ClockCycles(dut.clk, 8 * MIDI_BIT)
            assert bit_of(dut.uo_out, 1) == 1, "BUSY must be high in the middle of the byte"
            await ClockCycles(dut.clk, MIDI_FRAME - 8 * MIDI_BIT + 200)
        else:                         # START held for two frame times: still only one byte
            assert len(mon.frames) == n0 + 1, "a held START must send the byte once"
            dut.uio_in.value = 0
            await ClockCycles(dut.clk, 200)
        sent.append(byte)
        assert len(mon.frames) == n0 + 1
        assert bit_of(dut.uo_out, 1) == 0, "BUSY must clear after the stop bit"
        assert bit_of(dut.uo_out, 0) == 1, "TX must return to idle high"
    assert not mon.errors, mon.errors
    assert mon.bytes == sent, f"MIDI receiver got {[hex(b) for b in mon.bytes]}, expected {[hex(b) for b in sent]}"
    # Frames never overlap or repeat, and the held strobe produced exactly one frame (checked by the byte list)
    assert all(b - a >= MIDI_FRAME for (a, _), (b, _) in zip(mon.frames, mon.frames[1:]))


@cocotb.test()
async def test_midi_running_status_sequence(dut):
    """PLAY sends 90 3C 64 40 64 ... 80 3C 40 40 40: two note ons with running status, then two note offs
    with running status. The bytes inside each run follow each other with no gap; PLAY held high plays
    the sequence only once."""
    mon = await midi_setup(dut)
    busy_edges = []
    cocotb.start_soon(watch_pin(dut, 1, busy_edges))
    dut.uio_in.value = MIDI_PLAY_PIN
    await ClockCycles(dut.clk, 10 * MIDI_FRAME + 3 * 4097 * SLOT + 8000)    # PLAY stays high all the time
    dut.uio_in.value = 0
    await ClockCycles(dut.clk, 3000)

    expected = [0x90, 0x3C, 0x64, 0x40, 0x64, 0x80, 0x3C, 0x40, 0x40, 0x40]
    assert not mon.errors, mon.errors
    assert mon.bytes == expected, f"MIDI receiver got {[hex(b) for b in mon.bytes]}"
    assert parse_midi(mon.bytes) == [("on", 0, 0x3C, 0x64), ("on", 0, 0x40, 0x64),
                                     ("off", 0, 0x3C, 0x40), ("off", 0, 0x40, 0x40)]
    starts = [t for t, _ in mon.frames]
    first_run = [b - a for a, b in zip(starts[:5], starts[1:5])]
    second_run = [b - a for a, b in zip(starts[5:], starts[6:])]
    assert first_run == [MIDI_FRAME] * 4, f"bytes of the note-on run not back to back: {first_run}"
    assert second_run == [MIDI_FRAME] * 4, f"bytes of the note-off run not back to back: {second_run}"
    note_gap = starts[5] - starts[4] - MIDI_FRAME          # the note length, in clocks
    assert 3 * 4096 * SLOT <= note_gap <= 3 * 4100 * SLOT, f"note length {note_gap * CLK_NS / 1000:.1f} us"
    # BUSY: one pulse around the whole sequence, rising before the first start bit, falling after the
    # last stop bit has started
    assert [lvl for _, lvl in busy_edges] == [1, 0], f"BUSY edges {busy_edges}"
    assert busy_edges[0][0] <= starts[0] and busy_edges[1][0] >= starts[-1] + 9 * MIDI_BIT, \
        f"BUSY {busy_edges} vs frames {starts[0]}..{starts[-1]}"
    assert bit_of(dut.uo_out, 0) == 1
