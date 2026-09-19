# Publier TM Activation sur Internet depuis un Raspberry Pi (derrière une box)

Objectif : les **chasseurs** consultent la page de l'indicatif depuis
n'importe où (`https://tm.mon-club.fr/tm50abc`), et les **opérateurs** loguent
sur place ou à distance. Le Raspberry Pi est branché en **Ethernet** sur la box
du club ou de la maison.

```
 Chasseurs, opérateurs à distance            Opérateurs sur place
          │  https://tm.mon-club.fr            │  http://<nom-du-pi>.local
          ▼                                    │
   ┌─────────────┐  ports 80 et 443   ┌────────▼────────────────────────┐
   │     Box     │ ─────────────────▶ │ Raspberry Pi                    │
   │ (IPv4 publ.)│   redirigés         │ nginx (HTTPS) → TM Activation  │
   └─────────────┘                     └─────────────────────────────────┘
```

**Il faut :**

- un Raspberry Pi installé avec Raspberry Pi OS Lite 64 bits et SSH activé
  (README, « Installation sur Raspberry Pi », étapes 1 et 2) ;
- un accès à l'interface d'administration de la box ;
- un nom de domaine : celui du club, ou un nom gratuit (DuckDNS) ;
- environ une heure.

**Le plus simple : l'installation guidée.** Lancez `sudo ./install.sh` et
choisissez « Internet ». L'installeur pose les questions et affiche les
réglages exacts pour votre box, avec l'adresse et la MAC du Pi. Il vérifie
lui-même l'adresse publique et le DNS, puis teste le HTTPS avant de demander
le certificat. Ce document détaille les mêmes étapes, pour comprendre ou
dépanner.

Suivez les étapes **dans l'ordre** : le certificat HTTPS (étape 5) ne peut
être obtenu que si le nom de domaine et la box sont prêts.

---

## Étape 1 — Donner une adresse fixe au Pi sur le réseau local

La box doit toujours attribuer la même adresse au Pi, sinon la redirection de
ports de l'étape 4 finirait par pointer dans le vide.

1. Sur le Pi, noter son adresse et son identifiant réseau (adresse MAC) :

   ```bash
   hostname -I                                     # ex. 192.168.1.42
   cat /sys/class/net/eth0/address                 # ex. d8:3a:dd:12:34:56
   ```

2. Dans l'interface de la box, rubrique **DHCP**, créer un **bail statique**
   (ou « réservation d'adresse ») : cette adresse MAC → cette adresse IP.

## Étape 2 — Vérifier que la box a sa propre adresse IPv4 publique

Certains abonnements partagent une même adresse IPv4 entre plusieurs clients :
les ports 80 et 443 sont alors inaccessibles.

1. Sur le Pi : `curl -4 ifconfig.me` affiche l'adresse publique vue d'Internet.
2. Comparer avec l'**adresse IPv4** affichée dans l'interface de la box.
   - Même adresse : c'est bon.
   - Adresse de la box en `100.64.x.x` à `100.127.x.x`, adresse différente,
     ou « plage de ports » indiquée (Freebox) : l'adresse est **partagée**.
     - **Free** : dans l'espace abonné, demander une **« IP fixe V4
       full-stack »** (gratuit, active après redémarrage de la Freebox).
     - Autres opérateurs : demander au service client une adresse IPv4 publique
       dédiée.
3. Noter si l'adresse change (redémarrage de la box, coupure) : dans ce cas,
   l'étape 3 utilise un DNS dynamique. Dans le doute, utilisez-le.

## Étape 3 — Le nom de domaine

### Option A — Nom gratuit DuckDNS (le plus simple)

1. Sur <https://www.duckdns.org>, se connecter (compte Google, GitHub…), puis
   créer un sous-domaine, par exemple `monclub-tm`, ce qui donne
   `monclub-tm.duckdns.org`. Noter le **token** affiché en haut de la page.
2. Sur le Pi, mettre l'adresse à jour toutes les 5 minutes (remplacer
   `monclub-tm` et `VOTRE-TOKEN`) :

   ```bash
   echo '*/5 * * * * root curl -fsS "https://www.duckdns.org/update?domains=monclub-tm&token=VOTRE-TOKEN&ip=" >/dev/null' \
     | sudo tee /etc/cron.d/duckdns
   sudo chmod 600 /etc/cron.d/duckdns
   curl "https://www.duckdns.org/update?domains=monclub-tm&token=VOTRE-TOKEN&ip="   # doit afficher OK
   ```

   (Si `cron` manque : `sudo apt install cron`.)

### Option B — Le domaine du club (ex. `tm.mon-club.fr`)

Dans la zone DNS du domaine (chez le registrar, ou demander au webmestre du
club), ajouter **un** enregistrement :

| Adresse de la box | Enregistrement |
|---|---|
| fixe | `tm  A  <adresse IPv4 publique>` |
| variable | `tm  CNAME  monclub-tm.duckdns.org.` (option A en plus, pour la mise à jour) |

**Pas d'enregistrement AAAA (IPv6)** : Let's Encrypt essaierait l'IPv6, que
la box bloque en entrée, et le certificat serait refusé.

### Vérifier

```bash
getent hosts tm.mon-club.fr        # doit afficher l'adresse publique de l'étape 2
```

