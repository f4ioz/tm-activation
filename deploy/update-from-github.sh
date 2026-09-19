#!/usr/bin/env bash
# TM Activation — installation ou mise à jour depuis GitHub.
#
#   sudo bash update-from-github.sh [options d'install.sh]
#
# Télécharge la dernière version publiée (releases/ du dépôt GitHub), vérifie
# son empreinte SHA-256, puis lance install.sh avec les options données :
#   - première installation : installation guidée (ou --lan, --domain…) ;
#   - installation existante : mise à jour, réglages repris d'install.env,
#     config.yml et var/ jamais touchés.
#
# Déjà à jour → rien n'est fait (--force pour réinstaller la même version).
# Variables : TM_REPO (f4ioz/tm-activation), TM_BRANCH (main), TM_VERSION
# (version précise au lieu de la dernière), TM_RAW_URL (miroir), TM_DIR
# (dossier d'installation, /opt/tm-activation).
set -euo pipefail

REPO="${TM_REPO:-f4ioz/tm-activation}"
BRANCH="${TM_BRANCH:-main}"
RAW="${TM_RAW_URL:-https://raw.githubusercontent.com/$REPO/$BRANCH}"
INSTALL_DIR="${TM_DIR:-/opt/tm-activation}"
TMP_DIR=""

die()  { echo "ERREUR : $*" >&2; exit 1; }
info() { echo "==> $*"; }

main() {
  local force=0 args=() arg version current tmp archive
  for arg in "$@"; do
    if [[ $arg == --force ]]; then force=1; else args+=("$arg"); fi
  done
  command -v curl >/dev/null || die "curl absent (apt install curl ca-certificates)"
  command -v sha256sum >/dev/null || die "sha256sum absent (paquet coreutils)"

  version="${TM_VERSION:-}"
  if [[ -z $version ]]; then
    version="$(curl -fsSL "$RAW/VERSION")" || die "impossible de lire $RAW/VERSION (réseau ? dépôt ?)"
  fi
  version="$(echo "$version" | tr -d '[:space:]')"
  [[ $version =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "version invalide : '$version'"

  current=""
  if [[ -f $INSTALL_DIR/.tm-activation ]]; then current="$(tr -d '[:space:]' < "$INSTALL_DIR/.tm-activation")"; fi
  if [[ $current == "$version" && $force == 0 ]]; then
    info "TM Activation $version déjà installé dans $INSTALL_DIR : rien à faire (--force pour réinstaller)"
    return 0
  fi

  tmp="$(mktemp -d)"
  TMP_DIR="$tmp"   # global : la variable locale n'existe plus quand le trap EXIT s'exécute
  trap 'rm -rf "$TMP_DIR"' EXIT
  archive="tm-activation-$version.tar.gz"
  info "Téléchargement de TM Activation $version ($REPO)"
  curl -fsSL -o "$tmp/$archive" "$RAW/releases/$archive" || die "archive introuvable : $RAW/releases/$archive"
  curl -fsSL -o "$tmp/$archive.sha256" "$RAW/releases/$archive.sha256" || die "empreinte introuvable : $archive.sha256"
  (cd "$tmp" && sha256sum --quiet -c "$archive.sha256") || die "empreinte SHA-256 incorrecte : archive corrompue"
  info "Empreinte SHA-256 vérifiée"
  tar xzf "$tmp/$archive" -C "$tmp"
  [[ -f $tmp/tm-activation-$version/install.sh ]] || die "install.sh absent de l'archive"

  if [[ -n $current ]]; then info "Mise à jour $current → $version"; fi
  if [[ $INSTALL_DIR != /opt/tm-activation ]]; then args=(--dir "$INSTALL_DIR" "${args[@]}"); fi
  bash "$tmp/tm-activation-$version/install.sh" "${args[@]}"
}

if [[ "${BASH_SOURCE[0]:-$0}" == "$0" ]]; then
  main "$@"
fi
