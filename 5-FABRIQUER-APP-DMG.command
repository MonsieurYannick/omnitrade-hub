#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
#  OmniTrade Hub — FABRICATION DE L'INSTALLATEUR macOS (.app + .dmg)
# ---------------------------------------------------------------------------
#  À lancer sur VOTRE Mac (celui qui a déjà Python). Produit un VRAI
#  installateur macOS : OmniTradeHub.app que l'utilisateur glisse dans son
#  dossier Applications, emballé dans un .dmg prêt à distribuer.
#
#  Résultat : OmniTradeHub-macOS.dmg
# ═══════════════════════════════════════════════════════════════════════════

SELF="$0"
case "$SELF" in */*) SELFDIR="${SELF%/*}" ;; *) SELFDIR="." ;; esac
cd "$SELFDIR" || { echo "Dossier inaccessible"; exit 1; }
export PATH="$PATH:/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin"

command -v clear >/dev/null && clear
echo "══════════════════════════════════════════════════════════════"
echo "   OmniTrade Hub — Fabrication du .app et du .dmg (macOS)"
echo "══════════════════════════════════════════════════════════════"
echo

die(){ echo; echo "[!] $1"; echo; read -r -p "Entrée pour fermer…"; exit 1; }

[[ -f "9-moteur-de-donnees.py" ]] || die "9-moteur-de-donnees.py introuvable.
    Placez ce script dans le MÊME dossier que 9-moteur-de-donnees.py."

# ── Fichier source : le HTML de production (JAMAIS les copies de travail) ─
#  Le moteur charge EN DUR "omnitrade-v21.html" (ligne ~9368 de
#  9-moteur-de-donnees.py) : on embarquera donc le fichier SANS alias sous ce
#  nom exact, quel que soit le numéro de version réellement choisi.
pick_app(){
  local best="" bestn=-1 f n
  for f in omnitrade-v*.html; do
    [[ -f "$f" ]] || continue
    case "$f" in
      *pre-refactor*|*backup*|*old*|*copie*|*sauvegarde*|*ancien*) continue ;;
    esac
    n=$(printf '%s' "$f" | sed -E 's/^omnitrade-v([0-9]+).*/\1/')
    [[ "$n" =~ ^[0-9]+$ ]] || continue
    if (( n > bestn )); then bestn=$n; best="$f"; fi
  done
  [[ -n "$best" ]] || best=$(ls -t omnitrade-*.html 2>/dev/null | head -1)
  printf '%s' "$best"
}
APP="$(pick_app)"
[[ -n "$APP" && -f "$APP" ]] || die "Aucun fichier omnitrade-*.html trouvé dans ce dossier."
#  Le fichier réellement chargé par le moteur (nom durci dans le moteur) :
HTML_NAME="omnitrade-v21.html"

# ── Python disponible ─────────────────────────────────────────────────────
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
echo "  Machine: $ARCH"
echo "  App    : $APP"
echo

# ── 1. Compilation du moteur (PyInstaller, HTML EMBARQUÉ) ─────────────────
#  L'HTML, le noyau de licence et la clé publique sont INCLUS DANS le binaire
#  (--add-data ...:.) : le moteur les retrouve via sys._MEIPASS quel que soit
#  l'emplacement de l'app. C'est ce qui rend le .app déplaçable partout.
echo "→ Compilation du moteur (2 à 4 minutes, patientez)…"
rm -rf build dist .venv-build .venv-app __pycache__ 2>/dev/null
find . -name "__pycache__" -type d -prune -exec rm -rf {} + 2>/dev/null
[[ -f "9-licence.py" ]] || die "9-licence.py introuvable (noyau de licence indispensable)."

"$PY" -m venv .venv-build 2>/dev/null || die "Création de l'environnement impossible."
# shellcheck disable=SC1091
source .venv-build/bin/activate
python -m pip install --upgrade pip wheel >/dev/null 2>&1
python -m pip install "pyinstaller>=6.0" flask flask-cors certifi >/dev/null 2>&1 \
  || die "Téléchargement impossible. Vérifiez votre connexion Internet."

python -m PyInstaller --clean --noconfirm \
  --name OmniTradeBridge --noupx \
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
  --add-data "$APP:." \
  --add-data "9-licence.py:." \
  --add-data "public_key.txt:." \
  9-moteur-de-donnees.py >/tmp/zt_build.log 2>&1 \
  || { echo; tail -25 /tmp/zt_build.log; die "Échec de la compilation (voir ci-dessus)."; }
deactivate 2>/dev/null

BIN_SRC="dist/OmniTradeBridge/OmniTradeBridge"
[[ -f "$BIN_SRC" ]] || die "Le binaire n'a pas été produit. Voir /tmp/zt_build.log"
rm -rf build .venv-build
echo "   ✓ binaire produit ($ARCH)"

