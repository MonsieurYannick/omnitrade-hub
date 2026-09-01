#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
#  OmniTrade Hub — VERSION PYTHON DÉFINITIVE (toutes plateformes)
#  Ne compile PAS : le moteur tourne en Python direct, comme l'ancien
#  1-START-MAC.command / 3-Lancer-le-moteur-seul.command. C'est la méthode
#  qui fonctionnait parfaitement.
#
#  Produit :
#   - macOS  : OmniTradeHub.app   (lanceur Python direct)
#   - Windows: OmniTradeHub-Lanceur.bat   (Python direct)
#
#  Usage : bash build-python-definitif.sh [version] [Intel|AppleSilicon]
# ═══════════════════════════════════════════════════════════════════════════
set -e
cd "$(dirname "$0")"
APP="omnitrade-v21.html"
VERSION="$1"
VARIANT="${2:-Intel}"
[[ -n "$VERSION" ]] || VERSION="$(sed -nE "s/.*version:'([0-9.]+)'.*/\1/p" "$APP" | head -1)"
VER_MAJOR="$(echo "$VERSION" | cut -d. -f1)"

FILES=("$APP" 9-moteur-de-donnees.py 9-licence.py public_key.txt \
       2-OmniTradeExport.mq5 cb_intel_backend.py cb_intel_prompt.md \
       favicon-48.png logo-1024.png OmniTradeHub.icns 0-LISEZ-MOI.txt)

echo "══════════════════════════════════════════════════════════"
echo "   OmniTrade Hub — version PYTHON définitive   (v$VERSION)"
echo "══════════════════════════════════════════════════════════"

# ═══════════════════════════════════════════════════════════════
#  macOS : OmniTradeHub.app
# ═══════════════════════════════════════════════════════════════
APPBUNDLE="OmniTradeHub.app"
RES="$APPBUNDLE/Contents/Resources"
rm -rf "$APPBUNDLE"
mkdir -p "$APPBUNDLE/Contents/MacOS" "$RES"
for f in "${FILES[@]}"; do [[ -f "$f" ]] && cp -f "$f" "$RES/"; done

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

# Lanceur macOS : détecte un Python réel, installe Flask, lance le moteur par
# dessus (premier plan) puis ouvre le navigateur — METHODE D'ORIGINE.
cat > "$APPBUNDLE/Contents/MacOS/OmniTradeHub" <<'EOF'
#!/bin/bash
SELF="$(cd "$(dirname "$0")" && pwd)"
RES="$(cd "$SELF/../Resources" && pwd)"
cd "$RES" || exit 1
export PATH="$PATH:/usr/bin:/bin:/usr/sbin:/sbin:/usr/local/bin:/opt/homebrew/bin"
LOGDIR="$HOME/Library/Logs"; [[ -d "$LOGDIR" ]] || LOGDIR="$HOME"
LOGF="$LOGDIR/OmniTradeHub.log"; : > "$LOGF" 2>/dev/null || LOGF="/tmp/OmniTradeHub.log"
PORT="8765"
HOST_BIND="127.0.0.1"

