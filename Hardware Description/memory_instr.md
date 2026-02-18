I am creating an accelerator the BCP "propagate()" function as seen in src/software/CDCL.c. The complete system architecture for reference is listed in Hardware Description/BCP_Accelerator_System_Architecture.md.

Now, we will implement the Top-Level Module in Amaranth HDL and a testbench to verify we can read and write based on the proper spec:

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
