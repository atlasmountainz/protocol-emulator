# SPDX-License-Identifier: Apache-2.0
"""Known-answer tests of the low-speed USB reference model (test/usb_ls.py). Pure Python, no simulator.

The model is the yardstick for the USB firmware, so it is checked against values that do not come from it:
the catalogue check values of the two CRCs, and the bytes of the best-known USB transactions
(a SETUP token to address 0 / endpoint 0, and the GET_DESCRIPTOR(device) request that every host sends first).
"""
import random

import pytest

import usb_ls as u


def test_crc16_catalogue_check_value():
    # CRC-16/USB (a.k.a. CRC-16/ARC with init 0xFFFF and final xor 0xFFFF): check("123456789") = 0xB4C8
    assert u.crc16(b"123456789") == 0xB4C8


def test_crc5_catalogue_check_value():
    # CRC-5/USB over the byte-string "123456789" is 0x19 in the catalogue (init 0x1F, xorout 0x1F, reflected)
    bits = u.to_bits(b"123456789")
    reg = u.crc5_register(bits) ^ 0x1F
    assert int(format(reg, "05b")[::-1], 2) == 0x19


def test_setup_token_to_address0_endpoint0():
    assert u.token(u.SETUP, 0, 0).hex() == "2d0010"


def test_get_descriptor_data_packet():
    payload = bytes.fromhex("80060001000040 00".replace(" ", ""))
    assert u.data_packet(u.DATA0, payload).hex() == "c38006000100004000dd94"


def test_other_known_tokens():
    # IN to address 1 endpoint 0 (the usual first poll) and SOF with frame number 0
    assert u.token(u.IN, 1, 0).hex() == "69 01 e8".replace(" ", "")
    assert u.sof(0).hex() == "a5 00 10".replace(" ", "")


def test_handshake_bytes():
    assert u.handshake(u.ACK).hex() == "d2"
    assert u.handshake(u.NAK).hex() == "5a"
    assert u.handshake(u.STALL).hex() == "1e"


def test_pid_complement():
    for pid in range(16):
        b = u.pid_byte(pid)
        assert b & 0xF == pid and b >> 4 == (~pid & 0xF)


def test_residues():
    for n in range(0, 9):
        pl = bytes(random.Random(n).randrange(256) for _ in range(n))
        assert u.crc16_register(pl + u.crc16(pl).to_bytes(2, "little")) == u.CRC16_RESIDUE
    for addr in (0, 1, 0x55, 0x7F):
        for ep in range(16):
            name, ok = u.check_packet(u.token(u.OUT, addr, ep))
            assert (name, ok) == ("OUT", True)


def test_every_single_bit_error_is_detected():
    pkts = [u.token(u.SETUP, 3, 2), u.data_packet(u.DATA1, bytes(range(8)))]
    for p in pkts:
        for byte in range(1, len(p)):          # byte 0 is the PID: a flipped PID bit breaks its own check
            for bit in range(8):
                bad = bytearray(p)
                bad[byte] ^= 1 << bit
                assert u.check_packet(bytes(bad))[1] is False
        for bit in range(8):
            bad = bytearray(p)
            bad[0] ^= 1 << bit
            with pytest.raises(u.PacketError):
                u.check_packet(bytes(bad))


def test_sync_is_kjkjkjkk():
    syms = u.packet_symbols(u.handshake(u.ACK))
    assert "".join(s[0] if s != "SE0" else "0" for s in syms[:8]) == "KJKJKJKK"
    assert syms[-3:] == ["SE0", "SE0", "J"]
    # the whole ACK: SYNC, d2 = 0100 1011 LSB first, EOP
    assert "".join({"J": "J", "K": "K", "SE0": "0"}[s] for s in syms) == "KJKJKJKKJJKJJKKK00J"


def test_stuffing_after_six_ones():
    assert u.stuff([1] * 6) == [1] * 6 + [0]
    assert u.stuff([1] * 7) == [1] * 6 + [0, 1]
    assert u.stuff([1] * 12) == [1] * 6 + [0] + [1] * 6 + [0]
    assert u.stuff([1, 1, 1, 0, 1, 1, 1, 1, 1, 1]) == [1, 1, 1, 0, 1, 1, 1, 1, 1, 1, 0]
    assert u.decode_bits(u.stuff([1] * 20)) == [1] * 20


def test_stuffing_violation_is_an_error():
    with pytest.raises(u.PacketError):
        u.decode_bits([1] * 7)


@pytest.mark.parametrize("seed", range(40))
def test_encode_decode_round_trip(seed):
    rng = random.Random(seed)
    kind = rng.choice(["token", "data", "hs"])
    if kind == "token":
        p = u.token(rng.choice(u.TOKENS), rng.randrange(128), rng.randrange(16))
    elif kind == "data":
        p = u.data_packet(rng.choice(u.DATA), bytes(rng.choice([0, 0xFF, rng.randrange(256)])
                                                    for _ in range(rng.randrange(0, 9))))
    else:
        p = u.handshake(rng.choice(u.HANDSHAKES))
    syms = u.packet_symbols(p)
    assert syms[-3:] == ["SE0", "SE0", "J"]
    assert u.decode_symbols(syms[:-3]) == p
    assert u.check_packet(p)[1] is True


def test_all_ones_payload_needs_stuffing_and_still_round_trips():
    p = u.data_packet(u.DATA0, b"\xff" * 8)
    raw = u.to_bits(bytes([u.SYNC]) + p)
    assert len(u.stuff(raw)) > len(raw)
    assert u.decode_symbols(u.packet_symbols(p)[:-3]) == p


def test_bus_levels_and_idle():
    p = u.handshake(u.ACK)
    bus = u.Bus([u.burst_for(p, 100)])
    assert bus.level(0) == (0, 1)                       # idle is J
    assert bus.level(101) == (1, 0)                     # first symbol of SYNC is K
    n = len(u.packet_symbols(p))
    end = 100 + n * u.CLOCKS_PER_BIT
    assert bus.level(int(end) + 5) == (0, 1)            # idle again after EOP
    # both lines low during the SE0 pair
    t = 100 + (n - 3) * u.CLOCKS_PER_BIT
    assert bus.level(int(t + 10)) == (0, 0)


def test_bus_ppm_and_skew():
    p = u.handshake(u.ACK)
    fast = u.Bus([u.burst_for(p, 0)], ppm=-15000)
    slow = u.Bus([u.burst_for(p, 0)], ppm=+15000)
    assert fast.end_clock() < slow.end_clock()
    sk = u.Bus([u.burst_for(p, 0)], skew=3)
    # D- follows D+ by 3 clocks
    ref = u.Bus([u.burst_for(p, 0)])
    for t in range(0, 60):
        assert sk.level(t)[0] == ref.level(t)[0]
        assert sk.level(t)[1] == ref.level(t - 3)[1]
