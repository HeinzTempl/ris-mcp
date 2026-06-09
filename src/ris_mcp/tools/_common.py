"""Gemeinsame Helfer fuer alle Tools.

Hauptzwecke:
* Aus dem FastMCP-Kontext den ``RisClient`` und den ``Cache`` ziehen.
* Treffer-Listen zu Markdown-Tabellen rendern (kompakt fuer LLM-Konsum).
* Volltext eines Treffers laden (XML bevorzugt, sonst HTML, sonst PDF/RTF-Hinweis).
"""

from __future__ import annotations

import asyncio
from datetime import date
from typing import Any

from fastmcp import Context

from ..adapters.content_html import html_to_markdown
from ..adapters.content_xml import parse_ris_xml
from ..adapters.response import BundesrechtHit, ContentUrl, JudikaturHit, best_content_url
from ..cache import Cache
from ..client import RisClient
from ..config import Settings


PAGE_SIZE_MAP = {
    10: "Ten",
    20: "Twenty",
    50: "Fifty",
    100: "OneHundred",
}

# Zentraler Anti-Halluzinations-Hinweis fuer Tool-Outputs. Wird ans Ende
# jeder Treffer-/Disambig-Tabelle gehaengt, damit der LLM bei jedem
# einzelnen Tool-Result daran erinnert wird.
DATA_DISCIPLINE_FOOTER = (
    "---\n"
    "**Datendisziplin:** Geschaeftszahlen, Daten, Doku-IDs, ECLI und "
    "Normbezuege in deiner Antwort MUESSEN exakt aus dieser Tabelle stammen. "
    "Reproduziere Zahlen WORT-WOERTLICH (kein Veraendern, kein Schaetzen). "
    "Wenn du Zusatzangaben (z. B. Inhalt einer Norm) brauchst, rufe das "
    "passende Tool auf -- erfinde nichts aus dem Vorwissen."
)


def to_page_size(n: int) -> str:
    """Mappt eine LLM-freundliche Zahl auf das RIS-Enum."""
    # naechstgroessere Stufe nehmen
    for size in (10, 20, 50, 100):
        if n <= size:
            return PAGE_SIZE_MAP[size]
    return PAGE_SIZE_MAP[100]


def get_runtime(ctx: Context) -> tuple[RisClient, Cache | None, Settings]:
    """Holt Client, Cache und Settings aus dem Lifespan-Context."""
    lc = ctx.request_context.lifespan_context  # type: ignore[union-attr]
    return lc["client"], lc.get("cache"), lc["settings"]


def _normalize_abschnitt(s: str) -> str:
    """Normalisiert eine Abschnitts-Bezeichnung auf den nackten Nummern-Token.

    '§ 1319' -> '1319', '§ 1319a' -> '1319a', 'Art. 5' -> '5', '1319' -> '1319'.
    So unterscheiden wir '§ 1319' von '§ 1319a', obwohl RIS beiden dieselbe
    Paragraphnummer gibt.
    """
    import re

    s = (s or "").lower()
    s = re.sub(r"§|art\.?|artikel|anlage|nr\.?", "", s)
    return s.replace(" ", "").strip()


def api_paragraph_number(p: str | int | None) -> int | None:
    """Numerischen Anteil einer Paragrafenangabe fuer die RIS-API extrahieren.

    RIS' ``Abschnitt.Von``/``Abschnitt.Bis`` akzeptieren nur die nackte Zahl,
    nicht den Buchstaben-Variant: '880a' -> 880, '1319' -> 1319, ' 880 a ' ->
    880, 880 -> 880. Der Buchstabe lebt in ``artikel_paragraph_anlage`` und
    wird clientseitig im Picker via ``_normalize_abschnitt`` unterschieden.
    Gibt ``None`` zurueck, wenn nichts Numerisches drin steht.
    """
    if p is None:
        return None
    import re

    m = re.match(r"^\s*(\d+)", str(p))
    return int(m.group(1)) if m else None


