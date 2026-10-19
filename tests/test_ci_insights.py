"""ci-insights against recorded GitHub API responses; the HTTP layer is faked, no network."""

import io
import json
import subprocess
import urllib.parse
import zipfile
from pathlib import Path

import pytest

from tools.ci_insights import cli
from tools.ci_insights.github import Cache, GitHubClient, GitHubError, RateLimitError, Response, resolve_token
from tools.ci_insights.junit import FAILED, PASSED, SKIPPED, TestCase, parse_junit_xml, parse_junit_zip
from tools.ci_insights.render import fmt_duration, render_markdown, render_table
from tools.ci_insights.report import RunData, build_report, percentile

FIXTURES = Path(__file__).parent / "fixtures" / "ci_insights"
REPO = "niushamjd/moshi-voice-chatbot"
# GitHub's "next" links use the numeric repository id, not owner/name.
PAGE_2 = "https://api.github.com/repositories/1207399446/actions/workflows/ci.yml/runs?status=completed&page=2"
BLOB = "https://productionresultssa0.blob.core.windows.net/actions-results/"


class FakeGitHub:
    """Serves the fixtures by URL and records every request."""

    def __init__(self, rate_remaining: int = 4000):
        self.requests: list[tuple[str, dict]] = []
        self.rate_remaining = rate_remaining

    def __call__(self, url: str, headers: dict) -> Response:
        self.requests.append((url, dict(headers)))
        parts = urllib.parse.urlsplit(url)
        rate = {"x-ratelimit-remaining": str(self.rate_remaining), "x-ratelimit-limit": "5000"}
        if url.startswith(BLOB):
            # Blob storage rejects requests that carry a second credential.
            if "Authorization" in headers:
                return Response(400, {}, b"<Error>InvalidAuthenticationInfo</Error>")
            return Response(200, {}, (FIXTURES / f"artifact_{parts.path.rsplit('/', 1)[1]}.zip").read_bytes())
        if url == PAGE_2:
            return self._file("runs_page2.json", rate)
        path = parts.path.removeprefix(f"/repos/{REPO}/actions/")
        if path == "workflows/ci.yml/runs":
            return self._file("runs_page1.json", {**rate, "link": f'<{PAGE_2}>; rel="next", <{PAGE_2}>; rel="last"'})
        kind, ident, *rest = path.split("/")
        if kind == "runs" and rest == ["jobs"]:
            return self._file(f"jobs_{ident}.json", rate)
        if kind == "runs" and rest == ["artifacts"]:
            return self._file(f"artifacts_{ident}.json", rate)
        if kind == "artifacts" and rest == ["zip"]:
            return Response(302, {**rate, "location": f"{BLOB}{ident}?sig=abc"}, b"")
        return Response(404, rate, b'{"message": "Not Found"}')

    @staticmethod
    def _file(name: str, headers: dict) -> Response:
        return Response(200, headers, (FIXTURES / name).read_bytes())

    def urls(self, needle: str) -> list[str]:
        return [url for url, _ in self.requests if needle in url]


@pytest.fixture
def fake():
    return FakeGitHub()


def collect(fake, limit=50, cache=None):
    client = GitHubClient("t0ken", transport=fake, cache=cache)
    return cli.collect(client, REPO, "ci.yml", limit, None, log=lambda _: None)


# --- GitHub client -----------------------------------------------------------


def test_list_runs_follows_link_header_across_pages(fake):
    runs = GitHubClient("t0ken", transport=fake).list_runs(REPO, "ci.yml", limit=50)
    assert [r["id"] for r in runs] == [101, 102, 103]
    assert fake.urls(PAGE_2) == [PAGE_2]


def test_list_runs_stops_paginating_at_limit(fake):
    runs = GitHubClient("t0ken", transport=fake).list_runs(REPO, "ci.yml", limit=2)
    assert [r["id"] for r in runs] == [101, 102]
    assert fake.urls(PAGE_2) == []
    assert "per_page=2" in fake.requests[0][0]


def test_list_runs_passes_branch_filter(fake):
    GitHubClient("t0ken", transport=fake).list_runs(REPO, "ci.yml", limit=5, branch="main")
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(fake.requests[0][0]).query)
    assert query["branch"] == ["main"] and query["status"] == ["completed"]


def test_artifact_redirect_drops_the_token(fake):
    client = GitHubClient("t0ken", transport=fake)
    artifact = client.run_artifacts(REPO, {"id": 101})[0]
    data = client.download_artifact(REPO, artifact)
    assert zipfile.ZipFile(io.BytesIO(data)).namelist() == ["junit.xml"]
    api_call, blob_call = fake.requests[-2:]
    assert api_call[1]["Authorization"] == "Bearer t0ken"
    assert "Authorization" not in blob_call[1]


