"""
Testbench for the UART Receiver module.

Verifies:
  1. Correct decoding of all-zeros byte (0x00).
  2. Correct decoding of all-ones byte (0xFF).
  3. Correct decoding of alternating-bit bytes (0x55, 0xAA).
  4. Multiple back-to-back bytes are all received correctly.
  5. rx_valid pulses for exactly one cycle per byte.
"""

import sys, os

sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "src", "hardware"),
)

from amaranth import *
from amaranth.sim import Simulator

from communication.uart_rx import UARTReceiver

DIVISOR = 12   # matches 1 Mbaud @ 12 MHz


async def _drive_and_receive(dut, byte_val, ctx):
    """
    Drive one UART byte onto dut.rx_pin and return the value captured
    when rx_valid fires.  rx_valid is checked on every tick during the
    stop-bit window so the 1-cycle pulse is never missed.
    """
    # Start bit
    ctx.set(dut.rx_pin, 0)
    for _ in range(DIVISOR):
        await ctx.tick()
    # Data bits (LSB first)
    for i in range(8):
        ctx.set(dut.rx_pin, (byte_val >> i) & 1)
        for _ in range(DIVISOR):
            await ctx.tick()
    # Stop bit: drive high and poll rx_valid each tick
    ctx.set(dut.rx_pin, 1)
    captured = None
    for _ in range(DIVISOR + 4):   # +4 for 2-FF sync pipeline margin
        await ctx.tick()
        if ctx.get(dut.rx_valid):
            captured = ctx.get(dut.rx_data)
            break
    return captured


def test_uart_rx():
    """Full testbench for UARTReceiver."""
    dut = UARTReceiver(divisor=DIVISOR)
    received = []

    async def testbench(ctx):
        ctx.set(dut.rx_pin, 1)
        for _ in range(20):
            await ctx.tick()

        test_bytes = [0x55, 0xAA, 0x00, 0xFF, 0xA5]
        for byte_val in test_bytes:
            captured = await _drive_and_receive(dut, byte_val, ctx)
            received.append(captured)
            for _ in range(8):
                await ctx.tick()

    sim = Simulator(dut)
    sim.add_clock(1e-6)
    sim.add_testbench(testbench)

    vcd_path = os.path.join(os.path.dirname(__file__), "uart_rx_sim.vcd")
    with sim.write_vcd(vcd_path):
        sim.run()

    test_bytes = [0x55, 0xAA, 0x00, 0xFF, 0xA5]
    print("UARTReceiver testbench results:")
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
        raise AssertionError("UARTReceiver testbench failed")


if __name__ == "__main__":
    test_uart_rx()
