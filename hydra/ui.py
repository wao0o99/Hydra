"""Terminal dashboard for learning progress and diagnostics."""

import os
import sys
import threading
import json
import time
from datetime import datetime
from pathlib import Path
from rich import box
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.progress_bar import ProgressBar
from rich.rule import Rule


def duration(seconds):
    seconds = max(0, int(seconds))
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def number(value):
    return f"{value or 0:,}"


class SnapshotRuntime:
    """Read existing run records without importing or controlling the learner."""

    read_only = True

    def __init__(self, path):
        self.path = Path(path)
        self.saved = json.loads((self.path / "summary.json").read_text())
        manifest = json.loads((self.path / "manifest.json").read_text())
        self.deadline = (
            True
            if any(
                str(arg).startswith("--duration") for arg in manifest.get("command", [])
            )
            else None
        )

    def snapshot(self):
        unavailable = False
        try:
            self.saved = json.loads((self.path / "summary.json").read_text())
        except (OSError, ValueError):
            unavailable = True
        result = dict(self.saved)
        age = max(
            0, time.time() - datetime.fromisoformat(result["updated_at"]).timestamp()
        )
        if result.get("status") == "running":
            if not unavailable and age <= 15:
                result["elapsed_seconds"] += age
                result["stage_seconds"] = result.get("stage_seconds", 0) + age
            else:
                result["stage"] = result.get("stage", "Unknown") + " [STALE SNAPSHOT]"
        return result


