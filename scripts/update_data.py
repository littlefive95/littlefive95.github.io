import json
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo
import requests

BASE = "https://financialmodelingprep.com/stable"
KEY = os.environ.get("FMP_API_KEY")
if not KEY:
    raise SystemExit("FMP_API_KEY missing")

ET = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")
S = requests.Session()
S.headers["User-Agent"] = "US-Alpha-Watch/1.0"

def api(path, **params):
    params["apikey"] = KEY
    r = S.get(f"{BASE}/{path}", params=params, timeout=25)
    r.raise_for_status()
    data = r.json()
    if isinstance(data, dict) and any(str(k).lower().startswith("error") for k in data):
        raise RuntimeError(data)
    return data

def num(v):
    try:
        if v is None or v == "" or v == "null":
            return None
        return float(v)
    except Exception:
        return None

def holiday_names(day):
    names = set()
    for exch in ("NASDAQ", "NYSE"):
        try:
            rows = api("holidays-by-exchange", exchange=exch, **{"from": day, "to": day})
            for x in rows or []:
                if x.get("date") == day and x.get("isClosed") is True:
                    names.add(x.get("name") or exch)
        except Exception as e:
            print("holiday lookup warning", exch, e, file=sys.stderr)
    return sorted(names)

def company_universe():
    merged = {}
    for exch in ("NASDAQ", "NYSE"):
        try:
            rows = api(
                "company-screener",
                exchange=exch,
                country="US",
                marketCapMoreThan=5_000_000_000,
                volumeMoreThan=300_000,
                isEtf="false",
                isFund="false",
                limit=200,
            )
        except Exception as e:
            print("company screener warning", exch, e, file=sys.stderr)
            rows = []
        for x in rows or []:
            sym = (x.get("symbol") or "").strip()
            if sym and "." not in sym and "^" not in sym:
                merged[sym] = x
    return list(merged.values())

def batch_quotes(symbols):
    out = {}
    for i in range(0, len(symbols), 80):
        chunk = symbols[i:i+80]
        try:
            rows = api("batch-quote", symbols=",".join(chunk))
        except Exception as e:
            print("batch quote warning", e, file=sys.stderr)
            rows = []
        for q in rows or []:
            sym = (q.get("symbol") or "").strip()
            if sym:
                out[sym] = q
    return out

def exchange_quotes(exchange):
    try:
        rows = api("batch-exchange-quote", exchange=exchange)
        return rows or []
    except Exception as e:
        print("exchange quote warning", exchange, e, file=sys.stderr)
        return []

def estimate(sym):
    try:
        rows = api("analyst-estimates", symbol=sym, period="annual", page=0, limit=6)
    except Exception as e:
        print("estimate warning", sym, e, file=sys.stderr)
        return None, None, None, None
    today = datetime.now(UTC).date()
    vals = []
    for x in rows or []:
        try:
            d = datetime.strptime(str(x.get("date")), "%Y-%m-%d").date()
        except Exception:
            continue
        eps = num(x.get("epsAvg"))
        analysts = int(num(x.get("numAnalystsEps")) or 0)
        if d >= today and eps is not None and eps > 0:
            vals.append((d, eps, analysts))
    vals.sort()
    if not vals:
        return None, None, None, None
    d, eps, analysts = vals[0]
    prev_eps = vals[1][1] if len(vals) > 1 else None
    return eps, analysts, prev_eps, d.isoformat()

def quality(sym):
    try:
        rows = api("ratios-ttm", symbol=sym)
        x = (rows or [{}])[0]
        return num(x.get("netProfitMarginTTM")), num(x.get("returnOnEquityTTM")), num(x.get("debtToEquityRatioTTM"))
    except Exception as e:
        print("quality warning", sym, e, file=sys.stderr)
        return None, None, None

def earnings(day):
    try:
        rows = api("earnings-calendar", **{"from": day, "to": day})
        return {x.get("symbol"): x for x in rows or [] if x.get("symbol")}
    except Exception as e:
        print("earnings warning", e, file=sys.stderr)
        return {}

def score(q, fpe, eps_growth, nm, roe, de, earnings_flag):
    price = num(q.get("price")) or 0
    chg = num(q.get("changePercentage")) or 0
    a50 = num(q.get("priceAvg50")) or 0
    a200 = num(q.get("priceAvg200")) or 0
    pts = 45
    reasons = []
    risk = "低"

    if a200 and price > a200:
        pts += 14; reasons.append("站上200日均線")
    if a50 and price > a50:
        pts += 12; reasons.append("站上50日均線")
    if 0.5 < chg < 6:
        pts += 11; reasons.append("短線動能健康")
    elif chg >= 6:
        pts += 3; risk = "中"; reasons.append("單日漲幅偏大")
    elif chg <= -3:
        pts -= 8; reasons.append("短線轉弱")

    if fpe is not None:
        if fpe < 15:
            pts += 18; reasons.append("FPE偏低")
        elif fpe < 22:
            pts += 15; reasons.append("FPE合理")
        elif fpe < 30:
            pts += 10; reasons.append("FPE中性")
        elif fpe < 45:
            pts += 3; risk = "中"; reasons.append("FPE偏高")
        else:
            pts -= 10; risk = "高"; reasons.append("FPE很高")

    if eps_growth is not None:
        if eps_growth >= 25:
            pts += 15; reasons.append("預估EPS高成長")
        elif eps_growth >= 10:
            pts += 9; reasons.append("預估EPS成長")
        elif eps_growth < 0:
            pts -= 8; risk = "高" if risk == "高" else "中"; reasons.append("預估EPS下滑")

    if nm is not None and nm > 0.15:
        pts += 4; reasons.append("獲利率佳")
    if roe is not None and roe > 0.15:
        pts += 4; reasons.append("ROE佳")
    if de is not None and de > 2.5:
        pts -= 5; risk = "高" if risk == "高" else "中"; reasons.append("負債偏高")
    if earnings_flag:
        pts -= 5; risk = "高"; reasons.append("今日財報事件")

    pts = max(0, min(100, round(pts)))
    label = "優先觀察" if pts >= 88 else "值得研究" if pts >= 80 else "觀察" if pts >= 70 else "暫不優先"
    return pts, label, risk, reasons[:5]

