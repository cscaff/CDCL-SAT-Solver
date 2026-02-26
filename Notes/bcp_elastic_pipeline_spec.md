# BCP Accelerator — Elastic Pipeline Modifications

> **Purpose:** System-level specification for adding valid/ready handshaking to the BCP accelerator pipeline  
> **Base Document:** BCP Hardware Accelerator Complete System Architecture Specification v1.0  
> **Target:** Claude Code implementation guide

---

## Overview

The current pipeline uses implicit timing assumptions between modules. This document specifies the changes required to make the pipeline **elastic** — i.e., each stage can independently stall and resume without data loss, using explicit **valid/ready handshakes** at every stage boundary.

---

## The Handshake Contract

The core principle is a two-signal handshake at every inter-module connection:

```
Producer ──valid──► Consumer
Producer ◄──ready── Consumer

Transaction fires only when: valid & ready (both high simultaneously)
```

**Rules every stage must follow:**

1. **No data loss.** A stage must hold its output register stable while `valid` is high and `ready` is low. It must never overwrite output until the transaction fires.
2. **No combinational loops.** A stage's `ready` output must depend only on its internal state — never combinationally on the incoming `valid` signal. This prevents timing hazards.
3. **Skid buffer at the BRAM boundary.** BRAM reads cannot be cancelled once issued (2-cycle fixed latency). The Clause Prefetcher must include a 2-entry output buffer so in-flight reads have somewhere to land even if the downstream stage stalls mid-flight.
4. **Conflict freezes, not flushes.** When a conflict is detected, freeze all stages in place. In Phase 1, BCP terminates after a conflict, so a full flush is unnecessary.

---

## Module-by-Module Changes

### 1. Watch List Manager

**Change:** Add `clause_id_ready` as an input from the Clause Prefetcher. The read pointer must only advance when the downstream stage accepts the current output.

```python
# NEW input:
clause_id_ready : Signal()   # IN — from Clause Prefetcher

# Internal change in STREAM state:
# Only increment watch_ptr when downstream accepts
if clause_id_valid & clause_id_ready:
    watch_ptr += 1
# If ~clause_id_ready: hold watch_ptr, re-present same clause_id, do not issue next BRAM read
```

**Interface diff:**
```
WatchListManager:
  [existing] clause_id       : Signal(range(max_clauses))   # OUT
  [existing] clause_id_valid : Signal()                     # OUT
+ [new]      clause_id_ready : Signal()                     # IN  ← from Prefetcher
  [existing] done            : Signal()                     # OUT
```

---

### 2. Clause Prefetcher

This is the most critical stage due to the fixed 2-cycle BRAM latency. The prefetcher must become a proper **skid buffer / elastic FIFO** (depth ≥ 2) to absorb the latency mismatch.

**Changes:**

- Add `meta_ready` as an input from the Clause Evaluator.
- Add `clause_id_ready` as an output back to WLM (backpressure propagation).
- Add a 2-entry internal output buffer to hold data returned from BRAM when the evaluator is stalled.
- When the internal buffer is full: de-assert `clause_id_ready` to stop WLM, and do not issue new BRAM reads.

```python
# NEW input:
meta_ready      : Signal()   # IN  — from Clause Evaluator

# NEW output (backpressure upstream):
clause_id_ready : Signal()   # OUT — to Watch List Manager
                             # = ~output_buffer_full

# Internal 2-entry output buffer:
# - BRAM reads in-flight land here when evaluator stalls
# - clause_id_ready goes low when buffer is full
# - Prevents issuing new reads when there is nowhere to put results
```

**Interface diff:**
```
ClausePrefetcher:
  [existing] clause_id_in    : Signal(range(max_clauses))   # IN
  [existing] clause_id_valid : Signal()                     # IN
+ [new]      clause_id_ready : Signal()                     # OUT ← backpressure to WLM
  [existing] clause_meta_out : ClauseData                   # OUT
  [existing] clause_id_out   : Signal(range(max_clauses))   # OUT
  [existing] meta_valid      : Signal()                     # OUT
+ [new]      meta_ready      : Signal()                     # IN  ← from Evaluator
```

---

### 3. Clause Evaluator

The evaluator takes K cycles to scan literals sequentially, making it the natural throughput bottleneck. It must not accept new input while processing a clause.

**Changes:**

- `meta_ready` is driven by internal FSM state: high only in `IDLE`, low while evaluating.
- After evaluation completes, hold the result stable in a `DONE` state until the downstream FIFO or conflict path accepts it.
- Add `result_ready` as an input from the downstream mux (FIFO / conflict logic).

