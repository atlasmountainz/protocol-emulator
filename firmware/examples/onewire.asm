; 1-Wire bus master (standard speed) that reads a device's ROM code and checks its CRC8.
; An example of firmware loaded into the program memory after fabrication; it is NOT in the ROM.
; Build the image with:  python3 tools/load_image.py build/onewire firmware/examples/onewire.asm
;
; Open-drain : uio[0] = DQ (1-Wire bus, external pull-up). The pad is read back as input pin 8.
;              No other pad is used or driven (uio_oe stays 0 except bit 0).
; Output pins: uo[0] = UART TX (115200 baud, 8N1)
; Do not use  : uio[5], uio[4] (program loader; LOAD must be low while this runs)
;
; Sequence: reset pulse (500 us low), presence sample 70 us after release, READ ROM (0x33), 64 bits of
; ROM code. The CRC8 (Dallas/Maxim, x^8+x^5+x^4+1, shifted right, polynomial 0x8C) is computed over all
; 8 bytes: it ends at zero exactly when the 8th byte equals the CRC of the first seven.
; UART output, in order: the 8 ROM bytes (family code first, CRC byte last), then one status byte:
;   0 = CRC ok, 1 = CRC error.   No presence pulse (or bus stuck low): only one byte, 2.
;
; Two threads: thread 0 does the bus, the byte assembly and the CRC; thread 1 is the UART transmitter
; and gets each byte through the channel. Thread 0 keeps its state in A, X and C and in two cells of its
; private data memory (the memory is zero after reset, which the program relies on):
;   MEM[0]  CRC register
;   MEM[1]  byte counter, counted in steps of 0x20 so that the carry out of ADD flags the 8th byte
;
; A received byte is assembled in A with SHR and OR: each bit is shifted in at the top, so the first
; bit on the wire (the LSB) ends up in A[0] and A holds the true byte. A starts as 0x80, a marker that
; falls out into C on the 8th shift and so ends the byte without a bit counter. After the 8th bit the
; CRC step runs inside the idle part of that same slot, so all 64 read slots have the same length.
;
; Timing (1 turn = 4 clocks = 80 ns):
;   reset low        6254 turns = 500.3 us   (spec: >= 480 us)
;   presence sample  876 turns after release = 70.1 us (device pulls low 15-60 us after release for 60-240 us)
;   bus recovery     the line is released for 483 us in all before the first command
;   write-1          low 75 turns = 6.0 us, slot 880 turns = 70.4 us
;   write-0          low 814 turns = 65.1 us (spec 60-120 us), slot 880 turns, recovery 66 turns = 5.3 us
;   read             low 75 turns = 6.0 us, sampled 12.1 us after the falling edge (spec: within 15 us),
;                    slot 863 turns = 69.0 us (every read slot, including the one that ends a byte)

ow_start:
        SPAWN 1 uart_thread
        ODRV 0 0              ; reset pulse: pull DQ low for 6254 turns
        WAIT 4000
        WAIT 2251
        ODRV 0 1              ; release
        WAIT 874
        JPIN 8 1 ow_nopres    ; presence pulse = line low 70 us after release
        WAIT 4000             ; wait out the rest of the reset slot (483 us in all)
        WAIT 1163
        JPIN 8 0 ow_nopres    ; the line must have returned high

        LDA  0x33             ; READ ROM, LSB first
        LDX  8
ow_wbit:
        SHR                   ; C = next command bit
        ODRV 0 0              ; falling edge
        WAIT 73
        ODRVC 0               ; 6 us later: release for a 1, keep holding for a 0
        WAIT 737
        ODRV 0 1              ; end of the slot's low time (a 0 has been low for 65 us)
        WAIT 62
        DJNZ ow_wbit

ow_byte:                      ; one ROM byte
        LDA  0x80             ; byte register with its end marker
ow_rbit:
        ODRV 0 0              ; falling edge
        WAIT 73
        ODRV 0 1              ; release after 6 us: the device now holds the line for a 0
        WAIT 73
        SHR                   ; C = marker bit; make room at the top
        JPIN 8 1 ow_one       ; sample DQ 12 us after the falling edge
        JMP  ow_join          ; a 0: nothing to insert (both paths take one turn)
ow_one:
        OR   0x80             ; a 1: becomes the new top bit
ow_join:
        JC   ow_bdone         ; the marker came out: A holds the complete byte
        WAIT 707              ; rest of the 69 us slot
        JMP  ow_rbit

ow_bdone:
        SEND                  ; the byte goes to the UART thread
        TAX                   ; CRC: register ^= byte, then 8 shifts, each followed by XOR 0x8C if a 1 fell out
        LDM  0
        XORX
        LDX  8
ow_crc:
        SHR
        JC   ow_cx
        JMP  ow_cn            ; (both paths take one turn: the slot length must not depend on the data)
ow_cx:
        XOR  0x8C
ow_cn:
        DJNZ ow_crc
        STM  0
        LDM  1                ; count the byte: 8 x 0x20 = 0x100 sets the carry
        ADD  0x20
        STM  1
        WAIT 665              ; rest of the slot
        JNC  ow_byte          ; next byte, or fall through after the 8th

        LDM  0                ; CRC register: zero = ok
        JZ   ow_send          ; A = 0 is the status "ok"
        LDA  1                ; status 1: CRC error
ow_send:
        SEND
        HALT
ow_nopres:
        LDA  2                ; status 2: no presence
        JMP  ow_send

; ---- thread 1: UART transmitter -------------------------------------------------------------
; The same transmitter as uart_send in uart_tx.asm: 108 turns per bit, LSB first.
uart_thread:
        RECV
        PIN  0 0              ; start bit
        LDX  8
        WAIT 104
uart_bit:
        SHR
        OUTC 0
        WAIT 104
        DJNZ uart_bit
        NOP
        PIN  0 1              ; stop bit
        WAIT 104
        JMP  uart_thread
