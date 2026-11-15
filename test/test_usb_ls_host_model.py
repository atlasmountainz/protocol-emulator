# SPDX-License-Identifier: Apache-2.0
"""The low-speed USB host (firmware/examples/usb_ls_host.asm) on the instruction-set model, against the device
model of usb_host_bus.py. Pure Python, no simulator:  cd test && python3 -m pytest test_usb_ls_host_model.py

The firmware runs on the schedule 0,1,0,1. Every clock the chip's pads (uio[1:0], output enable and value) and the
device's drive make the levels of D+ and D-, which go back into the chip as inputs 8 and 9. The device model decodes
everything the host sends with the reference model (usb_ls.py) and checks the bus timing (usb_host_bus.py); after a
SETUP + DATA0 pair it answers with a handshake, which the chip shows on uo[7:0].
The same scenarios run on the RTL in test_usb_ls_host_rtl.py.
"""
import random
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import asm  # noqa: E402
import usb_ls as u  # noqa: E402
from isa_model import Model  # noqa: E402
from usb_host_bus import HostBus  # noqa: E402

FIRMWARE = Path(__file__).resolve().parent.parent / "firmware" / "examples" / "usb_ls_host.asm"
SCHEDULE = (0, 1, 0, 1)
FRAME = 50_000                  # about 1 ms of clocks
GET_DESCRIPTOR = bytes.fromhex("8006000100004000")
SHORT_RESET = 4000              # clocks: the reset of the patched firmware (one 4097-turn wait) is about 8200


def source(payload=GET_DESCRIPTOR, addr=0, ep=0, short_reset=True):
    """The firmware, optionally with another request in the DATA0 packet and another address / endpoint in the
    token (their CRCs come from the reference model), and with a short bus reset for the tests."""
    text = FIRMWARE.read_text()
    if short_reset:
        text = text.replace("h_rlen: LDX  64", "h_rlen: LDX  1")
    tok = u.token(u.SETUP, addr, ep)
    text = text.replace("        LDA  0x00             ; address 0, endpoint bit 0\n",
                        f"        LDA  {tok[1]:#04x}\n")
    text = text.replace("        LDA  0x10             ; endpoint bits 3:1, CRC5\n", f"        LDA  {tok[2]:#04x}\n")
    data = u.data_packet(u.DATA0, payload)[1:]
    block = "".join(f"        LDA  {b:#04x}\n        CALL emit\n" for b in data)
    start = text.index("        LDA  0x80             ; GET_DESCRIPTOR")
    end = text.index("        CALL e_end\n        JMP  e_frame")
    return text[:start] + block + text[end:]


def words(text):
    w, _ = asm.assemble(text)
    assert len(w) <= 256
    return w


def run(text, bus, clocks):
    """Run the host against `bus`; returns the uo[7:0] values in order (each change) and the model."""
    m = Model(words(text), SCHEDULE)
    dp, dm = 0, 1
    shown = []
    for c in range(clocks):
        m.clock((dp | dm << 1 | 0xCC) << 8)          # pads 2, 3, 6, 7 pulled up, 4, 5 (the loader's) low
        assert m.uio_oe & ~3 == 0, "the chip drives a pad other than D+ / D-"
        dp, dm = bus.step(c, m.uio_oe, m.uio_out)
        if not shown or shown[-1] != m.pins_out:
            shown.append(m.pins_out)
    return shown[2:], m                              # (reset value 0x01, then OUT 0x00)


def check_wire(bus, frames, payload=GET_DESCRIPTOR, addr=0, ep=0):
    """Everything the host put on the wire was right, frame after frame."""
    bus.check()
    tok, dat = u.token(u.SETUP, addr, ep), u.data_packet(u.DATA0, payload)
    got = [(d, n, ok) for _, d, n, ok in bus.packets]
    assert got[:2 * frames] == [(tok, "SETUP", True), (dat, "DATA0", True)] * frames, got[:4]
    assert len(bus.keepalives) >= frames and all(62 <= k <= 75 for k in bus.keepalives)
    assert all(62 <= s <= 75 for s in bus.eops)
    token_to_data = bus.gaps[0::2]
    assert all(2 <= g <= 7.5 for g in token_to_data), token_to_data
    # the chip lets go of the lines one bit time after the EOP's J started (34 clocks), long before the device
    # may answer (2 bit times)
    assert set(bus.release_after_j) <= set(range(33, 40)), bus.release_after_j
    # every transition within 26.7 ns of the ideal grid; the cells are 680 / 680 / 640 ns
    assert min(bus.grid) > -0.1 and max(bus.grid) < 26.7, (min(bus.grid), max(bus.grid))
    assert max(abs(j) for j in bus.jitter1) < 26.7 and max(abs(j) for j in bus.jitter2) < 26.7


# ------------------------------------------------------------------ the reference model and the constants
def test_constant_packets_are_the_reference_ones():
    assert u.token(u.SETUP, 0, 0).hex() == "2d0010"
    assert u.data_packet(u.DATA0, GET_DESCRIPTOR).hex() == "c38006000100004000dd94"
    # (the firmware itself is checked on the wire: test_three_frames_acknowledged decodes exactly these bytes from
    # the unpatched packets; source() rewrites them only for the tests with other requests)


# ------------------------------------------------------------------ the firmware
def test_three_frames_acknowledged():
    text = FIRMWARE.read_text().replace("h_rlen: LDX  64", "h_rlen: LDX  1")      # the packets as shipped
    bus = HostBus(reset_min=SHORT_RESET)
    shown, _ = run(text, bus, 3 * FRAME + 20_000)
    check_wire(bus, 3)
    assert shown == [0xD2], [hex(v) for v in shown]          # ACK, and it stays (the same answer every frame)
    assert len(bus.answers) >= 3


