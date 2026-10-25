#!/usr/bin/env python3
"""
Hexagone Flux — préparation des prévisions AROME + ARPEGE (Météo-France, API officielles).

Pourquoi ce script : les API AROME/ARPEGE donnent des GRILLES (un paramètre, une échéance par appel),
pas une prévision « pour un point ». On les télécharge ici, hors du site, on les ramène sur une grille
de 0,05° (≈ 5 km) découpée en tuiles de 1° et on les dépose dans Cloudflare KV. La Function /api/meteo
lit ensuite une seule tuile KV : plus aucun appel Météo-France, ni coordonnée de visiteur envoyée, à chaque visite.

Échéances (heure UTC absolue) :
  AROME  : +0 à +24 h toutes les heures, puis +27 à +48 h toutes les 3 h
  ARPEGE : toutes les 3 h, de la fin d'AROME jusqu'à +96 h de son propre run

Secrets / variables d'environnement :
  MF_KEY_AROME, MF_KEY_ARPEGE              clés API du portail, une par API (en-tête « apikey »)
  MF_API_KEY                               (optionnel) clé unique de repli si le portail en donne une pour toutes les API
  CF_ACCOUNT_ID, CF_KV_NAMESPACE_ID, CF_API_TOKEN   (jeton Cloudflare « Workers KV Storage : Edit »)
  HEURES_RUN                               (optionnel) heures UTC des runs publiés, défaut « 0,6,12,18 »
                                           → 4 publications/jour × 177 écritures = 708 < 1 000 écritures/jour (limite KV gratuite)
  AROME_SERVICE, ARPEGE_SERVICE            (optionnel) identifiant du service WCS si le nom change

Modes :
  python previ.py --discover               liste les couvertures disponibles (à lancer en premier)
  python previ.py --out dossier            écrit les tuiles en local au lieu de KV (test)
  python previ.py --selftest               test complet hors-ligne avec de fausses données
  python previ.py                          exécution normale (s'arrête vite s'il n'y a pas de nouveau run)
"""
import argparse, base64, io, json, os, re, struct, sys, time
from datetime import datetime, timedelta, timezone

import numpy as np
import requests
import tifffile

BASE = "https://public-api.meteofrance.fr/public"
MODELES = {
    "arome": {"chemin": "arome/1.0", "service": os.environ.get("AROME_SERVICE", "MF-NWP-HIGHRES-AROME-0025-FRANCE-WCS"), "periode": "PT1H"},
    "arpege": {"chemin": "arpege/1.0", "service": os.environ.get("ARPEGE_SERVICE", "MF-NWP-GLOBAL-ARPEGE-005-EURAT-WCS"), "periode": "PT3H"},
}

# Grille de sortie : nœuds tous les 0,05° ; tuiles de 1° × 1° (20 × 20 points)
STEP, N = 0.05, 20
LAT_MIN, LAT_MAX, LON_MIN, LON_MAX = 41, 52, -6, 10
LATS = LAT_MIN + STEP * np.arange((LAT_MAX - LAT_MIN) * N)
LONS = LON_MIN + STEP * np.arange((LON_MAX - LON_MIN) * N)
BBOX = (40.9, 52.1, -6.1, 10.1)  # lat min/max, lon min/max (avec marge)

