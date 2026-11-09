; SCL timing monitor: a thread that watches the I2C clock line while the master runs, and sets
; SCL_VIOL (uo[5]) for good if SCL ever violates the standard-mode minimum times.
;
; Input pins : 10 = SCL (uio_in[2], the pad, so a slave's clock stretching is seen too)
; Output pins: 5 = SCL_VIOL (stays high until reset)
;
; After every SCL edge the thread polls the line every 2 slots (8 clocks) for a fixed time and
; flags a violation the moment the line changes early:
;   SCL low  : must stay low for 56 slots  (about 4.5 us; the spec minimum tLOW is 4.7 us)
;   SCL high : must stay high for 48 slots (about 3.9 us; the spec minimum tHIGH is 4.0 us)
; The edge itself is only seen within a slot or so, so the limits sit just below the spec
; minimums, so that a phase that is long enough never trips them. In clocks: a low phase shorter
; than 226 clocks is always flagged and one of 230 or more never is; a high phase shorter than
; 194 is always flagged and one of 198 or more never is.
; This costs the master nothing: it is a separate thread with its own program counter.

mon_start:
mon_fall:
        JPIN 10 1 mon_fall    ; wait for SCL to fall
        LDX  28
mon_lo:
        JPIN 10 1 mon_lo_bad  ; SCL came back up too early
        DJNZ mon_lo
mon_rise:
        JPIN 10 0 mon_rise    ; wait for SCL to rise
        LDX  24
mon_hi:
        JPIN 10 0 mon_hi_bad  ; SCL was pulled down too early
        DJNZ mon_hi
        JMP  mon_fall

mon_lo_bad:
        PIN  5 1
        JMP  mon_rise         ; SCL is high now: go and time that phase
mon_hi_bad:
        PIN  5 1
        JMP  mon_fall         ; SCL is low now: go and time that phase
