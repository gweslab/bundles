#!/usr/bin/env python3

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import io
import json
import os
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
ROMS_DIR = DATA_DIR / "roms"
MANIFEST_PATH = DATA_DIR / "manifest.json"
TOKEN_PATH = ROOT / ".r2_token"
MANIFEST_VERSION = 2
ROM_ZIP_BLOCKS = ("rom", "storage")
ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
BUILD_CACHE_NAME = ".build_cache.json"


class BundleError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def data_rel(path: Path) -> str:
    try:
        return path.relative_to(DATA_DIR).as_posix()
    except ValueError:
        return path.relative_to(ROOT).as_posix()


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def is_timestamp(value: object) -> bool:
    return isinstance(value, str) and "T" in value


def load_manifest() -> dict:
    empty = {"version": MANIFEST_VERSION, "bundles": []}
    if not MANIFEST_PATH.exists():
        return empty
    try:
        data = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return empty
    if data.get("version") != MANIFEST_VERSION or not isinstance(data.get("bundles"), list):
        return empty
    return data


def ensure_roms_ready() -> None:
    if not ROMS_DIR.exists() or not any(p.is_dir() for p in ROMS_DIR.iterdir()):
        raise BundleError(
            "data/roms/ is missing or empty. Run 'python pull.py' first "
            "to fetch the current ROM dataset from R2."
        )


def iter_bundle_dirs(selected: set[str] | None) -> Iterable[Path]:
    ensure_roms_ready()
    for rom_dir in sorted(p for p in ROMS_DIR.iterdir() if p.is_dir()):
        if selected and rom_dir.name not in selected:
            continue
        yield rom_dir


def rom_files_from_cerf(rom_dir: Path, cerf_json: dict | None) -> list[Path]:
    if not cerf_json:
        raise BundleError(f"{rom_dir.name}: cerf.json is required to select ROM files")
    rom = cerf_json.get("rom")
    storage = cerf_json.get("storage")
    has_primary = isinstance(rom, dict) and bool(rom.get("primary"))
    has_storage = isinstance(storage, dict) and any(storage.values())
    if not has_primary and not has_storage:
        raise BundleError(f"{rom_dir.name}: cerf.json names neither rom.primary nor a storage file")

    names: list[tuple[str, str]] = []
    for block_name in ROM_ZIP_BLOCKS:
        block = cerf_json.get(block_name)
        if block is None:
            continue
        if not isinstance(block, dict):
            raise BundleError(f"{rom_dir.name}: {block_name} must be an object")
        for key, value in block.items():
            label = f"{block_name}.{key}"
            if isinstance(value, str):
                names.append((label, value))
            elif isinstance(value, list) and all(isinstance(x, str) for x in value):
                names.extend((label, x) for x in value)
            else:
                raise BundleError(
                    f"{rom_dir.name}: {label} must be a string or a list of strings"
                )

    seen: set[str] = set()
    paths: list[Path] = []
    for label, fname in names:
        if fname in seen:
            continue
        seen.add(fname)
        path = rom_dir / fname
        if not path.is_file():
            raise BundleError(f"{rom_dir.name}: {label} references missing file {fname!r}")
        paths.append(path)
    return paths


