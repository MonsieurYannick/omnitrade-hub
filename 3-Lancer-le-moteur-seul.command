#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
#  OmniTrade Hub Bridge — lanceur macOS
#  Double-cliquez simplement sur ce fichier.
# ═══════════════════════════════════════════════════════════════════════════
#  Ordre de priorité :
#    1. Binaire autonome compilé  -> aucun Python, aucun Xcode, rien à installer
#    2. Python RÉEL déjà présent  -> uniquement s'il fonctionne vraiment
#  Le stub /usr/bin/python3 de macOS (qui déclenche la pop-up « Outils de ligne
#  de commande Xcode », plusieurs Go) est explicitement DÉTECTÉ ET ÉVITÉ.
# ═══════════════════════════════════════════════════════════════════════════

# Le double-clic depuis le Finder démarre dans « / » : on se replace TOUJOURS
# dans le dossier du script, sinon le binaire voisin reste introuvable.
# Expansion bash pure (pas d'appel à `dirname`) : fonctionne même si le PATH
# est incomplet, cas réel sur un Mac où les outils Xcode sont absents.
SELF="$0"
case "$SELF" in
  */*) SELFDIR="${SELF%/*}" ;;
  *)   SELFDIR="." ;;
esac
cd "$SELFDIR" || { echo "[!] Dossier inaccessible : $SELFDIR"; exit 1; }
# PATH minimal garanti (le Finder transmet parfois un PATH très réduit).
export PATH="$PATH:/usr/bin:/bin:/usr/sbin:/sbin:/usr/local/bin:/opt/homebrew/bin"

TOKEN="${ZT_TOKEN:-ZELLA_TOKEN}"     # identique au défaut de l'application
PORT="${ZT_PORT:-8765}"

command -v clear >/dev/null && clear
echo "══════════════════════════════════════════════════════════════"
echo "   OmniTrade Hub Bridge — MetaTrader 5 (macOS)"
echo "══════════════════════════════════════════════════════════════"
echo

# Lever la quarantaine du téléchargement (« développeur non vérifié »).
xattr -dr com.apple.quarantine . 2>/dev/null

# ── 1. Binaire autonome ────────────────────────────────────────────────────
BIN=""
for c in "./bin/OmniTrade HubBridge" \
         "./OmniTrade HubBridge" \
         "./bin/OmniTrade HubBridge/OmniTrade HubBridge"; do
  [[ -f "$c" ]] || continue
  # Garde : un binaire compilé est un Mach-O/ELF, jamais un script texte.
  # Sans ce test, l'exécutable du bundle .app (un script shell) serait pris
  # pour le pont compilé -> le lanceur se rappellerait lui-même en boucle.
  head -c 2 "$c" 2>/dev/null | grep -q '^#!' && continue
  BIN="$c"; break
done

# ── 2. Sinon, un Python RÉELLEMENT fonctionnel ─────────────────────────────
# Test décisif : on exécute un vrai bout de code. Le stub Xcode échoue ici,
# ce qui nous permet de l'écarter SANS jamais déclencher sa pop-up.
PY=""
if [[ -z "$BIN" ]]; then
  # Liste large : Homebrew (Intel + Apple Silicon), python.org (toutes
  # versions, le lien « Current » est souvent absent), pyenv, conda, MacPorts,
  # puis le PATH. /usr/bin/python3 est testé EN DERNIER : s'il répond, c'est
  # que les outils Xcode sont déjà installés et c'est un Python parfaitement
  # valide ; s'il ne répond pas, c'est le stub et le test l'écarte tout seul.
  CANDS=()
  for d in /opt/homebrew/bin /usr/local/bin /opt/local/bin; do
    [[ -x "$d/python3" ]] && CANDS+=("$d/python3")
  done
  # python.org : /Library/Frameworks/Python.framework/Versions/3.13/bin/...
  for f in /Library/Frameworks/Python.framework/Versions/*/bin/python3; do
    [[ -x "$f" ]] && CANDS+=("$f")
  done
  # Versions Homebrew explicites (python3.12, python3.13…)
  for f in /opt/homebrew/bin/python3.* /usr/local/bin/python3.*; do
    [[ -x "$f" ]] && CANDS+=("$f")
  done
  # pyenv / conda / autres, via le PATH
  for n in python3 python3.13 python3.12 python3.11 python3.10 python3.9; do
    w="$(command -v "$n" 2>/dev/null)"
    [[ -n "$w" && -x "$w" ]] && CANDS+=("$w")
  done
  [[ -x /usr/bin/python3 ]] && CANDS+=("/usr/bin/python3")

  for cand in "${CANDS[@]}"; do
    [[ -z "$cand" || ! -x "$cand" ]] && continue
    # Test décisif : le stub Xcode échoue ici (code non nul, pas de « ZTOK »)
    # et n'ouvre AUCUNE pop-up puisque sa sortie est redirigée.
    if [[ "$("$cand" -c 'print("ZTOK")' 2>/dev/null)" == "ZTOK" ]]; then
      PY="$cand"; break
    fi
  done
