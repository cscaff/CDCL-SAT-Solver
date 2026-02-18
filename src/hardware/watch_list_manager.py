"""
Watch List Manager Module for the BCP Accelerator.

Fetches and streams clause IDs from the watch list for a given false_lit.
Interfaces with the Watch List Memory (2-cycle read latency) using pipelined
reads to achieve one clause ID per cycle throughput during streaming.

FSM: IDLE → FETCH_LEN → STREAM → DONE → IDLE

See: Hardware Description/BCP_Accelerator_System_Architecture.md, Sub-Module 1
"""

from amaranth import *

from memory.watch_list_memory import NUM_LITERALS, MAX_WATCH_LEN, CLAUSE_ID_WIDTH, LENGTH_WIDTH
from memory.clause_memory import MAX_CLAUSES


class WatchListManager(Elaboratable):
    """
    Watch List Manager.

    Streams clause IDs from the watch list for a given literal.  Pipelined
    reads hide the Watch List Memory's 2-cycle latency so that clause IDs
    are output at one per cycle once streaming begins.

    Parameters
    ----------
    num_literals : int
        Number of literal encodings (default 1024).
    max_clauses : int
        Maximum clause count (default 8192).
    max_watch_len : int
        Maximum entries per watch list (default 100).

    Ports
    -----
    start : Signal(), in
        Pulse high for one cycle to begin processing.
    false_lit : Signal(range(num_literals)), in
        Literal that became false (sampled when start=1).
    clause_id : Signal(range(max_clauses)), out
        Clause ID being streamed.
    clause_id_valid : Signal(), out
        High when clause_id carries a valid entry.
    done : Signal(), out
        Asserted for one cycle when all entries have been dispatched.
    wl_rd_lit : Signal(range(num_literals)), out
        Literal address driven to Watch List Memory.
    wl_rd_idx : Signal(range(max_watch_len)), out
        Index within the watch list driven to Watch List Memory.
    wl_rd_en : Signal(), out
        Read-enable driven to Watch List Memory.
    wl_rd_data : Signal(CLAUSE_ID_WIDTH), in
        Clause ID returned by Watch List Memory.
    wl_rd_len : Signal(LENGTH_WIDTH), in
        Watch list length returned by Watch List Memory.
    """

    def __init__(self, num_literals=NUM_LITERALS, max_clauses=MAX_CLAUSES,
                 max_watch_len=MAX_WATCH_LEN):
        self.num_literals = num_literals
        self.max_clauses = max_clauses
        self.max_watch_len = max_watch_len

        # Inputs
        self.start = Signal()
        self.false_lit = Signal(range(num_literals))

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
        pipe_idx = Signal(range(self.max_watch_len + 1))
        output_count = Signal(range(self.max_watch_len + 1))

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
                        pipe_idx.eq(1),
                        output_count.eq(0),
                    ]
                    m.next = "FETCH_LEN"

            # -----------------------------------------------------------
            # FETCH_LEN: issue speculative read for idx=1 (pipeline fill)
            # -----------------------------------------------------------
            with m.State("FETCH_LEN"):
                m.d.comb += [
                    self.wl_rd_lit.eq(stored_lit),
                    self.wl_rd_idx.eq(pipe_idx),
                    self.wl_rd_en.eq(1),
                ]
                m.d.sync += pipe_idx.eq(pipe_idx + 1)
                m.next = "STREAM"

            # -----------------------------------------------------------
            # STREAM: output clause IDs as they arrive from the pipeline
            # -----------------------------------------------------------
            with m.State("STREAM"):
                with m.If(output_count == 0):
                    # First data arrival — length now available
                    with m.If(self.wl_rd_len == 0):
                        m.next = "DONE"
                    with m.Else():
                        m.d.comb += [
                            self.clause_id.eq(self.wl_rd_data),
                            self.clause_id_valid.eq(1),
                        ]
                        m.d.sync += [
                            watch_len.eq(self.wl_rd_len),
                            output_count.eq(1),
                        ]
                        with m.If(self.wl_rd_len == 1):
                            m.next = "DONE"
                        with m.Else():
                            # Keep pipeline fed
                            with m.If(pipe_idx < self.wl_rd_len):
                                m.d.comb += [
                                    self.wl_rd_lit.eq(stored_lit),
                                    self.wl_rd_idx.eq(pipe_idx),
                                    self.wl_rd_en.eq(1),
                                ]
                                m.d.sync += pipe_idx.eq(pipe_idx + 1)
                with m.Else():
                    # Subsequent data arrivals
                    m.d.comb += [
                        self.clause_id.eq(self.wl_rd_data),
                        self.clause_id_valid.eq(1),
                    ]
                    m.d.sync += output_count.eq(output_count + 1)
                    with m.If(output_count + 1 >= watch_len):
                        m.next = "DONE"
                    with m.Else():
                        with m.If(pipe_idx < watch_len):
                            m.d.comb += [
                                self.wl_rd_lit.eq(stored_lit),
                                self.wl_rd_idx.eq(pipe_idx),
                                self.wl_rd_en.eq(1),
                            ]
                            m.d.sync += pipe_idx.eq(pipe_idx + 1)

            # -----------------------------------------------------------
            # DONE: signal completion, return to IDLE
            # -----------------------------------------------------------
            with m.State("DONE"):
                m.d.comb += self.done.eq(1)
                m.next = "IDLE"

        return m
