#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
═══════════════════════════════════════════════════════════════════════════════
 OmniTrade Hub v3 — MT5 Local Bridge
═══════════════════════════════════════════════════════════════════════════════
 Pont local entre MetaTrader 5 (terminal Windows) et l'application web
 OmniTrade Hub v3 (fichier HTML ouvert dans le navigateur).

 Expose :
   • une API HTTP (Flask + Flask-CORS)   -> http://127.0.0.1:8765
   • un flux temps réel WebSocket         -> ws://127.0.0.1:8766   (option --ws)

 Données extraites :
   • Historique des positions FERMÉES (ticket, symbole, sens, prix entrée/sortie,
     SL, TP, lots, PnL brut, swap, commissions, PnL net, durée, MAE, MFE)
   • Positions OUVERTES (avec PnL flottant)
   • Compte : solde, équité, marge, marge libre, niveau de marge, levier...

───────────────────────────────────────────────────────────────────────────────
 INSTALLATION (Windows, Python 3.9+ 64 bits, même machine que MT5)
───────────────────────────────────────────────────────────────────────────────
   pip install MetaTrader5 flask flask-cors websockets

 LANCEMENT
   # HTTP seul (le plus simple, suffisant pour l'Auto-Sync)
   python mt5_bridge.py --token MON_TOKEN_SECRET

   # HTTP + WebSocket push temps réel
   python mt5_bridge.py --token MON_TOKEN_SECRET --ws

   # Connexion explicite à un compte
   python mt5_bridge.py --login 12345678 --password "xxx" --server "Deriv-Demo" \
                        --terminal "C:/Program Files/MetaTrader 5/terminal64.exe"

 Puis dans OmniTrade Hub v3 → onglet « Connexion MT5 » :
   URL/Hôte : 127.0.0.1     Port : 8765     Token : MON_TOKEN_SECRET
───────────────────────────────────────────────────────────────────────────────
 ENDPOINTS HTTP
   GET /api/ping                       -> statut du pont + terminal
   GET /api/account                    -> solde / équité / marge
   GET /api/trades?days=365            -> positions fermées (mappées OmniTrade Hub)
   GET /api/positions                  -> positions ouvertes
   GET /api/sync?days=365&mae=1        -> TOUT en une requête (utilisé par l'app)
   GET /api/symbols                    -> symboles visibles (debug)

 AUTH : header  X-ZT-Token: <token>   ou  ?token=<token>
        (si --token n'est pas fourni, l'authentification est désactivée)
═══════════════════════════════════════════════════════════════════════════════
"""

import argparse
import asyncio
import json
import logging
import math
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

# ═══════════════════════════════════════════════════════════════════════════
#  CONSOLE WINDOWS : SURVIVRE AUX ACCENTS      ← correctif v7.3, CRITIQUE
# ---------------------------------------------------------------------------
#  BUG EN PRODUCTION (5 clients bloqués) : sous Windows, la console utilise
#  cp850 ou cp1252, pas UTF-8. Le moteur affiche au démarrage des messages
#  contenant « — », « ═ », « · », « ✓ », « É »… Le tout premier print() de
#  ce type levait UnicodeEncodeError.
#
#  Conséquence exacte : le moteur MOURAIT avant d'atteindre app.run(). Le
#  port n'était jamais ouvert, aucun fetch() ne pouvait aboutir, et
#  l'application restait indéfiniment sur « recherche du moteur… ».
#  Reproduit : PYTHONIOENCODING=cp850 -> UnicodeEncodeError ligne 3550,
#  HTTP 000, port absent de la table d'écoute.
#
#  Correctif : on bascule la sortie en UTF-8 quand c'est possible, et sinon
#  on remplace les caractères impossibles au lieu de laisser l'exception
#  tuer le processus. Un message d'accueil ne doit JAMAIS empêcher un
#  serveur de démarrer.
# ═══════════════════════════════════════════════════════════════════════════
# Pas de dossier « __pycache__ » chez le client : Python crée sinon un
# répertoire de cache à côté des fichiers livrés. Sans intérêt pour lui,
# et il encombre un dossier qui doit rester lisible (constaté sur capture).
sys.dont_write_bytecode = True


def _console_sure():
    for flux in ("stdout", "stderr"):
        f = getattr(sys, flux, None)
        if f is None:
            continue
        try:
            # Python 3.7+ : on reconfigure le flux existant.
            f.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            # Flux non reconfigurable (binaire redirigé, pythonw.exe, service
            # Windows sans console…) : on l'emballe pour ne jamais lever.
            try:
                import io
                buf = getattr(f, "buffer", None)
                if buf is not None:
                    setattr(sys, flux, io.TextIOWrapper(
                        buf, encoding="utf-8", errors="replace",
                        line_buffering=True))
            except Exception:
                pass


_console_sure()


def _print_sur(*args, **kwargs):
    """print() qui ne peut pas tuer le moteur.

    Dernier filet : même si la reconfiguration ci-dessus a échoué (cas d'un
    binaire PyInstaller lancé sans console), afficher du texte accentué ne
    doit jamais interrompre le démarrage du serveur.
    """
    try:
        _print_original(*args, **kwargs)
    except UnicodeEncodeError:
        try:
            enc = (getattr(sys.stdout, "encoding", None) or "ascii")
            propre = [
                str(a).encode(enc, "replace").decode(enc, "replace")
                for a in args
            ]
            _print_original(*propre, **kwargs)
        except Exception:
            pass
    except Exception:
        pass


_print_original = print
print = _print_sur          # noqa: A001 — volontaire, portée module

# ─────────────────────────────────────────────────────────────────────────────
# Dépendances externes
# ─────────────────────────────────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────
#  BACKEND ADAPTATIF : Windows (API native) ou macOS/Linux (fichiers JSON)
# -----------------------------------------------------------------------------
#  Le paquet `MetaTrader5` ne publie que des roues binaires Windows : il ne
#  s'installe pas sur macOS ni Linux. Le terminal « MT5 pour Mac » distribué
#  par MetaQuotes est lui-même un habillage Wine.
#
#  Sur ces plateformes, le pont bascule automatiquement en mode FICHIER :
#  un Expert Advisor (OmniTradeExport.mq5, généré par --emit-ea) tourne dans
#  MT5 et écrit périodiquement account.json / trades.json / positions.json
#  dans MQL5/Files. Le pont lit ces fichiers — aucune DLL, aucun Wine côté
#  Python, et le même contrat JSON qu'en mode natif.
# ─────────────────────────────────────────────────────────────────────────────
import glob
import hashlib
import os
import re
import socket
import subprocess

IS_WINDOWS = sys.platform.startswith("win")
IS_MACOS = sys.platform == "darwin"

# ─────────────────────────────────────────────────────────────────────────────
#  EXÉCUTION GELÉE (PyInstaller)
# -----------------------------------------------------------------------------
#  Une fois compilé, l'exécutable se déballe dans un dossier temporaire exposé
#  via sys._MEIPASS. Tout accès à une ressource embarquée DOIT passer par
#  resource_path(), et tout fichier écrit pour l'utilisateur par app_dir() :
#  écrire dans _MEIPASS serait perdu à la fermeture (dossier auto-supprimé).
# ─────────────────────────────────────────────────────────────────────────────
IS_FROZEN = bool(getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"))


def resource_path(*parts):
    """Chemin d'une ressource EMBARQUÉE (lecture seule), gelée ou non."""
    base = getattr(sys, "_MEIPASS", None) or os.path.dirname(
        os.path.abspath(__file__))
    return os.path.join(base, *parts)


def app_dir():
    """Dossier de l'exécutable réel (à côté du .exe / du .app) — écriture OK.

    En gelé, os.path.dirname(__file__) pointe vers le dossier temporaire
    _MEIPASS ; il faut sys.executable. Sur macOS, si l'on est dans un bundle
    .app, on remonte au-dessus de Foo.app pour rester visible par l'utilisateur.
    """
    if IS_FROZEN:
        p = os.path.dirname(os.path.abspath(sys.executable))
        marker = os.sep + "Contents" + os.sep + "MacOS"
        if IS_MACOS and p.endswith(marker):
            bundle = p[: -len(marker)]                 # .../Foo.app
            return os.path.dirname(bundle)             # dossier parent
        return p
    return os.path.dirname(os.path.abspath(__file__))


def user_data_dir():
    """Dossier de config/logs d'OmniTrade Hub, toujours inscriptible.

    Le produit s'appelait « ZellaTrack » jusqu'à la version 5.0. Les traders
    déjà équipés possèdent donc un dossier à l'ancien nom : on le RÉCUPÈRE
    automatiquement au premier lancement, afin qu'aucun réglage ni journal
    ne soit perdu par le changement de marque. Aucune action de leur part.
    """
    if IS_WINDOWS:
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        d = os.path.join(base, "OmniTrade Hub")
        old = os.path.join(base, "ZellaTrack")
    elif IS_MACOS:
        base = os.path.expanduser("~/Library/Application Support")
        d = os.path.join(base, "OmniTrade Hub")
        old = os.path.join(base, "ZellaTrack")
    else:
        base = (os.environ.get("XDG_CONFIG_HOME")
                or os.path.expanduser("~/.config"))
        d = os.path.join(base, "OmniTrade Hub")
        old = os.path.join(base, "ZellaTrack")
    try:
        # Reprise silencieuse de l'ancien dossier s'il existe et que le
        # nouveau n'a pas encore été créé.
        if os.path.isdir(old) and not os.path.isdir(d):
            try:
                import shutil
                shutil.copytree(old, d)
            except Exception:
                pass
        os.makedirs(d, exist_ok=True)
    except Exception:
        d = os.path.expanduser("~")
    return d


mt5 = None
NATIVE_API = False
MT5_IMPORT_ERROR = None
if IS_MACOS:
    # Le paquet MetaTrader5 ne publie aucune roue macOS : ne pas même tenter
    # l'import (il serait de toute façon absent du binaire gelé).
    MT5_IMPORT_ERROR = "macOS : paquet MetaTrader5 indisponible (Windows uniquement)"
else:
    try:
        import MetaTrader5 as mt5  # type: ignore
        NATIVE_API = True
    except Exception as _e:        # ImportError, OSError (DLL absente)...
        mt5 = None
        NATIVE_API = False
        MT5_IMPORT_ERROR = str(_e) or type(_e).__name__


def _early_token_cli():
    """--show-token / --reset-token AVANT l'import Flask.

    Sinon, si Flask manque, le print « Flask manquant » partait sur stdout
    et le lanceur Windows le recopiait dans --token. argparse voyait alors
    « unrecognized arguments: Flask manquant… » et le pont ne démarrait pas.
    """
    if "--show-token" not in sys.argv and "--reset-token" not in sys.argv:
        return
    d = user_data_dir()
    f = os.path.join(d, "acces.key")
    if "--reset-token" in sys.argv:
        try:
            if os.path.isfile(f):
                os.remove(f)
        except Exception:
            pass
    tok = ""
    try:
        if os.path.isfile(f):
            tok = open(f, "r", encoding="utf-8").read().strip()
    except Exception:
        tok = ""
    if not re.fullmatch(r"[A-Za-z0-9_-]{24,64}", tok or ""):
        import secrets
        tok = secrets.token_urlsafe(24)[:32]
        try:
            os.makedirs(d, exist_ok=True)
            with open(f, "w", encoding="utf-8") as fh:
                fh.write(tok)
            try:
                os.chmod(f, 0o600)
            except Exception:
                pass
        except Exception:
            pass
    print(tok, flush=True)
    sys.exit(0)


_early_token_cli()

try:
    from flask import Flask, jsonify, request, make_response
    from flask_cors import CORS
except ImportError:  # pragma: no cover
    # Tente une installation unique, puis réimporte. Message sur STDERR
    # uniquement : stdout doit rester propre pour le lanceur.
    _ok = False
    try:
        print("[*] Flask absent — installation automatique…", file=sys.stderr, flush=True)
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "--disable-pip-version-check",
             "flask", "flask-cors"],
            timeout=240)
        from flask import Flask, jsonify, request, make_response
        from flask_cors import CORS
        _ok = True
    except Exception:
        try:
            subprocess.check_call(
                [sys.executable, "-m", "ensurepip", "--upgrade"], timeout=120)
            subprocess.check_call(
                [sys.executable, "-m", "pip", "install", "--user",
                 "--disable-pip-version-check", "flask", "flask-cors"],
                timeout=240)
            from flask import Flask, jsonify, request, make_response
            from flask_cors import CORS
            _ok = True
        except Exception:
            _ok = False
    if not _ok:
        print("[!] Flask manquant. Installez-le une fois :", file=sys.stderr)
        print("    %s -m pip install flask flask-cors" % sys.executable, file=sys.stderr)
        sys.exit(1)

try:
    import websockets  # optionnel (--ws)
except ImportError:  # pragma: no cover
    websockets = None


VERSION = "8.87"        # aligné sur la version de l'application
log = logging.getLogger("mt5-bridge")

# ─────────────────────────────────────────────────────────────────────────────
# Configuration globale (remplie par main())
# ─────────────────────────────────────────────────────────────────────────────
CFG = {
    "token": None,
    "http_port": 8765,
    "ws_port": 8766,
    "host": "127.0.0.1",
    "days": 365,
    "mae": True,          # calcul MAE/MFE (plus lent : lit les bougies M1)
    "push_interval": 15,  # secondes entre deux push WebSocket
    "login": None,
    "password": None,
    "server": None,
    "terminal": None,
    "data_dir": None,        # mode fichier : dossier MQL5/Files
}

_MT5_LOCK = threading.RLock()   # le module MetaTrader5 n'est PAS thread-safe
_CACHE = {"payload": None, "ts": 0.0}
_CACHE_TTL = 3.0                # secondes


# ═════════════════════════════════════════════════════════════════════════════
#  CONNEXION MT5
# ═════════════════════════════════════════════════════════════════════════════
def mt5_connect(force=False):
    """Initialise la connexion (API native) ou vérifie les fichiers (mode fichier)."""
    if not NATIVE_API:
        # Mode fichier : « connecté » = account.json présent et lisible.
        return _read_json(os.path.join(file_data_dir(), "account.json")) is not None
    with _MT5_LOCK:
        if not force and mt5.terminal_info() is not None:
            return True

        kwargs = {}
        if CFG["terminal"]:
            kwargs["path"] = CFG["terminal"]
        if CFG["login"]:
            kwargs["login"] = int(CFG["login"])
        if CFG["password"]:
            kwargs["password"] = CFG["password"]
        if CFG["server"]:
            kwargs["server"] = CFG["server"]

        ok = mt5.initialize(**kwargs) if kwargs else mt5.initialize()
        if not ok:
            log.error("initialize() a échoué : %s", mt5.last_error())
            return False

        acc = mt5.account_info()
        if acc:
            log.info("Connecté  →  #%s  %s  (%s)  levier 1:%s",
                     acc.login, acc.server, acc.currency, acc.leverage)
        return True


def mt5_shutdown():
    if not NATIVE_API:
        return
    with _MT5_LOCK:
        try:
            mt5.shutdown()
        except Exception:
            pass


# ═════════════════════════════════════════════════════════════════════════════
#  HELPERS
# ═════════════════════════════════════════════════════════════════════════════
def _iso(ts):
    """timestamp MT5 (secondes, heure serveur ≈ UTC) -> ISO 8601 UTC."""
    if not ts:
        return None
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()


# ═════════════════════════════════════════════════════════════════════════════
#  MODE FICHIER (macOS / Linux) — lecture des JSON écrits par l'EA
# ═════════════════════════════════════════════════════════════════════════════
def default_data_dirs():
    """Emplacements usuels du dossier MQL5/Files selon la plateforme.

    Détection AUTOMATIQUE, sans aucune saisie de l'utilisateur final.
    Les dossiers contenant réellement account.json sont remontés en tête, et
    à égalité le plus récemment modifié gagne (utile quand plusieurs terminaux
    ou plusieurs comptes cohabitent sur la machine).
    """
    home = os.path.expanduser("~")
    pats = []
    if IS_MACOS:
        # MT5 pour Mac = habillage Wine : les données vivent dans le préfixe.
        pats += [
            # Portage officiel MetaQuotes (Wine embarqué)
            os.path.join(home, "Library/Application Support/net.metaquotes.wine.metatrader5",
                         "drive_c/users/*/AppData/Roaming/MetaQuotes/Terminal/*/MQL5/Files"),
            os.path.join(home, "Library/Application Support/net.metaquotes.wine.metatrader5",
                         "drive_c/Program Files/MetaTrader 5/MQL5/Files"),
            # Variantes brokers (le bundle est renommé : ICMarkets, Pepperstone…)
            os.path.join(home, "Library/Application Support/*.wine.*",
                         "drive_c/users/*/AppData/Roaming/MetaQuotes/Terminal/*/MQL5/Files"),
            os.path.join(home, "Library/Application Support/MetaTrader 5",
                         "Bottles/metatrader5/drive_c/Program Files/MetaTrader 5/MQL5/Files"),
            # PlayOnMac / CrossOver / Wineskin
            os.path.join(home, "Library/Application Support/CrossOver/Bottles/*",
                         "drive_c/users/*/AppData/Roaming/MetaQuotes/Terminal/*/MQL5/Files"),
            os.path.join(home, "Library/PlayOnMac/wineprefix/*",
                         "drive_c/users/*/AppData/Roaming/MetaQuotes/Terminal/*/MQL5/Files"),
            os.path.join(home, ".wine/drive_c/users/*/AppData/Roaming/MetaQuotes/Terminal/*/MQL5/Files"),
        ]
    elif IS_WINDOWS:
        appdata = os.environ.get("APPDATA") or os.path.join(home, "AppData/Roaming")
        pats += [
            os.path.join(appdata, "MetaQuotes/Terminal/*/MQL5/Files"),
            os.path.join(home, "AppData/Roaming/MetaQuotes/Terminal/*/MQL5/Files"),
            # Installations « portables » (/portable) : MQL5 à côté du terminal
            r"C:\Program Files\MetaTrader 5\MQL5\Files",
            r"C:\Program Files (x86)\MetaTrader 5\MQL5\Files",
            r"C:\Program Files\*MetaTrader*\MQL5\Files",
            os.path.join(home, "Desktop/*MetaTrader*/MQL5/Files"),
        ]
    else:
        pats += [os.path.join(home, ".wine/drive_c/users/*/AppData/Roaming/MetaQuotes/Terminal/*/MQL5/Files"),
                 os.path.join(home, ".mt5/MQL5/Files")]

    # Replis manuels universels : l'utilisateur peut toujours déposer les JSON ici.
    pats += [os.path.join(home, "OmniTrade Hub"),
             os.path.join(app_dir(), "data"),
             os.path.join(user_data_dir(), "data")]

    found, seen = [], set()
    for pat in pats:
        for d in sorted(glob.glob(pat)):
            rd = os.path.realpath(d)
            if rd not in seen and os.path.isdir(rd):
                seen.add(rd)
                found.append(rd)

    def _rank(d):
        acc = os.path.join(d, "account.json")
        has = os.path.isfile(acc)
        mtime = os.path.getmtime(acc) if has else 0
        return (0 if has else 1, -mtime)

    found.sort(key=_rank)
    return found


# ═════════════════════════════════════════════════════════════════════════════
#  LIBÉRATION AUTOMATIQUE DU PORT  (« Address already in use »)
# -----------------------------------------------------------------------------
#  Cas réel : l'utilisateur ferme la fenêtre du pont sans l'arrêter proprement,
#  double-clique à nouveau -> le port 8765 est encore tenu par l'ancien
#  processus. On tue le squatteur AVANT de démarrer, sans jamais se suicider.
# ═════════════════════════════════════════════════════════════════════════════
def port_is_busy(port, host="127.0.0.1"):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.6)
        try:
            return s.connect_ex((host, int(port))) == 0
        except OSError:
            return False


def pids_on_port(port):
    """PID des processus qui écoutent sur `port` (hors nous-mêmes)."""
    me = {os.getpid(), os.getppid()}
    pids = set()
    try:
        if IS_WINDOWS:
            out = subprocess.run(
                ["netstat", "-ano", "-p", "TCP"], capture_output=True,
                text=True, timeout=10,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
            for line in out.splitlines():
                parts = line.split()
                if len(parts) >= 5 and parts[0].upper() == "TCP" \
                        and parts[-2].upper() == "LISTENING" \
                        and parts[1].rsplit(":", 1)[-1] == str(port):
                    if parts[-1].isdigit():
                        pids.add(int(parts[-1]))
        else:
            # lsof est présent d'origine sur macOS ; `ss` sert de repli Linux.
            try:
                out = subprocess.run(
                    ["lsof", "-nP", "-iTCP:%s" % port, "-sTCP:LISTEN", "-t"],
                    capture_output=True, text=True, timeout=10).stdout
                for tok in out.split():
                    if tok.strip().isdigit():
                        pids.add(int(tok))
            except FileNotFoundError:
                out = subprocess.run(
                    ["ss", "-lptnH", "sport = :%s" % port],
                    capture_output=True, text=True, timeout=10).stdout
                for m in re.finditer(r"pid=(\d+)", out):
                    pids.add(int(m.group(1)))
    except Exception as e:
        log.debug("Inspection du port %s impossible : %s", port, e)
    return sorted(pids - me)


def free_port(port, quiet=False):
    """Termine tout processus qui occupe `port`. Retourne True si libre."""
    if not port_is_busy(port):
        return True
    victims = pids_on_port(port)
    if not victims:
        if not quiet:
            print("[!] Port %s occupé par un processus inaccessible." % port)
        return False
    for pid in victims:
        if not quiet:
            print("  · Port %s occupé par le PID %s — libération…" % (port, pid))
        try:
            if IS_WINDOWS:
                subprocess.run(["taskkill", "/PID", str(pid), "/F", "/T"],
                               capture_output=True, timeout=10,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            else:
                os.kill(pid, 15)                       # SIGTERM d'abord
        except Exception as e:
            log.debug("kill %s : %s", pid, e)
    for _ in range(20):                                # jusqu'à 2 s
        if not port_is_busy(port):
            return True
        time.sleep(0.1)
    if not IS_WINDOWS:                                 # insistance : SIGKILL
        for pid in victims:
            try:
                os.kill(pid, 9)
            except Exception:
                pass
        for _ in range(10):
            if not port_is_busy(port):
                return True
            time.sleep(0.1)
    return not port_is_busy(port)


def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return None
    except Exception as e:
        log.warning("Lecture %s : %s", os.path.basename(path), e)
        return None


def file_data_dir():
    """Dossier de données effectif (option --data-dir ou détection auto)."""
    if CFG.get("data_dir"):
        return CFG["data_dir"]
    for d in default_data_dirs():
        if os.path.isfile(os.path.join(d, "account.json")):
            return d
    dirs = default_data_dirs()
    return dirs[0] if dirs else os.path.expanduser("~/OmniTrade Hub")


def _ts_of(v):
    """Normalise une date EA (secondes, millisecondes ou texte) en epoch (s)."""
    if v in (None, "", 0, "0"):
        return 0
    if isinstance(v, (int, float)):
        v = float(v)
        return int(v / 1000) if v > 1e11 else int(v)
    txt = str(v).strip().replace(".", "-", 2)
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return int(datetime.strptime(txt[:19], fmt).replace(tzinfo=timezone.utc).timestamp())
        except ValueError:
            continue
    return 0


def file_get_account():
    d = _read_json(os.path.join(file_data_dir(), "account.json"))
    if not d:
        return None
    srv = d.get("server") or d.get("account_server") or ""
    code, label, src = resolve_account_status(srv, d.get("trade_mode"))
    return {
        "login": d.get("login") or d.get("account") or 0,
        "account_id": d.get("login") or 0,
        "name": d.get("name", ""),
        "server": srv,
        "account_server": srv,
        "currency": d.get("currency", "USD"),
        "leverage": d.get("leverage", 0),
        "balance": round(float(d.get("balance", 0) or 0), 2),
        "equity": round(float(d.get("equity", 0) or 0), 2),
        "profit": round(float(d.get("profit", 0) or 0), 2),
        "margin": round(float(d.get("margin", 0) or 0), 2),
        "margin_free": round(float(d.get("margin_free", 0) or 0), 2),
        "margin_level": round(float(d.get("margin_level", 0) or 0), 2),
        "trade_mode": code,
        "trade_mode_raw": d.get("trade_mode"),
        "trade_mode_str": label,
        "status_source": src,
        "is_live": code == 2,
        "company": d.get("company", ""),
        "broker": d.get("company") or srv,
        "source": "file",
    }


def _file_trade(t, acct):
    """Convertit une ligne d'historique de l'EA au contrat JSON du pont."""
    t_open = _ts_of(t.get("open_time") or t.get("time_open") or t.get("time"))
    t_close = _ts_of(t.get("close_time") or t.get("time_close") or t.get("time"))
    if not t_open and t_close:
        t_open = t_close
    if not t_close and t_open:
        t_close = t_open

    sym = t.get("symbol") or ""
    is_buy = str(t.get("type", "")).upper() in ("BUY", "0", "LONG")
    entry = float(t.get("open_price") or t.get("price_open") or t.get("entry") or 0)
    exit_ = float(t.get("close_price") or t.get("price_close") or t.get("exit") or 0)
    sl = float(t.get("sl") or 0)
    tp = float(t.get("tp") or 0)
    vol = float(t.get("volume") or t.get("lots") or 0)
    gross = float(t.get("profit") or 0)
    swap = float(t.get("swap") or 0)
    comm = float(t.get("commission") or 0)
    net = t.get("pnl")
    net = float(net) if net is not None else gross + swap + comm

    def iso(ts):
        return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat() if ts else None

    def txt(ts):
        return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S") if ts else None

    return {
        "ticket": t.get("ticket") or t.get("position_id") or t.get("id"),
        "id": str(t.get("ticket") or t.get("position_id") or ""),
        "position_id": t.get("position_id") or t.get("ticket"),
        "symbol": sym,
        "market": _market_of(sym),
        "direction": "Long" if is_buy else "Short",
        "type": "BUY" if is_buy else "SELL",
        "entry": entry or None, "exit": exit_ or None,
        "open_price": entry or None, "close_price": exit_ or None,
        "sl": sl or None, "tp": tp or None,
        "volume": vol, "lots": vol,
        "profit": round(gross, 2), "swap": round(swap, 2),
        "commission": round(comm, 2), "pnl": round(net, 2),
        "result": "Win" if net > 0 else ("Loss" if net < 0 else "Break-even"),
        "open_time": iso(t_open), "close_time": iso(t_close), "date": iso(t_close),
        "open_time_str": txt(t_open), "close_time_str": txt(t_close),
        "open_timestamp": t_open * 1000 if t_open else None,
        "timestamp": t_close * 1000 if t_close else None,
        "duration_min": int(max(0, t_close - t_open) // 60) if (t_open and t_close) else 0,
        "session": _session_of(t_open) if t_open else "",
        "rrr": _rrr(entry, sl, exit_) if (entry and sl and exit_) else None,
        "rrr_target": _rrr(entry, sl, tp) if (entry and sl and tp) else None,
        "mae": float(t.get("mae") or 0), "mfe": float(t.get("mfe") or 0),
        "magic": int(t.get("magic") or 0),
        "comment": t.get("comment", ""),
        "login": acct.get("login") if acct else 0,
        "account_id": acct.get("login") if acct else 0,
        "server": acct.get("server") if acct else "",
    }


def file_get_trades(days=None):
    d = _read_json(os.path.join(file_data_dir(), "trades.json")) or \
        _read_json(os.path.join(file_data_dir(), "closed_trades.json"))
    if not d:
        return []
    rows = d.get("trades", d) if isinstance(d, dict) else d
    acct = file_get_account() or {}
    out = [_file_trade(t, acct) for t in rows if isinstance(t, dict)]
    if days:
        limit = (datetime.now(tz=timezone.utc) - timedelta(days=int(days))).timestamp() * 1000
        out = [t for t in out if not t["timestamp"] or t["timestamp"] >= limit]
    out.sort(key=lambda t: t.get("close_time") or "", reverse=True)
    return out


def file_get_positions():
    d = _read_json(os.path.join(file_data_dir(), "positions.json"))
    if not d:
        return []
    rows = d.get("positions", d) if isinstance(d, dict) else d
    out = []
    for p in rows:
        if not isinstance(p, dict):
            continue
        is_buy = str(p.get("type", "")).upper() in ("BUY", "0", "LONG")
        sl, tp = float(p.get("sl") or 0), float(p.get("tp") or 0)
        po = float(p.get("open_price") or p.get("price_open") or 0)
        t_open = _ts_of(p.get("open_time") or p.get("time"))
        out.append({
            "ticket": p.get("ticket"), "symbol": p.get("symbol", ""),
            "market": _market_of(p.get("symbol", "")),
            "direction": "Long" if is_buy else "Short",
            "volume": float(p.get("volume") or p.get("lots") or 0),
            "price_open": po or None,
            "price_current": float(p.get("price_current") or 0) or None,
            "sl": sl or None, "tp": tp or None,
            "profit": round(float(p.get("profit") or 0), 2),
            "swap": round(float(p.get("swap") or 0), 2),
            "open_time": datetime.fromtimestamp(t_open, tz=timezone.utc).isoformat() if t_open else None,
            "duration_min": int(max(0, time.time() - t_open) // 60) if t_open else 0,
            "magic": int(p.get("magic") or 0), "comment": p.get("comment", ""),
            "rrr_target": _rrr(po, sl, tp) if (po and sl and tp) else None,
        })
    return out


# ═════════════════════════════════════════════════════════════════════════════
#  STATUT DU COMPTE — le NOM DU SERVEUR fait autorité
# -----------------------------------------------------------------------------
#  Les propfirms opèrent sur des serveurs où le capital est simulé : MT5 renvoie
#  alors trade_mode = 0 (démo) même sur un compte financé réel. C'est le cas de
#  « AudaCityGlobal-Live » (#206927), classé à tort en DÉMO.
#  Règle appliquée, dans l'ordre :
#     1. serveur contenant DEMO/PRACTICE/TEST/TRIAL  -> DÉMO
#     2. serveur contenant LIVE/REAL/PROD            -> REAL LIVE
#     3. sinon repli sur trade_mode (0 démo, 1 concours, 2 réel)
#     4. sinon NON VÉRIFIÉ (jamais présumé réel)
# ═════════════════════════════════════════════════════════════════════════════
SRV_DEMO = ("DEMO", "PRACTICE", "TEST", "TRIAL", "SANDBOX", "PAPER")
SRV_LIVE = ("LIVE", "REAL", "PROD")


def resolve_account_status(server, trade_mode=None):
    """Retourne (code, libellé, source). code: 0 démo · 1 concours · 2 réel."""
    srv = (server or "").upper()
    if srv:
        # Les marqueurs de démo priment : « XyzLive-Demo » reste une démo.
        if any(k in srv for k in SRV_DEMO):
            return 0, "DÉMO", "server"
        if any(k in srv for k in SRV_LIVE):
            return 2, "REAL LIVE", "server"
    if trade_mode == 0:
        return 0, "DÉMO", "trade_mode"
    if trade_mode == 1:
        return 1, "CONCOURS", "trade_mode"
    if trade_mode == 2:
        return 2, "REAL LIVE", "trade_mode"
    return -1, "NON VÉRIFIÉ", "none"


def _session_of(ts):
    """Session de trading déduite de l'heure UTC d'ouverture."""
    if not ts:
        return "Autre"
    h = datetime.fromtimestamp(int(ts), tz=timezone.utc).hour
    if 0 <= h < 7:
        return "Asian"
    if 7 <= h < 12:
        return "London"
    if 12 <= h < 16:
        return "Overlap"
    if 16 <= h < 21:
        return "New York"
    return "Autre"


_CRYPTO = ("BTC", "ETH", "XRP", "LTC", "SOL", "BNB", "ADA", "DOGE",
           "DOT", "AVAX", "MATIC", "LINK", "USDT", "TRX", "SHIB")
_SYNTH = ("VOLATILITY", "BOOM", "CRASH", "STEP", "JUMP", "RANGE BREAK",
          "DRIFT", "SYNTHETIC", "R_", "1HZ")


def _market_of(symbol):
    """Classe le symbole dans les 3 marchés de OmniTrade Hub : Forex / Crypto / Synthétique."""
    s = (symbol or "").upper()
    if any(k in s for k in _SYNTH):
        return "Synthétique"
    if any(k in s for k in _CRYPTO):
        return "Crypto"
    return "Forex"


def _digits(symbol):
    info = mt5.symbol_info(symbol)
    return info.digits if info else 5


def _round(v, symbol=None):
    if v is None:
        return None
    try:
        d = _digits(symbol) if symbol else 5
        return round(float(v), d)
    except Exception:
        return float(v)


def _money(symbol, order_type, volume, price_open, price_target):
    """Convertit un déplacement de prix en montant (devise du compte)."""
    try:
        p = mt5.order_calc_profit(order_type, symbol, volume,
                                  float(price_open), float(price_target))
        if p is not None:
            return float(p)
    except Exception:
        pass
    # Repli : approximation via tick_value / tick_size
    info = mt5.symbol_info(symbol)
    if not info or not info.trade_tick_size:
        return 0.0
    ticks = (float(price_target) - float(price_open)) / info.trade_tick_size
    val = ticks * info.trade_tick_value * float(volume)
    return val if order_type == mt5.ORDER_TYPE_BUY else -val


def _mae_mfe(symbol, is_buy, volume, price_open, t_open, t_close):
    """
    MAE / MFE en montant : on lit les bougies M1 entre l'ouverture et la clôture
    et on prend l'excursion la plus défavorable / la plus favorable.
    Retourne (mae_abs, mfe_abs) en devise du compte, valeurs positives.
    """
    try:
        if not t_open or not t_close or t_close <= t_open:
            return 0.0, 0.0
        d_from = datetime.fromtimestamp(int(t_open) - 60, tz=timezone.utc)
        d_to = datetime.fromtimestamp(int(t_close) + 60, tz=timezone.utc)

        # Choix du TF selon la durée pour éviter de charger 100k bougies
        span = int(t_close) - int(t_open)
        if span <= 6 * 3600:
            tf = mt5.TIMEFRAME_M1
        elif span <= 3 * 86400:
            tf = mt5.TIMEFRAME_M5
        elif span <= 30 * 86400:
            tf = mt5.TIMEFRAME_M30
        else:
            tf = mt5.TIMEFRAME_H4

        if not mt5.symbol_select(symbol, True):
            return 0.0, 0.0
        rates = mt5.copy_rates_range(symbol, tf, d_from, d_to)
        if rates is None or len(rates) == 0:
            return 0.0, 0.0

        hi = max(float(r["high"]) for r in rates)
        lo = min(float(r["low"]) for r in rates)
        otype = mt5.ORDER_TYPE_BUY if is_buy else mt5.ORDER_TYPE_SELL
        worst = lo if is_buy else hi
        best = hi if is_buy else lo

        mae = _money(symbol, otype, volume, price_open, worst)
        mfe = _money(symbol, otype, volume, price_open, best)
        return abs(min(mae, 0.0)), abs(max(mfe, 0.0))
    except Exception as e:  # pragma: no cover
        log.debug("MAE/MFE %s : %s", symbol, e)
        return 0.0, 0.0


def _rrr(entry, sl, exit_):
    try:
        entry, sl, exit_ = float(entry), float(sl), float(exit_)
        risk = abs(entry - sl)
        reward = abs(exit_ - entry)
        return round(reward / risk, 3) if risk > 0 else None
    except Exception:
        return None


# ═════════════════════════════════════════════════════════════════════════════
#  EXTRACTION DES DONNÉES
# ═════════════════════════════════════════════════════════════════════════════
def get_account():
    if not NATIVE_API:
        return file_get_account()
    with _MT5_LOCK:
        if not mt5_connect():
            return None
        a = mt5.account_info()
        if a is None:
            return None
        code, label, src = resolve_account_status(a.server, int(a.trade_mode))
        return {
            "login": a.login,
            "account_id": a.login,          # alias de repli
            "name": a.name,
            "server": a.server,
            "account_server": a.server,     # alias
            "currency": a.currency,
            "leverage": a.leverage,
            "balance": round(a.balance, 2),
            "equity": round(a.equity, 2),
            "profit": round(a.profit, 2),
            "margin": round(a.margin, 2),
            "margin_free": round(a.margin_free, 2),
            "margin_level": round(a.margin_level, 2) if a.margin_level else 0.0,
            # ── Statut résolu (serveur prioritaire) ──
            "trade_mode": code,                     # 0 démo · 1 concours · 2 réel
            "trade_mode_raw": int(a.trade_mode),    # valeur brute MT5
            "trade_mode_str": label,                # « REAL LIVE » / « DÉMO »
            "status_source": src,                   # server | trade_mode | none
            "is_live": code == 2,
            "company": a.company,
            "broker": a.company or a.server,        # jamais vide, jamais « - »
        }


def get_positions():
    """Positions actuellement ouvertes."""
    if not NATIVE_API:
        return file_get_positions()
    with _MT5_LOCK:
        if not mt5_connect():
            return []
        pos = mt5.positions_get()
        if pos is None:
            return []
        out = []
        for p in pos:
            is_buy = p.type == mt5.POSITION_TYPE_BUY
            out.append({
                "ticket": int(p.ticket),
                "symbol": p.symbol,
                "market": _market_of(p.symbol),
                "direction": "Long" if is_buy else "Short",
                "volume": float(p.volume),
                "price_open": _round(p.price_open, p.symbol),
                "price_current": _round(p.price_current, p.symbol),
                "sl": _round(p.sl, p.symbol) or None,
                "tp": _round(p.tp, p.symbol) or None,
                "profit": round(float(p.profit), 2),
                "swap": round(float(p.swap), 2),
                "open_time": _iso(p.time),
                "duration_min": int(max(0, time.time() - p.time) // 60),
                "magic": int(p.magic),
                "comment": p.comment,
                "rrr_target": _rrr(p.price_open, p.sl, p.tp) if (p.sl and p.tp) else None,
            })
        return out


def get_closed_trades(days=None, with_mae=None):
    """
    Reconstitue les positions FERMÉES à partir des deals de l'historique,
    groupés par position_id (in / out / out_by / partiels).
    """
    days = CFG["days"] if days is None else days
    with_mae = CFG["mae"] if with_mae is None else with_mae

    if not NATIVE_API:
        return file_get_trades(days)

    with _MT5_LOCK:
        if not mt5_connect():
            return []

        _a = mt5.account_info()
        acct_login = int(_a.login) if _a else 0
        acct_server = _a.server if _a else ""

        d_to = datetime.now(tz=timezone.utc) + timedelta(days=1)
        d_from = d_to - timedelta(days=int(days) + 1)

        deals = mt5.history_deals_get(d_from, d_to)
        if deals is None:
            log.warning("history_deals_get: %s", mt5.last_error())
            return []

        # ── Regroupement par position ──────────────────────────────────────
        groups = {}
        for d in deals:
            if d.entry == mt5.DEAL_ENTRY_STATE:
                continue
            if d.type not in (mt5.DEAL_TYPE_BUY, mt5.DEAL_TYPE_SELL):
                continue  # balance / credit / bonus ...
            groups.setdefault(int(d.position_id), []).append(d)

        # ── SL / TP : plusieurs sources, par ordre de fiabilité ──────────
        # Lire uniquement l'ordre d'entrée ne suffit pas : un stop déplacé en
        # cours de trade, ou porté par le deal lui-même, était perdu — d'où
        # des colonnes SL/TP vides dans le journal alors que le trader place
        # systématiquement stop et objectif.
        orders = mt5.history_orders_get(d_from, d_to) or []
        sltp = {}
        for o in orders:
            pid = int(o.position_id) if o.position_id else None
            if pid is None:
                continue
            cur = sltp.get(pid, {"sl": 0.0, "tp": 0.0})
            # Dernière valeur non nulle = niveau réellement actif à la clôture.
            if o.sl:
                cur["sl"] = float(o.sl)
            if o.tp:
                cur["tp"] = float(o.tp)
            sltp[pid] = cur
        # Repli : niveaux portés par les deals eux-mêmes (selon le broker).
        for d in deals:
            pid = int(getattr(d, "position_id", 0) or 0)
            if not pid:
                continue
            cur = sltp.setdefault(pid, {"sl": 0.0, "tp": 0.0})
            ds_ = float(getattr(d, "sl", 0.0) or 0.0)
            dt_ = float(getattr(d, "tp", 0.0) or 0.0)
            if ds_ and not cur["sl"]:
                cur["sl"] = ds_
            if dt_ and not cur["tp"]:
                cur["tp"] = dt_

        trades = []
        for pid, ds in groups.items():
            ds.sort(key=lambda x: (x.time_msc, x.ticket))
            ins = [d for d in ds if d.entry == mt5.DEAL_ENTRY_IN]
            outs = [d for d in ds if d.entry in (mt5.DEAL_ENTRY_OUT,
                                                 mt5.DEAL_ENTRY_OUT_BY,
                                                 mt5.DEAL_ENTRY_INOUT)]
            if not ins or not outs:
                continue  # position encore ouverte (ou incomplète)

            symbol = ins[0].symbol
            vol_in = sum(d.volume for d in ins) or 1.0
            vol_out = sum(d.volume for d in outs) or vol_in
            entry = sum(d.price * d.volume for d in ins) / vol_in
            exit_ = sum(d.price * d.volume for d in outs) / vol_out

            is_buy = ins[0].type == mt5.DEAL_TYPE_BUY
            gross = sum(float(d.profit) for d in ds)
            swap = sum(float(d.swap) for d in ds)
            comm = sum(float(d.commission) for d in ds)
            fee = sum(float(getattr(d, "fee", 0.0) or 0.0) for d in ds)
            net = gross + swap + comm + fee

            t_open = int(ins[0].time)
            t_close = int(outs[-1].time)
            st = sltp.get(pid, {})
            sl = st.get("sl") or 0.0
            tp = st.get("tp") or 0.0

            mae = mfe = 0.0
            if with_mae:
                mae, mfe = _mae_mfe(symbol, is_buy, vol_in, entry, t_open, t_close)
                # cohérence minimale avec le résultat réel
                mfe = max(mfe, max(net, 0.0))
                mae = max(mae, abs(min(net, 0.0)))

            comment = next((d.comment for d in reversed(ds) if d.comment), "")

            # ── Dates : converties explicitement, jamais nulles ──
            # (une valeur 0/None produisait « 21/01/70 » côté JavaScript)
            open_iso = _iso(t_open)
            close_iso = _iso(t_close)
            open_str = datetime.fromtimestamp(t_open, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
            close_str = datetime.fromtimestamp(t_close, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

            rrr_real = _rrr(entry, sl, exit_) if sl else None
            direction = "Long" if is_buy else "Short"

            trades.append({
                # ── identité (anti-doublons côté web) ──
                "ticket": pid,
                "id": str(pid),                     # alias
                "position_id": pid,                 # alias
                "deal_tickets": [int(d.ticket) for d in ds],
                # ── marché ──
                "symbol": symbol,
                "market": _market_of(symbol),
                "direction": direction,
                "type": "BUY" if is_buy else "SELL",   # alias
                # ── prix (toujours renseignés) ──
                "entry": _round(entry, symbol),
                "exit": _round(exit_, symbol),
                "open_price": _round(entry, symbol),   # alias
                "close_price": _round(exit_, symbol),  # alias
                "sl": _round(sl, symbol) if sl else None,
                "tp": _round(tp, symbol) if tp else None,
                "volume": round(vol_in, 2),
                "lots": round(vol_in, 2),              # alias
                # ── argent ──
                "profit": round(gross, 2),
                "swap": round(swap, 2),
                "commission": round(comm + fee, 2),
                "pnl": round(net, 2),
                "result": "Win" if net > 0 else ("Loss" if net < 0 else "Break-even"),
                # ── temps (ISO + lisible + millisecondes JS) ──
                "open_time": open_iso,
                "close_time": close_iso,
                "date": close_iso,
                "open_time_str": open_str,
                "close_time_str": close_str,
                "open_timestamp": int(t_open) * 1000,
                "timestamp": int(t_close) * 1000,
                "duration_min": int(max(0, t_close - t_open) // 60),
                "session": _session_of(t_open),
                # ── analytique ──
                "rrr": rrr_real,
                "rrr_target": _rrr(entry, sl, tp) if (sl and tp) else None,
                "mae": round(mae, 2),
                "mfe": round(mfe, 2),
                # ── divers ──
                "magic": int(ins[0].magic),
                "comment": comment,
                "reason": int(getattr(outs[-1], "reason", 0)),
                # ── rattachement au compte (repli si le bloc account est vide) ──
                "login": acct_login,
                "account_id": acct_login,
                "account": acct_login,
                "server": acct_server,
            })

        trades.sort(key=lambda t: t["close_time"] or "", reverse=True)
        return trades


def build_payload(days=None, with_mae=None):
    """Charge utile complète envoyée à OmniTrade Hub (HTTP /api/sync et push WS)."""
    return {
        "ok": True,
        "version": VERSION,
        "server_time": datetime.now(tz=timezone.utc).isoformat(),
        "account": get_account(),
        "trades": get_closed_trades(days, with_mae),
        "positions": get_positions(),
    }


def cached_payload(days=None, with_mae=None, ttl=_CACHE_TTL):
    now = time.time()
    if _CACHE["payload"] and (now - _CACHE["ts"]) < ttl:
        return _CACHE["payload"]
    p = build_payload(days, with_mae)
    _CACHE["payload"] = p
    _CACHE["ts"] = now
    return p


# ═════════════════════════════════════════════════════════════════════════════
#  API HTTP (Flask)
# ═════════════════════════════════════════════════════════════════════════════
app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": "*"}},
     allow_headers=["Content-Type", "X-ZT-Token", "Authorization"])


# ═══════════════════════════════════════════════════════════════════════════
#  CLÉ D'ACCÈS UNIQUE
# ---------------------------------------------------------------------------
#  Jusqu'à la version 6.8, tous les postes partageaient la même clé,
#  « ZELLA_TOKEN », écrite en clair dans les fichiers livrés. Tant que le
#  moteur n'écoutait que sur 127.0.0.1, le risque restait théorique : seul
#  l'ordinateur lui-même pouvait l'interroger.
#
#  Dès lors que l'accès à distance devient possible (Wi-Fi, tunnel), cette
#  clé unique partagée deviendrait une porte ouverte : n'importe qui
#  connaissant l'adresse lirait les trades, les positions et le compte.
#
#  Chaque installation fabrique donc désormais SA PROPRE clé de 32
#  caractères, au premier démarrage, conservée dans le dossier de
#  configuration de l'utilisateur. Elle ne quitte jamais sa machine.
# ═══════════════════════════════════════════════════════════════════════════

TOKEN_LEGACY = "ZELLA_TOKEN"      # clé historique, encore acceptée en local


def _token_path():
    try:
        return os.path.join(user_data_dir(), "acces.key")
    except Exception:
        return None


def _token_charge_ou_cree():
    """Clé d'accès de CETTE installation.

    Relue si elle existe, créée sinon. Jamais régénérée : une clé qui
    changerait à chaque lancement obligerait à reconfigurer le téléphone
    tous les jours.
    """
    f = _token_path()
    if f:
        try:
            if os.path.isfile(f):
                v = open(f, "r", encoding="utf-8").read().strip()
                if re.fullmatch(r"[A-Za-z0-9_-]{24,64}", v or ""):
                    return v
        except Exception:
            pass
    # 32 caractères tirés d'une source cryptographique.
    import secrets
    v = secrets.token_urlsafe(24)[:32]
    if f:
        try:
            os.makedirs(os.path.dirname(f), exist_ok=True)
            with open(f, "w", encoding="utf-8") as fh:
                fh.write(v)
            os.chmod(f, 0o600)      # lisible par le propriétaire seul
        except Exception:
            pass
    return v


def _ip_locale(addr):
    """La requête vient-elle de l'ordinateur lui-même ?"""
    return addr in ("127.0.0.1", "::1", "localhost", "::ffff:127.0.0.1")


def _auth_ok():
    if not CFG["token"]:
        return True
    given = (request.headers.get("X-ZT-Token")
             or request.headers.get("X-Bridge-Token")     # compat
             or request.args.get("token")
             or (request.headers.get("Authorization", "").replace("Bearer ", "")))
    if given and secrets_compare(given, CFG["token"]):
        return True
    # Tolérance à l'ancienne clé, mais UNIQUEMENT depuis l'ordinateur
    # lui-même : les installations existantes continuent de fonctionner
    # sans reconfiguration, sans jamais ouvrir l'accès à distance.
    if given and secrets_compare(given, TOKEN_LEGACY):
        try:
            return _ip_locale(request.remote_addr or "")
        except Exception:
            return False
    return False


def secrets_compare(a, b):
    """Comparaison à durée constante : ne révèle rien par le temps de réponse."""
    try:
        import hmac
        return hmac.compare_digest(str(a), str(b))
    except Exception:
        return str(a) == str(b)


@app.before_request
def _guard():
    if request.method == "OPTIONS":
        return None
    if request.path.startswith("/api/") and not _auth_ok():
        return jsonify({"ok": False, "error": "unauthorized",
                        "message": "Token invalide ou manquant"}), 401
    # L'application elle-même est protégée de la même façon. Sans cela,
    # n'importe qui atteignant l'adresse verrait l'interface se charger —
    # vide, certes, mais c'est une information de trop, et une invitation
    # à chercher la clé.
    # La route « / » n'existe plus (accès téléphone retiré) : plus rien à
    # protéger de ce côté. Seules les routes /api/ subsistent, ci-dessus.
    return None


def _page_cle_requise():
    """Page sobre affichée quand la clé manque ou ne correspond pas."""
    return (
        "<!doctype html><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<div style=\"font:16px/1.55 system-ui,-apple-system,sans-serif;"
        "padding:28px;max-width:32em;margin:auto;color:#e6edf3;"
        "background:#0d1117;min-height:100vh;box-sizing:border-box\">"
        "<h2 style='color:#ffb020;margin:0 0 14px'>Clé d\u2019accès requise</h2>"
        "<p>Cette adresse donne accès à votre journal de trading. "
        "Elle est protégée par une clé propre à votre ordinateur.</p>"
        "<p style='color:#7d8590'>Ouvrez le lien complet fourni par "
        "OmniTrade Hub sur votre ordinateur : page "
        "<b style='color:#e6edf3'>Accès à distance</b>.</p>"
        "</div>")



# ═══════════════════════════════════════════════════════════════════════════
#  LICENCE — vérification côté pont
# ═══════════════════════════════════════════════════════════════════════════
#  La clé PUBLIQUE ci-dessous ne permet que de VÉRIFIER une signature, jamais
#  d'en fabriquer une. Elle peut donc être lue sans danger par l'utilisateur.
#  La clé privée correspondante reste exclusivement chez l'éditeur.
#
#  Le pont est le bon endroit pour cette vérification : lui seul peut lire
#  l'identifiant matériel (une page web ne le peut pas), et il est distribué
#  sous forme de binaire compilé, ce qui élève nettement la barre.
#  La valeur ci-dessous est la clé publique OFFICIELLE du projet. Elle est
#  dérivée de oth_admin/private_key.txt, qui reste exclusivement chez l'éditeur.
LIC_PUBKEY_BUILTIN = "8f036ca58e3a0cb5db9efff994456d637e0bc2d121b5d3406977642bf7852b94"


def _load_pubkey():
    """Clé publique de vérification.

    Deux sources, dans cet ordre :
      1. un fichier « public_key.txt » livré à côté de l'application ;
      2. à défaut, la valeur intégrée au programme.

    Le fichier permet à l'éditeur de renouveler sa paire de clés sans
    recompiler quoi que ce soit. Il n'expose aucun secret : une clé publique
    sert uniquement à VÉRIFIER une signature, jamais à en fabriquer une.

    Une valeur illisible est ignorée : on retombe alors sur la clé intégrée,
    de sorte qu'un fichier corrompu ne débloque jamais l'application et ne la
    casse pas non plus.
    """
    import re as _re
    seen = []
    for d in _pubkey_dirs():
        f = os.path.join(d, "public_key.txt")
        try:
            if not os.path.isfile(f):
                continue
            raw = open(f, "r", encoding="utf-8", errors="ignore").read().strip()
            cand = "".join(raw.split()).lower()
            if _re.fullmatch(r"[0-9a-f]{64}", cand):
                return cand
            seen.append(f)
        except Exception:
            continue
    if seen:
        # Pas de dépendance à un logger : cette fonction s'exécute très tôt,
        # avant que quoi que ce soit d'autre ne soit initialisé.
        try:
            print("[licence] public_key.txt illisible (%s) : clé integree utilisee"
                  % ", ".join(seen), flush=True)
        except Exception:
            pass
    return LIC_PUBKEY_BUILTIN


def _pubkey_dirs():
    """Emplacements où chercher public_key.txt, du plus proche au plus loin."""
    out = []
    try:
        out.append(os.path.dirname(os.path.abspath(__file__)))
    except Exception:
        pass
    try:
        out.append(os.getcwd())
    except Exception:
        pass
    # Application compilée (.app macOS / dossier PyInstaller)
    try:
        if getattr(sys, "frozen", False):
            exe = os.path.dirname(os.path.abspath(sys.executable))
            out += [exe, os.path.abspath(os.path.join(exe, "..", "Resources")),
                    os.path.abspath(os.path.join(exe, "..", "..", "..", ".."))]
        base = getattr(sys, "_MEIPASS", None)
        if base:
            out.append(base)
    except Exception:
        pass
    vus, res = set(), []
    for d in out:
        if d and d not in vus:
            vus.add(d); res.append(d)
    return res


LIC_PUBKEY = _load_pubkey()

def _load_license_module():
    """Charge le module de licence, quel que soit son emplacement.

    On essaie d'abord l'import normal. S'il échoue (fichier déplacé ou
    renommé), on le cherche à côté du moteur. En dernier ressort on
    renvoie None : l'application se VERROUILLE alors intégralement,
    elle ne devient jamais gratuite.
    """
    try:
        import license_core as m
        return m
    except Exception:
        pass
    try:
        import importlib.util
        # Dans une application compilée, __file__ pointe vers un dossier
        # temporaire d'extraction : le noyau n'y est pas. Il faut donc
        # regarder aussi à côté de l'exécutable ET dans le dossier parent —
        # le binaire vit dans « bin/ » tandis que « 9-licence.py » est à la
        # racine du paquet. Sans cela, le moteur compilé répondait
        # « reason: module » et verrouillait toute l'application.
        bases = []
        try:
            bases.append(os.path.dirname(os.path.abspath(__file__)))
        except Exception:
            pass
        for d in _pubkey_dirs():
            if d not in bases:
                bases.append(d)
            parent = os.path.abspath(os.path.join(d, ".."))
            if parent not in bases:
                bases.append(parent)
        for base in bases:
            for name in ("license_core.py", "9-licence.py", "licence_core.py"):
                path = os.path.join(base, name)
                if os.path.isfile(path):
                    spec = importlib.util.spec_from_file_location("license_core", path)
                    if spec and spec.loader:
                        m = importlib.util.module_from_spec(spec)
                        sys.modules["license_core"] = m
                        spec.loader.exec_module(m)
                        return m
    except Exception:
        pass
    return None


_lic = _load_license_module()


def _lic_state_path():
    """Fichier d'état hors du navigateur : survit à l'effacement des cookies."""
    return os.path.join(user_data_dir(), "license_state.json")


def _lic_key_path():
    return os.path.join(user_data_dir(), "license.key")


def _lic_read_key():
    try:
        with open(_lic_key_path(), encoding="utf-8") as f:
            return f.read().strip()
    except Exception:
        return ""


def _lic_write_key(k):
    try:
        os.makedirs(user_data_dir(), exist_ok=True)
        with open(_lic_key_path(), "w", encoding="utf-8") as f:
            f.write((k or "").strip())
        return True
    except Exception:
        return False


def _lic_refuse(reason):
    """Réponse de refus, identique quelle que soit la cause."""
    return {"valid": False, "reason": reason, "plan": "", "planLabel": "",
            "expires": "", "daysLeft": 0, "machine": "", "serial": ""}


def _lic_selfcheck():
    """Le module de licence est-il authentique ?

    FAILLE CORRIGÉE : il ne suffit pas que le module soit PRÉSENT. Un
    utilisateur peut le remplacer par une version de trois lignes qui
    répond « valide » à tout. Mesuré : une licence « PIRATE » à vie était
    alors acceptée.

    On vérifie donc que le module se comporte comme l'original, sur des
    cas dont NOUS connaissons la réponse :
      1. une clé vide doit être refusée ;
      2. une clé inventée doit être refusée ;
      3. une clé authentique mais signée par une AUTRE clé privée doit
         être refusée (c'est le test qui démasque un faux vérificateur).
    Un module trafiqué qui dit « oui » à tout échoue immédiatement.
    """
    try:
        for bad in ("", "OTH1-INVALIDE", "OTH1-" + ("A" * 160)):
            r = _lic.check_license(bad, LIC_PUBKEY, _lic_state_path())
            if not isinstance(r, dict) or r.get("valid"):
                return False
        # Leurre : une licence PARFAITEMENT formée, mais signée avec une
        # clé privée qui n'est pas la nôtre. Seule une vraie vérification
        # de signature la rejette.
        decoy_sk = hashlib.sha256(b"OmniTradeHub-decoy-key").digest()
        # Le code machine est OBLIGATOIRE depuis la v6.5 : on donne ici celui
        # de la machine courante, sinon make_license lève une exception et
        # l'autotest concluait à tort « module altéré » — ce qui bloquait
        # TOUTES les activations (bug mesuré, corrigé).
        decoy_key, _ = _lic.make_license(decoy_sk.hex(), "life", None,
                                         machine_id=_lic.machine_id())
        r = _lic.check_license(decoy_key, LIC_PUBKEY, _lic_state_path())
        if not isinstance(r, dict) or r.get("valid"):
            return False

        # Le module doit AUSSI refuser de fabriquer une licence sans code
        # machine : un module trafiqué qui l'accepterait permettrait des
        # licences transférables, donc partageables sans limite.
        try:
            _lic.make_license(decoy_sk.hex(), "life", None, machine_id="")
            return False          # aucune exception levée -> module suspect
        except ValueError:
            pass                  # comportement attendu
        # Les fonctions indispensables doivent exister.
        for fn in ("check_license", "make_license", "machine_id",
                   "ed25519_verify", "ed25519_sign"):
            if not callable(getattr(_lic, fn, None)):
                return False
        return True
    except Exception:
        return False


_LIC_TRUSTED = None          # calculé une fois, au premier appel


def _lic_status(key=None):
    """État complet de la licence, prêt pour le client.

    Règle absolue : au moindre doute, on REFUSE. Jamais l'inverse.
    Supprimer, remplacer ou tronquer le module de licence verrouille
    l'application — cela ne la rend jamais gratuite.
    """
    global _LIC_TRUSTED
    if _lic is None:
        # Module absent (supprimé par l'utilisateur, ou fichier corrompu).
        return _lic_refuse("module")
    if _LIC_TRUSTED is None:
        _LIC_TRUSTED = _lic_selfcheck()
    if not _LIC_TRUSTED:
        # Module présent mais qui ne se comporte pas comme l'original.
        return _lic_refuse("altere")
    k = key if key is not None else _lic_read_key()
    try:
        st = _lic.check_license(k, LIC_PUBKEY, _lic_state_path())
    except Exception:
        return _lic_refuse("erreur")
    if not isinstance(st, dict):
        return _lic_refuse("altere")

    # Contre-vérification INDÉPENDANTE : le pont revalide lui-même la
    # signature. Même si le module mentait, la licence serait rejetée ici.
    if st.get("valid"):
        try:
            payload, err = _lic.parse_license(k, LIC_PUBKEY)
            if err or not payload:
                return _lic_refuse("signature")
            raw = json.dumps(payload, sort_keys=True,
                             separators=(",", ":")).encode()
            body = (k or "").strip().upper()[len("OTH1"):].lstrip("-").replace("-", "")
            sig = _lic._b32d(body[-103:])
            if not _lic.ed25519_verify(bytes.fromhex(LIC_PUBKEY), raw, sig):
                return _lic_refuse("signature")
            # L'expiration est recontrôlée ici aussi, sans faire confiance
            # au « daysLeft » renvoyé par le module.
            exp = payload.get("exp", "")
            if exp != "never":
                d = datetime.strptime(exp, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                if (d - datetime.now(timezone.utc)).days < 0:
                    return _lic_refuse("expiree")
        except Exception:
            return _lic_refuse("signature")
    return st


@app.get("/api/license")
def api_license():
    """État courant de la licence + code machine à communiquer à l'éditeur."""
    st = _lic_status()
    st["machineId"] = st.get("machine", "")
    return jsonify({"ok": True, **st})


@app.post("/api/license")
def api_license_activate():
    """Active une clé. Elle n'est enregistrée QUE si elle est valide."""
    data = request.get_json(silent=True) or {}
    key = (data.get("key") or "").strip()
    st = _lic_status(key)
    if st.get("valid"):
        _lic_write_key(key)
    st["machineId"] = st.get("machine", "")
    return jsonify({"ok": bool(st.get("valid")), **st})


@app.post("/api/license/clear")
def api_license_clear():
    """Retire la licence enregistrée (support, changement de poste…)."""
    try:
        os.remove(_lic_key_path())
    except Exception:
        pass
    return jsonify({"ok": True})


# ═══════════════════════════════════════════════════════════════════════════
#  L'APPLICATION SERVIE PAR LE MOTEUR
# ---------------------------------------------------------------------------
#  Sur l'ordinateur, le trader ouvre un fichier « omnitrade-vNN.html » posé
#  sur son disque. Un téléphone, lui, ne peut pas lire un fichier situé sur
#  une autre machine : il lui faut une adresse web.
#
#  Le moteur sert donc lui-même l'application. Le téléphone ouvre
#  http://<adresse-du-PC>:8765/?token=<clé> et retrouve exactement la même
#  interface — mêmes données, même journal, même calendrier.
# ═══════════════════════════════════════════════════════════════════════════

_APP_CACHE = {"chemin": None, "mtime": 0, "html": None}


def _trouver_app():
    """Le fichier HTML de l'application, version la plus récente.

    On ne code aucun nom en dur : le fichier a déjà changé de nom une fois
    (zellatrack → omnitrade) et les lanceurs ont dû être corrigés en
    catastrophe. On prend le numéro de version le plus élevé réellement
    présent, dans les mêmes dossiers que la clé publique.
    """
    meilleur, best_n = None, -1
    vus = set()
    for d in _pubkey_dirs():
        if not d or d in vus:
            continue
        vus.add(d)
        try:
            noms = os.listdir(d)
        except Exception:
            continue
        for prefixe in ("omnitrade-v", "zellatrack-v"):
            for nom in noms:
                if not (nom.startswith(prefixe) and nom.endswith(".html")):
                    continue
                m = re.match(re.escape(prefixe) + r"(\d+)", nom)
                if not m:
                    continue
                n = int(m.group(1))
                # Un fichier OmniTrade l'emporte toujours sur un ZellaTrack.
                score = n + (1000 if prefixe.startswith("omni") else 0)
                if score > best_n:
                    best_n, meilleur = score, os.path.join(d, nom)
    return meilleur


def _lire_app():
    """Contenu du HTML, relu seulement s'il a changé."""
    f = _trouver_app()
    if not f:
        return None, None
    try:
        mt = os.path.getmtime(f)
    except Exception:
        return None, None
    if _APP_CACHE["chemin"] == f and _APP_CACHE["mtime"] == mt and _APP_CACHE["html"]:
        return _APP_CACHE["html"], f
    try:
        with open(f, "r", encoding="utf-8", errors="replace") as fh:
            html = fh.read()
    except Exception:
        return None, None
    _APP_CACHE.update({"chemin": f, "mtime": mt, "html": html})
    return html, f


# ═══════════════════════════════════════════════════════════════════════════
#  ROUTE « / » RETIRÉE — v7.7
# ---------------------------------------------------------------------------
#  Le moteur servait l'application par le réseau pour qu'un téléphone puisse
#  l'ouvrir. Ce projet est abandonné : le trader travaille sur ordinateur,
#  en ouvrant directement le fichier « omnitrade-v21.html ».
#
#  Retirer cette route supprime toute possibilité d'atteindre l'application
#  depuis un autre appareil, et avec elle la surface d'exposition associée.
#  Le moteur n'expose plus que ses routes /api/, sur 127.0.0.1 uniquement.
# ═══════════════════════════════════════════════════════════════════════════

@app.get("/api/ping")
def api_ping():
    """Test de connexion. Fonctionne dans les DEUX modes (natif et fichier).

    Bug corrigé : en mode fichier (macOS), `mt5` vaut None et l'appel direct à
    mt5.terminal_info() levait AttributeError -> HTTP 500 sur le tout premier
    bouton « Tester la connexion » de l'application.
    """
    if NATIVE_API and mt5 is not None:
        with _MT5_LOCK:
            connected = mt5_connect()
            ti = mt5.terminal_info() if connected else None
            acc = mt5.account_info() if connected else None
        return jsonify({
            "ok": bool(connected),
            "version": VERSION,
            "mode": "native",
            "platform": sys.platform,
            "frozen": IS_FROZEN,
            "server_time": datetime.now(tz=timezone.utc).isoformat(),
            "terminal": {
                "connected": bool(ti.connected) if ti else False,
                "company": ti.company if ti else None,
                "name": ti.name if ti else None,
                "build": ti.build if ti else None,
            } if ti else None,
            "account": {"login": acc.login, "server": acc.server,
                        "currency": acc.currency} if acc else None,
        })

    # ── Mode FICHIER (macOS / Linux / Windows sans le paquet MetaTrader5) ──
    acc = file_get_account()
    ddir = file_data_dir()
    return jsonify({
        "ok": acc is not None,
        "version": VERSION,
        "mode": "file",
        "platform": sys.platform,
        "frozen": IS_FROZEN,
        "data_dir": ddir,
        "server_time": datetime.now(tz=timezone.utc).isoformat(),
        "terminal": {
            "connected": acc is not None,
            "company": (acc.get("company") or acc.get("broker")) if acc else None,
            "name": "OmniTradeExport EA (mode fichier)",
            "build": "EA",
        },
        "account": {"login": acc["login"], "server": acc["server"],
                    "currency": acc["currency"]} if acc else None,
        "message": None if acc else (
            "account.json introuvable dans " + ddir +
            " — l'EA OmniTradeExport est-il actif sur un graphique ?"),
    })


def _watch_path():
    return os.path.join(user_data_dir(), "watch.json")


def _watch_load():
    try:
        o = json.loads(open(_watch_path(), encoding="utf-8").read())
        if isinstance(o, dict) and isinstance(o.get("pairs"), list):
            pairs = [str(x).replace("/", "").upper()[:12] for x in o["pairs"] if x]
            return [x for x in pairs if x][:3]
    except Exception:
        pass
    return ["EURUSD", "XAUUSD", "AUDUSD"]


def _watch_save(pairs):
    try:
        os.makedirs(user_data_dir(), exist_ok=True)
        blob = {"pairs": list(pairs)[:3]}
        tmp = _watch_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(blob, f)
        os.replace(tmp, _watch_path())
    except Exception:
        pass


@app.get("/api/watch")
def api_watch_get():
    return jsonify({"ok": True, "pairs": _watch_load()})


@app.post("/api/watch")
def api_watch_post():
    j = request.get_json(silent=True) or {}
    pairs = j.get("pairs") or []
    if not isinstance(pairs, list):
        pairs = []
    out = []
    for x in pairs:
        s = str(x or "").replace("/", "").replace(" ", "").upper()[:12]
        if s and s not in out:
            out.append(s)
    if not out:
        out = ["EURUSD", "XAUUSD", "AUDUSD"]
    _watch_save(out[:3])
    return jsonify({"ok": True, "pairs": out[:3]})


def _mute_load():
    try:
        o = json.loads(open(_tg_path(), encoding="utf-8").read())
        m = (o or {}).get("mute") or {}
        if isinstance(m, dict):
            return {k: bool(m.get(k)) for k in ("sydney", "tokyo", "london", "ny")}
    except Exception:
        pass
    return {"sydney": False, "tokyo": False, "london": False, "ny": False}


def _tg_muted_now():
    mute = _mute_load()
    now_utc = datetime.now(timezone.utc)
    for s in _TG_SES:
        if mute.get(s["id"]) and _tg_ses_is_open(s, now_utc)[0]:
            return True, s.get("name") or s["id"]
    return False, ""


@app.post("/api/mute")
def api_mute_post():
    j = request.get_json(silent=True) or {}
    mute = {k: bool(j.get(k)) for k in ("sydney", "tokyo", "london", "ny")}
    _TG["mute"] = mute
    try:
        _tg_load()
        _TG["mute"] = mute
        _tg_save()
    except Exception:
        pass
    return jsonify({"ok": True, "mute": mute})


@app.get("/api/account")
def api_account():
    a = get_account()
    return jsonify({"ok": a is not None, "account": a})


@app.get("/api/trades")
def api_trades():
    days = int(request.args.get("days", CFG["days"]))
    with_mae = request.args.get("mae", "1") not in ("0", "false", "no")
    t = get_closed_trades(days, with_mae)
    return jsonify({"ok": True, "count": len(t), "trades": t})


@app.get("/api/positions")
def api_positions():
    p = get_positions()
    return jsonify({"ok": True, "count": len(p), "positions": p})


@app.get("/api/sync")
def api_sync():
    days = int(request.args.get("days", CFG["days"]))
    with_mae = request.args.get("mae", "1") not in ("0", "false", "no")
    fresh = request.args.get("fresh", "0") in ("1", "true", "yes")
    payload = build_payload(days, with_mae) if fresh else cached_payload(days, with_mae)
    return jsonify(payload)


@app.get("/api/status")
def api_status():
    """Compatibilité : même information que /api/ping, format « status »."""
    a = get_account()
    if not a:
        return jsonify({"status": "error", "message": "MT5 non initialisé"}), 500
    return jsonify({"status": "connected", "account": a})


@app.get("/api/trades_flat")
def api_trades_flat():
    """Compatibilité : liste simple, format « status/count/trades »."""
    days = int(request.args.get("days", CFG["days"]))
    with_mae = request.args.get("mae", "1") not in ("0", "false", "no")
    t = get_closed_trades(days, with_mae)
    return jsonify({"status": "success", "count": len(t), "trades": t})


# ═════════════════════════════════════════════════════════════════════════
#  SERVICES MARCHÉ — actualités · calendrier · sentiment
# -------------------------------------------------------------------------
#  Tout passe par CE pont local, déjà lancé au double-clic par l'utilisateur.
#  Aucune clé API, aucun compte, aucun service tiers à déployer : le trader
#  n'a strictement rien à configurer.
#
#  Pourquoi côté pont et non dans le navigateur : les flux financiers
#  (Investing, FinancialJuice, ForexFactory, TraderSentiments) refusent les
#  requêtes d'une page file:// à cause du CORS. Un processus local, lui,
#  n'a aucune restriction.
# ═════════════════════════════════════════════════════════════════════════
import urllib.request as _urq
import urllib.parse as _urp

_MKT_CACHE = {}
_UA_MKT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/122.0 Safari/537.36")


# ── CONTEXTE SSL ──────────────────────────────────────────────────────────
# Dans une application compilée par PyInstaller, Python n'utilise PAS le
# trousseau de macOS : il cherche un fichier de certificats qui n'est pas
# toujours embarqué. Résultat mesuré chez les utilisateurs : toutes les
# requêtes HTTPS échouent avec CERTIFICATE_VERIFY_FAILED, et le Market Hub
# reste désespérément vide alors que la connexion Internet fonctionne.
#
# On construit donc UNE fois un contexte SSL en essayant, dans l'ordre :
#   1. le paquet certifi s'il est présent (PyInstaller l'embarque souvent) ;
#   2. les emplacements standards de macOS et de Linux ;
#   3. le magasin par défaut du système.
_SSL_CTX = None
_SSL_ORIGINE = "non initialisé"


def _build_ssl_context():
    """Contexte SSL fiable, y compris dans un binaire compilé."""
    global _SSL_ORIGINE
    import ssl
    # 1) certifi
    try:
        import certifi
        c = ssl.create_default_context(cafile=certifi.where())
        _SSL_ORIGINE = "certifi (%s)" % certifi.where()
        return c
    except Exception:
        pass
    # 2) emplacements connus (macOS avec Python.org, Homebrew, Linux)
    for p_ in (
        os.environ.get("SSL_CERT_FILE"),
        os.environ.get("REQUESTS_CA_BUNDLE"),
        "/etc/ssl/cert.pem",                       # macOS ≥ 10.15
        "/usr/local/etc/openssl@3/cert.pem",       # Homebrew
        "/usr/local/etc/openssl@1.1/cert.pem",
        "/opt/homebrew/etc/openssl@3/cert.pem",    # Homebrew Apple Silicon
        "/etc/ssl/certs/ca-certificates.crt",      # Debian / Ubuntu
        "/etc/pki/tls/certs/ca-bundle.crt",        # RedHat
    ):
        try:
            if p_ and os.path.isfile(p_):
                c = ssl.create_default_context(cafile=p_)
                _SSL_ORIGINE = p_
                return c
        except Exception:
            continue
    # 3) magasin par défaut
    try:
        c = ssl.create_default_context()
        _SSL_ORIGINE = "magasin système"
        return c
    except Exception:
        _SSL_ORIGINE = "aucun (vérification désactivée)"
        import ssl as _s
        c = _s.create_default_context()
        c.check_hostname = False
        c.verify_mode = _s.CERT_NONE
        return c


def _ssl_ctx():
    global _SSL_CTX
    if _SSL_CTX is None:
        _SSL_CTX = _build_ssl_context()
    return _SSL_CTX


# Délai par défaut ramené de 15 s à 6 s. Avec une vingtaine de sources
# interrogées, un timeout de 15 s pouvait bloquer une réponse pendant
# plusieurs minutes (mesuré : 24 s avec seulement 7 sources lentes).
_MKT_TIMEOUT = 6


def _mkt_get(url, timeout=None):
    if timeout is None:
        timeout = _MKT_TIMEOUT
    req = _urq.Request(url, headers={
        "User-Agent": _UA_MKT,
        "Accept": "application/rss+xml, application/xml, text/xml, application/json, text/html, */*",
        "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
    })
    kw = {"timeout": timeout}
    if url.lower().startswith("https"):
        kw["context"] = _ssl_ctx()
    with _urq.urlopen(req, **kw) as r:
        return r.read().decode("utf-8", "replace")


def _mkt_parallel(taches, timeout=None):
    """Exécute plusieurs collectes EN PARALLÈLE, sans jamais lever.

    `taches` : liste de callables sans argument, retournant une liste.
    Chaque tâche qui échoue ou dépasse le délai est simplement ignorée :
    on préfère un flux partiel à un écran vide.

    C'est le correctif central du gel : les sources étaient interrogées
    l'une après l'autre, si bien que le temps total était la SOMME des
    latences. Il devient la latence de la source la plus lente.
    """
    if timeout is None:
        timeout = _MKT_TIMEOUT + 3
    out = []
    if not taches:
        return out
    try:
        from concurrent.futures import ThreadPoolExecutor, as_completed
    except Exception:                       # environnement très restreint
        for f in taches:
            try:
                r = f()
                if r:
                    out.extend(r)
            except Exception:
                pass
        return out
    # ATTENTION : on n'utilise PAS « with ThreadPoolExecutor(...) ». Le bloc
    # « with » appelle shutdown(wait=True) en sortant et attend donc la fin
    # de TOUS les threads — y compris une source bloquée. Mesuré : 30 s de
    # réponse pour une seule source muette, malgré un budget de 3 s.
    # On rend la main dès le délai écoulé et on laisse les threads restants
    # se terminer seuls, en arrière-plan (ils sont « daemon » par défaut et
    # leur propre timeout réseau finit toujours par les interrompre).
    ex = ThreadPoolExecutor(max_workers=min(12, len(taches)))
    futs = [ex.submit(f) for f in taches]
    try:
        for fu in as_completed(futs, timeout=timeout):
            try:
                r = fu.result(timeout=0.1)
                if r:
                    out.extend(r)
            except Exception:
                pass
    except Exception:
        # Budget dépassé : on prend ce qui est déjà prêt, on abandonne le reste.
        for fu in futs:
            if fu.done():
                try:
                    r = fu.result(timeout=0.1)
                    if r:
                        out.extend(r)
                except Exception:
                    pass
    finally:
        # wait=False : la réponse part immédiatement.
        try:
            ex.shutdown(wait=False)
        except Exception:
            pass
    return out


# ── CACHE PERSISTANT ──────────────────────────────────────────────────────
# Les données du marché survivent à l'arrêt du moteur. Au redémarrage, même
# sans réseau, le trader retrouve immédiatement son calendrier et son fil
# d'actualités, horodatés, plutôt qu'un écran vide.
_MKT_DISK_LOCK = threading.Lock()
# Ce qui mérite d'être conservé sur le disque, avec sa durée de validité
# « raisonnable » à l'affichage (en secondes) — purement indicatif : on
# affiche TOUJOURS, en précisant l'âge.
_MKT_DISK_KEYS = ("cal_fr", "cal_en", "news_fr", "news_en", "sent")


def _mkt_disk_path():
    try:
        d = os.path.join(user_data_dir(), "cache")
        os.makedirs(d, exist_ok=True)
        return d
    except Exception:
        return None


def _mkt_disk_load(key):
    """Dernière valeur connue pour cette clé, ou None."""
    d = _mkt_disk_path()
    if not d:
        return None
    f = os.path.join(d, key + ".json")
    try:
        if not os.path.isfile(f):
            return None
        with open(f, "r", encoding="utf-8") as fh:
            o = json.load(fh)
        if isinstance(o, dict) and "payload" in o:
            return o
    except Exception:
        pass
    return None


def _mkt_disk_save(key, payload):
    """Écriture atomique : jamais de fichier à moitié écrit."""
    if key not in _MKT_DISK_KEYS:
        return
    d = _mkt_disk_path()
    if not d:
        return
    f = os.path.join(d, key + ".json")
    tmp = f + ".tmp"
    try:
        with _MKT_DISK_LOCK:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump({"ts": time.time(), "payload": payload}, fh)
            os.replace(tmp, f)
    except Exception:
        try:
            os.remove(tmp)
        except Exception:
            pass


def _mkt_cached(key, ttl):
    e = _MKT_CACHE.get(key)
    return e[1] if (e and time.time() - e[0] < ttl) else None


def _mkt_put(key, val):
    _MKT_CACHE[key] = (time.time(), val)
    _mkt_disk_save(key, val)


def _mkt_last_known(key):
    """Dernière valeur connue : mémoire d'abord, puis disque.

    Sert de FILET quand toutes les sources échouent. Retourne
    (payload, age_en_secondes) ou (None, 0).
    """
    e = _MKT_CACHE.get(key)
    if e:
        return e[1], max(0.0, time.time() - e[0])
    o = _mkt_disk_load(key)
    if o:
        # On réinjecte en mémoire pour les appels suivants.
        try:
            _MKT_CACHE[key] = (o.get("ts", 0), o["payload"])
        except Exception:
            pass
        return o["payload"], max(0.0, time.time() - float(o.get("ts") or 0))
    return None, 0.0


def _mkt_stale(payload, age):
    """Marque une réponse comme provenant du cache de secours."""
    try:
        d = dict(payload or {})
        d["stale"] = True
        d["ageSec"] = int(age)
        d["ok"] = True
        return d
    except Exception:
        return payload


# ── Actualités ────────────────────────────────────────────────────────────
# Flux VÉRIFIÉS un par un : ceux qui renvoient 403 (FXStreet, ZoneBourse) ou
# dont le contenu est figé (WSJ, bloqué au 27/01/2025) sont exclus.
# AUCUNE source crypto : le trader suit le Forex, l'or, le pétrole, la macro.
FEEDS_FR = [
    ("https://fr.investing.com/rss/news_1.rss",            "Investing FR", "macro"),
    ("https://fr.investing.com/rss/news_25.rss",           "Investing FR", "macro"),
    ("https://fr.investing.com/rss/forex.rss",             "Investing FR", "forex"),
    ("https://fr.investing.com/rss/commodities.rss",       "Investing FR", "commodities"),
    ("https://fr.investing.com/rss/market_overview.rss",   "Investing FR", "indices"),
    ("https://fr.investing.com/rss/stock_Indices.rss",     "Investing FR", "indices"),
    # Sources ajoutées — chacune testée : elle répond, elle est fraîche
    # (moins de 24 h) et son contenu est réellement financier.
    ("https://fr.investing.com/rss/news_285.rss",          "Investing FR", "forex"),
    ("https://fr.investing.com/rss/news_14.rss",           "Investing FR", "macro"),
    ("https://fr.investing.com/rss/commodities_Metals.rss","Investing FR", "commodities"),
    ("https://www.lemonde.fr/economie/rss_full.xml",       "Le Monde",     "macro"),
    ("https://www.lefigaro.fr/rss/figaro_economie.xml",    "Le Figaro",    "macro"),
    ("https://www.lerevenu.com/rss.xml",                   "Le Revenu",    "indices"),
    ("https://www.lesechos.fr/rss/rss_finance_marches.xml", "Les Echos",    "macro"),
    ("https://www.latribune.fr/rss/rss.xml",               "La Tribune",   "macro"),
    ("https://www.rfi.fr/fr/economie/rss",                 "RFI",          "macro"),
    ("https://news.google.com/rss/search?q=when:12h+(BCE+OR+Fed+OR+forex+OR+or+OR+dollar)+when:12h&hl=fr&gl=FR&ceid=FR:fr",
                                                           "Google News FR", "macro"),
]
FEEDS_EN = [
    ("https://www.investing.com/rss/news_1.rss",           "Investing",     "macro"),
    ("https://www.investing.com/rss/news_11.rss",          "Investing",     "forex"),
    ("https://www.investing.com/rss/commodities.rss",      "Investing",     "commodities"),
    ("https://www.investing.com/rss/news_25.rss",          "Investing",     "indices"),
    ("https://www.financialjuice.com/feed.ashx",           "FinancialJuice", "macro"),
    # Sources ajoutées, testées une par une.
    ("https://www.investing.com/rss/news_95.rss",          "Investing",     "macro"),
    ("https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=20910258",
                                                           "CNBC",          "macro"),
    ("https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=15839135",
                                                           "CNBC",          "indices"),
    ("https://feeds.bbci.co.uk/news/business/rss.xml",     "BBC Business",  "macro"),
    ("https://feeds.content.dowjones.io/public/rss/mw_topstories",
                                                           "MarketWatch",   "indices"),
    ("https://seekingalpha.com/market_currents.xml",       "Seeking Alpha", "indices"),
    ("https://oilprice.com/rss/main",                      "OilPrice",      "commodities"),
    ("https://news.google.com/rss/search?q=when:24h+forex+OR+%22central+bank%22"
     "+OR+%22interest+rate%22&hl=en-US&gl=US&ceid=US:en", "Google News",   "forex"),
    ("https://www.forexlive.com/feed/news",                "ForexLive",     "forex"),
    ("https://www.kitco.com/rss/KitcoNews.xml",            "Kitco",         "commodities"),
]

# ── FLUX CRYPTO : STRICTEMENT ISOLÉS ──────────────────────────────────────
# Ces sources ne sont JAMAIS mélangées aux autres. Elles sont récupérées
# séparément, forcées en catégorie « crypto », et le client les exclut de
# l'onglet « Tout ». Voir _categorize() : la catégorie crypto n'est jamais
# attribuée par déduction, uniquement par appartenance à cette liste.
FEEDS_CRYPTO_FR = [
    ("https://cryptoast.fr/feed/",                         "Cryptoast",     "crypto"),
    ("https://journalducoin.com/feed/",                    "Journal du Coin", "crypto"),
]
FEEDS_CRYPTO_EN = [
    ("https://cointelegraph.com/rss",                      "Cointelegraph", "crypto"),
    ("https://www.coindesk.com/arc/outboundfeeds/rss/",    "CoinDesk",      "crypto"),
    ("https://decrypt.co/feed",                            "Decrypt",       "crypto"),
    ("https://news.bitcoin.com/feed/",                     "Bitcoin.com",   "crypto"),
]


def _strip(t):
    import html as _html
    t = re.sub(r"<!\[CDATA\[([\s\S]*?)\]\]>", r"\1", t or "")
    t = re.sub(r"<[^>]+>", "", t)
    t = _html.unescape(t)
    for a, b in (("\x96", "—"), ("\x97", "—"), ("\u0096", "—"), ("\u0097", "—"),
                 ("\u00a0", " "), ("\u2013", "—"), ("\u2014", "—")):
        t = t.replace(a, b)
    t = re.sub(r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF]+", " ", t)
    return re.sub(r"\s+", " ", t).strip(" \t-–—|")


_NEWS_TR_CACHE = {}


def _news_looks_fr(s):
    s = s or ""
    if re.search(r"[éèêëàâùûçîïôœ]", s, re.I):
        return True
    return bool(re.search(
        r"\b(le|la|les|des|une|un|du|de la|est|aux|dans|pour|avec|sur|par|"
        r"fait|taux|euro|marché|titres|or|dollar|selon|après|avant|contre)\b", s, re.I))


def _news_looks_en(s):
    """Tout titre non-français (latin) doit passer en traduction."""
    s = (s or "").strip()
    if len(s) < 8:
        return False
    if _news_looks_fr(s):
        return False
    letters = re.findall(r"[A-Za-z]", s)
    return len(letters) >= 8


def _news_tr_lex(s):
    if not s:
        return s
    out = s
    pairs = [
        ("Ahead of Fed Minutes", "avant les minutes de la Fed"),
        ("Ahead of", "avant"),
        ("What should investors watch for", "ce que les investisseurs doivent surveiller"),
        ("Needs to Launch", "doit lancer"),
        ("Gathers Pace", "s'accélère"),
        ("holds above", "se maintient au-dessus de"),
        ("fuels rally", "alimente le rallye"),
        ("weak dollar", "dollar faible"),
        ("Treasury buyback", "rachats du Trésor"),
        ("US Treasury", "Trésor US"),
        ("Bond Yields Stabilize", "les rendements obligataires se stabilisent"),
        ("Global Government Bond Yields", "Les rendements des obligations d'État"),
        ("Gold holds above", "L'or se maintient au-dessus de"),
        ("Gold price rises", "Le cours de l'or grimpe de"),
        ("silver jumps", "l'argent grimpe de"),
        ("Operation Twist", "Operation Twist"),
        ("Fed Minutes", "minutes de la Fed"),
        ("interest rate policy", "politique de taux"),
        ("as US", "tandis que les États-Unis"),
    ]
    for a, b in pairs:
        out = re.sub(re.escape(a), b, out, flags=re.I)
    return out.strip()


def _news_tr_batch(titres):
    """Traduit une liste de titres EN -> FR. Cache + Groq, sinon lexique."""
    out = {}
    rest = []
    for t0 in titres:
        t0 = (t0 or "").strip()
        if not t0:
            continue
        if t0 in _NEWS_TR_CACHE:
            out[t0] = _NEWS_TR_CACHE[t0]
        elif not _news_looks_en(t0):
            out[t0] = t0
        else:
            rest.append(t0)
    if not rest:
        return out
    key = ""
    try:
        key = _groq_key_read() or ""
    except Exception:
        key = ""
    gem = ""
    cer = ""
    if not _TG.get("busy"):
        try:
            gem = _gemini_key_read() or ""
        except Exception:
            gem = ""
        try:
            cer = _cerebras_key_read() or ""
        except Exception:
            cer = ""
    else:
        key = ""
    chatfn = None
    if _cb is not None:
        chatfn = getattr(_cb, "llm_chat", None) or getattr(_cb, "groq_chat", None)
    if chatfn and (key or gem or cer):
        sys = ("Traduis chaque titre en francais journalistique, registre AFP. "
               "Une ligne par titre, meme numero (1. ...). "
               "Garde chiffres, %, $, noms propres. Pas de commentaire.")
        for start in range(0, min(len(rest), 60), 20):
            chunk = rest[start:start + 20]
            blob = "\n".join("%d. %s" % (i + 1, x) for i, x in enumerate(chunk))
            try:
                if chatfn is getattr(_cb, "llm_chat", None):
                    raw, err = _cb.llm_chat(
                        key,
                        [{"role": "system", "content": sys},
                         {"role": "user", "content": blob}],
                        max_tokens=900, disable_thinking=True,
                        gemini_key=gem, cerebras_key=cer,
                        mistral_key=_mistral_key_read(), nvidia_key=_nvidia_key_read(),
                        job="translate")
                else:
                    raw, err = _cb.groq_chat(
                        key,
                        [{"role": "system", "content": sys},
                         {"role": "user", "content": blob}],
                        max_tokens=900, disable_thinking=True)
                txt = ((raw or {}).get("content") or "") if not err else ""
                for line in txt.splitlines():
                    m = re.match(r"\s*(\d+)[.)]\s*(.+)$", line.strip())
                    if not m:
                        continue
                    i = int(m.group(1)) - 1
                    if 0 <= i < len(chunk):
                        fr = m.group(2).strip().strip('"')
                        if fr and len(fr) > 8:
                            _NEWS_TR_CACHE[chunk[i]] = fr
                            out[chunk[i]] = fr
            except Exception:
                pass
    for x in rest:
        if x not in out:
            fr = _news_tr_lex(x)
            _NEWS_TR_CACHE[x] = fr
            out[x] = fr
    return out


def _news_force_fr_items(items, title_key="title"):
    """Garde les flux EN, affiche le titre en FR. titleEn conservé."""
    if not items:
        return items
    need = []
    for n in items:
        raw = _strip(n.get(title_key) or n.get("titre") or "")
        n["titleEn"] = n.get("titleEn") or n.get("titreEn") or raw
        if title_key == "titre":
            n["titre"] = raw
        else:
            n["title"] = raw
        if _news_looks_en(raw):
            need.append(n)
    if not need:
        return items
    mp = _news_tr_batch([n.get("titleEn") or n.get(title_key) or "" for n in need])
    for n in need:
        src = n.get("titleEn") or ""
        fr = mp.get(src) or _news_tr_lex(src)
        if fr:
            if title_key == "titre":
                n["titre"] = fr
            else:
                n["title"] = fr
    return items


def _tag(block, name):
    m = re.search(r"<%s[^>]*>([\s\S]*?)</%s>" % (name, name), block, re.I)
    return _strip(m.group(1)) if m else ""


def _categorize(title, fallback):
    # ISOLATION CRYPTO — règle stricte demandée par le trader.
    # Si la dépêche provient d'une source crypto (fallback == "crypto"), elle
    # RESTE crypto, quoi qu'il arrive. Sans ce verrou, un titre comme
    # « Bitcoin dépasse les 64 000 dollars » était reclassé en « forex » à
    # cause du mot « dollars » et polluait l'onglet « Tout » (mesuré : 5 cas
    # de fuite sur 6 titres crypto réalistes).
    if fallback == "crypto":
        return "crypto"
    t = title.lower()
    # Réciproquement : une dépêche NON crypto qui parle de cryptomonnaies ne
    # doit pas non plus se retrouver mélangée. On la marque crypto pour
    # qu'elle soit confinée au même onglet.
    if re.search(r"\b(bitcoin|btc|ethereum|eth\b|crypto|blockchain|altcoin|"
                 r"stablecoin|binance|solana|ripple|xrp|dogecoin|nft|"
                 r"web3|defi)\b", t):
        return "crypto"
    if re.search(r"\b(s&p|nasdaq|dow jones|cac ?40|dax|ftse|nikkei|stoxx|russell|"
                 r"indice|wall street|bourse)", t):
        return "indices"
    if re.search(r"\b(or |gold|argent|silver|p[ée]trole|oil|brent|wti|cuivre|copper|"
                 r"gaz|mati[èe]re premi)", t):
        return "commodities"
    if re.search(r"\b(eur|usd|gbp|jpy|chf|cad|aud|forex|devise|dollar|euro|yen|"
                 r"sterling|livre|change)", t):
        return "forex"
    if re.search(r"\b(fed|bce|ecb|boe|boj|inflation|cpi|ipc|nfp|pib|gdp|taux|rate|"
                 r"emploi|ch[ôo]mage|jobs|croissance)", t):
        return "macro"
    return fallback


def _parse_date(d):
    if not d:
        return datetime.now(timezone.utc)
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(d)
    except Exception:
        try:
            dt = datetime.fromisoformat(d.replace("Z", "+00:00"))
        except Exception:
            return datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _fetch_feed(url, src, cat):
    try:
        xml = _mkt_get(url, _MKT_TIMEOUT)
    except Exception:
        return []
    blocks = re.findall(r"<item[\s\S]*?</item>", xml, re.I) or \
             re.findall(r"<entry[\s\S]*?</entry>", xml, re.I)
    out = []
    now = datetime.now(timezone.utc)
    for b in blocks[:60]:
        title = _tag(b, "title")
        link = _tag(b, "link")
        if not link:
            m = re.search(r'<link[^>]*href=["\']([^"\']+)["\']', b, re.I)
            link = m.group(1) if m else ""
        if not title or not link:
            continue
        dt = _parse_date(_tag(b, "pubDate") or _tag(b, "published") or _tag(b, "updated"))
        # Garde-fou : un flux figé (cas réel du WSJ, bloqué 547 jours) est
        # ignoré. Seuil ramené de 30 à 21 jours : un article d'un mois n'a
        # aucune valeur pour un trader, et la limite à 30 laissait passer
        # des dépêches de 30,8 jours (constaté en test).
        if (now - dt).days > 21:
            continue
        chapo = _tag(b, "description") or _tag(b, "summary") or _tag(b, "content")
        chapo = re.sub(r"<[^>]+>", " ", chapo or "")
        chapo = re.sub(r"\s+", " ", chapo).strip()[:320]
        out.append({"title": title, "link": link, "date": dt.isoformat(),
                    "source": src, "category": _categorize(title, cat),
                    "resume": chapo})
    return out


def _fetch_forexfactory_news():
    """Breaking news ForexFactory.

    Aucun flux RSS n'est exposé (rss.php -> 404). On extrait donc les titres
    de la page /news : liens /news/<id>-<slug>, en écartant le bruit
    (« N comments », « From source.com ») et les doublons.
    """
    try:
        h = _mkt_get("https://www.forexfactory.com/news", _MKT_TIMEOUT + 2)
    except Exception:
        return []
    out, seen = [], set()
    now = datetime.now(timezone.utc)
    for href, title in re.findall(r'href="(/news/\d+[^"]*)"[^>]*>([^<]{12,220})<', h):
        t = re.sub(r"\s+", " ", title).strip()
        low = t.lower()
        # Bruit structurel de la page.
        if (low.startswith("from ") or re.match(r"^\d+\s+comments?$", low)
                or low in ("read more", "comments")):
            continue
        # Entités hexadécimales des apostrophes typographiques.
        t = (t.replace("&#x91;", "\u2018").replace("&#x92;", "\u2019")
              .replace("&#x93;", "\u201c").replace("&#x94;", "\u201d")
              .replace("&amp;", "&").replace("&quot;", '"').replace("&#39;", "'"))
        key = re.sub(r"/hit$", "", href)
        if key in seen:
            continue
        seen.add(key)
        # La page n'expose pas d'horodatage absolu. Les estampiller toutes à
        # « maintenant » les plaçait DEVANT des dépêches réellement datées de
        # quelques minutes, ce qui faisait remonter de l'anglais en tête du
        # flux français. On applique donc un recul prudent, échelonné selon
        # l'ordre d'affichage de la page (le plus récent en premier).
        out.append({
            "title": t,
            "link": "https://www.forexfactory.com" + key,
            "date": (now - timedelta(minutes=4 + len(out) // 3)).isoformat(),
            "source": "ForexFactory",
            "category": _categorize(t, "macro"),
        })
    return out[:40]


# ── Baha (groupe TeleTrader) ──────────────────────────────────────────────
# Point d'entrée découvert en écoutant le trafic réseau d'un vrai navigateur
# sur baha.com/BahaNews : /psapi/Ticker/GetNewsTickerItems/<langue>.
# Il renvoie du JSON pur, sans clé ni compte. Testé : « en » et « de »
# répondent, « fr » renvoie une liste vide côté éditeur (pas de rédaction FR).
def _fetch_baha_news(lang="en"):
    codes = ["en"] if lang == "en" else ["en"]
    out, now = [], datetime.now(timezone.utc)
    for code in codes:
        try:
            data = json.loads(_mkt_get(
                "https://www.baha.com/psapi/Ticker/GetNewsTickerItems/" + code,
                _MKT_TIMEOUT))
        except Exception:
            continue
        for it in data:
            t = re.sub(r"\s+", " ", (it.get("text") or "")).strip()
            u = (it.get("url") or "").strip()
            if not t or not u:
                continue
            # Même précaution que pour ForexFactory : recul échelonné plutôt
            # qu'un horodatage massif à l'instant présent.
            out.append({"title": t, "link": u,
                        "date": (now - timedelta(minutes=4 + len(out) // 3)).isoformat(),
                        "source": "Baha", "category": _categorize(t, "macro")})
    return out[:40]


# ── ZoneBourse (Surperformance) ───────────────────────────────────────────
# Le site est protégé par Akamai : requête directe et navigateur automatisé
# renvoient tous deux « Access Denied » depuis une IP de centre de données
# (vérifié). Depuis la connexion domestique du trader, l'accès direct passe
# généralement : on l'ESSAIE D'ABORD. En cas de refus, on bascule sur un
# lecteur de texte public, sans clé ni compte.
_ZB_SECTIONS = [
    ("actualite-bourse/devises/", "forex"),
    ("actualite-bourse/matieres-premieres/", "commodities"),
    ("actualite-bourse/indices/", "indices"),
    ("actualite-bourse/economie/", "macro"),
]


def _fetch_zonebourse_news():
    """Sections ZoneBourse collectées EN PARALLÈLE.

    Chaque section tentait un accès direct puis un repli par proxy de rendu,
    le tout en série : mesuré à 9,2 s pour cette seule source, ce qui
    consommait à elle seule tout le budget de la collecte.
    """
    now = datetime.now(timezone.utc)
    lots = _mkt_parallel([(lambda p_=p_, c_=c_: _zb_section(p_, c_, now))
                          for p_, c_ in _ZB_SECTIONS],
                         timeout=_MKT_TIMEOUT + 3)
    out, seen = [], set()
    for it in lots:
        k = (it.get("title") or "").lower()
        if not k or k in seen:
            continue
        seen.add(k)
        out.append(it)
    return out[:60]


def _zb_section(path, cat, now):
    """Une seule section ZoneBourse : accès direct, puis repli."""
    out, seen = [], set()
    for _once in (0,):
        raw = None
        try:                                    # 1. accès direct
            raw = _mkt_get("https://www.zonebourse.com/" + path, _MKT_TIMEOUT)
            if "Access Denied" in raw[:500]:
                raw = None
        except Exception:
            raw = None
        if raw:
            for href, title in re.findall(
                    r'href="(/cours/[^"]+|/actualite-bourse/[^"]+)"[^>]*>'
                    r"([^<]{25,190})</a>", raw):
                t = _strip(title)
                if not t or t.lower() in seen:
                    continue
                seen.add(t.lower())
                out.append({"title": t,
                            "link": "https://www.zonebourse.com" + href,
                            "date": (now - timedelta(minutes=4 + len(out) // 3)).isoformat(),
                            "source": "ZoneBourse",
                            "category": _categorize(t, cat)})
            continue
        try:                                    # 2. repli lecteur de texte
            md = _mkt_get("https://r.jina.ai/https://www.zonebourse.com/" + path, _MKT_TIMEOUT + 2)
        except Exception:
            continue
        # Format rendu : « <âge> \n\t <titre> ». On ne garde que ces couples.
        for m in re.finditer(r"\n\s*(\d{1,2})\s*(min|h)\s*\n\s*\t?\s*"
                             r"([^\n\t]{25,190})\n", md):
            t = re.sub(r"\s+", " ", m.group(3)).strip()
            if not t or t.lower() in seen:
                continue
            seen.add(t.lower())
            age = int(m.group(1)) * (60 if m.group(2) == "h" else 1)
            out.append({"title": t,
                        "link": "https://www.zonebourse.com/" + path,
                        "date": (now - timedelta(minutes=age)).isoformat(),
                        "source": "ZoneBourse",
                        "category": _categorize(t, cat)})
    return out


# Durées de vie des caches marché, en secondes.
#   news      : 8 min  — les flux RSS ne bougent pas plus vite
#   calendrier: 3 min  — 30 s dans les minutes qui entourent une annonce
#   sentiment : 10 min — donnée de positionnement, lente par nature
_NEWS_TTL = 480
_CAL_TTL = 180
_CAL_TTL_HOT = 30
_SENT_TTL = 600
# Budget global d'une collecte : au-delà, on rend ce qui est déjà arrivé.
_NEWS_BUDGET = 9
_CAL_BUDGET = 9


@app.get("/api/news")
def api_news():
    lang = "fr" if request.args.get("lang", "fr").lower() == "fr" else "en"
    key = "news_" + lang
    # ?fresh=1 : le trader a cliqué sur « Actualiser », on ignore le cache.
    fresh = request.args.get("fresh", "0") in ("1", "true", "yes")
    # Cache porté de 3 à 8 minutes : les flux RSS ne sont de toute façon pas
    # republiés plus vite, et cela divise par presque trois le nombre de
    # sollicitations des sources (première cause de blocage par leur pare-feu).
    if not fresh:
        hit = _mkt_cached(key, _NEWS_TTL)
        if hit:
            if lang == "fr":
                try:
                    _news_force_fr_items((hit or {}).get("items") or [], "title")
                except Exception:
                    pass
            return jsonify(hit)

    # FR : on prend AUSSI les flux EN (plus frais) puis on traduit. On ne coupe pas l'anglais.
    feeds = (list(FEEDS_FR) + list(FEEDS_EN)) if lang == "fr" else list(FEEDS_EN)
    # ── Collecte PARALLÈLE ────────────────────────────────────────────────
    # Toutes les sources sont interrogées en même temps. Avant, la boucle
    # séquentielle additionnait les latences : mesuré à 24 s avec seulement
    # 7 sources lentes, ce qui dépassait le délai du navigateur et vidait
    # l'affichage. Désormais le coût total est celui de la source la plus
    # lente, plafonné par un délai global.
    taches = [(lambda u=u, s_=s_, c=c: _fetch_feed(u, s_, c)) for u, s_, c in feeds]
    taches.append(_fetch_forexfactory_news)
    taches.append(_fetch_zonebourse_news)
    taches.append(lambda: _fetch_baha_news("en"))
    items = _mkt_parallel(taches, timeout=_NEWS_BUDGET)

    # ── Crypto : récupéré À PART, jamais mélangé ──────────────────────────
    # Les dépêches sont forcées en catégorie « crypto » à la source ; le
    # client se charge ensuite de les exclure de l'onglet « Tout ».
    crypto_feeds = FEEDS_CRYPTO_FR if lang == "fr" else FEEDS_CRYPTO_EN
    if lang == "fr":
        crypto_feeds = list(crypto_feeds) + list(FEEDS_CRYPTO_EN[:2])

    def _crypto(u, s_, c):
        out = []
        for it in _fetch_feed(u, s_, c):
            it["category"] = "crypto"          # verrou : jamais reclassé
            out.append(it)
        return out

    crypto_items = _mkt_parallel(
        [(lambda u=u, s_=s_, c=c: _crypto(u, s_, c)) for u, s_, c in crypto_feeds],
        timeout=_NEWS_BUDGET)


    # ── FILET DE SÉCURITÉ ─────────────────────────────────────────────────
    # Toutes les sources muettes (réseau coupé, pare-feu, blocage) : on rend
    # la dernière collecte connue plutôt qu'une liste vide, qui faisait
    # effacer l'affichage côté navigateur.
    if not items and not crypto_items:
        last, age = _mkt_last_known(key)
        if last and last.get("items"):
            return jsonify(_mkt_stale(last, age))
    seen, uniq = set(), []
    for n in items:
        k = n["title"].lower()[:60]
        if k in seen:
            continue
        seen.add(k)
        uniq.append(n)
    # Tri chronologique, mais en flux FRANÇAIS les sources francophones
    # priment à horodatage voisin : ForexFactory n'expose pas de date absolue
    # et remontait donc des titres anglais en tête du flux français.
    if lang == "fr":
        _FR_SRC = ("Investing FR", "ZoneBourse")
        uniq.sort(key=lambda x: (x["source"] in _FR_SRC, x["date"]), reverse=True)
    else:
        uniq.sort(key=lambda x: x["date"], reverse=True)
    # Buffer élargi : 200 dépêches au lieu de 60. Le client applique ensuite
    # sa propre purge FIFO, donc rien ne s'accumule indéfiniment côté navigateur.
    # Le crypto est ajouté APRÈS le tri des autres catégories, avec son
    # propre quota : sans cela, la troncature à 200 aurait pu l'évincer
    # entièrement, ou à l'inverse le laisser noyer les autres flux.
    seen_c = set()
    cuniq = []
    for n in sorted(crypto_items, key=lambda x: x["date"], reverse=True):
        k = n["title"].lower()[:60]
        if k in seen_c:
            continue
        seen_c.add(k)
        cuniq.append(n)
    uniq = uniq[:200] + cuniq[:60]
    if lang == "fr":
        try:
            _news_force_fr_items(uniq, "title")
        except Exception:
            pass
    out = {"ok": True, "lang": lang, "count": len(uniq),
           "updated": datetime.now(timezone.utc).isoformat(), "items": uniq}
    if uniq:
        _mkt_put(key, out)
    return jsonify(out)


# ── Calendrier économique (ForexFactory JSON + traduction FR intégrée) ────
CAL_FR = {
 "Federal Funds Rate":"Taux directeur de la Fed","FOMC Statement":"Communiqué du FOMC",
 "FOMC Press Conference":"Conférence de presse du FOMC","FOMC Meeting Minutes":"Minutes du FOMC",
 "FOMC Member Speaks":"Discours d'un membre du FOMC",
 "Non-Farm Employment Change":"Emplois non agricoles (NFP)","Unemployment Rate":"Taux de chômage",
 "Average Hourly Earnings m/m":"Salaire horaire moyen (m/m)",
 "CPI m/m":"Indice des prix à la consommation (m/m)","CPI y/y":"Indice des prix à la consommation (a/a)",
 "Core CPI m/m":"IPC sous-jacent (m/m)","Core CPI y/y":"IPC sous-jacent (a/a)",
 "PPI m/m":"Prix à la production (m/m)","Core PPI m/m":"Prix à la production sous-jacents (m/m)",
 "Retail Sales m/m":"Ventes au détail (m/m)","Core Retail Sales m/m":"Ventes au détail sous-jacentes (m/m)",
 "Advance GDP q/q":"PIB avancé (t/t)","Prelim GDP q/q":"PIB préliminaire (t/t)",
 "Final GDP q/q":"PIB final (t/t)","GDP m/m":"PIB (m/m)","GDP q/q":"PIB (t/t)","GDP y/y":"PIB (a/a)",
 "ISM Manufacturing PMI":"PMI manufacturier ISM","ISM Services PMI":"PMI des services ISM",
 "Flash Manufacturing PMI":"PMI manufacturier flash","Flash Services PMI":"PMI des services flash",
 "Manufacturing PMI":"PMI manufacturier","Services PMI":"PMI des services",
 "Unemployment Claims":"Inscriptions au chômage","Trade Balance":"Balance commerciale",
 "Crude Oil Inventories":"Stocks de pétrole brut","Natural Gas Storage":"Stocks de gaz naturel",
 "Consumer Confidence":"Confiance des consommateurs","Building Permits":"Permis de construire",
 "Durable Goods Orders m/m":"Commandes de biens durables (m/m)",
 "Main Refinancing Rate":"Taux de refinancement (BCE)",
 "Monetary Policy Statement":"Déclaration de politique monétaire",
 "Official Bank Rate":"Taux directeur (BoE)","Official Cash Rate":"Taux directeur",
 "Cash Rate":"Taux directeur (RBA)","German ifo Business Climate":"Climat des affaires ifo (Allemagne)",
 "Employment Change":"Variation de l'emploi","Retail Sales y/y":"Ventes au détail (a/a)",
 "Industrial Production m/m":"Production industrielle (m/m)",
 "Trimmed Mean CPI m/m":"IPC moyenne tronquée (m/m)",
 "BOE Monetary Policy Report":"Rapport de politique monétaire (BoE)",
 "Monetary Policy Summary":"Résumé de politique monétaire",
 "MPC Official Bank Rate Votes":"Votes du MPC sur le taux directeur",
 "BOE Gov Bailey Speaks":"Discours du gouverneur Bailey (BoE)",
 "Core PCE Price Index m/m":"Indice des prix PCE sous-jacent (m/m)",
 "BOJ Policy Rate":"Taux directeur (BoJ)","BOJ Outlook Report":"Rapport de perspectives (BoJ)",
 "BOJ Press Conference":"Conférence de presse (BoJ)",
 "RBA Gov Bullock Speaks":"Discours de la gouverneure Bullock (RBA)",
 "Fed Chair Powell Speaks":"Discours du président Powell (Fed)",
 "ECB President Lagarde Speaks":"Discours de la présidente Lagarde (BCE)",
 "ADP Non-Farm Employment Change":"Emploi privé ADP","JOLTS Job Openings":"Offres d'emploi JOLTS",
 "Employment Cost Index q/q":"Indice du coût de l'emploi (t/t)","Chicago PMI":"PMI de Chicago",
 "Pending Home Sales m/m":"Promesses de vente de logements (m/m)",
 "Goods Trade Balance":"Balance commerciale des biens",
 "Personal Income m/m":"Revenu des ménages (m/m)","Personal Spending m/m":"Dépenses des ménages (m/m)",
 "German Prelim CPI m/m":"IPC préliminaire allemand (m/m)",
 "French Prelim CPI m/m":"IPC préliminaire français (m/m)",
 "Spanish Flash CPI y/y":"IPC flash espagnol (a/a)",
 "Italian Prelim CPI m/m":"IPC préliminaire italien (m/m)",
 "Core PCE Price Index y/y":"Indice PCE sous-jacent (a/a)",
 "Revised UoM Consumer Sentiment":"Confiance des consommateurs UoM (révisée)",
 "Richmond Manufacturing Index":"Indice manufacturier de Richmond",
 "HPI m/m":"Indice des prix immobiliers (m/m)",
 "SPPI y/y":"Prix des services aux entreprises (a/a)",
 "Bank Holiday":"Jour férié",
 # ── Intitulés propres à TradingEconomics (nommage différent de ForexFactory)
 "Fed Interest Rate Decision":"Décision de taux de la Fed",
 "ECB Interest Rate Decision":"Décision de taux de la BCE",
 "BoE Interest Rate Decision":"Décision de taux de la BoE",
 "BoJ Interest Rate Decision":"Décision de taux de la BoJ",
 "RBA Interest Rate Decision":"Décision de taux de la RBA",
 "BoC Interest Rate Decision":"Décision de taux de la BoC",
 "Interest Rate Decision":"Décision de taux directeur",
 "Non Farm Payrolls":"Emplois non agricoles (NFP)",
 "Government Payrolls":"Emplois publics",
 "Manufacturing Payrolls":"Emplois manufacturiers",
 "Unemployment Rate":"Taux de chômage",
 "Inflation Rate YoY":"Taux d'inflation (a/a)",
 "Inflation Rate MoM":"Taux d'inflation (m/m)",
 "Core Inflation Rate YoY":"Inflation sous-jacente (a/a)",
 "Core Inflation Rate MoM":"Inflation sous-jacente (m/m)",
 "GDP Growth Rate QoQ":"Croissance du PIB (t/t)",
 "GDP Growth Rate YoY":"Croissance du PIB (a/a)",
 "GDP Growth Rate QoQ Adv":"Croissance du PIB (t/t, avancée)",
 "GDP Growth Rate QoQ Prel":"Croissance du PIB (t/t, préliminaire)",
 "GDP Growth Rate YoY Prel":"Croissance du PIB (a/a, préliminaire)",
 "GDP Growth Rate QoQ Flash":"Croissance du PIB (t/t, flash)",
 "GDP Growth Rate YoY Flash":"Croissance du PIB (a/a, flash)",
 "Balance of Trade":"Balance commerciale",
 "Retail Sales MoM":"Ventes au détail (m/m)",
 "Retail Sales YoY":"Ventes au détail (a/a)",
 "Industrial Production YoY":"Production industrielle (a/a)",
 "Industrial Production MoM":"Production industrielle (m/m)",
 "Consumer Confidence":"Confiance des consommateurs",
 "Michigan Consumer Sentiment":"Confiance des consommateurs (Michigan)",
 "ISM Manufacturing PMI":"PMI manufacturier ISM",
 "ISM Services PMI":"PMI des services ISM",
 "Manufacturing PMI":"PMI manufacturier",
 "Services PMI":"PMI des services",
 "Composite PMI":"PMI composite",
 "JOLTs Job Openings":"Offres d'emploi JOLTS",
 "Initial Jobless Claims":"Inscriptions hebdomadaires au chômage",
 "Continuing Jobless Claims":"Inscriptions continues au chômage",
 "Core PCE Price Index MoM":"Indice PCE sous-jacent (m/m)",
 "Core PCE Price Index YoY":"Indice PCE sous-jacent (a/a)",
 "PCE Price Index MoM":"Indice PCE (m/m)",
 "Personal Income MoM":"Revenu des ménages (m/m)",
 "Personal Spending MoM":"Dépenses des ménages (m/m)",
 "PPI MoM":"Prix à la production (m/m)",
 "PPI YoY":"Prix à la production (a/a)",
 "Exports YoY":"Exportations (a/a)",
 "Imports YoY":"Importations (a/a)",
 "Existing Home Sales":"Ventes de logements anciens",
 "New Home Sales":"Ventes de logements neufs",
 "Building Permits":"Permis de construire",
 "Housing Starts":"Mises en chantier",
 "Crude Oil Stocks Change":"Variation des stocks de pétrole brut",
 "Gasoline Stocks Change":"Variation des stocks d'essence",
 "Fed Press Conference":"Conférence de presse de la Fed",
 "Fed Balance Sheet":"Bilan de la Fed",
 "Employment Change":"Variation de l'emploi",
 "Wage Growth YoY":"Croissance des salaires (a/a)",
 "Business Confidence":"Confiance des entreprises",
 "Consumer Inflation Expectations":"Anticipations d'inflation des ménages",
}
IMPACT_FR = {"High":"Fort","Medium":"Moyen","Low":"Faible","Holiday":"Férié"}
_CAL_RULES = [
 (r"\bMonetary Policy\b","Politique monétaire"),(r"\bPress Conference\b","Conférence de presse"),
 (r"\bCPI\b","IPC"),(r"\bGDP\b","PIB"),(r"\bRetail Sales\b","Ventes au détail"),
 (r"\bUnemployment\b","Chômage"),(r"\bEmployment\b","Emploi"),(r"\bRate\b","Taux"),
 (r"\bInventories\b","Stocks"),(r"\bIndex\b","Indice"),(r"\bSales\b","Ventes"),
 (r"\bOrders\b","Commandes"),(r"\bConfidence\b","Confiance"),(r"\bSpeaks\b","Discours"),
 (r"\bCore\b","Sous-jacent"),(r"\bPrelim\b","Préliminaire"),(r"\bClaims\b","Inscriptions"),
 (r"\bReport\b","Rapport"),(r"\bSummary\b","Résumé"),(r"\bStatement\b","Communiqué"),
 (r"\bVotes\b","Votes"),(r"\bBalance\b","Balance"),(r"\bProduction\b","Production"),
 (r"\bm/m\b","(m/m)"),(r"\by/y\b","(a/a)"),(r"\bq/q\b","(t/t)"),
 # Suffixes et termes propres à TradingEconomics.
 (r"\bMoM\b","(m/m)"),(r"\bYoY\b","(a/a)"),(r"\bQoQ\b","(t/t)"),
 (r"\bDecision\b","Décision"),(r"\bPayrolls\b","Emplois"),
 (r"\bStocks Change\b","Variation des stocks"),
 (r"\bGrowth\b","Croissance"),(r"\bPrice Index\b","Indice des prix"),
 (r"\bJob Openings\b","Offres d'emploi"),
 (r"\bJobless Claims\b","Inscriptions au chômage"),
 (r"\bHome Sales\b","Ventes de logements"),
 (r"\bInflation Rate\b","Taux d'inflation"),
 (r"\bGDP Growth Rate\b","Croissance du PIB"),
 (r"\bAnnualized\b","annualisé"),
 (r"\bAnnual Revision\b","révision annuelle"),
 (r"\bNon[- ]?Farm\b","non agricoles"),
 (r"\bPrelim\b","prélim."),(r"\bPrel\b","prélim."),
]


def _cal_tr(title):
    if title in CAL_FR:
        return CAL_FR[title]
    t = title
    for pat, rep in _CAL_RULES:
        t = re.sub(pat, rep, t)
    return t


def _cal_from_forexfactory():
    """Calendrier depuis la page ForexFactory.

    Le flux JSON ff_calendar_thisweek.json ne contient PAS le champ « actual »
    (vérifié : 0/92 événements) — c'est un fichier de prévisions figé. La page
    HTML, elle, expose l'état complet du calendrier dans une variable
    JavaScript, valeurs publiées incluses. On l'extrait donc directement.
    """
    h = _mkt_get("https://www.forexfactory.com/calendar", _MKT_TIMEOUT + 2)
    m = re.search(r"calendarComponentStates\[1\]\s*=\s*\{(.*?)\};\s*\n", h, re.S)
    if not m:
        raise RuntimeError("state introuvable")
    raw = m.group(1)
    dm = re.search(r"days:\s*(\[.*?\])\s*,\s*\n\s*[a-zA-Z_]+:", raw, re.S) \
         or re.search(r"days:\s*(\[.*\])", raw, re.S)
    if not dm:
        raise RuntimeError("days introuvable")
    days = json.loads(dm.group(1))

    IMP = {"high": "High", "medium": "Medium", "low": "Low", "holiday": "Holiday"}
    out = []
    for d in days:
        base = d.get("dateline")
        for e in d.get("events", []):
            ts = e.get("dateline") or base
            try:
                iso = datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()
            except Exception:
                iso = datetime.now(timezone.utc).isoformat()
            imp = IMP.get(str(e.get("impactName", "")).lower(), "Low")
            out.append({
                "title": (e.get("name") or "").strip(),
                "country": (e.get("currency") or "").strip(),
                "date": iso,
                "impact": imp,
                # Valeurs telles que publiées par la source, sans retraitement.
                "actual": str(e.get("actual") or "").strip(),
                "forecast": str(e.get("forecast") or "").strip(),
                "previous": str(e.get("previous") or "").strip(),
            })
    return [x for x in out if x["title"]]


# ── Source n°2 : TradingEconomics (valeurs PUBLIÉES incluses) ─────────────
# Vérifié empiriquement : la page /calendar est rendue côté serveur et expose
# actual / previous / consensus. C'est la seule source testée, en plus de
# ForexFactory, qui accepte une requête automatisée ET porte les valeurs
# réellement publiées (Investing.com renvoie 403 sur TOUS ses points d'entrée).
_TE_CUR = {
    "US": "USD", "AU": "AUD", "GB": "GBP", "JP": "JPY", "CA": "CAD",
    "CH": "CHF", "NZ": "NZD", "CN": "CNY", "EA": "EUR", "DE": "EUR",
    "FR": "EUR", "IT": "EUR", "ES": "EUR", "NL": "EUR", "BE": "EUR",
    "AT": "EUR", "PT": "EUR", "IE": "EUR", "FI": "EUR", "GR": "EUR",
}
_TE_IMPACT = {"3": "High", "2": "Medium", "1": "Low"}


def _te_txt(s):
    """Supprime le balisage et rétablit les entités HTML."""
    s = re.sub(r"<[^>]+>", "", s)
    for a, b in (("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'),
                 ("&#39;", "'"), ("&nbsp;", " ")):
        s = s.replace(a, b)
    return re.sub(r"\s+", " ", s).strip()


def _cal_from_tradingeconomics():
    """Calendrier TradingEconomics, valeurs publiées comprises.

    Attention : les <tr> du tableau sont IMBRIQUÉS (le drapeau du pays est
    lui-même une <table>). Une regex « <tr>...</tr> » s'arrête donc au premier
    </tr> interne et ne capture rien. On découpe sur la frontière réelle,
    « <tr data-url= », qui n'apparaît qu'en tête de chaque ligne d'événement.
    """
    h = _mkt_get("https://tradingeconomics.com/calendar", _MKT_TIMEOUT + 2)
    parts = re.split(r"(<tr data-url=)", h)
    if len(parts) < 3:
        raise RuntimeError("TE : aucune ligne")
    chunks = [parts[k] + parts[k + 1] for k in range(1, len(parts), 2)]
    out = []
    for body in chunks:
        ev = re.search(r"class='calendar-event'[^>]*>(.*?)</a>", body, re.S)
        if not ev:
            continue
        iso = re.search(r'class="calendar-iso">([A-Z]{2})<', body)
        cur = _TE_CUR.get(iso.group(1) if iso else "", "")
        if not cur:
            continue                       # on ne garde que les devises tradées
        tm = re.search(r">\s*(\d{1,2}):(\d{2})\s*(AM|PM)\s*<", body)
        # La date N'EST PAS dans un attribut « data-date » (il n'existe pas) :
        # TradingEconomics la place dans la CLASSE de la première cellule,
        # sous la forme class=' 2026-07-30'. Faute de la lire, tous les
        # événements retombaient sur la date du jour — le NFP du 7 août
        # apparaissait donc au 29 juillet.
        dm = re.search(r'class=["\']\s*(\d{4}-\d{2}-\d{2})\s*["\']', body) \
             or re.search(r'data-date=["\'](\d{4}-\d{2}-\d{2})', body)
        imp = re.search(r"calendar-date-(\d)", body)
        act = re.search(r"id='actual'>(.*?)</span>", body, re.S)
        prv = re.search(r"id='previous'>(.*?)</span>", body, re.S)
        con = re.search(r"id='consensus'[^>]*>(.*?)</a>", body, re.S)
        out.append({
            "title": _te_txt(ev.group(1)),
            "country": cur,
            "_hm": ((int(tm.group(1)) % 12) + (12 if tm.group(3) == "PM" else 0),
                    int(tm.group(2))) if tm else None,
            "_day": dm.group(1) if dm else "",
            "impact": _TE_IMPACT.get(imp.group(1) if imp else "1", "Low"),
            "actual": _te_txt(act.group(1)) if act else "",
            "forecast": _te_txt(con.group(1)) if con else "",
            "previous": _te_txt(prv.group(1)) if prv else "",
        })
    return [x for x in out if x["title"]]


# Synonymes ForexFactory <-> TradingEconomics : les deux sites nomment
# différemment les mêmes statistiques. Table établie par comparaison réelle.
_CAL_SYN = [
    (r"\binflation rate\b", "cpi"), (r"\binterest rate decision\b", "rate"),
    (r"\bmom\b", "m/m"), (r"\byoy\b", "y/y"), (r"\bqoq\b", "q/q"),
    (r"\bquarterly\b", ""), (r"\bs&p global\b", ""), (r"\bmarkit\b", ""),
    (r"\bfed\b", ""), (r"\brba\b", ""), (r"\bboe\b", ""), (r"\becb\b", ""),
    (r"\bboj\b", ""), (r"\bnon farm\b", "non-farm"), (r"\bprelim\b", ""),
    (r"\bflash\b", ""), (r"\bfinal\b", ""), (r"\badvance\b", ""),
]


def _cal_key(title):
    t = title.lower().strip()
    for a, b in _CAL_SYN:
        t = re.sub(a, b, t)
    return set(re.findall(r"[a-z0-9/]+", t))


def _cal_sim(a, b):
    A, B = _cal_key(a), _cal_key(b)
    return len(A & B) / max(1, len(A | B))


# Familles d'événements que les deux sources nomment TOTALEMENT différemment.
# « Federal Funds Rate » (ForexFactory) et « Fed Interest Rate Decision »
# (TradingEconomics) désignent la même annonce mais ne partagent presque aucun
# mot : la similarité lexicale seule ne suffit pas à les rapprocher.
_CAL_FAMILIES = [
    # (motif ForexFactory, motif TradingEconomics)
    (r"federal funds rate|fomc statement", r"fed interest rate decision"),
    (r"official bank rate|monetary policy summary", r"boe interest rate decision"),
    (r"main refinancing rate|ecb.*rate", r"ecb interest rate decision"),
    (r"\bcash rate\b", r"rba interest rate decision"),
    (r"boj policy rate", r"boj interest rate decision"),
    (r"overnight rate|boc rate", r"boc interest rate decision"),
    (r"official cash rate", r"rbnz interest rate decision"),
    (r"fomc press conference", r"fed press conference"),
]


def _cal_same_event(a, b):
    """Deux intitulés désignent-ils le même événement ?

    On combine la similarité lexicale et la table des familles connues.
    """
    if _cal_sim(a, b) >= 0.45:
        return True
    la, lb = a.lower(), b.lower()
    for pa, pb in _CAL_FAMILIES:
        if (re.search(pa, la) and re.search(pb, lb)) or \
           (re.search(pa, lb) and re.search(pb, la)):
            return True
    return False


def _cal_enrich(rows, te_rows):
    """Complète les valeurs manquantes de ForexFactory avec TradingEconomics.

    On n'ÉCRASE jamais une valeur déjà publiée par ForexFactory : on ne comble
    que les trous. Objectif : si l'une des deux sources a déjà publié le
    chiffre, le trader le voit immédiatement, sans attendre l'autre.
    Retourne (nb_complétés, nb_divergences).
    """
    slots = {}
    for x in te_rows:
        if x["_hm"]:
            slots.setdefault((x["country"], x["_hm"][0], x["_hm"][1]), []).append(x)
    filled = diverge = 0
    for r in rows:
        try:
            d = datetime.fromisoformat(r["date"])
        except Exception:
            continue
        cand = slots.get((r["country"], d.hour, d.minute))
        if not cand:
            continue
        best = max(cand, key=lambda c: _cal_sim(r["title"], c["title"]))
        if _cal_sim(r["title"], best["title"]) < 0.45:
            continue
        for f in ("actual", "forecast", "previous"):
            if not r.get(f) and best.get(f):
                r[f] = best[f]
                if f == "actual":
                    filled += 1
            elif r.get(f) and best.get(f) and r[f] != best[f] and f == "actual":
                diverge += 1
    return filled, diverge


def _te_to_rows(te_rows):
    """Convertit les lignes TradingEconomics au format interne (date ISO UTC).

    Les événements sans horaire ou sans date exploitable sont ÉCARTÉS : mieux
    vaut ne rien afficher qu'afficher une date inventée. C'est exactement ce
    défaut qui plaçait le NFP du 7 août au 29 juillet.
    """
    out = []
    for x in te_rows:
        if not x.get("_hm") or not x.get("_day"):
            continue
        try:
            d = datetime.fromisoformat(x["_day"]).replace(
                hour=x["_hm"][0], minute=x["_hm"][1], tzinfo=timezone.utc)
        except Exception:
            continue
        out.append({"title": x["title"], "country": x["country"],
                    "date": d.isoformat(), "impact": x["impact"],
                    "actual": x["actual"], "forecast": x["forecast"],
                    "previous": x["previous"]})
    return out


def _cal_merge(base, extra):
    """Ajoute les événements d'`extra` absents de `base`.

    Deux événements sont considérés identiques s'ils partagent la devise,
    l'horaire à la minute près et un intitulé proche. Sert à étendre la
    couverture de ForexFactory (semaine courante) avec TradingEconomics
    (deux semaines), sans créer de doublons.
    """
    # On indexe par (devise, jour) et non à la minute près : les deux sources
    # décalent parfois l'horaire de quelques minutes pour un même événement.
    slots = {}
    for r in base:
        slots.setdefault((r["country"], r["date"][:10]), []).append(r)
    added = 0
    for x in extra:
        key = (x["country"], x["date"][:10])
        peers = slots.get(key, [])
        # Doublon si l'intitulé est proche ET l'horaire à moins de 45 min :
        # « Federal Funds Rate » (FF) et « Fed Interest Rate Decision » (TE)
        # sont le MÊME événement et ne doivent pas apparaître deux fois.
        dup = False
        try:
            dx = datetime.fromisoformat(x["date"])
        except Exception:
            continue
        for r in peers:
            try:
                dr = datetime.fromisoformat(r["date"])
            except Exception:
                continue
            if abs((dx - dr).total_seconds()) > 2700:
                continue
            if _cal_same_event(x["title"], r["title"]):
                dup = True
                break
        if dup:
            continue
        base.append(x)
        slots.setdefault(key, []).append(x)
        added += 1
    return added


def _cal_from_json():
    """Repli : flux JSON officiel (prévisions seules, sans « actual »)."""
    for src in ("https://nfs.faireconomy.media/ff_calendar_thisweek.json",
                "https://nfs.faireconomy.media/ff_calendar_nextweek.json"):
        try:
            d = json.loads(_mkt_get(src, _MKT_TIMEOUT))
        except Exception:
            continue
        if not d:
            continue
        out = []
        for e in d:
            out.append({
                "title": e.get("title", ""), "country": e.get("country", ""),
                "date": e.get("date", ""), "impact": e.get("impact", "Low"),
                "actual": str(e.get("actual") or "").strip(),
                "forecast": str(e.get("forecast") or "").strip(),
                "previous": str(e.get("previous") or "").strip(),
            })
        return out
    return None


def _cal_hot():
    """Sommes-nous dans la fenêtre de publication d'une annonce majeure ?

    On se fonde sur le dernier jeu d'événements connu : si une annonce à
    fort impact tombe dans les 10 minutes ou vient de tomber il y a moins
    de 15 minutes, on considère la période comme « chaude ».
    """
    st = _MKT_CACHE.get("cal_raw")
    if not st:
        return False
    now = datetime.now(timezone.utc)
    for e in st[1]:
        if e.get("impact") != "High":
            continue
        try:
            d = datetime.fromisoformat(e.get("date", ""))
        except Exception:
            continue
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        delta = (d - now).total_seconds()
        if -900 <= delta <= 600:
            return True
    return False


def _cal_jour_courant():
    """Date du jour, en temps universel : repère commun à tous les postes."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _cal_memoire_path(lang):
    return os.path.join(user_data_dir(), "cache", "cal_jour_%s.json" % lang)


def _cal_memoire_du_jour(lang, evs):
    """Complète la liste avec les publications du jour déjà observées.

    POURQUOI (constaté avec le client) : ForexFactory et TradingEconomics
    effacent de leur page les événements au fur et à mesure qu'ils sont
    publiés. À 18 h, les chiffres de 8 h n'y figurent plus. Le trader ne
    pouvait donc plus consulter ce qui était sorti le matin même.

    Fonctionnement : on garde sur disque les événements du jour déjà vus.
    Ceux qui disparaissent de la source sont réinjectés. Le fichier est
    daté : au changement de jour, il est ignoré puis remplacé — la journée
    close disparaît d'elle-même, exactement comme demandé.
    """
    jour = _cal_jour_courant()
    chemin = _cal_memoire_path(lang)

    def cle(e):
        return "%s|%s|%s" % ((e.get("date") or "")[:16],
                             (e.get("country") or "").upper(),
                             (e.get("title") or "").strip().lower()[:60])

    # Ce que la source vient de renvoyer, pour la journée en cours.
    frais = {}
    for e in evs:
        if (e.get("date") or "")[:10] == jour:
            frais[cle(e)] = e

    memoire = {}
    try:
        with open(chemin, encoding="utf-8") as f:
            d = json.load(f)
        # Fichier d'une journée révolue : on repart de zéro.
        if d.get("jour") == jour:
            for e in d.get("events") or []:
                memoire[cle(e)] = e
    except Exception:
        memoire = {}

    # Fusion : la version fraîche prime toujours (elle porte le chiffre
    # publié), mais on n'oublie jamais ce qui a été vu plus tôt.
    fusion = dict(memoire)
    fusion.update(frais)

    try:
        os.makedirs(os.path.dirname(chemin), exist_ok=True)
        tmp = chemin + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"jour": jour, "events": list(fusion.values())},
                      f, ensure_ascii=False)
        os.replace(tmp, chemin)
    except Exception:
        pass

    # On réinjecte uniquement ce qui manquait, sans toucher au reste.
    manquants = [e for k, e in fusion.items() if k not in frais]
    if not manquants:
        return evs
    complet = list(evs) + manquants
    complet.sort(key=lambda e: (e.get("date") or ""))
    return complet


@app.get("/api/calendar")
def api_calendar():
    lang = "fr" if request.args.get("lang", "fr").lower() == "fr" else "en"
    # ?fresh=1 : le trader a cliqué sur « Actualiser », on ignore le cache.
    fresh = request.args.get("fresh", "0") in ("1", "true", "yes")
    # Cache porté de 20 s à 3 minutes, et de 8 s à 30 s en fenêtre chaude.
    # Un cache de 20 s obligeait à re-scraper ForexFactory et
    # TradingEconomics à presque chaque affichage : c'était la première
    # cause de blocage par leurs pare-feu (429) et de gel de l'interface.
    # 30 s suffisent largement pour voir apparaître une valeur publiée.
    if not fresh:
        hit = _mkt_cached("cal_" + lang, _CAL_TTL_HOT if _cal_hot() else _CAL_TTL)
        if hit:
            return jsonify(hit)

    # ── Les DEUX sources sont interrogées EN PARALLÈLE ───────────────────
    # ForexFactory donne les horaires et les niveaux d'impact ;
    # TradingEconomics fournit les valeurs publiées et une couverture plus
    # longue. Auparavant elles étaient appelées l'une après l'autre : deux
    # sources lentes suffisaient à faire expirer la requête du navigateur.
    rows, source, srcs = None, "", []
    te_rows = []
    _res = {}

    def _job_ff():
        try:
            _res["ff"] = _cal_from_forexfactory()
        except Exception:
            _res["ff"] = None
        return []

    def _job_te():
        try:
            _res["te"] = _cal_from_tradingeconomics()
        except Exception:
            _res["te"] = []
        return []

    _mkt_parallel([_job_ff, _job_te], timeout=_CAL_BUDGET)
    rows = _res.get("ff")
    te_rows = _res.get("te") or []
    if rows:
        srcs.append("ForexFactory")

    filled = diverge = merged = 0
    if rows and te_rows:
        # a) on complète les valeurs publiées manquantes et on recoupe
        filled, diverge = _cal_enrich(rows, te_rows)
        # b) on ÉTEND la couverture : ForexFactory ne publie que la semaine
        #    en cours, TradingEconomics porte deux semaines. Sans cette
        #    fusion, une annonce majeure de la semaine suivante (le NFP par
        #    exemple) restait tout simplement invisible.
        merged = _cal_merge(rows, _te_to_rows(te_rows))
        srcs.append("TradingEconomics")
    elif not rows and te_rows:
        # ForexFactory indisponible (429) : TradingEconomics prend le relais,
        # valeurs publiées comprises. L'ancien repli JSON était muet sur
        # « actual » (0/92 vérifié) : il gelait la colonne.
        rows = _te_to_rows(te_rows)
        srcs.append("TradingEconomics")

    # ── Repli ultime : flux JSON (prévisions seules, AUCUN « actual ») ──
    if not rows:
        rows = _cal_from_json()
        if rows:
            srcs.append("ForexFactory (JSON, sans valeurs publiées)")

    source = " + ".join(srcs)
    if rows:
        _mkt_put("cal_raw", rows)
    else:
        st = _MKT_CACHE.get("cal_raw")
        if not st:
            # Dernier recours : la collecte précédente, en mémoire ou sur
            # disque. Renvoyer une liste vide faisait effacer l'affichage
            # côté navigateur — c'est précisément le gel constaté.
            last, age = _mkt_last_known("cal_" + lang)
            if last and last.get("events"):
                return jsonify(_mkt_stale(last, age))
            return jsonify({"ok": False, "error": "unavailable", "events": []})
        rows, source = st[1], "cache"

    evs = []
    for e in rows:
        ttl = e.get("title", "")
        evs.append({
            "title": _cal_tr(ttl) if lang == "fr" else ttl, "titleEn": ttl,
            "country": e.get("country", ""), "date": e.get("date", ""),
            "impact": e.get("impact", "Low"),
            "impactLabel": (IMPACT_FR.get(e.get("impact"), e.get("impact"))
                            if lang == "fr" else e.get("impact", "Low")),
            "forecast": e.get("forecast", ""), "previous": e.get("previous", ""),
            "actual": e.get("actual", ""),
        })
    evs.sort(key=lambda x: x["date"])
    n_act = sum(1 for x in evs if x["actual"])
    out = {"ok": True, "lang": lang, "count": len(evs), "source": source,
           "withActual": n_act, "filledFromTE": filled, "mismatch": diverge,
           "mergedFromTE": merged,
           "updated": datetime.now(timezone.utc).isoformat(), "events": evs}
    # ── MÉMOIRE DU JOUR ──────────────────────────────────────────────────
    # Les fournisseurs retirent progressivement de leur page les
    # publications déjà tombées : en fin d'après-midi, les chiffres du matin
    # ont disparu. Le trader perdait donc la trace de ce qui s'était produit
    # dans SA journée, alors que c'est exactement ce qu'il consulte pour
    # comprendre le mouvement en cours.
    #
    # On conserve donc tout événement DU JOUR déjà vu, jusqu'à minuit. À
    # minuit, la journée est close : la mémoire est vidée d'elle-même.
    out["events"] = _cal_memoire_du_jour(lang, out.get("events") or [])
    out["count"] = len(out["events"])
    _mkt_put("cal_" + lang, out)
    return jsonify(out)


# ── Sentiment Forex / matières premières / indices ────────────────────────
# Extraction depuis tradersentiments.com : le contenu est rendu côté serveur,
# donc lisible sans navigateur. Structure : Instrument|SYMBOLE|Biais|Avg Long|XX%
@app.get("/api/sentiment")
def api_sentiment():
    fresh = request.args.get("fresh", "0") in ("1", "true", "yes")
    if not fresh:
        hit = _mkt_cached("sent", _SENT_TTL)
        if hit:
            return jsonify(hit)

    def _sec(sec):
        out = []
        try:
            h = _mkt_get("https://tradersentiments.com/sentiment/" + sec, _MKT_TIMEOUT)
        except Exception:
            return out
        t = re.sub(r"<[^>]+>", "|", h)
        t = re.sub(r"\|+", "|", t)
        for sym, bias, lp in re.findall(
                r"Instrument\|([A-Z0-9]{3,10})\|(Bullish|Bearish|Neutral)\|Avg Long\|(\d{1,3})%", t):
            lp = int(lp)
            out.append({"symbol": sym, "longPct": lp, "shortPct": 100 - lp,
                        "bias": bias, "section": sec})
        return out

    # Les trois sections sont chargées en parallèle : trois appels
    # séquentiels de 6 s pouvaient à eux seuls dépasser le délai du client.
    rows = _mkt_parallel([(lambda x=x: _sec(x))
                          for x in ("forex", "commodities", "indices")],
                         timeout=_MKT_TIMEOUT + 3)
    if not rows:
        last, age = _mkt_last_known("sent")
        if last and last.get("symbols"):
            return jsonify(_mkt_stale(last, age))
        st = _MKT_CACHE.get("sent_last")
        if st:
            return jsonify(st[1])
        return jsonify({"ok": False, "error": "unavailable", "symbols": []})
    out = {"ok": True, "source": "TraderSentiments", "count": len(rows),
           "updated": datetime.now(timezone.utc).isoformat(), "symbols": rows}
    _mkt_put("sent", out)
    _mkt_put("sent_last", out)
    return jsonify(out)


# ── Veille autonome de l'agent IA ─────────────────────────────────────────
# L'agent, qui vit dans le navigateur, ne peut pas joindre l'extérieur : les
# sites financiers refusent les requêtes venant d'une page « file:// » (CORS).
# Ce point d'entrée lui donne un accès Internet INDIRECT mais RÉEL, en
# réutilisant les sources déjà en place. Un seul appel lui suffit pour décider
# de lui-même s'il doit alerter le trader.
#
# Il ne renvoie QUE l'essentiel : ce qui vient d'être publié, ce qui arrive,
# et les mouvements de sentiment notables. Aucune clé, aucun compte.
# ═══════════════════════════════════════════════════════════════════════════
#  GOLD EVENT RISK — module macroéconomique dédié XAU/USD          (v7.4)
# ---------------------------------------------------------------------------
#  Un seul module unifié, trois sous-composants servis par trois routes :
#
#     /api/gold/macro   → moteurs de fond + flux institutionnels + biais
#     /api/gold/news    → dépêches filtrées « catalyseurs Gold » uniquement
#     /api/gold/calendar→ calendrier prioritaire + matrice d'impact
#
#  CADENCE RÉELLE (mesurée, pas supposée)
#  --------------------------------------
#  Le cahier des charges demandait « 1 seconde ». Mesure faite sur les
#  sources gratuites disponibles : Yahoo ne publie une nouvelle valeur que
#  toutes les ~11 s (15 appels en 45 s → 4 horodatages distincts), et les
#  flux RSS se renouvellent toutes les 5 à 10 minutes. Interroger chaque
#  seconde produirait 86 400 requêtes par jour et par client, pour quatre
#  valeurs réelles sur quarante-cinq — avec un risque de blocage d'IP.
#
#  Le compteur « LIVE » de l'interface bat donc à la seconde, mais le
#  réseau est sollicité à la cadence utile, et ACCÉLÉRÉE automatiquement
#  autour des publications à fort impact (fenêtre chaude).
# ═══════════════════════════════════════════════════════════════════════════

# Durées de vie des caches, en secondes.
_GOLD_TTL_MACRO = 10          # cours : la source bouge toutes les ~11 s
_GOLD_TTL_MACRO_HOT = 3       # 30 min avant/après un événement rouge
_GOLD_TTL_NEWS = 55           # dépêches : les RSS bougent bien plus lentement
_GOLD_TTL_NEWS_HOT = 15
_GOLD_TTL_CAL = 180
_GOLD_TTL_FRED = 3600 * 6     # séries FRED : une publication par jour ouvré

_GOLD_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36")

# Instruments suivis. La clé est notre nom interne, jamais celui du
# fournisseur : changer de source ne doit pas changer l'API cliente.
# Chaque instrument possède DEUX identifiants : celui de Yahoo et celui de
# CNBC. Aucune source unique n'est fiable en permanence — mesuré en cours de
# développement : Yahoo a renvoyé « HTTP 429 Too Many Requests » et bloqué
# l'adresse plusieurs minutes après une série de tests. Un module qui
# n'aurait qu'une source aurait affiché un panneau vide chez le client.
_GOLD_SYMBOLES = {
    "xau":   ("GC=F",      "@GC.1", "XAU/USD",  "Once d'or (COMEX)"),
    "dxy":   ("DX-Y.NYB",  ".DXY",  "DXY",      "Indice dollar"),
    "us10y": ("^TNX",      "US10Y", "US10Y",    "Rendement 10 ans US"),
    "wti":   ("CL=F",      "@CL.1", "WTI",      "Pétrole brut WTI"),
    "gld":   ("GLD",       "GLD",   "GLD",      "SPDR Gold Shares"),
    "iau":   ("IAU",       "IAU",   "IAU",      "iShares Gold Trust"),
}

# Miroirs Yahoo : query1 sature avant query2. On alterne.
_GOLD_MIROIRS = ("https://query1.finance.yahoo.com",
                 "https://query2.finance.yahoo.com")
_GOLD_MIROIR_OK = {"i": 0}          # dernier miroir ayant répondu


def _gold_parallele(nommees, timeout=14):
    """Exécute des tâches NOMMÉES en parallèle et renvoie un dictionnaire.

    `_mkt_parallel` du Market Hub concatène des listes : il convient à la
    collecte de dépêches, pas ici où chaque tâche produit une valeur
    distincte (le cours du dollar, celui du pétrole…).

    Comme son aîné, cette fonction n'utilise PAS « with » sur
    l'exécuteur : le bloc « with » attend la fin de tous les threads, y
    compris une source muette, et ferait dépasser le budget. On rend la
    main au délai imparti et on laisse les retardataires mourir seuls.
    """
    out = {k: None for k in nommees}
    if not nommees:
        return out
    try:
        from concurrent.futures import ThreadPoolExecutor, as_completed
    except Exception:
        for k, f in nommees.items():
            try:
                out[k] = f()
            except Exception:
                pass
        return out
    ex = ThreadPoolExecutor(max_workers=min(10, len(nommees)))
    futs = {}
    for k, f in nommees.items():
        try:
            futs[ex.submit(f)] = k
        except Exception:
            pass
    try:
        for fu in as_completed(list(futs), timeout=timeout):
            k = futs.get(fu)
            try:
                out[k] = fu.result(timeout=0)
            except Exception:
                out[k] = None
    except Exception:
        pass                      # délai global atteint : on rend le partiel
    try:
        ex.shutdown(wait=False)
    except Exception:
        pass
    return out


def _gold_http(url, timeout=8):
    """Requête HTTP réutilisant le contexte SSL déjà éprouvé du Market Hub."""
    req = _urq.Request(url, headers={
        "User-Agent": _GOLD_UA,
        "Accept": "application/json, text/csv, text/plain, */*",
        "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
    })
    kw = {"timeout": timeout}
    if url.lower().startswith("https"):
        kw["context"] = _ssl_ctx()
    with _urq.urlopen(req, **kw) as r:
        return r.read().decode("utf-8", "replace")


# ───────────────────────────────────────────────────────────────────────────
#  A. MOTEURS DE FOND — cours des instruments
# ───────────────────────────────────────────────────────────────────────────
def _gold_cours_yahoo(cle):
    """Cours via Yahoo, en essayant les deux miroirs."""
    sym = _GOLD_SYMBOLES[cle][0]
    dernier = None
    ordre = [_GOLD_MIROIRS[_GOLD_MIROIR_OK["i"]],
             _GOLD_MIROIRS[1 - _GOLD_MIROIR_OK["i"]]]
    for idx, base in enumerate(ordre):
        try:
            d = json.loads(_gold_http(
                base + "/v8/finance/chart/" + _urq.quote(sym)
                + "?interval=5m&range=1d", 7))
            res = (d.get("chart") or {}).get("result") or []
            if not res:
                continue
            meta = res[0].get("meta") or {}
            prix = meta.get("regularMarketPrice")
            if prix is None:
                continue
            # Ce miroir fonctionne : on s'en souvient pour les appels suivants.
            _GOLD_MIROIR_OK["i"] = _GOLD_MIROIRS.index(base)
            veille = meta.get("chartPreviousClose") or meta.get("previousClose")
            serie = []
            try:
                cl = [c for c in (res[0]["indicators"]["quote"][0].get("close")
                                  or []) if c is not None]
                if len(cl) > 40:
                    pas = len(cl) / 40.0
                    cl = [cl[int(i * pas)] for i in range(40)]
                serie = [round(float(c), 4) for c in cl]
            except Exception:
                serie = []
            var = None
            if veille:
                try:
                    var = (float(prix) - float(veille)) / float(veille) * 100.0
                except Exception:
                    var = None
            return {
                "prix": round(float(prix), 4),
                "veille": round(float(veille), 4) if veille else None,
                "varPct": round(var, 3) if var is not None else None,
                "serie": serie,
                "maj": int(meta.get("regularMarketTime") or 0),
                "origine": "yahoo",
            }
        except Exception as e:
            dernier = e
            continue
    return None


def _gold_cours_cnbc(cle):
    """Repli : CNBC. Sert quand Yahoo limite l'accès (HTTP 429)."""
    sym = _GOLD_SYMBOLES[cle][1]
    try:
        d = json.loads(_gold_http(
            "https://quote.cnbc.com/quote-html-webservice/restQuote/"
            "symbolType/symbol?symbols=" + _urq.quote(sym)
            + "&requestMethod=itv&noform=1&partnerId=2&fund=1"
            "&exthrs=1&output=json", 7))
        q = (d.get("FormattedQuoteResult") or {}).get("FormattedQuote") or []
        if not q:
            return None
        x = q[0]

        def nb(v):
            if v in (None, "", "UNCH"):
                return None
            t = str(v).replace(",", "").replace("%", "").replace("+", "").strip()
            try:
                return float(t)
            except ValueError:
                return None

        prix = nb(x.get("last"))
        if prix is None:
            return None
        var = nb(x.get("change_pct"))
        # CNBC préfixe les baisses d'un « - » conservé par nb().
        return {"prix": round(prix, 4), "veille": None,
                "varPct": round(var, 3) if var is not None else None,
                "serie": [], "maj": 0, "origine": "cnbc"}
    except Exception:
        return None


def _gold_cours(cle):
    """Cours d'un instrument, avec repli automatique de source.

    Renvoie None si TOUTES les sources échouent — jamais une valeur
    inventée. Un panneau sans donnée doit le dire, pas mentir.
    """
    court, libelle = _GOLD_SYMBOLES[cle][2], _GOLD_SYMBOLES[cle][3]
    for collecteur in (_gold_cours_yahoo, _gold_cours_cnbc):
        d = collecteur(cle)
        if d:
            d.update({"cle": cle, "sym": court, "libelle": libelle,
                      "devise": "USD"})
            return d
    return None


def _gold_fred(serie_id):
    """Dernière valeur d'une série FRED (CSV public, sans clé d'API).

    DFII10 = rendement réel 10 ans (TIPS), la donnée fondamentale de l'or.
    T10YIE = inflation anticipée 10 ans (point mort).
    Ces deux séries sont PUBLIÉES, pas calculées par nous : le taux réel
    affiché est donc le vrai, pas une approximation.
    """
    # DEUX PIÈGES mesurés sur cette source :
    #  1. FRED ignore les requêtes qui se présentent comme un navigateur
    #     (« Mozilla/… ») : la connexion reste ouverte puis expire au bout
    #     de 12 s. Avec un agent neutre, la réponse arrive en 0,1 s.
    #  2. Sans borne de date, le CSV contient des décennies d'historique et
    #     dépasse le délai. On ne demande que les 60 derniers jours.
    debut = (datetime.now(timezone.utc) - timedelta(days=60)).strftime("%Y-%m-%d")
    url = ("https://fred.stlouisfed.org/graph/fredgraph.csv?id="
           + _urq.quote(serie_id) + "&cosd=" + debut)
    try:
        # L'agent DOIT rester « curl/… ». Testé : « Mozilla/5.0 », un agent
        # applicatif ou un agent vide font expirer la connexion au bout de
        # 8 à 12 s, tandis que « curl/8.5.0 » répond en 0,06 s. Le serveur
        # de FRED réserve visiblement ce chemin CSV aux clients en ligne de
        # commande. Ne pas « moderniser » cet en-tête.
        req = _urq.Request(url, headers={"User-Agent": "curl/8.5.0",
                                         "Accept": "*/*"})
        with _urq.urlopen(req, timeout=8, context=_ssl_ctx()) as r:
            txt = r.read().decode("utf-8", "replace")
        lignes = [l.strip() for l in txt.splitlines() if l.strip()]
        precedent = None
        dernier = None
        for l in reversed(lignes[1:]):
            parts = l.split(",")
            if len(parts) < 2:
                continue
            val = parts[-1].strip()
            if val in (".", "", "NA"):
                continue
            try:
                v = float(val)
            except ValueError:
                continue
            if dernier is None:
                dernier = {"date": parts[0].strip(), "valeur": v}
            elif precedent is None:
                precedent = v
                break
        if dernier is None:
            return None
        if precedent is not None:
            dernier["delta"] = round(dernier["valeur"] - precedent, 3)
        return dernier
    except Exception:
        return None


# ───────────────────────────────────────────────────────────────────────────
#  B. FLUX INSTITUTIONNELS — ETF et banques centrales
# ---------------------------------------------------------------------------
#  HONNÊTETÉ SUR CE BLOC (vérifié par mesure)
#  spdrgoldshares.com et gold.org répondent 301/Cloudflare à toute requête
#  automatisée depuis un serveur : les tonnages officiels ne sont PAS
#  récupérables. L'API « quote » de Yahoo (actifs nets) répond
#  « Unauthorized ».
#
#  Nous publions donc un INDICATEUR INDIRECT, calculé à partir du prix et
#  du volume de GLD et IAU — deux séries qui, elles, sont accessibles. Le
#  champ « estime » vaut True et l'interface l'affiche explicitement.
#  Jamais un chiffre inventé présenté comme officiel.
# ───────────────────────────────────────────────────────────────────────────

# Réserves officielles des banques centrales (World Gold Council).
# Chiffre trimestriel saisi à la main, avec sa date : le site bloque
# l'accès automatisé. À réviser à chaque version.
_GOLD_BC_REF = {
    "periode": "T2 2026",
    "achatsNetsTonnes": 166.0,
    "commentaire": "Achats nets des banques centrales, source World Gold Council.",
    "source": "World Gold Council — Gold Demand Trends",
    "saisieLe": "2026-08-03",
}


def _gold_etf():
    """Tendance des flux ETF, estimée à partir du prix et du volume."""
    out = {"estime": True, "fonds": [], "biais": "neutre",
           "note": "Estimation calculée sur prix et volume : les tonnages "
                   "officiels ne sont pas accessibles automatiquement."}
    score = 0.0
    poids = 0
    for cle in ("gld", "iau"):
        c = _gold_cours(cle)
        if not c:
            continue
        vol = None
        try:
            sym = _GOLD_SYMBOLES[cle][0]
            base = _GOLD_MIROIRS[_GOLD_MIROIR_OK["i"]]
            d = json.loads(_gold_http(
                base + "/v8/finance/chart/"
                + _urq.quote(sym) + "?interval=1d&range=1mo", 8))
            r0 = d["chart"]["result"][0]
            vols = [v for v in (r0["indicators"]["quote"][0].get("volume") or [])
                    if v]
            if len(vols) >= 5:
                recent = vols[-1]
                moyen = sum(vols[-20:]) / float(len(vols[-20:]))
                vol = {"dernier": int(recent), "moyen20": int(moyen),
                       "ratio": round(recent / moyen, 2) if moyen else None}
        except Exception:
            vol = None

        # Un volume au-dessus de sa moyenne AVEC un prix en hausse suggère
        # une collecte ; en baisse, une décollecte. C'est un faisceau
        # d'indices, pas une mesure — d'où « estime ».
        sens = 0.0
        if c.get("varPct") is not None and vol and vol.get("ratio"):
            sens = (1.0 if c["varPct"] > 0 else -1.0) * min(vol["ratio"], 2.5)
            score += sens
            poids += 1
        out["fonds"].append({
            "sym": c["sym"], "prix": c["prix"], "varPct": c.get("varPct"),
            "volume": vol, "tendance": ("entrees" if sens > 0.3
                                        else "sorties" if sens < -0.3
                                        else "stable"),
        })
    if poids:
        m = score / poids
        out["biais"] = ("entrees" if m > 0.3 else "sorties" if m < -0.3
                        else "neutre")
        out["intensite"] = round(abs(m), 2)
    return out


# ───────────────────────────────────────────────────────────────────────────
#  C. BIAIS MACRO SYNTHÉTIQUE
# ---------------------------------------------------------------------------
#  Trois moteurs, pondérés selon leur influence réelle sur le métal :
#    · taux réels  (poids 3) — le driver dominant : l'or ne verse pas de
#      coupon, il souffre quand le rendement réel monte ;
#    · dollar      (poids 2) — corrélation inverse structurelle ;
#    · pétrole     (poids 1) — proxy d'inflation, soutient l'or à la hausse.
#  Le détail du calcul est renvoyé au client : rien n'est opaque.
# ───────────────────────────────────────────────────────────────────────────
def _gold_biais(reels, dxy, wti):
    composantes = []
    total = 0.0
    somme_poids = 0

    def ajouter(nom, libelle, valeur, poids, sens, explication):
        nonlocal total, somme_poids
        if valeur is None:
            composantes.append({"cle": nom, "libelle": libelle,
                                "dispo": False, "explication": explication})
            return
        contribution = sens * poids
        total += contribution
        somme_poids += poids
        composantes.append({
            "cle": nom, "libelle": libelle, "dispo": True,
            "valeur": valeur, "poids": poids,
            "effet": ("haussier" if sens > 0 else
                      "baissier" if sens < 0 else "neutre"),
            "explication": explication,
        })

    # Taux réels : en hausse -> défavorable à l'or.
    if reels and reels.get("delta") is not None:
        d = reels["delta"]
        sens = -1.0 if d > 0.01 else 1.0 if d < -0.01 else 0.0
        ajouter("reels", "Taux réels US 10 ans", reels.get("valeur"), 3, sens,
                "Un taux réel qui monte renchérit le coût d'opportunité de "
                "détenir un actif sans rendement.")
    else:
        ajouter("reels", "Taux réels US 10 ans", None, 3, 0.0,
                "Série FRED momentanément indisponible.")

    # Dollar : en hausse -> défavorable à l'or.
    if dxy and dxy.get("varPct") is not None:
        v = dxy["varPct"]
        sens = -1.0 if v > 0.05 else 1.0 if v < -0.05 else 0.0
        ajouter("dxy", "Dollar (DXY)", v, 2, sens,
                "L'or se règle en dollars : un billet vert plus cher pèse "
                "mécaniquement sur le métal.")
    else:
        ajouter("dxy", "Dollar (DXY)", None, 2, 0.0, "Cours indisponible.")

    # Pétrole : en hausse -> favorable à l'or (inflation attendue).
    if wti and wti.get("varPct") is not None:
        v = wti["varPct"]
        sens = 1.0 if v > 0.5 else -1.0 if v < -0.5 else 0.0
        ajouter("wti", "Pétrole WTI", v, 1, sens,
                "Un baril plus cher nourrit l'inflation, ce qui soutient "
                "traditionnellement l'or comme couverture.")
    else:
        ajouter("wti", "Pétrole WTI", None, 1, 0.0, "Cours indisponible.")

    score = (total / somme_poids) if somme_poids else 0.0
    if score > 0.34:
        etiquette, cle = "Haussier", "bull"
    elif score < -0.34:
        etiquette, cle = "Baissier", "bear"
    else:
        etiquette, cle = "Neutre", "neutral"

    return {
        "score": round(score, 3),
        "score100": int(round((score + 1) / 2 * 100)),
        "cle": cle, "etiquette": etiquette,
        "fiabilite": ("haute" if somme_poids >= 6 else
                      "moyenne" if somme_poids >= 3 else "faible"),
        "composantes": composantes,
    }


# ───────────────────────────────────────────────────────────────────────────
#  D. CALENDRIER PRIORITAIRE + MATRICE D'IMPACT INTERMARCHÉ
# ───────────────────────────────────────────────────────────────────────────
#  Chaque famille : (motif de reconnaissance, libellé, priorité,
#                    impact XAU / DXY / WTI si le chiffre sort AU-DESSUS
#                    des attentes)
# ═══════════════════════════════════════════════════════════════════════════
#  FICHES PÉDAGOGIQUES — une par famille d'indicateur
# ---------------------------------------------------------------------------
#  Le trader clique sur une ligne du calendrier et obtient, sur le modèle de
#  l'analyste du Market Hub : ce que mesure l'indicateur, POURQUOI il déplace
#  l'or, et comment le lire en séance. Rédigé pour un trader, pas pour un
#  économiste : phrases courtes, mécanisme concret, piège à éviter.
# ═══════════════════════════════════════════════════════════════════════════
_GOLD_FICHES = {
    "cpi": {
        "titre": "CPI — indice des prix à la consommation",
        "quoi": "Mesure la hausse des prix payés par les ménages américains "
                "sur un mois et sur un an. C'est le thermomètre de "
                "l'inflation le plus suivi de la planète.",
        "pourquoi": "L'or est traditionnellement une protection contre "
                    "l'inflation. Mais le lien n'est pas direct : une "
                    "inflation forte pousse la Fed à monter ses taux, ce qui "
                    "renchérit le coût de détention d'un métal qui ne verse "
                    "aucun intérêt. Dans les faits, un CPI au-dessus des "
                    "attentes fait le plus souvent BAISSER l'or à court "
                    "terme, par anticipation d'une Fed plus dure.",
        "lecture": "Ne regardez pas le chiffre brut mais l'ÉCART avec le "
                   "consensus. Un écart de 0,1 point suffit à déclencher un "
                   "mouvement. Surveillez surtout le « core » (hors "
                   "alimentation et énergie) : c'est celui que la Fed regarde.",
        "piege": "Publié à 14 h 30 heure de Paris : la première bougie est "
                 "souvent un faux mouvement. Beaucoup de traders se font "
                 "prendre sur le pic initial, avant le vrai sens.",
    },
    "pce": {
        "titre": "Core PCE — la mesure préférée de la Fed",
        "quoi": "Indice des prix des dépenses de consommation, hors "
                "alimentation et énergie. Moins médiatique que le CPI, mais "
                "c'est celui que la Réserve fédérale utilise officiellement "
                "pour sa cible de 2 %.",
        "pourquoi": "Parce que la Fed pilote ses taux sur cet indicateur, il "
                    "conditionne directement les taux réels — le premier "
                    "moteur de l'or. Un Core PCE qui s'éloigne de 2 % "
                    "repousse les baisses de taux espérées et pèse sur le "
                    "métal.",
        "lecture": "Le rythme annuel compte plus que le chiffre mensuel. "
                   "Trois publications de suite dans la même direction "
                   "valent bien plus qu'un chiffre isolé.",
        "piege": "Sort souvent en même temps que les revenus et dépenses des "
                 "ménages. Vérifiez lequel des trois le marché retient avant "
                 "d'interpréter le mouvement.",
    },
    "nfp": {
        "titre": "NFP — emplois non agricoles",
        "quoi": "Nombre d'emplois créés ou détruits aux États-Unis le mois "
                "précédent, hors secteur agricole. Publié le premier "
                "vendredi du mois.",
        "pourquoi": "Un marché du travail solide autorise la Fed à maintenir "
                    "des taux élevés : le dollar monte, les rendements "
                    "montent, et l'or recule. Un chiffre décevant nourrit "
                    "l'espoir de baisses de taux et soutient le métal.",
        "lecture": "Le chiffre principal, la révision du mois précédent et "
                   "le salaire horaire forment un tout. Une belle création "
                   "d'emplois accompagnée d'une révision en baisse du mois "
                   "d'avant annule souvent la bonne nouvelle.",
        "piege": "C'est la publication la plus volatile du mois sur XAU/USD. "
                 "Les écarts de cotation sont fréquents dans les secondes "
                 "qui suivent : un ordre stop peut être exécuté loin du prix "
                 "demandé.",
    },
    "unemp": {
        "titre": "Taux de chômage",
        "quoi": "Part de la population active à la recherche d'un emploi. "
                "Publié en même temps que le NFP.",
        "pourquoi": "Un chômage qui remonte signale une économie qui "
                    "ralentit, donc une Fed plus conciliante : favorable à "
                    "l'or. C'est l'un des deux mandats de la Fed, à égalité "
                    "avec la stabilité des prix.",
        "lecture": "À lire avec le taux de participation. Un chômage qui "
                   "baisse parce que des gens renoncent à chercher n'est pas "
                   "un bon signe et le marché le sait.",
        "piege": "Un dixième de point paraît minime mais suffit à retourner "
                 "la séance quand il contredit le NFP publié à la même "
                 "seconde.",
    },
    "fomc": {
        "titre": "FOMC — décision de taux de la Fed",
        "quoi": "Le comité de politique monétaire fixe le taux directeur "
                "américain. Huit réunions par an.",
        "pourquoi": "C'est l'événement le plus puissant du calendrier pour "
                    "l'or. Le taux directeur détermine la rémunération du "
                    "dollar : plus il est élevé, plus détenir un métal sans "
                    "coupon coûte cher en manque à gagner.",
        "lecture": "La décision elle-même est presque toujours anticipée. Ce "
                   "qui fait bouger le marché, c'est le TON du communiqué, "
                   "les projections de taux et la conférence de presse qui "
                   "suit trente minutes plus tard.",
        "piege": "Deux mouvements opposés dans l'heure sont la norme : un sur "
                 "le communiqué, un autre sur la conférence. Beaucoup de "
                 "traders gagnent sur le premier et rendent tout sur le "
                 "second.",
    },
    "fedspk": {
        "titre": "Intervention d'un membre de la Fed",
        "quoi": "Discours d'un gouverneur ou d'un président de Fed "
                "régionale, hors réunion officielle.",
        "pourquoi": "Entre deux réunions, ces prises de parole servent à "
                    "préparer le marché. Un ton plus dur ou plus souple que "
                    "prévu réajuste immédiatement les anticipations de taux, "
                    "donc l'or.",
        "lecture": "Tous les intervenants ne se valent pas : le président de "
                   "la Fed et les membres votants pèsent bien plus que les "
                   "autres. Repérez si la personne vote cette année.",
        "piege": "Aucun chiffre à comparer : l'impact dépend entièrement des "
                 "mots employés. Sans lire le discours, mieux vaut "
                 "s'abstenir plutôt que deviner le sens.",
    },
    "bond": {
        "titre": "Adjudication du Trésor et rendements",
        "quoi": "L'État américain emprunte en vendant des obligations. "
                "L'adjudication révèle à quel taux les investisseurs "
                "acceptent de prêter.",
        "pourquoi": "Le rendement à 10 ans est le concurrent direct de l'or : "
                    "un placement sûr qui, lui, rapporte. Quand ce rendement "
                    "monte, détenir du métal devient moins attractif.",
        "lecture": "Regardez la demande (ratio de couverture) autant que le "
                   "taux. Une adjudication mal souscrite fait monter les "
                   "rendements et pèse sur l'or dans les heures qui suivent.",
        "piege": "Effet plus lent et plus diffus qu'un CPI ou un NFP : le "
                 "mouvement se construit sur la séance, pas sur la minute.",
    },
    "opec": {
        "titre": "Réunion ou rapport OPEP+",
        "quoi": "Les pays producteurs décident de leurs quotas de "
                "production de pétrole.",
        "pourquoi": "Le baril alimente l'inflation. Une production réduite "
                    "fait monter le pétrole, donc les anticipations "
                    "d'inflation, ce qui soutient l'or comme couverture. "
                    "L'effet est indirect mais réel.",
        "lecture": "Comparez la décision aux fuites parues dans la presse "
                   "les jours précédents : le marché a souvent déjà intégré "
                   "l'essentiel avant l'annonce.",
        "piege": "L'impact sur l'or est de second rang. Ne construisez pas "
                 "une position XAU uniquement sur une décision OPEP.",
    },
    "eia": {
        "titre": "Stocks de pétrole EIA",
        "quoi": "Variation hebdomadaire des réserves de brut aux "
                "États-Unis. Publié chaque mercredi.",
        "pourquoi": "Des stocks en baisse signalent une demande vigoureuse : "
                    "le baril monte, les anticipations d'inflation avec lui, "
                    "et l'or en profite marginalement.",
        "lecture": "C'est d'abord un événement pour le WTI. Sur l'or, "
                   "l'effet n'apparaît que si la surprise est très large.",
        "piege": "Ne prenez pas position sur XAU/USD pour ce seul chiffre : "
                 "le rapport bouge le pétrole, rarement le métal.",
    },
    "ppi": {
        "titre": "PPI — prix à la production",
        "quoi": "Prix pratiqués par les producteurs américains, en amont de "
                "la chaîne.",
        "pourquoi": "Ce que paient les entreprises finit par se retrouver "
                    "dans les prix à la consommation. Le PPI est donc un "
                    "signal avancé de l'inflation, et à ce titre il "
                    "influence les anticipations de taux.",
        "lecture": "Sert surtout à confirmer ou nuancer le CPI publié à "
                   "quelques jours d'intervalle. Deux signaux concordants "
                   "portent davantage.",
        "piege": "Impact plus faible que le CPI : le marché lui accorde "
                 "moins d'attention, sauf en période de forte inflation.",
    },
    "gdp": {
        "titre": "PIB des États-Unis",
        "quoi": "Richesse produite sur le trimestre. Publié en trois "
                "versions successives, de la plus provisoire à la "
                "définitive.",
        "pourquoi": "Une croissance vigoureuse conforte la Fed dans une "
                    "politique restrictive : dollar ferme, or sous "
                    "pression. Une croissance qui déçoit ouvre la porte aux "
                    "baisses de taux.",
        "lecture": "La première estimation fait bouger le marché ; les "
                   "révisions suivantes passent souvent inaperçues.",
        "piege": "Donnée trimestrielle, donc largement anticipée. La "
                 "surprise est rare et le mouvement souvent modeste.",
    },
    "retail": {
        "titre": "Ventes au détail",
        "quoi": "Dépenses des ménages américains sur le mois écoulé.",
        "pourquoi": "La consommation représente près de 70 % de l'économie "
                    "américaine. Des ventes solides soutiennent la "
                    "croissance et les taux, donc pèsent sur l'or.",
        "lecture": "Regardez la version « hors automobile et carburant » : "
                   "elle élimine les postes les plus erratiques et reflète "
                   "mieux la tendance de fond.",
        "piege": "Chiffre souvent révisé le mois suivant. Une belle surprise "
                 "peut être effacée trente jours plus tard.",
    },
}


_GOLD_FAMILLES = [
    ("cpi",   r"\b(cpi|consumer price|inflation|ipc)\b",
     "CPI — inflation US", 1, ("baissier", "haussier", "neutre")),
    ("pce",   r"\b(pce|personal consumption)\b",
     "Core PCE — mesure préférée de la Fed", 1,
     ("baissier", "haussier", "neutre")),
    ("nfp",   r"\b(non.?farm|nfp|payroll|emploi non agricole)\b",
     "NFP — emplois non agricoles", 1, ("baissier", "haussier", "haussier")),
    ("unemp", r"\b(unemployment rate|taux de ch[oô]mage)\b",
     "Taux de chômage", 1, ("haussier", "baissier", "baissier")),
    ("fomc",  r"\b(fomc|fed interest rate|federal funds|taux directeur)\b",
     "Décision de taux FOMC", 1, ("baissier", "haussier", "neutre")),
    ("fedspk", r"\b(fed|powell|fomc member|governor|beige book)\b",
     "Intervention de la Fed", 2, ("variable", "variable", "neutre")),
    ("bond",  r"\b(treasury|adjudication|auction|bond|note|yield)\b",
     "Adjudication du Trésor / rendements", 2,
     ("baissier", "haussier", "neutre")),
    ("opec",  r"\b(opec|opep)\b",
     "Réunion / rapport OPEP+", 2, ("haussier", "neutre", "haussier")),
    ("eia",   r"\b(eia|crude oil invent|stocks de p[ée]trole|api weekly)\b",
     "Stocks de pétrole EIA", 2, ("neutre", "neutre", "baissier")),
    ("ppi",   r"\b(ppi|producer price)\b",
     "PPI — prix à la production", 2, ("baissier", "haussier", "neutre")),
    ("gdp",   r"\b(gdp|pib)\b",
     "PIB des États-Unis", 2, ("baissier", "haussier", "haussier")),
    ("retail", r"\b(retail sales|ventes au d[ée]tail)\b",
     "Ventes au détail", 2, ("baissier", "haussier", "haussier")),
]


def _gold_classer(titre, pays=""):
    """Reconnaît la famille d'un événement. None si hors périmètre Gold."""
    t = (titre or "").lower()
    p = (pays or "").upper()
    for cle, motif, libelle, prio, matrice in _GOLD_FAMILLES:
        if re.search(motif, t, re.I):
            # OPEP et pétrole sont mondiaux ; le reste ne compte que pour
            # les États-Unis — une inflation néo-zélandaise ne déplace pas
            # le métal.
            if cle in ("opec", "eia") or p in ("USD", "US", "ALL", ""):
                return {"famille": cle, "libelle": libelle,
                        "priorite": prio,
                        "impact": {"xau": matrice[0], "dxy": matrice[1],
                                   "wti": matrice[2]}}
    return None


def _gold_matrice(cls, actual, forecast):
    """Ajuste la matrice d'impact selon le chiffre réellement publié.

    La matrice de référence décrit l'effet d'une surprise À LA HAUSSE.
    Si le chiffre sort SOUS les attentes, chaque effet s'inverse.
    """
    base = dict(cls["impact"])
    surprise = None
    try:
        def nombre(x):
            s = re.sub(r"[^\d\.\-]", "", str(x or ""))
            return float(s) if s not in ("", "-", ".") else None
        a, f = nombre(actual), nombre(forecast)
        if a is not None and f is not None:
            surprise = "au-dessus" if a > f else "en-dessous" if a < f else "conforme"
            if surprise == "en-dessous":
                inv = {"haussier": "baissier", "baissier": "haussier"}
                base = {k: inv.get(v, v) for k, v in base.items()}
            elif surprise == "conforme":
                base = {k: "neutre" for k in base}
    except Exception:
        surprise = None
    return base, surprise


# ───────────────────────────────────────────────────────────────────────────
#  E. FILTRE DES DÉPÊCHES — ne garder que les catalyseurs
# ───────────────────────────────────────────────────────────────────────────
# ═══════════════════════════════════════════════════════════════════════════
#  FLUX DÉDIÉS AU GOLD — 26 sources, français et anglais
# ---------------------------------------------------------------------------
#  CONSTAT MESURÉ AVANT CORRECTION : le fil se contentait de filtrer les
#  dépêches du Market Hub, conçues pour un usage généraliste. Résultat :
#  19 dépêches retenues sur 244, dont 14 de plus de six heures, une médiane
#  à 94 heures et la plus ancienne à 409 heures (17 jours). Le filtre, trop
#  sévère, raclait le fond du panier pour remplir l'écran.
#
#  On interroge désormais des sources CIBLÉES sur les moteurs du métal, dont
#  des requêtes Google News bornées dans le temps (« when:6h », « when:12h »)
#  qui ne renvoient que du récent par construction. Toutes ont été testées :
#  celles qui refusent l'accès depuis un serveur (FXStreet, DailyFX, Kitco,
#  Les Échos, Trading Economics, Boursier : 403 ou 404) ont été écartées.
# ═══════════════════════════════════════════════════════════════════════════
def _gn(requete, langue="en"):
    """Requête Google News bornée dans le temps.

    L'opérateur « when: » garantit la fraîcheur à la source : inutile de
    filtrer un historique de trois semaines côté moteur.
    """
    base = "https://news.google.com/rss/search?q=" + _urq.quote(requete)
    return base + ("&hl=fr&gl=FR&ceid=FR:fr" if langue == "fr"
                   else "&hl=en-US&gl=US&ceid=US:en")


# (url, source affichée, thème dominant, langue)
_GOLD_FEEDS = [
    # ── Ciblés or, en français ────────────────────────────────────────────
    (_gn("when:6h (or OR once OR XAU) (cours OR prix OR marché)", "fr"),
     "Actus Or", "or", "fr"),
    (_gn("when:12h (Réserve fédérale OR Fed) (taux OR inflation)", "fr"),
     "Actus Fed", "fed", "fr"),
    (_gn("when:12h (dollar OR DXY) (marché OR change)", "fr"),
     "Actus Dollar", "dollar", "fr"),
    (_gn("when:12h (pétrole OR OPEP OR baril)", "fr"),
     "Actus Pétrole", "petrole", "fr"),
    (_gn("when:12h (tensions OR conflit OR sanctions) (marchés OR pétrole OR or)", "fr"),
     "Actus Géopolitique", "geopol", "fr"),
    (_gn("when:12h (inflation OR BCE OR taux directeur)", "fr"),
     "Actus Inflation", "inflation", "fr"),
    # ── Ciblés or, en anglais ─────────────────────────────────────────────
    (_gn("when:6h (gold OR XAU OR bullion) (price OR market OR futures)", "en"),
     "Gold Wire", "or", "en"),
    (_gn("when:12h (FOMC OR Powell OR \"rate cut\" OR \"rate hike\")", "en"),
     "Fed Wire", "fed", "en"),
    (_gn("when:12h (treasury yield OR \"10-year\" OR bond market)", "en"),
     "Rates Wire", "taux", "en"),
    (_gn("when:12h (dollar index OR DXY OR greenback)", "en"),
     "Dollar Wire", "dollar", "en"),
    (_gn("when:12h (CPI OR inflation OR \"core PCE\")", "en"),
     "Inflation Wire", "inflation", "en"),
    (_gn("when:12h (OPEC OR crude oil OR WTI)", "en"),
     "Oil Wire", "petrole", "en"),
    (_gn("when:12h (central bank gold OR gold reserves)", "en"),
     "Central Banks", "banque", "en"),
    (_gn("when:12h (geopolitical OR sanctions OR conflict) (markets OR gold OR oil)", "en"),
     "Geopolitics", "geopol", "en"),
    (_gn("when:12h (safe haven OR risk off OR flight to quality)", "en"),
     "Risk Wire", "or", "en"),
    (_gn("when:12h (non-farm payrolls OR jobless claims OR unemployment rate)", "en"),
     "Jobs Wire", "emploi", "en"),
    # ── Rédactions établies ───────────────────────────────────────────────
    ("https://fr.investing.com/rss/commodities_Metals.rss",
     "Investing FR", "or", "fr"),
    ("https://fr.investing.com/rss/news_14.rss",
     "Investing FR", "inflation", "fr"),
    ("https://fr.investing.com/rss/forex.rss",
     "Investing FR", "dollar", "fr"),
    ("https://www.investing.com/rss/news_285.rss",
     "Investing", "fed", "en"),
    ("https://feeds.content.dowjones.io/public/rss/RSSMarketsMain",
     "MarketWatch", "taux", "en"),
    ("https://feeds.content.dowjones.io/public/rss/mw_bulletins",
     "MarketWatch", "taux", "en"),
    ("https://feeds.bloomberg.com/markets/news.rss",
     "Bloomberg", "taux", "en"),
    ("https://search.cnbc.com/rs/search/combinedcms/view.xml"
     "?partnerId=wrss01&id=10000664", "CNBC", "fed", "en"),
    ("https://www.theguardian.com/uk/business/rss",
     "The Guardian", "inflation", "en"),
    ("https://feeds.bbci.co.uk/news/business/rss.xml",
     "BBC Business", "inflation", "en"),
    ("https://oilprice.com/rss/main", "OilPrice", "petrole", "en"),
    ("https://seekingalpha.com/market_currents.xml",
     "Seeking Alpha", "taux", "en"),
    ("https://finance.yahoo.com/news/rssindex", "Yahoo Finance", "taux", "en"),
    ("https://feeds.feedburner.com/zerohedge/feed", "ZeroHedge", "geopol", "en"),
    ("https://www.kitco.com/rss/KitcoNews.xml", "Kitco", "or", "en"),
    ("https://www.forexlive.com/feed/news", "ForexLive", "dollar", "en"),
]

# Au-delà de cet âge, une dépêche n'est plus un catalyseur mais une archive.
_GOLD_AGE_MAX = 36 * 3600


def _gold_feed(url, source, theme, langue):
    """Lit un flux et renvoie ses entrées récentes, sans jamais lever."""
    out = []
    try:
        xml = _gold_http(url, 9)
    except Exception:
        return out
    # RSS comme Atom : on accepte les deux formats. On réutilise _tag() et
    # _parse_date(), déjà éprouvés par le Market Hub, plutôt que de
    # réécrire une analyse XML approximative.
    blocs = re.findall(r"<item[\s\S]*?</item>", xml, re.I) or \
            re.findall(r"<entry[\s\S]*?</entry>", xml, re.I)
    now = datetime.now(timezone.utc)
    for b in blocs[:40]:
        titre = _tag(b, "title")
        if not titre:
            continue
        lien = _tag(b, "link")
        if not lien:
            m = re.search(r'<link[^>]*href=["\']([^"\']+)["\']', b, re.I)
            lien = m.group(1) if m else ""
        quand = _parse_date(_tag(b, "pubDate") or _tag(b, "published")
                            or _tag(b, "updated"))
        if not quand:
            continue                     # sans date, impossible de trier
        try:
            age = (now - quand).total_seconds()
        except Exception:
            continue
        if age < 0 or age > _GOLD_AGE_MAX:
            continue                     # trop vieux : ce n'est plus une info
        chapo = _tag(b, "description") or _tag(b, "summary") or _tag(b, "content")
        if chapo:
            chapo = re.sub(r"<[^>]+>", " ", chapo)
            chapo = re.sub(r"\s+", " ", chapo).strip()[:280]
        out.append({"title": titre, "link": lien, "source": source,
                    "date": quand.isoformat(), "theme": theme,
                    "lang": langue, "ageSec": int(age),
                    "resume": chapo or ""})
    return out


_GOLD_MOTS = {
    # « or » seul capte trop large en français : Côte-d'Or, « or donc »,
    # livre d'or, âge d'or… Constaté en test sur un flux ciblé, qui
    # remontait « Les éleveurs de Côte-d'Or mobilisés ». On exige donc un
    # contexte de marché autour du mot.
    "or": (r"\b(gold|xau|bullion|lingot|m[ée]taux pr[ée]cieux|precious metal|"
           r"once d.or|cours de l.or|prix de l.or|l.or (?:monte|recule|grimpe|"
           r"chute|baisse|progresse|s.envole|recul|hausse)|"
           r"(?:cours|prix|march[ée]|onces?) d.or)\b", 3),
    "fed": (r"\b(fed|f[ée]d[ée]rale|fomc|powell|taux directeur|rate cut|"
            r"rate hike|hausse des taux|baisse des taux|quantitative)\b", 3),
    "inflation": (r"\b(inflation|cpi|ipc|pce|ppi|d[ée]sinflation|"
                  r"prix à la consommation)\b", 2),
    "dollar": (r"\b(dollar|dxy|greenback|billet vert|us currency)\b", 2),
    "taux": (r"\b(treasury|rendement|yield|obligation|bond|10.?ans|10.?year|"
             r"courbe des taux)\b", 2),
    "emploi": (r"\b(nfp|non.?farm|payroll|emploi|unemployment|ch[oô]mage|"
               r"jobless)\b", 2),
    "petrole": (r"\b(oil|p[ée]trole|opec|opep|brut|wti|brent|baril|eia)\b", 2),
    "geopol": (r"\b(guerre|war|conflit|sanction|tension|missile|frappe|"
               r"attaque|c[eé]ssez-le-feu|ceasefire|escalade|nucl[ée]aire|"
               r"g[ée]opolitique|geopolit)\b", 3),
    "banque": (r"\b(banque centrale|central bank|bce|ecb|boj|pboc|"
               r"r[ée]serves d.or|gold reserves)\b", 2),
}

# Termes qui trahissent une dépêche « bruit » : promotion, sport, people.
_GOLD_EXCLUS = re.compile(
    r"\b(football|soccer|tennis|basket|cin[ée]ma|film|s[ée]rie|c[ée]l[éeè]brit|"
    r"people|horoscope|recette|m[ée]t[ée]o|goldman sachs|golden globe|"
    r"or noir dans le sport|promo|publi.?r[ée]dactionnel|sponsoris|"
    # Crypto : un piratage de portefeuille Bitcoin n'est pas un catalyseur
    # du métal. Constaté en test : « Attaque contre les portefeuilles
    # Bitcoin » était retenu au motif du mot « attaque ».
    r"bitcoin|ethereum|crypto|altcoin|nft|blockchain|token|"
    # « Mine d'or », « lingot record » : industrie minière, pas macro.
    r"plus (gros|grand) lingot|mineur|mining|minerai|"
    # Contenu boursier individuel sans portée macro.
    r"r[ée]sultats trimestriels|b[ée]n[ée]fice net|dividende|introduction en bourse|"
    # Faux positifs du mot « or » en français, relevés sur les flux ciblés.
    r"c[oô]te.d.or|livre d.or|[aâ]ge d.or|r[eè]gle d.or|but en or|"
    r"noces d.or|m[ée]daille d.or|palme d.or|ballon d.or)\b",
    re.I)


def _gold_pertinence(titre, source=""):
    """Note une dépêche. 0 = hors sujet, >= 3 = catalyseur retenu."""
    t = (titre or "")
    if not t or _GOLD_EXCLUS.search(t):
        return 0, []
    score = 0
    themes = []
    for nom, (motif, poids) in _GOLD_MOTS.items():
        if re.search(motif, t, re.I):
            score += poids
            themes.append(nom)
    # Une dépêche qui croise plusieurs thèmes est un vrai catalyseur.
    if len(themes) >= 2:
        score += 2
    # La géopolitique SEULE ne suffit pas : « tension », « attaque » ou
    # « sanction » apparaissent dans quantité de dépêches sans portée sur
    # le métal. Elle doit s'accompagner d'un thème de marché.
    if themes == ["geopol"]:
        return 0, themes
    return score, themes


def _gold_urgence(titre, ageSec):
    """Niveau d'alerte d'une dépêche : flash, chaud, ou normal."""
    t = (titre or "").lower()
    if re.search(r"\b(urgent|breaking|alerte|flash|vient de|just in)\b", t):
        return "flash"
    if ageSec is not None and ageSec < 900:
        return "chaud"
    return "normal"


# ═══════════════════════════════════════════════════════════════════════════
#  ROUTES HTTP
# ═══════════════════════════════════════════════════════════════════════════
def _gold_fenetre_chaude():
    """Sommes-nous à moins de 30 min d'un événement rouge (avant ou après) ?

    Sert à accélérer la cadence de rafraîchissement au moment précis où
    le marché bouge, et seulement là.
    """
    try:
        cal, _ = _mkt_last_known("gold_cal")
        if not cal:
            return False, None
        now = datetime.now(timezone.utc)
        for e in (cal.get("events") or []):
            if e.get("priorite") != 1:
                continue
            try:
                d = datetime.fromisoformat(str(e.get("iso")).replace("Z", "+00:00"))
            except Exception:
                continue
            ecart = (d - now).total_seconds()
            if -1800 <= ecart <= 1800:
                return True, e
    except Exception:
        pass
    return False, None


@app.get("/api/gold/macro")
def api_gold_macro():
    """Moteurs de fond, flux institutionnels et biais synthétique."""
    chaud, _ev = _gold_fenetre_chaude()
    ttl = _GOLD_TTL_MACRO_HOT if chaud else _GOLD_TTL_MACRO
    cache = _mkt_cached("gold_macro", ttl)
    if cache is not None:
        return jsonify(cache)

    res = _gold_parallele({
        "xau":   lambda: _gold_cours("xau"),
        "dxy":   lambda: _gold_cours("dxy"),
        "us10y": lambda: _gold_cours("us10y"),
        "wti":   lambda: _gold_cours("wti"),
        "reels": lambda: _gold_fred("DFII10"),
        "bpm":   lambda: _gold_fred("T10YIE"),
        "etf":   _gold_etf,
    }, timeout=14)

    reels = res.get("reels")
    dxy = res.get("dxy")
    wti = res.get("wti")

    payload = {
        "ok": True,
        "updated": datetime.now(timezone.utc).isoformat(),
        "chaud": chaud,
        "prochainTickSec": ttl,
        "cours": {k: res.get(k) for k in ("xau", "dxy", "us10y", "wti")},
        "tauxReels": reels,
        "inflationAnticipee": res.get("bpm"),
        "etf": res.get("etf") or {"estime": True, "fonds": []},
        "banquesCentrales": dict(_GOLD_BC_REF),
        "biais": _gold_biais(reels, dxy, wti),
    }
    # Si absolument tout a échoué, on renvoie la dernière photo connue
    # plutôt qu'un panneau vide : le trader garde un repère, clairement
    # marqué comme daté.
    if not any(payload["cours"].values()) and not reels:
        vieux, age = _mkt_last_known("gold_macro")
        if vieux:
            return jsonify(_mkt_stale(vieux, age))
    _mkt_put("gold_macro", payload)
    return jsonify(payload)


@app.get("/api/gold/news")
def api_gold_news():
    """Dépêches filtrées : uniquement les catalyseurs du métal."""
    lang = "fr" if request.args.get("lang", "fr").lower() == "fr" else "en"
    chaud, _ = _gold_fenetre_chaude()
    ttl = _GOLD_TTL_NEWS_HOT if chaud else _GOLD_TTL_NEWS
    cle = "gold_news_" + lang
    # ?fresh=1 : le trader a cliqué sur « Actualiser ». On ignore le cache
    # et on interroge réellement les sources.
    frais = request.args.get("fresh", "0") in ("1", "true", "yes")
    if not frais:
        cache = _mkt_cached(cle, ttl)
        if cache is not None:
            if lang == "fr":
                try:
                    _news_force_fr_items((cache or {}).get("items") or [], "titre")
                except Exception:
                    pass
            return jsonify(cache)

    # ── Collecte PARALLÈLE sur les flux dédiés au métal ──────────────────
    # Les 30 sources sont interrogées en même temps : le coût total est
    # celui de la plus lente, pas la somme des latences.
    taches = [(lambda u=u, s_=s_, t=t, l=l: _gold_feed(u, s_, t, l))
              for (u, s_, t, l) in _GOLD_FEEDS]
    brut = _mkt_parallel(taches, timeout=11)

    # Complément : les dépêches du Market Hub déjà en cache. Elles
    # apportent des rédactions francophones que nos requêtes ciblées ne
    # couvrent pas toujours, sans coûter une seule requête de plus.
    try:
        v, _a = _mkt_last_known("news_" + lang)
        for it in ((v or {}).get("items") or []):
            brut.append(it)
    except Exception:
        pass

    now = datetime.now(timezone.utc)
    retenues = []
    vus = set()
    for it in brut:
        titre = it.get("title") or ""
        score, themes = _gold_pertinence(titre, it.get("source"))
        if score < 3:
            continue
        empreinte = re.sub(r"\W+", "", titre.lower())[:70]
        if empreinte in vus:
            continue
        vus.add(empreinte)
        age = None
        try:
            d = datetime.fromisoformat(str(it.get("date")).replace("Z", "+00:00"))
            age = max(0, int((now - d).total_seconds()))
        except Exception:
            age = None
        # Une dépêche de plus de 36 h n'est plus un catalyseur. Avant ce
        # garde-fou, la médiane du fil était à 94 h et la plus ancienne à
        # 409 h — mesuré. Le fil doit informer, pas archiver.
        if age is not None and age > _GOLD_AGE_MAX:
            continue
        retenues.append({
            "titre": titre,
            "lien": it.get("link") or "",
            "source": it.get("source") or "",
            "date": it.get("date") or "",
            "ageSec": age,
            "score": score,
            "themes": themes,
            "urgence": _gold_urgence(titre, age),
            "resume": (it.get("resume") or it.get("summary") or it.get("description") or "")[:280],
        })

    # Les plus récentes d'abord ; à fraîcheur égale, les mieux notées.
    retenues.sort(key=lambda x: (x["ageSec"] if x["ageSec"] is not None
                                 else 10 ** 9, -x["score"]))
    if lang == "fr":
        try:
            for it in retenues:
                it["titreEn"] = it.get("titreEn") or it.get("titre") or ""
            _news_force_fr_items(retenues, "titre")
        except Exception:
            pass
    payload = {
        "ok": True, "lang": lang,
        "updated": now.isoformat(),
        "chaud": chaud,
        "prochainTickSec": ttl,
        "count": len(retenues),
        "examines": len(brut),
        "items": retenues[:60],
    }
    _mkt_put(cle, payload)
    return jsonify(payload)


@app.get("/api/gold/calendar")
def api_gold_calendar():
    """Calendrier prioritaire XAU/USD avec matrice d'impact intermarché."""
    lang = "fr" if request.args.get("lang", "fr").lower() == "fr" else "en"
    cache = _mkt_cached("gold_cal", _GOLD_TTL_CAL)
    if cache is not None:
        return jsonify(cache)

    evs = []
    try:
        with app.test_request_context("/api/calendar?lang=" + lang):
            base = api_calendar().get_json()
        evs = (base or {}).get("events") or []
    except Exception:
        evs = []
    if not evs:
        try:
            v, _a = _mkt_last_known("cal_" + lang)
            evs = (v or {}).get("events") or []
        except Exception:
            evs = []

    now = datetime.now(timezone.utc)
    sortie = []
    for e in evs:
        cls = _gold_classer(e.get("title"), e.get("country"))
        if not cls:
            continue
        impact, surprise = _gold_matrice(cls, e.get("actual"), e.get("forecast"))
        iso = e.get("date") or ""
        secondes = None
        try:
            d = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
            secondes = int((d - now).total_seconds())
        except Exception:
            secondes = None
        sortie.append({
            "iso": iso,
            "heure": e.get("time") or "",
            "pays": e.get("country") or "",
            "titre": e.get("title") or "",
            "famille": cls["famille"],
            "familleLibelle": cls["libelle"],
            "priorite": cls["priorite"],
            "importance": e.get("impact") or "",
            "precedent": e.get("previous") or "",
            "prevision": e.get("forecast") or "",
            "actuel": e.get("actual") or "",
            "publie": bool((e.get("actual") or "").strip()),
            "surprise": surprise,
            "impact": impact,
            "dansSec": secondes,
            "fiche": _GOLD_FICHES.get(cls["famille"]),
        })

    # On écarte ce qui est déjà loin derrière : un chiffre publié il y a
    # plus de six heures n'oriente plus la séance en cours et repoussait
    # les publications à venir hors de l'écran (constaté visuellement).
    sortie = [e for e in sortie
              if e["dansSec"] is None or e["dansSec"] > -21600]
    sortie.sort(key=lambda x: (x["dansSec"] if x["dansSec"] is not None
                               else 10 ** 9))

    # Alerte : un dossier rouge dans les 30 minutes qui viennent.
    alerte = None
    for e in sortie:
        s = e.get("dansSec")
        if e["priorite"] == 1 and s is not None and 0 <= s <= 1800:
            alerte = {"titre": e["titre"], "libelle": e["familleLibelle"],
                      "dansSec": s, "dansMin": max(1, s // 60),
                      "heure": e["heure"]}
            break

    payload = {
        "ok": True, "lang": lang,
        "updated": now.isoformat(),
        "count": len(sortie),
        "examines": len(evs),
        "alerte": alerte,
        "events": sortie[:40],
    }
    _mkt_put("gold_cal", payload)
    return jsonify(payload)


# ═══════════════════════════════════════════════════════════════════════════
#  COT INSTITUTIONAL TRACKER — positionnement des institutionnels    (v8.6)
# ---------------------------------------------------------------------------
#  Source : API publique de la CFTC (publicreporting.cftc.gov).
#  Vérifié le 12/08/2026 : réponse en 0,7 s, aucune clé ni inscription.
#
#  DEUX JEUX DE DONNÉES, ET C'EST IMPOSÉ PAR LA CFTC
#  --------------------------------------------------
#  · Disaggregated (72hh-3qpy) : expose les « Money Managers », c'est-à-dire
#    les fonds spéculatifs au sens strict. Disponible sur l'or, l'argent, le
#    pétrole, les agricoles.
#  · Legacy (6dca-aqww) : expose les « Non-Commercial » (grands spéculateurs).
#    C'est le SEUL jeu couvrant les devises.
#
#  Mesuré : EURO FX, BRITISH POUND, AUSTRALIAN DOLLAR et USD INDEX sont
#  ABSENTS du jeu Disaggregated. Réclamer les Money Managers sur l'euro n'a
#  donc pas de sens : la donnée n'existe pas. Chaque actif utilise le
#  meilleur jeu disponible, et l'interface affiche TOUJOURS lequel.
# ═══════════════════════════════════════════════════════════════════════════

_COT_API = "https://publicreporting.cftc.gov/resource/"
_COT_DISAGG = "72hh-3qpy"      # Money Managers  (matières premières)
_COT_LEGACY = "6dca-aqww"      # Non-Commercial  (devises + tout le reste)
_COT_TTL = 6 * 3600            # publication hebdomadaire : 6 h suffisent
_COT_TIMEOUT = 20

# Catalogue des actifs. Le code contrat CFTC est la clé : les intitulés
# changent au fil des années, pas les codes.
_COT_ACTIFS = {
    "XAUUSD": {"code": "088691", "jeu": "disagg", "nom": "Or (COMEX)",
               "sym": "XAU/USD", "inverse": False},
    "XAGUSD": {"code": "084691", "jeu": "disagg", "nom": "Argent (COMEX)",
               "sym": "XAG/USD", "inverse": False},
    "WTI":    {"code": "067651", "jeu": "disagg", "nom": "Pétrole WTI",
               "sym": "WTI", "inverse": False},
    "EURUSD": {"code": "099741", "jeu": "legacy", "nom": "Euro FX",
               "sym": "EUR/USD", "inverse": False},
    "GBPUSD": {"code": "096742", "jeu": "legacy", "nom": "Livre sterling",
               "sym": "GBP/USD", "inverse": False},
    "AUDUSD": {"code": "232741", "jeu": "legacy", "nom": "Dollar australien",
               "sym": "AUD/USD", "inverse": False},
    "USDJPY": {"code": "097741", "jeu": "legacy", "nom": "Yen japonais",
               "sym": "USD/JPY", "inverse": True},
    "USDCAD": {"code": "090741", "jeu": "legacy", "nom": "Dollar canadien",
               "sym": "USD/CAD", "inverse": True},
    "DXY":    {"code": "098662", "jeu": "legacy", "nom": "Indice dollar",
               "sym": "DXY", "inverse": False},
}

# Pondération des trois horizons. Le fond de tendance prime : un fonds ne
# retourne pas une position de 130 000 contrats en une semaine.
_COT_POIDS = {"annuel": 0.50, "mensuel": 0.35, "hebdo": 0.15}


def _cot_http(url, timeout=None):
    req = _urq.Request(url, headers={
        "User-Agent": "OmniTradeHub/8.6",
        "Accept": "application/json",
    })
    with _urq.urlopen(req, timeout=timeout or _COT_TIMEOUT,
                      context=_ssl_ctx()) as r:
        return r.read().decode("utf-8", "replace")


def _cot_nombre(v):
    """Convertit une valeur CFTC en entier. Les champs manquants valent 0."""
    try:
        if v in (None, "", "."):
            return 0
        return int(float(str(v).replace(",", "")))
    except Exception:
        return 0


def _cot_charger(cle, semaines=160):
    """Historique brut d'un actif, du plus ancien au plus récent.

    160 semaines ≈ 3 ans : assez pour le graphique long et pour un COT Index
    calculé sur 52 semaines glissantes.
    """
    a = _COT_ACTIFS.get(cle)
    if not a:
        return None, "actif inconnu"
    res = _COT_DISAGG if a["jeu"] == "disagg" else _COT_LEGACY
    where = _urq.quote("cftc_contract_market_code='%s'" % a["code"])
    order = _urq.quote("report_date_as_yyyy_mm_dd DESC")
    url = ("%s%s.json?$where=%s&$limit=%d&$order=%s"
           % (_COT_API, res, where, semaines, order))
    try:
        brut = json.loads(_cot_http(url))
    except Exception as e:
        return None, str(e)[:80]
    if not brut:
        return None, "aucune donnée"

    lignes = []
    for r in brut:
        if a["jeu"] == "disagg":
            lg = _cot_nombre(r.get("m_money_positions_long_all"))
            ct = _cot_nombre(r.get("m_money_positions_short_all"))
            cl = _cot_nombre(r.get("prod_merc_positions_long"))
            cs = _cot_nombre(r.get("prod_merc_positions_short"))
        else:
            lg = _cot_nombre(r.get("noncomm_positions_long_all"))
            ct = _cot_nombre(r.get("noncomm_positions_short_all"))
            cl = _cot_nombre(r.get("comm_positions_long_all"))
            cs = _cot_nombre(r.get("comm_positions_short_all"))
        lignes.append({
            "date": (r.get("report_date_as_yyyy_mm_dd") or "")[:10],
            "long": lg, "short": ct, "net": lg - ct,
            "commNet": cl - cs,
            "oi": _cot_nombre(r.get("open_interest_all")),
        })
    lignes.reverse()               # du plus ancien au plus récent
    return lignes, None


# ───────────────────────────────────────────────────────────────────────────
#  CALCUL DU BIAIS — trois horizons, puis une synthèse pondérée
# ───────────────────────────────────────────────────────────────────────────
def _cot_index(nets, fenetre=52):
    """COT Index : où se situe la position actuelle dans sa plage historique.

    0 % = plus vendeur qu'il ne l'a jamais été sur la fenêtre.
    100 % = plus acheteur que jamais.
    C'est la mesure de référence des analystes COT, préférée au chiffre brut
    parce qu'un net de +130 000 ne veut rien dire sans son contexte : c'est
    énorme sur l'argent, banal sur l'or.
    """
    ech = nets[-fenetre:] if len(nets) >= 2 else nets
    if not ech:
        return None
    bas, haut = min(ech), max(ech)
    if haut == bas:
        return 50.0
    return round((ech[-1] - bas) / (haut - bas) * 100.0, 1)


def _cot_pente(nets, n=4):
    """Pente moyenne des n dernières semaines, normalisée entre -1 et +1.

    On rapporte la variation à l'amplitude observée sur un an : une hausse
    de 5 000 contrats est décisive sur un marché calme, insignifiante sur un
    marché agité. Sans cette normalisation, le score n'aurait aucun sens
    d'un actif à l'autre.
    """
    if len(nets) < n + 1:
        return 0.0
    variation = nets[-1] - nets[-1 - n]
    ech = nets[-52:] if len(nets) >= 52 else nets
    amplitude = max(ech) - min(ech)
    if amplitude <= 0:
        return 0.0
    return max(-1.0, min(1.0, variation / (amplitude * 0.5)))


def _cot_biais(lignes):
    """Les trois horizons, puis la synthèse pondérée.

    Chaque horizon rend un score borné entre -1 (vendeur) et +1 (acheteur) :
      · annuel  — position du COT Index dans sa plage 52 semaines ;
      · mensuel — pente sur 4 semaines ;
      · hebdo   — variation de la dernière publication.
    """
    nets = [l["net"] for l in lignes]
    if not nets:
        return None

    # ── Annuel : le COT Index, ramené de 0..100 vers -1..+1 ───────────────
    idx = _cot_index(nets, 52)
    s_annuel = 0.0 if idx is None else (idx - 50.0) / 50.0

    # ── Mensuel : pente sur 4 semaines ────────────────────────────────────
    s_mensuel = _cot_pente(nets, 4)

    # ── Hebdomadaire : variation de la dernière semaine ───────────────────
    delta = (nets[-1] - nets[-2]) if len(nets) >= 2 else 0
    ech = nets[-52:] if len(nets) >= 52 else nets
    amp = (max(ech) - min(ech)) or 1
    s_hebdo = max(-1.0, min(1.0, delta / (amp * 0.25)))

    # ── Synthèse pondérée ─────────────────────────────────────────────────
    score = (s_annuel * _COT_POIDS["annuel"]
             + s_mensuel * _COT_POIDS["mensuel"]
             + s_hebdo * _COT_POIDS["hebdo"])
    score = max(-1.0, min(1.0, score))

    # Le score va de -1 à +1 ; la « dominance » est sa force en pourcentage,
    # ramenée sur une échelle 50-100 % : 50 % signifie aucune conviction,
    # 100 % une unanimité des trois horizons.
    dominance = int(round(50 + abs(score) * 50))
    if score > 0.12:
        sens, libelle = "achat", "ACHAT PRIORITAIRE"
    elif score < -0.12:
        sens, libelle = "vente", "VENTE PRIORITAIRE"
    else:
        sens, libelle, dominance = "neutre", "AUCUN BIAIS NET", 50

    def etiquette(s):
        if s > 0.15:
            return {"sens": "achat", "txt": "Acheteur"}
        if s < -0.15:
            return {"sens": "vente", "txt": "Vendeur"}
        return {"sens": "neutre", "txt": "Neutre"}

    h = etiquette(s_hebdo)
    m = etiquette(s_mensuel)
    a = etiquette(s_annuel)

    # ── Divergences : le point le plus utile pour un trader ───────────────
    # Trois horizons alignés = signal solide. Un hebdo qui contredit le fond
    # annonce souvent une correction, pas un retournement.
    sens_list = [x["sens"] for x in (h, m, a) if x["sens"] != "neutre"]
    accord = len(set(sens_list)) <= 1
    if not sens_list:
        conseil = ("Positionnement institutionnel sans direction nette. "
                   "Aucun avantage à tirer du COT cette semaine.")
        alerte = None
    elif accord and len(sens_list) == 3:
        conseil = ("Les trois horizons pointent dans le même sens. "
                   "Privilégiez les setups techniques en %s ; "
                   "ignorez les signaux contraires."
                   % ("achat" if sens == "achat" else "vente"))
        alerte = None
    elif a["sens"] != "neutre" and h["sens"] != "neutre" \
            and a["sens"] != h["sens"]:
        conseil = ("Le fond annuel est %s mais la dernière semaine part en "
                   "sens inverse. Souvent une respiration, rarement un "
                   "retournement : gardez le biais de fond et attendez "
                   "confirmation." % a["txt"].lower())
        alerte = "Divergence hebdomadaire contre la tendance de fond"
    elif m["sens"] != "neutre" and a["sens"] != "neutre" \
            and m["sens"] != a["sens"]:
        conseil = ("Le moyen terme s'oppose au fond annuel : un "
                   "retournement se prépare peut-être. Réduisez la taille "
                   "et exigez une confirmation technique.")
        alerte = "Divergence mensuelle / annuelle"
    else:
        conseil = ("Signal partiel : un seul horizon est engagé. "
                   "Traitez-le comme un appui, pas comme une décision.")
        alerte = None

    return {
        "sens": sens, "libelle": libelle, "dominance": dominance,
        "score": round(score, 3),
        "cotIndex": idx,
        "conseil": conseil, "alerte": alerte,
        "accord": accord,
        "horizons": {
            "hebdo":   {"sens": h["sens"], "txt": h["txt"],
                        "score": round(s_hebdo, 3), "poids": 15,
                        "detail": "Variation de la dernière publication"},
            "mensuel": {"sens": m["sens"], "txt": m["txt"],
                        "score": round(s_mensuel, 3), "poids": 35,
                        "detail": "Pente des 4 dernières semaines"},
            "annuel":  {"sens": a["sens"], "txt": a["txt"],
                        "score": round(s_annuel, 3), "poids": 50,
                        "detail": "Position dans la plage 52 semaines"},
        },
    }



# Cours non-FX pour le scanner COT (or, métaux, énergie, indices, DXY).
# frankfurter / open.er-api = devises seulement. Yahoo en rafale → 429 :
# on réutilise le cache gold/macro, puis on complète au compte-gouttes.
_COT_YAHOO = {
    "XAU": "GC=F", "XAG": "SI=F", "HG": "HG=F", "XPT": "PL=F", "XPD": "PA=F",
    "WTI": "CL=F", "BRT": "BZ=F", "NGS": "NG=F",
    "ES": "ES=F", "NQ": "NQ=F", "YM": "YM=F", "RTY": "RTY=F",
    "BTC": "BTC-USD", "ETH": "ETH-USD",
    "DXY": "DX-Y.NYB", "US10Y": "^TNX",
}
_COT_CNBC = {
    "XAU": "@GC.1", "XAG": "@SI.1", "HG": "@HG.1", "XPT": "@PL.1", "XPD": "@PA.1",
    "WTI": "@CL.1", "BRT": "@BZ.1", "NGS": "@NG.1",
    "DXY": ".DXY", "US10Y": "US10Y",
    "BTC": "BTC.CM=", "ETH": "ETH.CM=",
    "ES": "@ES.1", "NQ": "@NQ.1", "YM": "@YM.1", "RTY": "@RTY.1",
}


def _yahoo_last(sym):
    """Dernier prix Yahoo. None si les deux miroirs échouent."""
    for base in (_GOLD_MIROIRS[_GOLD_MIROIR_OK["i"]],
                 _GOLD_MIROIRS[1 - _GOLD_MIROIR_OK["i"]]):
        try:
            d = json.loads(_gold_http(
                base + "/v8/finance/chart/" + _urq.quote(sym, safe="=^")
                + "?interval=5m&range=1d", 7))
            res = ((d.get("chart") or {}).get("result") or [])
            if not res:
                continue
            meta = res[0].get("meta") or {}
            prix = meta.get("regularMarketPrice")
            if prix is None:
                continue
            try:
                _GOLD_MIROIR_OK["i"] = _GOLD_MIROIRS.index(base)
            except Exception:
                pass
            veille = meta.get("chartPreviousClose") or meta.get("previousClose")
            chg = None
            if veille:
                try:
                    chg = (float(prix) - float(veille)) / float(veille) * 100.0
                except Exception:
                    chg = None
            return {"px": round(float(prix), 4),
                    "chg": round(chg, 3) if chg is not None else None}
        except Exception as e:
            if "429" in str(e):
                return None
            continue
    return None


def _cnbc_last(sym):
    try:
        d = json.loads(_gold_http(
            "https://quote.cnbc.com/quote-html-webservice/restQuote/"
            "symbolType/symbol?symbols=" + _urq.quote(sym)
            + "&requestMethod=itv&noform=1&partnerId=2&fund=1"
            "&exthrs=1&output=json", 7))
        q = (d.get("FormattedQuoteResult") or {}).get("FormattedQuote") or []
        if not q:
            return None
        x = q[0]

        def nb(v):
            if v in (None, "", "UNCH"):
                return None
            t = str(v).replace(",", "").replace("%", "").replace("+", "").strip()
            try:
                return float(t)
            except ValueError:
                return None

        prix = nb(x.get("last"))
        if prix is None:
            return None
        return {"px": round(prix, 4), "chg": nb(x.get("change_pct"))}
    except Exception:
        return None


@app.get("/api/cot/quotes")
def api_cot_quotes():
    """Cours spot/futures pour les paires COT hors devises FX."""
    frais = request.args.get("fresh", "0") in ("1", "true", "yes")
    if not frais:
        hit = _mkt_cached("cot_quotes", 25)
        if hit and (hit.get("n") or 0) >= 4:
            return jsonify(hit)
    rates, chg = {}, {}

    def take(k, v):
        if not v or v.get("px") is None:
            return
        rates[k] = v["px"]
        if v.get("chg") is not None:
            chg[k] = v["chg"]

    # 1) Déjà en cache Gold Event Risk — 0 requête Yahoo.
    try:
        gm, _age = _mkt_last_known("gold_macro")
        cours = (gm or {}).get("cours") or {}
        alias = {"xau": "XAU", "dxy": "DXY", "us10y": "US10Y", "wti": "WTI"}
        for src, dst in alias.items():
            c = cours.get(src) or {}
            if c.get("prix") is not None:
                take(dst, {"px": c["prix"], "chg": c.get("varPct")})
    except Exception:
        pass

    # 2) Les 4 moteurs gold (Yahoo + CNBC déjà robustes).
    gold_map = {"XAU": "xau", "DXY": "dxy", "US10Y": "us10y", "WTI": "wti"}
    for dst, src in gold_map.items():
        if dst in rates:
            continue
        try:
            c = _gold_cours(src)
            if c and c.get("prix") is not None:
                take(dst, {"px": c["prix"], "chg": c.get("varPct")})
        except Exception:
            pass

    # 3) Le reste : une requête à la fois, CNBC si Yahoo s'étouffe.
    manquent = [k for k in _COT_YAHOO if k not in rates]
    yahoo_ok = True
    for k in manquent:
        v = None
        if yahoo_ok:
            v = _yahoo_last(_COT_YAHOO[k])
            if v is None:
                # un 429 probable : on arrête Yahoo pour ce tour
                yahoo_ok = False
        if v is None and k in _COT_CNBC:
            v = _cnbc_last(_COT_CNBC[k])
        take(k, v)

    payload = {
        "ok": True,
        "updated": datetime.now(timezone.utc).isoformat(),
        "rates": rates,
        "chg": chg,
        "n": len(rates),
    }
    if len(rates) >= 3:
        _mkt_put("cot_quotes", payload)
        return jsonify(payload)
    vieux, age = _mkt_last_known("cot_quotes")
    if vieux and (vieux.get("n") or 0) >= 3:
        return jsonify(_mkt_stale(vieux, age))
    return jsonify(payload)


@app.get("/api/cot/actifs")
def api_cot_actifs():
    """Catalogue des actifs suivis, avec la source réellement utilisée."""
    out = []
    for k, a in _COT_ACTIFS.items():
        out.append({
            "cle": k, "nom": a["nom"], "sym": a["sym"],
            "jeu": a["jeu"],
            "acteur": ("Money Managers" if a["jeu"] == "disagg"
                       else "Large Speculators"),
            "note": ("Fonds spéculatifs au sens strict (rapport "
                     "Disaggregated)" if a["jeu"] == "disagg"
                     else "La CFTC ne publie pas les Money Managers sur "
                          "cet actif : rapport Legacy (Non-Commercial)"),
        })
    return jsonify({"ok": True, "actifs": out})


@app.get("/api/cot/data")
def api_cot_data():
    """Positionnement, biais pondéré et historique d'un actif."""
    cle = (request.args.get("actif") or "XAUUSD").upper()
    if cle not in _COT_ACTIFS:
        return jsonify({"ok": False, "erreur": "actif inconnu"}), 400
    frais = request.args.get("fresh", "0") in ("1", "true", "yes")

    ccle = "cot_" + cle
    if not frais:
        hit = _mkt_cached(ccle, _COT_TTL)
        if hit:
            return jsonify(hit)

    lignes, err = _cot_charger(cle)
    if not lignes:
        # Dernière photo connue plutôt qu'un écran vide : le COT est
        # hebdomadaire, une donnée de la veille reste parfaitement valable.
        vieux, age = _mkt_last_known(ccle)
        if vieux:
            return jsonify(_mkt_stale(vieux, age))
        return jsonify({"ok": False, "erreur": err or "indisponible"}), 502

    a = _COT_ACTIFS[cle]

    # ── INVERSION DE LECTURE POUR USD/JPY ET USD/CAD ──────────────────────
    # La CFTC cote le YEN et le DOLLAR CANADIEN, pas la paire telle que le
    # trader la regarde. Un net VENDEUR sur le yen signifie un dollar
    # HAUSSIER, donc USD/JPY haussier. Sans cette inversion, le module
    # annonçait l'exact opposé du bon sens (défaut relevé en test :
    # « ACHAT » affiché sur un net de -42 085).
    if a.get("inverse"):
        lignes = [dict(l, net=-l["net"], commNet=-l["commNet"],
                       long=l["short"], short=l["long"]) for l in lignes]

    biais = _cot_biais(lignes)
    dernier = lignes[-1]
    precedent = lignes[-2] if len(lignes) >= 2 else dernier

    payload = {
        "ok": True,
        "actif": cle, "nom": a["nom"], "sym": a["sym"],
        "acteur": ("Money Managers" if a["jeu"] == "disagg"
                   else "Large Speculators"),
        "jeu": a["jeu"],
        "source": ("CFTC — rapport Disaggregated" if a["jeu"] == "disagg"
                   else "CFTC — rapport Legacy"),
        "inverse": bool(a.get("inverse")),
        "noteInverse": ("La CFTC cote la devise étrangère : les positions "
                        "sont inversées pour correspondre au sens de la "
                        "paire affichée." if a.get("inverse") else None),
        "dateRapport": dernier["date"],
        "updated": datetime.now(timezone.utc).isoformat(),
        "position": {
            "long": dernier["long"], "short": dernier["short"],
            "net": dernier["net"], "commNet": dernier["commNet"],
            "oi": dernier["oi"],
            "deltaNet": dernier["net"] - precedent["net"],
            "deltaLong": dernier["long"] - precedent["long"],
            "deltaShort": dernier["short"] - precedent["short"],
            "pctLong": (round(dernier["long"] * 100.0
                              / max(1, dernier["long"] + dernier["short"]), 1)),
        },
        "biais": biais,
        "historique": [
            {"d": l["date"], "n": l["net"], "c": l["commNet"]}
            for l in lignes[-156:]
        ],
        "semaines": len(lignes),
    }
    _mkt_put(ccle, payload)
    return jsonify(payload)


@app.post("/api/cot/import")
def api_cot_import():
    """Import manuel d'un fichier CFTC, en secours si l'API est bloquée.

    Certains réseaux d'entreprise filtrent les domaines gouvernementaux
    américains. Le trader peut alors télécharger le fichier lui-même et le
    déposer dans l'application.
    """
    data = request.get_json(silent=True) or {}
    contenu = data.get("contenu") or ""
    cle = (data.get("actif") or "XAUUSD").upper()
    if cle not in _COT_ACTIFS:
        return jsonify({"ok": False, "erreur": "actif inconnu"}), 400
    if not contenu.strip():
        return jsonify({"ok": False, "erreur": "fichier vide"}), 400

    a = _COT_ACTIFS[cle]
    lignes = []
    try:
        import csv
        import io
        lect = csv.DictReader(io.StringIO(contenu))
        for r in lect:
            # Les en-têtes CFTC varient selon le format téléchargé : on
            # cherche la colonne par mot-clé plutôt que par nom exact.
            def col(*mots):
                for k in r:
                    kl = (k or "").lower()
                    if all(m in kl for m in mots):
                        return r[k]
                return None
            code = (col("contract", "market", "code") or "").strip()
            if code and code != a["code"]:
                continue
            d = (col("report", "date") or col("as_of_date") or "")[:10]
            lg = _cot_nombre(col("m_money", "long") or col("noncomm", "long"))
            ct = _cot_nombre(col("m_money", "short") or col("noncomm", "short"))
            if not d or (lg == 0 and ct == 0):
                continue
            lignes.append({"date": d, "long": lg, "short": ct,
                           "net": lg - ct, "commNet": 0,
                           "oi": _cot_nombre(col("open", "interest"))})
    except Exception as e:
        return jsonify({"ok": False, "erreur": "lecture impossible : %s"
                        % str(e)[:60]}), 400

    if not lignes:
        return jsonify({"ok": False,
                        "erreur": "aucune ligne exploitable pour %s dans ce "
                                  "fichier" % a["nom"]}), 400
    lignes.sort(key=lambda x: x["date"])
    return jsonify({"ok": True, "lignes": len(lignes),
                    "du": lignes[0]["date"], "au": lignes[-1]["date"]})


# ═══════════════════════════════════════════════════════════════════════════
#  FED & MACRO POLICY TRACKER (v8.10)
#  Worker : cb_intel_backend.py à côté du moteur. Stockage JSON local.
#  Collecte EN ARRIÈRE-PLAN : un premier /snapshot ne doit plus renvoyer
#  un magasin vide (bug mesuré : scores 0.0 + « Lancez le pont » alors
#  que le pont répondait déjà).
# ═══════════════════════════════════════════════════════════════════════════
_CB_LOCK = threading.Lock()
_CB_BG = {"running": False}


def _load_cb():
    try:
        import importlib.util
        bases = []
        try:
            bases.append(os.path.dirname(os.path.abspath(__file__)))
        except Exception:
            pass
        bases.append(os.getcwd())
        try:
            bases.extend(_pubkey_dirs())
        except Exception:
            pass
        seen = set()
        for base in bases:
            if not base or base in seen:
                continue
            seen.add(base)
            path = os.path.join(base, "cb_intel_backend.py")
            if not os.path.isfile(path):
                continue
            spec = importlib.util.spec_from_file_location("cb_intel_backend", path)
            m = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(m)
            try:
                m.DIR = os.path.join(user_data_dir(), "cb_intel")
                m.ITEMS_DIR = os.path.join(m.DIR, "items")
            except Exception:
                pass
            return m
    except Exception:
        return None
    return None


_cb = _load_cb()


def _cb_kick():
    """Démarre une collecte si aucune n'est déjà en cours. Ne bloque pas."""
    if _cb is None:
        return False
    with _CB_LOCK:
        if _CB_BG["running"]:
            return True
        _CB_BG["running"] = True

    def _run():
        try:
            _cb.poll_feeds(limit_per_feed=6, fetch_body=True)
            if hasattr(_cb, "_bootstrap_known_docs"):
                _cb._bootstrap_known_docs()
        except Exception as e:
            log.warning("cb bg : %s", e)
        finally:
            with _CB_LOCK:
                _CB_BG["running"] = False
        # Groq APRÈS la collecte : ne doit plus bloquer le loader RSS.
        def _llm():
            try:
                key = _groq_key_read()
                if not (key and hasattr(_cb, "latest_for_llm")):
                    return
                for doc in _cb.latest_for_llm():
                    try:
                        prev = None
                        if doc.get("kind") == "statement":
                            prev = _cb.last_statement_text(
                                doc.get("bank"), exclude_id=doc.get("id"),
                                before=doc.get("published"))
                        llm, err = _cb.groq_analyze(
                            key, doc.get("bank"), doc.get("kind"),
                            doc.get("title"), doc.get("text"), previous=prev)
                        if llm:
                            _cb.attach_llm(doc["id"], llm)
                        elif err:
                            log.warning("cb llm : %s", err)
                    except Exception as e:
                        log.warning("cb llm : %s", e)
            except Exception as e:
                log.warning("cb llm thread : %s", e)
        threading.Thread(target=_llm, daemon=True, name="cb-llm").start()

    threading.Thread(target=_run, daemon=True, name="cb-poll").start()
    return True


def _cb_watch_loop():
    """Toutes les 3 min : relire les RSS G4. Pas un crawler de tout le web."""
    time.sleep(40)
    while True:
        try:
            _cb_kick()
        except Exception as e:
            log.warning("cb watch : %s", e)
        time.sleep(180)


def _cb_watch_start():
    if getattr(_cb_watch_start, "_on", False):
        return
    _cb_watch_start._on = True
    threading.Thread(target=_cb_watch_loop, daemon=True, name="cb-watch").start()


def _cb_payload():
    if _cb is None:
        return {"ok": False, "error": "module_absent",
                "banks": {}, "index": {"items": []}, "diff": None,
                "collecting": False, "n_items": 0, "poll_errors": [],
                "source": "module cb_intel_backend.py introuvable"}
    snap = _cb.snapshot()
    banks = snap.get("banks") or {}
    items = (snap.get("index") or {}).get("items") or []
    diff = None
    direc = None
    # Diff : dernière paire comparable, Fed d'abord puis BCE / BoE / BoJ.
    for bank in ("FED", "BCE", "BOE", "BOJ"):
        stmts = [it for it in items if it.get("kind") == "statement" and it.get("bank") == bank
                 and (it.get("chars") or 0) >= 350]
        stmts.sort(key=lambda x: x.get("published") or "", reverse=True)
        if not stmts:
            continue
        doc = _cb._load_json(os.path.join(_cb.ITEMS_DIR, stmts[0]["id"] + ".json"), None)
        if not doc:
            continue
        if direc is None:
            direc = doc.get("directive")
        d = doc.get("diff") or {}
        if not (d.get("added") or d.get("removed")) and len(stmts) >= 2:
            older = _cb._load_json(os.path.join(_cb.ITEMS_DIR, stmts[1]["id"] + ".json"), None)
            if older and hasattr(_cb, "word_diff"):
                try:
                    d = _cb.word_diff(older.get("text"), doc.get("text")) or {}
                except Exception:
                    d = {}
        if (d.get("added") or d.get("removed")):
            d = dict(d)
            d["bank"] = bank
            d["title"] = stmts[0].get("title")
            if hasattr(_cb, "title_fr"):
                try:
                    d["title_fr"] = _cb.title_fr(d.get("title") or "")
                except Exception:
                    d["title_fr"] = d.get("title")
            added_fr, removed_fr = [], []
            for x in (d.get("added") or []):
                added_fr.append(_cb.phrase_fr(x) if hasattr(_cb, "phrase_fr") else x)
            for x in (d.get("removed") or []):
                removed_fr.append(_cb.phrase_fr(x) if hasattr(_cb, "phrase_fr") else x)
            d["added_fr"] = added_fr
            d["removed_fr"] = removed_fr
            blob = " ".join((d.get("added") or []) + (d.get("removed") or [])).lower()
            clair = []
            if "voting against" in blob or "logan" in blob or "1/4" in blob:
                clair.append(
                    "En clair : la majorite a MAINTENu les taux. "
                    "Des membres (Logan, vote contre de Beth M.) voulaient +0,25 point. "
                    "Fed divisee, un peu plus hawkish qu'a l'unanimite — ce n'est PAS une hausse."
                )
            if "ample reserves" in blob:
                clair.append(
                    "Changement de formulation sur les reserves bancaires "
                    "(« continues » vs « reaffirmees »). Technique, pas un changement de taux."
                )
            if not clair:
                clair.append(
                    "En clair : le communique a change sur les phrases traduites ci-dessous. "
                    "Rouge = nouveau. Bleu = retire."
                )
            d["en_clair"] = " ".join(clair)
            d["vs"] = stmts[1]["id"] if len(stmts) >= 2 else doc.get("diff_vs")
            diff = d
            if bank == "FED":
                break
    poll = {}
    try:
        if hasattr(_cb, "poll_state"):
            poll = _cb.poll_state() or {}
    except Exception:
        poll = {}
    rss_on = bool(_CB_BG.get("running") or poll.get("running"))
    # Premier lancement seulement : ne pas relancer en boucle si le poll
    # a déjà fini (même à vide) — ça laissait l'UI sur « collecte… ».
    already = bool(poll.get("finished"))
    if not items and not rss_on and not already:
        _cb_kick()
        rss_on = True
    collecting = rss_on
    errs = (poll.get("errors") or [])[:6]
    ok_n = int(poll.get("feeds_ok") or 0)
    fail_n = int(poll.get("feeds_fail") or 0)
    if items:
        source = "RSS officiels (Fed/BCE/BoE/BoJ)"
    elif collecting:
        source = "collecte des flux officiels…"
    elif fail_n and not ok_n:
        source = "flux officiels injoignables depuis cet ordinateur"
    elif poll.get("finished"):
        source = "aucun document retenu (filtre ou flux vide)"
    else:
        source = "magasin local"
    return {
        "ok": True,
        "banks": banks,
        "index": snap.get("index") or {"items": []},
        "diff": diff,
        "latest_directive": direc,
        "updated": datetime.now(timezone.utc).isoformat(),
        "source": source,
        "collecting": collecting,
        "n_items": len(items),
        "poll_errors": errs,
        "feeds_ok": ok_n,
        "feeds_fail": fail_n,
        "groq": bool(_groq_key_read()),
        "excerpts": snap.get("excerpts") or {},
    }


@app.get("/api/cb/snapshot")
def api_cb_snapshot():
    return jsonify(_cb_with_llm(_cb_payload()))


@app.get("/api/cb/refresh")
def api_cb_refresh():
    if _cb is None:
        return jsonify(_cb_with_llm(_cb_payload()))
    _cb_kick()
    # Relire les communiqués trop courts (BCE « texte non lu »).
    try:
        if hasattr(_cb, "_bootstrap_known_docs"):
            _cb._bootstrap_known_docs()
            if hasattr(_cb, "refresh_statement_diffs"):
                _cb.refresh_statement_diffs()
    except Exception as e:
        log.warning("cb bootstrap refresh : %s", e)
    return jsonify(_cb_with_llm(_cb_payload()))


def _groq_key_path():
    return os.path.join(user_data_dir(), "groq.key")


def _or_key_path():
    return os.path.join(user_data_dir(), "openrouter.key")


def _gemini_key_path():
    return os.path.join(user_data_dir(), "gemini.key")


def _cerebras_key_path():
    return os.path.join(user_data_dir(), "cerebras.key")


def _mistral_key_path():
    return os.path.join(user_data_dir(), "mistral.key")


def _nvidia_key_path():
    return os.path.join(user_data_dir(), "nvidia.key")


def _key_read(path, prefix):
    try:
        v = open(path, encoding="utf-8").read().strip()
        return v if v.startswith(prefix) else ""
    except Exception:
        return ""


def _key_write(path, k, prefix):
    k = (k or "").strip()
    try:
        os.makedirs(user_data_dir(), exist_ok=True)
        if not k:
            try:
                os.remove(path)
            except Exception:
                pass
            return True
        if prefix and not k.startswith(prefix):
            return False
        with open(path, "w", encoding="utf-8") as f:
            f.write(k)
        try:
            os.chmod(path, 0o600)
        except Exception:
            pass
        return True
    except Exception:
        return False


_IA_BOOT_DONE = False


def _ia_sidecar_dirs():
    out = []
    try:
        out.append(user_data_dir())
    except Exception:
        pass
    try:
        here = os.path.dirname(os.path.abspath(__file__))
        out.append(here)
    except Exception:
        pass
    if getattr(sys, "frozen", False):
        ex = os.path.dirname(sys.executable)
        out.append(ex)
        out.append(os.path.normpath(os.path.join(ex, "..", "Resources")))
    # dossier de lancement (ZIP décompressé)
    try:
        out.append(os.getcwd())
    except Exception:
        pass
    seen, uniq = set(), []
    for d in out:
        d = os.path.abspath(d)
        if d not in seen and os.path.isdir(d):
            seen.add(d)
            uniq.append(d)
    return uniq


def _ia_keys_bootstrap():
    """Copie Groq + OpenRouter vers le dossier config si on les trouve à côté du moteur."""
    global _IA_BOOT_DONE
    if _IA_BOOT_DONE:
        return
    _IA_BOOT_DONE = True
    have_g = _key_read(_groq_key_path(), "gsk_")
    have_o = _key_read(_or_key_path(), "sk-or-")
    have_m = _key_read(_gemini_key_path(), "AIza")
    have_c = _cerebras_key_file()
    if have_g and have_o and have_m and have_c:
        return
    for d in _ia_sidecar_dirs():
        if not have_g:
            try:
                v = open(os.path.join(d, "groq.key"), encoding="utf-8").read().strip()
            except Exception:
                v = ""
            if v.startswith("gsk_"):
                _key_write(_groq_key_path(), v, "gsk_")
                have_g = v
        if not have_o:
            try:
                v = open(os.path.join(d, "openrouter.key"), encoding="utf-8").read().strip()
            except Exception:
                v = ""
            if v.startswith("sk-or-"):
                _key_write(_or_key_path(), v, "sk-or-")
                have_o = v
        if not have_m:
            try:
                v = open(os.path.join(d, "gemini.key"), encoding="utf-8").read().strip()
            except Exception:
                v = ""
            if v.startswith("AIza"):
                _key_write(_gemini_key_path(), v, "AIza")
                have_m = v
        if not have_c:
            try:
                v = open(os.path.join(d, "cerebras.key"), encoding="utf-8").read().strip()
            except Exception:
                v = ""
            if _cerebras_ok(v):
                _cerebras_key_write(v)
                have_c = v
        try:
            v = open(os.path.join(d, "mistral.key"), encoding="utf-8").read().strip()
            if _mistral_ok(v):
                _mistral_key_write(v)
        except Exception:
            pass
        try:
            v = open(os.path.join(d, "nvidia.key"), encoding="utf-8").read().strip()
            if _nvidia_ok(v):
                _nvidia_key_write(v)
        except Exception:
            pass
        if have_g and have_o and have_m and have_c:
            return
        try:
            for fn in os.listdir(d):
                if not fn.endswith(".html"):
                    continue
                try:
                    txt = open(os.path.join(d, fn), encoding="utf-8", errors="ignore").read()
                except Exception:
                    continue
                if not have_g:
                    m = re.search(r"GROQ_KEY_SEED='(gsk_[A-Za-z0-9]+)'", txt)
                    if m:
                        _key_write(_groq_key_path(), m.group(1), "gsk_")
                        have_g = m.group(1)
                if not have_o:
                    m = re.search(r"OR_KEY_SEED='(sk-or-v1-[A-Za-z0-9]+)'", txt)
                    if m:
                        _key_write(_or_key_path(), m.group(1), "sk-or-")
                        have_o = m.group(1)
        except Exception:
            pass


def _groq_key_read():
    v = _key_read(_groq_key_path(), "gsk_")
    if not v:
        _ia_keys_bootstrap()
        v = _key_read(_groq_key_path(), "gsk_")
    return v


def _groq_key_write(k):
    return _key_write(_groq_key_path(), k, "gsk_")


def _or_key_read():
    v = _key_read(_or_key_path(), "sk-or-")
    if not v:
        _ia_keys_bootstrap()
        v = _key_read(_or_key_path(), "sk-or-")
    return v


def _or_key_write(k):
    return _key_write(_or_key_path(), k, "sk-or-")


def _cerebras_ok(k):
    k = (k or "").strip()
    if not k or k.startswith("gsk_") or k.startswith("sk-or-") or k.startswith("AIza"):
        return False
    return k.lower().startswith("csk") or len(k) >= 20


def _mistral_ok(k):
    k = (k or "").strip()
    if not k or len(k) < 16:
        return False
    if k.startswith(("gsk_", "sk-or-", "AIza", "AQ.", "nvapi-", "csk")):
        return False
    return True


def _nvidia_ok(k):
    k = (k or "").strip()
    return k.startswith("nvapi-")


def _cerebras_key_file():
    try:
        v = open(_cerebras_key_path(), encoding="utf-8").read().strip()
        return v if _cerebras_ok(v) else ""
    except Exception:
        return ""


def _gemini_key_read():
    v = _key_read(_gemini_key_path(), "AIza")
    if not v:
        try:
            v2 = open(_gemini_key_path(), encoding="utf-8").read().strip()
        except Exception:
            v2 = ""
        if v2 and not v2.startswith("gsk_") and not v2.startswith("sk-or-") and len(v2) >= 20:
            v = v2
    if not v:
        _ia_keys_bootstrap()
        v = _key_read(_gemini_key_path(), "AIza")
        if not v:
            try:
                v2 = open(_gemini_key_path(), encoding="utf-8").read().strip()
            except Exception:
                v2 = ""
            if v2 and not v2.startswith("gsk_") and not v2.startswith("sk-or-") and len(v2) >= 20:
                v = v2
    return v


def _gemini_key_write(k):
    return _key_write(_gemini_key_path(), k, "AIza")


def _cerebras_key_read():
    v = _cerebras_key_file()
    if not v:
        _ia_keys_bootstrap()
        v = _cerebras_key_file()
    return v


def _cerebras_key_write(k):
    k = (k or "").strip()
    if not k:
        try:
            os.remove(_cerebras_key_path())
        except Exception:
            pass
        return True
    if not _cerebras_ok(k):
        return False
    try:
        os.makedirs(user_data_dir(), exist_ok=True)
        with open(_cerebras_key_path(), "w", encoding="utf-8") as f:
            f.write(k)
        try:
            os.chmod(_cerebras_key_path(), 0o600)
        except Exception:
            pass
        return True
    except Exception:
        return False


def _plain_key_read(path, okfn):
    try:
        v = open(path, encoding="utf-8").read().strip()
        return v if okfn(v) else ""
    except Exception:
        return ""


def _plain_key_write(path, k, okfn):
    k = (k or "").strip()
    try:
        if not k:
            try:
                os.remove(path)
            except Exception:
                pass
            return True
        if not okfn(k):
            return False
        os.makedirs(user_data_dir(), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(k)
        try:
            os.chmod(path, 0o600)
        except Exception:
            pass
        return True
    except Exception:
        return False


def _mistral_key_read():
    v = _plain_key_read(_mistral_key_path(), _mistral_ok)
    if not v:
        _ia_keys_bootstrap()
        v = _plain_key_read(_mistral_key_path(), _mistral_ok)
    return v


def _mistral_key_write(k):
    return _plain_key_write(_mistral_key_path(), k, _mistral_ok)


def _nvidia_key_read():
    v = _plain_key_read(_nvidia_key_path(), _nvidia_ok)
    if not v:
        _ia_keys_bootstrap()
        v = _plain_key_read(_nvidia_key_path(), _nvidia_ok)
    return v


def _nvidia_key_write(k):
    return _plain_key_write(_nvidia_key_path(), k, _nvidia_ok)


def _cb_with_llm(payload):
    """Attache l'analyse IA du dernier communiqué Fed, si déjà calculée."""
    if _cb is None:
        return payload
    items = ((payload.get("index") or {}).get("items") or [])
    stmts = [it for it in items if it.get("kind") == "statement" and it.get("bank") == "FED"]
    stmts.sort(key=lambda x: x.get("published") or "", reverse=True)
    if not stmts:
        return payload
    doc = _cb._load_json(os.path.join(_cb.ITEMS_DIR, stmts[0]["id"] + ".json"), None)
    if doc and doc.get("llm"):
        payload["latest_llm"] = doc["llm"]
        payload["latest_llm_id"] = stmts[0]["id"]
    return payload


@app.get("/api/cb/groq")
def api_cb_groq_get():
    k = _groq_key_read()
    o = _or_key_read()
    g = _gemini_key_read()
    c = _cerebras_key_read()
    mi = _mistral_key_read()
    nv = _nvidia_key_read()
    return jsonify({
        "ok": True,
        "configured": bool(k),
        "openrouter": bool(o),
        "gemini": bool(g),
        "cerebras": bool(c),
        "mistral": bool(mi),
        "nvidia": bool(nv),
        "tail": ("…" + k[-4:]) if k else "",
        "or_tail": ("…" + o[-4:]) if o else "",
        "gem_tail": ("…" + g[-4:]) if g else "",
        "cer_tail": ("…" + c[-4:]) if c else "",
    })


@app.post("/api/cb/groq")
def api_cb_groq_set():
    data = request.get_json(silent=True) or {}
    if "key" in data:
        k = (data.get("key") or "").strip()
        if k and not k.startswith("gsk_"):
            return jsonify({"ok": False, "error": "clé Groq invalide"}), 400
        _groq_key_write(k)
    if "openrouter" in data:
        o = (data.get("openrouter") or "").strip()
        if o and not o.startswith("sk-or-"):
            return jsonify({"ok": False, "error": "clé OpenRouter invalide"}), 400
        _or_key_write(o)
    if "gemini" in data:
        g = (data.get("gemini") or "").strip()
        if g and not (g.startswith("AIza") or g.startswith("AQ.") or (len(g) >= 24 and not g.startswith("gsk_") and not g.startswith("sk-or-"))):
            return jsonify({"ok": False, "error": "cle Gemini invalide"}), 400
        if g and g.startswith("AIza"):
            _gemini_key_write(g)
        elif g:
            try:
                os.makedirs(user_data_dir(), exist_ok=True)
                open(_gemini_key_path(), "w", encoding="utf-8").write(g)
                os.chmod(_gemini_key_path(), 0o600)
            except Exception:
                return jsonify({"ok": False, "error": "gemini non ecrit"}), 500
        else:
            _gemini_key_write(g)
    if "cerebras" in data:
        c = (data.get("cerebras") or "").strip()
        if c and not _cerebras_ok(c):
            return jsonify({"ok": False, "error": "cle Cerebras invalide"}), 400
        _cerebras_key_write(c)
    if "mistral" in data:
        m = (data.get("mistral") or "").strip()
        if m and not _mistral_ok(m):
            return jsonify({"ok": False, "error": "cle Mistral invalide"}), 400
        _mistral_key_write(m)
    if "nvidia" in data:
        n = (data.get("nvidia") or "").strip()
        if n and not _nvidia_ok(n):
            return jsonify({"ok": False, "error": "cle NVIDIA invalide (nvapi-)"}), 400
        _nvidia_key_write(n)
    return jsonify({"ok": True, "configured": bool(_groq_key_read()),
                    "openrouter": bool(_or_key_read()),
                    "gemini": bool(_gemini_key_read()),
                    "cerebras": bool(_cerebras_key_read()),
                    "mistral": bool(_mistral_key_read()),
                    "nvidia": bool(_nvidia_key_read())})


_LLM_PROVIDERS = {
    "groq": (getattr(_cb, "GROQ_URL", None)
             or "https://api.groq.com/openai/v1/chat/completions", _groq_key_read),
    "openrouter": (getattr(_cb, "OR_URL", None)
                   or "https://openrouter.ai/api/v1/chat/completions", _or_key_read),
}


@app.post("/api/llm/completions")
def api_llm_completions():
    """Proxie un appel OpenAI-compatible (Groq / OpenRouter) sans jamais
    exposer la clé au navigateur : le client n'envoie que provider, model et
    messages ; la clé demeure sur la machine (groq.key / openrouter.key)."""
    import urllib.error
    data = request.get_json(silent=True) or {}
    prov = (data.get("provider") or "groq").lower().strip()
    if prov not in _LLM_PROVIDERS:
        return jsonify({"ok": False, "error": "fournisseur_inconnu"}), 400
    if not (data.get("messages") or []):
        return jsonify({"ok": False, "error": "messages_absents"}), 400
    key = _LLM_PROVIDERS[prov][1]()
    if not key:
        return jsonify({"error": {
            "message": "Aucune clé %s configurée sur cet ordinateur." % prov}}), 507
    url = _LLM_PROVIDERS[prov][0]
    body = {}
    for _f in ("model", "messages", "temperature", "max_tokens", "top_p",
               "stop", "reasoning_effort", "reasoning_format"):
        if data.get(_f) is not None:
            body[_f] = data[_f]
    body.setdefault("model", "qwen/qwen3.6-27b")
    payload = json.dumps(body).encode("utf-8")
    req = _urq.Request(
        url,
        data=payload,
        method="POST",
        headers={
            "Authorization": "Bearer " + key,
            "Content-Type": "application/json",
            "User-Agent": "OmniTradeHub/1.0",
            "Accept": "application/json",
        },
    )
    raw = None
    status = 200
    err_payload = None
    for insecure in (False, True):
        try:
            ctx = None
            if _cb is not None:
                ctx = _cb._ssl(insecure)
            else:
                import ssl as _sslmod
                ctx = _sslmod.create_default_context()
                if insecure:
                    ctx.check_hostname = False
                    ctx.verify_mode = _sslmod.CERT_NONE
            with _urq.urlopen(req, timeout=30, context=ctx) as r:
                raw = r.read().decode("utf-8", "replace")
                status = getattr(r, "status", 200)
            break
        except urllib.error.HTTPError as e:
            try:
                err_payload = json.loads(e.read().decode("utf-8", "replace"))
            except Exception:
                err_payload = None
            status = e.code
            break
        except Exception as e1:
            if "CERTIFICATE" in str(e1).upper() or "SSL" in str(e1).upper():
                continue
            status = 502
            err_payload = {"error": {"message": (str(e1) or "erreur")[:240]}}
            break
    if raw is not None or err_payload is not None:
        if raw is not None:
            try:
                return jsonify(json.loads(raw))
            except Exception:
                return (raw or ""), status
        resp = jsonify(err_payload)
        resp.status_code = status
        return resp
    return jsonify({"error": {"message": "fournisseur_indisponible"}}), 502


@app.post("/api/cb/score")
def api_cb_score():
    if _cb is None:
        return jsonify({"ok": False, "error": "module_absent"}), 503
    data = request.get_json(silent=True) or {}
    text = data.get("text") or ""
    bank = (data.get("bank") or "FED").upper()
    kind = data.get("kind") or "statement"
    if not text.strip():
        return jsonify({"ok": False, "error": "vide"}), 400
    scored = _cb.score_text(text, kind)
    direc = _cb.directive(bank, scored["score"], scored["label"], kind)
    llm = None
    key = (data.get("key") or "").strip() or _groq_key_read()
    if key and hasattr(_cb, "groq_analyze") and len(text) >= 80:
        llm, _err = _cb.groq_analyze(key, bank, kind, data.get("title") or "Collage", text, or_key=_or_key_read())
        if llm:
            direc["plain_fr"] = llm.get("plain_fr") or direc.get("plain_fr")
            if llm.get("assets"):
                direc["assets"] = llm["assets"]
    return jsonify({"ok": True, "score": scored["score"], "label": scored["label"],
                    "action": scored.get("action"), "directive": direc, "llm": llm})


@app.post("/api/cb/web")
def api_cb_web():
    """Extrait web (compound-mini). Interdit au score hawk/dove."""
    if _cb is None or not hasattr(_cb, "web_brief"):
        return jsonify({"ok": False, "error": "module_absent"}), 503
    data = request.get_json(silent=True) or {}
    key = (data.get("key") or "").strip() or _groq_key_read()
    if not key:
        return jsonify({"ok": False, "error": "clé absente"}), 400
    q = str(data.get("q") or data.get("question") or "").strip()
    if not q:
        return jsonify({"ok": False, "error": "question vide"}), 400
    out, err = _cb.web_brief(key, q)
    if err:
        return jsonify({"ok": False, "error": err}), 502
    content = (out or {}).get("content") or ""
    import re as _re
    content = _re.sub(r"(?is)<think>.*?</think>", " ", content)
    content = _re.sub(r"(?is)<think>.*", " ", content).strip()
    if len(content) < 40:
        return jsonify({"ok": False, "error": "extrait web trop court"}), 502
    return jsonify({"ok": True, "content": content, "model": (out or {}).get("model"),
                    "provider": "web"})


@app.post("/api/cb/chat")
def api_cb_chat():
    """Chat coach : texte libre, pas de JSON hawk/dove."""
    if _cb is None or not hasattr(_cb, "groq_chat"):
        return jsonify({"ok": False, "error": "module_absent"}), 503
    data = request.get_json(silent=True) or {}
    key = (data.get("key") or "").strip() or _groq_key_read()
    or_key = (data.get("openrouter") or "").strip() or _or_key_read()
    gem_key = (data.get("gemini") or "").strip() or _gemini_key_read()
    cer_key = (data.get("cerebras") or "").strip() or _cerebras_key_read()
    mi_key = (data.get("mistral") or "").strip() or _mistral_key_read()
    nv_key = (data.get("nvidia") or "").strip() or _nvidia_key_read()
    if not key and not or_key and not gem_key and not cer_key and not mi_key and not nv_key:
        return jsonify({"ok": False, "error": "clé absente"}), 400
    messages = data.get("messages") or []
    if not isinstance(messages, list) or not messages:
        return jsonify({"ok": False, "error": "messages vides"}), 400
    clean = []
    for m in messages[:16]:
        if not isinstance(m, dict):
            continue
        role = m.get("role") if m.get("role") in ("system", "user", "assistant") else "user"
        content = str(m.get("content") or "")[:4000]
        if content:
            clean.append({"role": role, "content": content})
    if not clean:
        return jsonify({"ok": False, "error": "messages vides"}), 400
    try:
        mt = int(data.get("max_tokens") or 900)
    except Exception:
        mt = 700
    if hasattr(_cb, "llm_chat"):
        out, err = _cb.llm_chat(
            key, clean, max_tokens=max(200, min(mt, 1600)), disable_thinking=True,
            or_key=or_key, gemini_key=gem_key, cerebras_key=cer_key,
            mistral_key=mi_key, nvidia_key=nv_key,
        )
    else:
        out, err = _cb.groq_chat(key, clean, max_tokens=max(200, min(mt, 1600)), disable_thinking=True)
    if err:
        return jsonify({"ok": False, "error": err}), 502
    content = (out or {}).get("content") or ""
    import re as _re
    content = _re.sub(r"(?is)<think>.*?</think>", " ", content)
    content = _re.sub(r"(?is)<think>.*", " ", content).strip()
    content = _re.sub(r"^```(?:\w+)?\s*", "", content)
    content = _re.sub(r"\s*```$", "", content).strip()
    # Un mot isolé ou «...ban» n'est pas une réponse.
    words = [w for w in _re.split(r"\s+", content) if w]
    if (not content) or len(content) < 40 or len(words) < 8:
        return jsonify({"ok": False, "error": "réponse trop courte (modèle encore en train de penser)"}), 502
    return jsonify({"ok": True, "content": content, "model": (out or {}).get("model")})


@app.post("/api/cb/analyze")
def api_cb_analyze():
    if _cb is None or not hasattr(_cb, "groq_analyze"):
        return jsonify({"ok": False, "error": "module_absent"}), 503
    data = request.get_json(silent=True) or {}
    key = (data.get("key") or "").strip() or _groq_key_read()
    or_key = (data.get("openrouter") or "").strip() or _or_key_read()
    gem_key = (data.get("gemini") or "").strip() or _gemini_key_read()
    cer_key = (data.get("cerebras") or "").strip() or _cerebras_key_read()
    if not key and not or_key and not gem_key and not cer_key:
        return jsonify({"ok": False, "error": "clé absente"}), 400
    item_id = data.get("id") or ""
    text = data.get("text") or ""
    bank = (data.get("bank") or "FED").upper()
    kind = data.get("kind") or "speech"
    title = data.get("title") or ""
    if item_id:
        doc = _cb._load_json(os.path.join(_cb.ITEMS_DIR, item_id + ".json"), None)
        if not doc:
            return jsonify({"ok": False, "error": "document inconnu"}), 404
        if doc.get("llm") and not data.get("force"):
            return jsonify({"ok": True, "cached": True, "id": item_id, "llm": doc["llm"]})
        text = doc.get("text") or text
        bank = doc.get("bank") or bank
        kind = doc.get("kind") or kind
        title = doc.get("title") or title
        prev = None
        if kind == "statement" and hasattr(_cb, "last_statement_text"):
            prev = _cb.last_statement_text(bank, exclude_id=item_id, before=doc.get("published"))
        llm, err = _cb.groq_analyze(key, bank, kind, title, text, previous=prev, or_key=or_key, gemini_key=gem_key, cerebras_key=cer_key)
        if err:
            return jsonify({"ok": False, "error": err}), 502
        _cb.attach_llm(item_id, llm)
        return jsonify({"ok": True, "cached": False, "id": item_id, "llm": llm})
    if len(text) < 80:
        return jsonify({"ok": False, "error": "texte trop court"}), 400
    llm, err = _cb.groq_analyze(key, bank, kind, title or "Collage", text, or_key=or_key)
    if err:
        return jsonify({"ok": False, "error": err}), 502
    return jsonify({"ok": True, "cached": False, "llm": llm})


@app.get("/api/watch")
def api_watch():
    lang = "fr" if request.args.get("lang", "fr").lower() == "fr" else "en"
    now = datetime.now(timezone.utc)
    out = {"ok": True, "updated": now.isoformat(), "lang": lang,
           "released": [], "upcoming": [], "headlines": [], "sentiment": []}

    # 1. Calendrier : ce qui vient de tomber et ce qui arrive.
    try:
        cal = _mkt_cached("cal_" + lang, 8 if _cal_hot() else 20)
        if not cal:
            with app.test_request_context("/api/calendar?lang=" + lang):
                cal = api_calendar().get_json()
        for e in (cal or {}).get("events", []):
            try:
                d = datetime.fromisoformat(e.get("date", ""))
            except Exception:
                continue
            if d.tzinfo is None:
                d = d.replace(tzinfo=timezone.utc)
            mins = (d - now).total_seconds() / 60.0
            if e.get("impact") not in ("High", "Medium"):
                continue
            # Publié dans les 16 dernières heures, avec une valeur réelle.
            # Fenêtre volontairement large : un trader qui ouvre son journal
            # le matin doit voir ce qui est tombé pendant la nuit (l'IPC
            # australien de 1 h 30 en est l'exemple type).
            if -960 <= mins <= 0 and e.get("actual"):
                out["released"].append(e)
            # Attendu dans les 8 prochaines heures.
            elif 0 < mins <= 480 and e.get("impact") == "High":
                out["upcoming"].append(e)
        out["released"] = out["released"][-12:]
        out["upcoming"] = out["upcoming"][:8]
    except Exception:
        pass

    # 2. Dépêches les plus fraîches (l'agent y puise ses briefs).
    try:
        nw = _mkt_cached("news_" + lang, 180)
        if not nw:
            with app.test_request_context("/api/news?lang=" + lang):
                nw = api_news().get_json()
        out["headlines"] = (nw or {}).get("items", [])[:25]
    except Exception:
        pass

    # 3. Sentiment : positionnement extrême = risque de retournement.
    try:
        sn = _mkt_cached("sent", 300)
        if not sn:
            with app.test_request_context("/api/sentiment"):
                sn = api_sentiment().get_json()
        for x in (sn or {}).get("symbols", []):
            lp = x.get("longPct")
            # Seuil à 68/32 : mesuré sur la source, les valeurs dépassent
            # rarement 75 %, un seuil trop strict ne remontait jamais rien.
            if isinstance(lp, int) and (lp >= 68 or lp <= 32):
                out["sentiment"].append(x)
        out["sentiment"] = out["sentiment"][:12]
    except Exception:
        pass

    out["counts"] = {k: len(out[k]) for k in
                     ("released", "upcoming", "headlines", "sentiment")}
    return jsonify(out)


@app.get("/api/symbols")
def api_symbols():
    """Symboles visibles. En mode fichier : déduits des trades/positions."""
    if NATIVE_API and mt5 is not None:
        with _MT5_LOCK:
            mt5_connect()
            syms = mt5.symbols_get() or []
        return jsonify({"ok": True, "mode": "native",
                        "symbols": [s.name for s in syms if s.visible][:500]})
    seen = []
    for row in (file_get_trades() + file_get_positions()):
        s = row.get("symbol")
        if s and s not in seen:
            seen.append(s)
    return jsonify({"ok": True, "mode": "file", "symbols": seen[:500]})


@app.errorhandler(500)
def _err500(e):  # pragma: no cover
    return jsonify({"ok": False, "error": "server_error", "message": str(e)}), 500


# ═════════════════════════════════════════════════════════════════════════════
#  SERVEUR WEBSOCKET (push temps réel)
# ═════════════════════════════════════════════════════════════════════════════
async def _ws_handler(ws):
    # Auth : ?token=... dans l'URL, ou premier message {"token": "..."}
    path = getattr(ws, "path", "") or ""
    token = None
    if "token=" in path:
        token = path.split("token=", 1)[1].split("&", 1)[0]

    if CFG["token"] and token != CFG["token"]:
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=5)
            token = (json.loads(raw) or {}).get("token")
        except Exception:
            token = None
        if token != CFG["token"]:
            await ws.send(json.dumps({"ok": False, "error": "unauthorized"}))
            await ws.close()
            return

    log.info("WS client connecté")
    await ws.send(json.dumps({"type": "hello", "ok": True, "version": VERSION}))

    async def pump():
        while True:
            payload = await asyncio.to_thread(cached_payload, None, None, 1.0)
            payload["type"] = "sync"
            await ws.send(json.dumps(payload, default=str))
            await asyncio.sleep(CFG["push_interval"])

    async def listen():
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            if msg.get("action") in ("sync", "refresh"):
                payload = await asyncio.to_thread(build_payload)
                payload["type"] = "sync"
                await ws.send(json.dumps(payload, default=str))
            elif msg.get("action") == "ping":
                await ws.send(json.dumps({"type": "pong", "ok": True}))

    try:
        await asyncio.gather(pump(), listen())
    except Exception as e:
        log.info("WS client déconnecté (%s)", type(e).__name__)


def run_ws_server():  # pragma: no cover
    if websockets is None:
        log.error("Module 'websockets' absent -> pip install websockets")
        return

    async def _main():
        async with websockets.serve(_ws_handler, CFG["host"], CFG["ws_port"],
                                    ping_interval=20, max_size=8 * 1024 * 1024):
            log.info("WebSocket  →  ws://%s:%s", CFG["host"], CFG["ws_port"])
            await asyncio.Future()

    asyncio.run(_main())


# ═════════════════════════════════════════════════════════════════════════════
#  EXPERT ADVISOR MQL5 (mode fichier — macOS / Linux)
# ═════════════════════════════════════════════════════════════════════════════
EA_SOURCE = r"""//+------------------------------------------------------------------+
//|  OmniTradeExport.mq5                                            |
//|  Exporte compte / historique / positions en JSON pour OmniTrade Hub |
//|  Compatible macOS, Windows et Linux (aucune DLL requise).        |
//|                                                                  |
//|  Installation :                                                  |
//|    1. MT5 -> Outils -> MetaQuotes Language Editor                |
//|    2. Fichier -> Nouveau -> Expert Advisor, coller ce code       |
//|    3. Compiler (F7), puis glisser l'EA sur un graphique          |
//|    4. Cocher « Autoriser le trading algorithmique »              |
//+------------------------------------------------------------------+
#property copyright "OmniTrade Hub"
#property version   "1.00"
#property strict

input int  RefreshSeconds = 10;    // Fréquence d'écriture (secondes)
input int  HistoryDays    = 365;   // Profondeur d'historique

string Esc(string s){ StringReplace(s,"\\","\\\\"); StringReplace(s,"\"","\\\""); return s; }
string TS(datetime t){ return TimeToString(t, TIME_DATE|TIME_SECONDS); }

void WriteFile(string name, string content){
   int h = FileOpen(name, FILE_WRITE|FILE_TXT|FILE_ANSI);
   if(h == INVALID_HANDLE){ Print("OmniTrade Hub: écriture impossible ", name); return; }
   FileWriteString(h, content);
   FileClose(h);
}

void ExportAccount(){
   string j = "{";
   j += "\"login\":"        + IntegerToString(AccountInfoInteger(ACCOUNT_LOGIN)) + ",";
   j += "\"name\":\""       + Esc(AccountInfoString(ACCOUNT_NAME)) + "\",";
   j += "\"server\":\""     + Esc(AccountInfoString(ACCOUNT_SERVER)) + "\",";
   j += "\"company\":\""    + Esc(AccountInfoString(ACCOUNT_COMPANY)) + "\",";
   j += "\"currency\":\""   + Esc(AccountInfoString(ACCOUNT_CURRENCY)) + "\",";
   j += "\"leverage\":"     + IntegerToString(AccountInfoInteger(ACCOUNT_LEVERAGE)) + ",";
   j += "\"trade_mode\":"   + IntegerToString(AccountInfoInteger(ACCOUNT_TRADE_MODE)) + ",";
   j += "\"balance\":"      + DoubleToString(AccountInfoDouble(ACCOUNT_BALANCE),2) + ",";
   j += "\"equity\":"       + DoubleToString(AccountInfoDouble(ACCOUNT_EQUITY),2) + ",";
   j += "\"profit\":"       + DoubleToString(AccountInfoDouble(ACCOUNT_PROFIT),2) + ",";
   j += "\"margin\":"       + DoubleToString(AccountInfoDouble(ACCOUNT_MARGIN),2) + ",";
   j += "\"margin_free\":"  + DoubleToString(AccountInfoDouble(ACCOUNT_MARGIN_FREE),2) + ",";
   j += "\"margin_level\":" + DoubleToString(AccountInfoDouble(ACCOUNT_MARGIN_LEVEL),2) + ",";
   j += "\"updated\":\""    + TS(TimeCurrent()) + "\"}";
   WriteFile("account.json", j);
}

void ExportTrades(){
   datetime from = TimeCurrent() - (datetime)HistoryDays*86400;
   if(!HistorySelect(from, TimeCurrent()+86400)) return;

   string rows = "";
   int total = HistoryDealsTotal();
   // Un passage par deal de SORTIE : on retrouve l'entrée par position_id.
   for(int i=0; i<total; i++){
      ulong tk = HistoryDealGetTicket(i);
      if(tk == 0) continue;
      long entry = HistoryDealGetInteger(tk, DEAL_ENTRY);
      if(entry != DEAL_ENTRY_OUT && entry != DEAL_ENTRY_OUT_BY) continue;
      long dtype = HistoryDealGetInteger(tk, DEAL_TYPE);
      if(dtype != DEAL_TYPE_BUY && dtype != DEAL_TYPE_SELL) continue;

      long pid = HistoryDealGetInteger(tk, DEAL_POSITION_ID);
      double volume=0, openPrice=0, sl=0, tp=0;
      datetime openTime=0; long inType=-1; double gross=0, swap=0, comm=0;

      for(int k=0; k<total; k++){
         ulong dk = HistoryDealGetTicket(k);
         if(dk == 0) continue;
         if(HistoryDealGetInteger(dk, DEAL_POSITION_ID) != pid) continue;
         gross += HistoryDealGetDouble(dk, DEAL_PROFIT);
         swap  += HistoryDealGetDouble(dk, DEAL_SWAP);
         comm  += HistoryDealGetDouble(dk, DEAL_COMMISSION);
         if(HistoryDealGetInteger(dk, DEAL_ENTRY) == DEAL_ENTRY_IN){
            volume    = HistoryDealGetDouble(dk, DEAL_VOLUME);
            openPrice = HistoryDealGetDouble(dk, DEAL_PRICE);
            openTime  = (datetime)HistoryDealGetInteger(dk, DEAL_TIME);
            inType    = HistoryDealGetInteger(dk, DEAL_TYPE);
         }
      }
      if(openTime == 0) continue;

      // ── SL / TP : récupération en QUATRE passes ──────────────────────
      // Un seul point de lecture ne suffit pas : selon la façon dont le
      // trade a été géré (SL posé à l'entrée, déplacé ensuite, ou touché),
      // l'information ne se trouve pas au même endroit. Ne lire que
      // l'ordre d'entrée renvoyait sl=0 / tp=0 sur la plupart des trades.
      //
      //  1. l'ordre d'ENTRÉE (SL/TP posés à l'ouverture)
      //  2. TOUT ordre rattaché à la position (SL/TP modifiés ensuite)
      //  3. les niveaux portés par les DEALS de la position
      //  4. déduction par le motif de clôture (stop ou objectif atteint)
      int ot = HistoryOrdersTotal();
      for(int o=0; o<ot; o++){
         ulong ok_ = HistoryOrderGetTicket(o);
         if(ok_ == 0) continue;
         if(HistoryOrderGetInteger(ok_, ORDER_POSITION_ID) != pid) continue;
         double s1 = HistoryOrderGetDouble(ok_, ORDER_SL);
         double t1 = HistoryOrderGetDouble(ok_, ORDER_TP);
         // On conserve la DERNIÈRE valeur non nulle : c'est le niveau
         // réellement actif au moment de la clôture.
         if(s1 > 0) sl = s1;
         if(t1 > 0) tp = t1;
      }
      // Passe 3 : certains brokers renseignent SL/TP au niveau du deal.
      if(sl <= 0 || tp <= 0){
         for(int k2=0; k2<total; k2++){
            ulong dk2 = HistoryDealGetTicket(k2);
            if(dk2 == 0) continue;
            if(HistoryDealGetInteger(dk2, DEAL_POSITION_ID) != pid) continue;
            double s2 = HistoryDealGetDouble(dk2, DEAL_SL);
            double t2 = HistoryDealGetDouble(dk2, DEAL_TP);
            if(s2 > 0 && sl <= 0) sl = s2;
            if(t2 > 0 && tp <= 0) tp = t2;
         }
      }
      // Passe 4 : la position a été fermée PAR le stop ou PAR l'objectif.
      // Le prix de clôture EST alors le niveau, information de première
      // main que l'on ne doit surtout pas perdre.
      if(sl <= 0 || tp <= 0){
         string rsn = HistoryDealGetString(tk, DEAL_COMMENT);
         StringToLower(rsn);
         double cpx = HistoryDealGetDouble(tk, DEAL_PRICE);
         if(sl <= 0 && (StringFind(rsn, "sl") >= 0 || StringFind(rsn, "stop") >= 0))
            sl = cpx;
         if(tp <= 0 && (StringFind(rsn, "tp") >= 0 || StringFind(rsn, "take") >= 0))
            tp = cpx;
      }

      datetime closeTime = (datetime)HistoryDealGetInteger(tk, DEAL_TIME);
      double   closePrice= HistoryDealGetDouble(tk, DEAL_PRICE);
      string   sym       = HistoryDealGetString(tk, DEAL_SYMBOL);

      if(StringLen(rows) > 0) rows += ",";
      rows += "{";
      rows += "\"ticket\":"      + IntegerToString(pid) + ",";
      rows += "\"position_id\":" + IntegerToString(pid) + ",";
      rows += "\"symbol\":\""    + Esc(sym) + "\",";
      rows += "\"type\":\""      + (inType == DEAL_TYPE_BUY ? "BUY" : "SELL") + "\",";
      rows += "\"volume\":"      + DoubleToString(volume,2) + ",";
      rows += "\"open_price\":"  + DoubleToString(openPrice,5) + ",";
      rows += "\"close_price\":" + DoubleToString(closePrice,5) + ",";
      rows += "\"sl\":"          + DoubleToString(sl,5) + ",";
      rows += "\"tp\":"          + DoubleToString(tp,5) + ",";
      rows += "\"profit\":"      + DoubleToString(gross,2) + ",";
      rows += "\"swap\":"        + DoubleToString(swap,2) + ",";
      rows += "\"commission\":"  + DoubleToString(comm,2) + ",";
      rows += "\"pnl\":"         + DoubleToString(gross+swap+comm,2) + ",";
      rows += "\"open_time\":\"" + TS(openTime) + "\",";
      rows += "\"close_time\":\""+ TS(closeTime) + "\",";
      rows += "\"magic\":"       + IntegerToString(HistoryDealGetInteger(tk, DEAL_MAGIC)) + ",";
      rows += "\"comment\":\""   + Esc(HistoryDealGetString(tk, DEAL_COMMENT)) + "\"}";
   }
   WriteFile("trades.json", "{\"trades\":[" + rows + "]}");
}

void ExportPositions(){
   string rows = "";
   for(int i=0; i<PositionsTotal(); i++){
      ulong tk = PositionGetTicket(i);
      if(tk == 0) continue;
      if(StringLen(rows) > 0) rows += ",";
      rows += "{";
      rows += "\"ticket\":"        + IntegerToString(tk) + ",";
      rows += "\"symbol\":\""      + Esc(PositionGetString(POSITION_SYMBOL)) + "\",";
      rows += "\"type\":\""        + (PositionGetInteger(POSITION_TYPE)==POSITION_TYPE_BUY?"BUY":"SELL") + "\",";
      rows += "\"volume\":"        + DoubleToString(PositionGetDouble(POSITION_VOLUME),2) + ",";
      rows += "\"open_price\":"    + DoubleToString(PositionGetDouble(POSITION_PRICE_OPEN),5) + ",";
      rows += "\"price_current\":" + DoubleToString(PositionGetDouble(POSITION_PRICE_CURRENT),5) + ",";
      rows += "\"sl\":"            + DoubleToString(PositionGetDouble(POSITION_SL),5) + ",";
      rows += "\"tp\":"            + DoubleToString(PositionGetDouble(POSITION_TP),5) + ",";
      rows += "\"profit\":"        + DoubleToString(PositionGetDouble(POSITION_PROFIT),2) + ",";
      rows += "\"swap\":"          + DoubleToString(PositionGetDouble(POSITION_SWAP),2) + ",";
      rows += "\"open_time\":\""   + TS((datetime)PositionGetInteger(POSITION_TIME)) + "\",";
      rows += "\"magic\":"         + IntegerToString(PositionGetInteger(POSITION_MAGIC)) + "}";
   }
   WriteFile("positions.json", "{\"positions\":[" + rows + "]}");
}

void ExportAll(){ ExportAccount(); ExportTrades(); ExportPositions(); }

int OnInit(){
   EventSetTimer(MathMax(2, RefreshSeconds));
   ExportAll();
   Print("OmniTrade Hub: export actif (", RefreshSeconds, "s) -> MQL5/Files");
   return(INIT_SUCCEEDED);
}
void OnTimer(){ ExportAll(); }
void OnDeinit(const int reason){ EventKillTimer(); }
void OnTick(){}
"""


def ea_install_dir():
    """Dossier MQL5/Experts déduit du MQL5/Files détecté (installation directe)."""
    d = file_data_dir()
    if d and os.path.basename(d) == "Files":
        exp = os.path.join(os.path.dirname(d), "Experts")
        if os.path.isdir(exp):
            return exp
    return None


def emit_ea(path=None, auto_install=True):
    """Écrit OmniTradeExport.mq5.

    En binaire gelé, os.getcwd() vaut souvent « / » (lancement par double-clic
    depuis le Finder) : le fichier atterrirait dans un dossier invisible, voire
    non inscriptible. On écrit donc à côté de l'exécutable, et si le dossier
    MQL5/Experts est détecté, on y dépose une copie prête à compiler.
    """
    if not path:
        path = os.path.join(app_dir(), "OmniTradeExport.mq5")
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(EA_SOURCE)
    except OSError:                                   # dossier non inscriptible
        path = os.path.join(user_data_dir(), "OmniTradeExport.mq5")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(EA_SOURCE)
    print("Expert Advisor écrit : " + path)

    installed = None
    if auto_install:
        exp = ea_install_dir()
        if exp:
            try:
                installed = os.path.join(exp, "OmniTradeExport.mq5")
                with open(installed, "w", encoding="utf-8") as fh:
                    fh.write(EA_SOURCE)
                print("Copie installée      : " + installed)
            except Exception as e:
                log.debug("Installation EA : %s", e)
                installed = None

    print()
    print("Étapes :")
    if installed:
        print("  1. MT5 -> Outils -> MetaQuotes Language Editor")
        print("     OmniTradeExport.mq5 est déjà dans le Navigateur (Experts)")
        print("  2. Compiler (F7), glisser l'EA sur un graphique")
    else:
        print("  1. MT5 -> Outils -> MetaQuotes Language Editor")
        print("  2. Fichier -> Nouveau -> Expert Advisor, coller le contenu")
        print("  3. Compiler (F7), glisser l'EA sur un graphique")
    print("  4. Cocher « Autoriser le trading algorithmique »")
    print("  5. Relancer OmniTrade Hub Bridge")
    return path


# ═════════════════════════════════════════════════════════════════════════════
#  MAIN
# ═════════════════════════════════════════════════════════════════════════════
# ═══════════════════════════════════════════════════════════════════════════
#  TELEGRAM — alertes + dossiers (v8.42)
#  Tourne dans le pont : la page peut être fermée. PC allumé obligatoire.
#  Pas de signal d'entrée. Pas de 90 %.
# ═══════════════════════════════════════════════════════════════════════════

_TG = {
    "on": False,
    "token": "",
    "chat_id": "",
    "seen": {"high": "", "highs": {}, "cot": "", "cb": "", "gold": ""},
    "slot": "",
    "evening": "",
    "offset": 0,
    "last_ok": 0,
    "last_err": "",
    "busy": False,
    "last_topic": "",
    "last_cle": None,
}


def _tg_path():
    return os.path.join(user_data_dir(), "telegram.json")


def _tg_load():
    try:
        o = json.loads(open(_tg_path(), encoding="utf-8").read())
        if isinstance(o, dict):
            _TG["token"] = _tg_clean_token(o.get("token") or "")
            _TG["chat_id"] = str(o.get("chat_id") or "").strip()
            _TG["on"] = bool(o.get("on")) and bool(_TG["token"])
            _TG["seen"] = o.get("seen") or _TG["seen"]
            if not isinstance(_TG.get("seen"), dict):
                _TG["seen"] = {"high": "", "highs": {}, "cot": "", "cb": "", "gold": ""}
            if not isinstance(_TG["seen"].get("highs"), dict):
                _TG["seen"]["highs"] = {}
                oldh = _TG["seen"].get("high")
                if oldh:
                    _TG["seen"]["highs"][str(oldh)] = 1
            _TG["slot"] = o.get("slot") or ""
            _TG["evening"] = o.get("evening") or ""
            _TG["last_topic"] = o.get("last_topic") or ""
            _TG["last_cle"] = o.get("last_cle") or None
            if isinstance(o.get("mute"), dict):
                _TG["mute"] = o.get("mute")
            _TG["offset"] = int(o.get("offset") or 0)
    except Exception:
        pass
    return _TG


def _tg_save():
    try:
        os.makedirs(user_data_dir(), exist_ok=True)
        blob = {
            "on": bool(_TG["on"]),
            "token": _TG["token"],
            "chat_id": _TG["chat_id"],
            "seen": _TG["seen"],
            "slot": _TG["slot"],
            "evening": _TG["evening"],
            "offset": _TG["offset"],
            "last_topic": _TG.get("last_topic") or "",
            "last_cle": _TG.get("last_cle"),
            "mute": _TG.get("mute") or _mute_load(),
        }
        tmp = _tg_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(blob, f)
        os.replace(tmp, _tg_path())
        try:
            os.chmod(_tg_path(), 0o600)
        except Exception:
            pass
    except Exception as e:
        log.warning("tg save : %s", e)


def _tg_clean_token(s):
    """Accepte un collage sale : backticks, guillemets, message BotFather entier."""
    s = str(s or "").strip()
    for ch in ("`", "'", '"', "\u201c", "\u201d", "\u2018", "\u2019"):
        s = s.replace(ch, "")
    s = s.strip()
    m = re.search(r"(\d{6,}:[A-Za-z0-9_-]{20,})", s)
    if m:
        return m.group(1)
    return re.sub(r"\s+", "", s)


def _tg_ready():
    return bool(_TG.get("on") and _TG.get("token") and _TG.get("chat_id"))


def _tg_api(method, payload=None, timeout=20):
    tok = _tg_clean_token(_TG.get("token") or "")
    if not tok or ":" not in tok:
        return None, "token invalide"
    url = "https://api.telegram.org/bot%s/%s" % (tok, method)
    try:
        data = None
        headers = {"User-Agent": "OmniTradeHub/8.43", "Accept": "application/json"}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = _urq.Request(url, data=data, headers=headers)
        kw = {"timeout": timeout}
        if url.startswith("https"):
            kw["context"] = _ssl_ctx()
        with _urq.urlopen(req, **kw) as r:
            j = json.loads(r.read().decode("utf-8", "replace"))
        if not j.get("ok"):
            return None, str(j.get("description") or "telegram ko")
        return j.get("result"), None
    except Exception as e:
        return None, str(e)[:180]


def _tg_chunks(text, n=3800):
    text = (text or "").strip()
    if not text:
        return []
    out, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > n:
            if cur:
                out.append(cur)
            cur = line
        else:
            cur = (cur + "\n" + line) if cur else line
    if cur:
        out.append(cur)
    return out


def _tg_h(s):
    return ("" if s is None else str(s)).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _tg_line_html(line, first=False):
    t = (line or "").rstrip()
    if not t:
        return ""
    if t.strip() in ("————", "———", "---", "—", "——————", "----"):
        return "—"
    # déjà du HTML volontaire
    if "<b>" in t or "<i>" in t or "<code>" in t:
        return t
    esc = _tg_h(t)
    esc = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", esc)
    bare = t.strip()
    upperish = re.sub(r"[^A-Za-zÀ-ÿ0-9]", "", bare)
    is_head = (
        len(bare) <= 56 and len(bare) >= 4
        and not bare.startswith("•") and not bare.startswith("-")
        and upperish and upperish == upperish.upper()
        and sum(ch.isalpha() for ch in bare) >= 3
    )
    if first or is_head:
        return "<b>%s</b>" % esc
    if re.match(r"^(Pas un |Pas de |Ce n.est pas |Lecture de |Filtre, |La confluence|Analyse de régime|PC allumé)", bare):
        return "<i>%s</i>" % esc
    esc = re.sub(
        r"\b(XAUUSD|EURUSD|GBPUSD|USDJPY|AUDUSD|USDCAD|XAU/USD|EUR/USD|GBP/USD|USD/JPY|DXY|XAU|WTI|US10Y)\b",
        r"<code>\1</code>",
        esc,
    )
    return esc


def _tg_beautify(text):
    lines = (text or "").split("\n")
    out, first = [], True
    for line in lines:
        if not line.strip():
            out.append("")
            continue
        out.append(_tg_line_html(line, first=first))
        first = False
    return "\n".join(out)


def _tg_set_commands():
    cmds = [
        {"command": "start", "description": "Aide"},
        {"command": "brief", "description": "Brief de séance + radar"},
        {"command": "radar", "description": "Radar du jour"},
        {"command": "biais", "description": "Variation des biais"},
        {"command": "dossier", "description": "Analyse macro longue"},
        {"command": "macro", "description": "Fed BCE BoE BoJ"},
        {"command": "or", "description": "Gold Event Risk"},
        {"command": "cot", "description": "Positionnement COT"},
        {"command": "cal", "description": "Calendrier High"},
        {"command": "news", "description": "Fil Market Hub"},
        {"command": "test", "description": "Tester le pont"},
    ]
    try:
        _tg_api("setMyCommands", {"commands": cmds})
    except Exception:
        pass


def _tg_typing():
    try:
        if _TG.get("chat_id"):
            _tg_api("sendChatAction", {"chat_id": _TG["chat_id"], "action": "typing"})
    except Exception:
        pass


def _tg_log_path():
    return os.path.join(user_data_dir(), "telegram_log.json")


def _tg_log(direction, text, extra=None):
    """Archive légère de tout le trafic du pont (500 derniers messages).

    Chaque entrée : {ts, dir, text, ...extra}. Écriture atomique via .tmp.
    Consultation : GET /api/tg/log?limit=N
    """
    try:
        p = _tg_log_path()
        arr = []
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8") as f:
                arr = json.load(f) or []
        if not isinstance(arr, list):
            arr = []
        ent = {"ts": time.time(), "dir": direction, "text": str(text or "")[:6000]}
        if extra:
            try:
                ent.update(extra)
            except Exception:
                pass
        arr.append(ent)
        arr = arr[-500:]
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(arr, f, ensure_ascii=False)
        os.replace(tmp, p)
    except Exception as e:
        log.debug("tg log : %s", e)


def _persona_read():
    """Personnalité choisie par le trader (agent_persona.txt), '' si absente."""
    try:
        p = _persona_path()
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8") as f:
                return (f.read() or "").strip()
    except Exception:
        pass
    return ""


_TG_FLAG = {
    "USD": "🇺🇸", "EUR": "🇪🇺", "GBP": "🇬🇧", "JPY": "🇯🇵", "AUD": "🇦🇺",
    "CAD": "🇨🇦", "CHF": "🇨🇭", "NZD": "🇳🇿", "CNY": "🇨🇳",
}


def _tg_flag(ccy):
    return _TG_FLAG.get((ccy or "").upper().strip(), "🌍")


# Cotations de secours (Yahoo Finance, sans clé) quand le cache gold_macro est vide.
_TG_YAHOO = {"xau": "GC=F", "dxy": "DX-Y.NYB", "us10y": "^TNX", "wti": "CL=F"}
_TG_STOOQ_CACHE = {"t": 0.0, "v": {}}


def _tg_stooq_quotes():
    """{cle: {prix, varPct}} via Yahoo Finance ; cache 5 min ; échec silencieux."""
    now = time.time()
    if now - _TG_STOOQ_CACHE["t"] < 300:
        return _TG_STOOQ_CACHE["v"]
    out = {}
    import urllib.request
    import ssl as _ssl
    import json as _json
    for cle, sym in _TG_YAHOO.items():
        try:
            url = ("https://query1.finance.yahoo.com/v8/finance/chart/%s"
                   "?interval=1d&range=5d" % urllib.parse.quote(sym))
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            try:
                with urllib.request.urlopen(req, timeout=8) as r:
                    txt = r.read()
            except Exception:
                # macOS : chaîne de certificats incomplète → repli non vérifié
                ctx = _ssl._create_unverified_context()
                with urllib.request.urlopen(req, timeout=8, context=ctx) as r:
                    txt = r.read()
            d = _json.loads(txt)
            meta = (d.get("chart", {}).get("result") or [{}])[0].get("meta", {})
            price = meta.get("regularMarketPrice")
            prev = meta.get("chartPreviousClose") or meta.get("previousClose")
            if price:
                var = None
                if price and prev:
                    var = round((price - prev) / prev * 100.0, 2)
                out[cle] = {"prix": round(float(price), 2), "varPct": var}
        except Exception:
            continue
    _TG_STOOQ_CACHE["t"] = now
    _TG_STOOQ_CACHE["v"] = out
    return out


def tg_send(text, force=False):
    if not force and not _tg_ready():
        return False, "telegram non configuré"
    if not _TG.get("token") or not _TG.get("chat_id"):
        return False, "token ou chat manquant"
    ok_any = False
    err = ""
    pretty = _tg_beautify(text)
    # Type déduit du préfixe (FLASH OR, BRIEF …, SYNTHÈSE …, TAILLE …, /cmd…)
    _kind = re.sub(r"<[^>]+>", "", (text or "").strip())
    _kind = re.split(r"[\n:—–]", _kind, 1)[0].strip()[:32] or "msg"
    for part in _tg_chunks(pretty):
        payload = {
            "chat_id": _TG["chat_id"],
            "text": part,
            "disable_web_page_preview": True,
            "parse_mode": "HTML",
        }
        res, err = _tg_api("sendMessage", payload)
        if res is None and err and "parse" in str(err).lower():
            payload.pop("parse_mode", None)
            plain = re.sub(r"<[^>]+>", "", part)
            for a, b in (("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"')):
                plain = plain.replace(a, b)
            payload["text"] = plain
            res, err = _tg_api("sendMessage", payload)
        if res is not None:
            ok_any = True
            _TG["last_ok"] = time.time()
            _TG["last_err"] = ""
        else:
            _TG["last_err"] = err or "envoi échoué"
            log.warning("tg send : %s", err)
    _tg_log("out", text, {"ok": ok_any, "err": (_TG.get("last_err") or "")[:160], "kind": _kind})
    return ok_any, (_TG["last_err"] if not ok_any else "")


def _tg_abj_hm():
    try:
        from zoneinfo import ZoneInfo
        now = datetime.now(ZoneInfo("Africa/Abidjan"))
    except Exception:
        now = datetime.now(timezone.utc)
    return now, now.hour * 60 + now.minute, now.strftime("%Y-%m-%d")


# Sessions type Market Hub (heure locale de la ville, DST auto).
_TG_SES = [
    {"id": "sydney", "name": "Sydney", "tz": "Australia/Sydney", "openH": 7, "closeH": 16},
    {"id": "tokyo", "name": "Tokyo", "tz": "Asia/Tokyo", "openH": 9, "closeH": 18},
    {"id": "london", "name": "Londres", "tz": "Europe/London", "openH": 8, "closeH": 17},
    {"id": "ny", "name": "New York", "tz": "America/New_York", "openH": 8, "closeH": 17},
]
_TG_COT_CACHE = {"t": 0.0, "rows": []}
_TG_CMD_HELP = (
    "OMNITRADE HUB\n"
    "Connecté. Le pont de ce Mac / PC tourne.\n"
    "\n"
    "QUESTION\n"
    "Écrivez comme dans Psycho & IA. Ex. : que se passe-t-il sur l'or ? "
    "Ou : on est sur quelle session ?\n"
    "\n"
    "AUTOMATIQUE\n"
    "Brief Sydney / Tokyo / Londres / New York, 10 min avant l'ouverture réelle.\n"
    "Radar + variation des biais, High T-15, flash Or, communiqué G4, synthèse 18 h Abidjan.\n"
    "\n"
    "COMMANDES\n"
    "/brief  /sydney  /tokyo  /londres  /ny\n"
    "/radar  /biais  /dossier  /macro  /or  /cot  /cal  /news  /test\n"
    "\n"
    "Pas de signal d'entrée."
)


def _tg_zi(name):
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(name)
    except Exception:
        return timezone.utc


def _tg_fx_weekend(now_utc):
    """Même règle que Market Hub : vendredi 21:00 UTC → dimanche 21:00 UTC."""
    day = now_utc.weekday()
    um = now_utc.hour * 60 + now_utc.minute
    if day == 5:
        return True
    if day == 4 and um >= 21 * 60:
        return True
    if day == 6 and um < 21 * 60:
        return True
    return False


def _tg_ses_open_close_utc(s, local_day):
    tz = _tg_zi(s["tz"])
    o = local_day.replace(hour=int(s["openH"]), minute=0, second=0, microsecond=0)
    if o.tzinfo is None:
        o = o.replace(tzinfo=tz)
    else:
        o = o.astimezone(tz).replace(hour=int(s["openH"]), minute=0, second=0, microsecond=0)
    c = o.replace(hour=int(s["closeH"]))
    if c <= o:
        c = c + timedelta(days=1)
    return o.astimezone(timezone.utc), c.astimezone(timezone.utc)


def _tg_ses_is_open(s, now_utc):
    tz = _tg_zi(s["tz"])
    loc = now_utc.astimezone(tz)
    for off in (0, -1):
        day = (loc + timedelta(days=off)).replace(hour=0, minute=0, second=0, microsecond=0)
        o, c = _tg_ses_open_close_utc(s, day)
        if o <= now_utc < c:
            return True, o, c
    return False, None, None


def _tg_ses_next_open(s, now_utc):
    tz = _tg_zi(s["tz"])
    loc = now_utc.astimezone(tz)
    for off in range(0, 4):
        day = (loc + timedelta(days=off)).replace(hour=0, minute=0, second=0, microsecond=0)
        o, c = _tg_ses_open_close_utc(s, day)
        if now_utc < o:
            return o
    return None


def _tg_fmt_mins(m):
    m = int(max(0, m))
    if m < 60:
        return "%s min" % m
    h, mm = divmod(m, 60)
    if mm == 0:
        return "%s h" % h
    return "%s h %02d" % (h, mm)


def _tg_sessions_line(now_utc=None):
    now_utc = now_utc or datetime.now(timezone.utc)
    bits = []
    for s in _TG_SES:
        op, _o, _c = _tg_ses_is_open(s, now_utc)
        if op:
            bits.append(s["name"] + " ouverte")
        else:
            nxt = _tg_ses_next_open(s, now_utc)
            if nxt is None:
                bits.append(s["name"] + " —")
            else:
                mins = (nxt - now_utc).total_seconds() / 60.0
                bits.append("%s dans %s" % (s["name"], _tg_fmt_mins(mins)))
    return " · ".join(bits)


def _tg_session_slot(now_utc=None):
    """Fenêtre brief : 10 min avant → 20 min après l'ouverture locale."""
    now_utc = now_utc or datetime.now(timezone.utc)
    if _tg_fx_weekend(now_utc):
        return None
    for s in _TG_SES:
        tz = _tg_zi(s["tz"])
        loc = now_utc.astimezone(tz)
        for off in (0, -1, 1):
            day = (loc + timedelta(days=off)).replace(hour=0, minute=0, second=0, microsecond=0)
            o, _c = _tg_ses_open_close_utc(s, day)
            delta = (now_utc - o).total_seconds() / 60.0
            if -10 <= delta <= 20:
                jour = o.astimezone(tz).strftime("%Y-%m-%d")
                return {
                    "id": s["id"],
                    "name": s["name"],
                    "key": jour + "-" + s["id"],
                    "open_utc": o,
                    "delta_min": delta,
                }
    return None


def _tg_ses_by_id(sid):
    sid = (sid or "").lower().strip()
    aliases = {
        "londres": "london", "london": "london", "lon": "london", "l": "london",
        "ny": "ny", "newyork": "ny", "new-york": "ny", "new york": "ny", "nyc": "ny",
        "tokyo": "tokyo", "tyo": "tokyo", "asie": "tokyo",
        "sydney": "sydney", "syd": "sydney", "australie": "sydney",
    }
    sid = aliases.get(sid, sid)
    for s in _TG_SES:
        if s["id"] == sid:
            return s
    return None


def _tg_fmt_abj(dt_utc):
    try:
        from zoneinfo import ZoneInfo
        return dt_utc.astimezone(ZoneInfo("Africa/Abidjan")).strftime("%H:%M")
    except Exception:
        return dt_utc.astimezone(timezone.utc).strftime("%H:%M")


def _tg_cot_pack(cle, semaines=60):
    hist, err = _cot_charger(cle, semaines=semaines)
    if not hist:
        return None, err
    a = _COT_ACTIFS.get(cle) or {}
    if a.get("inverse"):
        hist = [dict(l, net=-l["net"], commNet=-l.get("commNet", 0),
                     long=l["short"], short=l["long"]) for l in hist]
    b = _cot_biais(hist) or {}
    return {"cle": cle, "a": a, "hist": hist, "b": b}, None


def _tg_cot_scan(force=False):
    now = time.time()
    if (not force) and _TG_COT_CACHE["rows"] and (now - _TG_COT_CACHE["t"] < 180):
        return _TG_COT_CACHE["rows"]
    rows = []
    cles = list(_COT_ACTIFS.keys())
    w = _watch_load()
    if w:
        wset = set(w)
        pref = [c for c in cles if c in wset]
        if pref:
            cles = pref
    for cle in cles:
        pack, _err = _tg_cot_pack(cle)
        if pack:
            rows.append(pack)
    _TG_COT_CACHE["t"] = now
    _TG_COT_CACHE["rows"] = rows
    return rows


def _tg_fmt_when(iso):
    try:
        d = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        return d.strftime("%d/%m %H:%M")
    except Exception:
        return str(iso or "")[:16]


def _tg_cal_events():
    evs = []
    try:
        v, _a = _mkt_last_known("cal_fr")
        evs = (v or {}).get("events") or []
    except Exception:
        evs = []
    if not evs:
        try:
            with app.test_request_context("/api/calendar?lang=fr"):
                j = api_calendar().get_json() or {}
            evs = j.get("events") or []
        except Exception:
            evs = []
    return evs


def _tg_news_items():
    try:
        v, _a = _mkt_last_known("news_fr")
        return (v or {}).get("items") or []
    except Exception:
        return []


def _tg_gold_macro():
    try:
        v, _a = _mkt_last_known("gold_macro")
        return v or {}
    except Exception:
        return {}


def _tg_gold_news():
    """Dépêches « catalyseurs or ». Repli : Market Hub filtré si le cache
    dédié est vide (le pont ne déclenche pas la collecte lourde de l'app)."""
    try:
        v, _a = _mkt_last_known("gold_news_fr")
        items = (v or {}).get("items") or []
        if items:
            return items
    except Exception:
        pass
    try:
        v, _a = _mkt_last_known("news_fr")
        items = (v or {}).get("items") or []
        keep = [x for x in items if _tg_news_keep(x) and re.search(
            r"\b(or\b|gold|xau|dxy|dollar|fed|fomc|taux|inflation|pce|cpi|"
            r"jackson hole|banque centrale|p[eé]trole|wti|rendement)\b",
            str(x.get("title") or x.get("titre") or "").lower())]
        return keep[:12]
    except Exception:
        return []


def _tg_cb_snap():
    try:
        if _cb is None:
            return {}
        return _cb.snapshot() or {}
    except Exception:
        return {}


def tg_text_cal():
    now = datetime.now(timezone.utc)
    evs = _tg_cal_events()
    high_soon, published = [], []
    for e in evs:
        try:
            d = datetime.fromisoformat(str(e.get("date") or "").replace("Z", "+00:00"))
        except Exception:
            continue
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        mins = (d - now).total_seconds() / 60.0
        imp = e.get("impact") or ""
        ccy = e.get("country") or ""
        line = "%s %s  %s  %s" % (_tg_flag(ccy), _tg_fmt_when(e.get("date")), ccy,
                                  e.get("title") or "")
        if 0 < mins <= 12 * 60:
            line += "  ⏳ %s" % _tg_fmt_mins(mins)
        if e.get("actual"):
            line += "  📊 %s" % e.get("actual")
            if e.get("forecast"):
                line += " (prévu %s)" % e.get("forecast")
        if imp == "High" and 0 <= mins <= 12 * 60:
            high_soon.append(line)
        if e.get("actual") and -16 * 60 <= mins <= 0 and imp in ("High", "Medium"):
            published.append(line)
    lines = ["📅 CALENDRIER ÉCONOMIQUE", ""]
    lines.append("🔥 High à venir (12 h)")
    lines.extend(high_soon[:8] or ["  aucun High dans les 12 h — séance tranquille"])
    lines.append("")
    lines.append("✅ Chiffres déjà tombés (journée)")
    lines.extend(published[:8] or ["  rien de publié récemment"])
    lines.append("")
    lines.append("<i>Ce n'est pas un signal d'entrée.</i>")
    return "\n".join(lines)


def _tg_news_keep(it):
    """Filtre de pertinence : finance/macro FX uniquement, anecdotes rejetées."""
    it = it or {}
    cat = str(it.get("category") or "").lower()
    if cat == "crypto":
        return False
    tit = str(it.get("title") or it.get("titre") or "")
    if len(tit) < 28:                       # fils trop courts = bruit
        return False
    low = tit.lower()
    # Rejet d'anecdotes hors marché (faits divers, people, sport, tech grand public)
    if re.search(
        r"\b(vol\w*es?\s|voleurs|braquage|cambriol|stolen|robbery|jewel|bijoux|"
        r"burqa|loterie|horoscope|recette|celebrit|people\b|tennis|football|"
        r"basket|jeux olympiques|série netflix|box-office|iphone|robotique|"
        r"jetson|ipos? de 20\d\d)\b", low,
    ):
        return False
    # Mots-clés marché avec frontières de mots (évite « aud » dans « chaud »)
    if re.search(
        r"\b(fed|fomc|powell|warsh|bce|ecb|lagarde|boe|boj|ueda|dollar|dxy|"
        r"euro\b|yen\b|sterling|aussie|kiwi|loonie|xau|\bor\b|gold|oil\b|wti|"
        r"brent|yield|treasury|bons du trésor|cpi|pce|nfp|inflation|d[eé]flation|"
        r"emploi|ch[oô]mage|unemployment|payroll|taux|int[eé]r[eê]t|minutes|"
        r"opec|opep|jackson hole|geopolit|guerre|cessez-le-feu|sanctions|"
        r"pib\b|gdp\b|croissance|r[eé]cession|banque centrale|politique mon[ée]taire)\b",
        low,
    ):
        return True
    return cat in ("forex", "commodities")


def _tg_news_fx(limit=10):
    items = _tg_news_items()
    keep = [x for x in items if _tg_news_keep(x)]
    return keep[:limit]


def tg_text_brief():
    """Sessions + chiffres + news FX + or + COT court — synthese globale."""
    now, _hm, _j = _tg_abj_hm()
    lines = [
        "BRIEF MARCHE",
        "Heure Abidjan %s" % now.strftime("%d/%m %H:%M"),
        "Sessions : %s" % _tg_sessions_line(),
        "",
        tg_text_cal()[:900],
        "",
        "NEWS FX/MACRO",
    ]
    news = _tg_news_fx(8)
    if not news:
        lines.append("  fil vide")
    for it in news:
        tit = it.get("title") or it.get("titre") or ""
        src = it.get("source") or ""
        extra = it.get("resume") or it.get("summary") or ""
        lines.append("• %s — %s" % (src, tit))
        if extra:
            lines.append("  %s" % str(extra)[:180])
    gm = _tg_gold_macro()
    c = (gm.get("cours") or {})
    b = (gm.get("biais") or {})
    def _px(k):
        x = c.get(k) or {}
        return "—" if x.get("prix") is None else str(x.get("prix"))
    lines.append("")
    def _var(k):
        x = c.get(k) or {}
        v = x.get("varPct")
        if v is None:
            return ""
        return " (%s%.2f%%)" % ("+" if v > 0 else "", v)
    lines.append("OR XAU %s%s · DXY %s%s · biais %s" % (
        _px("xau"), _var("xau"), _px("dxy"), _var("dxy"), b.get("etiquette") or "—"))
    try:
        lines.append("")
        lines.append(tg_text_cot()[:350])
    except Exception:
        pass
    lines.append("Un % COT = alignement, pas une news ni un taux de reussite.")
    return "\n".join(lines)


def tg_text_news():
    items = _tg_news_fx(12)
    lines = ["MARKET HUB — ACTUALITES", ""]
    if not items:
        return "MARKET HUB — aucune depeche en cache. Relancez le pont et attendez 1 minute."
    for it in items:
        cat = it.get("category") or ""
        src = it.get("source") or ""
        tit = it.get("title") or it.get("titre") or ""
        extra = it.get("resume") or it.get("summary") or it.get("description") or ""
        lines.append("• [%s] %s — %s" % (cat, src, tit))
        if extra:
            lines.append("  %s" % str(extra)[:220])
    lines.append("")
    lines.append("Depeches reelles du fil Market Hub, pas une invention.")
    return "\n".join(lines)


def tg_text_gold():
    m = _tg_gold_macro()
    news = [n for n in _tg_gold_news() if _tg_news_keep(n)][:10]
    c = (m.get("cours") or {})
    # Secours : cotations live Stooq si le cache app est vide (prix « — »)
    if not any((c.get(k) or {}).get("prix") is not None for k in ("xau", "dxy", "us10y", "wti")):
        live = _tg_stooq_quotes()
        for k, v in live.items():
            if (c.get(k) or {}).get("prix") is None:
                c.setdefault(k, v)
    b = m.get("biais") or {}
    tr = m.get("tauxReels") or {}
    lines = ["💛 <b>GOLD EVENT RISK</b>", ""]
    def px(k, lab):
        x = c.get(k) or {}
        p = x.get("prix")
        if p is None:
            return "%s : —" % lab
        var = x.get("varPct")
        bit = "%s : <b>%s</b>" % (lab, p)
        if var is not None:
            arrow = "🟢+" if var >= 0 else "🔴"
            bit += " (%s%s%%)" % (arrow, var)
        return bit
    lines.append(px("xau", "XAU/USD"))
    lines.append(px("dxy", "DXY"))
    lines.append(px("us10y", "US10Y"))
    lines.append(px("wti", "WTI"))
    if tr.get("valeur") is not None:
        lines.append("Taux réel 10 ans (TIPS) : %s %%" % tr.get("valeur"))
    sc = b.get("score")
    lines.append("Biais macro : %s · fiabilité %s · score %s" % (
        b.get("etiquette") or "—", b.get("fiabilite") or "—",
        ("%+.1f" % float(sc)) if isinstance(sc, (int, float)) else "—"))
    for comp in (b.get("composantes") or [])[:4]:
        if comp.get("dispo"):
            lines.append("  - %s : %s" % (comp.get("libelle"), comp.get("effet")))
    lines.append("")
    lines.append("<b>⚡ Fil (catalyseurs)</b>")
    if not news:
        lines.append("  aucune dépêche pertinente en cache")
    for n in news:
        urg = n.get("urgence") or ""
        tag = "🔥 " if urg == "flash" else ("🌡 " if urg == "chaud" else "• ")
        tit = n.get("titre") or n.get("title") or ""
        src = n.get("source") or ""
        lines.append("%s%s <i>[%s]</i>" % (tag, _tg_h(str(tit))[:160], _tg_h(src)))
        if n.get("resume"):
            lines.append("   %s" % _tg_h(str(n.get("resume")))[:200])
    lines.append("")
    lines.append("<i>Lecture de régime, pas un ordre.</i>")
    return "\n".join(lines)


def tg_text_cot():
    lines = ["COT & BIAIS", ""]
    cles = ["XAUUSD", "EURUSD", "GBPUSD", "USDJPY", "DXY", "AUDUSD", "USDCAD"]
    last = ""
    for cle in cles:
        try:
            hist, err = _cot_charger(cle, semaines=60)
        except Exception as e:
            hist, err = None, str(e)
        if not hist:
            lines.append("%s : indisponible%s" % (cle, (" (%s)" % err) if err else ""))
            continue
        a = _COT_ACTIFS.get(cle) or {}
        if a.get("inverse"):
            hist = [dict(l, net=-l["net"], commNet=-l.get("commNet", 0),
                         long=l["short"], short=l["long"]) for l in hist]
        b = _cot_biais(hist) or {}
        d0 = hist[-1]
        last = d0.get("date") or last
        lines.append("%s  %s  index %s  net %s  conv. %s %%" % (
            a.get("sym") or cle,
            (b.get("libelle") or b.get("sens") or "—"),
            b.get("cotIndex"),
            d0.get("net"),
            b.get("dominance")))
        if b.get("alerte"):
            lines.append("  alerte : %s" % b.get("alerte"))
        if b.get("conseil"):
            lines.append("  %s" % b.get("conseil"))
    if last:
        lines.insert(2, "Rapport CFTC du %s" % last)
        lines.insert(3, "")
    lines.append("")
    lines.append("La confluence n'est PAS un taux de réussite. Filtre, pas entrée.")
    return "\n".join(lines)


def _tg_fmt_int(n):
    try:
        n = int(float(n))
    except Exception:
        return "—"
    s = "{:,}".format(abs(n)).replace(",", " ")
    if n > 0:
        return "+" + s
    if n < 0:
        return "-" + s
    return s


def _tg_pair_icon(sens):
    s = (sens or "").lower()
    if s == "achat":
        return "🟢"
    if s == "vente":
        return "🔴"
    return "⚪"


def _tg_pack_card(pack):
    """Carte type radar app : paire, biais, confluence, index, net."""
    if not pack:
        return []
    b = pack.get("b") or {}
    a = pack.get("a") or {}
    hist = pack.get("hist") or []
    d0 = hist[-1] if hist else {}
    sym = a.get("sym") or pack.get("cle") or "—"
    lib = b.get("libelle") or (b.get("sens") or "—").upper()
    icon = _tg_pair_icon(b.get("sens"))
    lines = [
        "%s <b>%s</b> — %s" % (icon, _tg_h(sym), _tg_h(lib)),
        "confluence <b>%s %%</b> · index %s · net %s" % (
            b.get("dominance") if b.get("dominance") is not None else "—",
            b.get("cotIndex") if b.get("cotIndex") is not None else "—",
            _tg_fmt_int(d0.get("net"))),
    ]
    if b.get("alerte"):
        lines.append("⚠️ %s" % _tg_h(b.get("alerte")))
    hz = b.get("horizons") or {}
    bits = []
    if (hz.get("hebdo") or {}).get("txt"):
        bits.append("hebdo " + hz["hebdo"]["txt"])
    if (hz.get("annuel") or {}).get("txt"):
        bits.append("fond " + hz["annuel"]["txt"])
    if bits:
        lines.append(" · ".join(bits))
    if b.get("conseil"):
        lines.append(_tg_h(str(b.get("conseil"))[:280]))
    return lines


def _tg_is_follow(q):
    """Suite ('ca signifie ?') — pas un nouveau sujet."""
    s = (q or "").strip()
    if len(s) > 160:
        return False
    low = s.lower()
    return bool(re.search(
        r"signifie|oeil|œil|oeuil|expert|explique|interprete|interprétation|"
        r"ca veut dire|ça veut dire|et alors|vas[- ]?y|continue|"
        r"qu.est.ce que (cela|ca|ça)|et du coup|pourquoi",
        low,
    ))


def _tg_detect_topic(q):
    """Retourne (sujet, cle_cot). sujet: gold|pair|session|cal|fed|news|radar."""
    raw = (q or "")
    low = raw.lower()
    compact = re.sub(r"[^a-z0-9]", "", low)
    if re.search(r"market\s*hub|markethub|\bactus?\b|\bactualit", low) or "markethub" in compact:
        return "news", None
    if re.search(r"\b(fomc|fed|federal reserve|powell|minutes du fomc|minutes fomc)\b", low) or "fomc" in compact:
        return "fed", None
    if re.search(r"\b(session|seance|séance|londres|london|tokyo|sydney|new york)\b", low):
        if not re.search(r"\b(eur|gbp|usd|xau|or|gold|cot)\b", low):
            return "session", None
    if re.search(r"\b(calendrier|high|nfp|chiffre)\b", low):
        if not re.search(r"\b(eurusd|gold|xau|cot|fomc|fed)\b", low):
            return "cal", None
    goldish = (
        "gold" in low or "xau" in compact or re.search(r"\bl[' ]?or\b", low)
        or "l'or" in low or "lor " in low or low.strip() in ("or", "l'or")
    )
    if goldish:
        return "gold", "XAUUSD"
    aliases = [
        ("EURUSD", ("eurusd", "eur/usd", "euro")),
        ("GBPUSD", ("gbpusd", "gbp/usd", "sterling", "livre")),
        ("USDJPY", ("usdjpy", "usd/jpy", "yen")),
        ("AUDUSD", ("audusd", "aud/usd", "aussie", "australien")),
        ("USDCAD", ("usdcad", "usd/cad", "loonie", "canadien")),
        ("DXY", ("dxy", "dollar index", "indice dollar")),
        ("XAGUSD", ("xagusd", "silver", "argent")),
        ("WTI", ("wti", "petrole", "pétrole", "oil")),
    ]
    for cle, keys in aliases:
        for k in keys:
            kk = k.replace("/", "")
            # "oil" dans "moila" (fais MOI LA synthèse) ne doit PAS matcher WTI
            if len(kk) <= 3:
                if re.search(r"\b" + re.escape(k) + r"\b", low):
                    return "pair", cle
            elif kk in compact or k in low:
                return "pair", cle
    if re.search(r"\b(news|actu|actualité|actualite|fil)\b", low):
        return "news", None
    if re.search(r"synthese|synthèse|global|sante macro|santé macro|que se passe|actuellement|\bmarche\b|\bmarché\b|\bmarcher\b|brief|macro", low):
        return "brief", None
    return "radar", None


def tg_text_radar():
    rows = _tg_cot_scan()
    now_utc = datetime.now(timezone.utc)
    lines = [
        "🎯 <b>RADAR DU JOUR</b>",
        "",
        "🕐 " + _tg_h(_tg_sessions_line(now_utc)),
        "<i>3 COT · 3 High · Fed · Or — alignement, pas une entrée</i>",
        "",
        "📊 <b>COT à surveiller</b>",
    ]
    ranked = []
    for p in rows:
        b = p.get("b") or {}
        ranked.append((int(b.get("dominance") or 0), abs(float(b.get("score") or 0)), p))
    ranked.sort(key=lambda x: (x[0], x[1]), reverse=True)
    if not ranked:
        lines.append("pas encore de lecture CFTC")
    for _d, _s, p in ranked[:3]:
        lines.extend(_tg_pack_card(p))
        lines.append("")
    lines.append("<i>confluence = alignement, pas un taux de réussite</i>")
    lines.append("")
    lines.append("📅 <b>High à venir (10 h)</b>")
    now = datetime.now(timezone.utc)
    highs = []
    for e in _tg_cal_events():
        if (e.get("impact") or "") != "High":
            continue
        try:
            d = datetime.fromisoformat(str(e.get("date") or "").replace("Z", "+00:00"))
        except Exception:
            continue
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        mins = (d - now).total_seconds() / 60.0
        if -1 <= mins <= 10 * 60:
            highs.append((mins, e))
    highs.sort(key=lambda x: x[0])
    if not highs:
        lines.append("aucun High dans les 10 h")
    for mins, e in highs[:3]:
        lines.append("• <b>%s</b> — %s · %s" % (
            _tg_h(_tg_fmt_mins(mins)),
            _tg_h(e.get("country") or ""),
            _tg_h(e.get("title") or "")))
    snap = _tg_cb_snap()
    banks = snap.get("banks") or {}
    f = banks.get("FED") or {}
    sc = f.get("score")
    try:
        scs = "%+.1f" % float(sc)
    except Exception:
        scs = "—"
    gm = _tg_gold_macro()
    c = (gm.get("cours") or {})
    b = (gm.get("biais") or {})
    def _px(k):
        x = c.get(k) or {}
        if x.get("prix") is None:
            return "—"
        return str(x.get("prix"))
    lines.append("")
    lines.append("🏛️ <b>Macro</b>")
    tit = str(f.get("last_title") or "")[:70]
    lines.append("Fed %s %s%s" % (
        _tg_h(scs), _tg_h(f.get("label") or "neutre"),
        (" · " + _tg_h(tit)) if tit else ""))
    lines.append("XAU <b>%s</b> · DXY <b>%s</b> · biais %s" % (
        _tg_h(_px("xau")), _tg_h(_px("dxy")), _tg_h(b.get("etiquette") or "—")))
    lines.append("")
    lines.append("<i>Lecture de régime, pas un ordre.</i>")
    return "\n".join(lines)


def tg_text_biais_var():
    rows = _tg_cot_scan()
    lines = [
        "VARIATION DES BIAIS",
        "",
        "Delta depuis le dernier rapport CFTC (hebdomadaire). Ce n'est pas un COT live du jour.",
        "",
    ]
    last = ""
    if not rows:
        lines.append("  pas encore de lecture CFTC")
    for p in rows:
        hist = p["hist"]
        b = p["b"] or {}
        a = p["a"]
        d0 = hist[-1]
        d1 = hist[-2] if len(hist) >= 2 else None
        last = d0.get("date") or last
        nets = [l["net"] for l in hist]
        idx0 = b.get("cotIndex")
        idx1 = _cot_index(nets[:-1], 52) if len(nets) >= 3 else None
        net0 = d0.get("net")
        net1 = d1.get("net") if d1 else None
        dnet = (net0 - net1) if (net0 is not None and net1 is not None) else None
        if dnet is None:
            dnet_s = "—"
        else:
            dnet_s = "%+d" % int(dnet)
        idx_s = "—"
        if idx1 is not None and idx0 is not None:
            idx_s = "%s → %s" % (idx1, idx0)
        elif idx0 is not None:
            idx_s = str(idx0)
        net_s = "—"
        if net1 is not None and net0 is not None:
            net_s = "%s → %s" % (net1, net0)
        elif net0 is not None:
            net_s = str(net0)
        hz = (b.get("horizons") or {})
        h = (hz.get("hebdo") or {})
        an = (hz.get("annuel") or {})
        lines.append("%s  %s  index %s  net %s  (%s)" % (
            a.get("sym") or p["cle"],
            b.get("libelle") or b.get("sens") or "—",
            idx_s, net_s, dnet_s))
        extra = []
        if h.get("txt"):
            extra.append("hebdo %s" % h.get("txt"))
        if an.get("txt"):
            extra.append("fond %s" % an.get("txt"))
        if extra:
            lines.append("  " + " · ".join(extra))
        if b.get("alerte"):
            lines.append("  alerte : %s" % b.get("alerte"))
    if last:
        lines.insert(3, "Rapport CFTC du %s" % last)
        lines.insert(4, "")
    lines.append("")
    lines.append("La confluence n'est PAS un taux de réussite. Filtre, pas entrée.")
    return "\n".join(lines)


def tg_text_session_brief(ses=None):
    now_abj, _hm, _jour = _tg_abj_hm()
    now_utc = datetime.now(timezone.utc)
    if ses is None:
        slot = _tg_session_slot(now_utc)
        if slot:
            ses = _tg_ses_by_id(slot["id"])
        else:
            # prochaine session
            best = None
            for s in _TG_SES:
                nxt = _tg_ses_next_open(s, now_utc)
                if nxt and (best is None or nxt < best[0]):
                    best = (nxt, s)
            ses = best[1] if best else _TG_SES[2]
    name = ses["name"]
    tz = _tg_zi(ses["tz"])
    loc = now_utc.astimezone(tz)
    op, o, c = _tg_ses_is_open(ses, now_utc)
    nxt = _tg_ses_next_open(ses, now_utc)
    if op and o is not None:
        etat = "ouverte depuis %s (Abidjan %s)" % (
            o.astimezone(tz).strftime("%H:%M"), _tg_fmt_abj(o))
    elif nxt is not None:
        etat = "ouverture %s %s (Abidjan %s) — dans %s" % (
            nxt.astimezone(tz).strftime("%d/%m"),
            nxt.astimezone(tz).strftime("%H:%M"),
            _tg_fmt_abj(nxt),
            _tg_fmt_mins((nxt - now_utc).total_seconds() / 60.0))
    else:
        etat = "horaire indisponible"
    week = "Week-end forex (vendredi 21:00 → dimanche 21:00 UTC)." if _tg_fx_weekend(now_utc) else ""
    head = [
        "BRIEF %s  —  %s  (Abidjan)" % (name.upper(), now_abj.strftime("%d/%m/%Y %H:%M")),
        "Heure %s : %s. %s" % (name, loc.strftime("%H:%M"), etat),
        "Sessions : " + _tg_sessions_line(now_utc),
    ]
    if week:
        head.append(week)
    head.append("PC allumé + pont. Analyse de régime, pas un ordre.")
    head.append("")
    return "\n".join(head) + "\n" + tg_text_radar() + "\n\n————\n\n" + tg_text_biais_var()


def tg_text_brief_ia(ses_name, facts):
    key = ""
    try:
        key = _groq_key_read()
    except Exception:
        key = ""
    if not key or _cb is None or not hasattr(_cb, "groq_chat"):
        return None
    sys = ""
    _ptxt = _persona_read()
    if _ptxt:
        sys += ("PERSONNALITE CHOISIE PAR LE TRADER — prioritaire sur le ton par "
                "defaut, applique-la :\n%s\n\n" % _ptxt[:1200])
    sys += (
        "Tu rediges un brief de seance (%s) pour un trader a Abidjan. "
        "Francais, ton direct, 6 a 9 phrases completes, une seule idee fluide "
        "sans decoupage artificiel phrase-par-phrase. "
        "Uniquement les faits fournis. INTERDIT d'inventer un niveau de prix ou "
        "un seuil absent des faits. INTERDIT signal d'entree, interdit 90 %%, "
        "interdit 'achetez'. Un pourcentage COT est un alignement positionnement, "
        "JAMAIS une probabilite : interdit le mot probabilite. "
        "Mentionne le radar et la variation des biais si les chiffres sont la. "
        "**Gras** sur 2 chiffres cles. EMOJIS OBLIGATOIRES : 3 a 6, places DANS "
        "les phrases (drapeaux pays 🇺🇸🇪🇺, 🥇 or, ⏳ horaires), jamais une file "
        "seule a la fin. Texte brut, pas de think."
    ) % ses_name
    try:
        out, err = _cb.groq_chat(
            key,
            [{"role": "system", "content": sys},
             {"role": "user", "content": facts[:10000]}],
            max_tokens=900,
            disable_thinking=True,
        )
        if err:
            return None
        txt = (out or {}).get("content") or ""
        txt = re.sub(r"(?is)<think>.*?</think>", " ", txt)
        txt = re.sub(r"(?is)<think>.*", " ", txt).strip()
        if len(txt) < 220:
            return None
        return "LECTURE %s — OmniTrade Hub\n(faits du pont, pas d'internet libre)\n\n" % ses_name.upper() + txt
    except Exception:
        return None


def tg_send_session_brief(ses=None, want_ia=True):
    """UN SEUL message : faits du radar + lecture IA intégrée (si dispo)."""
    facts = tg_text_session_brief(ses)
    name = (ses or {}).get("name") if isinstance(ses, dict) else None
    if not name:
        slot = _tg_session_slot()
        name = (slot or {}).get("name") or "séance"
    ia = tg_text_brief_ia(name, facts) if want_ia else None
    if ia:
        # On retire le titre redondant de la partie IA : une seule carte.
        body = facts + "\n\n————\n\n🧠 <b>LECTURE IA</b>\n" + \
            re.sub(r"^LECTURE [^\n]*\n(?:\(.*?\)\n)?", "", ia).strip()
        return tg_send(body)
    return tg_send(facts)


def tg_text_macro_facts():
    snap = _tg_cb_snap()
    banks = snap.get("banks") or {}
    names = {"FED": "Fed", "BCE": "BCE", "BOE": "BoE", "BOJ": "BoJ"}
    lines = ["MACRO G4 (Fed · BCE · BoE · BoJ)", ""]
    for k in ("FED", "BCE", "BOE", "BOJ"):
        b = banks.get(k) or {}
        sc = b.get("score")
        try:
            scs = "%+.1f" % float(sc)
        except Exception:
            scs = "—"
        lines.append("%s  %s  %s" % (names[k], scs, b.get("label") or "neutre"))
        if b.get("last_title"):
            lines.append("  %s" % b.get("last_title"))
    direc = None
    try:
        items = (snap.get("index") or {}).get("items") or []
        for it in items:
            if it.get("bank") == "FED" and it.get("kind") == "statement":
                doc = _cb._load_json(os.path.join(_cb.ITEMS_DIR, it["id"] + ".json"), None) if _cb else None
                if doc:
                    direc = (doc.get("llm") or {}).get("plain_fr") or (doc.get("directive") or {}).get("plain_fr")
                break
    except Exception:
        direc = None
    if direc:
        lines.append("")
        lines.append("Lecture Fed :")
        lines.append(str(direc))
    gm = _tg_gold_macro()
    b = (gm.get("biais") or {})
    lines.append("")
    lines.append("Or / dollar : biais %s (fiabilité %s)" % (b.get("etiquette") or "—", b.get("fiabilite") or "—"))
    lines.append("")
    lines.append("Banques G4 seulement. Pas un signal d'entrée.")
    return "\n".join(lines)


def tg_facts_blob():
    parts = [tg_text_macro_facts(), tg_text_gold(), tg_text_cot(), tg_text_cal(), tg_text_news()]
    return "\n\n————\n\n".join(parts)


def tg_text_dossier_local():
    now, hm, jour = _tg_abj_hm()
    head = "DOSSIER OMNITRADE  %s  (Abidjan)\nPC allumé + pont. Analyse factuelle, pas un ordre.\n" % now.strftime("%d/%m/%Y %H:%M")
    return head + "\n" + tg_facts_blob()


def tg_text_dossier_ia():
    facts = tg_facts_blob()
    key = ""
    try:
        key = _groq_key_read()
    except Exception:
        key = ""
    if not key or _cb is None or not hasattr(_cb, "groq_chat"):
        return None
    sys = ""
    _ptxt = _persona_read()
    if _ptxt:
        sys += ("PERSONNALITE CHOISIE PAR LE TRADER — prioritaire sur le ton par "
                "defaut, applique-la :\n%s\n\n" % _ptxt[:1200])
    sys += (
        "Tu es l'analyste macro d'OmniTrade Hub. Francais, ton direct, sans flagornerie. "
        "Redige une VRAIE analyse, 14 a 22 phrases completes, pas une liste de titres. "
        "Groupe : banques centrales G4, or/dollar/taux, COT institutionnel, calendrier High, fil d'actus. "
        "Cite uniquement les faits fournis. Interdit d'inventer un chiffre, Goldman, ou un cours absent. "
        "INTERDIT de citer un niveau de prix/seuil qui n'est pas dans les faits : si le cours "
        "d'un actif est absent, parle direction et biais sans aucun niveau chiffre. "
        "Interdit signal d'entree, interdit 90 %, interdit 'achetez'. "
        "Un % COT est un alignement positionnement, JAMAIS une probabilite ni un taux de reussite. "
        "Titres en MAJUSCULES sur leur ligne. **gras** sur 2 ou 3 chiffres cles. "
        "EMOJIS OBLIGATOIRES : 3 a 6 dans le texte (🇺🇸🇪🇺 banques, 🥇 or, 🛢 petrole, "
        "📅 horaires), jamais qu'en file finale. Pas de think."
    )
    try:
        out, err = _cb.groq_chat(
            key,
            [{"role": "system", "content": sys},
             {"role": "user", "content": facts[:12000]}],
            max_tokens=1400,
            disable_thinking=True,
        )
        if err:
            return None
        txt = (out or {}).get("content") or ""
        txt = re.sub(r"(?is)<think>.*?</think>", " ", txt)
        txt = re.sub(r"(?is)<think>.*", " ", txt).strip()
        if len(txt) < 400:
            return None
        return "ANALYSE MACRO — OmniTrade Hub\n(rédigée à partir des faits du pont, pas d'internet libre)\n\n" + txt
    except Exception:
        return None


def tg_send_dossier(want_ia=True):
    if want_ia:
        ia = tg_text_dossier_ia()
        if ia:
            ok, err = tg_send(ia)
            if ok:
                return True, "ia"
    ok, err = tg_send(tg_text_dossier_local())
    return ok, ("local" if ok else err)


def _tg_poll_chat():
    """Récupère le chat_id si l'utilisateur a écrit /start au bot."""
    res, err = _tg_api("getUpdates", {"offset": _TG.get("offset") or 0, "timeout": 0})
    if err or not isinstance(res, list):
        return None, err or "pas de mise à jour"
    chat_id = None
    last = _TG.get("offset") or 0
    for u in res:
        last = max(last, int(u.get("update_id") or 0) + 1)
        msg = u.get("message") or u.get("edited_message") or {}
        chat = msg.get("chat") or {}
        cid = chat.get("id")
        txt = str(msg.get("text") or "")
        if cid is None:
            continue
        if cid:
            chat_id = str(cid)
        low = txt.strip().lower()
        # n'importe quel message privé lie le chat (pas seulement /start)
        if cid and (msg.get("chat") or {}).get("type") in ("private", None, ""):
            chat_id = str(cid)
        if low.startswith("/start") or low.startswith("/dossier"):
            chat_id = str(cid)
    _TG["offset"] = last
    if chat_id:
        _TG["chat_id"] = chat_id
        _tg_save()
    return chat_id, None


def _tg_strip_think(txt):
    txt = re.sub(r"(?is)<think>.*?</think>", " ", txt or "")
    txt = re.sub(r"(?is)<think>.*", " ", txt)
    txt = re.sub(r"^```(?:\w+)?\s*", "", txt)
    txt = re.sub(r"\s*```$", "", txt)
    return txt.strip()


def tg_text_pair(cle):
    pack, err = _tg_cot_pack(cle)
    a = (_COT_ACTIFS.get(cle) or {})
    sym = a.get("sym") or cle
    lines = ["📌 <b>%s — COT et biais</b>" % _tg_h(sym), ""]
    if not pack:
        lines.append("Pas de lecture CFTC pour %s%s." % (
            _tg_h(sym), (" (%s)" % _tg_h(err)) if err else ""))
        return "\n".join(lines)
    lines.extend(_tg_pack_card(pack))
    hist = pack.get("hist") or []
    if len(hist) >= 2:
        d0, d1 = hist[-1], hist[-2]
        try:
            delta = int(d0.get("net") or 0) - int(d1.get("net") or 0)
            lines.append("Variation vs rapport précédent : %s" % _tg_fmt_int(delta))
            if d0.get("date"):
                lines.append("Rapport CFTC du %s" % _tg_h(d0.get("date")))
        except Exception:
            pass
    lines.append("")
    lines.append("<i>Alignement institutionnel, pas un signal d'entrée. COT hebdo, pas live.</i>")
    return "\n".join(lines)


def tg_text_gold_answer():
    g = tg_text_gold()
    # gold text is still plain-ish; prepend title
    pack, _e = _tg_cot_pack("XAUUSD")
    extra = []
    if pack:
        extra = ["", "📊 <b>COT or</b>"] + _tg_pack_card(pack)
    return "🥇 <b>OR / GOLD</b>\n\n" + g + ("\n".join(extra) if extra else "")


def tg_text_fomc():
    now = datetime.now(timezone.utc)
    lines = ["🏛️ <b>FOMC / Fed</b>", ""]
    snap = _tg_cb_snap()
    f = ((snap.get("banks") or {}).get("FED") or {})
    sc = f.get("score")
    try:
        scs = "%+.1f" % float(sc)
    except Exception:
        scs = "—"
    # Agenda 2026 embarqué — minutes ≠ décision de taux
    nowd = datetime.now(timezone.utc).date()
    decs = [
        (datetime(2026, 9, 16).date(), "15-16 sept. 2026", True),
        (datetime(2026, 10, 7).date(), "7 oct. 2026 (minutes sept.)", False),
        (datetime(2026, 10, 28).date(), "27-28 oct. 2026", False),
        (datetime(2026, 11, 18).date(), "18 nov. 2026 (minutes oct.)", False),
    ]
    nxt = None
    for d, lab, sep in decs:
        if d >= nowd:
            nxt = (d, lab, sep)
            break
    if nxt:
        extra = " + projections/dot plot" if nxt[2] else ""
        lines.append("PROCHAINE DECISION DE TAUX : <b>%s</b> (~14h ET / 18h Abidjan)%s" % (_tg_h(nxt[1]), extra))
    lines.append("Les minutes FOMC ne sont PAS une decision de taux.")
    try:
        ex = ((snap.get("excerpts") or {}).get("FED_minutes") or {})
        if ex.get("excerpt"):
            lines.append("")
            lines.append("<b>Extrait du proces-verbal</b> (%s car.)" % (ex.get("chars") or len(ex.get("excerpt") or "")))
            lines.append(_tg_h(str(ex.get("excerpt") or "")[:1800]))
    except Exception:
        pass
    lines.append("Score lexique Fed : <b>%s</b> · %s" % (_tg_h(scs), _tg_h(f.get("label") or "neutre")))
    if f.get("last_title"):
        lines.append(_tg_h(f.get("last_title")))
    hits = []
    for e in _tg_cal_events():
        title = str(e.get("title") or "")
        blob = (title + " " + str(e.get("country") or "")).lower()
        if not any(x in blob for x in ("fomc", "powell", "minutes")):
            continue
        try:
            d = datetime.fromisoformat(str(e.get("date") or "").replace("Z", "+00:00"))
        except Exception:
            continue
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        mins = (d - now).total_seconds() / 60.0
        if -12 * 60 <= mins <= 24 * 60:
            hits.append((mins, e, d))
    hits.sort(key=lambda x: abs(x[0]))
    if hits:
        lines.append("")
        lines.append("<b>Autour de maintenant</b>")
        for mins, e, d in hits[:6]:
            if mins >= 0:
                when = "dans %s" % _tg_fmt_mins(mins)
            else:
                when = "il y a %s" % _tg_fmt_mins(-mins)
            extra = ""
            if e.get("actual"):
                extra = " · actuel %s" % e.get("actual")
                if e.get("forecast"):
                    extra += " (prévu %s)" % e.get("forecast")
            lines.append("• <b>%s</b> — %s · %s%s" % (
                _tg_h(when), _tg_h(e.get("country") or "USD"),
                _tg_h(e.get("title") or ""), _tg_h(extra)))
    else:
        lines.append("Aucun événement Fed/FOMC dans les prochaines 24 h (cache calendrier).")
    lines.append("")
    lines.append("<i>Lecture de régime, pas un ordre. Les minutes ne sont pas un signal d'entrée.</i>")
    return "\n".join(lines)


def _tg_strip_greet(txt):
    """Coupe le rituel Abidjan / salut — le fond reste."""
    s = (txt or "").strip()
    s = re.sub(
        r"^(salut[^\n.!?]{0,80}[.!?]+\s*)+",
        "",
        s,
        flags=re.I,
    )
    s = re.sub(
        r"^(on est (en pleine nuit|a|à)[^\n.!?]{0,120}[.!?]+\s*)",
        "",
        s,
        flags=re.I,
    )
    s = re.sub(
        r"^(il est \d{1,2}h\d{0,2}[^\n.!?]{0,140}[.!?]+\s*)",
        "",
        s,
        flags=re.I,
    )
    return s.strip()



# Banque interne prop/broker — profils TYPE, pas le contrat live.
_PROP_BANK = [
    {
        "id": 'fundingpips', "kind": 'prop', "name": 'FundingPips',
        "keys": ['fundingpips', 'funding pips', 'funding-pips'],
        'program': '2-step', 'ddDay': 5, 'ddMax': 10, 'split': 80, 'target': 8, 'minDays': 3, 'note': 'Profil type 2-step. SIM = evaluation. 1-step / Pro / Zero : autres limites.'
    },
    {
        "id": 'ftmo', "kind": 'prop', "name": 'FTMO',
        "keys": ['ftmo'],
        'program': 'challenge', 'ddDay': 5, 'ddMax': 10, 'split': 80, 'target': 10, 'minDays': 4, 'note': 'Profil type Challenge : cible 10 %, verif 5 %, 4 jours min, split ~80 %.'
    },
    {
        "id": 'the5ers', "kind": 'prop', "name": 'The5%ers',
        "keys": ['the5ers', 'the 5ers', '5ers', 'the5%ers'],
        'program': 'high-stakes', 'ddDay': 5, 'ddMax': 6, 'split': 80, 'target': 6, 'minDays': 0, 'note': 'Profil type High Stakes. Bootcamp / Hyper Growth : autres chiffres.'
    },
    {
        "id": 'fundednext', "kind": 'prop', "name": 'FundedNext',
        "keys": ['fundednext', 'funded next'],
        'program': 'stellar', 'ddDay': 5, 'ddMax': 10, 'split': 80, 'target': 8, 'minDays': 5, 'note': 'Profil type Stellar 2-step. Express / Stellar Lite : autres limites.'
    },
    {
        "id": 'apex', "kind": 'prop', "name": 'Apex Trader Funding',
        "keys": ['apex trader', 'apextrader', 'apex'],
        'program': 'futures', 'ddDay': 5, 'ddMax': 5, 'split': 90, 'target': 0, 'minDays': 0, 'note': 'Futures, trailing DD type. Split souvent ~90 %. Verifie le palier.'
    },
    {
        "id": 'e8', "kind": 'prop', "name": 'E8 Markets',
        "keys": ['e8 markets', 'e8funding', 'e8-'],
        'program': 'e8', 'ddDay': 4, 'ddMax': 8, 'split': 80, 'target': 8, 'minDays': 1, 'note': 'Profil type E8. Tranche et one-step different.'
    },
    {
        "id": 'alpha', "kind": 'prop', "name": 'Alpha Capital',
        "keys": ['alpha capital', 'alphacapital'],
        'program': '2-step', 'ddDay': 5, 'ddMax': 10, 'split': 80, 'target': 8, 'minDays': 3, 'note': 'Profil type 2-step Alpha Capital.'
    },
    {
        "id": 'goat', "kind": 'prop', "name": 'Goat Funded Trader',
        "keys": ['goat funded', 'goatfunded'],
        'program': '2-step', 'ddDay': 5, 'ddMax': 10, 'split': 80, 'target': 8, 'minDays': 3, 'note': 'Profil type Goat 2-step.'
    },
    {
        "id": 'blueguardian', "kind": 'prop', "name": 'Blue Guardian',
        "keys": ['blue guardian', 'blueguardian'],
        'program': '2-step', 'ddDay': 4, 'ddMax': 8, 'split': 85, 'target': 8, 'minDays': 3, 'note': 'Profil type Blue Guardian.'
    },
    {
        "id": 'myfundedfx', "kind": 'prop', "name": 'MyFundedFX',
        "keys": ['myfundedfx', 'my funded fx'],
        'program': '2-step', 'ddDay': 5, 'ddMax': 10, 'split': 80, 'target': 8, 'minDays': 3, 'note': 'Profil type MyFundedFX.'
    },
    {
        "id": 'topstep', "kind": 'prop', "name": 'Topstep',
        "keys": ['topstep'],
        'program': 'combine', 'ddDay': 0, 'ddMax': 4, 'split': 90, 'target': 0, 'minDays': 0, 'note': 'Futures Combine, trailing. Pas un DD jour Forex type.'
    },
    {
        "id": 'maven', "kind": 'prop', "name": 'Maven Trading',
        "keys": ['maven trading', 'maventrading'],
        'program': 'instant', 'ddDay': 3, 'ddMax': 6, 'split': 80, 'target': 0, 'minDays': 0, 'note': 'Profil type instant Maven.'
    },
    {
        "id": 'instant', "kind": 'prop', "name": 'Instant Funding',
        "keys": ['instant funding', 'instantfunding'],
        'program': 'instant', 'ddDay': 3, 'ddMax': 6, 'split': 80, 'target': 0, 'minDays': 0, 'note': 'Profil type instant.'
    },
    {
        "id": 'cti', "kind": 'prop', "name": 'City Traders Imperium',
        "keys": ['city traders', 'cti '],
        'program': '2-step', 'ddDay': 5, 'ddMax': 10, 'split': 80, 'target': 10, 'minDays': 0, 'note': 'Profil type CTI.'
    },
    {
        "id": 'lark', "kind": 'prop', "name": 'Lark Funding',
        "keys": ['lark funding', 'larkfunding'],
        'program': '2-step', 'ddDay': 5, 'ddMax': 10, 'split': 80, 'target': 8, 'minDays': 3, 'note': 'Profil type Lark.'
    },
    {
        "id": 'seacrest', "kind": 'prop', "name": 'Seacrest Funded',
        "keys": ['seacrest'],
        'program': '2-step', 'ddDay': 5, 'ddMax': 10, 'split': 80, 'target': 8, 'minDays': 3, 'note': 'Profil type Seacrest.'
    },
    {
        "id": 'ftp', "kind": 'prop', "name": 'Funded Trading Plus',
        "keys": ['funded trading plus', 'fundedtradingplus'],
        'program': '2-step', 'ddDay': 5, 'ddMax': 10, 'split': 80, 'target': 10, 'minDays': 0, 'note': 'Profil type FTP.'
    },
    {
        "id": 'sabio', "kind": 'prop', "name": 'SabioTrade',
        "keys": ['sabiotrade', 'sabio trade'],
        'program': '1-step', 'ddDay': 3, 'ddMax': 6, 'split': 80, 'target': 10, 'minDays': 0, 'note': 'Profil type SabioTrade.'
    },
    {
        "id": 'smartprop', "kind": 'prop', "name": 'Smart Prop Trader',
        "keys": ['smart prop'],
        'program': '2-step', 'ddDay': 4, 'ddMax': 8, 'split': 80, 'target': 8, 'minDays': 3, 'note': 'Profil type Smart Prop.'
    },
    {
        "id": 'tradingpit', "kind": 'prop', "name": 'The Trading Pit',
        "keys": ['trading pit', 'thetradingpit'],
        'program': '2-step', 'ddDay': 5, 'ddMax': 10, 'split': 80, 'target': 8, 'minDays': 3, 'note': 'Profil type Trading Pit.'
    },
    {
        "id": 'ment', "kind": 'prop', "name": 'Ment Funding',
        "keys": ['ment funding', 'mentfunding'],
        'program': '2-step', 'ddDay': 5, 'ddMax': 10, 'split': 80, 'target': 10, 'minDays': 3, 'note': 'Profil type Ment.'
    },
    {
        "id": 'nordic', "kind": 'prop', "name": 'Nordic Funder',
        "keys": ['nordic funder', 'nordicfunder'],
        'program': '2-step', 'ddDay': 5, 'ddMax': 10, 'split': 80, 'target': 8, 'minDays': 3, 'note': 'Profil type Nordic Funder.'
    },
    {
        "id": 'breakout', "kind": 'prop', "name": 'Breakout Prop',
        "keys": ['breakout prop', 'breakoutprop'],
        'program': '2-step', 'ddDay': 5, 'ddMax': 10, 'split': 80, 'target': 8, 'minDays': 3, 'note': 'Profil type Breakout.'
    },
    {
        "id": 'hola', "kind": 'prop', "name": 'Hola Prime',
        "keys": ['hola prime', 'holaprime'],
        'program': '2-step', 'ddDay': 4, 'ddMax': 8, 'split': 80, 'target': 8, 'minDays': 3, 'note': 'Profil type Hola Prime.'
    },
    {
        "id": 'tekel', "kind": 'prop', "name": 'Quant Tekel',
        "keys": ['quant tekel', 'tekel'],
        'program': '2-step', 'ddDay': 5, 'ddMax': 10, 'split': 80, 'target': 8, 'minDays': 3, 'note': 'Profil type Quant Tekel.'
    },
    {
        "id": 'tradeify', "kind": 'prop', "name": 'Tradeify',
        "keys": ['tradeify'],
        'program': 'futures', 'ddDay': 0, 'ddMax': 5, 'split': 90, 'target': 0, 'minDays': 0, 'note': 'Futures, trailing type.'
    },
    {
        "id": 'aqua', "kind": 'prop', "name": 'Aqua Funded',
        "keys": ['aqua funded', 'aquafunded'],
        'program': '2-step', 'ddDay': 5, 'ddMax': 10, 'split': 80, 'target': 8, 'minDays': 3, 'note': 'Profil type Aqua Funded.'
    },
    {
        "id": 'thinkcap', "kind": 'prop', "name": 'ThinkCapital',
        "keys": ['thinkcapital', 'think capital'],
        'program': '2-step', 'ddDay': 5, 'ddMax': 10, 'split': 80, 'target': 8, 'minDays': 3, 'note': 'Profil type ThinkCapital.'
    },
    {
        "id": 'blueberry', "kind": 'prop', "name": 'Blueberry Funded',
        "keys": ['blueberry funded', 'blueberryfunded'],
        'program': '2-step', 'ddDay': 5, 'ddMax': 10, 'split': 80, 'target': 8, 'minDays': 3, 'note': 'Profil type Blueberry Funded.'
    },
    {
        "id": 'fortraders', "kind": 'prop', "name": 'For Traders',
        "keys": ['for traders', 'fortraders'],
        'program': '2-step', 'ddDay': 4, 'ddMax': 8, 'split': 80, 'target': 8, 'minDays': 3, 'note': 'Profil type For Traders.'
    },
    {
        "id": 'fxify', "kind": 'prop', "name": 'FXIFY',
        "keys": ['fxify'],
        'program': '2-step', 'ddDay': 5, 'ddMax': 10, 'split': 80, 'target': 8, 'minDays': 3, 'note': 'Profil type FXIFY.'
    },
    {
        "id": 'funderpro', "kind": 'prop', "name": 'FunderPro',
        "keys": ['funderpro', 'funder pro'],
        'program': '2-step', 'ddDay': 5, 'ddMax': 10, 'split': 80, 'target': 8, 'minDays': 3, 'note': 'Profil type FunderPro.'
    },
    {
        "id": 'pipfarm', "kind": 'prop', "name": 'PipFarm',
        "keys": ['pipfarm', 'pip farm'],
        'program': 'consistency', 'ddDay': 3, 'ddMax': 6, 'split': 80, 'target': 12, 'minDays': 0, 'note': 'Profil type PipFarm (consistency).'
    },
    {
        "id": 'tpt', "kind": 'prop', "name": 'Take Profit Trader',
        "keys": ['take profit trader', 'takeprofittrader'],
        'program': 'futures', 'ddDay': 0, 'ddMax': 5, 'split': 80, 'target': 0, 'minDays': 0, 'note': 'Futures, trailing type.'
    },
    {
        "id": 'audacity', "kind": 'prop', "name": 'AudaCity Global',
        "keys": ['audacity', 'audacityglobal'],
        'program': '2-step', 'ddDay': 5, 'ddMax': 10, 'split': 80, 'target': 8, 'minDays': 3, 'note': 'Profil type AudaCity.'
    },
    {
        "id": 'mff', "kind": 'prop', "name": 'MyForexFunds',
        "keys": ['myforexfunds', 'my forex funds'],
        'program': '2-step', 'ddDay': 5, 'ddMax': 12, 'split': 80, 'target': 8, 'minDays': 3, 'note': 'Profil historique MFF. Verifie si le programme existe encore.'
    },
    {
        "id": 'axi', "kind": 'prop', "name": 'Axi Select',
        "keys": ['axi select', 'axiselect'],
        'program': 'select', 'ddDay': 0, 'ddMax': 0, 'split': 0, 'target': 0, 'minDays': 0, 'note': 'Programme Axi (hybrid). Pas un DD 5/10 classique.'
    },
    {
        "id": 'msolutions', "kind": 'prop', "name": 'MSolutions',
        "keys": ['msolutions', 'm solutions'],
        'program': '2-step', 'ddDay': 5, 'ddMax': 10, 'split': 80, 'target': 8, 'minDays': 3, 'note': 'Profil type MSolutions.'
    },
    {
        "id": 'exness', "kind": 'broker', "name": 'Exness',
        "keys": ['exness'],
    },
    {
        "id": 'icmarkets', "kind": 'broker', "name": 'IC Markets',
        "keys": ['ic markets', 'icmarkets', 'raw-trading'],
    },
    {
        "id": 'pepperstone', "kind": 'broker', "name": 'Pepperstone',
        "keys": ['pepperstone'],
    },
    {
        "id": 'xm', "kind": 'broker', "name": 'XM',
        "keys": ['xmglobal', 'xm.com', ' xm'],
    },
    {
        "id": 'deriv', "kind": 'broker', "name": 'Deriv',
        "keys": ['deriv', 'binary.com'],
    },
    {
        "id": 'fxpro', "kind": 'broker', "name": 'FxPro',
        "keys": ['fxpro'],
    },
    {
        "id": 'vantage', "kind": 'broker', "name": 'Vantage',
        "keys": ['vantage'],
    },
    {
        "id": 'tickmill', "kind": 'broker', "name": 'Tickmill',
        "keys": ['tickmill'],
    },
    {
        "id": 'admiral', "kind": 'broker', "name": 'Admiral Markets',
        "keys": ['admiral'],
    },
    {
        "id": 'roboforex', "kind": 'broker', "name": 'RoboForex',
        "keys": ['roboforex', 'roboforex'],
    },
    {
        "id": 'oanda', "kind": 'broker', "name": 'Oanda',
        "keys": ['oanda'],
    },
    {
        "id": 'swissquote', "kind": 'broker', "name": 'Swissquote',
        "keys": ['swissquote'],
    },
    {
        "id": 'ibkr', "kind": 'broker', "name": 'Interactive Brokers',
        "keys": ['interactive brokers', 'ibkr'],
    },
    {
        "id": 'plus500', "kind": 'broker', "name": 'Plus500',
        "keys": ['plus500'],
    },
    {
        "id": 'etoro', "kind": 'broker', "name": 'eToro',
        "keys": ['etoro'],
    },
    {
        "id": 'avatrade', "kind": 'broker', "name": 'AvaTrade',
        "keys": ['avatrade'],
    },
    {
        "id": 'eightcap', "kind": 'broker', "name": 'Eightcap',
        "keys": ['eightcap'],
    },
    {
        "id": 'thinkmarkets', "kind": 'broker', "name": 'ThinkMarkets',
        "keys": ['thinkmarkets'],
    },
    {
        "id": 'blackbull', "kind": 'broker', "name": 'BlackBull',
        "keys": ['blackbull'],
    },
    {
        "id": 'fusion', "kind": 'broker', "name": 'Fusion Markets',
        "keys": ['fusion markets', 'fusionmarkets'],
    },
    {
        "id": 'fbs', "kind": 'broker', "name": 'FBS',
        "keys": ['fbs'],
    },
    {
        "id": 'octa', "kind": 'broker', "name": 'OctaFX',
        "keys": ['octafx', 'octa'],
    },
    {
        "id": 'hfm', "kind": 'broker', "name": 'HFM',
        "keys": ['hfm', 'hotforex'],
    },
    {
        "id": 'alpari', "kind": 'broker', "name": 'Alpari',
        "keys": ['alpari'],
    },
    {
        "id": 'justmarkets', "kind": 'broker', "name": 'JustMarkets',
        "keys": ['justmarkets'],
    },
    {
        "id": 'dukascopy', "kind": 'broker', "name": 'Dukascopy',
        "keys": ['dukascopy'],
    },
    {
        "id": 'saxo', "kind": 'broker', "name": 'Saxo Bank',
        "keys": ['saxo'],
    },
    {
        "id": 'weltrade', "kind": 'broker', "name": 'Weltrade',
        "keys": ['weltrade'],
    },
    {
        "id": 'insta', "kind": 'broker', "name": 'InstaForex',
        "keys": ['instaforex'],
    },
    {
        "id": 'errante', "kind": 'broker', "name": 'Errante',
        "keys": ['errante'],
    },
    {
        "id": 'globalprime', "kind": 'broker', "name": 'Global Prime',
        "keys": ['global prime'],
    },
    {
        "id": 'skilling', "kind": 'broker', "name": 'Skilling',
        "keys": ['skilling'],
    },
    {
        "id": 'capitalcom', "kind": 'broker', "name": 'Capital.com',
        "keys": ['capital.com', 'capitalcom'],
    },
    {
        "id": 't212', "kind": 'broker', "name": 'Trading 212',
        "keys": ['trading 212', 'trading212'],
    },
]


def _prop_lookup(hay: str):
    hay = (hay or "").lower()
    best, best_len = None, 0
    for f in _PROP_BANK:
        for key in f.get("keys") or []:
            k = key.lower()
            if hay.find(k) >= 0 and len(k) > best_len:
                best, best_len = f, len(k)
    return best


def _prop_line_account():
    """Une ligne courte pour Telegram / Groq."""
    try:
        a = get_account() or {}
    except Exception:
        a = {}
    hay = " ".join(str(a.get(k) or "") for k in ("server", "company", "broker", "name"))
    hit = _prop_lookup(hay)
    name = (hit or {}).get("name") or (a.get("company") or a.get("server") or "compte")
    kind = (hit or {}).get("kind") or "compte"
    phase = "compte"
    s = hay.lower()
    if any(x in s for x in ("sim", "trial", "chall", "eval", "phase", "step")):
        phase = "evaluation"
    if any(x in s for x in ("master", "funded", "live", "real")) and "sim" not in s:
        phase = "financement"
    cap = a.get("balance") or a.get("equity") or 0
    bits = ["PROP: %s (%s%s) phase %s" % (
        name, kind, (" " + hit["program"]) if hit and hit.get("program") else "", phase)]
    try:
        if cap:
            bits.append("capital $%s" % int(round(float(cap))))
    except Exception:
        pass
    if hit and hit.get("kind") == "prop":
        bits.append("DD %s/%s split ~%s cible ~%s" % (
            hit.get("ddDay") or "—", hit.get("ddMax") or "—",
            hit.get("split") or "—", hit.get("target") or "—"))
        bits.append("profil TYPE banque interne, pas le contrat")
    return " ".join(str(x) for x in bits)


def _tg_etat_macro():
    """Digest court type ETAT APP pour Groq."""
    bits = []
    try:
        gm = _tg_gold_macro() or {}
        c = gm.get("cours") or {}
        b = gm.get("biais") or {}
        def px(k):
            x = c.get(k) or {}
            if x.get("prix") is None:
                return "—"
            s = str(x.get("prix"))
            v = x.get("varPct")
            if v is not None:
                s += " (%+.2f%%)" % float(v)
            return s
        bits.append("XAU %s DXY %s biais %s" % (px("xau"), px("dxy"), b.get("etiquette") or "—"))
    except Exception:
        pass
    try:
        banks = (_tg_cb_snap().get("banks") or {})
        g4 = []
        for k in ("FED", "BCE", "BOE", "BOJ"):
            bb = banks.get(k) or {}
            try:
                scs = "%+.1f" % float(bb.get("score"))
            except Exception:
                scs = "—"
            g4.append("%s %s %s" % (k, scs, bb.get("label") or ""))
        if g4:
            bits.append("G4 " + " · ".join(g4))
    except Exception:
        pass
    try:
        now = datetime.now(timezone.utc)
        soon, fell = [], []
        for e in _tg_cal_events():
            if (e.get("impact") or "") != "High":
                continue
            try:
                d = datetime.fromisoformat(str(e.get("date") or "").replace("Z", "+00:00"))
            except Exception:
                continue
            if d.tzinfo is None:
                d = d.replace(tzinfo=timezone.utc)
            sec = (d - now).total_seconds()
            lab = "%s %s" % (e.get("country") or "", e.get("title") or "")
            if 0 < sec <= 18 * 3600:
                soon.append(lab)
            elif -12 * 3600 <= sec <= 0 and e.get("actual"):
                fell.append("%s actuel %s prevu %s" % (lab, e.get("actual"), e.get("forecast") or "—"))
        if fell:
            bits.append("TOMBES: " + " · ".join(fell[:4]))
        if soon:
            bits.append("HIGH 18h: " + " · ".join(soon[:4]))
    except Exception:
        pass
    try:
        news = _tg_news_fx(5)
        if news:
            bits.append("NEWS: " + " / ".join((n.get("title") or n.get("titre") or "")[:90] for n in news[:5]))
    except Exception:
        pass
    try:
        pl = _prop_line_account()
        if pl:
            bits.append(pl)
    except Exception:
        pass
    return " | ".join(bits)[:1400]


def _persona_path():
    """Fichier de personnalité de l'agent IA, partagé app <-> pont Telegram."""
    try:
        return os.path.join(user_data_dir(), "agent_persona.txt")
    except Exception:
        return os.path.join(os.path.expanduser("~"), "agent_persona.txt")


@app.get("/api/agent/persona")
def api_agent_persona_get():
    txt = ""
    try:
        p = _persona_path()
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8") as f:
                txt = (f.read() or "").strip()
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:120], "persona": ""})
    return jsonify({"ok": True, "persona": txt[:2000]})


@app.post("/api/agent/persona")
def api_agent_persona_set():
    data = request.get_json(silent=True) or {}
    txt = str(data.get("persona") or "").strip()
    if len(txt) > 2000:
        txt = txt[:2000]
    try:
        os.makedirs(user_data_dir(), exist_ok=True)
        with open(_persona_path(), "w", encoding="utf-8") as f:
            f.write(txt)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:120]}), 500
    return jsonify({"ok": True, "persona": txt})


def _tg_ask_llm(question, facts):
    """Meme ton que Psycho & IA dans l'app. Renvoie (texte, erreur)."""
    key = ""
    ork = ""
    gem = ""
    cer = ""
    try:
        key = _groq_key_read() or ""
    except Exception:
        key = ""
    try:
        ork = _or_key_read() or ""
    except Exception:
        ork = ""
    try:
        gem = _gemini_key_read() or ""
    except Exception:
        gem = ""
    try:
        cer = _cerebras_key_read() or ""
    except Exception:
        cer = ""
    mi = ""
    nv = ""
    try:
        mi = _mistral_key_read() or ""
    except Exception:
        mi = ""
    try:
        nv = _nvidia_key_read() or ""
    except Exception:
        nv = ""
    if not key and not ork and not gem and not cer and not mi and not nv:
        return None, "cle absente"
    if _cb is None:
        return None, "module IA absent"
    sys = (
        "Tu es le coach d'OmniTrade Hub, EXACTEMENT le meme ton que la page Psycho et IA. "
        "Francais parle, 5 a 9 phrases COMPLETES. Analyse, pas ceremonie. "
        "INTERDIT: Salut le trader, Salut le pote, On est a Abidjan, il est XXh a Abidjan, "
        "Sydney et Tokyo qui tournent, recap sessions, 'on est en pleine nuit calme'. "
        "Sauf si on DEMANDE l'heure ou la session : commence DIRECTEMENT par le fond. "
        "Ne repete pas ce que tu viens de dire. Reponds A LA QUESTION POSEE, pas a une autre. "
        "Si on demande les NEWS : chiffres tombes + fil FX/macro. "
        "INTERDIT les actions individuelles (Hynix, Zip Co, Service Stream). "
        "INTERDIT de dump le COT si on n'a pas demande le biais. "
        "Si on demande une synthese GLOBALE du marche : sessions, chiffres, news FX, or/DXY, puis COT en une phrase. "
        "Mets 2 ou 3 emojis DANS les phrases, colles au pays ou au chiffre "
        "(ex: 🇦🇺 a cote de l'AUD, 🇯🇵 a cote du Japon, 📅 pour un horaire). "
        "Le message DOIT avoir des emojis. INTERDIT de les mettre uniquement a la fin en signature. "
        "Pas de liste a puces, pas de titres EN MAJUSCULES, pas de fiche. "
        "Un % COT = alignement, pas un taux de reussite. "
        "Minutes FOMC != decision de taux. Interdit signal d'entree, 90 %, achetez. "
        "Interdit d'envoyer sur ForexFactory si la date est dans les faits. "
        "Si tu cites un chiffre, il doit etre dans les faits. "
        "Section TOMBES / deja sortis = NE DIS PAS on publie. "
        "Section A VENIR = pas encore sorti. Ne les melange pas. "
        "N'invente pas un biais COT si BIAIS n'est pas dans les faits. "
        "Pas de think. Pas de HTML."
    )
    # Garde-fous anti-hallucination (A) : prix inventés & « probabilité ».
    sys += (
        " REGLES ABSOLUES : "
        "INTERDIT de citer un niveau de prix, seuil ou objectif (ex 2 300 $) qui "
        "n'est PAS dans les faits fournis ; si le cours actuel d'un actif est absent "
        "des faits, parle direction et biais sans AUCUN niveau chiffre. "
        "La confluence COT est un ALIGNEMENT de positionnement : INTERDIT les mots "
        "probabilite, chance ou taux de reussite pour la decrire. "
        "N'invente jamais un chiffre de positionnement net absent des faits. "
        "STYLE : mets en **gras** 1 a 3 chiffres cles, reste vivant et concret. "
        "EMOJIS OBLIGATOIRES : 3 a 6 par message, places DANS les phrases, colles "
        "au pays ou au chiffre (ex 🇺🇸 pour l'USD, 🥇 pour l'or, 📅 devant un "
        "horaire, 🛢 pour le petrole). Jamais une file d'emojis seule a la fin."
    )
    # Personnalité choisie par le trader dans Psycho & IA (agent_persona.txt,
    # écrite par /api/agent/persona). Prioritaire sur le ton par défaut.
    try:
        _pp = _persona_path()
        if os.path.exists(_pp):
            with open(_pp, "r", encoding="utf-8") as _pf:
                _ptxt = (_pf.read() or "").strip()
            if _ptxt:
                sys = (
                    "PERSONNALITE CHOISIE PAR LE TRADER — prioritaire sur le ton par "
                    "defaut ci-dessous, applique-la (ton, longueur, emojis, tutoiement) :\n"
                    + _ptxt[:1200] + "\n\n" + sys
                )
    except Exception:
        pass
    # faits sans balises pour que le modele ecrive propre
    clean = re.sub(r"<[^>]+>", " ", facts or "")
    clean = re.sub(r"\s+", " ", clean).strip()[:1400]
    user = "Question du trader : %s\n\nETAT APP (ne recopie pas en liste) : %s" % (question, clean)
    msgs = [{"role": "system", "content": sys}, {"role": "user", "content": user}]
    last = ""
    # Priorité au meilleur modèle récent de la clé Groq (gpt-oss-120b),
    # puis qwen3.6 ; en dernier recours la chaîne multi-fournisseurs.
    if key and _cb is not None and hasattr(_cb, "groq_chat"):
        for mdl in ("openai/gpt-oss-120b", "qwen/qwen3.6-27b"):
            try:
                out, err = _cb.groq_chat(
                    key, msgs, model=mdl, max_tokens=800,
                    disable_thinking=True, strict_model=False,
                )
                txt = _tg_strip_think(((out or {}).get("content") or ""))
                txt = _tg_strip_greet(txt)
                if txt and len(txt) >= 25:
                    return txt, None
                if err:
                    last = str(err)[:160]
            except Exception as e:
                last = str(e)[:160]
    if hasattr(_cb, "llm_chat"):
        try:
            out, err = _cb.llm_chat(
                key, msgs, max_tokens=800, disable_thinking=True,
                or_key=ork, gemini_key=gem, cerebras_key=cer,
                mistral_key=mi, nvidia_key=nv,
            )
            last = err or ""
            txt = _tg_strip_think(((out or {}).get("content") or ""))
            txt = _tg_strip_greet(txt)
            if txt and len(txt) >= 25:
                return txt, None
            if not last:
                last = "reponse trop courte"
        except Exception as e:
            last = str(e)[:160]
        return None, (last or "IA muette")
    return None, "module IA absent"


def _tg_msg_ia(txt):
    """Un message de chat, pas une fiche."""
    s = (txt or "").strip()
    s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
    # eviter le double escape : on escape puis on remet b/i
    s = _tg_h(s)
    s = s.replace("&lt;b&gt;", "<b>").replace("&lt;/b&gt;", "</b>")
    s = s.replace("&lt;i&gt;", "<i>").replace("&lt;/i&gt;", "</i>")
    return s




def tg_local_answer(q):

    """Réponse factuelle ciblée — même sans Groq."""
    topic, cle = _tg_detect_topic(q)
    now, _hm, _j = _tg_abj_hm()
    head = "🧠 <b>Psycho et IA</b>  ·  %s\n" % now.strftime("%d/%m %H:%M")
    if topic == "gold":
        body = tg_text_gold_answer()
    elif topic == "pair" and cle:
        body = tg_text_pair(cle)
    elif topic == "session":
        body = "🕐 <b>Sessions</b>\n" + _tg_h(_tg_sessions_line())
    elif topic == "fed":
        body = tg_text_fomc()
    elif topic == "cal":
        body = tg_text_cal()
    elif topic == "news":
        body = tg_text_news()
    else:
        body = tg_text_radar()
    return head + "\n" + body


def tg_handle_chat(question):
    """Meme conversation que Psycho et IA — un message, pas un rapport."""
    q = (question or "").strip()
    if len(q) < 2:
        return False, "vide"
    low = q.lower()
    if low in ("ok", "merci", "thanks", "thx", "top", "parfait", "daccord", "d'accord"):
        tg_send("OK.")
        return True, "ack"
    if _TG.get("busy"):
        tg_send("J'arrive, je finis la phrase d'avant.")
        return False, "busy"
    _TG["busy"] = True
    try:
        _tg_typing()
        now, _hm, _j = _tg_abj_hm()
        topic, cle = _tg_detect_topic(q)
        follow = _tg_is_follow(q)
        if follow and _TG.get("last_topic"):
            topic = _TG.get("last_topic") or topic
            cle = _TG.get("last_cle") if _TG.get("last_cle") is not None else cle
        else:
            _TG["last_topic"] = topic
            _TG["last_cle"] = cle
            _tg_save()
        lowq = q.lower()
        bits = []
        if follow:
            bits.append("SUITE du sujet %s. Interprete CE sujet. Interdit de changer de module (pas de dump COT si le sujet est le calendrier)." % (topic or ""))
        if topic == "gold":
            bits.append(tg_text_gold()[:1800])
        elif topic == "pair" and cle:
            bits.append(tg_text_pair(cle))
        elif topic == "fed":
            bits.append(tg_text_fomc())
        elif topic == "session":
            bits.append(_tg_sessions_line())
        elif topic == "brief":
            bits.append(_tg_etat_macro())
        elif topic == "news":
            bits.append(tg_text_cal()[:800])
            bits.append(tg_text_news()[:1200])
        else:
            if topic == "cal" or re.search(r"calendrier|high|nfp|chiffre", lowq):
                bits.append(tg_text_cal())
            if re.search(r"news|actu|depeche|market\s*hub", lowq):
                bits.append(tg_text_news()[:1200])
            if re.search(r"biais|paire|cot|confluence", lowq) or topic == "radar":
                try:
                    bits.append(tg_text_cot()[:1100])
                except Exception:
                    bits.append(tg_text_radar()[:1100])
            if not bits:
                bits.append(tg_text_radar())
        want_clock = (topic == "session") or bool(re.search(r"\b(heure|session|seance|séance)\b", lowq))
        head = ""
        if want_clock:
            head = "Heure Abidjan %s. Sessions : %s.\n" % (
                now.strftime("%d/%m %H:%M"), _tg_sessions_line())
        facts = head + "\n".join(bits)
        txt, err_ia = _tg_ask_llm(q, facts)
        if txt:
            ok, err = tg_send(_tg_msg_ia(txt))
            return ok, err
        etat = _tg_etat_macro()
        txt2, err2 = _tg_ask_llm(q, etat)
        if txt2:
            ok, err = tg_send(_tg_msg_ia(txt2))
            return ok, err
        why = str(err_ia or err2 or "IA muette")
        msg = "Aucune IA n a repondu. " + why[:180]
        ok, err = tg_send(msg)
        return ok, err
    except Exception as e:
        log.warning("tg chat : %s", e)
        return False, str(e)[:120]
    finally:
        _TG["busy"] = False


def _tg_cmd_loop():
    """Commandes + questions libres (Psycho & IA)."""
    if not _TG.get("token"):
        return
    res, err = _tg_api("getUpdates", {"offset": _TG.get("offset") or 0, "timeout": 0})
    if err or not isinstance(res, list):
        return
    last = _TG.get("offset") or 0
    for u in res:
        last = max(last, int(u.get("update_id") or 0) + 1)
        msg = u.get("message") or {}
        chat = msg.get("chat") or {}
        cid = chat.get("id")
        raw = str(msg.get("text") or "").strip()
        txt = raw.lower()
        if txt.startswith("/"):
            txt = txt.split("@", 1)[0].split("\x00", 1)[0]
        if cid and not _TG.get("chat_id"):
            _TG["chat_id"] = str(cid)
        if not raw or not cid:
            continue
        if str(cid) != str(_TG.get("chat_id")):
            continue
        _tg_log("in", raw, {"cmd": (txt if txt.startswith("/") else "")})
        if txt.startswith("/start") or txt.startswith("/aide") or txt.startswith("/help"):
            tg_send(_TG_CMD_HELP)
        elif txt.startswith("/dossier"):
            tg_send_dossier(True)
        elif txt.startswith("/macro"):
            # /macro = banques centrales G4 uniquement (carte factuelle) ;
            # l'analyse IA globale reste sur /dossier.
            tg_send(tg_text_macro_facts())
        elif txt.startswith("/or") or txt.startswith("/gold"):
            tg_send(tg_text_gold())
        elif txt.startswith("/cot"):
            tg_send(tg_text_cot())
        elif txt.startswith("/cal"):
            tg_send(tg_text_cal())
        elif txt.startswith("/news"):
            tg_send(tg_text_news())
        elif txt.startswith("/radar"):
            tg_send(tg_text_radar())
        elif txt.startswith("/biais") or txt.startswith("/var") or txt.startswith("/variation"):
            tg_send(tg_text_biais_var())
        elif txt.startswith("/sydney") or txt.startswith("/syd"):
            tg_send_session_brief(_tg_ses_by_id("sydney"), True)
        elif txt.startswith("/tokyo") or txt.startswith("/tyo"):
            tg_send_session_brief(_tg_ses_by_id("tokyo"), True)
        elif txt.startswith("/londres") or txt.startswith("/london") or txt.startswith("/lon"):
            tg_send_session_brief(_tg_ses_by_id("london"), True)
        elif txt.startswith("/ny") or txt.startswith("/newyork") or txt.startswith("/new york"):
            tg_send_session_brief(_tg_ses_by_id("ny"), True)
        elif txt.startswith("/brief"):
            rest = txt.split(None, 1)
            sid = rest[1].strip() if len(rest) > 1 else ""
            ses = _tg_ses_by_id(sid) if sid else None
            tg_send_session_brief(ses, True)
        elif txt.startswith("/test"):
            tg_send("TEST\nLe pont tourne. Briefs auto : Sydney, Tokyo, Londres, New York.\nÉcrivez une question comme dans Psycho & IA.")
        elif not txt.startswith("/"):
            tg_handle_chat(raw)
    _TG["offset"] = last
    _tg_save()


def _tg_watch_once():
    if not _tg_ready():
        return
    now, hm, jour = _tg_abj_hm()

    # High : rappel toutes les 5 min dans la fenêtre T-15, puis un ping à l'heure.
    # Plusieurs High à la même heure = UN message (pas de ping-pong A/B).
    try:
        highs = _TG["seen"].setdefault("highs", {})
        if not isinstance(highs, dict):
            highs = {}
            _TG["seen"]["highs"] = highs
        now_ts = time.time()
        # ménage : vieux tickets (> 6 h)
        for k, rec in list(highs.items()):
            last = 0
            if isinstance(rec, dict):
                try:
                    last = float(rec.get("last") or 0)
                except Exception:
                    last = 0
            if last and now_ts - last > 6 * 3600:
                highs.pop(k, None)
        soon, live, pre60 = [], [], []
        now_utc = datetime.now(timezone.utc)
        muted, mute_name = _tg_muted_now()
        for e in _tg_cal_events():
            if (e.get("impact") or "") != "High":
                continue
            try:
                d = datetime.fromisoformat(str(e.get("date") or "").replace("Z", "+00:00"))
            except Exception:
                continue
            if d.tzinfo is None:
                d = d.replace(tzinfo=timezone.utc)
            left = (d - now_utc).total_seconds()
            key = "%s|%s|%s" % (e.get("date"), e.get("country"), e.get("title"))
            rec = highs.get(key)
            if isinstance(rec, dict) and "last" in rec:
                pass
            elif rec:
                rec = {"last": now_ts, "now": 0}
                highs[key] = rec
            else:
                rec = {"last": 0, "now": 0}
                highs[key] = rec
            try:
                last = float(rec.get("last") or 0)
            except Exception:
                last = 0
            if left > 75 * 60:
                continue
            if 45 * 60 <= left <= 75 * 60:
                if not rec.get("h60"):
                    rec["h60"] = 1
                    rec["last"] = now_ts
                    pre60.append((left, e))
                continue
            if left > 15 * 60:
                continue
            if left > 0:
                if last and (now_ts - last) < 5 * 60:
                    continue
                rec["last"] = now_ts
                rec["now"] = 0
                soon.append((left, e))
            elif left > -90:
                if rec.get("now"):
                    continue
                rec["now"] = 1
                rec["last"] = now_ts
                live.append((left, e))
            elif left > -20 * 60:
                pass  # fenêtre après print, gérée plus bas
            else:
                highs.pop(key, None)
        printed = []
        for e in _tg_cal_events():
            if (e.get("impact") or "") != "High":
                continue
            try:
                d = datetime.fromisoformat(str(e.get("date") or "").replace("Z", "+00:00"))
            except Exception:
                continue
            if d.tzinfo is None:
                d = d.replace(tzinfo=timezone.utc)
            left = (d - now_utc).total_seconds()
            if left > -120 or left < -20 * 60:
                continue
            if not (e.get("actual") or "").strip():
                continue
            key = "%s|%s|%s" % (e.get("date"), e.get("country"), e.get("title"))
            rec = highs.get(key)
            if not isinstance(rec, dict):
                rec = {"last": now_ts, "now": 1, "print": 0}
                highs[key] = rec
            if rec.get("print"):
                continue
            rec["print"] = 1
            rec["last"] = now_ts
            printed.append(e)
        changed = bool(soon or live or printed or pre60)
        if muted:
            soon, live, pre60, printed = [], [], [], []
        if pre60:
            pre60.sort(key=lambda x: x[0])
            lines = ["HIGH dans ~1 h"]
            for left, e in pre60:
                mins = max(1, int(left // 60))
                lines.append("dans %s min — %s  %s" % (
                    mins, e.get("country") or "", e.get("title") or ""))
            lines.append("Spreads. Ce n'est pas une entrée.")
            tg_send("\n".join(lines))
        if soon:
            soon.sort(key=lambda x: x[0])
            _TG["seen"]["high"] = "%s|%s|%s" % (
                soon[0][1].get("date"), soon[0][1].get("country"), soon[0][1].get("title"))
            lines = ["HIGH à venir"]
            for left, e in soon:
                mins = max(1, int(left // 60))
                lines.append("dans %s min — %s  %s" % (
                    mins, e.get("country") or "", e.get("title") or ""))
            lines.append(_tg_fmt_when(soon[0][1].get("date")))
            lines.append("Ce n'est pas une entrée.")
            tg_send("\n".join(lines))
        if live:
            live.sort(key=lambda x: x[0], reverse=True)
            _TG["seen"]["high"] = "%s|%s|%s" % (
                live[0][1].get("date"), live[0][1].get("country"), live[0][1].get("title"))
            lines = ["HIGH maintenant"]
            for _left, e in live:
                lines.append("%s  %s" % (e.get("country") or "", e.get("title") or ""))
            lines.append(_tg_fmt_when(live[0][1].get("date")))
            lines.append("Ce n'est pas une entrée.")
            tg_send("\n".join(lines))
        if printed:
            lines = ["CHIFFRE SORTI"]
            for e in printed:
                bit = "%s  %s  actuel %s" % (
                    e.get("country") or "", e.get("title") or "", e.get("actual"))
                if e.get("forecast"):
                    bit += " (prévu %s)" % e.get("forecast")
                lines.append(bit)
            lines.append("Ce n'est pas une entrée.")
            tg_send("\n".join(lines))
        # taille 0,5 %
        try:
            need_t, why = False, []
            if soon:
                need_t = True
                why.append("High dans %s min" % max(1, int(soon[0][0] // 60)))
            for p in _tg_cot_scan()[:3]:
                b = (p.get("b") or {}) if isinstance(p, dict) else {}
                idx = b.get("cotIndex")
                if idx is not None and (idx >= 85 or idx <= 15):
                    need_t = True
                    why.append("%s COT extrême" % ((p.get("a") or {}).get("sym") or p.get("cle") or ""))
            jour_k = jour + "|taille"
            if need_t and _TG["seen"].get("taille") != jour_k:
                _TG["seen"]["taille"] = jour_k
                tg_send("TAILLE 0,5 %%\n%s\nRisque 0,5 %% du capital. Ce n'est pas une entrée." % " · ".join(why))
                changed = True
        except Exception:
            pass
        if changed:
            _tg_save()
    except Exception as e:
        log.debug("tg high : %s", e)

    # Flash or
    try:
        for n in _tg_gold_news()[:8]:
            if n.get("urgence") != "flash":
                continue
            key = re.sub(r"\W+", "", str(n.get("titre") or n.get("title") or "").lower())[:80]
            if key and _TG["seen"].get("gold") != key:
                _TG["seen"]["gold"] = key
                _tg_save()
                tg_send("FLASH OR\n%s — %s\n%s" % (
                    n.get("source") or "", n.get("titre") or n.get("title") or "",
                    (n.get("resume") or "")[:400]))
                break
    except Exception as e:
        log.debug("tg gold : %s", e)

    # Communiqué G4
    try:
        items = ((_tg_cb_snap().get("index") or {}).get("items") or [])
        for it in items:
            if it.get("kind") not in ("statement", "minutes", "press"):
                continue
            key = str(it.get("id") or it.get("url") or it.get("title") or "")
            if key and _TG["seen"].get("cb") != key:
                if _TG["seen"].get("cb"):
                    kinds = {"statement": "communique", "minutes": "compte rendu (PAS une decision de taux)",
                             "press": "communique", "outlook": "perspectives"}
                    titre = it.get("title_fr") or it.get("title") or ""
                    if _cb is not None and hasattr(_cb, "title_fr") and not it.get("title_fr"):
                        try:
                            titre = _cb.title_fr(it.get("title") or "")
                        except Exception:
                            pass
                    sc = it.get("score")
                    try:
                        scs = "%+.1f" % float(sc)
                    except Exception:
                        scs = "—"
                    msg = (
                        "🏦 <b>G4 · %s</b>\n"
                        "%s\n"
                        "%s\n"
                        "Score lexique : <b>%s</b> %s\n"
                    ) % (
                        _tg_h(it.get("bank") or ""),
                        _tg_h(kinds.get(it.get("kind"), it.get("kind") or "")),
                        _tg_h(titre),
                        _tg_h(scs),
                        _tg_h(it.get("label") or ""),
                    )
                    if (it.get("kind") == "minutes"):
                        msg += "\n<i>Les minutes commentent la reunion precedente. Ce n'est pas un vote de taux.</i>"
                    if abs(float(sc or 0)) < 0.05:
                        msg += "\n<i>Score 0 : le corps du texte n'etait pas encore lu. Le pont reessaie tout seul.</i>"
                    tg_send(msg)
                _TG["seen"]["cb"] = key
                _tg_save()
            break
    except Exception as e:
        log.debug("tg cb : %s", e)

    # Brief de CHAQUE session : 10 min avant → 20 min après l'ouverture locale.
    slot = _tg_session_slot(now)
    if slot and _TG.get("slot") != slot["key"]:
        _TG["slot"] = slot["key"]
        _tg_save()
        ses = _tg_ses_by_id(slot["id"])
        tg_send("BRIEF %s — préparation (radar + variation des biais)…" % slot["name"].upper())
        tg_send_session_brief(ses, True)

    # Soir 18:00–18:25
    if 18 * 60 <= hm <= 18 * 60 + 25 and _TG.get("evening") != jour:
        _TG["evening"] = jour
        _tg_save()
        tg_send("SYNTHÈSE DE FIN DE SÉANCE")
        tg_send(tg_text_cal())
        tg_send(tg_text_gold())


def _tg_thread():
    _tg_load()
    # premier passage : mémoriser sans spammer
    try:
        items = ((_tg_cb_snap().get("index") or {}).get("items") or [])
        for it in items:
            if it.get("kind") in ("statement", "minutes", "press"):
                _TG["seen"]["cb"] = str(it.get("id") or it.get("url") or "")
                break
        for n in _tg_gold_news()[:5]:
            if n.get("urgence") == "flash":
                _TG["seen"]["gold"] = re.sub(r"\W+", "", str(n.get("titre") or "").lower())[:80]
                break
        _tg_save()
    except Exception:
        pass
    while True:
        try:
            if _TG.get("token"):
                _tg_cmd_loop()
            if _tg_ready():
                _tg_watch_once()
        except Exception as e:
            log.warning("tg watch : %s", e)
        time.sleep(25)


def tg_start():
    try:
        _tg_load()
        if _TG.get("token"):
            _tg_set_commands()
    except Exception:
        pass
    t = threading.Thread(target=_tg_thread, daemon=True, name="tg-watch")
    t.start()


@app.get("/api/tg/status")
def api_tg_status():
    _tg_load()
    tok = _TG.get("token") or ""
    return jsonify({
        "ok": True,
        "on": bool(_TG.get("on")),
        "configured": bool(tok and _TG.get("chat_id")),
        "has_token": bool(tok),
        "chat_id": _TG.get("chat_id") or "",
        "tail": ("…" + tok[-4:]) if len(tok) > 4 else "",
        "bot": _TG.get("bot_name") or "",
        "last_ok": _TG.get("last_ok") or 0,
        "last_err": _TG.get("last_err") or "",
        "ia_groq": bool(_groq_key_read()),
        "ia_or": bool(_or_key_read()),
        "ia_gemini": bool(_gemini_key_read()),
        "ia_cerebras": bool(_cerebras_key_read()),
        "ia_mistral": bool(_mistral_key_read()),
        "ia_nvidia": bool(_nvidia_key_read()),
        "ia_tail": (("…" + (_groq_key_read() or "")[-4:]) if _groq_key_read() else ""),
    })


@app.post("/api/tg/config")
def api_tg_config():
    data = request.get_json(silent=True) or {}
    if "token" in data:
        _TG["token"] = _tg_clean_token(data.get("token") or "")
        _TG["last_err"] = ""
    if "chat_id" in data:
        _TG["chat_id"] = str(data.get("chat_id") or "").strip()
    if "on" in data:
        _TG["on"] = bool(data.get("on"))
    me, err = (None, "token vide")
    if _TG.get("token"):
        me, err = _tg_api("getMe")
    if err or not me:
        _TG["last_err"] = err or "jeton refusé par Telegram"
        _tg_save()
        st = api_tg_status().get_json()
        st["ok"] = False
        st["error"] = _TG["last_err"]
        return jsonify(st), 400
    _TG["bot_name"] = (me.get("username") or me.get("first_name") or "")
    if _TG["token"] and not _TG["chat_id"]:
        cid, _e = _tg_poll_chat()
        if cid:
            _TG["chat_id"] = cid
    if _TG["token"] and _TG["chat_id"]:
        _TG["on"] = True
        _tg_save()
        tg_send(
            "OmniTrade Hub connecté au pont de ce PC.\n"
            + _TG_CMD_HELP,
            force=True,
        )
    else:
        _TG["last_err"] = "Jeton bon. Ouvrez t.me/%s et envoyez /start, puis Test." % (
            _TG.get("bot_name") or "votre_bot")
    _tg_save()
    return api_tg_status()


@app.post("/api/tg/test")
def api_tg_test():
    _tg_load()
    if not _TG.get("token"):
        return jsonify({"ok": False, "error": "collez le jeton dans Support (app locale, pas ce chat)"}), 400
    me, err = _tg_api("getMe")
    if err or not me:
        return jsonify({"ok": False, "error": "Jeton refusé : %s" % (err or "getMe")}), 400
    if not _TG.get("chat_id"):
        cid, err2 = _tg_poll_chat()
        if cid:
            _TG["chat_id"] = cid
            _TG["on"] = True
            _tg_save()
        else:
            uname = me.get("username") or "votre_bot"
            return jsonify({
                "ok": False,
                "error": "Jeton bon. Ouvrez t.me/%s , envoyez /start, puis Test. %s" % (
                    uname, err2 or ""),
            }), 400
    g = bool(_groq_key_read())
    o = bool(_or_key_read())
    ge = bool(_gemini_key_read())
    ce = bool(_cerebras_key_read())
    mi = bool(_mistral_key_read())
    nv = bool(_nvidia_key_read())
    ping = []
    if _cb is not None and hasattr(_cb, "llm_ping"):
        try:
            ping = _cb.llm_ping(
                gemini_key=_gemini_key_read(),
                cerebras_key=_cerebras_key_read(),
                groq_key=_groq_key_read(),
                or_key=_or_key_read(),
                mistral_key=_mistral_key_read(),
                nvidia_key=_nvidia_key_read(),
            ) or []
        except Exception as e:
            ping = ["ping KO " + str(e)[:60]]
    ia = "Cles : " + " · ".join(
        n for n, ok in (("Groq", g), ("OpenRouter", o), ("Gemini", ge), ("Mistral", mi), ("NVIDIA", nv), ("Cerebras", ce)) if ok
    ) or "aucune"
    live = "\n".join(ping) if ping else "ping non dispo"
    ok, err = tg_send(
        "TEST\nLe pont tourne.\n%s\nReponse reelle :\n%s\nEcrivez une question.\nPas un signal." % (ia, live),
        force=True,
    )
    return jsonify({"ok": ok, "error": err or "", "chat_id": _TG.get("chat_id"),
                    "ia_groq": g, "ia_or": o, "ia_gemini": ge, "ia_cerebras": ce})


@app.post("/api/tg/send")
def api_tg_send():
    _tg_load()
    data = request.get_json(silent=True) or {}
    kind = str(data.get("kind") or "dossier").lower()
    if kind == "raw":
        # Texte libre fourni par l'app (ex : note IA de Psycho & IA).
        txt = str(data.get("text") or "").strip()
        if not txt:
            return jsonify({"ok": False, "error": "texte vide"}), 400
        ok, err = tg_send(txt)
        return jsonify({"ok": ok, "error": err or ""})
    if kind == "macro":
        ia = tg_text_dossier_ia()
        ok, err = tg_send(ia or tg_text_macro_facts())
    elif kind in ("or", "gold"):
        ok, err = tg_send(tg_text_gold())
    elif kind == "cot":
        ok, err = tg_send(tg_text_cot())
    elif kind in ("cal", "calendar"):
        ok, err = tg_send(tg_text_cal())
    elif kind == "news":
        ok, err = tg_send(tg_text_news())
    elif kind == "radar":
        ok, err = tg_send(tg_text_radar())
    elif kind in ("biais", "var", "variation"):
        ok, err = tg_send(tg_text_biais_var())
    elif kind in ("brief", "sydney", "tokyo", "london", "londres", "ny"):
        ses = _tg_ses_by_id("london" if kind == "londres" else (None if kind == "brief" else kind))
        ok, err = tg_send_session_brief(ses, True)
    elif kind == "test":
        ok, err = tg_send("Test OK.")
    else:
        ok, err = tg_send_dossier(True)
        return jsonify({"ok": ok, "via": err if ok else "", "error": "" if ok else err})
    return jsonify({"ok": ok, "error": err or ""})


@app.get("/api/tg/log")
def api_tg_log():
    """Journal du pont : derniers messages envoyés/reçus (500 max)."""
    try:
        lim = max(1, min(500, int(request.args.get("limit") or 200)))
    except Exception:
        lim = 200
    arr = []
    try:
        p = _tg_log_path()
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8") as f:
                arr = json.load(f) or []
            if not isinstance(arr, list):
                arr = []
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:120], "items": []})
    items = arr[-lim:]
    return jsonify({"ok": True, "total": len(arr), "count": len(items), "items": items})


# ═══════════════════════════════════════════════════════════════════════════
#  FINANCIALJUICE — collecte automatique des articles du flux RSS officiel
#  https://features.financialjuice.com/feed/
#  Stockage : <config>/fj_articles.json (200 derniers). Poll toutes les 5 min.
# ═══════════════════════════════════════════════════════════════════════════

FJ_FEED_URL = "https://features.financialjuice.com/feed/"
FJ_MAX = 200
_FJ_LOCK = threading.Lock()
_FJ_LAST = {"t": 0.0}


def _fj_store_path():
    try:
        return os.path.join(user_data_dir(), "fj_articles.json")
    except Exception:
        return os.path.expanduser("~/fj_articles.json")


def _fj_load():
    try:
        with open(_fj_store_path(), "r", encoding="utf-8") as f:
            arr = json.load(f) or []
        return arr if isinstance(arr, list) else []
    except Exception:
        return []


def _fj_save(arr):
    tmp = _fj_store_path() + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(arr, f, ensure_ascii=False)
    os.replace(tmp, _fj_store_path())


def _fj_http(url, timeout=15):
    import urllib.request
    import ssl as _ssl
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except Exception:
        ctx = _ssl._create_unverified_context()
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            return r.read()


_FJ_TAG_RE = re.compile(r"<[^>]+>")
_FJ_ENT = (("&nbsp;", " "), ("&#8217;", "'"), ("&#8216;", "'"), ("&amp;", "&"),
           ("&quot;", '"'), ("&#8211;", "–"), ("&#8212;", "—"), ("&#8230;", "…"))


def _fj_paras(html):
    """Description RSS → liste de paragraphes texte propre."""
    s = str(html or "")
    s = re.sub(r"The post .*?appeared first on.*?</p>", "", s, flags=re.S | re.I)
    parts = re.split(r"</p\s*>", s, flags=re.I)
    out = []
    for p in parts:
        t = _FJ_TAG_RE.sub("", p)
        for a, b in _FJ_ENT:
            t = t.replace(a, b)
        t = re.sub(r"\s+", " ", t).strip()
        if len(t) > 2:
            out.append(t)
    return out


def fj_fetch(force=False):
    """Télécharge le flux et fusionne les nouveaux articles. → nb ajoutés."""
    with _FJ_LOCK:
        now = time.time()
        if not force and now - _FJ_LAST["t"] < 240:
            return -1                       # poll trop rapproché : skip
        _FJ_LAST["t"] = now
    try:
        raw = _fj_http(FJ_FEED_URL)
        import xml.etree.ElementTree as ET
        import email.utils as _eu
        root = ET.fromstring(raw)
        ch = root.find("channel") or root
        nsdc = "{http://purl.org/dc/elements/1.1/}"
        existing = {a.get("id") for a in _fj_load()}
        added = 0
        fresh = []
        for it in ch.iter("item"):
            title = (it.findtext("title") or "").strip()
            link = (it.findtext("link") or "").strip()
            guid = (it.findtext("guid") or link).strip() or link
            if not title or not link or guid in existing:
                continue
            date_s = (it.findtext("pubDate") or "").strip()
            try:
                dt = _eu.parsedate_to_datetime(date_s)
                iso = dt.isoformat()
            except Exception:
                iso = datetime.now(timezone.utc).isoformat()
            cats = [(c.text or "").strip() for c in it.findall("category") if c.text]
            author = (it.findtext(nsdc + "creator") or "").strip()
            paras = _fj_paras(it.findtext("description"))
            fresh.append({"id": guid, "title": title, "link": link, "date": iso,
                          "cats": cats, "author": author, "paras": paras})
            existing.add(guid)
            added += 1
        if added:
            arr = fresh + _fj_load()
            _fj_save(arr[:FJ_MAX])
        log.info("[financialjuice] %s nouvel(aux) article(s)", added)
        return added
    except Exception as e:
        log.warning("[financialjuice] %s", e)
        return 0


def _fj_loop():
    while True:
        try:
            fj_fetch()
        except Exception as e:
            log.debug("[financialjuice] %s", e)
        time.sleep(300)


@app.get("/api/fj/articles")
def api_fj_articles():
    try:
        limit = max(1, min(200, int(request.args.get("limit") or 60)))
    except Exception:
        limit = 60
    arr = _fj_load()
    return jsonify({"ok": True, "count": len(arr),
                    "items": [{k: a.get(k) for k in
                               ("id", "title", "link", "date", "cats", "author")}
                              for a in arr[:limit]]})


@app.get("/api/fj/article")
def api_fj_article():
    """Détail d'un article : paragraphes complets."""
    aid = (request.args.get("id") or "").strip()
    if not aid:
        return jsonify({"ok": False, "error": "id manquant"}), 400
    for a in _fj_load():
        if a.get("id") == aid:
            return jsonify({"ok": True, "title": a.get("title"),
                            "paras": a.get("paras") or []})
    return jsonify({"ok": False, "error": "article introuvable"}), 404


@app.post("/api/fj/refresh")
def api_fj_refresh():
    n = fj_fetch(force=True)
    total = len(_fj_load())
    ok = n >= 0
    return jsonify({"ok": ok, "added": max(n, 0), "total": total,
                    "error": "" if ok else "flux injoignable"})


# ── Proxy Yahoo Finance (évite CORS côté navigateur) ──────────────────────
@app.get("/api/yahoo/quote")
def api_yahoo_quote():
    """Proxy vers l'API Yahoo Finance v8 pour récupérer les prix en temps réel."""
    import urllib.request, urllib.parse, json as _json, ssl, concurrent.futures
    symbols_raw = (request.args.get("symbols") or "").strip()
    if not symbols_raw:
        return jsonify({"ok": False, "error": "symbols manquant"}), 400
    symbols = [s.strip() for s in symbols_raw.split(",") if s.strip()]
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    def _fetch_one(sym):
        try:
            url = "https://query2.finance.yahoo.com/v8/finance/chart/" + urllib.parse.quote(sym) + "?interval=1d&range=1d"
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
            })
            with urllib.request.urlopen(req, timeout=8, context=ctx) as resp:
                data = _json.loads(resp.read().decode())
            meta = data.get("chart", {}).get("result", [{}])[0].get("meta", {})
            price = meta.get("regularMarketPrice")
            prev = meta.get("chartPreviousClose") or meta.get("previousClose")
            chg = ((price - prev) / prev * 100) if price and prev else None
            return {
                "symbol": sym,
                "regularMarketPrice": price,
                "regularMarketChangePercent": round(chg, 4) if chg is not None else None,
                "regularMarketOpen": meta.get("regularMarketOpen"),
                "regularMarketDayHigh": meta.get("regularMarketDayHigh"),
                "regularMarketDayLow": meta.get("regularMarketDayLow"),
                "regularMarketVolume": meta.get("regularMarketVolume"),
            }
        except Exception:
            return {"symbol": sym, "regularMarketPrice": None}

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(_fetch_one, symbols))
    return jsonify({"quoteResponse": {"result": results}})


# ── Service du fichier HTML principal ────────────────────────────────────────
# Ouvrir http://127.0.0.1:8765/ au lieu de file:// rend les embeds YouTube
# possibles (YouTube bloque l'intégration depuis file://).
_HTML_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "omnitrade-v21.html")
_HTML_CACHE = {"mtime": 0, "data": b""}


@app.get("/")
def serve_html():
    try:
        st = os.stat(_HTML_PATH)
        if st.st_mtime != _HTML_CACHE["mtime"]:
            with open(_HTML_PATH, "rb") as fh:
                _HTML_CACHE["data"] = fh.read()
            _HTML_CACHE["mtime"] = st.st_mtime
        return _HTML_CACHE["data"], 200, {"Content-Type": "text/html; charset=utf-8"}
    except Exception as e:
        return f"<h3>Fichier introuvable : {e}</h3>", 404


def main():
    # ── Empreinte machine : calcul lancé AVANT TOUT LE RESTE ────────────────
    # Sur un Windows d'entreprise sans droits d'administrateur, lire le
    # matériel prend jusqu'à 2,6 s (wmic absent, PowerShell qui refuse).
    # Mesuré : le port s'ouvrait en 0,22 s mais la page Licence attendait
    # encore 2,43 s le code machine.
    #
    # Ce calcul est donc lancé ici, sur un fil séparé, dès la première ligne :
    # il se déroule pendant l'analyse des options, la libération du port et la
    # connexion à MetaTrader. Quand la page interroge le moteur, la réponse
    # est déjà en cache.
    try:
        if _lic is not None and hasattr(_lic, "prechauffer_machine_id"):
            _lic.prechauffer_machine_id()
    except Exception:
        pass        # un préchauffage raté ne doit jamais empêcher le démarrage

    ap = argparse.ArgumentParser(
        description="OmniTrade Hub v3 — pont local MetaTrader 5")
    ap.add_argument("--host", default="127.0.0.1", help="interface d'écoute")
    ap.add_argument("--port", type=int, default=8765, help="port HTTP")
    ap.add_argument("--ws-port", type=int, default=8766, help="port WebSocket")
    ap.add_argument("--ws", action="store_true", help="activer le push WebSocket")
    ap.add_argument("--token", default=None, help="clé de sécurité partagée")
    ap.add_argument("--days", type=int, default=365,
                    help="profondeur d'historique par défaut (jours)")
    ap.add_argument("--no-mae", action="store_true",
                    help="désactiver le calcul MAE/MFE (sync plus rapide)")
    ap.add_argument("--push-interval", type=int, default=15,
                    help="intervalle de push WebSocket (s)")
    ap.add_argument("--login", default=None, help="numéro de compte MT5")
    ap.add_argument("--password", default=None, help="mot de passe MT5")
    ap.add_argument("--server", default=None, help="serveur du broker")
    ap.add_argument("--terminal", default=None, help="chemin de terminal64.exe")
    ap.add_argument("--data-dir", default=None,
                    help="mode fichier : dossier MQL5/Files (macOS/Linux)")
    ap.add_argument("--emit-ea", nargs="?", const="", default=None,
                    help="génère OmniTradeExport.mq5 puis quitte")
    ap.add_argument("--no-free-port", action="store_true",
                    help="ne pas libérer automatiquement le port occupé")
    ap.add_argument("--keep-open", dest="keep_open", action="store_true",
                    default=None, help="garder la fenêtre ouverte à la sortie")
    ap.add_argument("--no-keep-open", dest="keep_open", action="store_false",
                    help="quitter immédiatement (usage script/CI)")
    ap.add_argument("--list-dirs", action="store_true",
                    help="lister les dossiers MQL5/Files détectés puis quitter")
    ap.add_argument("--selftest-ssl", action="store_true",
                    help="vérifier que les requêtes HTTPS aboutissent, puis quitter")
    ap.add_argument("--show-token", action="store_true",
                    help="afficher la clé d'accès de ce poste, puis quitter")
    ap.add_argument("--reset-token", action="store_true",
                    help="fabriquer une NOUVELLE clé d'accès, puis quitter")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()

    # Binaire lancé par double-clic : la fenêtre ne doit pas se refermer
    # instantanément sur un message d'erreur que personne n'aurait le temps
    # de lire. En ligne de commande, comportement inchangé.
    keep_open = IS_FROZEN if a.keep_open is None else a.keep_open

    def _bye(code):
        if keep_open:
            try:
                input("\nAppuyez sur Entrée pour fermer cette fenêtre…")
            except (EOFError, KeyboardInterrupt):
                pass
        sys.exit(code)

    logging.basicConfig(
        level=logging.DEBUG if a.verbose else logging.INFO,
        format="%(asctime)s  %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S")

    CFG.update({
        "data_dir": a.data_dir,
        "host": a.host, "http_port": a.port, "ws_port": a.ws_port,
        # Aucune clé passée en argument : on utilise celle de CE poste,
        # créée au premier démarrage et conservée ensuite.
        "token": a.token or _token_charge_ou_cree(),
        "days": a.days, "mae": not a.no_mae,
        "push_interval": max(5, a.push_interval),
        "login": a.login, "password": a.password,
        "server": a.server, "terminal": a.terminal,
    })

    if a.reset_token:
        f = _token_path()
        try:
            if f and os.path.isfile(f):
                os.remove(f)
        except Exception:
            pass
        print(_token_charge_ou_cree(), flush=True)
        _bye(0)

    if a.show_token:
        print(_token_charge_ou_cree(), flush=True)
        _bye(0)

    if a.selftest_ssl:
        # Contrôle destiné au binaire compilé : sans magasin de certificats,
        # toutes les requêtes HTTPS échouent et le Market Hub reste vide.
        # On le vérifie POUR DE VRAI, en joignant deux sources réelles.
        essais = [
            ("ForexFactory", "https://www.forexfactory.com/calendar"),
            ("TradingEconomics", "https://tradingeconomics.com/calendar"),
        ]
        _ssl_ctx()
        print("certificats : %s" % _SSL_ORIGINE, flush=True)
        reussis = 0
        for nom, url in essais:
            try:
                _mkt_get(url, 8)
                print("  OK   %s" % nom, flush=True)
                reussis += 1
            except Exception as e:
                print("  ECHEC %s : %s" % (nom, type(e).__name__), flush=True)
        if reussis:
            print("HTTPS operationnel (%d/%d sources jointes)"
                  % (reussis, len(essais)), flush=True)
            _bye(0)
        print("AUCUNE source HTTPS joignable.", flush=True)
        _bye(1)

    if a.emit_ea is not None:
        emit_ea(a.emit_ea or None)
        _bye(0)

    if a.list_dirs:
        print("Dossiers MQL5/Files détectés (le premier est retenu) :")
        found = default_data_dirs()
        for d in found or []:
            flag = "✓ account.json" if os.path.isfile(
                os.path.join(d, "account.json")) else "  (vide)"
            print("  %s  %s" % (flag, d))
        if not found:
            print("  (aucun)")
        _bye(0)

    print("═" * 74)
    print(f" OmniTrade Hub · MT5 Bridge {VERSION}")
    print("═" * 74)

    mode = "API native MetaTrader5" if NATIVE_API else "FICHIER (JSON via EA)"
    print(f" Plateforme : {sys.platform}"
          f"{' · binaire autonome' if IS_FROZEN else ''}  ·  mode {mode}")
    if not NATIVE_API:
        if MT5_IMPORT_ERROR and IS_WINDOWS:
            # Windows sans le paquet MetaTrader5 : bascule, pas d'arrêt.
            print(f" API native : indisponible ({MT5_IMPORT_ERROR})")
            print("              -> bascule automatique en mode fichier")
        print(f" Dossier    : {file_data_dir()}")

    # ── Libération automatique du port (exigence « Plug & Play ») ────────────
    if not a.no_free_port and port_is_busy(a.port):
        print(f" Port {a.port} déjà utilisé — nettoyage automatique…")
        if free_port(a.port):
            print(f" Port {a.port} libéré ✓")
        else:
            print(f"\n[!] Impossible de libérer le port {a.port}.")
            print("    Une autre application le retient (ou droits insuffisants).")
            print(f"    Relancez avec un autre port :  --port {a.port + 1}\n")
            _bye(3)

    if not mt5_connect(force=True):
        if NATIVE_API:
            print("\n[!] Impossible de se connecter au terminal MetaTrader 5.")
            print("    • Le terminal MT5 est-il ouvert et connecté au compte ?")
            print("    • Outils → Options → Expert Advisors → autoriser l'AutoTrading")
            print("    • Terminal 64 bits requis\n")
        else:
            print("\n[!] Aucun fichier account.json trouvé.")
            print("    Sur macOS, les données transitent par un Expert Advisor")
            print("    (le paquet Python MetaTrader5 est réservé à Windows).\n")
            print("    1. Lancez ce programme avec l'option --emit-ea")
            print("    2. Compilez OmniTradeExport.mq5 dans MetaEditor (F7)")
            print("    3. Glissez l'EA sur un graphique + autorisez l'AutoTrading")
            print("    4. Relancez ce pont")
            print(f"\n    Dossier attendu : {file_data_dir()}")
            print("    (ou précisez-le avec --data-dir \"/chemin/vers/MQL5/Files\")\n")
            print("    Astuce : --list-dirs affiche tous les dossiers détectés.\n")
        # On NE quitte PAS : les services de marché (actualités, calendrier,
        # sentiment) ne dépendent pas de MetaTrader. Un trader sans MT5 ouvert
        # doit quand même recevoir ses données de marché.
        print(" MetaTrader : non connecté — les données de marché restent"
              " disponibles.\n")

    acc = get_account()
    if acc:
        print(f" Compte   : #{acc['login']}  {acc['server']}  ({acc['currency']})")
        print(f" Solde    : {acc['balance']}   Équité : {acc['equity']}"
              f"   Marge : {acc['margin']}")
    print(f" HTTP     : http://{a.host}:{a.port}/api/sync")
    if a.ws:
        print(f" WS       : ws://{a.host}:{a.ws_port}")
    print(f" Token    : {'activé' if a.token else 'DÉSACTIVÉ (--token conseillé)'}")
    print(f" MAE/MFE  : {'activé' if CFG['mae'] else 'désactivé'}")
    print("═" * 74)
    print(" Ouvrez maintenant OmniTrade Hub : la connexion se fait TOUTE")
    print(" SEULE. Rien à saisir — ni hôte, ni port, ni jeton.")
    print("═" * 74 + "\n")

    if a.ws:
        threading.Thread(target=run_ws_server, daemon=True).start()

    # FinancialJuice : première collecte immédiate puis poll 5 min.
    try:
        threading.Thread(target=_fj_loop, daemon=True, name="fj-news").start()
    except Exception as _e:
        log.warning("fj thread : %s", _e)

    # Fed & Macro : première collecte pendant que le trader ouvre la page.
    try:
        _cb_kick()
        _cb_watch_start()
    except Exception:
        pass

    try:
        try:
            _ia_keys_bootstrap()
        except Exception:
            pass
        tg_start()
    except Exception:
        pass

    try:
        # use_reloader=False : indispensable en gelé, le rechargeur relance
        # sys.executable et créerait une boucle de processus.
        app.run(host=a.host, port=a.port, threaded=True,
                debug=False, use_reloader=False)
    except KeyboardInterrupt:
        pass
    except OSError as e:
        print(f"\n[!] Démarrage du serveur impossible : {e}")
        print(f"    Essayez un autre port :  --port {a.port + 1}\n")
        mt5_shutdown()
        _bye(4)
    finally:
        mt5_shutdown()
        print("\nPont arrêté.")


if __name__ == "__main__":
    # multiprocessing.freeze_support() : obligatoire pour les binaires gelés,
    # sinon un éventuel sous-processus relancerait l'application entière.
    try:
        import multiprocessing
        multiprocessing.freeze_support()
    except Exception:
        pass
    main()
