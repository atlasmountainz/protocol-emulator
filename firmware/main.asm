; Boot dispatcher: pick the protocol from the mode pins, sampled once at boot.
;   uio_in[6] = 1 -> I2C master (EEPROM reader, prints on UART) + SCL timing monitor thread
;   uio_in[7] = 1 -> SPI master
;   both          -> full-duplex UART echo (two threads)
;   neither       -> UART transmitter
; The input pins pass through a 2-flop synchroniser that is cleared during reset,
; so wait a slot before the first sample.

        WAIT 1
        JPIN 14 0 not_i2c_or_echo
        JPIN 15 1 echo_start
        JMP  i2c_start_fw
not_i2c_or_echo:
        JPIN 15 1 spi_start
        JMP  uart_start
