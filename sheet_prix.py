"""
sheet_prix.py
=============
Lit une liste d'appareils (Type / Marque / Modèle) depuis un Google Sheet,
retrouve automatiquement la fiche produit sur electromenager-compare.com
via leur moteur de recherche interne, en extrait le prix, et réécrit
Prix / Lien / Statut / Date MAJ dans le même Sheet.

────────────────────────────────────────────────────────────────────────
STRUCTURE ATTENDUE DE LA FEUILLE (les en-têtes, ligne 1, dans n'importe
quel ordre — le script les retrouve par leur nom) :

    Type d'appareil | Marque | Modèle | Prix | Lien | Statut | Date MAJ

Seules les 3 premières colonnes sont à remplir par toi. Les 4 dernières
sont écrites automatiquement par le script (créées si absentes).

────────────────────────────────────────────────────────────────────────
CONFIGURATION — à remplir ci-dessous avant de lancer :
"""

import os
import re
import sys
import time
import html
from datetime import datetime

import requests
import gspread
from google.oauth2.service_account import Credentials

# ══════════════════════════════════════════════════════════════════════
# CONFIGURATION — MODIFIE CES VALEURS
# ══════════════════════════════════════════════════════════════════════

# ID du Google Sheet (dans l'URL : docs.google.com/spreadsheets/d/CET_ID/edit)
# En local : remplace directement la chaîne ci-dessous.
# Sur GitHub Actions : laissé tel quel, la valeur vient du secret SHEET_ID.
SHEET_ID = os.environ.get("SHEET_ID", "1qzqx8HrIJf45F-U-hDuazYb9y54YSrnwgB1bNQkgLaM")

# Nom de l'onglet à traiter
WORKSHEET_NAME = "test1"

# Chemin vers le fichier JSON du compte de service (voir SETUP.md)
CREDENTIALS_FILE = "credentials.json"

# Ne retraiter que les lignes sans prix (False) ou tout recalculer (True)
FORCE_REFRESH_ALL = False

# Nombre MAXIMUM d'appareils traités PAR EXÉCUTION (par "shard" si plusieurs
# jobs tournent en parallèle avec des IP différentes — voir SHARD_INDEX/
# SHARD_COUNT plus bas, positionnés automatiquement par GitHub Actions).
BATCH_LIMIT = 5

# Pause entre chaque appareil traité (secondes) — volontairement généreuse
DELAY_SECONDS = 20.0

# ══════════════════════════════════════════════════════════════════════

HEADERS_HTTP = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "fr-FR,fr;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Referer": "https://www.electromenager-compare.com/",
}

# Session partagée (cookies conservés entre les requêtes, comme un navigateur)
SESSION = requests.Session()
_SESSION_INIT = False


def _init_session():
    global _SESSION_INIT
    if not _SESSION_INIT:
        SESSION.get(
            "https://www.electromenager-compare.com/", headers=HEADERS_HTTP, timeout=15
        )
        _SESSION_INIT = True


class RateLimitError(Exception):
    """Levée dès qu'un 403 est reçu — on arrête tout, on ne retente jamais
    dans la foulée. C'est au prochain run programmé (le lendemain, l'heure
    suivante...) de reprendre, jamais à ce process-ci d'insister."""
    pass


def _post_or_stop(url, **kwargs):
    r = SESSION.post(url, **kwargs)
    if r.status_code == 403:
        raise RateLimitError("403 reçu — arrêt immédiat, pas de retry")
    return r


def _get_or_stop(url, **kwargs):
    r = SESSION.get(url, **kwargs)
    if r.status_code == 403:
        raise RateLimitError("403 reçu — arrêt immédiat, pas de retry")
    return r

# Colonnes créées/gérées automatiquement par le script si absentes
AUTO_COLUMNS = ["Price Scrapping", "Lien", "Statut", "Date MAJ"]

PRICE_PATTERNS = [
    r"Dernier prix relev[ée]\s*[:\-]?\s*([\d\s]+[.,]\d{2})\s*€",
    r"[AÀ]\s*PARTIR\s*DE\s*([\d\s]+[.,]?\d*)\s*€",
    r"([\d]{2,4}[.,]\d{2})\s*€",
]

EXCLUDE_URL_PATTERNS = ("-liste-", "/recherche", "/marques", "/avis-")


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")


# ─── GOOGLE SHEETS ───────────────────────────────────────────────────

def get_worksheet():
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive.readonly",
    ]
    creds = Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=scopes)
    client = gspread.authorize(creds)
    sh = client.open_by_key(SHEET_ID)
    return sh.worksheet(WORKSHEET_NAME)


