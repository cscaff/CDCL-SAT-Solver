Let's go ahead and create a benchmarking suite that runs the CDCL.c algorithm with a call to the hardware accelerator in simulation.

Here are some previous LLM notes:

Getting the Benchmarks
The standard format for SAT instances is DIMACS CNF (.cnf files). You can download them directly from SATLIB, which has them pre-formatted and ready to use. The SAT competition archives also use this format. Your simulator will need a parser for this format — it's quite simple, with lines starting with 'p' for the problem definition and 'c' for comments, followed by clause listings.
What to Measure
Since you're in simulation, wall-clock time isn't meaningful. Instead you should track cycle counts as your primary performance metric, along with memory access counts (reads/writes), number of propagations or flips depending on whether you're doing CDCL or SLS (NOTE: WE ARE DOING CDCL), and resource utilization if your simulator models that. SAT-Accel uses milliseconds but derives it from their clock frequency (230 MHz), so you can do the same — take your cycle count and divide by your target clock frequency to get an estimated real-world time.
Comparing Against Software Solvers
This is actually the most practical comparison path. You'd run the same CNF benchmarks through a real CPU solver like MiniSat or Kissat, record their solve times and propagation counts, then compare against your simulated cycle counts converted to time at your target frequency. Both MiniSat and Kissat are easy to compile and run on Linux. Kissat in particular prints detailed statistics including propagation counts, which maps well to what you'd track in simulation.
Practical Workflow
The typical flow would look like: pick a benchmark CNF, run it through Kissat/MiniSat and record stats, feed the same CNF into your simulator, compare cycle-equivalent time against the CPU solver time, and repeat across the benchmark suite to get average speedup numbers like the papers report.
One thing worth flagging — since you're in simulation, you won't be able to replicate the memory latency effects that FYalSAT focuses heavily on (their DRAM bandwidth analysis). SAT-Accel's approach is probably more simulation-friendly since its bottlenecks are more compute and logic oriented rather than memory bandwidth oriented. That might be worth keeping in mind when deciding which benchmarks to prioritize.

Given these notes, can you help me build a benchmarking suite starting with https://www.cs.ubc.ca/~hoos/SATLIB/benchm.html SATLIB?



====== NOTES ======
 Here is Claude's plan:
╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌
 Benchmarking Suite Plan

 Context

 We need a benchmarking suite that runs SATLIB benchmarks through the CDCL solver with
 hardware BCP acceleration in Amaranth simulation. This measures cycle counts,
 propagation/implication counts, and compares against software solvers to quantify hardware
 speedup.

 Architecture: Python-Only

 The entire suite runs in Python. A Python CDCL solver (ported from CDCL.c) runs inside an
 Amaranth testbench coroutine, calling the hardware BCP accelerator via the existing
 JTAGDriver simulation protocol. This avoids the C↔Python bridge complexity and makes cycle
 counting trivial.

 File Structure

 bench/
   __init__.py
   dimacs_parser.py        # DIMACS CNF parser + hw constraint validation
   cdcl_solver.py          # Python CDCL solver (port of CDCL.c)
   hw_bcp_sim.py           # Hardware BCP simulation bridge + cycle counters
   benchmark_runner.py     # CLI harness: run benchmarks, collect results
   download_benchmarks.py  # Fetch SATLIB tarballs (uf20, uf50, uf75)
   benchmarks/             # Downloaded CNFs (gitignored)
   results/                # Output JSON results (gitignored)

 Phase 1: DIMACS Parser — bench/dimacs_parser.py

 What: Parse .cnf files, validate against hardware limits.

 - CNFFormula dataclass: num_vars, num_clauses, clauses (list of signed-int lists), filename
 - parse_dimacs(filepath) -> CNFFormula: skip c lines, parse p cnf header, read clause
 literals terminated by 0
 - validate_hw_constraints(formula) -> list[str]: check MAX_VARS=512, MAX_CLAUSES=8192,
 MAX_K=5, watch list lengths ≤100
 - Literal encoding helpers matching CDCL.c: lit_to_code(lit) = 2*lit if pos, 2*(-lit)+1 if
 neg

 Test: Parse a hand-crafted CNF, verify clause count and literal codes.

 Phase 2: Python CDCL Solver — bench/cdcl_solver.py

 What: Direct port of src/software/CDCL.c with pluggable BCP.

 Key structures (matching C solver exactly):
 - assigns[]: -1=UNASSIGNED, 0=FALSE, 1=TRUE (1-indexed)
 - watches[lit_code]: list of clause indices (two-watched-literal scheme)
 - trail[], prop_head, trail_delimiters[], num_decisions
 - VSIDS: activity[], var_inc, decay=0.95, rescale at 1e100

 Key methods:
 - solve() -> (is_sat, SolverStats) — main CDCL loop
 - propagate_sw() -> int — software BCP (port of propagate())
 - analyze(conflict_ci) -> (learnt_lits, bt_level) — first-UIP
 - backtrack(level), pick_decision_var() -> int

 SolverStats dataclass: decisions, conflicts, propagations, implications, learned_clauses

 Test: Solve uf20 instances in SW-only mode, verify all return SAT.

 Reference files:
 - src/software/CDCL.c — every function ported line-by-line
 - src/software/CDCL.h — data structure definitions

 Phase 3: Hardware BCP Bridge — bench/hw_bcp_sim.py

 What: Run the CDCL solver inside an Amaranth testbench, delegating BCP to the hardware
 accelerator.

 Reuse from test/hardware/test_integration_jtag.py:
 - JTAGDriver class (send_cmd, send_and_wait, read_response, decode)
 - build_command() and all encode_* payload helpers

 New classes:
 - InstrumentedJTAGDriver(JTAGDriver) — wraps every operation with cycle/scan counters
 - CycleCounters dataclass — jtag_scans, sync_ticks, bcp_sync_cycles, bcp_rounds,
 implications, per-round cycle list
 - HWBCPSimulator — main entry point

 Core flow (all async, inside testbench coroutine):
 1. _hw_init(ctx, drv) — upload all clauses, watch lists, assignments via JTAG (mirrors
 hw_init() in hw_interface_jtag.c)
 2. _solve_with_hw_bcp(ctx, drv, solver) — CDCL loop where propagate() calls _hw_propagate()
 3. _hw_propagate(ctx, drv, solver) — for each pending trail entry: CMD_BCP_START, poll for
 implications/done, enqueue implications (mirrors hw_propagate() in hw_interface_jtag.c)
 4. _hw_sync_assigns(ctx, drv, solver) — after backtrack, write unassigned vars to HW
 5. After learning a clause: upload it via CMD_WRITE_CLAUSE + CMD_WRITE_WL_ENTRY +
 CMD_WRITE_WL_LEN

 Assignment encoding conversion: SW(-1,0,1) ↔ HW(0,1,2)

 Simulation setup:
 dut = BCPTopJTAG(use_jtagg_primitive=False)
 sim.add_clock(1e-8)                    # 100 MHz sync
 sim.add_clock(1.3e-7, domain="jtck")   # ~7.7 MHz JTAG

 Test: Replicate the implication chain from test_integration_jtag.py via the new bridge, then
 solve a trivial 3-var CNF end-to-end.

 Reference files:
 - test/hardware/test_integration_jtag.py — JTAGDriver, protocol, timing
 - src/software/hw_interface_jtag.c — hw_init, hw_propagate, hw_sync_assigns flow
 - src/hardware/top_jtag.py — BCPTopJTAG constructor

 Phase 4: Benchmark Runner — bench/benchmark_runner.py + bench/download_benchmarks.py

 What: CLI tool to download benchmarks, run them, and output results.

 download_benchmarks.py:
 - Download SATLIB tarballs for uf20-91, uf50-218, uf75-325 (satisfiable 3-SAT)
 - Extract to bench/benchmarks/
 - URL pattern: https://www.cs.ubc.ca/~hoos/SATLIB/Benchmarks/SAT/RND3SAT/uf20-91.tar.gz

 benchmark_runner.py:
 - CLI: python -m bench.benchmark_runner --family uf20-91 --mode hw_sim --max-instances 10
 - Modes: hw_sim (hardware BCP), sw_only (Python software BCP), both
 - For each CNF: parse, validate, solve, collect stats + cycle counts
 - Output: JSON results file in bench/results/
 - Summary table printed to stdout: instance, SAT/UNSAT, decisions, conflicts, implications,
 BCP cycles, estimated time at target freq

 Add Makefile targets:
 - bench-download: fetch SATLIB benchmarks
 - bench-run: run benchmarking suite
 - bench-sw: run software-only baseline

 Phase 5: External Solver Comparison (future)

 Not in initial scope. Later: add comparison.py to run kissat/minisat via subprocess and
 compare stats.

 Verification

 1. Phase 1: python -c "from bench.dimacs_parser import parse_dimacs; f =
 parse_dimacs('bench/benchmarks/uf20-91/uf20-01.cnf'); print(f.num_vars, f.num_clauses)"
 2. Phase 2: python -c "from bench.cdcl_solver import CDCLSolver; from bench.dimacs_parser
 import parse_dimacs; s = CDCLSolver(parse_dimacs('bench/benchmarks/uf20-91/uf20-01.cnf'));
 print(s.solve())" → should print (True, SolverStats(...))
 3. Phase 3: Run a uf20 instance through HW sim, verify SAT result matches SW-only
 4. Phase 4: python -m bench.benchmark_runner --family uf20-91 --max-instances 5 --mode both →
  results JSON with cycle counts
 5. Existing tests still pass: make test-hw → 12/12 pass

  ⎿  You've hit your limit · resets 1pm (America/New_York)
                                                                  
