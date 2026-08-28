#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
#  OmniTrade Hub — FABRICATION DE L'INSTALLATEUR macOS EN CI (GitHub Actions)
# ---------------------------------------------------------------------------
#  Variante NON interactive de « 5-FABRIQUER-APP-DMG.command » destinée au
#  runner macOS de GitHub Actions : aucun prompt, sortie sur stdout, échec
#  = code de retour non nul.
#
#  Résultat : OmniTradeHub-macOS.dmg (+ OmniTradeHub-macOS-PRET.zip)
#  Version : env VER si fourni, sinon le numéro du tag vX.Y.Z (env GITHUB_REF),
#            sinon le numéro du fichier omnitrade-v<NN>.html.
# ═══════════════════════════════════════════════════════════════════════════
set -euo pipefail
cd "$(dirname "$0")"

echo "══════════════════════════════════════════════════════════════"
echo "   OmniTrade Hub — Fabrication du .dmg en CI (macOS)"
echo "══════════════════════════════════════════════════════════════"

# ── Version ─────────────────────────────────────────────────────────────────
VER_TAG=""
if [[ "${GITHUB_REF:-}" =~ ^refs/tags/v(.+)$ ]]; then VER_TAG="${BASH_REMATCH[1]}"; fi
VERSION="${VER:-}"
if [[ -z "$VERSION" && -n "$VER_TAG" ]]; then VERSION="$VER_TAG"; fi
if [[ -z "$VERSION" ]]; then VERSION="0"; fi
echo "  Version : $VERSION"

# ── Fichier source : le HTML de production (jamais les copies de travail) ──
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
  printf '%s' "${best:-}"
}
APP="$(pick_app)"
[[ -n "$APP" && -f "$APP" ]] || { echo "[!] Aucun omnitrade-v*.html trouvé"; exit 1; }
HTML_NAME="omnitrade-v21.html"
echo "  App    : $APP"

# ── Python −─────────────────────────────────────────────────────────────────
command -v python3 >/dev/null || { echo "[!] python3 introuvable"; exit 1; }
python3 -c "import sys; assert sys.version_info >= (3, 8), 'Python >= 3.8 requis'" 2>/dev/null \
  || { echo "[!] Python trop ancien"; exit 1; }

# ── 1. Compilation du moteur (PyInstaller, HTML EMBARQUÉ) ────────────────────
echo "→ Compilation du moteur (2 à 4 minutes, patientez)…"
rm -rf build dist .venv-build .venv-app staging 2>/dev/null
python3 -m venv .venv-build
# shellcheck disable=SC1091
source .venv-build/bin/activate
python -m pip install --upgrade pip wheel >/dev/null
python -m pip install "pyinstaller>=6.0" flask flask-cors certifi >/dev/null

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
  9-moteur-de-donnees.py 2>&1 | tail -5
deactivate 2>/dev/null

BIN_SRC="dist/OmniTradeBridge/OmniTradeBridge"
[[ -f "$BIN_SRC" ]] || { echo "[!] Binaire absent"; exit 1; }
rm -rf build .venv-build

# ── 2. Vérification RÉELLE du binaire ────────────────────────────────────────
echo "→ Vérification du binaire…"
env -i HOME="$HOME" "$PWD/$BIN_SRC" --list-dirs --no-keep-open >/dev/null
env -i HOME="$HOME" "$PWD/$BIN_SRC" --selftest-ssl --no-keep-open >/dev/null 2>&1 \
  || { echo "[!] HTTPS (certifi) échoue dans le binaire"; exit 1; }
echo "   ✓ le binaire fonctionne (SSL inclus)"

# ── 3. Assemblage de OmniTradeHub.app ────────────────────────────────────────
APPBUNDLE="OmniTradeHub.app"
RES="$APPBUNDLE/Contents/Resources"
echo "→ Assemblage de ${APPBUNDLE}…"
rm -rf "$APPBUNDLE"
mkdir -p "$APPBUNDLE/Contents/MacOS" "$RES"

cp -R dist/OmniTradeBridge "$RES/OmniTradeBridge"
chmod -R +x "$RES/OmniTradeBridge"
cp -f "$APP" "$RES/OmniTradeBridge/_internal/$HTML_NAME" 2>/dev/null || true

for f in "$APP" 9-moteur-de-donnees.py 9-licence.py public_key.txt 2-OmniTradeExport.mq5 cb_intel_backend.py cb_intel_prompt.md; do
  [[ -f "$f" ]] && cp -f "$f" "$RES/"
done
[[ -f "0-LISEZ-MOI.txt" ]] && cp -f "0-LISEZ-MOI.txt" "$RES/" 2>/dev/null
[[ -f "OmniTradeHub.icns" ]] && cp -f "OmniTradeHub.icns" "$RES/" 2>/dev/null

