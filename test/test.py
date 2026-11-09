# SPDX-License-Identifier: Apache-2.0

import os
import random
import sys
from pathlib import Path

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, RisingEdge

from loader import load_program

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import asm  # noqa: E402
from load_image import build_image  # noqa: E402

CLKS_PER_BIT = 432  # 108 thread slots of 4 clocks; must match firmware/uart_tx.asm

# FROM_RAM=1 make: run every test with the firmware loaded into the program memory over the
# pins instead of running the built-in ROM. The results must be identical.
FROM_RAM = os.environ.get("FROM_RAM") == "1"
FW_DIR = Path(__file__).resolve().parent.parent / "firmware"


def firmware_image(uio):
    """The ROM holds every protocol (154 words) but the program memory holds 128, so in FROM_RAM mode
    load just the protocol that the mode pins select, behind a one-word jump to its entry point."""
    pins = (uio >> 6) & 3
    entry, files = {0: ("uart_start", ["uart_tx"]),
                    1: ("i2c_start_fw", ["i2c_master", "i2c_monitor", "uart_tx"]),
                    2: ("spi_start", ["spi_master"]),
                    3: ("echo_start", ["echo"])}[pins]
    text = f"JMP {entry}\n" + "\n".join((FW_DIR / f"{f}.asm").read_text() for f in files)
    words, _ = asm.assemble(text)
    return words + [0xF000] * (128 - len(words))


async def reset(dut, mode=0, uio=None, ui=0):
    """Reset the chip. `mode` is the SPI select pin (uio_in[7]); `uio` overrides the whole
    uio_in value (the I2C mode pin is uio_in[6]). Neither set = UART."""
    dut.ena.value = 1
    dut.ui_in.value = ui
    dut.uio_in.value = (mode << 7) if uio is None else uio
    dut.rst_n.value = 0
    await ClockCycles(dut.clk, 10)
    dut.rst_n.value = 1
    if FROM_RAM:
        start_uio = int(dut.uio_in.value)
        await load_program(dut, firmware_image(start_uio))
        dut.uio_in.value = start_uio
        dut.ui_in.value = ui
    # Boot: the dispatcher and the protocol's init take about ten slots (40 clocks) to reach the
    # idle loop. The start input is level-triggered, so tests raise it after this.
    await ClockCycles(dut.clk, 60)


