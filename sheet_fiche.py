"""
Murfy — Scraper Prix Neuf + Fiche Technique
============================================
Sheet ID  : via secret SHEET_ID_FICHE
Onglet    : TEST
Colonnes  : B=Type | C=Marque | D=Modèle
            E=Prix neuf | F=Lien | G=Fiche technique | H=Dimensions
"""

import os, re, sys, time, html, unicodedata
from datetime import datetime

import requests, gspread
from google.oauth2.service_account import Credentials

# ── Config ────────────────────────────────────────────────────────────────────
SHEET_ID      = os.environ.get("SHEET_ID_FICHE", "1MK6TiPQZUX4IwoYzfVB4Ofo1fFbUm_5qitsrpIjJps0")
SHEET_TAB     = "TEST"
BATCH_LIMIT   = int(os.environ.get("BATCH_LIMIT",    "5"))
DELAY_SECONDS = int(os.environ.get("DELAY_SECONDS", "20"))
SHARD_INDEX   = int(os.environ.get("SHARD_INDEX",   "0"))
SHARD_COUNT   = int(os.environ.get("SHARD_COUNT",   "1"))
CREDS_FILE    = "credentials.json"

# Colonnes (index Sheet 1-based)
COL_TYPE  = 2  # B
COL_MARQUE= 3  # C
COL_MODELE= 4  # D
COL_PRIX  = 5  # E
COL_LIEN  = 6  # F
COL_FICHE = 7  # G
COL_DIM   = 8  # H

BASE_URL   = "https://www.electromenager-compare.com"
SEARCH_URL = f"{BASE_URL}/index.php"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9",
    "Referer": BASE_URL,
}

SESSION = None

# ── Helpers ───────────────────────────────────────────────────────────────────
def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)

class RateLimitError(Exception):
    pass

def _init_session():
    global SESSION
    if SESSION is None:
        SESSION = requests.Session()
        try:
            r = SESSION.get(BASE_URL, headers=HEADERS, timeout=15)
            if r.status_code == 403:
                raise RateLimitError("403 à l'init")
            log(f"Session OK (HTTP {r.status_code})")
        except RateLimitError:
            raise
        except Exception as e:
            log(f"Init session : {e}")

def _get(url):
    r = SESSION.get(url, headers=HEADERS, timeout=15, allow_redirects=True)
    if r.status_code == 403:
        raise RateLimitError(f"403 GET {url}")
    return r

def _post(url, data):
    r = SESSION.post(url, headers=HEADERS, data=data, timeout=15, allow_redirects=True)
    if r.status_code == 403:
        raise RateLimitError(f"403 POST {url}")
    return r

def _decode(r):
    """Décode la réponse en tenant compte de l'encodage iso-8859-1 du site."""
    try:
        return r.content.decode("iso-8859-1")
    except Exception:
        return r.text

def _norm(t):
    t = unicodedata.normalize("NFKD", t)
    return "".join(c for c in t if not unicodedata.combining(c)).lower()

# ── Nettoyage modèle (logique SQL Murfy) ─────────────────────────────────────
MODELES_INUTILISABLES = {"vde", "non renseignee", "non renseigne", ""}
MARQUES_PLACEHOLDER   = {"marque inconnue", "inconnue", "inconnu", "n/a", "na", ""}
SUFFIXES_REGIONAUX    = ["FR","EU","UK","GB","DE","IT","ES","PT","NL","BE",
                         "CH","AT","PL","INT","EUR","EF","EC","LE","US"]

def _nettoyer_modele(marque, modele):
    if not modele: return modele
    mu = (marque or "").upper()
    m  = modele.strip()
    m  = re.sub(r"(?i)EUROSAV", "", m).strip()
    if "SAMSUNG"  in mu: m = re.sub(r"/.*$",    "", m).strip()
    elif "SIEMENS" in mu: m = re.sub(r"[/,].*$", "", m).strip(); m = re.sub(r"\d{2,3}$", "", m).strip()
    elif "VEDETTE" in mu: m = re.sub(r"[/,].*$", "", m).strip(); m = re.sub(r"\d{2}$",   "", m).strip()
    elif "BOSCH"   in mu: m = m[:10]
    return m if m else modele

