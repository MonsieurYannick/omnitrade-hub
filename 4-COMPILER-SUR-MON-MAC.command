#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
#  OmniTrade Hub — COMPILATION DU PONT SUR VOTRE MAC
# ---------------------------------------------------------------------------
#  À lancer UNE SEULE FOIS, sur VOTRE Mac (celui qui a déjà Python).
#  Produit un paquet que vos utilisateurs pourront lancer SANS Python,
#  SANS Xcode, sans aucune installation.
#
#  Double-cliquez simplement ce fichier.
# ═══════════════════════════════════════════════════════════════════════════

SELF="$0"
case "$SELF" in */*) SELFDIR="${SELF%/*}" ;; *) SELFDIR="." ;; esac
cd "$SELFDIR" || { echo "Dossier inaccessible"; exit 1; }
export PATH="$PATH:/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"

command -v clear >/dev/null && clear
echo "══════════════════════════════════════════════════════════════"
echo "   OmniTrade Hub — Compilation du pont MT5 (macOS)"
echo "══════════════════════════════════════════════════════════════"
echo

die(){ echo; echo "[!] $1"; echo; read -r -p "Entrée pour fermer…"; exit 1; }

# ── 1. Vérifications ───────────────────────────────────────────────────────
[[ -f "9-moteur-de-donnees.py" ]] || die "9-moteur-de-donnees.py introuvable.
    Placez ce script dans le MÊME dossier que 9-moteur-de-donnees.py."

# ── Fichier source : LE PLUS RÉCENT, jamais un nom codé en dur ─────────────
# Bug corrigé : le script embarquait « zellatrack-v14.html » écrit en dur,
# donc une version périmée partait chez les utilisateurs. On sélectionne
# désormais le HTML le plus récemment modifié du dossier de travail.
# Fichier source : la version la PLUS ÉLEVÉE (tri numérique), avec la date
# comme départage. Se fier à la seule date est fragile : une copie ou une
# restauration aligne les horodatages et le tri devient arbitraire.
pick_app(){
  local best="" bestn=-1 f n
  # Le produit s'appelle désormais « omnitrade-vNN.html ». On accepte encore
  # les anciens « zellatrack-vNN.html » pour ne rien casser, mais un fichier
  # OmniTrade l'emporte TOUJOURS sur un ZellaTrack de numéro équivalent.
  for f in omnitrade-v*.html; do
    [[ -f "$f" ]] || continue
    n=$(printf '%s' "$f" | sed -E 's/^omnitrade-v([0-9]+).*/\1/')
    [[ "$n" =~ ^[0-9]+$ ]] || continue
    if (( n > bestn )); then bestn=$n; best="$f"; fi
  done
  if [[ -z "$best" ]]; then
    for f in zellatrack-v*.html; do
      [[ -f "$f" ]] || continue
      n=$(printf '%s' "$f" | sed -E 's/^zellatrack-v([0-9]+).*/\1/')
      [[ "$n" =~ ^[0-9]+$ ]] || continue
      if (( n > bestn )); then bestn=$n; best="$f"; fi
    done
  fi
  [[ -n "$best" ]] || best=$(ls -t omnitrade-*.html zellatrack-*.html 2>/dev/null | head -1)
  printf '%s' "$best"
}
APP="${ZT_APP:-$(pick_app)}"
[[ -n "$APP" && -f "$APP" ]] || die "Aucun fichier omnitrade-*.html trouvé
    dans ce dossier. Placez le script à côté de l'application."

# ── Purge totale des caches et des anciens builds ──────────────────────────
echo "→ Purge des caches et des anciens builds…"
rm -rf build dist .venv-build __pycache__ *.spec \
       OmniTradeHub-macOS-PRET OmniTradeHub-macOS-PRET.zip \
       ZellaTrack-macOS-PRET ZellaTrack-macOS-PRET.zip 2>/dev/null
find . -name "__pycache__" -type d -prune -exec rm -rf {} + 2>/dev/null
find . -name "*.pyc" -delete 2>/dev/null
find . -name ".DS_Store" -delete 2>/dev/null
# Caches PyInstaller hors du dossier de travail : ils font resurgir d'anciens
# fichiers embarqués d'une compilation à l'autre.
rm -rf "$HOME/Library/Application Support/pyinstaller" 2>/dev/null
rm -rf "$HOME/Library/Caches/pyinstaller" 2>/dev/null
echo "   ✓ caches supprimés (y compris le cache PyInstaller du système)"

