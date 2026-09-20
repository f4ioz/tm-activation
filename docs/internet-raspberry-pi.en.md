*Version française : [internet-raspberry-pi.md](internet-raspberry-pi.md)*

# Publishing TM Activation on the Internet from a Raspberry Pi (behind a router)

Aim: **hunters** read the callsign's page from anywhere
(`https://tm.mon-club.fr/tm50abc`), and **operators** log contacts on site or
from a distance. The Raspberry Pi is plugged in over **Ethernet** to the club's
or the home router.

```
 Hunters, remote operators                   Operators on site
          │  https://tm.mon-club.fr            │  http://<pi-name>.local
          ▼                                    │
   ┌─────────────┐  ports 80 and 443  ┌────────▼────────────────────────┐
   │   Router    │ ─────────────────▶ │ Raspberry Pi                    │
   │ (public v4) │   forwarded        │ nginx (HTTPS) → TM Activation   │
   └─────────────┘                    └─────────────────────────────────┘
```

**You need:**

- a Raspberry Pi installed with Raspberry Pi OS Lite 64-bit and SSH enabled
  (README, "Installation on a Raspberry Pi", steps 1 and 2);
- access to the router's administration interface;
- a domain name: the club's one, or a free name (DuckDNS);
- about an hour.

**The easiest way: the guided installation.** Run `sudo ./install.sh` and
choose **Internet through your router**. The installer asks the questions and shows the exact
settings for your router, with the Pi's address and MAC. It checks the public
address and the DNS itself, then tests HTTPS before asking for the
certificate. This document covers the same steps, to understand or to
troubleshoot.

Follow the steps **in order**: the HTTPS certificate (step 5) can only be
obtained once the domain name and the router are ready.

---

## Step 1 — Give the Pi a fixed address on the local network

The router must always give the Pi the same address, otherwise the port
forwarding of step 4 would end up pointing nowhere.

1. On the Pi, note its address and its network identifier (MAC address):

   ```bash
   hostname -I                                     # e.g. 192.168.1.42
   cat /sys/class/net/eth0/address                 # e.g. d8:3a:dd:12:34:56
   ```

2. In the router's interface, under **DHCP**, create a **static lease** (French
   routers call this « bail statique » or « Baux statiques », also
   « réservation d'adresse »): this MAC address → this IP address.

## Step 2 — Check that the router has its own public IPv4 address

Some subscriptions share a single IPv4 address between several customers:
ports 80 and 443 are then unreachable.

1. On the Pi: `curl -4 ifconfig.me` shows the public address as seen from the
   Internet.
2. Compare it with the **IPv4 address** shown in the router's interface.
   - Same address: all good.
   - Router address in `100.64.x.x` to `100.127.x.x`, a different address, or a
     « plage de ports » (port range) shown on a Freebox: the address is
     **shared**.
     - **Free**: in your subscriber area, ask for an **« IP fixe V4
       full-stack »** (the full option is « Demander une adresse IP fixe V4
       full-stack »; free of charge, active after the Freebox restarts).
     - Other providers: ask customer services for a dedicated public IPv4
       address.
3. Note whether the address changes (router restart, outage): in that case,
   step 3 uses a dynamic DNS. When in doubt, use it.

## Step 3 — The domain name

### Option A — Free DuckDNS name (the simplest)

1. On <https://www.duckdns.org>, sign in (Google account, GitHub…), then create
   a subdomain, for example `monclub-tm`, which gives
   `monclub-tm.duckdns.org`. Note the **token** shown at the top of the page.
2. On the Pi, update the address every 5 minutes (replace `monclub-tm` and
   `VOTRE-TOKEN` with your own token):

   ```bash
   echo '*/5 * * * * root curl -fsS "https://www.duckdns.org/update?domains=monclub-tm&token=VOTRE-TOKEN&ip=" >/dev/null' \
     | sudo tee /etc/cron.d/duckdns
   sudo chmod 600 /etc/cron.d/duckdns
   curl "https://www.duckdns.org/update?domains=monclub-tm&token=VOTRE-TOKEN&ip="   # should print OK
   ```

   (If `cron` is missing: `sudo apt install cron`.)

### Option B — The club's domain (e.g. `tm.mon-club.fr`)

In the domain's DNS zone (at the registrar, or ask the club's webmaster), add
**one** record:

| Router's address | Record |
|---|---|
| fixed | `tm  A  <public IPv4 address>` |
| variable | `tm  CNAME  monclub-tm.duckdns.org.` (option A as well, for the updating) |

**No AAAA (IPv6) record**: Let's Encrypt would try IPv6, which the router
blocks inbound, and the certificate would be refused.