fi

if [[ -z "$BIN" && -z "$PY" ]]; then
  echo "[!] Aucun Python utilisable n'a été trouvé sur ce Mac."
  echo
  echo "    Emplacements inspectés :"
  if [[ ${#CANDS[@]} -eq 0 ]]; then
    echo "      (aucun exécutable python3 présent sur la machine)"
  else
    for c in "${CANDS[@]}"; do
      if out=$("$c" -V 2>&1); then
        echo "      ✗ $c  →  $out  (ne répond pas au test)"
      else
        echo "      ✗ $c  →  non fonctionnel (stub Xcode probable)"
      fi
    done
  fi
  cat <<'EOF'

    SOLUTION (2 minutes, sans Xcode) :

      1. Ouvrez  https://www.python.org/downloads/macos/
      2. Téléchargez « macOS 64-bit universal2 installer »
      3. Installez-le (suivant → suivant)
      4. Relancez OmniTrade Hub Bridge

    IMPORTANT : si macOS propose d'installer les « Outils de ligne de
    commande Xcode » (plusieurs Go), cliquez sur ANNULER. C'est inutile
    ici, l'installeur python.org suffit.

EOF
  read -r -p "Appuyez sur Entrée pour fermer…"
  exit 1
fi

# ── Libération du port (le binaire le refait aussi de son côté) ────────────
PIDS=$(lsof -nP -iTCP:"$PORT" -sTCP:LISTEN -t 2>/dev/null || true)
if [[ -n "$PIDS" ]]; then
  echo "  · Port $PORT occupé — libération…"
  # shellcheck disable=SC2086
  kill $PIDS 2>/dev/null; sleep 1
  # shellcheck disable=SC2086
  kill -9 $PIDS 2>/dev/null || true
  echo "  · Port $PORT libéré"
  echo
fi

if [[ -n "$BIN" ]]; then
  chmod +x "$BIN" 2>/dev/null
  echo "  Moteur : binaire autonome (aucune dépendance)"
  RUN=("$BIN")
else
  echo "  Moteur : Python détecté ($PY)"
  # Dépendances minimales, installées une seule fois, sans Xcode.
  if ! "$PY" -c 'import flask, flask_cors' 2>/dev/null; then
    echo "  · Installation unique de Flask…"
    "$PY" -m pip install --quiet --user flask flask-cors 2>/dev/null \
      || "$PY" -m pip install --quiet flask flask-cors 2>/dev/null \
      || { echo "  [!] Installation impossible."; \
           read -r -p "Entrée pour fermer…"; exit 1; }
  fi
  # Chemin ABSOLU : le pont doit démarrer même si le cwd a changé.
  if [[ ! -f "$PWD/9-moteur-de-donnees.py" ]]; then
    echo "  [!] 9-moteur-de-donnees.py est absent de ce dossier."
    read -r -p "Appuyez sur Entrée pour fermer…"; exit 1
  fi
  RUN=("$PY" "$PWD/9-moteur-de-donnees.py")
fi

echo "  Port   : $PORT"
echo "  Token  : $TOKEN"
echo
echo "  ➜ Ouvrez maintenant OmniTrade Hub : la connexion se fait TOUTE SEULE."
echo "    (rien à saisir : ni hôte, ni port, ni jeton)"
echo
echo "  Laissez cette fenêtre OUVERTE pendant vos synchronisations."
echo "  Pour arrêter : fermez simplement la fenêtre."
echo "──────────────────────────────────────────────────────────────"
echo

"${RUN[@]}" --token "$TOKEN" --port "$PORT" "$@"
CODE=$?

echo
if [[ $CODE -ne 0 ]]; then
  echo "──────────────────────────────────────────────────────────────"
  echo "  Le pont s'est arrêté (code $CODE)."
  case $CODE in
    2) cat <<'EOF'

  Aucune donnée MetaTrader 5 trouvée. Sur Mac, MT5 tourne sous Wine :
  les données passent par le petit programme fourni (Expert Advisor).

    1. Ouvrez MT5 → Outils → MetaQuotes Language Editor
    2. Fichier → Nouveau → Expert Advisor → nommez-le OmniTradeExport
    3. Collez le contenu de 2-OmniTradeExport.mq5, compilez (F7)
    4. Glissez l'EA sur un graphique
    5. Cochez « Autoriser le trading algorithmique »
    6. Relancez ce lanceur
EOF
       echo
       echo "  Dossiers inspectés :"
       "${RUN[@]}" --list-dirs --no-keep-open 2>/dev/null | sed 's/^/    /'
       ;;
    3) echo "  Le port $PORT reste occupé par une autre application."
       echo "  Relancez avec :  ZT_PORT=8766 \"$0\"" ;;
  esac
  echo "──────────────────────────────────────────────────────────────"
  echo
  read -r -p "Appuyez sur Entrée pour fermer…"
fi
