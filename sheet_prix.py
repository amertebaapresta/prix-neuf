"""
sheet_prix_superset.py (v2 — worker pool, pas de sharding fixe)
================================================================
Chaque job GitHub Actions (shard) est un worker indépendant :
1. Lit le sheet, trouve la première ligne vide non réservée
2. Réserve cette ligne en écrivant "En cours" dans Price Scrapping
3. Cherche l'URL + le prix
4. Écrit le résultat final
5. Répète jusqu'à BATCH_LIMIT lignes ou 403

Avec 20 workers en parallèle, chacun a sa propre IP GitHub Actions.
Pas de division fixe des lignes — chaque worker prend ce qui est disponible.
"""

import os, re, sys, time, html, unicodedata, random
from datetime import datetime
from zoneinfo import ZoneInfo
import requests, gspread
from google.oauth2.service_account import Credentials

PARIS_TZ = ZoneInfo("Europe/Paris")
def now_paris(): return datetime.now(PARIS_TZ)
def log(msg): print(f"[{now_paris().strftime('%H:%M:%S')}] {msg}", flush=True)

# ── Config ────────────────────────────────────────────────────────────────────
SHEET_ID         = os.environ.get("SHEET_ID", "1qzqx8HrIJf45F-U-hDuazYb9y54YSrnwgB1bNQkgLaM")
WORKSHEET_NAME   = "Export Superset"
CREDENTIALS_FILE = "credentials.json"
FORCE_REFRESH_ALL = False
BATCH_LIMIT      = int(os.environ.get("BATCH_LIMIT",    "2"))
DELAY_SECONDS    = float(os.environ.get("DELAY_SECONDS", "20"))
SHARD_INDEX      = int(os.environ.get("SHARD_INDEX",    "0"))

COL_TYPE  = "Type appareil"
COL_MARQUE= "Marque"
COL_MODELE= "Modèle"
COL_PRIX  = "Price Scrapping"
COL_LIEN  = "Lien Scrapping"

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
        try:
            r = SESSION.get("https://www.electromenager-compare.com/", headers=HEADERS_HTTP, timeout=15)
            if r.status_code == 403:
                raise RateLimitError("403 sur session initiale")
        except RateLimitError:
            raise
        except Exception:
            pass
        _SESSION_INIT = True

class RateLimitError(Exception): pass

def _post_or_stop(url, **kwargs):
    r = SESSION.post(url, **kwargs)
    if r.status_code == 403: raise RateLimitError("403")
    return r

def _get_or_stop(url, **kwargs):
    r = SESSION.get(url, **kwargs)
    if r.status_code == 403: raise RateLimitError("403")
    return r

def _decode(r):
    try: return r.content.decode("iso-8859-1")
    except Exception: return r.text

# ── Logique recherche URL ─────────────────────────────────────────────────────
PRODUCT_LINK_REGEX = (
    r'href="(https?://(?:www\.)?electromenager-compare\.com/'
    r'[a-z\-]+-[A-Za-z0-9]+-[A-Za-z0-9\-]+\.htm)"'
)
EXCLUDE_URL_PATTERNS = ("-liste-", "/recherche", "/marques", "/avis-")

def _is_recherche_page(url):
    return url.rsplit("/", 1)[-1].startswith("recherche-")

def _slug_matches(url, modele):
    url_norm = re.sub(r"[\s\-]","",url).upper()
    norm = re.sub(r"[\s\-]","",modele).upper()
    if norm in url_norm: return True
    norm_clean = re.sub(r"^\.","",modele)
    norm_clean = re.sub(r"[\s\-\.\+/]","",norm_clean).upper()
    if norm_clean in url_norm: return True
    return False

SUFFIXES_REGIONAUX = ["FR","EU","UK","GB","DE","IT","ES","PT","NL","BE","CH","AT","PL","INT","EUR","US","EF","EC","LE"]

def _strip_regional_suffix(modele):
    mu = modele.upper()
    for suf in sorted(SUFFIXES_REGIONAUX, key=len, reverse=True):
        if mu.endswith(suf) and len(modele) > len(suf)+3:
            return modele[:-len(suf)]
    return None

