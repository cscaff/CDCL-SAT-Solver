"""
Host Interface Module for the BCP Accelerator.

Decodes a byte-stream command protocol received over UART, drives the BCP
Accelerator's memory write ports and control signals, and serialises BCP
results (implications + done/conflict) back over UART.

Protocol — Host → FPGA (8N1, big-endian multi-byte fields):
  0x01 WRITE_CLAUSE   [clause_id:2][size:1][sat:1][lit0..4:10]  14 payload bytes
  0x02 WRITE_WL_ENTRY [lit:2][idx:1][clause_id:2]                5 payload bytes
  0x03 WRITE_WL_LEN   [lit:2][len:1]                             3 payload bytes
  0x04 WRITE_ASSIGN   [var:2][val:1]                             3 payload bytes
  0x05 BCP_START      [false_lit:2]                              2 payload bytes
  0x06 RESET_STATE    (none)                                      0 payload bytes

Protocol — FPGA → Host (streamed after BCP_START completes):
  0xB0 [var:2][val:1][reason:2]  — one implication  (6 bytes total)
  0xC0 [clause_id:2][0x00]       — done, no conflict (4 bytes total)
  0xC1 [clause_id:2][0x00]       — done, conflict    (4 bytes total)

FSM states:
  CMD_WAIT     — idle, waiting for the command byte
  PAYLOAD_RECV — accumulating payload bytes into buf[]
  CMD_EXEC     — one-cycle dispatch: pulse write enable or start BCP
  BCP_WAIT     — waiting for the accelerator done pulse
  IMPL_CHECK   — decide: send next implication, or the done packet
  IMPL_SEND    — serialise 6-byte implication packet over UART TX
  DONE_SEND    — serialise 4-byte done/conflict packet over UART TX
"""

from amaranth import *

from memory.clause_memory import MAX_CLAUSES, LIT_WIDTH
from memory.watch_list_memory import (NUM_LITERALS, MAX_WATCH_LEN,
                                      CLAUSE_ID_WIDTH, LENGTH_WIDTH)
from memory.assignment_memory import MAX_VARS

# ── Command bytes ──────────────────────────────────────────────────────────
CMD_WRITE_CLAUSE   = 0x01
CMD_WRITE_WL_ENTRY = 0x02
CMD_WRITE_WL_LEN   = 0x03
CMD_WRITE_ASSIGN   = 0x04
CMD_BCP_START      = 0x05
CMD_RESET_STATE    = 0x06

# ── Response bytes ─────────────────────────────────────────────────────────
RSP_IMPLICATION = 0xB0
RSP_DONE_OK     = 0xC0
RSP_DONE_CONF   = 0xC1

# Maximum payload length across all commands
MAX_PAYLOAD = 14


