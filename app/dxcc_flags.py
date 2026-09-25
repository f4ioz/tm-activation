"""Flag of a station from its callsign (most common DXCC entities).

Used to show, while logging, the countries already worked. The flag is an emoji
(two "regional indicator" letters): nothing to download, so usable without
Internet, and readable on all recent systems.

The table covers the most frequent prefixes in Europe and the main DX areas. An
unknown prefix returns no flag: the QRZ record, when available, gives the exact
entity name anyway.
"""

from __future__ import annotations

import re
import unicodedata

# Callsign prefix → (flag code, entity name). The code is an ISO 3166-1 alpha-2,
# or a subdivision code for the nations of the United Kingdom (GB-SCT…), each
# being a DXCC entity in its own right.
PREFIXES: dict[str, tuple[str, str]] = {
    # ── Europe ──
    "F": ("FR", "France"), "TM": ("FR", "France"), "TK": ("FR-COR", "Corsica"),
    "ON": ("BE", "Belgium"), "OO": ("BE", "Belgium"), "OP": ("BE", "Belgium"),
    "OQ": ("BE", "Belgium"), "OR": ("BE", "Belgium"), "OS": ("BE", "Belgium"),
    "OT": ("BE", "Belgium"),
    "PA": ("NL", "Netherlands"), "PB": ("NL", "Netherlands"), "PC": ("NL", "Netherlands"),
    "PD": ("NL", "Netherlands"), "PE": ("NL", "Netherlands"), "PF": ("NL", "Netherlands"),
    "PG": ("NL", "Netherlands"), "PH": ("NL", "Netherlands"), "PI": ("NL", "Netherlands"),
    "DL": ("DE", "Germany"), "DA": ("DE", "Germany"), "DB": ("DE", "Germany"),
    "DC": ("DE", "Germany"), "DD": ("DE", "Germany"), "DF": ("DE", "Germany"),
    "DG": ("DE", "Germany"), "DH": ("DE", "Germany"), "DJ": ("DE", "Germany"),
    "DK": ("DE", "Germany"), "DM": ("DE", "Germany"), "DO": ("DE", "Germany"),
    "DN": ("DE", "Germany"), "DP": ("DE", "Germany"), "DQ": ("DE", "Germany"),
    "DR": ("DE", "Germany"),
    "G": ("GB-ENG", "England"), "M": ("GB-ENG", "England"), "2E": ("GB-ENG", "England"),
    "GM": ("GB-SCT", "Scotland"), "MM": ("GB-SCT", "Scotland"), "GW": ("GB-WLS", "Wales"),
    "MW": ("GB-WLS", "Wales"), "GI": ("GB-NIR", "Northern Ireland"), "MI": ("GB-NIR", "Northern Ireland"),
    "2M": ("GB-SCT", "Scotland"), "2W": ("GB-WLS", "Wales"), "2I": ("GB-NIR", "Northern Ireland"),
    "GD": ("IM", "Isle of Man"), "2D": ("IM", "Isle of Man"), "GU": ("GG", "Guernsey"),
    "2U": ("GG", "Guernsey"), "GJ": ("JE", "Jersey"), "2J": ("JE", "Jersey"),
    "EI": ("IE", "Ireland"), "EJ": ("IE", "Ireland"),
    "EA": ("ES", "Spain"), "EB": ("ES", "Spain"), "EC": ("ES", "Spain"), "ED": ("ES", "Spain"),
    "EE": ("ES", "Spain"), "EF": ("ES", "Spain"), "EG": ("ES", "Spain"), "EH": ("ES", "Spain"),
    "AM": ("ES", "Spain"),
    "EA6": ("ES-IB", "Balearic Islands"), "EA8": ("ES-CN", "Canary Islands"),
    "EA9": ("ES-CE", "Ceuta & Melilla"),
    "CT": ("PT", "Portugal"), "CR": ("PT", "Portugal"), "CQ": ("PT", "Portugal"),
    "CT3": ("PT-MAD", "Madeira"), "CU": ("PT-AZO", "Azores"),
    "I": ("IT", "Italy"), "IK": ("IT", "Italy"), "IZ": ("IT", "Italy"), "IW": ("IT", "Italy"),
    "IU": ("IT", "Italy"), "II": ("IT", "Italy"), "IS": ("IT-SAR", "Sardinia"),
    "IT9": ("IT-SIC", "Sicily"),
    "HB": ("CH", "Switzerland"), "HB9": ("CH", "Switzerland"), "HB0": ("LI", "Liechtenstein"),
    "OE": ("AT", "Austria"), "LX": ("LU", "Luxembourg"), "LA": ("NO", "Norway"),
    "LB": ("NO", "Norway"), "LC": ("NO", "Norway"), "LD": ("NO", "Norway"),
    "LE": ("NO", "Norway"), "LF": ("NO", "Norway"), "LG": ("NO", "Norway"),
    "LH": ("NO", "Norway"), "LI": ("NO", "Norway"), "LJ": ("NO", "Norway"),
    "LK": ("NO", "Norway"), "LL": ("NO", "Norway"), "LM": ("NO", "Norway"),
    "LN": ("NO", "Norway"), "JW": ("SJ", "Svalbard"), "JX": ("NO-JAN", "Jan Mayen"),
    "SM": ("SE", "Sweden"), "SA": ("SE", "Sweden"), "SB": ("SE", "Sweden"), "SC": ("SE", "Sweden"),
    "SD": ("SE", "Sweden"), "SE": ("SE", "Sweden"), "SF": ("SE", "Sweden"), "SG": ("SE", "Sweden"),
    "SH": ("SE", "Sweden"), "SI": ("SE", "Sweden"), "SJ": ("SE", "Sweden"), "SK": ("SE", "Sweden"),
    "SL": ("SE", "Sweden"), "7S": ("SE", "Sweden"), "8S": ("SE", "Sweden"),
    "OH": ("FI", "Finland"), "OF": ("FI", "Finland"), "OG": ("FI", "Finland"), "OH0": ("AX", "Åland"),
    "OZ": ("DK", "Denmark"), "OU": ("DK", "Denmark"), "OV": ("DK", "Denmark"),
    "OW": ("DK", "Denmark"), "5Q": ("DK", "Denmark"),
    "OX": ("GL", "Greenland"), "OY": ("FO", "Faroe Islands"), "TF": ("IS", "Iceland"),
    "ES": ("EE", "Estonia"), "YL": ("LV", "Latvia"), "LY": ("LT", "Lithuania"),
    "SP": ("PL", "Poland"), "SN": ("PL", "Poland"), "SO": ("PL", "Poland"), "SQ": ("PL", "Poland"),
    "SR": ("PL", "Poland"), "3Z": ("PL", "Poland"), "HF": ("PL", "Poland"),
    "OK": ("CZ", "Czech Republic"), "OL": ("CZ", "Czech Republic"),
    "OM": ("SK", "Slovakia"), "HA": ("HU", "Hungary"), "HG": ("HU", "Hungary"),
    "S5": ("SI", "Slovenia"), "9A": ("HR", "Croatia"), "E7": ("BA", "Bosnia-Herzegovina"),
    "YU": ("RS", "Serbia"), "YT": ("RS", "Serbia"), "4O": ("ME", "Montenegro"),
    "Z3": ("MK", "North Macedonia"), "ZA": ("AL", "Albania"), "SV": ("GR", "Greece"),
    "SW": ("GR", "Greece"), "SX": ("GR", "Greece"), "SY": ("GR", "Greece"), "SZ": ("GR", "Greece"),
    "SV5": ("GR-DOD", "Dodecanese"), "SV9": ("GR-CRE", "Crete"), "5B": ("CY", "Cyprus"), "C4": ("CY", "Cyprus"),
    "YO": ("RO", "Romania"), "YP": ("RO", "Romania"), "YR": ("RO", "Romania"),
    "LZ": ("BG", "Bulgaria"), "UR": ("UA", "Ukraine"), "US": ("UA", "Ukraine"),
    "UT": ("UA", "Ukraine"), "UU": ("UA", "Ukraine"), "UX": ("UA", "Ukraine"), "UY": ("UA", "Ukraine"),
    "EM": ("UA", "Ukraine"), "EO": ("UA", "Ukraine"), "EW": ("BY", "Belarus"), "EU": ("BY", "Belarus"),
    "EV": ("BY", "Belarus"), "ER": ("MD", "Moldova"),
    "R": ("RU", "Russia"), "U": ("RU", "Russia"), "RA": ("RU", "Russia"), "RK": ("RU", "Russia"),
    "RM": ("RU", "Russia"), "RN": ("RU", "Russia"), "RU": ("RU", "Russia"), "RV": ("RU", "Russia"),
    "RW": ("RU", "Russia"), "RX": ("RU", "Russia"), "RZ": ("RU", "Russia"), "UA": ("RU", "Russia"),
    "UB": ("RU", "Russia"), "UC": ("RU", "Russia"), "UD": ("RU", "Russia"), "UE": ("RU", "Russia"),
    "UF": ("RU", "Russia"), "UG": ("RU", "Russia"), "UH": ("RU", "Russia"), "UI": ("RU", "Russia"),
    "RA2": ("RU-KGD", "Kaliningrad"), "UA2": ("RU-KGD", "Kaliningrad"),
    "3A": ("MC", "Monaco"), "C3": ("AD", "Andorra"), "9H": ("MT", "Malta"), "T7": ("SM", "San Marino"),
    "HV": ("VA", "Vatican"), "1A": ("SMOM", "Sov. Military Order of Malta"), "Z6": ("XK", "Kosovo"),
    "TA": ("TR", "Türkiye"), "TB": ("TR", "Türkiye"), "TC": ("TR", "Türkiye"),
    "4X": ("IL", "Israel"), "4Z": ("IL", "Israel"), "ZB": ("GI", "Gibraltar"),
    # ── Americas ──
    "K": ("US", "United States"), "W": ("US", "United States"), "N": ("US", "United States"),
    "AA": ("US", "United States"), "AB": ("US", "United States"), "AC": ("US", "United States"),
    "AD": ("US", "United States"), "AE": ("US", "United States"), "AF": ("US", "United States"),
    "AG": ("US", "United States"), "AI": ("US", "United States"), "AJ": ("US", "United States"),
    "AK": ("US", "United States"), "AL": ("US-AK", "Alaska"), "KL": ("US-AK", "Alaska"),
    "KH6": ("US-HI", "Hawaii"), "KP4": ("PR", "Puerto Rico"), "KP2": ("VI", "US Virgin Islands"),
    "VE": ("CA", "Canada"), "VA": ("CA", "Canada"), "VO": ("CA", "Canada"), "VY": ("CA", "Canada"),
    "CY": ("CA", "Canada"), "XE": ("MX", "Mexico"), "XF": ("MX", "Mexico"), "4A": ("MX", "Mexico"),
    "6D": ("MX", "Mexico"), "PY": ("BR", "Brazil"), "PP": ("BR", "Brazil"), "PR": ("BR", "Brazil"),
    "PT": ("BR", "Brazil"), "PU": ("BR", "Brazil"), "PV": ("BR", "Brazil"), "PW": ("BR", "Brazil"),
    "ZZ": ("BR", "Brazil"), "LU": ("AR", "Argentina"), "AY": ("AR", "Argentina"), "LW": ("AR", "Argentina"),
    "CE": ("CL", "Chile"), "CA": ("CL", "Chile"), "XQ": ("CL", "Chile"), "CX": ("UY", "Uruguay"),
    "CP": ("BO", "Bolivia"), "OA": ("PE", "Peru"), "HC": ("EC", "Ecuador"), "HK": ("CO", "Colombia"),
    "YV": ("VE", "Venezuela"), "ZP": ("PY", "Paraguay"), "8R": ("GY", "Guyana"),
    "PZ": ("SR", "Suriname"), "FY": ("GF", "French Guiana"), "CO": ("CU", "Cuba"), "CM": ("CU", "Cuba"),
    "HI": ("DO", "Dominican Republic"), "HH": ("HT", "Haiti"), "6Y": ("JM", "Jamaica"),
    "9Y": ("TT", "Trinidad & Tobago"), "V3": ("BZ", "Belize"), "TG": ("GT", "Guatemala"),
    "YS": ("SV", "El Salvador"), "HR": ("HN", "Honduras"), "YN": ("NI", "Nicaragua"),
    "TI": ("CR", "Costa Rica"), "HP": ("PA", "Panama"), "FM": ("MQ", "Martinique"), "FJ": ("BL", "Saint-Barthélemy"),
    "FT5": ("TF", "French Southern Territories"), "FT": ("TF", "French Southern Territories"),
    "FG": ("GP", "Guadeloupe"), "FS": ("MF", "Saint Martin"), "FP": ("PM", "St Pierre & Miquelon"),
    "V2": ("AG", "Antigua & Barbuda"), "J3": ("GD", "Grenada"), "J6": ("LC", "Saint Lucia"),
    "J7": ("DM", "Dominica"), "J8": ("VC", "St Vincent"), "VP2": ("VG", "British Virgin Islands"),
    "VP5": ("TC", "Turks & Caicos"), "VP9": ("BM", "Bermuda"), "C6": ("BS", "Bahamas"),
    "ZF": ("KY", "Cayman Islands"), "P4": ("AW", "Aruba"), "PJ2": ("CW", "Curaçao"),
    # ── Africa ──
    "CN": ("MA", "Morocco"), "7X": ("DZ", "Algeria"), "3V": ("TN", "Tunisia"), "5A": ("LY", "Libya"),
    "SU": ("EG", "Egypt"), "ST": ("SD", "Sudan"), "ET": ("ET", "Ethiopia"), "5Z": ("KE", "Kenya"),
    "5H": ("TZ", "Tanzania"), "5X": ("UG", "Uganda"), "9J": ("ZM", "Zambia"), "Z2": ("ZW", "Zimbabwe"),
    "C9": ("MZ", "Mozambique"), "7Q": ("MW", "Malawi"), "A2": ("BW", "Botswana"), "V5": ("NA", "Namibia"),
    "ZS": ("ZA", "South Africa"), "ZR": ("ZA", "South Africa"), "ZT": ("ZA", "South Africa"),
    "3B8": ("MU", "Mauritius"), "3B9": ("MU-ROD", "Rodrigues"), "FR": ("RE", "Réunion"),
    "FH": ("YT", "Mayotte"), "5R": ("MG", "Madagascar"), "D4": ("CV", "Cape Verde"),
    "6W": ("SN", "Senegal"), "TU": ("CI", "Côte d'Ivoire"), "9G": ("GH", "Ghana"),
    "5N": ("NG", "Nigeria"), "TJ": ("CM", "Cameroon"), "TR": ("GA", "Gabon"), "TT": ("TD", "Chad"),
    "TZ": ("ML", "Mali"), "XT": ("BF", "Burkina Faso"), "5U": ("NE", "Niger"), "TY": ("BJ", "Benin"),
    "5V": ("TG", "Togo"), "9L": ("SL", "Sierra Leone"), "EL": ("LR", "Liberia"),
    "S9": ("ST", "São Tomé"), "D2": ("AO", "Angola"), "9X": ("RW", "Rwanda"), "9U": ("BI", "Burundi"),
    "IH9": ("IT-AFR", "African Italy"), "IG9": ("IT-AFR", "African Italy"),
    # ── Asia and Oceania ──
    "JA": ("JP", "Japan"), "JE": ("JP", "Japan"), "JF": ("JP", "Japan"), "JG": ("JP", "Japan"),
    "JH": ("JP", "Japan"), "JI": ("JP", "Japan"), "JJ": ("JP", "Japan"), "JK": ("JP", "Japan"),
    "JL": ("JP", "Japan"), "JM": ("JP", "Japan"), "JN": ("JP", "Japan"), "JO": ("JP", "Japan"),
    "JP": ("JP", "Japan"), "JQ": ("JP", "Japan"), "JR": ("JP", "Japan"), "JS": ("JP", "Japan"),
    "7K": ("JP", "Japan"), "7L": ("JP", "Japan"), "7M": ("JP", "Japan"), "8J": ("JP", "Japan"),
    "HL": ("KR", "South Korea"), "DS": ("KR", "South Korea"), "6K": ("KR", "South Korea"),
    "BY": ("CN", "China"), "BA": ("CN", "China"), "BD": ("CN", "China"), "BG": ("CN", "China"),
    "BH": ("CN", "China"), "BI": ("CN", "China"), "VR2": ("HK", "Hong Kong"), "BV": ("TW", "Taiwan"),
    "VU": ("IN", "India"), "AT": ("IN", "India"), "4S": ("LK", "Sri Lanka"), "S2": ("BD", "Bangladesh"),
    "9N": ("NP", "Nepal"), "AP": ("PK", "Pakistan"), "EP": ("IR", "Iran"), "YI": ("IQ", "Iraq"),
    "9K": ("KW", "Kuwait"), "A4": ("OM", "Oman"), "A6": ("AE", "United Arab Emirates"),
    "A7": ("QA", "Qatar"), "A9": ("BH", "Bahrain"), "HZ": ("SA", "Saudi Arabia"),
    "7Z": ("SA", "Saudi Arabia"), "JY": ("JO", "Jordan"), "OD": ("LB", "Lebanon"), "YK": ("SY", "Syria"),
    "4L": ("GE", "Georgia"), "EK": ("AM", "Armenia"), "4J": ("AZ", "Azerbaijan"), "4K": ("AZ", "Azerbaijan"),
    "UN": ("KZ", "Kazakhstan"), "UP": ("KZ", "Kazakhstan"), "EX": ("KG", "Kyrgyzstan"),
    "EY": ("TJ", "Tajikistan"), "EZ": ("TM", "Turkmenistan"), "UK": ("UZ", "Uzbekistan"),
    "JT": ("MN", "Mongolia"), "XU": ("KH", "Cambodia"), "XW": ("LA", "Laos"), "XV": ("VN", "Vietnam"),
    "3W": ("VN", "Vietnam"), "HS": ("TH", "Thailand"), "E2": ("TH", "Thailand"), "9V": ("SG", "Singapore"),
    "9M": ("MY", "Malaysia"), "YB": ("ID", "Indonesia"), "YC": ("ID", "Indonesia"), "YD": ("ID", "Indonesia"),
    "DU": ("PH", "Philippines"), "DV": ("PH", "Philippines"), "VK": ("AU", "Australia"),
    "AX": ("AU", "Australia"), "VI": ("AU", "Australia"), "ZL": ("NZ", "New Zealand"),
    "ZM": ("NZ", "New Zealand"), "FK": ("NC", "New Caledonia"), "FO": ("PF", "French Polynesia"),
    "FW": ("WF", "Wallis & Futuna"), "3D2": ("FJ", "Fiji"), "KH2": ("GU", "Guam"),
    "P2": ("PG", "Papua New Guinea"), "V7": ("MH", "Marshall Islands"), "T8": ("PW", "Palau"),
}

