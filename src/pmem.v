/*
 * Writable program memory: 128 words x 16 bits, loaded over the pins (see project.v).
 *
 * Reads are combinational like the ROM. Words 128-255 read as HALT so that the program counter
 * running off the end of the image stops the CPU, exactly as it does in the ROM.
 * There is no reset: after power-up the contents are undefined until the host loads a full
 * image (tools/load_image.py pads the image with HALT up to 128 words).
 */

`default_nettype none

module pmem (
    input  wire        clk,
    input  wire        we,
    input  wire [6:0]  waddr,
    input  wire [15:0] wdata,
    input  wire [7:0]  raddr,
    input  wire [111:0] raddr_rep,   // 16 copies of raddr[6:0], one per bit slice (fan-out)
    output wire [15:0] rdata
);
  // One 128-bit column per instruction bit, each read with its own copy of the address.
  genvar j;
  generate
    for (j = 0; j < 16; j = j + 1) begin : g_col
      reg [127:0] col;
      always @(posedge clk) begin
        if (we) col[waddr] <= wdata[j];
      end
      assign rdata[j] = raddr[7] ? (j >= 12) : col[raddr_rep[7*j+:7]];
    end
  endgenerate
endmodule
