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
  mutant)   # A deliberately broken CPU in which a thread's shift result depends on the NEXT thread's
            # program counter. Every one of these runs must report FAILED, otherwise the proof is vacuous.
            sed "s/a_q\[8\*e +: 8\]   <= {1'b0, a_e\[7:1\]};/a_q[8*e +: 8]   <= {1'b0, a_e[7:1]} ^ {7'b0, pc_q[8*(e+2'd1)]};/" src/cpu.v > "$OUT/cpu_mutant.v"
            cmp -s src/cpu.v "$OUT/cpu_mutant.v" && { echo "mutation did not apply"; exit 1; }
            CPU="$OUT/cpu_mutant.v"; BASE_ONLY=1
            for T in 0 1 2 3; do iso $T mut_t; done ;;
  all)      prove pipeline pipeline formal/pipeline.v
            for T in 0 1 2 3; do iso $T iso_t; done ;;
esac
