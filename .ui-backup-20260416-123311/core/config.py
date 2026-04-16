"""
core/config.py — Loads and exposes netorch.toml settings application-wide.

Config path resolution order:
  1. NETORCH_CONFIG env var (used by tests to point at a temp file)
  2. /opt/netorch/netorch.toml  (production)
  3. <project_root>/netorch.toml  (local dev / running from source)

All config values are instance attributes (not class attributes), so
patching _raw and reinstantiating the singletons at the bottom of this
file correctly propagates to every module that imports them.
"""
import os
import toml
from pathlib import Path


def _find_config() -> Path:
    # 1. Env override — used by the test suite
    env = os.environ.get("NETORCH_CONFIG")
    if env:
        p = Path(env)
        if p.exists():
            return p
        raise FileNotFoundError(f"NETORCH_CONFIG points to missing file: {env}")

    # 2. Production install path
    prod = Path("/opt/netorch/netorch.toml")
    if prod.exists():
        return prod

    # 3. Relative to this file (dev / running from source)
    rel = Path(__file__).parent.parent / "netorch.toml"
    if rel.exists():
        return rel

    raise FileNotFoundError(
        "netorch.toml not found. Set NETORCH_CONFIG env var or place it at "
        "/opt/netorch/netorch.toml"
    )


def _load() -> dict:
    return toml.load(_find_config())


class ServerConfig:
    def __init__(self, raw: dict):
        self.host:       str = raw["server"]["host"]
        self.port:       int = raw["server"]["port"]
        self.auth_token: str = raw["server"]["auth_token"]


class ExecutorConfig:
    def __init__(self, raw: dict):
        self.max_workers:    int = raw["executor"]["max_workers"]
        self.default_timeout:int = raw["executor"]["default_timeout"]
        self.max_queue_depth:int = raw["executor"]["max_queue_depth"]
        self.retry_attempts: int = raw["executor"]["retry_attempts"]
        self.retry_delay:    int = raw["executor"]["retry_delay"]


class InventoryConfig:
    def __init__(self, raw: dict):
        self.path: Path = Path(raw["inventory"]["path"])


class LoggingConfig:
    def __init__(self, raw: dict):
        self.log_dir: Path = Path(raw["logging"]["log_dir"])


class RateLimitConfig:
    def __init__(self, raw: dict):
        self.requests_per_minute:      int = raw["ratelimit"]["requests_per_minute"]
        self.job_submissions_per_minute:int = raw["ratelimit"]["job_submissions_per_minute"]


def _build_all():
    raw = _load()
    _server      = ServerConfig(raw)
    _executor    = ExecutorConfig(raw)
    _inventory   = InventoryConfig(raw)
    _logging     = LoggingConfig(raw)
    _ratelimit   = RateLimitConfig(raw)
    _logging.log_dir.mkdir(parents=True, exist_ok=True)
    return _server, _executor, _inventory, _logging, _ratelimit


server, executor, inventory, logging_cfg, ratelimit = _build_all()
