# SPDX-License-Identifier: Apache-2.0
"""The CAN receiver (firmware/examples/can_rx.asm) on the instruction-set model, against the reference model of the
bus (can.py). Pure Python, no simulator:  cd test && python3 -m pytest test_can_model.py

The firmware runs on the schedule 0,1,0,1 with a CAN waveform on RXD (ui[0]) generated clock by clock: bits of
100 clocks (500 kbit/s), with the bit rate off by some parts per million and random edge jitter. The chip's TXD
(open-drain uio[3]) goes back onto the bus through a transceiver model with a loop delay, so the chip sees its own
ACK, as on a real bus. The uio pads read back the chip's pull-downs. The testbench records the report (BYTE and STATUS
strobes) and every interval in which the chip pulls TXD low, and compares that with the ACK slot of the frame.
The same scenarios run on the RTL in test_can_rtl.py.
"""
import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import asm  # noqa: E402
import can  # noqa: E402
from isa_model import Model  # noqa: E402

FIRMWARE = Path(__file__).resolve().parent.parent / "firmware" / "examples" / "can_rx.asm"
SCHEDULE = (0, 1, 0, 1)
LEAD = 1200                      # idle clocks before the first frame: the node wants 7 bit times of idle bus first
GOOD, CRC_ERROR, SIX_DOMINANT, SIX_RECESSIVE = 0, 1, 2, 3
ACK_EARLY, ACK_LATE = 10, 14     # clocks: how far the ACK may start before / after the true start of the slot


def firmware_words(text=None):
    words, _ = asm.assemble(FIRMWARE.read_text() if text is None else text)
    assert len(words) <= 256
    return words


def place(frames, ppm=0, gap=0, other_ack=False):
    """Frames (can.Frame, or a list of levels) one after the other; returns (bursts, [ACK slot start or None])."""
    bursts, t, slots = [], LEAD, []
    scale = 1 + ppm * 1e-6
    for f in frames:
        if isinstance(f, can.Frame):
            w = f.wire(ack=0 if other_ack else 1)
            slots.append(t + f.ack_index * can.BIT_CLOCKS * scale)
        else:
            w = f
            slots.append(None)
        bursts.append((t, w))
        t += int(len(w) * can.BIT_CLOCKS * scale) + 1 + gap
    return bursts, slots


def receive(words, frames, ppm=0, jitter=0.0, rng=None, gap=0, listen=0, other_ack=False, loop=5, tail=1500):
    """Run the firmware; return ([(report bytes, status)], [(clock TXD pulled low, clock released)], slots, model)."""
    bursts, slots = place(frames, ppm, gap, other_ack)
    bus = can.Bus(bursts, ppm=ppm, jitter=jitter, rng=rng)
    m = Model(list(words), SCHEDULE)
    prev, cur, out, acks, low_at = m.od_pull, [], [], [], None
    echo = [0] * (loop + 1)                              # the transceiver puts TXD on the bus `loop` clocks later
    for c in range(bus.end_clock() + tail):
        echo.append((m.od_pull >> 3) & 1)
        rxd = bus.level(c) & (1 - echo[-1 - loop])
        m.clock(rxd | (listen << 7) | ((~m.od_pull & 0xFF) << 8))
        assert m.od_pull & 0xB0 == 0, "the chip pulled a pad it should not use"
        if m.od_pull != prev:
            fell, rose = m.od_pull & ~prev, prev & ~m.od_pull
            if fell & 1:
                cur.append(m.pins_out)
            if fell & 2:
                out.append((bytes(cur), m.pins_out))
                cur = []
            if fell & 8:
                low_at = c
            if rose & 8:
                acks.append((low_at, c))
            prev = m.od_pull
    assert not cur, f"bytes reported without a STATUS: {bytes(cur).hex()}"
    return out, acks, slots, m


def check_good(frames, out, acks, slots, acked=True):
    """Every frame reported exactly, status 0, and one ACK of exactly one bit in each ACK slot (or none)."""
    assert [(b.hex(), s) for b, s in out] == [(f.report().hex(), GOOD) for f in frames]
    if not acked:
        assert acks == []
        return
    assert len(acks) == len(frames), (acks, slots)
    for (lo, hi), slot in zip(acks, slots):
        assert hi - lo == can.BIT_CLOCKS, f"ACK is {hi - lo} clocks long"
        assert -ACK_EARLY <= lo - slot <= ACK_LATE, f"ACK starts {lo - slot:+.1f} clocks from the slot"


# ------------------------------------------------------------------ the reference model
def test_crc15_catalogue_check_value():
    # CRC-15/CAN: check("123456789") = 0x059E
    bits = [b for ch in b"123456789" for b in can.int_bits(ch, 8)]
    assert can.crc15(bits) == 0x059E


