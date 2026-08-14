"""DealRadar's own exception hierarchy. Owner: A0."""

from __future__ import annotations


class DealRadarError(Exception):
    """Base class for every error raised deliberately by DealRadar's own code."""


class ConfigError(DealRadarError):
    """Raised when configuration files under config/ are missing, malformed, or fail validation at startup."""


class MigrationError(DealRadarError):
    """Raised when a database migration file cannot be applied cleanly."""


class CollectorError(DealRadarError):
    """Raised when a collector cannot complete a fetch after exhausting its retry budget."""


class RunLockError(DealRadarError):
    """Raised when a pipeline run cannot start because another run already holds the run lock."""