def test_rate_limit_exhausted_raises_helpful_error():
    def transport(url, headers):
        return Response(403, {"x-ratelimit-remaining": "0", "x-ratelimit-limit": "60", "x-ratelimit-reset": "0"}, b"")

    with pytest.raises(RateLimitError, match="rate limit exhausted.*limit 60/hour"):
        GitHubClient("t0ken", transport=transport).list_runs(REPO, "ci.yml", limit=1)


def test_low_rate_limit_warns_once():
    warnings = []
    client = GitHubClient("t0ken", transport=FakeGitHub(rate_remaining=5), warn=warnings.append)
    client.list_runs(REPO, "ci.yml", limit=50)
    assert warnings == ["only 5 GitHub API requests left in this rate-limit window"]


def test_not_found_and_bad_token_errors(fake):
    with pytest.raises(GitHubError, match="404.*--repo"):
        GitHubClient("t0ken", transport=fake).list_runs("someone/else", "ci.yml", limit=1)
    with pytest.raises(GitHubError, match="401"):
        GitHubClient("bad", transport=lambda url, headers: Response(401, {}, b"")).list_runs(REPO, "ci.yml", 1)


def test_token_from_env_then_gh_cli():
    def no_gh(*args, **kwargs):
        raise FileNotFoundError

    assert resolve_token({"GITHUB_TOKEN": "a", "GH_TOKEN": "b"}, run=no_gh) == "a"
    assert resolve_token({"GH_TOKEN": "b"}, run=no_gh) == "b"
    gh = lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="gho_c\n")  # noqa: E731
    assert resolve_token({}, run=gh) == "gho_c"


def test_missing_token_is_a_clear_error():
    logged_out = lambda *a, **k: subprocess.CompletedProcess(a, 1, stdout="")  # noqa: E731
    with pytest.raises(GitHubError, match="GITHUB_TOKEN or GH_TOKEN.*gh auth login"):
        resolve_token({}, run=logged_out)


def test_cache_serves_completed_runs_without_requests(fake, tmp_path):
    collect(fake, cache=Cache(tmp_path))
    first = len(fake.requests)
    fake.requests.clear()
    collect(fake, cache=Cache(tmp_path))
    # Only the run list is fetched again; jobs, artifacts and zips come from disk.
    assert [u for u, _ in fake.requests if "/jobs" in u or "/artifacts" in u or u.startswith(BLOB)] == []
    assert len(fake.requests) == 2 < first


def test_cache_is_per_run_attempt(fake, tmp_path):
    client = GitHubClient("t0ken", transport=fake, cache=Cache(tmp_path))
    client.run_jobs(REPO, {"id": 101, "run_attempt": 1})
    client.run_jobs(REPO, {"id": 101, "run_attempt": 2})  # a re-run: must not reuse attempt 1
    assert len(fake.urls("/runs/101/jobs")) == 2


def test_no_cache_writes_nothing(fake, tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "t0ken")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    assert cli.main(["--repo", REPO, "--no-cache", "--format", "json"], transport=fake) == 0
    assert list(tmp_path.iterdir()) == []


# --- JUnit parsing -----------------------------------------------------------


def test_parse_junit_outcomes():
    xml = b"""<testsuites><testsuite>
      <testcase classname="tests.t" name="ok" time="0.5"/>
      <testcase classname="tests.t" name="bad" time="1"><failure message="x"/></testcase>
      <testcase classname="tests.t" name="crash" time="0"><error message="x"/></testcase>
      <testcase classname="tests.t" name="skip" time="0"><skipped/></testcase>
    </testsuite></testsuites>"""
    assert parse_junit_xml(xml) == [
        TestCase("tests.t::ok", PASSED, 0.5),
        TestCase("tests.t::bad", FAILED, 1.0),
        TestCase("tests.t::crash", FAILED, 0.0),
        TestCase("tests.t::skip", SKIPPED, 0.0),
    ]


def test_parse_junit_zip_fixture():
    cases = parse_junit_zip((FIXTURES / "artifact_1004.zip").read_bytes())
    assert {c.id.split("::")[1]: c.outcome for c in cases}["test_flaky"] == FAILED
    assert len(cases) == 5


# --- Report maths ------------------------------------------------------------


@pytest.mark.parametrize(
    ("values", "p", "expected"),
    [
        ([30, 120, 180], 90, 168.0),  # between ranks: 120 + 0.8 * (180 - 120)
        ([30, 120, 180], 50, 120.0),
        (list(range(1, 11)), 90, 9.1),
        ([7], 90, 7.0),
        ([5, 1], 0, 1.0),
        ([5, 1], 100, 5.0),
        ([], 90, None),
    ],
)
def test_percentile(values, p, expected):
    result = percentile(values, p)
    assert result == expected if expected is None else result == pytest.approx(expected)


