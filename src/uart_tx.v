/*
 * Simple 8N1 UART transmitter.
 * Idle line is high. Frame: start bit (0), 8 data bits LSB first, stop bit (1).
 * Pulse `start` for one clock while `busy` is low to send `data`.
 */

`default_nettype none

module uart_tx #(
    parameter CLKS_PER_BIT = 434  // e.g. 50 MHz clock / 115200 baud
) (
    input  wire       clk,
    input  wire       rst_n,
    input  wire       start,  // one-clock pulse to begin a transfer
    input  wire [7:0] data,   // byte to send (sampled when start is accepted)
    output wire       tx,
    output wire       busy
);

  localparam [1:0] S_IDLE = 2'd0, S_START = 2'd1, S_DATA = 2'd2, S_STOP = 2'd3;
  localparam CNT_W = (CLKS_PER_BIT > 1) ? $clog2(CLKS_PER_BIT) : 1;

  reg [1:0]       state;
  reg [CNT_W-1:0] clk_cnt;
  reg [2:0]       bit_idx;
  reg [7:0]       shreg;
  reg             tx_r;

  wire bit_done = (clk_cnt == CLKS_PER_BIT - 1);

  always @(posedge clk) begin
    if (!rst_n) begin
      state   <= S_IDLE;
      clk_cnt <= 0;
      bit_idx <= 0;
      shreg   <= 8'h00;
      tx_r    <= 1'b1;
    end else begin
      case (state)
        S_IDLE: begin
          tx_r    <= 1'b1;
          clk_cnt <= 0;
          bit_idx <= 0;
          if (start) begin
            shreg <= data;
            state <= S_START;
            tx_r  <= 1'b0;  // start bit begins immediately
          end
        end

        S_START: begin
          tx_r <= 1'b0;
          if (bit_done) begin
            clk_cnt <= 0;
            state   <= S_DATA;
            tx_r    <= shreg[0];
          end else begin
            clk_cnt <= clk_cnt + 1'b1;
          end
        end

        S_DATA: begin
          if (bit_done) begin
            clk_cnt <= 0;
            if (bit_idx == 3'd7) begin
              state <= S_STOP;
              tx_r  <= 1'b1;
            end else begin
              bit_idx <= bit_idx + 1'b1;
              shreg   <= {1'b0, shreg[7:1]};
              tx_r    <= shreg[1];
            end
          end else begin
            clk_cnt <= clk_cnt + 1'b1;
          end
        end

        S_STOP: begin
          tx_r <= 1'b1;
          if (bit_done) begin
            clk_cnt <= 0;
            state   <= S_IDLE;
          end else begin
            clk_cnt <= clk_cnt + 1'b1;
          end
        end

        default: state <= S_IDLE;
      endcase
    end
  end

  assign tx   = tx_r;
  assign busy = (state != S_IDLE);

endmodule
