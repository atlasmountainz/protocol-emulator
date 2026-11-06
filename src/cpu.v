/*
 * Protocol-emulator CPU core.
 *
 * Tiny, cycle-exact, single-issue core built for bit-banging protocols.
 * Every instruction takes exactly 1 clock, except WAIT n which takes 1+n.
 * See docs/isa.md for the instruction set.
 */

`default_nettype none

module cpu #(
    parameter [7:0] OUT_RESET = 8'h01  // output pin values during/after reset (TX idles high)
) (
    input  wire        clk,
    input  wire        rst_n,
    input  wire [15:0] pins_in,    // synchronised input pins
    output reg  [7:0]  pins_out,   // output pins
    output wire [6:0]  rom_addr,   // program counter
    input  wire [15:0] rom_data    // instruction at rom_addr
);

  localparam [3:0] OP_NOP = 4'h0, OP_PIN = 4'h1, OP_OUT = 4'h2, OP_WAIT = 4'h3,
                   OP_JMP = 4'h4, OP_JPIN = 4'h5, OP_LDA = 4'h6, OP_SHR = 4'h7,
                   OP_OUTC = 4'h8, OP_LDX = 4'h9, OP_DJNZ = 4'hA, OP_IN = 4'hB,
                   OP_HALT = 4'hF;

  reg [6:0]  pc;
  reg [7:0]  a;         // accumulator / shift register
  reg [7:0]  x;         // loop counter
  reg        c;         // carry: last bit shifted out of A
  reg [11:0] wait_cnt;  // remaining stall cycles
  reg        halted;

  assign rom_addr = pc;

  wire [3:0] op = rom_data[15:12];

  always @(posedge clk) begin
    if (!rst_n) begin
      pc       <= 7'd0;
      a        <= 8'd0;
      x        <= 8'd0;
      c        <= 1'b0;
      wait_cnt <= 12'd0;
      halted   <= 1'b0;
      pins_out <= OUT_RESET;
    end else if (wait_cnt != 12'd0) begin
      wait_cnt <= wait_cnt - 1'b1;
    end else if (!halted) begin
      pc <= pc + 1'b1;
      case (op)
        OP_PIN:  pins_out[rom_data[10:8]] <= rom_data[0];
        OP_OUT:  pins_out <= rom_data[7:0];
        OP_WAIT: wait_cnt <= rom_data[11:0];
        OP_JMP:  pc <= rom_data[6:0];
        OP_JPIN: if (pins_in[rom_data[10:7]] == rom_data[11]) pc <= rom_data[6:0];
        OP_LDA:  a <= rom_data[7:0];
        OP_SHR: begin
          c <= a[0];
          a <= {1'b0, a[7:1]};
        end
        OP_OUTC: pins_out[rom_data[10:8]] <= c;
        OP_LDX:  x <= rom_data[7:0];
        OP_DJNZ: begin
          x <= x - 1'b1;
          if (x != 8'd1) pc <= rom_data[6:0];
        end
        OP_IN:   a <= pins_in[7:0];
        OP_HALT: begin
          halted <= 1'b1;
          pc     <= pc;
        end
        default: ;  // NOP and undefined opcodes do nothing
      endcase
    end
  end

endmodule
