"""
Murfy — Scraper Prix Neuf + Fiche Technique
============================================
Sheet ID  : 1MK6TiPQZUX4IwoYzfVB4Ofo1fFbUm_5qitsrpIjJps0
Onglet    : TEST
Colonnes  : B=Type | C=Marque | D=Modèle
            E=Prix neuf | F=Lien | G=Fiche technique | H=Dimensions

GitHub Actions : 3 shards parallèles (IP différentes)
"""

import os
import re
import sys
import time
import json
import html
import unicodedata
from datetime import datetime

import requests
import gspread
from google.oauth2.service_account import Credentials

# ── Config ───────────────────────────────────────────────────────────────────
SHEET_ID        = os.environ.get("SHEET_ID_FICHE", "1MK6TiPQZUX4IwoYzfVB4Ofo1fFbUm_5qitsrpIjJps0")
SHEET_TAB       = "TEST"
BATCH_LIMIT     = int(os.environ.get("BATCH_LIMIT", "5"))    # appareils par shard/run
DELAY_SECONDS   = int(os.environ.get("DELAY_SECONDS", "20")) # pause entre appareils
SHARD_INDEX     = int(os.environ.get("SHARD_INDEX", "0"))    # 0, 1 ou 2
SHARD_COUNT     = int(os.environ.get("SHARD_COUNT", "1"))    # 3 en prod
CREDS_FILE      = "credentials.json"

# Colonnes du sheet (index 0-based par rapport à la colonne B=0)
COL_TYPE    = 0  # B
COL_MARQUE  = 1  # C
COL_MODELE  = 2  # D
COL_PRIX    = 3  # E
COL_LIEN    = 4  # F
COL_FICHE   = 5  # G
COL_DIM     = 6  # H

# ── Constantes scraping ───────────────────────────────────────────────────────
BASE_URL = "https://www.electromenager-compare.com"
SEARCH_URL = f"{BASE_URL}/index.php"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
    "Referer": BASE_URL,
}

URL_PATTERN = re.compile(
    r'href="((?:https?://(?:www\.)?electromenager-compare\.com/)?'
    r'[a-z][a-z0-9\-]*-[A-Za-z0-9][A-Za-z0-9\-]*\.htm)"',
    re.IGNORECASE,
)

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
                raise RateLimitError("403 dès l'initialisation")
            log(f"Session initialisée (HTTP {r.status_code})")
        except RateLimitError:
            raise
        except Exception as e:
            log(f"Avertissement init session : {e}")


def _get(url, **kwargs):
    r = SESSION.get(url, headers=HEADERS, timeout=15, **kwargs)
    if r.status_code == 403:
        raise RateLimitError(f"403 sur GET {url}")
    return r


def _post(url, **kwargs):
    r = SESSION.post(url, headers=HEADERS, timeout=15, **kwargs)
    if r.status_code == 403:
        raise RateLimitError(f"403 sur POST {url}")
    return r


def _normaliser(texte):
    t = unicodedata.normalize("NFKD", texte)
    return "".join(c for c in t if not unicodedata.combining(c)).lower()


# ── Nettoyage du modèle (réplique logique SQL Murfy) ─────────────────────────
MODELES_INUTILISABLES = {"vde", "non renseignée", "non renseigne", ""}
MARQUES_PLACEHOLDER   = {"marque inconnue", "inconnue", "inconnu", "n/a", "na", ""}

SUFFIXES_REGIONAUX = [
    "FR", "EU", "UK", "GB", "DE", "IT", "ES", "PT", "NL", "BE",
    "CH", "AT", "PL", "INT", "EUR", "EF", "EC", "LE", "US",
]


def _nettoyer_modele(marque, modele):
    if not modele:
        return modele
    marque_upper = (marque or "").upper()
    m = modele.strip()
    m = re.sub(r"(?i)EUROSAV", "", m).strip()
    if "SAMSUNG" in marque_upper:
        m = re.sub(r"/.*$", "", m).strip()
    elif "SIEMENS" in marque_upper:
        m = re.sub(r"[/,].*$", "", m).strip()
        m = re.sub(r"\d{2,3}$", "", m).strip()
    elif "VEDETTE" in marque_upper:
        m = re.sub(r"[/,].*$", "", m).strip()
        m = re.sub(r"\d{2}$", "", m).strip()
    elif "BOSCH" in marque_upper:
        m = m[:10]
    return m if m else modele


def _strip_regional_suffix(modele):
    mu = modele.upper()
    for suf in sorted(SUFFIXES_REGIONAUX, key=len, reverse=True):
        if mu.endswith(suf) and len(modele) > len(suf) + 3:
            return modele[:-len(suf)]
    return None


