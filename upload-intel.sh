#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════════
#  OmniTrade Hub — Construction + mise en ligne AUTOMATIQUE de l'installateur
#  macOS Intel (x86_64) dans la Release GitHub du tag courant.
# ---------------------------------------------------------------------------
#  Le runner GitHub « macos-13 » (Intel) étant souvent saturé, le DMG Intel n'est
#  pas toujours publié à temps par le workflow. Ce script build le DMG en local
#  (sur un Mac Intel) puis le dépose immédiatement sur la Release vX.Y.Z.
#
#  Usage :   bash upload-intel.sh [version]
#            (version par défaut : valeur de APP.version dans omnitrade-v21.html)
#
#  Prérequis : gh authentifié (gh auth login), hdiutil (macOS).
# ═══════════════════════════════════════════════════════════════════════════
set -euo pipefail
cd "$(dirname "$0")"

# ── Version ─────────────────────────────────────────────────────────────────
VER="${1:-}"
if [[ -z "$VER" ]]; then
  # Priorité : APP.version du HTML, puis le dernier tag v* local, puis dernier tag GitHub.
  VER="$(sed -nE "s/.*version:'([0-9.]+)'.*/\1/p" omnitrade-v21.html | head -1)"
fi
if [[ -z "$VER" ]]; then
  VER="$(git tag -l 'v*' | sort -V | tail -1 | sed 's/^v//' || true)"
fi
[[ -n "$VER" ]] || { echo "[!] Version introuvable (passez-la en argument)"; exit 1; }
echo "  Version : $VER"

# ── Construction du DMG Intel (détecte l'architecture de la machine) ────────
echo "→ Construction de l'installateur macOS Intel (v$VER)…"
rm -f OmniTradeHub-macOS-Intel.dmg OmniTradeHub-macOS-Intel-PRET.zip
VER="$VER" VARIANT=Intel bash build-ci-macos.sh || {
  echo "[!] Échec de la construction du .dmg"; exit 1; }

# ── Vérifications de garde ──────────────────────────────────────────────────
[[ -f OmniTradeHub-macOS-Intel.dmg ]] || { echo "[!] .dmg absent"; exit 1; }
if ! file OmniTradeHub.app/Contents/Resources/OmniTradeBridge/OmniTradeBridge 2>/dev/null | grep -q x86_64; then
  echo "[!] Le binaire n'est pas x86_64 : vous devez builder sur un Mac Intel."; exit 1
fi
echo "   ✓ binaire x86_64 (Mac Intel)"

# ── Mise en ligne sur la Release du tag ─────────────────────────────────────
echo "→ Mise en ligne sur la Release v$VER…"
gh release view "v$VER" >/dev/null 2>&1 || gh release create "v$VER" --title "OmniTrade Hub v$VER" --notes "Nouvelle version disponible." || true
gh release upload "v$VER" \
  OmniTradeHub-macOS-Intel.dmg \
  OmniTradeHub-macOS-Intel-PRET.zip \
  --clobber

echo "══════════════════════════════════════════════════════════════"
echo "  ✅ DMG Intel v$VER construit et mis en ligne."
echo "══════════════════════════════════════════════════════════════"
