"""
DIMACS CNF parser with hardware constraint validation.

Parses standard DIMACS .cnf files and validates formulas against
the hardware accelerator's resource limits.
"""

from dataclasses import dataclass, field

# Hardware limits (from memory modules)
MAX_VARS = 512
MAX_CLAUSES = 8192
MAX_K = 5           # Max literals per clause
MAX_WATCH_LEN = 100 # Max watch list length per literal


@dataclass
class CNFFormula:
    num_vars: int
    num_clauses: int
    clauses: list = field(default_factory=list)  # list of list[int] (signed literals)
    filename: str = ""


def parse_dimacs(filepath: str) -> CNFFormula:
    """Parse a DIMACS CNF file into a CNFFormula."""
    num_vars = 0
    num_clauses = 0
    clauses = []
    current_clause = []

    with open(filepath) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('c') or line.startswith('%'):
                continue
            if line == '0':
                # Some files have bare '0' lines
                if current_clause:
                    clauses.append(current_clause)
                    current_clause = []
                continue
            if line.startswith('p '):
                parts = line.split()
                # p cnf <num_vars> <num_clauses>
                num_vars = int(parts[2])
                num_clauses = int(parts[3])
                continue

            # Clause line: space-separated integers terminated by 0
            tokens = line.split()
            for tok in tokens:
                lit = int(tok)
                if lit == 0:
                    if current_clause:
                        clauses.append(current_clause)
                        current_clause = []
                else:
                    current_clause.append(lit)

    # Handle unterminated final clause
    if current_clause:
        clauses.append(current_clause)

    return CNFFormula(
        num_vars=num_vars,
        num_clauses=num_clauses,
        clauses=clauses,
        filename=filepath,
    )


def validate_hw_constraints(formula: CNFFormula) -> list:
    """Check formula against hardware limits. Returns list of error strings."""
    errors = []

    if formula.num_vars > MAX_VARS:
        errors.append(
            f"Too many variables: {formula.num_vars} > {MAX_VARS}")

    if len(formula.clauses) > MAX_CLAUSES:
        errors.append(
            f"Too many clauses: {len(formula.clauses)} > {MAX_CLAUSES}")

    for i, clause in enumerate(formula.clauses):
        if len(clause) > MAX_K:
            errors.append(
                f"Clause {i} has {len(clause)} literals > {MAX_K}")

    # Check watch list lengths: count occurrences per literal code
    lit_counts = {}
    for clause in formula.clauses:
        if len(clause) >= 2:
            for lit in clause[:2]:  # only first two lits are watched
                code = lit_to_code(lit)
                lit_counts[code] = lit_counts.get(code, 0) + 1

    for code, count in lit_counts.items():
        if count > MAX_WATCH_LEN:
            errors.append(
                f"Watch list for lit_code {code} has length {count} > {MAX_WATCH_LEN}")

    return errors


def lit_to_code(lit: int) -> int:
    """Convert signed literal to internal code. Matches CDCL.c encoding.
    Positive x -> 2*x, negative x -> 2*(-x)+1."""
    if lit > 0:
        return 2 * lit
    else:
        return 2 * (-lit) + 1


def lit_neg(code: int) -> int:
    """Negate an internal literal code."""
    return code ^ 1


def lit_var(code: int) -> int:
    """Extract variable index from internal literal code."""
    return code >> 1