# DXCC entity → thumbnail name in static/vendor/flags/. Defaults to the lowercase
# code; these entities have no ISO code, they have either their own flag
# (Corsica, Canaries, Sicily…) or that of their parent country.
FLAG_FILES = {
    "FR-COR": "fr-cor", "ES-CN": "es-cn", "ES-IB": "es-ib", "ES-CE": "es-ce",
    "PT-MAD": "pt-mad", "PT-AZO": "pt-azo", "IT-SIC": "it-sic", "IT-SAR": "it-sar",
    "RU-KGD": "ru-kgd", "MU-ROD": "mu-rod", "SMOM": "smom",
    # Crete and the Dodecanese fly the Greek flag: no flag of their own.
    "GR-CRE": "gr", "GR-DOD": "gr",
    "IT-AFR": "it",          # Pantelleria / Lampedusa
    "NO-JAN": "no",          # Jan Mayen
}


# Entity name (as returned by the QRZ callbook) → flag code. Without this
# fallback, a station whose prefix is missing from the table would form a
# separate, flagless entity next to the same entity found by prefix: the country
# was then counted twice ("Netherlands" twice for PH0DV next to the PA…).
NAME_ALIASES = {
    "fed rep of germany": "Germany", "federal republic of germany": "Germany",
    "czechia": "Czech Republic", "european russia": "Russia",
    "asiatic russia": "Russia", "russian federation": "Russia",
    "united states of america": "United States", "usa": "United States",
    "holland": "Netherlands", "the netherlands": "Netherlands",
    "bosnia herzegovina": "Bosnia-Herzegovina", "aland islands": "Åland",
}


