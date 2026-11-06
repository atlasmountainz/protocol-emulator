/*
 * Copyright (c) 2026
 * SPDX-License-Identifier: Apache-2.0
 */

`default_nettype none

// First milestone: a fixed UART transmitter on the Tiny Tapeout pins.
//   ui_in[7:0]  : byte to send
//   uio_in[0]   : start (rising edge begins a transfer)
//   uo_out[0]   : TX line
//   uo_out[1]   : busy
module tt_um_atlasmountainz_protocol_emulator (
    input  wire [7:0] ui_in,    // Dedicated inputs
    output wire [7:0] uo_out,   // Dedicated outputs
    input  wire [7:0] uio_in,   // IOs: Input path
    output wire [7:0] uio_out,  // IOs: Output path
    output wire [7:0] uio_oe,   // IOs: Enable path (active high: 0=input, 1=output)
    input  wire       ena,      // always 1 when the design is powered, so you can ignore it
    input  wire       clk,      // clock
    input  wire       rst_n     // reset_n - low to reset
);

  // Rising-edge detect on the start pin so a held-high pin sends only one byte.
  reg start_q;
  always @(posedge clk) begin
    if (!rst_n) start_q <= 1'b0;
    else start_q <= uio_in[0];
  end
  wire start_pulse = uio_in[0] & ~start_q;

  wire tx, busy;

  uart_tx #(
      .CLKS_PER_BIT(434)  // 50 MHz / 115200 baud
  ) u_tx (
      .clk  (clk),
      .rst_n(rst_n),
      .start(start_pulse),
      .data (ui_in),
      .tx   (tx),
      .busy (busy)
  );

  assign uo_out  = {6'b0, busy, tx};
  assign uio_out = 8'b0;
  assign uio_oe  = 8'b0;

  // List all unused inputs to prevent warnings
  wire _unused = &{ena, uio_in[7:1], 1'b0};

endmodule
