/*
 * Formal harness: "a thread's behaviour does not depend on the other threads".
 *
 * Two copies (A and B) of the real cpu.v run side by side with the same input pins, the same
 * program memory contents and the same reset. At the start, thread T is in the same state in both
 * copies but the other three threads are in ARBITRARY, different states (different program
 * counters, registers, stalls, running or halted, even different channel contents). The proof is
 * that thread T stays identical in A and B forever: same PC, A, X, C, stall counter, return stack,
 * running flag, and the same instruction executing in the same clock cycle.
 *
 * Why this is the claim that matters: the other threads only reach thread T through shared
 * resources. The pins are inputs to T and are the same in both copies. The output pins and the
 * open-drain controls are outputs only, so they cannot affect any thread. The channel and SPAWN are
 * the two real interaction points, so the proof excludes exactly those: thread T is assumed not to
 * execute SEND or RECV, and no other thread is assumed to SPAWN T. Everything else, including
 * every possible behaviour of the other threads, is covered.
 *
 * It is an inductive proof (unbounded in time), run by formal/run.sh. The helper invariants I1..I3
 * (the pipeline registers agree with the thread state) are proved separately in
 * formal/pipeline.v for every state reachable from reset.
 */
`default_nettype none

module isolation #(
    parameter T = 0               // the thread under test (0..3); run.sh checks all four
) (
    input wire        clk,
    input wire        rst_n,
    input wire [15:0] pins_in,
    input wire        keep_mem        // never set (assumed 0); only stops tools from folding rom into a constant ROM
);
  // The program memory: unconstrained contents, constant over time, shared by both copies.
  reg [15:0] rom[0:255];
  always @(posedge clk) if (keep_mem) rom[0] <= 16'd0;

  wire [7:0]  fa_a, fa_b;
  wire [7:0]  rom_addr_a, rom_addr_b;
  wire [15:0] rom_data_a = rom[rom_addr_a];
  wire [15:0] rom_data_b = rom[rom_addr_b];
  wire [7:0]  po_a, po_b, od_a, od_b;

  wire [31:0] pc_a, a_a, x_a, pc_b, a_b, x_b;
  wire [3:0]  c_a, act_a, c_b, act_b;
  wire [47:0] wait_a, wait_b;
  wire [7:0]  sp_a, sp_b;
  wire [127:0] stack_a, stack_b;
  wire [15:0] ir_a, ir_b;
  wire [1:0]  slot_a, ethr_a, slot_b, ethr_b;
  wire        ev_a, ev_b, mbf_a, mbf_b;

  cpu cpu_a (.clk(clk), .rst_n(rst_n), .pins_in(pins_in), .pins_out(po_a), .od_pull(od_a),
             .rom_addr(rom_addr_a), .rom_addr_rep(), .rom_data(rom_data_a),
             .f_pc(pc_a), .f_a(a_a), .f_x(x_a), .f_c(c_a), .f_act(act_a), .f_wait(wait_a),
             .f_sp(sp_a), .f_fa(fa_a), .f_stack(stack_a), .f_ir(ir_a), .f_slot(slot_a),
             .f_ethr(ethr_a), .f_evalid(ev_a), .f_mb_full(mbf_a));
  cpu cpu_b (.clk(clk), .rst_n(rst_n), .pins_in(pins_in), .pins_out(po_b), .od_pull(od_b),
             .rom_addr(rom_addr_b), .rom_addr_rep(), .rom_data(rom_data_b),
             .f_pc(pc_b), .f_a(a_b), .f_x(x_b), .f_c(c_b), .f_act(act_b), .f_wait(wait_b),
             .f_sp(sp_b), .f_fa(fa_b), .f_stack(stack_b), .f_ir(ir_b), .f_slot(slot_b),
             .f_ethr(ethr_b), .f_evalid(ev_b), .f_mb_full(mbf_b));

  // ---- thread-T slices ----------------------------------------------------------------------
  wire [7:0]  tpc_a = pc_a[8*T+:8],  tpc_b = pc_b[8*T+:8];
  wire [7:0]  ta_a  = a_a[8*T+:8],   ta_b  = a_b[8*T+:8];
  wire [7:0]  tx_a  = x_a[8*T+:8],   tx_b  = x_b[8*T+:8];
  wire        tc_a  = c_a[T],        tc_b  = c_b[T];
  wire [11:0] tw_a  = wait_a[12*T+:12], tw_b = wait_b[12*T+:12];
  wire [1:0]  tsp_a = sp_a[2*T+:2],  tsp_b = sp_b[2*T+:2];
  wire [31:0] tst_a = stack_a[32*T+:32], tst_b = stack_b[32*T+:32];
  wire        tact_a = act_a[T],     tact_b = act_b[T];

  // ---- helper invariants of one copy (proved for all reachable states in pipeline.v) --------
  function inv;
    input [31:0] pc;
    input [7:0]  fa;
    input [15:0] ir;
    input [1:0]  slot, ethr;
    input        ev;
    input [15:0] romword;       // rom[pc of the thread being executed]
    begin
      inv = (!ev || ethr == slot - 2'd1)                     // I1: executes the thread fetched last clock
            && (fa == pc[8*slot+:8])                         // I2: fetch address is that thread's PC
            && (!ev || ir == romword);                       // I3: the instruction is the one at its PC
    end
  endfunction

  wire inv_a = inv(pc_a, fa_a, ir_a, slot_a, ethr_a, ev_a, rom[pc_a[8*ethr_a+:8]]);
  wire inv_b = inv(pc_b, fa_b, ir_b, slot_b, ethr_b, ev_b, rom[pc_b[8*ethr_b+:8]]);

  // ---- the relation that must hold forever ---------------------------------------------------
  wire t_same = tpc_a == tpc_b && ta_a == ta_b && tx_a == tx_b && tc_a == tc_b && tw_a == tw_b &&
                tsp_a == tsp_b && tst_a == tst_b && tact_a == tact_b;
  wire pipe_same = slot_a == slot_b && ethr_a == ethr_b &&
                   (ethr_a != T || (ev_a == ev_b && (!ev_a || ir_a == ir_b)));
  wire rel = t_same && pipe_same && inv_a && inv_b;

  // ---- environment assumptions (the two excluded interaction points) --------------------------
  function bad_ir;               // an instruction thread T must not execute / others must not issue
    input [15:0] ir;
    input        mine;           // executed by thread T itself
    begin
      bad_ir = mine ? (ir[15:12] == 4'hF && (ir[11:8] == 4'h5 || ir[11:8] == 4'h6))   // SEND / RECV
                    : (ir[15:12] == 4'hF && ir[11:8] == 4'h8 + T);                    // SPAWN T
    end
  endfunction

  wire run_a = ev_a && wait_a[12*ethr_a+:12] == 12'd0;       // an instruction really executes
  wire run_b = ev_b && wait_b[12*ethr_b+:12] == 12'd0;

  reg first = 1'b1;
  always @(posedge clk) first <= 1'b0;

  always @* begin
    if (first) assume (rel);                                  // start anywhere that satisfies rel
    assume (!(run_a && bad_ir(ir_a, ethr_a == T)));
    assume (!(run_b && bad_ir(ir_b, ethr_b == T)));
    assume (!keep_mem);
    assert (rel);
  end
endmodule