# clé → préfixes de couverture possibles (le premier présent est retenu), hauteur, décimales
PARAMS = {
    "t":    {"pref": ["TEMPERATURE__SPECIFIC_HEIGHT_LEVEL_ABOVE_GROUND"], "h": 2, "dec": 1},
    "rh":   {"pref": ["RELATIVE_HUMIDITY__SPECIFIC_HEIGHT_LEVEL_ABOVE_GROUND"], "h": 2, "dec": 0},
    "ws":   {"pref": ["WIND_SPEED__SPECIFIC_HEIGHT_LEVEL_ABOVE_GROUND"], "h": 10, "dec": 0},
    "wd":   {"pref": ["WIND_DIRECTION__SPECIFIC_HEIGHT_LEVEL_ABOVE_GROUND"], "h": 10, "dec": 0},
    "gust": {"pref": ["WIND_SPEED_GUST__SPECIFIC_HEIGHT_LEVEL_ABOVE_GROUND"], "h": 10, "dec": 0},
    "rain": {"pref": ["TOTAL_PRECIPITATION__GROUND_OR_WATER_SURFACE"], "h": None, "dec": 2, "cumul": True},
    "cc":   {"pref": ["TOTAL_CLOUD_COVER__GROUND_OR_WATER_SURFACE"], "h": None, "dec": 0},
    "p":    {"pref": ["PRESSURE__MEAN_SEA_LEVEL"], "h": None, "dec": 1},
}
MANQUANT = -32768  # valeur « absente » dans les tuiles binaires (entiers 16 bits)
HEURES_RUN = {int(h) for h in os.environ.get("HEURES_RUN", "0,6,12,18").split(",")}
RE_ID = re.compile(r"^(.+?)___(\d{4}-\d\d-\d\dT\d\d\.\d\d\.\d\dZ)(?:_(P\w+))?$")
def cle_api(url):
    """Clé du portail pour l'API appelée (une clé par API ; MF_API_KEY en repli)."""
    if "/arome/" in url:
        return os.environ.get("MF_KEY_AROME") or os.environ.get("MF_API_KEY", "")
    if "/arpege/" in url:
        return os.environ.get("MF_KEY_ARPEGE") or os.environ.get("MF_API_KEY", "")
    return os.environ.get("MF_API_KEY", "")


FAUX = False
_dernier = [0.0]


# ── réseau ────────────────────────────────────────────────────────────
def get(url, params):
    """GET avec limite ≈ 80 req/min, reprise sur 429. None si 400/404 (échéance absente)."""
    if FAUX:
        return faux_get(url, params)
    for essai in range(4):
        attente = 0.75 - (time.time() - _dernier[0])
        if attente > 0:
            time.sleep(attente)
        _dernier[0] = time.time()
        r = requests.get(url, params=params, headers={"apikey": cle_api(url), "accept": "*/*"}, timeout=180)
        if r.status_code == 429:
            time.sleep(20 * (essai + 1))
            continue
        if r.status_code in (400, 404):
            return None
        r.raise_for_status()
        return r.content
    raise RuntimeError("trop de réponses 429 : " + url)


def url_wcs(m, action):
    d = MODELES[m]
    return f"{BASE}/{d['chemin']}/wcs/{d['service']}/{action}"


def couvertures(m):
    xml = get(url_wcs(m, "GetCapabilities"), [("service", "WCS"), ("version", "2.0.1"), ("language", "eng")])
    if not xml:
        raise RuntimeError(f"GetCapabilities vide pour {m} : service {MODELES[m]['service']} incorrect ?")
    out = []
    for cid in re.findall(r"CoverageId>([^<]+)<", xml.decode("utf-8", "replace")):
        mo = RE_ID.match(cid.strip())
        if mo:
            out.append((mo.group(1), mo.group(2), mo.group(3), cid.strip()))
    return out


def iso_ref(ref):  # 2026-10-25T09.00.00Z → datetime
    return datetime.strptime(ref, "%Y-%m-%dT%H.%M.%SZ").replace(tzinfo=timezone.utc)


def derniere_ref(couv):
    # on ne publie que les runs des heures HEURES_RUN (quota d'écritures KV gratuit : 1 000/jour)
    refs = sorted({r for n, r, _, _ in couv if n in PARAMS["t"]["pref"] and iso_ref(r).hour in HEURES_RUN})
    return refs[-1] if refs else None


def choisir(couv, cle, ref, modele):
    """Retourne l'identifiant de couverture à utiliser pour (cle, ref), ou None."""
    p = PARAMS[cle]
    for pref in p["pref"]:
        c = [x for x in couv if x[0] == pref and x[1] == ref]
        if not c:
            continue
        if p.get("cumul"):
            voulue = MODELES[modele]["periode"]
            c = [x for x in c if x[2] == voulue]
            if not c:
                return None  # période inconnue : on n'invente pas, la pluie restera vide
        return c[0][3]
    return None