def normalize_name(name: str) -> str:
    """Comparison key of an entity name: no accents, no punctuation, no case."""
    plain = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode()
    return " ".join(re.split(r"[^a-z0-9]+", plain.lower()))


_BY_NAME: dict[str, str] = {}
for _code, _name in PREFIXES.values():
    _BY_NAME.setdefault(normalize_name(_name), _code)


def code_for_name(name: str) -> str:
    """Flag code from the entity name ('' if the name is unknown)."""
    key = normalize_name(name)
    if not key:
        return ""
    return _BY_NAME.get(key) or _BY_NAME.get(normalize_name(NAME_ALIASES.get(key, "")), "")


def entity_key(call: str, qrz_name: str = "") -> tuple[str, str, str]:
    """(grouping key, flag code, name from the prefix).

    The key is the entity code — from the callsign prefix, otherwise from the
    name given by the callbook — so that the same entity is never counted twice.
    A country unknown to the prefix table but named by QRZ is grouped by its
    name; with neither a known name nor prefix, the key is empty.
    """
    code, prefix_name = entity_for_call(call)
    if not code:
        code = code_for_name(qrz_name)
    return code or normalize_name(qrz_name), code, prefix_name


def flag_file(code: str) -> str:
    """Thumbnail name of an entity ("FR-COR" → "fr-cor")."""
    key = (code or "").strip().upper()
    return FLAG_FILES.get(key, key.lower())


