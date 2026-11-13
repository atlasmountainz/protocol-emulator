; CAN 2.0A node, receive side, 500 kbit/s: receives standard (11-bit) frames, removes the stuff bits, checks the
; CRC-15 and acknowledges good frames in the ACK slot. With ui[7] high it only listens (no ACK).
; An example of firmware loaded into the program memory after fabrication; it is NOT in the ROM.
; Build the image with the schedule 0,1,0,1 (threads 0 and 1 run one instruction per 2 clocks = 40 ns, so one
; bit of 2 us is exactly 50 turns):
;     python3 tools/load_image.py --sched 0,1,0,1 build/can_rx firmware/examples/can_rx.asm
; (see docs/examples-swd-can.md). It needs a CAN transceiver between the bus and the pins.
;
; Pins   ui[0] = RXD from the transceiver (1 = recessive).   ui[7] = 1: listen only.
;        uio[3] = TXD to the transceiver, open drain with a pull-up: pulled low for one bit = the ACK.
;        uo[7:0] = report byte, valid while uio[0] (BYTE) or uio[1] (STATUS) is pulled low (open drain, 160 ns).
;        uio[2] = EVENT and uio[6] = LAST, internal flags between the two threads (open drain, they need pull-ups).
; Report One BYTE strobe per byte, then one STATUS strobe per frame:
;          byte 0 = ID[10:9]   byte 1 = ID[8:1]   byte 2 = ID[0], RTR, IDE, r0, DLC[3:0]
;          the data bytes (DLC of them, at most 8, none in a remote frame)      last byte = CRC[14:7]
;        STATUS: 0 = good frame (CRC right; acknowledged unless listen-only), 1 = CRC error, 2 = six dominant bits
;        (an error flag or a stuffing error), 3 = six recessive bits where the frame should have gone on.
;
; Thread 0 is the line receiver. A bit is not sampled in its middle: its value follows from whether the level
; changes at the bit boundary. The thread polls RXD with 13 JPIN in a row around the place where the boundary is
; expected (so +-6 turns = +-240 ns). An edge in the window: the new bit has the other level, and the next window
; starts exactly 44 turns after the poll that saw the edge, which re-aligns the receiver to the transmitter at every
; edge (both directions; stuffing guarantees one at least every 5 bits). No edge: the same level again, and the next
; window starts exactly 50 turns after this one. The bus level is the program counter (one window per level).
; X counts the equal bits in a row: after five, the next bit is a stuff bit and is dropped; a sixth equal bit ends the
; frame. Every bit goes through the CRC-15 (polynomial 0x4599, kept shifted left by one in two memory cells) and into
; an 8-bit chunk. The first chunk after the start bit is one bit long, so that the last chunk ends with the last CRC
; bit. Chunks go to thread 1 through the mailbox.
; When thread 1 has pulled LAST low, the chunk that just ended was the last one of the frame: if the CRC register is
; zero, thread 0 waits for the ACK slot (one bit later, two if a stuff bit follows the CRC) and pulls TXD low for
; exactly one bit.
; All of this takes the same number of turns on every path, which tools/timing.py proves (firmware/timing/can_rx.timing).
;
; Thread 1 is the protocol: it turns the chunks (which are one bit off the byte boundaries of the frame) into the
; report bytes, reads the DLC, and pulls LAST low when the next chunk will be the last.
;
; Data memory, thread 0: 0 = chunk shift register with an end marker, 1 = CRC low, 2 = CRC high (CRC << 1)
;              thread 1: 0 = chunks so far, 1 = chunks to go (0 = not known yet), 3 = previous chunk, 4 = report byte

; ------------------------------------------------------------------------------------------ thread 0
cn_start:
        OUT  0x00
        ODRV 0 1              ; BYTE strobe released
        ODRV 1 1              ; STATUS strobe released
        ODRV 2 1              ; EVENT released
        ODRV 3 1              ; TXD recessive
        ODRV 6 1              ; LAST released
        SPAWN 1 t1_main
cn_wait_idle:                 ; 7 bit times of recessive bus (350 turns), then a dominant level is a start bit
        LDX  175
cn_wi1: JPIN 0 0 cn_wait_idle
        DJNZ cn_wi1
cn_listen:
        JPIN 0 1 cn_listen    ; recessive: idle
        LDA  0x80             ; start of frame. The first chunk is one bit long (ID10)
        STM  0
        LDA  0
        STM  1
        STM  2                ; CRC = 0 (the start bit does not change it)
        LDX  5                ; the start bit is the first of a run of dominant bits
        WAIT 35
        JMP  d_win

; ---- level recessive: wait for RXD to fall
r_win:  JPIN 0 0 r_edge
        JPIN 0 0 r_edge
        JPIN 0 0 r_edge
        JPIN 0 0 r_edge
        JPIN 0 0 r_edge
        JPIN 0 0 r_edge
        JPIN 0 0 r_edge
        JPIN 0 0 r_edge
        JPIN 0 0 r_edge
        JPIN 0 0 r_edge
        JPIN 0 0 r_edge
        JPIN 0 0 r_edge
        JPIN 0 0 r_edge
r_ne:   DJNZ r_ne1            ; no edge: one more recessive bit
        JMP  six_r            ; the sixth in a row
r_ne1:  CALL bit1
        WAIT 5
        JMP  r_win
