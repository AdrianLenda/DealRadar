"""Config loading: YAML under config/ -> validated pydantic models, secrets from the environment only.

Owner: A0. Validation happens once, at startup (`load_config`), not lazily on first use — a malformed
sources.yaml must fail `dealradar db migrate` immediately, not three stages into a nightly run.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated, Any

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field

from dealradar.errors import ConfigError
from dealradar.models import SourceKind

# Populates os.environ from a local .env file if present, without overriding real environment variables
# that are already set (e.g. in CI or production). Secrets are still only ever read via os.environ.
load_dotenv(override=False)

DEFAULT_CONFIG_DIR = Path(__file__).resolve().parents[2] / "config"

REQUIRED_ENV_VARS: tuple[str, ...] = (
    "ALLEGRO_CLIENT_ID",
    "ALLEGRO_CLIENT_SECRET",
    "ALIEXPRESS_APP_KEY",
    "ALIEXPRESS_APP_SECRET",
    "AMAZON_ACCESS_KEY",
    "AMAZON_SECRET_KEY",
    "AMAZON_PARTNER_TAG",
    "SMTP_HOST",
    "SMTP_PORT",
    "SMTP_USER",
    "SMTP_PASSWORD",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_CHAT_ID",
)
"""Documents which secrets the full system can use; see .env.example. None are required just to run A0's
own skeleton (`dealradar db migrate` / `dealradar collect --source dummy` need none of these) — collectors
that need one are expected to disable themselves gracefully when it is unset (see collectors/__init__.py)."""


class SourceConfig(BaseModel):
    """One entry from config/sources.yaml: the `source` table's fields, plus room for collector-specific extras.

    `extra="allow"` is deliberate: A1 will add fields like `rps`, `auth_env`, or feed URLs per source without
    needing to touch this A0-owned file.
    """

    model_config = ConfigDict(extra="allow")

    name: str
    kind: SourceKind
    base_url: str
    enabled: bool = True
    risk_prior: Annotated[float, Field(ge=0.0, le=1.0)]


class SourcesConfig(BaseModel):
    """The fully parsed and validated contents of config/sources.yaml."""

    model_config = ConfigDict(extra="forbid")

    sources: list[SourceConfig] = Field(default_factory=list)

    def get(self, name: str) -> SourceConfig | None:
        """Returns the source config named `name` (case-insensitive), or None if it is not configured."""
        target = name.strip().lower()
        for entry in self.sources:
            if entry.name.strip().lower() == target:
                return entry
        return None


class BrandAliasesConfig(BaseModel):
    """The fully parsed contents of config/brands.yaml: canonical brand name -> list of known aliases."""

    model_config = ConfigDict(extra="forbid")

    aliases: dict[str, list[str]] = Field(default_factory=dict)


class AppConfig(BaseModel):
    """Top-level, fully validated application configuration, assembled once at startup by load_config."""

    model_config = ConfigDict(extra="forbid")

    database_url: str = "sqlite:///dealradar.db"
    sources: SourcesConfig
    brands: BrandAliasesConfig


def _read_yaml(path: Path) -> dict[str, Any]:
    """Reads and parses one YAML file into a dict; raises ConfigError if it is missing or not a mapping."""
    if not path.exists():
        raise ConfigError(f"missing required config file: {path}")
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError(f"expected a YAML mapping at the top level of {path}, got {type(data).__name__}")
    return data


def load_config(config_dir: Path | None = None, *, database_url: str | None = None) -> AppConfig:
    """Loads and validates every config/*.yaml file into an AppConfig; raises ConfigError on any problem."""
    directory = config_dir or DEFAULT_CONFIG_DIR
    sources_data = _read_yaml(directory / "sources.yaml")
    brands_data = _read_yaml(directory / "brands.yaml")
    try:
        sources = SourcesConfig.model_validate(sources_data)
        brands = BrandAliasesConfig.model_validate(brands_data)
    except Exception as exc:  # pydantic.ValidationError, normalized to our own error type
        raise ConfigError(f"invalid configuration under {directory}: {exc}") from exc

    resolved_db_url = database_url or os.environ.get("DEALRADAR_DATABASE_URL") or "sqlite:///dealradar.db"
    return AppConfig(database_url=resolved_db_url, sources=sources, brands=brands)


def get_secret(name: str) -> str | None:
    """Returns the named secret from the process environment only; config/*.yaml is never consulted for secrets."""
    value = os.environ.get(name)
    return value if value else None