async def send_and_capture(dut, byte):
    """Hold start with `byte` on ui_in, then sample the TX line mid-bit."""
    dut.ui_in.value = byte
    dut.uio_in.value = 1  # start pin high (level-triggered; firmware polls it)
    await ClockCycles(dut.clk, 8)
    dut.uio_in.value = 0

    # TX goes low (start bit) once the firmware has seen start
    for _ in range(200):
        await RisingEdge(dut.clk)
        if int(dut.uo_out.value) & 1 == 0:
            break
    else:
        raise AssertionError("TX never went low after start")

    # Move to the middle of the start bit, then sample once per bit time
    await ClockCycles(dut.clk, CLKS_PER_BIT // 2)
    start_bit = int(dut.uo_out.value) & 1
    assert start_bit == 0, "start bit should be 0"

    value = 0
    for i in range(8):
        await ClockCycles(dut.clk, CLKS_PER_BIT)
        value |= (int(dut.uo_out.value) & 1) << i

    await ClockCycles(dut.clk, CLKS_PER_BIT)
    stop_bit = int(dut.uo_out.value) & 1
    assert stop_bit == 1, "stop bit should be 1"
    return value


@cocotb.test()
async def test_idle_high(dut):
    cocotb.start_soon(Clock(dut.clk, 20, unit="ns").start())
    await reset(dut)
    await ClockCycles(dut.clk, 50)
    assert int(dut.uo_out.value) & 1 == 1, "TX must idle high"
    assert (int(dut.uo_out.value) >> 1) & 1 == 0, "busy must be low when idle"


@cocotb.test()
async def test_send_bytes(dut):
    cocotb.start_soon(Clock(dut.clk, 20, unit="ns").start())
    await reset(dut)

    for byte in (0x55, 0xA3, 0x00, 0xFF, 0x41):
        got = await send_and_capture(dut, byte)
        dut._log.info(f"sent 0x{byte:02X}, received 0x{got:02X}")
        assert got == byte, f"expected 0x{byte:02X}, got 0x{got:02X}"
        # We sampled mid stop bit, so busy must still be high; it should clear
        # within the rest of the stop bit (plus a little slack).
        assert (int(dut.uo_out.value) >> 1) & 1 == 1, "busy should still be high mid stop bit"
        for _ in range(CLKS_PER_BIT):
            await RisingEdge(dut.clk)
            if (int(dut.uo_out.value) >> 1) & 1 == 0:
                break
        else:
            raise AssertionError("busy never cleared after the stop bit")
        assert int(dut.uo_out.value) & 1 == 1, "TX must return to idle high"
        await ClockCycles(dut.clk, 16)   # let the firmware get back to its idle loop (a few slots)


@cocotb.test()
async def test_cycle_exact_timing(dut):
    """The CPU must hit the baud rate exactly: every TX transition is a multiple of one
    bit time apart. 0x55 toggles TX on every bit, so all 9 gaps must be exactly 432."""
    cocotb.start_soon(Clock(dut.clk, 20, unit="ns").start())
    await reset(dut)

    # Record every TX transition, starting *before* start is raised so the start-bit
    # edge is never missed.
    dut.ui_in.value = 0x55
    prev = int(dut.uo_out.value) & 1
    edges = []
    for cycle in range(10 * CLKS_PER_BIT + 150):
        if cycle == 2:
            dut.uio_in.value = 1   # raise start (held for 8 clocks)
        if cycle == 10:
            dut.uio_in.value = 0
        await RisingEdge(dut.clk)
        cur = int(dut.uo_out.value) & 1
        if cur != prev:
            edges.append(cycle)
            prev = cur

    # idle->start, start->d0, d0..d7 (7 toggles), d7->stop: 10 transitions, 9 gaps
    assert len(edges) == 10, f"expected 10 TX transitions, got {len(edges)}: {edges}"
    gaps = [b - a for a, b in zip(edges, edges[1:])]
    dut._log.info(f"transition gaps: {gaps}")
    assert all(g == CLKS_PER_BIT for g in gaps), f"bit times not exactly {CLKS_PER_BIT}: {gaps}"


# ---------------------------------------------------------------- SPI (mode 1 firmware)

SPI_MODE_PIN = 0x80  # uio_in[7]


class SpiSlave:
    """Behavioural SPI slave, mode 0, MSB first. Drives MISO (uio_in[1]); the test drives
    start (uio_in[0]). Records the MOSI bits seen during each CS_N-low transaction."""

    def __init__(self, dut, tx_byte):
        self.dut = dut
        self.tx_byte = tx_byte
        self.start = 0
        self.miso = 0
        self.mosi_bits = []
        self.transactions = []   # list of MOSI bit lists, one per CS_N-low window
        self.sck_in_idle = True  # SCK must be low whenever CS_N is high
        self.drive()

    def drive(self):
        self.dut.uio_in.value = SPI_MODE_PIN | (self.miso << 1) | self.start

    async def run(self):
        prev_cs, prev_sck, idx = 1, 0, 0
        while True:
            await RisingEdge(self.dut.clk)
            o = int(self.dut.uo_out.value)
            cs, sck, mosi = o & 1, (o >> 1) & 1, (o >> 2) & 1
            if cs == 1 and sck == 1:
                self.sck_in_idle = False
            if prev_cs == 1 and cs == 0:        # CS_N asserted: present first MISO bit
                idx, self.mosi_bits = 0, []
                self.miso = (self.tx_byte >> 7) & 1
                self.drive()
            if cs == 0:
                if sck == 1 and prev_sck == 0:  # rising edge: sample MOSI
                    self.mosi_bits.append(mosi)
                if sck == 0 and prev_sck == 1:  # falling edge: shift out next MISO bit
                    idx += 1
                    self.miso = (self.tx_byte >> (7 - idx)) & 1 if idx < 8 else 0
                    self.drive()
            if prev_cs == 0 and cs == 1:        # CS_N released: transaction complete
                self.transactions.append(list(self.mosi_bits))
            prev_cs, prev_sck = cs, sck


def bits_to_bytes(bits):
    return [int("".join(str(b) for b in bits[i:i + 8]), 2) for i in range(0, len(bits), 8)]


@cocotb.test()
async def test_spi_master(dut):
    """SPI firmware: sends ui_in MSB first and echoes back the byte it received."""
    cocotb.start_soon(Clock(dut.clk, 20, unit="ns").start())
    await reset(dut, mode=1)

    for send, recv in ((0x3A, 0xC5), (0x00, 0xFF), (0xA5, 0x5A)):
        slave = SpiSlave(dut, recv)
        task = cocotb.start_soon(slave.run())

        dut.ui_in.value = send
        slave.start = 1
        slave.drive()
        await ClockCycles(dut.clk, 8)
        slave.start = 0
        slave.drive()

        # 16 bits x 12 slots (48 clocks) plus setup/hold; wait for the transaction to finish
        for _ in range(1500):
            await RisingEdge(dut.clk)
            if slave.transactions:
                break
        else:
            raise AssertionError("SPI transaction never completed")
        await ClockCycles(dut.clk, 20)
        task.cancel()

        assert len(slave.transactions) == 1, "CS_N should pulse low exactly once"
        got = slave.transactions[0]
        assert len(got) == 16, f"expected 16 SCK pulses, got {len(got)}"
        first, second = bits_to_bytes(got)
        dut._log.info(f"SPI: sent 0x{send:02X} / slave replied 0x{recv:02X} / "
                      f"MOSI saw 0x{first:02X} then 0x{second:02X}")
        assert first == send, f"MOSI byte 1: expected 0x{send:02X}, got 0x{first:02X}"
        assert second == recv, f"MISO was not received correctly: echoed 0x{second:02X}, expected 0x{recv:02X}"
        assert slave.sck_in_idle, "SCK must stay low while CS_N is high"
        assert int(dut.uo_out.value) & 0b1111 == 0b0001, "after transfer: CS_N high, SCK low, MOSI low, not busy"


# ---------------------------------------------------------------- I2C (mode pin uio_in[6])

I2C_MODE_PIN = 0x40
SCL, SDA = 2, 3   # uio[2], uio[3]
I2C_IDLE_BUS = I2C_MODE_PIN | (1 << SCL) | (1 << SDA)  # both lines pulled high

# Standard-mode (100 kHz) minimums from the I2C spec, in 20 ns clock cycles
T_LOW, T_HIGH = 235, 200        # SCL low 4.7 us, SCL high 4.0 us
T_HD_STA, T_SU_STA = 200, 235   # START hold 4.0 us, repeated-START setup 4.7 us
T_SU_STO, T_BUF = 200, 235      # STOP setup 4.0 us, bus free time 4.7 us
T_SU_DAT = 13                   # data setup 250 ns


class I2CEeprom:
    """Open-drain I2C bus plus a simple EEPROM slave (7-bit address, 1-byte pointer).

    Wired-AND bus: a line is low if the master pulls it (uio_oe) or the slave does.
    Drives the pad inputs every clock and checks the master's timing against the spec.
    """

    def __init__(self, dut, mem, addr=0x50, stretch=0):
        self.dut, self.mem, self.addr = dut, dict(mem), addr
        self.stretch = stretch          # cycles to hold SCL low after the address byte (once)
        self.stretch_left = 0
        self.stretch_events = 0
        self.start_pin = 0              # uio_in[0], driven by the test
        self.sda_low = False            # slave pulling SDA
        self.mode, self.expect = "idle", None
        self.bits = self.shift = self.ptr = 0
        self.log = []                   # every byte the master sent
        self.violations = []

    def drive(self, scl, sda):
        self.dut.uio_in.value = (I2C_MODE_PIN | self.start_pin | (scl << SCL) | (sda << SDA))

    def levels(self):
        oe = int(self.dut.uio_oe.value)
        scl = 0 if ((oe >> SCL) & 1 or self.stretch_left > 0) else 1
        sda = 0 if ((oe >> SDA) & 1 or self.sda_low) else 1
        return scl, sda

    def bad(self, cyc, what):
        self.violations.append(f"cycle {cyc}: {what}")

    async def run(self):
        cyc = 0
        prev_scl = prev_sda = 1
        t_fall = t_rise = t_start = t_stop = t_sda = None
        after_start = False
        master_nack = 1
        while True:
            await RisingEdge(self.dut.clk)
            cyc += 1
            scl, sda = self.levels()

            if sda != prev_sda:
                t_sda = cyc
            # START: SDA falls while SCL high
            if prev_scl == 1 and scl == 1 and prev_sda == 1 and sda == 0:
                if t_rise is not None and t_stop is None and cyc - t_rise < T_SU_STA:
                    self.bad(cyc, f"tSU;STA {cyc - t_rise} < {T_SU_STA}")
                if t_stop is not None and cyc - t_stop < T_BUF:
                    self.bad(cyc, f"tBUF {cyc - t_stop} < {T_BUF}")
                t_start, t_stop, after_start = cyc, None, True
                self.mode, self.expect, self.bits, self.shift = "rx", "addr", 0, 0
                self.sda_low = False
            # STOP: SDA rises while SCL high
            elif prev_scl == 1 and scl == 1 and prev_sda == 0 and sda == 1:
                if t_rise is not None and cyc - t_rise < T_SU_STO:
                    self.bad(cyc, f"tSU;STO {cyc - t_rise} < {T_SU_STO}")
                t_stop = cyc
                self.mode, self.sda_low = "idle", False
            # SCL rising edge
            if prev_scl == 0 and scl == 1:
                if t_fall is not None and cyc - t_fall < T_LOW:
                    self.bad(cyc, f"tLOW {cyc - t_fall} < {T_LOW}")
                if t_sda is not None and 0 <= cyc - t_sda < T_SU_DAT and self.mode == "rx":
                    self.bad(cyc, f"tSU;DAT {cyc - t_sda} < {T_SU_DAT}")
                t_rise = cyc
                if self.mode == "rx":
                    self.shift = ((self.shift << 1) | sda) & 0xFF
                    self.bits += 1
                elif self.mode == "mack":
                    master_nack = sda
            # SCL falling edge
            if prev_scl == 1 and scl == 0:
                if after_start:
                    if cyc - t_start < T_HD_STA:
                        self.bad(cyc, f"tHD;STA {cyc - t_start} < {T_HD_STA}")
                    after_start = False
                elif t_rise is not None and cyc - t_rise < T_HIGH:
                    self.bad(cyc, f"tHIGH {cyc - t_rise} < {T_HIGH}")
                t_fall = cyc
                self.on_fall(master_nack)

            if self.stretch_left > 0:
                self.stretch_left -= 1
            scl, sda = self.levels()
            self.drive(scl, sda)
            prev_scl, prev_sda = scl, sda

    def on_fall(self, master_nack):
        if self.mode == "rx" and self.bits == 8:
            byte = self.shift
            self.log.append(byte)
            ack = True
            if self.expect == "addr":
                if byte >> 1 == self.addr:
                    self.expect = "ptr" if byte & 1 == 0 else "read"
                    if self.stretch:           # hold SCL low once, after the address byte
                        self.stretch_left, self.stretch, self.stretch_events = self.stretch, 0, 1
                else:
                    ack = False
            elif self.expect == "ptr":
                self.ptr, self.expect = byte, "ignore"
            self.bits = self.shift = 0
            if ack:
                self.sda_low, self.mode = True, "ack"
            else:
                self.mode = "idle"             # not for us: ignore until STOP/START
        elif self.mode == "ack":
            self.sda_low = False
            if self.expect == "read":
                self.txbyte, self.txidx, self.mode = self.mem.get(self.ptr, 0xFF), 0, "tx"
                self.sda_low = (self.txbyte >> 7) & 1 == 0
            else:
                self.mode, self.bits, self.shift = "rx", 0, 0
        elif self.mode == "tx":
            self.txidx += 1
            if self.txidx < 8:
                self.sda_low = (self.txbyte >> (7 - self.txidx)) & 1 == 0
            else:
                self.sda_low, self.mode = False, "mack"
        elif self.mode == "mack":
            self.sda_low, self.mode = False, "idle"


async def uart_receive(dut, limit=80000):
    """Wait for a UART frame on uo_out[0] and return the byte (samples mid-bit)."""
    for _ in range(limit):
        await RisingEdge(dut.clk)
        if int(dut.uo_out.value) & 1 == 0:
            break
    else:
        raise AssertionError("no UART frame appeared")
    await ClockCycles(dut.clk, CLKS_PER_BIT // 2)
    assert int(dut.uo_out.value) & 1 == 0, "start bit should be 0"
    value = 0
    for i in range(8):
        await ClockCycles(dut.clk, CLKS_PER_BIT)
        value |= (int(dut.uo_out.value) & 1) << i
    await ClockCycles(dut.clk, CLKS_PER_BIT)
    assert int(dut.uo_out.value) & 1 == 1, "stop bit should be 1"
    return value


async def i2c_transaction(dut, bus, pointer):
    """Hold start with the pointer byte on ui_in, return the byte printed on the UART."""
    dut.ui_in.value = pointer
    bus.start_pin = 1
    rx = cocotb.start_soon(uart_receive(dut))
    await ClockCycles(dut.clk, 8)
    bus.start_pin = 0
    return await rx


async def wait_idle(dut, cycles=3000):
    """Wait for I2C BUSY (uo[3]) to clear."""
    for _ in range(cycles):
        await RisingEdge(dut.clk)
        if (int(dut.uo_out.value) >> 3) & 1 == 0:
            return
    raise AssertionError("BUSY never cleared")


@cocotb.test()
async def test_i2c_eeprom_read(dut):
    """I2C firmware: EEPROM random read, bridged onto the UART, checked against the I2C spec."""
    cocotb.start_soon(Clock(dut.clk, 20, unit="ns").start())
    await reset(dut, uio=I2C_IDLE_BUS)
    bus = I2CEeprom(dut, {0x07: 0x5C, 0x00: 0xFF, 0xA5: 0x3C})
    cocotb.start_soon(bus.run())

    for pointer, expected in ((0x07, 0x5C), (0x00, 0xFF), (0xA5, 0x3C)):
        bus.log.clear()
        got = await i2c_transaction(dut, bus, pointer)
        dut._log.info(f"I2C: pointer 0x{pointer:02X} -> UART printed 0x{got:02X}; "
                      f"bytes on bus {[hex(b) for b in bus.log]}")
        assert got == expected, f"expected 0x{expected:02X}, got 0x{got:02X}"
        assert bus.log == [0xA0, pointer, 0xA1], f"unexpected bus traffic {bus.log}"
        await wait_idle(dut)
        assert (int(dut.uo_out.value) >> 4) & 1 == 0, "ERR must stay low on a good transfer"
        assert int(dut.uio_oe.value) & 0b1100 == 0, "bus must be released (both lines high) when idle"
        await ClockCycles(dut.clk, 300)   # let the bus-free time pass before the next START

    assert not bus.violations, "I2C timing violations:\n" + "\n".join(bus.violations)


@cocotb.test()
async def test_i2c_nack(dut):
    """A missing slave must raise ERR, release the bus with a STOP, and print nothing."""
    cocotb.start_soon(Clock(dut.clk, 20, unit="ns").start())
    await reset(dut, uio=I2C_IDLE_BUS)
    bus = I2CEeprom(dut, {0x07: 0x5C}, addr=0x51)   # nobody answers at 0x50
    cocotb.start_soon(bus.run())

    dut.ui_in.value = 0x07
    bus.start_pin = 1
    await ClockCycles(dut.clk, 8)
    bus.start_pin = 0
    tx_low = False
    for _ in range(8000):
        await RisingEdge(dut.clk)
        tx_low |= int(dut.uo_out.value) & 1 == 0
    assert (int(dut.uo_out.value) >> 4) & 1 == 1, "ERR should be high after a NACK"
    assert (int(dut.uo_out.value) >> 3) & 1 == 0, "BUSY should clear once the bus is released"
    assert not tx_low, "nothing should be printed on the UART after a NACK"
    assert bus.log == [0xA0], f"master should stop after the NACKed address: {bus.log}"
    assert int(dut.uio_oe.value) & 0b1100 == 0, "bus must be released after the STOP"
    assert not bus.violations, "I2C timing violations:\n" + "\n".join(bus.violations)


@cocotb.test()
async def test_i2c_clock_stretching(dut):
    """A slave that holds SCL low must just slow the master down, not corrupt data."""
    cocotb.start_soon(Clock(dut.clk, 20, unit="ns").start())
    await reset(dut, uio=I2C_IDLE_BUS)
    bus = I2CEeprom(dut, {0x42: 0x99}, stretch=900)
    cocotb.start_soon(bus.run())

    got = await i2c_transaction(dut, bus, 0x42)
    await wait_idle(dut)
    assert bus.stretch_events == 1, "the slave should have stretched the clock"
    assert got == 0x99, f"expected 0x99 after stretching, got 0x{got:02X}"
    assert bus.log == [0xA0, 0x42, 0xA1], f"unexpected bus traffic {bus.log}"
    assert not bus.violations, "I2C timing violations:\n" + "\n".join(bus.violations)


# ---- Loadable program memory ---------------------------------------------------------------

async def release_load(dut, uio=0, ui=0):
    """Lower LOAD (the CPU starts at address 0 of the loaded program)."""
    dut.ui_in.value = ui
    dut.uio_in.value = uio
    await ClockCycles(dut.clk, 6)


def assemble_words(text):
    words, _ = asm.assemble(text)
    return words + [0xF000] * (128 - len(words))


@cocotb.test()
async def test_load_custom_program(dut):
    """A program the chip was not built with: load it, run it, then load a different one."""
    cocotb.start_soon(Clock(dut.clk, 20, unit="ns").start())
    await reset(dut)
    rng = random.Random(7)

    # Program 1: count a byte up on the push-pull pins forever.
    prog1 = assemble_words("""
        LDA 0
    loop:
        OUT 0x3C
        WAIT 7
        OUT 0xC3
        WAIT 7
        JMP loop
    """)
    await load_program(dut, prog1, rng)
    await release_load(dut)
    await ClockCycles(dut.clk, 40)   # let the first instructions run (outputs start at 0x01)
    seen = set()
    for _ in range(100):
        await ClockCycles(dut.clk, 1)
        seen.add(int(dut.uo_out.value))
    assert seen == {0x3C, 0xC3}, f"unexpected outputs {seen}"

    # Program 2 replaces it without a reset, and ends in HALT with a fixed output.
    prog2 = assemble_words("""
        OUT 0xA5
        HALT
    """)
    await load_program(dut, prog2, rng)
    await release_load(dut)
    await ClockCycles(dut.clk, 20)
    assert int(dut.uo_out.value) == 0xA5
    await ClockCycles(dut.clk, 50)
    assert int(dut.uo_out.value) == 0xA5, "HALT must stop the CPU"


@cocotb.test()
async def test_loader_ignores_unstrobed_data(dut):
    """Data pins changing, or STROBE held high, must not write more than one byte per edge."""
    cocotb.start_soon(Clock(dut.clk, 20, unit="ns").start())
    await reset(dut)
    prog = assemble_words("OUT 0x5A\nHALT")
    await load_program(dut, prog)
    # Wiggle the data pins with LOAD still high but no strobe: nothing may be written.
    for v in (0x00, 0xFF, 0x12, 0x34):
        dut.ui_in.value = v
        await ClockCycles(dut.clk, 5)
    await release_load(dut)
    await ClockCycles(dut.clk, 20)
    assert int(dut.uo_out.value) == 0x5A


@cocotb.test()
async def test_reset_returns_to_rom(dut):
    """After a reset without LOAD the built-in firmware runs again."""
    cocotb.start_soon(Clock(dut.clk, 20, unit="ns").start())
    await reset(dut)
    await load_program(dut, assemble_words("OUT 0xA5\nHALT"))
    await release_load(dut)
    await ClockCycles(dut.clk, 20)
    assert int(dut.uo_out.value) == 0xA5

    dut.ui_in.value = 0
    dut.uio_in.value = 0
    dut.rst_n.value = 0
    await ClockCycles(dut.clk, 10)
    dut.rst_n.value = 1
    await ClockCycles(dut.clk, 30)
    assert int(dut.uo_out.value) == 0x01, "ROM firmware (UART idle) must run again"
    got = await send_and_capture(dut, 0x3C)
    assert got == 0x3C


@cocotb.test()
async def test_partial_load_then_reload(dut):
    """An aborted load (not a whole image) must not shift the next load: LOAD restarts at byte 0."""
    cocotb.start_soon(Clock(dut.clk, 20, unit="ns").start())
    await reset(dut)
    junk = assemble_words("OUT 0x11\nHALT")
    await load_program(dut, junk[:1] + [0xF000] * 0)      # one word...
    # ...plus one stray byte, so the pointer is left odd
    dut.ui_in.value = 0x77
    await ClockCycles(dut.clk, 3)
    dut.uio_in.value = 0x30   # LOAD | STROBE
    await ClockCycles(dut.clk, 3)
    dut.uio_in.value = 0x20
    await ClockCycles(dut.clk, 3)
    await release_load(dut)

    await load_program(dut, assemble_words("OUT 0x5A\nOUT 0xC3\nHALT"))
    await release_load(dut)
    await ClockCycles(dut.clk, 20)
    assert int(dut.uo_out.value) == 0xC3


EXAMPLES = str(Path(__file__).resolve().parent.parent / "firmware" / "examples")


async def uart_send_frame(dut, byte, bit_clks, pin=0):
    """Drive an 8N1 frame onto ui_in[pin] with `bit_clks` clocks per bit (LSB first)."""
    bits = [0] + [(byte >> i) & 1 for i in range(8)] + [1]
    for b in bits:
        dut.ui_in.value = b << pin
        await ClockCycles(dut.clk, bit_clks)


@cocotb.test()
async def test_example_uart_receiver(dut):
    """New protocol, no new silicon: a UART receiver loaded into the program memory. Also checks
    that it tolerates a +-3% baud-rate error, as a real receiver must."""
    cocotb.start_soon(Clock(dut.clk, 20, unit="ns").start())
    await reset(dut)
    await load_program(dut, build_image([EXAMPLES + "/uart_rx.asm"]))
    await release_load(dut, ui=1)            # RX idles high
    await ClockCycles(dut.clk, 20)
    assert (int(dut.uio_oe.value) >> 7) & 1 == 0, "RX_VALID must be released before any byte"

    for bit_clks, byte in [(432, 0x55), (432, 0xA3), (432, 0x00), (432, 0xFF), (432, 0x3C),
                           (419, 0x96), (445, 0xC5), (419, 0x01), (445, 0x80)]:
        await uart_send_frame(dut, byte, bit_clks)
        await ClockCycles(dut.clk, bit_clks)  # stop bit (ui is still 1)
        assert int(dut.uo_out.value) == byte, f"got {int(dut.uo_out.value):#04x}, sent {byte:#04x}"
        assert (int(dut.uio_oe.value) >> 7) & 1 == 1, "RX_VALID must be asserted after a byte"
        await ClockCycles(dut.clk, bit_clks)  # idle between frames


# ---- Threads: full-duplex UART echo --------------------------------------------------------

ECHO_MODE = 0xC0   # uio_in[7:6] = 11


async def watch_uart_tx(dut, frames, bit_clks=CLKS_PER_BIT):
    """Decode UART frames on uo_out[0] forever. Appends (start_cycle, byte) to `frames`; checks the
    stop bit. The sample points are the middle of each bit, as a real receiver would use."""
    cycle = 0
    prev = 1
    while True:
        await RisingEdge(dut.clk)
        cycle += 1
        cur = int(dut.uo_out.value) & 1
        if prev == 1 and cur == 0:
            start = cycle
            await ClockCycles(dut.clk, bit_clks // 2)
            cycle += bit_clks // 2
            assert int(dut.uo_out.value) & 1 == 0, "start bit glitch"
            value = 0
            for i in range(8):
                await ClockCycles(dut.clk, bit_clks)
                cycle += bit_clks
                value |= (int(dut.uo_out.value) & 1) << i
            await ClockCycles(dut.clk, bit_clks)
            cycle += bit_clks
            assert int(dut.uo_out.value) & 1 == 1, "stop bit must be high"
            frames.append((start, value))
            cur = 1
        prev = cur


@cocotb.test()
async def test_echo_full_duplex(dut):
    """Two threads, one byte mailbox: bytes sent back to back (no idle time) come out of TX in the same
    order while the next one is still being received. A sender that is 0.4 % fast must work too."""
    cocotb.start_soon(Clock(dut.clk, 20, unit="ns").start())
    await reset(dut, uio=ECHO_MODE, ui=1)
    await ClockCycles(dut.clk, 20)
    assert int(dut.uo_out.value) & 1 == 1, "TX must idle high"

    for bit_clks, payload in ((432, [0x55, 0xA3, 0x00, 0xFF, 0x3C, 0x81, 0x7E, 0x01, 0x80, 0xC5]),
                              (430, [0x12, 0x34, 0xFE, 0xDC, 0x00, 0xFF, 0xAA, 0x0F, 0xF0, 0x99,
                                     0x42, 0x24])):
        frames = []
        mon = cocotb.start_soon(watch_uart_tx(dut, frames))
        rx_end = []
        t0 = 0
        for byte in payload:
            await uart_send_frame(dut, byte, bit_clks)
            dut.ui_in.value = 1
            rx_end.append(t0)
            t0 += 10 * bit_clks
        dut.ui_in.value = 1
        await ClockCycles(dut.clk, 14 * 432)
        mon.cancel()
        got = [b for _, b in frames]
        assert got == payload, f"echo mismatch at {bit_clks} clocks/bit: {got} != {payload}"
        # Full duplex: the echo of byte n started before byte n+1 had finished arriving.
        starts = [s for s, _ in frames]
        gaps = [b - a for a, b in zip(starts, starts[1:])]
        assert max(gaps) <= 10 * 432 + 2 and min(gaps) >= 10 * 430 - 4 * 10, f"TX cadence {gaps}"
        dut._log.info(f"echo at {bit_clks} clks/bit: {len(got)} bytes back to back OK, TX frame gaps {gaps}")
        await ClockCycles(dut.clk, 3 * 432)


@cocotb.test()
async def test_echo_idle_gaps(dut):
    """Bytes with random idle gaps between them, including very long ones."""
    cocotb.start_soon(Clock(dut.clk, 20, unit="ns").start())
    await reset(dut, uio=ECHO_MODE, ui=1)
    rng = random.Random(11)
    frames = []
    cocotb.start_soon(watch_uart_tx(dut, frames))
    payload = [rng.randrange(256) for _ in range(12)]
    for byte in payload:
        await uart_send_frame(dut, byte, 432)
        dut.ui_in.value = 1
        await ClockCycles(dut.clk, rng.choice([0, 1, 50, 432, 3000, 6000]))
    await ClockCycles(dut.clk, 12 * 432)
    assert [b for _, b in frames] == payload


# ---- Threads: I2C master + SCL timing monitor ----------------------------------------------

MONITOR_IMAGE = None


async def drive_scl(dut, phases):
    """Drive uio_in[2] (SCL) through [(level, clocks), ...]."""
    for level, clks in phases:
        dut.uio_in.value = level << SCL
        await ClockCycles(dut.clk, clks)


async def monitor_case(dut, low, high):
    """Load the monitor alone, give it one SCL low phase of `low` clocks followed by one high phase of
    `high` clocks (each preceded and followed by long phases), and return SCL_VIOL."""
    await reset(dut)
    await load_program(dut, build_image([str(FW_DIR / "i2c_monitor.asm")]))
    await release_load(dut, uio=1 << SCL)
    await drive_scl(dut, [(1, 400), (0, low), (1, high), (0, 400), (1, 100)])
    return (int(dut.uo_out.value) >> 5) & 1


@cocotb.test()
async def test_scl_monitor_thresholds(dut):
    """The monitor thread on its own, on a bench-driven SCL: it must flag a clock that is too fast in
    either phase, and never flag one that meets the standard-mode minimums (4.7 us low, 4.0 us high)."""
    cocotb.start_soon(Clock(dut.clk, 20, unit="ns").start())
    results = {}
    for low, high, expect in [(240, 200, 0), (235, 200, 0), (232, 198, 0), (500, 300, 0),
                              (2000, 5000, 0),
                              (220, 400, 1), (200, 400, 1), (100, 400, 1), (24, 400, 1),
                              (400, 190, 1), (400, 150, 1), (400, 60, 1), (400, 8, 1),
                              (200, 150, 1)]:
        got = await monitor_case(dut, low, high)
        results[(low, high)] = got
        assert got == expect, f"SCL low {low} clocks, high {high} clocks: SCL_VIOL={got}, expected {expect}"
    # The flag is sticky and the reset clears it.
    await reset(dut)
    assert (int(dut.uo_out.value) >> 5) & 1 == 0


@cocotb.test()
async def test_i2c_monitor_quiet_on_good_traffic(dut):
    """The master's own transaction, and a slave stretching the clock, never trip the monitor."""
    cocotb.start_soon(Clock(dut.clk, 20, unit="ns").start())
    for stretch in (0, 900):
        await reset(dut, uio=I2C_IDLE_BUS)
        bus = I2CEeprom(dut, {0x42: 0x99, 0x07: 0x5C}, stretch=stretch)
        task = cocotb.start_soon(bus.run())
        for ptr, val in ((0x42, 0x99), (0x07, 0x5C)):
            got = await i2c_transaction(dut, bus, ptr)
            await wait_idle(dut)
            assert got == val
            assert (int(dut.uo_out.value) >> 5) & 1 == 0, "monitor flagged a legal clock"
        assert not bus.violations
        task.cancel()


@cocotb.test()
async def test_i2c_monitor_catches_rogue_slave(dut):
    """A slave that yanks SCL low 1.5 us after the master released it shortens the high phase below
    4.0 us; the monitor thread must catch it while the master carries on obliviously."""
    cocotb.start_soon(Clock(dut.clk, 20, unit="ns").start())
    await reset(dut, uio=I2C_IDLE_BUS)
    bus = I2CEeprom(dut, {0x42: 0x99})
    task = cocotb.start_soon(bus.run())
    flagged = []

    async def rogue():
        # Wait for the first SCL rise after the START, pull SCL low for 20 clocks 75 clocks into it.
        prev = 1
        seen_low = False
        while True:
            await RisingEdge(dut.clk)
            oe = int(dut.uio_oe.value)
            master_scl_low = (oe >> SCL) & 1
            if master_scl_low:
                seen_low = True
            if seen_low and prev == 1 and not master_scl_low:     # master released SCL
                await ClockCycles(dut.clk, 75)
                bus.rogue_low = True
                await ClockCycles(dut.clk, 20)      # a short glitch, back up before the master looks
                bus.rogue_low = False
                return
            prev = 0 if master_scl_low else 1

    bus.rogue_low = False
    orig_levels = bus.levels

    def levels():
        scl, sda = orig_levels()
        return (0 if bus.rogue_low else scl), sda
    bus.levels = levels
    cocotb.start_soon(rogue())
    try:
        await i2c_transaction(dut, bus, 0x42)       # the clipped clock may well corrupt the transfer
    except AssertionError:
        pass
    assert (int(dut.uo_out.value) >> 5) & 1 == 1, "monitor missed a 1.5 us SCL high phase"
    task.cancel()
