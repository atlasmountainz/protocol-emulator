# SPDX-License-Identifier: Apache-2.0
"""The low-speed USB receiver (firmware/examples/usb_ls_rx.asm) on the instruction-set model, against the
reference model of the bus (usb_ls.py). Pure Python, no simulator:  cd test && python3 -m pytest test_usb_ls_rx_model.py

The firmware runs on the schedule 0,1,0,1 with a USB waveform on D+ / D- (ui[0], ui[1]) generated clock by clock:
bits 666.67 ns long, with the bit rate off by some parts per million, random edge jitter and skew between the
lines. The uio pads read back the chip's open-drain pull-downs, as on the board. The testbench records what
the chip reports: every BYTE strobe (uio[0] low) captures uo[7:0], every STATUS strobe (uio[1] low) ends a packet.
The same scenarios run on the RTL in test_usb_ls_rx_rtl.py.
"""
import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import asm  # noqa: E402
import usb_ls as u  # noqa: E402
from isa_model import Model  # noqa: E402

FIRMWARE = Path(__file__).resolve().parent.parent / "firmware" / "examples" / "usb_ls_rx.asm"
SCHEDULE = (0, 1, 0, 1)          # byte 512 = 0x44
LEAD = 300                       # idle clocks before the first packet (the program needs about 10 to start)

# status byte: bits 7:6 = PID bits 1:0 (type), 5 stuffing violation, 4 stray bits, 2 CRC wrong, 1 PID check wrong, 0 SYNC wrong
TOKEN, DATAP, HAND = 0x40, 0xC0, 0x80


def firmware_words(text=None):
    words, _ = asm.assemble(FIRMWARE.read_text() if text is None else text)
    return words


def receive(words, bursts, ppm=0, jitter=0.0, skew=0, rng=None, tail=400):
    """Run the firmware against `bursts` ([(start clock, symbols)]); return [(bytes, status)] per STATUS strobe,
    plus the model (for its coverage counters)."""
    m = Model(list(words), SCHEDULE)
    bus = u.Bus([(s + LEAD, sy) for s, sy in bursts], ppm=ppm, jitter=jitter, skew=skew, rng=rng)
    prev, cur, packets = m.od_pull, [], []
    for c in range(bus.end_clock() + tail):
        dp, dm = bus.level(c)
        m.clock(dp | (dm << 1) | ((~m.od_pull & 0xFF) << 8))
        if m.od_pull != prev:
            fell = m.od_pull & ~prev
            if fell & 1:
                cur.append(m.pins_out)
            if fell & 2:
                packets.append((bytes(cur), m.pins_out))
                cur = []
            prev = m.od_pull
    assert not cur, f"bytes reported without a STATUS: {bytes(cur).hex()}"
    return packets, m


def schedule_bursts(packets, rng, ppm=0, gap=(60, 400)):
    """Packets one after the other, `gap` idle clocks between them (random in the range)."""
    bursts, t = [], 0
    for p in packets:
        syms = p if isinstance(p, list) else u.packet_symbols(p)
        bursts.append((t, syms))
        t += int(len(syms) * u.CLOCKS_PER_BIT * (1 + ppm * 1e-6)) + rng.randrange(*gap)
    return bursts


def good(packet):
    """What a good packet must be reported as: the bytes after SYNC and the type bits."""
    return bytes(packet), (packet[0] & 3) << 6


def random_packet(rng):
    kind = rng.choice(["token", "data", "ones", "zeros", "handshake", "sof"])
    if kind == "token":
        return u.token(rng.choice([u.OUT, u.IN, u.SETUP]), rng.randrange(128), rng.randrange(16))
    if kind == "sof":
        return u.sof(rng.randrange(2048))
    if kind == "handshake":
        return u.handshake(rng.choice(u.HANDSHAKES))
    n = rng.randrange(0, 9)
    fill = {"data": lambda: rng.choice([rng.randrange(256), 0x00, 0xFF, 0x7F, 0xFE, 0x80]),
            "ones": lambda: 0xFF, "zeros": lambda: 0x00}[kind]
    return u.data_packet(rng.choice(u.DATA), bytes(fill() for _ in range(n)))


# ------------------------------------------------------------------------------------------ the program
def test_fits_the_program_memory():
    assert len(firmware_words()) <= 256