def _strip_suffix(modele):
    mu = modele.upper()
    for s in sorted(SUFFIXES_REGIONAUX, key=len, reverse=True):
        if mu.endswith(s) and len(modele) > len(s)+3:
            return modele[:-len(s)]
    return None

def _sans_prefixe(marque, modele):
    p = re.sub(r"[^A-Za-z0-9]","",marque).upper()[:3]
    if modele.upper().startswith(p) and len(modele)>len(p)+2:
        return modele[len(p):]
    return None

def _variantes_tiret(modele):
    return [modele[:-n]+"-"+modele[-n:] for n in range(1,4) if len(modele)>n]

# ── Cohérence catégorie ───────────────────────────────────────────────────────
TYPE_PREFIXES = {
    "lave-linge":     ["lave-linge"],
    "lave linge":     ["lave-linge"],
    "lave-vaisselle": ["lave-vaisselle"],
    "lave vaisselle": ["lave-vaisselle"],
    "refrigerateur":  ["refrigerateur"],
    "réfrigérateur":  ["refrigerateur"],
    "seche-linge":    ["seche-linge"],
    "sèche-linge":    ["seche-linge"],
    "seche linge":    ["seche-linge"],
    "sèche linge":    ["seche-linge"],
    "congelateur":    ["congelateur"],
    "congélateur":    ["congelateur"],
    "four":           ["four"],
    "cuisiniere":     ["four","cuisiniere"],
    "cuisinière":     ["four","cuisiniere"],
    "micro-ondes":    ["four","micro-ondes"],
}

def _coherente(url, type_app):
    if not type_app: return True
    tn   = _norm(type_app)
    slug = url.rsplit("/",1)[-1].lower()
    for cle, prefixes in TYPE_PREFIXES.items():
        if cle in tn:
            return any(slug.startswith(p) for p in prefixes)
    return True

def _slug_match(url, modele):
    nm = re.sub(r"[\s\-]","", modele).upper()
    nu = re.sub(r"[\s\-]","", url).upper()
    return nm in nu

# ── Recherche URL ─────────────────────────────────────────────────────────────
URL_PAT = re.compile(
    r'href="((?:https?://(?:www\.)?electromenager-compare\.com/)?'
    r'[a-z][a-z0-9\-]*-[A-Za-z0-9][A-Za-z0-9\-]*\.htm)"',
    re.IGNORECASE
)

def _try_search(query, type_app=None, ref=None):
    try:
        r = _post(SEARCH_URL, {"action":"sbtsrch","search":query})
    except RateLimitError:
        raise
    except Exception as e:
        log(f"  Erreur recherche : {e}"); return None

    page = _decode(r)
    ref  = ref or query

    for m in URL_PAT.finditer(page):
        href = m.group(1)
        if not href.startswith("http"):
            href = BASE_URL+"/"+href.lstrip("/")
        if href.rsplit("/",1)[-1].startswith("recherche-"):
            continue
        if type_app and not _coherente(href, type_app):
            continue
        if _slug_match(href, ref):
            return href
    return None

def find_url(type_app, marque, modele):
    _init_session()
    me = "" if marque.strip().lower() in MARQUES_PLACEHOLDER else marque
    mc = _nettoyer_modele(marque, modele)
    if mc.lower() in MODELES_INUTILISABLES:
        return None

    # Passe 1 : marque + modèle nettoyé
    url = _try_search(f"{me} {mc}".strip(), type_app, mc)
    if url: return url
    time.sleep(2)

    # Passe 2 : modèle seul
    url = _try_search(mc, type_app, mc)
    if url: return url
    time.sleep(2)

    # Passe 3 : sans suffixe régional
    ss = _strip_suffix(mc)
    if ss:
        url = _try_search(f"{me} {ss}".strip(), type_app, ss)
        if url and _slug_match(url, ss): return url
        time.sleep(2)

    # Passe 4 : sans préfixe marque
    sp = _sans_prefixe(marque, mc)
    if sp:
        url = _try_search(f"{me} {sp}".strip(), type_app, sp)
        if url and _slug_match(url, sp): return url
        time.sleep(2)

    # Passe 5 : variantes tiret
    for v in _variantes_tiret(mc):
        url = _try_search(f"{me} {v}".strip(), type_app, mc)
        if url and _slug_match(url, mc): return url
        time.sleep(2)

    return None

