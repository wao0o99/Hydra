# Hydra

Hydra learns TCP state machines through active learning and input exploration.

[Paper (PDF)](ao_ccs2026.pdf)

Requires two connected Linux machines: a learner and a system under test (SUT).
Tested on Ubuntu 24.04 with Python 3.12. Run commands from the repository root.

## Installation

Python dependencies: `pcapy-ng`, `scapy`, `dpkt`, `pydot`, `rich`, and `psutil`.

## Usage

Set `my_ip`, `interface`, and `SUL_ip` in [configs/tcp.toml](configs/tcp.toml).
Allow TCP traffic between them; the SUT control port is 5000.

### 1. Start the SUT

Set these entries in `/etc/sysctl.conf`, then run `sudo sysctl -p`:

```ini
net.ipv4.tcp_retries1=0
net.ipv4.tcp_retries2=15
net.ipv4.tcp_synack_retries=15
net.ipv4.tcp_syncookies=1
net.ipv4.tcp_fin_timeout=240
```

```bash
sudo apt install build-essential
mkdir -p build runs
gcc -O2 SUT.c -o build/hydra-sut
sudo bash -c 'ulimit -n 65535; exec ./build/hydra-sut --log runs/sut.log'
```

### 2. Start the learner

Keep the SUT running, then on the learner:

```bash
sudo bash -c 'ulimit -n 65535; exec .venv/bin/hydra learn --config configs/tcp.toml --seed 4220090961 --output runs'
```

## Acknowledgements

The learning implementation is adapted from [AALpy](https://github.com/DES-Lab/AALpy),
licensed under the [MIT License](aalpy/LICENCE.txt).
