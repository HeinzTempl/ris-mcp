"""Convenience-Tool fuer die typische anwaltliche Recherche (Schicht 2).

Ein einziger Call buendelt, was sonst mehrere Tool-Aufrufe braeuchte:

* den Volltext der zentralen Norm (aus ``Bundesrecht``/``BrKons``) und
* die N einschlaegigsten OGH-Rechtssaetze MIT Leitsatz-Volltext
  (aus ``Judikatur``/``Justiz``).

Damit muss das Modell nicht mehr ``bundesrecht_get_norm`` + ``ogh_search``
+ pro Treffer ``judikatur_get_entscheidung`` orchestrieren -- genau die
Kette, an der kleinere lokale Modelle gescheitert sind.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Literal

from fastmcp import Context, FastMCP
from pydantic import Field

from ..adapters.response import (
    parse_bundesrecht_hit,
    parse_judikatur_hit,
    parse_search_result,
)
from ._common import (
    DATA_DISCIPLINE_FOOTER,
    api_paragraph_number,
    fetch_rechtssatz_texts,
    get_runtime,
    judikatur_text_blocks,
    load_content_as_markdown,
    pick_valid_bundesrecht_hit,
    to_page_size,
)


def register(mcp: FastMCP) -> None:
    """Registriert das Recherche-Convenience-Tool."""

    @mcp.tool(
        name="ris_recherche_norm",
        description=(
            "EIN-CALL-RECHERCHE zu einer oesterreichischen Bundes-Norm: liefert "
            "in einem einzigen Aufruf (a) den Volltext der Norm und (b) die N "
            "einschlaegigsten OGH-Rechtssaetze MIT Leitsatz-Volltext, alles als "
            "Markdown. Bevorzuge dieses Tool, wenn der User zu einer konkreten "
            "Gesetzesstelle 'Recherche', 'Judikatur', 'Rechtsprechung' oder "
            "'einschlaegige Entscheidungen' will -- es ersetzt die Kette "
            "bundesrecht_get_norm + ogh_search + judikatur_get_entscheidung.\n\n"
            "Pflicht: `gesetz` (z. B. 'ABGB') und `paragraph` (z. B. 871). "
            "Optional einschraenken mit `fachgebiet`, `rechtsgebiet`, "
            "`entscheidungsdatum_von/bis`.\n\n"
            "WICHTIG: Einzige zulaessige Quelle fuer Norm-Inhalt UND Judikatur. "
            "Reproduziere Geschaeftszahlen, Daten, Doku-IDs und Leitsaetze "
            "WORT-WOERTLICH aus dem Ergebnis -- erfinde nichts aus Vorwissen. "
            "Liefert das Tool keine Rechtssaetze, ist die Antwort 'keine "
            "einschlaegigen Rechtssaetze im RIS'."
        ),
    )
    async def ris_recherche_norm(
        ctx: Context,
        gesetz: Annotated[
            str, Field(description="Abkuerzung oder Titel, z. B. 'ABGB', 'UGB', 'KSchG'.")
        ],
        paragraph: Annotated[
            int | str,
            Field(
                description=(
                    "Paragrafennummer, z. B. 871 oder als String fuer "
                    "Buchstaben-Varianten wie '880a', '1319b'."
                )
            ),
        ],
        fachgebiet: Annotated[
            str | None,
            Field(
                description=(
                    "Optionaler Fachgebiet-Filter fuer die Judikatur, z. B. "
                    "'Bestandrecht', 'Konsumentenschutz und Produkthaftung'."
                )
            ),
        ] = None,
        rechtsgebiet: Annotated[
            Literal["Zivilrecht", "Strafrecht"] | None,
            Field(description="Rechtsgebiet-Filter fuer die Judikatur, Default Zivilrecht."),
        ] = "Zivilrecht",
        entscheidungsdatum_von: Annotated[
            str | None, Field(description="Judikatur ab YYYY-MM-DD.")
        ] = None,
        entscheidungsdatum_bis: Annotated[
            str | None, Field(description="Judikatur bis YYYY-MM-DD.")
        ] = None,
        max_rechtssaetze: Annotated[
            int,
            Field(description="Anzahl der zu liefernden OGH-Rechtssaetze.", ge=1, le=50),
        ] = 12,
        fassung_vom: Annotated[
            str | None,
            Field(description="Historische Norm-Fassung YYYY-MM-DD (Default heute)."),
        ] = None,
        force_refresh: Annotated[bool, Field(description="Cache umgehen.")] = False,
    ) -> str:
        client, cache, settings = get_runtime(ctx)
        norm_str = f"§ {paragraph} {gesetz}"

        # ----------------------------------------------------- Teil A: Norm
        norm_md = await _load_norm(
            client,
            cache,
            settings,
            gesetz=gesetz,
            paragraph=paragraph,
            fassung_vom=fassung_vom,
            force_refresh=force_refresh,
        )

        # --------------------------------------------- Teil B: OGH-Rechtssaetze
        params: dict[str, str | int] = {
            "Gericht": "OGH",
            "DokumenteProSeite": to_page_size(max_rechtssaetze),
            "Seitennummer": 1,
            "Sortierung.SortDirection": "Descending",
            "Sortierung.SortedByColumn": "Datum",
            "Dokumenttyp.SucheInRechtssaetzen": "true",
            "Norm": norm_str,
        }
        if rechtsgebiet:
            params["Rechtsgebiet"] = rechtsgebiet
        if fachgebiet:
            params["Fachgebiet"] = fachgebiet
        if entscheidungsdatum_von:
            params["EntscheidungsdatumVon"] = entscheidungsdatum_von
        if entscheidungsdatum_bis:
            params["EntscheidungsdatumBis"] = entscheidungsdatum_bis

        payload = await client.search(
            controller="Judikatur",
            application="Justiz",
            params=params,
            force_refresh=force_refresh,
        )
        result = parse_search_result(
            payload, application="Justiz", hit_parser=parse_judikatur_hit
        )
        # nur die angeforderte Menge (RIS rundet auf Seitengroesse auf)
        hits = result.hits[:max_rechtssaetze]
        texts = await fetch_rechtssatz_texts(
            client, cache, hits, settings=settings  # type: ignore[arg-type]
        )

        # ----------------------------------------------------- Zusammenbau
        lines = [
            f"# RIS-Recherche: {norm_str}",
            "",
            "_Norm-Volltext + einschlaegige OGH-Rechtsprechung in einem Dokument. "
            "Alle Angaben stammen woertlich aus dem RIS._",
            "",
            "## A. Norm im Volltext",
            "",
            norm_md.strip(),
            "",
        ]

        filt = []
        if rechtsgebiet:
            filt.append(rechtsgebiet)
        if fachgebiet:
            filt.append(f"Fachgebiet '{fachgebiet}'")
        if entscheidungsdatum_von or entscheidungsdatum_bis:
            filt.append(
                f"Zeitraum {entscheidungsdatum_von or '…'} bis "
                f"{entscheidungsdatum_bis or '…'}"
            )
        filt_str = f" (Filter: {', '.join(filt)})" if filt else ""

        lines.append(
            f"## B. Einschlaegige OGH-Rechtsprechung zu {norm_str}{filt_str}"
        )
        lines.append("")
        if not hits:
            lines.append(
                f"_Keine einschlaegigen Rechtssaetze im RIS{filt_str}._"
            )
            lines.append("")
        else:
            lines.append(
                f"Angezeigt: **{len(hits)}** Rechtssaetze (von **{result.total}** "
                f"im RIS zu {norm_str}), sortiert nach letzter Bestaetigung. "
                "Leitsatz jeweils direkt eingebettet."
            )
            lines.append("")
            blocks, has_rs = judikatur_text_blocks(hits, texts)
            lines.extend(blocks)
            if has_rs:
                lines.append(
                    "_Bei Rechtssaetzen ist das Datum die zuletzt unter dem "
                    "Rechtssatz aggregierte Entscheidung (= letzte Bestaetigung)._"
                )
            lines.append(
                "_Volle Entscheidungsbegruendung mit "
                "`judikatur_get_entscheidung(doc_id=...)` holen._"
            )
            lines.append("")

        lines.append(DATA_DISCIPLINE_FOOTER)
        return "\n".join(lines) + "\n"


async def _load_norm(
    client,
    cache,
    settings,
    *,
    gesetz: str,
    paragraph: int | str,
    fassung_vom: str | None,
    force_refresh: bool,
) -> str:
    """Volltext einer einzelnen Norm holen (gespiegelt aus bundesrecht_get_norm)."""
    # Stichtag: ohne explizite Angabe HEUTE -- sonst liefert RIS bei Normen mit
    # mehreren Zeitscheiben auch ausgelaufene Fassungen (z. B. § 356 UGB: alte
    # HGB-Linie bis 2006 + UGB-Fassung ab 2007) und hits[0] erwischt die falsche.
    effective_fassung = fassung_vom or date.today().isoformat()

    # Buchstaben-Variante: '880a' -> 880 fuer die API; im Picker via
    # _normalize_abschnitt exakt unterschieden.
    paragraph_api = api_paragraph_number(paragraph)

    async def _run_search(fassung: str | None):
        params: dict[str, str | int] = {
            "Abschnitt.Typ": "Paragraph",
            "DokumenteProSeite": "Ten",
            "Titel": gesetz,
        }
        if paragraph_api is not None:
            params["Abschnitt.Von"] = paragraph_api
            params["Abschnitt.Bis"] = paragraph_api
        if fassung:
            params["Fassung.FassungVom"] = fassung
        payload = await client.search(
            controller="Bundesrecht",
            application="BrKons",
            params=params,
            force_refresh=force_refresh,
        )
        return parse_search_result(
            payload, application="BrKons", hit_parser=parse_bundesrecht_hit
        )

    result = await _run_search(effective_fassung)
    # Fallback fuer echt ausgelaufene Normen: 0 Treffer mit Stichtag heute ->
    # ohne Datumsfilter erneut, Header markiert sie dann als ausgelaufen.
    if not result.hits and fassung_vom is None:
        result = await _run_search(None)
    if not result.hits:
        return (
            f"_Keine Norm gefunden ({gesetz} § {paragraph}"
            + (f", Fassung {fassung_vom}" if fassung_vom else "")
            + ")._"
        )

    # paragraphnummer mitgeben, damit Buchstaben-Varianten (§ 880a vs § 880)
    # ueber _normalize_abschnitt exakt selektiert werden.
    hit = pick_valid_bundesrecht_hit(
        result.hits, on_date=effective_fassung, paragraphnummer=paragraph
    ) or result.hits[0]
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