r_edge: XCMP 2                ; X == 1: five recessive bits before this edge, so this is a stuff bit
        JC   r_drop
        LDX  5
        WAIT 3
        CALL bit0
        WAIT 5
        JMP  d_win
r_drop: LDX  5
        WAIT 38
        JMP  d_win

; ---- level dominant: wait for RXD to rise
d_win:  JPIN 0 1 d_edge
        JPIN 0 1 d_edge
        JPIN 0 1 d_edge
        JPIN 0 1 d_edge
        JPIN 0 1 d_edge
        JPIN 0 1 d_edge
        JPIN 0 1 d_edge
        JPIN 0 1 d_edge
        JPIN 0 1 d_edge
        JPIN 0 1 d_edge
        JPIN 0 1 d_edge
        JPIN 0 1 d_edge
        JPIN 0 1 d_edge
d_ne:   DJNZ d_ne1
        JMP  six_d
d_ne1:  CALL bit0
        WAIT 5
        JMP  d_win
d_edge: XCMP 2
        JC   d_drop
        LDX  5
        WAIT 3
        CALL bit1
        WAIT 5
        JMP  r_win
d_drop: LDX  5
        WAIT 38
        JMP  r_win

; ---- one bit of the frame: CRC, then into the chunk. Both entries and all paths that return take 29 turns
;      (CALL and RET included).
bit1:   LDM  2
        XOR  0x80             ; the bit goes into the top of the CRC register
        STM  2
        CALL crc
        LDM  0
        SHL                   ; C = the end marker when the chunk is complete
        INC                   ; the bit
        JMP  bend
bit0:   WAIT 2
        CALL crc
        LDM  0
        SHL
        WAIT 1
bend:   STM  0
        JC   chunk
        WAIT 3
        RET
chunk:  SEND                  ; the chunk, to thread 1
        LDA  1
        STM  0                ; end marker for the next 8 bits
        JPIN 14 1 ch_ret      ; LAST (uio[6]) is not pulled low: the frame goes on
        LDM  1                ; that was the last CRC bit: the CRC register must be zero now
        JNZ  ev_crc
        LDM  2
        JNZ  ev_crc
        XCMP 2                ; five equal bits at the end of the CRC: a stuff bit comes first
        JNC  ack1
        WAIT 49
ack1:   WAIT 55               ; the rest of the last CRC bit and the CRC delimiter
        JPIN 7 1 ack2         ; listen only
        ODRV 3 0              ; ACK: dominant ...
ack2:   WAIT 48
        ODRV 3 1              ; ... for exactly one bit (50 turns)
        LDA  0                ; status 0: good frame
        JMP  ev_idle
ch_ret: RET

crc:    LDM  1                ; CRC register (16 bits, CRC << 1) one place to the left
        SHL
        STM  1
        LDM  2
        ROL                   ; C = the bit that leaves: CRC bit 14, already XORed with the data bit
        STM  2
        JNC  crc_n
        XOR  0x8B             ; polynomial 0x4599, shifted left by one: 0x8B32
        STM  2
        LDM  1
        XOR  0x32
        STM  1
        RET
crc_n:  WAIT 4
        RET

; ---- end of frame and errors: the EVENT flag and a status code in the mailbox
six_r:  LDA  3                ; six recessive bits: the frame ended where it should not
        CALL ev
        JMP  cn_listen        ; the bus is idle already
six_d:  LDA  2                ; six dominant bits: an error flag, or a stuffing error
        JMP  ev_idle
ev_crc: LDA  1                ; CRC error: no ACK
ev_idle:
        CALL ev
        JMP  cn_wait_idle
ev:     ODRV 2 0              ; the EVENT flag reaches thread 1 before the code does
        NOP
        NOP
        SEND
        RET

; ------------------------------------------------------------------------------------------ thread 1
t1_main:
        RECV
        JPIN 10 0 t1_event    ; EVENT (uio[2] read back) is low: A is a status code, not a chunk
        TAX
        SHL                   ; C = the top bit of this chunk
        LDM  3
        ROL                   ; the previous chunk and that bit: one byte of the frame
        STM  4
        TXA
        STM  3
        LDM  0
        INC
        STM  0
        DEC
        JZ   t1_main          ; the first chunk (one bit): nothing to report yet
        LDM  4
        OUTA                  ; report the byte
        ODRV 0 0
        WAIT 2
        ODRV 0 1
        LDM  0
        XOR  4
        JNZ  t1_cnt
        LDM  4                ; the fourth chunk completes ID[0], RTR, IDE, r0, DLC[3:0]
        AND  0x0F
        CMP  9
        JC   t1_dlc
        LDA  8                ; a DLC above 8 means 8 bytes
t1_dlc: TAX
        LDM  4
        AND  0x40             ; a remote frame has no data
        JZ   t1_len
        LDX  0
t1_len: TXA
        INC                   ; chunks to go: the data bytes and the last CRC chunk
        JMP  t1_set
t1_cnt: LDM  1
        JZ   t1_main          ; before the DLC, or after the end
        DEC
t1_set: STM  1
        XOR  1
        JNZ  t1_main
        ODRV 6 0              ; the next chunk is the last one
        JMP  t1_main

t1_event:
        ODRV 2 1              ; release EVENT
        ODRV 6 1              ; and LAST
        OUTA                  ; the status
        ODRV 1 0
        WAIT 2
        ODRV 1 1
        LDA  0
        STM  0
        STM  1
        JMP  t1_main
