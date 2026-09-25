# Layout specification — TM Activation certificate

A4 landscape page, 841.89 × 595.28 pt, origin at the bottom left (reportlab).

```python
from app.tmcert import render
pdf_bytes = render(data)          # data: see "Data format" below
```

Every printed text comes from `labels` (French by default, see
`DEFAULT_LABELS` in `render.py`): pass translated texts to get another language.

## Layers
1. **Static** (`decor.py`): white background, navy arc + double gold arc (left), navy shape
   (top right), gold gradient band (bottom right), microphone, Morse key, Yagi, **radio set**
   (right, below the medal), stars, logo.
2. **Medal**: fixed position (726, 485), red ribbons; content = rank + "er/e" + "SUR n".
   Without a ranking → the medal shows "QSO".
3. **Dynamic** (`render.py`): everything else.

## Data format

| Key | Required | Content |
|---|---|---|
| `activation` | yes | `callsign`, `event`; optional `period` (free text, e.g. "du 6 au 20 septembre 2026"), `title` (default "CERTIFICAT"), `subtitle` (an empty string removes it; missing → "ACTIVATION SPÉCIALE · EVENT"), `morse` (text of the Morse underline; default: the title) |
| `recipient` | yes | `callsign`; optional `name`, `locator` |
| `qso` | yes | list of `{date: "YYYY-MM-DD", time_utc: "HH:MM", mode, freq_mhz?, band?, rst_sent?, rst_rcvd?, points?}` |
| `certificate` | yes | optional `number`, `issue_date` (ISO, default today), `manager` (default: the activation callsign) |
| `ranking` | no | `{position, total, suffix?}` — shown on the medal; `suffix` = ordinal suffix (default French "er"/"e", e.g. "st" in English) |
| `scoring` | no | points per mode, e.g. `{"CW": 3, "*": 1}` (`*` = default) |
| `options` | no | see "Caller options" |
| `award_text` | no | replaces the "pour avoir contacté…" sentence |
| `footnote` | no | small line at the bottom of the page |
| `logo_path` | no | club logo (PNG with transparency, or JPEG); default: the neutral emblem in `assets/` |
| `flag` | no | path to a flag PNG, drawn right of the title |
| `border` | no | three `#rrggbb` colours for the thin page border |
| `emblem` | no | `{text}` — see "Emblem and symbol" |
| `ham_symbol` | no | `true` — see "Emblem and symbol" |
| `qr_url` | no | address encoded in a QR code, left column below the logo, printed underneath without `https://` |
| `labels` | no | printed texts and formats, see "Labels" |

`render()` also adds `stats` (`count`, `bands`, `modes`, `points`) while normalizing.

## Palette
| Role | Hex |
|---|---|
| Navy / dark navy / grey-blue | `#2B3547` / `#1D2533` / `#5A6478` |
| Gold / light gold / pale gold | `#C9982E` / `#E9C46A` / `#F6E3A8` |
| Red / dark red | `#B0202B` / `#7E131B` |
| Body text | `#4A4F5A` |
| Mode badges | SSB `#C9982E`, CW `#B0202B`, FT8/FT4/DIGI `#2C6E9C`, FM `#3B7D4F`, RTTY `#6A4C93`, other `#5A6478` |

## Fonts
Poppins (Regular, Medium, SemiBold, Bold, ExtraBold); Great Vibes is registered as `Script`.

## Dynamic areas (content column x = 228, width 420)
| Element | y (pt) | Font | Rule |
|---|---|---|---|
| Title | 445 | Poppins ExtraBold 60 | default "CERTIFICAT"; the application puts the special callsign there |
| Morse underline | 435 | gold blocks, computed unit | the title in Morse (or `activation.morse`), fitted to its width |
| Subtitle | 415 | Poppins SemiBold 20 | shrunk down to 12 pt; the application puts the activation label there, an empty string removes it |
| Band | 373–397 | SemiBold 10 white on navy | fixed text |
| Name | 321 | Poppins SemiBold 30 | shrunk down to 17 pt; missing → callsign in ExtraBold 44 |
| Rule | 305 | — | |
| Callsign + locator | 277 | ExtraBold 30 red (shrunk down to 18) + SemiBold 0.42× | centred together |
| Award sentence | 259 | Regular 9.5 | 2 lines max, centred |
| QSO table | below the sentence | header 16 pt, rows 13.5 pt | columns: date, UTC, band, frequency, mode (badge), RST sent, RST received |
| Summary | 18 pt below the table | Medium 8 + ExtraBold 11 | QSOs, bands, modes, points (red) |
| Footer | 56 / 50 / 37 | SemiBold 10.5 / rule / Medium 8.5 | manager (x 268), number (x 585), date (x 735) — **no signature** |
| Footnote | 14 | Regular 6 | optional |

## Labels

