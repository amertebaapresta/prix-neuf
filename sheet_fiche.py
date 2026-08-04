"""
sheet_fiche.py
==============
Même logique de recherche que sheet_prix.py (qui fonctionne),
+ récupération de la fiche technique et des dimensions.

STRUCTURE ONGLET TEST :
    B=Type | C=Marque | D=Modèle | E=Prix neuf | F=Lien | G=Fiche technique | H=Dimensions
"""

import os, re, sys, time, html, unicodedata
from datetime import datetime
from zoneinfo import ZoneInfo

import requests, gspread
from google.oauth2.service_account import Credentials

PARIS_TZ = ZoneInfo("Europe/Paris")
def now_paris():
    return datetime.now(PARIS_TZ)

# ── Config ────────────────────────────────────────────────────────────────────
SHEET_ID       = os.environ.get("SHEET_ID_FICHE", "1MK6TiPQZUX4IwoYzfVB4Ofo1fFbUm_5qitsrpIjJps0")
WORKSHEET_NAME = "TEST"
CREDENTIALS_FILE = "credentials.json"
FORCE_REFRESH_ALL = False
BATCH_LIMIT    = int(os.environ.get("BATCH_LIMIT",    "5"))
DELAY_SECONDS  = float(os.environ.get("DELAY_SECONDS", "20"))
SHARD_INDEX    = int(os.environ.get("SHARD_INDEX",    "0"))
SHARD_COUNT    = int(os.environ.get("SHARD_COUNT",    "1"))

# Noms des colonnes dans l'onglet TEST (ligne 1)
COL_TYPE  = "Type"
COL_MARQUE= "Marque"
COL_MODELE= "Modèle"
COL_PRIX  = "Prix neuf"
COL_LIEN  = "Lien"
COL_FICHE = "Fiche technique"
COL_DIM   = "Dimension"

HEADERS_HTTP = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "fr-FR,fr;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Referer": "https://www.electromenager-compare.com/",
}

SESSION = requests.Session()
_SESSION_INIT = False

def _init_session():
    global _SESSION_INIT
    if not _SESSION_INIT:
        SESSION.get("https://www.electromenager-compare.com/", headers=HEADERS_HTTP, timeout=15)
        _SESSION_INIT = True

class RateLimitError(Exception):
    pass

def _post_or_stop(url, **kwargs):
    r = SESSION.post(url, **kwargs)
    if r.status_code == 403:
        raise RateLimitError("403 reçu — arrêt immédiat")
    return r

def _get_or_stop(url, **kwargs):
    r = SESSION.get(url, **kwargs)
    if r.status_code == 403:
        raise RateLimitError("403 reçu — arrêt immédiat")
    return r

def _decode(r):
    """Décode en iso-8859-1 (encodage réel du site)."""
    try:
        return r.content.decode("iso-8859-1")
    except Exception:
        return r.text

def log(msg):
    print(f"[{now_paris().strftime('%H:%M:%S')}] {msg}", flush=True)

# ── Logique de recherche URL (IDENTIQUE à sheet_prix.py) ─────────────────────

PRODUCT_LINK_REGEX = (
    r'href="(https?://(?:www\.)?electromenager-compare\.com/'
    r'[a-z\-]+-[A-Za-z0-9]+-[A-Za-z0-9\-]+\.htm)"'
)
EXCLUDE_URL_PATTERNS = ("-liste-", "/recherche", "/marques", "/avis-")

def _is_recherche_page(url):
    return url.rsplit("/", 1)[-1].startswith("recherche-")

def _slug_matches(url, modele):
    norm_model = re.sub(r"[\s\-]", "", modele).upper()
    norm_url   = re.sub(r"[\s\-]", "", url).upper()
    return norm_model in norm_url

SUFFIXES_REGIONAUX = [
    "FR","EU","UK","GB","DE","IT","ES","PT","NL","BE",
    "CH","AT","PL","INT","EUR","US","EF","EC","LE",
]

def _strip_regional_suffix(modele):
    mu = modele.upper()
    for suf in sorted(SUFFIXES_REGIONAUX, key=len, reverse=True):
        if mu.endswith(suf) and len(modele) > len(suf) + 3:
            return modele[:-len(suf)]
    return None

