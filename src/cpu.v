/*
 * Protocol-emulator core: a deterministic four-thread ("barrel") I/O processor.
 *
 * Four hardware threads share one program memory and one set of pins. They take turns in a fixed
 * schedule (default: thread 0, 1, 2, 3, ..., one clock each), one instruction per turn, so a thread's
 * timing is exact and does not depend on what the other threads are doing. Each thread has its own
 * PC, A, X, C, stall counter, 4-deep return stack and 16 bytes of private data memory. The pins,
 * the open-drain controls and a one-byte blocking channel are shared. See docs/isa.md.
 *
 * Pipeline:
 *   fetch   clock k   : read the instruction of the thread that owns this slot from `fetch address`
 *   execute clock k+1 : run it, update that thread's registers (and shared state)
 * With the default schedule a thread's next fetch is three clocks after its execute, so it always
 * sees its updated PC. In a schedule where a thread owns two slots two clocks apart, its next fetch
 * address is computed during its own execute clock, so the new PC is forwarded (see fa_next).
 */

`default_nettype none

module cpu #(
    parameter [7:0] OUT_RESET = 8'h01  // output pin values during/after reset (TX idles high)
) (
    input  wire        clk,
    input  wire        rst_n,
    input  wire [7:0]  sched,      // slot -> thread: slot s runs thread sched[2s+:2]; no two
                                   // cyclically adjacent slots may name the same thread (project.v
                                   // checks this and falls back to 8'hE4 = 0,1,2,3)
    input  wire [15:0] pins_in,    // synchronised input pins
    output reg  [7:0]  pins_out,   // push-pull output pins
    output reg  [7:0]  od_pull,    // open-drain pins: 1 = pull the line low, 0 = released
    output wire [7:0]  rom_addr,   // fetch address
    output wire [127:0] rom_addr_rep, // 16 copies of rom_addr, one per instruction bit
    input  wire [15:0] rom_data    // instruction at rom_addr
`ifdef FORMAL
    ,  // observation ports for the formal proofs (formal/); not present in the chip
    output wire [31:0]  f_pc, f_a, f_x,
    output wire [3:0]   f_c, f_act,
    output wire [47:0]  f_wait,
    output wire [7:0]   f_sp, f_fa,
    output wire [127:0] f_stack,
    output wire [511:0] f_mem,
    output wire [15:0]  f_ir,
    output wire [1:0]   f_slot, f_ethr,
    output wire         f_evalid, f_mb_full
