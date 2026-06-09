"""FastMCP-Setup fuer den RIS-MCP-Server.

Tools:

* ``bundesrecht_search`` / ``bundesrecht_get_norm`` /
  ``bundesrecht_get_gesamte_vorschrift``
* ``ogh_search`` / ``judikatur_search`` / ``judikatur_get_entscheidung``
* ``ris_recherche_norm`` (Ein-Call: Norm-Volltext + OGH-Rechtssaetze)
* ``ris_local_search`` (FTS5 ueber lokal gecachte Dokumente)

plus ``ris_health`` als Lebenszeichen.

Aufruf in LM Studio / Msty per stdio:

```jsonc
{
  "mcpServers": {
    "ris": {
      "command": "uv",
      "args": ["run", "--directory", "/pfad/zu/ris-mcp", "ris-mcp"]
    }
  }
}
```
"""

from __future__ import annotations

import logging
import sys
from contextlib import asynccontextmanager
from typing import Any

import structlog
from fastmcp import Context, FastMCP

from . import __version__
from .cache import Cache
from .client import RisClient
from .config import get_settings
from .tools import bundesrecht as t_bundesrecht
from .tools import judikatur as t_judikatur
from .tools import landesrecht as t_landesrecht
from .tools import local as t_local
from .tools import recherche as t_recherche

# --- Logging-Setup -----------------------------------------------------------
# MCP laeuft ueber stdio, also Logs strikt nach stderr.
logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(message)s")
structlog.configure(
    processors=[
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer(),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
    logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
)
log = structlog.get_logger("ris_mcp.server")


# --- Lifespan ---------------------------------------------------------------
@asynccontextmanager
async def lifespan(_app: FastMCP):
    """Startet Cache und HTTP-Client; raeumt beim Shutdown auf."""
    settings = get_settings()
    cache = Cache(settings.cache_db_path) if settings.cache_enabled else None
    if cache is not None:
        await cache.connect()
    client = RisClient(settings=settings, cache=cache)
    await client.start()

    try:
        yield {"settings": settings, "cache": cache, "client": client}
    finally:
        await client.close()
        if cache is not None:
            await cache.close()


# --- App --------------------------------------------------------------------
mcp = FastMCP(
    name="ris-mcp",
    instructions=(
        "Lokaler MCP-Server fuer die oesterreichische RIS-OGD-API "
        "(Bundesrecht konsolidiert + Judikatur: OGH, VfGH, VwGH, BVwG, LVwG, ...). "
        "Alle Antworten kommen als Markdown.\n\n"
        "REGELN -- bitte strikt einhalten, da es sich um ein anwaltliches "
        "Recherche-Tool handelt und Falschangaben rechtlich folgenreich sein "
        "koennen:\n\n"
        "1. Bei JEDER Frage zum INHALT einer oesterreichischen Bundes-Norm "
        "(z. B. 'Was sagt § 871 ABGB?', 'Erklaere § 1295 ABGB') MUSST du "
        "zuerst `bundesrecht_get_norm` aufrufen. Erfinde NIE eine Norm-"
        "Bedeutung aus deinem Vorwissen.\n\n"
        "1b. Will der User zu EINER konkreten Gesetzesstelle eine Recherche "
        "MIT Rechtsprechung (z. B. 'Recherche zu § 871 ABGB mit Judikatur', "
        "'einschlaegige OGH-Entscheidungen zu § 1295 ABGB'), nimm das "
        "Ein-Call-Tool `ris_recherche_norm(gesetz=..., paragraph=...)`. Es "
        "liefert Norm-Volltext UND die einschlaegigen OGH-Rechtssaetze mit "
        "Leitsatz in einem Schritt -- du musst dann NICHT mehr einzeln "
        "`bundesrecht_get_norm` + `ogh_search` aufrufen.\n\n"
        "2. Bei JEDER Frage zu Judikatur (OGH, VfGH, VwGH, ...) MUSST du "
        "zuerst `ogh_search` (fuer OGH-Standardfaelle, optimiert auf "
        "Zivilrecht) oder `judikatur_search` (alle anderen Gerichte) "
        "aufrufen. Erfinde NIE Geschaeftszahlen, Entscheidungsdaten oder "
        "Doku-IDs.\n\n"
        "3. Wenn du in deiner Antwort eine Geschaeftszahl, ein Datum, eine "
        "Doku-ID, eine ECLI oder einen Normbezug nennst, muss dieser "
        "Wert WORT-WOERTLICH aus einem Tool-Output stammen, den du in "
        "der laufenden Konversation erhalten hast. Reproduziere Zahlen "
        "EXAKT (kein Verkuerzen, kein Umformulieren, kein Schaetzen). "
        "Wenn du dir bei einer Zahl unsicher bist, lass sie weg und "
        "verweise stattdessen auf das Tool-Ergebnis.\n\n"
        "4. Wenn ein Tool keine Treffer liefert, ist die korrekte Antwort "
        "'keine Treffer im RIS' -- nicht 'die mir bekannten Entscheidungen sind ...'.\n\n"
        "5. Volltext einer einzelnen Norm holst du mit `bundesrecht_get_norm`; "
        "Volltext einer Entscheidung mit `judikatur_get_entscheidung`. Die "
        "Doku-IDs dafuer stammen aus den jeweiligen Such-Tools.\n\n"
        "6. Bei mehreren passenden Treffern (Disambig-Tabelle) frage den "
        "User kurz, welcher gemeint ist, statt zu raten.\n\n"
        "Workflow-Standardpfad: `*_search` -> `*_get_*` mit doc_id."
    ),
    lifespan=lifespan,
)

# --- Tools registrieren -----------------------------------------------------
t_bundesrecht.register(mcp)
t_judikatur.register(mcp)
t_landesrecht.register(mcp)
t_local.register(mcp)
t_recherche.register(mcp)


# --- Health-Tool ------------------------------------------------------------
@mcp.tool(
    name="ris_health",
    description=(
        "Liefert Status des RIS-MCP-Servers: Version, Cache-Pfad, "
        "Anzahl gecachter Suchen und Dokumente. Nuetzlich zum Pruefen "
        "ob der Server in LM Studio / Msty laeuft."
    ),
)
async def ris_health(ctx: Context) -> dict[str, Any]:
    settings = get_settings()
    cache: Cache | None = ctx.request_context.lifespan_context.get("cache")  # type: ignore[union-attr]

    stats: dict[str, Any]
    if cache is None:
        stats = {"cache": "disabled"}
    else:
        stats = await cache.stats()

    return {
        "version": __version__,
        "ris_base_url": settings.ris_base_url,
        "cache_enabled": settings.cache_enabled,
        "rate_limit_per_second": settings.rate_limit_per_second,
        "default_output_format": settings.default_output_format,
        "stats": stats,
    }


# --- Entry-Point ------------------------------------------------------------
def main() -> None:
    """Console-Script-Eintrittspunkt: startet den FastMCP-Server auf stdio."""
    log.info("ris_mcp.start", version=__version__)
    mcp.run()  # default transport = stdio


if __name__ == "__main__":
    main()
