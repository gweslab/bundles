#!/usr/bin/env python3

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import hmac
import http.client
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from pathlib import Path

from make_bundles import MANIFEST_VERSION


ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
ROMS_DIR = DATA_DIR / "roms"
MANIFEST_PATH = DATA_DIR / "manifest.json"
INDEX_PATH = DATA_DIR / "index.html"
TOKEN_PATH = ROOT / ".r2_token"
UPLOAD_PREFIXES = ("roms/",)
UPLOAD_FILES = ("manifest.json", "index.html")
PUBLIC_USER_AGENT = "CERF-Bundles/1.0"
REQUEST_TIMEOUT = 60
REQUEST_ATTEMPTS = 5
RETRY_STATUS = (429, 500, 502, 503, 504)
UPLOAD_CHUNK = 64 * 1024
RATE_WINDOW = 30
DEFAULT_MIN_UPLOAD_KBPS = 128


class PushError(RuntimeError):
    pass


class RetryableError(PushError):
    pass


def paced_body(body: bytes, min_rate: int) -> Iterator[memoryview]:
    view = memoryview(body)
    window_start = time.monotonic()
    window_bytes = 0
    for offset in range(0, len(view), UPLOAD_CHUNK):
        chunk = view[offset:offset + UPLOAD_CHUNK]
        yield chunk
        window_bytes += len(chunk)
        now = time.monotonic()
        elapsed = now - window_start
        if elapsed >= RATE_WINDOW:
            rate = window_bytes / elapsed
            if min_rate and rate < min_rate:
                raise TimeoutError(
                    f"upload slower than {min_rate // 1024} KB/s "
                    f"({rate / 1024:.0f} KB/s over {elapsed:.0f}s)"
                )
            window_start, window_bytes = now, 0


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


def require_config(config: dict[str, str], *keys: str) -> None:
    missing = [key for key in keys if not config.get(key)]
    if missing:
        raise PushError(
            "Missing R2 config value(s): "
            + ", ".join(missing)
            + ". Put them in .r2_token or environment variables."
        )


def normalize_prefix(prefix: str | None) -> str:
    if not prefix:
        return ""
    return prefix.strip().strip("/")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def data_rel(path: Path) -> str:
    return path.relative_to(DATA_DIR).as_posix()


def load_manifest() -> dict:
    if not MANIFEST_PATH.exists():
        raise PushError("data/manifest.json is missing; run make_bundles.py first")
    try:
        data = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise PushError(f"manifest.json is not valid JSON: {exc}") from exc
    if data.get("version") != MANIFEST_VERSION:
        raise PushError(f"manifest.json must have version: {MANIFEST_VERSION}")
    if not isinstance(data.get("bundles"), list):
        raise PushError("manifest.json must contain a bundles list")
    return data


def archive_hashes(manifest: dict) -> dict[str, str]:
    result: dict[str, str] = {}
    for bundle in manifest.get("bundles", []):
        path, digest = bundle.get("archive_path"), bundle.get("archive_sha256")
        if isinstance(path, str) and isinstance(digest, str):
            result[path] = digest
        for entries in (bundle.get("additional_packages") or {}).values():
            for package in entries or []:
                if not isinstance(package, dict):
                    continue
                path, digest = package.get("archive_path"), package.get("archive_sha256")
                if isinstance(path, str) and isinstance(digest, str):
                    result[path] = digest
    return result


def upload_items_from_manifest(manifest: dict) -> list[tuple[str, Path]]:
    items: list[tuple[str, Path]] = []
    for bundle in manifest["bundles"]:
        archive_path = bundle.get("archive_path")
        if not isinstance(archive_path, str) or not archive_path:
            raise PushError(f"bundle {bundle.get('name')!r} is missing archive_path")
        local_archive = DATA_DIR / archive_path
        if not local_archive.is_file():
            raise PushError(
                f"manifest references {archive_path} but the file is missing locally"
            )
        items.append((archive_path, local_archive))

        for category, entries in (bundle.get("additional_packages") or {}).items():
            for package in entries or []:
                package_path = package.get("archive_path") if isinstance(package, dict) else None
                if not isinstance(package_path, str) or not package_path:
                    raise PushError(
                        f"bundle {bundle.get('name')!r} has an additional package in "
                        f"{category!r} without archive_path"
                    )
                local_package = DATA_DIR / package_path
                if not local_package.is_file():
                    raise PushError(
                        f"manifest references {package_path} but the file is missing locally"
                    )
                items.append((package_path, local_package))

    if INDEX_PATH.is_file():
        items.append(("index.html", INDEX_PATH))
    items.append(("manifest.json", MANIFEST_PATH))
    return items


