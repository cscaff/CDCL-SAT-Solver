"""
Watch List Memory Module for the BCP Accelerator.

Maps each literal to its list of watching clause IDs.
Read by the Watch List Manager during BCP with 2-cycle read latency.

Two separate memories:
  - Length memory: 7-bit × NUM_LITERALS entries
  - Clause ID memory: 13-bit × (NUM_LITERALS * MAX_WATCH_LEN) entries

See: Hardware Description/BCP_Accelerator_System_Architecture.md, Memory Module 2
"""

from amaranth import *
from amaranth.lib.memory import Memory


# Default configuration
NUM_LITERALS = 1024
MAX_WATCH_LEN = 100
CLAUSE_ID_WIDTH = 13
LENGTH_WIDTH = 7


class WatchListMemory(Elaboratable):
    """
    Watch List Memory.

    Stores, for each literal, the list of clause IDs that watch it.
    Two backing memories: one for list lengths, one for clause IDs.

    Parameters
    ----------
    num_literals : int
        Maximum number of literal encodings (default 1024).
    max_watch_len : int
        Maximum watch list length per literal (default 100).

    Ports
    -----
    rd_lit : Signal(range(num_literals)), in
        Literal encoding to read.
    rd_idx : Signal(range(max_watch_len)), in
        Index within the literal's watch list.
    rd_en : Signal(), in
        Read enable — reads both length and clause ID simultaneously.
    rd_len : Signal(LENGTH_WIDTH), out
        Watch list length for rd_lit (valid 2 cycles after rd_en).
    rd_data : Signal(CLAUSE_ID_WIDTH), out
        Clause ID at rd_lit's watch list position rd_idx (valid 2 cycles after rd_en).
    rd_valid : Signal(), out
        Data valid (asserted 2 cycles after rd_en).
    wr_lit : Signal(range(num_literals)), in
        Literal encoding to write.
    wr_idx : Signal(range(max_watch_len)), in
        Index within the literal's watch list to write.
    wr_data : Signal(CLAUSE_ID_WIDTH), in
        Clause ID to write.
    wr_len : Signal(LENGTH_WIDTH), in
        Watch list length to write.
    wr_en : Signal(), in
        Write enable for clause ID memory.
    wr_len_en : Signal(), in
        Write enable for length memory.
    """

    def __init__(self, num_literals=NUM_LITERALS, max_watch_len=MAX_WATCH_LEN):
        self.num_literals = num_literals
        self.max_watch_len = max_watch_len

        # Read port (to Watch List Manager)
        self.rd_lit = Signal(range(num_literals))
        self.rd_idx = Signal(range(max_watch_len))
        self.rd_en = Signal()
        self.rd_len = Signal(LENGTH_WIDTH)
        self.rd_data = Signal(CLAUSE_ID_WIDTH)
        self.rd_valid = Signal()

        # Write port (from software / watch list updates)
        self.wr_lit = Signal(range(num_literals))
        self.wr_idx = Signal(range(max_watch_len))
        self.wr_data = Signal(CLAUSE_ID_WIDTH)
        self.wr_len = Signal(LENGTH_WIDTH)
        self.wr_en = Signal()
        self.wr_len_en = Signal()

    def elaborate(self, platform):
        m = Module()

        # --- Length memory: 7-bit × num_literals ---
        m.submodules.len_mem = len_mem = Memory(
            shape=LENGTH_WIDTH, depth=self.num_literals, init=[]
        )

        # --- Clause ID memory: 13-bit × (num_literals * max_watch_len) ---
        clause_id_depth = self.num_literals * self.max_watch_len
        m.submodules.cid_mem = cid_mem = Memory(
            shape=CLAUSE_ID_WIDTH, depth=clause_id_depth, init=[]
        )

        # Composite address for clause ID memory: lit * max_watch_len + idx
        rd_cid_addr = Signal(range(clause_id_depth))
        wr_cid_addr = Signal(range(clause_id_depth))
        m.d.comb += [
            rd_cid_addr.eq(self.rd_lit * self.max_watch_len + self.rd_idx),
            wr_cid_addr.eq(self.wr_lit * self.max_watch_len + self.wr_idx),
        ]

        # --- Length memory write port ---
        len_wr = len_mem.write_port()
        m.d.comb += [
            len_wr.addr.eq(self.wr_lit),
            len_wr.data.eq(self.wr_len),
            len_wr.en.eq(self.wr_len_en),
        ]

        # --- Length memory read port (synchronous) ---
        len_rd = len_mem.read_port(domain="sync")
        m.d.comb += [
            len_rd.addr.eq(self.rd_lit),
            len_rd.en.eq(self.rd_en),
        ]

        # Stage 2 pipeline register for length
        stage2_len = Signal(LENGTH_WIDTH)
        m.d.sync += stage2_len.eq(len_rd.data)
        m.d.comb += self.rd_len.eq(stage2_len)

        # --- Clause ID memory write port ---
        cid_wr = cid_mem.write_port()
        m.d.comb += [
            cid_wr.addr.eq(wr_cid_addr),
            cid_wr.data.eq(self.wr_data),
            cid_wr.en.eq(self.wr_en),
        ]

        # --- Clause ID memory read port (synchronous) ---
        cid_rd = cid_mem.read_port(domain="sync")
        m.d.comb += [
            cid_rd.addr.eq(rd_cid_addr),
            cid_rd.en.eq(self.rd_en),
        ]

        # Stage 2 pipeline register for clause ID
        stage2_data = Signal(CLAUSE_ID_WIDTH)
        m.d.sync += stage2_data.eq(cid_rd.data)
        m.d.comb += self.rd_data.eq(stage2_data)

        # --- rd_valid: 2-stage shift register on rd_en ---
        rd_en_pipe1 = Signal()
        rd_en_pipe2 = Signal()
        m.d.sync += [
            rd_en_pipe1.eq(self.rd_en),
            rd_en_pipe2.eq(rd_en_pipe1),
        ]
        m.d.comb += self.rd_valid.eq(rd_en_pipe2)

        return m