def test_bus_reset_is_10ms():
    bus = HostBus()
    run(source(short_reset=False), bus, 560_000)
    assert len(bus.resets) == 1 and 500_000 <= bus.resets[0] <= 540_000, bus.resets
    assert bus.keepalives and not bus.errors


def test_bit_rate_is_exact():
    """The cells are 17 / 17 / 16 turns: on average exactly 1.5 Mbit/s, so a packet's transitions stay on its ideal
    grid however long it is (the last one as close as the first)."""
    bus = HostBus(reset_min=SHORT_RESET)
    run(source(payload=b"\x00" * 8), bus, FRAME)                    # all 0s: a transition at every bit
    bus.check()
    edges = bus.packet_edges[1]                                     # the DATA0 packet
    cells = round((edges[-1] - edges[0]) / u.CLOCKS_PER_BIT)
    assert cells > 80 and abs((edges[-1] - edges[0]) - cells * u.CLOCKS_PER_BIT) < 1.4


@pytest.mark.parametrize("answer,shown", [(u.NAK, 0x5A), (u.STALL, 0x1E), (None, 0x00)])
def test_other_answers(answer, shown):
    bus = HostBus(answer=answer, reset_min=SHORT_RESET)
    got, _ = run(source(), bus, 2 * FRAME + 20_000)
    check_wire(bus, 2)
    assert got == ([shown] if shown else []), [hex(v) for v in got]       # (0x00 is already showing)


def test_garbled_answer():
    bus = HostBus(garble=True, reset_min=SHORT_RESET)
    got, _ = run(source(), bus, FRAME + 20_000)
    assert got == [0xFF]


@pytest.mark.parametrize("delay", [2.0, 3.3, 4.5, 6.5])
@pytest.mark.parametrize("ppm", [-15000, 0, 15000])
def test_answer_timing_and_rate(delay, ppm):
    """The device may answer 2 to 6.5 bit times after the EOP, at +-1.5 % off the nominal rate."""
    bus = HostBus(delay_bits=delay, ppm=ppm, reset_min=SHORT_RESET)
    got, _ = run(source(), bus, FRAME + 20_000)
    assert got == [0xD2], (delay, ppm, [hex(v) for v in got])
    bus.check()


@pytest.mark.parametrize("seed", range(6))
def test_other_requests_and_addresses(seed):
    """Other DATA0 payloads (bit stuffing, all 1s, random) and other token addresses / endpoints: the bytes on the
    wire are the reference model's, stuffing included."""
    rng = random.Random(seed)
    payload = [b"\xff" * 8, b"\x7e\xfc\xf9\xf3\xe7\xcf\x9f\x3f", bytes(8)][seed] if seed < 3 else \
        bytes(rng.randrange(256) for _ in range(8))
    addr, ep = rng.randrange(128), rng.randrange(16)
    bus = HostBus(reset_min=SHORT_RESET)
    got, _ = run(source(payload, addr, ep), bus, 2 * FRAME + 20_000)
    check_wire(bus, 2, payload, addr, ep)
    assert got == [0xD2]


def test_stuffing_is_exercised():
    """All 1s needs a stuffed 0 after every six 1s: 8 data bytes + CRC give at least 10 stuffed bits."""
    raw = u.to_bits(bytes([u.SYNC]) + u.data_packet(u.DATA0, b"\xff" * 8))
    assert len(u.stuff(raw)) - len(raw) >= 10


# ------------------------------------------------------------------ do the tests have teeth?
MUTANTS = [
    ("stuffing after seven 1s", "e_st:   XCMP 6", "e_st:   XCMP 7"),
    ("no NRZI toggle", "        XOR  3                ; J <-> K", "        XOR  0"),
    ("a cell one turn long", "tx_g:   CALL cell             ; 17 turns\n        WAIT 1",
     "tx_g:   CALL cell             ; 17 turns\n        WAIT 2"),
    ("EOP too long", "tx_se0: ODRVA                 ; SE0\n        WAIT 30", "tx_se0: ODRVA                 ; SE0\n        WAIT 38"),
    ("lines kept after the EOP", "tx_j:   ODRVA                 ; J\n        WAIT 13", "tx_j:   ODRVA                 ; J\n        WAIT 60"),
    ("samples half a bit late", "h_rk:   WAIT 3", "h_rk:   WAIT 12"),
    ("group sent without its marker bit", "e_f0:   ROR                   ; C = 1 (the marker) becomes the group's top bit\n",
     "e_f0:   SHR\n"),
    ("no keep-alive", "        LDA  0\n        SEND                  ; a keep-alive\n", ""),
]


@pytest.mark.parametrize("name,old,new", MUTANTS, ids=[m[0] for m in MUTANTS])
def test_mutant_is_caught(name, old, new):
    text = source(payload=b"\xff" * 8)
    assert text.count(old) == 1, name
    text = text.replace(old, new)
    bus = HostBus(reset_min=SHORT_RESET, delay_bits=2.5)
    caught = False
    try:
        got, _ = run(text, bus, 2 * FRAME + 20_000)
        check_wire(bus, 2, b"\xff" * 8)
        assert got == [0xD2]
        if name == "no keep-alive":
            assert len(bus.keepalives) >= 2
    except (AssertionError, KeyError):
        caught = True
    assert caught, f"mutant '{name}' passes"
