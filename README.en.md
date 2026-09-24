*Version française : [README.md](README.md)*

# TM Activation

**Your club is activating a special callsign? Here is everything to run the
schedule, the log and the public page — with nothing to install on anyone's
computer.**

Web application for the activation of amateur radio **special callsigns** (TM…,
TO…, etc.) by a radio club: each operator books their slots and logs their QSOs
from a phone or a PC, while hunters follow the activation live on a public page.

![Public activation page](docs/images/public.png)

- **Shared schedule**: who is on air, when, on which band and mode, with an
  overlap warning, countdowns and the QSO count of each slot.
- **Multi-operator QSO log**: quick entry, "now" time, duplicates flagged as
  soon as the callsign is typed, QRZ lookup, satellite mode, **rate gauges**
  (QSO/h and 10 min, with the trend), a **DX spot panel** ("am I being
  spotted?") and the **flags of the countries worked**, added as the QSOs come
  in.
- **ADIF**: import with a preview, full or selective export, ready for
  TQSL / LoTW.
- **A certificate for hunters**: whoever finds their QSOs on the public page
  leaves with a **PDF certificate** in A4 landscape — their contacts, their
  points, their place in the ranking, the club logo and the name of the
  activation, with the special callsign in pride of place, underlined by its own
  Morse. Opened to the public (or not) with one click in Settings; the hunter's
  name only appears if the administrator asks for it, and both the number of
  contacts listed and the appendix are adjustable.
- **A PDF report** for the administrator: counters, pace day by day, bands,
  modes, **DXCC entities with their flags** and a hunter ranking. Each section
  QSOs made through **satellites** get their own section, broken down per
  satellite, and the **best moments** (the pile-up periods, with their rate) are
  listed. The document can be forced onto **a single page** — and even
  without that option, an overflow of three lines is absorbed by tightening the
  layout rather than printing another page. Each section can be switched on or off in Settings, and the others
  use the room freed up. A
  **club logo** can be uploaded: it appears at the top of the report, in the page
  banner and in the site header. All generated without any dependency — offline
  too.
- **Public page** for hunters: current and upcoming activations, **a contacts
  map coloured by mode and shaped by band, filtered with tick boxes**, a DXCC
  table with flags (no QRZ account needed), points ranking and an "am I in the
  log?" search.
- **Operator accounts**, your choice: one shared password, or one per operator
  with optional approval and several administrators.
- **French and English**, picked by the visitor.

It installs in a few minutes on a **Raspberry Pi**, a **Proxmox container** or a
Linux server, and runs on its own — including on a local network with **no
Internet** (libraries, fonts, calendar and flags are bundled; only the map
background, the QRZ lookups and the DX spots need a connection).

The log page during an activation: current slot, DX spots, rate gauges,
countries already worked, and the log book right next to the entry form.

![The log page during an activation](docs/images/log.png)

| Slot schedule | Settings: accounts and public display |
|---|---|
| ![Slot schedule](docs/images/planning.png) | ![Settings: operator accounts and public display](docs/images/settings.png) |

![DXCC worked and hunter ranking](docs/images/dxcc.png)

The contact map: one colour per mode, one shape per band, and the legend
doubles as a filter.

![Contacts map](docs/images/map.jpg)

Developed by Olivier F4IOZ, then extracted from his website to be shared with
radio clubs.

## Download

Source code and latest versions: **<https://github.com/f4ioz/tm-activation>**

- zip archive: <https://github.com/f4ioz/tm-activation/raw/main/releases/tm-activation-1.38.0.zip>
- tar.gz archive: <https://github.com/f4ioz/tm-activation/raw/main/releases/tm-activation-1.38.0.tar.gz>
- SHA-256 checksums and previous versions: the
  [`releases/`](https://github.com/f4ioz/tm-activation/tree/main/releases) folder

If the Pi has Internet access, the archive can be downloaded straight onto it:

```bash
wget https://github.com/f4ioz/tm-activation/raw/main/releases/tm-activation-1.38.0.tar.gz
```

## Features

- **Operators area** (`/activation`), protected by a shared password: each
  operator logs in with their callsign.
  - **Operator accounts (your choice)**: one password shared by everyone, or
    **one password per operator**, set at their first login (their callsign is
    enough). The password requirements are adjustable (length, capitals, digits,
    special characters; 8/1/1/1 by default, 0 = not required) and the rule in
    force is shown on the login page. An **anti-robot question** (simple sum, no
    outside service) protects account creation. Approval of new accounts by an
    administrator is optional, and several operators can be **administrators**.
    Settings → Operator accounts.
  - **Everyone on their own callsign**: an operator who is not ticked
    "administrator" logs, books slots, imports and exports under the callsign
    given at login only, and cannot edit or delete other operators' QSOs and
    slots — with a password per operator as well as with the shared password.
    They still see the station's whole log — handy to spot duplicates — but
    their exports contain only their own QSOs. With the shared password this is
    a guard against mistakes, not a barrier: whoever knows the password can log
    back in under another callsign. Administrators keep full control, and the
    Settings still require a personal password (or the site one).
  - **Schedule** of slots (who, when, band, mode), with a warning if two slots
    overlap on the same band, countdowns and the **number of QSOs logged** in
    each slot (same operator, same band, same mode); nothing is shown until a
    QSO is recorded, as the log may simply not be imported yet. Dates and times
    are picked in a **calendar** with slider time setting and **shortcuts**
    (Now, Tonight 8 pm, Tomorrow 9 am, +1 h/+2 h/+4 h); moving the start moves
    the end and keeps the length. With no Internet, a simple bundled calendar
    takes over; on a phone, the native picker is used.
  - **The log catches what was forgotten**: an operator who goes on air without
    booking gets their slot created from their QSOs, and one who runs over the
    planned time gets theirs extended. The log page shows the **slots right
    now**, with the logged-in operator's one highlighted. The catch-up runs when
    a QSO is logged and on ADIF import; a Settings button triggers it on demand
    (handy after an import made elsewhere) and reports what it created or
    extended. A slot is never shortened or deleted.
  - **Never two stations at once** on the same band and mode: if another
    operator has booked it, the entry is flagged in red and the QSO is refused
    (can be switched off).
  - Quick **QSO log**: "now" time, duplicate detection, QRZ lookup (name,
    locator, country), satellite mode. As soon as the callsign is typed, the
    screen says whether the **station has already been worked**: never seen,
    already in the log (with the bands/modes and the date of the last QSO), or a
    **duplicate** on the chosen band and mode; QSOs made under another club
    callsign are reported separately.
  - **Rate gauges**: next to "Operator on the mic", side by side, the number of
    QSOs in the last hour and in the last 10 minutes, the rate per hour, a trend
    arrow (▲ / ▼ against the previous period) and two gauges that turn green
    when it takes off — from "quiet station" to "pile-up!". Updated with every
    QSO logged.
  - **The log stays in sight**: on a wide screen, entry on the left and the
    latest QSOs on the right, visible while logging; the page is tightened so
    that about ten rows fit without scrolling.
  - **Who is on the other end?**: as soon as the locator is known — typed in or
    from QRZ — a **compass** shows where to turn the antenna, with the
    **bearing and the distance**, next to the **photo from the station's QRZ
    page**. Both sizes are adjustable (0 = hidden), and everything is worked out
    in the browser: no waiting between two QSOs.
  - **Am I being spotted?**: the last three DX spots for the callsign **on the
    band and kind of traffic being worked** (DXWatch, with HamQTH as a fallback)
    are shown next to the gauges, with the frequency, the spotter and the age;
    clicking the frequency copies it into the form. Change band or mode and the
    spots follow: the cluster does not give the mode, so it is read from the
    spot comment ("CQ LSB", "FT8 -06db") and, failing that, from the band plan.
    With no Internet the panel simply disappears, with no error.
  - **Countries worked**: the flags of the DXCC entities already worked are added
    with every QSO logged, most recent first (derived from the callsign prefix,
    with the images served by the application: no network access needed, and it
    looks the same on Windows, Android or Linux). Separate DXCC entities stay
    separate: Corsica, the Canaries, Sicily, Madeira, Alaska or Kaliningrad
    count on their own, each with its own flag, just like Guadeloupe or French
    Guiana.
  - **ADIF**: two-stage import (preview of new QSOs, duplicates and invalid
    lines, then confirmation) and full or selective export, ready to sign with
    TQSL for LoTW. CSV export.
- **Public page** per callsign (`/tm50abc`): live and upcoming activations, map
  of the stations worked, **DXCC table with flags**, hunter ranking
  (configurable points rule), "am I in the log?" search. The DXCC entity is
  derived from the callsign prefix, so the table and the flags are right from
  the very first QSO, **even without a QRZ account** — QRZ, when configured,
  refines the official name of the entity. The names of the stations worked are
  never published.
- **A map you can read at a glance**: one dot per grid square, band and mode —
  the **colour is the mode**, the **shape is the band**. Dots sharing a square
  are spread in a ring so they all stay clickable, and a **tick-box legend**
  under the map shows only 40 m, only CW, or any combination ("All" / "None").
  Colours and shapes are set in Settings; the filters can be switched off for a
  fixed map.
- **Every slot card**, upcoming and past, unless you'd rather cap how many
  (Settings). Each card carries its QSO tally, recomputed on every page view.
- **Several special callsigns**: only one "current" at a time, the previous ones
  remain available to view (`/activations`).
- **Everyone at their own time**: "Local" shows times in the **visitor's** time
  zone, as announced by their browser — a Canadian hunter reads the slots at
  their own time without setting anything, and the times they type are
  understood in their zone. A **Time: Local / UTC** button on the pages, and UTC
  everywhere in the database and the ADIF. With no JavaScript, the station's
  time zone (`site.timezone` in `config.yml`) is the reference.
- **Français / English**: every page is displayed in the browser's language
  (English for foreign hunters), with an **FR | EN** button in the banner; the
  choice is remembered. The installer asks the question when it starts (or
  `--lang en`). Translations: `app/locales/en.json` for the pages,
  `deploy/lang/en.sh` for the installer.
- **Automatic backups** of the database (at start-up and after every change),
  downloadable from the Settings.
- **Security**: blocking of password guessing and scanner bots, security
  headers, operator pages not indexed. A **monitoring** page
  (`/admin/surveillance`, administrator) shows the login log, the IPs with
  repeated attempts and the blocked robots.

### Options (Settings)

Everything can be switched on or off from **Settings** (administrator): nothing
is forced on you.

| Option | Default | Effect |
|---|---|---|
| One password per operator | off | individual accounts instead of the shared password |
| Approve new accounts | off | an administrator approves before the first login |
| Password requirements | 8 chars, 1 capital, 1 digit, 1 special | every threshold adjustable (0 = not required) |
| Create and extend slots from the log | on | catches forgotten bookings |
| Realign slots on the log now | button | immediate catch-up, even when unticked |
| Slot cards shown | all | or a maximum number on the public page |
| PDF activity report | button | illustrated summary, sections of your choice |
| Hour-by-hour pace in the PDF | off | histogram of the 24 UTC hours |
| DXCC entities with flags in the PDF | on | every entity (otherwise the first ten) |
| Hunters listed in the PDF | 10 | 0 to drop the ranking |
| Satellite breakdown in the PDF | on | QSOs per satellite, after the modes |
| Single-page PDF | off | tightens up, and trims the lists if needed |
| Club logo | optional | large page banner (display can be switched off) and PDF report |
| QRZ photo and compass on the log | 96 and 120 px | adjustable size, 0 to hide |
| Best moments in the PDF | 3 | pile-up periods listed, 0 to drop |
| Hunter certificates | off | PDF downloadable from the public page |
| Contacts on the certificate | 10 | plus the full log as an appendix (can be dropped) |
| Flag on the certificate | from the callsign | or a chosen entity, or none |
| Certificate border | off | three lines, colours of your choice (blue-white-red by default) |
| QR code on the certificate | none | any address (club website, activation page…), also printed in plain text |
| Refuse logging on a booked band and mode | on | avoids two stations at once |
| Show the contact list | your choice | "Latest contacts" on the public page |
| Show map, DXCC and ranking | your choice | the "hunters" part of the public page |
| Points ranking | off | otherwise ranked by distinct band × mode pairs |
| Map colours and shapes | on | one colour per mode, one shape per band (adjustable) |
| Band/mode filters on the map | on | tick boxes to show only what you care about |
| QRZ.com account | optional | names, locators and countries of the stations worked |
| Local time or UTC | local | local time is **the visitor's own**, remembered |
| French / English | browser | button in the banner, remembered |

## Three ways to use it

| | Local network | Internet through your router | Internet through Cloudflare Tunnel |
|---|---|---|---|
| For whom | operators on site | hunters and operators, from anywhere | same, without touching the router |
| Access | `http://<machine-name>.local` | `https://<your-domain>` | `https://<your-domain>` |
| Domain name | no | yes | yes, managed by Cloudflare |
| Ports to open | none | 80 and 443 on the router | none |
| Dedicated public IPv4 | no | required | no (works on 4G, shared IPv4) |
| Installation | `sudo ./install.sh --lan` | `sudo ./install.sh --domain tm.mon-club.fr --email vous@exemple.fr` | `sudo ./install.sh --tunnel --domain tm.mon-club.fr` |
| Details | below | [docs/internet-raspberry-pi.en.md](docs/internet-raspberry-pi.en.md) | [docs/cloudflare-tunnel.en.md](docs/cloudflare-tunnel.en.md) |

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
   `scp tm-activation-1.38.0.tar.gz utilisateur@tm50abc.local:`
   (or download it straight onto the Pi with `wget`, see
   [Download](#download))
4. On the Pi:

   ```bash
   tar xzf tm-activation-1.38.0.tar.gz
   cd tm-activation-1.38.0
   sudo ./install.sh --lan
   ```

   From the zip (sent by email, passed through Windows):

   ```bash
   unzip tm-activation-1.38.0.zip
   cd tm-activation-1.38.0
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
  for every callsign already logged;
- the **DX spots**: the "am I being spotted?" panel simply disappears from the
  log page.

The **flags of the countries worked** need nothing at all: the DXCC entity is
derived from the callsign prefix and the images are served by the application.

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
tar xzf tm-activation-1.38.0.tar.gz
cd tm-activation-1.38.0
sudo ./install.sh --domain tm.mon-club.fr --email vous@exemple.fr
```

The script installs nginx and configures it for the domain, obtains the Let's
Encrypt HTTPS certificate (renewed automatically), then starts the application
behind nginx. Without `--email`, HTTPS is enabled later with
`sudo certbot --nginx -d tm.mon-club.fr`.

**Raspberry Pi behind the club's or the home router**: static address for the
Pi, public IPv4 address, domain name (the club's or a free DuckDNS one), port
forwarding on the router… everything is detailed step by step in
[`docs/internet-raspberry-pi.en.md`](docs/internet-raspberry-pi.en.md). The operators
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
