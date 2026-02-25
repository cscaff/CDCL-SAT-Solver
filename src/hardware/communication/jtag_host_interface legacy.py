"""
JTAG Host Interface Module for the BCP Accelerator.

Uses the ECP5 JTAGG primitive to communicate with the host via JTAG instead
of UART.  Provides the same BCP-facing ports as the original HostInterface
so the BCP accelerator is unchanged.

Protocol — 128-bit DR register accessed via ER1 (IR=0x32):

  Command (host -> FPGA), shifted in via drscan:
    [127:120] cmd_byte    (0x01-0x07)
    [119:8]   payload     (14 bytes, same encoding as UART protocol)
    [7:0]     seq_num     (incremented per command, for handshake)

  Response (FPGA -> host), shifted out during same drscan:
    [127:120] status      (0x00=IDLE, 0x01=BUSY, 0xB0=IMPL, 0xC0=DONE_OK, 0xC1=DONE_CONFLICT)
    [119:104] var         (16 bits, for IMPL)
    [103:96]  val         (8 bits, for IMPL)
    [95:80]   reason      (16 bits, for IMPL) / clause_id (for DONE)
    [79:8]    reserved    (72 bits)
    [7:0]     ack_seq     (echoes seq_num when command consumed)

  New command (JTAG only): CMD_ACK_IMPL = 0x07 -- pops implication FIFO.

Clock domain crossing (2-FF synchronizer, adopted from proven bcp_engine.py):
  - Command path (jtck -> sync): jce1 & jupdate latches rx_shift into a
    stable register and asserts a valid flag.  A 2-FF synchronizer with
    edge detection in the sync domain picks up the new command.  Data is
    safe to sample because it was stable for many jtck cycles before the
    edge propagates.
  - Response path (sync -> jtck): a shadow register in the sync domain
    continuously snapshots the response word.  The jtck domain reads it
    asynchronously — standard practice for slow-changing status registers.

JTAG shift register (adapted from proven bcp_engine.py implementation):
  - Edge detection on jshift: first shift loads rsp_shadow, subsequent
    shifts shift out LSB-first via JTDO1.
  - jce1 gating (synthesis) / sel gating (simulation) ensures the shift
    register only operates when ER1 is selected.

Command processing runs independently of the FSM: when cmd_pending fires,
data is latched into registers and action-pending flags are set atomically.
Write enables pulse one cycle later from the pending flags.  The FSM only
handles the BCP lifecycle.

FSM states:
  IDLE       -- waiting for bcp_start_pending
  BCP_WAIT   -- waiting for the accelerator done pulse
  IMPL_CHECK -- decide: load next implication or the done packet
  IMPL_READY -- implication available, waiting for ack_impl_pending
  DONE_READY -- BCP finished, results loaded, waiting for next command

Constructor parameter use_jtagg_primitive (default True):
  True  -> instantiate real JTAGG primitive (for synthesis)
  False -> expose jtag_* test ports (for simulation)
"""

from amaranth import *
from amaranth.hdl import *

from memory.clause_memory import MAX_CLAUSES, LIT_WIDTH
from memory.watch_list_memory import (NUM_LITERALS, MAX_WATCH_LEN,
                                      CLAUSE_ID_WIDTH, LENGTH_WIDTH)
from memory.assignment_memory import MAX_VARS

# -- Command bytes -------------------------------------------------------------
CMD_WRITE_CLAUSE   = 0x01
CMD_WRITE_WL_ENTRY = 0x02
CMD_WRITE_WL_LEN   = 0x03
CMD_WRITE_ASSIGN   = 0x04
CMD_BCP_START      = 0x05
CMD_RESET_STATE    = 0x06
CMD_ACK_IMPL       = 0x07

# -- Response status bytes -----------------------------------------------------
RSP_IDLE        = 0x00
RSP_BUSY        = 0x01
RSP_IMPLICATION = 0xB0
RSP_DONE_OK     = 0xC0
RSP_DONE_CONF   = 0xC1

# Register width
REG_WIDTH = 128


