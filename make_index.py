#!/usr/bin/env python3

from __future__ import annotations

import html
import json
import re
import sys
from pathlib import Path
from typing import Any

from push import load_config

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
MANIFEST_PATH = DATA_DIR / "manifest.json"
DB_PATH = DATA_DIR / "db.json"
TEMPLATE_PATH = ROOT / "index.html"
EXAMPLE_PATH = ROOT / "index-example.html"
INDEX_PATH = DATA_DIR / "index.html"
CARDS_PLACEHOLDER = "%ROM_CARDS%"
RUNNING_LEVELS = ("bootable_full", "bootable_partial")
LAST_TIER_PREFIX = "device emulator"
SEP = " · "
SUPPORT_CLAUSES = {
    "bootable_full": (("ROM is bootable", "good"), ("Full hardware coverage", "good")),
    "bootable_partial": (("ROM is bootable", "good"), ("Only shared hardware support", "caution")),
    "unknown_partial": (("No ROM ever tested", "caution"), ("Only shared hardware support", "caution")),
    "no_support": (("Unbootable", "bad"), ("No hardware support", "bad")),
}
MISSING_BOARD = (("Board not in the CERF database", "bad"),)
CHIP_SATURATION = 55
CHIP_LIGHTNESS = 30
CHIP_ALPHA = 0.9


class MakeIndexError(RuntimeError):
    pass