# ── lecture d'une grille GeoTIFF ──────────────────────────────────────
def lire_tiff(octets):
    with tifffile.TiffFile(io.BytesIO(octets)) as tf:
        page = tf.pages[0]
        a = page.asarray().astype("float64")
        if a.ndim == 3:
            a = a[..., 0] if a.shape[-1] <= 4 else a[0]
        sc, tp = page.tags.get(33550), page.tags.get(33922)
        if sc is None or tp is None:
            raise ValueError("GeoTIFF sans géoréférencement (tags 33550/33922)")
        sx, sy = float(sc.value[0]), float(sc.value[1])
        x0, y0 = float(tp.value[3]), float(tp.value[4])
        nd = page.tags.get(42113)
        if nd is not None:
            try:
                a[a == float(str(nd.value).strip("\x00"))] = np.nan
            except ValueError:
                pass
    a[np.abs(a) > 1e15] = np.nan
    return a, x0, y0, sx, sy


def echantillonner(a, x0, y0, sx, sy):
    h, w = a.shape
    ii = np.rint((y0 - LATS) / sy).astype(int)
    jj = np.rint((LONS - x0) / sx).astype(int)
    sub = a[np.clip(ii, 0, h - 1)][:, np.clip(jj, 0, w - 1)].astype("float32")
    sub[(ii < 0) | (ii >= h), :] = np.nan
    sub[:, (jj < 0) | (jj >= w)] = np.nan
    return sub


def grille(modele, cid, cle, valide):
    p = PARAMS[cle]
    params = [("service", "WCS"), ("version", "2.0.1"), ("coverageid", cid),
              ("subset", f"time({valide.strftime('%Y-%m-%dT%H:%M:%SZ')})"),
              ("subset", f"lat({BBOX[0]},{BBOX[1]})"), ("subset", f"long({BBOX[2]},{BBOX[3]})"),
              ("format", "image/tiff")]
    if p["h"] is not None:
        params.insert(4, ("subset", f"height({p['h']})"))
    brut = get(url_wcs(modele, "GetCoverage"), params)
    if brut is None:
        return None
    g = echantillonner(*lire_tiff(brut))
    if p.get("cumul"):
        g = g / (3.0 if MODELES[modele]["periode"] == "PT3H" else 1.0)  # mm sur la période → mm/h
    return g


def convertir(cle, g):
    if cle == "t" and np.nanmean(g) > 150:
        g = g - 273.15
    if cle == "p" and np.nanmean(g) > 2000:
        g = g / 100.0
    if cle in ("ws", "gust"):
        g = g * 3.6
    return g


# ── échéances ─────────────────────────────────────────────────────────
def echeances_arome(ref_a):
    d = iso_ref(ref_a)
    return [d + timedelta(hours=h) for h in list(range(0, 25)) + list(range(27, 49, 3))]


def echeances_arpege(ref_p, apres):
    """Échéances ARPEGE (toutes les 3 h, jusqu'à +96 h) postérieures à `apres` (dernière échéance AROME obtenue)."""
    d = iso_ref(ref_p)
    out = []
    for h in range(0, 97, 3):
        t = d + timedelta(hours=h)
        if apres is None or t > apres:
            out.append(t)
    return out


# ── tuiles ────────────────────────────────────────────────────────────
# Format binaire (lu par la Function sans JSON.parse, pour rester sous la limite de CPU gratuite) :
#   "HFP1" | uint32 LE = taille de l'en-tête | en-tête JSON (UTF-8) | Int16 LE [paramètre][échéance][point 20×20]
#   valeur réelle = entier / 10^décimales ; -32768 = absent
def cle_tuile(la0, lo0):
    return f"t_{la0}_{'m' + str(-lo0) if lo0 < 0 else lo0}"


