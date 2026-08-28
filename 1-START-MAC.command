#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
#  OmniTrade Hub — DÉMARRAGE macOS
#  Double-cliquez ce fichier. C'est tout.
#  Il lance le moteur de données puis ouvre OmniTrade Hub dans votre navigateur.
# ═══════════════════════════════════════════════════════════════════════════

SELF="$0"
case "$SELF" in */*) SELFDIR="${SELF%/*}" ;; *) SELFDIR="." ;; esac
cd "$SELFDIR" || exit 1
export PATH="$PATH:/usr/bin:/bin:/usr/sbin:/sbin:/usr/local/bin:/opt/homebrew/bin"

# ── Fichier de l'application : DÉTECTÉ, jamais codé en dur ────────────────
# Bug corrigé : « zellatrack-v20.html » était écrit en dur ici. Après le
# changement de nom, le fichier n'existait plus et la page ne s'ouvrait pas.
# On choisit désormais le numéro de version le PLUS ÉLEVÉ réellement présent
# (les anciens noms restent acceptés pour ne rien casser).
pick_app(){
  local best="" bestn=-1 f n
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
  # Dernier recours : n'importe quel HTML de l'application présent.
  [[ -n "$best" ]] || best=$(ls -t omnitrade-*.html zellatrack-*.html 2>/dev/null | head -1)
  printf '%s' "$best"
}
APP="${ZT_APP:-$(pick_app)}"
PORT="${ZT_PORT:-8765}"
# ── Clé d'accès de CE poste ───────────────────────────────────────────────
# Chaque installation possède sa propre clé de 32 caractères, créée au
# premier démarrage. Elle remplace l'ancienne « ZELLA_TOKEN », qui était
# identique chez tous les utilisateurs et lisible dans les fichiers livrés.
TOKEN="${ZT_TOKEN:-}"

# Journal technique du moteur. On écrit dans le dossier de l'utilisateur
# plutôt que dans /tmp : c'est toujours inscriptible, et le fichier reste
# consultable en cas de souci.
LOGDIR="$HOME/Library/Logs"
[[ -d "$LOGDIR" ]] || LOGDIR="$HOME"
LOGF="$LOGDIR/OmniTradeHub.log"
: > "$LOGF" 2>/dev/null || LOGF="/tmp/OmniTradeHub.log"

command -v clear >/dev/null && clear
echo "══════════════════════════════════════════════════════════════"
echo "   OmniTrade Hub — démarrage"
echo "══════════════════════════════════════════════════════════════"
echo

xattr -dr com.apple.quarantine . 2>/dev/null

# ── Moteur : binaire compilé si présent, sinon Python ─────────────────────
BIN=""
for c in "./bin/OmniTradeBridge" "./OmniTradeBridge"; do
  [[ -f "$c" ]] || continue
  head -c 2 "$c" 2>/dev/null | grep -q '^#!' && continue   # un script n'est pas un binaire
  BIN="$c"; break
done

PY=""
if [[ -z "$BIN" ]]; then
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
    # Test réel : le stub Xcode de macOS échoue ici et est donc écarté
    # SANS jamais déclencher sa fenêtre d'installation (plusieurs Go).
    [[ "$("$c" -c 'print("ZTOK")' 2>/dev/null)" == "ZTOK" ]] && { PY="$c"; break; }
  done
fi

if [[ -z "$BIN" && -z "$PY" ]]; then
  cat <<'EOF'
[!] Python est nécessaire pour le moteur de données.

    Installation en 2 minutes :
      1. Ouvrez  https://www.python.org/downloads/macos/
      2. Téléchargez « macOS 64-bit universal2 installer »
      3. Installez, puis relancez ce fichier

    IMPORTANT : si macOS propose les « Outils de ligne de commande Xcode »
    (plusieurs Go), cliquez sur ANNULER. C'est inutile ici.

    OmniTrade Hub fonctionne malgré tout : le journal, les analyses et les
    cours d'éducation n'ont besoin d'aucun moteur. Seules les données de
    marché en direct le nécessitent.
EOF
  echo
  read -r -p "Ouvrir OmniTrade Hub quand même ? [Entrée] "
  [[ -f "$APP" ]] && open "$PWD/$APP"
  exit 0
fi

# ── Libération du port ────────────────────────────────────────────────────
PIDS=$(lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t 2>/dev/null || true)
if [[ -n "$PIDS" ]]; then
  echo "  · Port $PORT occupé — libération…"
  # shellcheck disable=SC2086
  kill $PIDS 2>/dev/null; sleep 1
  # shellcheck disable=SC2086
  kill -9 $PIDS 2>/dev/null || true
fi

# ── Dépendances (une seule fois) ──────────────────────────────────────────
if [[ -n "$PY" ]]; then
  if ! "$PY" -c 'import flask, flask_cors' 2>/dev/null; then
    echo "  · Première installation des composants…"
    "$PY" -m pip install --quiet --user flask flask-cors 2>/dev/null \
      || "$PY" -m pip install --quiet flask flask-cors 2>/dev/null
  fi
fi

# Le moteur connaît sa clé : on la lui demande plutôt que d'en inventer une.
if [[ -z "$TOKEN" ]]; then
  if [[ -n "$BIN" ]]; then
    TOKEN=$("$BIN" --show-token --no-keep-open 2>/dev/null | tail -1)
  else
    TOKEN=$("$PY" "$PWD/9-moteur-de-donnees.py" --show-token --no-keep-open 2>/dev/null | tail -1)
  fi
fi
# Filet : si la clé n'a pas pu être lue, on retombe sur l'ancienne, qui
# reste acceptée depuis cet ordinateur.
[[ -n "$TOKEN" ]] || TOKEN="ZELLA_TOKEN"
# Un token n'a pas d'espace : si --show-token a renvoyé un message d'erreur, on jette.
case "$TOKEN" in
  *" "*|*[Ff]lask*) TOKEN="ZELLA_TOKEN" ;;
esac

echo "  Moteur : ${BIN:-$PY}"
echo "  Port   : $PORT"
echo

# ── Interface d'écoute ────────────────────────────────────────────────────
# Le moteur n'écoute QUE sur 127.0.0.1 : lui seul peut lui parler.
# L'accès depuis un téléphone a été retiré (projet abandonné) ; le moteur
# n'est donc plus visible sur le réseau local, ce qui supprime du même coup
# toute surface d'exposition.
HOST_BIND="127.0.0.1"

# ── Lancement en arrière-plan ─────────────────────────────────────────────
if [[ -n "$BIN" ]]; then
  chmod +x "$BIN" 2>/dev/null
  "$BIN" --host "$HOST_BIND" --port "$PORT" --token "$TOKEN" \
    --no-keep-open >"$LOGF" 2>&1 &
else
  "$PY" "$PWD/9-moteur-de-donnees.py" --host "$HOST_BIND" --port "$PORT" \
    --token "$TOKEN" --no-keep-open >"$LOGF" 2>&1 &
fi
ENGINE_PID=$!

# On attend que le moteur réponde avant d'ouvrir le navigateur.
for i in $(seq 1 20); do
  sleep 0.5
  curl -s -o /dev/null -m 2 "http://127.0.0.1:$PORT/api/ping?token=$TOKEN" && break
done

echo "  ➜ Ouverture de OmniTrade Hub dans votre navigateur…"
# On ouvre via HTTP (le moteur sert le fichier HTML) : cela permet
# l'intégration de vidéos YouTube/Dailymotion en iframe (impossible en file://).
if curl -s -o /dev/null -m 2 "http://127.0.0.1:$PORT/api/ping?token=$TOKEN"; then
  open "http://127.0.0.1:$PORT/"
else
  # Fallback : le moteur ne répond pas encore, on tente le fichier local.
  if [[ -n "$APP" && -f "$APP" ]]; then
    open "file://$PWD/$APP" 2>/dev/null \
      || echo "  [!] Ouvrez manuellement : http://127.0.0.1:$PORT/"
  else
    echo "  [!] Ouvrez manuellement : http://127.0.0.1:$PORT/"
  fi
fi

cat <<EOF

──────────────────────────────────────────────────────────────
  OmniTrade Hub est lancé.

  • Les données de marché (actualités, calendrier, sentiment)
    arrivent automatiquement.
  • Si MetaTrader 5 est ouvert avec l'EA, vos trades se
    synchronisent aussi.

  LAISSEZ CETTE FENÊTRE OUVERTE pendant votre session.
  Pour arrêter : fermez simplement la fenêtre.
──────────────────────────────────────────────────────────────

EOF

# Le moteur reste au premier plan : fermer la fenêtre l'arrête proprement.
wait $ENGINE_PID 2>/dev/null