### Checking

```bash
getent hosts tm.mon-club.fr        # should print the public address from step 2
```

A new record can take up to an hour to become visible.

## Step 4 — Forward ports 80 and 443 from the router to the Pi

In the router's interface, create two **port forwarding** rules (also called
NAT, PAT or port translation):

| Protocol | External port | Destination (Pi's IP, step 1) | Internal port |
|---|---|---|---|
| TCP | 80 | 192.168.1.42 | 80 |
| TCP | 443 | 192.168.1.42 | 443 |

Where to look (the wording changes with the model and the version):

| Router | Interface | Section |
|---|---|---|
| Freebox | <http://mafreebox.freebox.fr> | « Paramètres de la Freebox » (Freebox settings) → « Gestion des ports » (port management, advanced mode) |
| Livebox | <http://192.168.1.1> | « Paramètres avancés » (advanced settings) → « Réseau » (network) → NAT/PAT |
| SFR Box | <http://192.168.1.1> | « Réseau v4 » (IPv4 network) → NAT |
| Bbox | <https://mabbox.bytel.fr> | the « NAT/PAT » or « Redirection de ports » (port forwarding) section |

- Forward **only** 80 and 443: never port 22 (SSH) nor 8000.
- Do not use the "DMZ", which would expose the whole Pi.
- If the ports stay closed although the rules are in place, check the router's
  firewall level.

## Step 5 — Install TM Activation in Internet mode

On the Pi, in the archive's folder:

```bash
sudo ./install.sh --domain tm.mon-club.fr --email you@example.org
```

(With DuckDNS alone: `--domain monclub-tm.duckdns.org`.)

The installer installs nginx, configures it, obtains the HTTPS certificate
(renewed automatically) and starts the application. **Already installed with
`--lan`?** The same command switches over, without losing the data.

If the certificate is refused, the installation still finishes, over HTTP:
correct step 3 or 4 (see [Troubleshooting](#troubleshooting)), then run
`sudo certbot --nginx -d tm.mon-club.fr`.

## Step 6 — Check

1. From a **phone on 4G/5G, with Wi-Fi turned off**: open
   `http://tm.mon-club.fr`. The page must switch to `https://` on its own, with
   the padlock.
2. From the club's network: same address. If it does not answer although it
   works on 4G, the router does not handle "NAT loopback". In that case use
   **`http://<pi-name>.local`**, which is meant for this and limited to the
   local network.
3. Sign in as administrator (`/login`), tick **Public page online** in the
   callsign's record, then give the hunters the address
   `https://tm.mon-club.fr/tm50abc`.

## Step 7 — Security and upkeep

- **Passwords**: a long and unique administrator one; the operators password
  changed after the activation (Settings).
- **Automatic protection**: an IP is blocked for 15 min after 5 failed
  sign-ins, and robots looking for weaknesses are blocked for 1 h. The failures
  appear under **Settings → Logins**.
- **System updates**: set up automatic security updates once and for all:

  ```bash
  sudo apt install unattended-upgrades
  ```

  then run `sudo apt update && sudo apt full-upgrade` from time to time.
- **SSH** stays reachable from the local network only (no forwarding of port
  22).
- **Backups**: Settings → **Download (.sqlite)** at the end of each day of
  activation.
- **Certificate**: renewed automatically; test: `sudo certbot renew --dry-run`.

## Moving the Pi

On a new connection (another router, another place), redo steps 1, 2 and 4 on
the new router, then point the DNS record of step 3 to the new public address.
HTTPS and the data stay in place. To find out what is missing:

```bash
sudo /opt/tm-activation/install.sh --check
```

## Troubleshooting

| Symptom | Likely cause | Solution |
|---|---|---|
| `curl -4 ifconfig.me` ≠ the router's address, or router in `100.64…` | shared IPv4 | step 2 (full-stack at Free) |
| certbot: "Timeout during connect" | port 80 not forwarded, or the router's firewall | step 4 |
| certbot: "DNS problem", "NXDOMAIN" | name not visible yet or mistyped | wait, `getent hosts …` |
| certbot quotes an IPv6 address | an AAAA record is present | delete it (step 3) |
| "Welcome to nginx" page | reached by IP address instead of by name | use the domain name or `.local` |
| the domain works on 4G but not at the club | router without NAT loopback | `http://<pi-name>.local` |
| "502 Bad Gateway" | application stopped | `sudo systemctl status tm-activation`, `journalctl -u tm-activation -n 50` |
| the site stops answering after an outage | public address changed | dynamic DNS (step 3, option A) |