def _str(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def rel(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def sort_text(value: Any) -> str:
    return " ".join(value.casefold().split()) if isinstance(value, str) else ""


def format_size(n: Any) -> str:
    n = _int(n)
    if n <= 0:
        return ""
    if n >= 1024 ** 3:
        return f"{n / 1024 ** 3:.1f} GB"
    if n >= 1024 ** 2:
        return f"{n / 1024 ** 2:.1f} MB"
    if n >= 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n} B"


def chip_color(text: str) -> str:
    value = 0
    units = text.encode("utf-16-le")
    for i in range(0, len(units), 2):
        value = (value * 31 + int.from_bytes(units[i:i + 2], "little")) & 0xFFFFFFFF
    return f"hsla({value % 360}, {CHIP_SATURATION}%, {CHIP_LIGHTNESS}%, {CHIP_ALPHA})"


def os_name_has_version(name: str, major: int, minor: int) -> bool:
    if not name or not (major or minor):
        return False
    if re.search(rf"(?<![\d.]){major}\.{minor}(?:\.\d+)*(?!\d)", name):
        return True
    if minor == 0:
        return bool(re.search(rf"(?<![\d.]){major}(?![\d.])", name))
    return False


def read_json(path: Path, hint: str) -> dict:
    if not path.is_file():
        raise MakeIndexError(f"{rel(path)} is missing. {hint}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise MakeIndexError(f"{rel(path)} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise MakeIndexError(f"{rel(path)} is not a JSON object")
    return data


def template_path() -> Path:
    return TEMPLATE_PATH if TEMPLATE_PATH.is_file() else EXAMPLE_PATH


def read_template(path: Path) -> str:
    if not path.is_file():
        raise MakeIndexError(f"{rel(path)} is missing.")
    template = path.read_text(encoding="utf-8")
    if CARDS_PLACEHOLDER not in template:
        raise MakeIndexError(f"{rel(path)} has no {CARDS_PLACEHOLDER} placeholder.")
    return template


class BoardDb:
    def __init__(self, db: dict):
        def by_id(table: str) -> dict[str, dict]:
            rows = db.get(table)
            if not isinstance(rows, list):
                return {}
            return {r["id"]: r for r in rows if isinstance(r, dict) and isinstance(r.get("id"), str)}

        self.soc_families = by_id("soc_families")
        self.socs = by_id("socs")
        self.families = by_id("board_families")
        self.boards: dict[str, dict] = {}
        for board_id, board in by_id("boards").items():
            family = self.families.get(board.get("board_family_id", ""), {})
            effective = {k: v for k, v in family.items() if k not in ("id", "name")}
            effective.update(board)
            self.boards[board_id] = effective

    def family_name(self, board: dict) -> str:
        return _str(self.families.get(board.get("board_family_id", ""), {}).get("name"))

    def soc_label(self, board: dict) -> str:
        soc = self.socs.get(board.get("soc_id", ""))
        family = self.soc_families.get(soc.get("family_id", "")) if soc else None
        if soc is None or family is None:
            return ""
        return f"{_str(soc.get('name'))} ({_str(family.get('name'))})"


class Rom:
    def __init__(self, bundle: dict, db: BoardDb, base_url: str):
        cerf = _dict(bundle.get("cerf_json"))
        meta = _dict(cerf.get("meta"))
        os_block = _dict(meta.get("os"))
        source = _dict(meta.get("source"))

        self.bundle_name = _str(bundle.get("name"))
        self.size = format_size(bundle.get("unpacked_size"))
        self.name = _str(meta.get("name"))
        self.device_name = _str(meta.get("device_name"))
        self.board_id = _str(_dict(cerf.get("board")).get("id"))
        self.os_name = _str(os_block.get("name"))
        self.os_major = _int(os_block.get("ver_major"))
        self.os_minor = _int(os_block.get("ver_minor"))
        self.os_build = _int(os_block.get("ver_build"))
        self.os_language = _str(os_block.get("language"))
        self.os_year = _int(os_block.get("year"))
        notes = os_block.get("notes")
        self.os_notes = [n.strip() for n in notes if isinstance(n, str) and n.strip()] if isinstance(notes, list) else []
        self.source_name = _str(source.get("name"))
        self.source_website = _str(source.get("website"))
        self.source_is_community_member = source.get("is_community_member") is True

        self.board = db.boards.get(self.board_id)
        board = self.board or {}
        self.board_name = _str(board.get("name"))
        self.family = db.family_name(board) if self.board else ""
        self.soc = db.soc_label(board) if self.board else ""
        self.level = _str(board.get("support_level"))
        self.device_year = _int(board.get("device_year"))
        self.display = self.device_name or self.board_name or self.bundle_name
        self.group = f"{self.display} ({self.device_year})" if self.device_year else self.display

        self.downloads = [("rom.zip", base_url + _str(bundle.get("archive_path")), format_size(bundle.get("archive_size")))]
        for entries in _dict(bundle.get("additional_packages")).values():
            for entry in entries if isinstance(entries, list) else []:
                if isinstance(entry, dict) and _str(entry.get("archive_path")):
                    label = _str(entry.get("name")) or _str(entry.get("file")) or _str(entry.get("directory"))
                    self.downloads.append((label, base_url + entry["archive_path"], format_size(entry.get("archive_size"))))

        self.haystack = sort_text(" ".join([
            self.bundle_name, self.name, self.group, self.board_id, self.board_name, self.family, self.soc,
            self.os_name, self.os_language, self.ce_version(), str(self.os_year or ""),
            _str(meta.get("description")), *self.os_notes,
        ]))

    def supported(self) -> bool:
        return self.level in RUNNING_LEVELS

    def os_title(self) -> str:
        edition = self.os_name or self.group
        return f"{edition}{SEP}{self.os_language}" if self.os_language else edition

    def ce_version(self) -> str:
        if not (self.os_major or self.os_minor):
            return ""
        if os_name_has_version(self.os_name, self.os_major, self.os_minor):
            return ""
        ver = f"CE {self.os_major}.{self.os_minor}"
        if self.os_build:
            ver += f".{self.os_build}"
        return ver

    def heading(self, collide: bool) -> str:
        if self.name:
            return self.name
        title = self.os_title()
        if collide and self.ce_version():
            title += SEP + self.ce_version()
        return SEP.join([title] + self.os_notes)

    def detail(self, collide: bool) -> str:
        parts = []
        if not collide and self.ce_version():
            parts.append(self.ce_version())
        if self.os_year:
            parts.append(str(self.os_year))
        if self.soc:
            parts.append(self.soc)
        if self.size:
            parts.append(self.size)
        return SEP.join(parts)

    def source_html(self) -> str:
        if not self.source_name:
            return ""
        e = html.escape
        if re.fullmatch(r"https?://[^\s()<>\"']+", self.source_website):
            name = f'<a href="{e(self.source_website)}" rel="noopener">{e(self.source_name)}</a>'
        else:
            name = e(self.source_name)
        caption = "Contributed by:" if self.source_is_community_member else "Source:"
        return f'<div class="source">{caption} {name}</div>'

    def card_html(self, collide: bool) -> str:
        e = html.escape
        clauses = SUPPORT_CLAUSES.get(self.level, MISSING_BOARD) if self.board else MISSING_BOARD
        support = "".join(f'<span class="clause"><span class="dot {tone}"></span>{e(text)}</span>'
                          for text, tone in clauses)
        family = f'<span class="family">{e(self.family)}</span>' if self.family else ""
        year = f'<span class="year">{self.device_year}</span>' if self.device_year else ""
        detail = self.detail(collide)
        detail_html = f'<div class="detail">{e(detail)}</div>' if detail else ""
        downloads = "".join(
            f'<a class="download" href="{e(url)}">{e(label)}' + (f"<span>{e(size)}</span>" if size else "") + "</a>"
            for label, url, size in self.downloads
        )
        return (
            f'<article class="card" data-support="{"supported" if self.supported() else "unsupported"}" '
            f'data-bundle="{e(self.bundle_name)}" data-search="{e(self.haystack)}" '
            f'style="--tint: {chip_color(self.family or self.display)}">'
            f'<div class="head"><div class="title"><span class="name">{e(self.display)}</span>{family}</div>{year}</div>'
            f'<div class="rom">{e(self.heading(collide))}</div>'
            f"{detail_html}"
            f'<div class="support">{support}</div>'
            f"{self.source_html()}"
            f'<div class="downloads">{downloads}</div>'
            "</article>\n"
        )

    def sort_key(self) -> tuple:
        group_name = sort_text(self.board_name)
        tier = 0 if not group_name else 2 if group_name.startswith(LAST_TIER_PREFIX) else 1
        missing = not (self.os_major or self.os_minor)
        return (
            (tier, group_name),
            sort_text(self.group),
            sort_text(self.device_name or self.bundle_name),
            (not self.os_name, sort_text(self.os_name)),
            (missing, self.os_major),
            (missing, self.os_minor),
            sort_text(self.bundle_name),
        )


def main() -> int:
    config = load_config()
    repository_url = _str(config.get("REPOSITORY_URL"))
    base_url = _str(config.get("R2_PUBLIC_BASE_URL")).rstrip("/")
    if not repository_url:
        raise MakeIndexError("REPOSITORY_URL is missing. Put it in .r2_token.")
    if not base_url:
        raise MakeIndexError("R2_PUBLIC_BASE_URL is missing. Put it in .r2_token.")

    source = template_path()
    template = read_template(source)
    manifest = read_json(MANIFEST_PATH, "Run 'python make_bundles.py' first.")
    db = BoardDb(read_json(DB_PATH, "Run 'python gen_db.py' first."))
    bundles = manifest.get("bundles")
    roms = sorted((Rom(b, db, base_url + "/") for b in bundles if isinstance(b, dict)) if isinstance(bundles, list) else [],
                  key=Rom.sort_key)

    title_counts: dict[tuple[str, str], int] = {}
    for rom in roms:
        key = (rom.group, rom.os_title())
        title_counts[key] = title_counts.get(key, 0) + 1
    dates = [_str(b.get("updated_at"))[:10] for b in bundles if isinstance(b, dict)] if isinstance(bundles, list) else []
    values = {
        "%REPOSITORY_URL%": html.escape(repository_url),
        "%ROM_COUNT%": str(len(roms)),
        "%UPDATED%": html.escape(max((d for d in dates if d), default="")),
        "%ABUSE_EMAIL%": html.escape(_str(manifest.get("abuse_email"))),
        CARDS_PLACEHOLDER: "".join(rom.card_html(title_counts[(rom.group, rom.os_title())] > 1) for rom in roms),
    }
    page = re.sub("|".join(re.escape(k) for k in values), lambda m: values[m.group(0)], template)
    INDEX_PATH.write_text(page, encoding="utf-8")
    supported = sum(rom.supported() for rom in roms)
    print(f"Wrote {rel(INDEX_PATH)} from {rel(source)}: {len(roms)} ROMs, {supported} supported.", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except MakeIndexError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
