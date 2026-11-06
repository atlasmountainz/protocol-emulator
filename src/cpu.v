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
    output reg  [7:0]  pins_out,   // push-pull output pins
    output reg  [7:0]  od_pull,    // open-drain pins: 1 = pull the line low, 0 = released
    // Program memory interface. The instruction at `pc` is held in the register `ir`; the next
    // instruction is fetched in the same clock from BOTH possible places, the next address and
    // the jump target, and the one that is actually next is selected at the end of the cycle.
    output wire [7:0]  seq_addr,    // pc + 1
    input  wire [15:0] seq_data,    // instruction at seq_addr
    output wire [7:0]  tgt_addr,    // jump / call / return target (0 during reset)
    input  wire [15:0] tgt_data     // instruction at tgt_addr
);

  // JPIN's pin-state is part of the opcode (0x5 = jump if pin high, 0x0 = jump if pin low),
  // which frees a bit so every address field can be 8 bits wide.
  localparam [3:0] OP_JPIN0 = 4'h0, OP_PIN = 4'h1, OP_OUT = 4'h2, OP_WAIT = 4'h3,
                   OP_JMP = 4'h4, OP_JPIN1 = 4'h5, OP_LDA = 4'h6, OP_SHR = 4'h7,
                   OP_OUTC = 4'h8, OP_LDX = 4'h9, OP_DJNZ = 4'hA, OP_IN = 4'hB,
                   OP_SHL = 4'hC, OP_INB = 4'hD, OP_CALL = 4'hE, OP_EXT = 4'hF;

  // Sub-opcodes of the extended family (ir[11:8] when op == OP_EXT)
  localparam [3:0] EXT_HALT = 4'h0, EXT_RET = 4'h1, EXT_ODRV = 4'h2, EXT_ODRVC = 4'h3,
                   EXT_NOP = 4'h4;

  reg [7:0]  pc;
  reg [15:0] ir;        // instruction at pc (always equal to program_memory[pc])
  reg [7:0]  a;         // accumulator / shift register
  reg [7:0]  x;         // loop counter
  reg        c;         // carry: last bit shifted out of A
  reg [11:0] wait_cnt;  // remaining stall cycles
  reg        halted;

  // Return-address stack: 4 entries, circular. Nesting deeper than 4 overwrites the oldest
  // entry. Abandoning a call without RET (e.g. jumping to an error handler) is harmless
  // because only the relative position of the stack pointer matters.
  reg [7:0] stack [0:3];
  reg [1:0] sp;
  integer   i;

  wire [3:0] op  = ir[15:12];
  wire [3:0] ext = ir[11:8];

  assign seq_addr = pc + 8'd1;

  // Where this instruction goes next if it jumps, and whether it does.
  wire [7:0] stack_top = stack[sp - 2'd1];
  reg        take;
  reg  [7:0] tgt;
  always @* begin
    take = 1'b0;
    tgt  = ir[7:0];
    case (op)
      OP_JMP, OP_CALL: take = 1'b1;
      OP_JPIN1: begin
        take = pins_in[ir[10:7]];
        tgt  = {ir[11], ir[6:0]};
      end
      OP_JPIN0: begin
        take = ~pins_in[ir[10:7]];
        tgt  = {ir[11], ir[6:0]};
      end
      OP_DJNZ: take = (x != 8'd1);
      OP_EXT: if (ext == EXT_RET) begin
        take = 1'b1;
        tgt  = stack_top;
      end
      default: ;
    endcase
  end
  assign tgt_addr = rst_n ? tgt : 8'd0;

  always @(posedge clk) begin
    if (!rst_n) begin
      pc       <= 8'd0;
      ir       <= tgt_data;      // program_memory[0]; re-sampled every clock while in reset
      a        <= 8'd0;
      x        <= 8'd0;
      c        <= 1'b0;
      wait_cnt <= 12'd0;
      halted   <= 1'b0;
      sp       <= 2'd0;
      for (i = 0; i < 4; i = i + 1) stack[i] <= 8'd0;  // deterministic RET even before any CALL
      pins_out <= OUT_RESET;
      od_pull  <= 8'd0;
    end else if (wait_cnt != 12'd0) begin
      wait_cnt <= wait_cnt - 1'b1;
    end else if (!halted) begin
      if (op == OP_EXT && ext == EXT_HALT) begin
        halted <= 1'b1;            // pc and ir stay put
      end else begin
        pc <= take ? tgt : seq_addr;
        ir <= take ? tgt_data : seq_data;
      end
      case (op)
        OP_PIN:  pins_out[ir[10:8]] <= ir[0];
        OP_OUT:  pins_out <= ir[7:0];
        OP_WAIT: wait_cnt <= ir[11:0];
        OP_LDA:  a <= ir[7:0];
        OP_SHR: begin
          c <= a[0];
          a <= {1'b0, a[7:1]};
        end
        OP_OUTC: pins_out[ir[10:8]] <= c;
        OP_LDX:  x <= ir[7:0];
        OP_DJNZ: x <= x - 1'b1;
        OP_IN:   a <= pins_in[7:0];
        OP_SHL: begin
          c <= a[7];
          a <= {a[6:0], 1'b0};
        end
        OP_INB:  a <= {a[7:1], pins_in[ir[10:7]]};  // A[0] = input pin
        OP_CALL: begin
          stack[sp] <= seq_addr;
          sp        <= sp + 1'b1;
        end
        OP_EXT: begin
          case (ext)
            EXT_RET: sp <= sp - 1'b1;
            // ODRV p v : v=0 pulls open-drain pin p low, v=1 releases it
            EXT_ODRV:  od_pull[ir[2:0]] <= ~ir[3];
            // ODRVC p  : pulls pin p low if C=0, releases it if C=1
            EXT_ODRVC: od_pull[ir[2:0]] <= ~c;
            default: ;  // EXT_HALT (handled above), EXT_NOP and undefined sub-opcodes
          endcase
        end
        default: ;  // JMP, JPIN and undefined opcodes: only the control flow above
      endcase
    end
  end

endmodule