# ── Prix ──────────────────────────────────────────────────────────────────────
def fetch_prix(url):
    try:
        r    = _get(url)
        page = _decode(r)
    except RateLimitError:
        raise
    except Exception as e:
        return None

    # Prix le plus bas affiché en haut de page (ex: "475.35 €" ou "475,35 €")
    for pat in [
        r'A PARTIR DE\s*[\r\n\s]*([0-9][0-9 ]*[,\.][0-9]{2})\s*',
        r'([0-9][0-9 ]*[,\.][0-9]{2})\s*\xe2\x82\xac',  # € utf8
        r'([0-9][0-9 ]*[,\.][0-9]{2})\s*&euro;',
        r'([0-9][0-9 ]*[,\.][0-9]{2})\s*€',
        r'([0-9][0-9 ]*[,\.][0-9]{2})\s*&#8364;',
    ]:
        m = re.search(pat, page, re.IGNORECASE)
        if m:
            return m.group(1).replace(" ","").replace(".",",") + " €"
    return None

# ── Fiche technique ───────────────────────────────────────────────────────────
def fetch_fiche(url, type_app):
    """
    Retourne (fiche_str, dimensions_str).
    La page est en iso-8859-1 — on décode correctement avant de parser.
    Structure de la page :
      - "Capacité de chargement : X kg"
      - "Niveau sonore : X dB(A)"
      - "Classe énergie : A++"
      - "Dimensions déballé : 850 x 596 x 650 mm (HxLxP)"
      - "Poids déballé : 46.01 kg"
    """
    try:
        r    = _get(url)
        page = _decode(r)
    except RateLimitError:
        raise
    except Exception as e:
        return "Erreur réseau", "Non trouvé"

    tn    = _norm(type_app or "")
    lines = []

    # ── Poids ─────────────────────────────────────────────────────────────
    m = re.search(r'Poids d.ball.\s*:\s*([0-9]+[,.]?[0-9]*)\s*kg', page, re.IGNORECASE)
    if m: lines.append(f"Poids : {m.group(1)} kg")

    # ── Champs selon catégorie ─────────────────────────────────────────────
    if "lave-linge" in tn or "lave linge" in tn:
        _add(page, lines, "Capacité", [
            r'Capacit. de chargement\s*:\s*([0-9]+[,.]?[0-9]*)\s*kg',
            r'Capacit.\s*:\s*([0-9]+[,.]?[0-9]*)\s*kg',
        ], "kg")
        _add(page, lines, "Essorage", [
            r'Essorage\s*:\s*([0-9 ]+)\s*(?:tr/min|trs)',
            r'Vitesse d.essorage\s*:\s*([0-9 ]+)\s*tr',
        ], "tr/min")
        _energie(page, lines)
        _add(page, lines, "Niveau sonore", [
            r'Niveau sonore\s*:\s*([0-9]+)\s*dB',
        ], "dB")

    elif "lave-vaisselle" in tn or "lave vaisselle" in tn:
        _add(page, lines, "Capacité", [
            r'([0-9]+)\s*couverts',
            r'Capacit.\s*:\s*([0-9]+)\s*couvert',
        ], "couverts")
        _energie(page, lines)
        _add(page, lines, "Niveau sonore", [
            r'Niveau sonore\s*:\s*([0-9]+)\s*dB',
        ], "dB")
        _add(page, lines, "Consommation eau", [
            r'Consommation d.eau\s*:\s*([0-9]+[,.]?[0-9]*)\s*[lL]',
        ], "L/cycle")

    elif any(x in tn for x in ["seche-linge","sèche-linge","seche linge","sèche linge"]):
        # Type de séchage — chercher dans la section "Type de séche-linge"
        for mot, label in [
            ("pompe", "Pompe à chaleur"),
            ("condensation", "Condensation"),
            ("evacuation", "Évacuation"),
            ("évacuation", "Évacuation"),
        ]:
            if mot in page.lower():
                lines.append(f"Type de séchage : {label}")
                break
        _add(page, lines, "Capacité", [
            r'Capacit. de chargement\s*:\s*([0-9]+[,.]?[0-9]*)\s*kg',
            r'Capacit.\s*:\s*([0-9]+[,.]?[0-9]*)\s*kg',
        ], "kg")
        _energie(page, lines)
        _add(page, lines, "Niveau sonore", [
            r'Niveau sonore\s*:\s*([0-9]+)\s*dB',
        ], "dB")

    elif "four" in tn or "cuisini" in tn:
        for mot, label in [
            ("pyrolyse","Pyrolyse"),
            ("chaleur tournante","Chaleur tournante"),
            ("gaz","Gaz"),
            ("lectrique","Électrique"),
        ]:
            if mot in page.lower():
                lines.append(f"Type de cuisson : {label}")
                break
        _add(page, lines, "Capacité", [
            r'Volume\s*:\s*([0-9]+[,.]?[0-9]*)\s*[lL]',
            r'Capacit.\s*:\s*([0-9]+[,.]?[0-9]*)\s*[lL]',
        ], "L")
        _energie(page, lines)

    elif any(x in tn for x in ["réfrigérateur","refrigerateur","frigo"]):
        for mot, label in [
            ("no frost","No Frost"),
            ("ventil","Froid ventilé"),
            ("statique","Froid statique"),
        ]:
            if mot in page.lower():
                lines.append(f"Type de froid : {label}")
                break
        _add(page, lines, "Capacité totale", [
            r'Capacit. totale\s*:\s*([0-9]+)\s*[lL]',
            r'Capacit.\s*:\s*([0-9]+)\s*[lL]',
        ], "L")
        _energie(page, lines)
        _add(page, lines, "Niveau sonore", [
            r'Niveau sonore\s*:\s*([0-9]+)\s*dB',
        ], "dB")

    elif any(x in tn for x in ["congélateur","congelateur"]):
        _add(page, lines, "Capacité", [
            r'Capacit.\s*:\s*([0-9]+)\s*[lL]',
        ], "L")
        _energie(page, lines)
        _add(page, lines, "Niveau sonore", [
            r'Niveau sonore\s*:\s*([0-9]+)\s*dB',
        ], "dB")

    else:
        _energie(page, lines)

    # ── Dimensions ─────────────────────────────────────────────────────────
    dims = "Non trouvé"
    # Format page : "850 x 596 x 650 mm (HxLxP)"  → on convertit en LxHxP cm
    m = re.search(
        r'Dimensions d.ball.\s*:\s*([0-9]+)\s*x\s*([0-9]+)\s*x\s*([0-9]+)\s*mm\s*\(HxLxP\)',
        page, re.IGNORECASE
    )
    if m:
        h, l, p = int(m.group(1)), int(m.group(2)), int(m.group(3))
        dims = f"{l/10:.1f}×{h/10:.1f}×{p/10:.1f} cm"
    else:
        # Essayer format cm direct
        m = re.search(
            r'([0-9]+[,.]?[0-9]*)\s*[xX×]\s*([0-9]+[,.]?[0-9]*)\s*[xX×]\s*([0-9]+[,.]?[0-9]*)\s*cm',
            page
        )
        if m:
            dims = f"{m.group(1)}×{m.group(2)}×{m.group(3)} cm"

    fiche = "\n".join(lines) if lines else "Non trouvé"
    return fiche, dims