def test_crc_residue_is_zero():
    for seed in range(50):
        f = can.random_frame(random.Random(seed))
        assert can.crc15(f.plain) == 0


def test_stuffing():
    assert can.stuff([0] * 5) == [0] * 5 + [1]
    assert can.stuff([0] * 10) == [0] * 5 + [1] + [0] * 5 + [1]
    assert can.stuff([1, 1, 1, 1, 1, 0, 0, 0, 0]) == [1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 1]     # the stuff bit counts
    for seed in range(200):
        rng = random.Random(seed)
        bits = [rng.choice([0, 0, 0, 1]) if seed % 2 else rng.choice([0, 1, 1, 1]) for _ in range(rng.randrange(1, 120))]
        st = can.stuff(bits)
        assert can.destuff(st) == bits
        assert all(len(set(st[i:i + 6])) > 1 for i in range(len(st) - 5))
    with pytest.raises(ValueError):
        can.destuff([0] * 6)


def test_frame_layout():
    f = can.Frame(0x123, b"\xA5")
    assert f.head == [0] + can.int_bits(0x123, 11) + [0, 0, 0] + [0, 0, 0, 1] + can.int_bits(0xA5, 8)
    assert len(f.plain) == 19 + 8 + 15
    w = f.wire()
    assert w[len(f.stuffed):] == [1] * 13 and w[f.ack_index - 1:f.ack_index + 2] == [1, 1, 1]
    assert f.report() == bytes([0x00, 0x91, 0x81, 0xA5, f.crc >> 7])


# ------------------------------------------------------------------ the firmware
def test_known_frames():
    fr = [can.Frame(0x123, b"\x11\x22\x33"), can.Frame(0x7FF), can.Frame(0, bytes(8)), can.Frame(0x555, b"\xff" * 8),
          can.Frame(0x2AA, rtr=1, dlc=4), can.Frame(0x100, bytes(range(8)), dlc=15)]
    out, acks, slots, _ = receive(firmware_words(), fr)
    check_good(fr, out, acks, slots)


@pytest.mark.parametrize("seed", range(12))
def test_random_frames_rate_off_and_jitter(seed):
    """Random frames back to back (3 bits of intermission), bit rate off by up to +-1 %, edges jittered by +-2 clocks."""
    rng = random.Random(seed)
    ppm = [-10000, 10000, -5000, 5000, 0, rng.randrange(-10000, 10001)][seed % 6]
    fr = [can.random_frame(rng) for _ in range(6)]
    out, acks, slots, _ = receive(firmware_words(), fr, ppm=ppm, jitter=2.0, rng=rng)
    check_good(fr, out, acks, slots)


def frames_with_stuff_bit_after_crc(n):
    found, seed = [], 0
    while len(found) < n:
        f = can.random_frame(random.Random(1000 + seed))
        seed += 1
        if f.stuff_after_crc:
            found.append(f)
    return found


def test_stuff_bit_after_the_crc_moves_the_ack_by_one_bit():
    """When the CRC ends with five equal bits a stuff bit follows it, and the ACK slot is one bit later."""
    fr = frames_with_stuff_bit_after_crc(6)
    assert {f.plain[-1] for f in fr} == {0, 1}, "want both levels"
    out, acks, slots, _ = receive(firmware_words(), fr)
    check_good(fr, out, acks, slots)


def test_every_dlc_and_remote_frames():
    rng = random.Random(5)
    fr = [can.Frame(rng.randrange(2048), bytes(rng.randrange(256) for _ in range(min(d, 8))), dlc=d) for d in range(16)]
    fr += [can.Frame(rng.randrange(2048), rtr=1, dlc=d) for d in (0, 1, 8, 15)]
    out, acks, slots, _ = receive(firmware_words(), fr)
    check_good(fr, out, acks, slots)


def test_listen_only_never_drives_txd():
    rng = random.Random(7)
    fr = [can.random_frame(rng) for _ in range(5)]
    out, acks, slots, _ = receive(firmware_words(), fr, listen=1)
    check_good(fr, out, acks, slots, acked=False)


def test_another_node_acknowledges_too():
    rng = random.Random(8)
    fr = [can.random_frame(rng) for _ in range(5)]
    out, acks, slots, _ = receive(firmware_words(), fr, other_ack=True)
    check_good(fr, out, acks, slots)


@pytest.mark.parametrize("loop", [0, 5, 12])
def test_transceiver_loop_delay(loop):
    """The chip's own ACK comes back on RXD 0, 100 or 240 ns later."""
    rng = random.Random(9)
    fr = [can.random_frame(rng) for _ in range(4)]
    out, acks, slots, _ = receive(firmware_words(), fr, loop=loop)
    check_good(fr, out, acks, slots)


