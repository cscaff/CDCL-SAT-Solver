⏺ Here's the current state and how to run everything:                                                                                                                           
                                                                                                                                                                                
  Is it ready for FPGA?                                                                                                                                                         
                                                                                                                                                                                
  The hardware design is ready for synthesis, but actual FPGA deployment requires:                                                                                              
                                                                                                                                                                                
  1. A Lattice ECP5-5G Evaluation Board (the platform target in top.py)                                                                                                         
  2. The Yosys/nextpnr open-source FPGA toolchain (used by Amaranth under the hood)                                                                                             
  3. A USB-serial connection between your Mac and the board

  You'd also need to figure out the actual serial port name on your Mac (something like /dev/cu.usbserial-XXXXX) — it shows up when you plug in the board.

  How to run everything

  Simulation tests (no FPGA needed)

  # Run ALL tests (software + hardware simulation)
  make test

  # Software solver tests only (C unit tests)
  make test-sw

  # All hardware simulation tests (pytest + Amaranth sim)
  make test-hw

  # Just the full-stack UART integration test
  make test-integration

  Build the solver

  # Software-only solver (runs entirely on CPU)
  make
  ./sat_solver some_problem.cnf

  # Hardware-enabled solver (needs FPGA connected)
  make hw
  ./sat_solver_hw -p /dev/cu.usbserial-XXXXX some_problem.cnf

  FPGA synthesis (generates bitstream, doesn't program)

  make synth

  This runs top.py which calls Amaranth's platform.build(). It produces a bitstream file but doesn't flash it (do_program=False). To actually program the board you'd change
  that flag or use openFPGALoader.

  Quick test right now

  The fastest way to verify everything works without hardware:

  # Build and test the solver on a small problem
  make && echo "p cnf 3 2
  1 -2 0
  2 3 0" | ./sat_solver /dev/stdin

  # Run the full-stack simulation (UART → HostInterface → BCP → UART)
  make test-integration

  The integration test is the closest thing to "running on the FPGA" — it bit-bangs actual UART frames through the entire hardware stack in simulation, using the exact same
  byte protocol that hw_interface.c will speak over the serial port.