TYPE_VERS_PREFIXES_URL = {
    "lave-linge":["lave-linge"], "lave linge":["lave-linge"],
    "lave-vaisselle":["lave-vaisselle"], "lave vaisselle":["lave-vaisselle"],
    "refrigerateur":["refrigerateur"], "réfrigérateur":["refrigerateur"],
    "seche-linge":["seche-linge","lave-linge"], "sèche-linge":["seche-linge","lave-linge"],
    "seche linge":["seche-linge","lave-linge"], "sèche linge":["seche-linge","lave-linge"],
    "congelateur":["congelateur"], "congélateur":["congelateur"],
    "four":["four"], "cuisiniere":["four","cuisiniere"], "cuisinière":["four","cuisiniere"],
    "micro-ondes":["four","micro-ondes"],
}

def _normaliser(t):
    t = unicodedata.normalize("NFKD", t)
    return "".join(c for c in t if not unicodedata.combining(c)).lower()

def _categorie_coherente(url, type_appareil):
    if not type_appareil: return True
    tn = _normaliser(type_appareil)
    slug = url.rsplit("/",1)[-1].lower()
    for cle, prefixes in TYPE_VERS_PREFIXES_URL.items():
        if cle in tn:
            return any(slug.startswith(p) for p in prefixes)
    return True

def _try_search(query, type_appareil=None, modele=None):
    try:
        r = _post_or_stop(
            "https://www.electromenager-compare.com/index.php",
            params={"action":"sbtsrch","type":"0"},
            data={"q":query},
            headers=HEADERS_HTTP, timeout=15, allow_redirects=True,
        )
    except RateLimitError: raise
    except Exception as e: log(f"  ⚠ Erreur recherche: {e}"); return None, None
    if r.status_code != 200: return None, None
    final_url = r.url
    if (final_url.endswith(".htm") and "electromenager-compare.com" in final_url
            and not _is_recherche_page(final_url)
            and not any(p in final_url for p in EXCLUDE_URL_PATTERNS)
            and _categorie_coherente(final_url, type_appareil)):
        return final_url, "directe"
    page_html = html.unescape(r.text)
    candidates = [
        c for c in re.findall(PRODUCT_LINK_REGEX, page_html)
        if not _is_recherche_page(c)
        and not any(p in c for p in EXCLUDE_URL_PATTERNS)
        and re.search(r"[A-Z]{2,}", c.rsplit("/",1)[-1])
        and _categorie_coherente(c, type_appareil)
    ]
    if not candidates: return None, None
    if modele:
        for c in candidates:
            if _slug_matches(c, modele): return c, "liste-exact"
    return candidates[0], "liste"

MARQUES_PLACEHOLDER   = {"marque inconnue","inconnue","inconnu","n/a","na",""}
MODELES_INUTILISABLES = {"vde","non renseignée","non renseigne",""}

def _nettoyer_modele(marque, modele):
    if not modele: return modele
    mu = (marque or "").upper()
    m  = modele.strip()
    m  = re.sub(r"(?i)EUROSAV","",m).strip()
    if "SAMSUNG" in mu: m = re.sub(r"/.*$","",m).strip()
    elif "SIEMENS" in mu: m = re.sub(r"[/,].*$","",m).strip(); m = re.sub(r"\d{2,3}$","",m).strip()
    elif "VEDETTE" in mu: m = re.sub(r"[/,].*$","",m).strip(); m = re.sub(r"\d{2}$","",m).strip()
    elif "BOSCH"   in mu: m = m[:10]
    return m if m else modele

def _generer_variantes_tiret(modele):
    return [modele[:-n]+"-"+modele[-n:] for n in (1,2,3) if len(modele)>n+2]

def _sans_prefixe_marque(marque, modele):
    mn = re.sub(r"[^A-Za-z]","",marque).upper()
    for t in (4,3):
        if len(mn)>=t and len(modele)>t+3 and modele.upper().startswith(mn[:t]):
            return modele[t:]
    return None

