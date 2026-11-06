/*
 * Writable program memory: 128 words x 16 bits, loaded over the pins (see project.v).
 *
 * Two combinational read ports (the CPU fetches the sequential and the jump-target instruction together). Words 128-255 read as HALT so that the program counter
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
    input  wire [7:0]  raddr0,
    output wire [15:0] rdata0,
    input  wire [7:0]  raddr1,
    output wire [15:0] rdata1
);
  reg [15:0] mem [0:127];

  always @(posedge clk) begin
    if (we) mem[waddr] <= wdata;
  end

  assign rdata0 = raddr0[7] ? 16'hF000 : mem[raddr0[6:0]];
  assign rdata1 = raddr1[7] ? 16'hF000 : mem[raddr1[6:0]];
endmodule
