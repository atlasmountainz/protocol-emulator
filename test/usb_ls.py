# SPDX-License-Identifier: Apache-2.0
"""Reference model of low-speed USB (1.5 Mbit/s) at the symbol level, for the tests of the USB firmware.

What is in here, all written from the definitions and checked in test_usb_ls_model.py against the published
check values of the two CRCs and the well-known bytes of a GET_DESCRIPTOR request:

* crc5 / crc16: the CRCs of USB tokens and data packets;
* packet builders (token, data, handshake) and the encoding chain  bytes -> bits (LSB first) -> bit stuffing
  (a 0 after six 1s, counted from the SYNC pattern) -> NRZI (0 = toggle, 1 = hold) -> J / K symbols -> EOP;
* Bus: a waveform generator that turns packets into the D+ / D- levels, clock by clock, as a device (or host)
  would drive them, with a bit rate that is off by some parts per million, edge jitter, and skew between D+ and D-;
* decode_symbols: the matching receiver for what the chip drives (used to check the chip's transmitter).

Bus states (low speed): idle / J is D+ = 0, D- = 1; K is D+ = 1, D- = 0; SE0 is both low.
The chip's clock is 50 MHz, so one bit (666.67 ns) is 100/3 clocks.
"""
import bisect
import random

CLOCKS_PER_BIT = 100 / 3

# ------------------------------------------------------------------------------------------------ PIDs
OUT, IN, SOF, SETUP = 0x1, 0x9, 0x5, 0xD
DATA0, DATA1 = 0x3, 0xB
ACK, NAK, STALL = 0x2, 0xA, 0xE
PID_NAMES = {OUT: "OUT", IN: "IN", SOF: "SOF", SETUP: "SETUP", DATA0: "DATA0", DATA1: "DATA1",
             ACK: "ACK", NAK: "NAK", STALL: "STALL"}
TOKENS = (OUT, IN, SOF, SETUP)
DATA = (DATA0, DATA1)
HANDSHAKES = (ACK, NAK, STALL)


def pid_byte(pid):
    """The PID and its complement in the upper nibble."""
    return (pid & 0xF) | ((~pid & 0xF) << 4)


# ------------------------------------------------------------------------------------------------ CRCs
def crc16_register(data, crc=0xFFFF):
    """CRC-16/USB without the final complement (the register after the bytes, reflected form, poly 0x8005)."""
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ (0xA001 if crc & 1 else 0)
    return crc


def crc16(data):
    """CRC-16/USB: what follows the data of a DATA packet, low byte first (catalogue check value 0xB4C8)."""
    return crc16_register(data) ^ 0xFFFF


CRC16_RESIDUE = 0xB001      # the register after the data and its CRC (low byte first)


def crc5_register(bits, crc=0x1F):
    """The CRC-5 shift register after `bits` (LSB of each field first), poly x^5 + x^2 + 1."""
    for b in bits:
        fb = (b ^ (crc >> 4)) & 1
        crc = (crc << 1) & 0x1F
        if fb:
            crc ^= 0x05
    return crc


CRC5_RESIDUE = 0x0C         # after the 11 bits and the 5 CRC bits (sent MSB of the register first)


def field_bits(value, n):
    return [(value >> i) & 1 for i in range(n)]


def crc5(addr, ep):
    """The 5-bit CRC of a token's address / endpoint, as the value of bits 11..15 of the token's 16-bit field."""
    reg = crc5_register(field_bits(addr | (ep << 7), 11)) ^ 0x1F
    # the register is sent MSB first, the field is sent LSB first: the field holds it bit-reversed
    return int(format(reg, "05b")[::-1], 2)


# ------------------------------------------------------------------------------------------------ packets
def token(pid, addr, ep):
    field = (addr & 0x7F) | ((ep & 0xF) << 7)
    field |= crc5(addr & 0x7F, ep & 0xF) << 11
    return bytes([pid_byte(pid), field & 0xFF, field >> 8])


def sof(frame):
    field = frame & 0x7FF
    field |= crc5(field & 0x7F, field >> 7) << 11
    return bytes([pid_byte(SOF), field & 0xFF, field >> 8])


def data_packet(pid, payload):
    c = crc16(payload)
    return bytes([pid_byte(pid)]) + bytes(payload) + bytes([c & 0xFF, c >> 8])


def handshake(pid):
    return bytes([pid_byte(pid)])


# ------------------------------------------------------------------------------------------------ encoding
def to_bits(data):
    return [(b >> i) & 1 for b in data for i in range(8)]


def stuff(bits):
    """Insert a 0 after every six consecutive 1s."""
    out, ones = [], 0
    for b in bits:
        out.append(b)
        ones = ones + 1 if b else 0
        if ones == 6:
            out.append(0)
            ones = 0
    return out


