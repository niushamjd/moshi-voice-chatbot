"""Render a report as an aligned text table, Markdown or JSON."""

import json


def fmt_duration(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    if seconds < 1:
        return f"{seconds * 1000:.0f}ms"
    if seconds < 60:
        return f"{seconds:.1f}s" if seconds < 10 else f"{seconds:.0f}s"
    minutes, secs = divmod(round(seconds), 60)
    return f"{minutes}m {secs:02d}s"


def fmt_pct(value: float | None) -> str:
    return "-" if value is None else f"{value:.0%}"


def _sections(report: dict) -> list[tuple[str, list[str], list[list[str]]]]:
    """Each section is (title, headers, rows); both text formats render these."""
    builds, tests = report["builds"], report["tests"]
    slowest = builds["slowest_run"]
    summary = [
        ["Runs analysed", str(builds["runs"])],
        ["Success rate", fmt_pct(builds["success_rate"])],
        ["Conclusions", ", ".join(f"{k} {v}" for k, v in builds["conclusions"].items()) or "-"],
        ["Duration median", fmt_duration(builds["duration_median_s"])],
        ["Duration p90", fmt_duration(builds["duration_p90_s"])],
        [
            "Slowest run",
            "-"
            if slowest is None
            else f"{fmt_duration(slowest['duration_s'])} on {slowest['branch']} @ {slowest['sha']} {slowest['url']}",
        ],
        ["Runs without test reports", str(builds["runs_without_test_reports"])],
        ["Test executions", f"{tests['executions']} ({tests['distinct_tests']} distinct tests)"],
    ]
    return [
        ("Builds", ["Metric", "Value"], summary),
        ("Median duration per job", ["Job", "Median", "Runs"], [
            [j["name"], fmt_duration(j["median_s"]), str(j["runs"])] for j in builds["jobs"]
        ]),
        ("Most failing tests", ["Test", "Failures", "Runs", "Failure rate"], [
            [t["id"], str(t["failures"]), str(t["executions"]), fmt_pct(t["failure_rate"])]
            for t in tests["most_failing"]
        ]),
        ("Flaky tests (passed and failed on the same commit)", ["Test", "Commits", "Failures", "Runs"], [
            [t["id"], ", ".join(t["commits"]), str(t["failures"]), str(t["executions"])] for t in tests["flaky"]
        ]),
        ("Slowest tests", ["Test", "Median", "Runs"], [
            [t["id"], fmt_duration(t["median_s"]), str(t["executions"])] for t in tests["slowest"]
        ]),
    ]  # fmt: skip


def render_table(report: dict) -> str:
    out = [f"ci-insights: {report['repo']} ({report['workflow']}, branch: {report['branch'] or 'all'})"]
    for title, headers, rows in _sections(report):
        out += ["", title, "-" * len(title)]
        if not rows:
            out.append("(none)")
            continue
        widths = [max(len(r[i]) for r in [headers, *rows]) for i in range(len(headers))]
        for row in [headers, *rows]:
            out.append("  ".join(cell.ljust(w) for cell, w in zip(row, widths, strict=True)).rstrip())
    return "\n".join(out) + "\n"


def _md_cell(text: str) -> str:
    return text.replace("|", "\\|")


def render_markdown(report: dict) -> str:
    branch = report["branch"] or "all branches"
    out = [f"## CI insights: `{report['repo']}` ({report['workflow']}, {branch})"]
    for title, headers, rows in _sections(report):
        out += ["", f"### {title}", ""]
        if not rows:
            out.append("_None._")
            continue
        out.append("| " + " | ".join(headers) + " |")
        out.append("|" + "|".join("---" for _ in headers) + "|")
        out += ["| " + " | ".join(_md_cell(c) for c in row) + " |" for row in rows]
    return "\n".join(out) + "\n"


def render_json(report: dict) -> str:
    return json.dumps(report, indent=2) + "\n"


RENDERERS = {"table": render_table, "markdown": render_markdown, "json": render_json}
