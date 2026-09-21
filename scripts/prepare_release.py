#!/usr/bin/env python3
"""Build deterministic release assets and detect changes against the latest release."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_manifest(workdir: Path) -> dict:
    json_path = workdir / "bamf_questions.json"
    csv_path = workdir / "bamf_questions.csv"
    image_dir = workdir / "bamf_images"

    if not json_path.is_file() or not csv_path.is_file():
        raise SystemExit("Missing scraper output JSON or CSV.")

    images = []
    for path in sorted(image_dir.rglob("*")) if image_dir.exists() else []:
        if path.is_file():
            images.append({
                "path": path.relative_to(workdir).as_posix(),
                "sha256": sha256_file(path),
                "size": path.stat().st_size,
            })

    return {
        "schema_version": 1,
        "json": {
            "path": json_path.name,
            "sha256": sha256_file(json_path),
            "size": json_path.stat().st_size,
        },
        "csv": {
            "path": csv_path.name,
            "sha256": sha256_file(csv_path),
            "size": csv_path.stat().st_size,
        },
        "images": images,
        "image_count": len(images),
    }


def download_latest_manifest(repository: str, token: str) -> dict | None:
    api = f"https://api.github.com/repos/{repository}/releases/latest"
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "User-Agent": "bamf-data-release-builder",
    }
    try:
        with urlopen(Request(api, headers=headers), timeout=30) as response:
            release = json.load(response)
    except (HTTPError, URLError, TimeoutError, ValueError):
        return None

    for asset in release.get("assets", []):
        if asset.get("name") != "manifest.json":
            continue
        try:
            with urlopen(Request(asset["browser_download_url"], headers=headers), timeout=30) as response:
                return json.load(response)
        except (HTTPError, URLError, TimeoutError, ValueError):
            return None
    return None


def write_deterministic_zip(image_dir: Path, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(output, "w", compression=ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(image_dir.rglob("*")) if image_dir.exists() else []:
            if not path.is_file():
                continue
            arcname = path.relative_to(image_dir.parent).as_posix()
            info = ZipInfo(arcname)
            info.date_time = (2020, 1, 1, 0, 0, 0)
            info.compress_type = ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, path.read_bytes())


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workdir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repository", default=os.environ.get("GITHUB_REPOSITORY", ""))
    parser.add_argument("--token", default=os.environ.get("GITHUB_TOKEN", ""))
    parser.add_argument("--github-output", default=os.environ.get("GITHUB_OUTPUT"))
    args = parser.parse_args()

    manifest = build_manifest(args.workdir)
    previous = (
        download_latest_manifest(args.repository, args.token)
        if args.repository and args.token
        else None
    )

    changed = previous is None or manifest != previous
    timestamp = datetime.now(timezone.utc)
    tag = "data-" + timestamp.strftime("%Y.%m.%d-%H%M")

    output = args.output
    output.mkdir(parents=True, exist_ok=True)

    (output / "bamf_questions.json").write_bytes(
        (args.workdir / "bamf_questions.json").read_bytes()
    )
    (output / "bamf_questions.csv").write_bytes(
        (args.workdir / "bamf_questions.csv").read_bytes()
    )
    (output / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    write_deterministic_zip(
        args.workdir / "bamf_images",
        output / "bamf_images.zip",
    )

    if args.github_output:
        with open(args.github_output, "a", encoding="utf-8") as handle:
            handle.write(f"changed={'true' if changed else 'false'}\n")
            handle.write(f"release_tag={tag}\n")

    print(f"Change detected: {changed}")
    print(f"Release tag: {tag}")
    print(f"Images: {manifest['image_count']}")


if __name__ == "__main__":
    main()
