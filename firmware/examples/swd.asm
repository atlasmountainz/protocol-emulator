; SWD (ARM Serial Wire Debug) host: wakes a debug port and powers up its debug domain.
; An example of firmware loaded into the program memory after fabrication; it is NOT in the ROM.
; Build the image with:  python3 tools/load_image.py build/swd firmware/examples/swd.asm     (docs/examples-swd-can.md)
;
; Output pins: 2 = uo[2] = SWCLK (push-pull, idles low), 0 = uo[0] = UART TX (115200 baud, 8N1)
; Open-drain : 0 = uio[0] = SWDIO, read back as input pin 8. It needs a pull-up (4.7 kOhm or less): the host only
;              pulls the line low or lets go, so a 1 is the pull-up and the host can never fight the target.
; Do not use : uio[5], uio[4] (program loader; LOAD must be low while this runs)
;
; Sequence:
;   1. Line reset (56 clocks with SWDIO high), the JTAG-to-SWD select sequence 0xE79E (LSB first), line reset
;      again, 8 idle clocks (SWDIO low).
;   2. Read DPIDR (request 0xA5). The first transaction after a line reset has to be this one.
;   3. Write ABORT = 0x0000001E (request 0x81): clear the sticky error flags.
;   4. Write CTRL/STAT = 0x50000000 (request 0xA9): CSYSPWRUPREQ and CDBGPWRUPREQ.
;   5. Read CTRL/STAT (request 0x8D): a debug port that powered up answers 0xF0000000.
; A transaction is: 8 request bits from the host, one turnaround clock, 3 ACK bits from the target, then either
; 32 data bits and a parity bit from the target and a turnaround clock (read), or a turnaround clock and 32 data bits
; and a parity bit from the host (write); then 8 idle clocks. All LSB first. The parity of the data is computed here,
; in firmware, for both directions.
; ACK: 1 = OK, 2 = WAIT, 4 = FAULT, 7 = nobody answered. On WAIT the request is repeated, up to 16 times.
; Any final ACK other than OK ends the program after the report.
;
; UART report: for a read  ACK, then (if OK) data bytes 0..3 (byte 0 = bits 0..7) and 0 (parity good) or 1 (bad);
;              for a write ACK.   A debug port that works gives 14 bytes:  01 ID0 ID1 ID2 ID3 00  01  01  01 00 00 00 F0 00
;
; Timing (default schedule, 80 ns per turn; proved by firmware/timing/swd.timing): SWCLK is high for exactly
; 12 turns (960 ns) and low for at least 17 (1.36 us), so it never exceeds 431 kHz. The host changes SWDIO only
; while SWCLK is low, at least 14 turns (1.12 us, room for the pull-up to raise the line) before the rising edge
; on which the target samples it. The target's bits are read 1 turn before a rising edge, which is at least
; 2.24 us after the rising edge on which the target put them on the line. While a byte waits for the UART, SWCLK
; simply stays low: the host owns the clock, so the protocol does not mind.
;
; Data memory (thread 0): 0 = XOR of the data bytes read, 1 = parity bit read, 2 = byte counter, 3 = request,
;                         4..7 = data to write (byte 0 first), 8 = tries left on WAIT, 9 = counter of sw_reset,
;                         10 = the shift register while a bit is read

sw_start:
        SPAWN 1 sw_uart
        PIN  2 0              ; SWCLK low
        ODRV 0 1              ; SWDIO released
        CALL sw_reset
        LDA  0x9E             ; JTAG-to-SWD: 0xE79E, LSB first
        CALL sw_wbyte
        LDA  0xE7
        CALL sw_wbyte
        CALL sw_reset
        LDA  0
        CALL sw_wbyte         ; 8 idle clocks
        LDA  0xA5             ; read DPIDR
        CALL sw_read
        LDA  0x1E             ; ABORT = 0x0000001E (memory cells 5..7 are still 0 after reset)
        STM  4
        LDA  0x81
        CALL sw_write
        LDA  0
        STM  4
        LDA  0x50             ; CTRL/STAT = 0x50000000
        STM  7
        LDA  0xA9
        CALL sw_write
        LDA  0x8D             ; read CTRL/STAT
        CALL sw_read
        HALT

sw_reset:                     ; line reset: 7 * 8 = 56 clocks with SWDIO high
        LDA  7
        STM  9
sw_rs1: LDA  0xFF
        CALL sw_wbyte
        LDM  9
        DEC
        STM  9
        JNZ  sw_rs1
        RET

; Request A, then the ACK. Repeats the request while the target says WAIT. Sends the final ACK to the UART and
; returns with A = 0 if it was OK.
sw_req: STM  3
        LDA  16
        STM  8
