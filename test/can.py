# SPDX-License-Identifier: Apache-2.0
"""Reference model of classic CAN (CAN 2.0A, 11-bit identifiers) for the tests of firmware/examples/can_rx.asm.

A data or remote frame on the wire, first bit first (0 = dominant, 1 = recessive):
    SOF 0 | ID[10..0] | RTR | IDE 0 | r0 0 | DLC[3..0] | data, MSB first | CRC[14..0] |      <- bit stuffing up to here
    CRC delimiter 1 | ACK slot (1 from the transmitter, a receiver makes it 0) | ACK delimiter 1 | EOF 1111111 |
    intermission 111
Stuffing: after five equal bits the transmitter inserts one bit of the other level; inserted bits count for the next
run. The CRC is CRC-15/CAN (polynomial 0x4599, start value 0) over the bits from SOF to the end of the data, before
stuffing. Written from the Bosch CAN 2.0 specification as remembered; the CRC is checked against the catalogue check
value in test_can_model.py.
"""
import random

BIT_CLOCKS = 100                 # 500 kbit/s at 50 MHz


def crc15(bits):
    crc = 0
    for b in bits:
        nxt = b ^ (crc >> 14)
        crc = (crc << 1) & 0x7FFF
        if nxt:
            crc ^= 0x4599
    return crc


def int_bits(v, n):
    return [(v >> i) & 1 for i in range(n - 1, -1, -1)]


def stuff(bits):
    out, run, last = [], 0, None
    for b in bits:
        out.append(b)
        run = run + 1 if b == last else 1
        last = b
        if run == 5:
            out.append(1 - b)
            last, run = 1 - b, 1
    return out


def destuff(bits):
    """Inverse of stuff(); raises ValueError on six equal bits."""
    out, run, last, i = [], 0, None, 0
    while i < len(bits):
        b = bits[i]
        run = run + 1 if b == last else 1
        if run == 6:
            raise ValueError("six equal bits")
        out.append(b)
        last = b
        if run == 5 and i + 1 < len(bits):
            i += 1
            if bits[i] == b:
                raise ValueError("stuff bit missing")
            last, run = bits[i], 1
        i += 1
    return out


class Frame:
    def __init__(self, ident, data=b"", rtr=0, dlc=None, ide=0, crc_xor=0):
        self.ident, self.data, self.rtr, self.ide = ident, bytes(data), rtr, ide
        self.dlc = len(self.data) if dlc is None else dlc
        assert 0 <= ident < 2048 and 0 <= self.dlc < 16
        assert rtr or len(self.data) == min(self.dlc, 8)
        assert not (rtr and self.data)
        self.head = [0] + int_bits(ident, 11) + [rtr, ide, 0] + int_bits(self.dlc, 4)
        for byte in self.data:
            self.head += int_bits(byte, 8)
        self.crc = crc15(self.head) ^ crc_xor                    # crc_xor != 0: a frame with a wrong CRC
        self.plain = self.head + int_bits(self.crc, 15)
        self.stuffed = stuff(self.plain)

    def wire(self, ack=1, ifs=3):
        """All bits of the frame as the transmitter sends them (ack = level it sees in the ACK slot is not its
        business: it sends 1)."""
        return self.stuffed + [1, ack, 1] + [1] * 7 + [1] * ifs

    @property
    def ack_index(self):
        """Index of the ACK slot in wire()."""
        return len(self.stuffed) + 1

    @property
    def stuff_after_crc(self):
        return len(self.stuffed) > 0 and len(stuff(self.plain[:-1])) + 1 < len(self.stuffed)

    def report(self):
        """What can_rx.asm reports for this frame (the BYTE strobes)."""
        ctl = (self.ident & 1) << 7 | self.rtr << 6 | self.ide << 5 | self.dlc
        return bytes([self.ident >> 9, (self.ident >> 1) & 0xFF, ctl]) + self.data + bytes([self.crc >> 7])


def random_frame(rng):
    kind = rng.choice(["data", "data", "data", "zeros", "ones", "rtr", "bigdlc", "edge_id"])
    ident = rng.choice([0, 0x7FF, 0x555, 0x2AA]) if kind == "edge_id" else rng.randrange(2048)
    if kind == "rtr":
        return Frame(ident, rtr=1, dlc=rng.randrange(9))
    if kind == "bigdlc":
        return Frame(ident, bytes(rng.randrange(256) for _ in range(8)), dlc=rng.randrange(9, 16))
    n = rng.randrange(9)
    if kind == "zeros":
        return Frame(ident, bytes(n))
    if kind == "ones":
        return Frame(ident, b"\xff" * n)
    return Frame(ident, bytes(rng.randrange(256) for _ in range(n)))


class Bus:
    """The transmitter's side of the bus: bursts = [(start clock, [levels])], each level BIT_CLOCKS * (1 + ppm/1e6)
    clocks long, every edge moved by a random jitter of up to +-`jitter` clocks. Recessive between bursts."""

    def __init__(self, bursts, ppm=0, jitter=0.0, rng=None):
        self.bit = BIT_CLOCKS * (1 + ppm * 1e-6)
        self.edges = []                                   # (clock, new level), sorted
        rng = rng or random.Random(0)
        level, self.end = 1, 0
        self.starts = []
        for start, levels in sorted(bursts, key=lambda b: b[0]):
            assert start >= self.end, "bursts overlap"
            self.starts.append(start)
            for i, lv in enumerate(levels):
                if lv != level:
                    t = start + i * self.bit + (rng.uniform(-jitter, jitter) if jitter else 0)
                    self.edges.append((t, lv))
                    level = lv
            self.end = start + len(levels) * self.bit
            if level != 1:
                self.edges.append((self.end, 1))
                level = 1
        self.pos, self.cur = 0, 1

    def level(self, clock):
        while self.pos < len(self.edges) and self.edges[self.pos][0] <= clock:
            self.cur = self.edges[self.pos][1]
            self.pos += 1
        return self.cur

    def end_clock(self):
        return int(self.end) + 1
