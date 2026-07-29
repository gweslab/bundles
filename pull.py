#!/usr/bin/env python3

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

from make_bundles import MANIFEST_VERSION


ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
ROMS_DIR = DATA_DIR / "roms"
MANIFEST_PATH = DATA_DIR / "manifest.json"
TOKEN_PATH = ROOT / ".r2_token"
PUBLIC_USER_AGENT = "CERF-Bundles/1.0"


class PullError(RuntimeError):
    pass


def load_config() -> dict[str, str]:
    config: dict[str, str] = {}
    if TOKEN_PATH.exists():
        for raw_line in TOKEN_PATH.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export "):].strip()
            if "=" not in line:
                continue
            key, value = line.split("=", 1)
            config[key.strip()] = value.strip().strip("'\"")

    for key, value in os.environ.items():
        if key.startswith("R2_"):
            config[key] = value
    return config


def public_base_url(config: dict[str, str]) -> str:
    base = config.get("R2_PUBLIC_BASE_URL", "").strip()
    if not base:
        raise PullError(
            "R2_PUBLIC_BASE_URL is missing. Put it in .r2_token, "
            "for example R2_PUBLIC_BASE_URL=https://pub.example.com"
        )
    return base.rstrip("/")


def sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def object_url(base_url: str, key: str) -> str:
    return f"{base_url}/{urllib.parse.quote(key, safe='/')}"


def download_object(base_url: str, key: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    url = object_url(base_url, key)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".part",
        dir=str(destination.parent),
    )
    os.close(fd)
    temp_path = Path(temp_name)
    try:
        request = urllib.request.Request(url, headers={"User-Agent": PUBLIC_USER_AGENT})
        with urllib.request.urlopen(request) as response, temp_path.open("wb") as handle:
            shutil.copyfileobj(response, handle, length=1024 * 1024)
        temp_path.replace(destination)
    except urllib.error.HTTPError as exc:
        temp_path.unlink(missing_ok=True)
        raise PullError(f"Failed to download {key}: HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        temp_path.unlink(missing_ok=True)
        raise PullError(f"Failed to download {key}: {exc.reason}") from exc
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise


def load_manifest(base_url: str) -> dict:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    download_object(base_url, "manifest.json", MANIFEST_PATH)
    try:
        data = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise PullError(f"Downloaded manifest.json is invalid JSON: {exc}") from exc
    if data.get("version") != MANIFEST_VERSION or not isinstance(data.get("bundles"), list):
        raise PullError(f"manifest.json must contain version: {MANIFEST_VERSION} and a bundles list")
    return data


def verify(path: Path, expected_size: int | None, expected_sha256: str | None, label: str) -> None:
    if expected_size is not None and path.stat().st_size != expected_size:
        raise PullError(
            f"{label} size mismatch: got {path.stat().st_size}, expected {expected_size}"
        )
    if expected_sha256 and sha256_file(path) != expected_sha256:
        raise PullError(f"{label} sha256 mismatch")


def safe_extract_zip(zip_path: Path, destination: Path, *, clean: bool) -> None:
    if clean and destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True, exist_ok=True)
    root = destination.resolve()

    with zipfile.ZipFile(zip_path) as archive:
        for member in archive.infolist():
            member_path = Path(member.filename)
            if member_path.is_absolute() or ".." in member_path.parts:
                raise PullError(f"Unsafe zip entry in {zip_path.name}: {member.filename}")
            target = (destination / member_path).resolve()
            try:
                target.relative_to(root)
            except ValueError:
                raise PullError(f"Unsafe zip entry in {zip_path.name}: {member.filename}")
        archive.extractall(destination)


def apply_bundle(bundle: dict, base_url: str) -> None:
    name = bundle.get("name")
    if not isinstance(name, str):
        raise PullError("manifest entry without a name")
    bundle_dir = ROMS_DIR / name

    archive_key = bundle.get("archive_path")
    if not isinstance(archive_key, str) or not archive_key:
        raise PullError(f"bundle {name} has no archive_path")
    archive_local = DATA_DIR / archive_key
    print(f"pull {archive_key}", flush=True)
    download_object(base_url, archive_key, archive_local)
    verify(archive_local, bundle.get("archive_size"), bundle.get("archive_sha256"), archive_key)
    safe_extract_zip(archive_local, bundle_dir, clean=False)

    cerf_json = bundle.get("cerf_json")
    if cerf_json is not None:
        (bundle_dir / "cerf.json").write_text(
            json.dumps(cerf_json, indent=2) + "\n", encoding="utf-8"
        )

    for category, entries in (bundle.get("additional_packages") or {}).items():
        for package in entries or []:
            package_key = package.get("archive_path") if isinstance(package, dict) else None
            if not isinstance(package_key, str) or not package_key:
                raise PullError(
                    f"bundle {name} has an additional package in {category!r} without archive_path"
                )
            package_local = DATA_DIR / package_key
            print(f"pull {package_key}", flush=True)
            download_object(base_url, package_key, package_local)
            verify(package_local, package.get("archive_size"), package.get("archive_sha256"), package_key)
            directory = package.get("directory")
            if directory is not None:
                if not isinstance(directory, str) or not directory or Path(directory).name != directory:
                    raise PullError(
                        f"bundle {name} package {package_key} has an unsafe directory {directory!r}"
                    )
                safe_extract_zip(package_local, bundle_dir / directory, clean=True)
            else:
                safe_extract_zip(package_local, bundle_dir, clean=False)


def clean_input_tree() -> None:
    if ROMS_DIR.exists():
        shutil.rmtree(ROMS_DIR)
    if MANIFEST_PATH.exists():
        MANIFEST_PATH.unlink()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Pull CERF ROM source data from R2")
    return parser.parse_args()


def main() -> int:
    parse_args()
    base_url = public_base_url(load_config())
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    clean_input_tree()
    manifest = load_manifest(base_url)
    for bundle in sorted(manifest["bundles"], key=lambda b: b.get("name", "")):
        apply_bundle(bundle, base_url)
    print(f"Pulled {len(manifest['bundles'])} bundle(s) into data/.", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PullError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
