#!/usr/bin/env python3
"""Back up all Cloudflare DNS zones accessible by an API token."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


API_BASE_URL = "https://api.cloudflare.com/client/v4"
DEFAULT_BACKUP_DIR = "backups"
DEFAULT_ENV_FILE = ".env"
DEFAULT_BACKUP_FORMAT = "json"
TIMESTAMP_FORMAT = "%Y-%m-%d_%H-%M-%S"
BACKUP_DIR_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}$")
BACKUP_FORMAT_ALIASES = {
    "json": "json",
    "txt": "txt",
    "bind": "txt",
    "bind9": "txt",
    "zone": "txt",
}


class ConfigError(Exception):
    """Raised when configuration is missing or invalid."""


class CloudflareAPIError(Exception):
    """Raised when Cloudflare returns an API or transport error."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Back up all Cloudflare DNS zones accessible by an API token."
    )
    parser.add_argument(
        "--env-file",
        default=DEFAULT_ENV_FILE,
        help=f"Path to the local .env file. Default: {DEFAULT_ENV_FILE}",
    )
    return parser.parse_args()


def load_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        raise ConfigError(f"Environment file not found: {path}")

    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            raise ConfigError(f"Invalid .env line {line_number}: expected KEY=VALUE")

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            raise ConfigError(f"Invalid .env line {line_number}: empty key")

        if (
            len(value) >= 2
            and value[0] == value[-1]
            and value[0] in ("'", '"')
        ):
            value = value[1:-1]

        values[key] = value

    return values


def normalize_backup_format(raw_format: str) -> str:
    backup_format = raw_format.strip().lower()
    if backup_format not in BACKUP_FORMAT_ALIASES:
        accepted = ", ".join(sorted(BACKUP_FORMAT_ALIASES))
        raise ConfigError(f"BACKUP_FORMAT must be one of: {accepted}")
    return BACKUP_FORMAT_ALIASES[backup_format]


def load_config(env_file: Path) -> tuple[str, int, Path, str]:
    file_values = load_env_file(env_file)
    values = {**file_values, **os.environ}

    api_token = values.get("CLOUDFLARE-API-TOKEN") or values.get("CLOUDFLARE_API_TOKEN")
    if not api_token:
        raise ConfigError(
            "Missing CLOUDFLARE-API-TOKEN in .env "
            "(CLOUDFLARE_API_TOKEN is also accepted)."
        )

    retention_raw = values.get("BACKUP_RETENTION_DAYS")
    if not retention_raw:
        raise ConfigError("Missing BACKUP_RETENTION_DAYS in .env")
    try:
        retention_days = int(retention_raw)
    except ValueError as exc:
        raise ConfigError("BACKUP_RETENTION_DAYS must be an integer") from exc
    if retention_days < 0:
        raise ConfigError("BACKUP_RETENTION_DAYS must be zero or greater")

    backup_root = Path(values.get("BACKUP_OUTPUT_DIR", DEFAULT_BACKUP_DIR)).expanduser()
    backup_format = normalize_backup_format(
        values.get("BACKUP_FORMAT", DEFAULT_BACKUP_FORMAT)
    )
    return api_token, retention_days, backup_root, backup_format


def cloudflare_get(api_token: str, path: str, params: dict[str, Any] | None = None) -> Any:
    url = f"{API_BASE_URL}{path}"
    if params:
        url = f"{url}?{urlencode(params)}"

    request = Request(
        url,
        headers={
            "Authorization": f"Bearer {api_token}",
            "Content-Type": "application/json",
        },
        method="GET",
    )

    try:
        with urlopen(request, timeout=60) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise CloudflareAPIError(f"HTTP {exc.code} from Cloudflare: {body}") from exc
    except URLError as exc:
        raise CloudflareAPIError(f"Could not reach Cloudflare API: {exc.reason}") from exc
    except json.JSONDecodeError as exc:
        raise CloudflareAPIError("Cloudflare returned invalid JSON") from exc

    if not payload.get("success"):
        errors = payload.get("errors") or []
        raise CloudflareAPIError(f"Cloudflare API returned errors: {errors}")

    return payload


