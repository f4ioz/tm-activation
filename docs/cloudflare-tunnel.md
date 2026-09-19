# Publier sans ouvrir de ports : Cloudflare Tunnel

Le Raspberry Pi ouvre une connexion **sortante** vers Cloudflare, qui publie le
site en HTTPS. Rien n'est à ouvrir sur la box.

```
 Chasseurs  ──https──▶  Cloudflare  ◀──connexion sortante──  Raspberry Pi
                                                             cloudflared → TM Activation
```

**À préférer quand :**

- l'opérateur partage l'adresse IPv4 entre plusieurs abonnés (aucune
  redirection de ports possible) ;
- le Pi change souvent de lieu, ou passe par un partage de connexion 4G : rien
  n'est à refaire à chaque déplacement ;
- vous ne voulez pas toucher aux réglages de la box.

**À savoir :** tout le trafic public passe par Cloudflare, qui voit les pages
servies et termine le HTTPS. Le service est gratuit, mais dépend de ce
fournisseur. Pour rester maître de bout en bout, préférez la
[publication par la box](internet-raspberry-pi.md).

## Prérequis

1. Un compte Cloudflare (gratuit) sur <https://dash.cloudflare.com>.
2. **Le domaine doit être géré par Cloudflare** : dans le tableau de bord,
   « Add a site », puis remplacer les serveurs DNS du domaine chez le
   registrar (OVH, Gandi…) par ceux indiqués par Cloudflare. C'est l'étape la
   plus longue : de quelques minutes à quelques heures.
3. Le Pi installé, connecté à Internet (Ethernet, Wi-Fi ou 4G).

## Installation

```bash
sudo ./install.sh --tunnel --domain tm.mon-club.fr
```

Ou, en installation guidée, `sudo ./install.sh` puis le choix
« Internet par Cloudflare Tunnel ».

L'installeur :

1. installe `cloudflared` (paquet officiel Cloudflare correspondant au
   processeur du Pi) ;
2. affiche un **lien d'autorisation** : ouvrez-le sur un appareil connecté à
   votre compte Cloudflare et choisissez le domaine. Le Pi attend ;
3. crée le tunnel (nom par défaut : `tm-` suivi du domaine, modifiable avec
   `--tunnel-name`) et son fichier `/etc/cloudflared/config.yml`, qui renvoie
   le domaine vers l'application locale ;
4. crée l'enregistrement DNS chez Cloudflare (le domaine pointe vers le
   tunnel) ;
5. installe et démarre le service `cloudflared`, qui redémarre avec le Pi.

L'application écoute aussi sur le réseau local : les opérateurs sur place
peuvent utiliser `http://<nom-du-pi>.local:8000`.

## Vérifier

```bash
sudo /opt/tm-activation/install.sh --check
```

Le diagnostic contrôle le service `cloudflared`, la configuration du tunnel, la
résolution du nom et le certificat HTTPS. Testez ensuite depuis un téléphone
en 4G, Wi-Fi coupé : `https://tm.mon-club.fr`.

## Déplacer le Pi

Rien à refaire : le tunnel se reconnecte tout seul depuis n'importe quelle
connexion (autre box, partage de connexion, 4G). Vérifiez avec `--check`.

## Dépannage

| Symptôme | Cause probable | Solution |
|---|---|---|
| « Cannot determine default origin certificate path » | autorisation Cloudflare pas faite | relancer `sudo cloudflared tunnel login` |
| Erreur 1033 sur le site | le tunnel ne tourne pas | `sudo systemctl status cloudflared`, puis `journalctl -u cloudflared -n 50` |
| Erreur 502 / 503 | l'application ne répond pas | `sudo systemctl status tm-activation` |
| « route DNS non créée » | le nom existe déjà dans la zone | supprimer l'ancien enregistrement dans le DNS Cloudflare, puis relancer |
| Le nom ne répond pas | domaine pas (encore) géré par Cloudflare | vérifier les serveurs DNS chez le registrar |

## Revenir à une publication par la box

```bash
sudo systemctl disable --now cloudflared
sudo /opt/tm-activation/install.sh --domain tm.mon-club.fr --email vous@exemple.fr
```

Pensez à remplacer, dans le DNS Cloudflare, l'enregistrement du tunnel par un
enregistrement A vers l'adresse publique de la box.