# Python réel (le stub Xcode est écarté par le test d'exécution)
# Le noyau de licence est indispensable : sans lui, le pont refuserait
# toute clé et l'application serait inutilisable pour vos clients.
[[ -f "9-licence.py" ]] || die "9-licence.py est introuvable dans ce dossier.
    Ce fichier contient la vérification des licences : sans lui, aucune clé
    ne pourra être activée. Placez-le à côté de ce script."

PY=""
CANDS=()
for d in /opt/homebrew/bin /usr/local/bin /opt/local/bin; do
  [[ -x "$d/python3" ]] && CANDS+=("$d/python3")
done
for f in /Library/Frameworks/Python.framework/Versions/*/bin/python3; do
  [[ -x "$f" ]] && CANDS+=("$f")
done
w="$(command -v python3 2>/dev/null)"; [[ -n "$w" ]] && CANDS+=("$w")
[[ -x /usr/bin/python3 ]] && CANDS+=("/usr/bin/python3")
for c in "${CANDS[@]}"; do
  [[ "$("$c" -c 'print("ZTOK")' 2>/dev/null)" == "ZTOK" ]] && { PY="$c"; break; }
done
[[ -n "$PY" ]] || die "Aucun Python trouvé sur ce Mac.
    Installez-le : https://www.python.org/downloads/macos/"

ARCH="$(uname -m)"
echo "  Python : $PY"
echo "  Version: $("$PY" -V 2>&1)"
echo "  Machine: $ARCH"
echo

# ── 2. Environnement isolé ─────────────────────────────────────────────────
echo "→ Préparation de l'environnement (1re fois : ~1 minute)…"
rm -rf .venv-build
"$PY" -m venv .venv-build 2>/dev/null || die "Création de l'environnement impossible."
# shellcheck disable=SC1091
source .venv-build/bin/activate

python -m pip install --upgrade pip wheel >/dev/null 2>&1
echo "→ Téléchargement de PyInstaller et Flask…"
# certifi est INDISPENSABLE : sans lui, un binaire compilé n'a aucun
# magasin de certificats et TOUTES les requêtes HTTPS échouent
# (CERTIFICATE_VERIFY_FAILED). Le Market Hub reste alors vide alors que la
# connexion Internet fonctionne parfaitement — panne constatée sur .app.
python -m pip install "pyinstaller>=6.0" flask flask-cors certifi >/dev/null 2>&1 \
  || die "Téléchargement impossible. Vérifiez votre connexion Internet."

# ── 3. Compilation ─────────────────────────────────────────────────────────
echo "→ Compilation en cours (2 à 4 minutes, soyez patient)…"
rm -rf build dist
python -m PyInstaller --clean --noconfirm \
  --name OmniTradeBridge \
  --console \
  --noupx \
  --hidden-import flask --hidden-import flask_cors \
  --hidden-import werkzeug --hidden-import werkzeug.serving \
  --hidden-import jinja2 --hidden-import itsdangerous \
  --hidden-import click --hidden-import blinker \
  --hidden-import license_core \
  --hidden-import certifi --collect-data certifi \
  --hidden-import concurrent.futures --hidden-import concurrent.futures.thread \
  --hidden-import ssl --hidden-import _ssl \
  --exclude-module tkinter --exclude-module numpy --exclude-module pandas \
  --exclude-module matplotlib --exclude-module PIL --exclude-module pytest \
  9-moteur-de-donnees.py >/tmp/zt_build.log 2>&1 \
  || { echo; tail -25 /tmp/zt_build.log; die "Échec de la compilation (voir ci-dessus)."; }

[[ -f "dist/OmniTradeBridge/OmniTradeBridge" ]] \
  || die "Le binaire n'a pas été produit. Voir /tmp/zt_build.log"

# ── 4. Vérification RÉELLE du binaire ──────────────────────────────────────
echo "→ Vérification du binaire compilé…"
if ! env -i HOME="$HOME" "./dist/OmniTradeBridge/OmniTradeBridge" \
        --list-dirs --no-keep-open >/dev/null 2>&1; then
  die "Le binaire compilé ne démarre pas. Voir /tmp/zt_build.log"
fi
echo "   ✓ le binaire fonctionne sans Python"

# Vérification SSL : le binaire doit pouvoir joindre un site en HTTPS.
# C'est LE test qui manquait : un binaire qui démarre mais dont toutes les
# requêtes sécurisées échouent donne un Market Hub désespérément vide.
echo "→ Vérification des certificats SSL du binaire…"
if env -i HOME="$HOME" "./dist/OmniTradeBridge/OmniTradeBridge" \
        --selftest-ssl --no-keep-open >/tmp/zt_ssl.log 2>&1; then
  echo "   ✓ requêtes HTTPS opérationnelles ($(head -1 /tmp/zt_ssl.log))"
else
  echo "   ⚠️  Les requêtes HTTPS échouent dans le binaire :"
  sed -n "1,6p" /tmp/zt_ssl.log
  echo "   Le calendrier et les actualités resteraient vides."
  die "Certificats SSL absents du binaire. Relancez après : python -m pip install certifi"
fi

# ── 5. Assemblage du paquet de distribution ────────────────────────────────
echo "→ Assemblage du paquet…"
OUT="OmniTradeHub-macOS-PRET"
rm -rf "$OUT"; mkdir -p "$OUT"

cp -R "dist/OmniTradeBridge" "$OUT/bin"
# Le LANCEUR est indispensable : sans lui l'utilisateur ne peut rien démarrer.
for f in "$APP" 9-moteur-de-donnees.py 9-licence.py 2-OmniTradeExport.mq5 0-LISEZ-MOI.txt \
         public_key.txt \
         "1-START-MAC.command" "Lancer OmniTrade Hub Bridge.command"; do
  [[ -f "$f" ]] && cp "$f" "$OUT/"
done

# Déblocage Gatekeeper : c'est LE fichier qui évite au client le message
# « ... est endommagé » lorsqu'il reçoit le paquet par Internet.
for d in "0-DEBLOQUER-SI-BLOQUE.command" "DEBLOQUER-SUR-MAC.command"; do
  [[ -f "$d" ]] && { cp "$d" "$OUT/0-DEBLOQUER-SI-BLOQUE.command"; break; }
done
chmod +x "$OUT/0-DEBLOQUER-SI-BLOQUE.command" 2>/dev/null

# Contrôle : sans le noyau de licence à la racine, le binaire compilé
# répondrait « reason: module » et verrouillerait toute l'application.
if ! ls "$OUT"/9-licence.py "$OUT"/9-licence.py >/dev/null 2>&1; then
  die "Le noyau de licence n'a pas été copié dans le paquet.
    L'application se verrouillerait chez vos clients."
fi

# Contrôle bloquant : on refuse de livrer une archive sans lanceur.
if [[ ! -f "$OUT/1-START-MAC.command" ]]; then
  die "1-START-MAC.command introuvable dans ce dossier.
    C'est le fichier que vos utilisateurs double-cliquent : sans lui,
    l'archive serait inutilisable. Placez-le à côté de ce script."
fi
chmod +x "$OUT/1-START-MAC.command"

# Contrôle d'intégrité du lanceur. Une ancienne version contenait le nom du
# fichier HTML écrit EN DUR : après le changement de nom, la page ne s'ouvrait
# plus (« zellatrack-v20.html introuvable »). On refuse catégoriquement de
# livrer une archive contenant encore ce défaut.
if grep -qE '^APP="(zellatrack|omnitrade)-v[0-9]+\.html"' "$OUT/1-START-MAC.command"; then
  die "Le fichier 1-START-MAC.command de ce dossier est PÉRIMÉ :
    il contient encore un nom de fichier écrit en dur, et vos utilisateurs
    verraient l'erreur « introuvable dans ce dossier ».
    Remplacez-le par la version fournie avec cette mise à jour."
fi
if ! grep -q 'pick_app' "$OUT/1-START-MAC.command"; then
  die "1-START-MAC.command ne contient pas la détection automatique du fichier
    de l'application. Utilisez la version fournie avec cette mise à jour."
fi
# Le lanceur doit être syntaxiquement valide, sinon rien ne démarrera.
if command -v bash >/dev/null && ! bash -n "$OUT/1-START-MAC.command" 2>/dev/null; then
  die "1-START-MAC.command comporte une erreur de syntaxe : compilation annulée."
fi
echo "   ✓ lanceur 1-START-MAC.command vérifié (détection auto, syntaxe valide)"
if [[ -d "OmniTrade Hub Bridge.app" ]]; then
  cp -R "OmniTrade Hub Bridge.app" "$OUT/"
  # Resources reconstruit : sinon un ancien HTML du bundle repartirait.
  RES="$OUT/OmniTrade Hub Bridge.app/Contents/Resources"
  # On purge les DEUX familles de noms : un ancien « zellatrack-vNN.html »
  # oublié dans le bundle repartirait sinon chez les utilisateurs.
  rm -f "$RES"/omnitrade-*.html "$RES"/zellatrack-*.html 2>/dev/null
  mkdir -p "$RES"
  cp "$APP" 9-moteur-de-donnees.py 9-licence.py 2-OmniTradeExport.mq5 "$RES/" 2>/dev/null || true
  [[ -f "Lancer OmniTrade Hub Bridge.command" ]] && \
    cp "Lancer OmniTrade Hub Bridge.command" "$RES/" 2>/dev/null || true
fi

# Le générateur de licences et la clé privée ne doivent JAMAIS partir chez
# un client : leur présence permettrait de fabriquer des licences gratuites.
rm -rf "$OUT/oth_admin" 2>/dev/null
rm -f  "$OUT/GENERATEUR-DE-LICENCES.py" "$OUT/private_key.txt" 2>/dev/null
if find "$OUT" \( -name 'private_key*' -o -name 'GENERATEUR*' -o -name 'oth_admin' \) \
     2>/dev/null | grep -q .; then
  die "Un secret de licence a été détecté dans le paquet. Compilation annulée."
fi

# Purge de sécurité : aucune AUTRE version ne doit subsister dans le paquet.
find "$OUT" \( -name 'omnitrade-*.html' -o -name 'zellatrack-*.html' \) \
     ! -name "$(basename "$APP")" -delete 2>/dev/null || true

chmod +x "$OUT/Lancer OmniTrade Hub Bridge.command" 2>/dev/null
chmod +x "$OUT/OmniTrade Hub Bridge.app/Contents/MacOS/OmniTradeBridge" 2>/dev/null
chmod -R +x "$OUT/bin" 2>/dev/null

# Signature ad-hoc : sans elle, Gatekeeper tue le binaire sur Apple Silicon
echo "→ Signature (Gatekeeper)…"
codesign --force --deep --sign - "$OUT/bin/OmniTradeBridge" 2>/dev/null \
  && echo "   ✓ binaire signé" || echo "   (signature ignorée)"
[[ -d "$OUT/OmniTrade Hub Bridge.app" ]] && \
  codesign --force --deep --sign - "$OUT/OmniTrade Hub Bridge.app" 2>/dev/null
xattr -dr com.apple.quarantine "$OUT" 2>/dev/null

# ── 6. Archive finale ──────────────────────────────────────────────────────
echo "→ Création de l'archive ZIP…"
rm -f OmniTradeHub-macOS-PRET.zip
# ditto préserve permissions ET métadonnées macOS, mieux que zip
if command -v ditto >/dev/null; then
  ditto -c -k --sequesterRsrc --keepParent "$OUT" "OmniTradeHub-macOS-PRET.zip"
else
  ( cd "$OUT" && zip -qry "../OmniTradeHub-macOS-PRET.zip" . )
fi

deactivate 2>/dev/null
rm -rf build .venv-build

SIZE=$(du -sh "OmniTradeHub-macOS-PRET.zip" 2>/dev/null | cut -f1)
echo
echo "══════════════════════════════════════════════════════════════"
echo "  ✅ TERMINÉ"
echo
echo "     Fichier à distribuer :"
echo "       OmniTradeHub-macOS-PRET.zip   ($SIZE)"
echo
echo "     Vos utilisateurs n'auront QU'À :"
echo "       1. Décompresser le ZIP"
echo "       2. Double-cliquer « 1-START-MAC.command »"
echo "          (lance le moteur ET ouvre $APP)"
echo
echo "     Aucun Python, aucun Xcode, aucune installation."
echo "══════════════════════════════════════════════════════════════"
echo
echo "  ⚠️  Architecture compilée : $ARCH"
if [[ "$ARCH" == "arm64" ]]; then
  echo "      Ce binaire fonctionne sur les Mac Apple Silicon (M1→M4)."
  echo "      Sur un Mac Intel, il ne démarrera PAS."
else
  echo "      Ce binaire fonctionne sur Mac Intel, et sur Apple Silicon"
  echo "      via Rosetta 2 (proposé automatiquement par macOS)."
fi
echo
read -r -p "Appuyez sur Entrée pour fermer…"
