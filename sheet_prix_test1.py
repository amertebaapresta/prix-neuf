"""
sheet_prix_test1.py — Scrape prix neuf sur electromenager-compare.com
Lit l'onglet TEST1 directement, traite les lignes vides (H+I vides),
avec WebShare proxy + retry 403 (ne s'arrête jamais sur 403).
"""

import os, sys, time, re, random, logging
import requests
import gspread
from google.oauth2.service_account import Credentials

# ── Config ──────────────────────────────────────────────────────────────────
SHEET_ID         = os.environ.get("SHEET_ID", "1qzqx8HrIJf45F-U-hDuazYb9y54YSrnwgB1bNQkgLaM")
WORKSHEET_NAME   = "TEST1"
CREDENTIALS_FILE = "credentials.json"
BATCH_LIMIT      = int(os.environ.get("BATCH_LIMIT", "15"))
DELAY_SECONDS    = float(os.environ.get("DELAY_SECONDS", "5"))
SHARD_INDEX      = int(os.environ.get("SHARD_INDEX", "0"))
SHARD_COUNT      = int(os.environ.get("SHARD_COUNT", "1"))
DELAY_START      = int(os.environ.get("DELAY_START", "0"))  # délai démarrage par shard

# Colonnes
COL_TYPE  = "Type appareil"
COL_MARQUE = "Marque"
COL_MODELE = "Modèle"
COL_PRIX  = "Price Scrapping"
COL_LIEN  = "Lien Scrapping"

# WebShare proxy
_WEBSHARE_RAW = os.environ.get("WEBSHARE_PROXY", "")
PROXY_CONF = {"http": _WEBSHARE_RAW, "https": _WEBSHARE_RAW} if _WEBSHARE_RAW else None

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.WARNING)
_start = time.time()

def log(msg):
    elapsed = time.time() - _start
    h = int(elapsed // 3600)
    m = int((elapsed % 3600) // 60)
    s = int(elapsed % 60)
    print(f"[{h:02d}:{m:02d}:{s:02d}] {msg}", flush=True)

# ── HTTP Session ─────────────────────────────────────────────────────────────
SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "fr-FR,fr;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
})

class RateLimitError(Exception):
    pass

def _post_or_stop(url, **kwargs):
    """POST avec proxy WebShare + retry 5x sur 403."""
    if PROXY_CONF:
        kwargs["proxies"] = PROXY_CONF
    for attempt in range(5):
        try:
            r = SESSION.post(url, **kwargs)
            if r.status_code != 403:
                return r
            log(f"  ⚠ 403 — nouvelle IP WebShare (tentative {attempt+1}/5)")
            time.sleep(3)
        except Exception as e:
            log(f"  ⚠ Erreur réseau (tentative {attempt+1}/5) : {e}")
            time.sleep(3)
    raise RateLimitError("403 — 5 tentatives épuisées")

def _get_or_stop(url, **kwargs):
    """GET avec proxy WebShare + retry 5x sur 403."""
    if PROXY_CONF:
        kwargs["proxies"] = PROXY_CONF
    for attempt in range(5):
        try:
            r = SESSION.get(url, **kwargs)
            if r.status_code != 403:
                return r
            log(f"  ⚠ 403 GET — nouvelle IP WebShare (tentative {attempt+1}/5)")
            time.sleep(3)
        except Exception as e:
            log(f"  ⚠ Erreur réseau GET (tentative {attempt+1}/5) : {e}")
            time.sleep(3)
    raise RateLimitError("403 GET — 5 tentatives épuisées")

# ── Scraping helpers ─────────────────────────────────────────────────────────
BASE_URL = "https://www.electromenager-compare.com"
SEARCH_URL = f"{BASE_URL}/index.php"

def _normalise(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip().upper())

def _prefixes_marque(marque: str) -> list[str]:
    """Génère des variantes de préfixe pour les marques composées (ex: VALBERG → VAL)."""
    m = _normalise(marque)
    prefixes = [m]
    if len(m) >= 3:
        prefixes.append(m[:3])
    if " " in m:
        prefixes.append(m.split()[0])
    return list(dict.fromkeys(prefixes))

