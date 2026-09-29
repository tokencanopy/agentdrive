"""The self-hosted job scheduler (`agentdrive.jobs.scheduler`).

A hosted deployment schedules the maintenance jobs with its platform's
scheduler; a self-hosted install runs this one inside its API container. These
tests pin the three things that decide whether an install ever reclaims
storage: the schedule itself, the cron matching, and the runner's behaviour
when jobs are due together, overlap, fail or hang. The runner is exercised
with real child processes (tiny Python one-liners), never mocks.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta

import pytest

from agentdrive.jobs import scheduler
from agentdrive.jobs.schedule import SCHEDULE, ScheduledJob
from agentdrive.jobs.scheduler import Cron, CronError, Runner, due_between


def _utc(*args: int) -> datetime:
    return datetime(*args, tzinfo=UTC)


# --- the schedule --------------------------------------------------------


def test_the_schedule_is_the_hosted_cadence():
    """The same jobs, arguments and times the hosted deployment runs, so a
    self-hosted install behaves like the product its tests describe."""
    rows = {(j.name, j.cron, j.module, tuple(j.args)) for j in SCHEDULE}
    assert rows == {
        ("gc-hourly", "0 * * * *", "agentdrive.jobs.gc", ("--sessions-only",)),
        ("gc-daily", "0 3 * * *", "agentdrive.jobs.gc", ()),
        ("gc-weekly", "0 4 * * 0", "agentdrive.jobs.gc", ("--orphan-sweep",)),
        ("usage-snapshot", "*/15 * * * *", "agentdrive.jobs.usage_snapshot", ("--no-notify",)),
    }


def test_every_job_has_a_unique_name_and_a_timeout_inside_an_hour():
    names = [j.name for j in SCHEDULE]
    assert len(names) == len(set(names))
    for job in SCHEDULE:
        Cron.parse(job.cron)  # parses
        assert 0 < job.timeout_s <= 3600, job.name


def test_the_gc_timeout_leaves_room_for_the_sweepers_own_deadline():
    """The sweeper stops itself at HARD_TIMEOUT_S; the scheduler's kill must
    come after that, or it interrupts a sweep that was about to finish."""
    from agentdrive.core.gc import GCSweeper

    for job in SCHEDULE:
        if job.module == "agentdrive.jobs.gc":
            assert job.timeout_s > GCSweeper.HARD_TIMEOUT_S


# --- cron -----------------------------------------------------------------


@pytest.mark.parametrize(
    "expr, when, expected",
    [
        ("0 * * * *", _utc(2026, 9, 29, 14, 0), True),
        ("0 * * * *", _utc(2026, 9, 29, 14, 1), False),
        ("0 3 * * *", _utc(2026, 9, 29, 3, 0), True),
        ("0 3 * * *", _utc(2026, 9, 29, 4, 0), False),
        # 2026-10-04 is a Sunday; cron's Sunday is 0 (and 7).
        ("0 4 * * 0", _utc(2026, 10, 4, 4, 0), True),
        ("0 4 * * 7", _utc(2026, 10, 4, 4, 0), True),
        ("0 4 * * 0", _utc(2026, 10, 5, 4, 0), False),
        ("*/15 * * * *", _utc(2026, 9, 29, 14, 45), True),
        ("*/15 * * * *", _utc(2026, 9, 29, 14, 50), False),
        ("5,35 1-3 * * *", _utc(2026, 9, 29, 2, 35), True),
        ("5,35 1-3 * * *", _utc(2026, 9, 29, 4, 35), False),
        ("0 0 1 * *", _utc(2026, 10, 1, 0, 0), True),
        ("0 0 * 10 *", _utc(2026, 9, 1, 0, 0), False),
    ],
)
def test_cron_matching(expr, when, expected):
    assert Cron.parse(expr).matches(when) is expected


@pytest.mark.parametrize(
    "expr",
    ["", "* * * *", "* * * * * *", "60 * * * *", "* 24 * * *", "*/0 * * * *", "a * * * *",
     "5-1 * * * *", "* * 0 * *", "* * * 13 *", "* * * * 8"],
)
def test_malformed_cron_is_refused(expr):
    with pytest.raises(CronError):
        Cron.parse(expr)


def test_when_both_day_fields_are_restricted_either_may_match():
    """Classic cron: day-of-month OR day-of-week once both are restricted."""
    cron = Cron.parse("0 0 13 * 5")  # the 13th, or any Friday
    assert cron.matches(_utc(2026, 10, 13, 0, 0))  # a Tuesday the 13th
    assert cron.matches(_utc(2026, 10, 2, 0, 0))  # a Friday the 2nd
    assert not cron.matches(_utc(2026, 10, 3, 0, 0))


# --- which jobs are due ---------------------------------------------------


def test_three_oclock_runs_the_hourly_the_daily_and_usage_in_schedule_order():
    due = due_between(SCHEDULE, _utc(2026, 9, 29, 2, 59), _utc(2026, 9, 29, 3, 0))
    assert [j.name for j in due] == ["gc-hourly", "gc-daily", "usage-snapshot"]


def test_minutes_missed_while_a_job_ran_are_caught_up_once_each():
    """A daily sweep that ran from 03:00 to 03:40 must not cost the 03:15 and
    03:30 usage passes their run — but it owes ONE usage pass, not two."""
    due = due_between(SCHEDULE, _utc(2026, 9, 29, 3, 0), _utc(2026, 9, 29, 3, 40))
    assert [j.name for j in due] == ["usage-snapshot"]


def test_nothing_is_due_retroactively_for_the_minute_already_handled():
    assert due_between(SCHEDULE, _utc(2026, 9, 29, 3, 0), _utc(2026, 9, 29, 3, 0)) == []


def test_a_long_outage_catches_up_each_job_once():
    """A host asleep over a weekend still runs each job exactly once."""
    due = due_between(SCHEDULE, _utc(2026, 10, 2, 0, 0), _utc(2026, 10, 5, 0, 0))
    assert sorted(j.name for j in due) == sorted(j.name for j in SCHEDULE)


def test_the_catch_up_window_is_bounded():
    """Days of missed minutes are scanned without iterating forever."""
    start = _utc(2026, 1, 1, 0, 0)
    assert due_between(SCHEDULE, start, start + timedelta(days=400))


# --- running jobs ---------------------------------------------------------


def _job(name: str, code: str, timeout_s: int = 30) -> ScheduledJob:
    """A job whose "module" is a Python one-liner, run through `-c`."""
    return ScheduledJob(name=name, cron="* * * * *", module="", args=("-c", code),
                        timeout_s=timeout_s)


def _runner() -> Runner:
    return Runner(command_for=lambda job: [sys.executable, *job.args])


def test_jobs_run_one_at_a_time_and_report_their_exit_status(tmp_path):
    marker = tmp_path / "order"
    first = _job("first", f"open({str(marker)!r}, 'a').write('1')")
    second = _job("second", f"open({str(marker)!r}, 'a').write('2'); raise SystemExit(3)")
    results = _runner().run_all([first, second])
    assert marker.read_text() == "12"
    assert [(r.name, r.exit_code) for r in results] == [("first", 0), ("second", 3)]


def test_a_failing_job_does_not_stop_the_ones_after_it(tmp_path):
    marker = tmp_path / "ran"
    results = _runner().run_all([
        _job("boom", "raise SystemExit(1)"),
        _job("after", f"open({str(marker)!r}, 'w').write('ok')"),
    ])
    assert marker.read_text() == "ok"
    assert [r.exit_code for r in results] == [1, 0]


def test_a_hung_job_is_killed_at_its_timeout():
    results = _runner().run_all([_job("hang", "import time; time.sleep(60)", timeout_s=1)])
    assert results[0].timed_out and results[0].exit_code != 0
    assert results[0].duration_s < 30


def test_the_real_jobs_are_started_as_python_modules():
    job = next(j for j in SCHEDULE if j.name == "gc-weekly")
    assert scheduler.module_command(job) == [
        sys.executable, "-m", "agentdrive.jobs.gc", "--orphan-sweep",
    ]


def test_run_one_by_name_and_refuse_an_unknown_name(capsys):
    assert scheduler.main(["--list"]) == 0
    listed = capsys.readouterr().out
    for job in SCHEDULE:
        assert job.name in listed and job.cron in listed
        assert scheduler.display_command(job) in listed
    assert scheduler.main(["--run", "no-such-job"]) == 2


def test_the_service_loop_stops_promptly_when_asked():
    """`docker compose stop` sends SIGTERM; the loop must not sit out the
    rest of its minute before exiting."""
    import threading

    runner = _runner()
    exit_codes: list[int] = []
    thread = threading.Thread(target=lambda: exit_codes.append(scheduler.serve(runner, ())))
    thread.start()
    runner.stop()
    thread.join(timeout=5)
    assert not thread.is_alive() and exit_codes == [0]


def test_stop_ends_the_running_child_and_runs_nothing_after_it(tmp_path):
    import threading

    marker = tmp_path / "second"
    runner = _runner()
    results: list = []
    jobs = [_job("slow", "import time; time.sleep(60)"),
            _job("second", f"open({str(marker)!r}, 'w').write('ran')")]
    thread = threading.Thread(target=lambda: results.extend(runner.run_all(jobs)))
    thread.start()
    import time

    time.sleep(1)
    runner.stop()
    thread.join(timeout=15)
    assert not thread.is_alive()
    assert [r.name for r in results] == ["slow"] and results[0].exit_code != 0
    assert not marker.exists()


# --- review round: cron parity with classic cron ---------------------------


def test_a_step_on_a_single_number_runs_to_the_fields_maximum():
    assert Cron.parse("5/15 * * * *").minute == {5, 20, 35, 50}


def test_a_starred_day_field_is_unrestricted_even_with_a_step():
    """Classic cron ORs the day fields only when NEITHER starts with `*`:
    `*/2` in day-of-month still ANDs with a restricted weekday."""
    cron = Cron.parse("0 0 */2 * 1")  # odd days of the month that are Mondays
    assert cron.matches(_utc(2026, 10, 5, 0, 0))  # Monday the 5th
    assert not cron.matches(_utc(2026, 10, 12, 0, 0))  # Monday the 12th
    assert not cron.matches(_utc(2026, 10, 7, 0, 0))  # Wednesday the 7th


def test_every_scheduled_job_fires_within_any_five_weeks():
    """A job that never fires would be a schedule typo nothing else notices."""
    start = _utc(2026, 1, 1, 0, 0)
    for job in SCHEDULE:
        cron = Cron.parse(job.cron)
        minutes = range(35 * 24 * 60)
        assert any(cron.matches(start + timedelta(minutes=m)) for m in minutes), job.name


def test_the_catch_up_window_really_bounds_the_look_back():
    """A yearly job last due more than CATCH_UP_WINDOW ago is not caught up
    — and the four real jobs, all due inside it, each are."""
    yearly = ScheduledJob(name="yearly", cron="0 0 1 1 *", module="m", args=(), timeout_s=60)
    assert due_between([yearly], _utc(2026, 1, 1, 0, 0) - timedelta(days=1),
                       _utc(2026, 2, 15, 0, 0)) == []
    everything = due_between(SCHEDULE, _utc(2025, 1, 1, 0, 0), _utc(2026, 2, 15, 0, 0))
    assert [j.name for j in everything] == [j.name for j in SCHEDULE]


# --- review round: the service loop, driven by a fake clock ----------------


class _Clock:
    def __init__(self, start: datetime):
        self.t = start

    def now(self) -> datetime:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += timedelta(seconds=max(seconds, 0.001))


class _FakeRunner(Runner):
    """Records runs, advances the clock by each job's duration, and stops the
    loop once the clock passes `until`."""

    def __init__(self, clock: _Clock, until: datetime, durations: dict[str, timedelta]):
        super().__init__()
        self.clock, self.until, self.durations = clock, until, durations
        self.runs: list[tuple[str, datetime]] = []

    @property
    def stopping(self) -> bool:
        return self.clock.t >= self.until

    def run(self, job):
        self.runs.append((job.name, self.clock.t))
        self.clock.t += self.durations.get(job.name, timedelta(seconds=5))
        return scheduler.RunResult(job.name, 0, 0.0, False)


def _serve(start, until, durations=None, state_path=None):
    clock = _Clock(start)
    runner = _FakeRunner(clock, until, durations or {})
    scheduler.serve(runner, SCHEDULE, state_path=state_path, now=clock.now, sleep=clock.sleep)
    return [(name, when.strftime("%H:%M")) for name, when in runner.runs]


def test_a_job_that_waited_behind_a_long_sweep_is_not_run_twice():
    """gc-daily takes 55 minutes; usage due at 03:00 runs once when it ends,
    and the 03:15/03:30/03:45 dues it covered do not run it again."""
    runs = _serve(_utc(2026, 9, 29, 2, 59, 30), _utc(2026, 9, 29, 4, 1),
                  {"gc-daily": timedelta(minutes=55)})
    assert [name for name, _ in runs] == [
        "gc-hourly", "gc-daily", "usage-snapshot",   # the 03:00 batch
        "gc-hourly", "usage-snapshot",               # 04:00
    ]


def test_a_restart_catches_up_what_came_due_while_it_was_down(tmp_path):
    """Down across Sunday 04:00: the weekly orphan sweep runs once on start,
    beside one hourly and one usage pass — not once per missed minute."""
    state = tmp_path / "state.json"
    scheduler.State(last_tick=_utc(2026, 10, 4, 3, 50)).save(state)
    runs = _serve(_utc(2026, 10, 4, 6, 10, 20), _utc(2026, 10, 4, 6, 11), state_path=state)
    assert sorted(name for name, _ in runs) == ["gc-hourly", "gc-weekly", "usage-snapshot"]


def test_a_first_start_runs_nothing_retroactively(tmp_path):
    runs = _serve(_utc(2026, 9, 29, 3, 0, 20), _utc(2026, 9, 29, 3, 1),
                  state_path=tmp_path / "state.json")
    assert runs == []


def test_state_persists_the_minute_and_each_start(tmp_path):
    state = tmp_path / "state.json"
    _serve(_utc(2026, 9, 29, 2, 59, 30), _utc(2026, 9, 29, 3, 2), state_path=state)
    saved = scheduler.State.load(state)
    assert saved.last_tick == _utc(2026, 9, 29, 3, 2) or saved.last_tick == _utc(2026, 9, 29, 3, 1)
    assert set(saved.started) == {"gc-hourly", "gc-daily", "usage-snapshot"}


def test_a_clock_stepped_backwards_replays_nothing(tmp_path):
    state = tmp_path / "state.json"
    scheduler.State(last_tick=_utc(2026, 9, 29, 5, 0)).save(state)
    runs = _serve(_utc(2026, 9, 29, 4, 0, 10), _utc(2026, 9, 29, 4, 14), state_path=state)
    assert runs == []


def test_an_unreadable_state_file_starts_fresh(tmp_path):
    state = tmp_path / "state.json"
    state.write_text("{not json")
    assert _serve(_utc(2026, 9, 29, 3, 0, 20), _utc(2026, 9, 29, 3, 1), state_path=state) == []


# --- review round: the runner ----------------------------------------------


def test_a_timeout_kills_the_jobs_whole_process_group(tmp_path):
    """A job's own children (a wrapper, a pool) die with it, TERM-deaf or not."""
    import os
    import time as _time

    pidfile = tmp_path / "grandchild"
    code = (
        "import subprocess, time\n"
        f"p = subprocess.Popen(['sh', '-c', 'trap \"\" TERM; echo $$ > {pidfile}; sleep 60'])\n"
        "time.sleep(60)\n"
    )
    result = _runner().run(_job("spawner", code, timeout_s=1))
    assert result.timed_out
    grandchild = int(pidfile.read_text())
    for _ in range(50):
        try:
            os.kill(grandchild, 0)
        except ProcessLookupError:
            break
        _time.sleep(0.1)
    else:
        raise AssertionError("the job's grandchild outlived the timeout")


def test_stop_escalates_to_kill_for_a_job_that_ignores_sigterm(monkeypatch):
    import threading
    import time as _time

    monkeypatch.setattr(scheduler, "KILL_GRACE_S", 1)
    runner = _runner()
    deaf = _job("deaf", "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                        "time.sleep(60)")
    results: list = []
    thread = threading.Thread(target=lambda: results.append(runner.run(deaf)))
    started = _time.monotonic()
    thread.start()
    _time.sleep(1)
    runner.stop()
    thread.join(timeout=15)
    assert not thread.is_alive() and _time.monotonic() - started < 10
    assert results[0].exit_code == 128 + 9


def test_a_stop_that_lands_before_the_child_starts_still_stops_it():
    runner = _runner()
    runner.stop()
    result = runner.run(_job("late", "import time; time.sleep(30)"))
    assert result.exit_code == 128 + 15 and result.duration_s < 10


def test_a_signal_death_reports_as_a_shell_would():
    result = _runner().run(_job("sig", "import os, signal; os.kill(os.getpid(), signal.SIGTERM)"))
    assert result.exit_code == 143
