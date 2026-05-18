"""
core/config.py — Loads and exposes netorch.toml settings application-wide.

Config path resolution order:
  1. NETORCH_CONFIG env var (used by tests to point at a temp file)
  2. /opt/netorch/netorch.toml  (production)
  3. <project_root>/netorch.toml  (local dev / running from source)

All config values are instance attributes (not class attributes), so
patching _raw and reinstantiating the singletons at the bottom of this
file correctly propagates to every module that imports them.

ADDED: DatabaseConfig — reads optional [database] section from netorch.toml.
       Defaults to /opt/netorch/netorch.db if the section is absent, so
       existing installs work without any toml change.
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
        self.requests_per_minute:       int = raw["ratelimit"]["requests_per_minute"]
        self.job_submissions_per_minute:int = raw["ratelimit"]["job_submissions_per_minute"]


class BigFixConfig:
    """Reads the optional [bigfix] section from netorch.toml."""
    def __init__(self, raw: dict):
        bf = raw.get("bigfix", {})
        self.server_url:    str  = bf.get("server_url", "")
        self.username:      str  = bf.get("username", "")
        self.password:      str  = bf.get("password", "")   # optional inline; prefer OpenBao/env
        self.verify_ssl:    bool = bf.get("verify_ssl", False)
        self.scan_point_id: str  = str(bf.get("scan_point_id", "0"))
        self.scan_point_os: str  = bf.get("scan_point_os", "linux")


class DatabaseConfig:
    """
    Reads the optional [database] section from netorch.toml.

    If the section is absent (existing installs), defaults to
    /opt/netorch/netorch.db — sibling of the config file itself.

    netorch.toml example:
        [database]
        db_path = "/opt/netorch/netorch.db"
    """
    def __init__(self, raw: dict, config_path: Path):
        db_section = raw.get("database", {})
        default_db = config_path.parent / "netorch.db"
        self.db_path: Path = Path(db_section.get("db_path", str(default_db)))


def _build_all():
    config_path  = _find_config()
    raw          = toml.load(config_path)
    _server      = ServerConfig(raw)
    _executor    = ExecutorConfig(raw)
    _inventory   = InventoryConfig(raw)
    _logging     = LoggingConfig(raw)
    _ratelimit   = RateLimitConfig(raw)
    _database    = DatabaseConfig(raw, config_path)
    _bigfix      = BigFixConfig(raw)
    _logging.log_dir.mkdir(parents=True, exist_ok=True)
    return _server, _executor, _inventory, _logging, _ratelimit, _database, _bigfix


server, executor, inventory, logging_cfg, ratelimit, database, bigfix = _build_all()
