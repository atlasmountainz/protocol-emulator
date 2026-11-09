/*
 * Writable program memory: 256 words x 16 bits, loaded over the pins (see project.v).
 *
 * Reads are combinational like the ROM. There is no reset: after power-up the contents are
 * undefined until the host loads a full image (tools/load_image.py pads the image with HALT up to
 * 256 words, so a program that runs off its end stops the CPU exactly as it does in the ROM).
 */

`default_nettype none

module pmem (
    input  wire        clk,
    input  wire        we,
    input  wire [7:0]  waddr,
    input  wire [15:0] wdata,
    input  wire [7:0]  raddr,
    input  wire [127:0] raddr_rep,   // 16 copies of raddr, one per bit slice (fan-out)
    output wire [15:0] rdata
);
  // One 256-bit column per instruction bit, each read with its own copy of the address.
  genvar j;
  generate
    for (j = 0; j < 16; j = j + 1) begin : g_col
      reg [255:0] col;
      always @(posedge clk) begin
        if (we) col[waddr] <= wdata[j];
      end
      assign rdata[j] = col[raddr_rep[8*j+:8]];
    end
  endgenerate
  wire _unused = &{raddr, 1'b0};
endmodule
