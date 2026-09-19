#!/usr/bin/env bash
# TM Activation — installation guidée et mise à jour.
#
#   sudo ./install.sh            installation guidée : questions, récapitulatif, puis
#                                (mode Internet) réglage pas à pas de la box et du DNS
#   sudo ./install.sh --lan      réseau local : http://<nom>.local, sans nginx
#   sudo ./install.sh --domain tm.mon-club.fr --email moi@example.org --box freebox
#                                Internet : nginx + HTTPS Let's Encrypt
#   sudo ./install.sh --tunnel --domain tm.mon-club.fr
#                                Internet par Cloudflare Tunnel : aucun port
#                                ouvert sur la box (marche en 4G, IPv4 partagée)
#   sudo ./install.sh --check    diagnostic : service, réseau, DNS, certificat
#                                (après un déplacement du Pi, une panne…)
#   ./install.sh --no-systemd --dir ~/tm-activation     sans root, sans service
#
# Relancé depuis une version plus récente, le script fait la MISE À JOUR avec
# les réglages mémorisés dans install.env. Il ne touche JAMAIS à config.yml ni
# au dossier var/ (base, mots de passe, sauvegardes) ; la base est copiée dans
# var/backups/ avant.
#
# Le script ne fait rien quand il est sourcé (tests) : tout part de main().
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VERSION="$(cat "$SRC_DIR/VERSION")"
MARKER=".tm-activation"
STATE="install.env"
CODE_ITEMS=(app templates static tests deploy docs requirements.txt requirements-dev.txt pytest.ini
            VERSION BUILD_INFO README.md LICENSE config.yml.example install.sh)
BOXES=(freebox livebox sfr bbox autre)

RE_CALL='^[A-Z0-9]{3,10}(/[A-Z0-9]{1,4})?$'
RE_GRID='^[A-R]{2}[0-9]{2}([A-X]{2}([0-9]{2})?)?$'
RE_DOMAIN='^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$'
RE_EMAIL='^[^@[:space:]]+@[^@[:space:]]+\.[^@[:space:]]+$'

# Options (vide = non précisée : reprise d'install.env, sinon défaut du mode).
CLI_DIR="" CLI_USER="" CLI_SERVICE="" CLI_HOST="" CLI_PORT="" CLI_MODE="" CLI_DOMAIN="" CLI_BOX="" CLI_EMAIL=""
CLI_TUNNEL_NAME=""
USE_SYSTEMD="auto" INTERACTIVE="auto" CHECK=0
PYTHON="${PYTHON:-python3}"

# État
MODE="" DOMAIN="" BOX="" EMAIL="" HOST="" PORT="" SVC_USER="" SERVICE="" DIR="" TUNNEL_NAME=""
UPGRADE=0 IS_ROOT=0 FIRST_CONFIG=0 ASKED=0 OLD_PORT="" TUNNEL_READY=1
PREV_MODE="" PREV_HOST="" PREV_PORT="" PREV_SVC_USER="" PREV_SERVICE="" PREV_DOMAIN="" PREV_BOX="" PREV_EMAIL=""
PREV_TUNNEL_NAME=""
CF_CONF_DIR="${CF_CONF_DIR:-/etc/cloudflared}"     # configuration du tunnel (surchargeable : tests)
CF_LOGIN_DIR="${CF_LOGIN_DIR:-$HOME/.cloudflared}" # cert.pem et identifiants créés par « cloudflared tunnel login »
VPY="" PORT_SUFFIX="" GENERATED_ADMIN_PW="" SHARED_IP=0
NC_LOCAL_IP="" NC_IFACE="" NC_MAC="" NC_PUBLIC_IP="" NC_PUBLIC_KIND="" NC_DNS_A="" NC_DNS_AAAA="" NC_DNS_VIA=""
NC_CERT="" NC_CERT_DAYS="" NC_CERT_ERROR=""
CALLSIGN="" LABEL="" GRID="" PUBLIC="0" CLUB_CALLSIGN="" CLUB_NAME="" CLUB_CITY="" CLUB_WEBSITE=""
BASE_URL="" OPERATORS="" ADMIN_PASSWORD="" OPERATOR_PASSWORD="" QRZ_USER="" QRZ_PASSWORD="" TRUSTED_PROXIES=""
MDNS_NAME="$(hostname 2>/dev/null | tr '[:upper:]' '[:lower:]')"   # adresse <nom>.local

usage() {
  cat <<EOF
TM Activation $VERSION — installation guidée / mise à jour

Usage : sudo ./install.sh [options]

Sans option : installation guidée (questions, puis récapitulatif avant toute modification).

Mode :
  --lan               réseau local (Raspberry Pi, radio-club, portable) :
                      écoute sur le port 80, adresse http://<nom-machine>.local
  --domain DOMAINE    Internet : nginx installé et configuré pour DOMAINE
  --email EMAIL       avec --domain : certificat HTTPS Let's Encrypt
  --box NOM           box de la connexion (${BOXES[*]}) : aide adaptée
  --tunnel            Internet par Cloudflare Tunnel (avec --domain) : aucun port
                      ouvert sur la box, HTTPS assuré par Cloudflare
  --tunnel-name NOM   nom du tunnel Cloudflare (défaut : d'après le domaine)
  --manual            avancé : écoute sur 127.0.0.1:8000, reverse proxy à votre charge

Autres options :
  --check             diagnostic de l'installation (rien n'est modifié)
  --dir DIR           dossier d'installation          (défaut : /opt/tm-activation)
  --user USER         compte système du service       (défaut : tmact)
  --service NAME      nom du service systemd          (défaut : tm-activation)
  --host IP           adresse d'écoute                (défaut selon le mode)
  --port PORT         port d'écoute                   (défaut selon le mode)
  --python BIN        interpréteur Python ≥ 3.11      (défaut : python3)
  --no-systemd        pas de service (installation sans root, essai, développement)
  --non-interactive   aucune question : valeurs lues dans les variables TM_* (voir README)
  -h, --help          cette aide
EOF
}

# ── Affichage et saisie ─────────────────────────────────────────────────────

