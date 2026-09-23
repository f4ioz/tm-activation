# Spécification de mise en page — certificat TM-activation

Page A4 paysage, 841,89 × 595,28 pt, origine en bas à gauche (reportlab).

## Couches
1. **Statique** (`decor.py`) : fond blanc, arc marine + double arc or (gauche), forme marine
   (haut droit), bande or dégradée (bas droit), micro, manipulateur, Yagi, étoiles, logo.
2. **Médaille** : position fixe (726, 485), rubans rouges ; contenu = rang + « er/e » + « SUR n ».
   Sans classement → médaille affichant « QSO ».
3. **Dynamique** (`render.py`) : tout le reste.

## Palette
| Rôle | Hex |
|---|---|
| Marine / marine foncé / gris-bleu | `#2B3547` / `#1D2533` / `#5A6478` |
| Or / or clair / or pâle | `#C9982E` / `#E9C46A` / `#F6E3A8` |
| Rouge / rouge foncé | `#B0202B` / `#7E131B` |
| Texte courant | `#4A4F5A` |
| Badges mode | SSB `#C9982E`, CW `#B0202B`, FT8/FT4/DIGI `#2C6E9C`, FM `#3B7D4F`, RTTY `#6A4C93`, autre `#5A6478` |

## Polices
Poppins (Regular, Medium, SemiBold, Bold, ExtraBold) ; Great Vibes pour le nom.

## Zones dynamiques (colonne de contenu x = 228, largeur 420)
| Élément | y (pt) | Police | Règle |
|---|---|---|---|
| Titre | 445 | Poppins ExtraBold 60 | défaut « CERTIFICAT » |
| Sous-titre | 415 | Poppins SemiBold 20 | réduit jusqu'à 12 pt pour tenir en 400 pt |
| Bandeau | 373–397 | SemiBold 10 blanc sur marine | texte fixe |
| Nom | 319 | Great Vibes 44 | réduit jusqu'à 26 pt ; absent → indicatif en ExtraBold 40 |
| Filet | 305 | — | |
| Indicatif + locator | 284 | ExtraBold 20 rouge + SemiBold 10,5 | centrés ensemble |
| Phrase d'attribution | 267 | Regular 9,5 | 2 lignes max, centrée |
| Tableau QSO | sous la phrase | en-tête 16 pt, lignes 13,5 pt | colonnes : date, UTC, bande, fréquence, mode (badge), RST env., RST reçu |
| Synthèse | 18 pt sous le tableau | Medium 8 + ExtraBold 11 | QSO, bandes, modes, points (rouge) |
| Pied | 56 / 50 / 37 | SemiBold 10,5 / filet / Medium 8,5 | gestionnaire (x 268), n° (x 585), date (x 735) — **pas de signature** |
| Mention | 14 | Regular 6 | optionnelle |

## Règles de données
- QSO triés par date/heure UTC ; bande déduite de la fréquence si absente.
- Fréquence affichée en MHz, 3 décimales, virgule décimale.
- Dates affichées JJ/MM/AAAA (entrée ISO AAAA-MM-JJ).
- Points = somme du barème par mode (`*` = défaut, 1 si absent).
- Suffixe de rang : 1 → « er », sinon « e ».

## Débordement
- Page 1 : 7 QSO maximum. Au-delà : 6 QSO + ligne « … et N autres QSO — journal complet en annexe ».
- Annexe(s) : 28 QSO par page, bandeau marine « JOURNAL DES CONTACTS · INDICATIF », pagination « annexe n/N ».

## Logo
Source PNG transparent carré ~600 px. Le moteur utilise `logo.jpg` (PNG aplati sur `#E9C46A`,
qualité 88) découpé en disque de diamètre 176 pt centré en (100, 318) : PDF ~190 Ko au lieu de ~900 Ko.
```python
from PIL import Image
im = Image.open("logo.png").convert("RGBA")
bg = Image.new("RGBA", im.size, (0xE9, 0xC4, 0x6A, 255)); bg.alpha_composite(im)
bg.convert("RGB").save("logo.jpg", quality=88)
```
