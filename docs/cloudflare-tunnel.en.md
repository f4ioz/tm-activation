*Version française : [cloudflare-tunnel.md](cloudflare-tunnel.md)*

# Publishing without opening ports: Cloudflare Tunnel

The Raspberry Pi opens an **outbound** connection to Cloudflare, which publishes
the site over HTTPS. Nothing needs opening on the router.

```
 Hunters  ──https──▶  Cloudflare  ◀──outbound connection──  Raspberry Pi
                                                            cloudflared → TM Activation
```

**Prefer this when:**

- the operator shares the IPv4 address between several subscribers (no port
  forwarding possible);
- the Pi often changes location, or connects through a 4G hotspot: nothing has
  to be redone on each move;
- you would rather not touch the router's settings.

**Be aware:** all public traffic goes through Cloudflare, which sees the pages
served and terminates the HTTPS. The service is free, but it depends on that
provider. To stay in control end to end, prefer
[publishing through the router](internet-raspberry-pi.en.md).

## Requirements

1. A Cloudflare account (free) at <https://dash.cloudflare.com>.
2. **The domain must be managed by Cloudflare**: in the dashboard, Add a site,
   then replace the domain's DNS servers at the registrar (OVH, Gandi…) with
   the ones Cloudflare gives you. This is the longest step: from a few minutes
   to a few hours.
3. The Pi installed and connected to the Internet (Ethernet, Wi-Fi or 4G).

## Installation

```bash
sudo ./install.sh --tunnel --domain tm.mon-club.fr
```

Or, in guided installation, `sudo ./install.sh` then the choice
**Internet through Cloudflare Tunnel**.

The installer:

1. installs `cloudflared` (the official Cloudflare package matching the Pi's
   processor);
2. shows an **authorisation link**: open it on a device signed in to your
   Cloudflare account and choose the domain. The Pi waits;
3. creates the tunnel (default name: `tm-` followed by the domain, changeable
   with `--tunnel-name`) and its `/etc/cloudflared/config.yml` file, which
   forwards the domain to the local application;
4. creates the DNS record at Cloudflare (the domain points to the tunnel);
5. installs and starts the `cloudflared` service, which restarts with the Pi.

The application also listens on the local network: operators on site can use
`http://<pi-name>.local:8000`.

## Checking

```bash
sudo /opt/tm-activation/install.sh --check
```

The diagnostic checks the `cloudflared` service, the tunnel configuration, name
resolution and the HTTPS certificate. Then test from a phone on 4G, with Wi-Fi
turned off: `https://tm.mon-club.fr`.

## Moving the Pi

Nothing to redo: the tunnel reconnects on its own from any connection (another
router, a hotspot, 4G). Check with `--check`.

## Troubleshooting

| Symptom | Likely cause | Solution |
|---|---|---|
| "Cannot determine default origin certificate path" | Cloudflare authorisation not done | run `sudo cloudflared tunnel login` again |
| Error 1033 on the site | the tunnel is not running | `sudo systemctl status cloudflared`, then `journalctl -u cloudflared -n 50` |
| Error 502 / 503 | the application is not responding | `sudo systemctl status tm-activation` |
| "DNS route not created" | the name already exists in the zone | delete the old record in the Cloudflare DNS, then run again |
| The name does not respond | domain not (yet) managed by Cloudflare | check the DNS servers at the registrar |

## Going back to publishing through the router

```bash
sudo systemctl disable --now cloudflared
sudo /opt/tm-activation/install.sh --domain tm.mon-club.fr --email you@example.org
```

Remember to replace, in the Cloudflare DNS, the tunnel's record with an A record
pointing to the router's public address.
