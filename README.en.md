*Version française : [README.md](README.md)* — the step-by-step guides in `docs/` are still French only.

# TM Activation

Web application to manage the activation of amateur radio **special callsigns**
(TM…, TO…, etc.) by a radio club: operator schedule, multi-operator QSO log,
ADIF import/export and a public page for hunters.

It installs in a few minutes on a **Raspberry Pi** or a **Linux server** and
runs on its own, including on a local network with no Internet.

Developed by Olivier F4IOZ, then extracted from his website to be shared with
radio clubs.

## Download

Source code and latest versions: **<https://github.com/f4ioz/tm-activation>**

- zip archive: <https://github.com/f4ioz/tm-activation/raw/main/releases/tm-activation-1.4.0.zip>
- tar.gz archive: <https://github.com/f4ioz/tm-activation/raw/main/releases/tm-activation-1.4.0.tar.gz>
- SHA-256 checksums and previous versions: the
  [`releases/`](https://github.com/f4ioz/tm-activation/tree/main/releases) folder

If the Pi has Internet access, the archive can be downloaded straight onto it:

```bash
wget https://github.com/f4ioz/tm-activation/raw/main/releases/tm-activation-1.4.0.tar.gz
```

## Features

- **Operators area** (`/activation`), protected by a shared password: each
  operator logs in with their callsign.
  - **Schedule** of slots (who, when, band, mode), with a warning if two slots
    overlap on the same band, and countdowns.
  - Quick **QSO log**: "now" time, duplicate detection, QRZ lookup (name,
    locator, country), satellite mode.
  - **ADIF**: two-stage import (preview of new QSOs, duplicates and invalid
    lines, then confirmation) and full or selective export, ready to sign with
    TQSL for LoTW. CSV export.
- **Public page** per callsign (`/tm50abc`): live and upcoming activations, map
  of the stations worked, DXCC table, hunter ranking (configurable points
  rule), "am I in the log?" search. The names of the stations worked are never
  published.
- **Several special callsigns**: only one "current" at a time, the previous ones
  remain available to view (`/activations`).
- **Français / English**: every page is displayed in the browser's language
  (English for foreign hunters), with an **FR | EN** button in the banner; the
  choice is remembered. The installer asks the question when it starts (or
  `--lang en`). Translations: `app/locales/en.json` for the pages,
  `deploy/lang/en.sh` for the installer.
- **Automatic backups** of the database (at start-up and after every change),
  downloadable from the Settings.
- **Security**: blocking of password guessing and scanner bots, security
  headers, operator pages not indexed.

## Three ways to use it

| | Local network | Internet through your router | Internet through Cloudflare Tunnel |
|---|---|---|---|
| For whom | operators on site | hunters and operators, from anywhere | same, without touching the router |
| Access | `http://<machine-name>.local` | `https://<your-domain>` | `https://<your-domain>` |
| Domain name | no | yes | yes, managed by Cloudflare |
| Ports to open | none | 80 and 443 on the router | none |
| Dedicated public IPv4 | no | required | no (works on 4G, shared IPv4) |
| Installation | `sudo ./install.sh --lan` | `sudo ./install.sh --domain tm.mon-club.fr --email vous@exemple.fr` | `sudo ./install.sh --tunnel --domain tm.mon-club.fr` |
| Details | below | [docs/internet-raspberry-pi.md](docs/internet-raspberry-pi.md) | [docs/cloudflare-tunnel.md](docs/cloudflare-tunnel.md) |

Run with no option, `sudo ./install.sh` is a **guided installation**. It first
asks all the questions (usage, domain, router, callsign, club, passwords) and
shows a summary **before making any change**. In Internet mode, it then guides
you through the router settings (Freebox, Livebox, SFR, Bbox), with the Pi's
address and MAC. It also checks DNS, then makes a dry run with Let's Encrypt
before requesting the real certificate, and explains the cause if it fails.

## Hardware and system

- **Raspberry Pi** 3, 4, 5 or Zero 2 W, with a microSD card of 16 GB or more,
  and **Raspberry Pi OS Lite 64-bit** (Bookworm or newer). The 32-bit version
  works too.
- Or any **Linux server**: Debian 12/13, Ubuntu 22.04 or newer.
- Python 3.11 or newer: that is the version shipped by these systems. Raspberry
  Pi OS Bullseye and Debian 11 are too old.
- **Internet access during installation**: Debian and Python packages.
  Everything is precompiled for the Raspberry Pi processors, nothing is
  compiled on the spot.
- Optional: a QRZ.com account with an **XML subscription** for the names,
  locators and countries of the stations worked (entered during installation
  or, later, in **Settings → QRZ Callbook**).

The application uses about 80 MB of memory.

## Installation on a Raspberry Pi, step by step

1. With **Raspberry Pi Imager**, write *Raspberry Pi OS Lite (64-bit)*. In the
   Imager settings, choose the **machine name** (e.g. `tm50abc`: this will be
   the address `http://tm50abc.local`), the user and password, the Wi-Fi if
   needed, and **enable SSH**.
2. Start the Pi, then connect to it from a PC on the same network:
   `ssh utilisateur@tm50abc.local`
3. Copy the archive onto the Pi, from the PC:
   `scp tm-activation-1.4.0.tar.gz utilisateur@tm50abc.local:`
   (or download it straight onto the Pi with `wget`, see
   [Download](#download))
4. On the Pi:

   ```bash
   tar xzf tm-activation-1.4.0.tar.gz
   cd tm-activation-1.4.0
   sudo ./install.sh --lan
   ```

   From the zip (sent by email, passed through Windows):

   ```bash
   unzip tm-activation-1.4.0.zip
   cd tm-activation-1.4.0
   sudo bash install.sh --lan
   ```

   The script installs what is missing (Python, avahi), asks a few questions
   (callsign, locator, club, passwords) and starts the service.
5. Open **http://tm50abc.local/** from a phone or a PC on the same network. If
   the `.local` address does not answer (some Android devices or older
   Windows), use the IP address shown at the end of the installation.

The service restarts on its own every time the Pi is powered up.

### Raspberry Pi clock

**Important for the log**: the Raspberry Pi has no battery-backed clock. At
every boot it updates its time over the Internet (NTP). Without Internet its
time is wrong, and so are the QSOs entered with "Now".

- With Internet at boot (router, phone tethering), there is nothing to do.
- Without Internet: use a **Raspberry Pi 5** with its clock battery (RTC
  connector), or an RTC clock module (DS3231) on the other models. Failing
  that, set the Pi's time by hand after every boot:
  `sudo date -u -s "2026-09-20 14:05"` (UTC time).
- Check: `timedatectl`. The installer warns if the clock is not synchronised.

### Without Internet

On a local network with no Internet, **everything works**: schedule, log, ADIF,
public page, backups. The libraries and fonts of the pages are provided by the
application itself. Only the following are missing:

- the OpenStreetMap **base map**: the stations are still placed, on an empty
  background;
- the **QRZ lookups**: they are done automatically when Internet comes back,
  for every callsign already logged.

## Installation on Proxmox VE (LXC container)

On a **Proxmox VE** server, a single command creates a dedicated Debian
container, downloads the latest version from GitHub (SHA-256 checksum verified)
and installs it. In the Proxmox **node Shell**, as root:

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/f4ioz/tm-activation/main/proxmox/tm-activation-lxc.sh)"
```

The script asks for the container's characteristics (ID, name, 4 GB disk,
1 vCPU, 512 MB of RAM, storage, DHCP or static IP network, root password,
optional SSH key), then offers:

1. **Guided installation**: the usual TM Activation questions (local network,
   Internet through the router with HTTPS, or Cloudflare Tunnel; callsign,
   club, passwords). In Internet-through-the-router mode, ports 80 and 443 must
   be forwarded to the container's IP.
2. **Quick test**: local network, no questions, callsign `TM0TEST`,
   administrator password generated and shown at the end. Ideal to try it out,
   then throw the container away (`pct stop <ID> && pct destroy <ID>`).

The container is **unprivileged** (option `nesting=1`, required by the hardened
service), starts with the node and takes its time from the host. Afterwards,
from the node:

| Action | Command |
|---|---|
| Update | `pct exec <ID> -- /usr/local/sbin/tm-activation-update` |
| Diagnostics | `pct exec <ID> -- /opt/tm-activation/install.sh --check` |
| Log | `pct exec <ID> -- journalctl -u tm-activation -n 50` |
| Console | `pct enter <ID>` |

Optional variables (before `bash -c …`): `TM_REPO` (another GitHub repository,
e.g. a fork), `TM_BRANCH`, `TM_VERSION` (a specific version).

## Installation on an Internet server

Beforehand: the domain name (e.g. `tm.mon-club.fr`) must point to the server
(DNS A/AAAA record), and ports 80 and 443 must be open.

```bash
tar xzf tm-activation-1.4.0.tar.gz
cd tm-activation-1.4.0
sudo ./install.sh --domain tm.mon-club.fr --email vous@exemple.fr
```

The script installs nginx and configures it for the domain, obtains the Let's
Encrypt HTTPS certificate (renewed automatically), then starts the application
behind nginx. Without `--email`, HTTPS is enabled later with
`sudo certbot --nginx -d tm.mon-club.fr`.

**Raspberry Pi behind the club's or the home router**: static address for the
Pi, public IPv4 address, domain name (the club's or a free DuckDNS one), port
forwarding on the router… everything is detailed step by step in
[`docs/internet-raspberry-pi.md`](docs/internet-raspberry-pi.md). The operators
on site keep the `http://<pi-name>.local` access.

If you already manage nginx yourself, use `--manual` and start from
`deploy/nginx.conf.example`. If nginx runs on **another machine**, add
`--host 0.0.0.0` and put nginx's IP in `server.trusted_proxies`
(`config.yml`).

## Installation options

| Option | Purpose | Default |
|---|---|---|
| `--lan` | local network: port 80, `http://<name>.local` | asked |
| `--domain DOMAINE` | Internet server: nginx for this domain | asked |
| `--email EMAIL` | with `--domain`: Let's Encrypt HTTPS certificate | — |
| `--box NOM` | router of the connection (`freebox`, `livebox`, `sfr`, `bbox`, `autre`): matching settings | asked |
| `--tunnel` | with `--domain`: publication through Cloudflare Tunnel, without opening ports | asked |
| `--tunnel-name NOM` | name of the Cloudflare tunnel | from the domain |
| `--manual` | advanced: `127.0.0.1:8000`, reverse proxy is up to you | — |
| `--check` | diagnostics: service, network, DNS, certificate, clock (nothing is changed) | — |
| `--dir DIR` | installation folder | `/opt/tm-activation` |
| `--user USER` | system account of the service | `tmact` |
| `--service NAME` | name of the systemd service (several instances possible) | `tm-activation` |
| `--host IP` / `--port PORT` | listening address and port | depends on the mode |
| `--python BIN` | Python interpreter | `python3` |
| `--no-systemd` | without service or root (trial, development) | — |
| `--non-interactive` | no questions: values read from the `TM_*` variables | — |

Installation with no questions (automation):

```bash
sudo TM_CALLSIGN=TM50ABC TM_LABEL="50 ans du radio-club" TM_GRID=JN18FS \
     TM_CLUB_NAME="Radio-club de Villeneuve" TM_CLUB_CALLSIGN=F6ABC \
     TM_OPERATOR_PASSWORD='…' ./install.sh --lan --non-interactive
```

Recognised variables: `TM_CALLSIGN` (required), `TM_LABEL`, `TM_GRID`,
`TM_PUBLIC` (1 = public page online), `TM_CLUB_NAME`, `TM_CLUB_CALLSIGN`,
`TM_CLUB_CITY`, `TM_CLUB_WEBSITE`, `TM_BASE_URL`, `TM_OPERATORS` (comma
separated list), `TM_ADMIN_PASSWORD` (generated if empty),
`TM_OPERATOR_PASSWORD`, `TM_QRZ_USER`, `TM_QRZ_PASSWORD`, `TM_TRUSTED_PROXIES`.

Quick trial without root on a PC: `./install.sh --no-systemd --dir ~/tm-activation`,
then run the command shown and open <http://127.0.0.1:8000/>.

## Moving the Pi, diagnostics

The Pi can change location (radio club, home, portable activation). In
**Internet through the router** mode, each new connection means redoing, on the
new router, the DHCP lease and the forwarding of ports 80 and 443, then
pointing the DNS record to the new public address. In **Cloudflare Tunnel**
mode, there is nothing to redo. The diagnostics show what is missing:

```bash
sudo /opt/tm-activation/install.sh --check
```

It checks the service, the application, the local and public address, DNS, the
HTTPS certificate, nginx and the clock, without changing anything.

## First steps

1. **Administrator**: open `/login` (administrator password chosen or shown
   during installation), which leads to the **Settings**
   (`/activation/settings`).
2. In the Settings, **Operators password**: set it if it was not set during
   installation. **QRZ Callbook**: enter the club's QRZ.com account (XML
   subscription); the connection is tested before saving.
3. **Special callsigns** → ✎: fill in the record (label, locator, dates, badge,
   subtitle, flags) and tick **Public page online** when you are ready.
4. Give the operators the address `/activation/login` and the shared password.
   Each of them logs in with **their** callsign and automatically joins the
   list of operators.
5. The public page is `/tm50abc` (callsign in lower case). The root of the site
   redirects to it.

For a new callsign, just create it in the Settings (**+ New special callsign**),
then click **Set as current**.

## Configuration

The file is `/opt/tm-activation/config.yml`. `config.yml.example` describes
every key. After any change:

```bash
sudo systemctl restart tm-activation
```

| Section | Contents |
|---|---|
| `auth.password` | administrator password |
| `club` | name, callsign, town and website of the club (banner, footer, `/activations`) |
| `qrz` | default QRZ.com XML account (optional); an account entered in **Settings → QRZ Callbook** takes precedence |
| `activation` | first special callsign, read **at first start-up only**; after that everything is set in the interface |
| `server.trusted_proxies` | addresses of the reverse proxy allowed to pass on the visitors' IP |

## Update

New versions are published on <https://github.com/f4ioz/tm-activation> (the
`releases/` folder).

