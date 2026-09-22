"""Générateur PDF minimal — texte, aplats de couleur, barres.

Écrit à la main (bibliothèque standard seulement) : le package d'activation
tourne sur un Raspberry Pi avec huit dépendances, on n'en ajoute pas une
neuvième pour sortir une page. Un PDF est un fichier texte structuré ; ce
module écrit le strict nécessaire — catalogue, pages, flux de contenu et les
polices de base (Helvetica), présentes dans tous les lecteurs.

Repère de coordonnées : ORIGINE EN HAUT À GAUCHE, en points (1/72 pouce),
comme on raisonne pour une mise en page. La conversion vers le repère PDF
(origine en bas) est faite ici.

    doc = Pdf()
    page = doc.page()
    page.rect(0, 0, page.width, 90, fill=(0.05, 0.35, 0.65))
    page.text(40, 30, "TM25TEST", size=28, bold=True, color=(1, 1, 1))
    data = doc.output()
"""

from __future__ import annotations

import unicodedata
A4 = (595.28, 841.89)          # points
Color = tuple[float, float, float]

# Largeurs Helvetica (millièmes de cadratin) pour les caractères ASCII : elles
# servent à centrer, aligner à droite et couper les textes trop longs. Un
# caractère accenté prend la largeur de sa lettre de base (é → e), ce qui est
# exact pour les polices Helvetica.
_W_REGULAR = (
    "278 278 355 556 556 889 667 191 333 333 389 584 278 333 278 278 "
    "556 556 556 556 556 556 556 556 556 556 278 278 584 584 584 556 "
    "1015 667 667 722 722 667 611 778 722 278 500 667 556 833 722 778 "
    "667 778 722 667 611 722 667 944 667 667 611 278 278 278 469 556 "
    "333 556 556 500 556 556 278 556 556 222 222 500 222 833 556 556 "
    "556 556 333 500 278 556 500 722 500 500 500 334 260 334 584"
)
_W_BOLD = (
    "278 333 474 556 556 889 722 238 333 333 389 584 278 333 278 278 "
    "556 556 556 556 556 556 556 556 556 556 333 333 584 584 584 611 "
    "975 722 722 722 722 667 611 778 722 278 556 722 611 833 722 778 "
    "667 778 722 667 611 722 667 944 667 667 611 333 278 333 584 556 "
    "333 556 611 556 611 556 333 611 611 278 278 556 278 889 611 611 "
    "611 611 389 556 333 611 556 778 556 556 500 389 280 389 584"
)


def _widths(spec: str) -> list[int]:
    return [int(w) for w in spec.split()]


_WIDTHS = {False: _widths(_W_REGULAR), True: _widths(_W_BOLD)}


def _base_char(ch: str) -> str:
    """« é » → « e » : la largeur d'un accentué est celle de sa lettre de base."""
    plain = unicodedata.normalize("NFD", ch)
    return next((c for c in plain if not unicodedata.combining(c)), ch)


def text_width(s: str, size: float, bold: bool = False) -> float:
    """Largeur d'un texte, en points."""
    table = _WIDTHS[bool(bold)]
    total = 0
    for ch in s or "":
        base = _base_char(ch)
        index = ord(base) - 32
        total += table[index] if 0 <= index < len(table) else 556
    return total * size / 1000.0


def fit(s: str, size: float, max_width: float, bold: bool = False) -> str:
    """Texte raccourci avec une ellipse pour tenir dans ``max_width``."""
    s = s or ""
    if text_width(s, size, bold) <= max_width:
        return s
    ell = "…"
    while s and text_width(s + ell, size, bold) > max_width:
        s = s[:-1]
    return s + ell if s else ""


def _escape(s: str) -> bytes:
    """Chaîne PDF : encodage WinAnsi (accents compris) et parenthèses échappées."""
    raw = (s or "").encode("cp1252", "replace")
    return raw.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")