Basic Usage                                                                                  
                                                                                               
  python -m bench.benchmark_runner --family <FAMILY> --mode <MODE> [--max-instances N]         
  [--verbose]                                                                                  
                                                                                               
  Modes                                                                                        
                                                                                               
  ┌─────────┬──────────────────────────────────────────────────┐                               
  │  Mode   │                   Description                    │                               
  ├─────────┼──────────────────────────────────────────────────┤
  │ sw_only │ Pure Python CDCL solver (default)                │
  ├─────────┼──────────────────────────────────────────────────┤
  │ hw_sim  │ Hardware-accelerated BCP via Amaranth simulation │
  ├─────────┼──────────────────────────────────────────────────┤
  │ both    │ Side-by-side comparison of both                  │
  └─────────┴──────────────────────────────────────────────────┘

  Available Families

  ┌──────────┬──────────────────────┐
  │  Family  │         Size         │
  ├──────────┼──────────────────────┤
  │ uf20-91  │ 20 vars, 91 clauses  │
  ├──────────┼──────────────────────┤
  │ uf50-218 │ 50 vars, 218 clauses │
  ├──────────┼──────────────────────┤
  │ uf75-325 │ 75 vars, 325 clauses │
  └──────────┴──────────────────────┘

  Download missing families with:
  python -m bench.download_benchmarks uf50-218 uf75-325

  Examples

  # Software-only, first 5 instances
  python -m bench.benchmark_runner --family uf20-91 --mode sw_only --max-instances 5

  # Hardware sim, all instances
  python -m bench.benchmark_runner --family uf20-91 --mode hw_sim

  # Compare both modes on 10 instances with verbose output
  python -m bench.benchmark_runner --family uf20-91 --mode both --max-instances 10 --verbose

  Flags

  - --max-instances N — limit how many instances to run (0 = all)
  - --verbose — detailed output during solving

  Output

  Results print a summary table to stdout and save JSON to
  bench/results/<family>_<mode>_<timestamp>.json. HW-sim mode tracks cycle counts, BCP rounds,
  implications, and estimates real hardware time at 100 MHz.