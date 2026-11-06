# SPDX-License-Identifier: Apache-2.0

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import ClockCycles, RisingEdge

CLKS_PER_BIT = 434  # must match project.v


async def reset(dut, mode=0):
    """Reset the chip. `mode` is the firmware-select pin (uio_in[7]): 0 = UART, 1 = SPI."""
    dut.ena.value = 1
    dut.ui_in.value = 0
    dut.uio_in.value = mode << 7
    dut.rst_n.value = 0
    await ClockCycles(dut.clk, 10)
    dut.rst_n.value = 1
    await ClockCycles(dut.clk, 2)


async def send_and_capture(dut, byte):
    """Hold start with `byte` on ui_in, then sample the TX line mid-bit."""
    dut.ui_in.value = byte
    dut.uio_in.value = 1  # start pin high (level-triggered; firmware polls it)
    await ClockCycles(dut.clk, 8)
    dut.uio_in.value = 0

    # TX goes low (start bit) once the firmware has seen start
    for _ in range(40):
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


@cocotb.test()
async def test_cycle_exact_timing(dut):
    """The CPU must hit the baud rate exactly: every TX transition is a multiple of one
    bit time apart. 0x55 toggles TX on every bit, so all 9 gaps must be exactly 434."""
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

        # 16 bits x 20 cycles plus setup/hold; wait for the transaction to finish
        for _ in range(600):
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
