#!/usr/bin/env python3

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import hmac
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parent
TOKEN_PATH = ROOT / ".r2_token"
DB_PATH = ROOT / "analytics_db.json"

GRAPHQL_URL = "https://api.cloudflare.com/client/v4/graphql"
PUBLIC_ANALYTICS_KEY = "analytics.json"
DB_VERSION = 1
DEFAULT_FETCH_DAYS = 7
DEFAULT_WINDOW_DAYS = 14
USER_AGENT = "CERF-Analytics/1.0"

GRAPHQL_QUERY = """
query($zone: String!, $since: Time!, $until: Time!, $path: String!) {
  viewer {
    zones(filter: { zoneTag: $zone }) {
      httpRequestsAdaptiveGroups(
        limit: 5000
        filter: {
          datetime_geq: $since
          datetime_leq: $until
          clientRequestPath_like: $path
          edgeResponseStatus_in: [200, 206]
        }
        orderBy: [count_DESC]
      ) {
        count
        dimensions { clientRequestPath }
        avg { sampleInterval }
        sum { edgeResponseBytes }
      }
    }
  }
}
"""


class AnalyticsError(RuntimeError):
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
        if key.startswith("R2_") or key.startswith("CF_"):
            config[key] = value
    return config


def require(config: dict[str, str], *keys: str) -> None:
    missing = [key for key in keys if not config.get(key)]
    if missing:
        raise AnalyticsError(
            "Missing config value(s): "
            + ", ".join(missing)
            + ". Put them in .r2_token or environment variables."
        )


def normalize_prefix(prefix: str | None) -> str:
    if not prefix:
        return ""
    return prefix.strip().strip("/")


def rom_path_pattern(config: dict[str, str]) -> str:
    prefix = normalize_prefix(config.get("R2_PREFIX"))
    lead = f"/{prefix}" if prefix else ""
    return f"{lead}/roms/%rom.zip"


def rom_name_from_path(path: str) -> str | None:
    if "/roms/" not in path or not path.endswith("/rom.zip"):
        return None
    name = path.split("/roms/", 1)[1][: -len("/rom.zip")]
    if not name or "/" in name:
        return None
    return name


