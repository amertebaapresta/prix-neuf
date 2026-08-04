"""
sheet_fiche.py — VERSION DEBUG PURE
Dump le HTML brut dans les logs pour identifier les vrais patterns
"""
import os, re, sys, time, html, unicodedata
from datetime import datetime
from zoneinfo import ZoneInfo
import requests, gspread
from google.oauth2.service_account import Credentials

PARIS_TZ = ZoneInfo("Europe/Paris")
def now_paris(): return datetime.now(PARIS_TZ)

SHEET_ID       = os.environ.get("SHEET_ID_FICHE", "1MK6TiPQZUX4IwoYzfVB4Ofo1fFbUm_5qitsrpIjJps0")
WORKSHEET_NAME = "TEST"
CREDENTIALS_FILE = "credentials.json"
BATCH_LIMIT    = int(os.environ.get("BATCH_LIMIT", "1"))  # 1 seule ligne
DELAY_SECONDS  = float(os.environ.get("DELAY_SECONDS", "5"))
SHARD_INDEX    = int(os.environ.get("SHARD_INDEX", "0"))
SHARD_COUNT    = int(os.environ.get("SHARD_COUNT", "1"))

COL_TYPE="Type"; COL_MARQUE="Marque"; COL_MODELE="Modèle"
COL_PRIX="Prix neuf"; COL_LIEN="Lien"; COL_FICHE="Fiche technique"; COL_DIM="Dimension"

HEADERS_HTTP = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "fr-FR,fr;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Referer": "https://www.electromenager-compare.com/",
}
SESSION = requests.Session()

def log(msg): print(f"[{now_paris().strftime('%H:%M:%S')}] {msg}", flush=True)

class RateLimitError(Exception): pass

def _get_or_stop(url):
    r = SESSION.get(url, headers=HEADERS_HTTP, timeout=15, allow_redirects=True)
    if r.status_code == 403: raise RateLimitError("403")
    return r

def get_worksheet():
    scopes = ["https://www.googleapis.com/auth/spreadsheets","https://www.googleapis.com/auth/drive.readonly"]
    creds = Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=scopes)
    return gspread.authorize(creds).open_by_key(SHEET_ID).worksheet(WORKSHEET_NAME)

def process_all():
    ws = get_worksheet()
    all_values = ws.get_all_values()
    header = all_values[0]
    idx = {name: i for i, name in enumerate(header)}

    # Prendre la première ligne qui a un lien valide
    for i, row in enumerate(all_values[1:], start=2):
        def get(col):
            j = idx.get(col)
            return row[j].strip() if j is not None and j < len(row) else ""

        url = get(COL_LIEN)
        type_app = get(COL_TYPE)
        marque = get(COL_MARQUE)
        modele = get(COL_MODELE)

        if not url or url == "Non trouvé": continue

        log(f"=== DEBUG ligne {i} : {type_app} | {marque} | {modele} ===")
        log(f"URL : {url}")

        try:
            r = _get_or_stop(url)
        except RateLimitError:
            log("403 !")
            sys.exit(1)

        log(f"HTTP {r.status_code}")
        log(f"Content-Type: {r.headers.get('content-type','?')}")
        log(f"Encoding annoncé: {r.encoding}")

        # Tester les deux encodages
        for enc in ["iso-8859-1", "utf-8"]:
            try:
                page = r.content.decode(enc)
                log(f"=== Décodage {enc} ({len(page)} chars) ===")

                # Dump 2000 premiers chars
                log(f"DEBUT PAGE: {repr(page[:500])}")

                # Chercher chaque mot-clé et dumper le contexte
                keywords = ["Poids", "Capacit", "Dimension", "Classe", "ssorage", "sonore", "couverts", "Volume"]
                for kw in keywords:
                    idx2 = page.lower().find(kw.lower())
                    if idx2 >= 0:
                        extrait = page[max(0,idx2-20):idx2+300]
                        log(f"  KW[{kw}]: {repr(extrait)}")
                    else:
                        log(f"  KW[{kw}]: ABSENT")
                break
            except Exception as e:
                log(f"Erreur décodage {enc}: {e}")

        # Écrire DEBUG dans le sheet
        col_letter = gspread.utils.rowcol_to_a1(i, idx[COL_FICHE] + 1)
        ws.update_acell(col_letter, "DEBUG - voir logs GitHub Actions")
        break  # une seule ligne

    log("DEBUG terminé.")

if __name__ == "__main__":
    process_all()