def _add(page, lines, label, patterns, unit):
    for p in patterns:
        m = re.search(p, page, re.IGNORECASE)
        if m:
            val = m.group(1).strip()
            lines.append(f"{label} : {val} {unit}".strip())
            return True
    return False


def _energie(page, lines):
    # Nouvelle classe (post-2021 : A, B, C, D, E, F, G)
    for p in [
        r'Classe .nergie\s*:\s*([A-G])\s*\(Indice',      # "(Indice d'efficacité)"
        r'\(depuis mi-2025\).*?Classe .nergie\s*:\s*([A-G])\b',
        r'Classe .nergie.*?:\s*\*\*([A-G])\*\*',
    ]:
        m = re.search(p, page, re.IGNORECASE | re.DOTALL)
        if m:
            lines.append(f"Nouvelle classe énergétique : {m.group(1)}")
            return
    # Ancienne classe
    for p in [
        r'Classe .nergie\s*:\s*\*\*([A-G][+]*)\*\*',
        r'Classe .nergie\s*:\s*([A-G][+]+)',
    ]:
        m = re.search(p, page, re.IGNORECASE)
        if m:
            lines.append(f"Ancienne classe énergétique : {m.group(1)}")
            return


# ── Google Sheets ─────────────────────────────────────────────────────────────
def open_sheet():
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_file(CREDS_FILE, scopes=scopes)
    gc    = gspread.authorize(creds)
    return gc.open_by_key(SHEET_ID).worksheet(SHEET_TAB)