def graphql(token: str, variables: dict) -> dict:
    body = json.dumps({"query": GRAPHQL_QUERY, "variables": variables}).encode()
    request = urllib.request.Request(
        GRAPHQL_URL,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise AnalyticsError(f"Cloudflare GraphQL HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise AnalyticsError(f"Cloudflare GraphQL request failed: {exc.reason}") from exc
    return payload


def fetch_day(token: str, zone: str, day: dt.date, path_pattern: str) -> dict[str, dict[str, int]] | None:
    since = f"{day.isoformat()}T00:00:00Z"
    until = f"{(day + dt.timedelta(days=1)).isoformat()}T00:00:00Z"
    payload = graphql(token, {"zone": zone, "since": since, "until": until, "path": path_pattern})

    if payload.get("errors"):
        messages = "; ".join(e.get("message", "") for e in payload["errors"])
        if "older than" in messages:
            return None
        raise AnalyticsError(f"Cloudflare GraphQL error for {day}: {messages}")

    zones = (((payload.get("data") or {}).get("viewer") or {}).get("zones")) or []
    rows = zones[0].get("httpRequestsAdaptiveGroups", []) if zones else []

    result: dict[str, dict[str, int]] = {}
    for row in rows:
        name = rom_name_from_path(row["dimensions"]["clientRequestPath"])
        if name is None:
            continue
        interval = row.get("avg", {}).get("sampleInterval", 1.0) or 1.0
        downloads = round(row["count"] * interval)
        by = round(row.get("sum", {}).get("edgeResponseBytes", 0) * interval)
        bucket = result.setdefault(name, {"downloads": 0, "bytes": 0})
        bucket["downloads"] += downloads
        bucket["bytes"] += by
    return result


def load_db() -> dict:
    if not DB_PATH.exists():
        return {"version": DB_VERSION, "days": {}}
    try:
        data = json.loads(DB_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise AnalyticsError(f"{DB_PATH.name} is not valid JSON: {exc}") from exc
    if not isinstance(data.get("days"), dict):
        raise AnalyticsError(f"{DB_PATH.name} is missing a 'days' object")
    data.setdefault("version", DB_VERSION)
    return data


def save_db(db: dict) -> None:
    tmp = DB_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(db, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(DB_PATH)


def collect(config: dict[str, str], fetch_days: int, *, persist: bool) -> dict:
    require(config, "CF_API_TOKEN", "CF_ZONE_ID")
    token, zone = config["CF_API_TOKEN"], config["CF_ZONE_ID"]
    pattern = rom_path_pattern(config)

    db = load_db()
    today = dt.datetime.now(dt.timezone.utc).date()
    fetched = 0
    for offset in range(1, fetch_days + 1):
        day = today - dt.timedelta(days=offset)
        per_rom = fetch_day(token, zone, day, pattern)
        if per_rom is None:
            continue
        db["days"][day.isoformat()] = per_rom
        total = sum(v["downloads"] for v in per_rom.values())
        print(f"collect {day}: {total} downloads across {len(per_rom)} ROM(s)", flush=True)
        fetched += 1

    if fetched == 0:
        raise AnalyticsError("Cloudflare returned no usable days; check CF_API_TOKEN / CF_ZONE_ID.")
    if persist:
        save_db(db)
    return db


def window_totals(db: dict, window_days: int) -> list[tuple[str, int]]:
    today = dt.datetime.now(dt.timezone.utc).date()
    cutoff = today - dt.timedelta(days=window_days)
    totals: dict[str, int] = defaultdict(int)
    for day_str, per_rom in db.get("days", {}).items():
        try:
            day = dt.date.fromisoformat(day_str)
        except ValueError:
            continue
        if cutoff <= day < today:
            for name, rec in per_rom.items():
                totals[name] += rec.get("downloads", 0)
    return sorted(totals.items(), key=lambda kv: (-kv[1], kv[0]))


def build_public(ranking: list[tuple[str, int]]) -> dict:
    return {"per_rom": [{"name": name, "place": i} for i, (name, _) in enumerate(ranking, 1)]}


class R2Client:
    def __init__(self, config: dict[str, str]) -> None:
        require(config, "R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET")
        self.account_id = config["R2_ACCOUNT_ID"]
        self.access_key = config["R2_ACCESS_KEY_ID"]
        self.secret_key = config["R2_SECRET_ACCESS_KEY"]
        self.bucket = config["R2_BUCKET"]
        self.region = config.get("R2_REGION", "auto")
        self.endpoint = config.get(
            "R2_ENDPOINT", f"https://{self.account_id}.r2.cloudflarestorage.com"
        ).rstrip("/")
        self.prefix = normalize_prefix(config.get("R2_PREFIX"))
        parsed = urllib.parse.urlparse(self.endpoint)
        if parsed.scheme != "https" or not parsed.netloc:
            raise AnalyticsError("R2_ENDPOINT must be an https URL")
        self.host = parsed.netloc

    def key(self, relative_path: str) -> str:
        return f"{self.prefix}/{relative_path}" if self.prefix else relative_path

    def _signing_key(self, datestamp: str) -> bytes:
        key_date = hmac.new(("AWS4" + self.secret_key).encode(), datestamp.encode(), hashlib.sha256).digest()
        key_region = hmac.new(key_date, self.region.encode(), hashlib.sha256).digest()
        key_service = hmac.new(key_region, b"s3", hashlib.sha256).digest()
        return hmac.new(key_service, b"aws4_request", hashlib.sha256).digest()

    def _request(self, method: str, key: str = "", body: bytes = b"", headers: dict[str, str] | None = None) -> bytes:
        headers = {k.lower(): v for k, v in (headers or {}).items()}
        now = dt.datetime.now(dt.timezone.utc)
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        datestamp = now.strftime("%Y%m%d")
        payload_hash = hashlib.sha256(body).hexdigest()

        canonical_uri = "/" + urllib.parse.quote(self.bucket, safe="")
        if key:
            canonical_uri += "/" + urllib.parse.quote(key, safe="/")

        headers.update({"host": self.host, "x-amz-content-sha256": payload_hash, "x-amz-date": amz_date})
        canonical_headers = "".join(f"{k}:{headers[k].strip()}\n" for k in sorted(headers))
        signed_headers = ";".join(sorted(headers))
        canonical_request = "\n".join([method, canonical_uri, "", canonical_headers, signed_headers, payload_hash])
        credential_scope = f"{datestamp}/{self.region}/s3/aws4_request"
        string_to_sign = "\n".join([
            "AWS4-HMAC-SHA256",
            amz_date,
            credential_scope,
            hashlib.sha256(canonical_request.encode()).hexdigest(),
        ])
        signature = hmac.new(self._signing_key(datestamp), string_to_sign.encode(), hashlib.sha256).hexdigest()
        headers["authorization"] = (
            f"AWS4-HMAC-SHA256 Credential={self.access_key}/{credential_scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        )

        request = urllib.request.Request(
            f"{self.endpoint}{canonical_uri}",
            data=body if method not in ("GET", "HEAD") else None,
            method=method,
        )
        for name, value in headers.items():
            request.add_header(name, value)
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise AnalyticsError(f"R2 {method} {key or self.bucket} failed: HTTP {exc.code} {detail}") from exc
        except urllib.error.URLError as exc:
            raise AnalyticsError(f"R2 {method} {key or self.bucket} failed: {exc.reason}") from exc

    def put_json(self, relative_path: str, obj: dict) -> None:
        body = (json.dumps(obj, indent=2, sort_keys=True) + "\n").encode("utf-8")
        self._request(
            "PUT",
            key=self.key(relative_path),
            body=body,
            headers={"content-length": str(len(body)), "content-type": "application/json"},
        )

    def get_optional(self, relative_path: str) -> bytes | None:
        try:
            return self._request("GET", key=self.key(relative_path))
        except AnalyticsError as exc:
            if "HTTP 404" in str(exc):
                return None
            raise


def publish(config: dict[str, str], public: dict) -> str:
    client = R2Client(config)
    client.put_json(PUBLIC_ANALYTICS_KEY, public)
    base = config.get("R2_PUBLIC_BASE_URL", "").rstrip("/")
    return f"{base}/{PUBLIC_ANALYTICS_KEY}" if base else client.key(PUBLIC_ANALYTICS_KEY)


def print_ranking(ranking: list[tuple[str, int]], window_days: int, top: int = 20) -> None:
    total = sum(c for _, c in ranking)
    print(f"\nWindow: last {window_days} day(s)  |  {total} downloads across {len(ranking)} ROM(s)")
    for i, (name, count) in enumerate(ranking[:top], 1):
        print(f"  {i:>3}. {count:>6}  {name}")
    if len(ranking) > top:
        print(f"  ... and {len(ranking) - top} more")


def do_check(config: dict[str, str]) -> None:
    require(config, "CF_API_TOKEN", "CF_ZONE_ID")
    yesterday = dt.datetime.now(dt.timezone.utc).date() - dt.timedelta(days=1)
    per_rom = fetch_day(config["CF_API_TOKEN"], config["CF_ZONE_ID"], yesterday, rom_path_pattern(config))
    got = 0 if per_rom is None else sum(v["downloads"] for v in per_rom.values())
    print(f"cloudflare=ok (yesterday: {got} rom.zip downloads)")

    client = R2Client(config)
    probe_body = {"probe": True}
    client.put_json("__cerf_analytics_probe.json", probe_body)
    back = client.get_optional("__cerf_analytics_probe.json")
    if back is None:
        raise AnalyticsError("R2 probe write/read failed")
    client._request("DELETE", key=client.key("__cerf_analytics_probe.json"))
    print("r2=ok (signed put/get/delete)")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Collect ROM download analytics from Cloudflare and publish a public summary.")
    parser.add_argument("--window-days", type=int, default=DEFAULT_WINDOW_DAYS, help=f"Trailing days for the ranking (default {DEFAULT_WINDOW_DAYS}).")
    parser.add_argument("--fetch-days", type=int, default=DEFAULT_FETCH_DAYS, help=f"Days to backfill from Cloudflare per run (default {DEFAULT_FETCH_DAYS}; retention is ~8).")
    parser.add_argument("--collect-only", action="store_true", help="Update the local DB but do not publish analytics.json.")
    parser.add_argument("--dry-run", action="store_true", help="Collect in memory and print the ranking; write nothing, publish nothing.")
    parser.add_argument("--check", action="store_true", help="Validate Cloudflare + R2 credentials and exit.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = load_config()

    if args.check:
        do_check(config)
        return 0

    db = collect(config, args.fetch_days, persist=not args.dry_run)
    ranking = window_totals(db, args.window_days)
    print_ranking(ranking, args.window_days)
    public = build_public(ranking)

    if args.dry_run:
        print("\n--dry-run: analytics.json that WOULD be published:\n")
        print(json.dumps(public, indent=2))
        return 0
    if args.collect_only:
        print(f"\nCollected into {DB_PATH.name}; skipped publish (--collect-only).")
        return 0

    url = publish(config, public)
    print(f"\nPublished {PUBLIC_ANALYTICS_KEY} ({len(public['per_rom'])} ROM(s)) -> {url}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AnalyticsError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