def pick_valid_bundesrecht_hit(
    hits: list[BundesrechtHit],
    *,
    on_date: str | None = None,
    paragraphnummer: str | int | None = None,
) -> BundesrechtHit | None:
    """Waehlt aus mehreren BrKons-/LrKons-Treffern die am Stichtag GUELTIGE Fassung.

    RIS liefert ohne ``Fassung.FassungVom`` mehrere Zeitscheiben einer Norm
    (z. B. eine 2006 ausgelaufene HGB-Linie UND die ab 2007 geltende
    UGB-Fassung). Blind ``hits[0]`` zu nehmen erwischt mitunter die
    ausgelaufene -- mit deren altem Außerkrafttretensdatum im Body. Dieser
    Selektor bevorzugt die Fassung, die am ``on_date`` (Default heute) in
    Kraft ist: Inkrafttretensdatum <= Stichtag UND (kein Außerkrafttreten ODER
    Außerkrafttreten >= Stichtag). Datums-Strings sind ISO (YYYY-MM-DD), also
    lexikografisch vergleichbar.

    ``paragraphnummer``: Eine Suche nach ``Abschnitt.Von=1319`` liefert auch
    ``§ 1319a``, ``§ 1319b`` usw. -- und deren juengere Fassung wuerde sonst das
    eigentlich gesuchte ``§ 1319`` ueberholen. ACHTUNG: RIS vergibt den
    lettered-Varianten dieselbe ``Paragraphnummer`` (alle drei = "1319"),
    unterscheidbar sind sie nur ueber ``artikel_paragraph_anlage`` ("§ 1319"
    vs "§ 1319a"). Ist ``paragraphnummer`` gesetzt, werden daher zuerst nur
    EXAKT passende Treffer betrachtet (nur ``§ 1319``, nicht ``§ 1319a``);
    gibt es keine, wird auf alle Treffer zurueckgefallen.
    """
    if not hits:
        return None
    stichtag = on_date or date.today().isoformat()

    if paragraphnummer is not None:
        want = _normalize_abschnitt(str(paragraphnummer))
        exact = [
            h for h in hits
            if _normalize_abschnitt(h.artikel_paragraph_anlage or "") == want
        ]
        if exact:
            hits = exact

    def _ink(h: BundesrechtHit) -> str:
        return (h.inkrafttretensdatum or "")[:10]

    gueltig = [
        h
        for h in hits
        if _ink(h) <= stichtag
        and (not h.ausserkrafttretensdatum or h.ausserkrafttretensdatum[:10] >= stichtag)
    ]
    if gueltig:
        # juengste in Kraft getretene gueltige Fassung
        return max(gueltig, key=_ink)
    # keine am Stichtag gueltig -> juengste ueberhaupt (wird im Header als
    # ausgelaufen markiert)
    return max(hits, key=_ink)


# -------------------------------------------------------------- markdown views
def render_bundesrecht_hits_md(
    title: str,
    total: int,
    page_number: int,
    page_size: int,
    hits: list[BundesrechtHit],
) -> str:
    if not hits:
        return f"# {title}\n\n_Keine Treffer._\n"
    lines = [
        f"# {title}",
        "",
        f"Treffer gesamt: **{total}**, Seite {page_number} (Groesse {page_size})",
        "",
        "| # | Kurztitel | §/Art/Anlage | Inkrafttretensdatum | Doku-ID |",
        "|---|-----------|--------------|---------------------|---------|",
    ]
    for i, h in enumerate(hits, 1):
        lines.append(
            f"| {i} | {h.abkuerzung or h.kurztitel or '—'} "
            f"| {h.artikel_paragraph_anlage or '—'} "
            f"| {h.inkrafttretensdatum or '—'} "
            f"| `{h.id or '—'}` |"
        )
    lines.append("")
    lines.append("_Volltext eines Treffers mit `bundesrecht_get_norm(doc_id=...)` holen._")
    lines.append("")
    lines.append(DATA_DISCIPLINE_FOOTER)
    return "\n".join(lines) + "\n"