# ── 2. Vérification RÉELLE du binaire ─────────────────────────────────────
echo "→ Vérification du binaire…"
if ! env -i HOME="$HOME" "$PWD/$BIN_SRC" --list-dirs --no-keep-open >/dev/null 2>&1; then
  die "Le binaire compilé ne démarre pas. Voir /tmp/zt_build.log"
fi
if ! env -i HOME="$HOME" "$PWD/$BIN_SRC" --selftest-ssl --no-keep-open >/tmp/zt_ssl.log 2>&1; then
  echo "   ⚠️  HTTPS échoue dans le binaire :"
  sed -n "1,4p" /tmp/zt_ssl.log
  die "Certificats SSL absents du binaire."
fi
echo "   ✓ le binaire fonctionne (SSL inclus)"

# ── 3. Assemblage de OmniTradeHub.app ─────────────────────────────────────
APPBUNDLE="OmniTradeHub.app"
RES="$APPBUNDLE/Contents/Resources"
echo "→ Assemblage de $APPBUNDLE…"
rm -rf "$APPBUNDLE" staging
mkdir -p "$APPBUNDLE/Contents/MacOS" "$RES"

# Le dossier compilé (executeur + _internal) vit dans Resources.
cp -R dist/OmniTradeBridge "$RES/OmniTradeBridge"
chmod -R +x "$RES/OmniTradeBridge"

# Normalisation : le moteur lit "_internal/<HTML_NAME>" (nom durci ~l.9368).
# On y place une copie plate en plus de celle embarquée, pour couvrir toute
# évolution future du nom de version.
cp -f "$APP" "$RES/OmniTradeBridge/_internal/$HTML_NAME"

# Fichiers utiles au trader, visibles dans Resources (Bonus : l'ancien
# moteur, la licence et la clé y sont aussi — le moteur sait les chercher).
for f in "$APP" 9-moteur-de-donnees.py 9-licence.py public_key.txt 2-OmniTradeExport.mq5; do
  [[ -f "$f" ]] && cp -f "$f" "$RES/"
done
[[ -f "OmniTrade Hub Bridge.app" ]] && cp -R "OmniTrade Hub Bridge.app" "$RES/" 2>/dev/null
[[ -f "Lancer OmniTrade Hub Bridge.command" ]] && \
  cp -f "Lancer OmniTrade Hub Bridge.command" "$RES/" 2>/dev/null
[[ -f "0-LISEZ-MOI.txt" ]] && cp -f "0-LISEZ-MOI.txt" "$RES/" 2>/dev/null

# ── Info.plist ────────────────────────────────────────────────────────────
VERSION=$(printf '%s' "$APP" | sed -E 's/^omnitrade-v([0-9]+).*/\1/')
[[ "$VERSION" =~ ^[0-9]+$ ]] || VERSION="887"
cat > "$APPBUNDLE/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleDevelopmentRegion</key><string>fr</string>
  <key>CFBundleExecutable</key><string>OmniTradeHub</string>
  <key>CFBundleIdentifier</key><string>com.omnitrade.hub</string>
  <key>CFBundleInfoDictionaryVersion</key><string>6.0</string>
  <key>CFBundleName</key><string>OmniTrade Hub</string>
  <key>CFBundleDisplayName</key><string>OmniTrade Hub</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>$VERSION</string>
  <key>CFBundleVersion</key><string>$VERSION</string>
  <key>LSMinimumSystemVersion</key><string>12.0</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>LSUIElement</key><false/>
</dict>
</plist>
EOF
printf 'APPL????' > "$APPBUNDLE/Contents/PkgInfo"

# ── Launcher : ouvre le navigateur et maintient le moteur en vie ──────────
cat > "$APPBUNDLE/Contents/MacOS/OmniTradeHub" <<'EOF'
#!/bin/bash
# OmniTrade Hub — Launcher du .app.
SELF="$(cd "$(dirname "$0")" && pwd)"
RES="$(cd "$SELF/../Resources" && pwd)"
cd "$RES" || exit 1
export PATH="$PATH:/usr/bin:/bin:/usr/sbin:/sbin:/usr/local/bin:/opt/homebrew/bin"

LOGDIR="$HOME/Library/Logs"; [[ -d "$LOGDIR" ]] || LOGDIR="$HOME"
LOGF="$LOGDIR/OmniTradeHub.log"; : > "$LOGF" 2>/dev/null || LOGF="/tmp/OmniTradeHub.log"