Any key missing from `data["labels"]` takes its French default. `{…}` fields are filled
in by the engine.

| Key | French default | Where |
|---|---|---|
| `title` | CERTIFICAT | title when `activation.title` is missing |
| `subtitle` | ACTIVATION SPÉCIALE · {event} | subtitle when `activation.subtitle` is missing |
| `awarded_to` | CE CERTIFICAT EST DÉCERNÉ À | navy band |
| `award_text` | pour avoir contacté la station spéciale {callsign}, activée lors du {event} {period}. | sentence (unless `award_text` is given at the top level) |
| `columns` | DATE, UTC, BANDE, FRÉQ. MHz, MODE, RST ENV., RST REÇU | table header (7 items) |
| `more_qso` / `see_appendix` | … et {n} autres QSO / " — journal complet en annexe" | overflow line |
| `summary` | "QSO ", "   BANDES ", "   MODES ", "   POINTS " | summary line (4 items, spacing included) |
| `manager` / `number` / `date` | GESTIONNAIRE / N° DU CERTIFICAT / DATE | footer captions |
| `rank_of` | SUR {total} | medal |
| `appendix_title` / `appendix_header` / `appendix_footer` | JOURNAL DES CONTACTS · {callsign} / {callsign} · {event} · annexe {n}/{total} / {count} QSO · {bands} bandes · {modes} · {points} points · certificat n° {number} | appendix pages |
| `pdf_title` | Certificat {callsign} — {recipient} | PDF metadata |
| `date_format` / `issue_date_format` | {d:02d}/{m:02d}/{y} | tables / footer; fields {d} {m} {y} {mon} {month} |
| `months` / `months_long` | janv.… / janvier… | {mon} / {month} |
| `decimal` | `,` | frequency decimal separator |

The web application passes `labels` in the language of the page (`app/certificate.py`).

## Caller options (`options`)
| Key | Default | Effect |
|---|---|---|
| `max_qso` | 10 | contacts listed on the main page (1 to 14, never more than fit above the footer) |
| `appendix` | true | add the full log as an appendix when it overflows |

## Emblem and symbol (below the radio set, optional)
| Data key | Effect |
|---|---|
| `emblem: {text}` | mast with lightning bolts inside a gold arc, winged navy banner carrying `text` in white Poppins Bold along the curve (9 pt, shrunk down to 4.5 pt); default "HAM RADIO". Banner middle at (745, 176), (733, 176) with the symbol |
| `ham_symbol: true` | international amateur radio symbol (navy/gold diamond, antenna-coil-ground): 46 pt high at (808, 205) next to the emblem, 70 pt at (745, 205) alone |

Drawn as vectors (`decor.emblem`, `decor.ham_symbol`), no external image.

## Data rules
- QSOs sorted by UTC date/time; band derived from the frequency when missing.
- Frequency shown in MHz, 3 decimals, `labels.decimal` separator (default comma).
- Dates shown with `labels.date_format` (default DD/MM/YYYY; ISO YYYY-MM-DD input).
- Points = the ones given by the caller (`qso[].points`) when present, otherwise the
  per-mode `scoring` (`*` = default, 1 if missing). Total = sum of the QSO points.
- Rank suffix: `ranking.suffix`, otherwise 1 → "er", else "e" (French ordinals).

## Overflow
- Page 1: at most `options.max_qso` QSOs (default 10, capped at 14), and never more rows than
  fit above the footer (table bottom at y ≥ 90: 9 or 10 rows depending on the sentence). Beyond that:
  `max_qso - 1` QSOs + a "… et N autres QSO" line, followed by "— journal complet en annexe"
  when the appendix is requested.
- Appendix page(s): produced if `options.appendix` (default true) — 28 QSOs per page, navy band
  "JOURNAL DES CONTACTS · CALLSIGN", numbered "annexe n/N".

## Logo
Square transparent PNG source, ~600 px. A PNG is drawn as is, with its transparency, on the
gold disc (diameter 172 pt, centred at (100, 318)). A JPEG is clipped to the disc: flattening
the PNG on `#E9C46A` gives a much lighter PDF (~190 KB instead of ~900 KB):
```python
from PIL import Image
im = Image.open("logo.png").convert("RGBA")
bg = Image.new("RGBA", im.size, (0xE9, 0xC4, 0x6A, 255)); bg.alpha_composite(im)
bg.convert("RGB").save("logo.jpg", quality=88)
```

## Command line
```bash
python -m app.tmcert data.json -o certificate.pdf
python -m app.tmcert --adif log.adi --config activation.json -o folder/
```
With `--adif`, `activation.json` holds `activation`, `scoring`,
`certificate: {number_prefix, manager}`, and optionally `footnote` and
`names: {CALL: name}`. One certificate per contacted callsign, ranked by points,
then QSO count, then earliest first QSO.