def render_landesrecht_hits_md(
    title: str,
    total: int,
    page_number: int,
    page_size: int,
    hits: list[BundesrechtHit],
) -> str:
    """Wie render_bundesrecht_hits_md, aber mit Bundesland-Spalte."""
    if not hits:
        return f"# {title}\n\n_Keine Treffer._\n"
    lines = [
        f"# {title}",
        "",
        f"Treffer gesamt: **{total}**, Seite {page_number} (Groesse {page_size})",
        "",
        "| # | Bundesland | Kurztitel | §/Art/Anlage | Inkrafttretensdatum | Doku-ID |",
        "|---|------------|-----------|--------------|---------------------|---------|",
    ]
    for i, h in enumerate(hits, 1):
        lines.append(
            f"| {i} | {h.bundesland or '—'} "
            f"| {h.abkuerzung or h.kurztitel or '—'} "
            f"| {h.artikel_paragraph_anlage or '—'} "
            f"| {h.inkrafttretensdatum or '—'} "
            f"| `{h.id or '—'}` |"
        )
    lines.append("")
    lines.append("_Volltext eines Treffers mit `landesrecht_get_norm(doc_id=...)` holen._")
    lines.append("")
    lines.append(DATA_DISCIPLINE_FOOTER)
    return "\n".join(lines) + "\n"


def render_judikatur_hits_md(
    title: str,
    total: int,
    page_number: int,
    page_size: int,
    hits: list[JudikaturHit],
) -> str:
    if not hits:
        return f"# {title}\n\n_Keine Treffer._\n"
    lines = [
        f"# {title}",
        "",
        f"Treffer gesamt: **{total}**, Seite {page_number} (Groesse {page_size})",
        "",
        "| # | Gericht | GZ | Datum* | Typ | Doku-ID |",
        "|---|---------|----|--------|-----|---------|",
    ]
    has_rechtssatz = False
    for i, h in enumerate(hits, 1):
        if (h.dokumenttyp or "").lower() == "rechtssatz":
            has_rechtssatz = True
        gz = "; ".join(h.geschaeftszahlen[:3])
        if len(h.geschaeftszahlen) > 3:
            gz += f" (+{len(h.geschaeftszahlen) - 3})"
        lines.append(
            f"| {i} | {h.gericht or '—'} "
            f"| {gz or '—'} "
            f"| {h.entscheidungsdatum or '—'} "
            f"| {h.dokumenttyp or '—'} "
            f"| `{h.id or '—'}` |"
        )
    lines.append("")
    if has_rechtssatz:
        lines.append(
            "_*Bei Rechtssaetzen ist das angegebene Datum die zuletzt unter dem "
            "Rechtssatz aggregierte Entscheidung (= letzte Bestaetigung), nicht "
            "das urspruengliche Rechtssatzdatum._"
        )
    lines.append(
        "_Volltext mit `judikatur_get_entscheidung(doc_id=...)` holen. Tipp: "
        "den `norm`-Parameter mitgeben, wenn ein bestimmter Normbezug interessiert "
        "(eine GZ kann in mehreren Rechtssaetzen vorkommen)._"
    )
    lines.append("")
    lines.append(DATA_DISCIPLINE_FOOTER)
    return "\n".join(lines) + "\n"


