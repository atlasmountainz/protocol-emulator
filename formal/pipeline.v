/*
 * Formal harness: pipeline invariants of the real cpu.v, proved for every state reachable from reset.
 *
 *   I1  an instruction being executed belongs to the thread that was fetched in the previous clock
 *   I2  the fetch address always equals the program counter of the thread being fetched
 *   I3  the instruction register holds the instruction at that thread's program counter
 *   I4  the schedule is a rigid round robin: the slot counter advances by one every clock
 *
 * Together these say the 2-stage pipeline never loses or reorders anything, which is what makes
 * a thread's timing exact. isolation.v uses I1..I3 as its starting assumptions; this file shows
 * they hold in every state the chip can actually be in (reset may arrive at any time).
 */
`default_nettype none

module pipeline (
    input wire        clk,
    input wire        rst_n,
    input wire [15:0] pins_in,
    input wire        keep_mem        // never set (assumed 0); only stops tools from folding rom into a constant ROM
);
  reg [15:0] rom[0:255];
  always @(posedge clk) if (keep_mem) rom[0] <= 16'd0;
  wire [7:0]  rom_addr;
  wire [15:0] rom_data = rom[rom_addr];
  wire [7:0]  po, od;
  wire [31:0] pc, a, x;
  wire [3:0]  c, act;
  wire [47:0] wt;
  wire [7:0]  sp, fa;
  wire [127:0] stack;
  wire [15:0] ir;
  wire [1:0]  slot, ethr;
  wire        ev, mbf;

  cpu dut (.clk(clk), .rst_n(rst_n), .pins_in(pins_in), .pins_out(po), .od_pull(od),
           .rom_addr(rom_addr), .rom_addr_rep(), .rom_data(rom_data),
           .f_pc(pc), .f_a(a), .f_x(x), .f_c(c), .f_act(act), .f_wait(wt), .f_sp(sp),
           .f_fa(fa), .f_stack(stack), .f_ir(ir), .f_slot(slot), .f_ethr(ethr),
           .f_evalid(ev), .f_mb_full(mbf));

  reg first = 1'b1;
  reg [1:0] slot_prev;
  reg       slot_prev_ok = 1'b0;
  always @(posedge clk) begin
    first        <= 1'b0;
    slot_prev    <= slot;
    slot_prev_ok <= rst_n;      // slot_prev is meaningful only after a clock with reset released
  end

  always @* begin
    assume (!keep_mem);
    if (first) assume (!rst_n);                               // the chip starts in reset
    // (state at step 0 is arbitrary until the first clock edge applies the reset)
    if (!first) begin
      assert (!ev || ethr == slot - 2'd1);                    // I1
      assert (fa == pc[8*slot+:8]);                           // I2
      assert (!ev || ir == rom[pc[8*ethr+:8]]);               // I3
      if (slot_prev_ok) assert (slot == slot_prev + 2'd1);    // I4
    end
  end
endmodule
