; Low-speed USB (1.5 Mbit/s) packet receiver: SYNC, bit unstuffing, PID check, CRC5 of tokens, CRC16 of data packets.
; An example of firmware loaded into the program memory after fabrication: it is NOT in the ROM.
; Build the image with the schedule 0,1,0,1 (byte 512 = 0x44), so that threads 0 and 1 run one instruction per
; 2 clocks (40 ns) and one USB bit (666.67 ns) is 16.67 turns:
;     python3 tools/load_image.py --sched 0,1,0,1 build/usb_ls_rx firmware/examples/usb_ls_rx.asm
; (see docs/usb-low-speed.md)
;
; Pins   ui[0] = D+, ui[1] = D- (inputs).
;        uo[7:0] = report byte, valid while uio[0] (BYTE) or uio[1] (STATUS) is pulled low (open drain, low for 4 turns = 160 ns).
;        uio[2] = EVENT, internal: thread 0 pulls it low to tell thread 1 that the next mailbox byte is not data.
; Report For every packet, one BYTE strobe per byte after SYNC (PID, payload, CRC, as received), then one STATUS strobe
;        after the end of the packet:  bit 0 SYNC wrong, 1 PID check wrong, 2 CRC wrong (tokens, data), 3 unused,
;        4 not a whole number of bytes, 5 bit-stuffing violation (packet abandoned), 7:6 = PID bits 1:0
;        (1 token, 3 data, 2 handshake, 0 special).  Status 0x00 with type bits only (0x40, 0xC0, 0x80) = a good packet.
;
; Thread 0 is the line receiver. It samples nothing: it watches the line for an edge in a window of nine
; consecutive JPIN instructions around the place where the next bit boundary is expected. An edge in the window is
; a 0 (NRZI toggles on 0), no edge is a 1, and the edge also re-aligns the window (one clock per bit, no PLL).
; After an edge the next window starts 13 turns later, after a bit without an edge 9 turns after the last poll
; (17 turns per bit; the real period is 16.67, so long runs of 1s drift by up to 0.33 turn per bit, which is why a
; stuffed 0 is needed at least every 7 bits anyway). The bus level, J or K, is the program counter: one copy of the
; window per level. A is the byte shift register with a sentinel bit that leaves into C after 8 bits; X counts
; down the 1s in a row, which also finds the stuffed 0s and the stuffing violations.
; Thread 0 never waits for anybody: every path that handles one bit takes exactly 13 (edge) or 9 (no edge) turns
; between two windows, which tools/timing.py proves (firmware/timing/usb_ls_rx.timing).
;
; Thread 1 gets each byte through the mailbox (it is idle in RECV, a byte takes 133 turns on the wire and its handler
; takes at most 69, proved in firmware/timing/usb_ls_rx_thread1.timing) and does the protocol: PID check, running CRC5 (tokens) or CRC16 (data), the report.
; It runs from the first instruction, so nothing is spawned per byte.

; ------------------------------------------------------------------------------------------ thread 0
usb_start:
        OUT  0x00
        ODRV 0 1              ; BYTE strobe released
        ODRV 1 1              ; STATUS strobe released
        ODRV 2 1              ; EVENT flag released
        SPAWN 1 t1_main
listen: LDA  0x80             ; A: byte shift register with a sentinel bit that leaves into C after 8 bits
        LDX  7                ; X: 7 - (number of 1s in a row); 1 = six 1s, so the next bit must be a stuffed 0
idle:   JPIN 0 0 idle         ; D+ low = J = idle. The first K starts SYNC and falls through into j_edge below:
                              ; its first SYNC bit is a 0 like any other edge.
; ---- level J, edge: D+ rose
j_edge: XCMP 2                ; X == 1: this 0 is a stuffed bit and is dropped
        JC   j_drop
        XCMP 0                ; C = 0: shift in a 0
        ROR
        JC   j_full0
        LDX  7
        WAIT 4
        JMP  k_win
j_drop: LDX  7
        WAIT 7
        JMP  k_win
