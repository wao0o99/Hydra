"""Run metadata and user-requested stopping; query policy lives in the learner."""

import json
import threading
import time
from collections import deque
from pathlib import Path


class RunStopped(Exception):
    """A cooperative stop with an explicit reason."""


class Runtime:
    def __init__(self, duration=None, profile_dir=None):
        self.started = time.monotonic()
        self.deadline = self.started + duration if duration else None
        self.stop = threading.Event()
        self.reason = None
        self.lock = threading.RLock()
        self.values = {}
        self.profile_dir = Path(profile_dir) if profile_dir else None
        self.event_sink = None
        self.latencies = deque(maxlen=10000)
        self.stage_started = self.started
        self.recent_events = deque(maxlen=50)
        self.recent_problems = deque(maxlen=20)
        self.profiler = None

    def check(self):
        if self.deadline and time.monotonic() >= self.deadline:
            self.cancel("time_budget")
        if self.stop.is_set():
            raise RunStopped(self.reason or "user_interrupt")

    def cancel(self, reason):
        with self.lock:
            if not self.reason:
                self.reason = reason
            self.stop.set()

    def set(self, **values):
        with self.lock:
            stage = values.get("stage")
            if stage and stage != self.values.get("stage"):
                self.stage_started = time.monotonic()
            if stage and stage not in ("Stopping", "Finished"):
                self.values["last_learning_stage"] = stage
            self.values.update(values)

    def add(self, name, count=1):
        with self.lock:
            self.values[name] = self.values.get(name, 0) + count
            if name.startswith(("mq_", "eq_")):
                total = "lifetime_" + name
                self.values[total] = self.values.get(total, 0) + count

    def snapshot(self):
        with self.lock:
            snapshot = dict(
                self.values, elapsed_seconds=time.monotonic() - self.started
            )
            if self.latencies:
                samples = sorted(self.latencies)
                for label, quantile in (("p50", 0.50), ("p95", 0.95), ("p99", 0.99)):
                    snapshot["query_latency_" + label + "_seconds"] = samples[
                        min(len(samples) - 1, int((len(samples) - 1) * quantile))
                    ]
            snapshot["stage_seconds"] = time.monotonic() - self.stage_started
            snapshot["recent_events"] = list(self.recent_events)
            snapshot["recent_problems"] = [dict(item) for item in self.recent_problems]
            return snapshot

    def record_log(self, level, message, component="hydra", category=None):
        with self.lock:
            entry = dict(
                time=time.strftime("%H:%M:%S"),
                level=level,
                message=message[:2000],
                component=component,
                count=1,
                category=category or message[:2000],
            )
            if level in ("WARNING", "ERROR", "CRITICAL"):
                self.values["last_problem"] = message[:2000]
                existing = next(
                    (
                        item
                        for item in self.recent_problems
                        if item["category"] == entry["category"]
                        and item["component"] == component
                    ),
                    None,
                )
                if existing:
                    entry["count"] = existing["count"] + 1
                    self.recent_problems.remove(existing)
                self.recent_problems.append(entry)
            else:
                self.recent_events.append(entry)

    def event(self, event, **fields):
        if event in (
            "hypothesis_generated",
            "counterexample_found",
            "alphabet_expanded",
        ):
            self.set(**{event + "_at": time.monotonic() - self.started})
        if self.event_sink:
            try:
                self.event_sink(event, fields)
            except Exception as error:
                self.record_log(
                    "ERROR",
                    f"Observation could not be recorded: {error}",
                    "hydra.records",
                )

    def record_latency(self, seconds):
        with self.lock:
            self.latencies.append(seconds)

    def start_profile(self):
        if self.profile_dir:
            import yappi

            self.profile_dir.mkdir(parents=True, exist_ok=True)
            yappi.clear_stats()
            yappi.set_clock_type("cpu")
            yappi.start(profile_threads=True)
            self.profiler = yappi

    def stop_profile(self):
        if self.profiler is None:
            return
        profiler, self.profiler = self.profiler, None
        profiler.stop()
        threads = []
        try:
            for thread in profiler.get_thread_stats():
                name = (
                    "coordinator"
                    if thread.name == "_MainThread"
                    else f"worker-{thread.id}"
                )
                path = self.profile_dir / (name + ".pstats")
                profiler.get_func_stats(filter={"ctx_id": thread.id}).save(
                    str(path), type="pstat"
                )
                threads.append(
                    dict(
                        id=thread.id,
                        name=thread.name,
                        cpu_seconds=thread.ttot,
                        file=path.name,
                    )
                )
            (self.profile_dir / "threads.json").write_text(
                json.dumps(
                    dict(schema_version=1, clock="thread_cpu", threads=threads),
                    indent=2,
                )
                + "\n"
            )
        finally:
            profiler.clear_stats()

    def close(self):
        # Only invoked after the coordinator has stopped scheduling queries.
        for thread in threading.enumerate():
            if thread.name.startswith("hydra-query-"):
                thread.join()


_current = Runtime()


def current():
    return _current


def install(runtime):
    global _current
    _current = runtime


def append_json(path, record):
    with open(path, "a", encoding="utf-8") as stream:
        stream.write(json.dumps(dict(schema_version=1, **record)) + "\n")
