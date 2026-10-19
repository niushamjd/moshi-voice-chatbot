"""Report on this repo's CI: build success and durations, failing, flaky and slow tests.

    python -m tools.ci_insights --repo niushamjd/moshi-voice-chatbot --limit 50 --format markdown

Needs a GitHub token (GITHUB_TOKEN, GH_TOKEN, or `gh auth token`): test reports
are artifact zips, which GitHub only serves to authenticated requests.
"""

import argparse
import sys
from pathlib import Path

from .github import Cache, GitHubClient, GitHubError, default_cache_dir, resolve_token, urllib_transport
from .junit import parse_junit_zip
from .render import RENDERERS
from .report import RunData, build_report

JUNIT_PREFIX = "junit-"


def collect(client: GitHubClient, repo: str, workflow: str, limit: int, branch: str | None, log) -> list[RunData]:
    runs = client.list_runs(repo, workflow, limit, branch)
    log(f"analysing {len(runs)} completed runs of {workflow} in {repo}")
    collected = []
    for run in runs:
        data = RunData(run=run, jobs=client.run_jobs(repo, run))
        for artifact in client.run_artifacts(repo, run):
            # Runs that failed before the test step have no junit artifact;
            # old ones may have expired. Both just count as "no test reports".
            if not artifact["name"].startswith(JUNIT_PREFIX) or artifact.get("expired"):
                continue
            data.tests.extend(parse_junit_zip(client.download_artifact(repo, artifact)))
            data.has_reports = True
        collected.append(data)
    return collected


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m tools.ci_insights",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--repo", required=True, help="owner/name")
    parser.add_argument("--workflow", default="ci.yml", help="workflow file name (default: ci.yml)")
    parser.add_argument("--limit", type=int, default=50, help="number of most recent completed runs (default: 50)")
    parser.add_argument("--branch", help="only runs on this branch")
    parser.add_argument("--format", choices=sorted(RENDERERS), default="table")
    parser.add_argument("--top", type=int, default=10, help="rows per test table (default: 10)")
    parser.add_argument("--no-cache", action="store_true", help="don't read or write the on-disk cache")
    parser.add_argument("--cache-dir", type=Path, default=None, help=f"default: {default_cache_dir()}")
    args = parser.parse_args(argv)
    if args.limit < 1:
        parser.error("--limit must be at least 1")
    return args


def main(argv: list[str] | None = None, transport=None) -> int:
    args = parse_args(argv)

    def log(msg: str) -> None:
        print(msg, file=sys.stderr)

    try:
        cache = Cache(None if args.no_cache else (args.cache_dir or default_cache_dir()))
        client = GitHubClient(resolve_token(), transport=transport or urllib_transport, cache=cache)
        runs = collect(client, args.repo, args.workflow, args.limit, args.branch, log)
    except GitHubError as e:
        log(f"error: {e}")
        return 2
    log(f"{client.requests} API requests")

    report = {"repo": args.repo, "workflow": args.workflow, "branch": args.branch, **build_report(runs, args.top)}
    sys.stdout.write(RENDERERS[args.format](report))
    return 0
