"""Turn runs, jobs and test results into the report (plain dicts, JSON-ready)."""

import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime

from .junit import FAILED, PASSED, TestCase


@dataclass
class RunData:
    run: dict
    jobs: list[dict] = field(default_factory=list)
    tests: list[TestCase] = field(default_factory=list)
    has_reports: bool = False


def percentile(values: list[float], p: float) -> float | None:
    """Linear interpolation between closest ranks (numpy's default method)."""
    if not values:
        return None
    ordered = sorted(values)
    k = (len(ordered) - 1) * p / 100
    lo = int(k)
    hi = min(lo + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo)


def _seconds(start: str | None, end: str | None) -> float | None:
    if not start or not end:
        return None
    parse = lambda s: datetime.fromisoformat(s.replace("Z", "+00:00"))  # noqa: E731
    return (parse(end) - parse(start)).total_seconds()


def run_duration(run: dict) -> float | None:
    return _seconds(run.get("run_started_at") or run.get("created_at"), run.get("updated_at"))


def build_report(runs: list[RunData], top: int = 10) -> dict:
    return {"builds": _builds(runs), "tests": _tests(runs, top)}


def _builds(runs: list[RunData]) -> dict:
    conclusions: dict[str, int] = defaultdict(int)
    durations = []
    slowest = None
    job_durations: dict[str, list[float]] = defaultdict(list)
    for data in runs:
        run = data.run
        conclusions[run.get("conclusion") or "unknown"] += 1
        duration = run_duration(run)
        if duration is not None:
            durations.append(duration)
            if slowest is None or duration > slowest[0]:
                slowest = (duration, run)
        for job in data.jobs:
            if job.get("conclusion") == "skipped":
                continue
            job_seconds = _seconds(job.get("started_at"), job.get("completed_at"))
            if job_seconds is not None:
                job_durations[job["name"]].append(job_seconds)

    total = len(runs)
    return {
        "runs": total,
        "conclusions": dict(sorted(conclusions.items())),
        "success_rate": conclusions["success"] / total if total else None,
        "duration_median_s": statistics.median(durations) if durations else None,
        "duration_p90_s": percentile(durations, 90),
        "jobs": [
            {"name": name, "median_s": statistics.median(values), "runs": len(values)}
            for name, values in sorted(job_durations.items())
        ],
        "slowest_run": None
        if slowest is None
        else {
            "id": slowest[1]["id"],
            "url": slowest[1].get("html_url"),
            "duration_s": slowest[0],
            "branch": slowest[1].get("head_branch"),
            "sha": (slowest[1].get("head_sha") or "")[:7],
        },
        "runs_without_test_reports": sum(1 for data in runs if not data.has_reports),
    }


def _tests(runs: list[RunData], top: int) -> dict:
    executions: dict[str, int] = defaultdict(int)
    failures: dict[str, int] = defaultdict(int)
    times: dict[str, list[float]] = defaultdict(list)
    # Outcomes per (commit, test) across runs, re-runs and matrix entries.
    by_commit: dict[tuple[str, str], set[str]] = defaultdict(set)
    for data in runs:
        sha = data.run.get("head_sha", "")
        for case in data.tests:
            if case.outcome not in (PASSED, FAILED):
                continue
            executions[case.id] += 1
            times[case.id].append(case.time)
            if case.outcome == FAILED:
                failures[case.id] += 1
            by_commit[(sha, case.id)].add(case.outcome)

    flaky_commits: dict[str, list[str]] = defaultdict(list)
    for (sha, test_id), outcomes in by_commit.items():
        if {PASSED, FAILED} <= outcomes:
            flaky_commits[test_id].append(sha[:7])

    failing = sorted(failures, key=lambda t: (-failures[t], -failures[t] / executions[t], t))
    slowest = sorted(times, key=lambda t: (-statistics.median(times[t]), t))
    return {
        "executions": sum(executions.values()),
        "distinct_tests": len(executions),
        "most_failing": [
            {
                "id": t,
                "failures": failures[t],
                "executions": executions[t],
                "failure_rate": failures[t] / executions[t],
            }
            for t in failing[:top]
        ],
        "flaky": [
            {"id": t, "commits": sorted(shas), "failures": failures[t], "executions": executions[t]}
            for t, shas in sorted(flaky_commits.items(), key=lambda kv: (-len(kv[1]), kv[0]))
        ][:top],
        "slowest": [
            {"id": t, "median_s": statistics.median(times[t]), "executions": len(times[t])} for t in slowest[:top]
        ],
    }
