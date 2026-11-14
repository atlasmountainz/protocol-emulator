#!/bin/bash
# Run the formal proofs with Yosys + yosys-smtbmc + z3 (no SymbiYosys needed).
#   formal/run.sh            # pipeline invariants + thread isolation for threads 0..3
#   formal/run.sh mutant     # prove the proof can fail: run isolation against a deliberately broken CPU
# Needs: yosys, yosys-smtbmc, z3.
set -e
cd "$(dirname "$0")/.."
OUT=${FORMAL_OUT:-/tmp/formal_out}
mkdir -p "$OUT"
CPU=${CPU:-src/cpu.v}
DEPTH=${DEPTH:-12}

prove() {   # prove <name> <top module> <extra yosys read args...>   (BASE_ONLY=1: counterexample search only)
  name=$1; top=$2; shift 2
  yosys -q -p "read_verilog -formal -DFORMAL $CPU src/pc_rep.v $* ; \
               prep -top $top ; flatten ; memory_map -rom-only ; async2sync ; dffunmap ; \
               write_smt2 -wires $OUT/$name.smt2" 2>&1 | grep -v -i warning || true
  echo "== $name: base case (from reset / assumed start) + induction, depth $DEPTH"
  yosys-smtbmc -s z3 -t $DEPTH --dump-vcd "$OUT/$name.vcd" "$OUT/$name.smt2" | grep -E "Status|Reached|Assert|failed|passed" | tail -3
  [ -n "$BASE_ONLY" ] && return 0
  yosys-smtbmc -s z3 -i -t $DEPTH --dump-vcd "$OUT/$name.ind.vcd" "$OUT/$name.smt2" | grep -E "Status|Reached|Assert|failed|passed|PASSED|FAILED" | tail -3
}

MODE=${1:-all}

iso() {  # iso <T> <name-prefix>
  sed "s/parameter T = 0 /parameter T = $1 /" formal/isolation.v > "$OUT/iso_t$1.v"
  prove $2$1 isolation "$OUT/iso_t$1.v"
}

case "$MODE" in
  pipeline) prove pipeline pipeline formal/pipeline.v ;;
  iso)      for T in 0 1 2 3; do iso $T iso_t; done ;;
  iso[0-3]) iso ${MODE#iso} iso_t ;;      # one thread only (so the four can run in parallel)
  mutant)   # Deliberately broken CPUs. Every one of these runs must report FAILED, otherwise the proofs are vacuous.
            #  M1: a thread's shift result depends on the NEXT thread's program counter  (isolation, 4 threads)
            #  M2: a data-memory load reads the NEXT thread's memory                      (isolation, 4 threads)
            #  M3: the PC forwarding path for a thread owning two slots is removed        (pipeline invariants)
            mut() {  # mut <name> <sed expression>
              sed "$2" src/cpu.v > "$OUT/cpu_$1.v"
              cmp -s src/cpu.v "$OUT/cpu_$1.v" && { echo "mutation $1 did not apply"; exit 1; }
              return 0
            }
            mut m1 "s/a_n = {1'b0, a_e\\[7:1\\]};/a_n = {1'b0, a_e[7:1]} ^ {7'b0, pc_q[8*(e ^ 2'd1)]};/"
            mut m2 "s/wire \\[7:0\\]  m_rd   = dmem_q\\[8\\*{e, m_idx} +: 8\\];/wire [7:0]  m_rd   = dmem_q[8*{e ^ 2'd1, m_idx} +: 8];/"
            mut m3 "s/(e_valid \\&\\& e == ftn)     ? pc_e_new :/(e_valid \\&\\& e == ftn)     ? pc_e :/"
            BASE_ONLY=1
            CPU="$OUT/cpu_m1.v"; for T in 0 1 2 3; do iso $T mut1_t; done
            CPU="$OUT/cpu_m2.v"; for T in 0 1 2 3; do iso $T mut2_t; done
            CPU="$OUT/cpu_m3.v"; prove mut3_pipeline pipeline formal/pipeline.v ;;
  all)      prove pipeline pipeline formal/pipeline.v
            for T in 0 1 2 3; do iso $T iso_t; done ;;
esac