def render_judikatur_disambiguation_md(
    geschaeftszahl: str,
    candidates: list[JudikaturHit],
    norm_hint: str | None = None,
) -> str:
    """Wenn eine GZ in mehreren Rechtssaetzen / Entscheidungstexten vorkommt:
    Mini-Tabelle rendern statt blind den ersten Treffer als Volltext zu nehmen.
    """
    title = f"Mehrere Dokumente zur GZ `{geschaeftszahl}`"
    intro = (
        f"Im RIS sind unter dieser Geschaeftszahl **{len(candidates)} Dokumente** "
        "abgelegt (eine OGH-Entscheidung kann in mehreren Rechtssaetzen aufscheinen, "
        "wenn sie zu mehreren Rechtsfragen Stellung nimmt)."
    )
    if norm_hint:
        intro += (
            f"\n\nDie automatische Filterung nach Norm `{norm_hint}` hat keinen "
            "eindeutigen Treffer ergeben. Bitte waehle eine Doku-ID aus der Liste."
        )

    lines = [
        f"# {title}",
        "",
        intro,
        "",
        "| # | Typ | Datum | Wichtigste Normen | Doku-ID |",
        "|---|-----|-------|-------------------|---------|",
    ]
    for i, h in enumerate(candidates, 1):
        normen = "; ".join(h.normen[:3]) or "—"
        if len(h.normen) > 3:
            normen += f" (+{len(h.normen) - 3})"
        lines.append(
            f"| {i} | {h.dokumenttyp or '—'} "
            f"| {h.entscheidungsdatum or '—'} "
            f"| {normen} "
            f"| `{h.id or '—'}` |"
        )
    lines.append("")
    lines.append(
        "_Folge-Aufruf: `judikatur_get_entscheidung(doc_id=...)` mit der "
        "gewuenschten ID._"
    )
    lines.append("")
    lines.append(DATA_DISCIPLINE_FOOTER)
    return "\n".join(lines) + "\n"


# -------------------------------------------------------------- content fetch
async def load_content_as_markdown(
    client: RisClient,
    cache: Cache | None,
    doc_id: str,
    content_urls: list[ContentUrl],
    *,
    settings: Settings,
    metadata_header: dict[str, str] | None = None,
) -> str:
    """Best-effort Markdown-Volltext.

    Versucht XML zuerst (am saubersten), fallt auf HTML zurueck. Caching
    in ``document_cache`` per ``doc_id``.
    """
    if cache:
        cached = await cache.get_document(doc_id)
        if cached and cached.get("text_content"):
            return cached["text_content"]

    chosen = best_content_url(content_urls, prefer=("Xml", "Html"))
    if chosen is None:
        return _fallback_pdf_rtf_hint(doc_id, content_urls, metadata_header)

    raw, content_type = await client.fetch_content(chosen.url)

    if chosen.data_type == "Xml":
        parsed = parse_ris_xml(raw)
        if metadata_header:
            for k, v in metadata_header.items():
                parsed.metadata.setdefault(k, v)
        markdown = parsed.to_markdown()
    else:  # Html
        markdown = html_to_markdown(raw)

    if cache:
        await cache.put_document(
            doc_id=doc_id,
            source_url=chosen.url,
            content_type=content_type,
            raw_content=raw,
            text_content=markdown,
            metadata=metadata_header or {},
            ttl_seconds=settings.ttl_document_current_seconds,
        )
    return markdown


def _fallback_pdf_rtf_hint(
    doc_id: str,
    urls: list[ContentUrl],
    header: dict[str, str] | None,
) -> str:
    lines = [f"# Dokument `{doc_id}`"]
    if header:
        for k, v in header.items():
            lines.append(f"- **{k}**: {v}")
    lines.append("")
    lines.append("Nur in PDF/RTF verfuegbar -- Volltext bitte direkt herunterladen:")
    for u in urls:
        lines.append(f"- [{u.data_type}]({u.url})")
    return "\n".join(lines) + "\n"


