# SPDX-License-Identifier: Apache-2.0
"""Differential test: RTL vs the Python ISA model on a random program with random pin inputs.

Not part of the default `make` run; use `python3 random_run.py` (it sets PROG_HEX / PROG_SEED).
"""
import json
import os
import random
from pathlib import Path

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, Timer

from isa_model import Model, STATE_NAMES

CYCLES = int(os.environ.get("RAND_CYCLES", "6000"))


def load_hex(path):
    return [int(line, 16) for line in Path(path).read_text().split()]


@cocotb.test()
async def test_random_program(dut):
    seed = int(os.environ["PROG_SEED"])
    rom = load_hex(os.environ["PROG_HEX"])
    model = Model(rom)
    rng = random.Random(seed ^ 0x5EED)

    cocotb.start_soon(Clock(dut.clk, 20, unit="ns").start())
    cpu = dut.user_project.u_cpu

    # Reset (the model resets with it)
    dut.ena.value = 1
    dut.rst_n.value = 0
    dut.ui_in.value = 0
    dut.uio_in.value = 0
    for _ in range(4):
        await RisingEdge(dut.clk)
    model.reset()
    dut.rst_n.value = 1

    # Pins change with different "stickiness" per run so both busy loops and fast exits occur
    hold = rng.choice([1, 1, 2, 5, 20])
    ui = uio = 0
    mismatches = 0
    for cyc in range(CYCLES):
        if cyc % hold == 0:
            ui, uio = rng.getrandbits(8), rng.getrandbits(8)
        dut.ui_in.value = ui
        dut.uio_in.value = uio
        await RisingEdge(dut.clk)
        model.clock((uio << 8) | ui)
        await Timer(1, unit="ns")              # let the post-edge state settle

        got = (int(cpu.pc.value), int(cpu.a.value), int(cpu.x.value), int(cpu.c.value),
               int(cpu.wait_cnt.value), int(cpu.halted.value), int(cpu.sp.value),
               int(dut.uo_out.value), int(dut.uio_oe.value))
        exp = model.state()
        if got != exp:
            diff = ", ".join(f"{n}: rtl={g:#x} model={e:#x}"
                             for n, g, e in zip(STATE_NAMES, got, exp) if g != e)
            dut._log.error(f"seed {seed} cycle {cyc}: MISMATCH {diff}")
            mismatches += 1
            if mismatches >= 3:
                break

    Path(os.environ.get("COV_OUT", f"cov_{seed}.json")).write_text(json.dumps(model.cov))
    assert mismatches == 0, f"seed {seed}: RTL and model diverged"