```python
# NEW output (backpressure upstream):
meta_ready   : Signal()   # OUT — high only when state == IDLE

# NEW input (backpressure downstream):
result_ready : Signal()   # IN  — from FIFO / conflict output mux

# Internal FSM change:
# In DONE state: hold result_valid high, hold result data stable
# Only transition back to IDLE when result_valid & result_ready
if result_valid & result_ready:
    transition_to(IDLE)
# If ~result_ready: remain in DONE, do not accept new clause
```

**Interface diff:**
```
ClauseEvaluator:
  [existing] clause_meta  : ClauseData                      # IN
  [existing] clause_id_in : Signal(range(max_clauses))      # IN
  [existing] meta_valid   : Signal()                        # IN
+ [new]      meta_ready   : Signal()                        # OUT ← backpressure to Prefetcher
  [existing] result       : EvalResult                      # OUT
  [existing] result_valid : Signal()                        # OUT
+ [new]      result_ready : Signal()                        # IN  ← from output mux
```

---

### 4. Implication FIFO

The existing `fifo_full` and `pop_ready` signals already provide the right semantics. The key change is ensuring `fifo_full` is properly wired as backpressure to the evaluator, and that the pop-side output holds stable when software is not ready.

**Changes:**

- Wire `push_ready = ~fifo_full` and connect it to the evaluator's `result_ready` (via the output mux).
- Verify that when `pop_valid & ~pop_ready`, the FIFO holds `pop_var`, `pop_value`, and `pop_reason` stable (standard FIFO behavior — confirm in implementation).

```python
# Rename / clarify existing signals:
push_ready  : Signal()   # OUT — = ~fifo_full, this IS the backpressure signal
                         # Must be wired into evaluator result_ready (via mux with conflict path)

# Pop side (existing, verify hold behavior):
# pop_var, pop_value, pop_reason must be stable while pop_valid & ~pop_ready
```

**Interface diff:**
```
ImplicationFIFO:
  [existing] push_valid  : Signal()                    # IN
+ [rename]   push_ready  : Signal()                    # OUT — was fifo_full (inverted); make explicit
  [existing] pop_valid   : Signal()                    # OUT
  [existing] pop_ready   : Signal()                    # IN  ← from software
  [existing] fifo_full   : Signal()                    # OUT (keep for status monitoring)
  [existing] fifo_empty  : Signal()                    # OUT
```

---

### 5. Conflict Path (Top-Level)

Currently the conflict signal pulses and returns immediately. In an elastic pipeline, the conflict must be held until software explicitly acknowledges it, and the pipeline must be frozen during this period to prevent spurious implications from being enqueued after the conflict.

**Changes:**

- Add `conflict_ack` input from software.
- Hold `conflict` and `conflict_clause_id` stable until `conflict_ack` is received.
- Assert `pipeline_stall` while waiting for acknowledgement.

```python
# NEW input:
conflict_ack    : Signal()   # IN — software pulses to acknowledge conflict

# Internal change:
# Latch conflict_clause_id on detection
# Hold conflict=1 until conflict_ack=1
# Assert pipeline_stall while conflict & ~conflict_ack
```

**Interface diff (BCPAccelerator top-level):**
```
BCPAccelerator:
  [existing] conflict           : Signal()                    # OUT — now held until ack
  [existing] conflict_clause_id : Signal(range(max_clauses))  # OUT — held stable
+ [new]      conflict_ack       : Signal()                    # IN  ← software acknowledges
```

---

## New Top-Level Stall Logic

Add an internal `pipeline_stall` signal computed from all stall sources and distributed to each stage:

```python
# Stall sources:
stall_fifo_full  = impl_fifo.fifo_full
stall_conflict   = conflict_latched & ~conflict_ack

# Global stall (propagates right-to-left through pipeline):
pipeline_stall   = stall_fifo_full | stall_conflict

# Each stage's upstream-facing ready signal incorporates stall:
evaluator.meta_ready    = (eval_state == IDLE) & ~pipeline_stall
prefetcher.clause_ready = ~prefetch_output_buf_full & ~pipeline_stall
wlm.downstream_ready   = prefetcher.clause_id_ready
```

---

## Updated Interface Summary (Diff View)

The table below shows only the signals that are **added or changed**. Existing signals not listed here remain unchanged.

