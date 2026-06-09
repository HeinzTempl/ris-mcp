"""Normalisiert das XML-zu-JSON konvertierte OGD-RIS-Response.

Die RIS-API liefert ueberall ``#text`` und ``@attr`` Geschwister-Felder,
und Listen kommen mal als ein Dict (bei 1 Treffer) und mal als Liste
(bei >1). Dieser Adapter buegelt das glatt und liefert ein einheitliches,
pydantic-getyptes Datenmodell.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


# --------------------------------------------------------------------- helpers
def as_list(value: Any) -> list[Any]:
    """JSON-from-XML-Wart: liefert immer eine Liste, auch wenn nur 1 Element da ist."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def text(value: Any) -> str | None:
    """Holt ``#text`` aus einem RIS-Wrapper-Dict, ansonsten den Wert direkt."""
    if value is None:
        return None
    if isinstance(value, dict):
        if "#text" in value:
            return str(value["#text"])
        # xsi:nil? leeres Dict? → None
        if value.get("@xsi:nil") in ("true", True):
            return None
        return None
    return str(value)


def items(value: Any) -> list[str]:
    """``{"item": "..."} `` oder ``{"item": ["a","b"]}`` -> Liste von Strings."""
    if value is None:
        return []
    if isinstance(value, dict) and "item" in value:
        return [str(x) for x in as_list(value["item"]) if x is not None]
    if isinstance(value, list):
        return [str(x) for x in value if x is not None]
    return [str(value)]


# --------------------------------------------------------------------- models
class ContentUrl(BaseModel):
    data_type: str  # "Xml" | "Html" | "Pdf" | "Rtf"
    url: str


class HitMeta(BaseModel):
    """Gemeinsamer Metadaten-Header fuer alle RIS-Treffer."""

    id: str | None = None
    application: str | None = None
    organ: str | None = None
    document_url: str | None = None
    changed: str | None = None
    content_urls: list[ContentUrl] = Field(default_factory=list)


class BundesrechtHit(HitMeta):
    """Ein Treffer aus Bundesrecht/BrKons (Paragraph/Artikel/Anlage).

    Wird auch fuer Landesrecht (LrKons) wiederverwendet -- die Struktur ist
    identisch, ``bundesland`` ist nur dort gesetzt.
    """

    kurztitel: str | None = None
    abkuerzung: str | None = None
    artikel_paragraph_anlage: str | None = None
    paragraphnummer: str | None = None
    typ: str | None = None
    dokumenttyp: str | None = None
    kundmachungsorgan: str | None = None
    inkrafttretensdatum: str | None = None
    ausserkrafttretensdatum: str | None = None
    schlagworte: str | None = None
    gesetzesnummer: str | None = None
    eli: str | None = None
    gesamte_rechtsvorschrift_url: str | None = None
    bundesland: str | None = None  # nur bei Landesrecht (LrKons) gesetzt


class JudikaturHit(HitMeta):
    """Ein Treffer aus Judikatur (OGH, VfGH, VwGH, BVwG, ...)."""

    gericht: str | None = None
    dokumenttyp: str | None = None  # Rechtssatz | Entscheidungstext
    entscheidungsdatum: str | None = None
    veroeffentlicht: str | None = None
    geschaeftszahlen: list[str] = Field(default_factory=list)
    normen: list[str] = Field(default_factory=list)
    rechtssatznummern: list[str] = Field(default_factory=list)
    rechtsgebiete: list[str] = Field(default_factory=list)
    fachgebiete: list[str] = Field(default_factory=list)
    schlagworte: str | None = None
    entscheidungsart: str | None = None
    ecli: str | None = None


class SearchResult(BaseModel):
    """Container fuer eine RIS-Suche, generisch ueber den Treffer-Typ."""

    total: int = 0
    page_number: int = 1
    page_size: int = 0
    application: str = ""
    hits: list[BundesrechtHit | JudikaturHit] = Field(default_factory=list)