# ── Python réel détecté (le stub Xcode est écarté sans pop-up) ──────────────
PY=""
CANDS=()
for d in /opt/homebrew/bin /usr/local/bin /opt/local/bin; do [[ -x "$d/python3" ]] && CANDS+=("$d/python3"); done
for f in /Library/Frameworks/Python.framework/Versions/*/bin/python3; do [[ -x "$f" ]] && CANDS+=("$f"); done
for n in python3 python3.13 python3.12 python3.11 python3.10; do w="$(command -v "$n" 2>/dev/null)"; [[ -n "$w" && -x "$w" ]] && CANDS+=("$w"); done
[[ -x /usr/bin/python3 ]] && CANDS+=("/usr/bin/python3")
for c in "${CANDS[@]}"; do
  if [[ "$("$c" -c 'print("ZTOK")' 2>/dev/null)" == "ZTOK" ]]; then PY="$c"; break; fi
done
if [[ -z "$PY" ]]; then
  echo "[!] Python est nécessaire. Installez-le depuis https://www.python.org/downloads/macos/ (PAS les outils Xcode)." >> "$LOGF" 2>&1
  osascript -e 'display dialog "OmniTrade Hub nécessite Python.\n\nInstallez-le depuis https://www.python.org/downloads/macos/\n(PAS les outils de ligne de commande Xcode)." with title "OmniTrade Hub" buttons {"OK"} default button "OK" with icon caution' 2>/dev/null || true
  exit 1
fi

# ── Dépendances ─────────────────────────────────────────────────────────────
if ! "$PY" -c 'import flask, flask_cors' 2>/dev/null; then
  "$PY" -m pip install --quiet --user flask flask-cors 2>/dev/null \
    || "$PY" -m pip install --quiet flask flask-cors 2>/dev/null
fi

# ── Token ───────────────────────────────────────────────────────────────────
TOKEN=$("$PY" "$RES/9-moteur-de-donnees.py" --show-token --no-keep-open 2>/dev/null | tail -1)
case "$TOKEN" in *" "*|*[Ff]lask*) TOKEN="ZELLA_TOKEN" ;; esac
[[ -n "$TOKEN" ]] || TOKEN="ZELLA_TOKEN"

# ── Libération du port ──────────────────────────────────────────────────────
PIDS=$(lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t 2>/dev/null || true)
[[ -n "$PIDS" ]] && { kill $PIDS 2>/dev/null; sleep 1; kill -9 $PIDS 2>/dev/null || true; }

# ── Lancement du moteur en PREMIER PLAN (méthode d'origine) ────────────────
"$PY" "$RES/9-moteur-de-donnees.py" --host "$HOST_BIND" --port "$PORT" \
   --token "$TOKEN" --no-keep-open >>"$LOGF" 2>&1 &
ENGINE_PID=$!
trap 'kill $ENGINE_PID 2>/dev/null' EXIT INT TERM
for i in $(seq 1 40); do
  sleep 0.5
  curl -s -o /dev/null -m 2 "http://127.0.0.1:$PORT/api/ping?token=$TOKEN" && break
done
open "http://127.0.0.1:$PORT/"
wait "$ENGINE_PID" 2>/dev/null
EOF
chmod +x "$APPBUNDLE/Contents/MacOS/OmniTradeHub"

# Signer + quarantaine
codesign --force --deep --sign - "$APPBUNDLE" 2>/dev/null && echo "   ✓ .app signé"
xattr -dr com.apple.quarantine "$APPBUNDLE" 2>/dev/null || true

# Test réel : le moteur Python répond + sert la page de PRODUCTION
TPORT="8887"
PIDS=$(lsof -nP -iTCP:"$TPORT" -sTCP:LISTEN -t 2>/dev/null || true); [[ -n "$PIDS" ]] && kill $PIDS 2>/dev/null; sleep 1
PYF="$(command -v python3 || echo /usr/bin/python3)"
# Sur la CI (runner vierge) il faut Flask pour que le moteur démarre.
if ! "$PYF" -c 'import flask, flask_cors, waitress' 2>/dev/null; then
  "$PYF" -m pip install --quiet flask flask-cors waitress 2>/dev/null || true
fi
"$PYF" "$RES/9-moteur-de-donnees.py" --host 127.0.0.1 --port "$TPORT" --token ZT_PY_TEST --no-keep-open >/tmp/zt_py_test.log 2>&1 &
ENG=$!
PONG="000"
for i in $(seq 1 20); do sleep 1; PONG=$(curl -s -o /dev/null -w "%{http_code}" -m 2 "http://127.0.0.1:$TPORT/api/ping?token=ZT_PY_TEST" || true); [[ "$PONG" == "200" ]] && break; done
CODE=$(curl -s -o /dev/null -w "%{http_code}" -m 2 "http://127.0.0.1:$TPORT/" || true)
PAGE=$(curl -s -m 2 "http://127.0.0.1:$TPORT/" || true)
kill $ENG 2>/dev/null; sleep 1; kill -9 $ENG 2>/dev/null || true
[[ "$PONG" == "200" && "$CODE" == "200" && "$PAGE" == *"licCodeActivate"* ]] \
  && echo "   ✓ moteur PYTHON répond (200) + page PRODUCTION (200)" \
  || echo "   ⚠️ test: ping=$PONG code=$CODE (voir /tmp/zt_py_test.log)"

# DMG (le moteur Python ne dépend pas de l'architecture : un DMG par variante
# pour rester lisible dans la liste des téléchargements).
echo "→ Création du .dmg (Python, $VARIANT)…"
DMG="OmniTradeHub-macOS-$VARIANT-Python.dmg"
STAGE="stage_py"
rm -rf "$STAGE"; mkdir -p "$STAGE"
cp -R "$APPBUNDLE" "$STAGE/"
hdiutil create -volname "OmniTradeHub" -srcfolder "$STAGE" -ov -format UDZO \
  "$DMG" >/dev/null 2>&1
rm -rf "$STAGE"
echo "   ✓ $DMG (v$VERSION)"
echo "══════════════════════════════════════════════════════════"
echo "   ✅ PYTHON DÉFINITIF macOS : $APPBUNDLE + $DMG (v$VERSION)"
echo "══════════════════════════════════════════════════════════"