die()   { echo "ERREUR : $*" >&2; exit 1; }
warn()  { echo "  ATTENTION : $*" >&2; }
info()  { echo "==> $*"; }
title() { echo; echo "── $* ──"; }
line() {  # libellé aligné sur 24 caractères (accents comptés comme une lettre)
  local LC_ALL=C.UTF-8 pad
  pad=$((24 - ${#1}))
  if (( pad < 1 )); then pad=1; fi
  printf '  %s%*s %s\n' "$1" "$pad" "" "$2"
}

ask() {  # ask VAR "Question" [défaut]
  local __var=$1 __q=$2 __def=${3:-} __ans
  read -r -p "  $__q${__def:+ [$__def]} : " __ans || true
  printf -v "$__var" '%s' "${__ans:-$__def}"
}
ask_secret() {  # ask_secret VAR "Question"
  local __var=$1 __q=$2 __ans
  read -r -s -p "  $__q : " __ans || true; echo
  printf -v "$__var" '%s' "$__ans"
}
ask_password() {  # ask_password VAR "Question" : saisi deux fois, vide autorisé
  local __var=$1 __q=$2 __a __b
  while :; do
    read -r -s -p "  $__q : " __a || true; echo
    if [[ -z $__a ]]; then printf -v "$__var" '%s' ""; return 0; fi
    read -r -s -p "  Confirmer : " __b || true; echo
    if [[ $__a == "$__b" ]]; then printf -v "$__var" '%s' "$__a"; return 0; fi
    echo "  Les deux saisies diffèrent, recommencez."
  done
}
confirm() {  # confirm "Question" [o|n] → 0 si oui
  local __ans __def=${2:-o} __hint="O/n"
  if [[ $__def == n ]]; then __hint="o/N"; fi
  read -r -p "  $1 [$__hint] : " __ans || true
  __ans="${__ans:-$__def}"
  [[ ${__ans,,} == o* || ${__ans,,} == y* ]]
}
pause() {  # pause ["message"]
  # Pas d'apostrophe dans une expansion ${…} entre guillemets : bash la lirait
  # comme un début de citation.
  local __ans __msg="Entrée quand c'est fait…"
  if [[ $# -gt 0 ]]; then __msg=$1; fi
  read -r -p "  $__msg " __ans || true
}
menu() {  # menu VAR "Question" défaut "choix 1" "choix 2"… → VAR = numéro choisi
  local __var=$1 __q=$2 __def=$3 __i=1 __opt __ans
  shift 3
  for __opt in "$@"; do echo "    $__i) $__opt"; __i=$((__i + 1)); done
  while :; do
    read -r -p "  $__q [$__def] : " __ans || true
    __ans="${__ans:-$__def}"
    if [[ $__ans =~ ^[0-9]+$ ]] && (( __ans >= 1 && __ans <= $# )); then
      printf -v "$__var" '%s' "$__ans"; return 0
    fi
    echo "  Choix invalide."
  done
}
in_list() { local x=$1 y; shift; for y in "$@"; do if [[ $x == "$y" ]]; then return 0; fi; done; return 1; }

# ── Contrôles réseau (deploy/netcheck.py) ───────────────────────────────────

netcheck() {  # netcheck COMMANDE [ARG] → variables NC_<CLE>
  local key value
  case "$1" in
    local) NC_LOCAL_IP="" NC_IFACE="" NC_MAC="" ;;
    public) NC_PUBLIC_IP="" NC_PUBLIC_KIND="" ;;
    dns) NC_DNS_A="" NC_DNS_AAAA="" NC_DNS_VIA="" ;;
    cert) NC_CERT="" NC_CERT_DAYS="" NC_CERT_ERROR="" ;;
  esac
  while IFS='=' read -r key value; do
    case "$key" in
      LOCAL_IP|IFACE|MAC|PUBLIC_IP|PUBLIC_KIND|DNS_A|DNS_AAAA|DNS_VIA|CERT|CERT_DAYS|CERT_ERROR)
        printf -v "NC_$key" '%s' "$value" ;;
    esac
  done < <("$PYTHON" "$SRC_DIR/deploy/netcheck.py" "$@" 2>/dev/null || true)
}

dns_ok() {  # le domaine pointe vers l'adresse publique, sans AAAA
  [[ -n $NC_DNS_A && -z $NC_DNS_AAAA ]] || return 1
  [[ -z $NC_PUBLIC_IP || ",$NC_DNS_A," == *",$NC_PUBLIC_IP,"* ]]
}
dns_explain() {
  if [[ -z $NC_DNS_A ]]; then
    echo "  $DOMAIN n'existe pas encore dans le DNS (création récente ? faute de frappe ?)."
  elif [[ -n $NC_PUBLIC_IP && ",$NC_DNS_A," != *",$NC_PUBLIC_IP,"* ]]; then
    echo "  $DOMAIN pointe vers $NC_DNS_A au lieu de $NC_PUBLIC_IP."
  fi
  if [[ -n $NC_DNS_AAAA ]]; then
    echo "  Enregistrement AAAA (IPv6) présent : $NC_DNS_AAAA — à supprimer."
  fi
  return 0
}

# ── Aide propre à chaque box (intitulés variables selon les versions) ───────

box_label() {
  case "$BOX" in
    freebox) echo "Freebox" ;; livebox) echo "Livebox" ;; sfr) echo "SFR Box" ;;
    bbox) echo "Bbox" ;; *) echo "box" ;;
  esac
}
box_url() {
  case "$BOX" in
    freebox) echo "http://mafreebox.freebox.fr" ;;
    livebox|sfr) echo "http://192.168.1.1" ;;
    bbox) echo "https://mabbox.bytel.fr" ;;
    *) echo "adresse indiquée sous la box" ;;
  esac
}
help_dhcp() {
  local mac="${NC_MAC:-<adresse MAC du Pi>}" ip="${NC_LOCAL_IP:-<adresse du Pi>}"
  case "$BOX" in
    freebox) cat <<EOF
  Freebox OS → Paramètres de la Freebox → mode avancé → DHCP → onglet
  « Baux statiques » → « Ajouter un bail DHCP statique » :
      Adresse MAC : $mac
      Adresse IP  : $ip
      Commentaire : TM Activation
EOF
    ;;
    livebox) echo "  $(box_url) → Paramètres avancés → Réseau → DHCP → bail statique : $mac → $ip" ;;
    sfr) echo "  $(box_url) → Réseau v4 → DHCP → attribution statique : $mac → $ip" ;;
    bbox) echo "  $(box_url) → rubrique DHCP → adresse réservée : $mac → $ip" ;;
    *) echo "  Interface de la box → DHCP → bail statique (réservation) : $mac → $ip" ;;
  esac
}
help_ipv4() {
  case "$BOX" in
    freebox) cat <<EOF
  Free partage parfois une même adresse IPv4 entre plusieurs abonnés : les
  ports 80 et 443 sont alors inaccessibles. Pour vérifier : espace abonné Free
  → Ma Freebox → « Demander une adresse IP fixe V4 full-stack ». La page
  indique si c'est déjà le cas ; sinon, faites la demande (gratuite), attendez
  la confirmation, puis redémarrez la Freebox.
EOF
    ;;
    *) cat <<EOF
  Comparez avec l'adresse IPv4 affichée dans l'interface de la $(box_label) :
  elle doit être identique. Sinon, l'adresse est partagée entre plusieurs
  abonnés : demandez à l'opérateur une adresse IPv4 publique dédiée.
EOF
    ;;
  esac
}
help_ports() {
  local ip="${NC_LOCAL_IP:-<adresse du Pi>}"
  case "$BOX" in
    freebox) cat <<EOF
  Freebox OS → Paramètres de la Freebox → mode avancé → Gestion des ports
  → « Ajouter une redirection », deux fois :

      IP de destination   Protocole   Port de début   Port de fin   Port de destination
      $(printf '%-19s' "$ip") TCP         80              80            80
      $(printf '%-19s' "$ip") TCP         443             443           443

  Laissez « IP source » sur « Toutes ». Si la Freebox refuse le port 443
  (déjà pris par l'accès distant à Freebox OS) : Paramètres de la Freebox →
  Accès à distance → changez le port HTTPS de Freebox OS, puis recommencez.
EOF
    ;;
    livebox) echo "  $(box_url) → Paramètres avancés → Réseau → NAT/PAT : TCP 80 → $ip:80 et TCP 443 → $ip:443" ;;
    sfr) echo "  $(box_url) → Réseau v4 → NAT : TCP 80 → $ip:80 et TCP 443 → $ip:443" ;;
    bbox) echo "  $(box_url) → NAT/PAT (redirection de ports) : TCP 80 → $ip:80 et TCP 443 → $ip:443" ;;
    *) echo "  Interface de la box → redirection de ports (NAT/PAT) : TCP 80 → $ip:80 et TCP 443 → $ip:443" ;;
  esac
  echo "  Ne redirigez aucun autre port (surtout pas 22/SSH) et n'utilisez pas la DMZ."
}

# ── Contexte ────────────────────────────────────────────────────────────────

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --lan) CLI_MODE="lan"; shift ;;
      --domain)
        CLI_DOMAIN="${2:?--domain demande un nom}"
        if [[ -z $CLI_MODE ]]; then CLI_MODE="internet"; fi   # --tunnel garde la main
        shift 2 ;;
      --tunnel) CLI_MODE="tunnel"; shift ;;
      --tunnel-name) CLI_TUNNEL_NAME="${2:?--tunnel-name demande un nom}"; shift 2 ;;
      --email) CLI_EMAIL="${2:?--email demande une adresse}"; shift 2 ;;
      --box) CLI_BOX="${2:?--box demande un nom}"; shift 2 ;;
      --manual) CLI_MODE="manual"; shift ;;
      --check) CHECK=1; shift ;;
      --dir) CLI_DIR="${2:?}"; shift 2 ;;
      --user) CLI_USER="${2:?}"; shift 2 ;;
      --service) CLI_SERVICE="${2:?}"; shift 2 ;;
      --host) CLI_HOST="${2:?}"; shift 2 ;;
      --port) CLI_PORT="${2:?}"; shift 2 ;;
      --python) PYTHON="${2:?}"; shift 2 ;;
      --no-systemd) USE_SYSTEMD="no"; shift ;;
      --non-interactive) INTERACTIVE="no"; shift ;;
      -h|--help) usage; exit 0 ;;
      *) usage >&2; die "option inconnue : $1" ;;
    esac
  done
  CLI_DOMAIN="${CLI_DOMAIN,,}"
  CLI_BOX="${CLI_BOX,,}"
  if [[ -n $CLI_BOX ]] && ! in_list "$CLI_BOX" "${BOXES[@]}"; then
    die "box inconnue : $CLI_BOX (au choix : ${BOXES[*]})"
  fi
}

