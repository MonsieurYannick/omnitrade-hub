# test_check.py — vérifie avec le CŒUR DU MOTEUR (9-licence.py) les clés
# signées par oth_core.ts. Exécution : python3 test_check.py < test_sign.jsonl
import json
import sys

import importlib

sys.path.insert(0, "/Users/macbookdeeliysha/Documents/Default Project/OMNITRADE/OmniTradeHub-macOS v8.87")
sys.path.insert(0, "/Users/macbookdeeliysha/Documents/Default Project/OMNITRADE/OmniTradeHub-macOS v8.87/OmniTrade Hub Bridge.app/Contents/Resources")
# 9-licence.py se trouve à la racine ; license_core.py dans le Bundle.
lic = importlib.import_module("9-licence")
core = importlib.import_module("license_core")

SK = "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
PK = lic.ed25519_publickey(bytes.fromhex(SK)).hex()

ok = 0
fail = 0
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    d = json.loads(line)
    key = d["key"]
    # Vérif stricte avec parse_license du noyau applicatif
    payload, err = lic.parse_license(key, PK)
    if payload is None:
        print("ECHEC parse:", d["plan"], err, key)
        fail += 1
        continue
    if payload.get("plan") != d["plan"] or payload.get("mid") != d["mid"]:
        print("ECHEC contenu:", d["plan"], payload)
        fail += 1
        continue
    print("OK:", d["plan"], "->", payload["exp"], "| sn", payload["sn"])
    ok += 1

print(f"\n{ok} OK / {fail} ECHEC")
sys.exit(1 if fail else 0)