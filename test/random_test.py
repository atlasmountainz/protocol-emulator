# SPDX-License-Identifier: Apache-2.0
"""Differential test: RTL vs the Python ISA model on a random program with random pin inputs.

Not part of the default `make` run; use `python3 random_run.py` (it sets PROG_HEX / PROG_SEED, and in
RAM mode PROG_SCHED, see below).

Compared after every clock edge: pc, A, X, C, WAIT counter, running flag and return-stack pointer of
every thread, the output pins, the open-drain pulls, the channel, and the 4 x 16 bytes of data memory
(cpu.dmem_q, a packed 512-bit register: byte i of thread t is bits [8*(16*t+i) +: 8]).

In RAM mode (LOAD_RAM=1) the program is loaded over the pins together with a thread schedule (byte 512,
see docs/loading.md): PROG_SCHED="a,b,c,d" (slot 0 first), default gen_schedule(PROG_SEED). About one in
eight generated schedules is illegal; the hardware must then run the default 0 1 2 3, so the model is
built with effective_schedule(). In ROM mode the schedule is the default.
"""
import json
import os
import random
from pathlib import Path

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, Timer

from isa_model import Model, STATE_NAMES, DEFAULT_SCHEDULE, effective_schedule, gen_schedule
from loader import load_program, LOAD, STROBE, RAM_WORDS

CYCLES = int(os.environ.get("RAND_CYCLES", "6000"))
# LOAD_RAM=1: send all 256 words over the pins into the program memory and run from there
# Otherwise the program is in the simulation-only ROM.
LOAD_RAM = os.environ.get("LOAD_RAM") == "1"
# PROG_SCHED="0,1,0,2": the schedule to load in RAM mode (default: gen_schedule(PROG_SEED)).
PROG_SCHED = os.environ.get("PROG_SCHED")


def load_hex(path):
    return [int(line, 16) for line in Path(path).read_text().split()]


@cocotb.test()
async def test_random_program(dut):
    seed = int(os.environ["PROG_SEED"])
    rom = load_hex(os.environ["PROG_HEX"])
    if LOAD_RAM:
        rom = rom[:RAM_WORDS]
    sched = None                               # the schedule that is loaded (RAM mode only)
    if LOAD_RAM:
        sched = (tuple(int(v) for v in PROG_SCHED.split(",")) if PROG_SCHED else gen_schedule(seed))
    model = Model(rom, effective_schedule(sched) if sched else DEFAULT_SCHEDULE)
    rng = random.Random(seed ^ 0x5EED)

    cocotb.start_soon(Clock(dut.clk, 20, unit="ns").start())
    cpu = dut.user_project.u_cpu
    dut._log.info(f"seed {seed}: loaded schedule {sched}, effective {tuple(model.sched)}")

    # Reset (the model resets with it)
    dut.ena.value = 1
    dut.rst_n.value = 0
    dut.ui_in.value = 0
    dut.uio_in.value = 0
    for _ in range(4):
        await RisingEdge(dut.clk)

    # Pins change with different "stickiness" per run so both busy loops and fast exits occur
    hold = rng.choice([1, 1, 2, 5, 20])
    ui = uio = 0
    # uio[5] is the loader's LOAD pin: while it is high the CPU is held in reset (and the program
    # memory is selected), so the random stimulus keeps it low. uio[4] (STROBE) is ignored when
    # LOAD is low and is left random.
    uio_mask = 0xDF

    if LOAD_RAM:
        dut.rst_n.value = 1
        await load_program(dut, rom + [0xF000] * (RAM_WORDS - len(rom)), rng, sched)
        # Release LOAD with the first stimulus pins p0, then p1. The CPU leaves reset when LOAD
        # has gone through the 2-flop synchroniser, i.e. its first live edge sees p0 on sync2.
        p0 = (rng.getrandbits(8), rng.getrandbits(8) & uio_mask)
        p1 = (rng.getrandbits(8), rng.getrandbits(8) & uio_mask)
        dut.ui_in.value, dut.uio_in.value = p0
        await RisingEdge(dut.clk)
        dut.ui_in.value, dut.uio_in.value = p1
        await RisingEdge(dut.clk)
        model.reset()
        model.sync2 = (p0[1] << 8) | p0[0]
        model.sync1 = (p1[1] << 8) | p1[0]
    else:
        model.reset()
        dut.rst_n.value = 1

    mismatches = 0
    for cyc in range(CYCLES):
        if cyc % hold == 0:
            ui, uio = rng.getrandbits(8), rng.getrandbits(8) & uio_mask
        dut.ui_in.value = ui
        dut.uio_in.value = uio
        await RisingEdge(dut.clk)
        model.clock((uio << 8) | ui)
        await Timer(1, unit="ns")              # let the post-edge state settle

        got = []
        for th in range(4):
            got += [(int(cpu.pc_q.value) >> (8 * th)) & 0xFF,
                    (int(cpu.a_q.value) >> (8 * th)) & 0xFF,
                    (int(cpu.x_q.value) >> (8 * th)) & 0xFF,
                    (int(cpu.c_q.value) >> th) & 1,
                    (int(cpu.wait_q.value) >> (12 * th)) & 0xFFF,
                    (int(cpu.act_q.value) >> th) & 1,
                    (int(cpu.sp_q.value) >> (2 * th)) & 3]
        got += [int(dut.uo_out.value), int(dut.uio_oe.value), int(cpu.mb.value),
                int(cpu.mb_full.value)]
        dmem = int(cpu.dmem_q.value)
        got += [(dmem >> (8 * (16 * th + i))) & 0xFF for th in range(4) for i in range(16)]
        got = tuple(got)
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
