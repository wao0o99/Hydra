"""Output paths configured explicitly for each run; imports have no side effects."""

from pathlib import Path

_BASE = None


def configure(path):
    global _BASE, LOGS, MODELS, PCAP, CEX, INPUTS, DEBUG
    global LOG_FILE, TUI_LOG_FILE, PCAP_FILE, CEX_FILE, CEX_DETAIL_FILE, CEX_KNOWN_FILE
    global INPUT_FILE, NEW_INPUT_FILE, EQUAL_INPUT_FILE, NONDET_LOG_FILE, OB_TABLE_FILE
    _BASE = Path(path)
    LOGS, MODELS, PCAP, CEX, INPUTS, DEBUG = [
        str(_BASE / n)
        for n in ("logs", "models", "captures", "counterexamples", "inputs", "debug")
    ]
    for directory in (LOGS, MODELS, PCAP, CEX, INPUTS, DEBUG):
        Path(directory).mkdir(parents=True, exist_ok=True)
    LOG_FILE = str(Path(LOGS) / "hydra.log")
    TUI_LOG_FILE = str(Path(LOGS) / "terminal.log")
    PCAP_FILE = str(Path(PCAP) / "packets_0001.pcap")
    CEX_FILE = str(Path(CEX) / "counterexamples.jsonl")
    CEX_DETAIL_FILE = CEX_FILE
    CEX_KNOWN_FILE = str(Path(CEX) / "known_counterexamples.jsonl")
    INPUT_FILE = str(Path(INPUTS) / "exploration.jsonl")
    NEW_INPUT_FILE = str(Path(INPUTS) / "accepted.txt")
    EQUAL_INPUT_FILE = str(Path(INPUTS) / "equivalent.txt")
    NONDET_LOG_FILE = str(Path(DEBUG) / "cache_nondet.txt")
    OB_TABLE_FILE = str(Path(DEBUG) / "observation_table.txt")
    Path(CEX_KNOWN_FILE).touch(exist_ok=True)


def init(clean=False):
    if _BASE is None:
        raise RuntimeError("Configure a run directory before learning")
    if clean:
        raise ValueError("Existing run data must not be deleted by initialization")


def model_path(index):
    if _BASE is None:
        raise RuntimeError("No run directory configured")
    return str(Path(MODELS) / f"hypothesis_{index + 1:04d}.dot")