def construire(series, temps, refs):
    cles = [c for c in PARAMS if c in series]
    cube = {c: np.stack([series[c].get(t, np.full((len(LATS), len(LONS)), np.nan, "float32")) for t in temps]) for c in cles}
    gen = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
    for la0 in range(LAT_MIN, LAT_MAX):
        for lo0 in range(LON_MIN, LON_MAX):
            i0, j0 = (la0 - LAT_MIN) * N, (lo0 - LON_MIN) * N
            blocs = []
            for c in cles:
                blk = cube[c][:, i0:i0 + N, j0:j0 + N].reshape(len(temps), N * N).astype("float64")
                x = np.clip(np.rint(np.nan_to_num(blk) * 10 ** PARAMS[c]["dec"]), -32767, 32767)
                x[np.isnan(blk)] = MANQUANT
                blocs.append(x.astype("<i2"))
            entete = json.dumps({"v": 2, "step": STEP, "n": N, "lat0": la0, "lon0": lo0, "gen": gen, "ref": refs,
                                 "times": [t.strftime("%Y-%m-%dT%H:%MZ") for t in temps], "params": cles,
                                 "dec": {c: PARAMS[c]["dec"] for c in cles}}, separators=(",", ":")).encode()
            yield cle_tuile(la0, lo0), b"HFP1" + struct.pack("<I", len(entete)) + entete + np.stack(blocs).tobytes()


# ── Cloudflare KV (API REST) / disque ─────────────────────────────────
CF = "https://api.cloudflare.com/client/v4"


def kv_url(suite):
    return f"{CF}/accounts/{os.environ['CF_ACCOUNT_ID']}/storage/kv/namespaces/{os.environ['CF_KV_NAMESPACE_ID']}/{suite}"


def kv_entetes():
    return {"Authorization": "Bearer " + os.environ["CF_API_TOKEN"]}


def lire_meta(out):
    try:
        if out:
            return json.load(open(os.path.join(out, "kv", "meta")))
        r = requests.get(kv_url("values/meta"), headers=kv_entetes(), timeout=60)
        return r.json() if r.status_code == 200 else {}
    except Exception:
        return {}


def ecrire_lot(out, paires):
    """paires : [(clé, octets)]. Une écriture KV par clé (quota gratuit : 1 000 / jour)."""
    if out:
        for k, v in paires:
            chemin = os.path.join(out, "kv", k)
            os.makedirs(os.path.dirname(chemin), exist_ok=True)
            open(chemin, "wb").write(v)
        return
    for i in range(0, len(paires), 40):
        corps = [{"key": k, "value": base64.b64encode(v).decode(), "base64": True} for k, v in paires[i:i + 40]]
        r = requests.put(kv_url("bulk"), headers={**kv_entetes(), "Content-Type": "application/json"}, data=json.dumps(corps), timeout=300)
        if r.status_code != 200 or not r.json().get("success", False):
            raise RuntimeError(f"écriture KV refusée ({r.status_code}) : {r.text[:300]}")


