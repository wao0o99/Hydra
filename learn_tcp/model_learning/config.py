"""Network configuration for TCP state learning.

Edit these values to match your test environment:
  - my_ip / my_port: testing machine (where this code runs)
  - SUL_ip / SUL_port_control: SUT machine (the target being tested)
  - interface: network interface for raw packet I/O
"""

# SUT (System Under Test)
SUL_ip = "192.168.77.20"
SUL_port_control = 5000

# Testing machine
my_ip = "192.168.77.10"
my_port = 20000
my_port_control = 5000
interface = "test0"

# Timing
RCV_WAIT_TIME = 0.5  # max wait for a response (seconds)
TIME_SLOT = 0.1  # poll interval (seconds)

# Learning
MAX_CONCURRENCY = 300  # max concurrent MQ/EQ threads
