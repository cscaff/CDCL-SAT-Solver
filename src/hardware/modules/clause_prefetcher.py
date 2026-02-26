"""
Clause Prefetcher Module for the BCP Accelerator.

Pipelines clause memory reads to hide the 2-cycle BRAM latency.
Contains a 2-entry skid buffer so that in-flight BRAM reads always have
somewhere to land, even if the downstream Clause Evaluator is stalled.

Uses valid/ready handshaking on both interfaces:
  - Upstream  (WLM):       clause_id_valid / clause_id_ready
  - Downstream (Evaluator): meta_valid / meta_ready

Source: FYalSAT III-C (prefetching optimization)

See: Hardware Description/BCP_Accelerator_System_Architecture.md, Module 2
     Notes/bcp_elastic_pipeline_spec.md, Section 2
"""

from amaranth import *

from memory.clause_memory import MAX_CLAUSES, LIT_WIDTH


class ClausePrefetcher(Elaboratable):
    """
    Clause Prefetcher with 2-entry elastic output buffer.

    Ports -- inputs (from Watch List Manager)
    ------------------------------------------
    clause_id_in    : Signal(range(max_clauses)), in
    clause_id_valid : Signal(), in

    Ports -- backpressure to WLM
    ----------------------------
    clause_id_ready : Signal(), out

    Ports -- outputs (to Clause Evaluator)
    ---------------------------------------
    clause_id_out : Signal(range(max_clauses)), out
    meta_valid    : Signal(), out
    out_sat_bit   : Signal(), out
    out_size      : Signal(3), out
    out_lit0-4    : Signal(LIT_WIDTH), out

    Ports -- backpressure from Evaluator
    ------------------------------------
    meta_ready    : Signal(), in

    Ports -- control
    ----------------
    flush         : Signal(), in   -- clear buffer & pipeline on new BCP start

    Ports -- memory interface (to Clause Memory)
    ---------------------------------------------
    clause_rd_addr      : Signal(range(max_clauses)), out
    clause_rd_en        : Signal(), out
    clause_rd_sat_bit   : Signal(), in
    clause_rd_size      : Signal(3), in
    clause_rd_lit0-4    : Signal(LIT_WIDTH), in
    """

    def __init__(self, max_clauses=MAX_CLAUSES):
        self.max_clauses = max_clauses

        # Inputs (from Watch List Manager)
        self.clause_id_in = Signal(range(max_clauses))
        self.clause_id_valid = Signal()

        # Backpressure to WLM
        self.clause_id_ready = Signal()

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

        # Backpressure from Evaluator
        self.meta_ready = Signal()

        # Control
        self.flush = Signal()

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

        BUF_DEPTH = 2

        # --- Pipeline tracking (2-stage shift register for in-flight reads) ---
        pipe1_valid = Signal()
        pipe1_cid = Signal(range(self.max_clauses))
        pipe2_valid = Signal()
        pipe2_cid = Signal(range(self.max_clauses))

        # --- 2-entry output buffer ---
        buf_cid  = Array([Signal(range(self.max_clauses), name=f"buf_cid{i}")  for i in range(BUF_DEPTH)])
        buf_sat  = Array([Signal(name=f"buf_sat{i}")                           for i in range(BUF_DEPTH)])
        buf_size = Array([Signal(3, name=f"buf_size{i}")                       for i in range(BUF_DEPTH)])
        buf_lit0 = Array([Signal(LIT_WIDTH, name=f"buf_l0_{i}")                for i in range(BUF_DEPTH)])
        buf_lit1 = Array([Signal(LIT_WIDTH, name=f"buf_l1_{i}")                for i in range(BUF_DEPTH)])
        buf_lit2 = Array([Signal(LIT_WIDTH, name=f"buf_l2_{i}")                for i in range(BUF_DEPTH)])
        buf_lit3 = Array([Signal(LIT_WIDTH, name=f"buf_l3_{i}")                for i in range(BUF_DEPTH)])
        buf_lit4 = Array([Signal(LIT_WIDTH, name=f"buf_l4_{i}")                for i in range(BUF_DEPTH)])

        wr_ptr    = Signal()                    # 0 or 1
        rd_ptr    = Signal()                    # 0 or 1
        buf_count = Signal(range(BUF_DEPTH + 1))  # 0, 1, or 2

        # --- Handshake logic ---
        # Total occupancy = buffer entries + in-flight BRAM reads
        occupancy = Signal(range(5))
        m.d.comb += occupancy.eq(buf_count + pipe1_valid + pipe2_valid)

        # Accept new clause_id when there is room for the eventual result
        m.d.comb += self.clause_id_ready.eq(occupancy < BUF_DEPTH)

        accept_input = Signal()
        m.d.comb += accept_input.eq(self.clause_id_valid & self.clause_id_ready)

        # Output from buffer head
        m.d.comb += [
            self.meta_valid.eq(buf_count > 0),
            self.clause_id_out.eq(buf_cid[rd_ptr]),
            self.out_sat_bit.eq(buf_sat[rd_ptr]),
            self.out_size.eq(buf_size[rd_ptr]),
            self.out_lit0.eq(buf_lit0[rd_ptr]),
            self.out_lit1.eq(buf_lit1[rd_ptr]),
            self.out_lit2.eq(buf_lit2[rd_ptr]),
            self.out_lit3.eq(buf_lit3[rd_ptr]),
            self.out_lit4.eq(buf_lit4[rd_ptr]),
        ]

        consume_output = Signal()
        m.d.comb += consume_output.eq(self.meta_valid & self.meta_ready)

        # --- Issue BRAM read (combinational) ---
        m.d.comb += [
            self.clause_rd_addr.eq(self.clause_id_in),
            self.clause_rd_en.eq(accept_input),
        ]

        # --- Synchronous update ---
        with m.If(self.flush):
            m.d.sync += [
                pipe1_valid.eq(0),
                pipe2_valid.eq(0),
                buf_count.eq(0),
                wr_ptr.eq(0),
                rd_ptr.eq(0),
            ]
        with m.Else():
            # Pipeline shift register
            m.d.sync += [
                pipe2_valid.eq(pipe1_valid),
                pipe2_cid.eq(pipe1_cid),
                pipe1_valid.eq(accept_input),
                pipe1_cid.eq(self.clause_id_in),
            ]

            # Buffer write: BRAM result arrives when pipe2_valid
            with m.If(pipe2_valid):
                m.d.sync += [
                    buf_cid[wr_ptr].eq(pipe2_cid),
                    buf_sat[wr_ptr].eq(self.clause_rd_sat_bit),
                    buf_size[wr_ptr].eq(self.clause_rd_size),
                    buf_lit0[wr_ptr].eq(self.clause_rd_lit0),
                    buf_lit1[wr_ptr].eq(self.clause_rd_lit1),
                    buf_lit2[wr_ptr].eq(self.clause_rd_lit2),
                    buf_lit3[wr_ptr].eq(self.clause_rd_lit3),
                    buf_lit4[wr_ptr].eq(self.clause_rd_lit4),
                    wr_ptr.eq(~wr_ptr),
                ]

            # Buffer count update
            with m.If(pipe2_valid & ~consume_output):
                m.d.sync += buf_count.eq(buf_count + 1)
            with m.Elif(consume_output & ~pipe2_valid):
                m.d.sync += buf_count.eq(buf_count - 1)
            # Simultaneous write + consume: count unchanged

            # Read pointer advance on consume
            with m.If(consume_output):
                m.d.sync += rd_ptr.eq(~rd_ptr)

        return m
