"""Tools fuer Judikatur (OGH, VfGH, VwGH, BVwG, LVwG, ...).

Drei Tools:

* ``ogh_search`` -- schmales Tool fuer Zivilrecht-Workflow, kein gericht-Param
* ``judikatur_search`` -- generisch mit gericht-Enum, fuer alle anderen Faelle
* ``judikatur_get_entscheidung`` -- Volltext einer Rechtssatz-/Entscheidungs-Doku
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastmcp import Context, FastMCP
from pydantic import Field

from ..adapters.response import ContentUrl, parse_judikatur_hit, parse_search_result
from ._common import (
    fetch_rechtssatz_texts,
    get_runtime,
    load_content_as_markdown,
    render_judikatur_disambiguation_md,
    render_judikatur_hits_md,
    render_judikatur_hits_with_text_md,
    to_page_size,
)

CONTROLLER = "Judikatur"

# Mapping von LLM-freundlichen Gericht-Namen zur RIS-Applikation +
# optionalem Gericht-Filter. ``Justiz`` ist der Sammeltopf fuer OGH/OLG/LG/BG.
GerichtName = Literal[
    "OGH", "OLG", "LG", "BG", "OPMS",
    "VfGH", "VwGH", "BVwG", "LVwG",
    "DSB",  # Datenschutzbehoerde
]

_GERICHT_MAP: dict[str, tuple[str, str | None]] = {
    "OGH":  ("Justiz", "OGH"),
    "OLG":  ("Justiz", "OLG"),
    "LG":   ("Justiz", "LG"),
    "BG":   ("Justiz", "BG"),
    "OPMS": ("Justiz", "OPMS"),
    "VfGH": ("Vfgh",   None),
    "VwGH": ("Vwgh",   None),
    "BVwG": ("Bvwg",   None),
    "LVwG": ("Lvwg",   None),
    "DSB":  ("Dsk",    None),
}


# Bundesdeutsche Schreibweisen, die in der oesterreichischen Rechtssprache (und
# damit im RIS-Volltext) anders lauten. Die Volltextsuche ist woertlich -- ein
# Modell, das "Schmerzensgeld" tippt, findet die AT-Entscheidungen nicht, weil
# dort durchgehend "Schmerzengeld" (ohne Fugen-s) steht. Bewusst klein und
# kuratiert gehalten, um Fehltreffer zu vermeiden.
_AT_SPELLING: dict[str, str] = {
    "schmerzensgeld": "Schmerzengeld",
}


# Host, unter dem RIS die Dokument-Volltexte ausliefert (nicht die OGD-API).
_RIS_DOC_HOST = "https://www.ris.bka.gv.at"


def _doc_content_urls(application: str, doc_id: str) -> list[ContentUrl]:
    """Baut die direkten Content-URLs (XML bevorzugt, HTML als Fallback) zu einer
    Judikatur-Doku.

    Die RIS-OGD-Suche kennt KEINEN Filter auf die Dokumentnummer -- ein Aufruf
    mit reiner ``doc_id`` lieferte sonst die ersten 20 von ~138.000 Dokumenten.
    Der Volltext liegt aber unter einem stabilen Pfad bereit:
    ``{host}/Dokumente/{Applikation}/{doc_id}/{doc_id}.{xml|html}``.
    """
    base = f"{_RIS_DOC_HOST}/Dokumente/{application}/{doc_id}/{doc_id}"
    return [
        ContentUrl(data_type="Xml", url=f"{base}.xml"),
        ContentUrl(data_type="Html", url=f"{base}.html"),
    ]


def _austrianize(suchworte: str) -> str:
    """Ersetzt bekannte bundesdeutsche Begriffe durch die AT-Schreibweise.

    Wortweise, case-insensitiv; unbekannte Tokens bleiben unveraendert.
    """
    import re

    def _repl(m: re.Match[str]) -> str:
        return _AT_SPELLING.get(m.group(0).lower(), m.group(0))

    if not _AT_SPELLING:
        return suchworte
    pattern = re.compile(
        r"\b(" + "|".join(re.escape(k) for k in _AT_SPELLING) + r")\b",
        re.IGNORECASE,
    )
    return pattern.sub(_repl, suchworte)


def register(mcp: FastMCP) -> None:
    """Registriert die drei Judikatur-Tools."""

    @mcp.tool(
        name="ogh_search",
        description=(
            "Sucht OGH-Entscheidungen (Oberster Gerichtshof) -- spezialisiert auf "
            "den Zivilrecht-Workflow. Zwei Modi laufen AUTOMATISCH je nach "
            "Eingabe:\n"
            "* Mit `suchworte` (Themen-/Sachverhaltssuche, z. B. 'Hundebiss', "
            "'Porsche Verkehrsunfall') sucht das Tool im VOLLTEXT der "
            "Entscheidungen UND in den Rechtssaetzen, neueste zuerst -- denn "
            "konkrete Sachverhaltsbegriffe stehen im Entscheidungstext, nicht im "
            "abstrakten Rechtssatz.\n"
            "* Nur mit `norm`/`geschaeftszahl` (ohne Suchworte) liefert es kompakt "
            "die Rechtssaetze mit eingebettetem LEITSATZ-VOLLTEXT -- ideal fuer "
            "'Judikatur zu § X'. (Fuer reine Norm-Recherche ist `ris_recherche_norm` "
            "noch bequemer.)\n\n"
            "SUCHSTRATEGIE:\n"
            "* `suchworte` werden UND-verknuepft (ALLE Woerter muessen vorkommen) "
            "-- jedes zusaetzliche Wort verengt stark. Lieber EIN praegnantes "
            "Stichwort als eine Wortkette.\n"
            "* Verwende oesterreichische Rechtsterminologie: 'Schmerzengeld' "
            "(NICHT das bundesdeutsche 'Schmerzensgeld'), 'Bestandvertrag', "
            "'Mietzins'. (Das Tool korrigiert 'Schmerzensgeld' automatisch und "
            "weist darauf hin, deckt aber nicht jeden Begriff ab.)\n"
            "* `norm` ist der staerkste Filter fuer eine Gesetzesstelle -- als "
            "`norm` setzen, nicht als Suchwort.\n"
            "* `fachgebiet` muss EXAKT der RIS-Taxonomie entsprechen; ein "
            "erfundener Wert liefert stumm 0 Treffer. Im Zweifel WEGLASSEN. (Das "
            "Tool faellt bei 0 Treffern automatisch auf die Suche ohne "
            "Fachgebiet-Filter zurueck und weist darauf hin.)\n\n"
            "WICHTIG: Ist die einzige zulaessige Quelle fuer OGH-Treffer. "
            "Erfinde NIE Geschaeftszahlen, Daten, Doku-IDs oder Leitsaetze aus "
            "Vorwissen. Wenn das Tool keine Treffer liefert, sage 'keine Treffer "
            "im RIS' -- rate nicht 'die mir bekannten Entscheidungen sind ...'."
        ),
    )
    async def ogh_search(
        ctx: Context,
        suchworte: Annotated[
            str | None, Field(description="Volltextausdruck im Inhalt der Entscheidung.")
        ] = None,
        norm: Annotated[
            str | None,
            Field(description="Norm-Bezug, z. B. '§ 879 ABGB' oder '§ 1295 ABGB'."),
        ] = None,
        geschaeftszahl: Annotated[
            str | None, Field(description="OGH-GZ, z. B. '1 Ob 145/22b'.")
        ] = None,
        entscheidungsdatum_von: Annotated[
            str | None, Field(description="ab YYYY-MM-DD.")
        ] = None,
        entscheidungsdatum_bis: Annotated[
            str | None, Field(description="bis YYYY-MM-DD.")
        ] = None,
        rechtsgebiet: Annotated[
            Literal["Zivilrecht", "Strafrecht"] | None,
            Field(description="Rechtsgebiet-Filter, Default Zivilrecht."),
        ] = "Zivilrecht",
        fachgebiet: Annotated[
            str | None,
            Field(
                description=(
                    "Fachgebiet (z. B. 'Schadenersatz nach Verkehrsunfall', "
                    "'Bestandrecht', 'Konsumentenschutz und Produkthaftung', "
                    "'Versicherungsvertragsrecht', 'Erbrecht und "
                    "Verlassenschaftsverfahren')."
                )
            ),
        ] = None,
        nur_rechtssaetze: Annotated[
            bool | None,
            Field(
                description=(
                    "Dokumenttyp-Steuerung. None (Default) = AUTOMATISCH: mit "
                    "`suchworte` auch Entscheidungstexte, sonst nur Rechtssaetze. "
                    "True = strikt nur Rechtssaetze. False = strikt auch "
                    "Entscheidungstexte. Im Normalfall None lassen."
                )
            ),
        ] = None,
        include_rechtssatz_text: Annotated[
            bool,
            Field(
                description=(
                    "Leitsatz-Volltext jedes Rechtssatzes gleich mitliefern "
                    "(Default True). Auf False setzen, wenn nur die Trefferliste "
                    "(GZ/Datum/Doku-ID) ohne Texte gebraucht wird -- spart Fetches."
                )
            ),
        ] = True,
        max_results: Annotated[int, Field(ge=1, le=100)] = 15,
        page: Annotated[int, Field(ge=1)] = 1,
        force_refresh: Annotated[bool, Field()] = False,
    ) -> str:
        client, cache, settings = get_runtime(ctx)

        # effektiver Suchbegriff (kann durch AT-Normalisierung ersetzt werden)
        effective_suchworte = suchworte

        def _build_params(*, include_texte: bool, use_fachgebiet: bool) -> dict[str, str | int]:
            params: dict[str, str | int] = {
                "Gericht": "OGH",
                "DokumenteProSeite": to_page_size(max_results),
                "Seitennummer": page,
                "Sortierung.SortDirection": "Descending",
                "Sortierung.SortedByColumn": "Datum",
                "Dokumenttyp.SucheInRechtssaetzen": "true",
            }
            if include_texte:
                params["Dokumenttyp.SucheInEntscheidungstexten"] = "true"
            if effective_suchworte:
                params["Suchworte"] = effective_suchworte
            if norm:
                params["Norm"] = norm
            if geschaeftszahl:
                params["Geschaeftszahl"] = geschaeftszahl
            if entscheidungsdatum_von:
                params["EntscheidungsdatumVon"] = entscheidungsdatum_von
            if entscheidungsdatum_bis:
                params["EntscheidungsdatumBis"] = entscheidungsdatum_bis
            if rechtsgebiet:
                params["Rechtsgebiet"] = rechtsgebiet
            if use_fachgebiet and fachgebiet:
                params["Fachgebiet"] = fachgebiet
            return params

        async def _run(params: dict[str, str | int]):
            payload = await client.search(
                controller=CONTROLLER,
                application="Justiz",
                params=params,
                force_refresh=force_refresh,
            )
            return parse_search_result(
                payload, application="Justiz", hit_parser=parse_judikatur_hit
            )

        # Dokumenttyp automatisch waehlen: eine Themen-/Sachverhaltssuche
        # (freie Suchworte) muss in die Entscheidungstexte -- die abstrakten
        # Rechtssaetze enthalten keine Sachverhaltsbegriffe ("Hund", "Porsche").
        # Reine Norm-/GZ-Suche bleibt kompakt bei den Rechtssaetzen.
        if nur_rechtssaetze is True:
            include_texte = False
        elif nur_rechtssaetze is False:
            include_texte = True
        else:  # auto
            include_texte = bool(suchworte)

        use_fachgebiet = True
        notes: list[str] = []

        result = await _run(
            _build_params(include_texte=include_texte, use_fachgebiet=use_fachgebiet)
        )

        # Stufe 1: ein (oft geratener) Fachgebiet-Filter liefert stumm 0 Treffer.
        if result.total == 0 and use_fachgebiet and fachgebiet:
            use_fachgebiet = False
            result = await _run(
                _build_params(include_texte=include_texte, use_fachgebiet=use_fachgebiet)
            )
            notes.append(
                f"Fachgebiet-Filter '{fachgebiet}' lieferte 0 Treffer "
                "(moeglicherweise kein gueltiger RIS-Taxonomie-Wert) und wurde "
                "fuer dieses Ergebnis entfernt."
            )

        # Stufe 2: bundesdeutsche Schreibweise (z. B. "Schmerzensgeld") findet im
        # AT-Volltext kaum etwas. AT-Variante probieren; uebernehmen, wenn sie
        # MEHR Treffer bringt (faengt auch den Fall "2 alte vs. 56 aktuelle" ab).
        if suchworte:
            at_variant = _austrianize(suchworte)
            if at_variant != suchworte:
                effective_suchworte = at_variant
                at_result = await _run(
                    _build_params(include_texte=include_texte, use_fachgebiet=use_fachgebiet)
                )
                if at_result.total > result.total:
                    result = at_result
                    notes.append(
                        f"Begriff '{suchworte}' angepasst auf oesterreichische "
                        f"Schreibweise '{at_variant}' (deutlich mehr Treffer)."
                    )
                else:
                    effective_suchworte = suchworte  # AT-Variante half nicht

        # Stufe 3: explizit erzwungene Rechtssatz-Suche, die leer bleibt -> einmal
        # auf Entscheidungstexte ausweiten (Sachverhaltsbegriffe leben dort).
        if result.total == 0 and not include_texte and effective_suchworte:
            include_texte = True
            result = await _run(
                _build_params(include_texte=include_texte, use_fachgebiet=use_fachgebiet)
            )
            notes.append(
                "Keine Treffer in den Rechtssaetzen -- Suche auf Entscheidungstexte "
                "ausgeweitet (Sachverhaltsbegriffe stehen meist dort)."
            )

        title = f"OGH-Suche: {norm or effective_suchworte or 'OGH'}"
        if include_rechtssatz_text and result.hits:
            texts = await fetch_rechtssatz_texts(
                client, cache, result.hits, settings=settings  # type: ignore[arg-type]
            )
            body = render_judikatur_hits_with_text_md(
                title=title,
                total=result.total,
                page_number=result.page_number,
                page_size=result.page_size,
                hits=result.hits,  # type: ignore[arg-type]
                texts=texts,
            )
        else:
            body = render_judikatur_hits_md(
                title=title,
                total=result.total,
                page_number=result.page_number,
                page_size=result.page_size,
                hits=result.hits,  # type: ignore[arg-type]
            )
        if notes:
            note_block = "\n".join(f"> _{n}_" for n in notes)
            body = f"{note_block}\n\n{body}"
        return body

    @mcp.tool(
        name="judikatur_search",
        description=(
            "Generische Judikatur-Suche fuer alle Gerichte ausser OGH-Standardfall "
            "(dafuer ist `ogh_search` besser). Pflichtparameter: `gericht` "
            "(OGH, OLG, LG, BG, OPMS, VfGH, VwGH, BVwG, LVwG, DSB).\n\n"
            "Zwei Modi laufen AUTOMATISCH je nach Eingabe -- wie bei `ogh_search`:\n"
            "* Mit `suchworte` (Themen-/Sachverhaltssuche) sucht das Tool im "
            "VOLLTEXT der Entscheidungen UND in den Rechtssaetzen, neueste zuerst -- "
            "denn konkrete Sachverhaltsbegriffe stehen im Entscheidungstext, nicht "
            "im abstrakten Rechtssatz.\n"
            "* Nur mit `norm`/`geschaeftszahl` (ohne Suchworte) liefert es kompakt "
            "die Rechtssaetze.\n\n"
            "SUCHSTRATEGIE: `suchworte` werden UND-verknuepft -- lieber EIN "
            "praegnantes Stichwort als eine Wortkette. Oesterreichische "
            "Terminologie verwenden (z. B. 'Schmerzengeld', nicht 'Schmerzensgeld').\n\n"
            "WICHTIG: Einzige zulaessige Quelle fuer Treffer der angegebenen "
            "Gerichtsbarkeit. Erfinde NIE Geschaeftszahlen, Daten oder "
            "Doku-IDs aus Vorwissen. Keine Treffer = sage 'keine Treffer im RIS'."
        ),
    )
    async def judikatur_search(
        ctx: Context,
        gericht: Annotated[
            GerichtName,
            Field(description="Gericht: OGH, OLG, LG, BG, OPMS, VfGH, VwGH, BVwG, LVwG, DSB."),
        ],
        suchworte: Annotated[str | None, Field()] = None,
        norm: Annotated[str | None, Field()] = None,
        geschaeftszahl: Annotated[str | None, Field()] = None,
        entscheidungsdatum_von: Annotated[str | None, Field()] = None,
        entscheidungsdatum_bis: Annotated[str | None, Field()] = None,
        nur_rechtssaetze: Annotated[
            bool | None,
            Field(
                description=(
                    "Dokumenttyp-Steuerung. None (Default) = AUTOMATISCH: mit "
                    "`suchworte` auch Entscheidungstexte, sonst nur Rechtssaetze. "
                    "True = strikt nur Rechtssaetze. False = strikt auch "
                    "Entscheidungstexte. Im Normalfall None lassen."
                )
            ),
        ] = None,
        include_rechtssatz_text: Annotated[
            bool,
            Field(
                description=(
                    "Leitsatz-Volltext jedes Rechtssatz-Treffers gleich mitliefern "
                    "(Default True). False = nur Trefferliste ohne Texte."
                )
            ),
        ] = True,
        max_results: Annotated[int, Field(ge=1, le=100)] = 20,
        page: Annotated[int, Field(ge=1)] = 1,
        force_refresh: Annotated[bool, Field()] = False,
    ) -> str:
        client, cache, settings = get_runtime(ctx)
        application, gericht_filter = _GERICHT_MAP[gericht]

        # effektiver Suchbegriff (kann durch AT-Normalisierung ersetzt werden)
        effective_suchworte = suchworte

        def _build_params(*, include_texte: bool) -> dict[str, str | int]:
            params: dict[str, str | int] = {
                "DokumenteProSeite": to_page_size(max_results),
                "Seitennummer": page,
                "Sortierung.SortDirection": "Descending",
                "Sortierung.SortedByColumn": "Datum",
                "Dokumenttyp.SucheInRechtssaetzen": "true",
            }
            if include_texte:
                params["Dokumenttyp.SucheInEntscheidungstexten"] = "true"
            if gericht_filter:
                params["Gericht"] = gericht_filter
            if effective_suchworte:
                params["Suchworte"] = effective_suchworte
            if norm:
                params["Norm"] = norm
            if geschaeftszahl:
                params["Geschaeftszahl"] = geschaeftszahl
            if entscheidungsdatum_von:
                params["EntscheidungsdatumVon"] = entscheidungsdatum_von
            if entscheidungsdatum_bis:
                params["EntscheidungsdatumBis"] = entscheidungsdatum_bis
            return params

        async def _run(params: dict[str, str | int]):
            payload = await client.search(
                controller=CONTROLLER,
                application=application,
                params=params,
                force_refresh=force_refresh,
            )
            return parse_search_result(
                payload, application=application, hit_parser=parse_judikatur_hit
            )

        # Dokumenttyp automatisch waehlen -- gleiche Logik wie ogh_search:
        # Themen-/Sachverhaltssuche muss in die Entscheidungstexte, reine
        # Norm-/GZ-Suche bleibt kompakt bei den Rechtssaetzen.
        if nur_rechtssaetze is True:
            include_texte = False
        elif nur_rechtssaetze is False:
            include_texte = True
        else:  # auto
            include_texte = bool(suchworte)

        notes: list[str] = []
        result = await _run(_build_params(include_texte=include_texte))

        # AT-Schreibweise (z. B. "Schmerzensgeld" -> "Schmerzengeld") probieren;
        # uebernehmen, wenn sie MEHR Treffer bringt.
        if suchworte:
            at_variant = _austrianize(suchworte)
            if at_variant != suchworte:
                effective_suchworte = at_variant
                at_result = await _run(_build_params(include_texte=include_texte))
                if at_result.total > result.total:
                    result = at_result
                    notes.append(
                        f"Begriff '{suchworte}' angepasst auf oesterreichische "
                        f"Schreibweise '{at_variant}' (deutlich mehr Treffer)."
                    )
                else:
                    effective_suchworte = suchworte

        # Erzwungene Rechtssatz-Suche, die leer bleibt -> einmal auf
        # Entscheidungstexte ausweiten (Sachverhaltsbegriffe leben dort).
        if result.total == 0 and not include_texte and effective_suchworte:
            include_texte = True
            result = await _run(_build_params(include_texte=include_texte))
            notes.append(
                "Keine Treffer in den Rechtssaetzen -- Suche auf Entscheidungstexte "
                "ausgeweitet (Sachverhaltsbegriffe stehen meist dort)."
            )

        title = f"{gericht}-Suche: {norm or effective_suchworte or gericht}"
        if include_rechtssatz_text and result.hits:
            texts = await fetch_rechtssatz_texts(
                client, cache, result.hits, settings=settings  # type: ignore[arg-type]
            )
            body = render_judikatur_hits_with_text_md(
                title=title,
                total=result.total,
                page_number=result.page_number,
                page_size=result.page_size,
                hits=result.hits,  # type: ignore[arg-type]
                texts=texts,
            )
        else:
            body = render_judikatur_hits_md(
                title=title,
                total=result.total,
                page_number=result.page_number,
                page_size=result.page_size,
                hits=result.hits,  # type: ignore[arg-type]
            )
        if notes:
            note_block = "\n".join(f"> _{n}_" for n in notes)
            body = f"{note_block}\n\n{body}"
        return body

    @mcp.tool(
        name="judikatur_get_entscheidung",
        description=(
            "Liefert den Volltext einer Rechtssatz- oder Entscheidungsdoku aus dem "
            "RIS als Markdown.\n\n"
            "Drei Wege:\n"
            "1. Per Dokument-ID (eindeutig, immer der zuverlaessigste Pfad).\n"
            "2. Per `gericht` + `geschaeftszahl` -- wenn die GZ in mehreren "
            "Dokumenten vorkommt (eine OGH-Entscheidung kann in mehreren "
            "Rechtssaetzen zu verschiedenen Rechtsfragen aufscheinen), kommt "
            "eine Auswahltabelle zurueck.\n"
            "3. Wie 2., aber mit `norm`-Kontext (z. B. `norm='§ 879 ABGB'`), um "
            "automatisch den passenden Rechtssatz zu treffen.\n\n"
            "Mit `dokumenttyp` kann auf 'Rechtssatz' (abstrakte Regel mit "
            "Bestaetigungs-Folgerechtsprechung) oder 'Entscheidungstext' (volle "
            "Begruendung der konkreten Entscheidung) eingeschraenkt werden."
        ),
    )
    async def judikatur_get_entscheidung(
        ctx: Context,
        doc_id: Annotated[
            str | None,
            Field(description="ID wie 'JJR_19880615_OGH0002_009OBA00118_8800000_002'."),
        ] = None,
        gericht: Annotated[
            GerichtName | None, Field(description="Falls keine doc_id: Gericht.")
        ] = None,
        geschaeftszahl: Annotated[
            str | None, Field(description="Falls keine doc_id: Geschaeftszahl.")
        ] = None,
        norm: Annotated[
            str | None,
            Field(
                description=(
                    "Optionaler Norm-Kontext, z. B. '§ 879 ABGB'. Wenn die GZ in "
                    "mehreren Rechtssaetzen vorkommt, wird der Rechtssatz mit "
                    "passendem Normbezug bevorzugt."
                )
            ),
        ] = None,
        dokumenttyp: Annotated[
            Literal["Alle", "Rechtssatz", "Entscheidungstext"],
            Field(
                description=(
                    "'Rechtssatz' = abstrakte Regel mit Folgerechtsprechung; "
                    "'Entscheidungstext' = vollstaendige Entscheidung mit Begruendung; "
                    "'Alle' = beide Typen (default)."
                )
            ),
        ] = "Alle",
        force_refresh: Annotated[bool, Field()] = False,
    ) -> str:
        client, cache, settings = get_runtime(ctx)

        if doc_id is None and not (gericht and geschaeftszahl):
            return (
                "Fehler: Bitte entweder `doc_id` angeben oder die Kombination "
                "`gericht` + `geschaeftszahl`."
            )

        application: str
        gericht_filter: str | None = None
        if gericht:
            application, gericht_filter = _GERICHT_MAP[gericht]
        else:
            application = "Justiz"  # Standardannahme, wenn nur doc_id da ist

        # --- Schnellpfad: doc_id direkt ueber die Content-URL laden -----------
        # Die OGD-Suche hat keinen Dokumentnummer-Filter; eine reine doc_id-Suche
        # liefert sonst Muell. Stattdessen den stabilen Volltext-Pfad nutzen.
        if doc_id:
            try:
                header = {
                    "Dokument-ID": doc_id,
                    "RIS-Link": (
                        f"{_RIS_DOC_HOST}/Dokument.wxe?Abfrage={application}"
                        f"&Dokumentnummer={doc_id}"
                    ),
                }
                if gericht:
                    header["Gericht"] = gericht
                md = await load_content_as_markdown(
                    client=client,
                    cache=cache,
                    doc_id=doc_id,
                    content_urls=_doc_content_urls(application, doc_id),
                    settings=settings,
                    metadata_header=header,
                )
                return md
            except Exception:  # noqa: BLE001 -- direkter Pfad fehlgeschlagen
                # Wenn wir auch GZ haben, faellt es unten auf die Suche zurueck;
                # sonst klare Rueckmeldung statt der alten 138k-Disambig.
                if not (gericht and geschaeftszahl):
                    return (
                        f"Konnte Dokument `{doc_id}` nicht direkt laden. Pruefe die "
                        "Doku-ID; bei einem anderen Gericht als OGH/OLG/LG bitte "
                        "zusaetzlich `gericht` angeben (z. B. gericht='VfGH')."
                    )

        params: dict[str, str | int] = {"DokumenteProSeite": "Twenty"}
        if gericht_filter:
            params["Gericht"] = gericht_filter
        if geschaeftszahl:
            params["Geschaeftszahl"] = geschaeftszahl
        if norm:
            params["Norm"] = norm
        if dokumenttyp == "Rechtssatz":
            params["Dokumenttyp.SucheInRechtssaetzen"] = "true"
        elif dokumenttyp == "Entscheidungstext":
            params["Dokumenttyp.SucheInEntscheidungstexten"] = "true"
        # "Alle": kein Dokumenttyp-Filter -> RIS liefert beides

        payload = await client.search(
            controller=CONTROLLER,
            application=application,
            params=params,
            force_refresh=force_refresh,
        )
        result = parse_search_result(
            payload, application=application, hit_parser=parse_judikatur_hit
        )

        if not result.hits:
            return (
                f"Keine Treffer ({doc_id=}, {gericht=}, {geschaeftszahl=}, "
                f"{norm=}, {dokumenttyp=})."
            )

        # --- Auswahl der konkreten Treffer-Doku --------------------------
        candidates: list = list(result.hits)  # JudikaturHit erwartet, aber generisch

        # 1) doc_id ist immer eindeutig: direkten Treffer raussuchen.
        if doc_id:
            matching = [h for h in candidates if h.id == doc_id]
            if matching:
                return await _serve_hit(
                    matching[0], client=client, cache=cache, settings=settings
                )
            # doc_id nicht gefunden -> als Disambig anbieten, war wohl falsch
            return render_judikatur_disambiguation_md(
                geschaeftszahl=geschaeftszahl or doc_id,
                candidates=candidates,
                norm_hint=norm,
            )

        # 2) Wenn norm gesetzt: Treffer mit passendem Normbezug bevorzugen.
        if norm:
            user_tokens = _norm_tokens(norm)
            with_norm = [
                h for h in candidates
                if any(user_tokens <= _norm_tokens(n) for n in h.normen)
            ]
            if len(with_norm) == 1:
                return await _serve_hit(
                    with_norm[0], client=client, cache=cache, settings=settings
                )
            if len(with_norm) > 1:
                candidates = with_norm  # weiter unten Disambig zeigen

        # 3) Wenn jetzt nur ein Treffer da ist: direkt servieren.
        if len(candidates) == 1:
            return await _serve_hit(
                candidates[0], client=client, cache=cache, settings=settings
            )

        # 4) Mehrere Treffer und keine eindeutige Wahl: Disambig-Tabelle.
        return render_judikatur_disambiguation_md(
            geschaeftszahl=geschaeftszahl or "",
            candidates=candidates,
            norm_hint=norm,
        )


async def _serve_hit(hit, *, client, cache, settings) -> str:
    """Header bauen und Volltext laden -- mit Rechtssatz-spezifischem Datums-Label."""
    is_rechtssatz = (hit.dokumenttyp or "").lower() == "rechtssatz"
    gz = "; ".join(hit.geschaeftszahlen[:5])
    if len(hit.geschaeftszahlen) > 5:
        gz += f" (+{len(hit.geschaeftszahlen) - 5})"

    datum_label = "Letzte Bestaetigung" if is_rechtssatz else "Entscheidungsdatum"
    header = {
        "Gericht": str(hit.gericht or ""),
        "Dokumenttyp": str(hit.dokumenttyp or ""),
        "Geschaeftszahl(en)": gz,
        datum_label: str(hit.entscheidungsdatum or ""),
        "Dokument-ID": str(hit.id or ""),
        "RIS-Link": str(hit.document_url or ""),
    }
    if is_rechtssatz and hit.veroeffentlicht:
        header["Erstmals veroeffentlicht"] = str(hit.veroeffentlicht)
    if hit.normen:
        header["Normen"] = "; ".join(hit.normen[:5]) + (
            f" (+{len(hit.normen) - 5})" if len(hit.normen) > 5 else ""
        )

    return await load_content_as_markdown(
        client=client,
        cache=cache,
        doc_id=hit.id or "unknown",
        content_urls=hit.content_urls,
        settings=settings,
        metadata_header=header,
    )


def _norm_tokens(norm: str) -> frozenset[str]:
    """Norm-String zu Token-Set normalisieren -- robust gegen Reihenfolge.

    '§ 879 ABGB' -> {'§879', 'abgb'}
    'ABGB §879 AIIc' -> {'abgb', '§879', 'aiic'}
    Damit wir testen koennen: user_tokens <= ris_tokens.
    """
    import re

    tokens: set[str] = set()
    for tok in re.findall(r"§\s*\d+[a-z]?|\d+[a-z]?|[a-zA-ZÀ-ſ]+", norm.lower()):
        # '§ 879' -> '§879'
        tokens.add(tok.replace(" ", ""))
    return frozenset(tokens)
