"""
UART Transmitter Module for the BCP Accelerator Host Interface.

Standard 8N1 UART transmitter. Accepts one byte at a time via a
valid/ready handshake, serialises it LSB-first, and drives the TX line.

Parameters
----------
divisor : int
    Clock cycles per bit = clk_freq / baud_rate.
    At 12 MHz / 1 Mbaud = 12.

Ports
-----
tx_pin   : Signal(1), out  — serial output (idle high)
tx_data  : Signal(8), in   — byte to transmit
tx_valid : Signal(),  in   — asserted by producer when tx_data is valid
tx_ready : Signal(),  out  — asserted when transmitter can accept a new byte
"""

from amaranth import *


class UARTTransmitter(Elaboratable):
    """
    8N1 UART Transmitter.

    When tx_valid & tx_ready, the byte on tx_data is latched and
    serialised: start bit (0), 8 data bits LSB-first, stop bit (1).
    tx_ready goes low for the duration of the transmission.
    """

    def __init__(self, divisor: int = 12):
        self.divisor = divisor

        self.tx_pin   = Signal(reset=1)   # idle high
        self.tx_data  = Signal(8)
        self.tx_valid = Signal()
        self.tx_ready = Signal()

    def elaborate(self, platform):
        m = Module()

        divisor = self.divisor

        bit_timer = Signal(range(divisor))
        bit_count = Signal(range(9))      # 0..7 = data bits, 8 = done
        shift_reg = Signal(8)

        with m.FSM():
            with m.State("IDLE"):
                m.d.comb += self.tx_ready.eq(1)
                m.d.comb += self.tx_pin.eq(1)          # idle high

                with m.If(self.tx_valid):
                    m.d.sync += [
                        shift_reg.eq(self.tx_data),
                        bit_timer.eq(divisor - 1),
                        bit_count.eq(0),
                    ]
                    m.next = "START"

            with m.State("START"):
                # Drive start bit (logic 0) for one full bit period
                m.d.comb += self.tx_pin.eq(0)
                with m.If(bit_timer == 0):
                    m.d.sync += bit_timer.eq(divisor - 1)
                    m.next = "DATA"
                with m.Else():
                    m.d.sync += bit_timer.eq(bit_timer - 1)

            with m.State("DATA"):
                # Drive current LSB; shift on each bit boundary
                m.d.comb += self.tx_pin.eq(shift_reg[0])
                with m.If(bit_timer == 0):
                    m.d.sync += [
                        shift_reg.eq(Cat(shift_reg[1:], 0)),
                        bit_timer.eq(divisor - 1),
                        bit_count.eq(bit_count + 1),
                    ]
                    with m.If(bit_count == 7):
                        m.next = "STOP"
                with m.Else():
                    m.d.sync += bit_timer.eq(bit_timer - 1)

            with m.State("STOP"):
                # Drive stop bit (logic 1) for one full bit period
                m.d.comb += self.tx_pin.eq(1)
                with m.If(bit_timer == 0):
                    m.next = "IDLE"
                with m.Else():
                    m.d.sync += bit_timer.eq(bit_timer - 1)

        return m
