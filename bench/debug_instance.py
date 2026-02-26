"""Debug a single CNF instance with VCD trace output."""

import sys, os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src", "hardware"))

from amaranth.sim import Simulator
from modules.bcp_accelerator import BCPAccelerator

from .dimacs_parser import parse_dimacs
from .hw_bcp_sim import HWBCPSimulator


def debug_instance(cnf_path, vcd_path=None, max_bcp_cycles=5000):
    """Run a single instance with VCD tracing."""
    formula = parse_dimacs(cnf_path)
    print(f"Parsed {cnf_path}: {formula.num_vars} vars, {formula.num_clauses} clauses")

    sim_obj = HWBCPSimulator(formula, verbose=True)
    dut = BCPAccelerator()
    sim = Simulator(dut)
    sim.add_clock(1e-8)

    async def testbench(ctx):
        sim_obj.solver  # ensure solver exists
        # Patch dut into the simulator object and run
        is_sat = await sim_obj._run_solve(ctx, dut)
        sim_obj._result = is_sat
        sim_obj._stats = sim_obj.solver.stats

    sim.add_testbench(testbench)

    if vcd_path is None:
        vcd_path = os.path.splitext(os.path.basename(cnf_path))[0] + ".vcd"

    print(f"Writing VCD to: {vcd_path}")
    with sim.write_vcd(vcd_path):
        try:
            sim.run()
        except RuntimeError as e:
            print(f"ERROR: {e}")
            print(f"VCD written up to the point of failure — open {vcd_path} to inspect.")
            return

    c = sim_obj.counters
    print(f"Result: {'SAT' if sim_obj._result else 'UNSAT'}")
    print(f"BCP rounds: {c.bcp_rounds}, BCP cycles: {c.bcp_cycles}, "
          f"implications: {c.implications}, conflicts: {c.conflicts}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python -m bench.debug_instance <path/to/file.cnf> [output.vcd]")
        sys.exit(1)
    cnf = sys.argv[1]
    vcd = sys.argv[2] if len(sys.argv) > 2 else None
    debug_instance(cnf, vcd)