# --------------------------------------------------------------------- pickers
def pick_content_urls(reference: dict[str, Any]) -> list[ContentUrl]:
    """Extrahiert ContentUrls aus einem OgdDocumentReference."""
    cr = (
        reference.get("Data", {})
        .get("Dokumentliste", {})
        .get("ContentReference")
    )
    urls: list[ContentUrl] = []
    # ContentReference kann selbst eine Liste sein (z. B. mehrere Anlagen).
    for ref in as_list(cr):
        if not isinstance(ref, dict):
            continue
        url_list = (ref.get("Urls") or {}).get("ContentUrl")
        for u in as_list(url_list):
            if isinstance(u, dict) and u.get("Url") and u.get("DataType"):
                urls.append(ContentUrl(data_type=u["DataType"], url=u["Url"]))
    return urls


def best_content_url(
    urls: list[ContentUrl],
    *,
    prefer: tuple[str, ...] = ("Xml", "Html", "Rtf", "Pdf"),
) -> ContentUrl | None:
    for fmt in prefer:
        for u in urls:
            if u.data_type == fmt:
                return u
    return urls[0] if urls else None


# --------------------------------------------------------------------- parsers
def _common_meta(reference: dict[str, Any]) -> dict[str, Any]:
    meta = reference.get("Data", {}).get("Metadaten", {})
    tech = meta.get("Technisch") or {}
    allg = meta.get("Allgemein") or {}
    return {
        "id": tech.get("ID"),
        "application": tech.get("Applikation"),
        "organ": tech.get("Organ"),
        "document_url": allg.get("DokumentUrl"),
        "changed": allg.get("Geaendert"),
        "content_urls": pick_content_urls(reference),
    }


def parse_bundesrecht_hit(reference: dict[str, Any]) -> BundesrechtHit:
    base = _common_meta(reference)
    br = (reference.get("Data", {}).get("Metadaten", {}).get("Bundesrecht") or {})
    brkons = br.get("BrKons") or {}
    return BundesrechtHit(
        **base,
        kurztitel=br.get("Kurztitel"),
        eli=br.get("Eli"),
        abkuerzung=brkons.get("Abkuerzung"),
        artikel_paragraph_anlage=brkons.get("ArtikelParagraphAnlage"),
        paragraphnummer=brkons.get("Paragraphnummer"),
        typ=brkons.get("Typ"),
        dokumenttyp=brkons.get("Dokumenttyp"),
        kundmachungsorgan=brkons.get("Kundmachungsorgan"),
        inkrafttretensdatum=brkons.get("Inkrafttretensdatum"),
        ausserkrafttretensdatum=brkons.get("Ausserkrafttretensdatum"),
        schlagworte=brkons.get("Schlagworte"),
        gesetzesnummer=brkons.get("Gesetzesnummer"),
        gesamte_rechtsvorschrift_url=brkons.get("GesamteRechtsvorschriftUrl"),
    )


def parse_landesrecht_hit(reference: dict[str, Any]) -> BundesrechtHit:
    """Parst einen LrKons-Treffer (Landesrecht konsolidiert).

    Die Struktur spiegelt Bundesrecht: ``Metadaten.Landesrecht.LrKons`` ist das
    Analogon zu ``Metadaten.Bundesrecht.BrKons``; Kurztitel und Bundesland
    liegen eine Ebene hoeher unter ``Landesrecht``.
    """
    base = _common_meta(reference)
    lr = (reference.get("Data", {}).get("Metadaten", {}).get("Landesrecht") or {})
    lrkons = lr.get("LrKons") or {}
    return BundesrechtHit(
        **base,
        kurztitel=lr.get("Kurztitel"),
        bundesland=lr.get("Bundesland"),
        eli=lrkons.get("Eli"),
        abkuerzung=lrkons.get("Abkuerzung"),
        artikel_paragraph_anlage=lrkons.get("ArtikelParagraphAnlage"),
        paragraphnummer=lrkons.get("Paragraphnummer"),
        typ=lrkons.get("Typ"),
        dokumenttyp=lrkons.get("Dokumenttyp"),
        kundmachungsorgan=lrkons.get("Kundmachungsorgan"),
        inkrafttretensdatum=lrkons.get("Inkrafttretensdatum"),
        ausserkrafttretensdatum=lrkons.get("Ausserkrafttretensdatum"),
        schlagworte=lrkons.get("Schlagworte"),
        gesetzesnummer=lrkons.get("Gesetzesnummer"),
        gesamte_rechtsvorschrift_url=lrkons.get("GesamteRechtsvorschriftUrl"),
    )


