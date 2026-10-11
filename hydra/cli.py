"""Hydra command-line interface."""

import argparse
import fcntl
import json
import logging
import os
import random
import re
import math
import multiprocessing
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import uuid
from datetime import datetime, timezone
from pathlib import Path

from hydra.runtime import Runtime, install, RunStopped

ALPHABET = [
    "LISTEN",
    "ACCEPT",
    "CLOSE",
    "CLOSECONNECTION",
    "0_0_5_0_S_1024_0_0_-1_0_-1_-1_-1",
    "0_0_5_0_A_1024_0_0_-1_0_-1_-1_-1",
    "0_0_5_0_FA_1024_0_0_-1_0_-1_-1_-1",
    "0_0_5_0_R_1024_0_0_-1_0_-1_-1_-1"
]

DEFAULTS = dict(
    SUL_ip="192.168.77.20",
    SUL_port_control=5000,
    my_ip="192.168.77.10",
    my_port=20000,
    my_port_control=5000,
    interface="test0",
    RCV_WAIT_TIME=0.5,
    TIME_SLOT=0.1,
    MAX_CONCURRENCY=300,
)


def duration(value):
    match = re.fullmatch(r"(\d+(?:\.\d+)?)([smh]?)", value)
    if not match or not math.isfinite(float(match[1])) or float(match[1]) <= 0:
        raise argparse.ArgumentTypeError(
            "Duration must be positive, for example 30s, 5m, or 1h"
        )
    return float(match[1]) * {"": 1, "s": 1, "m": 60, "h": 3600}[match[2]]


def configuration(path):
    result = DEFAULTS.copy()
    if path:
        data = tomllib.loads(Path(path).read_text())
        values = data.get("tcp", data)
        unknown = set(values) - set(result)
        if unknown:
            raise ValueError(f"Unknown configuration keys: {sorted(unknown)}")
        result.update(values)
    if (
        type(result["MAX_CONCURRENCY"]) is not int
        or not 1 <= result["MAX_CONCURRENCY"] <= 300
    ):
        raise ValueError(
            "MAX_CONCURRENCY must be an integer between 1 and the SUT capacity of 300"
        )
    for key in ("SUL_port_control", "my_port", "my_port_control"):
        if type(result[key]) is not int or not 1 <= result[key] <= 65535:
            raise ValueError(f"{key} must be a valid port")
    for key in ("RCV_WAIT_TIME", "TIME_SLOT"):
        if (
            type(result[key]) not in (int, float)
            or not math.isfinite(result[key])
            or result[key] <= 0
        ):
            raise ValueError(f"{key} must be positive")
    for key in ("SUL_ip", "my_ip"):
        if not isinstance(result[key], str):
            raise ValueError(f"{key} must be an IPv4 address")
        socket.inet_pton(socket.AF_INET, result[key])
    if not isinstance(result["interface"], str) or not result["interface"]:
        raise ValueError("interface must be a nonempty name")
    return result