Un nouvel enregistrement peut mettre jusqu'à une heure à être visible.

## Étape 4 — Rediriger les ports 80 et 443 de la box vers le Pi

Dans l'interface de la box, créer deux règles de **redirection de ports**
(appelée aussi NAT, PAT ou translation de ports) :

| Protocole | Port externe | Destination (IP du Pi, étape 1) | Port interne |
|---|---|---|---|
| TCP | 80 | 192.168.1.42 | 80 |
| TCP | 443 | 192.168.1.42 | 443 |

Où chercher (les intitulés changent selon le modèle et la version) :

| Box | Interface | Rubrique |
|---|---|---|
| Freebox | <http://mafreebox.freebox.fr> | Paramètres de la Freebox → Gestion des ports (mode avancé) |
| Livebox | <http://192.168.1.1> | Paramètres avancés → Réseau → NAT/PAT |
| SFR Box | <http://192.168.1.1> | Réseau v4 → NAT |
| Bbox | <https://mabbox.bytel.fr> | rubrique NAT/PAT ou Redirection de ports |

- Ne redirigez **que** 80 et 443 : jamais le port 22 (SSH) ni le 8000.
- N'utilisez pas la « DMZ », qui exposerait tout le Pi.
- Si les ports restent fermés alors que les règles sont en place, vérifiez le
  niveau du pare-feu de la box.

## Étape 5 — Installer TM Activation en mode Internet

Sur le Pi, dans le dossier de l'archive :

```bash
sudo ./install.sh --domain tm.mon-club.fr --email vous@exemple.fr
```

(Avec DuckDNS seul : `--domain monclub-tm.duckdns.org`.)

L'installeur installe nginx, le configure, obtient le certificat HTTPS
(renouvelé automatiquement) et démarre l'application. **Déjà installé avec
`--lan` ?** La même commande fait la bascule, sans perdre les données.

Si le certificat est refusé, l'installation se termine quand même, en HTTP :
corrigez l'étape 3 ou 4 (voir [Dépannage](#dépannage)), puis lancez
`sudo certbot --nginx -d tm.mon-club.fr`.

## Étape 6 — Vérifier

1. Depuis un **téléphone en 4G/5G, Wi-Fi coupé** : ouvrir
   `http://tm.mon-club.fr`. La page doit passer d'elle-même en `https://` avec
   le cadenas.
2. Depuis le réseau du club : même adresse. Si elle ne répond pas alors
   qu'elle marche en 4G, la box ne gère pas le « NAT loopback ». Utilisez
   alors **`http://<nom-du-pi>.local`**, prévu pour cela et réservé au réseau
   local.
3. Se connecter en administrateur (`/login`), cocher **Page publique en
   ligne** dans la fiche de l'indicatif, puis communiquer aux chasseurs
   l'adresse `https://tm.mon-club.fr/tm50abc`.

## Étape 7 — Sécurité et entretien

- **Mots de passe** : administrateur long et unique ; mot de passe opérateurs
  changé après l'activation (Réglages).
- **Protection automatique** : une IP est bloquée 15 min après 5 échecs de
  connexion, et les robots qui cherchent des failles sont bloqués 1 h. Les
  échecs apparaissent dans **Réglages → Connexions**.
- **Mises à jour du système** : installer les mises à jour de sécurité
  automatiques une fois pour toutes :

  ```bash
  sudo apt install unattended-upgrades
  ```

  puis faire de temps en temps `sudo apt update && sudo apt full-upgrade`.
- **SSH** reste accessible depuis le réseau local seulement (pas de
  redirection du port 22).
- **Sauvegardes** : Réglages → **Télécharger (.sqlite)** à la fin de chaque
  journée d'activation.
- **Certificat** : renouvelé automatiquement ; test : `sudo certbot renew --dry-run`.

## Déplacer le Pi

Sur une nouvelle connexion (autre box, autre lieu), refaites les étapes 1, 2
et 4 sur la nouvelle box, puis mettez l'enregistrement DNS de l'étape 3 à la
nouvelle adresse publique. Le HTTPS et les données restent en place. Pour
savoir ce qui manque :

```bash
sudo /opt/tm-activation/install.sh --check
```

## Dépannage

| Symptôme | Cause probable | Solution |
|---|---|---|
| `curl -4 ifconfig.me` ≠ adresse de la box, ou box en `100.64…` | IPv4 partagée | étape 2 (full-stack chez Free) |
| certbot : « Timeout during connect » | port 80 non redirigé, ou pare-feu de la box | étape 4 |
| certbot : « DNS problem », « NXDOMAIN » | nom pas encore visible ou mal saisi | attendre, `getent hosts …` |
| certbot cite une adresse IPv6 | enregistrement AAAA présent | le supprimer (étape 3) |
| page « Welcome to nginx » | accès par l'adresse IP au lieu du nom | utiliser le nom de domaine ou `.local` |
| le domaine marche en 4G mais pas au club | box sans NAT loopback | `http://<nom-du-pi>.local` |
| « 502 Bad Gateway » | application arrêtée | `sudo systemctl status tm-activation`, `journalctl -u tm-activation -n 50` |
| le site ne répond plus après une coupure | adresse publique changée | DNS dynamique (étape 3, option A) |
