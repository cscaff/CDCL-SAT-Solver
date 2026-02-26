# BCP Hardware Accelerator - Complete System Architecture Specification

> **Version:** 1.0  
> **Date:** February 2026  
> **Target Platform:** Lattice ECP5-85F FPGA  
> **Author:** Christian Scaff, Barnard PL  
> **Purpose:** Modular, open-source SAT acceleration framework for CDCL solvers

---

## Table of Contents

1. [System Overview](#system-overview)
2. [Global Parameters](#global-parameters)
3. [Memory Subsystem](#memory-subsystem)
4. [Top-Level Module](#top-level-module)
5. [Sub-Module Specifications](#sub-module-specifications)
6. [Data Structures](#data-structures)
7. [Pipeline Flow](#pipeline-flow)
8. [Resource Estimates](#resource-estimates)
9. [References](#references)

---

## System Overview

### Purpose

The BCP (Boolean Constraint Propagation) accelerator replaces the inner loop of the CDCL SAT solver's `propagate()` function. It processes all clauses watching a given literal that became false, identifies unit clauses (implications) and conflicts, and streams results back to the software CDCL controller.

### Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────────┐
│                    BCP Accelerator (Top Module)                      │
│                                                                      │
│  External Interface (to CDCL Software):                              │
│  ┌────────────────────────────────────────────────────────────┐    │
│  │  IN:  false_lit, start                                     │    │
│  │  OUT: done, conflict, conflict_clause_id                   │    │
│  │       impl_valid, impl_var, impl_value, impl_reason        │    │
│  └────────────────────────────────────────────────────────────┘    │
│                                                                      │
│  Internal Pipeline:                                                  │
│                                                                      │
│  ┌──────────────┐   ┌──────────────┐   ┌─────────────────────┐    │
│  │ Watch List   │──►│   Clause     │──►│   Clause Evaluator  │    │
│  │ Manager      │   │ Prefetcher   │   │   (Single PE)       │    │
│  └──────────────┘   └──────────────┘   └──────────┬──────────┘    │
│                                                    │                │
│         ┌──────────────────────────────────────────┘                │
│         ▼                                                            │
│  ┌──────────────┐                                                   │
│  │ Implication  │                                                   │
│  │ FIFO         │                                                   │
│  └──────────────┘                                                   │
│                                                                      │
│  Memory Subsystem (BRAM):                                            │
│  ┌────────────────────────────────────────────────────────────┐    │
│  │ • Clause Database (84 KB)                                  │    │
│  │ • Watch Lists (164 KB, P=4 banks)                          │    │
│  │ • Variable Assignments (0.125 KB)                          │    │
│  └────────────────────────────────────────────────────────────┘    │
└──────────────────────────────────────────────────────────────────────┘
```

### Design Philosophy

- **Modular:** Each sub-module is independently testable
- **Simple First:** No optimizations in initial version (ucnt/signature, parallel PEs deferred)
- **Amaranth HDL:** Python-based, open-source HDL for accessibility
- **Yosys Synthesis:** Open toolchain targeting Lattice ECP5
- **Baseline Implementation:** 2-WL scheme with satisfaction bit optimization only

---

## Global Parameters

```python
class BCPConfig:
    """Global configuration for BCP accelerator"""
    
    # Problem size
    max_vars        = 512       # Maximum variables
    max_clauses     = 8192      # Maximum clauses (original + learned)
    max_k           = 5         # Maximum literals per clause
    max_watch_len   = 100       # Maximum clauses per watch list
    
    # Parallelism (Phase 1: single PE)
    num_pes         = 1         # Number of parallel processing elements
    num_banks       = 4         # Watch list memory banks (for future scaling)
    
    # Derived parameters
    num_literals    = 2 * max_vars              # 1024 (a, ¬a, b, ¬b, ...)
    clause_id_width = 13                        # log2(max_clauses)
    var_id_width    = 9                         # log2(max_vars)
    literal_width   = 10                        # log2(num_literals)
    lit_encoding    = 16                        # Literal encoding width (rounded)
    
    # FPGA target
    target_platform = "Lattice ECP5-85F"
    available_bram  = 468                       # KB
```

---

## Memory Subsystem

### Overview

The memory subsystem stores three primary data structures required for BCP:

1. **Clause Database** — CNF formula clauses with metadata
2. **Watch Lists** — Which clauses watch each literal
3. **Variable Assignments** — Current variable assignment state

All stored in on-chip BRAM for maximum throughput.

---

### Memory Module 1: Clause Database

#### Purpose
Stores all clauses (original formula + learned clauses) with satisfaction bit for early termination.

#### Structure

```
Per clause entry:
┌─────────┬──────┬────────┬────────┬────────┬────────┬────────┐
│ sat_bit │ size │ lit[0] │ lit[1] │ lit[2] │ lit[3] │ lit[4] │
│  1 bit  │ 3bit │ 16 bit │ 16 bit │ 16 bit │ 16 bit │ 16 bit │
└─────────┴──────┴────────┴────────┴────────┴────────┴────────┘
Total: 84 bits per clause
```

**Fields:**
- `sat_bit` (1 bit): Is clause currently satisfied? (Optimization 5)
- `size` (3 bits): Number of valid literals (0-5)
- `lits[0..4]` (5 × 16 bits): Literal encodings (even=positive, odd=negative)

**Literal Encoding:**
```
Variable v → Positive literal = 2v (even)
             Negative literal = 2v + 1 (odd)

Example:
  var a (id=1) → positive 'a'  = 2
                 negative '¬a' = 3
```

#### Memory Organization

```
Address Space: 0 to 8191 (max_clauses - 1)
Entry Width:   84 bits
Total Size:    84 bits × 8192 = 688,128 bits = 84 KB

Storage: Single BRAM block, dual-port
  Port A: Read-only (BCP access)
  Port B: Write (clause learning, initialization)
```

#### Example Content

```
Address | sat_bit | size | lit[0] | lit[1] | lit[2] | lit[3] | lit[4] | Clause
--------|---------|------|--------|--------|--------|--------|--------|-------------
   0    |    0    |  3   |   2    |   4    |   6    |   0    |   0    | (a ∨ b ∨ c)
   1    |    1    |  2   |   3    |   8    |   0    |   0    |   0    | (¬a ∨ d) ✓
   2    |    0    |  3   |   5    |   9    |  10    |   0    |   0    | (¬b ∨ ¬d ∨ e)
   3    |    0    |  2   |   6    |  11    |   0    |   0    |   0    | (c ∨ ¬e)
  ...   |   ...   | ...  |  ...   |  ...   |  ...   |  ...   |  ...   |
```

#### Interface

```python
class ClauseMemory:
    # Read port (to Clause Prefetcher)
    rd_addr  : Signal(range(max_clauses))      # Clause ID to read
    rd_en    : Signal()                        # Read enable
    rd_data  : ClauseData                      # Clause contents
    rd_valid : Signal()                        # Data valid (after 2-cycle latency)
    
    # Write port (from software/clause learning)
    wr_addr  : Signal(range(max_clauses))      # Clause ID to write
    wr_data  : ClauseData                      # Clause contents
    wr_en    : Signal()                        # Write enable
```

**Timing:** 2-cycle read latency (standard BRAM)

---

### Memory Module 2: Watch Lists

#### Purpose
Maps each literal to the list of clauses watching it. Organized into P banks for conflict-free parallel access (FYalSAT Optimization 3).

#### Structure

```
Per watch list entry:
┌────────┬─────────────┬─────────────┬─────────────┬     ┬─────────────┐
│ length │ clause_id[0]│ clause_id[1]│ clause_id[2]│ ... │clause_id[99]│
│ 7 bits │   13 bits   │   13 bits   │   13 bits   │     │   13 bits   │
└────────┴─────────────┴─────────────┴─────────────┴─────┴─────────────┘
Total: 7 + (100 × 13) = 1307 bits per watch list
```

**Fields:**
- `length` (7 bits): Number of valid clause IDs (0-100)
- `clause_id[0..99]` (100 × 13 bits): Clause indices watching this literal

#### Memory Organization

```
Number of watch lists: 1024 (one per literal: 2 × max_vars)
Entry width:          1307 bits
Total size:           1307 × 1024 = 1,338,368 bits = 164 KB

Storage: P=4 BRAM banks (modulo-P partitioned for future parallel access)
  Bank k stores clause IDs where clause_id % P == k
```

#### Conflict-Free Partitioning (FYalSAT §III-A)

**Partitioning Rule:**
```
Clause C with ID=i is stored in Bank (i % P)

Example (P=4):
  Clause 0  → Bank 0
  Clause 1  → Bank 1
  Clause 2  → Bank 2
  Clause 3  → Bank 3
  Clause 4  → Bank 0
  Clause 5  → Bank 1
  ...
```

**Result:** When reading watch list entries in parallel (future enhancement), each PE accesses a different bank → zero conflicts.

#### Example Content

```
Literal | Encoding | Length | clause_id[0] | clause_id[1] | clause_id[2] | ...
--------|----------|--------|--------------|--------------|--------------|-----
   a    |    2     |   3    |      0       |      2       |      4       | ...
  ¬a    |    3     |   1    |      1       |      0       |      0       | ...
   b    |    4     |   1    |      0       |      0       |      0       | ...
  ¬b    |    5     |   1    |      3       |      0       |      0       | ...
  ...   |   ...    |  ...   |     ...      |     ...      |     ...      | ...

Note: Unused slots filled with 0
```

#### Interface

```python
class WatchListMemory:
    # Read port (to Watch List Manager)
    rd_lit   : Signal(range(num_literals))     # Which literal's watch list
    rd_idx   : Signal(range(max_watch_len))    # Which entry in list
    rd_data  : Signal(clause_id_width)         # Clause ID
    rd_len   : Signal(range(max_watch_len))    # Total length of this watch list
    rd_en    : Signal()                        # Read enable
    
    # Write port (for updates during BCP, initialization)
    wr_lit   : Signal(range(num_literals))
    wr_idx   : Signal(range(max_watch_len))
    wr_data  : Signal(clause_id_width)
    wr_len   : Signal(range(max_watch_len))    # Update length
    wr_en    : Signal()
```

**Access Pattern:** Sequential read through watch list (idx = 0, 1, 2, ..., length-1)

---

### Memory Module 3: Variable Assignments

#### Purpose
Stores current assignment state for all variables. Read by Clause Evaluator to determine literal values.

#### Structure

```
Per variable:
┌────────────────┐
│ assignment[v]  │
│    2 bits      │
└────────────────┘

Values:
  0 = UNASSIGNED
  1 = FALSE
  2 = TRUE
```

#### Memory Organization

```
Number of variables: 512
Entry width:        2 bits
Total size:         2 × 512 = 1024 bits = 128 bytes

Storage: Small BRAM or distributed RAM (LUT-based)
```

#### Example Content

```
Variable | var_id | assignment | State
---------|--------|------------|-------------
    a    |   0    |     2      | TRUE
    b    |   1    |     1      | FALSE
    c    |   2    |     0      | UNASSIGNED
    d    |   3    |     2      | TRUE
   ...   |  ...   |    ...     | ...
```

#### Interface

```python
class AssignmentMemory:
    # Read port (to Clause Evaluator)
    rd_addr : Signal(range(max_vars))          # Variable ID
    rd_data : Signal(2)                        # Assignment value
    
    # Write port (from software when variables assigned)
    wr_addr : Signal(range(max_vars))
    wr_data : Signal(2)
    wr_en   : Signal()
```

**Access Pattern:** Random read (different variables per clause evaluation)

---

### Memory Summary

```
┌──────────────────────┬──────────┬─────────────┬──────────────┐
│ Memory Module        │ Size     │ Access      │ Technology   │
├──────────────────────┼──────────┼─────────────┼──────────────┤
│ Clause Database      │ 84 KB    │ Sequential  │ BRAM (dual)  │
│ Watch Lists          │ 164 KB   │ Sequential  │ BRAM (4-bank)│
│ Variable Assignments │ 0.125 KB │ Random      │ Dist. RAM    │
├──────────────────────┼──────────┼─────────────┼──────────────┤
│ TOTAL                │ ~248 KB  │             │              │
└──────────────────────┴──────────┴─────────────┴──────────────┘

Available BRAM (ECP5-85F): 468 KB
Utilization: 53% ✓
```

---

## Top-Level Module

### Module: BCPAccelerator

#### Purpose
Top-level module integrating all sub-modules. Provides clean interface to software CDCL controller.

#### Interface

```python
class BCPAccelerator(Elaboratable):
    """
    BCP Hardware Accelerator - Top Level
    
    Replaces the inner loop of CDCL propagate() function.
    Processes all clauses watching a literal that became false.
    """
    
    def __init__(self, config: BCPConfig):
        # ===== CONTROL INTERFACE =====
        # From software
        self.start        = Signal()                           # Start BCP
        self.false_lit    = Signal(range(config.num_literals)) # Literal that became false
        
        # To software
        self.done         = Signal()                           # BCP complete
        self.busy         = Signal()                           # Currently processing
        
        # ===== CONFLICT INTERFACE =====
        # To software
        self.conflict           = Signal()                     # Conflict detected
        self.conflict_clause_id = Signal(range(config.max_clauses))
        
        # ===== IMPLICATION INTERFACE =====
        # To software (stream of implications)
        self.impl_valid   = Signal()                           # Implication available
        self.impl_var     = Signal(range(config.max_vars))     # Variable to assign
        self.impl_value   = Signal()                           # 0=FALSE, 1=TRUE
        self.impl_reason  = Signal(range(config.max_clauses))  # Reason clause
        self.impl_ready   = Signal()                           # SW ready for next (input)
        
        # ===== MEMORY INTERFACES =====
        # (Internal - connected to memory modules)
```

#### Sub-Module Instantiation

```python
def elaborate(self, platform):
    m = Module()
    
    # Instantiate memory subsystem
    m.submodules.clause_mem = clause_mem = ClauseMemory(self.config)
    m.submodules.watch_mem  = watch_mem  = WatchListMemory(self.config)
    m.submodules.assign_mem = assign_mem = AssignmentMemory(self.config)
    
    # Instantiate pipeline modules
    m.submodules.watch_mgr  = watch_mgr  = WatchListManager(self.config)
    m.submodules.prefetcher = prefetcher = ClausePrefetcher(self.config)
    m.submodules.evaluator  = evaluator  = ClauseEvaluator(self.config)
    m.submodules.impl_fifo  = impl_fifo  = ImplicationFIFO(self.config)
    
    # Wire connections (see Pipeline Flow section)
    # ...
    
    return m
```

#### Timing Diagram

```
Software-Hardware Transaction:

Cycle:   1    2    3    4    5    6    7    8    9    10   11
         │    │    │    │    │    │    │    │    │    │    │
SW:   setup  start═════════════════╗                    ╗
      false_lit─────────────────╗  │                    │
                                ↓  ↓                    │
HW:                           busy═══════════════════done
                                   │                    │
      [idle]            [process watch list]      [idle]│
                                   │                    │
                              impl_valid───╗            │
                              [impl data]  ╝            │
                                   │                    ▼
SW:                           impl_ready═══╗      Check done
                              enqueue()    ╝
```

---

## Sub-Module Specifications

### Module 1: Watch List Manager

#### Purpose
Fetches and streams clause IDs from the watch list for a given `false_lit`.

#### Interface

```python
class WatchListManager:
    # Inputs
    start       : Signal()                              # Begin processing
    false_lit   : Signal(range(num_literals))           # Literal to process
    
    # Outputs
    clause_id       : Signal(range(max_clauses))        # Clause ID output
    clause_id_valid : Signal()                          # Clause ID valid
    done            : Signal()                          # All entries dispatched
    
    # Memory interface (to Watch List Memory)
    wl_rd_lit   : Signal(range(num_literals))
    wl_rd_idx   : Signal(range(max_watch_len))
    wl_rd_data  : Signal(clause_id_width)
    wl_rd_len   : Signal(range(max_watch_len))
    wl_rd_en    : Signal()
```

#### Internal State

```python
# FSM states
state       : WLMState = {IDLE, FETCH_LEN, STREAM, DONE}

# Iteration state
watch_ptr   : Signal(range(max_watch_len))          # Current position
watch_len   : Signal(range(max_watch_len))          # Total length
```

#### Functional Behavior

**State Machine:**

```
IDLE:
  Wait for start=1
  On start:
    - Capture false_lit
    - Request watch_length[false_lit]
    - → FETCH_LEN

FETCH_LEN:
  Wait for length data
  On valid:
    - Store watch_len
    - Initialize watch_ptr = 0
    - → STREAM

STREAM:
  For ptr = 0 to watch_len-1:
    - Request watch_list[false_lit][ptr]
    - Output clause_id with valid=1
    - Increment ptr
  On completion:
    - → DONE

DONE:
  Assert done=1
  → IDLE
```

#### Timing Example

```
Cycle:    1     2     3     4     5     6     7
          │     │     │     │     │     │     │
start:   ‾╲_____│     │     │     │     │     │
false_lit:╱═══L═╲_____│     │     │     │     │
          │     │     │     │     │     │     │
State:   IDLE  FETCH STREAM─────────────────DONE
          │     │     │     │     │     │     │
cid:     XXXXX│XXXXX╱═══5═╲══17═╲══42═╲XXXXX│
cid_valid:____│_____/‾‾‾‾‾╲‾‾‾‾╲‾‾‾‾╲_____│
done:    ______│_____│_____│_____│_____/‾‾‾‾│
```

**For watch_list[L] = [5, 17, 42] (length=3)**

---

### Module 2: Clause Prefetcher

#### Purpose
Pipelines clause memory reads to hide 2-cycle BRAM latency. Fetches clause `i+1` while `i` is being evaluated.

**Source:** FYalSAT §III-C (prefetching optimization)

#### Interface

```python
class ClausePrefetcher:
    # Inputs (from Watch List Manager)
    clause_id_in    : Signal(range(max_clauses))
    clause_id_valid : Signal()
    
    # Outputs (to Clause Evaluator)
    clause_meta_out : ClauseData
    clause_id_out   : Signal(range(max_clauses))
    meta_valid      : Signal()
    
    # Memory interface (to Clause Memory)
    clause_rd_addr  : Signal(range(max_clauses))
    clause_rd_en    : Signal()
    clause_rd_data  : ClauseData
    clause_rd_valid : Signal()
```

#### Internal State

```python
# 2-stage pipeline
stage1_clause_id   : Signal(range(max_clauses))
stage1_valid       : Signal()

stage2_clause_data : ClauseData
stage2_clause_id   : Signal(range(max_clauses))
stage2_valid       : Signal()
```

#### Functional Behavior

**Pipeline Structure:**

```
Stage 1 (FETCH):
  When clause_id_valid:
    - Capture clause_id_in
    - Issue memory read (clause_rd_addr, clause_rd_en)
    - Store in stage1 register

Stage 2 (FORWARD):
  When clause_rd_valid (after 2-cycle BRAM latency):
    - Capture clause_rd_data
    - Forward clause_id from stage1
    - Store in stage2 register
    - Output to downstream
```

#### Timing Diagram

```
Cycle:    1     2     3     4     5     6
          │     │     │     │     │     │
Input:    │     │     │     │     │     │
cid_in:   XXXXX╱══5══╲══17═╲XXXXX│     │
valid_in: _____/‾‾‾‾‾╲‾‾‾‾‾╲_____│     │
          │     │     │     │     │     │
Stage1:   │     │     │     │     │     │
s1_cid:   XXXXX│╱══5══╲══17═╲XXXXX│     │
mem_rd:   ______/‾‾‾‾‾╲‾‾‾‾‾╲_____│     │
          │     │  latency  │  latency  │
Stage2:   │     │     │     │     │     │
s2_data:  XXXXX│XXXXX│╱═D5══╲═D17═╲XXXXX
s2_cid:   XXXXX│XXXXX│╱══5══╲══17═╲XXXXX
          │     │     │     │     │     │
Output:   │     │     │     │     │     │
meta_out: XXXXX│XXXXX│╱═D5══╲═D17═╲XXXXX
valid_out:______│_____/‾‾‾‾‾╲‾‾‾‾‾╲_____
```

**Key:** While clause 5 is being output (cycle 4), clause 17 is already fetched. 2-cycle latency hidden.

---

### Module 3: Clause Evaluator

#### Purpose
Evaluates a clause to determine if it is:
1. **Satisfied** (sat_bit=1, early exit)
2. **Unit** (one unassigned literal, all others false)
3. **Conflict** (all literals false)
4. **Unresolved** (multiple unassigned)

#### Interface

```python
class ClauseEvaluator:
    # Inputs (from Clause Prefetcher)
    clause_meta     : ClauseData
    clause_id_in    : Signal(range(max_clauses))
    meta_valid      : Signal()
    
    # Inputs (from Assignment Memory)
    assign_rd_addr  : Signal(range(max_vars))
    assign_rd_data  : Signal(2)
    
    # Outputs
    result          : EvalResult
    result_valid    : Signal()
```

#### Data Structures

```python
class EvalResult:
    status       : Signal(2)  # SATISFIED=0, UNIT=1, CONFLICT=2, UNRESOLVED=3
    implied_var  : Signal(range(max_vars))   # Valid when status=UNIT
    implied_val  : Signal()                  # Polarity of implication
    clause_id    : Signal(range(max_clauses))
```

#### Functional Behavior

**Evaluation Algorithm:**

```
Step 1: Check sat_bit (Optimization 5)
  if clause_meta.sat_bit == 1:
    status = SATISFIED
    return  (no further evaluation needed)

Step 2: Evaluate each literal
  For i = 0 to clause_meta.size-1:
    lit = clause_meta.lits[i]
    var = lit >> 1
    sign = lit & 1
    
    Read assignment[var]
    
    Determine literal value:
      if assignment == UNASSIGNED:
        unassigned_count++
        last_unassigned = lit
      elif assignment == FALSE:
        if sign == 0: false_count++  (positive lit, var=F → lit=F)
        else: satisfied = True       (negative lit, var=F → lit=T)
      elif assignment == TRUE:
        if sign == 0: satisfied = True (positive lit, var=T → lit=T)
        else: false_count++            (negative lit, var=T → lit=F)

Step 3: Determine status
  if satisfied:
    status = SATISFIED
  elif unassigned_count == 0:
    status = CONFLICT
  elif unassigned_count == 1:
    status = UNIT
    implied_var = last_unassigned >> 1
    implied_val = last_unassigned & 1
  else:
    status = UNRESOLVED (need new watch - not implemented in Phase 1)
```

#### Timing

```
Latency: K cycles (sequential literal evaluation, K=max_k=5)
Can be optimized later with parallel literal evaluation (Optimization 6)
```

---

### Module 4: Implication FIFO

#### Purpose
Buffers unit clause implications before sending to software. Handles cases where multiple implications arrive faster than software can process.

**Source:** SAT-Accel §IV-B (pipelined BCP with implication queue)

#### Interface

```python
class ImplicationFIFO:
    # Inputs (from Clause Evaluator)
    push_valid  : Signal()
    push_var    : Signal(range(max_vars))
    push_value  : Signal()
    push_reason : Signal(range(max_clauses))
    
    # Outputs (to software)
    pop_valid   : Signal()
    pop_var     : Signal(range(max_vars))
    pop_value   : Signal()
    pop_reason  : Signal(range(max_clauses))
    pop_ready   : Signal()  (input from software)
    
    # Status
    fifo_empty  : Signal()
    fifo_full   : Signal()
```

#### Parameters

```python
fifo_depth  = 16  # Buffer up to 16 implications
entry_width = var_id_width + 1 + clause_id_width  # var + value + reason
```

#### Functional Behavior

**Standard synchronous FIFO:**
- Push when evaluator finds unit clause and `~fifo_full`
- Pop when software asserts `pop_ready` and `~fifo_empty`
- FIFO full: backpressure to evaluator (stalls pipeline)

---

## Data Structures

### ClauseData

```python
class ClauseData:
    """
    Complete clause metadata.
    Maps directly to clause database memory layout.
    """
    sat_bit : Signal()                          # 1 bit
    size    : Signal(3)                         # 3 bits (0-5)
    lits    : Array(Signal(16) for _ in range(5))  # 5 × 16 bits
    
    # Total: 84 bits
```

### EvalResult

```python
class EvalResult:
    """
    Result of clause evaluation.
    Output from Clause Evaluator.
    """
    status      : Signal(2)  # 0=SAT, 1=UNIT, 2=CONFLICT, 3=UNRESOLVED
    implied_var : Signal(range(max_vars))
    implied_val : Signal()
    clause_id   : Signal(range(max_clauses))
```

### Implication

```python
class Implication:
    """
    A single unit clause implication.
    """
    var    : Signal(range(max_vars))
    value  : Signal()                    # 0=FALSE, 1=TRUE
    reason : Signal(range(max_clauses))  # Which clause implied it
```

---

## Pipeline Flow

### Complete Dataflow Diagram

```
┌──────────────────────────────────────────────────────────────────┐
│  Software CDCL Controller                                         │
│  ┌──────────────┐                                                │
│  │ false_lit ═══╪══════════════════════════════════════╗         │
│  │ start     ═══╪══════════════════════════════════╗   │         │
│  └──────────────┘                                  ↓   ↓         │
└────────────────────────────────────────────────────║───║─────────┘
                                                     ║   ║
┌────────────────────────────────────────────────────║───║─────────┐
│  BCP Accelerator                                   ↓   ↓         │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │ Watch List Manager                                       │   │
│  │   Input: false_lit, start                                │   │
│  │   Output: clause_id stream                               │   │
│  └──────────────────┬───────────────────────────────────────┘   │
│                     │ clause_id, clause_id_valid                 │
│                     ▼                                             │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │ Clause Prefetcher                                        │   │
│  │   Input: clause_id                                       │   │
│  │   Fetch: clause_meta from Clause Memory (2-cycle latency)│   │
│  │   Output: clause_meta, clause_id                         │   │
│  └──────────────────┬───────────────────────────────────────┘   │
│                     │ clause_meta, meta_valid                    │
│                     ▼                                             │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │ Clause Evaluator                                         │   │
│  │   Input: clause_meta                                     │   │
│  │   Read: assignment[var] for each literal                 │   │
│  │   Evaluate: SAT? UNIT? CONFLICT?                         │   │
│  │   Output: eval_result                                    │   │
│  └──────────────────┬───────────────────────────────────────┘   │
│                     │ eval_result, result_valid                  │
│                     ▼                                             │
│       ┌─────────────┴─────────────┐                              │
│       │                           │                              │
│       ▼ (if UNIT)                 ▼ (if CONFLICT)                │
│  ┌──────────────────┐      ┌─────────────────────┐              │
│  │ Implication FIFO │      │ conflict = 1        │              │
│  │   Buffer impls   │      │ conflict_clause_id  │              │
│  └────────┬─────────┘      └──────────┬──────────┘              │
│           │                           │                          │
│           ↓                           ↓                          │
└───────────║───────────────────────────║──────────────────────────┘
            ║                           ║
┌───────────║───────────────────────────║──────────────────────────┐
│  Software │                           │                          │
│  ┌────────▼──────────┐      ┌─────────▼────────────┐            │
│  │ impl_valid        │      │ if (conflict)        │            │
│  │ impl_var, value   │      │   return clause_id   │            │
│  │ impl_reason       │      │ (start conflict      │            │
│  │ → enqueue()       │      │  analysis)           │            │
│  └───────────────────┘      └──────────────────────┘            │
└──────────────────────────────────────────────────────────────────┘
```

### Cycle-by-Cycle Example

**Scenario:** Process `false_lit = ¬a`, watch list contains [C5, C17, C42]

```
Cycle:   1    2    3    4    5    6    7    8    9    10   11
         │    │    │    │    │    │    │    │    │    │    │
SW:   start═╗│    │    │    │    │    │    │    │    │    │
         ║  ││    │    │    │    │    │    │    │    │    │
WLM:     ║ FETCH STREAM──────────────────────────────────DONE
         ║  ││    │    │    │    │    │    │    │    │    │
         ║  ││  cid=5 cid=17 cid=42                         │
         ║  ││    ↓    ↓    ↓    │    │    │    │    │    │
Prefetch:║  ││  [fetch C5] [fetch C17] [fetch C42]          │
         ║  ││    │    │    │    │    │    │    │    │    │
         ║  ││    │ D5 avail│ D17 avail│ D42 avail│         │
         ║  ││    │    ↓    │    ↓    │    ↓    │    │    │
Eval:    ║  ││    │ [eval C5] [eval C17] [eval C42]         │
         ║  ││    │    │    │    │    │    │    │    │    │
Result:  ║  ││    │    │ UNIT│    │CONFLICT│    │    │    │
         ║  ││    │    │    ↓    │    ↓    │    │    │    │
SW:      ║  ││    │    │ enqueue │ return conflict_id       │
```

**C5 → UNIT:** Implication enqueued  
**C17 → CONFLICT:** BCP returns conflict_clause_id=17 immediately

---

## Resource Estimates

### Memory Utilization

```
┌─────────────────────┬──────────┬─────────────┐
│ Resource            │ Used     │ Available   │
├─────────────────────┼──────────┼─────────────┤
│ Clause Database     │ 84 KB    │             │
│ Watch Lists         │ 164 KB   │             │
│ Var Assignments     │ 0.125 KB │             │
├─────────────────────┼──────────┼─────────────┤
│ Total BRAM          │ 248 KB   │ 468 KB      │
│ Utilization         │ 53%      │             │
└─────────────────────┴──────────┴─────────────┘
```

### Logic Utilization (Estimated)

```
┌─────────────────────┬──────────┬─────────────┐
│ Module              │ LUTs     │ FFs         │
├─────────────────────┼──────────┼─────────────┤
│ Watch List Manager  │ ~100     │ ~50         │
│ Clause Prefetcher   │ ~50      │ ~120        │
│ Clause Evaluator    │ ~300     │ ~100        │
│ Implication FIFO    │ ~50      │ ~250        │
│ Top-level glue      │ ~100     │ ~50         │
├─────────────────────┼──────────┼─────────────┤
│ Total               │ ~600     │ ~570        │
│ Available (ECP5-85F)│ 84,000   │ 84,000      │
│ Utilization         │ <1%      │ <1%         │
└─────────────────────┴──────────┴─────────────┘
```

### Timing

```
Target Frequency:   100 MHz
Critical Path:      Clause evaluation loop (~10 ns estimated)
Slack:              Comfortable (lightweight logic)
```

---

## References

### Source Papers

1. **SAT-Accel (Lo et al., FPGA 2025)**  
   *SAT-Accel: A Modern SAT Solver on a FPGA*  
   DOI: 10.1145/3706628.3708869

2. **FYalSAT (Choi & Kim, IEEE Access 2024)**  
   *FYalSAT: High-Throughput Stochastic Local Search K-SAT Solver on FPGA*  
   DOI: 10.1109/ACCESS.2024.3397330

3. **SMT Solver (Chen et al., IEEE TCAD 2023)**  
   *SMT Solver With Hardware Acceleration*  
   DOI: 10.1109/TCAD.2022.3209550

### Optimizations Applied

**Phase 1 Implementation:**
- ✅ **Optimization 5:** Satisfaction bit for early termination (FYalSAT §IV-B)
- ✅ **Optimization 2:** Clause prefetching to hide latency (FYalSAT §III-C)
- ✅ **Optimization 3:** Conflict-free memory partitioning (FYalSAT §III-A)
- ✅ **Optimization 8:** Hardware FIFO for implications (SAT-Accel §IV-B)

**Future Enhancements (Phase 2):**
- ⏳ **Optimization 7:** State-based representation (ucnt + XOR signature)
- ⏳ **Optimization 1:** Parallel processing elements (P PEs)
- ⏳ **Optimization 6:** Parallel literal scanning
- ⏳ **Optimization 10:** Parallel conflict reduction

---

## Revision History

| Version | Date | Changes |
|---------|------|---------|
| 1.0 | Feb 2026 | Initial specification - baseline 2-WL implementation with sat_bit optimization |

---

**End of Specification**