j_full0:
        SEND
        LDA  0x80
        NOP
        LDX  7
        WAIT 1
        JMP  k_win

; ---- level J: wait for D+ to rise
j_win:  JPIN 0 1 j_edge
        JPIN 0 1 j_edge
        JPIN 0 1 j_edge
        JPIN 0 1 j_edge
        JPIN 0 1 j_edge
        JPIN 0 1 j_edge
        JPIN 0 1 j_edge
        JPIN 0 1 j_edge
        JPIN 0 1 j_edge
j_ne:   JPIN 1 0 se0_j        ; D- low as well: SE0, end of packet
        XCMP 8                ; C = 1: shift in a 1
        ROR
        JC   j_full1
        WAIT 2
        DJNZ j_win
        JMP  err_stuff        ; seven 1s in a row
j_full1:
        SEND                  ; the byte is complete: to thread 1
        LDA  0x80
        NOP
        DJNZ j_win
        JMP  err_stuff
; ---- level K: wait for D+ to fall
k_win:  JPIN 0 0 k_edge
        JPIN 0 0 k_edge
        JPIN 0 0 k_edge
        JPIN 0 0 k_edge
        JPIN 0 0 k_edge
        JPIN 0 0 k_edge
        JPIN 0 0 k_edge
        JPIN 0 0 k_edge
        JPIN 0 0 k_edge
k_ne:   XCMP 8
        ROR
        JC   k_full1
        WAIT 3
        DJNZ k_win
        JMP  err_stuff
k_full1:
        SEND
        LDA  0x80
        NOP
        WAIT 0
        DJNZ k_win
        JMP  err_stuff
k_edge: XCMP 2
        JC   k_drop
        XCMP 0
        ROR
        JC   k_full0
        LDX  7
        WAIT 0
        JPIN 1 0 se0_k        ; D+ fell and D- is still low: SE0, not a change from K to J
        WAIT 2
        JMP  j_win
k_drop: LDX  7
        WAIT 3
        JPIN 1 0 se0_j
        WAIT 2
        JMP  j_win
k_full0:
        SEND
        LDA  0x80
        NOP
        LDX  7
        JPIN 1 0 se0_bad
        WAIT 0
        JMP  j_win

; ---- end of packet and errors: tell thread 1 with the EVENT flag and a code in the mailbox
se0_j:  XOR  0x80             ; no bit pending: the packet ended on a byte boundary
        JZ   eop_ok
        JMP  eop_bad
se0_k:  XOR  0x40             ; the 0 that was shifted in for the fall of D+ is still in A
        JZ   eop_ok
se0_bad:
eop_bad:
        LDA  1                ; code 1: end of packet, not a whole number of bytes
        JMP  event
eop_ok: LDA  0                ; code 0: end of packet
        JMP  event
err_stuff:
        LDA  2                ; code 2: bit-stuffing violation
event:  ODRV 2 0              ; the EVENT flag reaches thread 1 before the byte does
        NOP
        NOP
        SEND
ev_wait:
        JPIN 0 1 ev_wait      ; wait for the bus to be back in J (D+ low, D- high), then listen again
        JPIN 1 0 ev_wait
        JMP  listen

; ------------------------------------------------------------------------------------------ thread 1
; Memory: 0 = bytes received in this packet (SYNC counts), 2/3 = CRC register (CRC16 low/high, or CRC5 in 2),
;         4 = status bits so far, 5 = PID bits 1:0 (the packet type), 7/8 = scratch
t1_main:
        RECV
        JPIN 10 0 t1_event    ; EVENT flag (uio[2] read back as input 10) low: A is an event code, not a byte
        TAX                   ; X = byte
        LDM  0
        JZ   t1_sync          ; first byte of the packet
        TXA
        OUTA                  ; report the byte
        ODRV 0 0
        WAIT 2
        ODRV 0 1
        LDM  0
        DEC
        JZ   t1_pid           ; second byte: PID
        LDM  5                ; later bytes: the CRC for this packet type (X = byte)
        XOR  1
        JZ   c5               ; token
        XOR  2
        JZ   c16              ; data