# ------------------------------------------------------ rechtssatz-text (Hebel 2)
async def load_rechtssatz_text(
    client: RisClient,
    cache: Cache | None,
    *,
    doc_id: str,
    content_urls: list[ContentUrl],
    settings: Settings,
    max_chars: int = 700,
) -> str | None:
    """Holt nur den Rechtssatz-/Leitsatz-Text (Body) eines Dokuments.

    Eigener, leichter Cache-Key ``rs_text::<doc_id>`` -- beruehrt den
    Volltext-Cache von ``load_content_as_markdown`` NICHT, damit
    ``judikatur_get_entscheidung`` weiterhin seinen vollen Header liefert.
    Gibt ``None`` zurueck, wenn kein XML/Body verfuegbar (z. B. nur PDF).
    """
    cache_key = f"rs_text::{doc_id}"
    if cache:
        cached = await cache.get_document(cache_key)
        if cached and cached.get("text_content"):
            return cached["text_content"]

    # Nur XML taugt fuer saubere Body-Extraktion (HTML waere die Vorlesefassung).
    chosen = best_content_url(content_urls, prefer=("Xml",))
    if chosen is None or chosen.data_type != "Xml":
        return None

    try:
        raw, content_type = await client.fetch_content(chosen.url)
    except Exception:  # noqa: BLE001 -- ein einzelner Fetch darf die Suche nicht killen
        return None

    parsed = parse_ris_xml(raw)
    # Bei Rechtssatz-XML steht der Leitsatz im Feld <absatz ct="rechtssatz">,
    # das landet im Metadaten-Dict unter "Rechtssatz" (es gibt keinen
    # "Text"-Body-Marker wie bei Normen). Body-Markdown als Fallback.
    body = (parsed.metadata.get("Rechtssatz") or parsed.body_markdown or "").strip()
    if not body:
        return None
    # Einzeilig + Whitespace eindampfen -> block-/zitiersicher.
    body = " ".join(body.split())
    if len(body) > max_chars:
        body = (
            body[:max_chars].rstrip()
            + " … [gekuerzt -- Volltext via judikatur_get_entscheidung]"
        )

    if cache:
        await cache.put_document(
            doc_id=cache_key,
            source_url=chosen.url,
            content_type=content_type,
            raw_content=raw,
            text_content=body,
            metadata={"kind": "rechtssatz_snippet"},
            ttl_seconds=settings.ttl_judikatur_document_seconds,
        )
    return body


async def fetch_rechtssatz_texts(
    client: RisClient,
    cache: Cache | None,
    hits: list[JudikaturHit],
    *,
    settings: Settings,
    max_chars: int = 700,
) -> dict[str, str]:
    """Zieht die Leitsatz-Texte fuer alle Rechtssatz-Treffer parallel.

    Nur fuer ``dokumenttyp == 'rechtssatz'`` -- bei Entscheidungstexten waere
    der ``body`` die komplette (riesige) Entscheidung, das wollen wir hier
    nicht. Rate-Limiting erledigt der Token-Bucket im Client; ein einzelner
    Fehlschlag wird verschluckt. Rueckgabe: ``{doc_id: leitsatz_text}``.
    """
    targets = [
        h for h in hits
        if h.id and (h.dokumenttyp or "").lower() == "rechtssatz"
    ]
    if not targets:
        return {}

    async def _one(h: JudikaturHit) -> tuple[str, str | None]:
        txt = await load_rechtssatz_text(
            client,
            cache,
            doc_id=h.id,  # type: ignore[arg-type]  -- durch Filter oben garantiert
            content_urls=h.content_urls,
            settings=settings,
            max_chars=max_chars,
        )
        return h.id, txt  # type: ignore[return-value]

    results = await asyncio.gather(
        *(_one(h) for h in targets), return_exceptions=True
    )
    out: dict[str, str] = {}
    for r in results:
        if isinstance(r, Exception):
            continue
        doc_id, txt = r
        if txt:
            out[doc_id] = txt
    return out