# ── programme principal ───────────────────────────────────────────────
def principal(args):
    if not FAUX:
        for m in MODELES:
            if not cle_api(url_wcs(m, "")):
                sys.exit(f"Clé API manquante pour {m} : définir MF_KEY_{m.upper()} (ou MF_API_KEY)")
    couv = {m: couvertures(m) for m in MODELES}
    if args.discover:
        for m, c in couv.items():
            ref = derniere_ref(c)
            print(f"\n== {m} ({MODELES[m]['service']}) — dernier run : {ref} — {len(c)} couvertures")
            for n in sorted({(n, p) for n, r, p, _ in c if r == ref}):
                print("  ", n[0], n[1] or "")
        return 0
    refs = {m: derniere_ref(couv[m]) for m in MODELES}
    if not refs["arome"] and not refs["arpege"]:
        print("Aucun run aux heures publiées (HEURES_RUN) : rien à faire.")
        return 0
    # ARPEGE sort ~1 h après AROME : on attend les deux du même cycle (sinon 2 publications = quota KV dépassé),
    # sauf si le run AROME a plus de 6 h.
    if refs["arome"] and refs["arpege"] and not args.force:
        ra, rp = iso_ref(refs["arome"]), iso_ref(refs["arpege"])
        if rp < ra and datetime.now(timezone.utc) - ra < timedelta(hours=6):
            print("ARPEGE du même cycle pas encore disponible : on attend le prochain passage.", refs)
            return 0
    meta = lire_meta(args.out)
    if meta.get("ref") == refs and not args.force:
        print("Aucun nouveau run, rien à faire.", refs)
        return 0
    print("Runs retenus :", refs)

    series = {c: {} for c in PARAMS}
    temps_ok = set()

    def recuperer(modele, valide):
        ok = False
        for cle in PARAMS:
            cid = choisir(couv[modele], cle, refs[modele], modele)
            if not cid:
                continue
            g = grille(modele, cid, cle, valide)
            if g is None:
                continue
            series[cle][valide] = convertir(cle, g)
            if cle == "t":
                temps_ok.add(valide)
                ok = True
        print(f"  {modele} {valide:%d/%m %H}h {'ok' if ok else 'absent'}")
        return ok

    derniere_arome = None
    if refs["arome"]:
        for valide in echeances_arome(refs["arome"]):
            if recuperer("arome", valide):
                derniere_arome = valide
    if refs["arpege"]:
        for valide in echeances_arpege(refs["arpege"], derniere_arome):
            recuperer("arpege", valide)

    if len(temps_ok) < 10:
        print("Trop peu d'échéances récupérées : abandon, rien n'est publié.")
        return 1
    series = {c: s_ for c, s_ in series.items() if s_}
    temps = sorted(temps_ok)
    paires = list(construire(series, temps, refs))
    ecrire_lot(args.out, paires)
    meta = {"ref": refs, "gen": datetime.now(timezone.utc).isoformat(), "echeances": len(temps), "params": sorted(series)}
    ecrire_lot(args.out, [("meta", json.dumps(meta).encode())])  # écrit en dernier : le run n'est « vu » qu'une fois les tuiles en place
    print(f"{len(paires)} tuiles publiées, {len(temps)} échéances, paramètres : {sorted(series)}")
    return 0


# ── faux serveur pour --selftest ──────────────────────────────────────
def faux_get(url, params):
    if "GetCapabilities" in url:
        ref = (datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0) - timedelta(hours=3)).strftime("%Y-%m-%dT%H.00.00Z")
        ids = []
        for p in PARAMS.values():
            for pref in p["pref"]:
                ids += [f"{pref}___{ref}"] if not p.get("cumul") else [f"{pref}___{ref}_PT1H", f"{pref}___{ref}_PT3H"]
        return ("<c>" + "".join(f"<wcs:CoverageId>{i}</wcs:CoverageId>" for i in ids) + "</c>").encode()
    d = dict(params)
    cid = d["coverageid"]
    lat = np.linspace(52.1, 40.9, 448)[:, None]
    lon = np.linspace(-6.1, 10.1, 648)[None, :]
    base = {"TEMPERATURE": 285 + (50 - lat) * 0.8 + 0 * lon, "RELATIVE_HUMIDITY": 70 + 0 * lat + 0 * lon,
            "WIND_SPEED_GUST": 12 + 0 * lat + 0 * lon, "WIND_SPEED": 5 + 0 * lat + 0 * lon, "WIND_DIRECTION": 200 + 0 * lat + 0 * lon,
            "TOTAL_PRECIPITATION": np.where(lon > 5, 1.5, 0.0) + 0 * lat, "TOTAL_CLOUD_COVER": 40 + 0 * lat + 0 * lon,
            "PRESSURE": 101300 + 0 * lat + 0 * lon}
    cle = next(k for k in base if cid.startswith(k))
    buf = io.BytesIO()
    tifffile.imwrite(buf, base[cle].astype("float32"), extratags=[(33550, "d", 3, (0.025, 0.025, 0.0), True), (33922, "d", 6, (0, 0, 0, -6.1, 52.1, 0.0), True)])
    return buf.getvalue()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--discover", action="store_true")
    ap.add_argument("--out")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        FAUX = True
        HEURES_RUN.update(range(24))
        a.out, a.force = a.out or "/tmp/previ_selftest", True
        _dernier[0] = 1e18  # pas d'attente en test
        time.sleep = lambda s: None
    sys.exit(principal(a))
