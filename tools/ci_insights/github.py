"""A small GitHub REST client: auth, pagination, rate limits and an on-disk cache."""

import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError

API = "https://api.github.com"
# Warn once when fewer requests than this are left in the rate-limit window.
LOW_RATE_LIMIT = 20


class GitHubError(Exception):
    pass


class RateLimitError(GitHubError):
    pass


@dataclass
class Response:
    status: int
    headers: dict[str, str]  # lower-cased names
    body: bytes

    def json(self):
        return json.loads(self.body)


Transport = Callable[[str, dict[str, str]], Response]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    # Redirects are followed by GitHubClient so it can drop the token when the
    # target is another host (artifact zips redirect to signed blob-storage URLs).
    def redirect_request(self, *args, **kwargs):
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def urllib_transport(url: str, headers: dict[str, str]) -> Response:
    request = urllib.request.Request(url, headers=headers)
    try:
        with _opener.open(request, timeout=60) as resp:
            return Response(resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read())
    except HTTPError as e:
        return Response(e.code, {k.lower(): v for k, v in e.headers.items()}, e.read())


def resolve_token(env=os.environ, run=subprocess.run) -> str:
    for name in ("GITHUB_TOKEN", "GH_TOKEN"):
        if env.get(name):
            return env[name]
    try:
        result = run(["gh", "auth", "token"], capture_output=True, text=True, timeout=10)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        result = None
    if result is not None and result.returncode == 0 and result.stdout.strip():
        return result.stdout.strip()
    raise GitHubError(
        "no GitHub token found. Set GITHUB_TOKEN or GH_TOKEN, or run `gh auth login`. "
        "Downloading test reports needs a token even for public repos."
    )


class Cache:
    """Files under a directory, keyed by a readable path. A None dir disables it."""

    def __init__(self, directory: Path | None):
        self.directory = directory

    def _path(self, key: str) -> Path:
        safe = re.sub(r"[^A-Za-z0-9._/-]", "_", key)
        return self.directory / safe

    def get(self, key: str) -> bytes | None:
        if self.directory is None:
            return None
        path = self._path(key)
        return path.read_bytes() if path.exists() else None

    def put(self, key: str, data: bytes) -> None:
        if self.directory is None:
            return
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(path)


def _next_link(link_header: str | None) -> str | None:
    for part in (link_header or "").split(","):
        match = re.match(r'\s*<([^>]+)>;\s*rel="next"', part)
        if match:
            return match.group(1)
    return None


class GitHubClient:
    def __init__(
        self,
        token: str,
        transport: Transport = urllib_transport,
        cache: Cache | None = None,
        warn: Callable[[str], None] = lambda msg: print(f"warning: {msg}", file=sys.stderr),
    ):
        self.token = token
        self.transport = transport
        self.cache = cache or Cache(None)
        self.warn = warn
        self.requests = 0
        self._warned_low = False

    def _get(self, url: str, accept: str = "application/vnd.github+json") -> Response:
        headers = {
            "Accept": accept,
            "Authorization": f"Bearer {self.token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "ci-insights",
        }
        api_host = urllib.parse.urlsplit(API).netloc
        for _ in range(5):
            self.requests += 1
            resp = self.transport(url, headers)
            self._check_rate_limit(resp)
            if resp.status in (301, 302, 303, 307, 308) and "location" in resp.headers:
                url = urllib.parse.urljoin(url, resp.headers["location"])
                if urllib.parse.urlsplit(url).netloc != api_host:
                    headers.pop("Authorization", None)
                continue
            if resp.status == 401:
                raise GitHubError("GitHub rejected the token (401). Check that it is valid and not expired.")
            if resp.status == 404:
                raise GitHubError(f"not found (404): {url}. Check --repo and --workflow, and the token's access.")
            if resp.status >= 400:
                raise GitHubError(f"GitHub returned {resp.status} for {url}: {resp.body[:200]!r}")
            return resp
        raise GitHubError(f"too many redirects for {url}")

    def _check_rate_limit(self, resp: Response) -> None:
        remaining = resp.headers.get("x-ratelimit-remaining")
        if remaining is None:
            return
        if resp.status in (403, 429) and remaining == "0":
            reset = int(resp.headers.get("x-ratelimit-reset", "0"))
            wait_min = max(0, reset - int(time.time())) // 60 + 1
            raise RateLimitError(
                f"GitHub API rate limit exhausted (limit {resp.headers.get('x-ratelimit-limit', '?')}/hour); "
                f"it resets in about {wait_min} min. Cached data is reused, so re-running later is cheap; "
                "lower --limit to need fewer requests."
            )
        if int(remaining) < LOW_RATE_LIMIT and not self._warned_low:
            self._warned_low = True
            self.warn(f"only {remaining} GitHub API requests left in this rate-limit window")

    def _paginate(self, url: str, item_key: str, limit: int | None = None) -> list[dict]:
        items: list[dict] = []
        next_url: str | None = url
        while next_url and (limit is None or len(items) < limit):
            resp = self._get(next_url)
            items.extend(resp.json()[item_key])
            next_url = _next_link(resp.headers.get("link"))
        return items if limit is None else items[:limit]

    def list_runs(self, repo: str, workflow: str, limit: int, branch: str | None = None) -> list[dict]:
        # Never cached: new runs keep arriving.
        params = {"status": "completed", "per_page": str(min(100, max(1, limit)))}
        if branch:
            params["branch"] = branch
        url = f"{API}/repos/{repo}/actions/workflows/{workflow}/runs?{urllib.parse.urlencode(params)}"
        return self._paginate(url, "workflow_runs", limit)

    # A completed run attempt never changes, so its jobs and artifacts are cached
    # per attempt (a re-run starts a new attempt with different jobs).
    def run_jobs(self, repo: str, run: dict) -> list[dict]:
        key = f"{repo}/runs/{run['id']}/attempt-{run.get('run_attempt', 1)}/jobs.json"
        cached = self.cache.get(key)
        if cached is not None:
            return json.loads(cached)
        jobs = self._paginate(f"{API}/repos/{repo}/actions/runs/{run['id']}/jobs?per_page=100", "jobs")
        self.cache.put(key, json.dumps(jobs).encode())
        return jobs

    def run_artifacts(self, repo: str, run: dict) -> list[dict]:
        key = f"{repo}/runs/{run['id']}/attempt-{run.get('run_attempt', 1)}/artifacts.json"
        cached = self.cache.get(key)
        if cached is not None:
            return json.loads(cached)
        artifacts = self._paginate(f"{API}/repos/{repo}/actions/runs/{run['id']}/artifacts?per_page=100", "artifacts")
        self.cache.put(key, json.dumps(artifacts).encode())
        return artifacts

    def download_artifact(self, repo: str, artifact: dict) -> bytes:
        key = f"{repo}/artifacts/{artifact['id']}.zip"
        cached = self.cache.get(key)
        if cached is not None:
            return cached
        data = self._get(artifact["archive_download_url"], accept="application/zip").body
        self.cache.put(key, data)
        return data


def default_cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "ci-insights"
