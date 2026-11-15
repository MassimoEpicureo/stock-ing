#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Stock-Ing — construction quotidienne des données (sans Finnhub, sans clé API)
=============================================================================

Sources (toutes gratuites, sans inscription) :
  1. SEC EDGAR  — company_tickers_exchange.json : univers NYSE / Nasdaq (ticker -> CIK)
  2. SEC EDGAR  — companyfacts.zip : tous les chiffres XBRL des 10-K / 20-F (un seul téléchargement)
  3. Nasdaq.com — screener public : dernier cours + capitalisation de toutes les actions US
     (secours : Yahoo Finance « spark », par paquets de 20 tickers)
  4. GitHub datasets — composition du S&P 500 (étiquette)

Sortie : out/screen.json (une ligne par société) + out/meta.json
Les seuils viennent de criteria.json (source unique, aussi lue par le site).

Si les données sont trop incomplètes, le script s'arrête en erreur (code 1) :
le workflow ouvre alors une « issue » GitHub, qui vous est notifiée par e-mail.
"""

import csv, io, json, math, os, re, sys, time, zipfile
from datetime import date, datetime, timezone
import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CRIT = json.load(open(os.path.join(ROOT, "criteria.json"), encoding="utf-8"))
OUT = os.path.join(ROOT, "out")
CACHE = os.environ.get("SEC_CACHE_DIR", os.path.join(ROOT, ".cache"))
os.makedirs(OUT, exist_ok=True); os.makedirs(CACHE, exist_ok=True)

# La SEC exige un User-Agent identifiant le demandeur (règle « fair access »).
SEC_UA = os.environ.get("SEC_USER_AGENT", "Stock-Ing screener MassimoEpicureo@users.noreply.github.com")
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

TODAY = date.today()
DIAG = set(filter(None, (os.environ.get("DIAG_TICKERS") or "").split(",")))
LOG = []
def log(msg):
    print(msg, flush=True); LOG.append(msg)

def get(url, ua=SEC_UA, timeout=60, tries=3, **kw):
    last = None
    for i in range(tries):
        try:
            r = requests.get(url, headers={"User-Agent": ua, "Accept-Encoding": "gzip, deflate",
                                           "Accept": "application/json, text/plain, */*"},
                             timeout=timeout, **kw)
            if r.status_code == 200:
                return r
            last = f"HTTP {r.status_code}"
        except Exception as e:
            last = str(e)
        time.sleep(2 * (i + 1))
    raise RuntimeError(f"{url} -> {last}")

def norm_ticker(t):
    return re.sub(r"[^A-Z0-9]", "", (t or "").upper())

# ============================================================ 1. UNIVERS SEC
def load_universe():
    d = get("https://www.sec.gov/files/company_tickers_exchange.json").json()
    f = d["fields"]; rows = [dict(zip(f, r)) for r in d["data"]]
    ex = set(CRIT["universe"]["exchanges"])
    out, seen = {}, set()
    for r in rows:
        if r.get("exchange") not in ex: continue
        t = r["ticker"]
        # on écarte warrants / droits / unités / préférentielles (suffixes typiques)
        if re.search(r"-(W|WT|WS|U|UN|R|RT|P[A-Z]?)$", t) or len(norm_ticker(t)) > 6: continue
        cik = int(r["cik"])
        # Un seul titre par société : le premier listé par la SEC (action principale).
        # Écarte les obligations cotées (ex. GOOGM, METAM) et les classes secondaires.
        if cik in seen: continue
        seen.add(cik)
        out.setdefault(norm_ticker(t), {"ticker": t, "cik": cik, "name": r["name"], "exchange": r["exchange"]})
    log(f"Univers SEC NYSE+Nasdaq : {len(out)} sociétés (un titre par société)")
    return out

# ============================================================ 2. COURS
def prices_nasdaq():
    url = "https://api.nasdaq.com/api/screener/stocks?tableonly=true&download=true"
    r = get(url, ua=BROWSER_UA, timeout=90, tries=3)
    rows = (r.json().get("data") or {}).get("rows") or []
    out = {}
    def num(s):
        try: return float(str(s).replace("$", "").replace(",", "").strip())
        except Exception: return None
    for x in rows:
        p = num(x.get("lastsale")); mc = num(x.get("marketCap"))
        if p and p > 0:
            out[norm_ticker(x.get("symbol"))] = {"price": p, "mcap": mc if mc else None,
                                                 "sector": x.get("sector") or "", "industry": x.get("industry") or "",
                                                 "country": x.get("country") or ""}
    return out

def prices_yahoo(tickers):
    """Secours : endpoint « spark » (sans clé), 20 tickers par appel."""
    out = {}
    tick = list(tickers)
    for i in range(0, len(tick), 20):
        chunk = tick[i:i + 20]
        ysyms = [t.replace(".", "-") for t in chunk]
        try:
            r = get("https://query1.finance.yahoo.com/v8/finance/spark?interval=1d&range=5d&symbols=" + ",".join(ysyms),
                    ua=BROWSER_UA, timeout=30, tries=2)
            j = r.json()
            for ys, t in zip(ysyms, chunk):
                node = j.get(ys) or {}
                closes = [c for c in (node.get("close") or []) if c]
                if closes:
                    out[norm_ticker(t)] = {"price": float(closes[-1]), "mcap": None, "sector": "", "industry": "", "country": ""}
        except Exception as e:
            log(f"  ! Yahoo paquet {i}: {e}")
        time.sleep(0.4)
    return out

# ============================================================ 3. S&P 500
def load_sp500():
    try:
        r = get("https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv", timeout=30)
        rows = list(csv.DictReader(io.StringIO(r.text)))
        s = {norm_ticker(x.get("Symbol")) for x in rows if x.get("Symbol")}
        log(f"S&P 500 : {len(s)} constituants")
        return s
    except Exception as e:
        log(f"  ! S&P 500 indisponible ({e}) — étiquette non renseignée")
        return set()

# ============================================================ 4. FONDAMENTAUX SEC
# Pour chaque indicateur : liste ordonnée de concepts XBRL. Les sociétés changent de
# balise au fil des années ; on fusionne donc année par année (premier concept trouvé).
CONCEPTS = {
    "revenue": [("us-gaap", "Revenues"), ("us-gaap", "RevenueFromContractWithCustomerExcludingAssessedTax"),
                ("us-gaap", "RevenueFromContractWithCustomerIncludingAssessedTax"), ("us-gaap", "SalesRevenueNet"),
                ("us-gaap", "SalesRevenueGoodsNet"), ("us-gaap", "RevenuesNetOfInterestExpense"),
                ("ifrs-full", "Revenue"), ("ifrs-full", "RevenueFromContractsWithCustomers")],
    "net_income": [("us-gaap", "NetIncomeLoss"), ("us-gaap", "NetIncomeLossAvailableToCommonStockholdersBasic"),
                   ("us-gaap", "ProfitLoss"), ("ifrs-full", "ProfitLossAttributableToOwnersOfParent"), ("ifrs-full", "ProfitLoss")],
    "op_income": [("us-gaap", "OperatingIncomeLoss"), ("ifrs-full", "ProfitLossFromOperatingActivities")],
    "pretax": [("us-gaap", "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest"),
               ("us-gaap", "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments"),
               ("ifrs-full", "ProfitLossBeforeTax")],
    "interest_exp": [("us-gaap", "InterestExpense"), ("us-gaap", "InterestExpenseNonoperating"),
                     ("us-gaap", "InterestExpenseDebt"), ("ifrs-full", "FinanceCosts"),
                     ("ifrs-full", "InterestExpenseOnBorrowings"), ("ifrs-full", "InterestExpense")],
    "cfo": [("us-gaap", "NetCashProvidedByUsedInOperatingActivities"),
            ("us-gaap", "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"),
            ("ifrs-full", "CashFlowsFromUsedInOperatingActivities")],
    "capex": [("us-gaap", "PaymentsToAcquirePropertyPlantAndEquipment"), ("us-gaap", "PaymentsToAcquireProductiveAssets"),
              ("us-gaap", "PaymentsForCapitalImprovements"),
              ("ifrs-full", "PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities"),
              ("ifrs-full", "PurchaseOfPropertyPlantAndEquipment")],
    "dividends": [("us-gaap", "PaymentsOfDividends"), ("us-gaap", "PaymentsOfDividendsCommonStock"),
                  ("ifrs-full", "DividendsPaidClassifiedAsFinancingActivities"), ("ifrs-full", "DividendsPaid"),
                  ("ifrs-full", "DividendsPaidToEquityHoldersOfParentClassifiedAsFinancingActivities")],
    "eps_diluted": [("us-gaap", "EarningsPerShareDiluted"), ("us-gaap", "EarningsPerShareBasicAndDiluted"),
                    ("ifrs-full", "DilutedEarningsLossPerShare")],
    "shares_diluted": [("us-gaap", "WeightedAverageNumberOfDilutedSharesOutstanding"),
                       ("us-gaap", "WeightedAverageNumberOfShareOutstandingBasicAndDiluted"),
                       ("ifrs-full", "AdjustedWeightedAverageShares"), ("ifrs-full", "WeightedAverageShares")],
    # --- bilan (valeurs instantanées à la clôture)
    "assets": [("us-gaap", "Assets"), ("ifrs-full", "Assets")],
    "assets_cur": [("us-gaap", "AssetsCurrent"), ("ifrs-full", "CurrentAssets")],
    "liab_cur": [("us-gaap", "LiabilitiesCurrent"), ("ifrs-full", "CurrentLiabilities")],
    "equity": [("us-gaap", "StockholdersEquity"),
               ("us-gaap", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"),
               ("ifrs-full", "EquityAttributableToOwnersOfParent"), ("ifrs-full", "Equity")],
    "cash": [("us-gaap", "CashAndCashEquivalentsAtCarryingValue"),
             ("us-gaap", "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents"),
             ("us-gaap", "CashAndDueFromBanks"), ("ifrs-full", "CashAndCashEquivalents")],
    "st_invest": [("us-gaap", "ShortTermInvestments"), ("us-gaap", "MarketableSecuritiesCurrent"),
                  ("us-gaap", "AvailableForSaleSecuritiesDebtSecuritiesCurrent"), ("ifrs-full", "CurrentFinancialAssets")],
    # Plusieurs balises « dette totale » coexistent et certaines ne couvrent qu'une ligne secondaire :
    # on retient la plus élevée (voir fundamentals()).
    "debt_total": [("us-gaap", "LongTermDebt"), ("us-gaap", "DebtLongtermAndShorttermCombinedAmount"),
                   ("us-gaap", "LongTermDebtAndCapitalLeaseObligationsIncludingCurrentMaturities"),
                   ("us-gaap", "DebtAndCapitalLeaseObligations"), ("us-gaap", "DebtInstrumentCarryingAmount")],
    "debt_lt": [("us-gaap", "LongTermDebtNoncurrent"), ("us-gaap", "LongTermDebtAndCapitalLeaseObligations"),
                ("us-gaap", "SecuredLongTermDebt"), ("us-gaap", "LongTermNotesPayable"), ("us-gaap", "SeniorLongTermNotes"),
                ("us-gaap", "UnsecuredLongTermDebt"), ("us-gaap", "OtherLongTermDebtNoncurrent"),
                ("ifrs-full", "NoncurrentPortionOfNoncurrentBorrowings"), ("ifrs-full", "NoncurrentBorrowings"),
                ("ifrs-full", "LongtermBorrowings")],
    "debt_cur": [("us-gaap", "LongTermDebtCurrent"), ("us-gaap", "DebtCurrent"),
                 ("us-gaap", "LongTermDebtAndCapitalLeaseObligationsCurrent"),
                 ("ifrs-full", "CurrentBorrowingsAndCurrentPortionOfNoncurrentBorrowings"),
                 ("ifrs-full", "CurrentPortionOfNoncurrentBorrowings"), ("ifrs-full", "CurrentBorrowings")],
    "debt_st": [("us-gaap", "ShortTermBorrowings"), ("us-gaap", "CommercialPaper"), ("us-gaap", "OtherShortTermBorrowings")],
    "income_tax": [("us-gaap", "IncomeTaxExpenseBenefit"), ("ifrs-full", "IncomeTaxExpenseContinuingOperations")],
}
INSTANT = {"assets", "assets_cur", "liab_cur", "equity", "cash", "st_invest", "debt_total", "debt_lt", "debt_cur", "debt_st"}
ANNUAL_FORMS = {"10-K", "10-K/A", "10-KT", "20-F", "20-F/A", "40-F", "40-F/A"}

def fy_of(end):
    """Exercice = année de clôture, sauf clôture en janvier-mars (rattachée à l'année précédente)."""
    return end.year if end.month >= 4 else end.year - 1

def pdate(s):
    try: return datetime.strptime(s, "%Y-%m-%d").date()
    except Exception: return None

def pick_units(units, key):
    if key == "shares_diluted": return units.get("shares")
    if key == "eps_diluted":
        for u in ("USD/shares", "EUR/shares", "GBP/shares", "CAD/shares", "JPY/shares", "CHF/shares"):
            if u in units: return units[u]
        return None
    for u in ("USD", "EUR", "GBP", "CAD", "JPY", "CHF", "DKK", "SEK", "AUD", "CNY", "TWD", "INR", "BRL", "MXN"):
        if u in units: return units[u]
    return None

def currency_of(facts):
    for tax in ("us-gaap", "ifrs-full"):
        for c in ("Revenues", "Revenue", "NetIncomeLoss", "ProfitLoss", "Assets"):
            u = ((facts.get(tax) or {}).get(c) or {}).get("units") or {}
            for k in u:
                if "/" not in k and k != "shares": return k
    return "USD"

def series(facts, key):
    """{exercice: valeur} pour un indicateur, à partir des dépôts annuels."""
    res = {}
    for tax, concept in CONCEPTS[key]:
        node = (facts.get(tax) or {}).get(concept)
        if not node: continue
        items = pick_units(node.get("units") or {}, key)
        if not items: continue
        best = {}
        for it in items:
            if it.get("form") not in ANNUAL_FORMS: continue
            end = pdate(it.get("end", ""))
            if not end: continue
            if key not in INSTANT:
                start = pdate(it.get("start", ""))
                if not start or not (340 <= (end - start).days <= 380): continue
            fy = fy_of(end)
            filed = it.get("filed", "")
            # on garde la valeur la plus récemment déposée (retraitements)
            cur = best.get(fy)
            if cur is None or (end, filed) > (cur[0], cur[1]):
                best[fy] = (end, filed, it.get("val"))
        for fy, (end, filed, v) in best.items():
            if fy not in res and v is not None:
                res[fy] = (float(v), end.isoformat())
    return res

def series_max(facts, key):
    """Comme series(), mais retient pour chaque exercice la valeur la PLUS ÉLEVÉE parmi les concepts."""
    res = {}
    for tc in CONCEPTS[key]:
        saved = CONCEPTS[key]; CONCEPTS[key] = [tc]
        try: one = series(facts, key)
        finally: CONCEPTS[key] = saved
        for fy, (v, end) in one.items():
            if fy not in res or v > res[fy][0]: res[fy] = (v, end)
    return res

def fundamentals(facts):
    raw = {k: series(facts, k) for k in CONCEPTS}
    raw["debt_total"] = series_max(facts, "debt_total")
    years = sorted(set(raw["revenue"]) & set(raw["net_income"]))
    if not years: return None
    last = years[-1]
    end_last = pdate(raw["revenue"][last][1])
    if (TODAY - end_last).days > 550:   # dernier 10-K trop ancien : société inactive / données périmées
        return None
    years = [y for y in years if y > last - 11]
    def v(k, y):
        x = raw[k].get(y); return x[0] if x else None
    hist = {}
    for y in years:
        # Dette financière = dette long terme totale (ou part non courante + part courante)
        # + emprunts court terme / billets de trésorerie (publiés à part). Hors dettes locatives.
        cands = []
        if v("debt_total", y) is not None: cands.append(v("debt_total", y))
        parts = [v("debt_lt", y), v("debt_cur", y)]
        if any(p is not None for p in parts): cands.append(sum(p for p in parts if p is not None))
        debt = max(cands) if cands else None
        if v("debt_st", y) is not None:
            debt = (debt or 0) + v("debt_st", y)
        op = v("op_income", y)
        if op is None and v("pretax", y) is not None:
            op = v("pretax", y) + (v("interest_exp", y) or 0)   # approximation de l'EBIT
        if op is None and v("net_income", y) is not None and v("income_tax", y) is not None:
            # IFRS sans résultat opérationnel publié (ex. TotalEnergies) : RN + impôt + frais financiers
            op = v("net_income", y) + v("income_tax", y) + (v("interest_exp", y) or 0)
        cfo, capex = v("cfo", y), v("capex", y)
        hist[y] = {
            "end": raw["revenue"][y][1], "revenue": v("revenue", y), "net_income": v("net_income", y),
            "op_income": op, "cfo": cfo, "capex": capex,
            "fcf": (cfo - abs(capex)) if (cfo is not None and capex is not None) else (cfo if cfo is not None and capex is None else None),
            "dividends": abs(v("dividends", y)) if v("dividends", y) is not None else None,
            "eps": v("eps_diluted", y), "shares": v("shares_diluted", y),
            "assets": v("assets", y), "assets_cur": v("assets_cur", y), "liab_cur": v("liab_cur", y),
            "equity": v("equity", y), "cash": (v("cash", y) or 0) + (v("st_invest", y) or 0) if v("cash", y) is not None else None,
            "debt": debt,
        }
    # Divisions d'actions (splits) postérieures au dernier 10-K : le cours est déjà ajusté, pas le BPA.
    # On compare le nombre d'actions le plus récent (page de garde 10-Q/10-K, toutes classes) au nombre dilué.
    split = None
    try:
        items = facts["dei"]["EntityCommonStockSharesOutstanding"]["units"]["shares"]
        last_end = max(i["end"] for i in items)
        latest = sum({i["val"] for i in items if i["end"] == last_end})
        sh = hist[last]["shares"]
        if sh and latest and last_end > hist[last]["end"]:
            f = latest / sh
            if f >= 1.8 or f <= 0.55:
                split = round(f, 2)
    except Exception:
        pass
    return {"currency": currency_of(facts), "last_fy": last, "hist": hist, "split_factor": split}

# ============================================================ 5. RATIOS
def pct(a, b):
    return (a / b * 100) if (a is not None and b not in (None, 0)) else None

def ratios_for_year(h):
    rev, ni, op = h["revenue"], h["net_income"], h["op_income"]
    cap_emp = (h["assets"] - h["liab_cur"]) if (h["assets"] is not None and h["liab_cur"] is not None) else None
    return {
        "net_margin": pct(ni, rev) if rev and rev > 0 else None,
        "roce": pct(op, cap_emp) if (cap_emp and cap_emp > 0) else None,
    }

def compute(f, price, mcap_px=None):
    H = f["hist"]; ys = sorted(H); L = ys[-1]; h = H[L]
    r = {}
    # --- croissance CA 5 ans (cumulée)
    base = L - 5
    if base in H and H[base]["revenue"] and H[base]["revenue"] > 0 and h["revenue"] is not None:
        r["rev_growth_5y"] = (h["revenue"] / H[base]["revenue"] - 1) * 100
        r["rev_cagr_5y"] = ((h["revenue"] / H[base]["revenue"]) ** (1 / 5) - 1) * 100 if h["revenue"] > 0 else None
    yr = ratios_for_year(h)
    r["net_margin"] = yr["net_margin"]; r["roce"] = yr["roce"]
    # moyennes 5 ans (liste C)
    last5 = [y for y in ys if y > L - 5]
    nm5 = [ratios_for_year(H[y])["net_margin"] for y in last5]; nm5 = [x for x in nm5 if x is not None]
    ro5 = [ratios_for_year(H[y])["roce"] for y in last5]; ro5 = [x for x in ro5 if x is not None]
    r["net_margin_avg5"] = sum(nm5) / len(nm5) if len(nm5) >= 3 else None
    r["roce_avg5"] = sum(ro5) / len(ro5) if len(ro5) >= 3 else None
    # --- dette nette / FCF
    if h["debt"] is not None or h["cash"] is not None:
        nd = (h["debt"] or 0) - (h["cash"] or 0)
        r["net_debt"] = nd
        if nd <= 0: r["net_debt_fcf"] = 0.0
        elif h["fcf"] is not None and h["fcf"] > 0: r["net_debt_fcf"] = nd / h["fcf"]
        else: r["net_debt_fcf"] = None; r["net_debt_fcf_note"] = "FCF négatif"
    # --- liquidité
    if h["assets_cur"] is not None and h["liab_cur"]:
        r["current_ratio"] = h["assets_cur"] / h["liab_cur"]
    # --- dividende / payout
    div = h["dividends"]
    r["has_dividend"] = bool(div and div > 0)
    if h["net_income"] and h["net_income"] > 0:
        r["payout"] = (div or 0) / h["net_income"] * 100
    # --- rachats (variation annuelle moyenne du nombre d'actions diluées, signe inversé)
    sh_years = [y for y in ys if H[y]["shares"] and y >= L - 5]
    if len(sh_years) >= 3:
        y0, y1 = sh_years[0], sh_years[-1]
        n = y1 - y0
        if n > 0 and H[y0]["shares"] > 0:
            r["buyback"] = -(((H[y1]["shares"] / H[y0]["shares"]) ** (1 / n)) - 1) * 100
    # --- années bénéficiaires
    r["years_profitable"] = sum(1 for y in ys if (H[y]["net_income"] or 0) > 0)
    r["years_available"] = len(ys)
    # --- valorisation (par action : neutre vis-à-vis des classes d'actions multiples)
    sh = h["shares"]
    eps0 = h["eps"]
    if f.get("split_factor") and sh:
        sh = sh * f["split_factor"]
        eps0 = eps0 / f["split_factor"] if eps0 is not None else None
        r["split_factor"] = f["split_factor"]
    if price and f["currency"] == "USD":
        eps = eps0 if eps0 is not None else (h["net_income"] / sh if sh else None)
        if eps and eps > 0: r["pe"] = price / eps
        if sh and h["fcf"] and h["fcf"] > 0: r["p_fcf"] = price / (h["fcf"] / sh)
        if sh and h["equity"] and h["equity"] > 0: r["pb"] = price / (h["equity"] / sh)
        if sh and div: r["div_yield"] = (div / sh) / price * 100
        if sh: r["mcap_calc"] = price * sh
        # Repli : capitalisation publiée par Nasdaq (sociétés à plusieurs classes d'actions, ex. Visa)
        if mcap_px:
            ni, fcf = h["net_income"], h["fcf"]
            if "pe" not in r and ni and ni > 0: r["pe"] = mcap_px / ni; r["valuation_note"] = "capitalisation Nasdaq"
            if "p_fcf" not in r and fcf and fcf > 0: r["p_fcf"] = mcap_px / fcf; r["valuation_note"] = "capitalisation Nasdaq"
            if "div_yield" not in r and div: r["div_yield"] = div / mcap_px * 100
            if "pb" not in r and h["equity"] and h["equity"] > 0: r["pb"] = mcap_px / h["equity"]
    return r

# ============================================================ 6. CRITÈRES & LISTES
def test(val, op, th):
    if val is None: return None
    if op == ">=": return val >= th
    if op == ">": return val > th
    if op == "<=": return val <= th
    if op == "<": return val < th
    if op == "is": return bool(val) == th
    return None

def evaluate(r):
    res = {}
    for block in ("quality", "price"):
        checks = {c["key"]: test(r.get(c["key"]), c["op"], c["value"]) for c in CRIT[block]["criteria"]}
        fails = sum(1 for v in checks.values() if v is not True)          # donnée manquante = non validé
        missing = sum(1 for v in checks.values() if v is None)
        res[block] = {"checks": checks, "fails": fails, "missing": missing,
                      "ok": fails <= CRIT[block]["max_misses"]}
    lists = []
    q, p = res["quality"]["ok"], res["price"]["ok"]
    if q and p: lists.append("A")
    if q and not p: lists.append("B")
    th = {c["key"]: c["value"] for c in CRIT["quality"]["criteria"]}
    if (not q and r.get("roce_avg5") is not None and r.get("net_margin_avg5") is not None
            and r["roce_avg5"] >= th["roce"] and r["net_margin_avg5"] >= th["net_margin"]
            and ((r.get("roce") or 0) < th["roce"] or (r.get("net_margin") or 0) < th["net_margin"])):
        lists.append("C")
    D = CRIT["lists"]["D"]
    pe, pb, cr = r.get("pe"), r.get("pb"), r.get("current_ratio")
    if (pe and pb and cr and pe <= D["pe_max"] and pe * pb <= D["graham_max"] and cr >= D["current_ratio_min"]
            and r.get("years_profitable") == r.get("years_available") and r.get("years_available", 0) >= D["min_years_positive"]):
        lists.append("D")
    res["lists"] = lists
    return res

# ============================================================ 7. MAIN
def download_companyfacts():
    path = os.path.join(CACHE, "companyfacts.zip")
    url = "https://www.sec.gov/Archives/edgar/daily-index/xbrl/companyfacts.zip"
    log("Téléchargement SEC companyfacts.zip …")
    t0 = time.time()
    with requests.get(url, headers={"User-Agent": SEC_UA}, stream=True, timeout=600) as r:
        r.raise_for_status()
        with open(path, "wb") as fh:
            for chunk in r.iter_content(1 << 20): fh.write(chunk)
    log(f"  ok : {os.path.getsize(path)/1e9:.2f} Go en {time.time()-t0:.0f} s")
    return path

def r2(x, n=2):
    return None if x is None or (isinstance(x, float) and (math.isnan(x) or math.isinf(x))) else round(x, n)

def diagnose(facts, label):
    """Journal : concepts disponibles (dernière valeur annuelle) utiles pour déboguer une société."""
    pat = re.compile(r"(Debt|Borrow|Notes|OperatingIncome|ProfitLoss|BeforeTax|FinanceCost|Shares|EarningsPerShare|LiabilitiesCurrent|CurrentLiabilities)", re.I)
    out = []
    for tax in ("us-gaap", "ifrs-full", "dei"):
        for c, node in (facts.get(tax) or {}).items():
            if not pat.search(c): continue
            for unit, items in (node.get("units") or {}).items():
                ann = [i for i in items if i.get("form") in ANNUAL_FORMS]
                if not ann: continue
                last = max(ann, key=lambda i: (i.get("end", ""), i.get("filed", "")))
                if last.get("end", "") >= f"{TODAY.year-2}-01-01":
                    out.append(f"{tax}:{c} [{unit}] {last.get('end')} = {last.get('val')}")
    log(f"  DIAG {label} :\n    " + "\n    ".join(sorted(out)[:80]))

def main():
    errors = []
    uni = load_universe()
    sp = load_sp500()

    # Cours
    px = {}
    try:
        px = prices_nasdaq(); log(f"Cours Nasdaq.com : {len(px)} actions")
    except Exception as e:
        log(f"  ! Nasdaq.com indisponible : {e}")
    missing_px = [u["ticker"] for k, u in uni.items() if k not in px]
    if len(missing_px) > 0.2 * len(uni):
        log(f"Secours Yahoo pour {len(missing_px)} tickers …")
        px.update(prices_yahoo(missing_px))
    cov_px = sum(1 for k in uni if k in px) / max(1, len(uni))
    log(f"Couverture des cours : {cov_px:.0%}")

    # Fondamentaux
    zpath = os.environ.get("COMPANYFACTS_ZIP") or download_companyfacts()
    z = zipfile.ZipFile(zpath)
    names = set(z.namelist())
    min_cap = CRIT["universe"]["min_market_cap_bn_usd"] * 1e9
    rows, n_try, n_fund = [], 0, 0
    n_big, n_big_ok = 0, 0
    for k, u in uni.items():
        p = px.get(k) or {}
        price = p.get("price")
        # Pré-filtre capitalisation avec la donnée Nasdaq (quand elle existe) pour éviter de lire 6 000 fichiers
        if p.get("mcap") is not None and p["mcap"] < min_cap * 0.5: continue
        fn = f"CIK{u['cik']:010d}.json"
        if fn not in names: continue
        n_try += 1
        big = p.get("mcap") is not None and p["mcap"] >= min_cap
        n_big += big
        try:
            facts = json.loads(z.read(fn)).get("facts") or {}
            if u["ticker"] in DIAG: diagnose(facts, u["ticker"])
            f = fundamentals(facts)
        except Exception as e:
            f = None
        if not f: continue
        n_fund += 1
        n_big_ok += big
        r = compute(f, price, p.get("mcap"))
        mcap = p.get("mcap") or r.get("mcap_calc")
        if mcap is None or mcap < min_cap: continue
        ev = evaluate(r)
        H = f["hist"]
        hist = {str(y): {kk: (r2(vv, 0) if isinstance(vv, float) and abs(vv) > 1000 else (r2(vv, 3) if isinstance(vv, float) else vv))
                         for kk, vv in H[y].items()} for y in sorted(H)}
        flag = None
        if r.get("split_factor"):
            flag = f"division d'actions détectée après le dernier 10-K (facteur {r['split_factor']}) : BPA et actions ajustés"
        elif p.get("mcap") and r.get("mcap_calc"):
            ratio = r["mcap_calc"] / p["mcap"]
            if ratio > 2 or ratio < 0.5: flag = "capitalisation incohérente (classes d'actions multiples ?) : vérifier PER et P/FCF"
        rows.append({
            "ticker": u["ticker"], "cik": u["cik"], "name": u["name"], "exchange": u["exchange"],
            "sp500": k in sp, "sector": p.get("sector", ""), "industry": p.get("industry", ""), "country": p.get("country", ""),
            "price": r2(price), "mcap_bn": r2(mcap / 1e9, 1), "currency": f["currency"], "last_fy": f["last_fy"],
            "last_fy_end": H[f["last_fy"]]["end"],
            "metrics": {kk: (r2(vv) if isinstance(vv, float) else vv) for kk, vv in r.items() if kk != "mcap_calc"},
            "quality_fails": ev["quality"]["fails"], "quality_missing": ev["quality"]["missing"],
            "price_fails": ev["price"]["fails"],
            "checks": {**ev["quality"]["checks"], **ev["price"]["checks"]},
            "lists": ev["lists"], "flag": flag, "hist": hist,
        })
    cov_f = n_big_ok / max(1, n_big)
    log(f"Fondamentaux lus : {n_fund}/{n_try} dossiers · couverture des sociétés ≥ seuil (selon Nasdaq) : {n_big_ok}/{n_big} ({cov_f:.0%}) · retenues (≥ {CRIT['universe']['min_market_cap_bn_usd']} Md$) : {len(rows)}")

    dq = CRIT["data_quality"]
    if cov_px < dq["min_price_coverage"]: errors.append(f"Couverture des cours trop faible : {cov_px:.0%}")
    if cov_f < dq["min_fundamental_coverage"]: errors.append(f"Couverture des fondamentaux trop faible : {cov_f:.0%}")
    if len(rows) < dq["min_universe_size"]: errors.append(f"Univers anormalement petit : {len(rows)} sociétés")
    miss = sum(1 for x in rows if x["quality_missing"] >= 3)
    if rows and miss / len(rows) > 0.25:
        errors.append(f"{miss} sociétés ({miss/len(rows):.0%}) ont 3 critères qualité ou plus sans donnée")

    rows.sort(key=lambda x: (x["quality_fails"] + x["price_fails"], -(x["mcap_bn"] or 0)))
    counts = {L: sum(1 for x in rows if L in x["lists"]) for L in "ABCD"}
    meta = {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "n": len(rows),
            "sp500_tagged": sum(1 for x in rows if x["sp500"]), "lists": counts,
            "coverage": {"prices": round(cov_px, 3), "fundamentals": round(cov_f, 3)},
            "sources": ["SEC EDGAR companyfacts (10-K / 20-F)", "SEC company_tickers_exchange",
                        "Nasdaq.com screener (cours)", "github.com/datasets/s-and-p-500-companies"],
            "criteria_version": CRIT["version"], "errors": errors, "log": LOG[-40:]}
    json.dump({"meta": meta, "rows": rows}, open(os.path.join(OUT, "screen.json"), "w", encoding="utf-8"),
              ensure_ascii=False, separators=(",", ":"))
    json.dump(meta, open(os.path.join(OUT, "meta.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    log(f"Listes : {counts}")
    for t in (os.environ.get("CHECK_TICKERS") or "AAPL,V,DPZ,KO,GOOGL,TTE").split(","):
        x = next((y for y in rows if y["ticker"] == t), None)
        if x: log(f"  [{t}] FY{x['last_fy']} {json.dumps(x['metrics'], ensure_ascii=False)} listes={x['lists']} flag={x['flag']}")
        else: log(f"  [{t}] absent")
    if errors:
        open(os.path.join(OUT, "errors.txt"), "w", encoding="utf-8").write("\n".join(errors))
        log("ERREURS QUALITÉ DES DONNÉES :\n  - " + "\n  - ".join(errors))
        sys.exit(1)

if __name__ == "__main__":
    main()
