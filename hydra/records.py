"""Persistent run events, metrics, and atomic summaries."""

import csv
import hashlib
import json
import logging
import logging.handlers
import multiprocessing
import os
import platform
import sys
import threading
import time
from datetime import datetime, timezone
from importlib.metadata import version, PackageNotFoundError
from pathlib import Path

import psutil


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, indent=2, default=str) + "\n", encoding="utf-8"
    )
    os.replace(temporary, path)


class JsonHandler(logging.Handler):
    def __init__(self, path):
        super().__init__()
        self.stream = open(path, "a", encoding="utf-8", buffering=1)
        self.warnings = 0
        self.errors = 0

    def emit(self, record):
        self.warnings += record.levelno == logging.WARNING
        self.errors += record.levelno >= logging.ERROR
        self.stream.write(
            json.dumps(
                dict(
                    schema_version=1,
                    timestamp=utcnow(),
                    level=record.levelname,
                    component=record.name,
                    message=record.getMessage(),
                    event=getattr(record, "event_name", None),
                    fields=getattr(record, "event_fields", None),
                )
            )
            + "\n"
        )

    def close(self):
        self.stream.close()
        super().close()


class CheckedFileHandler(logging.handlers.RotatingFileHandler):
    """Propagate write failures to the run instead of hiding logging errors."""

    def handleError(self, record):
        raise


class CheckedQueueListener(logging.handlers.QueueListener):
    def __init__(self, queue, runtime, *handlers, event_callback=None):
        super().__init__(queue, *handlers)
        self.runtime = runtime
        self.event_callback = event_callback

    def handle(self, record):
        try:
            super().handle(record)
            if self.event_callback and getattr(record, "event_name", None):
                self.event_callback(record.event_name, record.event_fields)
            if getattr(record, "event_name", None) == "query_inconsistent":
                fields = record.event_fields
                outputs = fields.get("outputs", [])
                differences = []
                if outputs and all(isinstance(row, list) for row in outputs):
                    for index, values in enumerate(zip(*outputs), 1):
                        if len(set(map(str, values))) > 1:
                            differences.append(
                                f"step {index}: " + " / ".join(map(str, values))
                            )
                detail = "Three observations: " + (
                    "; ".join(differences) or repr(outputs)
                )
                with self.runtime.lock:
                    for problem in reversed(self.runtime.recent_problems):
                        expected = f"Query outputs differ on attempt {fields.get('attempt')}/6: {tuple(fields.get('word', []))!r}"
                        if problem["message"] == expected:
                            problem["detail"] = detail[:2000]
                            break
            if (
                record.levelno >= logging.INFO
                and getattr(record, "event_name", None) != "query_inconsistent"
            ):
                self.runtime.record_log(
                    record.levelname, record.getMessage(), record.name, str(record.msg)
                )
        except Exception as error:
            self.runtime.record_log(
                "ERROR", f"Observation recording failed: {error}", "hydra.records"
            )


