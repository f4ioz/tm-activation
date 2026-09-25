"""Proxmox installation (proxmox/tm-activation-lxc.sh) and upgrade from
GitHub (deploy/update-from-github.sh).

No Proxmox here: pct, pveam, pvesm and lxc-attach are simulated and log
their calls; GitHub is replaced by a local HTTP server publishing a
fake release whose install.sh records its arguments.
"""

from __future__ import annotations

import functools
import hashlib
import os
import subprocess
import tarfile
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
LXC = ROOT / "proxmox" / "tm-activation-lxc.sh"
UPDATE = ROOT / "deploy" / "update-from-github.sh"


# ── Fake GitHub ─────────────────────────────────────────────────────────────


class _Quiet(SimpleHTTPRequestHandler):
    def log_message(self, *args) -> None:  # no noise in the pytest output
        pass


@pytest.fixture
def fake_github(tmp_path):
    """Repository served over HTTP: VERSION + releases/tm-activation-9.9.9.tar.gz (+ .sha256)."""
    repo, build = tmp_path / "repo", tmp_path / "build" / "tm-activation-9.9.9"
    (repo / "releases").mkdir(parents=True)
    build.mkdir(parents=True)
    (repo / "VERSION").write_text("9.9.9\n")
    (build / "install.sh").write_text('#!/usr/bin/env bash\necho "install.sh $*" >> "$CALLS"\n')
    archive = repo / "releases" / "tm-activation-9.9.9.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(build, arcname=build.name)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    (repo / "releases" / "tm-activation-9.9.9.tar.gz.sha256").write_text(f"{digest}  {archive.name}\n")
    server = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(_Quiet, directory=str(repo)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield repo, f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def update(tmp_path: Path, url: str, *args: str) -> subprocess.CompletedProcess:
    env = {**os.environ, "TM_RAW_URL": url, "TM_DIR": str(tmp_path / "inst"), "CALLS": str(tmp_path / "calls")}
    return subprocess.run(["bash", str(UPDATE), *args], capture_output=True, text=True, timeout=60, env=env)


def calls(tmp_path: Path) -> str:
    f = tmp_path / "calls"
    return f.read_text() if f.exists() else ""


def test_update_downloads_verifies_and_runs_install(tmp_path, fake_github) -> None:
    _, url = fake_github
    r = update(tmp_path, url, "--lan", "--non-interactive", "--force")
    assert r.returncode == 0, r.stderr
    assert "Empreinte SHA-256 vérifiée" in r.stdout
    # --force is consumed by the script; --dir added because TM_DIR is not the default.
    assert calls(tmp_path).strip() == f"install.sh --dir {tmp_path / 'inst'} --lan --non-interactive"


def test_update_skips_when_already_up_to_date(tmp_path, fake_github) -> None:
    _, url = fake_github
    (tmp_path / "inst").mkdir()
    (tmp_path / "inst" / ".tm-activation").write_text("9.9.9\n")
    r = update(tmp_path, url)
    assert r.returncode == 0 and "déjà installé" in r.stdout
    assert calls(tmp_path) == ""
    r = update(tmp_path, url, "--force")
    assert r.returncode == 0 and "install.sh" in calls(tmp_path)


def test_update_from_older_version(tmp_path, fake_github) -> None:
    _, url = fake_github
    (tmp_path / "inst").mkdir()
    (tmp_path / "inst" / ".tm-activation").write_text("1.0.0\n")
    r = update(tmp_path, url)
    assert r.returncode == 0 and "Mise à jour 1.0.0 → 9.9.9" in r.stdout
    assert "install.sh --dir" in calls(tmp_path)


def test_update_refuses_corrupted_archive(tmp_path, fake_github) -> None:
    repo, url = fake_github
    (repo / "releases" / "tm-activation-9.9.9.tar.gz.sha256").write_text(f"{'0' * 64}  tm-activation-9.9.9.tar.gz\n")
    r = update(tmp_path, url)
    assert r.returncode != 0 and "SHA-256 incorrecte" in r.stderr
    assert calls(tmp_path) == ""


def test_update_rejects_invalid_version(tmp_path, fake_github) -> None:
    repo, url = fake_github
    (repo / "VERSION").write_text("<html>404</html>")
    r = update(tmp_path, url)
    assert r.returncode != 0 and "version invalide" in r.stderr


# ── Fake Proxmox ────────────────────────────────────────────────────────────

FAKE_PCT = r"""#!/usr/bin/env bash
echo "pct $*" >> "$CALLS"
case "$1" in
  status) [[ $2 == 100 ]] && exit 0; exit 1 ;;          # la CT 100 existe déjà
  exec)
    if [[ "$*" == *"ip -4"* ]]; then echo 192.168.1.50; fi
    if [[ "$*" == *"tm-activation-update --lan"* && -n ${FAIL:-} ]]; then exit 1; fi ;;
esac
exit 0
"""
FAKE_QM = r"""#!/usr/bin/env bash
[[ $1 == status && $2 == 101 ]]                        # la VM 101 existe déjà
"""
FAKE_PVESH = r"""#!/usr/bin/env bash
# /cluster/nextid : prochain ID libre (VM et CT) ; --vmid N : échec si N est pris.
if [[ "$*" == *--vmid* ]]; then
  v="${@: -1}"
  if [[ $v == 100 || $v == 101 ]]; then echo "VM $v already exists" >&2; exit 2; fi
  echo "$v"; exit 0
fi
echo 102
"""
FAKE_PVEAM = r"""#!/usr/bin/env bash
echo "pveam $*" >> "$CALLS"
if [[ $1 == available ]]; then
  echo "system          debian-12-standard_12.7-1_amd64.tar.zst"
  echo "system          debian-13-standard_13.1-2_amd64.tar.zst"
fi
exit 0
"""
FAKE_PVESM = r"""#!/usr/bin/env bash
echo "Name Type Status Total Used Available %"
case "$*" in
  *rootdir*) echo "local-zfs zfspool active 1 1 1 1%" ;;
  *vztmpl*) echo "local dir active 1 1 1 1%" ;;
esac
"""
FAKE_ATTACH = r"""#!/usr/bin/env bash
echo "lxc-attach $*" >> "$CALLS"
[[ -z ${FAIL:-} ]]
"""


@pytest.fixture
def proxmox(tmp_path, request):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fakes = {"pct": FAKE_PCT, "qm": FAKE_QM, "pvesh": FAKE_PVESH, "pveam": FAKE_PVEAM, "pvesm": FAKE_PVESM,
             "lxc-attach": FAKE_ATTACH}
    if getattr(request, "param", "") == "sans-pvesh":
        del fakes["pvesh"]
    for name, body in fakes.items():
        (bin_dir / name).write_text(body)
        (bin_dir / name).chmod(0o755)
    # TM_LANG: the language question is skipped (tested separately).
    return {"PATH": f"{bin_dir}:{os.environ['PATH']}", "CALLS": str(tmp_path / "calls"), "TERM": "dumb",
            "TM_LANG": "fr"}


def lxc(env: dict[str, str], answers: list[str], **extra: str) -> subprocess.CompletedProcess:
    # Sourced (main() does not start by itself): check_root disabled, we are not root.
    script = f'source "{LXC}"\ncheck_root() {{ :; }}\nmain'
    return subprocess.run(["bash", "-c", script], input="\n".join(answers) + "\n", capture_output=True,
                          text=True, timeout=60, env={**os.environ, **env, **extra})


# CT 100 and VM 101 already taken → asked again, then defaults (ID 102 offered).
CT_DEFAULTS = ["100", "101", "", "", "", "", "", "", "", ""]
KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAItest test@pc"


def test_lxc_quick_test_install(tmp_path, proxmox) -> None:
    r = lxc(proxmox, [*CT_DEFAULTS, "", KEY, "secret", "secret", "2", "tm1abc", ""])
    assert r.returncode == 0, r.stdout + r.stderr
    log = calls(tmp_path)
    assert "ID invalide ou déjà utilisé" in r.stdout
    assert "pveam download local debian-13-standard_13.1-2_amd64.tar.zst" in log   # the most recent one
    create = next(line for line in log.splitlines() if line.startswith("pct create"))
    assert create.startswith("pct create 102 local:vztmpl/debian-13-standard_13.1-2_amd64.tar.zst")
    for opt in ("--hostname tm-activation", "--rootfs local-zfs:4", "--memory 512", "--features nesting=1",
                "--unprivileged 1", "--net0 name=eth0,bridge=vmbr0,ip=dhcp", "--ssh-public-keys",
                "--tags tm-activation"):
        assert opt in create, opt
    assert "avahi-daemon openssh-server" in log
    assert "raw.githubusercontent.com/f4ioz/tm-activation/main/deploy/update-from-github.sh" in log
    run = next(line for line in log.splitlines() if "/usr/local/sbin/tm-activation-update --lan" in line)
    assert "TM_CALLSIGN=TM1ABC" in run and "TM_PUBLIC=1" in run and run.endswith("--lan --non-interactive")
    assert "lxc-attach" not in log
    assert "http://192.168.1.50/" in r.stdout and "pct destroy 102" in r.stdout
    # pct exec has no /usr/local/sbin in its PATH: commands shown with the full path.
    assert "pct exec 102 -- /usr/local/sbin/tm-activation-update" in r.stdout
    assert "-- tm-activation-update" not in r.stdout


@pytest.mark.parametrize("proxmox", ["avec-pvesh", "sans-pvesh"], indirect=True)
def test_lxc_default_id_skips_existing_vm_and_ct(tmp_path, proxmox) -> None:
    """A VM also takes an ID: 100 (CT) and 101 (VM) taken → 102 offered."""
    r = lxc(proxmox, ["", "", "", "", "", "", "", "", "", "", "pw", "pw", "1", "n"])
    assert "CT 102 «" in r.stdout, r.stdout             # summary (the read -p prompt is silent outside a terminal)
    assert r.stdout.count("ID invalide ou déjà utilisé") == 0


def test_lxc_guided_install_uses_a_terminal(tmp_path, proxmox) -> None:
    answers = ["", "TM-Club", "", "", "", "", "", "", "192.168.1.60/24", "192.168.1.1", "", "pw", "pw", "", ""]
    r = lxc(proxmox, answers, TM_REPO="radioclub/tm-fork", TM_BRANCH="dev")
    assert r.returncode == 0, r.stdout + r.stderr
    log = calls(tmp_path)
    create = next(line for line in log.splitlines() if line.startswith("pct create"))
    assert "--hostname tm-club" in create and "ip=192.168.1.60/24,gw=192.168.1.1" in create
    assert "--ssh-public-keys" not in create and "openssh-server" not in log
    assert "raw.githubusercontent.com/radioclub/tm-fork/dev/deploy/update-from-github.sh" in log
    attach = next(line for line in log.splitlines() if line.startswith("lxc-attach"))
    assert attach.startswith("lxc-attach -n 102 -- env") and "TM_REPO=radioclub/tm-fork" in attach
    assert attach.endswith("/usr/local/sbin/tm-activation-update")


def test_lxc_cancel_creates_nothing(tmp_path, proxmox) -> None:
    r = lxc(proxmox, [*CT_DEFAULTS, "", "", "pw", "pw", "1", "n"])
    assert r.returncode == 0 and "rien n'a été créé" in r.stdout
    assert "pct create" not in calls(tmp_path)


def test_lxc_install_failure_keeps_ct_and_explains(tmp_path, proxmox) -> None:
    r = lxc(proxmox, [*CT_DEFAULTS, "", "", "pw", "pw", "2", "", ""], FAIL="1")
    assert r.returncode == 1
    assert "la CT 102 est conservée" in r.stderr and "pct enter 102" in r.stdout
    assert "pct exec 102 -- /usr/local/sbin/tm-activation-update" in r.stdout
    assert "destroy" not in calls(tmp_path)


def test_scripts_are_valid_bash() -> None:
    for script in (LXC, UPDATE):
        r = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr


def test_lxc_asks_the_language_and_speaks_english(tmp_path, proxmox) -> None:
    """Without TM_LANG: first question « Langue / Language », then everything in English."""
    env = {k: v for k, v in proxmox.items() if k != "TM_LANG"}
    r = lxc(env, ["2", *CT_DEFAULTS, "", "", "pw", "pw", "2", "", ""])
    assert r.returncode == 0, r.stdout + r.stderr
    assert "1) Français" in r.stdout and "2) English" in r.stdout   # the read -p prompt is silent outside a terminal
    # Questions (read -p) are silent outside a terminal: we check the printed lines.
    for expected in ("Summary", "Container 102 created.", "Packages installed.",
                     "TM Activation installed in container 102", "Delete the test container"):
        assert expected in r.stdout, expected
    assert "Récapitulatif" not in r.stdout and "Disque" not in r.stdout
