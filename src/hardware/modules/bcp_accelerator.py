"""
BCP Hardware Accelerator -- Top-Level Module.

Integrates the full BCP pipeline: Watch List Manager -> Clause Prefetcher ->
Clause Evaluator -> Implication FIFO, backed by the three memory modules
(Clause Database, Watch Lists, Variable Assignments).

Uses elastic valid/ready handshaking at every stage boundary with a global
pipeline_stall signal for immediate freeze on FIFO-full or conflict.

Provides a clean interface to the software CDCL controller: start with a
false_lit, receive implications and/or a conflict, wait for done.

See: Hardware Description/BCP_Accelerator_System_Architecture.md
     Notes/bcp_elastic_pipeline_spec.md
"""

from amaranth import *

from memory.clause_memory import ClauseMemory, MAX_CLAUSES, LIT_WIDTH
from memory.watch_list_memory import (WatchListMemory, NUM_LITERALS,
                                      MAX_WATCH_LEN, CLAUSE_ID_WIDTH, LENGTH_WIDTH)
from memory.assignment_memory import AssignmentMemory, MAX_VARS

from .watch_list_manager import WatchListManager
from .clause_prefetcher import ClausePrefetcher
from .clause_evaluator import ClauseEvaluator, UNIT, CONFLICT
from .implication_fifo import ImplicationFIFO


