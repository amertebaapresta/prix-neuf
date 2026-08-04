"""
sheet_fiche.py
==============
Même logique de recherche que sheet_prix.py +
extraction fiche technique depuis le HTML réel (iso-8859-1).

Structure HTML du site :
  Résumé : <li>Capacités de <b>9 kg</b> (lavage)</li>
  Tableau : <div class="product-tech-label">Label :</div>
            <div class="product-tech-text">Valeur</div>

Onglet TEST — colonnes :
  B=Type | C=Marque | D=Modèle | E=Prix neuf | F=Lien | G=Fiche technique | H=Dimension
"""

import os, re, sys, time, html, unicodedata
from datetime import datetime
from zoneinfo import ZoneInfo
import requests, gspread
from google.oauth2.service_account import Credentials

PARIS_TZ = ZoneInfo("Europe/Paris")
def now_paris(): return datetime.now(PARIS_TZ)

# ── Config ────────────────────────────────────────────────────────────────────
SHEET_ID         = os.environ.get("SHEET_ID_FICHE", "1MK6TiPQZUX4IwoYzfVB4Ofo1fFbUm_5qitsrpIjJps0")
WORKSHEET_NAME   = "TEST"
CREDENTIALS_FILE = "credentials.json"
FORCE_REFRESH_ALL = False
BATCH_LIMIT      = int(os.environ.get("BATCH_LIMIT",    "5"))
DELAY_SECONDS    = float(os.environ.get("DELAY_SECONDS", "20"))
SHARD_INDEX      = int(os.environ.get("SHARD_INDEX",    "0"))
SHARD_COUNT      = int(os.environ.get("SHARD_COUNT",    "1"))

# Noms exacts des colonnes dans la ligne 1 du sheet
COL_TYPE  = "Type"
COL_MARQUE= "Marque"
COL_MODELE= "Modèle"
COL_PRIX  = "Prix neuf"
COL_LIEN  = "Lien"
COL_FICHE = "Fiche technique "
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

def log(msg): print(f"[{now_paris().strftime('%H:%M:%S')}] {msg}", flush=True)

# ── Logique recherche URL (identique sheet_prix.py) ───────────────────────────
PRODUCT_LINK_REGEX = (
    r'href="(https?://(?:www\.)?electromenager-compare\.com/'
    r'[a-z\-]+-[A-Za-z0-9]+-[A-Za-z0-9\-]+\.htm)"'
)
EXCLUDE_URL_PATTERNS = ("-liste-", "/recherche", "/marques", "/avis-")

def _is_recherche_page(url):
    return url.rsplit("/", 1)[-1].startswith("recherche-")

def _slug_matches(url, modele):
    return re.sub(r"[\s\-]","",modele).upper() in re.sub(r"[\s\-]","",url).upper()

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

# ── Extraction prix + fiche depuis HTML réel ──────────────────────────────────
PRICE_PATTERNS = [
    r"Dernier prix relev[ée]\s*[:\-]?\s*([\d\s]+[.,]\d{2})\s*€",
    r"[AÀ]\s*PARTIR\s*DE\s*([\d\s]+[.,]?\d*)\s*€",
    r"([\d]{2,4}[.,]\d{2})\s*€",
]

def _tech(page, label_pattern):
    """
    Extrait la valeur depuis :
    <div class="product-tech-label">Label :</div>
    <div class="...product-tech-text">VALEUR</div>
    Décode automatiquement les entités HTML (&Agrave; → À, etc.)
    """
    pat = (
        label_pattern +
        r'\s*:\s*\n</div>\s*<div[^>]*product-tech-text[^>]*>\s*\n?'
        r'(.*?)</div>'
    )
    m = re.search(pat, page, re.IGNORECASE | re.DOTALL)
    if m:
        val = re.sub(r'<[^>]+>', '', m.group(1)).strip()
        val = html.unescape(val)  # &Agrave; → À, &eacute; → é, etc.
        return val if val else None
    return None