def nrzi(bits, level="J"):
    out = []
    for b in bits:
        if b == 0:
            level = "K" if level == "J" else "J"
        out.append(level)
    return out


SYNC = 0x80


def packet_symbols(packet, with_eop=True):
    """All symbols of a packet on the wire: SYNC, the stuffed and NRZI encoded bytes, then EOP (SE0 SE0 J)."""
    syms = nrzi(stuff(to_bits(bytes([SYNC]) + bytes(packet))))
    return syms + (["SE0", "SE0", "J"] if with_eop else [])


LEVELS = {"J": (0, 1), "K": (1, 0), "SE0": (0, 0)}


# ------------------------------------------------------------------------------------------------ decoding
class PacketError(Exception):
    pass


def decode_bits(bits):
    """Unstuff the (NRZI-decoded) bit stream after SYNC; raises PacketError on a stuffing violation."""
    out, ones, i = [], 0, 0
    while i < len(bits):
        b = bits[i]
        if ones == 6:
            if b != 0:
                raise PacketError("stuffing violation")
            ones = 0
            i += 1
            continue
        out.append(b)
        ones = ones + 1 if b else 0
        i += 1
    return out


def decode_symbols(symbols):
    """Packet bytes (without SYNC) from the J/K symbols before EOP. Raises PacketError if anything is wrong:
    SYNC, stuffing, PID check, byte alignment. Does not check the CRC (see check_packet)."""
    level, bits = "J", []
    for s in symbols:
        bits.append(1 if s == level else 0)
        level = s
    bits = decode_bits(bits)
    if len(bits) < 16 or len(bits) % 8:
        raise PacketError(f"{len(bits)} bits")
    data = bytes(sum(bits[8 * i + k] << k for k in range(8)) for i in range(len(bits) // 8))
    if data[0] != SYNC:
        raise PacketError(f"SYNC {data[0]:02x}")
    return data[1:]


def check_packet(packet):
    """(name, ok) for a decoded packet: PID complement, token CRC5 or data CRC16, length."""
    if not packet:
        raise PacketError("empty")
    pid, chk = packet[0] & 0xF, packet[0] >> 4
    if chk != (~pid & 0xF):
        raise PacketError(f"PID check {packet[0]:02x}")
    name = PID_NAMES.get(pid, f"PID{pid:x}")
    if pid in HANDSHAKES:
        return name, len(packet) == 1
    if pid in TOKENS:
        if len(packet) != 3:
            return name, False
        field = packet[1] | (packet[2] << 8)
        reg = crc5_register(field_bits(field & 0x7FF, 11))
        for i in range(11, 16):                      # the CRC bits go through the register as well
            reg = crc5_register([(field >> i) & 1], reg)
        return name, reg == CRC5_RESIDUE
    if pid in DATA:
        return name, len(packet) >= 3 and crc16_register(packet[1:]) == CRC16_RESIDUE
    return name, False


# ------------------------------------------------------------------------------------------------ waveform
class Bus:
    """Levels of D+ and D- over time, built from a list of (start clock, symbols).

    `ppm`: the transmitter's bit rate is off by this many parts per million (positive = slower bits).
    `jitter`: each symbol edge moves by a random amount in [-jitter, +jitter] clocks (a fraction, then rounded).
    `skew`: D- changes this many clocks after D+ (negative: before).
    Between packets the bus is idle (J). Call level(clock) -> (dp, dm).
    """

    def __init__(self, bursts, ppm=0, jitter=0.0, skew=0, rng=None, idle="J"):
        rng = rng or random.Random(0)
        self.idle = LEVELS[idle]
        self.events = []             # (clock, dp, dm), sorted
        bit = CLOCKS_PER_BIT * (1 + ppm * 1e-6)
        for start, symbols in bursts:
            t_prev = None
            for i, s in enumerate(symbols):
                t = start + i * bit + (rng.uniform(-jitter, jitter) if jitter and i else 0)
                tc = int(round(t))
                if t_prev is not None and tc <= t_prev:
                    tc = t_prev + 1
                t_prev = tc
                dp, dm = LEVELS[s]
                self.events.append((tc, dp, dm, s))
            end = start + len(symbols) * bit
            self.events.append((int(round(end)), None, None, "idle"))
        self.events.sort(key=lambda e: e[0])
        self.times = [e[0] for e in self.events]
        self.skew = skew
        self._cache = {}

    def level(self, clock):
        """(dp, dm) at `clock`."""
        dp = self._pin(clock, 0)
        dm = self._pin(clock - self.skew, 1)
        return dp, dm

    def _pin(self, clock, which):
        i = bisect.bisect_right(self.times, clock) - 1
        if i < 0:
            return self.idle[which]
        v = self.events[i][1 + which]
        return self.idle[which] if v is None else v

    def end_clock(self):
        return self.times[-1] if self.times else 0


def burst_for(packet, start):
    return start, packet_symbols(packet)