class R2Client:
    def __init__(self, config: dict[str, str]) -> None:
        require_config(config, "R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET")
        self.account_id = config["R2_ACCOUNT_ID"]
        self.access_key = config["R2_ACCESS_KEY_ID"]
        self.secret_key = config["R2_SECRET_ACCESS_KEY"]
        self.bucket = config["R2_BUCKET"]
        self.region = config.get("R2_REGION", "auto")
        self.endpoint = config.get(
            "R2_ENDPOINT",
            f"https://{self.account_id}.r2.cloudflarestorage.com",
        ).rstrip("/")
        self.prefix = normalize_prefix(config.get("R2_PREFIX"))
        try:
            self.min_upload_rate = int(config.get("R2_MIN_UPLOAD_KBPS", DEFAULT_MIN_UPLOAD_KBPS)) * 1024
        except ValueError as exc:
            raise PushError("R2_MIN_UPLOAD_KBPS must be a whole number") from exc
        parsed = urllib.parse.urlparse(self.endpoint)
        if parsed.scheme != "https" or not parsed.netloc:
            raise PushError("R2_ENDPOINT must be an https URL")
        self.host = parsed.netloc

    def key(self, relative_path: str) -> str:
        if self.prefix:
            return f"{self.prefix}/{relative_path}"
        return relative_path

    def _signing_key(self, datestamp: str) -> bytes:
        key_date = hmac.new(("AWS4" + self.secret_key).encode(), datestamp.encode(), hashlib.sha256).digest()
        key_region = hmac.new(key_date, self.region.encode(), hashlib.sha256).digest()
        key_service = hmac.new(key_region, b"s3", hashlib.sha256).digest()
        return hmac.new(key_service, b"aws4_request", hashlib.sha256).digest()

    def _request(
        self,
        method: str,
        key: str = "",
        query: dict[str, str] | None = None,
        body: bytes = b"",
        headers: dict[str, str] | None = None,
    ) -> bytes:
        for attempt in range(1, REQUEST_ATTEMPTS + 1):
            try:
                return self._request_once(method, key, query, body, headers)
            except RetryableError as exc:
                if attempt == REQUEST_ATTEMPTS:
                    raise
                delay = 2 ** attempt
                print(f"    retry {attempt + 1}/{REQUEST_ATTEMPTS} in {delay}s: {exc}", flush=True)
                time.sleep(delay)
        raise AssertionError("unreachable")

    def _request_once(
        self,
        method: str,
        key: str,
        query: dict[str, str] | None,
        body: bytes,
        headers: dict[str, str] | None,
    ) -> bytes:
        query = query or {}
        headers = {k.lower(): v for k, v in (headers or {}).items()}
        now = dt.datetime.now(dt.timezone.utc)
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        datestamp = now.strftime("%Y%m%d")
        payload_hash = sha256_bytes(body)

        canonical_uri = "/" + urllib.parse.quote(self.bucket, safe="")
        if key:
            canonical_uri += "/" + urllib.parse.quote(key, safe="/")
        canonical_query = "&".join(
            f"{urllib.parse.quote(k, safe='')}={urllib.parse.quote(str(v), safe='-_.~')}"
            for k, v in sorted(query.items())
        )

        headers.update({
            "host": self.host,
            "x-amz-content-sha256": payload_hash,
            "x-amz-date": amz_date,
        })
        canonical_headers = "".join(f"{k}:{headers[k].strip()}\n" for k in sorted(headers))
        signed_headers = ";".join(sorted(headers))
        canonical_request = "\n".join([
            method,
            canonical_uri,
            canonical_query,
            canonical_headers,
            signed_headers,
            payload_hash,
        ])
        credential_scope = f"{datestamp}/{self.region}/s3/aws4_request"
        string_to_sign = "\n".join([
            "AWS4-HMAC-SHA256",
            amz_date,
            credential_scope,
            sha256_bytes(canonical_request.encode()),
        ])
        signature = hmac.new(self._signing_key(datestamp), string_to_sign.encode(), hashlib.sha256).hexdigest()
        headers["authorization"] = (
            "AWS4-HMAC-SHA256 "
            f"Credential={self.access_key}/{credential_scope}, "
            f"SignedHeaders={signed_headers}, "
            f"Signature={signature}"
        )

        url = f"{self.endpoint}{canonical_uri}"
        if canonical_query:
            url += f"?{canonical_query}"

        if method in ("GET", "HEAD"):
            data = None
        elif body:
            data = paced_body(body, self.min_upload_rate)
        else:
            data = body
        request = urllib.request.Request(url, data=data, method=method)
        for key_name, value in headers.items():
            request.add_header(key_name, value)

        label = f"R2 {method} {key or self.bucket} failed"
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            error_type = RetryableError if exc.code in RETRY_STATUS else PushError
            raise error_type(f"{label}: HTTP {exc.code} {detail}") from exc
        except urllib.error.URLError as exc:
            raise RetryableError(f"{label}: {exc.reason}") from exc
        except (OSError, http.client.HTTPException) as exc:
            raise RetryableError(f"{label}: {exc!r}") from exc

    def list_keys(self) -> list[str]:
        keys: list[str] = []
        token = ""
        while True:
            query = {"list-type": "2"}
            if self.prefix:
                query["prefix"] = self.prefix + "/"
            if token:
                query["continuation-token"] = token
            payload = self._request("GET", query=query)
            root = ET.fromstring(payload)
            for contents in child_nodes(root, "Contents"):
                key_node = first_child(contents, "Key")
                if key_node is not None and key_node.text:
                    keys.append(key_node.text)
            truncated_node = first_child(root, "IsTruncated")
            truncated = truncated_node.text if truncated_node is not None and truncated_node.text else "false"
            if truncated.lower() != "true":
                break
            token_node = first_child(root, "NextContinuationToken")
            token = token_node.text if token_node is not None and token_node.text else ""
            if not token:
                break
        return keys

    def get_optional(self, relative_path: str) -> bytes | None:
        try:
            return self._request("GET", key=self.key(relative_path))
        except PushError as exc:
            if "HTTP 404" in str(exc):
                return None
            raise

    def put_file(self, relative_path: str, path: Path) -> None:
        body = path.read_bytes()
        suffix = path.suffix.lower()
        if suffix == ".zip":
            content_type = "application/zip"
        elif suffix == ".json":
            content_type = "application/json"
        elif suffix == ".html":
            content_type = "text/html; charset=utf-8"
        else:
            content_type = "application/octet-stream"
        headers = {
            "content-length": str(len(body)),
            "content-type": content_type,
        }
        self._request("PUT", key=self.key(relative_path), body=body, headers=headers)

    def delete_key(self, key: str) -> None:
        self._request("DELETE", key=key)