def test_crc_error_is_not_acknowledged_and_the_next_frame_is_received():
    rng = random.Random(10)
    bad = [can.Frame(0x321, b"\x01\x02\x03\x04", crc_xor=1 << k) for k in (0, 7, 14)]
    good = can.Frame(0x456, b"\xde\xad\xbe\xef")
    seq = [bad[0], good, bad[1], bad[2], good]
    out, acks, slots, _ = receive(firmware_words(), seq, rng=rng)
    assert [s for _, s in out] == [CRC_ERROR, GOOD, CRC_ERROR, CRC_ERROR, GOOD]
    assert [b for b, s in out if s == GOOD] == [good.report()] * 2
    assert out[0][0] == bad[0].report()                     # the bytes are reported as received all the same
    assert len(acks) == 2
    for (lo, hi), slot in zip(acks, [slots[1], slots[4]]):
        assert hi - lo == can.BIT_CLOCKS and -ACK_EARLY <= lo - slot <= ACK_LATE


def test_single_bit_errors_in_the_data_are_never_acknowledged():
    f = can.Frame(0x2F0, b"\x5a\xc3\x0f")
    frames = []
    for i in range(19, 19 + 24):                           # each data bit flipped in turn, stuffed again
        plain = list(f.plain)
        plain[i] ^= 1
        frames.append(can.stuff(plain) + [1] * 13)
    out, acks, _, _ = receive(firmware_words(), frames)
    assert [s for _, s in out] == [CRC_ERROR] * len(frames) and acks == []


def test_error_flag_and_recovery():
    """Six dominant bits in the middle of a frame (an error flag), then the error delimiter; then a good frame."""
    f = can.Frame(0x111, b"\x01\x02")
    broken = f.stuffed[:25] + [0] * 6 + [1] * 11
    good = can.Frame(0x222, b"\x99")
    out, acks, slots, _ = receive(firmware_words(), [broken, good, broken, good])
    assert [s for _, s in out] == [SIX_DOMINANT, GOOD, SIX_DOMINANT, GOOD]
    assert [b for b, s in out if s == GOOD] == [good.report()] * 2 and len(acks) == 2


def test_frame_that_stops_in_the_middle():
    f = can.Frame(0x111, b"\x01\x02\x03\x04")
    cut = f.stuffed[:40] + [1] * 14
    good = can.Frame(0x333, b"\x42")
    out, acks, slots, _ = receive(firmware_words(), [cut, good])
    assert [s for _, s in out] == [SIX_RECESSIVE, GOOD] and out[1][0] == good.report() and len(acks) == 1


def test_nobody_acknowledges_and_the_transmitter_sends_an_error_flag():
    """Listen-only, so the ACK slot stays recessive; the transmitter then sends an error flag after the ACK delimiter.
    The frame itself is reported as good, and the node is ready for the retransmission."""
    f = can.Frame(0x0F0, b"\xaa\x55")
    with_flag = f.stuffed + [1, 1, 1] + [0] * 6 + [1] * 11
    out, acks, _, _ = receive(firmware_words(), [with_flag, f, with_flag, f], listen=1)
    assert [(b, s) for b, s in out] == [(f.report(), GOOD)] * 4 and acks == []


def extended_frame(ident29, data, rng):
    """A CAN 2.0B frame with a 29-bit identifier (not supported by the example: it must not be acknowledged)."""
    head = ([0] + can.int_bits(ident29 >> 18, 11) + [1, 1] + can.int_bits(ident29 & 0x3FFFF, 18) + [0, 0, 0]
            + can.int_bits(len(data), 4))
    for b in data:
        head += can.int_bits(b, 8)
    plain = head + can.int_bits(can.crc15(head), 15)
    return can.stuff(plain) + [1] * 13


def test_extended_frames_are_not_acknowledged():
    rng = random.Random(11)
    ext = [extended_frame(rng.getrandbits(29), bytes(rng.randrange(256) for _ in range(rng.randrange(9))), rng)
           for _ in range(8)]
    good = can.Frame(0x7E0, b"\x01")
    out, acks, slots, _ = receive(firmware_words(), ext + [good])
    assert all(s != GOOD for _, s in out[:-1]) and out[-1] == (good.report(), GOOD)
    assert len(acks) == 1 and -ACK_EARLY <= acks[0][0] - slots[-1] <= ACK_LATE


def test_bus_busy_at_start_up():
    """The program starts in the middle of a frame: it waits for 7 bits of idle bus and then receives the next one."""
    junk = can.Frame(0x5A5, bytes(8)).stuffed[7:]          # no start bit to synchronise on
    good = can.Frame(0x1AB, b"\x10\x20")
    bursts = [(10, junk + [1] * 13), (10 + (len(junk) + 13) * 100 + 5, good.wire())]
    bus = can.Bus(bursts)
    m = Model(firmware_words(), SCHEDULE)
    prev, cur, out = 0, [], []
    for c in range(bus.end_clock() + 800):
        m.clock((bus.level(c) & (1 - ((m.od_pull >> 3) & 1))) | ((~m.od_pull & 0xFF) << 8))
        fell = m.od_pull & ~prev
        if fell & 1:
            cur.append(m.pins_out)
        if fell & 2:
            out.append((bytes(cur), m.pins_out))
            cur = []
        prev = m.od_pull
    assert out == [(good.report(), GOOD)]


