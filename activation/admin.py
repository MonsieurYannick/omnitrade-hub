#!/usr/bin/env python3
# admin.py — OUTIL VENDEUR : crée, liste, révoque et consulte les codes
# d'achat via la fonction Supabase « oth-issue ».
# Pure library standard Python : aucune installation (pip) nécessaire.
#
# Première utilisation : remplir activation/.adm.json
#   {
#     "function_url": "https://<projet>.supabase.co/functions/v1/oth-issue",
#     "admin_key":   "<le mot de passe vendeur (OTH_ADMIN_KEY)>"
#   }
#
# Commandes :
#   python3 admin.py create m12                 # code licence 12 mois, 2 machines
#   python3 admin.py create life --max 1        # code à vie, 1 seul ordinateur
#   python3 admin.py create m3 --days 75 --note "offre amis"
#   python3 admin.py list
#   python3 admin.py get OTH-XXXXXXXX-XXXXXXXX  # voir la licence émise (clé)
#   python3 admin.py revoke OTH-XXXXXXXX-XXXXXXXX
#   python3 admin.py stats
import argparse
import json
import os
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
CFG = os.path.join(HERE, ".adm.json")

PLANNING = {"demo7": 7, "m3": 90, "m6": 180, "m12": 365, "life": None}


def load_cfg():
    if not os.path.exists(CFG):
        sys.exit(
            f"Il manque le fichier {CFG}.\n"
            "Créez-le ainsi (remplacez les valeurs par les vôtres) :\n"
            json.dumps(
                {"function_url": "https://<projet>.supabase.co/functions/v1/oth-issue",
                 "admin_key": "<mot de passe vendeur>"},
                indent=2,
            )
        )
    with open(CFG) as f:
        cfg = json.load(f)
    if not cfg.get("function_url") or not cfg.get("admin_key"):
        sys.exit(f"{CFG} : les clés function_url et admin_key doivent être remplies.")
    return cfg


def call(cfg, payload):
    req = urllib.request.Request(
        cfg["function_url"],
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer anon",  # ignoré : la fonction est publique,
            "x-oth-admin": cfg["admin_key"],  # la vraie sécurité est ici.
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def show(resp, commands=("create", "revoke", "stats")):
    if resp.get("ok") is False:
        print("❌", resp.get("msg") or resp.get("error") or "erreur")
        sys.exit(1)
    if commands == "list":
        rows = resp.get("rows", [])
        if not rows:
            print("(aucun code pour l'instant — créez-en un : `admin.py create m12`)")
            return
        print(f"{'CODE':22} {'PLAN':8} {'J/RVQ':6} {'ACT':4}/{'MAX':4} {'CRÉÉ LE':21} EXPIRE   CLIENT/NOTE")
        for r in rows:
            print(
                f"{r['code']:22} {r['plan']:8} {'R' if r['revoke'] else 'J':6} "
                f"{r.get('activations', 0):4}/{r.get('max_activations', '-'):4} "
                f"{str(r.get('created_at') or '')[:16]:21} {str(r.get('expires_at') or '')[:10]:8} "
                f"{r.get('customer') or r.get('note') or ''}"
            )
    elif commands == "create":
        c = resp.get("code")
        print("✅ Code d'achat créé — à envoyer au client :")
        print("   " + c)
        print("   (cet écran ne sera jamais reproduit ailleurs : copiez-le maintenant)")
    elif commands == "get":
        r = resp
        print("Code   :", r.get("code"))
        print("Plan   :", r.get("plan"))
        print("Limite :", r.get("max_activations"), "machine(s)")
        print("Bloc   :", "RÉVOQUÉ" if r.get("revoked") else "actif")
        print("Expire :", r.get("expires_at") or "jamais")
        machines = r.get("machines") or []
        if machines:
            print(f"\nLicence(s) émise(s) ({len(machines)}) :")
            for m in machines:
                print("  - machine :", m.get("mid"))
                print("    expire  :", m.get("exp"))
                print("    clé     :", m.get("key"))
        else:
            print("\n(ce code n'a pas encore été activé)")
    elif commands == "revoke":
        print("✅", "Code révoqué" if resp.get("revoked") else "Code réactivé", ":", resp.get("code"))


def main():
    ap = argparse.ArgumentParser(description="Gestion des codes d'achat OmniTradeHub")
    sub = ap.add_subparsers(dest="action", required=True)

    pc = sub.add_parser("create", help="créer un code d'achat")
    pc.add_argument("plan", choices=list(PLANNING))
    pc.add_argument("--days", type=int, default=None, help="durée en jours (défaut : plan)")
    pc.add_argument("--max", type=int, default=2, help="nb max d'ordinateurs (défaut 2)")
    pc.add_argument("--note", default=None, help="note interne (client, offre…)")
    pc.add_argument("--customer", default=None, help="client (nom, téléphone…)")
    pc.add_argument("--expires", default=None, help="date limite d'activation (AAAA-MM-JJ)")

    sub.add_parser("list", help="lister les codes")

    pg = sub.add_parser("get", help="détail d'un code (clé émise incluse)")
    pg.add_argument("code")

    pr = sub.add_parser("revoke", help="révoquer / réactiver un code")
    pr.add_argument("code")
    pr.add_argument("--off", action="store_true", help="réactiver au lieu de révoquer")

    sub.add_parser("stats", help="nombre total de codes")

    a = ap.parse_args()
    cfg = load_cfg()

    if a.action == "create":
        resp = call(cfg, {
            "action": "create",
            "plan": a.plan,
            "days": a.days,
            "max_activations": a.max,
            "note": a.note,
            "customer": a.customer,
            "expires_at": a.expires,
        })
        show(resp, "create")
    elif a.action == "list":
        show(call(cfg, {"action": "list"}), "list")
    elif a.action == "get":
        show(call(cfg, {"action": "get", "code": a.code}), "get")
    elif a.action == "revoke":
        show(call(cfg, {"action": "revoke", "code": a.code, "revoked": not a.off}), "revoke")
    elif a.action == "stats":
        show(call(cfg, {"action": "stats"}), "stats")


if __name__ == "__main__":
    main()