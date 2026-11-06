// Simulation-only ROM for random differential testing: same ports as the generated rom.v,
// but the program is loaded from a hex file given on the command line (+prog=<file>).
`default_nettype none

module rom (
    input  wire [7:0]  addr,
    output wire [15:0] data
);
  reg [15:0] mem [0:255];
  reg [1023:0] fname;
  integer i;
  initial begin
    for (i = 0; i < 256; i = i + 1) mem[i] = 16'hF000;  // HALT
    if ($value$plusargs("prog=%s", fname)) $readmemh(fname, mem);
  end
  assign data = mem[addr];
endmodule