def _sans_prefixe_marque(marque, modele):
    prefix = re.sub(r"[^A-Za-z0-9]", "", marque).upper()[:3]
    mu = modele.upper()
    if mu.startswith(prefix) and len(modele) > len(prefix) + 2:
        return modele[len(prefix):]
    return None


def _generer_variantes_tiret(modele):
    variants = []
    for n in range(1, 4):
        if len(modele) > n:
            variants.append(modele[:-n] + "-" + modele[-n:])
    return variants


# ── Cohérence catégorie URL ───────────────────────────────────────────────────
TYPE_VERS_PREFIXES_URL = {
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
    "cuisiniere":     ["four", "cuisiniere"],
    "cuisinière":     ["four", "cuisiniere"],
    "micro-ondes":    ["four", "micro-ondes"],
    "hotte":          ["hotte"],
    "cave a vin":     ["cave"],
}


def _categorie_coherente(url, type_appareil):
    if not type_appareil:
        return True
    type_norm = _normaliser(type_appareil)
    slug = url.rsplit("/", 1)[-1].lower()
    for cle, prefixes in TYPE_VERS_PREFIXES_URL.items():
        if cle in type_norm:
            return any(slug.startswith(p) for p in prefixes)
    return True


def _slug_matches(url, modele):
    norm_m = re.sub(r"[\s\-]", "", modele).upper()
    norm_u = re.sub(r"[\s\-]", "", url).upper()
    return norm_m in norm_u


# ── Recherche URL produit ─────────────────────────────────────────────────────
def _try_search(query, type_appareil=None, modele_ref=None):
    """Recherche interne POST → retourne (url, mode) ou (None, None)."""
    try:
        r = _post(
            SEARCH_URL,
            data={"action": "sbtsrch", "search": query},
            allow_redirects=True,
        )
    except RateLimitError:
        raise
    except Exception as e:
        log(f"    Erreur réseau recherche : {e}")
        return None, None

    page_html = html.unescape(r.text)
    candidates = []
    for m in URL_PATTERN.finditer(page_html):
        href = m.group(1)
        if not href.startswith("http"):
            href = BASE_URL + "/" + href.lstrip("/")
        last = href.rsplit("/", 1)[-1]
        if last.startswith("recherche-"):
            continue
        if type_appareil and not _categorie_coherente(href, type_appareil):
            continue
        candidates.append(href)

    if not candidates:
        return None, None

    ref = modele_ref or query
    # Chercher le meilleur candidat (match exact du modèle dans l'URL)
    for c in candidates:
        if _slug_matches(c, ref):
            return c, "search"

    return None, None


def find_product_url(type_appareil, marque, modele):
    """Stratégie de recherche multi-passes. Retourne (url, confiance)."""
    _init_session()
    marque_eff = "" if marque.strip().lower() in MARQUES_PLACEHOLDER else marque
    modele_clean = _nettoyer_modele(marque, modele)

    if modele_clean.lower() in MODELES_INUTILISABLES:
        return None, None

    # Passe 1 : marque + modèle nettoyé
    url, _ = _try_search(f"{marque_eff} {modele_clean}".strip(), type_appareil, modele_clean)
    if url:
        return url, "haute"

    time.sleep(2)

    # Passe 2 : modèle seul
    url, _ = _try_search(modele_clean, type_appareil, modele_clean)
    if url:
        return url, "haute"

    time.sleep(2)

    # Passe 3 : sans suffixe régional
    sans_suf = _strip_regional_suffix(modele_clean)
    if sans_suf:
        url, _ = _try_search(f"{marque_eff} {sans_suf}".strip(), type_appareil, sans_suf)
        if url and _slug_matches(url, sans_suf):
            return url, "haute"
        time.sleep(2)

    # Passe 4 : sans préfixe marque
    sans_pref = _sans_prefixe_marque(marque, modele_clean)
    if sans_pref:
        url, _ = _try_search(f"{marque_eff} {sans_pref}".strip(), type_appareil, sans_pref)
        if url and _slug_matches(url, sans_pref):
            return url, "haute"
        time.sleep(2)

    # Passe 5 : variantes avec tiret
    for variante in _generer_variantes_tiret(modele_clean):
        url, _ = _try_search(f"{marque_eff} {variante}".strip(), type_appareil, modele_clean)
        if url and _slug_matches(url, modele_clean):
            return url, "haute"
        time.sleep(2)

    return None, None


