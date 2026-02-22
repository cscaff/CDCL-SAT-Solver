"""
Clause Prefetcher Module for the BCP Accelerator.

Pipelines clause memory reads to hide the 2-cycle BRAM latency.
Fetches clause i+1 while clause i is being evaluated downstream.

The clause_id is pipelined through two registers so it arrives at the
output at the same time as the memory data.

Source: FYalSAT §III-C (prefetching optimization)

See: Hardware Description/BCP_Accelerator_System_Architecture.md, Module 2
"""

from amaranth import *

from memory.clause_memory import MAX_CLAUSES, LIT_WIDTH


class ClausePrefetcher(Elaboratable):
    """
    Clause Prefetcher.

    Parameters
    ----------
    max_clauses : int
        Maximum number of clauses (default 8192).

    Ports — inputs (from Watch List Manager)
    -----------------------------------------
    clause_id_in    : Signal(range(max_clauses)), in
    clause_id_valid : Signal(), in

    Ports — outputs (to Clause Evaluator)
    --------------------------------------
    clause_id_out : Signal(range(max_clauses)), out
    meta_valid    : Signal(), out
    out_sat_bit   : Signal(), out
    out_size      : Signal(3), out
    out_lit0–4    : Signal(LIT_WIDTH), out

    Ports — memory interface (to Clause Memory)
    --------------------------------------------
    clause_rd_addr      : Signal(range(max_clauses)), out
    clause_rd_en        : Signal(), out
    clause_rd_valid     : Signal(), in
    clause_rd_sat_bit   : Signal(), in
    clause_rd_size      : Signal(3), in
    clause_rd_lit0–4    : Signal(LIT_WIDTH), in
    """

    def __init__(self, max_clauses=MAX_CLAUSES):
        self.max_clauses = max_clauses

        # Inputs (from Watch List Manager)
        self.clause_id_in = Signal(range(max_clauses))
        self.clause_id_valid = Signal()

        # Outputs (to Clause Evaluator)
        self.clause_id_out = Signal(range(max_clauses))
        self.meta_valid = Signal()
        self.out_sat_bit = Signal()
        self.out_size = Signal(3)
        self.out_lit0 = Signal(LIT_WIDTH)
        self.out_lit1 = Signal(LIT_WIDTH)
        self.out_lit2 = Signal(LIT_WIDTH)
        self.out_lit3 = Signal(LIT_WIDTH)
        self.out_lit4 = Signal(LIT_WIDTH)

        # Memory interface (to Clause Memory)
        self.clause_rd_addr = Signal(range(max_clauses))
        self.clause_rd_en = Signal()
        self.clause_rd_valid = Signal()
        self.clause_rd_sat_bit = Signal()
        self.clause_rd_size = Signal(3)
        self.clause_rd_lit0 = Signal(LIT_WIDTH)
        self.clause_rd_lit1 = Signal(LIT_WIDTH)
        self.clause_rd_lit2 = Signal(LIT_WIDTH)
        self.clause_rd_lit3 = Signal(LIT_WIDTH)
        self.clause_rd_lit4 = Signal(LIT_WIDTH)

    def elaborate(self, platform):
        m = Module()

        # --- Stage 1: issue memory read, capture clause_id ---
        m.d.comb += [
            self.clause_rd_addr.eq(self.clause_id_in),
            self.clause_rd_en.eq(self.clause_id_valid),
        ]

        # Pipeline clause_id through 2 registers to match memory latency
        stage1_cid = Signal(range(self.max_clauses))
        stage1_valid = Signal()
        stage2_cid = Signal(range(self.max_clauses))
        stage2_valid = Signal()

        m.d.sync += [
            stage1_cid.eq(self.clause_id_in),
            stage1_valid.eq(self.clause_id_valid),
            stage2_cid.eq(stage1_cid),
            stage2_valid.eq(stage1_valid),
        ]

        # --- Stage 2: forward memory data + delayed clause_id ---
        m.d.comb += [
            self.meta_valid.eq(stage2_valid),
            self.clause_id_out.eq(stage2_cid),
            self.out_sat_bit.eq(self.clause_rd_sat_bit),
            self.out_size.eq(self.clause_rd_size),
            self.out_lit0.eq(self.clause_rd_lit0),
            self.out_lit1.eq(self.clause_rd_lit1),
            self.out_lit2.eq(self.clause_rd_lit2),
            self.out_lit3.eq(self.clause_rd_lit3),
            self.out_lit4.eq(self.clause_rd_lit4),
        ]

        return m
