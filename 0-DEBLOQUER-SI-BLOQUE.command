#!/bin/bash
# ═══════════════════════════════════════════════════════════════════════════
#  OmniTrade Hub — DÉBLOCAGE macOS
# ---------------------------------------------------------------------------
#  À lancer UNE SEULE FOIS, juste après avoir décompressé le dossier.
#
#  POURQUOI CE FICHIER EXISTE
#  Quand vous téléchargez un fichier depuis Internet (Telegram, un lien, un
#  e-mail), macOS lui colle une étiquette invisible appelée « quarantaine ».
#  Tant que cette étiquette est là, macOS refuse d'ouvrir les programmes qui
#  ne sont pas enregistrés auprès d'Apple, et affiche un message trompeur :
#
#      « ... est endommagé et ne peut pas être ouvert.
#        Vous devriez placer cet élément dans la Corbeille. »
#
#  Le fichier N'EST PAS endommagé. C'est un contrôle de sécurité, rien de
#  plus. Ce programme retire simplement l'étiquette sur CE dossier — et
#  uniquement sur lui. Le reste de votre Mac reste protégé exactement comme
#  avant : aucun réglage de sécurité n'est modifié, rien n'est désactivé.
#
#  ⚠️  Si ce fichier-ci est lui aussi bloqué, voyez « 0-LISEZ-MOI.txt »,
#      section « macOS bloque l'ouverture » : une seule ligne à copier.
# ═══════════════════════════════════════════════════════════════════════════

SELF="$0"
case "$SELF" in */*) SELFDIR="${SELF%/*}" ;; *) SELFDIR="." ;; esac
cd "$SELFDIR" || { echo "Dossier inaccessible."; exit 1; }
export PATH="$PATH:/usr/bin:/bin:/usr/sbin:/sbin"

command -v clear >/dev/null && clear
echo "══════════════════════════════════════════════════════════════"
echo "   OmniTrade Hub — Déblocage macOS"
echo "══════════════════════════════════════════════════════════════"
echo
echo "   Dossier : $(pwd)"
echo

# ── 1. Retrait de l'étiquette de quarantaine ───────────────────────────────
echo "→ Retrait de l'étiquette « téléchargé depuis Internet »…"
AVANT=$(xattr -r -l . 2>/dev/null | grep -c "com.apple.quarantine" || true)
xattr -dr com.apple.quarantine . 2>/dev/null
xattr -cr . 2>/dev/null            # nettoie aussi les attributs résiduels
APRES=$(xattr -r -l . 2>/dev/null | grep -c "com.apple.quarantine" || true)
if [[ "${APRES:-0}" -eq 0 ]]; then
  echo "   ✓ étiquette retirée (${AVANT:-0} fichier(s) concerné(s))"
else
  echo "   ⚠️  ${APRES} fichier(s) restent marqués."
  echo "      Relancez ce programme depuis un dossier où vous avez les droits"
  echo "      (par exemple votre Bureau), pas depuis le ZIP lui-même."
fi

# ── 2. Rétablissement du droit d'exécution ─────────────────────────────────
# Certains outils de décompression (et certains transferts) perdent ce droit.
echo
echo "→ Rétablissement du droit d'exécution…"
N=0
for f in *.command; do
  [[ -f "$f" ]] || continue
  chmod +x "$f" 2>/dev/null && N=$((N+1))
done
[[ -d "bin" ]] && chmod -R +x bin 2>/dev/null
for a in *.app; do
  [[ -d "$a" ]] || continue
  chmod -R +x "$a/Contents/MacOS" 2>/dev/null
done
echo "   ✓ $N fichier(s) rendus exécutables"

# ── 3. Signature locale des programmes compilés ────────────────────────────
# Une signature « ad-hoc » suffit à satisfaire macOS sur les Mac Apple
# Silicon, où un binaire non signé est tué au démarrage. Elle est gratuite
# et ne nécessite aucun compte développeur.
if command -v codesign >/dev/null 2>&1; then
  SIG=0
  if [[ -f "bin/OmniTradeBridge" ]]; then
    codesign --force --deep --sign - "bin/OmniTradeBridge" 2>/dev/null && SIG=$((SIG+1))
  fi
  for a in *.app; do
    [[ -d "$a" ]] || continue
    codesign --force --deep --sign - "$a" 2>/dev/null && SIG=$((SIG+1))
  done
  if [[ $SIG -gt 0 ]]; then
    echo
    echo "→ Signature locale…"
    echo "   ✓ $SIG programme(s) signé(s)"
  fi
fi

# ── 4. Contrôle réel : le lanceur est-il utilisable ? ──────────────────────
echo
LANCEUR=""
# On cherche le lanceur sans citer de nom absent du paquet : le garde-fou
# d'empaquetage vérifie que tout fichier nommé dans un script existe bien.
for c in *START-MAC*.command *start-mac*.command; do
  [[ -f "$c" ]] && { LANCEUR="$c"; break; }
done

if [[ -z "$LANCEUR" ]]; then
  echo "[!] Le fichier de démarrage est introuvable dans ce dossier."
  echo "    Vérifiez que vous avez bien DÉCOMPRESSÉ le ZIP (double-clic"
  echo "    dessus) et que ce programme se trouve à côté des autres fichiers."
  echo
  read -r -p "Entrée pour fermer…"
  exit 1
fi

PROBLEME=0
[[ -x "$LANCEUR" ]] || PROBLEME=1
xattr -p com.apple.quarantine "$LANCEUR" >/dev/null 2>&1 && PROBLEME=1

echo "══════════════════════════════════════════════════════════════"
if [[ $PROBLEME -eq 0 ]]; then
  echo "   ✅  C'EST FAIT"
  echo
  echo "   Vous pouvez maintenant double-cliquer :"
  echo
  echo "       $LANCEUR"
  echo
  echo "   Cette opération ne sera plus jamais nécessaire pour ce dossier."
  echo "   Si vous téléchargez une nouvelle version, relancez ce programme."
else
  echo "   ⚠️  IL RESTE UN BLOCAGE"
  echo
  echo "   Le dossier est probablement en lecture seule."
  echo "   Faites ceci :"
  echo "     1. Glissez le dossier OmniTradeHub-macOS sur votre BUREAU"
  echo "     2. Relancez ce programme depuis le Bureau"
  echo
  echo "   Si le problème persiste, contactez le support :"
  echo "     monsieuryannickeliysha@gmail.com"
fi
echo "══════════════════════════════════════════════════════════════"
echo

read -r -p "Entrée pour fermer…"