resolve_context() {
  local key value
  if [[ $EUID -eq 0 ]]; then IS_ROOT=1; fi
  if [[ $USE_SYSTEMD == auto ]]; then
    if [[ $IS_ROOT == 1 && -d /run/systemd/system ]] && command -v systemctl >/dev/null; then
      USE_SYSTEMD="yes"
    else
      USE_SYSTEMD="no"
    fi
  fi
  if [[ $INTERACTIVE == auto ]]; then
    if [[ -t 0 ]]; then INTERACTIVE="yes"; else INTERACTIVE="no"; fi
  fi

  DIR="${CLI_DIR:-/opt/tm-activation}"
  DIR="${DIR%/}"
  [[ $DIR == /* ]] || DIR="$(pwd)/$DIR"
  [[ $DIR != "" && $DIR != "/" ]] || die "dossier d'installation invalide : '$DIR'"
  case "$DIR" in /bin|/boot|/dev|/etc|/home|/lib|/lib64|/opt|/proc|/root|/run|/sbin|/srv|/sys|/tmp|/usr|/var)
    die "refus d'installer directement dans $DIR (choisir un sous-dossier, ex. /opt/tm-activation)" ;;
  esac
  if [[ -f $DIR/$MARKER ]]; then UPGRADE=1; fi
  # Garde-fou : ne jamais écraser un dossier qui n'est pas une installation TM Activation.
  if [[ $CHECK == 0 && $UPGRADE == 0 && -d $DIR && -n "$(ls -A "$DIR" 2>/dev/null)" ]]; then
    die "$DIR existe, n'est pas vide et n'est pas une installation TM Activation"
  fi

  # Réglages de l'installation précédente (clé=valeur, lus sans exécution).
  if [[ -f $DIR/$STATE ]]; then
    while IFS='=' read -r key value; do
      case "$key" in
        MODE|HOST|PORT|SVC_USER|SERVICE|DOMAIN|BOX|EMAIL|TUNNEL_NAME) printf -v "PREV_$key" '%s' "$value" ;;
      esac
    done < "$DIR/$STATE"
  fi
  OLD_PORT="$PREV_PORT"   # port réellement occupé par le service en place
  # Changement de mode demandé : l'adresse et le port précédents ne valent plus.
  if [[ -n $CLI_MODE && $CLI_MODE != "$PREV_MODE" ]]; then PREV_HOST=""; PREV_PORT=""; fi
  MODE="${CLI_MODE:-$PREV_MODE}"
  DOMAIN="${CLI_DOMAIN:-$PREV_DOMAIN}"
  BOX="${CLI_BOX:-$PREV_BOX}"
  EMAIL="${CLI_EMAIL:-$PREV_EMAIL}"
  TUNNEL_NAME="${CLI_TUNNEL_NAME:-$PREV_TUNNEL_NAME}"
  return 0
}

resolve_ports() {
  local def_host def_port
  case "$MODE" in
    lan) def_host="0.0.0.0"; def_port="8000"; if [[ $USE_SYSTEMD == yes ]]; then def_port="80"; fi ;;
    # Tunnel : cloudflared se connecte en 127.0.0.1, l'écoute sur toutes les
    # interfaces garde l'accès direct depuis le réseau local.
    tunnel) def_host="0.0.0.0"; def_port="8000" ;;
    internet|manual) def_host="127.0.0.1"; def_port="8000" ;;
    *) die "mode inconnu : $MODE" ;;
  esac
  HOST="${CLI_HOST:-${PREV_HOST:-$def_host}}"
  PORT="${CLI_PORT:-${PREV_PORT:-$def_port}}"
  SVC_USER="${CLI_USER:-${PREV_SVC_USER:-tmact}}"
  SERVICE="${CLI_SERVICE:-${PREV_SERVICE:-tm-activation}}"
  BOX="${BOX:-autre}"
  PORT_SUFFIX=""
  if [[ $PORT != 80 ]]; then PORT_SUFFIX=":$PORT"; fi
}

validate() {
  if ! [[ $PORT =~ ^[0-9]+$ ]] || (( PORT < 1 || PORT > 65535 )); then die "port invalide : $PORT"; fi
  if (( PORT < 1024 )) && [[ $USE_SYSTEMD != yes ]]; then
    die "port $PORT (< 1024) : seulement avec le service systemd (root) ; sinon --port 8000"
  fi
  [[ $SERVICE =~ ^[a-zA-Z0-9_.-]+$ ]] || die "nom de service invalide : $SERVICE"
  [[ $SVC_USER =~ ^[a-z_][a-z0-9_-]*$ ]] || die "nom de compte invalide : $SVC_USER"
  if [[ $MODE == internet || $MODE == tunnel ]]; then
    [[ $DOMAIN =~ $RE_DOMAIN ]] || die "nom de domaine invalide : '$DOMAIN'"
  fi
  if [[ $MODE == internet ]]; then
    [[ $USE_SYSTEMD == yes ]] || die "le mode Internet (nginx) demande root et systemd"
  fi
  if [[ $MODE == tunnel ]]; then
    [[ $USE_SYSTEMD == yes ]] || die "le mode Cloudflare Tunnel demande root et systemd"
    [[ $TUNNEL_NAME == "" || $TUNNEL_NAME =~ ^[a-zA-Z0-9_-]+$ ]] || die "nom de tunnel invalide : $TUNNEL_NAME"
  fi
  if [[ -n $EMAIL ]]; then
    [[ $EMAIL =~ $RE_EMAIL ]] || die "e-mail invalide : $EMAIL"
    [[ $MODE == internet ]] || die "--email ne sert qu'avec --domain"
  fi
  return 0
}

# ── Questions (rien n'est modifié avant le récapitulatif) ──────────────────

welcome() {
  cat <<EOF

  TM Activation $VERSION — installation guidée

  Gestion d'indicatifs spéciaux : planning des opérateurs, log QSO, ADIF et
  page publique pour les chasseurs.

  Quelques questions d'abord : rien n'est modifié avant le récapitulatif.
  Entrée = valeur proposée entre crochets. Ctrl+C pour abandonner.
EOF
}

ask_mode() {
  local choice
  title "Utilisation"
  echo "  Comment TM Activation sera-t-il utilisé ?"
  menu choice "Choix" 2 \
    "Réseau local : Pi ou PC au radio-club, en portable… (http://$MDNS_NAME.local)" \
    "Internet par votre box : nom de domaine, ports 80 et 443 ouverts, HTTPS Let's Encrypt" \
    "Internet par Cloudflare Tunnel : aucun port ouvert, marche aussi en 4G ou IPv4 partagée" \
    "Avancé : écoute sur 127.0.0.1:8000, reverse proxy à votre charge"
  case "$choice" in 1) MODE="lan" ;; 2) MODE="internet" ;; 3) MODE="tunnel" ;; *) MODE="manual" ;; esac
  if [[ $MODE == internet || $MODE == tunnel ]]; then
    echo
    echo "  Le nom de domaine doit vous appartenir : celui du club, ou un nom acheté"
    echo "  chez un registrar (OVH, Gandi, Ionos…). Exemple : tm.mon-club.fr"
    while :; do
      ask DOMAIN "Nom de domaine" "$DOMAIN"
      DOMAIN="${DOMAIN,,}"
      if [[ $DOMAIN =~ $RE_DOMAIN ]]; then break; fi
      echo "  Nom de domaine invalide."
    done
  fi
  if [[ $MODE == internet ]]; then ask_box_email; fi
  ASKED=1
}

ask_box_email() {
  local choice def=1 i
  if [[ -z $CLI_BOX ]]; then
    for i in "${!BOXES[@]}"; do
      if [[ ${BOXES[$i]} == "$BOX" ]]; then def=$((i + 1)); fi
    done
    echo
    echo "  Quelle box relie le Pi à Internet ? (pour afficher les bons réglages)"
    menu choice "Choix" "$def" "Freebox" "Livebox (Orange)" "SFR Box" "Bbox (Bouygues)" "Autre"
    BOX="${BOXES[$((choice - 1))]}"
  fi
  if [[ -z $EMAIL ]]; then
    echo
    echo "  Adresse e-mail pour le certificat HTTPS Let's Encrypt (avis d'expiration)."
    while :; do
      ask EMAIL "E-mail (vide = HTTPS plus tard)" ""
      if [[ -z $EMAIL || $EMAIL =~ $RE_EMAIL ]]; then break; fi
      echo "  E-mail invalide."
    done
  fi
  ASKED=1
}

default_url() {
  case "$MODE" in
    lan) echo "http://$MDNS_NAME.local$PORT_SUFFIX" ;;
    internet|tunnel) echo "https://$DOMAIN" ;;
    *) echo "" ;;
  esac
}

collect_config() {
  CALLSIGN="${TM_CALLSIGN:-}"; LABEL="${TM_LABEL:-}"; GRID="${TM_GRID:-}"; PUBLIC="${TM_PUBLIC:-0}"
  CLUB_CALLSIGN="${TM_CLUB_CALLSIGN:-}"; CLUB_NAME="${TM_CLUB_NAME:-}"
  CLUB_CITY="${TM_CLUB_CITY:-}"; CLUB_WEBSITE="${TM_CLUB_WEBSITE:-}"
  BASE_URL="${TM_BASE_URL:-$(default_url)}"; OPERATORS="${TM_OPERATORS:-}"
  ADMIN_PASSWORD="${TM_ADMIN_PASSWORD:-}"; OPERATOR_PASSWORD="${TM_OPERATOR_PASSWORD:-}"
  QRZ_USER="${TM_QRZ_USER:-}"; QRZ_PASSWORD="${TM_QRZ_PASSWORD:-}"
  TRUSTED_PROXIES="${TM_TRUSTED_PROXIES:-}"

  if [[ $INTERACTIVE == yes ]]; then
    title "Station"
    while :; do
      ask CALLSIGN "Indicatif spécial à activer (ex. TM50ABC)" "$CALLSIGN"
      CALLSIGN="${CALLSIGN^^}"
      if [[ $CALLSIGN =~ $RE_CALL && $CALLSIGN =~ [0-9] ]]; then break; fi
      echo "  Indicatif invalide."
    done
    ask LABEL "Libellé de l'activation (ex. 50 ans du radio-club)" "$LABEL"
    while :; do
      ask GRID "Locator de la station (4, 6 ou 8 caractères, facultatif)" "$GRID"
      GRID="${GRID^^}"
      if [[ -z $GRID || $GRID =~ $RE_GRID ]]; then break; fi
      echo "  Locator invalide."
    done

    title "Radio-club (facultatif, affiché sur les pages)"
    ask CLUB_NAME "Nom du radio-club" "$CLUB_NAME"
    ask CLUB_CALLSIGN "Indicatif du radio-club" "$CLUB_CALLSIGN"
    ask CLUB_CITY "Ville" "$CLUB_CITY"
    ask CLUB_WEBSITE "Site web du club (https://…)" "$CLUB_WEBSITE"

    title "Accès"
    ask BASE_URL "Adresse de ce site" "$BASE_URL"
    ask OPERATORS "Opérateurs de départ, séparés par des virgules (facultatif)" "$OPERATORS"
    echo "  Administrateur : Réglages du site (indicatifs, mot de passe opérateurs, sauvegardes)."
    ask_password ADMIN_PASSWORD "Mot de passe administrateur (vide = généré)"
    echo "  Opérateurs : un mot de passe commun ; chacun se connecte avec son indicatif."
    ask_password OPERATOR_PASSWORD "Mot de passe opérateurs (vide = plus tard, dans les Réglages)"

    title "QRZ.com (facultatif)"
    echo "  Avec un abonnement XML, nom, locator et pays des stations contactées sont complétés."
    ask QRZ_USER "Identifiant QRZ (vide = sans QRZ)" "$QRZ_USER"
    if [[ -n $QRZ_USER ]]; then ask_secret QRZ_PASSWORD "Mot de passe QRZ"; fi
    if [[ $MODE == manual && $HOST != 127.0.0.1 && $HOST != localhost && -z $TRUSTED_PROXIES ]]; then
      ask TRUSTED_PROXIES "IP du reverse proxy nginx (autre machine)" ""
    fi
  fi

  CALLSIGN="${CALLSIGN^^}"; GRID="${GRID^^}"; CLUB_CALLSIGN="${CLUB_CALLSIGN^^}"
  [[ $CALLSIGN =~ $RE_CALL && $CALLSIGN =~ [0-9] ]] || die "indicatif spécial invalide ou absent (TM_CALLSIGN)"
  [[ -z $GRID || $GRID =~ $RE_GRID ]] || die "locator invalide : $GRID"
  if [[ -n $TRUSTED_PROXIES ]]; then TRUSTED_PROXIES="127.0.0.1 ::1 $TRUSTED_PROXIES"; fi
  return 0
}

mode_label() {
  case "$MODE" in
    lan) echo "réseau local — http://$MDNS_NAME.local$PORT_SUFFIX/" ;;
    internet) echo "Internet par la box — https://$DOMAIN/" ;;
    tunnel) echo "Internet par Cloudflare Tunnel — https://$DOMAIN/" ;;
    *) echo "avancé — http://$HOST:$PORT/ (reverse proxy à votre charge)" ;;
  esac
}

recap() {
  local club="${CLUB_NAME:-—}"
  if [[ -n $CLUB_CALLSIGN ]]; then club="$club ($CLUB_CALLSIGN)"; fi
  title "Récapitulatif"
  line "Utilisation" "$(mode_label)"
  case "$MODE" in
    internet)
      line "Box" "$(box_label)"
      if [[ -n $EMAIL ]]; then line "Certificat HTTPS" "$EMAIL"; else line "Certificat HTTPS" "plus tard (pas d'e-mail)"; fi ;;
    tunnel)
      line "Accès Internet" "Cloudflare Tunnel (aucun port ouvert sur la box)"
      line "Nom du tunnel" "$(tunnel_name)" ;;
  esac
  if [[ $FIRST_CONFIG == 1 ]]; then
    line "Indicatif spécial" "$CALLSIGN${LABEL:+ — $LABEL}"
    line "Locator" "${GRID:-—}"
    line "Radio-club" "$club"
    line "Opérateurs de départ" "${OPERATORS:-—}"
    if [[ -n $ADMIN_PASSWORD ]]; then line "Mot de passe admin" "saisi"; else line "Mot de passe admin" "généré (affiché à la fin)"; fi
    if [[ -n $OPERATOR_PASSWORD ]]; then line "Mot de passe opérateurs" "saisi"; else line "Mot de passe opérateurs" "à définir dans les Réglages"; fi
    line "QRZ.com" "${QRZ_USER:-non}"
  fi
  line "Dossier" "$DIR (service $SERVICE)"
  echo
  if ! confirm "Lancer l'installation ?" o; then
    echo "  Rien n'a été modifié."
    exit 0
  fi
}

# ── Installation ────────────────────────────────────────────────────────────

install_packages() {
  local pkgs=()
  # Passage réseau local → Internet : le port 80 tenu par le service revient à
  # nginx, qui démarre dès son installation.
  if [[ $USE_SYSTEMD == yes && $MODE == internet && $PREV_MODE == lan ]] \
     && systemctl is-active --quiet "$SERVICE"; then
    info "Arrêt du service le temps de confier le port 80 à nginx"
    systemctl stop "$SERVICE"
  fi
  if ! command -v "$PYTHON" >/dev/null || ! "$PYTHON" -c 'import venv, ensurepip' 2>/dev/null; then
    pkgs+=(python3 python3-venv)
  fi
  if [[ $MODE == internet ]]; then
    if ! command -v nginx >/dev/null; then pkgs+=(nginx); fi
    if [[ -n $EMAIL ]] && ! command -v certbot >/dev/null; then pkgs+=(certbot python3-certbot-nginx); fi
  fi
  # Adresse <nom>.local (mDNS) : présent d'origine sur Raspberry Pi OS.
  if [[ $USE_SYSTEMD == yes && $MODE != manual ]] && ! command -v avahi-daemon >/dev/null; then
    pkgs+=(avahi-daemon)
  fi
  if (( ${#pkgs[@]} )); then
    if [[ $IS_ROOT == 1 ]] && command -v apt-get >/dev/null; then
      info "Installation des paquets système : ${pkgs[*]}"
      export DEBIAN_FRONTEND=noninteractive
      apt-get update -qq
      apt-get install -y -qq --no-install-recommends "${pkgs[@]}" >/dev/null
    else
      die "paquets manquants : ${pkgs[*]} (sudo apt install ${pkgs[*]})"
    fi
  fi
}

check_python() {
  command -v "$PYTHON" >/dev/null || die "$PYTHON introuvable"
  "$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' \
    || die "Python ≥ 3.11 requis ($("$PYTHON" -V 2>&1)) : Debian 12 / Raspberry Pi OS Bookworm ou plus récent"
  "$PYTHON" -c 'import venv, ensurepip' 2>/dev/null || die "module venv absent (apt install python3-venv)"
}

create_account() {
  if [[ $USE_SYSTEMD == yes ]] && ! id -u "$SVC_USER" >/dev/null 2>&1; then
    info "Création du compte système $SVC_USER"
    useradd --system --home-dir "$DIR" --no-create-home --shell /usr/sbin/nologin "$SVC_USER"
  fi
}

install_code() {
  local item stamp
  mkdir -p "$DIR/var"
  if [[ $UPGRADE == 1 && -f $DIR/var/activation.sqlite ]]; then
    stamp="$(date -u +%Y%m%d-%H%M%S)"
    mkdir -p "$DIR/var/backups"
    "$PYTHON" - "$DIR/var/activation.sqlite" "$DIR/var/backups/preupgrade-$stamp.sqlite" <<'PY'
import sqlite3, sys
src, dst = sqlite3.connect(sys.argv[1]), sqlite3.connect(sys.argv[2])
src.backup(dst)  # copie cohérente même si le service écrit en même temps
dst.close(); src.close()
PY
    info "Base sauvegardée : var/backups/preupgrade-$stamp.sqlite"
  fi
  if [[ "$SRC_DIR" != "$DIR" ]]; then
    info "Copie du code"
    for item in "${CODE_ITEMS[@]}"; do
      if [[ -e $SRC_DIR/$item ]]; then
        rm -rf "${DIR:?}/$item"
        cp -R "$SRC_DIR/$item" "$DIR/$item"
      fi
    done
  fi
  echo "$VERSION" > "$DIR/$MARKER"
  chmod 755 "$DIR/install.sh"
  printf 'MODE=%s\nHOST=%s\nPORT=%s\nSVC_USER=%s\nSERVICE=%s\nDOMAIN=%s\nBOX=%s\nEMAIL=%s\nTUNNEL_NAME=%s\n' \
    "$MODE" "$HOST" "$PORT" "$SVC_USER" "$SERVICE" "$DOMAIN" "$BOX" "$EMAIL" "$TUNNEL_NAME" > "$DIR/$STATE"
}

install_venv() {
  # Un venv créé par un Python disparu (mise à niveau de l'OS) est recréé.
  if [[ -e $DIR/.venv ]] && ! "$DIR/.venv/bin/python" -c 'import sys' 2>/dev/null; then
    info "Environnement Python obsolète : recréation"
    rm -rf "${DIR:?}/.venv"
  fi
  if [[ ! -x $DIR/.venv/bin/python ]]; then
    info "Création de l'environnement Python (.venv)"
    "$PYTHON" -m venv "$DIR/.venv"
  fi
  info "Installation des dépendances (pip)"
  "$DIR/.venv/bin/pip" install --quiet --disable-pip-version-check -r "$DIR/requirements.txt"
  VPY="$DIR/.venv/bin/python"
}

write_config() {
  if [[ -z $ADMIN_PASSWORD ]]; then
    ADMIN_PASSWORD="$("$VPY" -c 'import secrets; print(secrets.token_urlsafe(12))')"
    GENERATED_ADMIN_PW="$ADMIN_PASSWORD"
  fi
  TMCFG_CALLSIGN="$CALLSIGN" TMCFG_LABEL="$LABEL" TMCFG_GRID="$GRID" TMCFG_PUBLIC="$PUBLIC" \
  TMCFG_CLUB_CALLSIGN="$CLUB_CALLSIGN" TMCFG_CLUB_NAME="$CLUB_NAME" TMCFG_CLUB_CITY="$CLUB_CITY" \
  TMCFG_CLUB_WEBSITE="$CLUB_WEBSITE" TMCFG_BASE_URL="$BASE_URL" TMCFG_OPERATORS="$OPERATORS" \
  TMCFG_ADMIN_PASSWORD="$ADMIN_PASSWORD" TMCFG_OPERATOR_PASSWORD="$OPERATOR_PASSWORD" \
  TMCFG_QRZ_USER="$QRZ_USER" TMCFG_QRZ_PASSWORD="$QRZ_PASSWORD" TMCFG_TRUSTED_PROXIES="$TRUSTED_PROXIES" \
    "$VPY" "$DIR/deploy/make_config.py" "$DIR/config.yml.example" "$DIR/config.yml"
  info "config.yml créé"
}

set_permissions() {
  local item
  if [[ $USE_SYSTEMD == yes ]]; then
    # Code et venv à root (lecture seule pour le service) ; données au service.
    for item in "${CODE_ITEMS[@]}" "$MARKER" "$STATE" .venv; do
      if [[ -e $DIR/$item ]]; then chown -R root:root "$DIR/$item"; fi
    done
    chown root:root "$DIR"; chmod 755 "$DIR"
    chown -R "$SVC_USER:$SVC_USER" "$DIR/var"; chmod 750 "$DIR/var"
    chown "root:$SVC_USER" "$DIR/config.yml"; chmod 640 "$DIR/config.yml"
  else
    chmod 600 "$DIR/config.yml"
  fi
}

health() {  # 0 si l'application répond sur /healthz
  local check_host="$HOST"
  if [[ $HOST == 0.0.0.0 || $HOST == "::" ]]; then check_host="127.0.0.1"; fi
  "$VPY" - "http://$check_host:$PORT/healthz" >/dev/null 2>&1 <<'PY'
import sys, time, urllib.request
for _ in range(30):
    try:
        with urllib.request.urlopen(sys.argv[1], timeout=2) as r:
            sys.exit(0 if r.status == 200 else 1)
    except Exception:
        time.sleep(0.5)
sys.exit(1)
PY
}

install_service() {
  local caps="" protect_home="true" unit="/etc/systemd/system/$SERVICE.service"
  # Port pris par autre chose que ce service (ex. nginx après un passage Internet → réseau local).
  if command -v ss >/dev/null && [[ -n "$(ss -Hltn "sport = :$PORT" 2>/dev/null)" ]] \
     && ! { systemctl is-active --quiet "$SERVICE" && [[ $OLD_PORT == "$PORT" ]]; }; then
    die "le port $PORT est déjà utilisé par un autre programme (sudo ss -ltnp 'sport = :$PORT') ;" \
        "si c'est nginx devenu inutile : sudo systemctl disable --now nginx — sinon choisir --port"
  fi
  if (( PORT < 1024 )); then caps="CAP_NET_BIND_SERVICE"; fi
  case "$DIR" in /home/*|/root/*) protect_home="read-only" ;; esac
  sed -e "s|@DIR@|$DIR|g" -e "s|@USER@|$SVC_USER|g" -e "s|@SERVICE@|$SERVICE|g" \
      -e "s|@HOST@|$HOST|g" -e "s|@PORT@|$PORT|g" -e "s|@PROTECT_HOME@|$protect_home|g" \
      -e "s|@CAPS@|$caps|g" "$DIR/deploy/tm-activation.service.in" > "$unit"
  chmod 644 "$unit"
  systemctl daemon-reload
  systemctl enable --quiet "$SERVICE"
  systemctl restart "$SERVICE"
  info "Service $SERVICE (re)démarré, vérification…"
  health || die "le service ne répond pas sur $HOST:$PORT — voir : journalctl -u $SERVICE -n 50"
  info "Le service répond"
}

# ── Mode Internet : box, DNS, nginx, HTTPS ──────────────────────────────────

guide_step() { echo; echo "  [$1/4] $2"; echo "  ──────────────────────────────────────────"; }

network_guide() {
  local choice
  title "Réglage de la $(box_label) et du nom de domaine"
  echo "  Quatre réglages, à faire une seule fois. Ouvrez l'interface de la"
  echo "  $(box_label) dans un navigateur : $(box_url)"
  echo "  (les intitulés peuvent varier un peu selon la version de la box)."

  guide_step 1 "Adresse fixe du Pi sur le réseau local"
  netcheck local
  if [[ -n $NC_LOCAL_IP ]]; then
    echo "  Ce Pi : adresse $NC_LOCAL_IP, carte réseau ${NC_IFACE:-?}, adresse MAC ${NC_MAC:-?}."
    if [[ $NC_IFACE == wlan* ]]; then warn "le Pi passe par le Wi-Fi : le câble Ethernet est plus fiable."; fi
  else
    warn "adresse locale introuvable : le câble réseau est-il branché ?"
  fi
  echo "  Réservez cette adresse, pour que la box la donne toujours au Pi :"
  help_dhcp
  pause

  guide_step 2 "Adresse IPv4 publique de la connexion"
  netcheck public
  if [[ -n $NC_PUBLIC_IP ]]; then
    echo "  Adresse publique vue d'Internet : $NC_PUBLIC_IP"
  else
    warn "Internet est injoignable depuis le Pi."
  fi
  if [[ $NC_PUBLIC_KIND == cgnat ]]; then warn "adresse 100.64.x.x : partagée par l'opérateur."; fi
  help_ipv4
  if ! confirm "La $(box_label) a-t-elle une adresse IPv4 à elle (full-stack) ?" o; then
    SHARED_IP=1
    warn "sans adresse IPv4 dédiée, les chasseurs ne pourront pas joindre le Pi."
    echo "  L'installation se termine ; une fois l'adresse obtenue, relancez : sudo ./install.sh"
    return 0
  fi

  guide_step 3 "Nom de domaine"
  echo "  Dans la zone DNS de votre domaine (chez le registrar : OVH, Gandi, Ionos…),"
  echo "  créez UN enregistrement :"
  echo
  printf '      %-32s A   %s\n' "$DOMAIN." "${NC_PUBLIC_IP:-<adresse publique>}"
  echo
  echo "  et aucun enregistrement AAAA (IPv6) pour ce nom."
  if [[ $BOX == freebox ]]; then
    echo "  L'adresse full-stack de Free est fixe : cet enregistrement ne changera plus."
  fi
  while :; do
    pause "Entrée pour vérifier le DNS…"
    netcheck dns "$DOMAIN"
    if dns_ok; then info "DNS correct : $DOMAIN → $NC_DNS_A"; break; fi
    dns_explain
    menu choice "Choix" 1 "Réessayer (la propagation prend de quelques minutes à une heure)" "Continuer quand même"
    if [[ $choice == 2 ]]; then break; fi
  done

  guide_step 4 "Redirection des ports 80 et 443 vers le Pi"
  help_ports
  pause
  info "Réglages de la $(box_label) terminés"
}

setup_nginx() {
  local conf
  if [[ -d /etc/nginx/sites-available ]]; then
    conf="/etc/nginx/sites-available/$SERVICE"
  else
    conf="/etc/nginx/conf.d/$SERVICE.conf"
  fi
  # Jamais réécrite ensuite : certbot y ajoute le bloc HTTPS.
  if [[ -f $conf ]]; then
    info "Configuration nginx existante conservée : $conf"
  else
    sed -e "s|tm\.mon-club\.fr|$DOMAIN|g" -e "s|nom-machine\.local|$MDNS_NAME.local|g" \
        -e "s|127\.0\.0\.1:8000|127.0.0.1:$PORT|g" "$DIR/deploy/nginx.conf.example" > "$conf"
    if [[ -d /etc/nginx/sites-enabled ]]; then ln -sf "$conf" "/etc/nginx/sites-enabled/$SERVICE"; fi
    info "Configuration nginx créée : $conf"
  fi
  nginx -t -q || die "configuration nginx invalide (sudo nginx -t)"
  systemctl enable --quiet --now nginx
  systemctl reload nginx
}

explain_certbot() {  # explain_certbot "sortie de certbot"
  local out=$1 detail
  detail="$(grep -m1 -E 'Detail:' <<<"$out" | sed -E 's/^[[:space:]]*Detail:[[:space:]]*//' || true)"
  echo "  Let's Encrypt n'a pas pu valider $DOMAIN."
  if [[ -n $detail ]]; then echo "  Motif : $detail"; fi
  if [[ $out == *"too many"* || $out == *rateLimited* || $out == *"rate limit"* ]]; then
    echo "  → Trop d'essais récents : attendez une heure avant de réessayer."
  elif [[ $out == *NXDOMAIN* || $out == *"DNS problem"* || $out == *"no valid A records"* ]]; then
    echo "  → Le nom $DOMAIN n'est pas (encore) dans le DNS : voir l'étape « Nom de domaine »."
  elif [[ $detail =~ [0-9a-fA-F]{1,4}:[0-9a-fA-F]{0,4}: ]]; then
    echo "  → Let's Encrypt passe par l'IPv6 : supprimez l'enregistrement AAAA de $DOMAIN."
  elif [[ $out == *"Timeout during connect"* || $out == *"timed out"* || $out == *Timeout* ]]; then
    echo "  → Port 80 injoignable depuis Internet : redirection de ports de la $(box_label),"
    echo "    ou adresse IPv4 partagée."
  elif [[ $out == *"Connection refused"* ]]; then
    echo "  → Port 80 fermé : la redirection pointe-t-elle bien vers ce Pi (${NC_LOCAL_IP:-son adresse}) ?"
  else
    tail -n 8 <<<"$out" | sed 's/^/    /'
  fi
  return 0
}

setup_https() {
  local out
  if [[ -d /etc/letsencrypt/live/$DOMAIN ]]; then info "Certificat HTTPS déjà présent pour $DOMAIN"; return 0; fi
  if [[ -z $EMAIL ]]; then return 0; fi
  if [[ $SHARED_IP == 1 ]]; then return 0; fi
  while :; do
    info "Vérification par Let's Encrypt (essai à blanc, sans conséquence)…"
    if out="$(certbot certonly --nginx --dry-run --non-interactive --agree-tos -m "$EMAIL" -d "$DOMAIN" 2>&1)"; then
      info "Essai réussi : demande du certificat"
      if out="$(certbot --nginx --non-interactive --agree-tos -m "$EMAIL" -d "$DOMAIN" --redirect 2>&1)"; then
        info "HTTPS activé pour $DOMAIN (renouvellement automatique)"
      else
        warn "le certificat n'a pas pu être installé :"
        tail -n 8 <<<"$out" | sed 's/^/    /'
      fi
      return 0
    fi
    explain_certbot "$out"
    if [[ $INTERACTIVE == yes ]] && confirm "Corriger, puis réessayer ?" o; then continue; fi
    return 0
  done
}

check_clock() {
  if [[ $USE_SYSTEMD == yes ]] && command -v timedatectl >/dev/null \
     && [[ "$(timedatectl show -p NTPSynchronized --value 2>/dev/null)" != yes ]]; then
    warn "l'horloge n'est pas synchronisée (NTP). Heure du système : $(date -u '+%d/%m/%Y %H:%M') UTC."
    warn "les heures des QSO en dépendent : voir « Heure du Raspberry Pi » dans le README."
  fi
  return 0
}

summary() {
  local ip has_cert=0
  if [[ $MODE == internet && -d /etc/letsencrypt/live/$DOMAIN ]]; then has_cert=1; fi
  echo
  echo "TM Activation $VERSION installé dans $DIR (mode $MODE)"
  case "$MODE" in
    lan)
      line "Adresse" "http://$MDNS_NAME.local$PORT_SUFFIX/"
      for ip in $(hostname -I 2>/dev/null); do
        if [[ $ip != *:* ]]; then line "" "http://$ip$PORT_SUFFIX/"; fi
      done ;;
    internet)
      if [[ $has_cert == 1 ]]; then line "Adresse publique" "https://$DOMAIN/"
      else line "Adresse publique" "http://$DOMAIN/  (HTTPS pas encore actif)"; fi
      line "Réseau local" "http://$MDNS_NAME.local/" ;;
    tunnel)
      line "Adresse publique" "https://$DOMAIN/  (Cloudflare Tunnel)"
      line "Réseau local" "http://$MDNS_NAME.local$PORT_SUFFIX/"
      line "Tunnel" "systemctl status cloudflared" ;;
    *)
      line "Écoute" "http://$HOST:$PORT/  (derrière votre reverse proxy : deploy/nginx.conf.example)" ;;
  esac
  if [[ $USE_SYSTEMD == yes ]]; then
    line "Service" "systemctl status $SERVICE · journalctl -u $SERVICE -f"
  else
    line "Lancement" "cd $DIR && TM_CONFIG=$DIR/config.yml .venv/bin/uvicorn app.main:app --host $HOST --port $PORT"
  fi
  line "Pages publiques" "/activations et /<indicatif en minuscules>"
  line "Opérateurs" "/activation/login (indicatif + mot de passe commun)"
  line "Administration" "/login → Réglages"
  if [[ -n $GENERATED_ADMIN_PW ]]; then
    echo
    echo "  Mot de passe administrateur généré : $GENERATED_ADMIN_PW"
    echo "  (à noter ; il reste lisible dans $DIR/config.yml, clé auth.password)"
  fi
  line "Données" "$DIR/var/ (sauvegardes automatiques : var/backups/)"
  line "Diagnostic" "sudo $DIR/install.sh --check"
  line "Mise à jour" "sudo ./install.sh depuis la nouvelle version (réglages repris)"
  if [[ $MODE == tunnel ]]; then
    echo
    if [[ $TUNNEL_READY == 0 ]]; then
      echo "  À faire : confier $DOMAIN à Cloudflare, puis relancer sudo ./install.sh."
    else
      echo "  Test final : ouvrez https://$DOMAIN/ depuis un téléphone en 4G, Wi-Fi coupé."
      echo "  Le Pi peut changer de lieu ou de connexion : il n'y a rien à refaire."
    fi
  fi
  if [[ $MODE == internet ]]; then
    echo
    if [[ $has_cert == 1 ]]; then
      echo "  Test final : ouvrez https://$DOMAIN/ depuis un téléphone en 4G, Wi-Fi coupé."
    elif [[ $SHARED_IP == 1 ]]; then
      echo "  À faire : obtenir une adresse IPv4 dédiée, puis relancer sudo ./install.sh."
    elif [[ -z $EMAIL ]]; then
      echo "  HTTPS : relancez sudo ./install.sh --email vous@exemple.fr (ou sudo certbot --nginx -d $DOMAIN)."
    else
      echo "  HTTPS : corrigez le point signalé, puis relancez sudo ./install.sh (le guide reprend)."
    fi
  fi
  return 0
}

