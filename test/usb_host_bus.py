# SPDX-License-Identifier: Apache-2.0
"""The far end of a low-speed USB bus for the USB host example (firmware/examples/usb_ls_host.asm): a device model
that checks everything the host puts on the wire and answers like a device would.

Used clock by clock (20 ns) by test_usb_ls_host_model.py (instruction-set model) and test_usb_ls_host_rtl.py (RTL).

The wire. D+ and D- are the host's pads uio[0] and uio[1]. A line is driven by the chip when its output enable is
set (then it has the chip's output value), else by the device when the device is sending, else it rests on its
resistor: D+ on the host's 15 kOhm pull-down (low), D- on the device's 1.5 kOhm pull-up (high). So an idle bus is J.
Both ends driving the same line at once is an error (contention).

What is checked on the host's side (all in clocks of 20 ns; one bit is 33.33 clocks):
  * bus reset: SE0 for at least 10 ms (or `reset_min` clocks for the short tests);
  * keep-alives: an SE0 of 1.25 .. 1.5 us (tLEOPT) with no packet before it, then J;
  * every packet: decoded with the reference model (usb_ls.decode_symbols, check_packet: SYNC, stuffing, PID, CRC);
    every transition on the ideal 666.67 ns grid of its packet (started by its first transition), and the jitter
    of every interval and every pair of intervals measured as in the USB spec (tDDJ1, tDDJ2);
  * the EOP: SE0 of 1.25 .. 1.5 us at a bit boundary, then J; the chip lets go of the lines within 1.5 bit times
    after the J started, and before the device may answer;
  * the time between the token and the data packet: 2 to 7.5 bit times.
The device answers a SETUP token + DATA0 pair that both checked out with a handshake (`answer`, default ACK),
`delay_bits` bit times after the end of the DATA0 packet's EOP, at `ppm` off the nominal rate.
"""
import usb_ls as u

CELL = u.CLOCKS_PER_BIT                    # 33.33 clocks
J, K, SE0, SE1 = "J", "K", "SE0", "SE1"


def state_of(dp, dm):
    return {(0, 1): J, (1, 0): K, (0, 0): SE0, (1, 1): SE1}[(dp, dm)]