def sweep(ppm, jitter=0.0, n=4, seeds=(0, 1, 2)):
    return sweep_words(firmware_words(), ppm, jitter, n, seeds)


def sweep_words(words, ppm, jitter=0.0, n=4, seeds=(0, 1, 2)):
    worst = [can.Frame(0, bytes(8)), can.Frame(0x7FF, b"\xff" * 8), can.Frame(0x555, b"\x55" * 8),
             can.Frame(0x0F0, b"\x0f\x83\xe0\xf8\x3e")]
    for seed in seeds:
        rng = random.Random(seed)
        fr = worst + [can.random_frame(rng) for _ in range(n)]
        out, acks, slots, _ = receive(words, fr, ppm=ppm, jitter=jitter, rng=rng)
        if [(b, s) for b, s in out] != [(f.report(), GOOD) for f in fr] or len(acks) != len(fr):
            return False
    return True


def test_tolerance_envelope():
    """Received and acknowledged with the bit time from 4 % shorter to 2 % longer than 2 us, and at +-1 % with
    +-4 clocks (80 ns) of edge jitter. Well outside of that it must fail (so the sweep means something). The range is
    not symmetric: an edge that comes early is still seen by the first poll of the window, a late one is missed."""
    for ppm in (-40000, -20000, -15000, 15000, 20000):
        assert sweep(ppm), ppm
    assert sweep(-10000, jitter=4.0) and sweep(10000, jitter=4.0)
    assert not sweep(-70000)
    assert not sweep(30000)


# ------------------------------------------------------------------ do the tests have teeth?
MUTANTS = [
    ("wrong CRC polynomial", "XOR  0x8B             ; polynomial", "XOR  0x8A             ; polynomial"),
    ("stuff bits not dropped", "r_edge: XCMP 2 ", "r_edge: XCMP 1 "),
    ("ACK one bit early", "ack1:   WAIT 55", "ack1:   WAIT 5"),
    ("ACK too long", "ack2:   WAIT 48", "ack2:   WAIT 60"),
    ("no extra bit for a stuff bit after the CRC", "        JNC  ack1\n        WAIT 49", "        JNC  ack1\n        WAIT 0"),
    ("ACK without checking the CRC", "        JNZ  ev_crc\n        LDM  2\n        JNZ  ev_crc", "        NOP\n        LDM  2\n        NOP"),
    ("window re-aligned a turn late", "        LDX  5\n        WAIT 3\n        CALL bit0\n        WAIT 5\n        JMP  d_win",
     "        LDX  5\n        WAIT 3\n        CALL bit0\n        WAIT 9\n        JMP  d_win"),
    ("DLC read from the wrong chunk", "        XOR  4\n        JNZ  t1_cnt", "        XOR  3\n        JNZ  t1_cnt"),
    ("remote frames given data", "        AND  0x40             ; a remote frame has no data", "        AND  0x00"),
    ("listen-only ignored", "        JPIN 7 1 ack2         ; listen only", "        NOP"),
]


@pytest.mark.parametrize("name,old,new", MUTANTS, ids=[m[0] for m in MUTANTS])
def test_mutant_is_caught(name, old, new):
    text = FIRMWARE.read_text()
    assert text.count(old) == 1, name
    words = firmware_words(text.replace(old, new))
    rng = random.Random(3)
    fr = ([can.Frame(0x123, b"\x11\x22\x33"), can.Frame(0x2AA, rtr=1, dlc=4), can.Frame(0, bytes(8))]
          + frames_with_stuff_bit_after_crc(2) + [can.random_frame(rng) for _ in range(4)])
    bad = can.Frame(0x321, b"\x01\x02", crc_xor=0x10)
    caught = False
    try:
        out, acks, slots, _ = receive(words, fr, ppm=8000, jitter=1.0, rng=rng)
        check_good(fr, out, acks, slots)
        out, acks, slots, _ = receive(words, [bad, fr[0]])
        assert [s for _, s in out] == [CRC_ERROR, GOOD] and len(acks) == 1
        out, acks, slots, _ = receive(words, fr[:3], listen=1)
        check_good(fr[:3], out, acks, slots, acked=False)
        assert sweep_words(words, 19000) and sweep_words(words, -38000)     # the edges of the tolerance range
    except AssertionError:
        caught = True
    assert caught, f"mutant '{name}' passes"