TYPE_VERS_PREFIXES_URL = {
    "lave-linge":     ["lave-linge"],
    "lave linge":     ["lave-linge"],
    "lave-vaisselle": ["lave-vaisselle"],
    "lave vaisselle": ["lave-vaisselle"],
    "refrigerateur":  ["refrigerateur"],
    "réfrigérateur":  ["refrigerateur"],
    "seche-linge":    ["seche-linge", "lave-linge"],
    "sèche-linge":    ["seche-linge", "lave-linge"],
    "seche linge":    ["seche-linge", "lave-linge"],
    "sèche linge":    ["seche-linge", "lave-linge"],
    "congelateur":    ["congelateur"],
    "congélateur":    ["congelateur"],
    "four":           ["four"],
    "cuisiniere":     ["four","cuisiniere"],
    "cuisinière":     ["four","cuisiniere"],
    "micro-ondes":    ["four","micro-ondes"],
    "hotte":          ["hotte"],
    "cave a vin":     ["cave"],
}

def _normaliser(texte):
    texte = unicodedata.normalize("NFKD", texte)
    return "".join(c for c in texte if not unicodedata.combining(c)).lower()

def _categorie_coherente(url, type_appareil):
    if not type_appareil:
        return True
    type_norm = _normaliser(type_appareil)
    slug = url.rsplit("/", 1)[-1].lower()
    prefixes_attendus = None
    for cle, prefixes in TYPE_VERS_PREFIXES_URL.items():
        if cle in type_norm:
            prefixes_attendus = prefixes
            break
    if prefixes_attendus is None:
        return True
    return any(slug.startswith(p) for p in prefixes_attendus)

def _try_search(query, type_appareil=None, modele=None):
    try:
        r = _post_or_stop(
            "https://www.electromenager-compare.com/index.php",
            params={"action": "sbtsrch", "type": "0"},
            data={"q": query},
            headers=HEADERS_HTTP,
            timeout=15,
            allow_redirects=True,
        )
    except RateLimitError:
        raise
    except Exception as e:
        log(f"  ⚠ Erreur recherche: {e}")
        return None, None

    if r.status_code != 200:
        return None, None

    final_url = r.url
    if (
        final_url.endswith(".htm")
        and "electromenager-compare.com" in final_url
        and not _is_recherche_page(final_url)
        and not any(p in final_url for p in EXCLUDE_URL_PATTERNS)
        and _categorie_coherente(final_url, type_appareil)
    ):
        return final_url, "directe"

    page_html = html.unescape(r.text)
    candidates = re.findall(PRODUCT_LINK_REGEX, page_html)
    candidates = [
        c for c in candidates
        if not _is_recherche_page(c)
        and not any(p in c for p in EXCLUDE_URL_PATTERNS)
        and re.search(r"[A-Z]{2,}", c.rsplit("/", 1)[-1])
        and _categorie_coherente(c, type_appareil)
    ]
    if not candidates:
        return None, None

    if modele:
        for c in candidates:
            if _slug_matches(c, modele):
                return c, "liste-exact"

    return candidates[0], "liste"

MARQUES_PLACEHOLDER   = {"marque inconnue","inconnue","inconnu","n/a","na",""}
MODELES_INUTILISABLES = {"vde","non renseignée","non renseigne",""}

def _nettoyer_modele(marque, modele):
    if not modele: return modele
    mu = (marque or "").upper()
    m  = modele.strip()
    m  = re.sub(r"(?i)EUROSAV", "", m).strip()
    if "SAMSUNG"  in mu: m = re.sub(r"/.*$",    "", m).strip()
    elif "SIEMENS" in mu:
        m = re.sub(r"[/,].*$", "", m).strip()
        m = re.sub(r"\d{2,3}$","", m).strip()
    elif "VEDETTE" in mu:
        m = re.sub(r"[/,].*$", "", m).strip()
        m = re.sub(r"\d{2}$",  "", m).strip()
    elif "BOSCH"   in mu: m = m[:10]
    return m if m else modele