class HostBus:
    def __init__(self, answer=u.ACK, delay_bits=3.0, ppm=0, reset_min=500_000, garble=False):
        self.answer, self.delay_bits, self.ppm = answer, delay_bits, ppm
        self.reset_min = reset_min
        self.garble = garble                # answer with a broken SYNC
        self.errors = []
        self.packets = []                   # (clock, bytes or None, name, ok)
        self.packet_edges = []              # the transitions of each packet (clocks)
        self.keepalives = []                # SE0 widths in clocks
        self.resets = []                    # SE0 widths in clocks
        self.eops = []                      # SE0 widths in clocks
        self.jitter1, self.jitter2, self.grid = [], [], []     # ns
        self.gaps = []                      # bit times between the end of one host packet and the next one
        self.answers = []                   # clocks at which the device started an answer
        self.release_after_j = []           # clocks from the EOP's J to the chip letting go
        self.cur, self.start = J, 0
        self.pkt = None                     # the host packet being received: transitions and segments
        self.last_eop_j = None              # clock of the last SE0 -> J of a host packet
        self.prev_name = None
        self.dev = None                     # (start clock, symbols, cell) while the device sends
        self.chip_was = 0
        self.awaiting_release = None

    # ---- the device's own drive
    def device_level(self, c):
        if self.dev is None:
            return None
        t0, syms, cell = self.dev
        if c < t0:
            return None
        i = int((c - t0) / cell)
        if i >= len(syms):
            self.dev = None
            return None
        return u.LEVELS[syms[i]]

    def step(self, c, oe, out):
        """One clock: the chip's pad outputs -> (D+, D-) as both ends see them."""
        dev = self.device_level(c)
        chip = oe & 3
        if dev is not None and chip:
            self.errors.append(f"clock {c}: contention, the chip drives uio_oe={oe & 3:#x} while the device sends")
        dp = (out & 1) if oe & 1 else (dev[0] if dev else 0)
        dm = ((out >> 1) & 1) if oe & 2 else (dev[1] if dev else 1)
        if self.awaiting_release is not None and not chip:
            self.release_after_j.append(c - self.awaiting_release)
            self.awaiting_release = None
        self.chip_was = chip
        self.track(c, state_of(dp, dm), dev is not None)
        return dp, dm

    # ---- what the host sends
    def track(self, c, st, device_sending):
        if st == self.cur:
            return
        prev, length = self.cur, c - self.start
        self.cur, self.start = st, c
        if device_sending or (prev != J and self.pkt is None and prev != SE0 and st != SE0):
            return
        if st == SE1 or prev == SE1:
            self.errors.append(f"clock {c}: SE1 (both lines high)")
            return
        if self.pkt is None:
            if prev == J and st == K:                       # start of a packet (first SYNC bit)
                self.pkt = {"t": [c], "segs": [], "seg_start": c}
                if self.last_eop_j is not None:
                    self.gaps.append((c - self.last_eop_j) / CELL)
            elif prev == SE0 and st == J:                   # an SE0 outside a packet: keep-alive or reset
                if length >= self.reset_min:
                    self.resets.append(length)
                else:
                    self.keepalives.append(length)
                    if not 62 <= length <= 75:
                        self.errors.append(f"clock {c}: keep-alive SE0 of {length * 20} ns (1.25 .. 1.5 us)")
            return
        p = self.pkt
        p["segs"].append((prev, length))
        if st == SE0:
            p["t"].append(c)                                # the EOP starts on the grid as well
        elif prev == SE0:                                   # SE0 -> J: the packet is over
            self.end_packet(c, p, length)
            self.pkt = None
        else:
            p["t"].append(c)

    def end_packet(self, c, p, se0):
        t = p["t"]
        t0 = t[0]
        for ti in t:                                        # every transition against the packet's ideal grid
            k = round((ti - t0) / CELL)
            self.grid.append((ti - t0 - k * CELL) * 20)
        for a, b in zip(t, t[1:]):
            n = round((b - a) / CELL)
            self.jitter1.append((b - a - n * CELL) * 20)
        for a, b in zip(t, t[2:]):
            n = round((b - a) / CELL)
            self.jitter2.append((b - a - n * CELL) * 20)
        self.eops.append(se0)
        if not 62 <= se0 <= 75:
            self.errors.append(f"clock {c}: EOP SE0 of {se0 * 20} ns (1.25 .. 1.5 us)")
        syms = []
        for st, length in p["segs"][:-1]:                   # the J / K cells before the EOP
            syms += [st] * round(length / CELL)
        try:
            data = u.decode_symbols(syms)
            name, ok = u.check_packet(data)
        except u.PacketError as e:
            data, name, ok = None, f"error: {e}", False
        self.packets.append((t0, data, name, ok))
        self.packet_edges.append(list(t))
        if not ok:
            self.errors.append(f"clock {t0}: bad packet {name} {data.hex() if data else ''}")
        self.last_eop_j = c
        self.awaiting_release = c
        # the device answers a good DATA0 that follows a good SETUP token to address 0, endpoint 0
        if ok and name == "DATA0" and self.prev_name == "SETUP" and self.answer is not None:
            cell = CELL * (1 + self.ppm * 1e-6)
            syms = u.packet_symbols(u.handshake(self.answer))
            if self.garble:
                syms = ["K", "K"] + syms[2:]
            self.dev = (c + self.delay_bits * cell, syms, cell)
            self.answers.append(c + self.delay_bits * cell)
        self.prev_name = name if ok else None

    def check(self):
        assert not self.errors, "\n".join(self.errors[:10])