BIN="$RES/OmniTradeBridge/OmniTradeBridge"
chmod +x "$BIN" 2>/dev/null
{
  echo "== démarrage $(date) =="
  # Port et clé : même mécanique que le lanceur 1-START-MAC.command
  PORT="8765"
  TOKEN=$( "$BIN" --show-token --no-keep-open 2>/dev/null | tail -1 )
  case "$TOKEN" in *" "*|*[Ff]lask*) TOKEN="ZELLA_TOKEN" ;; esac
  [[ -n "$TOKEN" ]] || TOKEN="ZELLA_TOKEN"

  PIDS=$(lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t 2>/dev/null || true)
  if [[ -n "$PIDS" ]]; then
    kill $PIDS 2>/dev/null; sleep 1
    kill -9 $PIDS 2>/dev/null || true
  fi

  "$BIN" --host 127.0.0.1 --port "$PORT" --token "$TOKEN" --no-keep-open \
     >>"$LOGF" 2>&1 &
  ENGINE_PID=$!
  trap 'kill $ENGINE_PID 2>/dev/null' EXIT INT TERM

  for i in $(seq 1 40); do
    sleep 0.5
    curl -s -o /dev/null -m 2 "http://127.0.0.1:$PORT/api/ping?token=$TOKEN" \
      && break
  done
  open "http://127.0.0.1:$PORT/"
  wait $ENGINE_PID 2>/dev/null
} >>"$LOGF" 2>&1
EOF
chmod +x "$APPBUNDLE/Contents/MacOS/OmniTradeHub"

# ── 4. Signature Gatekeeper (ad-hoc) + quarantaine ────────────────────────
echo "→ Signature…"
codesign --force --deep --sign - "$APPBUNDLE" 2>/dev/null \
  && echo "   ✓ app signée" || echo "   (signature ignorée)"
xattr -dr com.apple.quarantine "$APPBUNDLE" 2>/dev/null

# ── 5. Test réel du .app : moteur lancé DIRECTEMENT (pas via open) ───────
#     Après l'assemblage, on lance le binaire installé, on vérifie que
#     - le moteur répond (ping),
#     - la page "/" est servie ET contient des marqueurs de la version
#       de production (licCodeActivate), preuve que le bon HTML est embarqué.
echo "→ Test réel du .app (moteur + HTML embarqué)…"
PIDS=$(lsof -nP -iTCP:8765 -sTCP:LISTEN -t 2>/dev/null || true)
[[ -n "$PIDS" ]] && kill $PIDS 2>/dev/null; sleep 1
BIN_TEST="$APPBUNDLE/Contents/Resources/OmniTradeBridge/OmniTradeBridge"
"$BIN_TEST" --host 127.0.0.1 --port 8765 --token ZT_TEST --no-keep-open \
  >/tmp/zt_apptest.log 2>&1 &
ENGINE_TEST=$!
sleep 4
PONG=$(curl -s -o /dev/null -w "%{http_code}" -m 2 \
  "http://127.0.0.1:8765/api/ping?token=ZT_TEST")
PAGE=$(curl -s -m 2 "http://127.0.0.1:8765/" )
CODE=$(curl -s -o /dev/null -w "%{http_code}" -m 2 "http://127.0.0.1:8765/")
[[ "$PONG" == "200" ]] \
  && echo "   ✓ le moteur du .app répond (token OK : $PONG)" \
  || echo "   ⚠️  ping du moteur : $PONG"
if [[ "$CODE" == "200" && "$PAGE" == *"licCodeActivate"* ]]; then
  echo "   ✓ la page '/' est la version de PRODUCTION (code retour $CODE)"
elif [[ "$CODE" == "200" ]]; then
  echo "   ⚠️  '/' répond ($CODE) mais SANS marqueur licCodeActivate →"
  echo "       l'HTML embarqué n'est peut-être pas omnitrade-v21.html."
else
  echo "   ❌ '/' renvoie $CODE (HTML introuvable). Voir /tmp/zt_apptest.log"
fi
kill $ENGINE_TEST 2>/dev/null; sleep 1
kill -9 $ENGINE_TEST 2>/dev/null || true

# ── 6. Création du .dmg ───────────────────────────────────────────────────
echo "→ Création du .dmg…"
mkdir -p staging
cp -R "$APPBUNDLE" staging/
ln -sf /Applications staging/Applications
DMG="OmniTradeHub-macOS.dmg"
rm -f "$DMG"
if command -v hdiutil >/dev/null; then
  hdiutil create -volname "OmniTradeHub" -srcfolder staging \
    -ov -format UDZO "$DMG" >/tmp/zt_dmg.log 2>&1 \
    || { echo; tail -5 /tmp/zt_dmg.log; die "Échec de la création du .dmg."; }
else
  die "hdiutil introuvable (macOS requis pour créer un .dmg)."
fi

SIZE=$(du -sh "$DMG" 2>/dev/null | cut -f1)
echo
echo "══════════════════════════════════════════════════════════════"
echo "  ✅ TERMINÉ"
echo
echo "     Installateur à distribuer :"
echo "       $DMG   ($SIZE)"
echo
echo "     VOS clients (macOS) n'auront QU'À :"
echo "       1. Ouvrir le .dmg"
echo "       2. Glisser « OmniTrade Hub » sur « Applications »"
echo "       3. Premier lancement : clic-droit → Ouvrir"
echo
echo "     Aucun Python, aucun Xcode, aucune installation."
echo "══════════════════════════════════════════════════════════════"
echo
rm -rf staging dist build
read -r -p "Appuyez sur Entrée pour fermer…"