def _generer_variantes_tiret(modele):
    return [modele[:-n]+"-"+modele[-n:] for n in (1,2,3) if len(modele) > n+2]

def _sans_prefixe_marque(marque, modele):
    marque_norm = re.sub(r"[^A-Za-z]","",marque).upper()
    mu = modele.upper()
    for taille in (4,3):
        if len(marque_norm) >= taille and len(modele) > taille+3:
            if mu.startswith(marque_norm[:taille]):
                return modele[taille:]
    return None

def find_product_url(type_appareil, marque, modele):
    """Identique à sheet_prix.py — logique éprouvée."""
    _init_session()
    marque_effective = "" if marque.strip().lower() in MARQUES_PLACEHOLDER else marque
    modele_clean = _nettoyer_modele(marque, modele)
    if modele_clean.lower() in MODELES_INUTILISABLES:
        log(f"  ⚠ Référence inutilisable ('{modele}')")
        return None, None
    if modele_clean != modele:
        log(f"  → Nettoyé : '{modele}' → '{modele_clean}'")

    query = f"{marque_effective} {modele_clean}".strip()
    url, mode = _try_search(query, type_appareil, modele_clean)
    if url and _slug_matches(url, modele_clean):
        return url, "haute"

    stripped = _strip_regional_suffix(modele_clean)
    if stripped:
        time.sleep(2)
        url2, _ = _try_search(f"{marque_effective} {stripped}".strip(), type_appareil, stripped)
        if url2 and (_slug_matches(url2, modele_clean) or _slug_matches(url2, stripped)):
            return url2, "haute"
    else:
        url2 = None

    sans_prefixe = _sans_prefixe_marque(marque, modele_clean)
    if sans_prefixe:
        time.sleep(2)
        url_sp, _ = _try_search(f"{marque_effective} {sans_prefixe}".strip(), type_appareil, sans_prefixe)
        if url_sp and _slug_matches(url_sp, sans_prefixe):
            return url_sp, "haute"

    for variante in _generer_variantes_tiret(modele_clean):
        time.sleep(2)
        url3, _ = _try_search(f"{marque_effective} {variante}".strip(), type_appareil, modele_clean)
        if url3 and _slug_matches(url3, modele_clean):
            return url3, "haute"

    if url2: return url2, "approximative"
    if url:  return url,  "approximative"
    return None, None

# ── Prix ──────────────────────────────────────────────────────────────────────
PRICE_PATTERNS = [
    r"Dernier prix relev[ée]\s*[:\-]?\s*([\d\s]+[.,]\d{2})\s*€",
    r"[AÀ]\s*PARTIR\s*DE\s*([\d\s]+[.,]?\d*)\s*€",
    r"([\d]{2,4}[.,]\d{2})\s*€",
]

def fetch_prix(url):
    try:
        r = _get_or_stop(url, headers=HEADERS_HTTP, timeout=15, allow_redirects=True)
    except RateLimitError:
        raise
    except Exception as e:
        return None

    page = html.unescape(r.text)
    texte = re.sub(r"<[^>]+>", " ", page)
    texte = re.sub(r"\s+", " ", texte)

    for pattern in PRICE_PATTERNS:
        m = re.search(pattern, texte, re.IGNORECASE)
        if m:
            raw = m.group(1).replace(" ","").replace("\xa0","").replace(".",",")
            return raw + " €"
    return None

