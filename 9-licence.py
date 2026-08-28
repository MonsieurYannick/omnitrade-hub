# -*- coding: utf-8 -*-
"""
═══════════════════════════════════════════════════════════════════════════════
 OmniTrade Hub — Noyau de licence
═══════════════════════════════════════════════════════════════════════════════
 Signature Ed25519 (RFC 8032) implémentée en Python PUR : aucune dépendance
 externe, donc rien à installer chez le trader et rien à embarquer de plus
 dans le binaire PyInstaller.

 Principe
 ---------------------------------------------------------------------------
 VOUS possédez la clé privée (elle ne quitte jamais votre machine).
 L'application n'embarque que la clé PUBLIQUE, qui permet de VÉRIFIER une
 signature mais jamais d'en fabriquer une. Un utilisateur ne peut donc pas
 forger une licence, même en lisant tout le code source.

 Contenu d'une licence
 ---------------------------------------------------------------------------
   v    : version du format
   exp  : date d'expiration (AAAA-MM-JJ) ou "never"
   mid  : empreinte machine (ou "" pour une licence transférable)
   act  : nombre d'activations autorisées (2 = portable + fixe)
   plan : libellé commercial (demo7 / m3 / m6 / m12 / life)
   iss  : date d'émission
   sn   : numéro de série (traçabilité commerciale)

 Format distribué : OTH1-<payload base32>-<signature base32>, en groupes de
 8 caractères séparés par des tirets pour la lisibilité et la dictée.
═══════════════════════════════════════════════════════════════════════════════
"""
import base64
import hashlib
import json
import os
import platform
import re
import subprocess
import threading
import sys
from datetime import datetime, timedelta, timezone