The simplest way, if the machine has Internet access: download and install the
latest version with a single command (checksum verified; nothing is done if the
installed version is already the latest):

```bash
sudo bash /opt/tm-activation/deploy/update-from-github.sh
```

(on a Proxmox container, from the node:
`pct exec <ID> -- /usr/local/sbin/tm-activation-update`; the full path is
required, `pct exec` does not search `/usr/local/sbin`).

Or by hand, from the archive of the new version:

```bash
tar xzf tm-activation-X.Y.Z.tar.gz
cd tm-activation-X.Y.Z
sudo ./install.sh
```

The options of the first installation (mode, port, domain…) are picked up
automatically: they are stored in `install.env`. The code and the dependencies
are replaced and the service restarts. `config.yml`, the `var/` folder
(database, passwords, backups) and the nginx configuration are **never**
touched. As a precaution, the database is first copied to
`var/backups/preupgrade-*.sqlite`.

## Backups and restoring

All the data is in `/opt/tm-activation/var/`:

| File | Contents |
|---|---|
| `activation.sqlite` | callsigns, operators, slots, QSOs, QRZ records |
| `activation_password` | operators password (if it was changed in the Settings) |
| `activation_settings.json` | settings: current callsign, public display, points rule |
| `activation_qrz.json` | QRZ.com account entered in the Settings (readable by the service only, excluded from backups) |
| `auth_secret` | session signing key |
| `backups/` | automatic snapshots (the last 40) |

