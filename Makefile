# Makefile — CDCL SAT Solver with optional BCP Hardware Accelerator
#
# Targets:
#   all                Software-only solver (default)
#   hw / hw-jtag       Hardware-enabled solver using JTAG communication
#   test-sw            Build and run the C software test suite
#   test-hw            Run ALL pytest hardware tests (modules + memory + communication + integration)
#   test-modules       Run BCP pipeline module tests only
#   test-memory        Run memory subsystem tests only
#   test-communication Run JTAG communication tests only
#   test-integration   Run JTAG full-stack integration tests only
#   test               Run all tests (software + hardware)
#   synth              Synthesise JTAG FPGA bitstream
#   bench-download     Fetch SATLIB benchmarks
#   bench-run          Run benchmarking suite (hw_sim mode)
#   bench-sw           Run software-only baseline benchmarks
#   clean              Remove build artifacts

CC       = gcc
CFLAGS   = -O2 -Wall -Isrc/software
LDFLAGS  = -lm

SRC_DIR  = src/software
HW_DIR   = src/hardware
TEST_DIR = test

# Source files
SRCS_COMMON  = $(SRC_DIR)/main.c $(SRC_DIR)/CDCL.c
SRCS_HW_JTAG = $(SRCS_COMMON) $(SRC_DIR)/hw_interface_jtag.c

# Test source
TEST_SW_SRC = $(TEST_DIR)/software/test_CDCL.c $(SRC_DIR)/CDCL.c

.PHONY: all hw hw-jtag test-sw test-hw test-modules test-memory \
        test-communication test-integration test synth clean \
        bench-download bench-run bench-sw

# ── Software-only build ───────────────────────────────────────────────────
all: sat_solver

sat_solver: $(SRCS_COMMON)
	$(CC) $(CFLAGS) -o $@ $^ $(LDFLAGS)

# ── Hardware-enabled build (JTAG) ─────────────────────────────────────────
hw: hw-jtag

hw-jtag: sat_solver_hw

sat_solver_hw: $(SRCS_HW_JTAG)
	$(CC) $(CFLAGS) -DUSE_HW_BCP -o $@ $^ $(LDFLAGS)

# ── Software tests ────────────────────────────────────────────────────────
test-sw: test_CDCL
	./test_CDCL

test_CDCL: $(TEST_SW_SRC)
	$(CC) $(CFLAGS) -o $@ $^ $(LDFLAGS)

# ── Hardware tests (pytest) ───────────────────────────────────────────────

# Run ALL hardware tests
test-hw:
	cd $(HW_DIR) && python -m pytest ../../$(TEST_DIR)/hardware/ -v

# BCP pipeline modules (accelerator, clause evaluator, prefetcher, etc.)
test-modules:
	cd $(HW_DIR) && python -m pytest ../../$(TEST_DIR)/hardware/modules/ -v

# Memory subsystems (assignment, clause, watch list)
test-memory:
	cd $(HW_DIR) && python -m pytest ../../$(TEST_DIR)/hardware/memory/ -v

# JTAG communication (host interface)
test-communication:
	cd $(HW_DIR) && python -m pytest ../../$(TEST_DIR)/hardware/communication/ -v

# Full-stack JTAG integration tests
test-integration:
	cd $(HW_DIR) && python -m pytest ../../$(TEST_DIR)/hardware/test_integration_jtag.py -v

# ── All tests ─────────────────────────────────────────────────────────────
test: test-sw test-hw

# ── FPGA synthesis (JTAG) ─────────────────────────────────────────────────
synth:
	cd $(HW_DIR) && python top_jtag.py

# ── Benchmarking ─────────────────────────────────────────────────────────
bench-download:
	python -m bench.download_benchmarks

bench-run:
	python -m bench.benchmark_runner --family uf20-91 --mode hw_sim --max-instances 10

bench-sw:
	python -m bench.benchmark_runner --family uf20-91 --mode sw_only

# ── Clean ─────────────────────────────────────────────────────────────────
clean:
	rm -f sat_solver sat_solver_hw test_CDCL
	rm -rf $(TEST_DIR)/logs/*.vcd