def cloudflare_get_text(
    api_token: str,
    path: str,
    params: dict[str, Any] | None = None,
) -> str:
    url = f"{API_BASE_URL}{path}"
    if params:
        url = f"{url}?{urlencode(params)}"

    request = Request(
        url,
        headers={
            "Authorization": f"Bearer {api_token}",
            "Accept": "text/plain",
        },
        method="GET",
    )

    try:
        with urlopen(request, timeout=60) as response:
            return response.read().decode("utf-8")
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise CloudflareAPIError(f"HTTP {exc.code} from Cloudflare: {body}") from exc
    except URLError as exc:
        raise CloudflareAPIError(f"Could not reach Cloudflare API: {exc.reason}") from exc


def fetch_paginated(api_token: str, path: str, params: dict[str, Any] | None = None) -> list[Any]:
    page = 1
    results: list[Any] = []
    base_params = dict(params or {})

    while True:
        payload = cloudflare_get(
            api_token,
            path,
            {**base_params, "page": page, "per_page": 100},
        )
        results.extend(payload.get("result", []))

        info = payload.get("result_info") or {}
        total_pages = int(info.get("total_pages") or page)
        if page >= total_pages:
            break
        page += 1

    return results


def safe_zone_filename(zone_name: str) -> str:
    safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", zone_name).strip("._")
    return safe_name or "zone"


def write_json(path: Path, data: Any) -> None:
    path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def write_text(path: Path, data: str) -> None:
    path.write_text(data.rstrip("\n") + "\n", encoding="utf-8")


def export_zone_bind9(api_token: str, zone_id: str) -> str:
    return cloudflare_get_text(api_token, f"/zones/{zone_id}/dns_records/export")


def create_backup(api_token: str, backup_root: Path, backup_format: str) -> Path:
    timestamp = datetime.now(timezone.utc).strftime(TIMESTAMP_FORMAT)
    backup_dir = backup_root / timestamp
    backup_dir.mkdir(parents=True, exist_ok=False)

    zones = fetch_paginated(api_token, "/zones")
    manifest: dict[str, Any] = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "backup_format": backup_format,
        "zone_count": len(zones),
        "zones": [],
    }

    write_json(backup_dir / "zones.json", zones)

    for zone in zones:
        zone_id = zone["id"]
        zone_name = zone["name"]
        records = fetch_paginated(api_token, f"/zones/{zone_id}/dns_records")
        filename = f"{safe_zone_filename(zone_name)}.{backup_format}"

        if backup_format == "json":
            write_json(
                backup_dir / filename,
                {
                    "zone": zone,
                    "dns_record_count": len(records),
                    "dns_records": records,
                },
            )
        else:
            write_text(backup_dir / filename, export_zone_bind9(api_token, zone_id))

        manifest["zones"].append(
            {
                "id": zone_id,
                "name": zone_name,
                "dns_record_count": len(records),
                "file": filename,
            }
        )

    write_json(backup_dir / "manifest.json", manifest)
    return backup_dir


def parse_backup_dir_timestamp(path: Path) -> datetime | None:
    if not path.is_dir() or not BACKUP_DIR_PATTERN.match(path.name):
        return None
    try:
        return datetime.strptime(path.name, TIMESTAMP_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def cleanup_old_backups(backup_root: Path, retention_days: int) -> list[Path]:
    if not backup_root.exists():
        return []

    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    removed: list[Path] = []

    for child in backup_root.iterdir():
        created_at = parse_backup_dir_timestamp(child)
        if created_at is None or created_at >= cutoff:
            continue

        shutil.rmtree(child)
        removed.append(child)

    return removed


def main() -> int:
    args = parse_args()

    try:
        api_token, retention_days, backup_root, backup_format = load_config(
            Path(args.env_file)
        )
        backup_dir = create_backup(api_token, backup_root, backup_format)
        removed = cleanup_old_backups(backup_root, retention_days)
    except (ConfigError, CloudflareAPIError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(f"Backup created: {backup_dir}")
    print(f"Old backups removed: {len(removed)}")
    for path in removed:
        print(f"Removed: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