def ensure_columns(ws, header):
    """Ajoute les colonnes auto-gérées si elles n'existent pas encore."""
    changed = False
    for col_name in AUTO_COLUMNS:
        if col_name not in header:
            header.append(col_name)
            changed = True
    if changed:
        ws.update("A1", [header])
        log(f"Colonnes ajoutées à l'en-tête : {AUTO_COLUMNS}")
    return header


# ─── RECHERCHE DE L'URL PRODUIT (moteur de recherche interne du site) ──

PRODUCT_LINK_REGEX = (
    r'href="(https?://(?:www\.)?electromenager-compare\.com/'
    r'[a-z\-]+-[A-Za-z0-9]+-[A-Za-z0-9\-]+\.htm)"'
)


def _is_recherche_page(url):
    last_seg = url.rsplit("/", 1)[-1]
    return last_seg.startswith("recherche-")


def _slug_matches(url, modele):
    norm_model = re.sub(r"[\s\-]", "", modele).upper()
    norm_url = re.sub(r"[\s\-]", "", url).upper()
    return norm_model in norm_url


def _try_search(query):
    """Une tentative de recherche interne. Retourne (url, mode) ou (None, None)."""
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
        raise  # on ne l'attrape pas : ça doit remonter et tout arrêter
    except Exception as e:
        log(f"  ⚠ Erreur requête recherche interne: {e}")
        return None, None

    if r.status_code != 200:
        log(f"  ⚠ Recherche interne HTTP {r.status_code}")
        return None, None

    final_url = r.url
    if (
        final_url.endswith(".htm")
        and "electromenager-compare.com" in final_url
        and not _is_recherche_page(final_url)
        and not any(p in final_url for p in EXCLUDE_URL_PATTERNS)
    ):
        return final_url, "directe"

    page_html = html.unescape(r.text)
    candidates = re.findall(PRODUCT_LINK_REGEX, page_html)
    candidates = [
        c for c in candidates
        if not _is_recherche_page(c)
        and not any(p in c for p in EXCLUDE_URL_PATTERNS)
        and re.search(r"[A-Z]{2,}", c.rsplit("/", 1)[-1])
    ]
    if candidates:
        return candidates[0], "liste"

    return None, None


MARQUES_PLACEHOLDER = {"marque inconnue", "inconnue", "inconnu", "n/a", "na", ""}


def find_product_url(type_appareil, marque, modele):
    """
    Utilise le moteur de recherche interne d'electromenager-compare.com
    (leur propre barre de recherche) — pas de Google, pas de clé API.
    Vérifie que le modèle demandé apparaît vraiment dans l'URL retenue ;
    si le site est retombé sur une page de résultats générique (ex: à
    cause d'un suffixe régional comme "FR"), retente sans ce suffixe.
    Retourne (url, confiance) avec confiance = "haute" ou "approximative".
    """
    _init_session()
    marque_effective = "" if marque.strip().lower() in MARQUES_PLACEHOLDER else marque
    query = f"{marque_effective} {modele}".strip()
    url, mode = _try_search(query)

    if url and _slug_matches(url, modele):
        return url, "haute"

    stripped = re.sub(r"[A-Za-z]{1,3}$", "", modele)
    if stripped and stripped != modele:
        url2, mode2 = _try_search(f"{marque_effective} {stripped}".strip())
        if url2:
            confiance = "haute" if _slug_matches(url2, stripped) else "approximative"
            return url2, confiance

    if url:
        return url, "approximative"

    return None, None


# ─── SCRAPING DU PRIX SUR LA PAGE PRODUIT ───────────────────────────

def _parse_prix(raw):
    cleaned = raw.replace(" ", "").replace("\xa0", "").replace(",", ".")
    try:
        return round(float(cleaned), 2)
    except ValueError:
        return None


def fetch_price(url):
    try:
        r = _get_or_stop(url, headers=HEADERS_HTTP, timeout=15, allow_redirects=True)
    except RateLimitError:
        raise
    except Exception as e:
        return None, f"Erreur réseau: {e}"

    if r.status_code != 200:
        return None, f"HTTP {r.status_code}"

    page_html = html.unescape(r.text)  # &euro; -> €, &#039; -> ', etc.
    texte = re.sub(r"<[^>]+>", " ", page_html)
    texte = re.sub(r"\s+", " ", texte)

    for pattern in PRICE_PATTERNS:
        m = re.search(pattern, texte, re.IGNORECASE)
        if m:
            prix = _parse_prix(m.group(1))
            if prix:
                return prix, "OK"

    return None, "Prix introuvable sur la page"


