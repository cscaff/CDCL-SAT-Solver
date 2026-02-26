"""
Benchmark runner CLI: run SATLIB benchmarks through SW-only or HW-sim CDCL solver.

Usage:
  python -m bench.benchmark_runner --family uf20-91 --mode sw_only --max-instances 10
  python -m bench.benchmark_runner --family uf20-91 --mode hw_sim --max-instances 5
  python -m bench.benchmark_runner --family uf20-91 --mode both --max-instances 5
"""

import argparse
import glob
import json
import os
import sys
import time
from dataclasses import asdict

from .dimacs_parser import parse_dimacs, validate_hw_constraints
from .cdcl_solver import CDCLSolver

BENCH_DIR = os.path.join(os.path.dirname(__file__), "benchmarks")
RESULTS_DIR = os.path.join(os.path.dirname(__file__), "results")

TARGET_FREQ_MHZ = 100  # for cycle-time estimates


def run_sw_only(formula):
    """Run software-only CDCL solve. Returns dict of results."""
    solver = CDCLSolver(formula)
    t0 = time.perf_counter()
    is_sat, stats = solver.solve()
    elapsed = time.perf_counter() - t0

    return {
        "mode": "sw_only",
        "sat": is_sat,
        "time_s": round(elapsed, 6),
        "decisions": stats.decisions,
        "conflicts": stats.conflicts,
        "propagations": stats.propagations,
        "implications": stats.implications,
        "learned_clauses": stats.learned_clauses,
    }


def run_hw_sim(formula):
    """Run hardware-accelerated BCP in Amaranth simulation. Returns dict of results."""
    from .hw_bcp_sim import HWBCPSimulator

    sim = HWBCPSimulator(formula, verbose=False)
    t0 = time.perf_counter()
    is_sat, stats, counters = sim.run()
    elapsed = time.perf_counter() - t0

    # Estimate real-time at target frequency
    estimated_hw_s = counters.bcp_cycles / (TARGET_FREQ_MHZ * 1e6)

    return {
        "mode": "hw_sim",
        "sat": is_sat,
        "sim_time_s": round(elapsed, 3),
        "decisions": stats.decisions,
        "conflicts": stats.conflicts,
        "propagations": stats.propagations,
        "implications": stats.implications,
        "learned_clauses": stats.learned_clauses,
        "total_cycles": counters.total_cycles,
        "bcp_cycles": counters.bcp_cycles,
        "init_cycles": counters.init_cycles,
        "sync_cycles": counters.sync_cycles,
        "bcp_rounds": counters.bcp_rounds,
        "hw_implications": counters.implications,
        "hw_conflicts": counters.conflicts,
        "estimated_hw_time_s": round(estimated_hw_s, 9),
    }


def run_benchmark(filepath, mode, verbose=False):
    """Run a single benchmark instance. Returns result dict."""
    formula = parse_dimacs(filepath)
    errors = validate_hw_constraints(formula)
    if errors and mode in ("hw_sim", "both"):
        return {
            "file": os.path.basename(filepath),
            "skipped": True,
            "errors": errors,
        }

    result = {"file": os.path.basename(filepath), "num_vars": formula.num_vars,
              "num_clauses": formula.num_clauses}

    if mode == "sw_only":
        result["sw"] = run_sw_only(formula)
    elif mode == "hw_sim":
        result["hw"] = run_hw_sim(formula)
    elif mode == "both":
        result["sw"] = run_sw_only(formula)
        result["hw"] = run_hw_sim(formula)

    return result


def find_cnf_files(family):
    """Find all .cnf files for a benchmark family."""
    family_dir = os.path.join(BENCH_DIR, family)
    if not os.path.isdir(family_dir):
        print(f"Error: benchmark family '{family}' not found at {family_dir}")
        print("Run: python -m bench.download_benchmarks")
        sys.exit(1)
    files = sorted(glob.glob(os.path.join(family_dir, "*.cnf")))
    if not files:
        print(f"Error: no .cnf files in {family_dir}")
        sys.exit(1)
    return files


def print_summary_table(results, mode):
    """Print a summary table to stdout."""
    print()
    if mode in ("sw_only", "both"):
        print(f"{'Instance':<20} {'SAT':>5} {'Dec':>6} {'Conf':>6} "
              f"{'Props':>6} {'Impls':>7} {'Time(s)':>10}")
        print("-" * 70)
        for r in results:
            if r.get("skipped"):
                print(f"{r['file']:<20} SKIP")
                continue
            sw = r.get("sw", {})
            if sw:
                print(f"{r['file']:<20} {'SAT' if sw['sat'] else 'UNS':>5} "
                      f"{sw['decisions']:>6} {sw['conflicts']:>6} "
                      f"{sw['propagations']:>6} {sw['implications']:>7} "
                      f"{sw['time_s']:>10.4f}")

    if mode in ("hw_sim", "both"):
        print()
        print(f"{'Instance':<20} {'SAT':>5} {'Dec':>6} {'Conf':>6} "
              f"{'BCPRnd':>6} {'HWImpl':>7} {'BCPCyc':>8} {'SimTime':>10}")
        print("-" * 78)
        for r in results:
            if r.get("skipped"):
                print(f"{r['file']:<20} SKIP")
                continue
            hw = r.get("hw", {})
            if hw:
                print(f"{r['file']:<20} {'SAT' if hw['sat'] else 'UNS':>5} "
                      f"{hw['decisions']:>6} {hw['conflicts']:>6} "
                      f"{hw['bcp_rounds']:>6} {hw['hw_implications']:>7} "
                      f"{hw['bcp_cycles']:>8} {hw['sim_time_s']:>10.3f}")
    print()


def main():
    parser = argparse.ArgumentParser(
        description="Run SATLIB benchmarks through CDCL solver")
    parser.add_argument("--family", default="uf20-91",
                        help="Benchmark family (default: uf20-91)")
    parser.add_argument("--mode", choices=["sw_only", "hw_sim", "both"],
                        default="sw_only",
                        help="Solve mode (default: sw_only)")
    parser.add_argument("--max-instances", type=int, default=0,
                        help="Max instances to run (0=all)")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    cnf_files = find_cnf_files(args.family)
    if args.max_instances > 0:
        cnf_files = cnf_files[:args.max_instances]

    print(f"Running {len(cnf_files)} instances from {args.family} "
          f"(mode={args.mode})")

    results = []
    for i, filepath in enumerate(cnf_files):
        fname = os.path.basename(filepath)
        print(f"  [{i+1}/{len(cnf_files)}] {fname} ...", end=" ", flush=True)
        try:
            result = run_benchmark(filepath, args.mode, args.verbose)
            results.append(result)
            # Quick status
            if result.get("skipped"):
                print("SKIPPED")
            else:
                key = "sw" if args.mode == "sw_only" else "hw"
                if args.mode == "both":
                    key = "sw"
                r = result.get(key, {})
                if r:
                    print(f"{'SAT' if r['sat'] else 'UNSAT'} "
                          f"({r['decisions']}d {r['conflicts']}c)")
                else:
                    print("done")
        except Exception as e:
            print(f"ERROR: {e}")
            results.append({"file": fname, "error": str(e)})

    print_summary_table(results, args.mode)

    # Save results
    os.makedirs(RESULTS_DIR, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    out_path = os.path.join(RESULTS_DIR,
                            f"{args.family}_{args.mode}_{ts}.json")
    with open(out_path, "w") as f:
        json.dump({
            "family": args.family,
            "mode": args.mode,
            "num_instances": len(results),
            "results": results,
        }, f, indent=2)
    print(f"Results saved to {out_path}")


if __name__ == "__main__":
    main()