class HostInterface(Elaboratable):
    """
    Command decoder and response serialiser.

    Sits between the UART modules and the BCPAccelerator.  Receives byte-stream
    commands, dispatches them to the accelerator memories and control signals,
    then streams results back as byte packets.

    Ports — UART RX (from UARTReceiver)
    ------------------------------------
    rx_data  : Signal(8), in
    rx_valid : Signal(),  in  — one-cycle strobe per received byte

    Ports — UART TX (to UARTTransmitter)
    -------------------------------------
    tx_data  : Signal(8), out
    tx_valid : Signal(),  out
    tx_ready : Signal(),  in  — asserted by transmitter when it can accept a byte

    Ports — BCP Accelerator control
    --------------------------------
    bcp_start       : Signal(), out — one-cycle pulse to begin BCP
    bcp_false_lit   : Signal(range(NUM_LITERALS)), out
    bcp_done        : Signal(), in  — one-cycle pulse when BCP finishes
    bcp_conflict    : Signal(), in
    bcp_conflict_id : Signal(range(MAX_CLAUSES)), in

    Ports — BCP Accelerator implication FIFO
    -----------------------------------------
    impl_valid  : Signal(), in
    impl_var    : Signal(range(MAX_VARS)), in
    impl_value  : Signal(), in
    impl_reason : Signal(range(MAX_CLAUSES)), in
    impl_ready  : Signal(), out — one-cycle pulse to pop FIFO head

    Ports — Memory write ports (driven to BCPAccelerator write port inputs)
    ------------------------------------------------------------------------
    clause_wr_*  — Clause database
    wl_wr_*      — Watch lists
    assign_wr_*  — Variable assignments
    """

    def __init__(self):
        # UART
        self.rx_data  = Signal(8)
        self.rx_valid = Signal()
        self.tx_data  = Signal(8)
        self.tx_valid = Signal()
        self.tx_ready = Signal()

        # BCP control
        self.bcp_start       = Signal()
        self.bcp_false_lit   = Signal(range(NUM_LITERALS))
        self.bcp_done        = Signal()
        self.bcp_conflict    = Signal()
        self.bcp_conflict_id = Signal(range(MAX_CLAUSES))

        # Implication stream
        self.impl_valid  = Signal()
        self.impl_var    = Signal(range(MAX_VARS))
        self.impl_value  = Signal()
        self.impl_reason = Signal(range(MAX_CLAUSES))
        self.impl_ready  = Signal()

        # Clause DB write port
        self.clause_wr_addr    = Signal(range(MAX_CLAUSES))
        self.clause_wr_sat_bit = Signal()
        self.clause_wr_size    = Signal(3)
        self.clause_wr_lit0    = Signal(LIT_WIDTH)
        self.clause_wr_lit1    = Signal(LIT_WIDTH)
        self.clause_wr_lit2    = Signal(LIT_WIDTH)
        self.clause_wr_lit3    = Signal(LIT_WIDTH)
        self.clause_wr_lit4    = Signal(LIT_WIDTH)
        self.clause_wr_en      = Signal()

        # Watch list write port
        self.wl_wr_lit    = Signal(range(NUM_LITERALS))
        self.wl_wr_idx    = Signal(range(MAX_WATCH_LEN))
        self.wl_wr_data   = Signal(CLAUSE_ID_WIDTH)
        self.wl_wr_len    = Signal(LENGTH_WIDTH)
        self.wl_wr_en     = Signal()
        self.wl_wr_len_en = Signal()

        # Assignment write port
        self.assign_wr_addr = Signal(range(MAX_VARS))
        self.assign_wr_data = Signal(2)
        self.assign_wr_en   = Signal()

    def elaborate(self, platform):
        m = Module()

        # ── Payload receive buffer ──────────────────────────────────────────
        # Array of MAX_PAYLOAD byte registers; addressed by buf_idx at runtime.
        buf = Array([Signal(8, name=f"buf_{i}") for i in range(MAX_PAYLOAD)])
        cmd         = Signal(8)   # latched command byte
        payload_len = Signal(4)   # expected payload length (0..14)
        buf_idx     = Signal(4)   # current write position into buf (0..13)

        # ── TX shift register ───────────────────────────────────────────────
        # Holds up to 6 bytes LSB-first.  The byte at bits [0:8] is always
        # the next byte to transmit.  When a byte is accepted (tx_ready high),
        # we shift right by 8 and decrement tx_count.
        tx_shift = Signal(48)
        tx_count = Signal(3)      # bytes remaining to send (0..6)

        # ── Latched BCP result ──────────────────────────────────────────────
        conflict_reg    = Signal()
        conflict_id_reg = Signal(range(MAX_CLAUSES))

        # ── Combinational response-byte helpers ─────────────────────────────
        # Implication packet bytes (derived from impl_* ports)
        impl_b1 = Signal(8)   # var  high byte  (1 bit zero-extended)
        impl_b2 = Signal(8)   # var  low  byte
        impl_b3 = Signal(8)   # value      byte (1 bit zero-extended)
        impl_b4 = Signal(8)   # reason high byte (5 bits zero-extended)
        impl_b5 = Signal(8)   # reason low  byte
        m.d.comb += [
            impl_b1.eq(self.impl_var[8:]),
            impl_b2.eq(self.impl_var[:8]),
            impl_b3.eq(self.impl_value),
            impl_b4.eq(self.impl_reason[8:]),
            impl_b5.eq(self.impl_reason[:8]),
        ]

        # Done packet bytes (derived from latched conflict registers)
        done_b0 = Signal(8)   # 0xC0 or 0xC1
        done_b1 = Signal(8)   # conflict_id high byte
        done_b2 = Signal(8)   # conflict_id low  byte
        m.d.comb += [
            done_b0.eq(Mux(conflict_reg, RSP_DONE_CONF, RSP_DONE_OK)),
            done_b1.eq(conflict_id_reg[8:]),
            done_b2.eq(conflict_id_reg[:8]),
        ]

        # ── FSM ─────────────────────────────────────────────────────────────
        with m.FSM():

            # ----------------------------------------------------------------
            # CMD_WAIT: idle until a command byte arrives on rx_valid.
            # ----------------------------------------------------------------
            with m.State("CMD_WAIT"):
                with m.If(self.rx_valid):
                    m.d.sync += [
                        cmd.eq(self.rx_data),
                        buf_idx.eq(0),
                    ]
                    with m.Switch(self.rx_data):
                        with m.Case(CMD_WRITE_CLAUSE):
                            m.d.sync += payload_len.eq(14)
                            m.next = "PAYLOAD_RECV"
                        with m.Case(CMD_WRITE_WL_ENTRY):
                            m.d.sync += payload_len.eq(5)
                            m.next = "PAYLOAD_RECV"
                        with m.Case(CMD_WRITE_WL_LEN, CMD_WRITE_ASSIGN):
                            m.d.sync += payload_len.eq(3)
                            m.next = "PAYLOAD_RECV"
                        with m.Case(CMD_BCP_START):
                            m.d.sync += payload_len.eq(2)
                            m.next = "PAYLOAD_RECV"
                        with m.Default():
                            # 0-byte payload (e.g. RESET_STATE): execute directly
                            m.d.sync += payload_len.eq(0)
                            m.next = "CMD_EXEC"

            # ----------------------------------------------------------------
            # PAYLOAD_RECV: shift incoming bytes into buf[] one at a time.
            # ----------------------------------------------------------------
            with m.State("PAYLOAD_RECV"):
                with m.If(self.rx_valid):
                    m.d.sync += buf[buf_idx].eq(self.rx_data)
                    with m.If(buf_idx + 1 >= payload_len):
                        m.next = "CMD_EXEC"
                    with m.Else():
                        m.d.sync += buf_idx.eq(buf_idx + 1)

            # ----------------------------------------------------------------
            # CMD_EXEC: one-cycle dispatch based on the latched command byte.
            # All write enables are asserted combinationally so that exactly
            # one memory write cycle occurs.
            # ----------------------------------------------------------------
            with m.State("CMD_EXEC"):
                with m.Switch(cmd):

                    with m.Case(CMD_WRITE_CLAUSE):
                        # buf: [clause_id:2][size:1][sat:1][lit0:2][lit1:2][lit2:2][lit3:2][lit4:2]
                        m.d.comb += [
                            self.clause_wr_addr.eq(   Cat(buf[1],  buf[0])),
                            self.clause_wr_size.eq(        buf[2]),
                            self.clause_wr_sat_bit.eq(     buf[3]),
                            self.clause_wr_lit0.eq(    Cat(buf[5],  buf[4])),
                            self.clause_wr_lit1.eq(    Cat(buf[7],  buf[6])),
                            self.clause_wr_lit2.eq(    Cat(buf[9],  buf[8])),
                            self.clause_wr_lit3.eq(    Cat(buf[11], buf[10])),
                            self.clause_wr_lit4.eq(    Cat(buf[13], buf[12])),
                            self.clause_wr_en.eq(1),
                        ]
                        m.next = "CMD_WAIT"

                    with m.Case(CMD_WRITE_WL_ENTRY):
                        # buf: [lit:2][idx:1][clause_id:2]
                        m.d.comb += [
                            self.wl_wr_lit.eq(  Cat(buf[1], buf[0])),
                            self.wl_wr_idx.eq(      buf[2]),
                            self.wl_wr_data.eq( Cat(buf[4], buf[3])),
                            self.wl_wr_en.eq(1),
                        ]
                        m.next = "CMD_WAIT"

                    with m.Case(CMD_WRITE_WL_LEN):
                        # buf: [lit:2][len:1]
                        m.d.comb += [
                            self.wl_wr_lit.eq(    Cat(buf[1], buf[0])),
                            self.wl_wr_len.eq(        buf[2]),
                            self.wl_wr_len_en.eq(1),
                        ]
                        m.next = "CMD_WAIT"

                    with m.Case(CMD_WRITE_ASSIGN):
                        # buf: [var:2][val:1]
                        m.d.comb += [
                            self.assign_wr_addr.eq(Cat(buf[1], buf[0])),
                            self.assign_wr_data.eq(    buf[2]),
                            self.assign_wr_en.eq(1),
                        ]
                        m.next = "CMD_WAIT"

                    with m.Case(CMD_BCP_START):
                        # buf: [false_lit:2]
                        m.d.comb += [
                            self.bcp_false_lit.eq(Cat(buf[1], buf[0])),
                            self.bcp_start.eq(1),
                        ]
                        m.next = "BCP_WAIT"

                    with m.Default():
                        m.next = "CMD_WAIT"

            # ----------------------------------------------------------------
            # BCP_WAIT: hold until the accelerator pulses done (1 cycle).
            # Latch conflict info immediately since done is a 1-cycle pulse.
            # ----------------------------------------------------------------
            with m.State("BCP_WAIT"):
                with m.If(self.bcp_done):
                    m.d.sync += [
                        conflict_reg.eq(self.bcp_conflict),
                        conflict_id_reg.eq(self.bcp_conflict_id),
                    ]
                    m.next = "IMPL_CHECK"

            # ----------------------------------------------------------------
            # IMPL_CHECK: if the implication FIFO has data, load and send a
            # 6-byte implication packet; otherwise send the 4-byte done packet.
            # ----------------------------------------------------------------
            with m.State("IMPL_CHECK"):
                with m.If(self.impl_valid):
                    m.d.sync += [
                        tx_shift.eq(Cat(
                            Const(RSP_IMPLICATION, 8),
                            impl_b1, impl_b2, impl_b3, impl_b4, impl_b5,
                        )),
                        tx_count.eq(6),
                    ]
                    m.next = "IMPL_SEND"
                with m.Else():
                    m.d.sync += [
                        tx_shift.eq(Cat(
                            done_b0,
                            done_b1, done_b2,
                            Const(0x00, 8),
                        )),
                        tx_count.eq(4),
                    ]
                    m.next = "DONE_SEND"

            # ----------------------------------------------------------------
            # IMPL_SEND: shift out the 6 implication bytes one per tx_ready.
            # On the last byte, pulse impl_ready to pop the FIFO and return
            # to IMPL_CHECK to check for more implications.
            # ----------------------------------------------------------------
            with m.State("IMPL_SEND"):
                m.d.comb += [
                    self.tx_data.eq(tx_shift[0:8]),
                    self.tx_valid.eq(1),
                ]
                with m.If(self.tx_ready):
                    m.d.sync += [
                        tx_shift.eq(Cat(tx_shift[8:], Const(0, 8))),
                        tx_count.eq(tx_count - 1),
                    ]
                    with m.If(tx_count == 1):
                        # Last byte — pop the FIFO entry we just described
                        m.d.comb += self.impl_ready.eq(1)
                        m.next = "IMPL_CHECK"

            # ----------------------------------------------------------------
            # DONE_SEND: shift out the 4 done/conflict bytes, then idle.
            # ----------------------------------------------------------------
            with m.State("DONE_SEND"):
                m.d.comb += [
                    self.tx_data.eq(tx_shift[0:8]),
                    self.tx_valid.eq(1),
                ]
                with m.If(self.tx_ready):
                    m.d.sync += [
                        tx_shift.eq(Cat(tx_shift[8:], Const(0, 8))),
                        tx_count.eq(tx_count - 1),
                    ]
                    with m.If(tx_count == 1):
                        m.next = "CMD_WAIT"

        return m
