⏺ Here's how to run it on the FPGA:                                                     
                                                                                        
  Step 1: Rebuild the bitstream                                                         
                                                                                      
  make synth                                                                            
  This runs python top_jtag.py which invokes Amaranth's build flow (yosys → nextpnr-ecp5
   → ecppack), producing a new bcp_accel_jtag.bit with the Capture-DR fix.              
                                                                                        
  Step 2: Flash the bitstream                                                           

  Using the OpenOCD config you already have:
  openocd -f openocd-ecp5.cfg -c "transport select jtag; init; svf
  src/hardware/build/bcp_accel_jtag.svf; exit"
  Or if you normally use ecpprog directly:
  ecpprog src/hardware/build/bcp_accel_jtag.bit

  Step 3: Run the hardware loopback test

  make test-jtag-hw
  This builds and runs test_jtag_loopback, which sends JTAG commands to the actual FPGA
  and verifies responses come back correctly. This is the real verification that the
  Capture-DR trigger is working — if commands are being latched and responses are
  loading properly, you'll see passing results instead of the stale/zero responses from
  the old jupdate-based logic.


  Option 2: Full SAT solver on real hardware

  The driver in hw_interface_jtag.c is already wired up. It uploads clauses, watch
  lists, and assignments, then runs BCP with implication readback. To run the full
  solver on a CNF problem:

  make hw                   # builds sat_solver_hw (links hw_interface_jtag.c)
  ./sat_solver_hw <input.cnf>

  This will:
  1. Fork OpenOCD in the background (using openocd-ecp5.cfg)
  2. Connect to its TCL server
  3. Parse the CNF, upload the problem to the FPGA via JTAG
  4. Run CDCL with hardware-accelerated BCP

  Use a small CNF file to start (few variables, few clauses) so you can verify
  correctness before scaling up. If you don't have one handy, a trivial 3-variable
  problem works:

  p cnf 3 2
  1 2 0
  -1 3 0

  What to watch for

  - Stuck at BUSY/IDLE: The Capture-DR fix should resolve this — if you still see it,
  check the LEDs you added
  - ack_seq mismatch: Means commands are being dropped or the FIFO isn't draining
  - Wrong implication values: Could indicate bit-reversal issues (the reverse_128 in the
   hardware path)

  ┌─────┬───────────────────────────────────────────────────┬─────────────┐
  │ LED │ Behavior                                          │ Domain      │
  ├─────┼───────────────────────────────────────────────────┼─────────────┤
  │ 7   │ Heartbeat (heartbeat[23], ~1 Hz toggle)           │ sync        │
  ├─────┼───────────────────────────────────────────────────┼─────────────┤
  │ 6   │ Pulse (~0.5s) on er1_shifted & jupdate            │ jtck        │
  ├─────┼───────────────────────────────────────────────────┼─────────────┤
  │ 5   │ Pulse (~0.5s) on rising edge of cmd_pending       │ sync        │
  ├─────┼───────────────────────────────────────────────────┼─────────────┤
  │ 4   │ Pulse (~0.5s) while/after sr_gate & jshift active │ jtck        │
  ├─────┼───────────────────────────────────────────────────┼─────────────┤
  │ 3   │ Solid while in CMD_EXEC                           │ sync (comb) │
  ├─────┼───────────────────────────────────────────────────┼─────────────┤
  │ 2   │ Solid while in BCP_WAIT                           │ sync (comb) │
  ├─────┼───────────────────────────────────────────────────┼─────────────┤
  │ 1   │ Solid while in IMPL_READY                         │ sync (comb) │
  ├─────┼───────────────────────────────────────────────────┼─────────────┤
  │ 0   │ Solid while in DONE_READY                         │ sync (comb) │
  └─────┴───────────────────────────────────────────────────┴─────────────┘

    ┌────────────────────────┬───────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┐
  │         Target         │                                                         Scope                                                         │   
  ├────────────────────────┼───────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┤   
  │ make test-hw           │ All 12 hardware tests                                                                                                 │   
  ├────────────────────────┼───────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┤   
  │ make test-modules      │ 6 BCP pipeline module tests (accelerator, end-to-end, clause evaluator, prefetcher, implication FIFO, watch list      │   
  │                        │ manager)                                                                                                              │   
  ├────────────────────────┼───────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┤
  │ make test-memory       │ 3 memory tests (assignment, clause, watch list)                                                                       │
  ├────────────────────────┼───────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┤
  │ make                   │ 1 JTAG host interface test                                                                                            │
  │ test-communication     │                                                                                                                       │
  ├────────────────────────┼───────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┤
  │ make test-integration  │ 2 JTAG full-stack integration tests                                                                                   │
  ├────────────────────────┼───────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┤
  │ make test              │ All tests (software + hardware)                                                                                       │
  └────────────────────────┴───────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┘