def xml_name(node: ET.Element) -> str:
    return node.tag.rsplit("}", 1)[-1]


def child_nodes(node: ET.Element, name: str) -> list[ET.Element]:
    return [child for child in list(node) if xml_name(child) == name]


def first_child(node: ET.Element, name: str) -> ET.Element | None:
    for child in list(node):
        if xml_name(child) == name:
            return child
    return None


def is_managed_key(client: R2Client, key: str) -> bool:
    relative = key
    if client.prefix:
        prefix = client.prefix + "/"
        if not key.startswith(prefix):
            return False
        relative = key[len(prefix):]
    return relative in UPLOAD_FILES or any(relative.startswith(p) for p in UPLOAD_PREFIXES)


def desired_keys(client: R2Client, items: list[tuple[str, Path]]) -> set[str]:
    return {client.key(rel) for rel, _ in items}


def remote_archive_hashes(client: R2Client) -> dict[str, str]:
    raw = client.get_optional("manifest.json")
    if raw is None:
        return {}
    try:
        return archive_hashes(json.loads(raw))
    except json.JSONDecodeError:
        return {}


def publish(client: R2Client, manifest: dict, dry_run: bool, force: bool) -> None:
    items = upload_items_from_manifest(manifest)
    desired = desired_keys(client, items)
    existing = set(client.list_keys())
    stale = sorted(key for key in existing if is_managed_key(client, key) and key not in desired)

    local = archive_hashes(manifest)
    remote = {} if force else remote_archive_hashes(client)
    upload: list[tuple[str, Path]] = []
    skipped = 0
    for relative_path, path in items:
        if (
            not force
            and relative_path not in UPLOAD_FILES
            and local.get(relative_path)
            and local.get(relative_path) == remote.get(relative_path)
            and client.key(relative_path) in existing
        ):
            skipped += 1
            continue
        upload.append((relative_path, path))

    if dry_run:
        print(f"Would upload {len(upload)} object(s); skip {skipped} unchanged.")
        print(f"Would delete {len(stale)} stale object(s).")
        return

    for relative_path, path in upload:
        print(f"put {relative_path}", flush=True)
        client.put_file(relative_path, path)

    for key in stale:
        print(f"delete {key}", flush=True)
        client.delete_key(key)

    print(
        f"Published {len(upload)} object(s); skipped {skipped} unchanged; "
        f"deleted {len(stale)} stale object(s).",
        flush=True,
    )


