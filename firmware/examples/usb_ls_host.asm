; Low-speed USB host (1.5 Mbit/s): resets the bus, sends keep-alives, and sends the first transaction of every USB
; enumeration, SETUP + DATA0 with GET_DESCRIPTOR(device), to address 0, endpoint 0. Then it turns the bus around
; and receives the device's handshake (ACK / NAK / STALL) and shows it on uo[7:0]. Once per 1 ms frame.
; An example of firmware loaded into the program memory after fabrication; it is NOT in the ROM.
; Build the image with the schedule 0,1,0,1 (threads 0 and 1: one instruction every 2 clocks = 40 ns):
;     python3 tools/load_image.py --sched 0,1,0,1 build/usb_ls_host firmware/examples/usb_ls_host.asm
; Documentation: docs/usb-low-speed.md. Tests: test/test_usb_ls_host_model.py, test/test_usb_ls_host_rtl.py.
;
; Pins   uio[0] = D+, uio[1] = D- (read back as inputs 8 and 9). Both lines are DRIVEN while the host sends
;        (push-pull mode, PPA) and released while the device answers; the board needs the host's 15 kOhm pull-downs
;        on D+ and D- (the device has its 1.5 kOhm pull-up on D-). Nothing else is driven.
;        uo[7:0] = the result of the last transaction: the handshake PID (0xD2 ACK, 0x5A NAK, 0x1E STALL), 0x00 if
;        no device answered within 17 bit times, 0xFF if what came back did not start with a SYNC.
;
; Levels are written to both pads at once with ODRVA (bit 0 = D+, bit 1 = D-; the other pads stay released):
;        J = 0xFE (D+ low, D- high), K = 0xFD, SE0 = 0xFC.
;
; Thread 1 is the encoder. It holds the packets (and computes their CRC5 and CRC16 once), does the bit stuffing,
; and passes the bits to thread 0 through the mailbox, 7 bits at a time: a byte with the bits at the bottom (first
; bit in bit 0) and a 1 just above them. A byte 0x00 ends a packet; a 0x00 with no packet before it is a keep-alive.
; Thread 0 is the line driver. It does the NRZI coding (a 0 toggles J / K) and writes one level per bit cell at
; exact times: cells of 17, 17 and 16 turns (680, 680 and 640 ns), so that every three bits take exactly 2 us
; and the bit rate is exactly 1.5 Mbit/s on average. Every cell takes the same number of turns on every path
; (new 7-bit group from the mailbox or not, J or K), which tools/timing.py proves (firmware/timing/usb_ls_host.timing).
; Then it receives the handshake by sampling D+ in the middle of each cell.
;
; Data memory, thread 0: 0 = the 7-bit group being sent (with its marker bit), 1 = the level (J / K), 2 = where to
; continue after an end of packet, 3 / 4 = the handshake samples (cells 0-7 / 8-15).

; ------------------------------------------------------------------------------------------ thread 0
h_start:
        SPAWN 1 e_start
        OUT  0x00
        LDA  0xFC             ; bus reset: SE0 for 10.5 ms
        ODRVA
        LDA  0x03
        PPA                   ; D+ and D- driven
h_rlen: LDX  64               ; 64 x 4097 turns
h_rst:  WAIT 4095
        DJNZ h_rst
        LDA  0xFE
        ODRVA                 ; J
        WAIT 30
        CALL h_rel
h_frame:
        LDA  h_tok
        STM  2
        JMP  tx               ; keep-alive (the encoder sends an empty packet first)
h_tok:  WAIT 200              ; time for the encoder to have two groups of the token ready
        LDA  h_dat
        STM  2
        JMP  tx               ; SETUP token
h_dat:  LDA  h_rx
        STM  2
        JMP  tx               ; DATA0, straight after the token (about 3 bit times between the packets)

; ---- the handshake: wait up to about 17 bit times for D+ to go high (the first K of the SYNC)
h_rx:   LDX  140
h_rw:   JPIN 8 1 h_rk
        DJNZ h_rw
        LDA  0                ; nobody answered
        JMP  h_rep
h_rk:   WAIT 3                ; to the middle of cell 0
        LDX  5
h_sg:   CALL h_smp            ; 15 samples, 17 / 17 / 16 turns apart
        WAIT 6
        CALL h_smp
        WAIT 6
        CALL h_smp
        WAIT 4
        DJNZ h_sg
        CALL h_smp            ; the 16th
        LDM  3
        XOR  0xD5             ; SYNC: K J K J K J K K (D+ = 1 0 1 0 1 0 1 1, first cell in bit 0)
        JNZ  h_bad
        LDM  4                ; the PID cells: NRZI back to bits
        SHL
        OR   1                ; the level before each cell (the cell before the PID is a K)
        TAX
        LDM  4
        XORX                  ; 1 where the level changed = a 0 bit
        NOT
        JMP  h_rep
h_bad:  LDA  0xFF
h_rep:  OUTA
        LDX  6                ; the rest of the 1 ms frame
h_fw:   WAIT 3800
        DJNZ h_fw
        JMP  h_frame

h_smp:  INB  8                ; D+ into the 16-bit sample register (mem 4 : mem 3), first sample ends in bit 0
        SHR
        LDM  4
        ROR
        STM  4
        LDM  3
        ROR
        STM  3
        RET

