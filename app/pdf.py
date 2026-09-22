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

import hashlib
import unicodedata
import zlib
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


def read_png(data: bytes) -> tuple[int, int, bytes]:
    """PNG → (largeur, hauteur, pixels RGB bruts, 8 bits par composante).

    Gère les images non entrelacées en niveaux de gris, RGB, palette (1, 2, 4
    ou 8 bits) et leurs variantes avec transparence — l'alpha est aplati sur
    du blanc, la page du rapport étant blanche. De quoi afficher les drapeaux
    DXCC et le logo du club sans Pillow.
    """
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("ce n'est pas une image PNG")
    width = height = depth = color = 0
    palette = b""
    trans = b""
    idat = bytearray()
    pos = 8
    while pos + 8 <= len(data):
        length = int.from_bytes(data[pos:pos + 4], "big")
        name = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + length]
        if name == b"IHDR":
            width = int.from_bytes(body[0:4], "big")
            height = int.from_bytes(body[4:8], "big")
            depth, color = body[8], body[9]
            if body[12]:
                raise ValueError("PNG entrelacé non géré")
        elif name == b"PLTE":
            palette = body
        elif name == b"tRNS":
            trans = body
        elif name == b"IDAT":
            idat += body
        elif name == b"IEND":
            break
        pos += 12 + length
    if not width or not height:
        raise ValueError("PNG illisible")
    if depth == 16:
        raise ValueError("PNG 16 bits non géré")

    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[color]
    bits = channels * depth
    stride = (width * bits + 7) // 8
    raw = zlib.decompress(bytes(idat))
    bpp = max(1, bits // 8)
    lines: list[bytearray] = []
    previous = bytearray(stride)
    at = 0
    for _ in range(height):
        filter_type = raw[at]
        line = bytearray(raw[at + 1:at + 1 + stride])
        at += 1 + stride
        for i in range(stride):
            a = line[i - bpp] if i >= bpp else 0
            b = previous[i]
            c = previous[i - bpp] if i >= bpp else 0
            if filter_type == 1:
                line[i] = (line[i] + a) & 0xFF
            elif filter_type == 2:
                line[i] = (line[i] + b) & 0xFF
            elif filter_type == 3:
                line[i] = (line[i] + (a + b) // 2) & 0xFF
            elif filter_type == 4:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                line[i] = (line[i] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 0xFF
        lines.append(line)
        previous = line

    out = bytearray()
    for line in lines:
        values = _unpack(line, depth, width * channels)
        for x in range(width):
            alpha = 255
            if color == 3:
                index = values[x]
                base = 3 * index
                rgb = (palette[base], palette[base + 1], palette[base + 2])
                if index < len(trans):
                    alpha = trans[index]
            elif color in (0, 4):
                grey = values[x * channels]
                rgb = (grey, grey, grey)
                if color == 4:
                    alpha = values[x * channels + 1]
            else:
                rgb = (values[x * channels], values[x * channels + 1], values[x * channels + 2])
                if color == 6:
                    alpha = values[x * channels + 3]
            if alpha != 255:       # aplati sur blanc (entiers : c'est un point chaud)
                rest = 255 * (255 - alpha)
                rgb = ((rgb[0] * alpha + rest) // 255,
                       (rgb[1] * alpha + rest) // 255,
                       (rgb[2] * alpha + rest) // 255)
            out += bytes(rgb)
    return width, height, bytes(out)


def read_jpeg(data: bytes) -> tuple[int, int, int]:
    """(largeur, hauteur, composantes) d'un JPEG, lues dans son marqueur SOF.

    Un JPEG s'embarque tel quel dans un PDF (filtre DCTDecode) : inutile de le
    décoder, il suffit de connaître ses dimensions."""
    if data[:3] != b"\xff\xd8\xff":
        raise ValueError("ce n'est pas une image JPEG")
    pos = 2
    while pos + 9 < len(data):
        if data[pos] != 0xFF:
            pos += 1
            continue
        marker = data[pos + 1]
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
            pos += 2
            continue
        length = int.from_bytes(data[pos + 2:pos + 4], "big")
        # SOF0..SOF15 sauf les marqueurs qui ne décrivent pas l'image.
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            height = int.from_bytes(data[pos + 5:pos + 7], "big")
            width = int.from_bytes(data[pos + 7:pos + 9], "big")
            return width, height, data[pos + 9]
        pos += 2 + length
    raise ValueError("JPEG illisible")


def downsample(width: int, height: int, rgb: bytes, max_side: int) -> tuple[int, int, bytes]:
    """Réduit une image RGB d'un facteur entier (moyenne des blocs).

    Suffisant pour un logo : pas de rééchantillonnage savant, mais pas d'effet
    d'escalier non plus, et aucune dépendance."""
    longest = max(width, height)
    if longest <= max_side:
        return width, height, rgb
    factor = -(-longest // max_side)            # arrondi au supérieur
    new_w, new_h = max(width // factor, 1), max(height // factor, 1)
    out = bytearray(new_w * new_h * 3)
    pixels = factor * factor
    for y in range(new_h):
        for x in range(new_w):
            r = g = b = 0
            for dy in range(factor):
                row = ((y * factor + dy) * width + x * factor) * 3
                for dx in range(factor):
                    at = row + dx * 3
                    r += rgb[at]
                    g += rgb[at + 1]
                    b += rgb[at + 2]
            at = (y * new_w + x) * 3
            out[at] = r // pixels
            out[at + 1] = g // pixels
            out[at + 2] = b // pixels
    return new_w, new_h, bytes(out)


def image_size(data: bytes) -> tuple[int, int]:
    """Dimensions d'une image PNG ou JPEG."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        width = int.from_bytes(data[16:20], "big")
        height = int.from_bytes(data[20:24], "big")
        return width, height
    width, height, _ = read_jpeg(data)
    return width, height


def _unpack(line: bytearray, depth: int, count: int) -> list[int]:
    """Échantillons d'une ligne, quelle que soit la profondeur (1, 2, 4, 8 bits).

    En dessous de 8 bits, la valeur reste l'INDICE de palette : pas de mise à
    l'échelle, sinon les couleurs des drapeaux seraient fausses."""
    if depth == 8:
        return list(line[:count])
    per_byte = 8 // depth
    mask = (1 << depth) - 1
    values: list[int] = []
    for byte in line:
        for k in range(per_byte):
            values.append((byte >> (8 - depth * (k + 1))) & mask)
            if len(values) >= count:
                return values
    return values


# Images déjà décodées (et réduites) : le rapport peut être recomposé plusieurs
# fois pour tenir en une page, inutile de refaire le travail à chaque essai.
_PREPARED: dict[tuple[str, int], tuple[int, int, bytes, bytes, bytes]] = {}
_PREPARED_MAX = 64


def _escape(s: str) -> bytes:
    """Chaîne PDF : encodage WinAnsi (accents compris) et parenthèses échappées."""
    raw = (s or "").encode("cp1252", "replace")
    return raw.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)")


class Page:
    """Une page : on y empile des ordres de dessin, en coordonnées « haut-gauche »."""

    def __init__(self, width: float, height: float, doc: "Pdf | None" = None) -> None:
        self.width, self.height = width, height
        self.doc = doc
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

    def image(self, x: float, y: float, w: float, h: float, png: bytes,
              max_side: int = 0) -> None:
        """Image PNG ou JPEG dessinée dans le rectangle (x, y, w, h).

        ``max_side`` : réduit un PNG trop grand avant de l'embarquer — un logo
        de 700 pixels imprimé sur 2 cm n'a pas besoin de peser 800 ko."""
        if self.doc is None:
            raise RuntimeError("page détachée du document")
        name = self.doc.add_image(png, max_side=max_side)
        self._ops.append(b"q %.2f 0 0 %.2f %.2f %.2f cm %s Do Q" % (
            w, h, x, self._y(y + h), name))

    def stream(self) -> bytes:
        return b"\n".join(self._ops)


class Pdf:
    """Document : une suite de pages, puis ``output()``."""

    def __init__(self, size: tuple[float, float] = A4) -> None:
        self.size = size
        self.pages: list[Page] = []
        self.title = ""
        self.author = ""
        # Images embarquées, dédoublonnées par empreinte : un drapeau répété
        # n'alourdit le fichier qu'une fois.
        self._images: dict[str, tuple[bytes, int, int, bytes, bytes, bytes]] = {}

    def page(self) -> Page:
        page = Page(*self.size, doc=self)
        self.pages.append(page)
        return page

    def add_image(self, data: bytes, max_side: int = 0) -> bytes:
        """Enregistre une image PNG ou JPEG, renvoie son nom de ressource (/Im3).

        Le PNG est décodé puis recompressé ; le JPEG part tel quel (DCTDecode),
        donc sans perte de qualité ni recompression."""
        key = hashlib.sha1(data).hexdigest()
        known = self._images.get(key)
        if known is not None:
            return known[0]
        name = b"/Im%d" % (len(self._images) + 1)
        prepared = _PREPARED.get((key, max_side))
        if prepared is None:
            if data[:8] == b"\x89PNG\r\n\x1a\n":
                width, height, rgb = read_png(data)
                if max_side:
                    width, height, rgb = downsample(width, height, rgb, max_side)
                prepared = (width, height, zlib.compress(rgb, 6), b"FlateDecode", b"DeviceRGB")
            else:
                width, height, components = read_jpeg(data)
                if components not in (1, 3):
                    raise ValueError("JPEG en CMJN non géré")
                space = b"DeviceGray" if components == 1 else b"DeviceRGB"
                prepared = (width, height, data, b"DCTDecode", space)
            if len(_PREPARED) >= _PREPARED_MAX:
                _PREPARED.clear()
            _PREPARED[(key, max_side)] = prepared
        self._images[key] = (name, *prepared)
        return name

    def output(self) -> bytes:
        objects: list[bytes] = []

        def add(body: bytes) -> int:
            objects.append(body)
            return len(objects)            # numéro d'objet (1-based)

        font_regular = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica "
                           b"/Encoding /WinAnsiEncoding >>")
        font_bold = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold "
                        b"/Encoding /WinAnsiEncoding >>")
        image_refs = []
        for name, width, height, blob, filt, space in self._images.values():
            obj = add(b"<< /Type /XObject /Subtype /Image /Width %d /Height %d "
                      b"/ColorSpace /%s /BitsPerComponent 8 /Filter /%s "
                      b"/Length %d >>\nstream\n" % (width, height, space, filt, len(blob))
                      + blob + b"\nendstream")
            image_refs.append(b"%s %d 0 R" % (name, obj))
        xobjects = (b" /XObject << %s >>" % b" ".join(image_refs)) if image_refs else b""
        resources = (b"<< /Font << /F1 %d 0 R /F2 %d 0 R >>%s >>" % (
            font_regular, font_bold, xobjects))
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


__all__: list[str] = ["A4", "Page", "Pdf", "Color", "hex_color", "mix", "text_width",
                      "fit", "read_png", "read_jpeg", "image_size"]
