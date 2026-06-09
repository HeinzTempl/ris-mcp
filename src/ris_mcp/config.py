"""Konfiguration fuer den RIS-MCP-Server.

Settings werden ueber Umgebungsvariablen mit Prefix ``RIS_MCP_`` ueberschrieben.
Defaults sind so gewaehlt, dass der Server out-of-the-box laeuft, ohne dass
irgendwas konfiguriert werden muss.
"""

from __future__ import annotations

from pathlib import Path

from platformdirs import user_cache_dir
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


def _default_cache_path() -> Path:
    """Liefert den Standard-Pfad fuer die SQLite-Cache-Datei.

    Auf macOS landet das unter ``~/Library/Caches/ris-mcp/cache.db``,
    auf Linux unter ``~/.cache/ris-mcp/cache.db``.
    """
    base = Path(user_cache_dir(appname="ris-mcp", appauthor=False))
    base.mkdir(parents=True, exist_ok=True)
    return base / "cache.db"


class Settings(BaseSettings):
    """Zentrale Settings-Klasse. Wird beim Serverstart einmal instanziiert."""

    model_config = SettingsConfigDict(
        env_prefix="RIS_MCP_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- RIS-API ---------------------------------------------------------
    ris_base_url: str = Field(
        default="https://data.bka.gv.at/ris/api/v2.6",
        description="Basis-URL der RIS-OGD-API. Version pinnen wir explizit.",
    )
    user_agent: str = Field(
        default="ris-mcp/0.1 (+https://github.com/local) httpx",
        description="User-Agent fuer alle HTTP-Requests gegen RIS.",
    )
    http_timeout_seconds: float = Field(default=30.0)

    # --- Rate Limiting ---------------------------------------------------
    # RIS dokumentiert kein offizielles Limit. Wir sind defensiv freundlich.
    rate_limit_per_second: float = Field(default=5.0)
    rate_limit_burst: int = Field(default=10)

    # --- Cache -----------------------------------------------------------
    cache_db_path: Path = Field(default_factory=_default_cache_path)
    cache_enabled: bool = Field(default=True)

    # TTLs in Sekunden. ``None`` heisst: nie automatisch invalidieren.
    ttl_search_seconds: int = Field(default=24 * 60 * 60)  # 24 h
    ttl_document_current_seconds: int = Field(default=30 * 24 * 60 * 60)  # 30 Tage
    ttl_document_historical_seconds: int | None = Field(default=None)
    ttl_judikatur_document_seconds: int | None = Field(default=None)

    # --- Tool-Defaults ---------------------------------------------------
    default_max_results: int = Field(default=20)
    default_output_format: str = Field(default="markdown")  # markdown | json | text

    # Warmup-Profil: Liste der Stammgesetze fuer ``ris-mcp warmup``.
    warmup_profile_zivilrecht: tuple[str, ...] = Field(
        default=(
            "ABGB", "UGB", "StGB", "ZPO", "EO", "IO", "GmbHG", "AktG",
            "KSchG", "MRG", "WEG", "AussStrG", "JN", "EheG", "FBG",
        ),
        description=(
            "Kurzbezeichnungen der Gesetze, die ein ``ris-mcp warmup --profil "
            "zivilrecht`` vorab in den Cache zieht."
        ),
    )


_settings: Settings | None = None


def get_settings() -> Settings:
    """Singleton-Zugriff auf die Settings.

    Lazy initialisiert, damit Tests die Env-Variablen vor dem ersten Zugriff
    setzen koennen.
    """
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def reset_settings() -> None:
    """Settings-Cache zuruecksetzen. Nur fuer Tests."""
    global _settings
    _settings = None
