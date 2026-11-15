# -*- coding: utf-8 -*-
"""
Métadonnées des sociétés pour la fiche du site (gratuit, sans clé) :
  - Wikidata (requête SPARQL par numéro SEC « CIK », propriété P5531) :
      année de création (P571), pays (P17 / siège P159), site web (P856), logo (P154),
      titre des articles Wikipédia FR / EN
  - Wikipédia (API REST « summary ») : courte description de l'activité (FR, sinon EN)
Mise en cache 30 jours sur la branche « data » (companies.json) pour ne pas
solliciter ces services chaque soir. Une panne ici ne bloque jamais le calcul.
"""
import json, re, time, urllib.parse
from datetime import date, timedelta
import requests

UA = "Stock-Ing/1.0 (https://github.com/MassimoEpicureo/stock-ing; screener personnel)"
PREV_URL = "https://raw.githubusercontent.com/MassimoEpicureo/stock-ing/data/companies.json"
REFRESH_DAYS = 30
SPARQL = "https://query.wikidata.org/sparql"

# Nasdaq.com donne un nom de pays : repli quand Wikidata n'en a pas.
COUNTRY_ISO = {
    "United States": "US", "Canada": "CA", "United Kingdom": "GB", "Ireland": "IE", "Netherlands": "NL",
    "Switzerland": "CH", "Germany": "DE", "France": "FR", "Israel": "IL", "China": "CN", "Hong Kong": "HK",
    "Japan": "JP", "Taiwan": "TW", "South Korea": "KR", "India": "IN", "Brazil": "BR", "Mexico": "MX",
    "Argentina": "AR", "Chile": "CL", "Colombia": "CO", "Peru": "PE", "Bermuda": "BM", "Cayman Islands": "KY",
    "Luxembourg": "LU", "Belgium": "BE", "Denmark": "DK", "Sweden": "SE", "Norway": "NO", "Finland": "FI",
    "Spain": "ES", "Italy": "IT", "Greece": "GR", "Australia": "AU", "Singapore": "SG", "South Africa": "ZA",
    "Monaco": "MC", "Jersey": "JE", "Guernsey": "GG", "Panama": "PA", "Puerto Rico": "PR", "Bahamas": "BS",
    "Cyprus": "CY", "Uruguay": "UY", "Indonesia": "ID", "Philippines": "PH", "Turkey": "TR", "Austria": "AT",
    "Portugal": "PT", "Isle of Man": "IM", "British Virgin Islands": "VG", "Macau": "MO", "Malaysia": "MY",
    "Thailand": "TH", "Kazakhstan": "KZ", "United Arab Emirates": "AE", "Costa Rica": "CR", "Poland": "PL",
}

def _get(url, **kw):
    for i in range(3):
        try:
            r = requests.get(url, headers={"User-Agent": UA, "Accept": "application/json"}, timeout=60, **kw)
            if r.status_code == 200: return r
            if r.status_code in (429, 503): time.sleep(5 * (i + 1)); continue
            return None
        except Exception:
            time.sleep(3 * (i + 1))
    return None

def load_previous():
    r = _get(PREV_URL)
    try: return r.json() if r else {}
    except Exception: return {}

