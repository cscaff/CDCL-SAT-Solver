# BCP Engine Hardware Description

## Top Module Overview

### Signals

Inputs:
1. false_lit - The literal that just became false (from the trail)
2. start - Signal to begin processing 

Outputs:
1. done - BCP finished processing
2. conflict - Did we find a conflict? (yes/no)
3. conflict_clause_id - Which clause conflicted (if conflict=yes)
4. implications - Stream of (variable, value, reason_clause) tuples

### Watch List Manager Module

#### Signals
Inputs:
1. false_lit
2. start

Outputs:
1. done
2. clause_id
3. clause_id_valid - (Handshake for back-pressure from downstream)

Functional Behavior:
   - Read Watch List Length
   - Parallel Read P banks from WatchList Memory
   - Output one valid clause ID per cycle 


### 

## Memory Overview

### Clause Database
For each clause:
  - Satisfaction bit: (is clause currently satisfied?)
  - Array of literals [lit_0, lit_1, ..., lit_K-1]
  - Clause size (how many literals, can be variable)

Parameters:
   - max_clauses (how many clauses total)
   - max_k (max literals per clause)
   - max_vars (max variables in total (literal = var w/ polarity))

Storage format:
Array of clauses, each clause has:
  - sat_bit: 1 bit (is clause currently satisfied?)
  - size:      3 bits (0-7, since max_k=5 we only need up to 5)
  - lits[max_k]: 5 × 16 bits = 80 bits (for max_vars=512, literal encoding needs ~10 bits, round to 16)

Memory Structure Example For Singular Clause Index:
```
[sat_bit] [size] [lit_0] [lit_1] [lit_2] [lit_3] [lit_4]
 1 bit   3 bits  16 bits 16 bits 16 bits 16 bits 16 bits
```
  
### Watch Lists
For each literal (2 × max_vars (Literal = !a or a) total):
  - Array of clause indices watching this literal
  - Length of array (how many clauses)

Parameters:
- num_watch_lists = 2 × max_vars = 1024 (one per positive/negative literal)
- max_watch_len = 100 (Conservative Estimate -Number of clauses watching that literal (0_MAX_CLAUSES))

Allocate a fixed maximum per watch list (They are variable).

Conservative estimate:
  - Average clause size: 3-4 literals
  - Each literal appears in ~(total_clauses × avg_size / num_vars) clauses
  - Example: 8192 clauses × 3.5 lits / 512 vars ≈ 56 clauses per variable
  - Double it for safety: 100 entries per watch list
  
Storage per watch list:
  - 100 × 16 bits (clause index) = 200 bytes
  - Plus length: 2 bytes
  - Total: ~202 bytes per watch list
  
For 1024 watch lists: 202 × 1024 ≈ 206 KB

Storage Format:
- num_watch_lists: 2 × max_vars = 1024 (one for each literal: a, ¬a, b, ¬b, ...)
- Each watch list has:
  - length: how many clauses are watching this literal
  - clause_ids[]: array of clause indices

We start w/ a Fixed-Length Storage for simplicity. (Later, we will move to pointer-based variable storage).

Memory Structure Example for Singular Watch List:
```
[length] [clause_id_0] [clause_id_1] [clause_id_2] ... [clause_id_99]
 7 bits    13 bits       13 bits       13 bits           13 bits

Total per watch list: 7 + (100 × 13) = 1307 bits ≈ 164 bytes
```

Should have P banks to perform conflict-free partitioning from FYalSAT

### Variable Assignments
For each variable:
  - Current assignment: UNASSIGNED / FALSE / TRUE

Parameters: 
  - max_vars = 512

Storage Format:
  - 2 bits per variable (0=UNASSIGNED, 1=FALSE, 2=TRUE)

Memory Structure Example for Each Variable:
```
[var_0_value] [var_1_value] [var_2_value] [var_3_value] ... [var_511_value]
    2 bits        2 bits        2 bits        2 bits            2 bits

Total: 512 vars × 2 bits = 1024 bits = 128 bytes
```




# Notes:
Make sure this works with the typical CVC5 format.