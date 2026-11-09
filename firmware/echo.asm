; Full-duplex UART echo: two threads, one byte mailbox between them.
; Mode select: uio_in[6] = 1 and uio_in[7] = 1 at reset.
;
;   thread 0 (receiver)    : watches RX, shifts a byte in, SENDs it into the channel
;   thread 1 (transmitter) : RECVs from the channel, shifts the byte out on TX
;
; Input pins : 0 = ui[0] (RX, idles high)
; Output pins: 0 = TX (idles high)
; 115200 baud at 50 MHz (108 slots = 432 clocks per bit), 8N1.
;
; Neither thread ever polls the other and neither has an interrupt: when the transmitter has
; nothing to send it sits in RECV, when the receiver has a byte but the transmitter is still busy
; it sits in SEND. Because the machine is a barrel, the two bit clocks are exact and
; independent of each other, so bytes can arrive back to back while the previous one is still
; being echoed.
;
; The receiver shifts with SHL / INB, so the first bit on the wire (the LSB) ends up in A[7]. The
; channel carries that wire order, and the transmitter shifts it out again with SHL / OUTC,
; so no bit reversal is needed anywhere.
;
; The transmitter's stop bit is 102 slots (94 % of a bit) so that its frame (1074 slots) is a little
; shorter than the receiver's (1080 slots): it can never fall behind a sender that is up to
; 0.5 % faster than nominal.

echo_start:
        SPAWN 1 echo_tx
echo_rx_idle:
        JPIN 0 1 echo_rx_idle ; spin while RX is high
        WAIT 157              ; to the middle of data bit 0
        LDX  8
echo_rx_bit:
        SHL
        INB  0                ; sample RX into A[0]
        WAIT 104
        DJNZ echo_rx_bit      ; (the last pass lands in the middle of the stop bit)
        SEND                  ; hand the byte to the transmitter (waits while it is still busy)
        JMP  echo_rx_idle

echo_tx:
        RECV                  ; A = byte in wire order; waits until the receiver has one
        PIN  0 0              ; start bit
        LDX  8
        WAIT 104
echo_tx_bit:
        SHL
        OUTC 0
        WAIT 104
        DJNZ echo_tx_bit
        NOP
        PIN  0 1              ; stop bit
        WAIT 99
        JMP  echo_tx