t1_done:
        LDM  0
        INC
        STM  0
        JMP  t1_main

t1_sync:
        TXA
        XOR  0x80             ; SYNC must be 0x80
        JZ   s_ok
        LDA  1                ; status bit 0
s_ok:   STM  4
        LDA  1
        STM  0
        JMP  t1_main

t1_pid: TXA
        SWAP
        XORX                  ; PID byte xor its nibble swap: 0xFF when the upper nibble is the complement
        XOR  0xFF
        JZ   p_ok
        LDM  4
        OR   2                ; status bit 1
        STM  4
p_ok:   TXA
        AND  3                ; PID bits 1:0: 1 token, 3 data, 2 handshake, 0 special
        STM  5
        LDA  0xFF
        STM  2                ; both CRC registers start as all ones (CRC5 uses the low 5 bits)
        STM  3
        JMP  t1_done

; CRC5 of a token, one byte (X = byte): reflected register 5 bits, polynomial 0x14, residue 0x06
c5:     LDM  2
        AND  0x1F
        XORX
        LDX  8
c5_l:   SHR
        JNC  c5_n
        XOR  0x14
c5_n:   DJNZ c5_l
        STM  2
        JMP  t1_done

; CRC16 of a data packet, one byte (X = byte): reflected, polynomial 0xA001, residue 0xB001.
; x = low ^ byte; p = parity(x); new low = high ^ p ^ (x << 6) ^ (x << 7); new high = p * 0xC0 ^ (x >> 1) ^ (x >> 2)
c16:    LDM  2
        XORX                  ; A = x
        STM  7
        SWAP
        TAX
        LDM  7
        XORX                  ; both nibbles: x7^x3 x6^x2 x5^x1 x4^x0
        AND  0x0F
        TAX
        SHR
        SHR
        XORX                  ; t ^ (t >> 2)
        TAX
        SHR
        XORX
        AND  1                ; A = p
        STM  8
        LDM  7                ; (x << 6) ^ (x << 7) = ((x ^ (x << 1)) & 3) << 6
        AND  3
        TAX
        SHL
        XORX
        AND  3
        SWAP
        SHL
        SHL                   ; A = (x << 6) ^ (x << 7)
        TAX
        LDM  8
        XORX                  ; ^ p
        TAX
        LDM  3
        XORX                  ; ^ old high: the new low byte
        STM  2
        LDM  7                ; new high = p * 0xC0 ^ (x >> 1) ^ (x >> 2) = p * 0xC0 ^ ((x ^ (x >> 1)) >> 1)
        TAX
        SHR
        XORX
        SHR
        TAX                   ; X = (x >> 1) ^ (x >> 2)
        LDM  8
        JZ   c16_h
        LDA  0xC0
c16_h:  XORX
        STM  3
        JMP  t1_done

; End of packet (code 0), end of packet with a stray bit (1), bit-stuffing violation (2): the STATUS report.
t1_event:
        ODRV 2 1              ; release the EVENT flag
        STM  7                ; the code
        LDM  5                ; PID bits 1:0
        XOR  1
        JZ   e_tok
        XOR  2
        JZ   e_dat
        LDA  0                ; handshake or special: no CRC
        JMP  e_chk
e_tok:  LDM  2
        XOR  6                ; CRC5 residue
        JMP  e_chk
e_dat:  LDM  2
        XOR  1                ; CRC16 residue 0xB001
        TAX
        LDM  3
        XOR  0xB0
        ORX
e_chk:  JZ   e_ok
        LDA  4                ; status bit 2: CRC wrong
e_ok:   TAX
        LDM  4                ; SYNC and PID bits
        ORX
        TAX
        LDM  7
        SWAP                  ; code << 4: bit 4 (stray bit) or 5 (stuffing)
        ORX
        TAX
        LDM  5
        SWAP
        SHL
        SHL                   ; PID bits 1:0 << 6
        ORX
        OUTA
        ODRV 1 0
        WAIT 2
        ODRV 1 1
        LDA  0
        STM  0                ; next packet starts with SYNC
        JMP  t1_main
