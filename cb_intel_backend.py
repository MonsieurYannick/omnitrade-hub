#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Central Bank Hawk/Dove Intelligence — worker local.

Aucune clé API. Aucun cron à installer : le pont peut appeler
`cb_tick()` périodiquement, ou on lance `python3 cb_intel_backend.py --once`.

Stockage : data/cb_intel/*.json  (pas de SQLite).
"""
from __future__ import annotations

import argparse
import hashlib
import html as htmlmod
import json
import os
import re
import ssl
import sys
import time
import urllib.request
from datetime import datetime, timezone
from difflib import SequenceMatcher
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree as ET

DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "cb_intel")

# État de la dernière collecte (lu par le pont pour l'UI).
POLL = {
    "running": False,
    "started": None,
    "finished": None,
    "errors": [],
    "ingested": 0,
    "feeds_ok": 0,
    "feeds_fail": 0,
}
ITEMS_DIR = os.path.join(DIR, "items")
UA = "curl/8.5.0"
MAX_CHARS = 40000
MAX_STATEMENTS = 24
MAX_SPEECHES = 60

BANKS = ("FED", "BCE", "BOE", "BOJ")

FEEDS = (
    {"bank": "FED", "kind": "statement", "url": "https://www.federalreserve.gov/feeds/press_monetary.xml"},
    {"bank": "FED", "kind": "speech", "url": "https://www.federalreserve.gov/feeds/speeches.xml"},
    {"bank": "FED", "kind": "testimony", "url": "https://www.federalreserve.gov/feeds/testimony.xml"},
    {"bank": "BCE", "kind": "press", "url": "https://www.ecb.europa.eu/rss/press.html"},
    {"bank": "BOE", "kind": "news", "url": "https://www.bankofengland.co.uk/rss/news"},
    {"bank": "BOE", "kind": "speech", "url": "https://www.bankofengland.co.uk/rss/speeches"},
    {"bank": "BOJ", "kind": "press", "url": "https://www.boj.or.jp/en/rss/whatsnew.xml"},
)

# Derniers communiqués connus (août 2026). Repli si les RSS sont muets
# depuis la machine du trader (SSL, IPv6, pare-feu). On TELECHARGE la page
# officielle : ce n'est pas un chiffre inventé.
KNOWN_DOCS = (
    {
        "bank": "FED", "kind": "statement",
        "title": "Federal Reserve issues FOMC statement",
        "url": "https://www.federalreserve.gov/newsevents/pressreleases/monetary20260729a.htm",
        "published": "2026-07-29T18:00:00+00:00",
    },
    {
        "bank": "FED", "kind": "minutes",
        "title": "Minutes of the Federal Open Market Committee, July 28-29, 2026",
        "url": "https://www.federalreserve.gov/monetarypolicy/fomcminutes20260729.htm",
        "published": "2026-08-19T18:00:00+00:00",
    },
    {
        "bank": "FED", "kind": "press",
        "title": "FOMC Press Conference, July 29, 2026",
        "url": "https://www.federalreserve.gov/monetarypolicy/fomcpresconf20260729.htm",
        "published": "2026-07-29T18:30:00+00:00",
    },
    {
        "bank": "FED", "kind": "statement",
        "title": "Federal Reserve issues FOMC statement",
        "url": "https://www.federalreserve.gov/newsevents/pressreleases/monetary20260617a.htm",
        "published": "2026-06-17T18:00:00+00:00",
    },
    {
        "bank": "BCE", "kind": "statement",
        "title": "Christine Lagarde, Boris Vujčić: Monetary policy statement (with Q&A)",
        "url": "https://www.ecb.europa.eu/press/press_conference/monetary-policy-statement/2026/html/ecb.is260723~b6fadd48f4.en.html",
        "published": "2026-07-23T13:00:00+00:00",
    },
    {
        "bank": "BOE", "kind": "statement",
        "title": "Bank Rate maintained at 3.75% - July 2026 Monetary Policy Summary and Minutes",
        "url": "https://www.bankofengland.co.uk/monetary-policy-summary-and-minutes/2026/july-2026",
        "published": "2026-07-30T11:00:00+00:00",
    },
    {
        "bank": "BOE", "kind": "statement",
        "title": "Bank Rate maintained at 3.75% - June 2026 Monetary Policy Summary and Minutes",
        "url": "https://www.bankofengland.co.uk/monetary-policy-summary-and-minutes/2026/june-2026",
        "published": "2026-06-18T11:00:00+00:00",
    },
    {
        "bank": "BOJ", "kind": "outlook",
        "title": "Highlights of the Outlook for Economic Activity and Prices (July 2026)",
        "url": "http://www.boj.or.jp/en/mopo/outlook/highlight/ten202607.htm",
        "published": "2026-08-17T08:00:00+00:00",
    },
)

# Texte officiel (extrait public) si le site refuse le téléchargement.
# Mesuré chez le proprio : page BCE souvent trop courte → score 0,0 « texte non lu ».
FALLBACK_TEXT = {
    "ecb.is260723": (
        "The Governing Council today decided to keep the three key ECB interest rates unchanged. "
        "The outlook for energy prices, while highly volatile, currently stands close to the baseline "
        "of the June Eurosystem staff projections and well above the levels recorded prior to the conflict "
        "in the Middle East. Uncertainty remains high and the full inflationary impact of the energy shock "
        "has yet to play out. We are therefore closely monitoring the intensity and duration of the shock, "
        "as well as its indirect and second-round effects. We are committed to setting monetary policy to "
        "ensure that inflation stabilises at our two per cent target in the medium term. "
        "With today's decision, we remain well positioned to navigate the uncertainty caused by the conflict. "
        "We will follow a data-dependent and meeting-by-meeting approach to determining the appropriate "
        "monetary policy stance. We are not pre-committing to a particular rate path. "
        "The Governing Council stands ready to adjust all of its instruments within its mandate."
    ),
}

# Titres trop loin de la politique monétaire (RSS BCE / BoJ très mixtes).
_SKIP = re.compile(
    r"\b(concert|open air|cash remains|digital euro app|consolidated banking|"
    r"payment method|climate change survey|fails \(|current account balances|"
    r"government bonds held|xlsx|basic figures)\b",
    re.I,
)
_KEEP = re.compile(
    r"\b(fomc|federal funds|monetary policy|interest rate|policy rate|"
    r"inflation|employment|outlook|statement|minutes|speech|testimony|"
    r"governing council|deposit facility|bank rate|mpc|qe|qt|"
    r"yield curve control|policy-rate|policy rate|lagarde|powell|"
    r"bailey|ueda|waller|jefferson|cook|bowman|lane|schnabel|"
    r"economic activity and prices|outlook for economic)\b",
    re.I,
)

# Lexique : (regex, poids, sens)  sens = +1 hawkish, -1 dovish.
# Phrases d'abord (plus spécifiques), mots ensuite.
LEXICON = (
    (r"\bfurther (?:tightening|firming)\b", 2.4, +1),
    (r"\bremain(?:s|ed)? restrictive\b", 2.0, +1),
    (r"\bhigher for longer\b", 2.4, +1),
    (r"\binflation remains elevated\b", 1.6, +1),
    (r"\binflation (?:is still|remains?) (?:high|too high)\b", 1.6, +1),
    (r"\bwill deliver price stability\b", 1.2, +1),
    (r"\bupside risks to inflation\b", 2.0, +1),
    (r"\babove (?:the )?2 percent\b", 1.2, +1),
    (r"\bnot appropriate to (?:reduce|cut)\b", 2.0, +1),
    (r"\blabor market remains? (?:solid|tight|strong)\b", 1.4, +1),
    (r"\badditional adjustments?\b", 0.6, +1),
    (r"\bquantitative tightening\b", 1.6, +1),
    (r"\beasing cycle\b|\bpolicy easing\b", 2.0, -1),
    (r"\beasing cycle\b|\bpolicy easing\b", 2.2, -1),
    (r"\bdownside risks to employment\b", 2.4, -1),
    (r"\bjob gains have slowed\b", 1.8, -1),
    (r"\bsoftening (?:in )?(?:the )?labor market\b", 1.8, -1),
    (r"\bprogress toward(?:s)? (?:our )?2 percent\b", 1.4, -1),
    (r"\baccommodative\b", 1.8, -1),
    (r"\bready to (?:adjust|ease|cut)\b", 1.2, -1),
    (r"\bquantitative easing\b|\b\bqe\b", 1.8, -1),
    (r"\bdata[- ]dependent\b", 0.4, 0),
    (r"\bincoming (?:data|information)\b", 0.3, 0),
    (r"\bdual mandate\b", 0.2, 0),
    (r"\bwell anchored\b", 0.3, 0),
)

LABELS = (
    (-5.0, -3.5, "très dovish"),
    (-3.5, -1.5, "dovish"),
    (-1.5, -0.4, "légèrement dovish"),
    (-0.4, 0.4, "neutre"),
    (0.4, 1.5, "légèrement hawkish"),
    (1.5, 3.5, "hawkish"),
    (3.5, 5.01, "très hawkish"),
)


def _ssl(insecure=False):
    """Certifi, magasins macOS/Linux, puis (si insecure) sans vérif.

    Sur le Python.org / Homebrew Mac, create_default_context() plante souvent
    en CERTIFICATE_VERIFY_FAILED : Groq et OpenRouter deviennent « IA muette »
    alors que le Test Telegram dit Groq OK (le fichier clé est là).
    """
    if insecure:
        c = ssl._create_unverified_context()
        return c
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        pass
    for cand in (
        os.environ.get("SSL_CERT_FILE"),
        os.environ.get("REQUESTS_CA_BUNDLE"),
        "/etc/ssl/cert.pem",
        "/usr/local/etc/openssl@3/cert.pem",
        "/opt/homebrew/etc/openssl@3/cert.pem",
        "/Library/Frameworks/Python.framework/Versions/Current/etc/openssl/cert.pem",
        "/etc/ssl/certs/ca-certificates.crt",
    ):
        try:
            if cand and os.path.isfile(cand):
                return ssl.create_default_context(cafile=cand)
        except Exception:
            continue
    try:
        return ssl.create_default_context()
    except Exception:
        return ssl._create_unverified_context()


def http_get(url, timeout=14):
    """Télécharge une URL officielle. Plusieurs agents, 2 essais, jamais de valeur inventée."""
    last = None
    headers_list = [
        {"User-Agent": UA, "Accept": "application/rss+xml, application/xml, text/xml, text/html, */*"},
        {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
         "Accept": "application/rss+xml, application/xml, text/xml, text/html, */*",
         "Accept-Language": "en-US,en;q=0.9,fr;q=0.8",
         "Referer": "https://www.ecb.europa.eu/"},
    ]
    ctx = _ssl()
    for headers in headers_list:
        for attempt in (1, 2):
            req = urllib.request.Request(url, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
                    return r.read()
            except Exception as e:
                last = e
                time.sleep(0.25 * attempt)
    raise last


def _parse_date(raw):
    if not raw:
        return None
    try:
        dt = parsedate_to_datetime(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    except Exception:
        m = re.search(r"(\d{4}-\d{2}-\d{2})", raw)
        return (m.group(1) + "T00:00:00+00:00") if m else None


def parse_rss(xml_bytes):
    # BOM + namespaces fréquents sur Fed / BoE.
    text = xml_bytes.decode("utf-8-sig", "replace")
    root = ET.fromstring(text)
    items = []
    for it in root.findall(".//item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        if not link:
            guid = it.find("guid")
            link = ((guid.text if guid is not None else "") or "").strip()
        pub = (
            it.findtext("pubDate")
            or it.findtext("{http://purl.org/dc/elements/1.1/}date")
            or ""
        )
        desc = it.findtext("description") or ""
        if title and link:
            items.append(
                {
                    "title": re.sub(r"\s+", " ", title),
                    "url": link.replace("europa.eu//", "europa.eu/"),
                    "published": _parse_date(pub),
                    "summary": re.sub(r"<[^>]+>", " ", desc)[:400],
                }
            )
    return items


def relevant(bank, title, url):
    blob = title + " " + url
    if _SKIP.search(blob):
        return False
    if bank in ("BCE", "BOJ") and not _KEEP.search(blob):
        return False
    # Communiqués de décision : toujours.
    if re.search(r"fomc statement|monetary policy (?:decision|statement)|bank rate|"
                 r"interest rate decision|outlook for economic activity", blob, re.I):
        return True
    return True



# Affichage francophone. Le score lexique reste sur le texte officiel EN.
_TITLE_FR = (
    ("Minutes of the Federal Open Market Committee", "Compte rendu du FOMC"),
    ("Federal Reserve issues FOMC statement", "La Fed publie le communique FOMC"),
    ("Highlights of the Outlook for Economic Activity and Prices",
     "Points saillants des Perspectives d'activite et des prix"),
    ("Monetary policy statement (with Q&A)", "Declaration de politique monetaire (avec questions)"),
    ("Monetary Policy Summary and Minutes", "Resume de politique monetaire et minutes"),
    ("Bank Rate maintained at", "Taux directeur maintenu a"),
    ("Meeting Accounts", "Comptes rendus de reunion"),
    ("Politique monetaire Meeting Accounts", "Comptes rendus de politique monetaire BCE"),
    ("Results of the Semi-Annual FX Turnover Surveys", "Resultats de l'enquete semestrielle sur les volumes de change"),
    ("Implementation Note", "Note de mise en oeuvre"),
    ("Press Conference", "Conference de presse"),
    ("Testimony", "Audition"),
    ("Speech", "Discours"),
)

_PHRASE_FR = (
    ("The Committee is continuing its policy of maintaining ample reserves in the banking system.",
     "Le Comite poursuit sa politique de reserves abondantes dans le systeme bancaire."),
    ("The Committee reaffirmed its policy of maintaining ample reserves in the banking system.",
     "Le Comite a reaffirme sa politique de reserves abondantes dans le systeme bancaire."),
    ("Voting against the monetary policy action were Beth M.",
     "Ont vote contre la decision : Beth M."),
    ("Logan, who preferred to raise the target range for the federal funds rate by 1/4 percentage point at this meeting.",
     "Logan, qui preferait relever la fourchette des fed funds de 0,25 point lors de cette reunion."),
    ("Inflation remains elevated", "L'inflation reste elevee"),
    ("The Committee seeks to achieve maximum employment and inflation at the rate of 2 percent over the longer run.",
     "Le Comite vise le plein emploi et une inflation a 2 % a long terme."),
)


def title_fr(title):
    s = title or ""
    out = s
    for en, fr_ in sorted(_TITLE_FR, key=lambda x: -len(x[0])):
        if en.lower() in out.lower():
            # replace case-insensitive
            import re as _re
            out = _re.sub(_re.escape(en), fr_, out, flags=_re.I)
    return out


def phrase_fr(s):
    raw = (s or "").strip()
    if not raw:
        return raw
    for en, fr_ in _PHRASE_FR:
        if en.lower() in raw.lower() or raw.lower() in en.lower():
            return fr_
    # titres courts
    tf = title_fr(raw)
    return tf if tf != raw else raw


def extract_pdf_text(raw):
    """Texte d'un PDF. pypdf si present, sinon extraction brute des chaines."""
    if not raw:
        return ""
    try:
        import io
        from pypdf import PdfReader
        r = PdfReader(io.BytesIO(raw))
        parts = []
        for i, page in enumerate(r.pages[:40]):
            try:
                parts.append(page.extract_text() or "")
            except Exception:
                continue
        txt = "\n".join(parts)
        if len(txt) >= 200:
            return txt
    except Exception:
        pass
    # Brut : chaines PDF (Tj)
    try:
        chunks = re.findall(br"\((?:\\.|[^\\)]){4,}\)\s*Tj", raw[:1200000])
        out = []
        for c in chunks:
            s = c[:-2]  # drop )Tj-ish
            s = s.strip()
            if s.startswith(b"("):
                s = s[1:]
            if s.endswith(b")"):
                s = s[:-1]
            try:
                out.append(s.decode("latin-1", "replace"))
            except Exception:
                continue
        txt = " ".join(out)
        txt = re.sub(r"\s+", " ", txt)
        return txt
    except Exception:
        return ""


def fetch_document_text(url):
    """HTML ou PDF officiel. Ne jamais inventer."""
    raw = http_get(url, timeout=25)
    if not raw:
        return ""
    low = (url or "").lower()
    if raw[:4] == b"%PDF" or low.endswith(".pdf"):
        return extract_pdf_text(raw)
    return strip_html(raw)


def classify_kind(bank, title, fallback):

    t = title.lower()
    if "task force" in t or "leadership and objectives" in t:
        return "press"
    if "discount rate meeting" in t:
        return "minutes"
    if "fomc statement" in t or "issues fomc" in t:
        return "statement"
    # BoE : « Monetary Policy Summary and Minutes » = décision, pas un PV interne.
    if "bank rate" in t or "monetary policy summary" in t:
        return "statement"
    if "minutes" in t:
        return "minutes"
    if "projection" in t or "sep" in t or "outlook" in t:
        return "outlook"
    if "testimony" in t or "semiannual" in t:
        return "testimony"
    if "speech" in t or "," in title and bank == "FED" and fallback == "speech":
        return "speech"
    if fallback in ("statement", "speech", "testimony", "news", "press"):
        if fallback == "press":
            return "statement" if re.search(r"decision|statement|rate", t) else "press"
        return fallback
    return "press"


def strip_html(raw):
    t = raw.decode("utf-8", "replace") if isinstance(raw, (bytes, bytearray)) else raw
    t = re.sub(r"(?is)<script[^>]*>.*?</script>", " ", t)
    t = re.sub(r"(?is)<style[^>]*>.*?</style>", " ", t)
    t = re.sub(r"(?is)<noscript[^>]*>.*?</noscript>", " ", t)
    # Corps Fed : #article → #lastUpdate
    i = t.find('id="article"')
    j = t.find('id="lastUpdate"')
    if i != -1:
        chunk = t[i : (j if j > i else i + 80000)]
    else:
        m = re.search(r'(?is)<(?:article|main)[^>]*>(.*)</(?:article|main)>', t)
        chunk = m.group(1) if m else t
    chunk = re.sub(r"(?is)<li class='shareDL__item'>.*?</li>", " ", chunk)
    paras = []
    for p in re.findall(r"(?is)<p[^>]*>(.*?)</p>", chunk):
        s = re.sub(r"(?is)<[^>]+>", " ", p)
        s = htmlmod.unescape(s)
        s = re.sub(r"\s+", " ", s).strip()
        if len(s) < 50:
            continue
        if re.search(r"^(Share|Last Update|For release|Implementation Note)\b", s):
            continue
        if "share?url=" in s or "facebook.com/sharer" in s:
            continue
        paras.append(s)
    text = "\n\n".join(paras)
    if len(text) < 200:
        # Repli : tout le texte visible du chunk.
        s = re.sub(r"(?is)<[^>]+>", " ", chunk)
        s = htmlmod.unescape(s)
        s = re.sub(r"\s+", " ", s).strip()
        text = s[:MAX_CHARS]
    return text[:MAX_CHARS]


def detect_action(text):
    """Décision = première phrase du comité, jamais le paragraphe de vote."""
    head = re.split(r"\bVoting against\b", text or "", maxsplit=1)[0]
    # Pas seulement la 1re phrase : la BCE met la décision au 2e paragraphe.
    head = head.lower()[:900]
    if re.search(
        r"maintain(?:ed)? the target range|"
        r"left (?:the )?(?:bank |policy )?rate unchanged|"
        r"decided to keep|"
        r"keep the three key (?:ecb )?interest rates unchanged|"
        r"bank rate maintained|"
        r"rates unchanged",
        head,
    ):
        return "hold", 0.0
    if re.search(r"lower(?:ed)? the target range|decided to reduce", head):
        return "cut", -2.2
    if re.search(r"raise(?:d)? the target range|increase(?:d)? the (?:bank |policy )?rate by", head):
        return "hike", 2.2
    return "unclear", 0.0


def score_text(text, kind="speech"):
    if not text:
        return {"score": 0.0, "label": "neutre", "hawk": 0.0, "dove": 0.0, "hits": [], "action": "unclear"}
    blob = text.lower()
    hawk = dove = 0.0
    hits = []
    for rx, w, sens in LEXICON:
        n = len(re.findall(rx, blob))
        if not n:
            continue
        # Un discours parle souvent de « hike » au conditionnel : on plafonne.
        if kind not in ("statement", "minutes") and n > 2:
            n = 2
        if sens > 0:
            hawk += w * n
        elif sens < 0:
            dove += w * n
        hits.append({"rx": rx, "n": n, "w": w, "sens": sens})
    action, base = detect_action(text)
    # Dissidence : minorité, donc petit ajustement, pas le score entier.
    dissent = 0.0
    if re.search(r"voting against.{0,180}preferred to raise", blob, re.S):
        dissent += 0.6
        hits.append({"rx": "dissent:raise", "n": 1, "w": 0.6, "sens": 1})
    if re.search(r"voting against.{0,180}preferred to (?:lower|cut|reduce)", blob, re.S):
        dissent -= 0.6
        hits.append({"rx": "dissent:cut", "n": 1, "w": 0.6, "sens": -1})
    denom = hawk + dove
    tilt = 0.0 if denom <= 0 else (hawk - dove) / (denom + 2.0) * 2.0  # ±2 max
    if kind == "statement":
        scaled = base + tilt + dissent
    else:
        scaled = tilt * 1.2
        scaled = max(-2.5, min(2.5, scaled))
    scaled = max(-5.0, min(5.0, scaled))
    score = round(scaled, 1)
    label = "neutre"
    for a, b, lab in LABELS:
        if a <= score < b:
            label = lab
            break
    return {
        "score": score,
        "label": label,
        "hawk": round(hawk, 2),
        "dove": round(dove, 2),
        "hits": hits[:20],
        "action": action,
    }


def word_diff(old, new):
    """Phrases ajoutées / retirées entre deux communiqués."""
    def toks(s):
        parts = re.split(r"(?<=[.!?])\s+", (s or "").strip())
        return [re.sub(r"\s+", " ", p).strip() for p in parts if len(p.strip()) > 40]

    a, b = toks(old), toks(new)
    sm = SequenceMatcher(a=a, b=b, autojunk=False)
    added, removed = [], []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "insert":
            added.extend(b[j1:j2])
        elif tag == "delete":
            removed.extend(a[i1:i2])
        elif tag == "replace":
            removed.extend(a[i1:i2])
            added.extend(b[j1:j2])
    return {"added": added[:12], "removed": removed[:12]}


def directive(bank, score, label, kind):
    """Conséquences de régime — pas un signal d'entrée."""
    if abs(score) < 0.4:
        tone = "La lecture est data-dependent : pas de hiérarchie nouvelle entre inflation et activité."
        assets = {k: "neutre" for k in ("XAUUSD", "USD", "EURUSD", "GBPUSD", "USDJPY", "US10Y")}
    elif score > 0:
        tone = (
            "Ton restrictif : la banque priorise l'inflation / refuse d'assouplir."
            " Régime typique : devise locale soutenue, or sous pression, taux longs plus fermes."
        )
        assets = {
            "XAUUSD": "baissier",
            "USD": "haussier" if bank == "FED" else "neutre",
            "EURUSD": "baissier" if bank == "FED" else ("haussier" if bank == "BCE" else "neutre"),
            "GBPUSD": "baissier" if bank == "FED" else ("haussier" if bank == "BOE" else "neutre"),
            "USDJPY": "haussier" if bank == "FED" else ("baissier" if bank == "BOJ" else "neutre"),
            "US10Y": "haussier" if bank == "FED" else "neutre",
        }
    else:
        tone = (
            "Ton accommodant : la banque ouvre la porte à un assouplissement"
            " ou insiste sur les risques d'activité. Régime typique : devise plus faible, or soutenu."
        )
        assets = {
            "XAUUSD": "haussier",
            "USD": "baissier" if bank == "FED" else "neutre",
            "EURUSD": "haussier" if bank == "FED" else ("baissier" if bank == "BCE" else "neutre"),
            "GBPUSD": "haussier" if bank == "FED" else ("baissier" if bank == "BOE" else "neutre"),
            "USDJPY": "baissier" if bank == "FED" else ("haussier" if bank == "BOJ" else "neutre"),
            "US10Y": "baissier" if bank == "FED" else "neutre",
        }
    caveat = " Lecture de ton, pas un signal d'entrée et pas un taux de réussite."
    if kind not in ("statement", "minutes", "outlook"):
        caveat += " Un discours pèse moins qu'un communiqué de décision."
    return {
        "plain_fr": tone + caveat,
        "assets": assets,
        "label": label,
        "score": score,
    }


def _ensure_dirs():
    os.makedirs(ITEMS_DIR, exist_ok=True)


def _load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _save_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _sid(bank, kind, published, url):
    day = (published or "")[:10].replace("-", "") or "na"
    tail = hashlib.sha256(url.encode("utf-8")).hexdigest()[:8]
    return f"{bank}-{kind}-{day}-{tail}"


def last_statement_text(bank, exclude_id=None, before=None):
    idx = _load_json(os.path.join(DIR, "index.json"), {"items": []})
    cands = [
        it
        for it in idx.get("items", [])
        if it.get("bank") == bank and it.get("kind") == "statement" and it.get("id") != exclude_id
        and it.get("chars", 0) >= 350
    ]
    if before:
        cands = [it for it in cands if (it.get("published") or "") < before]
    cands.sort(key=lambda x: x.get("published") or "", reverse=True)
    if not cands:
        return None
    p = os.path.join(ITEMS_DIR, cands[0]["id"] + ".json")
    doc = _load_json(p, None)
    return (doc or {}).get("text")


def rotate(index):
    by = {}
    keep = []
    # plus récent d'abord
    items = sorted(index.get("items", []), key=lambda x: x.get("published") or "", reverse=True)
    for it in items:
        key = (it.get("bank"), "stmt" if it.get("kind") == "statement" else "other")
        by.setdefault(key, 0)
        cap = MAX_STATEMENTS if key[1] == "stmt" else MAX_SPEECHES
        if by[key] >= cap:
            fp = os.path.join(ITEMS_DIR, it["id"] + ".json")
            if os.path.isfile(fp):
                try:
                    os.remove(fp)
                except OSError:
                    pass
            continue
        by[key] += 1
        keep.append(it)
    index["items"] = keep
    return index


def upsert_item(meta, text, scored, diff, direc):
    _ensure_dirs()
    index_path = os.path.join(DIR, "index.json")
    banks_path = os.path.join(DIR, "banks.json")
    index = _load_json(index_path, {"items": [], "updated": None})
    existing = {it["id"]: i for i, it in enumerate(index["items"])}
    light = {
        "id": meta["id"],
        "bank": meta["bank"],
        "kind": meta["kind"],
        "published": meta.get("published"),
        "title": meta["title"],
        "url": meta["url"],
        "hash": meta["hash"],
        "score": scored["score"],
        "label": scored["label"],
        "chars": len(text or ""),
    }
    if meta["id"] in existing:
        index["items"][existing[meta["id"]]] = light
    else:
        index["items"].insert(0, light)
    index["updated"] = datetime.now(timezone.utc).isoformat()
    index = rotate(index)
    _save_json(index_path, index)

    doc = dict(meta)
    doc["text"] = text
    doc["score"] = scored
    doc["diff"] = diff
    doc["directive"] = direc
    _save_json(os.path.join(ITEMS_DIR, meta["id"] + ".json"), doc)

    recompute_banks(index)
    return light


def recompute_banks(index=None):
    """Score banque = 70 % dernier communiqué + 30 % moyenne des 3 derniers docs scorés."""
    if index is None:
        index = _load_json(os.path.join(DIR, "index.json"), {"items": []})
    banks = {}
    now = datetime.now(timezone.utc).isoformat()
    for bank in BANKS:
        own = [it for it in index.get("items", []) if it.get("bank") == bank]
        own.sort(key=lambda x: x.get("published") or "", reverse=True)
        stmts = [it for it in own if it.get("kind") == "statement" and it.get("chars", 0) >= 350]
        scored = [it for it in own if it.get("chars", 0) >= 350]
        last_stmt = stmts[0]["score"] if stmts else 0.0
        recent = scored[:3]
        avg = sum(it.get("score") or 0 for it in recent) / max(1, len(recent))
        blended = round(0.7 * last_stmt + 0.3 * avg, 1) if (stmts or recent) else 0.0
        lab = "neutre"
        for a, b, l in LABELS:
            if a <= blended < b:
                lab = l
                break
        head = stmts[0] if stmts else None
        if not head:
            long_own = [it for it in own if (it.get("chars") or 0) >= 350]
            head = long_own[0] if long_own else (own[0] if own else None)
        banks[bank] = {
            "score": blended,
            "label": lab,
            "last_id": (head or {}).get("id"),
            "last_title": (head or {}).get("title"),
            "last_published": (head or {}).get("published"),
            "updated": now,
        }
    _save_json(os.path.join(DIR, "banks.json"), banks)
    return banks


def ingest(bank, kind, title, url, published, fetch_body=True):
    kind = classify_kind(bank, title, kind)
    body = ""
    err = None
    if fetch_body and url and not url.lower().endswith((".xlsx", ".xls", ".csv")):
        try:
            body = fetch_document_text(url)
        except Exception as e:
            body = ""
            err = str(e)
    if (not body or len(body) < 350) and url:
        for key, fb in FALLBACK_TEXT.items():
            if key in url:
                body = fb
                err = (err or "") + "|repli texte officiel embarqué"
                break
    text = body if body and len(body) >= 350 else (body or title)
    short = len(text) < 350
    h = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()
    sid = _sid(bank, kind, published, url)
    # Déjà vu (même hash) : on ne rescore pas.
    prev = _load_json(os.path.join(ITEMS_DIR, sid + ".json"), None)
    prev_len = len((prev or {}).get("text") or "")
    if prev and prev.get("hash") == h and prev_len >= 350:
        return {"id": sid, "skipped": "unchanged"}
    scored = score_text(text, kind) if not short else {
        "score": 0.0, "label": "neutre", "hawk": 0.0, "dove": 0.0,
        "hits": [], "action": "unclear",
        "note": "texte trop court (souvent un PDF, non lu)",
    }
    diff = {"added": [], "removed": []}
    if kind == "statement":
        older = last_statement_text(bank, exclude_id=sid, before=published)
        if older:
            diff = word_diff(older, text)
    direc = directive(bank, scored["score"], scored["label"], kind)
    meta = {
        "id": sid,
        "bank": bank,
        "kind": kind,
        "title": title,
        "url": url,
        "published": published,
        "hash": h,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "fetch_error": err,
    }
    return upsert_item(meta, text, scored, diff, direc)


def poll_feeds(only_bank=None, limit_per_feed=8, fetch_body=True):
    _ensure_dirs()
    POLL["running"] = True
    POLL["started"] = datetime.now(timezone.utc).isoformat()
    POLL["finished"] = None
    POLL["errors"] = []
    POLL["ingested"] = 0
    POLL["feeds_ok"] = 0
    POLL["feeds_fail"] = 0
    out = []
    try:
        for feed in FEEDS:
            if only_bank and feed["bank"] != only_bank:
                continue
            try:
                items = parse_rss(http_get(feed["url"], timeout=10))
                POLL["feeds_ok"] += 1
            except Exception as e:
                err = str(e)[:160]
                POLL["feeds_fail"] += 1
                POLL["errors"].append({"feed": feed["url"], "error": err})
                out.append({"feed": feed["url"], "error": err})
                continue
            n = 0
            for it in items:
                if not relevant(feed["bank"], it["title"], it["url"]):
                    continue
                want = fetch_body and needs_body(feed["kind"], it["title"])
                rec = ingest(
                    feed["bank"],
                    feed["kind"],
                    it["title"],
                    it["url"],
                    it["published"],
                    fetch_body=want,
                )
                rec["feed"] = feed["url"]
                out.append(rec)
                if not rec.get("skipped"):
                    POLL["ingested"] += 1
                n += 1
                if n >= limit_per_feed:
                    break
                time.sleep(0.12)
        _bootstrap_known_docs(only_bank)
        refresh_statement_diffs(only_bank)
    finally:
        POLL["running"] = False
        POLL["finished"] = datetime.now(timezone.utc).isoformat()
    return out


def _url_already_scored(url):
    idx = _load_json(os.path.join(DIR, "index.json"), {"items": []})
    u = (url or "").rstrip("/")
    for it in idx.get("items") or []:
        if (it.get("url") or "").rstrip("/") == u and (it.get("chars") or 0) >= 350:
            return True
    return False


def _bootstrap_known_docs(only_bank=None):
    """Complète le magasin avec les derniers communiqués officiels connus.

    On n'arrête PAS au premier statement d'une banque : le Statement Diff
    a besoin de DEUX textes (dernier vs précédent).
    """
    for doc in KNOWN_DOCS:
        if only_bank and doc["bank"] != only_bank:
            continue
        if _url_already_scored(doc["url"]):
            continue
        # URL déjà là mais texte trop court : on réessaie (cas BCE mesuré).
        try:
            rec = ingest(
                doc["bank"], doc["kind"], doc["title"], doc["url"],
                doc["published"], fetch_body=True,
            )
            if rec and not rec.get("skipped"):
                POLL["ingested"] = int(POLL.get("ingested") or 0) + 1
        except Exception as e:
            POLL["errors"].append({"feed": doc["url"], "error": str(e)[:160]})


def refresh_statement_diffs(only_bank=None):
    """Recalcule le diff du dernier communiqué une fois N-1 connu."""
    idx = _load_json(os.path.join(DIR, "index.json"), {"items": []})
    banks = [only_bank] if only_bank else list(BANKS)
    for bank in banks:
        stmts = [
            it for it in idx.get("items", [])
            if it.get("bank") == bank and it.get("kind") == "statement" and it.get("chars", 0) >= 350
        ]
        stmts.sort(key=lambda x: x.get("published") or "")
        if len(stmts) < 2:
            continue
        newer, older = stmts[-1], stmts[-2]
        new_doc = _load_json(os.path.join(ITEMS_DIR, newer["id"] + ".json"), None)
        old_doc = _load_json(os.path.join(ITEMS_DIR, older["id"] + ".json"), None)
        if not new_doc or not old_doc:
            continue
        new_doc["diff"] = word_diff(old_doc.get("text"), new_doc.get("text"))
        new_doc["diff_vs"] = older["id"]
        _save_json(os.path.join(ITEMS_DIR, newer["id"] + ".json"), new_doc)


def poll_state():
    return dict(POLL)


def needs_body(kind, title):
    """Telecharger le corps des docs de POLITIQUE. Pas les stats / green notice."""
    t = (title or "").lower()
    if re.search(r"green notice|fx turnover|current account balances|"
                 r"statistical release|money market operations", t):
        return False
    if kind in ("statement", "minutes", "outlook", "speech", "testimony", "press"):
        return True
    return bool(re.search(
        r"fomc|press conference|summary of opinions|monetary policy|"
        r"interest rate decision|bank rate|outlook for economic",
        t,
    ))


def snapshot():
    import copy
    banks = copy.deepcopy(_load_json(os.path.join(DIR, "banks.json"), {}))
    idx = copy.deepcopy(_load_json(os.path.join(DIR, "index.json"), {"items": []}))
    for k, b in (banks or {}).items():
        if isinstance(b, dict) and b.get("last_title"):
            b["last_title_fr"] = title_fr(b.get("last_title"))
    items = []
    for it in (idx.get("items") or []):
        if not isinstance(it, dict):
            continue
        it = dict(it)
        it["title_fr"] = title_fr(it.get("title") or "")
        items.append(it)
    # Un meme jour + meme type = une seule ligne (RSS titre seul vs HTML lu).
    best = {}
    for it in items:
        day = (it.get("published") or "")[:10]
        key = (it.get("bank"), it.get("kind"), day or (it.get("title") or "")[:60])
        chars = int(it.get("chars") or 0)
        old = best.get(key)
        if not old or chars > int(old.get("chars") or 0):
            best[key] = it
        elif old and abs(float(it.get("score") or 0)) > abs(float(old.get("score") or 0)):
            best[key] = it
    items = list(best.values())
    items.sort(key=lambda x: x.get("published") or "", reverse=True)
    idx["items"] = items

    def _ex(bank, kind, n=3800):
        cands = [it for it in items if it.get("bank") == bank and it.get("kind") == kind]
        cands.sort(key=lambda x: x.get("published") or "", reverse=True)
        for it in cands:
            doc = _load_json(os.path.join(ITEMS_DIR, it.get("id", "") + ".json"), None)
            txt = ((doc or {}).get("text") or "").strip()
            if len(txt) < 350:
                continue
            return {
                "id": it.get("id"),
                "title": it.get("title"),
                "title_fr": it.get("title_fr") or title_fr(it.get("title") or ""),
                "published": it.get("published"),
                "score": it.get("score"),
                "label": it.get("label"),
                "chars": len(txt),
                "excerpt": txt[:n],
            }
        return None

    excerpts = {
        "FED_statement": _ex("FED", "statement"),
        "FED_minutes": _ex("FED", "minutes", 5000),
        "BCE_statement": _ex("BCE", "statement"),
        "BOE_statement": _ex("BOE", "statement"),
        "BOJ_outlook": _ex("BOJ", "outlook"),
    }
    docs = []
    for it in items[:14]:
        doc = _load_json(os.path.join(ITEMS_DIR, it.get("id", "") + ".json"), None)
        txt = ((doc or {}).get("text") or "").strip()
        docs.append({
            "id": it.get("id"),
            "bank": it.get("bank"),
            "kind": it.get("kind"),
            "title": it.get("title_fr") or it.get("title"),
            "published": it.get("published"),
            "score": it.get("score"),
            "chars": len(txt),
            "lu": len(txt) >= 350,
            "excerpt": (txt[:2200] if len(txt) >= 350 else ""),
        })
    return {"banks": banks, "index": idx, "excerpts": excerpts, "docs": docs}


# ── Couche LLM Groq (optionnelle) ─────────────────────────────────────────
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
OR_URL = "https://openrouter.ai/api/v1/chat/completions"
# Choix mesuré 18/08/2026 (clés du proprio) :
#   Groq vivant et propre pour le chat : qwen/qwen3.6-27b + reasoning_effort=none
#     (0,23 s, phrases finies). Llama 3.3 specdec/versatile = mort.
#   gpt-oss Groq : marche mais coupe et raisonne — dernier recours seulement.
#   groq/compound : pas un scorer hawk/dove.
#   OpenRouter :free réellement OK : Gemma 4 26B puis 31B.
#     llama/qwen/gemini :free demandés = 404. nemotron dump think / vide.
GROQ_MODEL = "qwen/qwen3.6-27b"
GROQ_FALLBACKS = (
    "openai/gpt-oss-20b",
)
OR_MODELS = (
    "google/gemma-4-26b-a4b-it:free",
    "google/gemma-4-31b-it:free",
)
_GROQ_MODEL_OK = None
_LLM_LABELS = (
    "très dovish", "dovish", "légèrement dovish", "neutre",
    "légèrement hawkish", "hawkish", "très hawkish",
)


def _prompt_path():
    here = os.path.dirname(os.path.abspath(__file__))
    for cand in (
        os.path.join(here, "cb_intel_prompt.md"),
        os.path.join(os.getcwd(), "cb_intel_prompt.md"),
    ):
        if os.path.isfile(cand):
            return cand
    return None


def _system_prompt():
    path = _prompt_path()
    if path:
        try:
            return open(path, encoding="utf-8").read()
        except Exception:
            pass
    return (
        "Tu es un économiste de marché. Tu lis UN document de banque centrale. "
        "Réponds uniquement en JSON : score, label, confidence, plain_fr, "
        "what_changed, implied_path, assets, why. Pas de signal d'entrée."
    )


def groq_chat(key, messages, model=None, max_tokens=2200, disable_thinking=True, strict_model=False):
    """Appel Groq. Essaie le modèle demandé, puis le repli si 404.

    Qwen 3.6 pense par défaut : sans reasoning_effort=none, max_tokens
    part dans <think> et l'utilisateur voit une phrase coupée, puis
    un fragment du type «...ban» si on lui demande de continuer.
    """
    global _GROQ_MODEL_OK
    import ssl
    key = (key or "").strip()
    if not key:
        return None, "clé absente"
    models = []
    if model:
        models.append(model)
    if not strict_model:
        if _GROQ_MODEL_OK and _GROQ_MODEL_OK not in models:
            models.insert(0, _GROQ_MODEL_OK)
        for m in (GROQ_MODEL,) + GROQ_FALLBACKS:
            if m not in models:
                models.append(m)
    last_err = "échec"
    extras = []
    if disable_thinking:
        extras.append({"reasoning_effort": "none", "reasoning_format": "hidden"})
    extras.append({})
    seen_extra = set()
    for m in models:
        for extra in extras:
            sig = tuple(sorted(extra.items()))
            if (m, sig) in seen_extra:
                continue
            seen_extra.add((m, sig))
            body = {
                "model": m,
                "messages": messages,
                "temperature": 0.15,
                "max_tokens": max_tokens,
            }
            body.update(extra)
            payload = json.dumps(body).encode("utf-8")
            req = urllib.request.Request(
                GROQ_URL,
                data=payload,
                method="POST",
                headers={
                    "Authorization": "Bearer " + key,
                    "Content-Type": "application/json",
                    "User-Agent": "curl/8.5.0",
                    "Accept": "application/json",
                },
            )
            try:
                data = None
                last_ssl = None
                for insecure in (False, True):
                    try:
                        with urllib.request.urlopen(req, timeout=12, context=_ssl(insecure)) as r:
                            data = json.loads(r.read().decode("utf-8", "replace"))
                        break
                    except Exception as e1:
                        last_ssl = e1
                        if "CERTIFICATE" not in str(e1).upper() and "SSL" not in str(e1).upper():
                            raise
                if data is None:
                    raise last_ssl or RuntimeError("groq ssl")
                msg = ((data.get("choices") or [{}])[0].get("message") or {})
                content = (msg.get("content") or "").strip()
                if not content:
                    last_err = "réponse vide"
                    continue
                if "compound" not in m:
                    _GROQ_MODEL_OK = m
                return {"content": content, "model": data.get("model") or m}, None
            except Exception as e:
                err = str(e)
                http_body = ""
                try:
                    http_body = e.read().decode("utf-8", "replace")[:240]  # type: ignore
                except Exception:
                    pass
                last_err = (http_body or err)[:240]
                low = last_err.lower()
                if any(x in low for x in (
                    "model_not_found", "does not exist", "decommissioned",
                    "no longer supported", "not found",
                )):
                    break
                if extra and "reasoning" in low:
                    continue
                # 429 / quota : on tente le modèle Groq suivant, puis OpenRouter.
                if "429" in last_err or "rate" in low or "quota" in low:
                    llm_cool("groq", 90)
                    return None, "429 quota Groq"
                break
    return None, last_err


def openrouter_chat(key, messages, max_tokens=900):
    """Secours OpenRouter. Headers Referer + X-Title exigés."""
    import ssl
    key = (key or "").strip()
    if not key:
        return None, "clé OpenRouter absente"
    last_err = "échec"
    for m in OR_MODELS:
        body = {
            "model": m,
            "messages": messages,
            "temperature": 0.2,
            "max_tokens": max_tokens,
        }
        payload = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            OR_URL,
            data=payload,
            method="POST",
            headers={
                "Authorization": "Bearer " + key,
                "Content-Type": "application/json",
                "User-Agent": "curl/8.5.0",
                "Accept": "application/json",
                "HTTP-Referer": "https://omnitradehub.local",
                "X-Title": "OmniTrade Hub",
            },
        )
        try:
            data = None
            last_ssl = None
            for insecure in (False, True):
                try:
                    with urllib.request.urlopen(req, timeout=12, context=_ssl(insecure)) as r:
                        data = json.loads(r.read().decode("utf-8", "replace"))
                    break
                except Exception as e1:
                    last_ssl = e1
                    if "CERTIFICATE" not in str(e1).upper() and "SSL" not in str(e1).upper():
                        raise
            if data is None:
                raise last_ssl or RuntimeError("or ssl")
            msg = ((data.get("choices") or [{}])[0].get("message") or {})
            content = (msg.get("content") or "").strip()
            if not content:
                last_err = "réponse vide " + m
                continue
            return {"content": content, "model": data.get("model") or m, "provider": "openrouter"}, None
        except Exception as e:
            http_body = ""
            try:
                http_body = e.read().decode("utf-8", "replace")[:240]  # type: ignore
            except Exception:
                pass
            last_err = (http_body or str(e))[:240]
            low = last_err.lower()
            if "429" in last_err or "quota" in low or "rate" in low:
                llm_cool("openrouter", 90)
                return None, "429 quota openrouter"
            continue
    return None, last_err


def web_brief(key, question, max_tokens=500):
    """Recherche web via groq/compound-mini. Jamais pour le score hawk/dove."""
    q = (question or "").strip()
    if not q:
        return None, "question vide"
    messages = [
        {
            "role": "system",
            "content": (
                "Francais, 4 phrases maximum. Cite tes sources web. "
                "Or = XAUUSD metal, pas un jeu. Pas de signal d'entree, "
                "pas de promesse de gain, pas de chiffre invente."
            ),
        },
        {"role": "user", "content": q[:600]},
    ]
    # Appel cible : pas de repli Qwen (pas de web).
    out, err = groq_chat(
        key, messages, model="groq/compound-mini",
        max_tokens=max_tokens, disable_thinking=True, strict_model=True,
    )
    if out and out.get("content"):
        out["provider"] = "groq-web"
        return out, None
    return None, err or "web indisponible"


GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"
GEMINI_MODELS = ("gemini-3.5-flash", "gemini-3.1-flash-lite", "gemini-3.7-flash", "gemini-flash-latest")
CEREBRAS_URL = "https://api.cerebras.ai/v1/chat/completions"
MISTRAL_URL = "https://api.mistral.ai/v1/chat/completions"
MISTRAL_CHAT_MODELS = ("mistral-small-latest", "mistral-medium-latest")
MISTRAL_FAST_MODELS = ("ministral-8b-latest", "mistral-small-latest")
NVIDIA_URL = "https://integrate.api.nvidia.com/v1/chat/completions"
NVIDIA_CHAT_MODELS = ("mistralai/mistral-nemotron", "meta/llama-3.1-8b-instruct", "google/gemma-4-31b-it")
NVIDIA_FAST_MODELS = ("meta/llama-3.1-8b-instruct", "mistralai/mistral-nemotron")
CEREBRAS_MODELS = ("gemma-4-31b", "gpt-oss-120b")


_LLM_SKIP = {}


def llm_cooling(name):
    try:
        return time.time() < float(_LLM_SKIP.get(name) or 0)
    except Exception:
        return False


def llm_cool(name, sec=90):
    _LLM_SKIP[name] = time.time() + max(20, int(sec))


def openai_compat_chat(url, key, models, messages, max_tokens=900, extra_headers=None, provider=""):
    """Chat OpenAI-compatible (Gemini, Cerebras). SSL retry comme Groq."""
    import ssl
    key = (key or "").strip()
    if not key:
        return None, "cle absente"
    last_err = "echec"
    headers = {
        "Authorization": "Bearer " + key,
        "Content-Type": "application/json",
        "User-Agent": "curl/8.5.0",
        "Accept": "application/json",
    }
    if extra_headers:
        headers.update({k: v for k, v in extra_headers.items() if v})
        if extra_headers.get("x-goog-api-key"):
            headers.pop("Authorization", None)
    for m in models:
        body = {
            "model": m,
            "messages": messages,
            "temperature": 0.2,
            "max_tokens": max_tokens,
        }
        payload = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(url, data=payload, method="POST", headers=headers)
        try:
            data = None
            last_ssl = None
            for insecure in (False, True):
                try:
                    with urllib.request.urlopen(req, timeout=12, context=_ssl(insecure)) as r:
                        data = json.loads(r.read().decode("utf-8", "replace"))
                    break
                except Exception as e1:
                    last_ssl = e1
                    if "CERTIFICATE" not in str(e1).upper() and "SSL" not in str(e1).upper():
                        raise
            if data is None:
                raise last_ssl or RuntimeError(provider + " ssl")
            msg = ((data.get("choices") or [{}])[0].get("message") or {})
            content = (msg.get("content") or "").strip()
            if not content:
                last_err = "reponse vide " + m
                continue
            return {"content": content, "model": data.get("model") or m, "provider": provider}, None
        except Exception as e:
            http_body = ""
            try:
                http_body = e.read().decode("utf-8", "replace")[:240]  # type: ignore
            except Exception:
                pass
            last_err = (http_body or str(e))[:240]
            low = last_err.lower()
            if "429" in last_err or "quota" in low or "rate" in low:
                llm_cool(provider or "llm", 90)
                return None, "429 quota " + (provider or "llm")
            continue
    return None, last_err


def gemini_native(key, messages, max_tokens=900):
    """API Google native generateContent — celle qui marche avec une cle AIza."""
    key = (key or "").strip()
    if not key:
        return None, "cle absente"
    sys = " ".join(m.get("content") or "" for m in messages if m.get("role") == "system")
    parts = []
    for m in messages:
        role = m.get("role")
        txt = str(m.get("content") or "")
        if role == "system":
            continue
        if role == "assistant":
            parts.append("Assistant: " + txt)
        else:
            parts.append(txt)
    user = "\n".join(parts)[:12000]
    body = {
        "contents": [{"role": "user", "parts": [{"text": user}]}],
        "generationConfig": {
            "maxOutputTokens": int(max_tokens or 800),
            "temperature": 0.2,
            "thinkingConfig": {"thinkingBudget": 0},
        },
    }
    if sys.strip():
        body["systemInstruction"] = {"parts": [{"text": sys[:8000]}]}
    payload = json.dumps(body).encode("utf-8")
    models = (
        "gemini-3.5-flash", "gemini-3.1-flash-lite", "gemini-3.7-flash",
        "gemini-flash-latest",
    )
    last_err = "echec"
    for model in models:
        url = "https://generativelanguage.googleapis.com/v1beta/models/%s:generateContent?key=%s" % (model, key)
        req = urllib.request.Request(
            url, data=payload, method="POST",
            headers={"Content-Type": "application/json", "User-Agent": "curl/8.5.0",
                     "x-goog-api-key": key, "Accept": "application/json"},
        )
        try:
            data = None
            last_ssl = None
            for insecure in (False, True):
                try:
                    with urllib.request.urlopen(req, timeout=12, context=_ssl(insecure)) as r:
                        data = json.loads(r.read().decode("utf-8", "replace"))
                    break
                except Exception as e1:
                    last_ssl = e1
                    if "CERTIFICATE" not in str(e1).upper() and "SSL" not in str(e1).upper():
                        raise
            if data is None:
                raise last_ssl or RuntimeError("gemini ssl")
            cands = data.get("candidates") or []
            parts_out = (((cands[0] or {}).get("content") or {}).get("parts") or []) if cands else []
            content = " ".join(str(x.get("text") or "") for x in parts_out).strip()
            if content:
                return {"content": content, "model": model, "provider": "gemini"}, None
            last_err = "reponse vide " + model
        except Exception as e:
            http_body = ""
            try:
                http_body = e.read().decode("utf-8", "replace")[:200]  # type: ignore
            except Exception:
                pass
            last_err = (http_body or str(e))[:200].replace(key, "…")
            low = last_err.lower()
            if "429" in last_err or "quota" in low or "resource_exhausted" in low:
                llm_cool("gemini", 60)
                return None, "429 quota gemini"
            continue
    return None, last_err


def gemini_chat(key, messages, max_tokens=900):
    out, err = gemini_native(key, messages, max_tokens=max_tokens)
    if out:
        return out, None
    out2, err2 = openai_compat_chat(GEMINI_URL, key, GEMINI_MODELS, messages, max_tokens=max_tokens, provider="gemini")
    if out2:
        return out2, None
    return None, err or err2


def cerebras_chat(key, messages, max_tokens=900):
    return openai_compat_chat(CEREBRAS_URL, key, CEREBRAS_MODELS, messages, max_tokens=max_tokens, provider="cerebras")


def mistral_chat(key, messages, max_tokens=900, job="chat"):
    models = MISTRAL_FAST_MODELS if job == "translate" else MISTRAL_CHAT_MODELS
    return openai_compat_chat(MISTRAL_URL, key, models, messages, max_tokens=max_tokens, provider="mistral")


def nvidia_chat(key, messages, max_tokens=900, job="chat"):
    models = NVIDIA_FAST_MODELS if job == "translate" else NVIDIA_CHAT_MODELS
    return openai_compat_chat(NVIDIA_URL, key, models, messages, max_tokens=max_tokens, provider="nvidia")


def llm_chat(groq_key, messages, model=None, max_tokens=2200, disable_thinking=True, or_key=None,
             gemini_key=None, cerebras_key=None, mistral_key=None, nvidia_key=None, job="chat"):
    """Dispatch par tache + cooldown 429.
    chat : Groq si OK, sinon Gemini -> Cerebras -> OpenRouter.
    translate : Cerebras -> Gemini (pas Groq si 429, pas OpenRouter).
    """
    groq_key = (groq_key or "").strip()
    gemini_key = (gemini_key or "").strip()
    cerebras_key = (cerebras_key or "").strip()
    mistral_key = (mistral_key or "").strip()
    nvidia_key = (nvidia_key or "").strip()
    or_key = (or_key or "").strip()
    job = (job or "chat").lower()
    # Mesure 20/08/2026 (cles proprio) : Mistral 0.4s + 50 req/min ;
    # NVIDIA 8b/nemotron 0.2s ; Gemini 3.5 OK ; Groq souvent 429 ; OR 50/j ; Cerebras 402.
    ranked = ["mistral", "nvidia", "gemini", "groq", "openrouter", "cerebras"]
    have = {
        "mistral": mistral_key, "nvidia": nvidia_key, "gemini": gemini_key,
        "groq": groq_key, "openrouter": or_key, "cerebras": cerebras_key,
    }
    if job == "translate":
        ranked = ["mistral", "nvidia", "gemini"]
    order = []
    for name in ranked:
        if not have.get(name):
            continue
        if llm_cooling(name):
            continue
        order.append(name)
    if not order:
        order = [n for n in ranked if have.get(n)]
    err = None
    mt = min(max_tokens, 1200 if job == "translate" else max_tokens)
    for name in order:
        out = None
        if name == "groq" and groq_key and not llm_cooling("groq"):
            out, err = groq_chat(groq_key, messages, model=model, max_tokens=mt,
                                 disable_thinking=disable_thinking)
        elif name == "gemini" and gemini_key and not llm_cooling("gemini"):
            out, err = gemini_chat(gemini_key, messages, max_tokens=min(mt, 1200))
        elif name == "mistral" and mistral_key and not llm_cooling("mistral"):
            out, err = mistral_chat(mistral_key, messages, max_tokens=min(mt, 1200), job=job)
        elif name == "nvidia" and nvidia_key and not llm_cooling("nvidia"):
            out, err = nvidia_chat(nvidia_key, messages, max_tokens=min(mt, 1200), job=job)
        elif name == "cerebras" and cerebras_key and not llm_cooling("cerebras"):
            out, err = cerebras_chat(cerebras_key, messages, max_tokens=min(mt, 1200))
        elif name == "openrouter" and or_key and not llm_cooling("openrouter"):
            out, err = openrouter_chat(or_key, messages, max_tokens=min(mt, 1200))
        if out and out.get("content"):
            out.setdefault("provider", name)
            return out, None
    return None, err


def llm_ping(gemini_key=None, cerebras_key=None, groq_key=None, or_key=None, mistral_key=None, nvidia_key=None):
    """Un ping court par fournisseur. Renvoie liste de libelles."""
    msgs = [{"role": "user", "content": "Reponds uniquement: OK"}]
    out = []
    if gemini_key:
        r, e = gemini_chat(gemini_key, msgs, max_tokens=16)
        out.append("Gemini OK" if r else ("Gemini KO " + str(e or "")[:50]))
    else:
        out.append("Gemini absent")
    if mistral_key:
        r, e = mistral_chat(mistral_key, msgs, max_tokens=16)
        out.append("Mistral OK" if r else ("Mistral KO " + str(e or "")[:50]))
    else:
        out.append("Mistral absent")
    if nvidia_key:
        r, e = nvidia_chat(nvidia_key, msgs, max_tokens=16)
        out.append("NVIDIA OK" if r else ("NVIDIA KO " + str(e or "")[:50]))
    else:
        out.append("NVIDIA absent")
    if cerebras_key:
        r, e = cerebras_chat(cerebras_key, msgs, max_tokens=16)
        out.append("Cerebras OK" if r else ("Cerebras KO " + str(e or "")[:50]))
    else:
        out.append("Cerebras absent")
    if groq_key and not llm_cooling("groq"):
        r, e = groq_chat(groq_key, msgs, max_tokens=16, disable_thinking=True)
        out.append("Groq OK" if r else ("Groq KO " + str(e or "")[:40]))
    elif groq_key:
        out.append("Groq en pause 429")
    else:
        out.append("Groq absent")
    if or_key and not llm_cooling("openrouter"):
        r, e = openrouter_chat(or_key, msgs, max_tokens=16)
        out.append("OpenRouter OK" if r else ("OpenRouter KO " + str(e or "")[:40]))
    elif or_key:
        out.append("OpenRouter en pause 429")
    else:
        out.append("OpenRouter absent")
    return out


def _iter_json_objects(s):

    """Tous les objets JSON {…} bien parenthésés, même noyés dans du texte."""
    i = 0
    n = len(s)
    while i < n:
        if s[i] != "{":
            i += 1
            continue
        depth = 0
        in_str = False
        esc = False
        j = i
        while j < n:
            ch = s[j]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
            else:
                if ch == '"':
                    in_str = True
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        chunk = s[i : j + 1]
                        try:
                            obj = json.loads(chunk)
                        except Exception:
                            obj = None
                        if isinstance(obj, dict):
                            yield obj
                        break
            j += 1
        i += 1


def _norm_label(lab):
    s = str(lab or "").strip().lower()
    aliases = {
        "very dovish": "très dovish", "slightly dovish": "légèrement dovish",
        "very hawkish": "très hawkish", "slightly hawkish": "légèrement hawkish",
        "neutral": "neutre", "dove": "dovish", "hawk": "hawkish",
    }
    s = aliases.get(s, s)
    for lab in _LLM_LABELS:
        if s == lab or s.replace("é", "e") == lab.replace("é", "e"):
            return lab
    if "hawk" in s:
        return "hawkish" if "très" in s or "very" in s else (
            "légèrement hawkish" if "lég" in s or "slight" in s else "hawkish")
    if "dov" in s:
        return "dovish" if "très" in s or "very" in s else (
            "légèrement dovish" if "lég" in s or "slight" in s else "dovish")
    return "neutre"


def _norm_llm(obj):
    if not isinstance(obj, dict):
        return None
    try:
        score = float(obj.get("score"))
    except Exception:
        return None
    score = max(-5.0, min(5.0, score))
    label = _norm_label(obj.get("label"))
    assets_in = obj.get("assets") if isinstance(obj.get("assets"), dict) else {}
    assets = {}
    keymap = {
        "xauusd": "XAUUSD", "xau": "XAUUSD", "or": "XAUUSD", "gold": "XAUUSD",
        "usd": "USD", "dollar": "USD",
        "eurusd": "EURUSD", "eur": "EURUSD", "eur/usd": "EURUSD",
        "gbpusd": "GBPUSD", "gbp": "GBPUSD",
        "usdjpy": "USDJPY", "jpy": "USDJPY",
        "us10y": "US10Y", "taux": "US10Y",
    }
    for k, v in assets_in.items():
        kk = keymap.get(str(k).lower().replace(" ", ""), None)
        if not kk:
            continue
        vv = str(v or "neutre").lower()
        if "haus" in vv or "bull" in vv or "up" in vv:
            assets[kk] = "haussier"
        elif "baiss" in vv or "bear" in vv or "down" in vv:
            assets[kk] = "baissier"
        else:
            assets[kk] = "neutre"
    for k in ("XAUUSD", "USD", "EURUSD", "GBPUSD", "USDJPY", "US10Y"):
        assets.setdefault(k, "neutre")
    return {
        "score": round(score, 1),
        "label": label,
        "plain_fr": str(obj.get("plain_fr") or "").strip(),
        "assets": assets,
        "why": obj.get("why") if isinstance(obj.get("why"), list) else [],
        "what_changed": obj.get("what_changed") if isinstance(obj.get("what_changed"), list) else [],
        "implied_path": obj.get("implied_path") or "unclear",
        "confidence": obj.get("confidence") or "moyenne",
    }


def _parse_llm_json(raw):
    if not raw:
        return None
    s = str(raw).strip()
    s = re.sub(r"(?is)<think>.*?</think>", " ", s)
    s = re.sub(r"^```(?:json)?\s*", "", s, flags=re.I)
    s = re.sub(r"\s*```$", "", s)
    cands = []
    for obj in _iter_json_objects(s):
        if "score" in obj and "label" in obj:
            n = _norm_llm(obj)
            if n:
                cands.append(n)
    return cands[-1] if cands else None


def groq_analyze(key, bank, kind, title, text, previous=None, or_key=None, gemini_key=None, cerebras_key=None):
    """Analyse UN document. JSON strict. Ne remplace pas le score lexique."""
    text = (text or "")[:12000]
    if len(text) < 80:
        return None, "texte trop court"
    user = json.dumps(
        {
            "bank": bank,
            "kind": kind,
            "title": title,
            "previous_statement": (previous or "")[:4000] or None,
            "text": text,
        },
        ensure_ascii=False,
    )
    out, err = llm_chat(
        key,
        [
            {"role": "system", "content": _system_prompt()},
            {
                "role": "user",
                "content": "Analyse ce document. JSON uniquement.\n" + user,
            },
        ],
        or_key=or_key, gemini_key=gemini_key, cerebras_key=cerebras_key,
    )
    if err:
        return None, err
    parsed = _parse_llm_json(out["content"])
    if not parsed or not isinstance(parsed, dict) or "score" not in parsed:
        return None, "JSON sans score hawk/dove"
    if not str(parsed.get("plain_fr") or "").strip():
        parsed["plain_fr"] = "Ton " + parsed.get("label", "neutre") + " (score " + str(parsed["score"]) + ")."
    parsed["model"] = out.get("model")
    parsed["provider"] = "groq"
    return parsed, None


def attach_llm(item_id, llm):
    path = os.path.join(ITEMS_DIR, item_id + ".json")
    doc = _load_json(path, None)
    if not doc:
        return False
    doc["llm"] = llm
    _save_json(path, doc)
    return True


def latest_for_llm(bank=None):
    """Dernier communiqué / discours assez long, sans analyse IA."""
    idx = _load_json(os.path.join(DIR, "index.json"), {"items": []})
    items = idx.get("items") or []
    items.sort(key=lambda x: x.get("published") or "", reverse=True)
    out = []
    for it in items:
        if bank and it.get("bank") != bank:
            continue
        if it.get("kind") not in ("statement", "speech", "testimony", "minutes", "outlook"):
            continue
        if (it.get("chars") or 0) < 350:
            continue
        doc = _load_json(os.path.join(ITEMS_DIR, it["id"] + ".json"), None)
        if not doc:
            continue
        if doc.get("llm"):
            continue
        out.append(doc)
        if len(out) >= 2:
            break
    return out


def main(argv=None):
    p = argparse.ArgumentParser(description="CB Hawk/Dove worker (local)")
    p.add_argument("--once", action="store_true", help="un passage puis exit")
    p.add_argument("--bank", choices=BANKS, help="filtrer une banque")
    p.add_argument("--meta-only", action="store_true", help="RSS sans télécharger le HTML")
    p.add_argument("--limit", type=int, default=6)
    args = p.parse_args(argv)
    recs = poll_feeds(only_bank=args.bank, limit_per_feed=args.limit, fetch_body=not args.meta_only)
    snap = snapshot()
    print(json.dumps({"ingested": recs, "banks": snap["banks"]}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