h_rel:  LDA  0
        PPA                   ; pads back to open drain (D+ stays pulled low: still J) ...
        LDA  0xFF
        ODRVA                 ; ... then released: the pull-up on D- and the pull-down on D+ hold the J
        RET

; ---- send one packet from the encoder's groups, then the end of packet; continue at mem 2
tx:     RECV                  ; the first group (0x00: no packet, only the EOP = a keep-alive)
        STM  0
        LDA  0xFE
        STM  1                ; the line starts at J
        ODRVA
        LDA  0x03
        PPA                   ; drive J
        LDM  0
        JZ   tx_ka
        WAIT 8
tx_g:   CALL cell             ; 17 turns
        WAIT 1
        CALL cell             ; 17 turns
        WAIT 1
        CALL cell             ; 16 turns
        JMP  tx_g

; one bit cell: the next level is written by the ODRVA 13 turns after the CALL, on every path
cell:   LDM  0
        SHR                   ; C = the next bit
        JZ   c_new            ; that was the marker: the group is used up
        STM  0
        WAIT 3                ; as long as the c_new path
c_bit:  JC   c_one
        LDM  1                ; a 0: the level changes
        XOR  3                ; J <-> K
        STM  1
c_out:  ODRVA
        RET
c_one:  LDM  1                ; a 1: the level stays
        NOP
        JMP  c_out
c_new:  RECV                  ; the next group (the encoder is always ahead)
        JZ   c_eop            ; 0x00: no more bits
        SHR
        STM  0
        JMP  c_bit

; the end of packet: SE0 for two bit times (33 turns = 1.32 us), J for one, then the bus is released
c_eop:  LDA  0xFC             ; on the same turn as a level would have been written
        WAIT 5
tx_se0: ODRVA                 ; SE0
        WAIT 30
        LDA  0xFE
tx_j:   ODRVA                 ; J
        WAIT 13
        CALL h_rel
        LDM  2
        JMPA
tx_ka:  LDA  0xFC             ; keep-alive: only the EOP
        JMP  tx_se0

; ------------------------------------------------------------------------------------------ thread 1
; Memory: 0 = byte being sent, 1 = bits left in it, 2 = the 7-bit group being filled (marker 0x40 at the start:
; it falls out into C after 7 bits). X = 1s in a row (bit stuffing).
; The two packets are constants: they are the first request of every enumeration, to the default address 0,
; endpoint 0, which is the only address a device answers before it is given one. Their CRCs (CRC5 0x02 in the
; token's last byte 0x10; CRC16 0x94DD sent as DD 94) are checked against the reference model by the tests.
e_start:
e_frame:
        LDA  0
        SEND                  ; a keep-alive
        CALL e_new
        LDA  0x80             ; SYNC
        CALL emit
        LDA  0x2D             ; SETUP
        CALL emit
        LDA  0x00             ; address 0, endpoint bit 0
        CALL emit
        LDA  0x10             ; endpoint bits 3:1, CRC5
        CALL emit
        CALL e_end
        CALL e_new
        LDA  0x80
        CALL emit
        LDA  0xC3             ; DATA0
        CALL emit
        LDA  0x80             ; GET_DESCRIPTOR(device), 64 bytes: 80 06 00 01 00 00 40 00
        CALL emit
        LDA  0x06
        CALL emit
        LDA  0x00
        CALL emit
        LDA  0x01
        CALL emit
        LDA  0x00
        CALL emit
        CALL emit             ; (emit leaves A = 0)
        LDA  0x40
        CALL emit
        LDA  0x00
        CALL emit
        LDA  0xDD             ; CRC16, low byte first
        CALL emit
        LDA  0x94
        CALL emit
        CALL e_end
        JMP  e_frame

e_new:  LDA  0x40
        STM  2
        LDX  0
        RET

; emit A: its 8 bits (first bit 0) with bit stuffing into the groups; 13 to 16 turns per bit
emit:   STM  0
        LDA  8
        STM  1
e_bit:  LDM  0
        SHR
        STM  0                ; C = the bit
        JC   e_one
        LDX  0                ; a 0: no 1s in a row
        LDM  2
        ROR
        JC   e_f0             ; the marker came out: 7 bits in the group
        STM  2
e_nx:   LDM  1
        DEC
        STM  1
        JNZ  e_bit
        RET
e_one:  XADD 1
        LDM  2
        ROR
        JC   e_f1
        STM  2
e_st:   XCMP 6
        JNC  e_stf            ; six 1s in a row: a stuffed 0 follows
        JMP  e_nx
e_stf:  LDX  0
        CMP  0                ; C = 0
        LDM  2
        ROR
        JC   e_f0
        STM  2
        JMP  e_nx
e_f0:   ROR                   ; C = 1 (the marker) becomes the group's top bit
        SEND
        LDA  0x40
        STM  2
        JMP  e_nx
e_f1:   ROR
        SEND
        LDA  0x40
        STM  2
        JMP  e_st

; the last group: a 1 above the bits, then 0s until it is a whole byte; then 0x00 = end of packet
e_end:  LDM  2
        XOR  0x40
        JZ   e_e0             ; the group is empty
        LDA  1
        SHR                   ; C = 1
        LDM  2
        ROR
e_el:   JC   e_el2            ; the marker came out: one more 0
        CMP  0
        ROR
        JMP  e_el
e_el2:  CMP  0
        ROR
        SEND
e_e0:   LDA  0
        SEND
        RET