# ── Fiche technique ───────────────────────────────────────────────────────────
def fetch_fiche(url, type_appareil):
    """
    Parse la page produit (iso-8859-1) et extrait les champs de la fiche
    technique selon le type d'appareil.
    Retourne (fiche_str, dimensions_str).
    """
    try:
        r = _get_or_stop(url, headers=HEADERS_HTTP, timeout=15, allow_redirects=True)
    except RateLimitError:
        raise
    except Exception as e:
        return "Erreur réseau", "Non trouvé"

    # Décodage iso-8859-1 OBLIGATOIRE pour ce site
    page = _decode(r)
    tn   = _normaliser(type_appareil or "")
    lines = []

    # ── Poids ─────────────────────────────────────────────────────────────
    for p in [
        r'Poids d.ball.\s*:\s*([0-9]+[,.]?[0-9]*)\s*kg',
        r'Poids\s*:\s*\**([0-9]+[,.]?[0-9]*)\s*\**\s*kg',
    ]:
        m = re.search(p, page, re.IGNORECASE)
        if m:
            lines.append(f"Poids : {m.group(1)} kg")
            break

    # ── Champs selon type ──────────────────────────────────────────────────
    if "lave-linge" in tn or "lave linge" in tn:
        _add(page, lines, "Capacité", [
            r'Capacit. de chargement\s*:\s*\**([0-9]+[,.]?[0-9]*)\s*\**\s*kg',
            r'Capacit.\s*:\s*\**([0-9]+[,.]?[0-9]*)\s*\**\s*kg',
        ], "kg")
        _add(page, lines, "Essorage", [
            r'[Ee]ssorage max\s*:\s*\**([0-9 ]+)\s*\**\s*(?:tr/min|trs)',
            r'[Ee]ssorage\s*:\s*\**([0-9 ]+)\s*\**\s*(?:tr/min|trs)',
        ], "tr/min")
        _energie(page, lines)
        _add(page, lines, "Niveau sonore", [
            r'[Nn]iveau sonore\s*:\s*\**([0-9]+)\s*\**\s*dB',
        ], "dB")

    elif "lave-vaisselle" in tn or "lave vaisselle" in tn:
        _add(page, lines, "Capacité", [
            r'([0-9]+)\s*couverts',
            r'Capacit.\s*:\s*([0-9]+)\s*couvert',
        ], "couverts")
        _energie(page, lines)
        _add(page, lines, "Niveau sonore", [
            r'[Nn]iveau sonore\s*:\s*\**([0-9]+)\s*\**\s*dB',
        ], "dB")
        _add(page, lines, "Consommation eau", [
            r'[Cc]onsommation d.eau\s*:\s*([0-9]+[,.]?[0-9]*)\s*[lL]',
        ], "L/cycle")

    elif any(x in tn for x in ["seche-linge","sèche-linge","seche linge","sèche linge"]):
        # Type de séchage : chercher dans le texte de la fiche
        found = _add(page, lines, "Type de séchage", [
            r'Type de s.che-linge\s*:\s*\**([^\n\r*<]{5,60})',
        ], "")
        if not found:
            for mot, label in [
                ("pompe","Pompe à chaleur"),
                ("condensation","Condensation"),
                ("vacuation","Évacuation"),
            ]:
                if mot in page.lower():
                    lines.append(f"Type de séchage : {label}")
                    break
        _add(page, lines, "Capacité", [
            r'Capacit. de chargement\s*:\s*\**([0-9]+[,.]?[0-9]*)\s*\**\s*kg',
            r'Capacit.\s*:\s*\**([0-9]+[,.]?[0-9]*)\s*\**\s*kg',
        ], "kg")
        _energie(page, lines)
        _add(page, lines, "Niveau sonore", [
            r'[Nn]iveau sonore\s*:\s*\**([0-9]+)\s*\**\s*dB',
        ], "dB")

    elif "four" in tn or "cuisini" in tn or "micro" in tn:
        found = _add(page, lines, "Type de cuisson", [
            r'Type de cuisson\s*:\s*\**([^\n\r*<]{3,50})',
        ], "")
        if not found:
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
            r'[Vv]olume\s*:\s*\**([0-9]+[,.]?[0-9]*)\s*\**\s*[Ll]',
            r'Capacit.\s*:\s*\**([0-9]+[,.]?[0-9]*)\s*\**\s*[Ll]',
        ], "L")
        _energie(page, lines)

    elif any(x in tn for x in ["réfrigérateur","refrigerateur","frigo"]):
        found = _add(page, lines, "Type de froid", [
            r'Type de froid\s*:\s*\**([^\n\r*<]{3,40})',
        ], "")
        if not found:
            for mot, label in [
                ("no frost","No Frost"),
                ("ventil","Froid ventilé"),
                ("statique","Froid statique"),
            ]:
                if mot in page.lower():
                    lines.append(f"Type de froid : {label}")
                    break
        _add(page, lines, "Capacité totale", [
            r'Capacit. totale\s*:\s*\**([0-9]+)\s*\**\s*[Ll]',
            r'Capacit.\s*:\s*\**([0-9]+)\s*\**\s*[Ll]',
        ], "L")
        _energie(page, lines)
        _add(page, lines, "Niveau sonore", [
            r'[Nn]iveau sonore\s*:\s*\**([0-9]+)\s*\**\s*dB',
        ], "dB")

    elif any(x in tn for x in ["congélateur","congelateur"]):
        _add(page, lines, "Capacité", [
            r'Capacit.\s*:\s*\**([0-9]+)\s*\**\s*[Ll]',
        ], "L")
        _energie(page, lines)
        _add(page, lines, "Niveau sonore", [
            r'[Nn]iveau sonore\s*:\s*\**([0-9]+)\s*\**\s*dB',
        ], "dB")

    else:
        _energie(page, lines)

    # ── Dimensions ──────────────────────────────────────────────────────────
    dims = "Non trouvé"
    # Format page iso-8859-1 : "850 x 596 x 650 mm (HxLxP)" → LxHxP en cm
    m = re.search(
        r'Dimensions d.ball.\s*:\s*([0-9]+)\s*x\s*([0-9]+)\s*x\s*([0-9]+)\s*mm\s*\(HxLxP\)',
        page, re.IGNORECASE
    )
    if m:
        h, l, p = int(m.group(1)), int(m.group(2)), int(m.group(3))
        dims = f"{l/10:.1f}x{h/10:.1f}x{p/10:.1f} cm"
    else:
        m = re.search(
            r'([0-9]+[,.]?[0-9]*)\s*[xX]\s*([0-9]+[,.]?[0-9]*)\s*[xX]\s*([0-9]+[,.]?[0-9]*)\s*cm',
            page
        )
        if m:
            dims = f"{m.group(1)}x{m.group(2)}x{m.group(3)} cm"

    fiche = "\n".join(lines) if lines else "Non trouvé"
    return fiche, dims