# ── Cloudflare Tunnel ───────────────────────────────────────────────────────

tunnel_name() {  # nom du tunnel : celui demandé, sinon d'après le domaine
  if [[ -n $TUNNEL_NAME ]]; then echo "$TUNNEL_NAME"; else echo "tm-${DOMAIN//./-}"; fi
}

install_cloudflared() {
  local arch url tmp
  if command -v cloudflared >/dev/null; then return 0; fi
  arch="$(dpkg --print-architecture 2>/dev/null || echo amd64)"
  case "$arch" in arm64|armhf|amd64|386) ;; *) die "cloudflared n'existe pas pour l'architecture $arch" ;; esac
  url="https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-$arch.deb"
  tmp="$(mktemp -d)"
  info "Téléchargement de cloudflared ($arch)"
  "$PYTHON" - "$url" "$tmp/cloudflared.deb" <<'PY' || die "téléchargement de cloudflared impossible"
import sys, urllib.request
urllib.request.urlretrieve(sys.argv[1], sys.argv[2])
PY
  apt-get install -y -qq "$tmp/cloudflared.deb" >/dev/null || die "installation de cloudflared impossible"
  rm -rf "$tmp"
}

tunnel_guide() {
  title "Cloudflare Tunnel"
  cat <<EOF
  Le Pi ouvre une connexion SORTANTE vers Cloudflare : aucun port n'est ouvert
  sur la box, l'adresse IP publique n'a plus d'importance (IPv4 partagée, 4G,
  partage de connexion) et le HTTPS est assuré par Cloudflare. En changeant de
  lieu, il n'y a rien à refaire.

  Deux conditions, sur https://dash.cloudflare.com (compte gratuit) :
    1. le domaine doit être géré par Cloudflare (« Add a site », puis
       changement des serveurs DNS chez le registrar : de quelques minutes à
       quelques heures) ;
    2. être connecté à ce compte sur l'appareil où vous ouvrirez le lien
       d'autorisation affiché tout à l'heure.