class RunRecords:
    def __init__(self, root, runtime, config, mode, seed, argv, level="INFO"):
        self.root = Path(root)
        self.runtime = runtime
        self.stop = threading.Event()
        self.lock = threading.RLock()
        self.models = []
        self.phases = []
        self.last_phase = None
        self.phase_started = time.monotonic()
        self.last_saved = 0
        self.status = "running"
        self.root.mkdir(parents=True, exist_ok=False)
        for name in (
            "logs",
            "metrics",
            "profiles",
            "models",
            "counterexamples",
            "inputs",
            "captures",
        ):
            (self.root / name).mkdir()
        self.original_handlers = logging.getLogger().handlers[:]
        self.original_level = logging.getLogger().level
        try:
            self._initialize(config, mode, seed, argv, level)
        except Exception:
            self._release()
            raise

    def _initialize(self, config, mode, seed, argv, level):
        runtime = self.runtime
        self.queue = multiprocessing.Queue()
        self.text = CheckedFileHandler(
            self.root / "logs/hydra.log",
            maxBytes=20 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        formatter = logging.Formatter(
            "%(asctime)sZ %(levelname)s %(name)s %(processName)s/%(threadName)s %(message)s"
        )
        formatter.converter = time.gmtime
        self.text.setFormatter(formatter)
        self.events = JsonHandler(self.root / "logs/events.jsonl")
        self.listener = CheckedQueueListener(
            self.queue,
            runtime,
            self.text,
            self.events,
            event_callback=self._record_event,
        )
        self.original_handlers = logging.getLogger().handlers[:]
        self.original_level = logging.getLogger().level
        self.queue_handler = logging.handlers.QueueHandler(self.queue)
        packages = {}
        for package in (
            "scapy",
            "pcapy-ng",
            "dpkt",
            "pydot",
            "rich",
            "psutil",
            "yappi",
        ):
            try:
                packages[package] = version(package)
            except PackageNotFoundError:
                packages[package] = None
        source = Path(__file__).resolve().parents[1]
        digest = hashlib.sha256()
        paths = [
            path
            for package in ("hydra", "aalpy", "learn_tcp")
            for path in (source / package).rglob("*")
            if path.is_file() and path.suffix in (".py", ".txt")
        ]
        for path in sorted(paths):
            digest.update(str(path.relative_to(source)).encode())
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
        atomic_json(
            self.root / "manifest.json",
            dict(
                schema_version=1,
                run_id=self.root.name,
                started_at=utcnow(),
                mode=mode,
                seed=seed,
                config=config,
                command=argv,
                python=sys.version,
                platform=platform.platform(),
                packages=packages,
                source_sha256=digest.hexdigest(),
                pid=os.getpid(),
                profile=bool(runtime.profile_dir),
            ),
        )
        self.csv_stream = open(
            self.root / "metrics/samples.csv", "w", newline="", encoding="utf-8"
        )
        self.writer = csv.DictWriter(
            self.csv_stream,
            fieldnames=[
                "timestamp",
                "elapsed_seconds",
                "stage",
                "mq_scheduled",
                "mq_completed",
                "mq_failed",
                "eq_scheduled",
                "eq_completed",
                "eq_failed",
                "query_executions",
                "input_steps",
                "consistency_retries",
                "cache_hits",
                "cache_misses",
                "packet_queue_depth",
                "result_queue_depth",
                "packets_captured",
                "packets_processed",
                "packets_dropped",
                "query_latency_p50_seconds",
                "query_latency_p95_seconds",
                "query_latency_p99_seconds",
                "rss_bytes",
                "cpu_percent",
                "file_descriptors",
            ],
        )
        self.writer.writeheader()
        self.process = psutil.Process()
        self.thread = threading.Thread(
            target=self._sample, name="hydra-metrics", daemon=True
        )
        self.save()
        logging.getLogger().handlers = [self.queue_handler]
        logging.getLogger().setLevel(level)
        self.listener.start()
        runtime.event_sink = self.event
        self.thread.start()

    def event(self, event, fields):
        logging.getLogger("hydra.events").info(
            event, extra={"event_name": event, "event_fields": fields}
        )

    def _record_event(self, event, fields):
        if event not in (
            "hypothesis_generated",
            "eq_started",
            "eq_completed",
            "counterexample_found",
        ):
            return
        with self.lock:
            if event == "hypothesis_generated":
                self.models.append(
                    dict(fields, generated_at=utcnow(), verification="not_tested")
                )
            elif (
                event in ("eq_started", "eq_completed", "counterexample_found")
                and self.models
            ):
                self.models[-1]["verification"] = {
                    "eq_started": "in_progress",
                    "eq_completed": "passed_test_suite",
                    "counterexample_found": "counterexample_found",
                }[event]
                if event == "eq_completed" and fields.get("failed"):
                    self.models[-1]["verification"] = "incomplete"
                self.models[-1]["validation"] = fields
            atomic_json(
                self.root / "models/index.json",
                dict(schema_version=1, models=self.models),
            )
        self.save()

    def save(self, status=None, reason=None):
        with self.lock:
            if status:
                self.status = status
            snapshot = self.runtime.snapshot()
            atomic_json(
                self.root / "summary.json",
                dict(
                    schema_version=1,
                    run_id=self.root.name,
                    status=self.status,
                    reason=reason or self.runtime.reason,
                    updated_at=utcnow(),
                    **dict(
                        snapshot,
                        warnings=self.events.warnings,
                        errors=self.events.errors,
                    ),
                ),
            )
            atomic_json(
                self.root / "metrics/phases.json",
                dict(
                    schema_version=1,
                    phases=self.phases,
                    current_stage=self.last_phase,
                    current_elapsed=time.monotonic() - self.phase_started,
                ),
            )
            self.last_saved = time.monotonic()

    def _sample(self):
        previous = {}
        previous_time = time.monotonic()
        try:
            while not self.stop.is_set():
                self.runtime.set(
                    warnings=self.events.warnings, errors=self.events.errors
                )
                values = self.runtime.snapshot()
                stage = values.get("stage")
                if stage != self.last_phase:
                    if self.last_phase:
                        self.phases.append(
                            dict(
                                stage=self.last_phase,
                                seconds=time.monotonic() - self.phase_started,
                            )
                        )
                    self.last_phase = stage
                    self.phase_started = time.monotonic()
                    self.save()
                row = {key: values.get(key, 0) for key in self.writer.fieldnames}
                for key in ("packets_captured", "packets_processed", "packets_dropped"):
                    row[key] = values.get(key)
                row.update(
                    timestamp=utcnow(),
                    stage=stage or "Initializing",
                    rss_bytes=self.process.memory_info().rss,
                    cpu_percent=self.process.cpu_percent(),
                    file_descriptors=self.process.num_fds(),
                )
                now = time.monotonic()
                interval = max(now - previous_time, 0.001)
                rates = {
                    key + "_rate": max(0, values.get(key, 0) - previous.get(key, 0))
                    / interval
                    for key in (
                        "query_executions",
                        "input_steps",
                        "lifetime_mq_completed",
                        "lifetime_eq_completed",
                    )
                }
                self.runtime.set(
                    **rates,
                    rss_bytes=row["rss_bytes"],
                    cpu_percent=row["cpu_percent"],
                    file_descriptors=row["file_descriptors"],
                    process_threads=self.process.num_threads(),
                )
                previous, previous_time = values, now
                self.writer.writerow(row)
                self.csv_stream.flush()
                if time.monotonic() - self.last_saved >= 5:
                    self.save()
                self.stop.wait(1)
        except Exception as error:
            self.runtime.record_log(
                "ERROR", f"Observation recording failed: {error}", "hydra.records"
            )

    def _release(self):
        self.runtime.event_sink = None
        logging.getLogger().handlers = self.original_handlers
        logging.getLogger().setLevel(self.original_level)
        failure = None
        for name in ("csv_stream", "text", "events", "queue"):
            resource = getattr(self, name, None)
            if resource is not None:
                try:
                    resource.close()
                except Exception as error:
                    failure = failure or error
        if hasattr(self, "queue"):
            self.queue.join_thread()
        if failure:
            raise failure

    def close(self, status, reason):
        self.stop.set()
        self.thread.join(timeout=2)
        self.listener.stop()
        try:
            with self.lock:
                if self.models and self.models[-1]["verification"] == "in_progress":
                    self.models[-1]["verification"] = (
                        "failed" if status == "failed" else "incomplete"
                    )
                atomic_json(
                    self.root / "models/index.json",
                    dict(schema_version=1, models=self.models),
                )
            if self.runtime.snapshot().get("verification") == "in_progress":
                self.runtime.set(
                    verification="failed" if status == "failed" else "incomplete"
                )
            self.save(status, reason)
        finally:
            self._release()
