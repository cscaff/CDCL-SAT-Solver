"""
Implication FIFO Module for the BCP Accelerator.

Buffers unit clause implications (variable + value + reason clause ID)
produced by the Clause Evaluator before software consumes them.
Standard synchronous circular-buffer FIFO with push/pop handshaking.

See: Hardware Description/BCP_Accelerator_System_Architecture.md, Memory Module 4
"""

from amaranth import *
from amaranth.lib.memory import Memory

from memory.assignment_memory import MAX_VARS
from memory.clause_memory import MAX_CLAUSES


# Entry packing: var_id (9-bit) + value (1-bit) + reason (13-bit) = 23 bits
ENTRY_WIDTH = 23
DEFAULT_FIFO_DEPTH = 16


class ImplicationFIFO(Elaboratable):
    """
    Implication FIFO.

    Parameters
    ----------
    fifo_depth : int
        Number of entries the FIFO can hold (default 16).

    Ports
    -----
    push_valid : Signal(), in
        Asserted by the Clause Evaluator to push an implication.
    push_var : Signal(range(MAX_VARS)), in
        Variable ID of the implied literal.
    push_value : Signal(), in
        Assigned value (0=FALSE, 1=TRUE).
    push_reason : Signal(range(MAX_CLAUSES)), in
        Clause ID that caused the implication.
    pop_valid : Signal(), out
        Asserted when the FIFO has data available to pop.
    pop_var : Signal(range(MAX_VARS)), out
        Variable ID at the head of the FIFO.
    pop_value : Signal(), out
        Value at the head of the FIFO.
    pop_reason : Signal(range(MAX_CLAUSES)), out
        Reason clause ID at the head of the FIFO.
    pop_ready : Signal(), in
        Asserted by the consumer to acknowledge and pop the head entry.
    fifo_empty : Signal(), out
        High when the FIFO contains no entries.
    fifo_full : Signal(), out
        High when the FIFO cannot accept more entries.
    """

    def __init__(self, fifo_depth=DEFAULT_FIFO_DEPTH):
        self.fifo_depth = fifo_depth

        # Push side (from Clause Evaluator)
        self.push_valid = Signal()
        self.push_var = Signal(range(MAX_VARS))
        self.push_value = Signal()
        self.push_reason = Signal(range(MAX_CLAUSES))

        # Pop side (to software)
        self.pop_valid = Signal()
        self.pop_var = Signal(range(MAX_VARS))
        self.pop_value = Signal()
        self.pop_reason = Signal(range(MAX_CLAUSES))
        self.pop_ready = Signal()

        # Status
        self.fifo_empty = Signal()
        self.fifo_full = Signal()

    def elaborate(self, platform):
        m = Module()

        depth = self.fifo_depth

        # Storage
        m.submodules.mem = mem = Memory(
            shape=ENTRY_WIDTH, depth=depth, init=[]
        )

        # Pointers and count
        wr_ptr = Signal(range(depth))
        rd_ptr = Signal(range(depth))
        count = Signal(range(depth + 1))

        # Status flags
        m.d.comb += [
            self.fifo_empty.eq(count == 0),
            self.fifo_full.eq(count == depth),
            self.pop_valid.eq(~self.fifo_empty),
        ]

        # Internal handshake signals
        do_push = Signal()
        do_pop = Signal()
        m.d.comb += [
            do_push.eq(self.push_valid & ~self.fifo_full),
            do_pop.eq(self.pop_ready & ~self.fifo_empty),
        ]

        # --- Write port (synchronous) ---
        wr_port = mem.write_port()
        push_word = Signal(ENTRY_WIDTH)
        m.d.comb += [
            # Pack: var_id [0:9] | value [9] | reason [10:23]
            push_word[0:9].eq(self.push_var),
            push_word[9].eq(self.push_value),
            push_word[10:23].eq(self.push_reason),
            wr_port.addr.eq(wr_ptr),
            wr_port.data.eq(push_word),
            wr_port.en.eq(do_push),
        ]

        # --- Read port (combinational) ---
        rd_port = mem.read_port(domain="comb")
        m.d.comb += [
            rd_port.addr.eq(rd_ptr),
            # Unpack head entry
            self.pop_var.eq(rd_port.data[0:9]),
            self.pop_value.eq(rd_port.data[9]),
            self.pop_reason.eq(rd_port.data[10:23]),
        ]

        # --- Pointer and count update (synchronous) ---
        with m.If(do_push & ~do_pop):
            m.d.sync += count.eq(count + 1)
        with m.Elif(do_pop & ~do_push):
            m.d.sync += count.eq(count - 1)
        # Simultaneous push+pop: count unchanged

        with m.If(do_push):
            with m.If(wr_ptr == depth - 1):
                m.d.sync += wr_ptr.eq(0)
            with m.Else():
                m.d.sync += wr_ptr.eq(wr_ptr + 1)

        with m.If(do_pop):
            with m.If(rd_ptr == depth - 1):
                m.d.sync += rd_ptr.eq(0)
            with m.Else():
                m.d.sync += rd_ptr.eq(rd_ptr + 1)

        return m
