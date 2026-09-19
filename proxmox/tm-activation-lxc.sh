#!/usr/bin/env bash
# ------------------------------------------------------------------------------
# TM Activation — installation en conteneur LXC sur Proxmox VE
#
# Crée une CT Debian (non privilégiée), puis télécharge TM Activation depuis
# GitHub (https://github.com/f4ioz/tm-activation), vérifie son empreinte et
# l'installe dans la CT : installation guidée (réseau local, Internet par la
# box ou Cloudflare Tunnel) ou test rapide en réseau local.
#
# Usage : sur le nœud Proxmox, en root :
#   bash -c "$(curl -fsSL https://raw.githubusercontent.com/f4ioz/tm-activation/main/proxmox/tm-activation-lxc.sh)"
#
# Variables : TM_REPO (f4ioz/tm-activation), TM_BRANCH (main), TM_VERSION
# (version précise), TM_RAW_URL (miroir des fichiers bruts du dépôt).
# ------------------------------------------------------------------------------

set -euo pipefail

REPO="${TM_REPO:-f4ioz/tm-activation}"
BRANCH="${TM_BRANCH:-main}"
RAW="${TM_RAW_URL:-https://raw.githubusercontent.com/$REPO/$BRANCH}"
UPDATER="/usr/local/sbin/tm-activation-update"

# ─── Couleurs ─────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[0;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; NC='\033[0m'

msg_info() { echo -e "${CYAN}${BOLD}[..]${NC} $1"; }
msg_ok()   { echo -e "${GREEN}${BOLD}[OK]${NC} $1"; }
msg_warn() { echo -e "${YELLOW}${BOLD}[!!]${NC} $1"; }
msg_err()  { echo -e "${RED}${BOLD}[KO]${NC} $1" >&2; }