# ── Scraping prix depuis page produit ────────────────────────────────────────
def fetch_prix(url):
    """Retourne (prix_str, ok) depuis la page produit."""
    try:
        r = _get(url, allow_redirects=True)
    except RateLimitError:
        raise
    except Exception as e:
        return None, f"Erreur réseau: {e}"

    if r.status_code != 200:
        return None, f"HTTP {r.status_code}"

    page = html.unescape(r.text)

    # Patterns prix (du plus précis au plus général)
    for pattern in [
        r'class="[^"]*prix[^"]*"[^>]*>\s*([0-9][0-9 ]*[,\.][0-9]{2})\s*€',
        r'([0-9][0-9 ]*[,\.][0-9]{2})\s*€',
        r'([0-9][0-9 ]*[,\.][0-9]{2})\s*&euro;',
    ]:
        m = re.search(pattern, page, re.IGNORECASE)
        if m:
            prix = m.group(1).replace(" ", "").replace(".", ",") + " €"
            return prix, "ok"

    return None, "Prix non trouvé dans la page"


# ── Scraping fiche technique ──────────────────────────────────────────────────
def fetch_fiche_technique(url, type_appareil):
    """
    Retourne (fiche_str, dimensions_str) depuis la page produit.
    fiche_str : texte multiligne lisible, style Murfy
    """
    try:
        r = _get(url, allow_redirects=True)
    except RateLimitError:
        raise
    except Exception as e:
        return "Erreur réseau", "Non trouvé"

    if r.status_code != 200:
        return f"HTTP {r.status_code}", "Non trouvé"

    page = html.unescape(r.text)
    type_norm = _normaliser(type_appareil or "")

    lignes = []

    # ── Poids ──────────────────────────────────────────────────────────────
    for p in [
        r'[Pp]oids\s*(?:\([^)]*\))?\s*:\s*</?\w[^>]*>?\s*([0-9]+[,.]?[0-9]*)\s*(?:kg)?',
        r'[Pp]oids[^:]*:\s*([0-9]+[,.]?[0-9]*)\s*kg',
    ]:
        m = re.search(p, page)
        if m:
            lignes.append(f"Poids : {m.group(1).strip()} kg")
            break

    # ── Champs selon catégorie ─────────────────────────────────────────────
    if "lave-linge" in type_norm or "lave linge" in type_norm:
        _extract(page, lignes, "Capacité", [
            r'[Cc]apacit[eé][^:]*:\s*<?[^>]*>?\s*([0-9]+[,.]?[0-9]*)\s*kg',
        ], "kg")
        _extract(page, lignes, "Essorage", [
            r'[Ee]ssorage[^:]*:\s*<?[^>]*>?\s*([0-9 ]+)\s*(?:tr/?min|trs)',
            r'[Vv]itesse[^:]*:\s*([0-9 ]+)\s*tr/?min',
        ], "tr/min")
        _extract_energie(page, lignes)
        _extract(page, lignes, "Niveau sonore", [
            r'[Nn]iveau sonore[^:]*:\s*<?[^>]*>?\s*([0-9]+)\s*(?:dB)?',
            r'[Bb]ruit[^:]*:\s*([0-9]+)\s*dB',
        ], "dB")

    elif "lave-vaisselle" in type_norm or "lave vaisselle" in type_norm:
        _extract(page, lignes, "Capacité", [
            r'([0-9]+)\s*couverts?',
            r'[Cc]apacit[eé][^:]*:\s*([0-9]+)\s*couverts?',
        ], "couverts")
        _extract_energie(page, lignes)
        _extract(page, lignes, "Niveau sonore", [
            r'[Nn]iveau sonore[^:]*:\s*<?[^>]*>?\s*([0-9]+)\s*(?:dB)?',
        ], "dB")
        _extract(page, lignes, "Consommation eau", [
            r'[Cc]onsommation[^:]*eau[^:]*:\s*([0-9]+[,.]?[0-9]*)\s*[lL]',
        ], "L/cycle")

    elif "seche-linge" in type_norm or "sèche-linge" in type_norm or "seche linge" in type_norm or "sèche linge" in type_norm:
        # Type de séchage : chercher le mot-clé dans la page
        for mot, label in [
            ("pompe à chaleur", "Pompe à chaleur"),
            ("pompe a chaleur", "Pompe à chaleur"),
            ("condensation", "Condensation"),
            ("évacuation", "Évacuation"),
            ("evacuation", "Évacuation"),
        ]:
            if mot in page.lower():
                lignes.append(f"Type de séchage : {label}")
                break
        _extract(page, lignes, "Capacité", [
            r'[Cc]apacit[eé][^:]*:\s*([0-9]+[,.]?[0-9]*)\s*kg',
        ], "kg")
        _extract_energie(page, lignes)
        _extract(page, lignes, "Niveau sonore", [
            r'[Nn]iveau sonore[^:]*:\s*<?[^>]*>?\s*([0-9]+)\s*(?:dB)?',
        ], "dB")

    elif "four" in type_norm or "cuisini" in type_norm:
        # Type de cuisson
        for mot, label in [
            ("pyrolyse", "Pyrolyse"),
            ("chaleur tournante", "Chaleur tournante"),
            ("gaz", "Gaz"),
            ("électrique", "Électrique"),
            ("electrique", "Électrique"),
        ]:
            if mot in page.lower():
                lignes.append(f"Type de cuisson : {label}")
                break
        _extract(page, lignes, "Capacité", [
            r'[Cc]apacit[eé][^:]*:\s*([0-9]+[,.]?[0-9]*)\s*[lL]',
            r'[Vv]olume[^:]*:\s*([0-9]+[,.]?[0-9]*)\s*[lL]',
        ], "L")
        _extract_energie(page, lignes)

    elif "réfrigérateur" in type_norm or "refrigerateur" in type_norm or "frigo" in type_norm:
        # Type de froid
        for mot, label in [
            ("no frost", "No Frost"),
            ("froid ventilé", "Froid ventilé"),
            ("froid ventile", "Froid ventilé"),
            ("froid statique", "Froid statique"),
        ]:
            if mot in page.lower():
                lignes.append(f"Type de froid : {label}")
                break
        _extract(page, lignes, "Capacité totale", [
            r'[Cc]apacit[eé] totale[^:]*:\s*([0-9]+)\s*[lL]',
            r'[Cc]apacit[eé][^:]*:\s*([0-9]+)\s*[lL]',
        ], "L")
        _extract_energie(page, lignes)
        _extract(page, lignes, "Niveau sonore", [
            r'[Nn]iveau sonore[^:]*:\s*<?[^>]*>?\s*([0-9]+)\s*(?:dB)?',
        ], "dB")

    elif "congélateur" in type_norm or "congelateur" in type_norm:
        _extract(page, lignes, "Capacité", [
            r'[Cc]apacit[eé][^:]*:\s*([0-9]+)\s*[lL]',
        ], "L")
        _extract_energie(page, lignes)
        _extract(page, lignes, "Niveau sonore", [
            r'[Nn]iveau sonore[^:]*:\s*<?[^>]*>?\s*([0-9]+)\s*(?:dB)?',
        ], "dB")

    else:
        # Générique
        _extract_energie(page, lignes)
        _extract(page, lignes, "Capacité", [
            r'[Cc]apacit[eé][^:]*:\s*([0-9]+[,.]?[0-9]*)\s*(kg|L|couverts?)',
        ], "")

    # ── Dimensions ─────────────────────────────────────────────────────────
    dimensions = "Non trouvé"
    for p in [
        r'LxHxP\s*[:\s]*([0-9]+[,.]?[0-9]*\s*[×xX]\s*[0-9]+[,.]?[0-9]*\s*[×xX]\s*[0-9]+[,.]?[0-9]*\s*cm)',
        r'([0-9]+[,.]?[0-9]*\s*[×xX]\s*[0-9]+[,.]?[0-9]*\s*[×xX]\s*[0-9]+[,.]?[0-9]*\s*cm)',
        r'[Ll]argeur[^:]*:\s*([0-9]+[,.]?[0-9]*)\s*cm',
    ]:
        m = re.search(p, page, re.IGNORECASE)
        if m:
            dimensions = m.group(1).strip()
            break

    fiche = "\n".join(lignes) if lignes else "Non trouvé"
    return fiche, dimensions