# ─────────────────────────────────────────────────────────────────────────────
#  Ed25519 — implémentation de référence (RFC 8032), sans dépendance
# ─────────────────────────────────────────────────────────────────────────────
_P = 2 ** 255 - 19
_L = 2 ** 252 + 27742317777372353535851937790883648493
_D = -121665 * pow(121666, _P - 2, _P) % _P
_I = pow(2, (_P - 1) // 4, _P)


def _sha512(b):
    return hashlib.sha512(b).digest()


def _inv(x):
    return pow(x, _P - 2, _P)


def _x_recover(y):
    xx = (y * y - 1) * _inv(_D * y * y + 1)
    x = pow(xx, (_P + 3) // 8, _P)
    if (x * x - xx) % _P != 0:
        x = (x * _I) % _P
    if x % 2 != 0:
        x = _P - x
    return x


_BY = 4 * _inv(5) % _P
_BX = _x_recover(_BY)
_B = (_BX % _P, _BY % _P, 1, _BX * _BY % _P)


def _add(p, q):
    """Addition sur Ed25519 en coordonnées étendues (X, Y, Z, T).

    Formules de référence RFC 8032 (add-2008-hwcd-3), volontairement
    écrites sans optimisation : la lisibilité prime, et la vitesse est
    sans importance ici (une signature par génération de clé).
    """
    x1, y1, z1, t1 = p
    x2, y2, z2, t2 = q
    A = (y1 - x1) * (y2 - x2) % _P
    B = (y1 + x1) * (y2 + x2) % _P
    C = t1 * 2 * _D * t2 % _P
    D = z1 * 2 * z2 % _P
    E = (B - A) % _P
    F = (D - C) % _P
    G = (D + C) % _P
    H = (B + A) % _P
    return (E * F % _P, G * H % _P, F * G % _P, E * H % _P)


def _dbl(p):
    """Doublement : strictement add(p, p), donc cohérent par construction.

    Une implémentation séparée avait introduit une incohérence détectée par
    les vecteurs officiels RFC 8032 (add(B,B) != dbl(B)).
    """
    return _add(p, p)


def _mul(p, n):
    """Multiplication scalaire par doublement-et-addition."""
    q = (0, 1, 1, 0)                      # élément neutre
    while n > 0:
        if n & 1:
            q = _add(q, p)
        p = _dbl(p)
        n >>= 1
    return q


def _compress(p):
    x1, y1, z1, _ = p
    zi = _inv(z1)
    x = x1 * zi % _P
    y = y1 * zi % _P
    return (y | ((x & 1) << 255)).to_bytes(32, "little")


def _decompress(s):
    y = int.from_bytes(s, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    x = _x_recover(y)
    if x & 1 != sign:
        x = _P - x
    return (x, y, 1, x * y % _P)


def _secret_expand(sk):
    h = _sha512(sk)
    a = int.from_bytes(h[:32], "little")
    a &= (1 << 254) - 8
    a |= 1 << 254
    return a, h[32:]


def ed25519_publickey(sk):
    """Dérive la clé publique (32 octets) depuis la clé privée (32 octets)."""
    a, _ = _secret_expand(sk)
    return _compress(_mul(_B, a))


def ed25519_sign(sk, msg):
    """Signe `msg` et retourne 64 octets."""
    a, prefix = _secret_expand(sk)
    pk = _compress(_mul(_B, a))
    r = int.from_bytes(_sha512(prefix + msg), "little") % _L
    R = _compress(_mul(_B, r))
    k = int.from_bytes(_sha512(R + pk + msg), "little") % _L
    S = (r + k * a) % _L
    return R + S.to_bytes(32, "little")


def ed25519_verify(pk, msg, sig):
    """Vérifie la signature. Retourne True/False, ne lève jamais."""
    try:
        if len(sig) != 64 or len(pk) != 32:
            return False
        R = _decompress(sig[:32])
        A = _decompress(pk)
        S = int.from_bytes(sig[32:], "little")
        if S >= _L:
            return False
        k = int.from_bytes(_sha512(sig[:32] + pk + msg), "little") % _L
        # [S]B == R + [k]A
        return _compress(_mul(_B, S)) == _compress(_add(R, _mul(A, k)))
    except Exception:
        return False


# ─────────────────────────────────────────────────────────────────────────────
#  Encodage du texte de licence
# ─────────────────────────────────────────────────────────────────────────────
_B32 = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"          # base32 RFC 4648 sans padding


def _b32e(b):
    return base64.b32encode(b).decode("ascii").rstrip("=")


def _b32d(s):
    s = s.upper().replace("-", "").replace(" ", "")
    # Corrige les confusions de saisie les plus fréquentes.
    s = s.replace("0", "O").replace("1", "I").replace("8", "B")
    pad = (-len(s)) % 8
    return base64.b32decode(s + "=" * pad)


def _group(s, n=8):
    return "-".join(s[i:i + n] for i in range(0, len(s), n))


PREFIX = "OTH1"


# Un code machine valide : 16 caractères en base32 (voir machine_id()).
MID_RE = re.compile(r"^[A-Z2-7]{16}$")


def normalize_mid(mid):
    """Nettoie un code machine saisi à la main.

    L'utilisateur copie-colle depuis l'application : on tolère les espaces,
    les minuscules et les tirets, mais le résultat doit rester un code valide.
    Retourne None si ce n'en est pas un.
    """
    if not mid:
        return None
    v = "".join(str(mid).split()).replace("-", "").upper()
    return v if MID_RE.match(v) else None


def make_license(sk_hex, plan, days, machine_id="", activations=2, serial=None):
    """Fabrique une clé de licence signée.

    days = None ou 0 -> licence à vie.

    Le code machine est demandé pour la FACTURATION et le COMPTAGE des
    ordinateurs (le serveur limite le nombre d'ordinateurs distincts par
    code d'achat). Il n'est pas un verrou : la clé émise reste valable sur
    tout poste, la signature et l'échéance sont les vraies protections.
    """
    mid = normalize_mid(machine_id)
    if not mid:
        raise ValueError(
            "code machine obligatoire : 16 caracteres A-Z et 2-7. "
            "Le client le trouve dans l'application, page « Licence »."
        )
    machine_id = mid
    now = datetime.now(timezone.utc)
    if days:
        exp = (now + timedelta(days=int(days))).strftime("%Y-%m-%d")
    else:
        exp = "never"
    sn = serial or base64.b32encode(os.urandom(5)).decode().rstrip("=")
    payload = {"v": 1, "exp": exp, "mid": machine_id or "",
               "act": int(activations), "plan": plan,
               "iss": now.strftime("%Y-%m-%d"), "sn": sn}
    # Sérialisation DÉTERMINISTE : indispensable, sinon la signature calculée
    # ne correspondrait pas à celle vérifiée (ordre des clés différent).
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    sig = ed25519_sign(bytes.fromhex(sk_hex), raw)
    return PREFIX + "-" + _group(_b32e(raw)) + "-" + _group(_b32e(sig)), payload


def parse_license(key, pk_hex):
    """Décode et VÉRIFIE une clé.

    Retourne (payload, None) si la signature est valide,
    sinon (None, "motif").
    """
    try:
        k = (key or "").strip().upper()
        if not k.startswith(PREFIX):
            return None, "format"
        body = k[len(PREFIX):].lstrip("-")
        raw_all = body.replace("-", "")
        # La signature fait 64 octets -> 103 caractères base32 sans padding.
        if len(raw_all) < 110:
            return None, "tronquee"
        sig_txt = raw_all[-103:]
        pay_txt = raw_all[:-103]
        raw = _b32d(pay_txt)
        sig = _b32d(sig_txt)
        if not ed25519_verify(bytes.fromhex(pk_hex), raw, sig):
            return None, "signature"
        return json.loads(raw.decode()), None
    except Exception:
        return None, "illisible"


# ─────────────────────────────────────────────────────────────────────────────
#  Empreinte machine — macOS, Windows, Linux
# ─────────────────────────────────────────────────────────────────────────────
def _no_window_kwargs():
    """Empêche l'apparition de fenêtres noires sous Windows.

    Chaque appel à wmic / powershell / reg ouvre sinon une console qui
    clignote à l'écran. Le trader voyait donc jusqu'à six fenêtres noires
    surgir puis disparaître à l'ouverture de la page Licence.
    """
    if os.name != "nt":
        return {}
    kw = {}
    try:
        kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0          # SW_HIDE
        kw["startupinfo"] = si
    except Exception:
        return {}
    return kw


def _run(cmd, timeout=8):
    """Exécute une commande système et renvoie sa sortie standard.

    Le délai est réglable : les sondes matérielles Windows sont lentes
    (PowerShell met 2 à 4 s à démarrer) et il ne faut jamais qu'une seule
    d'entre elles bloque la réponse de l'application.
    """
    try:
        out = subprocess.run(cmd, shell=True, capture_output=True,
                             timeout=timeout, text=True, **_no_window_kwargs())
        return (out.stdout or "").strip()
    except Exception:
        return ""


def _win_registry_guid():
    """MachineGuid lu DIRECTEMENT dans la base de registre.

    Sert uniquement de dernier recours. Passer par le module `winreg`
    évite de lancer « reg.exe » : c'est instantané et sans fenêtre.
    """
    try:
        import winreg
        for vue in (getattr(winreg, "KEY_WOW64_64KEY", 0), 0):
            try:
                k = winreg.OpenKey(
                    winreg.HKEY_LOCAL_MACHINE,
                    r"SOFTWARE\Microsoft\Cryptography",
                    0, winreg.KEY_READ | vue)
                try:
                    val, _ = winreg.QueryValueEx(k, "MachineGuid")
                finally:
                    winreg.CloseKey(k)
                if val:
                    return str(val).strip()
            except Exception:
                continue
    except Exception:
        pass
    return ""


_RAW_MID_CACHE = None       # empreinte brute, calculée une seule fois
_MID_CACHE = None           # empreinte affichable, calculée une seule fois
_MID_LOCK = threading.Lock()
_MID_PRECHAUFFE = False

# ─────────────────────────────────────────────────────────────────────────────
#  IDENTIFIANT DE L'APPAREIL (correctif v9)
# ----------------------------------------------------------------------------
#  Les licences ne sont PLUS liées au matériel (l'empreinte changeait après une
#  mise à jour de Windows, une réinstallation ou un changement d'outil). À la
#  place : un identifiant LOGICIEL persistant, stocké DANS LE PROFIL UTILISATEUR
#  (~/.omnitrade/device.id). Il survit à la réinstallation de l'application.
#  S'il est perdu (réinitialisation du système), l'e-mail du compte permet de
#  gérer les appareils et de ré-associer la licence.
# ─────────────────────────────────────────────────────────────────────────────
_DEVICE_DIR_NAME = ".omnitrade"
_DEVICE_FILE_NAME = "device.id"
_DEVICE_PATH_CACHE = None
_DEVICE_PATH_LOCK = threading.Lock()
_SOFT_ID_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"   # base32 : 32 caractères


def _device_path():
    """Chemin du fichier d'identité, dans le dossier du profil utilisateur.

    ~ survit à une réinstallation de l'application (le dossier utilisateur
    n'est pas touché). Windows : %USERPROFILE% ; macOS/Linux : $HOME.
    """
    global _DEVICE_PATH_CACHE
    if _DEVICE_PATH_CACHE:
        return _DEVICE_PATH_CACHE
    with _DEVICE_PATH_LOCK:
        if _DEVICE_PATH_CACHE:
            return _DEVICE_PATH_CACHE
        base = os.path.expanduser("~")
        _DEVICE_PATH_CACHE = os.path.join(base, _DEVICE_DIR_NAME, _DEVICE_FILE_NAME)
        return _DEVICE_PATH_CACHE


def _gen_device_id():
    """Nouvel identifiant : 16 caractères, alphabet base32 (A-Z, 2-7).

    Même format que les anciennes empreintes, pour que l'API du moteur et le
    serveur d'activation (oth-activate) n'aient rien à changer.
    """
    import secrets
    return "".join(secrets.choice(_SOFT_ID_ALPHABET) for _ in range(16))


def soft_device_id():
    """Identifiant LOGICIEL persistant de l'appareil.

    Crée ~/.omnitrade/device.id au premier lancement, le lit ensuite. Peut
    échouer (profil en lecture seule, dossier verrouillé…) : dans ce cas on
    retombe sur l'empreinte matérielle, qui reste produite en dégradé.
    """
    global _MID_CACHE
    if _MID_CACHE:
        return _MID_CACHE
    try:
        p = _device_path()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        pid = ""
        try:
            with open(p, "r") as f:
                pid = f.read().strip().upper()
        except FileNotFoundError:
            pid = ""
        if not re.fullmatch(r"[A-Z2-7]{16}", pid):
            pid = _gen_device_id()
            fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                os.write(fd, pid.encode("ascii"))
            finally:
                os.close(fd)
        _MID_CACHE = pid
        return pid
    except Exception:
        # Repli : identité matérielle classique (jamais bloquante).
        return _mid_depuis(raw_machine_id())


def prechauffer_machine_id():
    """Calcule l'empreinte en arrière-plan, sans bloquer le démarrage.

    Sur un Windows verrouillé par une politique d'entreprise, wmic échoue et
    PowerShell met deux à quatre secondes avant de refuser à son tour. La
    toute première requête payait cette attente : mesuré à 2,64 s, trop
    près de la limite de 3 s que nous nous imposons.

    Le moteur lance donc ce préchauffage dès son démarrage. Pendant que le
    serveur finit de s'initialiser, l'empreinte est déjà en cours de calcul :
    quand la page Licence interroge le moteur, la réponse est immédiate.

    Le verrou garantit qu'un seul calcul a lieu, même si une requête arrive
    exactement en même temps que le préchauffage.
    """
    global _MID_PRECHAUFFE
    with _MID_LOCK:
        if _MID_PRECHAUFFE:
            return
        _MID_PRECHAUFFE = True
    t = threading.Thread(target=machine_id, name="hwid-prechauffe", daemon=True)
    t.start()
    return t


def raw_machine_id():
    """Identifiant matériel brut, mis en cache pour le processus.

    POURQUOI UN CACHE (correctif v7.2)
    ----------------------------------
    Le matériel d'un ordinateur ne change pas pendant qu'il fonctionne :
    interroger le système plus d'une fois est inutile.

    Or une seule requête « /api/license » appelait cette fonction SIX fois
    (l'autotest du moteur en déclenche cinq, la vérification une). Sous
    macOS et Linux la sonde est immédiate, le défaut passait inaperçu.
    Sous Windows chaque sonde coûte 2 à 4 s : la réponse mettait 13,3 s,
    le navigateur abandonnait à 8 s, et le trader ne voyait jamais que
    « moteur non démarré ».

    Avec le cache : une sonde au lieu de six, réponse immédiate ensuite.
    La valeur retournée est rigoureusement identique — les licences déjà
    émises restent valides.
    """
    global _RAW_MID_CACHE
    if _RAW_MID_CACHE:
        return _RAW_MID_CACHE
    val = _probe_machine_id()
    # On ne mémorise QUE si la sonde a réussi : un échec passager (service
    # WMI momentanément indisponible au démarrage de Windows) ne doit pas
    # figer un repli dégradé pour toute la session.
    if val and val != _fallback_machine_id():
        _RAW_MID_CACHE = val
    return val


def _fallback_machine_id():
    """Repli utilisé quand aucune sonde matérielle ne répond."""
    return (platform.node() or "") + "|" + (platform.machine() or "")


def _probe_machine_id():
    """Interroge réellement le système. Ne pas appeler directement.

    macOS   : IOPlatformUUID (survit à une réinstallation du système)
    Windows : UUID de la carte mère (WMIC puis PowerShell en repli)
    Linux   : machine-id
    Un repli générique est prévu si aucune de ces sources ne répond.
    """
    sysname = platform.system().lower()
    val = ""
    if sysname == "darwin":
        out = _run("ioreg -rd1 -c IOPlatformExpertDevice")
        m = re.search(r'"IOPlatformUUID"\s*=\s*"([^"]+)"', out)
        if m:
            val = m.group(1)
        if not val:
            out = _run("system_profiler SPHardwareDataType")
            m = re.search(r"Hardware UUID:\s*([0-9A-Fa-f\-]+)", out)
            if m:
                val = m.group(1)
    elif sysname == "windows":
        # ═══════════════════════════════════════════════════════════════════
        #  REGISTRE EN PREMIER — correctif v7.7
        # -------------------------------------------------------------------
        #  Cause du blocage signalé par les clients Windows 11 : Microsoft a
        #  RETIRÉ « wmic » de Windows 11 24H2. Il répond encore sur Windows 10,
        #  ce qui explique exactement la différence observée entre les deux.
        #
        #  L'ancienne chaîne était : wmic (4 s d'attente pour rien) puis
        #  PowerShell (8 s, souvent bridé par la politique de sécurité en
        #  entreprise). Douze secondes dans le meilleur des cas, un échec
        #  complet dans le pire.
        #
        #  MachineGuid, lu directement dans la base de registre par winreg,
        #  répond en quelques microsecondes, sans lancer le moindre programme
        #  et sans ouvrir de fenêtre noire. C'est désormais la source de
        #  référence.
        #
        #  La compatibilité avec les licences déjà vendues est assurée
        #  ailleurs : voir machine_ids_compatibles() plus bas, qui expose
        #  AUSSI l'ancienne empreinte wmic pour que les clés émises avant
        #  cette version restent valides.
        # ═══════════════════════════════════════════════════════════════════
        val = _win_registry_guid()
        if not val:
            out = _run("wmic csproduct get uuid", timeout=3)
            m = re.search(r"([0-9A-Fa-f]{8}-[0-9A-Fa-f\-]{20,})", out)
            if m:
                val = m.group(1)
        if not val:
            out = _run('powershell -NoProfile -NonInteractive -Command '
                       '"(Get-CimInstance Win32_ComputerSystemProduct).UUID"',
                       timeout=6)
            m = re.search(r"([0-9A-Fa-f]{8}-[0-9A-Fa-f\-]{20,})", out)
            if m:
                val = m.group(1)
    else:
        for p in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
            try:
                with open(p) as f:
                    val = f.read().strip()
                if val:
                    break
            except Exception:
                pass
    if not val:
        # Dernier recours : combinaison stable mais moins forte.
        val = _fallback_machine_id()
    return val


def machine_id():
    """Identifiant affichable de l'appareil : 16 caractères base32.

    DEPUIS la v9 : identifiant LOGICIEL persistant (soft_device_id), stocké
    dans le profil utilisateur. Il ne change ni après la réinstallation de
    l'application, ni après une mise à jour de Windows — seules les sources
    de cet historique causaient des refus.

    L'empreinte matérielle ne sert plus que de repli, si le profil est
    illisible. Résultat mémorisé : le moteur appelle cette fonction plusieurs
    fois par requête.
    """
    global _MID_CACHE
    if _MID_CACHE:
        return _MID_CACHE
    # Le verrou évite que deux requêtes simultanées lancent chacune leur
    # création du fichier d'identité.
    with _MID_LOCK:
        if _MID_CACHE:
            return _MID_CACHE
        try:
            mid = soft_device_id()
            if re.fullmatch(r"[A-Z2-7]{16}", mid):
                _MID_CACHE = mid
                return mid
        except Exception:
            pass
        raw = raw_machine_id()
        h = hashlib.sha256(("OmniTradeHub|" + raw).encode()).digest()
        mid = base64.b32encode(h)[:16].decode()
        if raw and raw != _fallback_machine_id():
            _MID_CACHE = mid
    return mid


# ─────────────────────────────────────────────────────────────────────────────
#  Garde anti-recul d'horloge
# ─────────────────────────────────────────────────────────────────────────────
def clock_guard(state_path, now=None):
    """Mémorise la date maximale jamais observée.

    Si l'horloge recule de plus de 48 h par rapport au maximum connu, on
    considère qu'elle a été manipulée. Les 48 h de tolérance évitent de
    pénaliser un changement de fuseau ou une correction NTP légitime.

    Retourne (ok, date_effective).
    """
    now = now or datetime.now(timezone.utc)
    seen = None
    try:
        with open(state_path) as f:
            st = json.load(f)
        seen = datetime.fromisoformat(st.get("max_seen"))
        if seen.tzinfo is None:
            seen = seen.replace(tzinfo=timezone.utc)
    except Exception:
        seen = None
    tampered = bool(seen and now < seen - timedelta(hours=48))
    eff = max(now, seen) if seen else now
    try:
        os.makedirs(os.path.dirname(state_path), exist_ok=True)
        with open(state_path, "w") as f:
            json.dump({"max_seen": eff.isoformat()}, f)
    except Exception:
        pass
    return (not tampered), eff


# ─────────────────────────────────────────────────────────────────────────────
#  Vérification complète
# ─────────────────────────────────────────────────────────────────────────────
PLANS = {"demo7": ("Démo 7 jours", 7),
         "m3": ("Licence 3 mois", 90),
         "m6": ("Licence 6 mois", 180),
         "m12": ("Licence 12 mois", 365),
         "life": ("Licence à vie", None)}


def _mid_depuis(brut):
    """Empreinte affichable calculée à partir d'un identifiant matériel brut."""
    if not brut:
        return ""
    h = hashlib.sha256(("OmniTradeHub|" + brut).encode()).digest()
    return base64.b32encode(h)[:16].decode()


def _wmic_machine_id():
    """Ancienne empreinte, telle que la calculaient les versions <= 7.6.

    Sur Windows 10, « wmic » répondait et servait de source principale. Les
    licences vendues à ces clients sont liées à CETTE empreinte. Depuis la
    v7.7 la source de référence est le registre : sans ce rappel, toutes
    ces clés seraient rejetées du jour au lendemain.
    """
    if platform.system().lower() != "windows":
        return ""
    try:
        out = _run("wmic csproduct get uuid", timeout=3)
        m = re.search(r"([0-9A-Fa-f]{8}-[0-9A-Fa-f\-]{20,})", out)
        if m:
            return _mid_depuis(m.group(1))
    except Exception:
        pass
    return ""


_MIDS_CACHE = None


def machine_ids_compatibles():
    """Toutes les empreintes que ce poste est en droit de présenter.

    La PREMIÈRE est l'empreinte officielle, celle qui s'affiche à l'écran et
    que le client communique lors d'une commande. Les suivantes sont des
    empreintes historiques, acceptées uniquement pour ne pas invalider une
    licence déjà payée.

    Concrètement : un client Windows 10 équipé avant la v7.7 garde sa clé,
    et un client Windows 11 obtient enfin un code, instantanément.
    """
    global _MIDS_CACHE
    if _MIDS_CACHE is not None:
        return list(_MIDS_CACHE)
    liste = [machine_id()]
    try:
        ancien = _wmic_machine_id()
        if ancien and ancien not in liste:
            liste.append(ancien)
    except Exception:
        pass
    _MIDS_CACHE = liste
    return list(liste)


def check_license(key, pk_hex, state_path, mid=None, now=None):
    """Vérifie une licence de bout en bout.

    Retourne un dictionnaire prêt à être renvoyé au client :
      {valid, reason, plan, planLabel, expires, daysLeft, machine, serial}
    """
    res = {"valid": False, "reason": "none", "plan": "", "planLabel": "",
           "expires": "", "daysLeft": 0, "machine": mid or machine_id(),
           "serial": ""}
    if not key:
        res["reason"] = "absente"
        return res
    payload, err = parse_license(key, pk_hex)
    if err:
        res["reason"] = err
        return res
    res["plan"] = payload.get("plan", "")
    res["planLabel"] = PLANS.get(payload.get("plan", ""), ("Licence", None))[0]
    res["serial"] = payload.get("sn", "")
    res["expires"] = payload.get("exp", "")

    # Liaison machine : PUREMENT INFORMATIVE, JAMAIS BLOQUANTE (correctif v9).
    #
    # Le code machine a causé trop de désagréments sous Windows : l'empreinte
    # changeait après une mise à jour du système, une réinstallation, ou selon
    # l'outil qui la lisait, et la licence payée était alors refusée. C'est
    # terminé : tant que la signature est bonne et la date d'échéance non
    # dépassée, la clé reste active sur n'importe quel poste.
    #
    # Les vraies protections restent la signature Ed25519 (aucune clé ne peut
    # être fabriquée) et l'échéance signée. Le « mid » n'est plus qu'un
    # renseignement ; la limite du nombre d'ordinateurs est déjà appliquée
    # côté serveur, au moment de l'activation du code d'achat.
    want = (payload.get("mid") or "").strip()
    if want:
        try:
            valides = machine_ids_compatibles()
        except Exception:
            valides = []
        cur = (mid or "").strip().upper()
        if cur and cur not in valides:
            # Le poste courant fait toujours PARTIE des empreintes légitimes :
            # même si la sonde a changé d'avis, on ne refuse jamais.
            valides.append(cur)
        if want not in valides:
            res["machineAutre"] = True

    ok_clock, eff = clock_guard(state_path, now)
    if not ok_clock:
        res["reason"] = "horloge"
        return res

    exp = payload.get("exp", "")
    if exp == "never":
        res["valid"] = True
        res["reason"] = "ok"
        res["daysLeft"] = 36500
        return res
    try:
        d = datetime.strptime(exp, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except Exception:
        res["reason"] = "date"
        return res
    left = (d - eff).days
    res["daysLeft"] = left
    if left < 0:
        res["reason"] = "expiree"
        return res
    res["valid"] = True
    res["reason"] = "ok"
    return res