def parse_judikatur_hit(reference: dict[str, Any]) -> JudikaturHit:
    base = _common_meta(reference)
    jk = (reference.get("Data", {}).get("Metadaten", {}).get("Judikatur") or {})
    allg = reference.get("Data", {}).get("Metadaten", {}).get("Allgemein") or {}

    # Gericht kann sowohl in Judikatur.Justiz.Gericht stehen als auch in Technisch.Organ
    gericht: str | None = None
    for app_key in ("Justiz", "Vfgh", "Vwgh", "Bvwg", "Lvwg", "Dsk", "Dok"):
        app_block = jk.get(app_key)
        if isinstance(app_block, dict) and app_block.get("Gericht"):
            gericht = app_block["Gericht"]
            break
    gericht = gericht or base.get("organ")

    # Rechtsgebiete, Rechtssatznummern, Fachgebiete leben unter Justiz.
    justiz = jk.get("Justiz") or {}
    rechtsgebiete = items(justiz.get("Rechtsgebiete"))
    fachgebiete = items(justiz.get("Fachgebiete"))
    rechtssatznummern = items(justiz.get("Rechtssatznummern"))

    return JudikaturHit(
        **base,
        gericht=gericht,
        dokumenttyp=jk.get("Dokumenttyp"),
        entscheidungsdatum=jk.get("Entscheidungsdatum"),
        veroeffentlicht=allg.get("Veroeffentlicht"),
        geschaeftszahlen=items(jk.get("Geschaeftszahl")),
        normen=items(jk.get("Normen")),
        rechtssatznummern=rechtssatznummern,
        rechtsgebiete=rechtsgebiete,
        fachgebiete=fachgebiete,
        schlagworte=jk.get("Schlagworte"),
        entscheidungsart=jk.get("Entscheidungsart"),
        ecli=jk.get("EuropeanCaseLawIdentifier"),
    )


def parse_search_result(
    payload: dict[str, Any],
    *,
    application: str,
    hit_parser,
) -> SearchResult:
    """Generischer Eintritt: nimmt das volle JSON-Response und parsed es.

    ``hit_parser`` ist entweder ``parse_bundesrecht_hit`` oder
    ``parse_judikatur_hit``.
    """
    root = (payload or {}).get("OgdSearchResult", {}).get("OgdDocumentResults") or {}
    hits_meta = root.get("Hits") or {}
    refs = as_list(root.get("OgdDocumentReference"))

    parsed = []
    for ref in refs:
        if isinstance(ref, dict):
            try:
                parsed.append(hit_parser(ref))
            except Exception:  # noqa: BLE001
                # Defensive: einen kaputten Treffer ueberspringen statt alles abzubrechen
                continue

    try:
        total = int(text(hits_meta) or "0")
    except (TypeError, ValueError):
        total = len(parsed)
    try:
        page_number = int(hits_meta.get("@pageNumber", "1"))
    except (TypeError, ValueError):
        page_number = 1
    try:
        page_size = int(hits_meta.get("@pageSize", "0"))
    except (TypeError, ValueError):
        page_size = 0

    return SearchResult(
        total=total,
        page_number=page_number,
        page_size=page_size,
        application=application,
        hits=parsed,
    )