def test_known_packets_are_reported_exactly():
    words = firmware_words()
    get_descriptor = bytes.fromhex("c38006000100004000dd94")
    pk = [u.token(u.SETUP, 0, 0), get_descriptor, u.handshake(u.ACK), u.token(u.IN, 1, 0), u.handshake(u.NAK),
          u.sof(0x123), u.data_packet(u.DATA1, b"")]
    got, _ = receive(words, schedule_bursts(pk, random.Random(1)))
    assert got == [good(p) for p in pk]
    assert got[0] == (bytes.fromhex("2d0010"), TOKEN)
    assert got[1] == (get_descriptor, DATAP)
    assert got[2] == (bytes([0xD2]), HAND)


@pytest.mark.parametrize("ppm", [-15000, 0, 15000])
@pytest.mark.parametrize("jitter,skew", [(0.0, 0), (2.0, 2)])
def test_random_packets_within_the_usb_tolerance(ppm, jitter, skew):
    """+-1.5 % bit rate (the low-speed limit), edge jitter up to +-2 clocks (40 ns), D+ / D- skew of 2 clocks."""
    words = firmware_words()
    rng = random.Random(ppm * 7 + int(jitter * 10) + skew)
    pk = [random_packet(rng) for _ in range(10)]
    got, _ = receive(words, schedule_bursts(pk, rng, ppm), ppm=ppm, jitter=jitter, skew=skew, rng=rng)
    assert got == [good(p) for p in pk]


def test_worst_stuffing_runs_every_byte_needs_a_stuffed_bit():
    words = firmware_words()
    rng = random.Random(3)
    pk = [u.data_packet(u.DATA0, b"\xff" * 8), u.data_packet(u.DATA1, b"\x7f\xff\xfe" * 2)]
    for ppm in (-15000, 15000):
        got, _ = receive(words, schedule_bursts(pk, rng, ppm), ppm=ppm)
        assert got == [good(p) for p in pk]


@pytest.mark.parametrize("gap", [10, 40, 66, 150])
def test_back_to_back_packets(gap):
    """The receiver is listening again a few clocks after the end of a packet."""
    words = firmware_words()
    rng = random.Random(gap)
    pk = [random_packet(rng) for _ in range(5)]
    got, _ = receive(words, schedule_bursts(pk, rng, gap=(gap, gap + 1)))
    assert got == [good(p) for p in pk]


def sweep_failures(words, ppm):
    """Worst-case patterns (a byte of all 1s needs the most stuffing, all 0s the most edges) at 17 start phases
    of one clock's halves: how many of the 68 runs are not received exactly."""
    pk = [u.data_packet(u.DATA0, b"\xff" * 8), u.data_packet(u.DATA1, b"\x00" * 8),
          u.data_packet(u.DATA0, bytes([0x7F, 0xFE, 0x3F, 0xFC, 0x1F, 0xF8, 0x0F, 0xF0])), u.token(u.SETUP, 0x7F, 0xF)]
    bad = 0
    for phase in range(17):
        for p in pk:
            bursts = [(phase / 2.0, u.packet_symbols(p))]
            got, _ = receive(words, bursts, ppm=ppm)
            bad += got != [good(p)]
    return bad


def test_worst_case_patterns_at_every_start_phase_at_the_rate_limits():
    words = firmware_words()
    assert sweep_failures(words, -15000) == 0
    assert sweep_failures(words, 15000) == 0


def test_the_tolerance_has_a_limit():
    """Measured envelope: bits from 2.0 % shorter to 5.0 % longer than 666.67 ns. Outside it, runs of 1s drift
    out of the window of nine polls; the test must be able to fail."""
    words = firmware_words()
    assert sweep_failures(words, -20000) == 0
    assert sweep_failures(words, 50000) == 0
    assert sweep_failures(words, -30000) > 0
    assert sweep_failures(words, 70000) > 0


# ------------------------------------------------------------------------------------------ errors
def flipped(packet, byte, bit):
    b = bytearray(packet)
    b[byte] ^= 1 << bit
    return bytes(b)


def raw(bits, eop=True):
    return u.nrzi(u.stuff(bits)) + (["SE0", "SE0", "J"] if eop else [])


def one(words, burst):
    got, m = receive(words, [(0, burst)])
    assert len(got) == 1
    return got[0]


def test_wrong_crc_is_flagged():
    words = firmware_words()
    pl = u.data_packet(u.DATA1, bytes(range(1, 9)))
    assert one(words, u.packet_symbols(flipped(pl, 3, 4))) == (flipped(pl, 3, 4), DATAP | 4)
    assert one(words, u.packet_symbols(flipped(pl, 9, 0)))[1] == DATAP | 4          # in the CRC itself
    tk = u.token(u.IN, 5, 1)
    assert one(words, u.packet_symbols(flipped(tk, 1, 0))) == (flipped(tk, 1, 0), TOKEN | 4)
    assert one(words, u.packet_symbols(flipped(tk, 2, 7)))[1] == TOKEN | 4