def parse_page(page, type_appareil):
    """
    Extrait prix, fiche technique et dimensions depuis le HTML brut iso-8859-1.
    
    Deux zones utilisées :
      1. Section résumé <li> : capacité, essorage, niveau sonore, classe énergie
      2. Tableau product-tech : dimensions, poids, et specs détaillées
    """
    tn    = _normaliser(type_appareil or "")
    lines = []

    # ── Prix ──────────────────────────────────────────────────────────────────
    # Décoder les entités HTML (&euro; → €, &nbsp; → espace, etc.)
    page_decoded = html.unescape(page)
    texte_plat = re.sub(r'<[^>]+>', ' ', page_decoded)
    texte_plat = re.sub(r'\s+', ' ', texte_plat)
    prix = None
    for pat in PRICE_PATTERNS:
        m = re.search(pat, texte_plat, re.IGNORECASE)
        if m:
            raw = m.group(1).replace(" ","").replace("\xa0","").replace(".",",")
            prix = raw + " €"
            break

    # ── Poids (tableau product-tech) ──────────────────────────────────────────
    poids = _tech(page, r'Poids d[eé]ball[eé]')
    if poids:
        # Nettoyer : "71.01 kg" → garder tel quel
        m = re.search(r'([0-9]+[.,]?[0-9]*)\s*kg', poids, re.IGNORECASE)
        if m: lines.append(f"Poids : {m.group(1)} kg")

    # ── Champs selon type ─────────────────────────────────────────────────────
    if "lave-linge" in tn or "lave linge" in tn:
        # Capacité dans le résumé <li>
        m = re.search(r'Capacit[eé]s?\s+de\s+<b>([0-9]+)\s*kg</b>', page, re.IGNORECASE)
        if m: lines.append(f"Capacité : {m.group(1)} kg")

        # Essorage dans le résumé <li>
        m = re.search(r'essorage max de <b>([0-9]+)\s*tr/min</b>', page, re.IGNORECASE)
        if not m:
            # Fallback tableau product-tech
            val = _tech(page, r"Vitesse d'essorage")
            if val:
                m2 = re.search(r'([0-9]+)\s*tr/min', val)
                if m2: lines.append(f"Essorage : {m2.group(1)} tr/min")
        else:
            lines.append(f"Essorage : {m.group(1)} tr/min")

        _classe(page, lines)

        # Niveau sonore dans le résumé <li> OU dans le tableau
        m = re.search(r'Niveau sonore max\s*:\s*<b>([0-9]+)\s*dB</b>', page, re.IGNORECASE)
        if not m:
            val = _tech(page, r'Niveau sonore')
            if val:
                m2 = re.search(r'([0-9]+)\s*dB', val)
                if m2: lines.append(f"Niveau sonore : {m2.group(1)} dB")
        else:
            lines.append(f"Niveau sonore : {m.group(1)} dB")

    elif "lave-vaisselle" in tn or "lave vaisselle" in tn:
        # Capacité : chercher X couverts
        m = re.search(r'<b>([0-9]+)\s*couvert', page, re.IGNORECASE)
        if not m: m = re.search(r'([0-9]+)\s*couverts?', page, re.IGNORECASE)
        if m: lines.append(f"Capacité : {m.group(1)} couverts")

        _classe(page, lines)

        m = re.search(r'Niveau sonore max\s*:\s*<b>([0-9]+)\s*dB</b>', page, re.IGNORECASE)
        if not m: m = re.search(r'<b>([0-9]+)\s*dB</b>', page, re.IGNORECASE)
        if m: lines.append(f"Niveau sonore : {m.group(1)} dB")

        val = _tech(page, r'Consommation d.eau')
        if val:
            m2 = re.search(r'([0-9]+[.,]?[0-9]*)\s*[lL]', val)
            if m2: lines.append(f"Consommation eau : {m2.group(1)} L/cycle")

    elif any(x in tn for x in ["seche-linge","sèche-linge","seche linge","sèche linge"]):
        # Type de séchage dans le tableau
        val = _tech(page, r'Type de s[eè]che-linge')
        if not val: val = _tech(page, r'Type de s[eè]chage')
        if val:
            lines.append(f"Type de séchage : {val}")
        else:
            for mot, label in [("pompe","Pompe à chaleur"),("condensation","Condensation"),("vacuation","Évacuation")]:
                if mot in page.lower():
                    lines.append(f"Type de séchage : {label}")
                    break

        # Capacité
        m = re.search(r'Capacit[eé]s?\s+de\s+<b>([0-9]+)\s*kg</b>', page, re.IGNORECASE)
        if not m:
            val = _tech(page, r'Capacit[eé]')
            if val:
                m2 = re.search(r'([0-9]+)\s*kg', val)
                if m2: lines.append(f"Capacité : {m2.group(1)} kg")
        else:
            lines.append(f"Capacité : {m.group(1)} kg")

        _classe(page, lines)

        m = re.search(r'Niveau sonore max\s*:\s*<b>([0-9]+)\s*dB</b>', page, re.IGNORECASE)
        if not m: m = re.search(r'<b>([0-9]+)\s*dB</b>', page, re.IGNORECASE)
        if m: lines.append(f"Niveau sonore : {m.group(1)} dB")

    elif "four" in tn or "cuisini" in tn or "micro" in tn:
        val = _tech(page, r'Type de cuisson')
        if not val: val = _tech(page, r'Fonctionnement')
        if val:
            lines.append(f"Type de cuisson : {val}")
        else:
            for mot, label in [("pyrolyse","Pyrolyse"),("chaleur tournante","Chaleur tournante"),("gaz","Gaz"),("lectrique","Électrique")]:
                if mot in page.lower():
                    lines.append(f"Type de cuisson : {label}")
                    break

        val = _tech(page, r'Volume')
        if not val: val = _tech(page, r'Capacit[eé]')
        if val:
            m2 = re.search(r'([0-9]+)\s*[Ll]', val)
            if m2: lines.append(f"Capacité : {m2.group(1)} L")

        _classe(page, lines)

    elif any(x in tn for x in ["réfrigérateur","refrigerateur","frigo"]):
        val = _tech(page, r'Type de froid')
        if val:
            lines.append(f"Type de froid : {val}")
        else:
            for mot, label in [("no frost","No Frost"),("ventil","Froid ventilé"),("statique","Froid statique")]:
                if mot in page.lower():
                    lines.append(f"Type de froid : {label}")
                    break

        val = _tech(page, r'Capacit[eé] totale')
        if not val: val = _tech(page, r'Capacit[eé]')
        if val:
            m2 = re.search(r'([0-9]+)\s*[Ll]', val)
            if m2: lines.append(f"Capacité totale : {m2.group(1)} L")

        _classe(page, lines)

        m = re.search(r'Niveau sonore max\s*:\s*<b>([0-9]+)\s*dB</b>', page, re.IGNORECASE)
        if not m: m = re.search(r'<b>([0-9]+)\s*dB</b>', page, re.IGNORECASE)
        if m: lines.append(f"Niveau sonore : {m.group(1)} dB")

    elif any(x in tn for x in ["congélateur","congelateur"]):
        val = _tech(page, r'Capacit[eé]')
        if val:
            m2 = re.search(r'([0-9]+)\s*[Ll]', val)
            if m2: lines.append(f"Capacité : {m2.group(1)} L")
        _classe(page, lines)

    else:
        _classe(page, lines)

    # ── Dimensions (tableau product-tech) ─────────────────────────────────────
    dims = "Non trouvé"
    val  = _tech(page, r'Dimensions d[eé]ball[eé]')
    if val:
        m = re.search(r'([0-9]+)\s*x\s*([0-9]+)\s*x\s*([0-9]+)\s*mm\s*\(HxLxP\)', val, re.IGNORECASE)
        if m:
            h, l, p = int(m.group(1)), int(m.group(2)), int(m.group(3))
            dims = f"{l/10:.1f}x{h/10:.1f}x{p/10:.1f} cm"
        else:
            dims = val  # garder tel quel si format différent

    fiche = "\n".join(lines) if lines else "Non trouvé"
    return prix, fiche, dims


