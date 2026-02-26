"""
Watch List Manager Module for the BCP Accelerator.

Fetches and streams clause IDs from the watch list for a given false_lit.
Interfaces with the Watch List Memory (2-cycle read latency).

Uses registered output with valid/ready handshaking.  The read pointer
only advances when the downstream stage (Clause Prefetcher) accepts the
current clause ID via clause_id_valid & clause_id_ready.

FSM: IDLE -> READ_1 -> READ_2 -> PRESENT -> DONE -> IDLE

See: Hardware Description/BCP_Accelerator_System_Architecture.md, Sub-Module 1
"""

from amaranth import *

from memory.watch_list_memory import NUM_LITERALS, MAX_WATCH_LEN, CLAUSE_ID_WIDTH, LENGTH_WIDTH
from memory.clause_memory import MAX_CLAUSES


class WatchListManager(Elaboratable):
    """
    Watch List Manager.

    Streams clause IDs from the watch list for a given literal.
    Each clause ID is registered before being presented, and the next
    read is only issued after the downstream stage accepts the current
    output (valid & ready handshake).

    Ports
    -----
    start : Signal(), in
        Pulse high for one cycle to begin processing.
    false_lit : Signal(range(num_literals)), in
        Literal that became false (sampled when start=1).
    clause_id : Signal(range(max_clauses)), out
        Clause ID being streamed (registered).
    clause_id_valid : Signal(), out
        High when clause_id carries a valid entry.
    clause_id_ready : Signal(), in
        Downstream (Prefetcher) can accept clause_id.
    done : Signal(), out
        Asserted for one cycle when all entries have been dispatched.
    """

    def __init__(self, num_literals=NUM_LITERALS, max_clauses=MAX_CLAUSES,
                 max_watch_len=MAX_WATCH_LEN):
        self.num_literals = num_literals
        self.max_clauses = max_clauses
        self.max_watch_len = max_watch_len

        # Inputs
        self.start = Signal()
        self.false_lit = Signal(range(num_literals))
        self.clause_id_ready = Signal()  # from Prefetcher (backpressure)

        # Outputs
        self.clause_id = Signal(range(max_clauses))
        self.clause_id_valid = Signal()
        self.done = Signal()

        # Memory interface (to Watch List Memory)
        self.wl_rd_lit = Signal(range(num_literals))
        self.wl_rd_idx = Signal(range(max_watch_len))
        self.wl_rd_data = Signal(CLAUSE_ID_WIDTH)
        self.wl_rd_len = Signal(LENGTH_WIDTH)
        self.wl_rd_en = Signal()

    def elaborate(self, platform):
        m = Module()

        stored_lit = Signal(range(self.num_literals))
        watch_len = Signal(range(self.max_watch_len + 1))
        next_idx = Signal(range(self.max_watch_len + 1))
        output_count = Signal(range(self.max_watch_len + 1))
        clause_id_reg = Signal(range(self.max_clauses))
        first_read = Signal()

        with m.FSM():
            # -----------------------------------------------------------
            # IDLE: wait for start, issue first memory read (idx=0)
            # -----------------------------------------------------------
            with m.State("IDLE"):
                with m.If(self.start):
                    m.d.comb += [
                        self.wl_rd_lit.eq(self.false_lit),
                        self.wl_rd_idx.eq(0),
                        self.wl_rd_en.eq(1),
                    ]
                    m.d.sync += [
                        stored_lit.eq(self.false_lit),
                        next_idx.eq(1),
                        output_count.eq(0),
                        first_read.eq(1),
                    ]
                    m.next = "READ_1"

            # -----------------------------------------------------------
            # READ_1: First cycle of 2-cycle BRAM latency
            # -----------------------------------------------------------
            with m.State("READ_1"):
                m.next = "READ_2"

            # -----------------------------------------------------------
            # READ_2: Data arrives from BRAM (2 cycles after rd_en)
            # -----------------------------------------------------------
            with m.State("READ_2"):
                with m.If(first_read):
                    # First read: check watch list length
                    m.d.sync += [
                        watch_len.eq(self.wl_rd_len),
                        first_read.eq(0),
                    ]
                    with m.If(self.wl_rd_len == 0):
                        m.next = "DONE"
                    with m.Else():
                        m.d.sync += clause_id_reg.eq(self.wl_rd_data)
                        m.next = "PRESENT"
                with m.Else():
                    # Subsequent reads: register data
                    m.d.sync += clause_id_reg.eq(self.wl_rd_data)
                    m.next = "PRESENT"

            # -----------------------------------------------------------
            # PRESENT: Hold registered output until downstream accepts
            # -----------------------------------------------------------
            with m.State("PRESENT"):
                m.d.comb += [
                    self.clause_id.eq(clause_id_reg),
                    self.clause_id_valid.eq(1),
                ]
                with m.If(self.clause_id_ready):
                    m.d.sync += output_count.eq(output_count + 1)
                    with m.If(output_count + 1 >= watch_len):
                        m.next = "DONE"
                    with m.Else():
                        # Issue next read
                        m.d.comb += [
                            self.wl_rd_lit.eq(stored_lit),
                            self.wl_rd_idx.eq(next_idx),
                            self.wl_rd_en.eq(1),
                        ]
                        m.d.sync += next_idx.eq(next_idx + 1)
                        m.next = "READ_1"

            # -----------------------------------------------------------
            # DONE: signal completion, return to IDLE
            # -----------------------------------------------------------
            with m.State("DONE"):
                m.d.comb += self.done.eq(1)
                m.next = "IDLE"

        return m
