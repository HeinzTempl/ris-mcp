"""Tools fuer Landesrecht konsolidiert (Applikation ``LrKons``).

Spiegelt die Bundesrecht-Tools -- die OGD-Struktur ist bis auf den
Bundesland-Bezug identisch:

* ``landesrecht_search`` -- Suche nach Norm/Paragraph in einem Bundesland
* ``landesrecht_get_norm`` -- Volltext eines Paragrafen/Artikels/Anlage

Wichtig: Ein UNGUELTIGER Bundesland-Filter wird von der RIS-API stumm
ignoriert (und liefert dann ALLE Bundeslaender). Deshalb ist ``bundesland``
ein fester Enum, kein Freitext.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Literal

from fastmcp import Context, FastMCP
from pydantic import Field

from ..adapters.response import ContentUrl, parse_landesrecht_hit, parse_search_result
from ._common import (
    api_paragraph_number,
    get_runtime,
    load_content_as_markdown,
    pick_valid_bundesrecht_hit,
    render_landesrecht_hits_md,
    to_page_size,
)

CONTROLLER = "Landesrecht"
APPLICATION = "LrKons"

# Host + Dokumente-Ordner, unter dem RIS die LrKons-Volltexte ausliefert.
_RIS_DOC_HOST = "https://www.ris.bka.gv.at"
_DOC_FOLDER = "Landesnormen"

# LLM-freundlicher Bundesland-Name -> RIS-Suchparameter. Genau diese 9 Werte
# sind gueltig; alles andere wird von RIS still verworfen (= alle Laender).
BundeslandName = Literal[
    "Burgenland", "Kaernten", "Niederoesterreich", "Oberoesterreich",
    "Salzburg", "Steiermark", "Tirol", "Vorarlberg", "Wien",
]


def _bundesland_param(bundesland: str) -> str:
    return f"Bundesland.SucheIn{bundesland}"


def _doc_content_urls(doc_id: str) -> list[ContentUrl]:
    """Stabile Content-URLs zu einer LrKons-Doku (XML bevorzugt, HTML-Fallback)."""
    base = f"{_RIS_DOC_HOST}/Dokumente/{_DOC_FOLDER}/{doc_id}/{doc_id}"
    return [
        ContentUrl(data_type="Xml", url=f"{base}.xml"),
        ContentUrl(data_type="Html", url=f"{base}.html"),
    ]


def register(mcp: FastMCP) -> None:
    """Registriert die Landesrecht-Tools am FastMCP-Server."""

    @mcp.tool(
        name="landesrecht_search",
        description=(
            "Sucht in der konsolidierten Sammlung des oesterreichischen "
            "Landesrechts (Applikation LrKons) -- die Landesgesetze der neun "
            "Bundeslaender. Liefert Liste von Paragrafen/Artikeln/Anlagen mit "
            "Metadaten und Dokument-IDs. Fuer Volltext anschliessend "
            "`landesrecht_get_norm(doc_id=...)`.\n\n"
            "Tipps:\n"
            "- `bundesland` moeglichst IMMER setzen (Enum: Wien, Niederoesterreich, "
            "...). Sonst kommen Treffer aller Laender gemischt -- und gleichnamige "
            "Gesetze (z. B. 'Bauordnung') gibt es je Land verschieden.\n"
            "- Fuer ein bestimmtes Gesetz: `titel='Bauordnung für Wien'` oder kurz "
            "`titel='Bauordnung'` plus `bundesland='Wien'`.\n"
            "- Fuer einen bestimmten Paragrafen zusaetzlich `paragraph_von=1`.\n\n"
            "WICHTIG: Erfinde NIE Landesgesetz-Inhalte aus Vorwissen -- die "
            "Materien unterscheiden sich je Bundesland stark. Nur Tool-Inhalt."
        ),
    )
    async def landesrecht_search(
        ctx: Context,
        bundesland: Annotated[
            BundeslandName | None,
            Field(description="Bundesland-Filter (dringend empfohlen)."),
        ] = None,
        suchworte: Annotated[
            str | None, Field(description="Volltext-Suchausdruck im Inhalt, optional.")
        ] = None,
        titel: Annotated[
            str | None,
            Field(description="Titel oder Abkuerzung, z. B. 'Bauordnung', 'Naturschutzgesetz'."),
        ] = None,
        paragraph_von: Annotated[
            int | None, Field(description="Erste Paragraf-/Artikel-/Anlagen-Nummer.")
        ] = None,
        paragraph_bis: Annotated[
            int | None, Field(description="Letzte Paragraf-/Artikel-/Anlagen-Nummer.")
        ] = None,
        abschnitt_typ: Annotated[
            Literal["Alle", "Paragraph", "Artikel", "Anlage"],
            Field(description="Welcher Strukturtyp gesucht wird."),
        ] = "Paragraph",
        fassung_vom: Annotated[
            str | None, Field(description="Geltungsdatum YYYY-MM-DD. Default = heute.")
        ] = None,
        max_results: Annotated[int, Field(ge=1, le=100)] = 20,
        page: Annotated[int, Field(ge=1)] = 1,
        force_refresh: Annotated[bool, Field()] = False,
    ) -> str:
        client, _cache, _settings = get_runtime(ctx)

        params: dict[str, str | int] = {
            "Abschnitt.Typ": abschnitt_typ,
            "DokumenteProSeite": to_page_size(max_results),
            "Seitennummer": page,
        }
        if bundesland:
            params[_bundesland_param(bundesland)] = "true"
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

        payload = await client.search(
            controller=CONTROLLER,
            application=APPLICATION,
            params=params,
            force_refresh=force_refresh,
        )
        result = parse_search_result(
            payload, application=APPLICATION, hit_parser=parse_landesrecht_hit
        )
        title = f"Landesrecht{f' {bundesland}' if bundesland else ''}: {titel or suchworte or 'Suche'}"
        if paragraph_von is not None and paragraph_bis == paragraph_von:
            title += f" § {paragraph_von}"
        elif paragraph_von is not None:
            title += f" ab § {paragraph_von}"
        return render_landesrecht_hits_md(
            title, result.total, result.page_number, result.page_size, result.hits  # type: ignore[arg-type]
        )

    @mcp.tool(
        name="landesrecht_get_norm",
        description=(
            "Liefert den Volltext einer einzelnen Landesnorm (Paragraf, Artikel, "
            "Anlage) als Markdown.\n\n"
            "Zwei Modi:\n"
            "1. Per Dokument-ID aus einem vorherigen `landesrecht_search`-Treffer "
            "(z. B. 'LWI40000074') -- der zuverlaessigste Pfad.\n"
            "2. Per Kombi `gesetz` + `paragraph` (+ `bundesland` zur Eindeutigkeit).\n\n"
            "WICHTIG: Einzige zulaessige Quelle fuer den Inhalt einer Landesnorm. "
            "Erklaere NIE eine Landesnorm aus Vorwissen."
        ),
    )
    async def landesrecht_get_norm(
        ctx: Context,
        doc_id: Annotated[
            str | None, Field(description="Dokument-ID aus Suchergebnis, z. B. 'LWI40000074'.")
        ] = None,
        gesetz: Annotated[
            str | None, Field(description="Titel oder Abkuerzung, z. B. 'Bauordnung'.")
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
        bundesland: Annotated[
            BundeslandName | None,
            Field(description="Bundesland (dringend empfohlen bei gesetz+paragraph)."),
        ] = None,
        abschnitt_typ: Annotated[
            Literal["Paragraph", "Artikel", "Anlage"],
            Field(description="Strukturtyp; Default Paragraph."),
        ] = "Paragraph",
        fassung_vom: Annotated[
            str | None, Field(description="Geltungsdatum YYYY-MM-DD.")
        ] = None,
        force_refresh: Annotated[bool, Field()] = False,
    ) -> str:
        client, cache, settings = get_runtime(ctx)

        if doc_id is None and (gesetz is None or paragraph is None):
            return (
                "Fehler: Bitte entweder `doc_id` angeben oder die Kombination "
                "`gesetz` + `paragraph` (am besten mit `bundesland`)."
            )

        # --- Schnellpfad: doc_id direkt ueber die stabile Content-URL laden ----
        if doc_id:
            try:
                header = {
                    "Dokument-ID": doc_id,
                    "RIS-Link": (
                        f"{_RIS_DOC_HOST}/Dokument.wxe?Abfrage=LrKons"
                        f"&Dokumentnummer={doc_id}"
                    ),
                }
                return await load_content_as_markdown(
                    client=client,
                    cache=cache,
                    doc_id=doc_id,
                    content_urls=_doc_content_urls(doc_id),
                    settings=settings,
                    metadata_header=header,
                )
            except Exception:  # noqa: BLE001
                if not (gesetz and paragraph is not None):
                    return (
                        f"Konnte Dokument `{doc_id}` nicht direkt laden. Pruefe die "
                        "Doku-ID, oder gib `gesetz` + `paragraph` (+ `bundesland`) an."
                    )

        # --- Suchpfad: gesetz + paragraph -------------------------------------
        effective_fassung = fassung_vom or date.today().isoformat()

        # Buchstaben-Variante: '880a' -> 880 fuer die API; im Picker unten
        # wird ueber _normalize_abschnitt exakt unterschieden.
        paragraph_api = api_paragraph_number(paragraph)

        async def _run_search(fassung: str | None):
            params: dict[str, str | int] = {
                "Abschnitt.Typ": abschnitt_typ,
                "DokumenteProSeite": "Ten",
            }
            if bundesland:
                params[_bundesland_param(bundesland)] = "true"
            if gesetz:
                params["Titel"] = gesetz
            if paragraph_api is not None:
                params["Abschnitt.Von"] = paragraph_api
                params["Abschnitt.Bis"] = paragraph_api
            if fassung:
                params["Fassung.FassungVom"] = fassung
            payload = await client.search(
                controller=CONTROLLER,
                application=APPLICATION,
                params=params,
                force_refresh=force_refresh,
            )
            return parse_search_result(
                payload, application=APPLICATION, hit_parser=parse_landesrecht_hit
            )

        result = await _run_search(effective_fassung)
        # Ausgelaufene Norm: mit Stichtag heute 0 Treffer -> ohne Datum nochmal.
        if not result.hits and fassung_vom is None:
            result = await _run_search(None)

        hit = pick_valid_bundesrecht_hit(
            result.hits, on_date=effective_fassung, paragraphnummer=paragraph  # type: ignore[arg-type]
        )
        if hit is None:
            return (
                f"Keine Landesnorm gefunden ({gesetz=}, §{paragraph}, "
                f"bundesland={bundesland}, fassung_vom={fassung_vom})."
            )

        header = {
            "Gesetz": str(hit.kurztitel or hit.abkuerzung or ""),
            "Bundesland": str(hit.bundesland or ""),
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