def _add(page, lines, label, patterns, unit):
    for p in patterns:
        m = re.search(p, page, re.IGNORECASE)
        if m:
            val = m.group(1).strip().rstrip("*").strip()
            lines.append((f"{label} : {val} {unit}").strip())
            return True
    return False


def _energie(page, lines):
    # Nouvelle classe (post-2021 : lettre seule A-G)
    for p in [
        r'Classe .nergie\s*:\s*\**([A-G])\**\s*\(Indice',
        r'depuis mi-2025.*?Classe .nergie\s*:\s*\**([A-G])\**',
    ]:
        m = re.search(p, page, re.IGNORECASE | re.DOTALL)
        if m:
            lines.append(f"Nouvelle classe énergétique : {m.group(1)}")
            return
    # Ancienne classe (A+++ à G)
    for p in [
        r'Classe .nergie\s*:\s*\**([A-G][+]*)\**\s*\([SL]',  # (Séchage) ou (Lavage)
        r'Classe .nergie\s*:\s*\**([A-G][+]+)\**',
        r'Classe\s*:\s*\**([A-G][+]+)\**',
    ]:
        m = re.search(p, page, re.IGNORECASE)
        if m:
            lines.append(f"Ancienne classe énergétique : {m.group(1)}")
            return


# ── Google Sheets ─────────────────────────────────────────────────────────────
def get_worksheet():
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive.readonly",
    ]
    creds  = Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=scopes)
    client = gspread.authorize(creds)
    return client.open_by_key(SHEET_ID).worksheet(WORKSHEET_NAME)