# ── Boucle principale ─────────────────────────────────────────────────────────
def process_all():
    log(f"Shard {SHARD_INDEX}/{SHARD_COUNT} | batch={BATCH_LIMIT} | delay={DELAY_SECONDS}s")
    ws        = open_sheet()
    all_rows  = ws.get_all_values()
    traites   = 0

    for i, row in enumerate(all_rows[1:], start=2):  # ligne 1 = en-têtes

        # Shard : ce job traite 1/N des lignes
        if (i - 2) % SHARD_COUNT != SHARD_INDEX:
            continue

        if traites >= BATCH_LIMIT:
            log(f"Limite de lot atteinte ({BATCH_LIMIT}) — arrêt.")
            break

        def g(col):
            idx = col - 2  # col B=2 → index 0
            return row[idx].strip() if idx < len(row) else ""

        type_app = g(COL_TYPE)
        marque   = g(COL_MARQUE)
        modele   = g(COL_MODELE)

        if not marque and not modele:
            continue

        # Sauter si déjà traité (E et G remplis)
        if g(COL_PRIX) and g(COL_FICHE):
            continue

        log(f"Ligne {i} : {type_app} | {marque} | {modele}")

        try:
            mc = _nettoyer_modele(marque, modele)

            # ── 1. URL ────────────────────────────────────────────────────
            url = g(COL_LIEN)
            if url and not (_slug_match(url, mc) or _slug_match(url, modele)):
                log(f"  ⚠ Lien existant invalide — re-cherche")
                url = None

            if not url:
                url = find_url(type_app, marque, modele)

            if not url:
                log(f"  ✗ URL introuvable")
                ws.update_cell(i, COL_PRIX,  "Non trouvé")
                ws.update_cell(i, COL_LIEN,  "Non trouvé")
                ws.update_cell(i, COL_FICHE, "Non trouvé")
                ws.update_cell(i, COL_DIM,   "Non trouvé")
                traites += 1
                time.sleep(DELAY_SECONDS)
                continue

            log(f"  → {url}")

            # ── 2. Prix ───────────────────────────────────────────────────
            time.sleep(2)
            prix = fetch_prix(url) or "Non trouvé"
            log(f"  → Prix : {prix}")

            # ── 3. Fiche technique ────────────────────────────────────────
            time.sleep(2)
            fiche, dims = fetch_fiche(url, type_app)
            log(f"  → Fiche :\n{fiche}")
            log(f"  → Dims : {dims}")

            # ── 4. Écriture ───────────────────────────────────────────────
            ws.update_cell(i, COL_PRIX,  prix)
            ws.update_cell(i, COL_LIEN,  url)
            ws.update_cell(i, COL_FICHE, fiche)
            ws.update_cell(i, COL_DIM,   dims)
            traites += 1
            log(f"  ✓ Ligne {i} OK")

        except RateLimitError as e:
            log(f"  ⛔ RATE LIMIT — arrêt immédiat. ({e})")
            sys.exit(1)
        except Exception as e:
            log(f"  ✗ Erreur : {e}")
            ws.update_cell(i, COL_PRIX,  "Erreur")
            ws.update_cell(i, COL_FICHE, f"Erreur : {e}")
            traites += 1

        time.sleep(DELAY_SECONDS)

    log(f"✓ Terminé — {traites} lignes traitées.")


if __name__ == "__main__":
    process_all()