sw_rq1: LDM  3
        CALL sw_wbyte         ; the park bit (1) leaves SWDIO released
        CALL sw_trn           ; turnaround
        LDA  0
        CALL sw_rbit
        CALL sw_rbit
        CALL sw_rbit          ; A = ACK << 5
        SWAP
        SHR                   ; A = ACK, first bit in bit 0
        XOR  2
        JNZ  sw_rq2           ; not WAIT
        LDM  8
        DEC
        STM  8
        JZ   sw_rq2           ; out of tries (A = 0 becomes ACK 2 again below)
        CALL sw_trn           ; WAIT: turnaround, idle, and again
        LDA  0
        CALL sw_wbyte
        JMP  sw_rq1
sw_rq2: XOR  2
        SEND                  ; the ACK
        XOR  1                ; zero if OK
        RET

sw_read:                      ; read transaction, request in A
        CALL sw_req
        JNZ  sw_fail
        STM  0                ; XOR of the bytes = 0
        LDA  4
        STM  2
sw_rd1: LDA  0x80             ; byte register with its end marker
sw_rd2: CALL sw_rbit
        JNC  sw_rd2
        TAX
        SEND                  ; the byte, as it is (the first bit received is bit 0)
        LDM  0
        XORX
        STM  0
        LDM  2
        DEC
        STM  2
        JNZ  sw_rd1
        CALL sw_rbit          ; the parity bit: A = 0x80 or 0
        STM  1
        CALL sw_trn           ; turnaround: the line is the host's again
        LDM  0
        CALL sw_par
        SHR
        ROR                   ; A = 0x80 if the data has an odd number of ones
        TAX
        LDM  1
        XORX
        JZ   sw_rd3
        LDA  1
sw_rd3: SEND                  ; 0 = parity good, 1 = parity error
        LDA  0
        JMP  sw_wbyte         ; 8 idle clocks; its RET returns to our caller

sw_write:                     ; write transaction, request in A, data in memory cells 4..7
        CALL sw_req
        JNZ  sw_fail
        CALL sw_trn           ; turnaround
        LDM  4
        CALL sw_wbyte
        LDM  5
        CALL sw_wbyte
        LDM  6
        CALL sw_wbyte
        LDM  7
        CALL sw_wbyte
        LDM  4
        TAX
        LDM  5
        XORX
        TAX
        LDM  6
        XORX
        TAX
        LDM  7
        XORX
        CALL sw_par
        CALL sw_wbit          ; the parity bit
        LDA  0
        JMP  sw_wbyte         ; 8 idle clocks

sw_fail:                      ; WAIT (after 16 tries), FAULT or no answer: turnaround, idle, stop
        CALL sw_trn
        LDA  0
        CALL sw_wbyte
        HALT

sw_wbit:                      ; one bit from A[0]
        LDX  1
        JMP  sw_wb1
sw_wbyte:                     ; 8 bits of A, LSB first. SWDIO changes while SWCLK is low; the target samples on the rise
        LDX  8
sw_wb1: SHR
        ODRVC 0               ; SWDIO = the bit (0 pulls low, 1 lets go)
        WAIT 12
        PIN  2 1
        WAIT 10
        PIN  2 0
        DJNZ sw_wb1
        RET

sw_rbit:                      ; one bit from the target into the top of A; C = the bit that falls out of A
        WAIT 11
        STM  10               ; keep the shift register
        INB  8                ; SWDIO, read back from the pad, just before the rising edge
        PIN  2 1
        SHR                   ; C = the bit
        LDM  10
        ROR                   ; into the top; the old bit 0 falls out into C
        WAIT 7
        PIN  2 0
        RET

sw_trn:                       ; turnaround: one clock with SWDIO released by the host
        ODRV 0 1
        WAIT 12
        PIN  2 1
        WAIT 10
        PIN  2 0
        RET

sw_par:                       ; A = parity of A (1 = odd number of ones); uses X
        TAX
        SWAP
        XORX                  ; both nibbles: bit i ^ bit i+4
        TAX
        SHR
        SHR
        XORX
        TAX
        SHR
        XORX
        AND  1
        RET

; ---- thread 1: UART transmitter (identical to the one in onewire.asm) ------------------------
sw_uart:
        RECV
        PIN  0 0              ; start bit
        LDX  8
        WAIT 104
sw_ubit:
        SHR
        OUTC 0
        WAIT 104
        DJNZ sw_ubit
        NOP
        PIN  0 1              ; stop bit
        WAIT 104
        JMP  sw_uart
