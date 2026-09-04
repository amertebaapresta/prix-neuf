"""
sheet_prix_superset.py
======================
Adapté de sheet_fiche.py pour l'onglet Export Superset.
Lit Type appareil (E), Marque (F), Modèle (G) et écrit
Price Scrapping (H) + Lien Scrapping (I).

Toute la logique de recherche URL est identique à sheet_fiche.py.
"""

import os, re, sys, time, html, unicodedata
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
BATCH_LIMIT      = int(os.environ.get("BATCH_LIMIT",    "10"))
DELAY_SECONDS    = float(os.environ.get("DELAY_SECONDS", "20"))
SHARD_INDEX      = int(os.environ.get("SHARD_INDEX",    "0"))
SHARD_COUNT      = int(os.environ.get("SHARD_COUNT",    "3"))

# Colonnes (noms exacts dans la ligne 1 du sheet)
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
            # Pause initiale pour laisser le temps au site de "oublier"
            # les requêtes récentes depuis cette IP
            time.sleep(10)
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

# ── Logique recherche URL (identique sheet_fiche.py) ─────────────────────────
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
    norm_plus = norm_clean.replace("+","PLUS")
    if norm_plus in url_norm: return True
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
    tn   = _normaliser(type_appareil)
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

def _prefixes_marque(marque, modele):
    variantes = []
    marque_norm = re.sub(r"[^A-Za-z0-9]","",marque).upper()
    modele_norm = re.sub(r"[^A-Za-z0-9]","",modele).upper()
    for taille in (3, 4):
        if len(marque_norm) >= taille:
            prefixe = marque_norm[:taille]
            variantes.append(prefixe + modele_norm)
    return variantes

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
    for v in _prefixes_marque(marque, mc):
        time.sleep(2)
        u, _ = _try_search(f"{me} {v}".strip(), type_appareil, mc)
        if u and _slug_matches(u, mc): return u, "haute"
        time.sleep(2)
        u, _ = _try_search(v, type_appareil, mc)
        if u and _slug_matches(u, mc): return u, "haute"
    if url2: return url2, "approximative"
    if url:  return url,  "approximative"
    return None, None

# ── Extraction prix depuis HTML ───────────────────────────────────────────────
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
def get_worksheet():
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=scopes)
    client = gspread.authorize(creds)
    sh = client.open_by_key(SHEET_ID)
    ws = sh.worksheet(WORKSHEET_NAME)
    ws_params = sh.worksheet("Paramètres")
    return ws, ws_params

def _write(ws, idx, row_num, values):
    if not values: return
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
            break
        except Exception as e:
            if "429" in str(e):
                log(f"  ⚠ Quota 429 — attente 30s...")
                time.sleep(30)
                retries -= 1
            else:
                log(f"  ⚠ Erreur écriture: {e}")
                break

# ── Boucle principale ─────────────────────────────────────────────────────────
def process_all():
    import random
    delai = SHARD_INDEX * 30 + random.randint(0, 15)
    log(f"Délai démarrage : {delai}s (shard {SHARD_INDEX})")
    time.sleep(delai)

    ws, ws_params = get_worksheet()

    # Lire B2 (dernière ligne vide) et C2 (première ligne ajoutée) dans Paramètres
    try:
        params = ws_params.get("B2:C2")[0]
        premiere_ligne = int(params[1])  # C2 = première ligne ajoutée par Make.com
        derniere_ligne = int(params[0])  # B2 = dernière ligne vide
        log(f"Plage à traiter : lignes {premiere_ligne} → {derniere_ligne}")
    except Exception as e:
        log(f"✗ Impossible de lire l'onglet Paramètres : {e}")
        log("  → Traitement annulé (C2 non renseigné — Make.com n'a pas encore tourné ?)")
        return

    if premiere_ligne >= derniere_ligne:
        log("Aucune nouvelle ligne à traiter (C2 >= B2).")
        return

    # Lire l'en-tête (ligne 1) + les nouvelles lignes seulement
    header_row = ws.get("A1:I1")[0]
    new_rows   = ws.get(f"A{premiere_ligne}:I{derniere_ligne - 1}")

    if not new_rows:
        log("Aucune ligne dans la plage.")
        return

    header = header_row
    idx    = {name: i for i, name in enumerate(header)}

    for col in [COL_TYPE, COL_MARQUE, COL_MODELE]:
        if col not in idx:
            log(f"✗ Colonne manquante : '{col}' | En-têtes : {header}")
            sys.exit(1)

    traites = 0; candidat_index = 0; arret = False

    for offset, row in enumerate(new_rows):
        if traites >= BATCH_LIMIT: log(f"Limite atteinte ({BATCH_LIMIT})."); break

        # Numéro réel de la ligne dans le Sheet
        i = premiere_ligne + offset

        def get(col):
            j = idx.get(col)
            return row[j].strip() if j is not None and j < len(row) else ""

        type_app = get(COL_TYPE)
        marque   = get(COL_MARQUE)
        modele   = get(COL_MODELE)
        if not marque and not modele: continue

        # Sauter si déjà traité
        prix_exist = get(COL_PRIX)
        if prix_exist and not FORCE_REFRESH_ALL: continue

        mon_tour = (candidat_index % SHARD_COUNT == SHARD_INDEX)
        candidat_index += 1
        if not mon_tour: continue

        log(f"Ligne {i} : {type_app} | {marque} | {modele}")

        try:
            mc  = _nettoyer_modele(marque, modele)
            url = get(COL_LIEN)
            confiance = None

            if url and (not url.startswith("http") or url in ("Non trouvé", "Erreur")):
                url = None

            if url:
                if _slug_matches(url, mc) or _slug_matches(url, modele):
                    confiance = "haute"
                else:
                    log(f"  ⚠ Lien invalide — re-cherche"); url = None

            if not url:
                url, confiance = find_product_url(type_app, marque, modele)

            if not url:
                log("  ✗ URL introuvable")
                _write(ws, idx, i, {COL_PRIX:"Non trouvé", COL_LIEN:"Non trouvé"})
                traites += 1; time.sleep(DELAY_SECONDS); continue

            if confiance == "approximative":
                log("  ⚠ Confiance approximative — Non trouvé")
                _write(ws, idx, i, {COL_PRIX:"Non trouvé", COL_LIEN:"Non trouvé"})
                traites += 1; time.sleep(DELAY_SECONDS); continue

            log(f"  → URL : {url}")
            time.sleep(2)
            prix = fetch_price(url)
            prix = prix or "Non trouvé"
            log(f"  → Prix : {prix}")
            _write(ws, idx, i, {COL_PRIX: prix, COL_LIEN: url})
            traites += 1
            log(f"  ✓ Ligne {i} OK")

        except RateLimitError:
            log("  ⛔ 403 — ARRÊT IMMÉDIAT."); arret = True; break
        except Exception as e:
            log(f"  ✗ Erreur : {e}")
            import traceback; traceback.print_exc()
            traites += 1

        time.sleep(DELAY_SECONDS)

    log(f"Terminé — {traites} lignes." + (" (rate-limit)" if arret else ""))

if __name__ == "__main__":
    process_all()
