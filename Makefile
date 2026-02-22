# Makefile — CDCL SAT Solver with optional BCP Hardware Accelerator
#
# Targets:
#   all             Software-only solver (default)
#   hw              Hardware-enabled solver (links hw_interface.c, defines USE_HW_BCP)
#   test-sw         Build and run the C software test suite
#   test-hw         Run all pytest hardware tests
#   test-integration Run the full-stack UART integration test
#   test            Run all tests (software + hardware)
#   synth           Synthesise the FPGA bitstream via Amaranth
#   clean           Remove build artifacts

CC       = gcc
CFLAGS   = -O2 -Wall -Isrc/software
LDFLAGS  = -lm

SRC_DIR  = src/software
HW_DIR   = src/hardware
TEST_DIR = test

# Source files
SRCS_COMMON = $(SRC_DIR)/main.c $(SRC_DIR)/CDCL.c
SRCS_HW     = $(SRCS_COMMON) $(SRC_DIR)/hw_interface.c

# Test source
TEST_SW_SRC = $(TEST_DIR)/software/test_CDCL.c $(SRC_DIR)/CDCL.c

.PHONY: all hw test-sw test-hw test-integration test synth clean

# ── Software-only build ───────────────────────────────────────────────────
all: sat_solver

sat_solver: $(SRCS_COMMON)
	$(CC) $(CFLAGS) -o $@ $^ $(LDFLAGS)

# ── Hardware-enabled build ────────────────────────────────────────────────
hw: sat_solver_hw

sat_solver_hw: $(SRCS_HW)
	$(CC) $(CFLAGS) -DUSE_HW_BCP -o $@ $^ $(LDFLAGS)

# ── Software tests ────────────────────────────────────────────────────────
test-sw: test_CDCL
	./test_CDCL

test_CDCL: $(TEST_SW_SRC)
	$(CC) $(CFLAGS) -o $@ $^ $(LDFLAGS)

# ── Hardware tests (pytest) ───────────────────────────────────────────────
test-hw:
	cd $(HW_DIR) && python -m pytest ../../$(TEST_DIR)/hardware/ -v

# ── Integration test only ─────────────────────────────────────────────────
test-integration:
	cd $(HW_DIR) && python -m pytest ../../$(TEST_DIR)/hardware/test_integration.py -v

# ── All tests ─────────────────────────────────────────────────────────────
test: test-sw test-hw

# ── FPGA synthesis ────────────────────────────────────────────────────────
synth:
	cd $(HW_DIR) && python top.py

# ── Clean ─────────────────────────────────────────────────────────────────
clean:
	rm -f sat_solver sat_solver_hw test_CDCL
	find $(TEST_DIR)/hardware $(HW_DIR) -name '*.vcd' -delete 2>/dev/null; rm -f *.vcd
