; 1-Wire bus master (standard speed) that reads a device's ROM code and checks its CRC8.
; An example of firmware loaded into the program memory after fabrication; it is NOT in the ROM.
; Build the image with:  python3 tools/load_image.py build/onewire firmware/examples/onewire.asm
;
; Open-drain : uio[0] = DQ (1-Wire bus, external pull-up). The pad is read back as input pin 8.
;              uio[1], uio[2], uio[3] = scratch lines between the threads, see below. Leave them unconnected.
; Output pins: uo[0] = UART TX (115200 baud, 8N1)
; Do not use  : uio[5], uio[4] (program loader; LOAD must be low while this runs).
;
; Sequence: reset pulse (500 us low), presence sample 70 us after release, READ ROM (0x33), 64 bits of
; ROM code. The CRC8 (Dallas/Maxim, x^8+x^5+x^4+1) is computed by firmware over all 64 bits as they
; arrive: it ends at zero exactly when the 8th byte equals the CRC of the first seven.
; UART output, in order: the 8 ROM bytes (family code first, CRC byte last), then one status byte:
;   0 = CRC ok, 1 = CRC error.   No presence pulse (or bus stuck low): only one byte, 2.
;
; Why three threads: the ISA has no ALU and no data memory. A branch can only test a pin, and a thread
; has just A, X and C. Receiving needs A for the byte; the CRC register needs a second A; the UART needs a
; third. So the work is split over the barrel:
;   thread 0  bus timing, byte assembly, sequencing
;   thread 1  CRC co-processor: A = the CRC register (kept bit-reversed, which does not matter: a zero
;             stays zero). It wakes on a strobe from thread 0, looks at the data line itself, and
;             publishes "CRC register is zero" on a pad
;   thread 2  UART transmitter
; The threads talk through three open-drain pads that they pull low and read back (docs/isa.md):
;   uio[1] = pad 1 / input 9   STROBE   thread 0 pulls it low for 8 slots at every sampled bit
;   uio[2] = pad 2 / input 10  Q        thread 1 scratch: carry from C to a pin, so that a bit of A can be branched on
;   uio[3] = pad 3 / input 11  ZERO     thread 1 releases it (high) while the CRC register is non-zero
; A pad that is released has to read back high; the pad needs no external part, but nothing may pull
; it low from outside.
;
; Bit order: the 1-Wire bus sends the LSB of each byte first, and SHL / INB shifts the first bit into A[7].
; So A holds each byte in wire order (a "bit-reversed" value); the UART thread shifts it out with
; SHL / OUTC, which puts the first bit (the LSB) on the line first: no reversal needed. Status bytes
; are therefore written reversed in this file (true 1 = 0x80, true 2 = 0x40).
;
; Timing (1 slot = 4 clocks = 80 ns):
;   reset low        6254 slots = 500.3 us   (spec: >= 480 us)
;   presence sample  876 slots after release = 70.1 us (device pulls low 15-60 us after release for 60-240 us)
;   bus recovery     the line is released for 483 us in all before the first command
;   write-1          low 75 slots = 6.0 us, slot 880 slots = 70.4 us
;   write-0          low 814 slots = 65.1 us (spec 60-120 us), slot 880 slots, recovery 66 slots = 5.3 us
;   read             low 75 slots = 6.0 us, sampled 12.0 us after the falling edge (spec: within 15 us),
;                    slot 863 slots = 69.0 us

ow_start:
        SPAWN 2 uart_thread
        ODRV 0 0              ; reset pulse: pull DQ low for 6254 slots
        LDX  2
ow_rst:
        WAIT 3124
        DJNZ ow_rst
        ODRV 0 1              ; release
        WAIT 874
        JPIN 8 1 ow_nopres    ; presence pulse = line low 70 us after release
        LDX  2