class BCPAccelerator(Elaboratable):
    """
    BCP Hardware Accelerator -- Top Level.

    Replaces the inner loop of CDCL propagate().  Processes all clauses
    watching a literal that became false.

    Ports -- control
    ----------------
    start     : Signal(), in   -- pulse to begin BCP
    false_lit : Signal(), in   -- literal that became false
    done      : Signal(), out  -- pulsed when BCP completes
    busy      : Signal(), out  -- high while processing

    Ports -- conflict
    -----------------
    conflict           : Signal(), out  -- held until conflict_ack
    conflict_clause_id : Signal(), out  -- held stable
    conflict_ack       : Signal(), in   -- software acknowledges conflict

    Ports -- implication stream (from FIFO)
    ---------------------------------------
    impl_valid  : Signal(), out
    impl_var    : Signal(), out
    impl_value  : Signal(), out
    impl_reason : Signal(), out
    impl_ready  : Signal(), in  -- software acknowledges / pops
    """

    def __init__(self):
        # --- Control interface ---
        self.start = Signal()
        self.false_lit = Signal(range(NUM_LITERALS))
        self.done = Signal()
        self.busy = Signal()

        # --- Conflict interface ---
        self.conflict = Signal()
        self.conflict_clause_id = Signal(range(MAX_CLAUSES))
        self.conflict_ack = Signal()

        # --- Implication interface ---
        self.impl_valid = Signal()
        self.impl_var = Signal(range(MAX_VARS))
        self.impl_value = Signal()
        self.impl_reason = Signal(range(MAX_CLAUSES))
        self.impl_ready = Signal()

        # --- Memory write ports (driven by the host interface) ---

        # Clause database write port
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

        # Assignment memory write port
        self.assign_wr_addr = Signal(range(MAX_VARS))
        self.assign_wr_data = Signal(2)
        self.assign_wr_en   = Signal()

        # --- Sub-modules (created here for external / test access) ---
        self.clause_mem = ClauseMemory()
        self.watch_mem = WatchListMemory()
        self.assign_mem = AssignmentMemory()
        self.watch_mgr = WatchListManager()
        self.prefetcher = ClausePrefetcher()
        self.evaluator = ClauseEvaluator()
        self.impl_fifo = ImplicationFIFO()

    def elaborate(self, platform):
        m = Module()

        # --- Register sub-modules ---
        clause_mem = self.clause_mem
        watch_mem = self.watch_mem
        assign_mem = self.assign_mem
        watch_mgr = self.watch_mgr
        prefetcher = self.prefetcher
        evaluator = self.evaluator
        impl_fifo = self.impl_fifo

        m.submodules.clause_mem = clause_mem
        m.submodules.watch_mem = watch_mem
        m.submodules.assign_mem = assign_mem
        m.submodules.watch_mgr = watch_mgr
        m.submodules.prefetcher = prefetcher
        m.submodules.evaluator = evaluator
        m.submodules.impl_fifo = impl_fifo

        # =============================================================
        # Pipeline stall logic
        # =============================================================

        pipeline_stall = Signal()
        conflict_reg = Signal()
        conflict_cid_reg = Signal(range(MAX_CLAUSES))

        m.d.comb += pipeline_stall.eq(
            impl_fifo.fifo_full | (conflict_reg & ~self.conflict_ack)
        )

        # =============================================================
        # Pipeline wiring (elastic valid/ready)
        # =============================================================

        # Top-level -> Watch List Manager
        m.d.comb += watch_mgr.false_lit.eq(self.false_lit)
        # watch_mgr.start is driven by the FSM below

        # Watch List Manager <-> Watch List Memory
        m.d.comb += [
            watch_mem.rd_lit.eq(watch_mgr.wl_rd_lit),
            watch_mem.rd_idx.eq(watch_mgr.wl_rd_idx),
            watch_mem.rd_en.eq(watch_mgr.wl_rd_en),
            watch_mgr.wl_rd_data.eq(watch_mem.rd_data),
            watch_mgr.wl_rd_len.eq(watch_mem.rd_len),
        ]

        # Watch List Manager -> Clause Prefetcher (with backpressure)
        m.d.comb += [
            prefetcher.clause_id_in.eq(watch_mgr.clause_id),
            prefetcher.clause_id_valid.eq(watch_mgr.clause_id_valid),
            # Backpressure: prefetcher ready, gated by pipeline_stall
            watch_mgr.clause_id_ready.eq(
                prefetcher.clause_id_ready & ~pipeline_stall),
        ]

        # Clause Prefetcher <-> Clause Memory
        m.d.comb += [
            clause_mem.rd_addr.eq(prefetcher.clause_rd_addr),
            clause_mem.rd_en.eq(prefetcher.clause_rd_en),
            prefetcher.clause_rd_valid.eq(clause_mem.rd_valid),
            prefetcher.clause_rd_sat_bit.eq(clause_mem.rd_data_sat_bit),
            prefetcher.clause_rd_size.eq(clause_mem.rd_data_size),
            prefetcher.clause_rd_lit0.eq(clause_mem.rd_data_lit0),
            prefetcher.clause_rd_lit1.eq(clause_mem.rd_data_lit1),
            prefetcher.clause_rd_lit2.eq(clause_mem.rd_data_lit2),
            prefetcher.clause_rd_lit3.eq(clause_mem.rd_data_lit3),
            prefetcher.clause_rd_lit4.eq(clause_mem.rd_data_lit4),
        ]

        # Clause Prefetcher -> Clause Evaluator (with backpressure)
        m.d.comb += [
            evaluator.clause_id_in.eq(prefetcher.clause_id_out),
            evaluator.meta_valid.eq(prefetcher.meta_valid),
            evaluator.sat_bit.eq(prefetcher.out_sat_bit),
            evaluator.size.eq(prefetcher.out_size),
            evaluator.lit0.eq(prefetcher.out_lit0),
            evaluator.lit1.eq(prefetcher.out_lit1),
            evaluator.lit2.eq(prefetcher.out_lit2),
            evaluator.lit3.eq(prefetcher.out_lit3),
            evaluator.lit4.eq(prefetcher.out_lit4),
            # Backpressure: evaluator ready, gated by pipeline_stall
            prefetcher.meta_ready.eq(
                evaluator.meta_ready & ~pipeline_stall),
        ]

        # Clause Evaluator <-> Assignment Memory
        m.d.comb += [
            assign_mem.rd_addr.eq(evaluator.assign_rd_addr),
            evaluator.assign_rd_data.eq(assign_mem.rd_data),
        ]

        # Clause Evaluator result_ready mux:
        # UNIT results need FIFO space; others are always accepted
        evaluator_result_ready = Signal()
        with m.If(evaluator.result_status == UNIT):
            m.d.comb += evaluator_result_ready.eq(~impl_fifo.fifo_full)
        with m.Else():
            m.d.comb += evaluator_result_ready.eq(1)
        m.d.comb += evaluator.result_ready.eq(evaluator_result_ready)

        # Clause Evaluator -> Implication FIFO (UNIT results only)
        m.d.comb += [
            impl_fifo.push_valid.eq(
                evaluator.result_valid
                & (evaluator.result_status == UNIT)
                & evaluator.result_ready),
            impl_fifo.push_var.eq(evaluator.result_implied_var),
            impl_fifo.push_value.eq(evaluator.result_implied_val),
            impl_fifo.push_reason.eq(evaluator.result_clause_id),
        ]

        # Implication FIFO -> Top-level interface
        m.d.comb += [
            self.impl_valid.eq(impl_fifo.pop_valid),
            self.impl_var.eq(impl_fifo.pop_var),
            self.impl_value.eq(impl_fifo.pop_value),
            self.impl_reason.eq(impl_fifo.pop_reason),
            impl_fifo.pop_ready.eq(self.impl_ready),
        ]

        # =============================================================
        # Memory write port pass-through (host interface -> memories)
        # =============================================================

        m.d.comb += [
            # Clause database
            clause_mem.wr_addr.eq(self.clause_wr_addr),
            clause_mem.wr_data_sat_bit.eq(self.clause_wr_sat_bit),
            clause_mem.wr_data_size.eq(self.clause_wr_size),
            clause_mem.wr_data_lit0.eq(self.clause_wr_lit0),
            clause_mem.wr_data_lit1.eq(self.clause_wr_lit1),
            clause_mem.wr_data_lit2.eq(self.clause_wr_lit2),
            clause_mem.wr_data_lit3.eq(self.clause_wr_lit3),
            clause_mem.wr_data_lit4.eq(self.clause_wr_lit4),
            clause_mem.wr_en.eq(self.clause_wr_en),

            # Watch lists
            watch_mem.wr_lit.eq(self.wl_wr_lit),
            watch_mem.wr_idx.eq(self.wl_wr_idx),
            watch_mem.wr_data.eq(self.wl_wr_data),
            watch_mem.wr_len.eq(self.wl_wr_len),
            watch_mem.wr_en.eq(self.wl_wr_en),
            watch_mem.wr_len_en.eq(self.wl_wr_len_en),

            # Assignments
            assign_mem.wr_addr.eq(self.assign_wr_addr),
            assign_mem.wr_data.eq(self.assign_wr_data),
            assign_mem.wr_en.eq(self.assign_wr_en),
        ]

        # =============================================================
        # Control logic
        # =============================================================

        in_flight = Signal(range(MAX_WATCH_LEN + 1))
        wlm_done_seen = Signal()
        fsm_starting = Signal()

        # Transaction signals for in-flight counting
        do_inc = watch_mgr.clause_id_valid & watch_mgr.clause_id_ready
        do_dec = evaluator.result_valid & evaluator.result_ready

        # --- In-flight counter ---
        with m.If(fsm_starting):
            m.d.sync += in_flight.eq(0)
        with m.Elif(do_inc & ~do_dec):
            m.d.sync += in_flight.eq(in_flight + 1)
        with m.Elif(do_dec & ~do_inc):
            m.d.sync += in_flight.eq(in_flight - 1)

        # --- Conflict latch (held until conflict_ack) ---
        with m.If(fsm_starting):
            m.d.sync += [
                conflict_reg.eq(0),
                conflict_cid_reg.eq(0),
            ]
        with m.Elif(evaluator.result_valid
                     & (evaluator.result_status == CONFLICT)
                     & evaluator.result_ready
                     & ~conflict_reg):
            m.d.sync += [
                conflict_reg.eq(1),
                conflict_cid_reg.eq(evaluator.result_clause_id),
            ]

        m.d.comb += [
            self.conflict.eq(conflict_reg),
            self.conflict_clause_id.eq(conflict_cid_reg),
        ]

        # --- WLM-done latch ---
        with m.If(fsm_starting):
            m.d.sync += wlm_done_seen.eq(0)
        with m.Elif(watch_mgr.done):
            m.d.sync += wlm_done_seen.eq(1)

        # --- Flush signal to pipeline stages on new BCP start ---
        m.d.comb += [
            prefetcher.flush.eq(fsm_starting),
            evaluator.flush.eq(fsm_starting),
        ]

        # --- Top-level FSM ---
        with m.FSM():
            with m.State("IDLE"):
                with m.If(self.start):
                    m.d.comb += [
                        fsm_starting.eq(1),
                        watch_mgr.start.eq(1),
                    ]
                    m.next = "ACTIVE"

            with m.State("ACTIVE"):
                m.d.comb += self.busy.eq(1)

                # Conflict detected: freeze pipeline, go to DONE
                with m.If(conflict_reg):
                    m.next = "DONE"
                # Normal completion: WLM done and all clauses drained
                with m.Elif((wlm_done_seen | watch_mgr.done)
                            & (in_flight == 0)):
                    m.next = "DONE"

            with m.State("DONE"):
                m.d.comb += self.done.eq(1)
                # If conflict, wait for ack before returning to IDLE
                with m.If(conflict_reg & ~self.conflict_ack):
                    pass  # stay in DONE
                with m.Else():
                    m.next = "IDLE"

        return m