# ─── TRAITEMENT PRINCIPAL ────────────────────────────────────────────

def process_all():
    ws = get_worksheet()
    all_values = ws.get_all_values()
    if not all_values:
        log("Feuille vide, rien à faire.")
        return

    header = all_values[0]
    header = ensure_columns(ws, header)
    # Recharger si l'en-tête a été modifié
    if header != all_values[0]:
        all_values = ws.get_all_values()
        header = all_values[0]

    idx = {name: i for i, name in enumerate(header)}

    required = ["Type appareil", "Marque", "Modèle"]
    for col in required:
        if col not in idx:
            log(f"✗ Colonne obligatoire manquante : '{col}'. Vérifie l'en-tête de la feuille.")
            sys.exit(1)

    # Sharding : quand ce script est lancé en parallèle (plusieurs jobs GitHub
    # Actions), chaque exécution a sa propre IP. SHARD_INDEX/SHARD_COUNT
    # permettent de répartir les lignes à traiter entre ces exécutions, sans
    # se marcher dessus : le job 0 prend les lignes 0, N, 2N... le job 1
    # prend 1, N+1, 2N+1... etc.
    SHARD_INDEX = int(os.environ.get("SHARD_INDEX", "0"))
    SHARD_COUNT = int(os.environ.get("SHARD_COUNT", "1"))

    rows = all_values[1:]
    updates = []  # liste de (row_number, {col_name: value})
    traites = 0
    arret_rate_limit = False
    candidat_index = 0  # compteur des lignes "à traiter", pour la répartition

    for i, row in enumerate(rows, start=2):  # ligne 2 = première donnée
        if traites >= BATCH_LIMIT:
            log(f"Limite de lot atteinte ({BATCH_LIMIT} appareils) — le reste sera traité "
                f"à la prochaine exécution programmée.")
            break

        def get(col):
            j = idx.get(col)
            return row[j].strip() if j is not None and j < len(row) else ""

        type_appareil = get("Type appareil")
        marque = get("Marque")
        modele = get("Modèle")

        if not marque or not modele:
            continue  # ligne incomplète, on saute

        prix_existant = get("Price Scrapping")
        if prix_existant and not FORCE_REFRESH_ALL:
            continue  # déjà traité

        # Cette ligne est un candidat à traiter : on décide si c'est à CE
        # job (shard) de s'en occuper
        mon_tour = (candidat_index % SHARD_COUNT == SHARD_INDEX)
        candidat_index += 1
        if not mon_tour:
            continue

        log(f"Ligne {i}: {type_appareil} {marque} {modele}")

        try:
            url = get("Lien")
            confiance = "haute"  # une URL déjà présente dans le Sheet est considérée fiable
            if not url:
                url, confiance = find_product_url(type_appareil, marque, modele)
                if not url:
                    log("  ✗ URL introuvable via la recherche")
                    updates.append((i, {"Statut": "URL introuvable", "Date MAJ": datetime.now().strftime("%d/%m/%Y %H:%M")}))
                    traites += 1
                    time.sleep(DELAY_SECONDS)
                    continue
                log(f"  → URL trouvée (confiance: {confiance}): {url}")

            prix, statut = fetch_price(url)
        except RateLimitError:
            log("  ⛔ 403 reçu — ARRÊT IMMÉDIAT du traitement (pas de retry).")
            log("     Le site sera probablement de nouveau accessible dans quelques heures.")
            log("     Les lignes restantes seront traitées à la prochaine exécution programmée.")
            arret_rate_limit = True
            break

        if confiance == "approximative":
            statut = f"{statut} — ⚠ modèle exact non confirmé, à vérifier"
        maj = datetime.now().strftime("%d/%m/%Y %H:%M")

        result = {"Lien": url, "Statut": statut, "Date MAJ": maj}
        if prix is not None:
            result["Price Scrapping"] = f"{prix:.2f}".replace(".", ",")
            log(f"  ✓ Prix: {result['Price Scrapping']} €")
        else:
            log(f"  ✗ {statut}")

        updates.append((i, result))
        traites += 1
        time.sleep(DELAY_SECONDS)

    if not updates:
        log("Aucune ligne à mettre à jour.")
        return

    log(f"Écriture de {len(updates)} ligne(s) dans le Sheet...")
    for row_number, values in updates:
        for col_name, val in values.items():
            col_letter = gspread.utils.rowcol_to_a1(row_number, idx[col_name] + 1)
            ws.update_acell(col_letter, val)
    log("Terminé." if not arret_rate_limit else "Terminé (arrêt anticipé pour cause de rate-limit).")


if __name__ == "__main__":
    process_all()
