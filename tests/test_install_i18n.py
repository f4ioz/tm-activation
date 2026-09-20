"""Traduction de l'installeur (install.sh) et du script Proxmox.

Les textes sont écrits en français dans les scripts ; deploy/lang/en.sh donne
leur traduction. On vérifie que chaque texte est traduit, que les champs {1},
{2}… correspondent, et que l'affichage en anglais ne laisse pas de français.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
INSTALL = ROOT / "install.sh"
LXC = ROOT / "proxmox" / "tm-activation-lxc.sh"
EN = ROOT / "deploy" / "lang" / "en.sh"

# Noms propres, commandes et intitulés d'interfaces : pas de traduction.
SKIP = {"texte français", "1) Français", "2) English", "systemctl status cloudflared",
        "Freebox", "Livebox", "SFR Box", "Bbox", "nginx", "UTC", "OK", "TM Activation",
        "Cloudflare Tunnel", "config.yml", "cloudflared", "Question", "choix 1", "choix 2"}
_STR = r'"((?:[^"\\$`]|\\.)*)"'
_PATS = [rf'\b(?:say|info|warn|die|title|t)\s+{_STR}', rf'\bline\s+{_STR}',
         rf'\b(?:ask|ask_secret|ask_password)\s+\w+\s+{_STR}', rf'\bconfirm\s+{_STR}',
         rf'\bpause\s+{_STR}', rf'\bguide_step\s+\d+\s+{_STR}', rf'\bmenu\s+\w+\s+{_STR}',
         rf'\bmsg_(?:info|ok|warn|err)\s+{_STR}']


def message_ids(path: Path) -> list[str]:
    """Textes passés aux fonctions d'affichage et de saisie d'un script."""
    src = re.sub(r'(?m)^\s*#.*$', '', path.read_text(encoding="utf-8"))
    found: list[str] = []
    for pat in _PATS:
        found += [m.group(1) for m in re.finditer(pat, src)]
    lines = src.splitlines()
    for i, line in enumerate(lines):          # options d'un menu (suite de la ligne)
        if re.search(r'\bmenu\s+\w+\s', line):
            j = i
            found += [x for x in re.findall(r'"([^"]*)"', line) if "$" not in x and "`" not in x]
            while j < len(lines) and lines[j].rstrip().endswith("\\"):
                j += 1
                found += [x for x in re.findall(r'"([^"]*)"', lines[j]) if "$" not in x and "`" not in x]
    out: list[str] = []
    for text in found:
        text = text.strip()
        if (text and text not in out and text not in SKIP and re.search(r'[A-Za-zÀ-ÿ]{2}', text)
                and not re.fullmatch(r'[a-z0-9_./:-]+', text)):
            out.append(text)
    return out


def catalog(source: str = f'source "{EN}"') -> dict[str, str]:
    """Traductions d'un catalogue bash : deploy/lang/en.sh, ou load_en (script Proxmox)."""
    out = subprocess.run(
        ["bash", "-c", f'declare -A MSG; {source}; for k in "${{!MSG[@]}}"; do '
                       'printf "%s\\t%s\\0" "$k" "${MSG[$k]}"; done'],
        capture_output=True, text=True, check=True).stdout
    return dict(entry.split("\t", 1) for entry in out.split("\0") if entry)


def lxc_catalog() -> dict[str, str]:
    # Le script Proxmox porte ses traductions (il est lancé seul, via curl).
    return catalog(f'source "{LXC}" >/dev/null 2>&1 || true; load_en')


def test_every_message_is_translated() -> None:
    missing = [f"install.sh: {m}" for m in message_ids(INSTALL) if m not in catalog()]
    missing += [f"proxmox: {m}" for m in message_ids(LXC) if m not in lxc_catalog()]
    assert not missing, f"{len(missing)} texte(s) sans traduction anglaise :\n" + "\n".join(missing)


def test_placeholders_match() -> None:
    fields = re.compile(r"\{\d+\}")
    for fr, en in {**catalog(), **lxc_catalog()}.items():
        assert set(fields.findall(fr)) == set(fields.findall(en)), f"{fr!r} → {en!r}"


def run_sh(script: Path, snippet: str, lang: str = "en", **env: str) -> str:
    """Exécute un bout de code après avoir sourcé le script dans la langue voulue."""
    code = f'UI_LANG={lang}\nsource "{script}"\nUI_LANG={lang}\nload_lang\n{snippet}'
    r = subprocess.run(["bash", "-c", code], capture_output=True, text=True, timeout=30,
                       cwd=ROOT, env={**os.environ, **env})
    return r.stdout + r.stderr


# Mots français : leur présence dans une sortie anglaise trahit un texte oublié.
FRENCH = re.compile(r"(?i)(?<![\w-])(le|la|les|des|du|une|avec|pour|dans|sur|aucun|aucune|"
                    r"réglages|opérateurs?|réseau|adresse|indicatif|mot de passe|à|être|où)(?![\w-])")


@pytest.mark.parametrize("snippet", [
    "usage",
    "welcome",
    'MODE=lan PORT=80 HOST=0.0.0.0 DIR=/opt/tm-activation SERVICE=tm-activation '
    'MDNS_NAME=tm50abc VERSION=9.9.9 GENERATED_ADMIN_PW=secret summary',
    'BOX=freebox NC_MAC=aa:bb NC_LOCAL_IP=192.168.1.42 help_dhcp; help_ipv4; help_ports',
    'MODE=internet DOMAIN=tm.example.org BOX=freebox EMAIL=a@b.c FIRST_CONFIG=0 DIR=/opt/x '
    'SERVICE=tm-activation PORT=80 HOST=0.0.0.0 MDNS_NAME=tm50abc recap <<< "n"',
])
def test_installer_speaks_english(snippet: str) -> None:
    out = run_sh(INSTALL, snippet)
    assert out.strip(), snippet
    # Les intitulés des interfaces de box françaises restent en français
    # (« Baux statiques », « Réseau v4 »…) : ces lignes sont hors contrôle.
    checked = "\n".join(l for l in out.splitlines()
                        if not re.search(r"«|Freebox|Paramètres|Réseau v4|rubrique DHCP", l))
    words = sorted({m.group(0) for m in FRENCH.finditer(checked)})
    assert not words, f"mots français dans « {snippet[:40]} » : {words}\n{out}"


def test_installer_still_speaks_french() -> None:
    out = run_sh(INSTALL, "usage", lang="fr")
    assert "installation guidée" in out and "guided install" not in out


def test_language_is_remembered(tmp_path) -> None:
    """--lang en est mémorisé dans install.env et repris à la mise à jour."""
    (tmp_path / "install.env").write_text("MODE=lan\nUI_LANG=en\n", encoding="utf-8")
    (tmp_path / ".tm-activation").write_text("9.9.9\n", encoding="utf-8")
    out = run_sh(INSTALL, f'CLI_DIR={tmp_path} resolve_context; echo "LANG=$UI_LANG"', lang="")
    assert "LANG=en" in out


def test_unknown_language_refused(tmp_path) -> None:
    out = run_sh(INSTALL, 'CLI_LANG=de resolve_context', lang="")
    assert "unknown language: de" in out or "langue inconnue : de" in out
