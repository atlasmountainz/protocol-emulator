#!/usr/bin/env python3
"""Run the RTL-vs-model differential test over many random programs.

Usage: python3 random_run.py [--ram] [NUM_SEEDS] [FIRST_SEED]

--ram loads each program into the chip's loadable program memory over the pins (first 128 words)
instead of using the simulation-only ROM.

Each seed makes a random program and random pin stimulus, runs it on the RTL (Icarus) and on
test/isa_model.py, and compares every architectural register on every clock edge. Fails if any
seed diverges, or if some instruction / branch outcome was never exercised across all seeds.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from isa_model import gen_program, write_hex  # noqa: E402

REQUIRED = ["PIN", "OUT", "WAIT", "JMP", "JPIN1_taken", "JPIN1_fall", "JPIN0_taken", "JPIN0_fall",
            "LDA", "SHR", "OUTC", "LDX", "DJNZ_taken", "DJNZ_exit", "IN", "SHL", "INB", "CALL",
            "RET", "ODRV", "ODRVC", "NOP", "HALT", "SEND_ok", "SEND_block", "RECV_ok", "RECV_block",
            "SPAWN_start", "SPAWN_self", "SPAWN_ignored"]


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
        cmd = ["make", "-C", str(HERE), "COCOTB_TEST_MODULES=random_test",
               f"ROM_SRC={rom_src}", "SIM_BUILD=sim_build/rand", f"PLUSARGS=+prog={hexf}",
               f"COCOTB_RESULTS_FILE={work}/results_{seed}.xml"]
        r = subprocess.run(cmd, env=env, capture_output=True, text=True)
        ok = r.returncode == 0 and "FAIL=0" in r.stdout
        if not ok:
            failed.append(seed)
            print(f"seed {seed}: FAIL")
            print("\n".join(l for l in r.stdout.splitlines() if "MISMATCH" in l or "rror" in l)[:1500])
        else:
            print(f"seed {seed}: ok")
        if cov.exists():
            for k, v in json.loads(cov.read_text()).items():
                total[k] = total.get(k, 0) + v

    missing = [k for k in REQUIRED if total.get(k, 0) == 0]
    print("\nCoverage (times executed across all seeds):")
    for k in REQUIRED:
        print(f"  {k:12s} {total.get(k, 0)}")
    print(f"\n{n - len(failed)}/{n} seeds matched the model")
    if missing:
        print("NOT EXERCISED:", ", ".join(missing))
    sys.exit(1 if failed or missing else 0)


if __name__ == "__main__":
    main()