cat > "$APPBUNDLE/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleDevelopmentRegion</key><string>fr</string>
  <key>CFBundleExecutable</key><string>OmniTradeHub</string>
  <key>CFBundleIconFile</key><string>OmniTradeHub</string>
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

cat > "$APPBUNDLE/Contents/MacOS/OmniTradeHub" <<'EOF'
#!/bin/bash
SELF="$(cd "$(dirname "$0")" && pwd)"
RES="$(cd "$SELF/../Resources" && pwd)"
cd "$RES" || exit 1
export PATH="$PATH:/usr/bin:/bin:/usr/sbin:/sbin:/usr/local/bin:/opt/homebrew/bin"
LOGDIR="$HOME/Library/Logs"; [[ -d "$LOGDIR" ]] || LOGDIR="$HOME"
LOGF="$LOGDIR/OmniTradeHub.log"; : > "$LOGF" 2>/dev/null || LOGF="/tmp/OmniTradeHub.log"
BIN="$RES/OmniTradeBridge/OmniTradeBridge"
chmod +x "$BIN" 2>/dev/null
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
EOF
chmod +x "$APPBUNDLE/Contents/MacOS/OmniTradeHub"

# ── 4. Signature Gatekeeper (ad-hoc) ─────────────────────────────────────────
echo "→ Signature…"
codesign --force --deep --sign - "$APPBUNDLE" 2>/dev/null \
  && echo "   ✓ app signée" || echo "   (signature ignorée)"
xattr -dr com.apple.quarantine "$APPBUNDLE" 2>/dev/null || true

# ── 5. Test réel du .app : moteur + HTML embarqué ────────────────────────────
echo "→ Test réel du .app (moteur + HTML embarqué)…"
PIDS=$(lsof -nP -iTCP:8765 -sTCP:LISTEN -t 2>/dev/null || true)
[[ -n "$PIDS" ]] && kill $PIDS 2>/dev/null; sleep 1
BIN_TEST="$APPBUNDLE/Contents/Resources/OmniTradeBridge/OmniTradeBridge"
"$BIN_TEST" --host 127.0.0.1 --port 8765 --token ZT_TEST --no-keep-open \
  >"/tmp/zt_apptest.log" 2>&1 &
ENGINE_TEST=$!
sleep 4
PONG=$(curl -s -o /dev/null -w "%{http_code}" -m 2 "http://127.0.0.1:8765/api/ping?token=ZT_TEST" || true)
CODE=$(curl -s -o /dev/null -w "%{http_code}" -m 2 "http://127.0.0.1:8765/" || true)
PAGE=$(curl -s -m 2 "http://127.0.0.1:8765/" || true)
[[ "$PONG" == "200" ]] && echo "   ✓ le moteur du .app répond ($PONG)" \
  || echo "   ⚠️  ping du moteur : $PONG"
if [[ "$CODE" == "200" && "$PAGE" == *"licCodeActivate"* ]]; then
  echo "   ✓ la page '/' est la version de PRODUCTION (code $CODE)"
elif [[ "$CODE" == "200" ]]; then
  echo "   ⚠️  '/' répond ($CODE) mais SANS marqueur licCodeActivate"
else
  echo "   ❌ '/' renvoie $CODE — voir /tmp/zt_apptest.log"
fi
kill $ENGINE_TEST 2>/dev/null || true; sleep 1
kill -9 $ENGINE_TEST 2>/dev/null || true
[[ "$PONG" == "200" && "$CODE" == "200" && "$PAGE" == *"licCodeActivate"* ]] \
  || { echo "[!] Le test du .app a échoué"; exit 1; }

# ── 6. Paquets : .app zipé + .dmg ────────────────────────────────────────────
echo "→ Paquetage…"
rm -f OmniTradeHub-macOS-PRET.zip OmniTradeHub-macOS.dmg
zip -qr OmniTradeHub-macOS-PRET.zip "$APPBUNDLE"
mkdir -p staging
cp -R "$APPBUNDLE" staging/
ln -sf /Applications staging/Applications
if command -v hdiutil >/dev/null; then
  hdiutil create -volname "OmniTradeHub" -srcfolder staging \
    -ov -format UDZO OmniTradeHub-macOS.dmg >/tmp/zt_dmg.log 2>&1 \
    || { tail -5 /tmp/zt_dmg.log; echo "[!] Échec hdiutil"; exit 1; }
else
  echo "[!] hdiutil introuvable"; exit 1
fi
rm -rf staging dist build

echo "══════════════════════════════════════════════════════════════"
echo "  ✅ TERMINÉ  (v$VERSION)  —  $(du -sh OmniTradeHub-macOS.dmg 2>/dev/null | cut -f1)"
echo "══════════════════════════════════════════════════════════════"