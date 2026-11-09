/*
 * Protocol-emulator core: a deterministic four-thread ("barrel") I/O processor.
 *
 * Four hardware threads share one program memory and one set of pins. They take turns in a fixed
 * order, one instruction each every fourth clock, so a thread's timing is exact and does not
 * depend on what the other threads are doing. Each thread has its own PC, A, X, C, stall counter
 * and 4-deep return stack. The pins, the open-drain controls and a one-byte blocking channel are
 * shared. See docs/isa.md.
 *
 * Pipeline (no hazards, because consecutive instructions belong to different threads):
 *   fetch   clock k   : read the instruction of thread `slot` from `fetch address`
 *   execute clock k+1 : run it, update that thread's registers (and shared state)
 * A thread's next fetch is three clocks after its execute, so it always sees its updated PC.
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
    output wire [7:0]  rom_addr,   // fetch address
    output wire [111:0] rom_addr_rep, // 16 copies of rom_addr[6:0], one per instruction bit
    input  wire [15:0] rom_data    // instruction at rom_addr
);

  // JPIN's pin-state is part of the opcode (0x5 = jump if pin high, 0x0 = jump if pin low),
  // which frees a bit so every address field can be 8 bits wide.
  localparam [3:0] OP_JPIN0 = 4'h0, OP_PIN = 4'h1, OP_OUT = 4'h2, OP_WAIT = 4'h3,
                   OP_JMP = 4'h4, OP_JPIN1 = 4'h5, OP_LDA = 4'h6, OP_SHR = 4'h7,
                   OP_OUTC = 4'h8, OP_LDX = 4'h9, OP_DJNZ = 4'hA, OP_IN = 4'hB,
                   OP_SHL = 4'hC, OP_INB = 4'hD, OP_CALL = 4'hE, OP_EXT = 4'hF;

  // Sub-opcodes of the extended family (instruction bits [11:8] when op == OP_EXT)
  localparam [3:0] EXT_HALT = 4'h0, EXT_RET = 4'h1, EXT_ODRV = 4'h2, EXT_ODRVC = 4'h3,
                   EXT_NOP = 4'h4, EXT_SEND = 4'h5, EXT_RECV = 4'h6;   // 4'h8..4'hB = SPAWN 0..3

  // ---- Per-thread state, packed: thread t lives in bits [width*t +: width] -----------------
  reg [31:0]  pc_q;      // program counters
  reg [31:0]  a_q;       // accumulators / shift registers
  reg [31:0]  x_q;       // loop counters
  reg [3:0]   c_q;       // carry flags
  reg [47:0]  wait_q;    // remaining stall slots
  reg [7:0]   sp_q;      // return-stack pointers (2 bits each, circular)
  reg [127:0] stack_q;   // return stacks: entry i of thread t at bits [8*(4*t+i) +: 8]
  reg [3:0]   act_q;     // thread is running

  // ---- Shared state -------------------------------------------------------------------------
  reg [7:0]   mb;        // channel byte
  reg         mb_full;   // channel holds a byte

  // ---- Pipeline registers -------------------------------------------------------------------
  reg [1:0]   slot;      // thread being fetched this clock
  reg [7:0]   fa;        // fetch address for `slot`
  reg [15:0]  ir;        // instruction being executed
  reg [1:0]   e_thr;     // its thread
  reg         e_valid;   // it belongs to a thread that was running when it was fetched

  assign rom_addr = fa;

  // ---- Execute stage: decode and thread-state selection ------------------------------------
  wire [1:0]  e      = e_thr;
  wire [3:0]  op     = ir[15:12];
  wire [3:0]  ext    = ir[11:8];
  wire [7:0]  pc_e   = pc_q[8*e +: 8];
  wire [7:0]  a_e    = a_q[8*e +: 8];
  wire [7:0]  x_e    = x_q[8*e +: 8];
  wire        c_e    = c_q[e];
  wire [11:0] wait_e = wait_q[12*e +: 12];
  wire [1:0]  sp_e   = sp_q[2*e +: 2];
  wire [1:0]  sp_m1  = sp_e - 2'd1;
  wire [7:0]  top_e  = stack_q[8*{e, sp_m1} +: 8];

  wire do_exec  = e_valid && (wait_e == 12'd0);
  wire is_spawn = (op == OP_EXT) && (ext[3:2] == 2'b10);
  wire [1:0] sp_t = ext[1:0];                       // SPAWN target thread
  // A running thread other than the caller cannot be respawned (the request is ignored).
  wire do_spawn = do_exec && is_spawn && ((sp_t == e) || !act_q[sp_t]);

  // ---- Fetch stage: address of the thread that will be fetched next clock -------------------
  // Its PC is stable (only its own execute changes it, and that is not this clock) except
  // when a SPAWN is starting it right now, in which case the new address is bypassed in.
  wire [1:0] slot_n  = slot + 2'd1;
  wire [7:0] fa_next = (do_spawn && sp_t == slot_n) ? ir[7:0] : pc_q[8*slot_n +: 8];

  genvar g;
  generate
    for (g = 0; g < 16; g = g + 1) begin : g_rep
      pc_rep u_rep (
          .clk  (clk),
          .rst_n(rst_n),
          .d    (fa_next[6:0]),
          .q    (rom_addr_rep[7*g+:7])
      );
    end
  endgenerate

  reg [7:0] npc;

  always @(posedge clk) begin
    if (!rst_n) begin
      pc_q     <= 32'd0;
      a_q      <= 32'd0;
      x_q      <= 32'd0;
      c_q      <= 4'd0;
      wait_q   <= 48'd0;
      sp_q     <= 8'd0;
      stack_q  <= 128'd0;
      act_q    <= 4'b0001;          // only thread 0 runs after reset
      mb       <= 8'd0;
      mb_full  <= 1'b0;
      pins_out <= OUT_RESET;
      od_pull  <= 8'd0;
      slot     <= 2'd0;
      fa       <= 8'd0;
      ir       <= 16'd0;
      e_thr    <= 2'd0;
      e_valid  <= 1'b0;
    end else begin
      // fetch stage
      slot    <= slot_n;
      fa      <= fa_next;
      ir      <= rom_data;
      e_thr   <= slot;
      e_valid <= act_q[slot];

      // execute stage
      if (e_valid) begin
        if (wait_e != 12'd0) begin
          wait_q[12*e +: 12] <= wait_e - 12'd1;
        end else begin
          npc = pc_e + 8'd1;
          case (op)
            OP_JMP:   npc = ir[7:0];
            // JPIN: pin = ir[10:7], target = {ir[11], ir[6:0]}
            OP_JPIN1: if (pins_in[ir[10:7]] == 1'b1) npc = {ir[11], ir[6:0]};
            OP_JPIN0: if (pins_in[ir[10:7]] == 1'b0) npc = {ir[11], ir[6:0]};
            OP_PIN:   pins_out[ir[10:8]] <= ir[0];
            OP_OUT:   pins_out <= ir[7:0];
            OP_WAIT:  wait_q[12*e +: 12] <= ir[11:0];
            OP_LDA:   a_q[8*e +: 8] <= ir[7:0];
            OP_SHR: begin
              c_q[e]          <= a_e[0];
              a_q[8*e +: 8]   <= {1'b0, a_e[7:1]};
            end
            OP_OUTC:  pins_out[ir[10:8]] <= c_e;
            OP_LDX:   x_q[8*e +: 8] <= ir[7:0];
            OP_DJNZ: begin
              x_q[8*e +: 8] <= x_e - 8'd1;
              if (x_e != 8'd1) npc = ir[7:0];
            end
            OP_IN:    a_q[8*e +: 8] <= pins_in[7:0];
            OP_SHL: begin
              c_q[e]          <= a_e[7];
              a_q[8*e +: 8]   <= {a_e[6:0], 1'b0};
            end
            OP_INB:   a_q[8*e +: 8] <= {a_e[7:1], pins_in[ir[10:7]]};  // A[0] = input pin
            OP_CALL: begin
              stack_q[8*{e, sp_e} +: 8] <= pc_e + 8'd1;
              sp_q[2*e +: 2]            <= sp_e + 2'd1;
              npc = ir[7:0];
            end
            OP_EXT: begin
              case (ext)
                EXT_HALT: begin
                  act_q[e] <= 1'b0;          // this thread stops; the others keep running
                  npc = pc_e;
                end
                EXT_RET: begin
                  sp_q[2*e +: 2] <= sp_m1;
                  npc = top_e;
                end
                // ODRV p v : v=0 pulls open-drain pin p low, v=1 releases it
                EXT_ODRV:  od_pull[ir[2:0]] <= ~ir[3];
                // ODRVC p  : pulls pin p low if C=0, releases it if C=1
                EXT_ODRVC: od_pull[ir[2:0]] <= ~c_e;
                // Channel: SEND waits for it to be empty, RECV waits for a byte
                EXT_SEND: begin
                  if (mb_full) npc = pc_e;
                  else begin
                    mb      <= a_e;
                    mb_full <= 1'b1;
                  end
                end
                EXT_RECV: begin
                  if (!mb_full) npc = pc_e;
                  else begin
                    a_q[8*e +: 8] <= mb;
                    mb_full       <= 1'b0;
                  end
                end
                default: ;  // NOP, SPAWN (below) and undefined sub-opcodes
              endcase
            end
            default: ;
          endcase
          pc_q[8*e +: 8] <= npc;
        end
      end

      // SPAWN t addr: (re)start thread t at addr with cleared registers. Last, so that it wins
      // over the executing thread's own updates when a thread spawns itself.
      if (do_spawn) begin
        pc_q[8*sp_t +: 8]  <= ir[7:0];
        a_q[8*sp_t +: 8]   <= 8'd0;
        x_q[8*sp_t +: 8]   <= 8'd0;
        c_q[sp_t]          <= 1'b0;
        wait_q[12*sp_t +: 12] <= 12'd0;
        sp_q[2*sp_t +: 2]  <= 2'd0;
        act_q[sp_t]        <= 1'b1;
      end
    end
  end
endmodule