def _sparql(ciks=None, tickers=None):
    """Par CIK (P5531) ou, à défaut, par ticker coté au NYSE (Q13677) / Nasdaq (Q82059)."""
    if ciks:
        vals = " ".join(f'"{c:010d}" "{c}"' for c in ciks)
        ident = f'VALUES ?cik {{ {vals} }}\n  ?item wdt:P5531 ?cik .'
    else:
        vals = " ".join('"' + t.replace('"', '') + '"' for t in tickers)
        ident = (f'VALUES ?cik {{ {vals} }}\n  ?item p:P414 ?st . ?st ps:P414 ?ex ; pq:P249 ?cik .'
                 '\n  VALUES ?ex { wd:Q13677 wd:Q82059 }'
                 '\n  FILTER NOT EXISTS { ?st pq:P582 ?fin . }'          # cotation terminée
                 '\n  FILTER NOT EXISTS { ?item wdt:P576 ?dissous . }'   # société dissoute
                 '\n  OPTIONAL { ?item rdfs:label ?lab . FILTER(lang(?lab) = "en") }')
    q = f"""
SELECT ?cik ?item ?lab ?inception ?iso ?hqiso ?website ?logo ?frTitle ?enTitle ?descFr ?descEn WHERE {{
  {ident}
  OPTIONAL {{ ?item wdt:P571 ?inception . }}
  OPTIONAL {{ ?item wdt:P17 ?c . ?c wdt:P297 ?iso . }}
  OPTIONAL {{ ?item wdt:P159 ?hq . ?hq wdt:P17 ?hc . ?hc wdt:P297 ?hqiso . }}
  OPTIONAL {{ ?item wdt:P856 ?website . }}
  OPTIONAL {{ ?item wdt:P154 ?logo . }}
  OPTIONAL {{ ?fr schema:about ?item ; schema:isPartOf <https://fr.wikipedia.org/> ; schema:name ?frTitle . }}
  OPTIONAL {{ ?en schema:about ?item ; schema:isPartOf <https://en.wikipedia.org/> ; schema:name ?enTitle . }}
  OPTIONAL {{ ?item schema:description ?descFr . FILTER(lang(?descFr) = "fr") }}
  OPTIONAL {{ ?item schema:description ?descEn . FILTER(lang(?descEn) = "en") }}
}}"""
    r = None
    for i in range(3):
        try:
            r = requests.post(SPARQL, data={"query": q, "format": "json"},
                              headers={"User-Agent": UA, "Accept": "application/sparql-results+json"}, timeout=90)
            if r.status_code == 200: break
        except Exception:
            pass
        time.sleep(5 * (i + 1)); r = None
    if r is None: return {}
    out = {}
    for b in r.json()["results"]["bindings"]:
        g = lambda k: b.get(k, {}).get("value")
        key = g("cik")
        key = int(key) if (ciks and key.isdigit()) else key
        m = out.setdefault(key, {})
        inc = g("inception")
        if inc and inc[:1] != "-":
            y = int(inc[:4])
            if 1600 <= y <= date.today().year and (m.get("founded") is None or y < m["founded"]): m["founded"] = y
        for k_src, k_dst in (("lab", "label"), ("iso", "country"), ("hqiso", "hq_country"), ("website", "website"),
                             ("frTitle", "wp_fr"), ("enTitle", "wp_en"), ("descFr", "wd_fr"), ("descEn", "wd_en")):
            if g(k_src) and not m.get(k_dst): m[k_dst] = g(k_src)
        if g("logo") and not m.get("logo"):
            fn = urllib.parse.unquote(g("logo").rsplit("/", 1)[-1])
            m["logo"] = "https://commons.wikimedia.org/wiki/Special:FilePath/" + urllib.parse.quote(fn) + "?width=96"
        m["wikidata"] = g("item")
    return out

def _summary(lang, title):
    r = _get(f"https://{lang}.wikipedia.org/api/rest_v1/page/summary/" + urllib.parse.quote(title.replace(" ", "_"), safe=""))
    if not r: return None
    j = r.json()
    if j.get("type") == "disambiguation": return None
    txt = (j.get("extract") or "").strip()
    if not txt: return None
    # deux premières phrases, 360 caractères max
    parts = txt.replace("\n", " ").split(". ")
    short = ". ".join(parts[:2]).strip()
    if not short.endswith("."): short += "."
    if len(short) > 360: short = short[:357].rsplit(" ", 1)[0] + "…"
    url = (j.get("content_urls") or {}).get("desktop", {}).get("page")
    return {"text": short, "lang": lang, "url": url}

