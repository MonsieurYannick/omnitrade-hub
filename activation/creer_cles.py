#!/usr/bin/env python3
# creer_cles.py — génère la paire de clés Ed25519 pour la vente de licences.
# Récupère simplement les fonctions déjà présentes dans « 9-licence.py ».
#
# Exécution : python3 creer_cles.py
#
#  1) Le SK_HEX (clé privée secrète) → à mettre dans le secret Supabase OTH_PRIV_KEY
#  2) Le public_key.txt → à remplacer dans l'application (et dans le bundle .app)
#
# ⚠️ GARDEZ LE SK_HEX SECRET. N'importe qui qui l'a peut créer des licences.
import sys
import os

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.dirname(HERE)  # dossier « OmniTradeHub-macOS v8.87 »
sys.path.insert(0, APP)
sys.path.insert(0, os.path.join(APP, "Omni Trade Hub Bridge.app/Contents/Resources"))

import importlib
lic = importlib.import_module("9-licence")

import secrets
sk = secrets.token_bytes(32)
pk = lic.ed25519_publickey(sk)

print("#" * 72)
print("#  NOUVELLE PAIRE DE CLÉS DE LICENCE OmniTradeHub")
print("#" * 72)
print()
print("CLÉ PRIVÉE  (SK_HEX, à garder SECRÈTE) :")
print(sk.hex())
print()
print("CLÉ PUBLIQUE (contenu de public_key.txt) :")
print(pk.hex())
print()
print("Récap des 2 actions :")
print("  1. Supabase → fonction oth-activate → Secrets → OTH_PRIV_KEY =", sk.hex())
print("  2. Remplacer public_key.txt dans l'application par :", pk.hex())
print()
print("⚠️  Si vous aviez déjà vendu des licences avec une AUTRE clé, elles")
print("   deviendront invalides avec cette nouvelle paire. Dans ce cas,")
print("   réutilisez l'ancienne clé privée pour OTH_PRIV_KEY au lieu d'en générer une.")