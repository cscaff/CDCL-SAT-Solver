"""
BCP Hardware Accelerator — Top-Level Module.

Integrates the full BCP pipeline: Watch List Manager → Clause Prefetcher →
Clause Evaluator → Implication FIFO, backed by the three memory modules
(Clause Database, Watch Lists, Variable Assignments).

Provides a clean interface to the software CDCL controller: start with a
false_lit, receive implications and/or a conflict, wait for done.

See: Hardware Description/BCP_Accelerator_System_Architecture.md
"""

from amaranth import *

from memory.clause_memory import ClauseMemory, MAX_CLAUSES
from memory.watch_list_memory import WatchListMemory, NUM_LITERALS, MAX_WATCH_LEN
from memory.assignment_memory import AssignmentMemory, MAX_VARS

from watch_list_manager import WatchListManager
from clause_prefetcher import ClausePrefetcher
from clause_evaluator import ClauseEvaluator, UNIT, CONFLICT
from implication_fifo import ImplicationFIFO


class BCPAccelerator(Elaboratable):
    """
    BCP Hardware Accelerator — Top Level.

    Replaces the inner loop of CDCL propagate().  Processes all clauses
    watching a literal that became false.

    Ports — control
    ----------------
    start     : Signal(), in   — pulse to begin BCP
    false_lit : Signal(), in   — literal that became false
    done      : Signal(), out  — pulsed when BCP completes
    busy      : Signal(), out  — high while processing

    Ports — conflict
    -----------------
    conflict           : Signal(), out
    conflict_clause_id : Signal(), out

    Ports — implication stream (from FIFO)
    ---------------------------------------
    impl_valid  : Signal(), out
    impl_var    : Signal(), out
    impl_value  : Signal(), out
    impl_reason : Signal(), out
    impl_ready  : Signal(), in  — software acknowledges / pops
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

        # --- Implication interface ---
        self.impl_valid = Signal()
        self.impl_var = Signal(range(MAX_VARS))
        self.impl_value = Signal()
        self.impl_reason = Signal(range(MAX_CLAUSES))
        self.impl_ready = Signal()

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
        # Pipeline wiring
        # =============================================================

        # Top-level → Watch List Manager
        m.d.comb += watch_mgr.false_lit.eq(self.false_lit)
        # watch_mgr.start is driven by the FSM below

        # Watch List Manager ↔ Watch List Memory
        m.d.comb += [
            watch_mem.rd_lit.eq(watch_mgr.wl_rd_lit),
            watch_mem.rd_idx.eq(watch_mgr.wl_rd_idx),
            watch_mem.rd_en.eq(watch_mgr.wl_rd_en),
            watch_mgr.wl_rd_data.eq(watch_mem.rd_data),
            watch_mgr.wl_rd_len.eq(watch_mem.rd_len),
        ]

        # Watch List Manager → Clause Prefetcher
        m.d.comb += [
            prefetcher.clause_id_in.eq(watch_mgr.clause_id),
            prefetcher.clause_id_valid.eq(watch_mgr.clause_id_valid),
        ]

        # Clause Prefetcher ↔ Clause Memory
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

        # Clause Prefetcher → Clause Evaluator
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
        ]

        # Clause Evaluator ↔ Assignment Memory
        m.d.comb += [
            assign_mem.rd_addr.eq(evaluator.assign_rd_addr),
            evaluator.assign_rd_data.eq(assign_mem.rd_data),
        ]

        # Clause Evaluator → Implication FIFO (UNIT results)
        m.d.comb += [
            impl_fifo.push_valid.eq(
                evaluator.result_valid & (evaluator.result_status == UNIT)),
            impl_fifo.push_var.eq(evaluator.result_implied_var),
            impl_fifo.push_value.eq(evaluator.result_implied_val),
            impl_fifo.push_reason.eq(evaluator.result_clause_id),
        ]

        # Implication FIFO → Top-level interface
        m.d.comb += [
            self.impl_valid.eq(impl_fifo.pop_valid),
            self.impl_var.eq(impl_fifo.pop_var),
            self.impl_value.eq(impl_fifo.pop_value),
            self.impl_reason.eq(impl_fifo.pop_reason),
            impl_fifo.pop_ready.eq(self.impl_ready),
        ]

        # =============================================================
        # Control logic
        # =============================================================

        in_flight = Signal(range(MAX_WATCH_LEN + 1))
        conflict_reg = Signal()
        conflict_cid_reg = Signal(range(MAX_CLAUSES))
        wlm_done_seen = Signal()
        fsm_starting = Signal()

        do_inc = watch_mgr.clause_id_valid
        do_dec = evaluator.result_valid

        # --- In-flight counter ---
        with m.If(fsm_starting):
            m.d.sync += in_flight.eq(0)
        with m.Elif(do_inc & ~do_dec):
            m.d.sync += in_flight.eq(in_flight + 1)
        with m.Elif(do_dec & ~do_inc):
            m.d.sync += in_flight.eq(in_flight - 1)

        # --- Conflict latch ---
        with m.If(fsm_starting):
            m.d.sync += [
                conflict_reg.eq(0),
                conflict_cid_reg.eq(0),
            ]
        with m.Elif(evaluator.result_valid
                     & (evaluator.result_status == CONFLICT)
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

        # --- Top-level FSM ---
        detect_conflict = (evaluator.result_valid
                           & (evaluator.result_status == CONFLICT))

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

                with m.If(conflict_reg | detect_conflict):
                    m.next = "DONE"
                with m.Elif((wlm_done_seen | watch_mgr.done)
                            & (in_flight == 0)):
                    m.next = "DONE"

            with m.State("DONE"):
                m.d.comb += self.done.eq(1)
                m.next = "IDLE"

        return m