EOF
  if ! confirm "Le domaine de $DOMAIN est-il déjà géré par Cloudflare ?" o; then
    TUNNEL_READY=0
    warn "à faire d'abord sur dash.cloudflare.com, puis relancez : sudo ./install.sh"
    echo "  L'installation se termine ; l'application restera joignable en réseau local."
  fi
  return 0
}

cf_tunnel_uuid() {  # identifiant du tunnel nommé $1 ("" s'il n'existe pas)
  local json
  json="$(cloudflared tunnel list --output json 2>/dev/null || true)"
  CF_JSON="$json" "$PYTHON" -c 'import json, os, sys
try:
    tunnels = json.loads(os.environ.get("CF_JSON") or "[]")
except ValueError:
    tunnels = []
print(next((t.get("id", "") for t in tunnels if t.get("name") == sys.argv[1]), ""))' "$1"
}

setup_tunnel() {
  local name uuid cred conf="$CF_CONF_DIR/config.yml"
  if [[ $TUNNEL_READY == 0 ]]; then return 0; fi
  name="$(tunnel_name)"
  install_cloudflared
  if [[ ! -f $CF_LOGIN_DIR/cert.pem ]]; then
    title "Autorisation Cloudflare"
    echo "  cloudflared affiche un lien : ouvrez-le sur un appareil connecté à votre"
    echo "  compte Cloudflare, puis choisissez le domaine. Le Pi attend."
    cloudflared tunnel login || die "autorisation Cloudflare abandonnée"
  fi
  uuid="$(cf_tunnel_uuid "$name")"
  if [[ -z $uuid ]]; then
    info "Création du tunnel $name"
    cloudflared tunnel create "$name" >/dev/null || die "création du tunnel impossible"
    uuid="$(cf_tunnel_uuid "$name")"
  fi
  [[ -n $uuid ]] || die "tunnel $name introuvable après création"
  mkdir -p "$CF_CONF_DIR"
  cred="$CF_CONF_DIR/$uuid.json"
  if [[ -f $CF_LOGIN_DIR/$uuid.json ]]; then
    cp "$CF_LOGIN_DIR/$uuid.json" "$cred"
    chmod 600 "$cred"
  fi
  [[ -f $cred ]] || die "identifiants du tunnel introuvables ($CF_LOGIN_DIR/$uuid.json)"
  cat > "$conf" <<EOF
# Tunnel Cloudflare de TM Activation — généré par install.sh
tunnel: $uuid
credentials-file: $cred
ingress:
  - hostname: $DOMAIN
    service: http://127.0.0.1:$PORT
  - service: http_status:404
EOF
  cloudflared tunnel --config "$conf" ingress validate || die "configuration cloudflared invalide"
  if cloudflared tunnel route dns "$name" "$DOMAIN" >/dev/null 2>&1; then
    info "Nom $DOMAIN routé vers le tunnel"
  else
    warn "route DNS non créée : elle existe déjà, ou $DOMAIN n'est pas géré par ce compte Cloudflare."
  fi
  if [[ ! -f /etc/systemd/system/cloudflared.service ]]; then
    cloudflared service install >/dev/null 2>&1 || warn "service cloudflared à installer à la main"
  fi
  systemctl daemon-reload
  systemctl enable --quiet cloudflared 2>/dev/null || true
  systemctl restart cloudflared
  TUNNEL_NAME="$name"
  info "Tunnel Cloudflare actif : https://$DOMAIN/"
}

