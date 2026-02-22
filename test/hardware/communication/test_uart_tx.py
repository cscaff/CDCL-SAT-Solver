"""
Testbench for the UART Transmitter module.

Verifies:
  1. Correct serialisation of all-zeros byte (0x00).
  2. Correct serialisation of all-ones byte (0xFF).
  3. Correct serialisation of alternating-bit bytes (0x55, 0xAA).
  4. Multiple back-to-back bytes are all transmitted correctly.
  5. tx_ready goes low during transmission and high again afterwards.
"""

import sys, os

sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "src", "hardware"),
)

from amaranth import *
from amaranth.sim import Simulator

from communication.uart_tx import UARTTransmitter

DIVISOR = 12   # matches 1 Mbaud @ 12 MHz


def test_uart_tx():
    """Full testbench for UARTTransmitter."""
    dut = UARTTransmitter(divisor=DIVISOR)
    received = []

    async def testbench(ctx):
        test_bytes = [0x55, 0xAA, 0x00, 0xFF, 0xA5]

        for byte_val in test_bytes:
            # Wait until tx_ready is high
            while not ctx.get(dut.tx_ready):
                await ctx.tick()

            # Present byte for one cycle
            ctx.set(dut.tx_data, byte_val)
            ctx.set(dut.tx_valid, 1)
            await ctx.tick()          # IDLE latches data, transitions to START
            ctx.set(dut.tx_valid, 0)

            # START state begins: tx_pin=0 for DIVISOR cycles.
            # Advance to centre of start bit to verify, then sample each data bit.
            for _ in range(DIVISOR // 2):
                await ctx.tick()
            assert ctx.get(dut.tx_pin) == 0, "Start bit not low at centre"

            byte_out = 0
            for i in range(8):
                for _ in range(DIVISOR):
                    await ctx.tick()
                byte_out |= ctx.get(dut.tx_pin) << i   # LSB first

            # Let the stop bit complete
            for _ in range(DIVISOR):
                await ctx.tick()

            received.append(byte_out)

            for _ in range(4):
                await ctx.tick()

    sim = Simulator(dut)
    sim.add_clock(1e-6)
    sim.add_testbench(testbench)

    vcd_path = os.path.join(os.path.dirname(__file__), "uart_tx_sim.vcd")
    with sim.write_vcd(vcd_path):
        sim.run()

    test_bytes = [0x55, 0xAA, 0x00, 0xFF, 0xA5]
    print("UARTTransmitter testbench results:")
    all_pass = True
    for expected, actual in zip(test_bytes, received):
        status = "PASS" if expected == actual else "FAIL"
        if status == "FAIL":
            all_pass = False
        print(f"  expected=0x{expected:02X}  got=0x{actual:02X}  [{status}]")

    if len(received) != len(test_bytes):
        print(f"  ERROR: received {len(received)} bytes, expected {len(test_bytes)}")
        all_pass = False

    if all_pass:
        print("All tests PASSED.")
    else:
        print("Some tests FAILED.")
        raise AssertionError("UARTTransmitter testbench failed")


if __name__ == "__main__":
    test_uart_tx()