def write_zip(zip_path: Path, members: list[tuple[str, Path]]) -> None:
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for archive_name, file_path in members:
            info = zipfile.ZipInfo(archive_name, ZIP_TIMESTAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            with file_path.open("rb") as handle:
                archive.writestr(info, handle.read())


def unpacked_size(members: list[tuple[str, Path]]) -> int:
    return sum(path.stat().st_size for _, path in members)


def source_signature(members: list[tuple[str, Path]]) -> list[list]:
    return [[name, st.st_size, st.st_mtime_ns]
            for name, path in members
            for st in (path.stat(),)]


def load_build_cache(rom_dir: Path) -> dict:
    path = rom_dir / BUILD_CACHE_NAME
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def save_build_cache(rom_dir: Path, cache: dict) -> None:
    (rom_dir / BUILD_CACHE_NAME).write_text(
        json.dumps(cache, indent=2) + "\n", encoding="utf-8"
    )


def build_archive(
    zip_path: Path, members: list[tuple[str, Path]], cache: dict, force: bool
) -> dict:
    key = zip_path.name
    sig = source_signature(members)
    cached = cache.get(key)
    if (
        not force
        and isinstance(cached, dict)
        and cached.get("sig") == sig
        and isinstance(cached.get("archive_sha256"), str)
        and zip_path.exists()
    ):
        return {
            "archive_path": data_rel(zip_path),
            "archive_sha256": cached["archive_sha256"],
            "archive_size": cached["archive_size"],
            "unpacked_size": cached["unpacked_size"],
            "reused": True,
        }
    write_zip(zip_path, members)
    meta = {
        "archive_path": data_rel(zip_path),
        "archive_sha256": sha256_file(zip_path),
        "archive_size": zip_path.stat().st_size,
        "unpacked_size": unpacked_size(members),
        "reused": False,
    }
    cache[key] = {
        "sig": sig,
        "archive_sha256": meta["archive_sha256"],
        "archive_size": meta["archive_size"],
        "unpacked_size": meta["unpacked_size"],
    }
    return meta


def pack_rom_archive(
    rom_binaries: list[Path], rom_archive_path: Path, cache: dict, force: bool
) -> dict:
    members: list[tuple[str, Path]] = sorted((b.name, b) for b in rom_binaries)
    return build_archive(rom_archive_path, members, cache, force)


def read_cerf_json(rom_dir: Path) -> dict | None:
    cerf_json_path = rom_dir / "cerf.json"
    if not cerf_json_path.exists():
        return None
    try:
        return json.loads(cerf_json_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise BundleError(f"{data_rel(cerf_json_path)} is not valid JSON: {exc}") from exc


def package_source(entry: dict) -> str:
    return entry["file"] if entry.get("file") is not None else entry["directory"]


def validate_additional_packages(rom_dir: Path, packages: dict | None) -> None:
    if packages is None:
        return
    label = f"{rom_dir.name} additional_packages"
    if not isinstance(packages, dict):
        raise BundleError(f"{label} must be an object of category -> package list")
    seen_sources: set[str] = set()
    for category, entries in packages.items():
        if not isinstance(entries, list):
            raise BundleError(f"{label}.{category} must be a list")
        for entry in entries:
            if not isinstance(entry, dict):
                raise BundleError(f"{label}.{category} entries must be objects")
            file_name = entry.get("file")
            dir_name = entry.get("directory")
            if (file_name is None) == (dir_name is None):
                raise BundleError(
                    f"{label}.{category} entry must have exactly one of 'file' or 'directory'"
                )
            source = file_name if file_name is not None else dir_name
            kind = "file" if file_name is not None else "directory"
            if not isinstance(source, str) or not source:
                raise BundleError(f"{label}.{category} {kind} must be a non-empty string")
            if Path(source).name != source:
                raise BundleError(f"{label}.{category} {kind} {source!r} must be a plain name")
            if source in seen_sources:
                raise BundleError(f"{label} declares {source!r} more than once")
            seen_sources.add(source)
            if f"{source}.zip" == "rom.zip":
                raise BundleError(
                    f"{label}.{category} {kind} {source!r} collides with the rom.zip archive"
                )
            path = rom_dir / source
            if kind == "file" and not path.is_file():
                raise BundleError(
                    f"{label}.{category} references file {source!r} but "
                    f"{data_rel(path)} does not exist"
                )
            if kind == "directory" and not path.is_dir():
                raise BundleError(
                    f"{label}.{category} references directory {source!r} but "
                    f"{data_rel(path)} is not a directory"
                )


def additional_package_files(packages: dict | None) -> set[str]:
    return {
        entry["file"]
        for entries in (packages or {}).values()
        for entry in entries
        if entry.get("file") is not None
    }


def pack_additional_packages(
    rom_dir: Path, packages: dict | None, cache: dict, force: bool
) -> tuple[dict | None, int, int]:
    if not packages:
        return None, 0, 0
    result: dict[str, list[dict]] = {}
    reused = built = 0
    for category, entries in packages.items():
        enriched: list[dict] = []
        for entry in entries:
            source = package_source(entry)
            if entry.get("file") is not None:
                members = [(source, rom_dir / source)]
            else:
                directory = rom_dir / source
                files = sorted(p for p in directory.rglob("*") if p.is_file())
                members = [(p.relative_to(directory).as_posix(), p) for p in files]
            archive_path = rom_dir / f"{source}.zip"
            meta = build_archive(archive_path, members, cache, force)
            reused, built = (reused + 1, built) if meta["reused"] else (reused, built + 1)
            enriched.append({
                **entry,
                "archive_path": meta["archive_path"],
                "archive_sha256": meta["archive_sha256"],
                "archive_size": meta["archive_size"],
                "unpacked_size": meta["unpacked_size"],
            })
        result[category] = enriched
    return result, reused, built


def prune_stale_package_archives(rom_dir: Path, old_entry: dict, packages_meta: dict | None) -> None:
    current = {
        entry["archive_path"]
        for entries in (packages_meta or {}).values()
        for entry in entries
    }
    stale: list[str] = []
    for entries in (old_entry.get("additional_packages") or {}).values():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if isinstance(entry, dict) and isinstance(entry.get("archive_path"), str):
                stale.append(entry["archive_path"])
    for archive_path in stale:
        if archive_path in current:
            continue
        local = DATA_DIR / archive_path
        if local.parent == rom_dir and local.exists():
            local.unlink()


def manifest_by_name(manifest: dict) -> dict[str, dict]:
    result = {}
    for bundle in manifest.get("bundles", []):
        name = bundle.get("name")
        if isinstance(name, str):
            result[name] = bundle
    return result


def build_bundle(
    rom_dir: Path, previous: dict[str, dict], now: str, force: bool
) -> tuple[dict, str]:
    log = io.StringIO()
    name = rom_dir.name
    log.write(f"==> Building {name}\n")

    rom_archive_path = rom_dir / "rom.zip"
    cache = load_build_cache(rom_dir)

    cerf_json = read_cerf_json(rom_dir)
    packages = cerf_json.get("additional_packages") if cerf_json else None
    validate_additional_packages(rom_dir, packages)
    package_files = additional_package_files(packages)
    rom_binaries = rom_files_from_cerf(rom_dir, cerf_json)
    overlap = {b.name for b in rom_binaries} & package_files
    if overlap:
        raise BundleError(
            f"{name}: file(s) {sorted(overlap)} appear in both rom.zip and additional_packages"
        )
    archive_meta = pack_rom_archive(rom_binaries, rom_archive_path, cache, force)
    packages_meta, pkg_reused, pkg_built = pack_additional_packages(
        rom_dir, packages, cache, force
    )

    log.write(f"    rom.zip {'reused' if archive_meta['reused'] else 'rebuilt'}\n")
    if pkg_reused or pkg_built:
        log.write(f"    packages: {pkg_reused} reused, {pkg_built} rebuilt\n")

    valid_keys = {rom_archive_path.name}
    for entries in (packages_meta or {}).values():
        for entry in entries:
            valid_keys.add(Path(entry["archive_path"]).name)
    save_build_cache(rom_dir, {k: v for k, v in cache.items() if k in valid_keys})

    archive_path = archive_meta["archive_path"]
    archive_sha256 = archive_meta["archive_sha256"]
    archive_size = archive_meta["archive_size"]
    archive_unpacked_size = archive_meta["unpacked_size"]

    old_entry = previous.get(name, {})
    prune_stale_package_archives(rom_dir, old_entry, packages_meta)
    updated_at = old_entry.get("updated_at")
    if (
        old_entry.get("archive_path") != archive_path
        or old_entry.get("archive_sha256") != archive_sha256
        or old_entry.get("additional_packages") != packages_meta
        or old_entry.get("cerf_json") != cerf_json
    ):
        updated_at = now
    if not is_timestamp(updated_at):
        updated_at = now

    entry = {
        "name": name,
        "updated_at": updated_at,
        "archive_path": archive_path,
        "archive_sha256": archive_sha256,
        "archive_size": archive_size,
        "unpacked_size": archive_unpacked_size,
        "additional_packages": packages_meta,
        "cerf_json": cerf_json,
    }
    return entry, log.getvalue()


def abuse_email() -> str:
    value = os.environ.get("ABUSE_EMAIL", "")
    if not value and TOKEN_PATH.exists():
        for raw_line in TOKEN_PATH.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export "):].strip()
            if "=" not in line:
                continue
            key, candidate = line.split("=", 1)
            if key.strip() == "ABUSE_EMAIL":
                value = candidate.strip().strip("'\"")
    return value.strip()


def write_manifest(entries: list[dict]) -> None:
    manifest: dict = {"version": MANIFEST_VERSION}
    email = abuse_email()
    if email:
        manifest["abuse_email"] = email
    manifest["bundles"] = sorted(entries, key=lambda entry: entry["name"])
    MANIFEST_PATH.write_text(
        json.dumps(manifest, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build CERF ROM bundles from data/roms")
    parser.add_argument(
        "bundles",
        nargs="*",
        help="Optional bundle names under data/roms/ to build. Defaults to all bundles.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=None,
        help="Parallel build workers. Defaults to min(CPU count, bundle count). Use 1 for sequential.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-zip every archive, ignoring the per-bundle build cache.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    selected = set(args.bundles) if args.bundles else None
    ensure_roms_ready()
    if selected:
        missing = sorted(name for name in selected if not (ROMS_DIR / name).is_dir())
        if missing:
            raise BundleError(f"Unknown bundle(s): {', '.join(missing)}")

    manifest = load_manifest()
    previous = manifest_by_name(manifest)
    now = utc_timestamp()
    entries: list[dict] = []

    rom_dirs = list(iter_bundle_dirs(selected))
    if rom_dirs:
        requested = args.workers if args.workers and args.workers > 0 else (os.cpu_count() or 1)
        workers = max(1, min(requested, len(rom_dirs)))
        print(
            f"Building {len(rom_dirs)} bundle(s) with {workers} parallel worker(s).",
            flush=True,
        )

        if workers == 1:
            for rom_dir in rom_dirs:
                entry, log = build_bundle(rom_dir, previous, now, args.force)
                sys.stdout.write(log)
                sys.stdout.flush()
                entries.append(entry)
        else:
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
                futures = [pool.submit(build_bundle, rd, previous, now, args.force) for rd in rom_dirs]
                for future in concurrent.futures.as_completed(futures):
                    entry, log = future.result()
                    sys.stdout.write(log)
                    sys.stdout.flush()
                    entries.append(entry)

    if selected:
        selected_names = {entry["name"] for entry in entries}
        for name, old_entry in sorted(previous.items()):
            if name not in selected_names and (ROMS_DIR / name).is_dir():
                if not is_timestamp(old_entry.get("updated_at")):
                    old_entry = {**old_entry, "updated_at": now}
                entries.append(old_entry)

    write_manifest(entries)
    print(f"Updated {data_rel(MANIFEST_PATH)} with {len(entries)} bundle(s).", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except BundleError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