def test_report_from_fixtures(fake):
    report = build_report(collect(fake))
    builds, tests = report["builds"], report["tests"]

    assert builds["runs"] == 3
    assert builds["conclusions"] == {"failure": 2, "success": 1}
    assert builds["success_rate"] == pytest.approx(1 / 3)
    assert builds["duration_median_s"] == 120
    assert builds["duration_p90_s"] == pytest.approx(168)
    assert builds["slowest_run"]["id"] == 101
    # Skipped jobs (docker/k8s-e2e on the red run) don't count towards medians.
    assert {j["name"]: (j["median_s"], j["runs"]) for j in builds["jobs"]} == {
        "docker": (34, 1),
        "k8s-e2e": (87, 1),
        "lint": (16, 3),
        "test (3.12)": (24, 3),
        "test (3.13)": (18, 3),
    }

    assert tests["executions"] == 16  # 4 zips x 4 non-skipped tests
    assert [(t["id"].split("::")[1], t["failures"], t["failure_rate"]) for t in tests["most_failing"]] == [
        ("test_broken", 2, 0.5),
        ("test_flaky", 1, 0.25),
    ]
    assert tests["slowest"][0]["id"].endswith("test_opus_round_trip_echoes_audio_and_text")


def test_flaky_means_both_outcomes_on_one_commit(fake):
    flaky = build_report(collect(fake))["tests"]["flaky"]
    # test_flaky failed on 3.12 and passed on 3.13 for commit bbbbbbb;
    # test_broken failed on both, which is a real failure, not flakiness.
    assert [(t["id"].split("::")[1], t["commits"]) for t in flaky] == [("test_flaky", ["bbbbbbb"])]


def test_failures_on_different_commits_are_not_flaky():
    runs = [
        RunData({"head_sha": "1" * 40}, tests=[TestCase("t", PASSED, 0)], has_reports=True),
        RunData({"head_sha": "2" * 40}, tests=[TestCase("t", FAILED, 0)], has_reports=True),
    ]
    assert build_report(runs)["tests"]["flaky"] == []


def test_runs_without_artifacts_are_counted_not_fatal(fake):
    runs = collect(fake)
    assert [(r.run["id"], r.has_reports) for r in runs] == [(101, True), (102, True), (103, False)]
    assert build_report(runs)["builds"]["runs_without_test_reports"] == 1


def test_only_unexpired_junit_artifacts_are_downloaded(fake, monkeypatch):
    original = GitHubClient.run_artifacts

    def with_expired(self, repo, run):
        artifacts = original(self, repo, run)
        if run["id"] == 102:
            artifacts[1] = {**artifacts[1], "expired": True}
        return artifacts

    monkeypatch.setattr(GitHubClient, "run_artifacts", with_expired)
    collect(fake)
    downloaded = [u.rsplit("/", 2)[-2] for u in fake.urls("/zip")]
    assert downloaded == ["1001", "1002", "1004"]  # no .dockerbuild (1003), no expired (1005)


def test_empty_report_renders():
    report = {"repo": REPO, "workflow": "ci.yml", "branch": None, **build_report([])}
    assert "(none)" in render_table(report)
    assert "_None._" in render_markdown(report)


# --- CLI and rendering -------------------------------------------------------


@pytest.mark.parametrize(
    ("seconds", "text"), [(None, "-"), (4.25, "4.2s"), (42, "42s"), (87, "1m 27s"), (3600, "60m 00s")]
)
def test_fmt_duration(seconds, text):
    assert fmt_duration(seconds) == text


def test_cli_json(fake, monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_TOKEN", "t0ken")
    assert cli.main(["--repo", REPO, "--no-cache", "--format", "json", "--top", "1"], transport=fake) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["repo"] == REPO and report["builds"]["runs"] == 3
    assert len(report["tests"]["most_failing"]) == 1


def test_cli_markdown_has_all_sections(fake, monkeypatch, capsys):
    monkeypatch.setenv("GITHUB_TOKEN", "t0ken")
    assert cli.main(["--repo", REPO, "--no-cache", "--format", "markdown"], transport=fake) == 0
    out = capsys.readouterr().out
    for heading in ["Builds", "Median duration per job", "Most failing tests", "Flaky tests", "Slowest tests"]:
        assert f"### {heading}" in out
    assert "| k8s-e2e | 1m 27s | 1 |" in out
    assert "https://github.com/niushamjd/moshi-voice-chatbot/actions/runs/101" in out


def test_cli_reports_errors_with_exit_code_2(monkeypatch, capsys):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 1, stdout=""))
    assert cli.main(["--repo", REPO, "--no-cache"]) == 2
    assert "error: no GitHub token found" in capsys.readouterr().err
