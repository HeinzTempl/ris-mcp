"""HTML-Dokumente von ris.bka.gv.at zu Markdown konvertieren.

Fallback, wenn kein XML verfuegbar ist (z. B. bei ``GesamteRechtsvorschriftUrl``,
die nur HTML liefert). Verlaesslicher fuer einzelne Paragrafen ist ``content_xml``.
"""

from __future__ import annotations

import re

from bs4 import BeautifulSoup
from markdownify import markdownify

# In den RIS-HTML-Dokumenten sind die Bildschirmleser-Hinweise als zusaetzlicher
# Lauftext eingebettet ("Paragraph 1295," nach "§ 1295.", "Absatz eins," nach
# "(1)" etc.). Wir filtern die per Klasse ``Vorlesefassung`` raus, falls
# vorhanden, ausserdem nehmen wir die Skripte/Style-Tags raus.
_ZU_ENTFERNENDE_KLASSEN = (
    "Vorlesefassung", "VorlesefassungInline", "VlfInline",
)


def html_to_markdown(raw: bytes) -> str:
    """Roh-HTML eines RIS-Dokuments zu Markdown."""
    soup = BeautifulSoup(raw, "lxml")

    # Style/Script weg
    for t in soup(("script", "style", "noscript", "head", "meta", "link")):
        t.decompose()

    # Vorlesefassung weg
    for cls in _ZU_ENTFERNENDE_KLASSEN:
        for t in soup.select(f".{cls}"):
            t.decompose()

    body = soup.body or soup
    md = markdownify(
        str(body),
        heading_style="ATX",
        strip=("a",),  # Anchor-Tags wegwerfen, nur Text behalten
    )

    # Mehrfache Leerzeilen normalisieren
    md = re.sub(r"\n{3,}", "\n\n", md)
    return md.strip() + "\n"
