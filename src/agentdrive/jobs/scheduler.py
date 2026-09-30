"""Run the maintenance jobs on their schedule, for a self-hosted install.

    python -m agentdrive.jobs.scheduler            # run forever
    python -m agentdrive.jobs.scheduler --list     # print the schedule
    python -m agentdrive.jobs.scheduler --run NAME # run one job now; exit with its status
    python -m agentdrive.jobs.scheduler --status   # print the persisted state

In `compose.selfhost.yml` this runs INSIDE the API container, started by the
process supervisor when `SCHEDULER_ENABLED=true` — one environment and one
volume for the API and the jobs that delete what it wrote, so no override can
point them at different databases or stores. The hosted product never sets
it: its platform scheduler runs the same jobs.

The schedule is `agentdrive.jobs.schedule.SCHEDULE`. Each due job runs as a
child process, `python -m <module> <args>`, in its own process group,
inheriting this process's environment. Jobs run one at a time. A job whose
due time passed while another ran, or while the scheduler was down (the last
handled minute persists in `SCHEDULER_STATE_FILE`), runs once when it can —
never once per missed minute. A job that outlives its timeout is killed with
its whole process group. A failing job is logged at ERROR and the scheduler
carries on.

Deliberately standard library only, and it never loads the application
settings: each job validates its own configuration and fails loudly.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from agentdrive.jobs.schedule import SCHEDULE, ScheduledJob

log = logging.getLogger("agentdrive.jobs.scheduler")

# How far back a catch-up looks. A job due at least monthly is always inside
# it, and the scan stays small after an arbitrarily long outage.
CATCH_UP_WINDOW = timedelta(days=32)
# Grace a child's process group gets between SIGTERM and SIGKILL.
KILL_GRACE_S = 10
STATE_FILE_ENV = "SCHEDULER_STATE_FILE"


class CronError(ValueError):
    pass


_FIELDS = (("minute", 0, 59), ("hour", 0, 23), ("day", 1, 31), ("month", 1, 12), ("weekday", 0, 7))


def _parse_field(text: str, low: int, high: int) -> frozenset[int]:
    values: set[int] = set()
    for part in text.split(","):
        body, slash, step_text = part.partition("/")
        try:
            step = int(step_text) if slash else 1
            if body == "*":
                start, end = low, high
            elif "-" in body:
                a, b = body.split("-", 1)
                start, end = int(a), int(b)
            else:
                start = int(body)
                # `N/S` means N, N+S, … up to the field's maximum, as in cron.
                end = high if slash else start
        except ValueError as exc:
            raise CronError(f"{text!r} is not a cron field") from exc
        if step < 1 or start > end or start < low or end > high:
            raise CronError(f"{text!r} is outside {low}-{high}")
        values.update(range(start, end + 1, step))
    return frozenset(values)


@dataclass(frozen=True)
class Cron:
    """Five-field cron: numbers, `*`, ranges, lists and steps; Sunday is 0 or
    7. When both day-of-month and day-of-week are restricted (neither field
    starts with `*`), either may match, as in classic cron."""

    minute: frozenset[int]
    hour: frozenset[int]
    day: frozenset[int]
    month: frozenset[int]
    weekday: frozenset[int]
    day_restricted: bool
    weekday_restricted: bool

    @classmethod
    def parse(cls, expr: str) -> Cron:
        parts = expr.split()
        if len(parts) != 5:
            raise CronError(f"{expr!r} must have five fields")
        fields = [_parse_field(p, lo, hi) for p, (_, lo, hi) in zip(parts, _FIELDS, strict=True)]
        weekday = frozenset(0 if d == 7 else d for d in fields[4])
        return cls(fields[0], fields[1], fields[2], fields[3], weekday,
                   not parts[2].startswith("*"), not parts[4].startswith("*"))

    def matches(self, when: datetime) -> bool:
        if when.minute not in self.minute or when.hour not in self.hour:
            return False
        if when.month not in self.month:
            return False
        day_ok = when.day in self.day
        weekday_ok = (when.isoweekday() % 7) in self.weekday
        if self.day_restricted and self.weekday_restricted:
            return day_ok or weekday_ok
        return day_ok and weekday_ok


def _minute(when: datetime) -> datetime:
    return when.astimezone(UTC).replace(second=0, microsecond=0)


def latest_due(cron: Cron, after: datetime, until: datetime) -> datetime | None:
    """The last minute in (after, until] the cron matches, if any."""
    tick = _minute(until)
    floor = max(_minute(after), tick - CATCH_UP_WINDOW)
    while tick > floor:
        if cron.matches(tick):
            return tick
        tick -= timedelta(minutes=1)
    return None


def due_between(
    jobs: Sequence[ScheduledJob],
    after: datetime,
    until: datetime,
    started: dict[str, datetime] | None = None,
) -> list[ScheduledJob]:
    """Jobs due in (after, until], each once, in schedule order — skipping a
    job that already STARTED at or after its latest due minute (it ran late,
    behind another job, and that run covered this due time)."""
    started = started or {}
    due = []
    for job in jobs:
        when = latest_due(Cron.parse(job.cron), after, until)
        if when is None:
            continue
        ran = started.get(job.name)
        if ran is not None and ran >= when:
            continue
        due.append(job)
    return due


def module_command(job: ScheduledJob) -> list[str]:
    return [sys.executable, "-m", job.module, *job.args]


def display_command(job: ScheduledJob) -> str:
    """The command as an operator would type it in their own scheduler."""
    return " ".join(["python", "-m", job.module, *job.args])


# --- persisted state ------------------------------------------------------


@dataclass
class State:
    """The last minute the loop handled, and when each job last started."""

    last_tick: datetime | None = None
    started: dict[str, datetime] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path | None) -> State:
        if path is None or not path.exists():
            return cls()
        try:
            raw = json.loads(path.read_text())
            return cls(
                datetime.fromisoformat(raw["last_tick"]) if raw.get("last_tick") else None,
                {k: datetime.fromisoformat(v) for k, v in raw.get("started", {}).items()},
            )
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            log.warning("at=scheduler.state_unreadable path=%s; starting fresh", path)
            return cls()

    def save(self, path: Path | None) -> None:
        if path is None:
            return
        payload = {
            "last_tick": self.last_tick.isoformat() if self.last_tick else None,
            "started": {k: v.isoformat() for k, v in self.started.items()},
        }
        tmp = path.with_name(path.name + ".tmp")
        try:
            tmp.write_text(json.dumps(payload))
            os.replace(tmp, path)
        except OSError:
            log.warning("at=scheduler.state_unwritable path=%s", path)


# --- running jobs ---------------------------------------------------------


@dataclass(frozen=True)
class RunResult:
    name: str
    exit_code: int
    duration_s: float
    timed_out: bool


def _exit_status(code: int) -> int:
    """A signal death as a shell would report it (128 + signal)."""
    return 128 - code if code < 0 else code


class Runner:
    """Runs jobs one at a time, each in its own process group."""

    def __init__(self, command_for: Callable[[ScheduledJob], list[str]] = module_command):
        self._command_for = command_for
        self._child: subprocess.Popen[bytes] | None = None
        self._stopping = threading.Event()
        self._stop_requested_at: float | None = None

    @property
    def stopping(self) -> bool:
        return self._stopping.is_set()

    def stop(self) -> None:
        """Ask the current job to finish and run nothing after it."""
        self._stop_requested_at = time.monotonic()
        self._stopping.set()
        self._signal_child(signal.SIGTERM)

    def _signal_child(self, sig: int) -> None:
        child = self._child
        if child is None or child.poll() is not None:
            return
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(child.pid, sig)

    def run_all(self, jobs: Iterable[ScheduledJob]) -> list[RunResult]:
        results = []
        for job in jobs:
            if self.stopping:
                break
            results.append(self.run(job))
        return results

    def run(self, job: ScheduledJob) -> RunResult:
        log.info("at=scheduler.job_started name=%s", job.name)
        started = time.monotonic()
        timed_out = False
        self._child = subprocess.Popen(self._command_for(job), start_new_session=True)
        try:
            if self.stopping:  # a stop that landed between the check and the start
                self._signal_child(signal.SIGTERM)
            term_sent_at: float | None = None
            while True:
                try:
                    code = self._child.wait(timeout=0.2)
                    break
                except subprocess.TimeoutExpired:
                    pass
                now = time.monotonic()
                if term_sent_at is None and now - started >= job.timeout_s:
                    timed_out = True
                    term_sent_at = now
                    self._signal_child(signal.SIGTERM)
                if self.stopping and term_sent_at is None:
                    term_sent_at = self._stop_requested_at or now
                if term_sent_at is not None and now - term_sent_at >= KILL_GRACE_S:
                    self._signal_child(signal.SIGKILL)
            # Take down anything the job left behind in its group.
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(self._child.pid, signal.SIGKILL)
        finally:
            self._child = None
        result = RunResult(job.name, _exit_status(code), time.monotonic() - started, timed_out)
        level = logging.INFO if result.exit_code == 0 else logging.ERROR
        log.log(
            level,
            "at=scheduler.job_finished name=%s exit=%s duration_s=%.1f timed_out=%s",
            result.name, result.exit_code, result.duration_s, result.timed_out,
        )
        return result


def serve(
    runner: Runner,
    jobs: Sequence[ScheduledJob] = SCHEDULE,
    *,
    state_path: Path | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    sleep: Callable[[float], None] = time.sleep,
) -> int:
    for job in jobs:
        log.info("at=scheduler.scheduled name=%s cron=%r command=%r",
                 job.name, job.cron, display_command(job))
    state = State.load(state_path)
    current = _minute(now())
    # Resume from the persisted minute, so a job due while the scheduler was
    # down runs once now; with no state (a first start), from this minute.
    last = state.last_tick if state.last_tick and state.last_tick <= current else current
    last = max(last, current - CATCH_UP_WINDOW)
    first = True
    while not runner.stopping:
        if not first:
            next_tick = last + timedelta(minutes=1)
            while not runner.stopping and now() < next_tick:
                sleep(min(1.0, max(0.0, (next_tick - now()).total_seconds())))
            if runner.stopping:
                break
        first = False
        tick = _minute(now())
        if tick < last:
            # The clock stepped backwards: replay nothing, resume from here.
            log.warning("at=scheduler.clock_stepped_back from=%s to=%s", last, tick)
            last = tick
            state.last_tick = tick
            state.save(state_path)
            continue
        due = due_between(jobs, last, tick, state.started)
        for job in due:
            if runner.stopping:
                break
            state.started[job.name] = now()
            state.save(state_path)
            runner.run(job)
        else:
            # Only mark the batch handled after every due job started. If
            # shutdown interrupts it, retain the old checkpoint so a restart
            # catches up pending jobs; started deduplicates the earlier ones.
            last = tick
            state.last_tick = tick
            state.save(state_path)
    log.info("at=scheduler.stopped")
    return 0


def _state_path() -> Path | None:
    value = os.environ.get(STATE_FILE_ENV, "").strip()
    return Path(value) if value else None


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    )
    parser = argparse.ArgumentParser(
        prog="agentdrive.jobs.scheduler", description=__doc__.split("\n\n")[0]
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--list", action="store_true", help="print the schedule and exit")
    group.add_argument("--run", metavar="NAME", help="run one job now and exit with its status")
    group.add_argument("--status", action="store_true", help="print the persisted state")
    args = parser.parse_args(argv)

    if args.list:
        for job in SCHEDULE:
            print(f"{job.name:16} {job.cron:14} {display_command(job)}")
            if job.description:
                print(f"{'':16} {job.description}")
        return 0

    if args.status:
        path = _state_path()
        if path is None:
            print(f"{STATE_FILE_ENV} is not set; no state is kept", file=sys.stderr)
            return 1
        state = State.load(path)
        print(json.dumps({
            "last_tick": state.last_tick.isoformat() if state.last_tick else None,
            "started": {k: v.isoformat() for k, v in sorted(state.started.items())},
        }, indent=2))
        return 0 if state.last_tick else 1

    runner = Runner()
    def _stop(signum: int, _frame: object) -> None:
        log.info("at=scheduler.stopping signal=%s", signum)
        runner.stop()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    if args.run:
        job = next((j for j in SCHEDULE if j.name == args.run), None)
        if job is None:
            print(f"no job named {args.run!r}; see --list", file=sys.stderr)
            return 2
        return runner.run(job).exit_code

    return serve(runner, state_path=_state_path())


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
