# Makefile — CDCL SAT Solver with optional BCP Hardware Accelerator
#
# Targets:
#   all                Software-only solver (default)
#   hw / hw-jtag       Hardware-enabled solver using JTAG communication
#   hw-uart            Hardware-enabled solver using UART communication (legacy)
#   test-sw            Build and run the C software test suite
#   test-hw            Run all pytest hardware tests
#   test-integration   Run the full-stack UART integration test
#   test-jtag          Run JTAG host interface unit tests
#   test-integration-jtag  Run JTAG full-stack integration test
#   test               Run all tests (software + hardware)
#   synth              Synthesise JTAG FPGA bitstream (default)
#   synth-uart         Synthesise UART FPGA bitstream (legacy)
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
SRCS_HW_UART = $(SRCS_COMMON) $(SRC_DIR)/hw_interface.c

# Test source
TEST_SW_SRC = $(TEST_DIR)/software/test_CDCL.c $(SRC_DIR)/CDCL.c

.PHONY: all hw hw-jtag hw-uart test-sw test-hw test-integration \
        test-jtag test-integration-jtag test-jtag-hw test synth synth-uart clean

# ── Software-only build ───────────────────────────────────────────────────
all: sat_solver

sat_solver: $(SRCS_COMMON)
	$(CC) $(CFLAGS) -o $@ $^ $(LDFLAGS)

# ── Hardware-enabled build (JTAG — default) ──────────────────────────────
hw: hw-jtag

hw-jtag: sat_solver_hw

sat_solver_hw: $(SRCS_HW_JTAG)
	$(CC) $(CFLAGS) -DUSE_HW_BCP -o $@ $^ $(LDFLAGS)

# ── Hardware-enabled build (UART — legacy) ───────────────────────────────
hw-uart: sat_solver_hw_uart

sat_solver_hw_uart: $(SRCS_HW_UART)
	$(CC) $(CFLAGS) -DUSE_HW_BCP -o $@ $^ $(LDFLAGS)

# ── Software tests ────────────────────────────────────────────────────────
test-sw: test_CDCL
	./test_CDCL

test_CDCL: $(TEST_SW_SRC)
	$(CC) $(CFLAGS) -o $@ $^ $(LDFLAGS)

# ── Hardware tests (pytest) ───────────────────────────────────────────────
test-hw:
	cd $(HW_DIR) && python -m pytest ../../$(TEST_DIR)/hardware/ -v

# ── Integration test only (UART) ─────────────────────────────────────────
test-integration:
	cd $(HW_DIR) && python -m pytest ../../$(TEST_DIR)/hardware/test_integration.py -v

# ── JTAG host interface unit tests ───────────────────────────────────────
test-jtag:
	cd $(HW_DIR) && python -m pytest ../../$(TEST_DIR)/hardware/communication/test_jtag_host_interface.py -v

# ── JTAG integration test ────────────────────────────────────────────────
test-integration-jtag:
	cd $(HW_DIR) && python -m pytest ../../$(TEST_DIR)/hardware/test_integration_jtag.py -v

# ── JTAG hardware loopback test (requires FPGA board) ────────────────────
test-jtag-hw: test_jtag_loopback
	./test_jtag_loopback

test_jtag_loopback: $(TEST_DIR)/test_jtag_loopback.c
	$(CC) $(CFLAGS) -o $@ $^ $(LDFLAGS)

# ── All tests ─────────────────────────────────────────────────────────────
test: test-sw test-hw

# ── FPGA synthesis (JTAG — default) ──────────────────────────────────────
synth:
	cd $(HW_DIR) && python top_jtag.py

# ── FPGA synthesis (UART — legacy) ───────────────────────────────────────
synth-uart:
	cd $(HW_DIR) && python top.py

# ── Clean ─────────────────────────────────────────────────────────────────
clean:
	rm -f sat_solver sat_solver_hw sat_solver_hw_uart test_CDCL test_jtag_loopback
	find $(TEST_DIR)/hardware $(HW_DIR) -name '*.vcd' -delete 2>/dev/null; rm -f *.vcd