def _extract(page, lignes, label, patterns, unit):
    for p in patterns:
        m = re.search(p, page, re.IGNORECASE)
        if m:
            val = m.group(1).strip()
            lignes.append(f"{label} : {val} {unit}".strip())
            return True
    return False


def _extract_energie(page, lignes):
    # Nouvelle classe (post-2021 : A, B, C, D, E, F, G sans +)
    for p in [
        r'[Nn]ouvelle classe[^:]*:\s*<[^>]*>([A-G])</a>',
        r'[Nn]ouvelle classe[^:]*:\s*([A-G])\b',
        r'[Cc]lasse.*?2021[^:]*:\s*([A-G])\b',
    ]:
        m = re.search(p, page, re.IGNORECASE)
        if m:
            lignes.append(f"Nouvelle classe énergétique : {m.group(1).strip()}")
            return

    # Ancienne classe (A+++, A++, A+, A, B, C, D, E, F, G)
    for p in [
        r'[Aa]ncienne classe[^:]*:\s*<[^>]*>([A-G][+]*)</a>',
        r'[Aa]ncienne classe[^:]*:\s*([A-G][+]+)',
        r'[Cc]lasse [eé]nerg[^:]*:\s*([A-G][+]*)',
    ]:
        m = re.search(p, page, re.IGNORECASE)
        if m:
            lignes.append(f"Ancienne classe énergétique : {m.group(1).strip()}")
            return