# ── Diagnostic (--check) ────────────────────────────────────────────────────

run_check() {
  local problems=0 state
  echo "TM Activation — diagnostic de $DIR"
  line "Version" "$(cat "$DIR/$MARKER") (mode $MODE)"
  if command -v systemctl >/dev/null && [[ -f /etc/systemd/system/$SERVICE.service ]]; then
    state="$(systemctl is-active "$SERVICE" 2>/dev/null || true)"
    line "Service $SERVICE" "$state"
    if [[ $state != active ]]; then problems=$((problems + 1)); fi
  fi
  if [[ -x $VPY ]] && health; then
    line "Application" "répond sur $HOST:$PORT"
  else
    line "Application" "NE RÉPOND PAS sur $HOST:$PORT"
    problems=$((problems + 1))
  fi
  netcheck local
  line "Réseau local" "${NC_LOCAL_IP:-?} (${NC_IFACE:-?}, MAC ${NC_MAC:-?})"
  case "$MODE" in
    lan)
      line "Adresse" "http://$MDNS_NAME.local$PORT_SUFFIX/" ;;
    internet)
      netcheck public
      line "IPv4 publique" "${NC_PUBLIC_IP:-injoignable}${NC_PUBLIC_KIND:+ ($NC_PUBLIC_KIND)}"
      if [[ -z $NC_PUBLIC_IP || $NC_PUBLIC_KIND == cgnat ]]; then problems=$((problems + 1)); fi
      netcheck dns "$DOMAIN"
      if dns_ok; then
        line "DNS $DOMAIN" "OK → $NC_DNS_A"
      else
        line "DNS $DOMAIN" "À CORRIGER"
        dns_explain
        problems=$((problems + 1))
      fi
      netcheck cert "$DOMAIN"
      if [[ $NC_CERT == ok ]]; then
        line "Certificat HTTPS" "valide, expire dans $NC_CERT_DAYS jours"
      else
        line "Certificat HTTPS" "absent ou invalide${NC_CERT_ERROR:+ ($NC_CERT_ERROR)}"
        problems=$((problems + 1))
      fi
      if [[ $IS_ROOT == 1 ]] && command -v nginx >/dev/null; then
        if nginx -t -q 2>/dev/null; then line "nginx" "configuration valide"
        else line "nginx" "CONFIGURATION INVALIDE (sudo nginx -t)"; problems=$((problems + 1)); fi
      fi
      echo
      echo "  $(box_label) : les ports TCP 80 et 443 doivent être redirigés vers ${NC_LOCAL_IP:-ce Pi}."
      echo "  Pi déplacé sur une autre connexion ? Refaites le bail DHCP et la redirection de"
      echo "  ports sur la nouvelle box, et mettez l'enregistrement DNS à la nouvelle adresse." ;;
    tunnel)
      state="$(systemctl is-active cloudflared 2>/dev/null || true)"
      line "Service cloudflared" "$state"
      if [[ $state != active ]]; then problems=$((problems + 1)); fi
      if [[ -f $CF_CONF_DIR/config.yml ]] && command -v cloudflared >/dev/null \
         && cloudflared tunnel --config "$CF_CONF_DIR/config.yml" ingress validate >/dev/null 2>&1; then
        line "Configuration tunnel" "valide (→ 127.0.0.1:$PORT)"
      else
        line "Configuration tunnel" "ABSENTE OU INVALIDE ($CF_CONF_DIR/config.yml)"
        problems=$((problems + 1))
      fi
      netcheck dns "$DOMAIN"
      if [[ -n $NC_DNS_A ]]; then
        line "DNS $DOMAIN" "→ $NC_DNS_A (Cloudflare)"
      else
        line "DNS $DOMAIN" "NE RÉPOND PAS"
        problems=$((problems + 1))
      fi
      netcheck cert "$DOMAIN" "$DOMAIN"
      if [[ $NC_CERT == ok ]]; then
        line "HTTPS $DOMAIN" "valide, expire dans $NC_CERT_DAYS jours"
      else
        line "HTTPS $DOMAIN" "injoignable ou invalide${NC_CERT_ERROR:+ ($NC_CERT_ERROR)}"
        problems=$((problems + 1))
      fi
      line "Réseau local" "http://$MDNS_NAME.local$PORT_SUFFIX/"
      echo
      echo "  Le tunnel sort du Pi : aucun port à ouvrir, même sur une autre connexion." ;;
  esac
  if command -v timedatectl >/dev/null; then
    if [[ "$(timedatectl show -p NTPSynchronized --value 2>/dev/null)" == yes ]]; then
      line "Horloge" "synchronisée"
    else
      line "Horloge" "NON SYNCHRONISÉE ($(date -u '+%d/%m/%Y %H:%M') UTC)"
      problems=$((problems + 1))
    fi
  fi
  echo
  if (( problems > 0 )); then echo "  $problems point(s) à corriger."; return 1; fi
  echo "  Tout est en ordre."
}