| Module | Signal | Direction | Type | Notes |
|--------|--------|-----------|------|-------|
| `WatchListManager` | `clause_id_ready` | IN | `Signal()` | From Prefetcher; gates `watch_ptr` increment |
| `ClausePrefetcher` | `clause_id_ready` | OUT | `Signal()` | Backpressure to WLM; `= ~output_buf_full` |
| `ClausePrefetcher` | `meta_ready` | IN | `Signal()` | From Evaluator; stalls BRAM reads |
| `ClausePrefetcher` | *(internal)* | — | 2-entry buf | Holds BRAM results when evaluator stalls |
| `ClauseEvaluator` | `meta_ready` | OUT | `Signal()` | `= (state == IDLE) & ~pipeline_stall` |
| `ClauseEvaluator` | `result_ready` | IN | `Signal()` | From FIFO/conflict mux; gates IDLE return |
| `ImplicationFIFO` | `push_ready` | OUT | `Signal()` | `= ~fifo_full`; rename from implicit full |
| `BCPAccelerator` | `conflict_ack` | IN | `Signal()` | SW acknowledges conflict; releases stall |
| `BCPAccelerator` | `pipeline_stall` | internal | `Signal()` | `= fifo_full \| (conflict & ~ack)` |

---

## Revised Pipeline Data Flow

```
     ┌─────────────────────────────────────────────────────────────────┐
     │                    Backpressure (right → left)                  │
     │                                                                  │
     ▼                                                                  │
  WLM ──[cid | valid/ready]──► Prefetcher ──[meta | valid/ready]──► Evaluator
                                  │  ▲                                  │
                          2-entry │  │ clause_id_ready              result_valid
                          output  │  └──────── ~buf_full             result_ready
                          buffer  │                                      │
                                  │                         ┌────────────┴──────────┐
                                  │                         ▼ (UNIT)                ▼ (CONFLICT)
                                  │                      Impl FIFO           conflict_valid
                                  │                      push_ready          conflict_ack ◄── SW
                                  │                      = ~full              (held until ack)
                                  │                         │
                                  │                         ▼
                                  │                      SW pop
                                  │                      pop_ready ◄── SW
                                  │
                                  └── pipeline_stall ──────────────────────────────────►
                                      (distributed to all stages when fifo_full or conflict)
```

---

## Implementation Notes for Claude Code

### Suggested Implementation Order

1. Add `valid/ready` to the **WLM → Prefetcher** boundary first (simplest — no buffering needed on WLM side).
2. Implement the **2-entry skid buffer** inside the Prefetcher. This is the hardest piece; get it right before moving on.
3. Add `meta_ready` and `result_ready` to the **Evaluator FSM**. Modify `DONE` state to wait for `result_ready`.
4. Wire `push_ready = ~fifo_full` from the **FIFO** back to the evaluator's `result_ready`.
5. Add `conflict_ack` and the **conflict latch** at the top level.
6. Add `pipeline_stall` and distribute it.

### Skid Buffer Implementation Note

The 2-entry output buffer in the Prefetcher needs to handle the case where a BRAM read was already in-flight when the evaluator asserted backpressure. A minimal implementation:

```python
# Two registers: slot 0 (head, output side) and slot 1 (tail, overflow)
# State: empty, one_entry, two_entries (full)
#
# On BRAM result arriving:
#   if empty: write to slot 0, state → one_entry
#   if one_entry and ~meta_ready: write to slot 1, state → two_entries (full)
#   if one_entry and meta_ready: pass through / write to slot 0 (keep one_entry)
#
# On meta_ready from evaluator:
#   if two_entries: shift slot 1 → slot 0, state → one_entry
#   if one_entry: state → empty
#
# clause_id_ready = ~(state == two_entries)
```

### Amaranth HDL Pattern

The standard Amaranth pattern for a valid/ready registered stage:

```python
# Downstream handshake
with m.If(out_valid & out_ready):
    m.d.sync += out_valid.eq(0)   # clear after accepted

# Accept new input only when output slot is free
with m.If(in_valid & in_ready):
    m.d.sync += [
        out_data.eq(in_data),
        out_valid.eq(1),
    ]

# Ready to accept when output is either empty or being consumed this cycle
m.d.comb += in_ready.eq(~out_valid | out_ready)
```

---

## Summary of What Does NOT Change

- **Memory interfaces** (ClauseMemory, WatchListMemory, AssignmentMemory) — internal BRAM access patterns are unchanged; only the modules consuming them gain flow control.
- **ClauseData and EvalResult data structures** — no changes to field definitions.
- **Implication FIFO depth** — 16 entries is sufficient; no resize needed.
- **Software interface signals** `impl_var`, `impl_value`, `impl_reason`, `impl_valid`, `impl_ready`, `done`, `busy`, `false_lit`, `start` — all unchanged except `conflict` now holds until `conflict_ack`.
- **Memory partitioning / banking scheme** — no changes.
- **Resource estimates** — logic overhead for handshaking is negligible (<50 additional LUTs/FFs estimated).

---

*End of Elastic Pipeline Modification Spec*
