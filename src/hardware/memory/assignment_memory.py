"""
Variable Assignment Memory Module for the BCP Accelerator.

Stores the current assignment state (UNASSIGNED, FALSE, TRUE) for each variable.
Used by the Clause Evaluator to determine literal truth values during BCP.

See: Hardware Description/BCP_Accelerator_System_Architecture.md, Memory Module 3
"""

from amaranth import *
from amaranth.lib.memory import Memory


# Assignment encoding constants
UNASSIGNED = 0
FALSE = 1
TRUE = 2

# Default configuration
MAX_VARS = 512


class AssignmentMemory(Elaboratable):
    """
    Variable Assignment Memory.

    Parameters
    ----------
    max_vars : int
        Maximum number of variables (default 512).

    Ports
    -----
    rd_addr : Signal(range(max_vars)), in
        Variable ID to read.
    rd_data : Signal(2), out
        Assignment value for the addressed variable (0=UNASSIGNED, 1=FALSE, 2=TRUE).
    wr_addr : Signal(range(max_vars)), in
        Variable ID to write.
    wr_data : Signal(2), in
        Assignment value to write.
    wr_en : Signal(), in
        Write enable.
    """

    def __init__(self, max_vars=MAX_VARS):
        self.max_vars = max_vars

        # Read port (to Clause Evaluator)
        self.rd_addr = Signal(range(max_vars))
        self.rd_data = Signal(2)

        # Write port (from software when variables are assigned)
        self.wr_addr = Signal(range(max_vars))
        self.wr_data = Signal(2)
        self.wr_en = Signal()

    def elaborate(self, platform):
        m = Module()

        # Instantiate the memory: 2-bit entries, one per variable
        m.submodules.mem = mem = Memory(
            shape=2, depth=self.max_vars, init=[]
        )

        # Read port - combinational (transparent) for single-cycle reads
        rd_port = mem.read_port(domain="comb")
        m.d.comb += [
            rd_port.addr.eq(self.rd_addr),
            self.rd_data.eq(rd_port.data),
        ]

        # Write port - synchronous
        wr_port = mem.write_port()
        m.d.comb += [
            wr_port.addr.eq(self.wr_addr),
            wr_port.data.eq(self.wr_data),
            wr_port.en.eq(self.wr_en),
        ]

        return m