A snapshot is taken at start-up and after every change (at most one every
10 minutes). **Settings → Download (.sqlite)** fetches a copy of it. A microSD
card can fail: remember to download this copy regularly, for example at the end
of each day of activation.

To restore:

```bash
sudo systemctl stop tm-activation
sudo cp /opt/tm-activation/var/backups/activation-AAAAMMJJ-HHMMSS.sqlite /opt/tm-activation/var/activation.sqlite
sudo chown tmact:tmact /opt/tm-activation/var/activation.sqlite
sudo systemctl start tm-activation
```

## Uninstalling

```bash
sudo systemctl disable --now tm-activation
sudo rm /etc/systemd/system/tm-activation.service && sudo systemctl daemon-reload
# Internet mode: sudo rm /etc/nginx/sites-*/tm-activation && sudo systemctl reload nginx
# Back up /opt/tm-activation/var/ first if the log must be kept!
sudo rm -r /opt/tm-activation
sudo userdel tmact
```

## Known limitations

- Local time: Europe/Paris time zone (the same as Belgium, Luxembourg and
  Switzerland). UTC time is always available through the "Time" selector, and
  storage is done in UTC.
- LoTW: no automatic upload. The ADIF export is signed with TQSL, which asks
  for the special callsign's certificate.
- A single process (worker): more than enough for an activation.

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest
.venv/bin/uvicorn app.main:app --reload
```

Without a `config.yml`, the application starts with `config.yml.example`.
`BUILD_INFO` gives the version and the origin of this package's code.

## Licence

Code under the MIT licence (see `LICENSE`). Embedded libraries and fonts: see
`static/vendor/README.md`.
