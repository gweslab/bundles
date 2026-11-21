#!/usr/bin/env python3

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
CORE_DIR = DATA_DIR / ".libCerfCore"
DB_PATH = DATA_DIR / "db.json"
SAD_DIR = ROOT / "sad"
CORE_URL = "https://github.com/gweslab/libCerfCore.git"


class GenDbError(RuntimeError):
    pass


def _make_writable(func, path, _exc) -> None:
    os.chmod(path, stat.S_IWRITE)
    func(path)


def remove_tree(path: Path) -> None:
    if not path.exists():
        return
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=_make_writable)
    else:
        shutil.rmtree(path, onerror=_make_writable)


def git(*args: str) -> str:
    result = subprocess.run(["git", *args], capture_output=True, text=True)
    if result.returncode != 0:
        raise GenDbError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def load_sad():
    if not (SAD_DIR / "sad" / "__init__.py").is_file():
        raise GenDbError("sad/ is empty. Run 'git submodule update --init' first.")
    sys.path.insert(0, str(SAD_DIR))
    import sad
    return sad


def main() -> int:
    sad = load_sad()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    remove_tree(CORE_DIR)
    try:
        git("clone", "--quiet", "--depth", "1", "--filter=blob:none", "--sparse", CORE_URL, str(CORE_DIR))
        git("-C", str(CORE_DIR), "sparse-checkout", "set", "db")
        commit = git("-C", str(CORE_DIR), "rev-parse", "HEAD")
        try:
            db = sad.compile_tree(CORE_DIR / "db", "json")
        except (OSError, sad.SadError) as exc:
            raise GenDbError(f"libCerfCore/db does not compile: {exc}") from exc
    finally:
        remove_tree(CORE_DIR)

    if not isinstance(db, dict) or not isinstance(db.get("boards"), list):
        raise GenDbError("compiled db.json has no boards list")
    tmp = DB_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(db, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(DB_PATH)
    print(f"Compiled {DB_PATH.relative_to(ROOT).as_posix()} at libCerfCore {commit[:10]}: "
          f"{len(db['boards'])} boards.", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except GenDbError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
