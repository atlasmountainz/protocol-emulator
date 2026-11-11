/*
 * Formal harness: pipeline invariants of the real cpu.v, proved for every state reachable from reset.
 *
 *   I1  an instruction being executed belongs to the thread that was fetched in the previous clock
 *   I2  the fetch address always equals the program counter of the thread being fetched
 *   I3  the instruction register holds the instruction at that thread's program counter
 *   I4  the schedule is rigid: the slot counter advances by one every clock
 *
 * "The thread being fetched" is whatever thread the schedule table names for the slot; the table is an
 * arbitrary legal schedule (an $anyconst, no thread in two adjacent slots), so the proof covers the default
 * schedule 0,1,2,3 and every faster one, including those that need the PC forwarding path.
 *
 * Together these say the 2-stage pipeline never loses or reorders anything, which is what makes
 * a thread's timing exact. isolation.v uses I1..I3 as its starting assumptions; this file shows
 * they hold in every state the chip can actually be in (reset may arrive at any time).
 */
`default_nettype none

module pipeline (
    input wire        clk,
    input wire        rst_n,
    input wire [15:0] pins_in
);
  // The program memory: 256 words, each an arbitrary value that never changes ($anyconst).
  wire [4095:0] rom_flat;
  genvar gi;
  generate
    for (gi = 0; gi < 256; gi = gi + 1) begin : g_rom
      (* anyconst *) wire [15:0] w;
      assign rom_flat[16*gi +: 16] = w;
    end
  endgenerate
  (* anyconst *) wire [7:0] sched;            // slot -> thread table
  wire [7:0]  rom_addr;
  wire [15:0] rom_data = rom_flat[16*(rom_addr) +: 16];
  wire [7:0]  po, od;
  wire [31:0] pc, a, x;
  wire [3:0]  c, act;
  wire [47:0] wt;
  wire [7:0]  sp, fa;
  wire [127:0] stack;
  wire [511:0] mem;
  wire [15:0] ir;
  wire [1:0]  slot, ethr;
  wire        ev, mbf;

  cpu dut (.clk(clk), .rst_n(rst_n), .sched(sched), .pins_in(pins_in), .pins_out(po), .od_pull(od),
           .rom_addr(rom_addr), .rom_addr_rep(), .rom_data(rom_data),
           .f_pc(pc), .f_a(a), .f_x(x), .f_c(c), .f_act(act), .f_wait(wt), .f_sp(sp),
           .f_fa(fa), .f_stack(stack), .f_mem(mem), .f_ir(ir), .f_slot(slot), .f_ethr(ethr),
           .f_evalid(ev), .f_mb_full(mbf));

  reg first = 1'b1;
  reg [1:0] slot_prev;
  reg       slot_prev_ok = 1'b0;
  always @(posedge clk) begin
    first        <= 1'b0;
    slot_prev    <= slot;
    slot_prev_ok <= rst_n;      // slot_prev is meaningful only after a clock with reset released
  end

  wire sched_ok = sched[1:0] != sched[3:2] && sched[3:2] != sched[5:4] &&
                  sched[5:4] != sched[7:6] && sched[7:6] != sched[1:0];
  wire [1:0] sched_ft = sched[2*slot +: 2];            // thread fetched this clock
  wire [1:0] slot_m1  = slot - 2'd1;                    // (2 bits: wraps, unlike a bare slot - 1 in an index)
  wire [1:0] sched_et = sched[2*slot_m1 +: 2];          // thread fetched in the previous clock

  always @* begin
    assume (sched_ok);                                        // the hardware never runs an illegal schedule
    if (first) assume (!rst_n);                               // the chip starts in reset
    // (state at step 0 is arbitrary until the first clock edge applies the reset)
  end
  always @* if (!first) assert (!ev || ethr == sched_et);                       // I1
  always @* if (!first) assert (fa == pc[8*sched_ft+:8]);                       // I2
  always @* if (!first) assert (!ev || ir == rom_flat[16*(pc[8*ethr+:8]) +: 16]);   // I3
  always @* if (!first && slot_prev_ok) assert (slot == slot_prev + 2'd1);      // I4
endmodule
