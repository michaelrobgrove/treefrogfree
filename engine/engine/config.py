"""Configuration loaded from environment variables (and a .env file if present).

The engine has very few knobs. We pull them in one place so the rest of the
codebase never reads os.environ directly. See plan.md §14 for rationale.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# Load .env from the project root (one level up from this package's parent)
# so that docker compose's `env_file: .env` and local dev share the same file.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_PROJECT_ROOT / ".env", override=False)


def _env(key: str, default: str | None = None, *, required: bool = False) -> str:
    """Read an env var, preferring ${key}_FILE (file content) over ${key}.

    The _FILE pattern is the standard Docker/secrets-manager convention:
    the deployment ships a path to a file (often a tmpfs mount or
    docker secret) instead of the value itself. This keeps raw secrets
    out of process listings, backup snapshots, and accidental commits.
    """
    # 1. ${key}_FILE: read the file and strip whitespace (including a
    #    trailing newline, which is the standard format for these files).
    file_val = os.getenv(f"{key}_FILE", "").strip()
    if file_val:
        try:
            with open(file_val, "r", encoding="utf-8") as fh:
                val = fh.read().strip()
        except OSError as e:
            raise RuntimeError(f"Could not read {key}_FILE={file_val!r}: {e}") from e
        if val:
            return val
    # 2. ${key} inline.
    val = os.getenv(key, default)
    if required and (val is None or val == ""):
        raise RuntimeError(f"Required env var {key!r} is not set")
    return val or ""  # type: ignore[return-value]


def _env_int(key: str, default: int) -> int:
    raw = os.getenv(key)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as e:
        raise RuntimeError(f"Env var {key!r} must be an int, got {raw!r}") from e


def _env_float(key: str, default: float) -> float:
    raw = os.getenv(key)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError as e:
        raise RuntimeError(f"Env var {key!r} must be a number, got {raw!r}") from e


@dataclass(frozen=True)
class Config:
    # Cloudflare
    cf_api_token: str
    cf_account_id: str
    cf_kv_namespace_id: str

    # Admin API
    admin_token: str
    admin_host: str
    admin_port: int

    # Engine behavior
    log_level: str
    health_concurrency: int
    health_timeout_sec: float
    health_cadence_sec: int
    health_offline_disable_hours: int
    health_manifest_bytes: int
    health_recent_failure_backoff: int

    # Paths
    data_dir: Path
    log_dir: Path
    db_path: Path

    @property
    def public_dir(self) -> Path:
        """Where the engine writes generated artifacts (playlist, catalog JSON)."""
        return self.data_dir / "public"


def load() -> Config:
    data_dir = Path(_env("DATA_DIR", "/app/data"))
    log_dir = Path(_env("LOG_DIR", "/app/logs"))
    db_path = Path(_env("DB_PATH", str(data_dir / "treefrog.db")))

    # Ensure runtime dirs exist. Idempotent.
    data_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "public").mkdir(parents=True, exist_ok=True)
    (data_dir / "cache").mkdir(parents=True, exist_ok=True)

    return Config(
        cf_api_token=_env("CF_API_TOKEN"),
        cf_account_id=_env("CF_ACCOUNT_ID"),
        cf_kv_namespace_id=_env("CF_KV_NAMESPACE_ID"),
        admin_token=_env("ADMIN_TOKEN", "change-me"),
        admin_host=_env("ADMIN_HOST", "127.0.0.1"),
        admin_port=_env_int("ADMIN_PORT", 8000),
        log_level=_env("LOG_LEVEL", "INFO"),
        health_concurrency=_env_int("HEALTH_CONCURRENCY", 50),
        health_timeout_sec=_env_float("HEALTH_TIMEOUT_SEC", 5.0),
        health_cadence_sec=_env_int("HEALTH_CADENCE_SEC", 1800),
        health_offline_disable_hours=_env_int("HEALTH_OFFLINE_DISABLE_HOURS", 72),
        health_manifest_bytes=_env_int("HEALTH_MANIFEST_BYTES", 16384),
        health_recent_failure_backoff=_env_int("HEALTH_RECENT_FAILURE_BACKOFF", 300),
        data_dir=data_dir,
        log_dir=log_dir,
        db_path=db_path,
    )


CONFIG = load()
