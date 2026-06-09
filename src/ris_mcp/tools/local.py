"""Lokale FTS5-Suche ueber alle gecachten Dokumente.

Geht NICHT ans RIS, sondern sucht direkt in der lokalen ``document_cache``-
Tabelle. Erst nuetzlich, wenn vorher Dokumente per ``bundesrecht_get_norm``,
``judikatur_get_entscheidung`` oder ``ris-mcp warmup`` reingezogen wurden.
"""

from __future__ import annotations

from typing import Annotated

from fastmcp import Context, FastMCP
from pydantic import Field

from ._common import get_runtime


def register(mcp: FastMCP) -> None:
    @mcp.tool(
        name="ris_local_search",
        description=(
            "Volltextsuche (FTS5) in den lokal gecachten Dokumenten. Liefert "
            "Snippets mit Treffer-Kontext und Dokument-IDs. Geht NICHT ans RIS -- "
            "praktisch fuer offline-Recherche in bereits geladenen Gesetzen.\n\n"
            "Tokens werden automatisch mit Praefix-Wildcard versehen (`schaden` "
            "matcht auch `schadenersatz`). Phrasensuche mit Anfuehrungszeichen, "
            "Boolean mit AND/OR/NOT."
        ),
    )
    async def ris_local_search(
        ctx: Context,
        query: Annotated[
            str, Field(description="Suchausdruck, FTS5-Syntax. Beispiele: 'arglistig', '\"culpa in contrahendo\"', 'verjaehrung AND schaden'.")
        ],
        max_results: Annotated[int, Field(ge=1, le=50)] = 15,
        exact: Annotated[
            bool,
            Field(description="True = keine automatische Praefix-Wildcard auf Tokens."),
        ] = False,
    ) -> str:
        _client, cache, _settings = get_runtime(ctx)
        if cache is None:
            return "Cache ist deaktiviert -- ris_local_search nicht verfuegbar."

        fts_query = _prepare_query(query, exact=exact)
        hits = await cache.fts_search(fts_query, limit=max_results)

        if not hits:
            return (
                f"Keine lokalen Treffer fuer `{query}`. "
                "Erst ueber `bundesrecht_get_norm` oder `judikatur_get_entscheidung` "
                "Dokumente in den Cache holen."
            )

        lines = [
            f"# Lokale Treffer fuer `{query}`",
            "",
            f"Gefunden: {len(hits)}",
            "",
        ]
        for i, h in enumerate(hits, 1):
            doc_id = h["doc_id"]
            snippet = (h["snippet"] or "").replace("\n", " ")
            url = h.get("source_url") or ""
            meta = h.get("metadata") or {}
            label = meta.get("Gesetz") or meta.get("Gericht") or meta.get("gesetz") or doc_id
            extra = meta.get("Bezeichnung") or meta.get("Geschaeftszahl(en)") or ""
            lines.append(f"## {i}. {label} {extra}".rstrip())
            lines.append(f"- Doku-ID: `{doc_id}`")
            if url:
                lines.append(f"- Quelle: {url}")
            lines.append(f"- Snippet: {snippet}")
            lines.append("")
        return "\n".join(lines)


def _prepare_query(q: str, *, exact: bool) -> str:
    """Wenn nicht exact: an jedes Token, das nicht in Quotes steht und kein
    Operator ist, ein `*` als Praefix-Wildcard anhaengen.
    """
    if exact:
        return q
    # Sehr einfache Heuristik: Tokens ausserhalb von "..." mit * suffixen.
    out: list[str] = []
    in_quote = False
    token = ""
    operators = {"AND", "OR", "NOT", "NEAR"}
    for ch in q + " ":
        if ch == '"':
            in_quote = not in_quote
            token += ch
            continue
        if in_quote:
            token += ch
            continue
        if ch.isspace() or ch in "():":
            if token:
                if token.upper() in operators or token.endswith("*") or token.startswith('"'):
                    out.append(token)
                else:
                    out.append(token + "*")
                token = ""
            if ch.strip():
                out.append(ch)
            else:
                out.append(" ")
            continue
        token += ch
    return "".join(out).strip()