def find_product_url(type_appareil, marque, modele):
    _init_session()
    me = "" if marque.strip().lower() in MARQUES_PLACEHOLDER else marque
    mc = _nettoyer_modele(marque, modele)
    if mc.lower() in MODELES_INUTILISABLES: return None, None
    if mc != modele: log(f"  → Nettoyé: '{modele}' → '{mc}'")
    url, _ = _try_search(f"{me} {mc}".strip(), type_appareil, mc)
    if url and _slug_matches(url, mc): return url, "haute"
    ss = _strip_regional_suffix(mc)
    url2 = None
    if ss:
        time.sleep(2)
        url2, _ = _try_search(f"{me} {ss}".strip(), type_appareil, ss)
        if url2 and (_slug_matches(url2, mc) or _slug_matches(url2, ss)): return url2, "haute"
    sp = _sans_prefixe_marque(marque, mc)
    if sp:
        time.sleep(2)
        u, _ = _try_search(f"{me} {sp}".strip(), type_appareil, sp)
        if u and _slug_matches(u, sp): return u, "haute"
    for v in _generer_variantes_tiret(mc):
        time.sleep(2)
        u, _ = _try_search(f"{me} {v}".strip(), type_appareil, mc)
        if u and _slug_matches(u, mc): return u, "haute"
    if url2: return url2, "approximative"
    if url:  return url,  "approximative"
    return None, None

# ── Extraction prix ───────────────────────────────────────────────────────────
PRICE_PATTERNS = [
    r"Dernier prix relev[ée]\s*[:\-]?\s*([\d\s]+[.,]\d{2})\s*€",
    r"[AÀ]\s*PARTIR\s*DE\s*([\d\s]+[.,]?\d*)\s*€",
    r"([\d]{2,4}[.,]\d{2})\s*€",
]

def fetch_price(url):
    r = _get_or_stop(url, headers=HEADERS_HTTP, timeout=15, allow_redirects=True)
    if r.status_code != 200: return None
    page = html.unescape(_decode(r))
    texte = re.sub(r"<[^>]+>", " ", page)
    texte = re.sub(r"\s+", " ", texte)
    for pat in PRICE_PATTERNS:
        m = re.search(pat, texte, re.IGNORECASE)
        if m:
            raw = m.group(1).replace(" ","").replace("\xa0","").replace(".",",")
            return raw
    return None

# ── Google Sheets ─────────────────────────────────────────────────────────────
def get_sheets():
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=scopes)
    client = gspread.authorize(creds)
    sh = client.open_by_key(SHEET_ID)
    return sh.worksheet(WORKSHEET_NAME), sh.worksheet("Paramètres")

def _write_cell(ws, row_num, col_idx, val):
    col_letter = gspread.utils.rowcol_to_a1(row_num, col_idx + 1)
    retries = 3
    while retries > 0:
        try:
            ws.update(col_letter, [[val]])
            return
        except Exception as e:
            if "429" in str(e):
                log(f"  ⚠ Quota 429 — attente 30s...")
                time.sleep(30)
                retries -= 1
            else:
                log(f"  ⚠ Erreur écriture: {e}")
                return

def _write_row(ws, idx, row_num, values):
    data = []
    for col_name, val in values.items():
        if col_name not in idx: continue
        col_letter = gspread.utils.rowcol_to_a1(row_num, idx[col_name]+1)
        data.append({"range": col_letter, "values": [[val]]})
    if not data: return
    retries = 3
    while retries > 0:
        try:
            ws.batch_update(data)
            return
        except Exception as e:
            if "429" in str(e):
                log(f"  ⚠ Quota 429 — attente 30s...")
                time.sleep(30)
                retries -= 1
            else:
                log(f"  ⚠ Erreur écriture: {e}")
                return