_MAX_PREFIX = max(len(p) for p in PREFIXES)


def base_call(call: str) -> str:
    """Callsign without suffix or portable prefix: "F4IOZ/P" → "F4IOZ",
    "F/DL1ABC" → "DL1ABC" (the country of operation is what counts)."""
    cs = (call or "").strip().upper()
    if "/" not in cs:
        return cs
    parts = [p for p in cs.split("/") if p]
    if not parts:
        return ""
    # A portable prefix ("F/DL1ABC") is shorter than the callsign; the usual
    # suffixes (P, M, QRP, 9…) do not change the entity.
    if len(parts) >= 2 and len(parts[0]) < len(parts[1]) and len(parts[0]) <= 3:
        return parts[0] + "0AA"          # the prefix alone is enough to find the entity
    return parts[0]


def entity_for_call(call: str) -> tuple[str, str]:
    """(ISO code, entity name) from the prefix; ('', '') if unknown."""
    cs = base_call(call)
    if not cs:
        return "", ""
    for size in range(min(_MAX_PREFIX, len(cs)), 0, -1):
        found = PREFIXES.get(cs[:size])
        if found:
            return found
    return "", ""


def flag(iso: str) -> str:
    """ISO code → emoji flag ("FR" → 🇫🇷). '' if the code is not usable."""
    code = (iso or "").strip().upper()
    if len(code) != 2 or not code.isalpha():
        return ""
    return "".join(chr(0x1F1E6 + ord(letter) - ord("A")) for letter in code)


def flag_for_call(call: str) -> tuple[str, str]:
    """(flag, entity name) from the callsign; ('', '') if unknown."""
    iso, name = entity_for_call(call)
    return flag(iso), name