class Display:
    def __init__(self, runtime, path, mode="auto"):
        self.runtime = runtime
        self.path = path
        self.console = Console(no_color="NO_COLOR" in os.environ)
        self.interactive = (
            mode == "auto" and sys.stdout.isatty() and os.environ.get("TERM") != "dumb"
        )
        self.mode = mode
        self.stop = threading.Event()
        self.thread = None

    @staticmethod
    def panel(title, rows, color="cyan"):
        table = Table.grid(expand=True, padding=(0, 1))
        table.add_column(style="dim", width=23, overflow="ellipsis", no_wrap=True)
        table.add_column(ratio=3, overflow="ellipsis")
        for key, value in rows:
            table.add_row(Text(key, style="dim"), Text(str(value), style="white"))
        return Group(Text(title.upper(), style="bold cyan"), Text(""), table)

    def render(self):
        s = self.runtime.snapshot()
        elapsed = s["elapsed_seconds"]
        n = lambda key: number(s.get(key, 0))
        rate = lambda key: f"{s.get(key + '_rate', 0):,.1f}/s"

        def age(key):
            stamp = s.get(key + "_at")
            return (
                duration(elapsed - stamp) + " ago" if stamp is not None else "not yet"
            )

        hits, misses = s.get("cache_hits", 0), s.get("cache_misses", 0)
        inflight = sum(
            max(
                0,
                s.get(p + "_scheduled", 0)
                - s.get(p + "_completed", 0)
                - s.get(p + "_failed", 0)
                - s.get(p + "_cancelled", 0),
            )
            for p in ("mq", "eq")
        )
        status = s.get("verification", "not_tested")
        color = (
            "red"
            if status == "failed"
            else "cyan"
            if status in ("in_progress", "incomplete")
            else "green"
        )
        heading = Text("HYDRA  ", style="bold cyan")
        heading.append("/  tcp state learner  ", style="dim")
        heading.append(
            f"  TOTAL ELAPSED {duration(elapsed)}",
            style="bold cyan",
        )
        heading.append(
            f"  |  {'NO TIME LIMIT' if self.runtime.deadline is None else 'BUDGETED RUN'}",
            style="white",
        )
        heading.append("\n" + s.get("stage", "Initializing"), style="bold white")
        heading.append(
            f"  |  STAGE ELAPSED {duration(s.get('stage_seconds', 0))}  |  {status}",
            style=color,
        )
        timing = self.panel(
            "Learning & timing",
            [
                ("Total elapsed", duration(elapsed)),
                ("Round / hypothesis", f"{n('rounds')} / #{n('hypothesis')}"),
                ("States / alphabet", f"{n('states')} / {n('alphabet_size')}"),
                ("Counterexamples", n("counterexamples")),
                ("Last hypothesis", age("hypothesis_generated")),
                ("Last counterexample", age("counterexample_found")),
                ("Inputs evaluated", n("inputs_evaluated")),
                (
                    "Pre-generated used",
                    f"{n('pre_generated_consumed')} / {n('pre_generated_total')}",
                ),
                ("Last input expansion", age("alphabet_expanded")),
            ],
        )
        execution = self.panel(
            "Execution & cache",
            [
                ("In flight / capacity", f"{inflight} / {n('worker_capacity')}"),
                (
                    "SUL executions",
                    f"{n('query_executions')}  |  {rate('query_executions')}",
                ),
                ("Input steps", f"{n('input_steps')}  |  {rate('input_steps')}"),
                ("Cache hits / misses", f"{number(hits)} / {number(misses)}"),
                ("Cache hit ratio", f"{100 * hits / max(1, hits + misses):.1f}%"),
                ("Consistency retries", n("consistency_retries")),
                (
                    "Lifetime MQ / EQ done",
                    f"{n('lifetime_mq_completed')} / {n('lifetime_eq_completed')}",
                ),
            ],
            "blue",
        )
        queries = Table(expand=True, box=None, padding=(0, 1), header_style="dim")
        for name in (
            "Task",
            "Done / planned",
            "Progress",
            "%",
            "In flight",
            "Failed",
            "Cancelled",
        ):
            queries.add_column(name, overflow="ellipsis")
        for prefix in ("mq", "eq"):
            done, total = s.get(prefix + "_completed", 0), s.get(prefix + "_total")
            active = max(
                0,
                s.get(prefix + "_scheduled", 0)
                - done
                - s.get(prefix + "_failed", 0)
                - s.get(prefix + "_cancelled", 0),
            )
            bar = ProgressBar(
                total=total or 1,
                completed=done if total else 0,
                width=None,
                complete_style="cyan",
                finished_style="cyan",
                style="grey23",
            )
            queries.add_row(
                prefix.upper(),
                f"{number(done)} / {number(total) if total is not None else '?'}",
                bar,
                f"{100 * done / total:.1f}" if total else "?",
                str(active),
                n(prefix + "_failed"),
                n(prefix + "_cancelled"),
            )
        capture = self.panel(
            "Capture & queues",
            [
                (
                    "Active-port delivery",
                    f"{s.get('packet_delivery_rate', 0):.1f} pkt/s",
                ),
                ("Consistency fallbacks", n("consistency_fallbacks")),
                ("Capture / drop totals", "Not instrumented"),
                (
                    "Packet / result queue",
                    f"{n('packet_queue_depth')} / {n('result_queue_depth')}",
                ),
                (
                    "Processing / end-to-end",
                    f"{s.get('packet_processing_mean_seconds', 0) * 1000:.1f} / {s.get('packet_latency_mean_seconds', 0) * 1000:.1f} ms",
                ),
            ],
        )
        resources = self.panel(
            "Coordinator resources & task latency",
            [
                (
                    "CPU / resident memory",
                    f"{s.get('cpu_percent', 0):.1f}% / {s.get('rss_bytes', 0) / 1048576:.1f} MiB",
                ),
                (
                    "Threads / open FDs",
                    f"{n('process_threads')} / {n('file_descriptors')}",
                ),
                (
                    "Task latency p50 / p95",
                    f"{s.get('query_latency_p50_seconds', 0):.3f} / {s.get('query_latency_p95_seconds', 0):.3f} s",
                ),
                ("Task latency p99", f"{s.get('query_latency_p99_seconds', 0):.3f} s"),
                ("Warnings / errors", f"{n('warnings')} / {n('errors')}"),
            ],
            color,
        )

        def pair(left, right):
            if self.console.width < 85:
                return Group(left, right)
            grid = Table.grid(expand=True, padding=(0, 2))
            grid.add_column(ratio=1)
            grid.add_column(ratio=1)
            grid.add_row(left, right)
            return grid

        activity = Table.grid(expand=True, padding=(0, 1))
        activity.add_column(width=8, style="dim")
        activity.add_column(width=7)
        activity.add_column(ratio=1, overflow="ellipsis", no_wrap=True)
        limit = max(3, min(6, self.console.height - 40))
        events = s.get("recent_events", [])[-limit:]
        for event in reversed(events):
            style = {"WARNING": "yellow", "ERROR": "red", "CRITICAL": "bold red"}.get(
                event["level"], "green"
            )
            activity.add_row(
                event["time"], Text(event["level"], style=style), Text(event["message"])
            )
        if not events:
            activity.add_row("--:--:--", "INFO", "Waiting for learning events")
        problems = []
        problem_count = 2 if self.console.height >= 64 else 1
        for item in reversed(s.get("recent_problems", [])[-problem_count:]):
            message = Text(
                f"{item['time']} {item['level']}  {item['component']}  (x{item['count']:,})\n",
                style="bold yellow" if item["level"] == "WARNING" else "bold red",
            )
            message.append(item["message"], style="white")
            if item.get("detail"):
                message.append("\n" + item["detail"], style="yellow")
            if "Query outputs differ" in item["message"]:
                message.append(
                    "\nMeaning: observations differ; attempts 1-5 retry, attempt 6 uses the last result. Details: logs/events.jsonl.",
                    style="dim",
                )
            problems.append(message)
        if not problems:
            problems.append(Text("No warnings or errors in this run.", style="green"))
        problem_panel = Group(
            Rule(
                "WARNINGS & ERRORS",
                align="left",
                style="yellow" if s.get("recent_problems") else "grey35",
            ),
            *problems,
        )
        footer = Text("Results: ", style="dim")
        footer.append(str(self.path), style="cyan")
        footer.append(
            (
                "\nCtrl+C: close viewer (learning continues)"
                if getattr(self.runtime, "read_only", False)
                else "\nCtrl+C: stop and save"
            )
            + " | MQ/EQ: latest batch/oracle | details: logs/hydra.log",
            style="dim",
        )
        content = Group(
            heading,
            Text(""),
            Rule(style="grey35"),
            Text(""),
            pair(timing, execution),
            Text(""),
            Rule(
                "QUERY PROGRESS  /  " + s.get("oracle", "pending"),
                align="left",
                style="grey35",
            ),
            queries,
            Text(""),
            Rule(style="grey35"),
            Text(""),
            pair(capture, resources),
            Text(""),
            Rule("RECENT ACTIVITY", align="left", style="grey35"),
            activity,
            Text(""),
            problem_panel,
            Text(""),
            footer,
        )
        return Panel(
            content,
            box=box.ROUNDED,
            border_style="grey50",
            padding=(1, 2),
            subtitle=" LIVE  ·  refresh 2 Hz ",
            subtitle_align="right",
        )

    def start(self):
        if self.mode == "off":
            return
        self.thread = threading.Thread(
            target=self._loop, daemon=True, name="hydra-display"
        )
        self.thread.start()

    def _loop(self):
        if self.interactive:
            with Live(
                self.render(),
                console=self.console,
                refresh_per_second=2,
                screen=True,
                redirect_stdout=False,
                redirect_stderr=False,
            ) as live:
                while not self.stop.wait(0.5):
                    live.update(self.render())
        else:
            while not self.stop.is_set():
                s = self.runtime.snapshot()
                self.console.print(
                    f"Hydra | TOTAL ELAPSED {duration(s['elapsed_seconds'])} | "
                    f"STAGE ELAPSED {duration(s.get('stage_seconds', 0))} | "
                    f"{s.get('stage', 'Initializing')} | {s.get('states', 0)} states | "
                    f"{s.get('alphabet_size', 0)} inputs | EQ completed={s.get('eq_completed', 0)} "
                    f"failed={s.get('eq_failed', 0)} | steps={s.get('input_steps', 0)} | "
                    f"warnings={s.get('warnings', 0)} errors={s.get('errors', 0)}",
                    markup=False,
                    highlight=False,
                )
                self.stop.wait(10)

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=2)
