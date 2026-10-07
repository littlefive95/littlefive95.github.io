import json
import os
import sys
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests

BASE = "https://financialmodelingprep.com/stable"
KEY = os.environ.get("FMP_API_KEY")
if not KEY:
    print("FMP_API_KEY is missing", file=sys.stderr)
    sys.exit(1)

ET = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")
session = requests.Session()
session.headers.update({"User-Agent": "US-Alpha-Watch/1.0"})

def api(path, params=None, timeout=20):
    params = dict(params or {})
    params["apikey"] = KEY
    url = f"{BASE}/{path.lstrip('/')}"
    r = session.get(url, params=params, timeout=timeout)
    r.raise_for_status()
    data = r.json()
    if isinstance(data, dict) and any(k.lower().startswith("error") for k in data):
        raise RuntimeError(str(data))
    return data

def num(v):
    try:
        if v is None or v == "" or v == "None":
            return None
        return float(v)
    except Exception:
        return None

def holiday_closed(date_str):
    closed = False
    names = []
    for exch in ("NASDAQ", "NYSE"):
        try:
            rows = api("holidays-by-exchange", {"exchange": exch, "from": date_str, "to": date_str})
            for x in rows or []:
                if x.get("date") == date_str and x.get("isClosed") is True:
                    closed = True
                    names.append(f"{exch}: {x.get('name') or 'Market Holiday'}")
        except Exception as e:
            print(f"holiday lookup warning {exch}: {e}", file=sys.stderr)
    return closed, names

def get_candidates():
    out = {}
    for exch in ("NASDAQ", "NYSE"):
        rows = api("company-screener", {
            "exchange": exch,
            "country": "US",
            "marketCapMoreThan": 5_000_000_000,
            "volumeMoreThan": 500_000,
            "isEtf": "false",
            "isFund": "false",
            "limit": 120,
        })
        for x in rows or []:
            sym = (x.get("symbol") or "").strip()
            if not sym or "." in sym or "^" in sym:
                continue
            out[sym] = x
    return list(out.values())

def get_quotes(symbols):
    quotes = {}
    # FMP batch quote endpoint supports multiple symbols in one request.
    for i in range(0, len(symbols), 80):
        chunk = symbols[i:i+80]
        data = api("batch-quote", {"symbols": ",".join(chunk)})
        for q in data or []:
            quotes[q.get("symbol")] = q
    return quotes

def get_estimate(symbol):
    try:
        rows = api("analyst-estimates", {
            "symbol": symbol,
            "period": "annual",
            "page": 0,
            "limit": 5,
        })
    except Exception as e:
        print(f"estimate warning {symbol}: {e}", file=sys.stderr)
        return None, None, None
    today = datetime.now(UTC).date()
    usable = []
    for x in rows or []:
        d = None
        try:
            d = datetime.fromisoformat((x.get("date") or "").replace("Z", "+00:00")).date()
        except Exception:
            try:
                d = datetime.strptime(x.get("date", ""), "%Y-%m-%d").date()
            except Exception:
                pass
        eps = num(x.get("epsAvg"))
        if d and eps is not None and eps > 0 and d >= today:
            usable.append((d, eps, int(x.get("numAnalystsEps") or 0)))
    usable.sort()
    if not usable:
        return None, None, None
    d, eps, analysts = usable[0]
    prev = usable[1] if len(usable) > 1 else None
    return eps, analysts, (prev[1] if prev else None)

def get_quality(symbol):
    try:
        rows = api("ratios-ttm", {"symbol": symbol})
        x = (rows or [None])[0] or {}
        return {
            "netMargin": num(x.get("netProfitMarginTTM")),
            "roe": num(x.get("returnOnEquityTTM")),
            "debtEquity": num(x.get("debtToEquityRatioTTM")),
        }
    except Exception as e:
        print(f"quality warning {symbol}: {e}", file=sys.stderr)
        return {"netMargin": None, "roe": None, "debtEquity": None}

def earnings_map(date_str):
    try:
        rows = api("earnings-calendar", {"from": date_str, "to": date_str})
        return {x.get("symbol"): x for x in rows or [] if x.get("symbol")}
    except Exception as e:
        print(f"earnings calendar warning: {e}", file=sys.stderr)
        return {}