def main():
    now = datetime.now(ET)
    day = now.date().isoformat()

    if now.weekday() >= 5:
        payload = {"status":"MARKET_CLOSED","asOf":now.isoformat(),"date":day,"holiday":["Weekend"],"rows":[],"coverage":0,"universeCount":0}
        json.dump(payload, open("data.json","w",encoding="utf-8"), ensure_ascii=False, indent=2)
        return

    holidays = holiday_names(day)
    if holidays:
        payload = {"status":"MARKET_CLOSED","asOf":now.isoformat(),"date":day,"holiday":holidays,"rows":[],"coverage":0,"universeCount":0}
        json.dump(payload, open("data.json","w",encoding="utf-8"), ensure_ascii=False, indent=2)
        return

    # Use exchange-wide quotes when available. If the account/time window does not return them,
    # fall back to the screener + batch-quote pair, which also works outside regular hours.
    qrows = exchange_quotes("NASDAQ") + exchange_quotes("NYSE")
    
    quotes = {}
    for q in qrows:
        sym = (q.get("symbol") or "").strip()
        if sym and "." not in sym and "^" not in sym:
            quotes[sym] = q

    base = []
    if quotes:
        for sym, q in quotes.items():
            price = num(q.get("price"))
            cap = num(q.get("marketCap"))
            vol = num(q.get("volume")) or 0
            if price and price >= 10 and cap and cap >= 5_000_000_000 and vol >= 300_000:
                base.append({"symbol":sym, "companyName":q.get("name") or sym, "exchange":q.get("exchange") or ""})
    else:
        base = company_universe()
        symbols = [x.get("symbol") for x in base if x.get("symbol")]
        quotes = batch_quotes(symbols)
        base = [{"symbol":x.get("symbol"),"companyName":x.get("companyName") or x.get("name") or x.get("symbol"),"exchange":x.get("exchange") or ""} for x in base if x.get("symbol") in quotes]

    universe = []
    for x in base:
        sym = x.get("symbol")
        q = quotes.get(sym)
        if not q:
            continue
        price = num(q.get("price"))
        cap = num(q.get("marketCap"))
        vol = num(q.get("volume")) or 0
        if not price or price < 10 or not cap or cap < 5_000_000_000 or vol < 300_000:
            continue
        a50 = num(q.get("priceAvg50")) or 0
        a200 = num(q.get("priceAvg200")) or 0
        chg = num(q.get("changePercentage")) or 0
        pre = (20 if a200 and price > a200 else 0) + (15 if a50 and price > a50 else 0)
        pre += 15 if 0.5 < chg < 6 else 5 if chg >= 6 else -10 if chg <= -3 else 0
        universe.append((pre, cap, sym, q, x))

    universe.sort(reverse=True, key=lambda z:(z[0], z[1]))
    focus = universe[:24]
    cal = earnings(day)
    rows = []
    fpe_count = 0

    for _, cap, sym, q, meta in focus:
        eps, analysts, prev_eps, estimate_date = estimate(sym)
        fpe = (num(q.get("price")) / eps) if eps and eps > 0 else None
        growth = ((eps / prev_eps) - 1) * 100 if eps and prev_eps and prev_eps > 0 else None
        nm, roe, de = quality(sym)
        sc, label, risk, reasons = score(q, fpe, growth, nm, roe, de, sym in cal)
        if fpe is not None:
            fpe_count += 1
        rows.append({
            "ticker":sym,
            "name":q.get("name") or q.get("companyName") or meta.get("companyName") or sym,
            "exchange":q.get("exchange") or meta.get("exchange") or "",
            "price":num(q.get("price")),
            "changePct":num(q.get("changePercentage")),
            "marketCap":num(q.get("marketCap")),
            "volume":num(q.get("volume")),
            "fpe":round(fpe,2) if fpe is not None else None,
            "forwardEps":round(eps,2) if eps is not None else None,
            "epsGrowthPct":round(growth,1) if growth is not None else None,
            "analysts":analysts,
            "priceAvg50":num(q.get("priceAvg50")),
            "priceAvg200":num(q.get("priceAvg200")),
            "score":sc,
            "label":label,
            "risk":risk,
            "reasons":reasons,
            "earningsDate":cal.get(sym,{}).get("date"),
            "estimateDate":estimate_date,
        })

    rows.sort(key=lambda r:(-r["score"], r["fpe"] is None, r["fpe"] if r["fpe"] is not None else 999))
    payload = {
        "status":"LIVE",
        "asOf":now.isoformat(),
        "date":day,
        "rows":rows,
        "coverage":round(100*fpe_count/max(1,len(rows))),
        "universeCount":len(universe),
        "focusCount":len(rows),
        "method":"FMP exchange quotes with company-screener/batch-quote fallback + analyst estimates + TTM ratios + earnings calendar",
        "fpeFormula":"latest price / next annual consensus EPS",
        "holiday":[],
        "note":"研究/監控工具，不構成投資建議。"
    }
    json.dump(payload, open("data.json","w",encoding="utf-8"), ensure_ascii=False, indent=2)

if __name__ == "__main__":
    main()