def search_product(type_app: str, marque: str, modele: str) -> dict | None:
    """
    Cherche le produit sur electromenager-compare.com.
    Retourne {"prix": "...", "lien": "..."} ou None.
    """
    modele_norm = _normalise(modele)
    marque_norm = _normalise(marque)

    # Essai 1 : marque + modele complet
    candidates = [(marque_norm, modele_norm)]

    # Essai 2 : préfixes de marque + modele complet
    for pfx in _prefixes_marque(marque):
        if pfx != marque_norm:
            candidates.append((pfx, modele_norm))

    # Essai 3 : sans marque, juste le modèle
    candidates.append(("", modele_norm))

    for marque_q, modele_q in candidates:
        query = f"{marque_q} {modele_q}".strip()
        try:
            r = _post_or_stop(
                SEARCH_URL,
                params={"action": "sbtsrch", "type": "0"},
                data={"recherche": query},
                timeout=30,
            )
        except RateLimitError:
            raise
        except Exception as e:
            log(f"  ✗ Erreur POST search '{query}' : {e}")
            continue

        if r.status_code != 200:
            log(f"  ✗ HTTP {r.status_code} pour '{query}'")
            continue

        # Parse résultats
        result = _parse_search_results(r.text, modele_norm, marque_norm)
        if result:
            return result

    return None

def _parse_search_results(html: str, modele_norm: str, marque_norm: str) -> dict | None:
    """
    Parse la page de résultats de recherche.
    Cherche le lien produit correspondant au modèle.
    """
    # Cherche les liens produits dans les résultats
    # Pattern: href="/congelateur-BEKFS166020-BEKO-FS166020.htm" ou similaire
    links = re.findall(r'href="(/[^"]*\.htm)"', html)

    for link in links:
        # Le modèle apparaît généralement dans l'URL (en majuscules, sans espaces)
        modele_slug = re.sub(r"[^A-Z0-9]", "", modele_norm)
        link_upper = link.upper()
        if modele_slug and modele_slug in link_upper:
            full_url = BASE_URL + link
            prix = _get_prix_from_page(full_url)
            if prix:
                return {"prix": prix, "lien": full_url}

    # Fallback: premier lien produit trouvé qui ressemble à une fiche
    for link in links:
        if re.match(r"/[a-z]+-[A-Z0-9]+-[A-Z0-9]+-[A-Z0-9]+\.htm", link):
            full_url = BASE_URL + link
            prix = _get_prix_from_page(full_url)
            if prix:
                return {"prix": prix, "lien": full_url}

    return None

def _get_prix_from_page(url: str) -> str | None:
    """Récupère le prix depuis une fiche produit."""
    try:
        r = _get_or_stop(url, timeout=30)
    except RateLimitError:
        raise
    except Exception as e:
        log(f"  ✗ Erreur GET {url} : {e}")
        return None

    if r.status_code != 200:
        return None

    html = r.text

    # Pattern prix : ex "233,24 €" ou "1 299,00 €"
    # Cherche dans les balises prix habituelles
    patterns = [
        r'class="[^"]*prix[^"]*"[^>]*>[\s\S]{0,50}?([\d\s]+,\d{2})\s*€',
        r'itemprop="price"[^>]*content="([\d.]+)"',
        r'([\d\s]+,\d{2})\s*€',
    ]

    for pattern in patterns:
        m = re.search(pattern, html, re.IGNORECASE)
        if m:
            prix_raw = m.group(1).strip()
            # Nettoie : "1 299,00" → "1299.00"
            prix_clean = prix_raw.replace(" ", "").replace(",", ".")
            try:
                val = float(prix_clean)
                if val > 0:
                    return str(val)
            except ValueError:
                continue

    return None

# ── Google Sheets ────────────────────────────────────────────────────────────
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

def open_sheet():
    creds = Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=SCOPES)
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(SHEET_ID)
    ws = sh.worksheet(WORKSHEET_NAME)
    return ws

def _write_cell(ws, row_1based, col_1based, value):
    """Écrit une cellule (1-indexed)."""
    ws.update_cell(row_1based, col_1based, value)

