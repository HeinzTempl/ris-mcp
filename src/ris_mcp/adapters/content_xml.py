"""XML-Dokumente von ris.bka.gv.at zu Markdown konvertieren.

Die RIS-XML-Dokumente folgen alle dem ``http://www.bka.gv.at``-Namespace mit
``risdok/nutzdaten/abschnitt`` als Wurzel. Die wichtigsten Element-Typen:

* ``ueberschrift typ="titel"`` -- Feldlabel im Metadaten-Kopf
* ``absatz typ="erltext" ct=<feldname>`` -- dazugehoeriger Wert
* ``ueberschrift typ="art" ct="text"`` -- Abschnitts-/Artikel-Ueberschrift
* ``ueberschrift typ="para" ct="text"`` -- Paragraf-Ueberschrift
* ``absatz typ="abs" ct="text"`` -- inhaltliche Absaetze (Vertragstext)
* ``gldsym`` -- Paragrafensymbol (``§ 1295.``), inline

Wir extrahieren Metadaten getrennt und liefern den Volltext als saubere
Markdown-Struktur.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from lxml import etree

NS = {"r": "http://www.bka.gv.at"}


# Welche ``ct``-Werte gehoeren zum Metadaten-Kopf? Reihenfolge spielt keine
# Rolle, wir uebernehmen die im Dokument.
_META_CT_WHITELIST = {
    "kurztitel", "kundmachungsorgan", "typ", "artikel_anlage",
    "ikra", "abkuerzung", "index", "schlagworte", "geaendert",
    "gesnr", "doknr", "adoknr", "stammnorm", "novellen", "bgblnr",
}


@dataclass
class ParsedDocument:
    """Strukturierte Sicht auf ein RIS-Dokument."""

    metadata: dict[str, str] = field(default_factory=dict)
    body_markdown: str = ""

    def to_markdown(self) -> str:
        """Kompletter Markdown-Output: Metadatenkopf + Volltext."""
        kurztitel = self.metadata.get("Kurztitel") or "RIS-Dokument"
        paragraf = self.metadata.get("§/Artikel/Anlage", "")
        head = f"# {kurztitel}".strip()
        if paragraf:
            head = f"{head}\n## {paragraf}"

        meta_lines = []
        for label, value in self.metadata.items():
            if label in ("Kurztitel", "§/Artikel/Anlage"):
                continue
            meta_lines.append(f"- **{label}**: {value}")
        meta_block = "\n".join(meta_lines)

        parts = [head]
        if meta_block:
            parts.append(meta_block)
        if self.body_markdown:
            parts.append("---")
            parts.append(self.body_markdown)
        return "\n\n".join(parts).strip() + "\n"


def parse_ris_xml(raw: bytes) -> ParsedDocument:
    """Hauptfunktion: rohes RIS-XML zu ParsedDocument."""
    parser = etree.XMLParser(recover=True, resolve_entities=False)
    root = etree.fromstring(raw, parser=parser)
    if root is None:
        return ParsedDocument()

    doc = ParsedDocument()
    current_label: str | None = None
    body_chunks: list[str] = []
    in_body = False

    for elem in root.iter():
        tag = etree.QName(elem).localname
        typ = elem.get("typ", "")
        ct = elem.get("ct", "")

        if tag == "ueberschrift" and typ == "titel":
            label = _text(elem)
            current_label = label
            # "Text" markiert den Beginn des Volltexts -- ab hier nicht mehr
            # in den Metadaten, sondern in den Body sammeln.
            if label.lower() == "text":
                in_body = True
            else:
                in_body = False
            continue

        if tag == "absatz" and typ == "erltext" and current_label and not in_body:
            if ct in _META_CT_WHITELIST or _META_CT_WHITELIST is None:
                doc.metadata[current_label] = _text(elem)
            else:
                doc.metadata[current_label] = _text(elem)
            continue

        if in_body:
            if tag == "ueberschrift" and ct == "text":
                level = "###" if typ == "art" else "####"
                body_chunks.append(f"{level} {_text(elem)}")
                continue
            if tag == "absatz" and ct == "text":
                body_chunks.append(_text(elem))
                continue

    doc.body_markdown = "\n\n".join(c for c in body_chunks if c)
    return doc


def _text(elem) -> str:
    """Sammelt allen Text in einem Element inkl. Tail von Kindelementen.

    Inline-Elemente wie ``<gldsym>§ 1295.</gldsym>`` werden mit ihrem Text
    eingebettet. ``<tab/>``, ``<feld/>`` und vergleichbares wird durch
    Leerzeichen ersetzt.
    """
    if elem is None:
        return ""
    parts: list[str] = []
    if elem.text:
        parts.append(elem.text)
    for child in elem:
        ct = etree.QName(child).localname
        if ct in ("tab", "feld", "br"):
            parts.append(" ")
        else:
            inner = _text(child)
            if inner:
                parts.append(inner)
        if child.tail:
            parts.append(child.tail)
    txt = "".join(parts)
    # Mehrfach-Whitespace eindampfen, NBSP normalisieren
    return " ".join(txt.replace("\xa0", " ").split()).strip()
