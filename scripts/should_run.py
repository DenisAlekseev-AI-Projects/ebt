#!/usr/bin/env python3
"""Return whether the scheduled scraper run is due."""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def latest_release_age_days(repository: str, token: str) -> float | None:
    url = f"https://api.github.com/repos/{repository}/releases/latest"
    request = Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "bamf-scheduled-run-gate",
        },
    )
    try:
        with urlopen(request, timeout=30) as response:
            data = json.load(response)
    except (HTTPError, URLError, TimeoutError, ValueError):
        return None

    published = data.get("published_at")
    if not published:
        return None
    timestamp = datetime.fromisoformat(published.replace("Z", "+00:00"))
    return (datetime.now(timezone.utc) - timestamp).total_seconds() / 86400


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=float, default=14)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    if args.force:
        print("Run forced.")
        return 0

    repository = os.environ.get("GITHUB_REPOSITORY")
    token = os.environ.get("GITHUB_TOKEN")
    if not repository or not token:
        print("Repository/token not available; run is allowed.")
        return 0

    age = latest_release_age_days(repository, token)
    if age is None:
        print("No usable latest release found; run is allowed.")
        return 0

    if age >= args.days:
        print(f"Latest release is {age:.1f} days old; run is due.")
        return 0

    print(f"Latest release is only {age:.1f} days old; skipping this scheduled run.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