def test_every_single_bit_error_of_a_token_and_a_data_packet_is_flagged():
    """The firmware's CRC5 and CRC16 against the model's: a flipped bit anywhere after SYNC is never a good packet."""
    words = firmware_words()
    for pkt in (u.token(u.SETUP, 3, 2), u.data_packet(u.DATA0, bytes([0xA5, 0x00, 0xFF, 0x3C]))):
        for byte in range(1, len(pkt)):
            for bit in range(0, 8, 3):
                bad = flipped(pkt, byte, bit)
                _, status = one(words, u.packet_symbols(bad))
                assert status & 0x07, (pkt.hex(), byte, bit, hex(status))


def test_wrong_pid_check_is_flagged():
    words = firmware_words()
    tk = u.token(u.SETUP, 0, 0)
    bad = bytes([tk[0] ^ 0x10]) + tk[1:]
    assert one(words, u.packet_symbols(bad)) == (bad, (bad[0] & 3) << 6 | 2)


def test_wrong_sync_is_flagged():
    words = firmware_words()
    bits = u.to_bits(bytes([0x40]) + u.token(u.SETUP, 0, 0))          # 0x40 instead of 0x80
    _, status = one(words, raw(bits))
    assert status & 1


def test_stray_bits_before_the_end_are_flagged():
    words = firmware_words()
    tk = u.token(u.SETUP, 0, 0)
    for extra in ([1], [0, 1, 1], [1, 1, 1, 1, 0], [0] * 6):
        got = one(words, raw(u.to_bits(bytes([0x80]) + tk) + extra))
        assert got == (tk, TOKEN | 0x10), (extra, got)
    # seven stray bits and the edge to SE0 count as one more byte: reported, and flagged as well
    got = one(words, raw(u.to_bits(bytes([0x80]) + tk) + [1, 1, 1, 1, 0, 1, 1]))
    assert got[0][:3] == tk and got[1] & 0x10


def test_a_stuffing_violation_abandons_the_packet_and_the_receiver_recovers():
    words = firmware_words()
    sym = u.nrzi(u.to_bits(bytes([0x80, 0x2D, 0xFF, 0xFF])))           # seven 1s in a row, no stuffed 0
    nxt = u.token(u.OUT, 7, 3)
    got, _ = receive(words, [(0, sym), (1500, u.packet_symbols(nxt))])
    assert got[0][1] & 0x20
    assert got[1] == good(nxt)
    assert len(got) == 2


def test_the_line_stuck_in_k_then_idle_is_an_error_not_a_hang():
    words = firmware_words()
    nxt = u.token(u.IN, 2, 2)
    got, _ = receive(words, [(0, ["K"] * 12 + ["J"] * 4), (500, u.packet_symbols(nxt))])
    assert got[0][1] & 0x20 and got[-1] == good(nxt)


# ------------------------------------------------------------------------------------------ the test can fail
def mutated(old, new):
    text = FIRMWARE.read_text()
    assert text.count(old) == 1, old
    return firmware_words(text.replace(old, new))


def test_mutation_a_late_window_is_caught():
    """Six turns too late after an edge: the window no longer covers the next edge."""
    words = mutated("        LDX  7\n        WAIT 4\n        JMP  k_win", "        LDX  7\n        WAIT 10\n        JMP  k_win")
    rng = random.Random(11)
    bad = 0
    for ppm in (-15000, 0, 15000):
        pk = [random_packet(rng) for _ in range(6)]
        got, _ = receive(words, schedule_bursts(pk, rng, ppm), ppm=ppm)
        bad += got != [good(p) for p in pk]
    assert bad


def test_mutation_a_wrong_crc_constant_is_caught():
    words = mutated("        XOR  0x14\n", "        XOR  0x15\n")
    got, _ = receive(words, [(0, u.packet_symbols(u.token(u.SETUP, 0, 0)))])
    assert got[0][1] & 4


def test_mutation_a_missing_stuffed_bit_check_is_caught():
    """If a stuffed 0 were taken as data, long runs of ones would decode wrongly."""
    words = mutated("j_edge: XCMP 2                ; X == 1: this 0 is a stuffed bit and is dropped\n        JC   j_drop",
                    "j_edge: XCMP 2                ; X == 1: this 0 is a stuffed bit and is dropped\n        JC   j_edge_x\nj_edge_x:")
    pk = [u.data_packet(u.DATA0, b"\xff" * 8)]
    got, _ = receive(words, schedule_bursts(pk, random.Random(5)))
    assert got != [good(p) for p in pk]