# ── Boucle principale ─────────────────────────────────────────────────────────
def process_all():
    ws         = get_worksheet()
    all_values = ws.get_all_values()
    if not all_values:
        log("Feuille vide.")
        return

    header = all_values[0]
    idx    = {name: i for i, name in enumerate(header)}

    # Vérifier que les colonnes requises existent
    for col in [COL_TYPE, COL_MARQUE, COL_MODELE]:
        if col not in idx:
            log(f"✗ Colonne manquante : '{col}'. En-têtes trouvés : {header}")
            sys.exit(1)

    rows           = all_values[1:]
    traites        = 0
    candidat_index = 0
    arret          = False

    for i, row in enumerate(rows, start=2):
        if traites >= BATCH_LIMIT:
            log(f"Limite de lot atteinte ({BATCH_LIMIT}) — arrêt.")
            break

        def get(col):
            j = idx.get(col)
            return row[j].strip() if j is not None and j < len(row) else ""

        type_app = get(COL_TYPE)
        marque   = get(COL_MARQUE)
        modele   = get(COL_MODELE)

        if not marque and not modele:
            continue

        # Sauter si déjà traité
        if get(COL_PRIX) and get(COL_FICHE) and not FORCE_REFRESH_ALL:
            continue

        # Sharding
        mon_tour = (candidat_index % SHARD_COUNT == SHARD_INDEX)
        candidat_index += 1
        if not mon_tour:
            continue

        log(f"Ligne {i} : {type_app} | {marque} | {modele}")

        try:
            modele_clean = _nettoyer_modele(marque, modele)

            # ── 1. URL ────────────────────────────────────────────────────
            url = get(COL_LIEN)
            confiance = None
            if url:
                if _slug_matches(url, modele_clean) or _slug_matches(url, modele):
                    confiance = "haute"
                else:
                    log(f"  ⚠ Lien existant invalide — re-cherche")
                    url = None

            if not url:
                url, confiance = find_product_url(type_app, marque, modele)
                if not url:
                    log("  ✗ URL introuvable")
                    _write(ws, idx, i, {COL_PRIX:"Non trouvé", COL_LIEN:"Non trouvé",
                                        COL_FICHE:"Non trouvé", COL_DIM:"Non trouvé"})
                    traites += 1
                    time.sleep(DELAY_SECONDS)
                    continue
                log(f"  → URL (confiance: {confiance}) : {url}")

            # Confiance approximative → on écrit "Non trouvé" comme sheet_prix.py
            if confiance == "approximative":
                log(f"  ⚠ Confiance approximative — Non trouvé")
                _write(ws, idx, i, {COL_PRIX:"Non trouvé", COL_LIEN:"Non trouvé",
                                    COL_FICHE:"Non trouvé", COL_DIM:"Non trouvé"})
                traites += 1
                time.sleep(DELAY_SECONDS)
                continue

            # ── 2. Prix ───────────────────────────────────────────────────
            time.sleep(2)
            prix = fetch_prix(url) or "Non trouvé"
            log(f"  → Prix : {prix}")

            # ── 3. Fiche technique ────────────────────────────────────────
            time.sleep(2)
            fiche, dims = fetch_fiche(url, type_app)
            log(f"  → Fiche :\n{fiche}")
            log(f"  → Dims  : {dims}")

            # ── 4. Écriture ───────────────────────────────────────────────
            _write(ws, idx, i, {
                COL_PRIX:  prix,
                COL_LIEN:  url,
                COL_FICHE: fiche,
                COL_DIM:   dims,
            })
            traites += 1
            log(f"  ✓ Ligne {i} OK")

        except RateLimitError:
            log("  ⛔ 403 — ARRÊT IMMÉDIAT.")
            arret = True
            break
        except Exception as e:
            log(f"  ✗ Erreur : {e}")
            _write(ws, idx, i, {COL_PRIX:"Erreur", COL_FICHE:f"Erreur : {e}"})
            traites += 1

        time.sleep(DELAY_SECONDS)

    log(f"Terminé — {traites} lignes traitées." + (" (rate-limit)" if arret else ""))


def _write(ws, idx, row_num, values):
    """Écrit plusieurs cellules d'un coup sur la même ligne."""
    for col_name, val in values.items():
        if col_name not in idx:
            continue
        col_letter = gspread.utils.rowcol_to_a1(row_num, idx[col_name] + 1)
        ws.update_acell(col_letter, val)


if __name__ == "__main__":
    process_all()