# ── Google Sheets ─────────────────────────────────────────────────────────────
def open_sheet():
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_file(CREDS_FILE, scopes=scopes)
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(SHEET_ID)
    return sh.worksheet(SHEET_TAB)


# ── Boucle principale ─────────────────────────────────────────────────────────
def process_all():
    log(f"Shard {SHARD_INDEX}/{SHARD_COUNT} — batch={BATCH_LIMIT} — delay={DELAY_SECONDS}s")

    ws = open_sheet()
    all_values = ws.get_all_values()

    if len(all_values) < 2:
        log("Aucune donnée dans l'onglet.")
        return

    # Lignes de données (à partir de la ligne 2, index 1)
    rows = all_values[1:]
    traites = 0

    for i, row in enumerate(rows, start=2):
        # Shard : chaque job traite 1/N des lignes
        if (i - 2) % SHARD_COUNT != SHARD_INDEX:
            continue

        if traites >= BATCH_LIMIT:
            log(f"Limite de lot atteinte ({BATCH_LIMIT}) — arrêt.")
            break

        def get(col_idx):
            return row[col_idx].strip() if col_idx < len(row) else ""

        type_app = get(COL_TYPE)
        marque   = get(COL_MARQUE)
        modele   = get(COL_MODELE)

        if not marque and not modele:
            continue

        # Sauter si déjà traité (E et G remplis)
        prix_exist  = get(COL_PRIX)
        fiche_exist = get(COL_FICHE)
        if prix_exist and fiche_exist:
            continue

        log(f"Ligne {i} : {type_app} | {marque} | {modele}")

        try:
            modele_clean = _nettoyer_modele(marque, modele)

            # ── 1. Trouver l'URL ──────────────────────────────────────────
            url = get(COL_LIEN)
            if url and not (_slug_matches(url, modele_clean) or _slug_matches(url, modele)):
                log(f"  ⚠ Lien existant invalide — recherche...")
                url = None

            if not url:
                url, _ = find_product_url(type_app, marque, modele)

            if not url:
                log(f"  ✗ URL introuvable")
                ws.update_cell(i, 5, "Non trouvé")   # E
                ws.update_cell(i, 6, "Non trouvé")   # F
                ws.update_cell(i, 7, "Non trouvé")   # G
                ws.update_cell(i, 8, "Non trouvé")   # H
                traites += 1
                time.sleep(DELAY_SECONDS)
                continue

            log(f"  → URL : {url}")

            # ── 2. Prix ───────────────────────────────────────────────────
            time.sleep(2)
            prix, _ = fetch_prix(url)
            prix_val = prix if prix else "Non trouvé"
            log(f"  → Prix : {prix_val}")

            # ── 3. Fiche technique ────────────────────────────────────────
            time.sleep(2)
            fiche, dimensions = fetch_fiche_technique(url, type_app)
            log(f"  → Fiche :\n{fiche}")
            log(f"  → Dimensions : {dimensions}")

            # ── 4. Écriture dans le sheet ─────────────────────────────────
            ws.update_cell(i, 5, prix_val)    # E — Prix neuf
            ws.update_cell(i, 6, url)          # F — Lien
            ws.update_cell(i, 7, fiche)        # G — Fiche technique
            ws.update_cell(i, 8, dimensions)   # H — Dimensions

            traites += 1
            log(f"  ✓ Ligne {i} écrite.")

        except RateLimitError as e:
            log(f"  ⛔ RATE LIMIT — arrêt immédiat. ({e})")
            sys.exit(1)

        except Exception as e:
            log(f"  ✗ Erreur inattendue : {e}")
            ws.update_cell(i, 5, "Erreur")
            ws.update_cell(i, 7, f"Erreur : {e}")
            traites += 1

        time.sleep(DELAY_SECONDS)

    log(f"✓ Terminé — {traites} lignes traitées.")


if __name__ == "__main__":
    process_all()