header() {
  clear 2>/dev/null || true
  cat <<'EOF'
  _____ __  __      _        _   _            _   _
 |_   _|  \/  |    / \   ___| |_(_)_   ____ _| |_(_) ___  _ __
   | | | |\/| |   / _ \ / __| __| \ \ / / _` | __| |/ _ \| '_ \
   | | | |  | |  / ___ \ (__| |_| |\ V / (_| | |_| | (_) | | | |
   |_| |_|  |_| /_/   \_\___|\__|_| \_/ \__,_|\__|_|\___/|_| |_|

 TM Activation — installation LXC pour Proxmox VE
 Indicatifs spéciaux : planning, log QSO, ADIF, page publique
EOF
  echo
}

# ─── Pré-checks ───────────────────────────────────────────────────────────────
check_root() {
  [[ $EUID -eq 0 ]] || { msg_err "À lancer en root sur le nœud Proxmox."; exit 1; }
}
check_proxmox() {
  local cmd
  for cmd in pct pveam pvesm lxc-attach; do
    command -v "$cmd" >/dev/null || { msg_err "$cmd introuvable : pas sur un nœud Proxmox VE ?"; exit 1; }
  done
}

# ─── Défauts ──────────────────────────────────────────────────────────────────
D_HOSTNAME="tm-activation"
D_DISK="4"           # Go : Debian + Python + base SQLite (quelques Mo)
D_CORES="1"
D_RAM="512"          # Mo : un seul worker uvicorn
D_SWAP="512"         # Mo
D_BRIDGE="vmbr0"
D_IP="dhcp"
D_TEST_CALL="TM0TEST"

# Les ID sont communs aux VM et aux CT, sur tout le cluster : pvesh le sait,
# pct status ne voit que les CT du nœud.
id_free() {
  if command -v pvesh >/dev/null; then
    pvesh get /cluster/nextid --vmid "$1" >/dev/null 2>&1
  else
    ! pct status "$1" &>/dev/null && ! qm status "$1" &>/dev/null
  fi
}

find_next_ctid() {
  local id
  id=$(pvesh get /cluster/nextid 2>/dev/null | tr -d '"[:space:]') || true
  if [[ $id =~ ^[0-9]+$ ]]; then echo "$id"; return; fi
  id=100
  while ! id_free "$id"; do ((id++)); done
  echo "$id"
}

first_storage() {  # first_storage CONTENU DÉFAUT → stockage acceptant ce contenu
  local found
  found=$(pvesm status -content "$1" 2>/dev/null | awk 'NR > 1 && $3 == "active" {print $1}')
  if grep -qx "$2" <<<"$found"; then echo "$2"; else echo "${found%%$'\n'*}"; fi
}

ask() {  # ask VAR "Question" [défaut]
  local answer
  read -rp "$2${3:+ [$3]} : " answer || true
  printf -v "$1" '%s' "${answer:-${3:-}}"
}

ask_password() {  # ask_password VAR "Question" : deux saisies identiques, non vides
  local pw pw2
  while :; do
    read -rsp "$2 : " pw || true; echo
    read -rsp "Confirmer : " pw2 || true; echo
    if [[ -n $pw && $pw == "$pw2" ]]; then break; fi
    msg_warn "Mots de passe différents ou vides."
  done
  printf -v "$1" '%s' "$pw"
}

# ─── Questions ────────────────────────────────────────────────────────────────
prompt_config() {
  local d_ctid d_storage choice c
  d_ctid=$(find_next_ctid)
  d_storage=$(first_storage rootdir local-lvm)
  TMPL_STORAGE=$(first_storage vztmpl local)
  [[ -n $TMPL_STORAGE ]] || { msg_err "Aucun stockage actif n'accepte les templates (vztmpl)."; exit 1; }

  echo -e "${BOLD}Conteneur${NC} (Entrée = valeur proposée)"
  while :; do
    ask CTID "Container ID" "$d_ctid"
    if [[ $CTID =~ ^[0-9]+$ ]] && (( CTID >= 100 )) && id_free "$CTID"; then break; fi
    msg_warn "ID invalide ou déjà utilisé (VM ou CT)."
  done
  while :; do
    ask CT_HOST "Nom de la CT (adresse http://<nom>.local)" "$D_HOSTNAME"
    CT_HOST="${CT_HOST,,}"
    if [[ $CT_HOST =~ ^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$ ]]; then break; fi
    msg_warn "Nom invalide : lettres, chiffres et tirets."
  done
  ask DISK "Disque (Go)" "$D_DISK"
  ask CORES "vCPU" "$D_CORES"
  ask RAM "RAM (Mo)" "$D_RAM"
  ask SWAP "Swap (Mo)" "$D_SWAP"
  ask STORAGE "Stockage de la CT" "$d_storage"
  ask BRIDGE "Bridge réseau" "$D_BRIDGE"
  while :; do
    ask IP "IP (dhcp ou CIDR, ex. 192.168.1.50/24)" "$D_IP"
    if [[ $IP == dhcp || $IP =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}/[0-9]{1,2}$ ]]; then break; fi
    msg_warn "Adresse invalide."
  done
  GATEWAY=""
  if [[ $IP != dhcp ]]; then ask GATEWAY "Passerelle (ex. 192.168.1.1)" ""; fi
  for c in DISK CORES RAM SWAP; do
    [[ ${!c} =~ ^[0-9]+$ ]] || { msg_err "$c doit être un nombre : '${!c}'"; exit 1; }
  done

  echo
  echo "Clé SSH publique pour root (facultatif) : une ligne ssh-ed25519/ssh-rsa…,"
  echo "ou Entrée pour ignorer."
  read -r SSH_PUBKEY || true
  ask_password CT_PW "Mot de passe root de la CT"

  echo
  echo -e "${BOLD}Installation de TM Activation${NC}"
  echo "  1) Guidée : les questions de TM Activation (réseau local, Internet par la"
  echo "     box avec HTTPS, ou Cloudflare Tunnel ; indicatif, club, mots de passe)"
  echo "  2) Test rapide : réseau local, sans question, mot de passe admin généré"
  while :; do
    ask choice "Choix" "1"
    case "$choice" in 1) INSTALL_MODE="guided"; break ;; 2) INSTALL_MODE="quick"; break ;; esac
  done
  TEST_CALL=""
  if [[ $INSTALL_MODE == quick ]]; then
    while :; do
      ask TEST_CALL "Indicatif de test" "$D_TEST_CALL"
      TEST_CALL="${TEST_CALL^^}"
      if [[ $TEST_CALL =~ ^[A-Z0-9]{3,10}$ && $TEST_CALL =~ [0-9] ]]; then break; fi
      msg_warn "Indicatif invalide."
    done
  fi

  echo
  echo -e "${BOLD}Récapitulatif${NC}"
  echo "  CT $CTID « $CT_HOST » : disque ${DISK} Go, $CORES vCPU, RAM ${RAM} Mo, swap ${SWAP} Mo"
  echo "  stockage $STORAGE, bridge $BRIDGE, IP $IP${GATEWAY:+ (passerelle $GATEWAY)}"
  echo "  SSH root : $([[ -n $SSH_PUBKEY ]] && echo "clé fournie" || echo "sans clé (console : pct enter $CTID)")"
  if [[ $INSTALL_MODE == quick ]]; then
    echo "  TM Activation : test rapide, réseau local, indicatif $TEST_CALL"
  else
    echo "  TM Activation : installation guidée"
  fi
  echo "  Source : $RAW"
  read -rp "Continuer ? [O/n] : " c || true
  [[ "${c:-O}" =~ ^[OoYy]$ ]] || { msg_warn "Annulé : rien n'a été créé."; exit 0; }
}

# ─── Template ─────────────────────────────────────────────────────────────────
ensure_template() {
  local tmpl="" version
  msg_info "Recherche d'un template Debian…"
  pveam update >/dev/null 2>&1 || true
  for version in 13 12; do
    tmpl=$(pveam available --section system 2>/dev/null \
           | awk -v v="debian-$version-standard" '$2 ~ "^"v".*amd64" {print $2}' | sort -V | tail -n1)
    if [[ -n $tmpl ]]; then break; fi
  done
  [[ -n $tmpl ]] || { msg_err "Aucun template debian-13/12-standard dans pveam."; exit 1; }
  if ! pveam list "$TMPL_STORAGE" 2>/dev/null | grep -q "$tmpl"; then
    msg_info "Téléchargement du template $tmpl…"
    pveam download "$TMPL_STORAGE" "$tmpl" >/dev/null
  fi
  TEMPLATE="${TMPL_STORAGE}:vztmpl/${tmpl}"
  msg_ok "Template : $tmpl"
}

# ─── Création CT ──────────────────────────────────────────────────────────────
create_lxc() {
  local net="name=eth0,bridge=${BRIDGE}" keyfile="" extra=()
  if [[ $IP == dhcp ]]; then net="${net},ip=dhcp"; else net="${net},ip=${IP}${GATEWAY:+,gw=${GATEWAY}}"; fi
  if [[ -n $SSH_PUBKEY ]]; then
    keyfile=$(mktemp)
    echo "$SSH_PUBKEY" > "$keyfile"
    extra+=(--ssh-public-keys "$keyfile")
  fi

  msg_info "Création de la CT $CTID…"
  # nesting=1 : le service TM Activation est durci (ProtectSystem, PrivateTmp…),
  # ce qui demande des espaces de noms dans une CT non privilégiée.
  pct create "$CTID" "$TEMPLATE" \
    --hostname "$CT_HOST" \
    --cores "$CORES" \
    --memory "$RAM" \
    --swap "$SWAP" \
    --rootfs "${STORAGE}:${DISK}" \
    --net0 "$net" \
    --password "$CT_PW" \
    --features nesting=1 \
    --unprivileged 1 \
    --onboot 1 \
    --timezone host \
    --ostype debian \
    --tags tm-activation \
    --description "TM Activation — https://github.com/$REPO" \
    "${extra[@]}" >/dev/null
  if [[ -n $keyfile ]]; then rm -f "$keyfile"; fi
  msg_ok "CT $CTID créée."

  msg_info "Démarrage…"
  pct start "$CTID"
  msg_ok "CT démarrée."
}

# ─── Préparation de la CT ─────────────────────────────────────────────────────
ct() { pct exec "$CTID" -- env LANG=C.UTF-8 LC_ALL=C.UTF-8 DEBIAN_FRONTEND=noninteractive "$@"; }

wait_network() {
  local n=30
  msg_info "Attente du réseau dans la CT…"
  while (( n-- > 0 )); do
    if ct getent hosts raw.githubusercontent.com &>/dev/null; then msg_ok "Réseau OK."; return; fi
    sleep 2
  done
  msg_err "Pas d'accès à Internet (DNS) dans la CT : vérifier bridge, IP et passerelle."
  exit 1
}

prepare_ct() {
  local pkgs="curl ca-certificates avahi-daemon"
  if [[ -n $SSH_PUBKEY ]]; then pkgs="$pkgs openssh-server"; fi
  msg_info "Mise à jour du système (apt)…"
  ct bash -c 'apt-get update -qq && apt-get upgrade -y -qq' >/dev/null
  msg_ok "Système à jour."
  msg_info "Paquets : $pkgs…"
  ct bash -c "apt-get install -y -qq --no-install-recommends $pkgs" >/dev/null
  # Adresse <nom>.local : dans une CT non privilégiée, la limite rlimit-nproc
  # d'avahi est partagée avec les autres CT (même plage d'UID) → désactivée.
  ct bash -c 'sed -i "s/^rlimit-nproc=/#rlimit-nproc=/" /etc/avahi/avahi-daemon.conf
              systemctl restart avahi-daemon' >/dev/null 2>&1 \
    || msg_warn "avahi (http://$CT_HOST.local) n'a pas démarré : utiliser l'adresse IP."
  msg_ok "Paquets installés."

  msg_info "Téléchargement de l'installeur TM Activation (GitHub)…"
  ct curl -fsSL -o "$UPDATER" "$RAW/deploy/update-from-github.sh" \
    || { msg_err "Impossible de télécharger $RAW/deploy/update-from-github.sh"; exit 1; }
  ct chmod 755 "$UPDATER"
  msg_ok "Installeur prêt : $UPDATER"
}

# ─── Installation de TM Activation ────────────────────────────────────────────
install_app() {
  local env=(LANG=C.UTF-8 LC_ALL=C.UTF-8 "TERM=${TERM:-xterm}" "TM_REPO=$REPO" "TM_BRANCH=$BRANCH")
  if [[ -n ${TM_RAW_URL:-} ]]; then env+=("TM_RAW_URL=$TM_RAW_URL"); fi
  if [[ -n ${TM_VERSION:-} ]]; then env+=("TM_VERSION=$TM_VERSION"); fi
  echo
  if [[ $INSTALL_MODE == quick ]]; then
    msg_info "Installation de TM Activation (test rapide, réseau local)…"
    pct exec "$CTID" -- env "${env[@]}" TM_CALLSIGN="$TEST_CALL" TM_LABEL="Test Proxmox" TM_PUBLIC=1 \
      "$UPDATER" --lan --non-interactive
  else
    msg_info "Installation guidée de TM Activation (questions dans la CT)…"
    # lxc-attach donne un vrai terminal à l'installeur (questions, mots de passe).
    lxc-attach -n "$CTID" -- env "${env[@]}" "$UPDATER"
  fi
}

# ─── Récap final ──────────────────────────────────────────────────────────────
ct_ip() {
  ct bash -c "ip -4 -o addr show eth0 | awk '{print \$4}' | cut -d/ -f1 | head -n1" 2>/dev/null || true
}

show_summary() {
  local ip
  ip=$(ct_ip)
  echo
  echo -e "${GREEN}${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
  echo -e "${GREEN}${BOLD}  TM Activation installé dans la CT $CTID${NC}"
  echo -e "${GREEN}${BOLD}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
  echo
  echo "  CT          : $CTID ($CT_HOST)  IP ${ip:-?}"
  echo "  Console     : pct enter $CTID"
  if [[ -n $SSH_PUBKEY ]]; then echo "  SSH         : ssh root@${ip:-$CT_HOST.local}"; fi
  echo "  Mise à jour : pct exec $CTID -- tm-activation-update"
  echo "  Diagnostic  : pct exec $CTID -- /opt/tm-activation/install.sh --check"
  echo "  Journal     : pct exec $CTID -- journalctl -u tm-activation -n 50"
  if [[ $INSTALL_MODE == quick ]]; then
    echo
    echo "  Site        : http://${ip:-$CT_HOST.local}/  (ou http://$CT_HOST.local/)"
    echo "  Admin       : /login avec le mot de passe généré affiché plus haut"
    echo "  Supprimer la CT de test : pct stop $CTID && pct destroy $CTID"
  fi
  echo
}

install_failed() {
  msg_err "L'installation de TM Activation n'a pas abouti (la CT $CTID est conservée)."
  echo "  Relancer   : pct exec $CTID -- tm-activation-update   (guidée : lxc-attach -n $CTID -- tm-activation-update)"
  echo "  Console    : pct enter $CTID"
  echo "  Supprimer  : pct stop $CTID && pct destroy $CTID"
  exit 1
}

# ─── Main ─────────────────────────────────────────────────────────────────────
main() {
  header
  check_root
  check_proxmox
  prompt_config
  ensure_template
  create_lxc
  wait_network
  prepare_ct
  install_app || install_failed
  show_summary
}

if [[ "${BASH_SOURCE[0]:-$0}" == "$0" ]]; then
  main "$@"
fi