class Page:
    """Une page : on y empile des ordres de dessin, en coordonnées « haut-gauche »."""

    def __init__(self, width: float, height: float) -> None:
        self.width, self.height = width, height
        self._ops: list[bytes] = []

    # ── primitives ────────────────────────────────────────────────────────
    def _y(self, y: float) -> float:
        return self.height - y

    def rect(self, x: float, y: float, w: float, h: float, fill: Color | None = None,
             stroke: Color | None = None, line_width: float = 1.0, radius: float = 0.0) -> None:
        """Rectangle (coin supérieur gauche en x, y). ``radius`` : coins arrondis."""
        parts = []
        if fill:
            parts.append(b"%.3f %.3f %.3f rg" % fill)
        if stroke:
            parts.append(b"%.3f %.3f %.3f RG" % stroke)
            parts.append(b"%.2f w" % line_width)
        top, bottom = self._y(y), self._y(y + h)
        if radius > 0:
            r = min(radius, w / 2, h / 2)
            k = r * 0.5523
            parts.append(
                b"%.2f %.2f m %.2f %.2f l %.2f %.2f %.2f %.2f %.2f %.2f c "
                b"%.2f %.2f l %.2f %.2f %.2f %.2f %.2f %.2f c "
                b"%.2f %.2f l %.2f %.2f %.2f %.2f %.2f %.2f c "
                b"%.2f %.2f l %.2f %.2f %.2f %.2f %.2f %.2f c h" % (
                    x + r, top,
                    x + w - r, top, x + w - r + k, top, x + w, top - r + k, x + w, top - r,
                    x + w, bottom + r, x + w, bottom + r - k, x + w - r + k, bottom, x + w - r, bottom,
                    x + r, bottom, x + r - k, bottom, x, bottom + r - k, x, bottom + r,
                    x, top - r, x, top - r + k, x + r - k, top, x + r, top,
                )
            )
        else:
            parts.append(b"%.2f %.2f %.2f %.2f re" % (x, bottom, w, h))
        if fill and stroke:
            parts.append(b"B")
        elif fill:
            parts.append(b"f")
        else:
            parts.append(b"S")
        self._ops.append(b" ".join(parts))

    def line(self, x1: float, y1: float, x2: float, y2: float,
             color: Color = (0, 0, 0), width: float = 1.0) -> None:
        self._ops.append(b"%.3f %.3f %.3f RG %.2f w %.2f %.2f m %.2f %.2f l S" % (
            *color, width, x1, self._y(y1), x2, self._y(y2)))

    def text(self, x: float, y: float, s: str, size: float = 10, bold: bool = False,
             color: Color = (0, 0, 0), align: str = "left", width: float = 0) -> None:
        """Texte dont ``y`` est la ligne de base du haut (le texte descend).

        ``align`` : "left", "center" ou "right" dans la boîte [x, x + width]."""
        drawn = s or ""
        if width:
            drawn = fit(drawn, size, width, bold)
        at = x
        if align == "center":
            at = x + (width - text_width(drawn, size, bold)) / 2
        elif align == "right":
            at = x + width - text_width(drawn, size, bold)
        font = b"/F2" if bold else b"/F1"
        self._ops.append(b"BT %.3f %.3f %.3f rg %s %.2f Tf %.2f %.2f Td (%s) Tj ET" % (
            *color, font, size, at, self._y(y + size), _escape(drawn)))

    def stream(self) -> bytes:
        return b"\n".join(self._ops)


class Pdf:
    """Document : une suite de pages, puis ``output()``."""

    def __init__(self, size: tuple[float, float] = A4) -> None:
        self.size = size
        self.pages: list[Page] = []
        self.title = ""
        self.author = ""

    def page(self) -> Page:
        page = Page(*self.size)
        self.pages.append(page)
        return page

    def output(self) -> bytes:
        objects: list[bytes] = []

        def add(body: bytes) -> int:
            objects.append(body)
            return len(objects)            # numéro d'objet (1-based)

        font_regular = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica "
                           b"/Encoding /WinAnsiEncoding >>")
        font_bold = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold "
                        b"/Encoding /WinAnsiEncoding >>")
        resources = (b"<< /Font << /F1 %d 0 R /F2 %d 0 R >> >>" % (font_regular, font_bold))
        pages_id = add(b"")          # objet « Pages » réservé : les pages le citent
        page_ids: list[int] = []
        for page in self.pages:
            data = page.stream()
            content = add(b"<< /Length %d >>\nstream\n%s\nendstream" % (len(data), data))
            page_ids.append(add(
                b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 %.2f %.2f] /Resources %s "
                b"/Contents %d 0 R >>" % (pages_id, self.size[0], self.size[1], resources, content)
            ))
        kids = b" ".join(b"%d 0 R" % pid for pid in page_ids)
        objects[pages_id - 1] = b"<< /Type /Pages /Count %d /Kids [%s] >>" % (len(page_ids), kids)
        info = add(b"<< /Title (%s) /Author (%s) /Producer (TM Activation) >>" % (
            _escape(self.title), _escape(self.author)))
        catalog = add(b"<< /Type /Catalog /Pages %d 0 R >>" % pages_id)

        out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        offsets = [0]
        for number, body in enumerate(objects, start=1):
            offsets.append(len(out))
            out += b"%d 0 obj\n%s\nendobj\n" % (number, body)
        xref_at = len(out)
        out += b"xref\n0 %d\n" % (len(objects) + 1)
        out += b"0000000000 65535 f \n"
        for offset in offsets[1:]:
            out += b"%010d 00000 n \n" % offset
        out += b"trailer\n<< /Size %d /Root %d 0 R /Info %d 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
            len(objects) + 1, catalog, info, xref_at)
        return bytes(out)


def hex_color(value: str, default: Color = (0.2, 0.2, 0.2)) -> Color:
    """« #e8543f » → (0.91, 0.33, 0.25), pour réutiliser les couleurs du site."""
    s = (value or "").strip().lstrip("#")
    if len(s) != 6:
        return default
    try:
        return tuple(int(s[i:i + 2], 16) / 255.0 for i in (0, 2, 4))  # type: ignore[return-value]
    except ValueError:
        return default


def mix(color: Color, other: Color, ratio: float) -> Color:
    """Mélange deux couleurs (``ratio`` = part de ``other``)."""
    return tuple(a + (b - a) * ratio for a, b in zip(color, other))  # type: ignore[return-value]


__all__: list[str] = ["A4", "Page", "Pdf", "Color", "hex_color", "mix", "text_width", "fit"]