def _classe(page, lines):
    """
    Extrait la classe énergie.
    Cherche dans : résumé <li>, balises <b>, et tableau product-tech.
    Distingue ancienne (A+++/A++/A+) et nouvelle (A à G sans +).
    """
    # 1. Résumé <li> : <li>Classe énergie : <b>A++</b></li>
    m = re.search(r'Classe [eé]nergie\s*:\s*<b>([A-G][+]*)</b>', page, re.IGNORECASE)
    if m:
        val = m.group(1)
        label = "Ancienne" if '+' in val else "Nouvelle"
        lines.append(f"{label} classe énergétique : {val}")
        return

    # 2. Tableau product-tech (Label + valeur dans div suivante)
    val = _tech(page, r'Classe [eé]nergie')
    if val:
        # Nettoyer : "A++ (Lavage)" → "A++"
        m2 = re.search(r'([A-G][+]*)', val)
        if m2:
            v = m2.group(1)
            label = "Ancienne" if '+' in v else "Nouvelle"
            lines.append(f"{label} classe énergétique : {v}")
            return

    # 3. Chercher n'importe quelle mention de classe dans toute la page
    # Pattern large : "classe" suivi de la lettre dans des balises
    for pat in [
        r'[Cc]lasse\s*[eé]nerg[^<]*<[^>]+>\s*([A-G][+]+)',   # ancienne avec +
        r'[Cc]lasse\s*[eé]nerg[^<]*<[^>]+>\s*([A-G])\s*<',   # nouvelle sans +
        r'[Cc]lasse\s*:\s*<b>([A-G][+]*)</b>',
        r'Indice\s+d\'efficacit[eé]\s*:\s*([A-G])',
    ]:
        m3 = re.search(pat, page, re.IGNORECASE)
        if m3:
            v = m3.group(1)
            label = "Ancienne" if '+' in v else "Nouvelle"
            lines.append(f"{label} classe énergétique : {v}")
            return