class JTAGHostInterface(Elaboratable):
    """
    JTAG-based command decoder and response provider.

    Same BCP-facing ports as HostInterface, but communicates via JTAG
    shift register instead of UART byte stream.
    """

    def __init__(self, use_jtagg_primitive=True, diagnostic_mode=False):
        self.use_jtagg_primitive = use_jtagg_primitive
        self.diagnostic_mode = diagnostic_mode

        # -- JTAG test ports (simulation only, when use_jtagg_primitive=False) --
        if not use_jtagg_primitive:
            self.jtag_shift = Signal()    # shift clock enable
            self.jtag_update = Signal()   # update strobe (latch command)
            self.jtag_tdi = Signal()      # serial data in
            self.jtag_tdo = Signal()      # serial data out
            self.jtag_sel = Signal()      # ER1 selected

        # -- BCP control -------------------------------------------------------
        self.bcp_start       = Signal()
        self.bcp_false_lit   = Signal(range(NUM_LITERALS))
        self.bcp_done        = Signal()
        self.bcp_conflict    = Signal()
        self.bcp_conflict_id = Signal(range(MAX_CLAUSES))

        # -- Implication stream ------------------------------------------------
        self.impl_valid  = Signal()
        self.impl_var    = Signal(range(MAX_VARS))
        self.impl_value  = Signal()
        self.impl_reason = Signal(range(MAX_CLAUSES))
        self.impl_ready  = Signal()

        # -- Clause DB write port ----------------------------------------------
        self.clause_wr_addr    = Signal(range(MAX_CLAUSES))
        self.clause_wr_sat_bit = Signal()
        self.clause_wr_size    = Signal(3)
        self.clause_wr_lit0    = Signal(LIT_WIDTH)
        self.clause_wr_lit1    = Signal(LIT_WIDTH)
        self.clause_wr_lit2    = Signal(LIT_WIDTH)
        self.clause_wr_lit3    = Signal(LIT_WIDTH)
        self.clause_wr_lit4    = Signal(LIT_WIDTH)
        self.clause_wr_en      = Signal()

        # -- Watch list write port ---------------------------------------------
        self.wl_wr_lit    = Signal(range(NUM_LITERALS))
        self.wl_wr_idx    = Signal(range(MAX_WATCH_LEN))
        self.wl_wr_data   = Signal(CLAUSE_ID_WIDTH)
        self.wl_wr_len    = Signal(LENGTH_WIDTH)
        self.wl_wr_en     = Signal()
        self.wl_wr_len_en = Signal()

        # -- Assignment write port ---------------------------------------------
        self.assign_wr_addr = Signal(range(MAX_VARS))
        self.assign_wr_data = Signal(2)
        self.assign_wr_en   = Signal()

    def elaborate(self, platform):
        m = Module()

        # =====================================================================
        # JTAG interface
        # =====================================================================
        jtck = Signal()
        jtdi = Signal()
        jshift = Signal()
        jupdate = Signal()
        jrstn = Signal()
        jce1 = Signal()
        jce2 = Signal()
        jrti1 = Signal()
        jrti2 = Signal()
        jtdo1 = Signal()

        if self.use_jtagg_primitive:
            m.submodules.jtagg = Instance("JTAGG",
                o_JTCK=jtck, o_JTDI=jtdi, o_JSHIFT=jshift,
                o_JUPDATE=jupdate, o_JRSTN=jrstn,
                o_JCE1=jce1, o_JCE2=jce2,
                o_JRTI1=jrti1, o_JRTI2=jrti2,
                i_JTDO1=jtdo1, i_JTDO2=0,
            )
        else:
            m.d.comb += [
                jtck.eq(ClockSignal("jtck")),
                jshift.eq(self.jtag_shift),
                jupdate.eq(self.jtag_update),
                jtdi.eq(self.jtag_tdi),
                jce1.eq(self.jtag_sel),
                self.jtag_tdo.eq(jtdo1),
            ]

        # Response fields (set by the FSM below)
        rsp_status    = Signal(8)
        rsp_var       = Signal(16)
        rsp_val       = Signal(8)
        rsp_reason_id = Signal(16)
        ack_seq       = Signal(8)
        conflict_reg    = Signal()
        conflict_id_reg = Signal(range(MAX_CLAUSES))

        # Assemble the 128-bit response word
        jtag_data = Signal(REG_WIDTH)
        m.d.comb += jtag_data.eq(Cat(
            ack_seq,                    # [7:0]
            Const(0, 72),               # [79:8]   reserved
            rsp_reason_id,              # [95:80]
            rsp_val,                    # [103:96]
            rsp_var,                    # [119:104]
            rsp_status,                 # [127:120]
        ))

        # Shadow register for reads
        jtag_shadow = Signal(REG_WIDTH)
        m.d.sync += jtag_shadow.eq(jtag_data)

        # JTAG read shift register
        shift_reg = Signal(REG_WIDTH)
        jshift_prev = Signal()
        m.domains += ClockDomain("jtag", local=True)
        m.d.comb += ClockSignal("jtag").eq(jtck)
        m.d.jtag += jshift_prev.eq(jshift)

        with m.If(jce1):
            with m.If(jshift & ~jshift_prev):
                m.d.comb += jtdo1.eq(jtag_shadow[0])
                m.d.jtag += shift_reg.eq(Cat(jtag_shadow[1:], jtag_shadow[0]))
            with m.Elif(jshift):
                m.d.comb += jtdo1.eq(shift_reg[0])
                m.d.jtag += shift_reg.eq(Cat(shift_reg[1:], shift_reg[0]))
            with m.Elif(~jshift & jshift_prev):
                m.d.jtag += shift_reg.eq(jtag_shadow)


        # JTAG write path
        rx_shift = Signal(REG_WIDTH)
        jtag_rx = Signal(REG_WIDTH)
        jtag_rx_toggle = Signal()

        # Track whether ER1 was selected during the shift phase
        er1_was_selected = Signal()
        with m.If(jce1 & jshift):
            m.d.jtag += er1_was_selected.eq(1)
        with m.Elif(jupdate):
            m.d.jtag += er1_was_selected.eq(0)

        # Shift: gate with jce1
        with m.If(jce1 & jshift):
            m.d.jtag += rx_shift.eq(Cat(rx_shift[1:], jtdi))

        # Latch: jupdate + ER1 was selected (NOT gated by jce1)
        with m.If(jupdate & er1_was_selected):
            m.d.jtag += [
                jtag_rx.eq(rx_shift),
                jtag_rx_toggle.eq(~jtag_rx_toggle),
            ]

        # Synchronize rx into sync domain (toggle-based CDC)
        toggle_sync1 = Signal()
        toggle_sync2 = Signal()
        toggle_prev = Signal()
        rx_data_latched = Signal(REG_WIDTH)

        m.d.sync += toggle_sync1.eq(jtag_rx_toggle)
        m.d.sync += toggle_sync2.eq(toggle_sync1)
        m.d.sync += toggle_prev.eq(toggle_sync2)

        cmd_pending = Signal()
        with m.If(toggle_sync2 ^ toggle_prev):
            m.d.sync += [
                cmd_pending.eq(1),
                rx_data_latched.eq(jtag_rx),
            ]

        # ==================================================================
        # Sync domain: command processing + FSM
        # ==================================================================

        # Payload byte extraction (big-endian: buf[0] = payload[119:112])
        # payload sits at rx_data_latched[8:120]
        buf = Array([Signal(8, name=f"pbyte_{i}") for i in range(14)])
        for i in range(14):
            lo = 112 - i * 8
            m.d.comb += buf[i].eq(rx_data_latched[lo:lo+8])

        # -- Latched command data registers --------------------------------
        clause_addr_r  = Signal(range(MAX_CLAUSES))
        clause_size_r  = Signal(3)
        clause_sat_r   = Signal()
        clause_lit0_r  = Signal(LIT_WIDTH)
        clause_lit1_r  = Signal(LIT_WIDTH)
        clause_lit2_r  = Signal(LIT_WIDTH)
        clause_lit3_r  = Signal(LIT_WIDTH)
        clause_lit4_r  = Signal(LIT_WIDTH)

        wl_lit_r  = Signal(range(NUM_LITERALS))
        wl_idx_r  = Signal(range(MAX_WATCH_LEN))
        wl_data_r = Signal(CLAUSE_ID_WIDTH)
        wl_len_r  = Signal(LENGTH_WIDTH)

        assign_addr_r = Signal(range(MAX_VARS))
        assign_data_r = Signal(2)

        bcp_false_lit_r = Signal(range(NUM_LITERALS))

        # -- Action pending flags ------------------------------------------
        clause_wr_pending  = Signal()
        wl_wr_pending      = Signal()
        wl_len_pending     = Signal()
        assign_wr_pending  = Signal()
        bcp_start_pending  = Signal()
        ack_impl_pending   = Signal()
        any_cmd_processed  = Signal()

        # Auto-clear one-shot flag each cycle (overridden when cmd_pending)
        m.d.sync += any_cmd_processed.eq(0)

        # =================================================================
        # Command processing — fires on cmd_pending, independent of FSM
        # =================================================================
        cmd_byte = rx_data_latched[120:128]
        with m.If(cmd_pending):
            m.d.sync += cmd_pending.eq(0)

            # Only process real commands (skip NOP scans with cmd_byte=0x00)
            with m.If((cmd_byte >= CMD_WRITE_CLAUSE) & (cmd_byte <= CMD_ACK_IMPL)):
                m.d.sync += [
                    any_cmd_processed.eq(1),
                    ack_seq.eq(rx_data_latched[0:8]),
                ]

            with m.Switch(cmd_byte):

                with m.Case(CMD_WRITE_CLAUSE):
                    m.d.sync += [
                        clause_addr_r.eq(Cat(buf[1], buf[0])),
                        clause_size_r.eq(buf[2]),
                        clause_sat_r.eq(buf[3]),
                        clause_lit0_r.eq(Cat(buf[5], buf[4])),
                        clause_lit1_r.eq(Cat(buf[7], buf[6])),
                        clause_lit2_r.eq(Cat(buf[9], buf[8])),
                        clause_lit3_r.eq(Cat(buf[11], buf[10])),
                        clause_lit4_r.eq(Cat(buf[13], buf[12])),
                        clause_wr_pending.eq(1),
                    ]

                with m.Case(CMD_WRITE_WL_ENTRY):
                    m.d.sync += [
                        wl_lit_r.eq(Cat(buf[1], buf[0])),
                        wl_idx_r.eq(buf[2]),
                        wl_data_r.eq(Cat(buf[4], buf[3])),
                        wl_wr_pending.eq(1),
                    ]

                with m.Case(CMD_WRITE_WL_LEN):
                    m.d.sync += [
                        wl_lit_r.eq(Cat(buf[1], buf[0])),
                        wl_len_r.eq(buf[2]),
                        wl_len_pending.eq(1),
                    ]

                with m.Case(CMD_WRITE_ASSIGN):
                    m.d.sync += [
                        assign_addr_r.eq(Cat(buf[1], buf[0])),
                        assign_data_r.eq(buf[2]),
                        assign_wr_pending.eq(1),
                    ]

                with m.Case(CMD_BCP_START):
                    m.d.sync += [
                        bcp_false_lit_r.eq(Cat(buf[1], buf[0])),
                        bcp_start_pending.eq(1),
                    ]

                with m.Case(CMD_ACK_IMPL):
                    m.d.sync += ack_impl_pending.eq(1)

                with m.Case(CMD_RESET_STATE):
                    pass

        # =================================================================
        # Write-enable pulse generation (one cycle after pending is set)
        # =================================================================
        with m.If(clause_wr_pending):
            m.d.sync += clause_wr_pending.eq(0)
            m.d.comb += [
                self.clause_wr_addr.eq(clause_addr_r),
                self.clause_wr_size.eq(clause_size_r),
                self.clause_wr_sat_bit.eq(clause_sat_r),
                self.clause_wr_lit0.eq(clause_lit0_r),
                self.clause_wr_lit1.eq(clause_lit1_r),
                self.clause_wr_lit2.eq(clause_lit2_r),
                self.clause_wr_lit3.eq(clause_lit3_r),
                self.clause_wr_lit4.eq(clause_lit4_r),
                self.clause_wr_en.eq(1),
            ]

        with m.If(wl_wr_pending):
            m.d.sync += wl_wr_pending.eq(0)
            m.d.comb += [
                self.wl_wr_lit.eq(wl_lit_r),
                self.wl_wr_idx.eq(wl_idx_r),
                self.wl_wr_data.eq(wl_data_r),
                self.wl_wr_en.eq(1),
            ]

        with m.If(wl_len_pending):
            m.d.sync += wl_len_pending.eq(0)
            m.d.comb += [
                self.wl_wr_lit.eq(wl_lit_r),
                self.wl_wr_len.eq(wl_len_r),
                self.wl_wr_len_en.eq(1),
            ]

        with m.If(assign_wr_pending):
            m.d.sync += assign_wr_pending.eq(0)
            m.d.comb += [
                self.assign_wr_addr.eq(assign_addr_r),
                self.assign_wr_data.eq(assign_data_r),
                self.assign_wr_en.eq(1),
            ]

        # =================================================================
        # FSM — only handles BCP lifecycle and response status
        # =================================================================
        in_bcp_wait   = Signal()
        in_impl_ready = Signal()
        in_done_ready = Signal()

        with m.FSM():
            with m.State("IDLE"):
                m.d.comb += rsp_status.eq(RSP_IDLE)
                with m.If(bcp_start_pending):
                    m.d.sync += bcp_start_pending.eq(0)
                    m.d.comb += [
                        self.bcp_false_lit.eq(bcp_false_lit_r),
                        self.bcp_start.eq(1),
                    ]
                    m.next = "BCP_WAIT"

            with m.State("BCP_WAIT"):
                m.d.comb += rsp_status.eq(RSP_BUSY)
                m.d.sync += in_bcp_wait.eq(1)
                with m.If(self.bcp_done):
                    m.d.sync += [
                        conflict_reg.eq(self.bcp_conflict),
                        conflict_id_reg.eq(self.bcp_conflict_id),
                    ]
                    m.next = "IMPL_CHECK"

            with m.State("IMPL_CHECK"):
                m.d.comb += rsp_status.eq(RSP_BUSY)
                with m.If(self.impl_valid):
                    m.d.sync += [
                        rsp_var.eq(self.impl_var),
                        rsp_val.eq(self.impl_value),
                        rsp_reason_id.eq(self.impl_reason),
                    ]
                    m.next = "IMPL_READY"
                with m.Else():
                    m.d.sync += [
                        rsp_var.eq(0),
                        rsp_val.eq(0),
                        rsp_reason_id.eq(conflict_id_reg),
                    ]
                    m.next = "DONE_READY"

            with m.State("IMPL_READY"):
                m.d.comb += rsp_status.eq(RSP_IMPLICATION)
                m.d.sync += in_impl_ready.eq(1)
                with m.If(ack_impl_pending):
                    m.d.sync += ack_impl_pending.eq(0)
                    m.d.comb += self.impl_ready.eq(1)
                    m.next = "IMPL_CHECK"
                with m.Elif(any_cmd_processed):
                    m.next = "IDLE"

            with m.State("DONE_READY"):
                m.d.sync += in_done_ready.eq(1)
                m.d.comb += rsp_status.eq(Mux(conflict_reg, RSP_DONE_CONF, RSP_DONE_OK))
                with m.If(bcp_start_pending):
                    m.d.sync += bcp_start_pending.eq(0)
                    m.d.comb += [
                        self.bcp_false_lit.eq(bcp_false_lit_r),
                        self.bcp_start.eq(1),
                    ]
                    m.next = "BCP_WAIT"
                with m.Elif(any_cmd_processed):
                    m.next = "IDLE"

        # # ==================================================================
        # # LED Control (8 LEDs)
        # #   LED 7 — heartbeat (always)
        # #   LED 6 — flash on jupdate latch  (jtck domain pulse)
        # #   LED 5 — flash on cmd_pending    (sync domain pulse)
        # #   LED 4 — flash on JTAG shift-out (jtck domain pulse)
        # #   LED 3 — flash on cmd processed  (sync domain pulse)
        # #   LED 2 — lit in BCP_WAIT state
        # #   LED 1 — lit in IMPL_READY state
        # #   LED 0 — lit in DONE_READY state
        # # ==================================================================

        # PULSE_LEN = 50_000_000  # ~0.5 s @ 100 MHz

        # # ── Heartbeat ──────────────────────────────────────────────────────
        # heartbeat = Signal(24)
        # m.d.sync += heartbeat.eq(heartbeat + 1)

        # # ── LED 6: jupdate latch flash (jtck domain) ───────────────────────
        # dbg_latch_pulse = Signal(26)
        # with m.If(jupdate & er1_was_selected):
        #     m.d.jtag += dbg_latch_pulse.eq(PULSE_LEN)
        # with m.Elif(dbg_latch_pulse != 0):
        #     m.d.jtag += dbg_latch_pulse.eq(dbg_latch_pulse - 1)

        # # ── LED 5: cmd_pending flash (sync domain) ─────────────────────────
        # cmd_pending_prev = Signal()
        # m.d.sync += cmd_pending_prev.eq(cmd_pending)

        # dbg_cmd_pulse = Signal(26)
        # with m.If(cmd_pending & ~cmd_pending_prev):
        #     m.d.sync += dbg_cmd_pulse.eq(PULSE_LEN)
        # with m.Elif(dbg_cmd_pulse != 0):
        #     m.d.sync += dbg_cmd_pulse.eq(dbg_cmd_pulse - 1)

        # # ── LED 4: JTAG shift-out flash (jtck domain) ──────────────────────
        # dbg_shift_pulse = Signal(26)
        # with m.If(jce1 & jshift):
        #     m.d.jtag += dbg_shift_pulse.eq(PULSE_LEN)
        # with m.Elif(dbg_shift_pulse != 0):
        #     m.d.jtag += dbg_shift_pulse.eq(dbg_shift_pulse - 1)

        # # ── LED 3: command processed flash (sync domain) ──────────────────
        # dbg_proc_pulse = Signal(26)
        # with m.If(any_cmd_processed):
        #     m.d.sync += dbg_proc_pulse.eq(PULSE_LEN)
        # with m.Elif(dbg_proc_pulse != 0):
        #     m.d.sync += dbg_proc_pulse.eq(dbg_proc_pulse - 1)

        # # ── LED assignments ────────────────────────────────────────────────
        # if platform is not None:
        #     leds = [platform.request("led", i) for i in range(8)]
        #     m.d.comb += [
        #         leds[7].o.eq(heartbeat[23]),         # heartbeat
        #         leds[6].o.eq(dbg_latch_pulse != 0),  # jupdate latch
        #         leds[5].o.eq(dbg_cmd_pulse != 0),    # cmd_pending
        #         leds[4].o.eq(dbg_shift_pulse != 0),  # JTAG shift-out
        #         leds[3].o.eq(dbg_proc_pulse != 0),   # cmd processed
        #         leds[2].o.eq(in_bcp_wait),            # BCP_WAIT state
        #         leds[1].o.eq(in_impl_ready),          # IMPL_READY state
        #         leds[0].o.eq(in_done_ready),          # DONE_READY state
        #     ]

        # return m
        # ==================================================================
        # LED Control — Display command byte in binary (with hold time)
        # ==================================================================
        # Store the command byte from the last received command
        last_cmd_byte = Signal(8)
        cmd_hold_counter = Signal(28)  # ~2.6 seconds @ 100MHz
        
        # When command arrives, latch it and start hold counter
        with m.If(cmd_pending):
            m.d.sync += [
                last_cmd_byte.eq(cmd_byte),
                cmd_hold_counter.eq(268_435_455),  # Max value for 28-bit signal
            ]
        # Otherwise, decrement hold counter
        with m.Elif(cmd_hold_counter != 0):
            m.d.sync += cmd_hold_counter.eq(cmd_hold_counter - 1)
        
        # ── LED assignments — binary display of command byte ──────────────
        if platform is not None:
            leds = [platform.request("led", i) for i in range(8)]
            m.d.comb += [
                leds[7].o.eq(last_cmd_byte[7] & (cmd_hold_counter != 0)),  # MSB (only on if holding)
                leds[6].o.eq(last_cmd_byte[6] & (cmd_hold_counter != 0)),
                leds[5].o.eq(last_cmd_byte[5] & (cmd_hold_counter != 0)),
                leds[4].o.eq(last_cmd_byte[4] & (cmd_hold_counter != 0)),
                leds[3].o.eq(last_cmd_byte[3] & (cmd_hold_counter != 0)),
                leds[2].o.eq(last_cmd_byte[2] & (cmd_hold_counter != 0)),
                leds[1].o.eq(last_cmd_byte[1] & (cmd_hold_counter != 0)),
                leds[0].o.eq(last_cmd_byte[0] & (cmd_hold_counter != 0)),  # LSB
            ]

        return m