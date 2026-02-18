"""
Clause Database Memory Module for the BCP Accelerator.

Stores all CNF clauses (original + learned) as 84-bit entries.
Read by the Clause Prefetcher during BCP with 2-cycle read latency.

See: Hardware Description/BCP_Accelerator_System_Architecture.md, Memory Module 1
"""

from amaranth import *
from amaranth.lib.memory import Memory


# Default configuration
MAX_CLAUSES = 8192
MAX_K = 5
LIT_WIDTH = 16

# Derived
CLAUSE_WORD_WIDTH = 1 + 3 + MAX_K * LIT_WIDTH  # 84 bits


class ClauseMemory(Elaboratable):
    """
    Clause Database Memory.

    Packing (84-bit word, LSB-first):
        sat_bit [0]    | size [1:4] | lit0 [4:20] | lit1 [20:36] |
        lit2 [36:52]   | lit3 [52:68] | lit4 [68:84]

    Parameters
    ----------
    max_clauses : int
        Maximum number of clauses (default 8192).

    Ports
    -----
    rd_addr : Signal(range(max_clauses)), in
        Clause ID to read.
    rd_en : Signal(), in
        Read enable.
    rd_data_sat_bit : Signal(), out
        Satisfaction bit of the read clause.
    rd_data_size : Signal(3), out
        Number of valid literals (0-5).
    rd_data_lit0..lit4 : Signal(16), out
        Literal encodings.
    rd_valid : Signal(), out
        Data valid (asserted 2 cycles after rd_en).
    wr_addr : Signal(range(max_clauses)), in
        Clause ID to write.
    wr_data_sat_bit : Signal(), in
    wr_data_size : Signal(3), in
    wr_data_lit0..lit4 : Signal(16), in
    wr_en : Signal(), in
        Write enable.
    """

    def __init__(self, max_clauses=MAX_CLAUSES):
        self.max_clauses = max_clauses

        # Read port (to Clause Prefetcher)
        self.rd_addr = Signal(range(max_clauses))
        self.rd_en = Signal()
        self.rd_data_sat_bit = Signal()
        self.rd_data_size = Signal(3)
        self.rd_data_lit0 = Signal(LIT_WIDTH)
        self.rd_data_lit1 = Signal(LIT_WIDTH)
        self.rd_data_lit2 = Signal(LIT_WIDTH)
        self.rd_data_lit3 = Signal(LIT_WIDTH)
        self.rd_data_lit4 = Signal(LIT_WIDTH)
        self.rd_valid = Signal()

        # Write port (from software / clause learning)
        self.wr_addr = Signal(range(max_clauses))
        self.wr_data_sat_bit = Signal()
        self.wr_data_size = Signal(3)
        self.wr_data_lit0 = Signal(LIT_WIDTH)
        self.wr_data_lit1 = Signal(LIT_WIDTH)
        self.wr_data_lit2 = Signal(LIT_WIDTH)
        self.wr_data_lit3 = Signal(LIT_WIDTH)
        self.wr_data_lit4 = Signal(LIT_WIDTH)
        self.wr_en = Signal()

    def elaborate(self, platform):
        m = Module()

        # Instantiate the memory: 84-bit entries, one per clause
        m.submodules.mem = mem = Memory(
            shape=CLAUSE_WORD_WIDTH, depth=self.max_clauses, init=[]
        )

        # --- Write port (synchronous) ---
        wr_port = mem.write_port()
        wr_word = Signal(CLAUSE_WORD_WIDTH)
        m.d.comb += [
            # Pack fields into 84-bit word
            wr_word[0].eq(self.wr_data_sat_bit),
            wr_word[1:4].eq(self.wr_data_size),
            wr_word[4:20].eq(self.wr_data_lit0),
            wr_word[20:36].eq(self.wr_data_lit1),
            wr_word[36:52].eq(self.wr_data_lit2),
            wr_word[52:68].eq(self.wr_data_lit3),
            wr_word[68:84].eq(self.wr_data_lit4),
            # Drive write port
            wr_port.addr.eq(self.wr_addr),
            wr_port.data.eq(wr_word),
            wr_port.en.eq(self.wr_en),
        ]

        # --- Read port (synchronous, 1-cycle latency from BRAM) ---
        rd_port = mem.read_port(domain="sync")
        m.d.comb += [
            rd_port.addr.eq(self.rd_addr),
            rd_port.en.eq(self.rd_en),
        ]

        # Stage 1: BRAM output available after 1 clock (sync read port).
        # We register it once more to get the spec's 2-cycle latency.
        stage2_data = Signal(CLAUSE_WORD_WIDTH)
        m.d.sync += stage2_data.eq(rd_port.data)

        # Unpack stage-2 data to output fields
        m.d.comb += [
            self.rd_data_sat_bit.eq(stage2_data[0]),
            self.rd_data_size.eq(stage2_data[1:4]),
            self.rd_data_lit0.eq(stage2_data[4:20]),
            self.rd_data_lit1.eq(stage2_data[20:36]),
            self.rd_data_lit2.eq(stage2_data[36:52]),
            self.rd_data_lit3.eq(stage2_data[52:68]),
            self.rd_data_lit4.eq(stage2_data[68:84]),
        ]

        # rd_valid: 2-stage shift register on rd_en
        rd_en_pipe1 = Signal()
        rd_en_pipe2 = Signal()
        m.d.sync += [
            rd_en_pipe1.eq(self.rd_en),
            rd_en_pipe2.eq(rd_en_pipe1),
        ]
        m.d.comb += self.rd_valid.eq(rd_en_pipe2)

        return m