def judikatur_text_blocks(
    hits: list[JudikaturHit],
    texts: dict[str, str],
    *,
    start_index: int = 1,
) -> tuple[list[str], bool]:
    """Rendert pro Treffer einen kompakten Block MIT Leitsatz.

    Wird von ``render_judikatur_hits_with_text_md`` (Such-Tools) UND von
    ``ris_recherche_norm`` (Recherche-Tool) genutzt, damit die Block-Optik
    identisch bleibt. Gibt ``(zeilen, has_rechtssatz)`` zurueck -- ohne H1
    und ohne Footer, das setzt der jeweilige Aufrufer.
    """
    lines: list[str] = []
    has_rechtssatz = False
    for i, h in enumerate(hits, start_index):
        is_rs = (h.dokumenttyp or "").lower() == "rechtssatz"
        if is_rs:
            has_rechtssatz = True
        heading = (h.rechtssatznummern[0] if h.rechtssatznummern else None) or (
            h.geschaeftszahlen[0] if h.geschaeftszahlen else "—"
        )
        gz = "; ".join(h.geschaeftszahlen[:5])
        if len(h.geschaeftszahlen) > 5:
            gz += f" (+{len(h.geschaeftszahlen) - 5})"
        normen = "; ".join(h.normen[:5])
        if len(h.normen) > 5:
            normen += f" (+{len(h.normen) - 5})"
        datum_label = "Datum (letzte Bestaetigung)" if is_rs else "Entscheidungsdatum"

        lines.append(f"### {i}. {heading}")
        lines.append(f"- **Gericht / Typ:** {h.gericht or '—'} / {h.dokumenttyp or '—'}")
        lines.append(f"- **Geschaeftszahl(en):** {gz or '—'}")
        lines.append(f"- **{datum_label}:** {h.entscheidungsdatum or '—'}")
        if normen:
            lines.append(f"- **Normen:** {normen}")
        lines.append(f"- **Doku-ID:** `{h.id or '—'}`")
        leitsatz = texts.get(h.id or "")
        if leitsatz:
            lines.append(f"- **Leitsatz:** {leitsatz}")
        elif is_rs:
            lines.append(
                "- **Leitsatz:** _(nicht als XML abrufbar -- Volltext via "
                "`judikatur_get_entscheidung(doc_id=...)`)_"
            )
        else:
            lines.append(
                "- **Volltext:** _Entscheidungstext via "
                "`judikatur_get_entscheidung(doc_id=...)`_"
            )
        lines.append("")
    return lines, has_rechtssatz


def render_judikatur_hits_with_text_md(
    title: str,
    total: int,
    page_number: int,
    page_size: int,
    hits: list[JudikaturHit],
    texts: dict[str, str],
) -> str:
    """Recherche-Block-Ansicht: pro Treffer ein kompakter Block MIT Leitsatz.

    Gegenueber der reinen Tabelle (``render_judikatur_hits_md``) steht der
    Rechtssatz-Volltext direkt beim jeweiligen Treffer -- so kann das Modell
    Relevanz beurteilen und wortwoertlich zitieren, ohne pro Treffer einen
    Folge-Call abzusetzen.
    """
    if not hits:
        return f"# {title}\n\n_Keine Treffer._\n"

    lines = [
        f"# {title}",
        "",
        f"Treffer gesamt: **{total}**, Seite {page_number} "
        f"(Groesse {page_size}). Leitsatz-Volltext direkt eingebettet.",
        "",
    ]
    blocks, has_rechtssatz = judikatur_text_blocks(hits, texts)
    lines.extend(blocks)

    if has_rechtssatz:
        lines.append(
            "_Bei Rechtssaetzen ist das Datum die zuletzt unter dem Rechtssatz "
            "aggregierte Entscheidung (= letzte Bestaetigung), nicht das "
            "urspruengliche Rechtssatzdatum._"
        )
    lines.append(
        "_Volle Begruendung einer Entscheidung mit "
        "`judikatur_get_entscheidung(doc_id=...)` holen._"
    )
    lines.append("")
    lines.append(DATA_DISCIPLINE_FOOTER)
    return "\n".join(lines) + "\n"