# ── Boucle principale (worker pool) ──────────────────────────────────────────
def process_all():
    # Délai aléatoire pour étaler les lectures Google Sheets
    delai = random.randint(SHARD_INDEX * 15, SHARD_INDEX * 15 + 30)
    log(f"Délai démarrage : {delai}s (shard {SHARD_INDEX})")
    time.sleep(delai)

    ws, ws_params = get_sheets()

    # Lire la plage depuis Paramètres
    try:
        params = ws_params.get("B2:C2")[0]
        premiere_ligne = int(params[1])  # C2
        derniere_ligne = int(params[0])  # B2
        log(f"Plage à traiter : lignes {premiere_ligne} → {derniere_ligne}")
    except Exception as e:
        log(f"✗ Impossible de lire Paramètres : {e}")
        return

    if premiere_ligne >= derniere_ligne:
        log("Aucune nouvelle ligne à traiter.")
        return

    header_row = ws.get("A1:I1")[0]
    idx = {name: i for i, name in enumerate(header_row)}

    for col in [COL_TYPE, COL_MARQUE, COL_MODELE]:
        if col not in idx:
            log(f"✗ Colonne manquante : '{col}'")
            sys.exit(1)

    traites = 0; arret = False

    for offset in range(derniere_ligne - premiere_ligne):
        if traites >= BATCH_LIMIT: log(f"Limite atteinte ({BATCH_LIMIT})."); break

        i = premiere_ligne + offset

        # Relire la ligne fraîchement pour voir son état actuel
        try:
            row_data = ws.get(f"A{i}:I{i}")[0]
        except Exception:
            row_data = []

        def get(col):
            j = idx.get(col)
            return row_data[j].strip() if j is not None and j < len(row_data) else ""

        type_app = get(COL_TYPE)
        marque   = get(COL_MARQUE)
        modele   = get(COL_MODELE)
        if not marque and not modele: continue

        # Vérifier si déjà traité ou réservé par un autre worker
        prix_exist = get(COL_PRIX)
        if prix_exist and not FORCE_REFRESH_ALL: continue

        # Réserver cette ligne (écrire "En cours") pour éviter la concurrence
        _write_cell(ws, i, idx[COL_PRIX], "En cours")
        time.sleep(1)  # laisser le temps aux autres workers de voir la réservation

        # Revérifier que personne d'autre n'a pris cette ligne entre-temps
        try:
            check = ws.get(f"A{i}:I{i}")[0]
            prix_check = check[idx[COL_PRIX]].strip() if idx[COL_PRIX] < len(check) else ""
            if prix_check != "En cours":
                log(f"  ⚠ Ligne {i} prise par un autre worker — on passe")
                continue
        except Exception:
            pass

        log(f"Ligne {i} : {type_app} | {marque} | {modele}")

        try:
            mc = _nettoyer_modele(marque, modele)
            url, confiance = find_product_url(type_app, marque, modele)

            if not url:
                log("  ✗ URL introuvable")
                _write_row(ws, idx, i, {COL_PRIX:"Non trouvé", COL_LIEN:"Non trouvé"})
                traites += 1; time.sleep(DELAY_SECONDS); continue

            if confiance == "approximative":
                log("  ⚠ Confiance approximative — Non trouvé")
                _write_row(ws, idx, i, {COL_PRIX:"Non trouvé", COL_LIEN:"Non trouvé"})
                traites += 1; time.sleep(DELAY_SECONDS); continue

            log(f"  → URL : {url}")
            time.sleep(2)
            prix = fetch_price(url)
            prix = prix or "Non trouvé"
            log(f"  → Prix : {prix}")
            _write_row(ws, idx, i, {COL_PRIX: prix, COL_LIEN: url})
            traites += 1
            log(f"  ✓ Ligne {i} OK")

        except RateLimitError:
            log("  ⛔ 403 — ARRÊT IMMÉDIAT.")
            # Libérer la réservation si on s'arrête
            _write_cell(ws, i, idx[COL_PRIX], "")
            arret = True; break
        except Exception as e:
            log(f"  ✗ Erreur : {e}")
            _write_cell(ws, i, idx[COL_PRIX], "")  # libérer
            traites += 1

        time.sleep(DELAY_SECONDS)

    log(f"Terminé — {traites} lignes." + (" (rate-limit)" if arret else ""))

if __name__ == "__main__":
    process_all()
