"""Tools fuer Bundesrecht konsolidiert (Applikation ``BrKons``).

Drei Tools:

* ``bundesrecht_search`` -- Suche nach Norm/Paragraph
* ``bundesrecht_get_norm`` -- Volltext eines Paragrafen/Artikels/Anlage
* ``bundesrecht_get_gesamte_vorschrift`` -- ganze Rechtsvorschrift via
  ``GesamteRechtsvorschriftUrl`` (HTML)
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Literal

from fastmcp import Context, FastMCP
from pydantic import Field

from ..adapters.response import parse_bundesrecht_hit, parse_search_result
from ._common import (
    api_paragraph_number,
    get_runtime,
    load_content_as_markdown,
    pick_valid_bundesrecht_hit,
    render_bundesrecht_hits_md,
    to_page_size,
)

CONTROLLER = "Bundesrecht"
APPLICATION = "BrKons"


def register(mcp: FastMCP) -> None:
    """Registriert die drei Bundesrecht-Tools am FastMCP-Server."""

    @mcp.tool(
        name="bundesrecht_search",
        description=(
            "Sucht in der konsolidierten Sammlung des oesterreichischen Bundesrechts "
            "(Applikation BrKons). Liefert Liste von Paragrafen/Artikeln/Anlagen mit "
            "Metadaten und Dokument-IDs. Fuer Volltext anschliessend "
            "`bundesrecht_get_norm(doc_id=...)` aufrufen.\n\n"
            "Tipps:\n"
            "- Fuer ein bestimmtes Gesetz: `titel='ABGB'` oder `titel='Allgemeines "
            "buergerliches Gesetzbuch'`.\n"
            "- Fuer einen bestimmten Paragrafen: zusaetzlich `paragraph_von=1295`.\n"
            "- Fuer historische Fassung: `fassung_vom='2010-01-01'`."
        ),
    )
    async def bundesrecht_search(
        ctx: Context,
        suchworte: Annotated[
            str | None,
            Field(description="Volltext-Suchausdruck im Inhalt, optional."),
        ] = None,
        titel: Annotated[
            str | None,
            Field(description="Titel oder Abkuerzung, z. B. 'ABGB', 'UGB', 'StGB'."),
        ] = None,
        paragraph_von: Annotated[
            int | None,
            Field(description="Erste Paragraf-/Artikel-/Anlagen-Nummer (inklusive)."),
        ] = None,
        paragraph_bis: Annotated[
            int | None,
            Field(description="Letzte Paragraf-/Artikel-/Anlagen-Nummer (inklusive)."),
        ] = None,
        abschnitt_typ: Annotated[
            Literal["Alle", "Paragraph", "Artikel", "Anlage"],
            Field(description="Welcher Strukturtyp gesucht wird."),
        ] = "Paragraph",
        fassung_vom: Annotated[
            str | None,
            Field(description="Geltungsdatum YYYY-MM-DD. Default = heute."),
        ] = None,
        index: Annotated[
            str | None,
            Field(description="RIS-Sachindex, z. B. '20/01' fuer ABGB."),
        ] = None,
        max_results: Annotated[
            int, Field(description="Max. Treffer pro Seite (10/20/50/100).", ge=1, le=100)
        ] = 20,
        page: Annotated[int, Field(description="Seitennummer ab 1.", ge=1)] = 1,
        force_refresh: Annotated[
            bool, Field(description="Cache umgehen und frisch holen.")
        ] = False,
    ) -> str:
        client, _cache, _settings = get_runtime(ctx)

        params: dict[str, str | int] = {
            "Abschnitt.Typ": abschnitt_typ,
            "DokumenteProSeite": to_page_size(max_results),
            "Seitennummer": page,
        }
        if suchworte:
            params["Suchworte"] = suchworte
        if titel:
            params["Titel"] = titel
        if paragraph_von is not None:
            params["Abschnitt.Von"] = paragraph_von
        if paragraph_bis is not None:
            params["Abschnitt.Bis"] = paragraph_bis
        if fassung_vom:
            params["Fassung.FassungVom"] = fassung_vom
        if index:
            params["Index"] = index

        payload = await client.search(
            controller=CONTROLLER,
            application=APPLICATION,
            params=params,
            force_refresh=force_refresh,
        )
        result = parse_search_result(
            payload, application=APPLICATION, hit_parser=parse_bundesrecht_hit
        )
        title = f"Bundesrecht: {titel or suchworte or 'Suche'}"
        if paragraph_von is not None and paragraph_bis is not None and paragraph_von == paragraph_bis:
            title += f" § {paragraph_von}"
        elif paragraph_von is not None:
            title += f" ab § {paragraph_von}"
        return render_bundesrecht_hits_md(
            title, result.total, result.page_number, result.page_size, result.hits
        )  # type: ignore[arg-type]

    @mcp.tool(
        name="bundesrecht_get_norm",
        description=(
            "Liefert den Volltext einer einzelnen Norm (Paragraf, Artikel, Anlage) "
            "aus dem Bundesrecht als Markdown.\n\n"
            "Zwei Modi:\n"
            "1. Per Dokument-ID aus einem vorherigen `bundesrecht_search`-Treffer.\n"
            "2. Per Kombi `gesetz` + `paragraph` (optional `fassung_vom`).\n\n"
            "WICHTIG: Ist die einzige zulaessige Quelle fuer den Inhalt einer "
            "Norm. Erklaere NIE eine Norm aus Vorwissen, ohne dieses Tool "
            "vorher aufgerufen zu haben."
        ),
    )
    async def bundesrecht_get_norm(
        ctx: Context,
        doc_id: Annotated[
            str | None, Field(description="Dokument-ID aus Suchergebnis, z. B. 'NOR12019037'.")
        ] = None,
        gesetz: Annotated[
            str | None, Field(description="Abkuerzung oder Titel, z. B. 'ABGB'.")
        ] = None,
        paragraph: Annotated[
            int | str | None,
            Field(
                description=(
                    "Paragrafennummer. Auch Buchstaben-Varianten als String, "
                    "z. B. '880a', '1319b'."
                )
            ),
        ] = None,
        abschnitt_typ: Annotated[
            Literal["Paragraph", "Artikel", "Anlage"],
            Field(description="Strukturtyp; Default Paragraph."),
        ] = "Paragraph",
        fassung_vom: Annotated[
            str | None, Field(description="Geltungsdatum YYYY-MM-DD.")
        ] = None,
        force_refresh: Annotated[bool, Field(description="Cache umgehen.")] = False,
    ) -> str:
        client, cache, settings = get_runtime(ctx)

        if doc_id is None and (gesetz is None or paragraph is None):
            return (
                "Fehler: Bitte entweder `doc_id` angeben oder die Kombination "
                "`gesetz` + `paragraph`."
            )

        # Stichtag der gewuenschten Fassung. Ohne explizite Angabe nehmen wir
        # HEUTE -- sonst liefert RIS bei Normen mit mehreren Zeitscheiben (z. B.
        # § 356 UGB: alte HGB-Linie ausgelaufen 2006 + UGB-Fassung ab 2007) auch
        # die ausgelaufene Fassung, und blindes hits[0] erwischt die falsche.
        # Im doc_id-Modus filtern wir NICHT auf heute, damit ein gezielt
        # angefordertes (evtl. historisches) Dokument auffindbar bleibt.
        effective_fassung = fassung_vom
        if effective_fassung is None and doc_id is None:
            effective_fassung = date.today().isoformat()

        # Buchstaben-Variante: '880a' -> 880 fuer die API; der Buchstabe
        # wird unten im Picker via _normalize_abschnitt unterschieden.
        paragraph_api = api_paragraph_number(paragraph)

        async def _run_search(fassung: str | None):
            params: dict[str, str | int] = {
                "Abschnitt.Typ": abschnitt_typ,
                "DokumenteProSeite": "Ten",
            }
            if gesetz:
                params["Titel"] = gesetz
            if paragraph_api is not None:
                params["Abschnitt.Von"] = paragraph_api
                params["Abschnitt.Bis"] = paragraph_api
            if fassung:
                params["Fassung.FassungVom"] = fassung
            if doc_id:
                params["Gesetzesnummer"] = doc_id  # Backend-Hint
            payload = await client.search(
                controller=CONTROLLER,
                application=APPLICATION,
                params=params,
                force_refresh=force_refresh,
            )
            return parse_search_result(
                payload, application=APPLICATION, hit_parser=parse_bundesrecht_hit
            )

        result = await _run_search(effective_fassung)
        # Fallback: echt ausgelaufene Norm liefert mit Stichtag heute 0 Treffer.
        # Dann ohne Datumsfilter erneut suchen -- der Header markiert sie dann
        # korrekt als ausgelaufen.
        if not result.hits and effective_fassung is not None and fassung_vom is None:
            result = await _run_search(None)

        hit = None
        if doc_id:
            for h in result.hits:
                if h.id == doc_id:
                    hit = h
                    break
        if hit is None:
            hit = pick_valid_bundesrecht_hit(
                result.hits, on_date=effective_fassung, paragraphnummer=paragraph
            )
        if hit is None:
            return (
                f"Keine Norm gefunden ({gesetz=}, §{paragraph}, fassung_vom={fassung_vom})."
            )

        header = {
            "Gesetz": str(hit.kurztitel or hit.abkuerzung or ""),
            "Bezeichnung": str(hit.artikel_paragraph_anlage or ""),
            "Inkrafttretensdatum": str(hit.inkrafttretensdatum or ""),
            "Dokument-ID": str(hit.id or ""),
            "RIS-Link": str(hit.document_url or ""),
        }
        if hit.ausserkrafttretensdatum:
            header["Außerkrafttretensdatum"] = str(hit.ausserkrafttretensdatum)
        return await load_content_as_markdown(
            client=client,
            cache=cache,
            doc_id=hit.id or f"unknown-{paragraph}",
            content_urls=hit.content_urls,
            settings=settings,
            metadata_header=header,
        )

    @mcp.tool(
        name="bundesrecht_get_gesamte_vorschrift",
        description=(
            "Liefert die GESAMTE konsolidierte Rechtsvorschrift (z. B. das ganze "
            "ABGB) als Markdown. Nutzt die `GesamteRechtsvorschriftUrl` aus dem "
            "RIS und konvertiert das HTML. Achtung: kann sehr lang sein (ABGB hat "
            "mehrere tausend Paragrafen)."
        ),
    )
    async def bundesrecht_get_gesamte_vorschrift(
        ctx: Context,
        gesetz: Annotated[str, Field(description="Abkuerzung oder Titel, z. B. 'ABGB'.")],
        fassung_vom: Annotated[
            str | None, Field(description="Geltungsdatum YYYY-MM-DD.")
        ] = None,
        force_refresh: Annotated[bool, Field(description="Cache umgehen.")] = False,
    ) -> str:
        client, cache, settings = get_runtime(ctx)

        params: dict[str, str | int] = {
            "Abschnitt.Typ": "Paragraph",
            "Abschnitt.Von": 1,
            "Abschnitt.Bis": 1,
            "Titel": gesetz,
            "DokumenteProSeite": "Ten",
        }
        if fassung_vom:
            params["Fassung.FassungVom"] = fassung_vom

        payload = await client.search(
            controller=CONTROLLER,
            application=APPLICATION,
            params=params,
            force_refresh=force_refresh,
        )
        result = parse_search_result(
            payload, application=APPLICATION, hit_parser=parse_bundesrecht_hit
        )
        if not result.hits:
            return f"Keine Rechtsvorschrift '{gesetz}' gefunden."

        # Den GesamteRechtsvorschrift-Link aus irgendeinem Treffer ziehen --
        # er ist fuer alle Paragrafen derselben Vorschrift identisch.
        url = None
        for h in result.hits:
            if isinstance(h, type(result.hits[0])) and getattr(h, "gesamte_rechtsvorschrift_url", None):
                url = h.gesamte_rechtsvorschrift_url
                break
        if url is None:
            return (
                f"Fuer '{gesetz}' wurde keine GesamteRechtsvorschriftUrl ausgeliefert."
            )

        from ..adapters.content_html import html_to_markdown

        cache_key = f"gesamt::{gesetz}::{fassung_vom or 'heute'}"
        if cache and not force_refresh:
            cached = await cache.get_document(cache_key)
            if cached and cached.get("text_content"):
                return cached["text_content"]

        raw, content_type = await client.fetch_content(url)
        markdown = html_to_markdown(raw)
        if cache:
            await cache.put_document(
                doc_id=cache_key,
                source_url=url,
                content_type=content_type,
                raw_content=raw,
                text_content=markdown,
                metadata={"gesetz": gesetz, "fassung_vom": fassung_vom or ""},
                ttl_seconds=settings.ttl_document_current_seconds,
            )
        return markdown