def public_url(config: dict[str, str], relative_path: str) -> str:
    base = config.get("R2_PUBLIC_BASE_URL", "").rstrip("/")
    if not base:
        raise PushError("R2_PUBLIC_BASE_URL is missing; cannot validate public downloads")
    return f"{base}/{urllib.parse.quote(relative_path, safe='/')}"


def check_r2(config: dict[str, str]) -> None:
    client = R2Client(config)
    keys = client.list_keys()
    print(f"list=ok ({len(keys)} object(s))")

    relative = "__cerf_r2_probe.txt"
    remote_key = client.key(relative)
    body = b"cerf r2 probe\n"
    try:
        client._request(
            "PUT",
            key=remote_key,
            body=body,
            headers={"content-length": str(len(body)), "content-type": "text/plain"},
        )
        print("signed_put=ok")

        signed = client._request("GET", key=remote_key)
        if signed != body:
            raise PushError("signed GET probe returned unexpected bytes")
        print("signed_get=ok")

        url = public_url(config, relative)
        try:
            request = urllib.request.Request(url, headers={"User-Agent": PUBLIC_USER_AGENT})
            with urllib.request.urlopen(request, timeout=30) as response:
                public = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace").strip()
            raise PushError(
                f"public GET probe failed: HTTP {exc.code} for {url}"
                + (f" ({detail[:300]})" if detail else "")
            ) from exc
        except urllib.error.URLError as exc:
            raise PushError(f"public GET probe failed: {exc.reason} for {url}") from exc
        if public != body:
            raise PushError("public GET probe returned unexpected bytes")
        print("public_get=ok")
    finally:
        client.delete_key(remote_key)
        print("delete=ok")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Publish generated CERF bundle data to R2")
    parser.add_argument("--check", action="store_true", help="Validate R2 list, signed write/read, public read, and delete.")
    parser.add_argument("--dry-run", action="store_true", help="Read manifest and show upload/delete counts only.")
    parser.add_argument("--force", action="store_true", help="Re-upload every archive, ignoring the published-hash diff.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = load_config()
    if args.check:
        check_r2(config)
        return 0

    if not ROMS_DIR.exists() or not any(p.is_dir() for p in ROMS_DIR.iterdir()):
        raise PushError("data/roms/ is missing or empty. Run 'python pull.py' first or add ROMs locally.")
    manifest = load_manifest()
    client = R2Client(config)
    publish(client, manifest, args.dry_run, args.force)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PushError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