def enrich(rows, log):
    """Ajoute row['meta'] à chaque ligne ; renvoie le dictionnaire à publier (companies.json)."""
    prev = load_previous()
    today = date.today()
    cutoff = (today - timedelta(days=REFRESH_DAYS)).isoformat()
    def stale(m):
        # à rafraîchir : jamais vu, plus de 30 jours, ou non trouvé et pas encore cherché par ticker
        return ((m.get("fetched", "") < cutoff) or (not m.get("wikidata") and not m.get("tp"))
                or (m.get("wikidata") and not m.get("via") and not m.get("v2")))   # revalider l'ancien cache
    todo = [x["cik"] for x in rows if stale(prev.get(str(x["cik"])) or {})]
    log(f"Métadonnées : {len(rows) - len(todo)} en cache, {len(todo)} à rafraîchir (Wikidata / Wikipédia)")
    fresh = {}
    t0 = time.time()
    for i in range(0, len(todo), 150):
        fresh.update(_sparql(ciks=todo[i:i + 150])); time.sleep(1)
    # Seconde passe par ticker pour les sociétés sans CIK sur Wikidata
    by_cik = {x["cik"]: x["ticker"] for x in rows}
    missing = [c for c in todo if c not in fresh]
    tick = [by_cik[c] for c in missing]
    found = {}
    for i in range(0, len(tick), 150):
        found.update(_sparql(tickers=tick[i:i + 150])); time.sleep(1)
    names = {x["cik"]: x["name"] for x in rows}
    def same_company(sec_name, m):
        """Le premier mot significatif du nom SEC doit figurer dans le libellé ou le titre Wikipédia."""
        stop = {"the", "inc", "corp", "co", "company", "group", "holdings", "plc", "ltd", "sa", "nv", "ag", "se"}
        words = [w for w in re.findall(r"[a-z0-9]+", sec_name.lower()) if w not in stop and len(w) >= 3]
        hay = " ".join(str(m.get(k, "")) for k in ("label", "wp_en", "wp_fr", "website")).lower()
        return bool(words) and words[0] in hay
    rejected = 0
    for c in missing:
        m = found.get(by_cik[c])
        if not m: continue
        if same_company(names[c], m): m["via"] = "ticker"; fresh[c] = m
        else: rejected += 1
    log(f"Métadonnées : {rejected} correspondances par ticker rejetées (nom différent)")
    log(f"Métadonnées : {len(todo) - len(missing)} trouvées par CIK, {sum(1 for c in missing if c in fresh)} de plus par ticker")
    n_desc = 0
    for cik in todo:
        m = fresh.setdefault(cik, {})
        if time.time() - t0 > 900: break                 # garde-fou : 15 min max, la suite au prochain passage
        d = None
        if m.get("wp_fr"): d = _summary("fr", m["wp_fr"])
        if not d and m.get("wp_en"): d = _summary("en", m["wp_en"])
        if d: m["desc"] = d; n_desc += 1
        m["fetched"] = today.isoformat(); m["tp"] = True; m["v2"] = True
        time.sleep(0.05)
    store = dict(prev)
    for cik, m in fresh.items():
        if m.get("fetched"): store[str(cik)] = m
    hits = 0
    for x in rows:
        m = dict(store.get(str(x["cik"])) or {})
        if m.get("wikidata"): hits += 1
        if not m.get("country") and not m.get("hq_country") and x.get("country"):
            iso = COUNTRY_ISO.get(x["country"])
            if iso: m["country"] = iso
        if not m.get("desc") and (m.get("wd_fr") or m.get("wd_en")):
            m["desc"] = {"text": (m.get("wd_fr") or m.get("wd_en")).capitalize() + ".", "lang": "fr" if m.get("wd_fr") else "en",
                         "url": m.get("wikidata")}
        x["meta"] = {k: m.get(k) for k in ("founded", "country", "hq_country", "website", "logo", "desc") if m.get(k)}
    log(f"Métadonnées : {hits}/{len(rows)} sociétés trouvées sur Wikidata, {n_desc} descriptions Wikipédia récupérées ce passage "
        f"({time.time() - t0:.0f} s)")
    return store