ow_rec:
        WAIT 2580             ; wait out the rest of the reset slot (483 us in all)
        DJNZ ow_rec
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

        SPAWN 1 crc_start
        CALL ow_rbyte         ; 8 ROM bytes (each is sent to the UART thread)
        CALL ow_rbyte
        CALL ow_rbyte
        CALL ow_rbyte
        CALL ow_rbyte
        CALL ow_rbyte
        CALL ow_rbyte
        CALL ow_rbyte
        LDA  0x00             ; status 0: CRC ok
        JPIN 11 0 ow_done     ; ZERO pad low = CRC register is zero
        LDA  0x80             ; status 1 (reversed): CRC error
ow_done:
        SEND
        HALT
ow_nopres:
        LDA  0x40             ; status 2 (reversed): no presence
        SEND
        HALT

; Read 8 bits into A (first bit ends up in A[7]) and hand the byte to the UART thread.
ow_rbyte:
        LDX  8
ow_rbit:
        ODRV 0 0              ; falling edge
        WAIT 73
        ODRV 0 1              ; release after 6 us: the device now holds the line for a 0
        WAIT 73
        SHL
        INB  8                ; sample DQ 12 us after the falling edge
        ODRV 1 0              ; STROBE: tell the CRC thread, which samples DQ 4 slots later
        WAIT 6
        ODRV 1 1
        WAIT 700              ; rest of the 69 us slot, and time for the CRC thread
        DJNZ ow_rbit
        SEND
        RET

; ---- thread 1: CRC co-processor -------------------------------------------------------------
; Galois LFSR, shifting left, polynomial 0x31 (the Dallas CRC8 with its bit order reversed; the
; bits are fed in as they arrive on the wire, so no reversal is ever needed). A = register.
crc_start:
        ODRV 3 1              ; ZERO released: register treated as non-zero until proven otherwise
crc_wait:
        JPIN 9 1 crc_wait     ; wait for STROBE low
        SHL                   ; C = register bit 7, A = register << 1
        ODRVC 2               ; Q = that bit
        JPIN 8 1 crc_d1       ; d = the data bit on DQ
        JPIN 10 0 crc_test    ; d = 0: feedback = bit 7 = 0, nothing to add
        JMP  crc_xor
crc_d1:
        JPIN 10 1 crc_test    ; d = 1: feedback = ~bit 7 = 0
crc_xor:                      ; feedback = 1: A = A xor 0x31 (bits 5, 4 and 0), by rotating through Q
        LDX  2
crc_p1:
        SHL                   ; bits 7, 6: unchanged
        ODRVC 2
        INB  10
        DJNZ crc_p1
        CALL crc_flip         ; bit 5
        CALL crc_flip         ; bit 4
        LDX  3
crc_p2:
        SHL                   ; bits 3, 2, 1: unchanged
        ODRVC 2
        INB  10
        DJNZ crc_p2
        CALL crc_flip         ; bit 0
crc_test:                     ; publish "register == 0" on ZERO (low = zero)
        ODRV 3 0
        LDX  8
crc_z:
        SHL
        ODRVC 2
        INB  10               ; (rotates the register back to where it was)
        JPIN 10 0 crc_zn
        ODRV 3 1              ; a 1 bit: not zero
crc_zn:
        DJNZ crc_z
crc_rel:
        JPIN 9 0 crc_rel      ; wait for STROBE high again
        JMP  crc_wait

crc_flip:                     ; rotate A left one place, inverting the bit that goes round
        SHL
        ODRVC 2
        JPIN 10 1 crc_f1      ; bit was 1: the inverted bit (0) is already there
        ODRV 2 1
        NOP
        INB  10               ; bit was 0: insert 1
crc_f1:
        RET

; ---- thread 2: UART transmitter -------------------------------------------------------------
; Same frame as uart_send in uart_tx.asm (108 slots per bit), but it shifts A out with SHL, so a byte
; in wire order (first bit in A[7]) goes out first bit first.
uart_thread:
        RECV
        PIN  0 0              ; start bit
        LDX  8
        WAIT 104
uart_bit:
        SHL
        OUTC 0
        WAIT 104
        DJNZ uart_bit
        NOP
        PIN  0 1              ; stop bit
        WAIT 104
        JMP  uart_thread