# ── Main ─────────────────────────────────────────────────────────────────────
def main():
    # Délai de démarrage (évite burst simultané si plusieurs shards)
    if DELAY_START > 0:
        log(f"Délai de démarrage : {DELAY_START}s (shard {SHARD_INDEX})")
        time.sleep(DELAY_START)
    else:
        delay = SHARD_INDEX * 10
        if delay > 0:
            log(f"Délai de démarrage : {delay}s (shard {SHARD_INDEX})")
            time.sleep(delay)

    log(f"Connexion Google Sheet (onglet {WORKSHEET_NAME})…")
    ws = open_sheet()

    # Lecture toutes les valeurs
    all_rows = ws.get_all_values()
    if not all_rows:
        log("Onglet vide.")
        return

    # Ligne d'entête
    headers = [h.strip() for h in all_rows[0]]
    try:
        idx = {
            COL_TYPE:   headers.index(COL_TYPE)   + 1,  # 1-based col
            COL_MARQUE: headers.index(COL_MARQUE) + 1,
            COL_MODELE: headers.index(COL_MODELE) + 1,
            COL_PRIX:   headers.index(COL_PRIX)   + 1,
            COL_LIEN:   headers.index(COL_LIEN)   + 1,
        }
    except ValueError as e:
        log(f"Colonne manquante : {e}")
        log(f"Colonnes trouvées : {headers}")
        sys.exit(1)

    log(f"Colonnes : {idx}")
    log(f"Lignes totales (hors entête) : {len(all_rows) - 1}")

    # Identifie les candidats : lignes vides (H et I vides)
    candidats = []
    for i, row in enumerate(all_rows[1:], start=2):  # 2 = ligne sheet (1-indexed, entête=1)
        # Extend row si nécessaire
        while len(row) < max(idx.values()):
            row.append("")

        prix_val = row[idx[COL_PRIX] - 1].strip()
        lien_val = row[idx[COL_LIEN] - 1].strip()

        # Skip si déjà rempli
        if prix_val and lien_val:
            continue

        # Skip si pas de modèle
        modele_val = row[idx[COL_MODELE] - 1].strip()
        if not modele_val:
            continue

        candidats.append(i)

    log(f"Candidats à traiter : {len(candidats)}")

    if not candidats:
        log("Rien à faire.")
        return

    # Distribution par shard
    mes_candidats = [
        c for j, c in enumerate(candidats)
        if j % SHARD_COUNT == SHARD_INDEX
    ]
    log(f"Shard {SHARD_INDEX}/{SHARD_COUNT} → {len(mes_candidats)} ligne(s)")

    traites = 0
    t_debut = time.time()

    for row_num in mes_candidats:
        if traites >= BATCH_LIMIT:
            break

        row = all_rows[row_num - 1]  # 0-indexed
        while len(row) < max(idx.values()):
            row.append("")

        type_app = row[idx[COL_TYPE] - 1].strip()
        marque   = row[idx[COL_MARQUE] - 1].strip()
        modele   = row[idx[COL_MODELE] - 1].strip()

        log(f"Ligne {row_num} : {type_app} | {marque} | {modele}")

        try:
            result = search_product(type_app, marque, modele)
        except RateLimitError as e:
            log(f"  ⚠ RateLimitError (5 tentatives épuisées) : {e} — ligne sautée")
            traites += 1
            continue
        except Exception as e:
            log(f"  ✗ Erreur inattendue : {e} — ligne sautée")
            traites += 1
            continue

        if result:
            prix = result["prix"]
            lien = result["lien"]
            log(f"  ✓ Prix : {prix} € | Lien : {lien}")
            try:
                _write_cell(ws, row_num, idx[COL_PRIX], prix)
                _write_cell(ws, row_num, idx[COL_LIEN], lien)
                log(f"  ✓ Ligne {row_num} écrite")
            except Exception as e:
                log(f"  ✗ Erreur écriture Google Sheet : {e}")
        else:
            log(f"  — Pas de résultat pour {marque} {modele}")

        traites += 1

        if traites < BATCH_LIMIT and row_num != mes_candidats[-1]:
            time.sleep(DELAY_SECONDS)

    duree = time.time() - t_debut
    log(f"Terminé — {traites} ligne(s) traitée(s) en {duree:.0f}s "
        f"({duree/max(traites,1):.1f}s/machine)")

    restants = len([
        c for c in candidats
        if c not in mes_candidats or mes_candidats.index(c) >= BATCH_LIMIT
    ])
    log(f"Lignes restantes à traiter (tous shards) : estimé {len(candidats) - traites}")

if __name__ == "__main__":
    main()