class NetworkLease:
    def __init__(self):
        self.stream = None
        self.added = False
        self.rule = ["OUTPUT", "-p", "tcp", "--tcp-flags", "RST", "RST", "-j", "DROP"]

    def open(self):
        if os.geteuid() != 0:
            raise PermissionError("Real packet learning requires root privileges")
        self.stream = open("/run/lock/hydra-tcp.lock", "a")
        fcntl.flock(self.stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if subprocess.run(
            ["iptables", "-C", *self.rule], capture_output=True
        ).returncode:
            subprocess.run(["iptables", "-A", *self.rule], check=True)
            self.added = True
        logging.info("Outgoing RST rule ready; created_by_run=%s", self.added)

    def close(self):
        try:
            if self.added:
                subprocess.run(["iptables", "-D", *self.rule], check=True)
        finally:
            if self.stream:
                self.stream.close()


def run(args):
    if args.command == "learn" and multiprocessing.get_start_method() != "fork":
        raise ValueError(
            "Real capture requires the fork start method; use the supported Linux/Python 3.12 environment"
        )
    if args.profile == "cpu":
        try:
            import yappi
        except ImportError as error:
            raise ValueError(
                "CPU profiling requires the profile extra: pip install hydra-tcp-learning[profile]"
            ) from error
    config = configuration(args.config)
    seed = (
        args.seed if args.seed is not None else random.SystemRandom().randrange(2**32)
    )
    random.seed(seed)
    path = Path(args.output).resolve() / (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        + "_"
        + uuid.uuid4().hex[:8]
    )
    rt = Runtime(args.duration, path / "profiles" if args.profile == "cpu" else None)
    install(rt)
    from hydra.records import RunRecords
    from hydra.ui import Display
    from learn_tcp.model_learning import output_dir

    original_signals = {}

    def stop(signum, frame):
        rt.cancel("user_interrupt" if signum == signal.SIGINT else "terminated")

    for sig in (signal.SIGINT, signal.SIGTERM):
        original_signals[sig] = signal.signal(sig, stop)
    records = None
    try:
        records = RunRecords(
            path, rt, config, args.command, seed, sys.argv, args.log_level
        )
        output_dir.configure(path)
        rt.set(
            alphabet_size=len(ALPHABET), initial_alphabet=ALPHABET, stage="Initializing"
        )
        display = Display(rt, path, args.ui)
    except BaseException:
        try:
            if records is not None:
                records.close("failed", "initialization_failed")
        finally:
            for sig, handler in original_signals.items():
                signal.signal(sig, handler)
        raise
    lease = NetworkLease()
    sul = None
    code = 0
    reason = "completed"
    status = "completed"
    watchdog = None
    cleanup_done = threading.Event()

    def force_cleanup():
        if not cleanup_done.wait(15):
            try:
                print("Shutdown exceeded 15 seconds", file=sys.stderr)

                def save_timeout():
                    try:
                        records.save("failed", "cleanup_timeout")
                    except Exception:
                        pass

                writer = threading.Thread(target=save_timeout, daemon=True)
                writer.start()
                writer.join(timeout=0.5)
                import multiprocessing

                for process in multiprocessing.active_children():
                    process.terminate()
                if lease.added:
                    subprocess.run(
                        ["iptables", "-D", *lease.rule], timeout=2, capture_output=True
                    )
            finally:
                os._exit(1)

    try:
        rt.check()
        from learn_tcp.model_learning import config as module_config

        for key, value in config.items():
            setattr(module_config, key, value)
        lease.open()
        from learn_tcp.model_learning.tcp_sul import TCP_SUL

        sul = TCP_SUL()
        rt.set(worker_capacity=config["MAX_CONCURRENCY"])
        display.start()
        from aalpy.learning_algs import run_Lstar

        rt.start_profile()
        run_Lstar(
            list(ALPHABET),
            sul,
            automaton_type="mealy",
            cache_and_non_det_check=True,
            print_level=0,
            max_concurrency=config["MAX_CONCURRENCY"],
        )
        reason = rt.reason or "completed"
    except RunStopped:
        reason = rt.reason or "user_interrupt"
        status = "stopped"
        code = 124 if reason == "time_budget" else 130
    except Exception as error:
        reason = "execution_failed"
        status = "failed"
        code = 1
        rt.set(error=str(error), verification="failed")
        logging.exception("Run failed")
    finally:
        rt.set(stage="Stopping")
        # Preserve the learning stage separately from cleanup state.
        rt.cancel(reason)
        watchdog = threading.Thread(target=force_cleanup, daemon=True)
        watchdog.start()
        cleanup = [rt.close, rt.stop_profile]
        if sul is not None and hasattr(sul, "tcp"):
            cleanup.append(sul.tcp.close)
        cleanup.extend([lease.close, display.close])
        for close in cleanup:
            try:
                close()
            except Exception:
                logging.exception("Cleanup failed")
                status, reason, code = "failed", "cleanup_failed", 1
        try:
            rt.set(stage="Finished")
            records.close(status, reason)
        except Exception as error:
            print(f"Could not finalize run records: {error}", file=sys.stderr)
            status, reason, code = "failed", "record_write_failed", 1
        finally:
            cleanup_done.set()
            for sig, handler in original_signals.items():
                signal.signal(sig, handler)
        print(
            f"Hydra {status}: {reason}\nResults: {path}\nVerification: {rt.snapshot().get('verification', 'not_tested')}"
        )
    return code


def doctor(args):
    import importlib.util
    from importlib.metadata import version, PackageNotFoundError

    config = configuration(args.config)
    checks = []
    for distribution, module in (
        ("pcapy-ng", "pcapy"),
        ("dpkt", "dpkt"),
        ("scapy", "scapy"),
        ("rich", "rich"),
        ("psutil", "psutil"),
    ):
        try:
            installed = version(distribution)
            checks.append(
                (
                    f"{distribution} {installed} (import {module})",
                    importlib.util.find_spec(module) is not None,
                )
            )
        except PackageNotFoundError:
            checks.append((f"{distribution} (import {module})", False))
    import psutil

    interfaces = psutil.net_if_addrs()
    checks.append(("interface", config["interface"] in interfaces))
    checks.append(
        (
            "learner address",
            any(
                a.address == config["my_ip"]
                for a in interfaces.get(config["interface"], [])
            ),
        )
    )
    checks.append(("root privileges", os.geteuid() == 0))
    checks.append(
        (
            "fork multiprocessing start method",
            multiprocessing.get_start_method() == "fork",
        )
    )
    checks.append(("iptables", shutil.which("iptables") is not None))
    try:
        output = Path(args.output).resolve()
        output.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryFile(dir=output) as stream:
            stream.write(b"hydra write check")
            stream.flush()
        checks.append(("output directory writable", True))
        print(f"Output: {output}; free bytes: {shutil.disk_usage(output).free}")
    except OSError:
        checks.append(("output directory writable", False))
    if shutil.which("iptables") and os.geteuid() == 0:
        rule = subprocess.run(
            ["iptables", "-C", *NetworkLease().rule], capture_output=True
        )
        print(
            "Outgoing RST DROP rule: "
            + (
                "present"
                if rule.returncode == 0
                else "will be added when learning starts"
            )
        )
    for name in (
        "tcp_retries1",
        "tcp_retries2",
        "tcp_synack_retries",
        "tcp_syncookies",
        "tcp_fin_timeout",
    ):
        path = Path("/proc/sys/net/ipv4") / name
        if path.exists():
            print(f"Local kernel {name}: {path.read_text().strip()}")
    # Opening the SUT control channel would consume its single client connection.
    # Inspect reachability through routing without consuming a control session.
    print(
        "Control connection is checked when learning starts; doctor does not consume a SUT session."
    )
    for label, ok in checks:
        print(f"{'OK' if ok else 'FAIL'}: {label}")
    for tool in ("dot",):
        print(f"Optional {tool}: {shutil.which(tool) or 'not found'}")
    return 0 if all(ok for _, ok in checks) else 2


def report(args):
    root = Path(args.path)
    summary = json.loads((root / "summary.json").read_text())
    if summary["status"] == "running":
        summary["notice"] = (
            "No final status recorded; inspect the last heartbeat and process before assuming completion."
        )
    print(json.dumps(summary, indent=2))
    return 0


def watch(args):
    """Display saved telemetry without restarting or signalling a learning run."""
    from hydra.ui import Display, SnapshotRuntime

    path = Path(args.path).resolve()
    view = Display(SnapshotRuntime(path), path, args.ui)
    try:
        view.start()
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        return 0
    finally:
        view.close()


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="hydra", description="TCP model learning and input exploration"
    )
    parser.add_argument("--version", action="version", version="Hydra 0.1.0")
    commands = parser.add_subparsers(dest="command", required=True)
    p = commands.add_parser("learn")
    p.add_argument("--config")
    p.add_argument("--duration", type=duration)
    p.add_argument("--output", default="runs")
    p.add_argument("--seed", type=int)
    p.add_argument(
        "--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"], default="INFO"
    )
    p.add_argument("--ui", choices=["auto", "plain", "off"], default="auto")
    p.add_argument("--profile", choices=["off", "cpu"], default="off")
    p.set_defaults(function=run)
    p = commands.add_parser("doctor")
    p.add_argument("--config")
    p.add_argument("--output", default="runs")
    p.set_defaults(function=doctor)
    p = commands.add_parser("report")
    p.add_argument("path")
    p.set_defaults(function=report)
    p = commands.add_parser("watch", help="Observe a run without controlling it")
    p.add_argument("path")
    p.add_argument("--ui", choices=["auto", "plain"], default="auto")
    p.set_defaults(function=watch)
    args = parser.parse_args(argv)
    try:
        return args.function(args)
    except (OSError, ValueError, RuntimeError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 2