# ── Google Sheets ─────────────────────────────────────────────────────────────
def get_worksheet():
    scopes = ["https://www.googleapis.com/auth/spreadsheets","https://www.googleapis.com/auth/drive.readonly"]
    creds  = Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=scopes)
    return gspread.authorize(creds).open_by_key(SHEET_ID).worksheet(WORKSHEET_NAME)


# ── Boucle principale ─────────────────────────────────────────────────────────
def process_all():
    ws         = get_worksheet()
    all_values = ws.get_all_values()
    if not all_values: log("Feuille vide."); return

    header = all_values[0]
    idx    = {name: i for i, name in enumerate(header)}

    for col in [COL_TYPE, COL_MARQUE, COL_MODELE, COL_LIEN, COL_FICHE]:
        if col not in idx:
            log(f"✗ Colonne manquante : '{col}' | En-têtes trouvés : {header}")
            sys.exit(1)

    traites = 0; candidat_index = 0; arret = False

    for i, row in enumerate(all_values[1:], start=2):
        if traites >= BATCH_LIMIT: log(f"Limite atteinte ({BATCH_LIMIT})."); break

        def get(col):
            j = idx.get(col)
            return row[j].strip() if j is not None and j < len(row) else ""

        type_app = get(COL_TYPE); marque = get(COL_MARQUE); modele = get(COL_MODELE)
        if not marque and not modele: continue

        # Sauter si fiche déjà remplie et non vide
        fiche_exist = get(COL_FICHE)
        if fiche_exist and fiche_exist not in ('Non trouvé','Erreur') and not FORCE_REFRESH_ALL: continue

        mon_tour = (candidat_index % SHARD_COUNT == SHARD_INDEX)
        candidat_index += 1
        if not mon_tour: continue

        log(f"Ligne {i} : {type_app} | {marque} | {modele}")

        try:
            mc  = _nettoyer_modele(marque, modele)
            url = get(COL_LIEN)
            confiance = None

            if url and url != "Non trouvé":
                if _slug_matches(url, mc) or _slug_matches(url, modele):
                    confiance = "haute"
                else:
                    log(f"  ⚠ Lien invalide — re-cherche"); url = None

            if not url:
                url, confiance = find_product_url(type_app, marque, modele)

            if not url:
                log("  ✗ URL introuvable")
                _write(ws, idx, i, {COL_PRIX:"Non trouvé",COL_LIEN:"Non trouvé",COL_FICHE:"Non trouvé",COL_DIM:"Non trouvé"})
                traites += 1; time.sleep(DELAY_SECONDS); continue

            if confiance == "approximative":
                log("  ⚠ Confiance approximative — Non trouvé")
                _write(ws, idx, i, {COL_PRIX:"Non trouvé",COL_LIEN:"Non trouvé",COL_FICHE:"Non trouvé",COL_DIM:"Non trouvé"})
                traites += 1; time.sleep(DELAY_SECONDS); continue

            log(f"  → URL : {url}")

            # Récupérer la page UNE SEULE FOIS → prix + fiche + dims
            time.sleep(2)
            r    = _get_or_stop(url, headers=HEADERS_HTTP, timeout=15, allow_redirects=True)
            page = _decode(r)
            prix, fiche, dims = parse_page(page, type_app)

            prix = prix or "Non trouvé"
            log(f"  → Prix  : {prix}")
            log(f"  → Fiche :\n{fiche}")
            log(f"  → Dims  : {dims}")

            _write(ws, idx, i, {COL_PRIX:prix, COL_LIEN:url, COL_FICHE:fiche, COL_DIM:dims})
            traites += 1
            log(f"  ✓ Ligne {i} OK")

        except RateLimitError:
            log("  ⛔ 403 — ARRÊT IMMÉDIAT."); arret = True; break
        except Exception as e:
            log(f"  ✗ Erreur : {e}")
            import traceback; traceback.print_exc()
            _write(ws, idx, i, {COL_PRIX:"Erreur", COL_FICHE:f"Erreur:{e}"})
            traites += 1

        time.sleep(DELAY_SECONDS)

    log(f"Terminé — {traites} lignes." + (" (rate-limit)" if arret else ""))


def _write(ws, idx, row_num, values):
    for col_name, val in values.items():
        if col_name not in idx: continue
        col_letter = gspread.utils.rowcol_to_a1(row_num, idx[col_name]+1)
        ws.update_acell(col_letter, val)


if __name__ == "__main__":
    process_all()