def score_row(q, eps, analysts, prev_eps, quality, earnings):
    price = num(q.get("price")) or 0
    chg = num(q.get("changePercentage")) or 0
    avg50 = num(q.get("priceAvg50")) or 0
    avg200 = num(q.get("priceAvg200")) or 0
    cap = num(q.get("marketCap")) or 0
    fpe = (price / eps) if eps and eps > 0 else None
    points = 0
    reasons = []
    risk = "低"

    if avg200 and price > avg200:
        points += 16
        reasons.append("站上 200 日均線")
    if avg50 and price > avg50:
        points += 14
        reasons.append("站上 50 日均線")
    if chg > 0.5 and chg < 6:
        points += 12
        reasons.append("短線動能健康")
    elif chg >= 6:
        points += 4
        risk = "中"
        reasons.append("單日漲幅偏大")
    elif chg < -3:
        points -= 8
        reasons.append("短線轉弱")

    if fpe is not None:
        if fpe < 15:
            points += 22
            reasons.append("FPE 偏低")
        elif fpe < 22:
            points += 18
            reasons.append("FPE 合理")
        elif fpe < 30:
            points += 12
            reasons.append("FPE 中性")
        elif fpe < 45:
            points += 5
            reasons.append("FPE 偏高")
            risk = "中"
        else:
            points -= 8
            reasons.append("FPE 很高")
            risk = "高"

    if prev_eps and eps and prev_eps > 0:
        growth = (eps / prev_eps - 1) * 100
        if growth >= 25:
            points += 18
            reasons.append(f"預估 EPS 成長 {growth:.0f}%")
        elif growth >= 10:
            points += 12
            reasons.append(f"預估 EPS 成長 {growth:.0f}%")
        elif growth < 0:
            points -= 8
            risk = "高" if risk == "高" else "中"
    else:
        growth = None

    if analysts is not None:
        if analysts >= 8:
            points += 7
        elif analysts >= 4:
            points += 4

    nm = quality.get("netMargin")
    roe = quality.get("roe")
    de = quality.get("debtEquity")
    if nm is not None and nm > 0.15:
        points += 5
        reasons.append("獲利率佳")
    if roe is not None and roe > 0.15:
        points += 4
    if de is not None and de > 2.5:
        points -= 5
        risk = "高" if risk == "高" else "中"
        reasons.append("負債權益比偏高")

    if earnings:
        risk = "高" if risk != "高" else risk
        reasons.append("近期財報事件")
        points -= 5

    score = max(0, min(100, round(45 + points)))
    if score >= 88 and risk == "低":
        label = "優先觀察"
    elif score >= 80:
        label = "值得研究"
    elif score >= 70:
        label = "觀察"
    else:
        label = "暫不優先"

    return fpe, growth, score, label, risk, reasons

def main():
    now = datetime.now(ET)
    today = now.date()
    date_str = today.isoformat()
    weekend = today.weekday() >= 5
    closed, holiday_names = holiday_closed(date_str) if not weekend else (True, ["Weekend"])

    if closed:
        payload = {
            "status": "MARKET_CLOSED",
            "asOf": now.isoformat(),
            "date": date_str,
            "holiday": holiday_names,
            "rows": [],
            "coverage": 0,
            "note": "休市日不虛構行情。",
        }
        with open("data.json", "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        return

    base = get_candidates()
    symbols = [x.get("symbol") for x in base if x.get("symbol")]
    quotes = get_quotes(symbols)

    prelim = []
    for x in base:
        sym = x.get("symbol")
        q = quotes.get(sym)
        if not q:
            continue
        price = num(q.get("price"))
        cap = num(q.get("marketCap"))
        if not price or price < 10 or not cap or cap < 5_000_000_000:
            continue
        avg50 = num(q.get("priceAvg50")) or 0
        avg200 = num(q.get("priceAvg200")) or 0
        chg = num(q.get("changePercentage")) or 0
        pre = 0
        if avg200 and price > avg200: pre += 20
        if avg50 and price > avg50: pre += 15
        if 0.5 < chg < 6: pre += 15
        if chg >= 6: pre += 5
        if chg < -3: pre -= 10
        if cap > 50_000_000_000: pre += 4
        prelim.append((pre, sym, q, x))

    prelim.sort(reverse=True, key=lambda z: (z[0], num(z[2].get("marketCap")) or 0))
    focus = prelim[:32]
    earnings = earnings_map(date_str)
    rows = []
    fpe_available = 0

    for _, sym, q, meta in focus:
        eps, analysts, prev_eps = get_estimate(sym)
        fpe, growth, score, label, risk, reasons = score_row(
            q, eps, analysts, prev_eps, get_quality(sym), earnings.get(sym)
        )
        if fpe is not None:
            fpe_available += 1

        rows.append({
            "ticker": sym,
            "name": q.get("name") or meta.get("companyName") or meta.get("name") or sym,
            "exchange": q.get("exchange") or meta.get("exchange") or "",
            "price": price_or_null(q.get("price")),
            "changePct": price_or_null(q.get("changePercentage")),
            "fpe": round(fpe, 2) if fpe is not None else None,
            "forwardEps": round(eps, 2) if eps is not None else None,
            "epsGrowthPct": round(growth, 1) if growth is not None else None,
            "analysts": analysts,
            "marketCap": q.get("marketCap"),
            "priceAvg50": q.get("priceAvg50"),
            "priceAvg200": q.get("priceAvg200"),
            "score": score,
            "label": label,
            "risk": risk,
            "reasons": reasons[:4],
            "earningsDate": earnings.get(sym, {}).get("date") if sym in earnings else None,
            "earningsFlag": sym in earnings,
        })

    rows.sort(key=lambda r: (-r["score"], r["fpe"] is None, r["fpe"] or 999))
    payload = {
        "status": "LIVE",
        "asOf": now.isoformat(),
        "date": date_str,
        "rows": rows,
        "coverage": round(100 * fpe_available / max(1, len(rows))),
        "universeCount": len(symbols),
        "focusCount": len(rows),
        "method": "FMP Quote + Analyst Estimates + TTM Ratios + Earnings Calendar",
        "fpeFormula": "Latest Price / next annual consensus EPS",
        "holiday": [],
        "note": "研究/監控工具，不構成投資建議。",
    }
    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

def price_or_null(v):
    n = num(v)
    return round(n, 4) if n is not None else None

if __name__ == "__main__":
    main()