# ── Programme principal ─────────────────────────────────────────────────────

main() {
  parse_args "$@"
  resolve_context
  if [[ $CHECK == 1 ]]; then
    [[ -f $DIR/$MARKER ]] || die "aucune installation TM Activation dans $DIR (option --dir ?)"
    MODE="${MODE:-manual}"
    resolve_ports
    VPY="$DIR/.venv/bin/python"
    run_check
    return
  fi

  if [[ ! -f $DIR/config.yml ]]; then FIRST_CONFIG=1; fi
  if [[ $INTERACTIVE == yes && $UPGRADE == 0 && $FIRST_CONFIG == 1 ]]; then welcome; fi
  if [[ -z $MODE ]]; then
    if [[ $UPGRADE == 0 && $INTERACTIVE == yes && $USE_SYSTEMD == yes ]]; then ask_mode; else MODE="manual"; fi
  elif [[ $MODE == internet && $INTERACTIVE == yes && $UPGRADE == 0 && $ASKED == 0 ]]; then
    ask_box_email
  fi
  resolve_ports
  validate
  if [[ $FIRST_CONFIG == 1 ]]; then collect_config; fi
  if [[ $INTERACTIVE == yes && ( $ASKED == 1 || $FIRST_CONFIG == 1 ) ]]; then recap; fi

  if [[ $UPGRADE == 1 ]]; then
    info "Mise à jour de $DIR vers la version $VERSION (était : $(cat "$DIR/$MARKER")) — mode $MODE"
  else
    info "Installation de TM Activation $VERSION dans $DIR — mode $MODE"
  fi
  install_packages
  check_python
  create_account
  install_code
  install_venv
  if [[ $FIRST_CONFIG == 1 ]]; then write_config; else info "config.yml existant conservé"; fi
  set_permissions
  if [[ $USE_SYSTEMD == yes ]]; then install_service; fi
  if [[ $MODE == internet ]]; then
    if [[ $INTERACTIVE == yes && ! -d /etc/letsencrypt/live/$DOMAIN ]]; then network_guide; fi
    setup_nginx
    setup_https
  elif [[ $MODE == tunnel ]]; then
    if [[ $INTERACTIVE == yes && ! -f $CF_CONF_DIR/config.yml ]]; then tunnel_guide; fi
    setup_tunnel
  fi
  check_clock
  summary
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
