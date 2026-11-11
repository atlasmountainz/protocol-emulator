#!/usr/bin/env python3
"""Run the RTL-vs-model differential test over many random programs.

Usage: python3 random_run.py [--ram] [NUM_SEEDS] [FIRST_SEED]

--ram loads each program into the chip's loadable program memory over the pins (all 256 words) instead
of using the simulation-only ROM, followed by a random thread schedule (gen_schedule(seed), byte 512 of
the load; about one seed in eight gets an illegal one, which the chip must ignore). Without --ram the
schedule is the default 0 1 2 3.

Each seed makes a random program and random pin stimulus, runs it on the RTL (Icarus) and on
test/isa_model.py, and compares every architectural register (and the data memory) on every clock edge.
Fails if any seed diverges, or if some instruction / branch outcome / carry or borrow outcome was never
exercised across all seeds.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from isa_model import gen_program, gen_schedule, valid_schedule, write_hex  # noqa: E402

REQUIRED = ["PIN", "OUT", "WAIT", "JMP", "JPIN1_taken", "JPIN1_fall", "JPIN0_taken", "JPIN0_fall",
            "LDA", "SHR", "OUTC", "LDX", "DJNZ_taken", "DJNZ_exit", "IN", "SHL", "INB", "CALL",
            "RET", "ODRV", "ODRVC", "NOP", "HALT", "SEND_ok", "SEND_block", "RECV_ok", "RECV_block",
            "SPAWN_start", "SPAWN_self", "SPAWN_ignored"]
# Version 2 (docs/isa.md): every instruction, both outcomes of every conditional branch, carry / borrow
# set and clear, wraparound of the indexed memory address, and the undefined sub-opcodes (= NOP).
REQUIRED += [f"{b}_{o}" for b in ("JC", "JNC", "JZ", "JNZ", "JMI", "JPL", "JEQ", "JNE")
             for o in ("taken", "fall")]
REQUIRED += ["ADD", "ADD_c0", "ADD_c1", "SUB", "SUB_b0", "SUB_b1", "AND", "OR", "XOR",
             "CMP", "CMP_b0", "CMP_b1",
             "ADC", "ADC_c0", "ADC_c1", "ADC_cin0", "ADC_cin1",
             "SBC", "SBC_b0", "SBC_b1", "SBC_cin0", "SBC_cin1",
             "ROR", "ROR_cin0", "ROR_cin1", "ROR_cout0", "ROR_cout1",
             "ROL", "ROL_cin0", "ROL_cin1", "ROL_cout0", "ROL_cout1",
             "INC", "INC_wrap", "DEC", "DEC_wrap", "NOT", "SWAP", "TAX", "TXA", "XCHG",
             "CMPX", "CMPX_b0", "CMPX_b1", "ADDX", "ADDX_c0", "ADDX_c1", "SUBX", "SUBX_b0", "SUBX_b1",
             "ANDX", "ORX", "XORX",
             "XADD", "XCMP", "XCMP_b0", "XCMP_b1",
             "INH", "OUTA", "ODRVA",
             "LDM", "STM", "LDMX", "STMX", "LDMX_wrap", "STMX_wrap",
             "JMPA", "CALLA", "RETL",
             "BR_undefined", "ACC_undefined", "XIMM_undefined", "WIDE_undefined", "EXT_undefined"]


def main():
    args = [a for a in sys.argv[1:] if a != "--ram"]
    ram = "--ram" in sys.argv[1:]
    n = int(args[0]) if len(args) > 0 else 40
    first = int(args[1]) if len(args) > 1 else 1
    work = HERE / "sim_build" / "random"
    work.mkdir(parents=True, exist_ok=True)
    rom_src = HERE / "rom_random.v"
    total, failed = {}, []

    for seed in range(first, first + n):
        hexf, cov = work / f"prog_{seed}.hex", work / f"cov_{seed}.json"
        write_hex(gen_program(seed), hexf)
        env = dict(os.environ, PROG_SEED=str(seed), PROG_HEX=str(hexf), COV_OUT=str(cov),
                   PYTHONPATH=str(HERE),
                   LOAD_RAM="1" if ram else "0")
        sched = gen_schedule(seed)
        if ram:
            env["PROG_SCHED"] = ",".join(str(t) for t in sched)
        else:
            env.pop("PROG_SCHED", None)         # the ROM firmware always runs the default schedule
        tag = (f" sched {sched}" + ("" if valid_schedule(sched) else " (illegal, default expected)")
               if ram else "")
        cmd = ["make", "-C", str(HERE), "COCOTB_TEST_MODULES=random_test",
               f"ROM_SRC={rom_src}", "SIM_BUILD=sim_build/rand", f"PLUSARGS=+prog={hexf}",
               f"COCOTB_RESULTS_FILE={work}/results_{seed}.xml"]
        r = subprocess.run(cmd, env=env, capture_output=True, text=True)
        ok = r.returncode == 0 and "FAIL=0" in r.stdout
        if not ok:
            failed.append(seed)
            print(f"seed {seed}: FAIL{tag}")
            print("\n".join(l for l in r.stdout.splitlines() if "MISMATCH" in l or "rror" in l)[:1500])
        else:
            print(f"seed {seed}: ok{tag}")
        if cov.exists():
            for k, v in json.loads(cov.read_text()).items():
                total[k] = total.get(k, 0) + v

    missing = [k for k in REQUIRED if total.get(k, 0) == 0]
    print("\nCoverage (times executed across all seeds):")
    for k in REQUIRED:
        print(f"  {k:14s} {total.get(k, 0)}")
    print(f"\n{n - len(failed)}/{n} seeds matched the model")
    if missing:
        print("NOT EXERCISED:", ", ".join(missing))
    sys.exit(1 if failed or missing else 0)


if __name__ == "__main__":
    main()
