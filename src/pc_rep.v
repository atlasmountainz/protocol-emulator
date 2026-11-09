/*
 * A copy of the low 7 bits of the fetch address, kept separate so it cannot be merged with the
 * others. The program-memory read (128:1 mux per bit) needs the address on thousands of mux
 * selects; one flop driving them through a long chain of buffers was the critical path of an
 * earlier version of the design. Each of the 16 copies drives one bit slice of the memory.
 *
 * keep_hierarchy stops synthesis from noticing that the copies are identical and merging them.
 */

`default_nettype none

(* keep_hierarchy *)
module pc_rep (
    input  wire       clk,
    input  wire       rst_n,
    input  wire [6:0] d,
    output reg  [6:0] q
);
  always @(posedge clk) begin
    if (!rst_n) q <= 7'd0;
    else q <= d;
  end
endmodule
