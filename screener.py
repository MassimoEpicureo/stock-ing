#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Stock-Ing — Screening quotidien (GitHub Actions)
------------------------------------------------
Réplique la logique de scoring du site Stock-Ing, applique les critères perso de
Massimo, et pousse vers Notion toute action qui dépasse le seuil :
        SCORE >= 90  ET  F-SCORE >= 7/9
Pour chaque action qualifiée (et pas déjà présente), crée :
  - une fiche dans la base "Entreprises identifiées" (Nom)
  - une ligne Watchlist liée, en statut "à analyser"
Le pipeline central (Apps Script) prend alors le relais.

Secrets attendus (variables d'environnement) :
  FINNHUB_KEY, NOTION_TOKEN
"""

import os, time, sys, requests

# ====================== CONFIG ======================

FINNHUB_KEY  = os.environ.get("FINNHUB_KEY", "").strip()
NOTION_TOKEN = os.environ.get("NOTION_TOKEN", "").strip()

FH = "https://finnhub.io/api/v1"
NOTION = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"

# IDs Notion (non secrets)
WATCHLIST_DB_ID    = "1f1664e8f47d806185f1e4a6f83e3f70"  # base "2. Watching List"
ENTREPRISES_DB_ID  = "1f1664e8f47d80db8f2b000b803b403a"  # base "1. Entreprises identifiées"

# Propriétés Notion (noms exacts)
STATUT_PROP        = "Statut"
STATUT_VALUE       = "à analyser"
RELATION_PROP      = "Entreprises identifiées"   # relation Watchlist -> entreprise
WATCHLIST_TITLE    = "PER < 20"                  # propriété titre de la Watchlist
NOM_PROP           = "Nom"                        # titre de la base entreprises

# Seuils de qualification finale
SEUIL_SCORE   = 90
SEUIL_FSCORE  = 7

# Critères perso de Massimo (seuils)
F = dict(rev5=0, nm=10, roic=12, roce=12, cr=1.0, ge=100, po=80, pb=8.0, pe=22, pfcf=28, mc=10)

# Univers analysé chaque jour : grandes capitalisations US de qualité (liste seed du site).
SEEDS = ['AAPL','MSFT','GOOGL','META','V','MA','ADBE','AVGO','INTU','NOW','CRM','ACN','TXN','CDNS','SNPS','AMAT','KLAC','ORCL','QCOM','AMD','CSCO','COST','MCD','NKE','SBUX','PEP','KO','PG','CL','MDLZ','MNST','HSY','KMB','HD','LOW','TJX','ORLY','AZO','ROST','WMT','HON','UNP','ITW','ETN','ROP','GE','EMR','PH','LMT','CAT','DE','BRK.B','SPGI','MCO','MSCI','AXP','BLK','MMC','AON','CME','ICE','UNH','JNJ','ABT','TMO','ISRG','DHR','LLY','ABBV','MRK','MDT','SYK','BDX','AMGN','ADP','PAYX','FAST','CTAS','WM','RSG','VRSK','EFX','TDG','WST','IDXX','RMD','ANET','LRCX','ADI','PANW','CRWD','FTNT','DIS','CMCSA','NFLX','TMUS','XOM','CVX','NEE','PLD','AMT','EQIX','O','SHW','ECL','LIN','NUE','PM','MO','CB','PGR','GS','MS','JPM','BAC','SCHW','NDAQ','GWW','PCAR','CSX','NSC','RTX','MMM','DOV','AME']

# ====================== FINNHUB ======================

_last_call = [0.0]
def _rate_gate(min_interval=1.1):
    """~55 appels/min pour rester sous la limite gratuite de 60/min."""
    dt = time.time() - _last_call[0]
    if dt < min_interval:
        time.sleep(min_interval - dt)
    _last_call[0] = time.time()

def fh(path):
    _rate_gate()
    url = f"{FH}/{path}{'&' if '?' in path else '?'}token={FINNHUB_KEY}"
    r = requests.get(url, timeout=30)
    if r.status_code == 429:
        time.sleep(15); return fh(path)
    if not r.ok:
        return None
    return r.json()

def pickM(M, keys):
    if not M: return None
    for k in keys:
        v = M.get(k)
        if v is not None:
            try:
                f = float(v)
                if f == f:  # not NaN
                    return f
            except (TypeError, ValueError):
                pass
    return None

def last_ser(SER, keys):
    """Dernière valeur d'une série annuelle Finnhub (series.annual)."""
    if not SER: return None
    for k in keys:
        arr = SER.get(k)
        if isinstance(arr, list) and arr:
            vals = [x for x in arr if x and x.get("v") is not None]
            vals.sort(key=lambda x: str(x.get("period", "")))
            if vals:
                try: return float(vals[-1]["v"])
                except (TypeError, ValueError): pass
    return None

# ====================== SCORING (réplique du site) ======================

def enrich(sym):
    p = fh(f"stock/profile2?symbol={sym}") or {}
    m = fh(f"stock/metric?symbol={sym}&metric=all") or {}
    M   = (m or {}).get("metric") or {}
    SER = ((m or {}).get("series") or {}).get("annual") or {}

    cap = (float(p["marketCapitalization"]) / 1000) if p.get("marketCapitalization") else None
    if not cap:
        mc = pickM(M, ["marketCapitalization"])
        cap = mc / 1000 if mc else None

    roic = pickM(M, ["roicTTM", "roicAnnual", "roic"])
    if roic is None:
        v = last_ser(SER, ["roicTTM", "roic"]); roic = (v * 100 if v is not None and abs(v) <= 2 else v)
    roce = pickM(M, ["rotcTTM", "rotcAnnual", "rotc"])
    if roce is None:
        v = last_ser(SER, ["rotcTTM", "rotc"]); roce = (v * 100 if v is not None and abs(v) <= 2 else v)

    nm   = pickM(M, ["netMarginTTM", "netProfitMarginTTM", "netMarginAnnual", "netMargin"])
    pb   = pickM(M, ["pbAnnual", "pbQuarterly", "pb", "pbTTM"])
    pe   = pickM(M, ["peTTM", "peBasicExclExtraTTM", "peNormalizedAnnual", "peAnnual", "pe"])
    pfcf = pickM(M, ["pfcfShareTTM", "pfcfTTM", "pfcfShareAnnual", "pfcf"])
    cr   = pickM(M, ["currentRatioAnnual", "currentRatioQuarterly", "currentRatio"])
    de_raw = pickM(M, ["totalDebt/totalEquityAnnual", "totalDebt/totalEquityQuarterly",
                       "totalDebtToEquityAnnual", "totalDebtToEquityQuarterly",
                       "totalDebtToEquity", "longTermDebt/equityAnnual"])
    gearing = None if de_raw is None else (de_raw if de_raw > 20 else de_raw * 100)
    po = pickM(M, ["payoutRatioTTM", "payoutRatioAnnual"])
    dy = pickM(M, ["currentDividendYieldTTM", "dividendYieldIndicatedAnnual", "dividendYield5Y"])
    rg = pickM(M, ["revenueGrowth5Y", "revenueGrowthTTMYoy", "revenueGrowth3Y"])

    # Hausse CA ~5 ans et rachats estimés (via salesPerShare)
    sps = [x for x in (SER.get("salesPerShare") or []) if x and x.get("v") and x["v"] > 0]
    rev5 = buyback = None
    if len(sps) >= 2:
        s = sorted(sps, key=lambda x: str(x.get("period", "")))
        n = min(5, len(s) - 1)
        v_old, v_new = s[-1 - n]["v"], s[-1]["v"]
        if v_old > 0:
            rev5 = (v_new / v_old - 1) * 100
            if rg is not None and v_new > 0:
                sps_cagr = ((v_new / v_old) ** (1 / n) - 1) * 100
                buyback = sps_cagr - rg   # CA/action plus rapide que CA total => rachats

    has_div = (dy is not None and dy > 0) or (po is not None and po > 0)

    # ----- SCORE : 13 critères (10 si OK, 4 si proche), normalisé /100 -----
    score = 0
    def chk(val, th, direction):
        nonlocal score
        if val is None: return
        ok   = (val >= th) if direction == "g" else (val <= th)
        warn = (val >= th * 0.8) if direction == "g" else (val <= th * 1.2)
        if ok: score += 10
        elif warn: score += 4
    chk(rev5, F["rev5"], "g"); chk(nm, F["nm"], "g")
    chk(roic, F["roic"], "g"); chk(roce, F["roce"], "g")
    chk(buyback, 0, "g"); chk(cr, F["cr"], "g"); chk(gearing, F["ge"], "l")
    chk(po, F["po"], "l"); chk(pb, F["pb"], "l"); chk(pe, F["pe"], "l"); chk(pfcf, F["pfcf"], "l")
    if has_div: score += 10
    if cap is not None and cap >= F["mc"]: score += 10
    score = round(score / 130 * 100)

    # ----- F-SCORE Piotroski (réplique du site) /9 -----
    f = 0
    if roic is not None and roic > 0: f += 1
    if roce is not None and roce >= 10: f += 1
    if cr is not None and cr >= 1: f += 1
    if gearing is not None and gearing < 100: f += 1
    if nm is not None and nm > 0: f += 1
    if nm is not None and nm >= 8: f += 1
    if has_div: f += 1
    if pb is not None and 0 < pb < 5: f += 1
    if buyback is not None and buyback > 0: f += 1

    return dict(sym=sym, name=p.get("name") or sym, sector=p.get("finnhubIndustry") or "",
                country=p.get("country") or "", cap=cap, pe=pe, score=score, fscore=f)

# ====================== NOTION ======================

def notion(method, path, payload=None):
    headers = {"Authorization": f"Bearer {NOTION_TOKEN}",
               "Notion-Version": NOTION_VERSION, "Content-Type": "application/json"}
    r = requests.request(method, f"{NOTION}/{path}", headers=headers, json=payload, timeout=30)
    if not r.ok:
        print(f"  ! Notion {r.status_code} sur {path} : {r.text[:300]}")
        return None
    return r.json()

def deja_present(name):
    """True si une entreprise du même Nom existe déjà dans la base."""
    res = notion("post", f"databases/{ENTREPRISES_DB_ID}/query",
                 {"filter": {"property": NOM_PROP, "title": {"equals": name}}})
    return bool(res and res.get("results"))

def creer_entreprise(name):
    res = notion("post", "pages", {
        "parent": {"database_id": ENTREPRISES_DB_ID},
        "properties": {NOM_PROP: {"title": [{"text": {"content": name}}]}},
    })
    return res.get("id") if res else None

def creer_ligne_watchlist(entreprise_id, pe):
    titre = f"{pe:.2f}" if isinstance(pe, (int, float)) else "—"
    notion("post", "pages", {
        "parent": {"database_id": WATCHLIST_DB_ID},
        "properties": {
            WATCHLIST_TITLE: {"title": [{"text": {"content": titre}}]},
            STATUT_PROP:     {"select": {"name": STATUT_VALUE}},
            RELATION_PROP:   {"relation": [{"id": entreprise_id}]},
        },
    })

# ====================== MAIN ======================

def main():
    if not FINNHUB_KEY or not NOTION_TOKEN:
        print("❌ Secrets manquants (FINNHUB_KEY / NOTION_TOKEN)."); sys.exit(1)

    print(f"▶ Screening de {len(SEEDS)} valeurs — seuil SCORE>={SEUIL_SCORE} ET F-Score>={SEUIL_FSCORE}/9")
    qualifiees = 0
    for i, sym in enumerate(SEEDS, 1):
        try:
            e = enrich(sym)
        except Exception as ex:
            print(f"[{i}/{len(SEEDS)}] {sym} — erreur : {ex}"); continue

        flag = "✓" if (e["score"] >= SEUIL_SCORE and e["fscore"] >= SEUIL_FSCORE) else " "
        print(f"[{i}/{len(SEEDS)}] {flag} {sym:<6} score={e['score']:>3} f={e['fscore']}/9  {e['name']}")

        if e["score"] >= SEUIL_SCORE and e["fscore"] >= SEUIL_FSCORE:
            if deja_present(e["name"]):
                print(f"        ↳ déjà dans Notion, ignorée."); continue
            ent_id = creer_entreprise(e["name"])
            if ent_id:
                creer_ligne_watchlist(ent_id, e["pe"])
                qualifiees += 1
                print(f"        ↳ ➕ ajoutée à la Watchlist en « à analyser ».")

    print(f"\n✅ Terminé. {qualifiees} nouvelle(s) entreprise(s) poussée(s) vers Notion.")

if __name__ == "__main__":
    main()