`endif
);

  // JPIN's pin-state is part of the opcode (0x5 = jump if pin high, 0x0 = jump if pin low),
  // which frees a bit so every address field can be 8 bits wide. Opcodes 4, 6, 7, 9, B and C are
  // families whose sub-opcode is instruction bits [11:8] (those bits were ignored in ISA v1).
  localparam [3:0] OP_JPIN0 = 4'h0, OP_PIN = 4'h1, OP_OUT = 4'h2, OP_WAIT = 4'h3,
                   OP_BR = 4'h4, OP_JPIN1 = 4'h5, OP_ACC = 4'h6, OP_REG = 4'h7,
                   OP_OUTC = 4'h8, OP_XI = 4'h9, OP_DJNZ = 4'hA, OP_WIDE = 4'hB,
                   OP_SHL = 4'hC, OP_INB = 4'hD, OP_CALL = 4'hE, OP_EXT = 4'hF;

  // Sub-opcodes of the extended family (instruction bits [11:8] when op == OP_EXT)
  localparam [3:0] EXT_HALT = 4'h0, EXT_RET = 4'h1, EXT_ODRV = 4'h2, EXT_ODRVC = 4'h3,
                   EXT_NOP = 4'h4, EXT_SEND = 4'h5, EXT_RECV = 4'h6, EXT_RETL = 4'h7,
                   EXT_MEM = 4'hC, EXT_JA = 4'hD;   // 4'h8..4'hB = SPAWN 0..3

  // ---- Per-thread state, packed: thread t lives in bits [width*t +: width] -----------------
  reg [31:0]  pc_q;      // program counters
  reg [31:0]  a_q;       // accumulators
  reg [31:0]  x_q;       // index / loop counters
  reg [3:0]   c_q;       // carry flags
  reg [47:0]  wait_q;    // remaining stall turns
  reg [7:0]   sp_q;      // return-stack pointers (2 bits each, circular)
  reg [127:0] stack_q;   // return stacks: entry i of thread t at bits [8*(4*t+i) +: 8]
  reg [511:0] dmem_q;    // data memory: byte i of thread t at bits [8*(16*t+i) +: 8]
  reg [3:0]   act_q;     // thread is running

  // ---- Shared state -------------------------------------------------------------------------
  reg [7:0]   mb;        // channel byte
  reg         mb_full;   // channel holds a byte

  // ---- Pipeline registers -------------------------------------------------------------------
  reg [1:0]   slot;      // schedule slot being fetched this clock
  reg [7:0]   fa;        // fetch address for the thread that owns `slot`
  reg [15:0]  ir;        // instruction being executed
  reg [1:0]   e_thr;     // its thread
  reg         e_valid;   // it belongs to a thread that was running when it was fetched

  assign rom_addr = fa;

  // ---- Execute stage: decode and thread-state selection ------------------------------------
  wire [1:0]  e      = e_thr;
  wire [3:0]  op     = ir[15:12];
  wire [3:0]  ext    = ir[11:8];                // sub-opcode of the families
  wire [7:0]  imm    = ir[7:0];
  wire [7:0]  pc_e   = pc_q[8*e +: 8];
  wire [7:0]  a_e    = a_q[8*e +: 8];
  wire [7:0]  x_e    = x_q[8*e +: 8];
  wire        c_e    = c_q[e];
  wire [11:0] wait_e = wait_q[12*e +: 12];
  wire [1:0]  sp_e   = sp_q[2*e +: 2];
  wire [1:0]  sp_m1  = sp_e - 2'd1;
  wire [7:0]  top_e  = stack_q[8*{e, sp_m1} +: 8];

  // data memory address: direct or X-indexed (wraps within the thread's 16 bytes)
  wire [3:0]  m_idx  = ir[6] ? (x_e[3:0] + ir[3:0]) : ir[3:0];
  wire [7:0]  m_rd   = dmem_q[8*{e, m_idx} +: 8];

  wire do_exec  = e_valid && (wait_e == 12'd0);
  wire is_spawn = (op == OP_EXT) && (ext[3:2] == 2'b10);
  wire [1:0] sp_t = ext[1:0];                       // SPAWN target thread
  // A running thread other than the caller cannot be respawned (the request is ignored).
  wire do_spawn = do_exec && is_spawn && ((sp_t == e) || !act_q[sp_t]);

  // ---- Shared 8-bit adder/subtractor (ADD, SUB, ADC, SBC, CMP, INC, DEC, ADDX, SUBX, CMPX, XADD, XCMP)
  wire acc_g = (op == OP_ACC);
  wire reg_g = (op == OP_REG);
  wire xi_g  = (op == OP_XI);
  wire sub_mode = acc_g ? (ext == 4'd2 || ext == 4'd6 || ext == 4'd8) :
                  reg_g ? (ext == 4'd4 || ext == 4'd6 || ext == 4'd12) :
                  xi_g  ? (ext == 4'd2) : 1'b0;
  wire use_c    = acc_g && (ext == 4'd7 || ext == 4'd8);          // ADC, SBC
  wire [7:0] add_a = xi_g ? x_e : a_e;
  wire [7:0] add_b = reg_g ? ((ext == 4'd3 || ext == 4'd4) ? 8'd1 : x_e) : imm;
  wire cin_raw = use_c & c_e;
  wire add_cin = sub_mode ? ~cin_raw : cin_raw;
  wire [8:0] add_s = {1'b0, add_a} + {1'b0, sub_mode ? ~add_b : add_b} + {8'd0, add_cin};
  wire [7:0] add_r = add_s[7:0];
  wire add_c = sub_mode ? ~add_s[8] : add_s[8];                   // carry out, or borrow for subtracts

  // ---- Execute stage: what the instruction does (all combinational) ---------------------------
  // Defaults hold the current state, so the clocked block below can write everything back.
  reg [7:0]  npc;                  // next PC of the executing thread
  reg [7:0]  a_n, x_n;
  reg        c_n;
  reg [7:0]  pins_n, od_n;
  reg [11:0] wait_n;
  reg        halt_r, push_r, pop_r, mem_w, mb_wr, mb_rd;
  reg [7:0]  stk_d;                // value pushed on the return stack

  always @* begin
    npc    = pc_e + 8'd1;
    a_n    = a_e;
    x_n    = x_e;
    c_n    = c_e;
    pins_n = pins_out;
    od_n   = od_pull;
    wait_n = wait_e;
    halt_r = 1'b0;
    push_r = 1'b0;
    pop_r  = 1'b0;
    mem_w  = 1'b0;
    mb_wr  = 1'b0;
    mb_rd  = 1'b0;
    stk_d  = pc_e + 8'd1;
    case (op)
      // JPIN: pin = ir[10:7], target = {ir[11], ir[6:0]}
      OP_JPIN1: if (pins_in[ir[10:7]] == 1'b1) npc = {ir[11], ir[6:0]};
      OP_JPIN0: if (pins_in[ir[10:7]] == 1'b0) npc = {ir[11], ir[6:0]};
      OP_PIN:   pins_n[ir[10:8]] = ir[0];
      OP_OUT:   pins_n = imm;
      OP_WAIT:  wait_n = ir[11:0];
      OP_BR: begin                   // JMP and the conditional branches
        case (ext)
          4'd0: npc = imm;
          4'd1: if (c_e) npc = imm;
          4'd2: if (!c_e) npc = imm;
          4'd3: if (a_e == 8'd0) npc = imm;
          4'd4: if (a_e != 8'd0) npc = imm;
          4'd5: if (a_e[7]) npc = imm;
          4'd6: if (!a_e[7]) npc = imm;
          4'd7: if (a_e == x_e) npc = imm;
          4'd8: if (a_e != x_e) npc = imm;
          default: ;
        endcase
      end
      OP_ACC: begin
        case (ext)
          4'd0: a_n = imm;                                   // LDA
          4'd1, 4'd2, 4'd7, 4'd8: begin a_n = add_r; c_n = add_c; end   // ADD SUB ADC SBC
          4'd3: a_n = a_e & imm;
          4'd4: a_n = a_e | imm;
          4'd5: a_n = a_e ^ imm;
          4'd6: c_n = add_c;                                 // CMP
          default: ;
        endcase
      end
      OP_REG: begin
        case (ext)
          4'd0: begin c_n = a_e[0]; a_n = {1'b0, a_e[7:1]}; end   // SHR
          4'd1: begin c_n = a_e[0]; a_n = {c_e, a_e[7:1]}; end    // ROR
          4'd2: begin c_n = a_e[7]; a_n = {a_e[6:0], c_e}; end    // ROL
          4'd3, 4'd4: a_n = add_r;                                // INC DEC
          4'd5: a_n = ~a_e;                                       // NOT
          4'd6: c_n = add_c;                                      // CMPX
          4'd7: a_n = {a_e[3:0], a_e[7:4]};                       // SWAP
          4'd8: x_n = a_e;                                        // TAX
          4'd9: a_n = x_e;                                        // TXA
          4'd10: begin a_n = x_e; x_n = a_e; end                  // XCHG
          4'd11, 4'd12: begin a_n = add_r; c_n = add_c; end       // ADDX SUBX
          4'd13: a_n = a_e & x_e;
          4'd14: a_n = a_e | x_e;
          4'd15: a_n = a_e ^ x_e;
        endcase
      end
      OP_OUTC:  pins_n[ir[10:8]] = c_e;
      OP_XI: begin
        case (ext)
          4'd0: x_n = imm;                                   // LDX
          4'd1: x_n = add_r;                                 // XADD
          4'd2: c_n = add_c;                                 // XCMP
          default: ;
        endcase
      end
      OP_DJNZ: begin
        x_n = x_e - 8'd1;
        if (x_e != 8'd1) npc = imm;
      end
      OP_WIDE: begin
        case (ext)
          4'd0: a_n = pins_in[7:0];                          // IN
          4'd1: a_n = pins_in[15:8];                         // INH
          4'd2: pins_n = a_e;                                // OUTA
          4'd3: od_n = ~a_e;                                 // ODRVA
          default: ;
        endcase
      end
      OP_SHL: if (ext == 4'd0) begin c_n = a_e[7]; a_n = {a_e[6:0], 1'b0}; end
      OP_INB:   a_n = {a_e[7:1], pins_in[ir[10:7]]};         // A[0] = input pin
      OP_CALL: begin
        push_r = 1'b1;
        npc    = imm;
      end
      OP_EXT: begin
        case (ext)
          EXT_HALT: begin
            halt_r = 1'b1;               // this thread stops; the others keep running
            npc    = pc_e;
          end
          EXT_RET: begin
            pop_r = 1'b1;
            npc   = top_e;
          end
          EXT_RETL: begin
            pop_r = 1'b1;
            npc   = top_e;
            a_n   = imm;
          end
          // ODRV p v : v=0 pulls open-drain pin p low, v=1 releases it
          EXT_ODRV:  od_n[ir[2:0]] = ~ir[3];
          // ODRVC p  : pulls pin p low if C=0, releases it if C=1
          EXT_ODRVC: od_n[ir[2:0]] = ~c_e;
          // Channel: SEND waits for it to be empty, RECV waits for a byte
          EXT_SEND: begin
            if (mb_full) npc = pc_e;
            else mb_wr = 1'b1;
          end
          EXT_RECV: begin
            if (!mb_full) npc = pc_e;
            else begin
              a_n   = mb;
              mb_rd = 1'b1;
            end
          end
          EXT_MEM: begin
            if (ir[7]) mem_w = 1'b1;     // STM / STMX
            else a_n = m_rd;             // LDM / LDMX
          end
          EXT_JA: begin                  // JMPA / CALLA
            npc    = a_e;
            push_r = ir[0];
          end
          default: ;  // NOP, SPAWN (below) and undefined sub-opcodes
        endcase
      end
      default: ;
    endcase
  end

  // ---- Fetch stage: address of the thread that will be fetched next clock -------------------
  wire [1:0] slot_n  = slot + 2'd1;
  wire [1:0] ft      = sched[2*slot   +: 2];   // thread fetched this clock
  wire [1:0] ftn     = sched[2*slot_n +: 2];   // thread fetched next clock
  // The PC of that thread is stable (only its own execute changes it) except when
  //  - a SPAWN is starting it right now: the new address is bypassed in, or
  //  - the thread is the one executing this clock (it owns two slots two clocks apart): the PC
  //    it is about to write is bypassed in.
  wire [7:0] pc_e_new = do_exec ? npc : pc_e;
  wire [7:0] fa_next = (do_spawn && sp_t == ftn) ? imm :
                       (e_valid && e == ftn)     ? pc_e_new :
                                                   pc_q[8*ftn +: 8];

  genvar g;
  generate
    for (g = 0; g < 16; g = g + 1) begin : g_rep
      pc_rep u_rep (
          .clk  (clk),
          .rst_n(rst_n),
          .d    (fa_next),
          .q    (rom_addr_rep[8*g+:8])
      );
    end
  endgenerate

  always @(posedge clk) begin
    if (!rst_n) begin
      pc_q     <= 32'd0;
      a_q      <= 32'd0;
      x_q      <= 32'd0;
      c_q      <= 4'd0;
      wait_q   <= 48'd0;
      sp_q     <= 8'd0;
      stack_q  <= 128'd0;
      dmem_q   <= 512'd0;
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
      e_thr   <= ft;
      e_valid <= act_q[ft];

      // execute stage
      if (e_valid) begin
        if (wait_e != 12'd0) begin
          wait_q[12*e +: 12] <= wait_e - 12'd1;
        end else begin
          pc_q[8*e +: 8]   <= npc;
          a_q[8*e +: 8]    <= a_n;
          x_q[8*e +: 8]    <= x_n;
          c_q[e]           <= c_n;
          pins_out         <= pins_n;
          od_pull          <= od_n;
          wait_q[12*e +: 12] <= wait_n;
          if (halt_r) act_q[e] <= 1'b0;
          if (push_r) begin
            stack_q[8*{e, sp_e} +: 8] <= stk_d;
            sp_q[2*e +: 2]            <= sp_e + 2'd1;
          end
          if (pop_r) sp_q[2*e +: 2] <= sp_m1;
          if (mem_w) dmem_q[8*{e, m_idx} +: 8] <= a_e;
          if (mb_wr) begin
            mb      <= a_e;
            mb_full <= 1'b1;
          end
          if (mb_rd) mb_full <= 1'b0;
        end
      end

      // SPAWN t addr: (re)start thread t at addr with cleared registers. Last, so that it wins
      // over the executing thread's own updates when a thread spawns itself.
      if (do_spawn) begin
        pc_q[8*sp_t +: 8]  <= imm;
        a_q[8*sp_t +: 8]   <= 8'd0;
        x_q[8*sp_t +: 8]   <= 8'd0;
        c_q[sp_t]          <= 1'b0;
        wait_q[12*sp_t +: 12] <= 12'd0;
        sp_q[2*sp_t +: 2]  <= 2'd0;
        act_q[sp_t]        <= 1'b1;
      end
    end
  end

`ifdef FORMAL
  assign f_pc = pc_q;  assign f_a = a_q;  assign f_x = x_q;  assign f_c = c_q;
  assign f_act = act_q;  assign f_wait = wait_q;  assign f_sp = sp_q;  assign f_fa = fa;
  assign f_stack = stack_q;  assign f_mem = dmem_q;  assign f_ir = ir;
  assign f_slot = slot;  assign f_ethr = e_thr;
  assign f_evalid = e_valid;  assign f_mb_full = mb_full;
`endif
endmodule
