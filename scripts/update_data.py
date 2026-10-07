import json
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas_market_calendars as mcal
import yfinance as yf

ET = ZoneInfo("America/New_York")

SCREENS = (
    "most_actives",
    "day_gainers",
    "growth_technology_stocks",
    "undervalued_growth_stocks",
    "undervalued_large_caps",
    "aggressive_small_caps",
)

FALLBACK_SYMBOLS = (
    "AAPL","MSFT","NVDA","AMZN","META","GOOGL","AVGO","TSLA","NFLX","ORCL",
    "AMD","QCOM","MU","CRM","ADBE","JPM","LLY","COST","WMT","XOM",
)

def num(v):
    try:
        if v is None or v == "" or v == "null":
            return None
        return float(v)
    except Exception:
        return None

def first(d, keys, default=None):
    for key in keys:
        v = d.get(key)
        if v is not None and v != "":
            return v
    return default

def as_date(v):
    if v is None:
        return None
    try:
        x = float(v)
        if x > 10_000_000_000:
            x /= 1000
        return datetime.fromtimestamp(x, tz=ET).date().isoformat()
    except Exception:
        return None

def market_closed(day):
    cal = mcal.get_calendar("NYSE")
    schedule = cal.schedule(start_date=day, end_date=day)
    return schedule.empty

def yahoo_screen(screen_name):
    try:
        result = yf.screen(screen_name, count=250)
        quotes = result.get("quotes", []) if isinstance(result, dict) else []
        print(f"Yahoo screen {screen_name}: {len(quotes)} rows")
        return quotes
    except Exception as e:
        print(f"Yahoo screen warning {screen_name}: {e}", file=sys.stderr)
        return []

def fallback_info():
    rows = []
    for sym in FALLBACK_SYMBOLS:
        try:
            info = yf.Ticker(sym).get_info()
            info["symbol"] = sym
            rows.append(info)
        except Exception as e:
            print(f"Fallback info warning {sym}: {e}", file=sys.stderr)
    return rows

def normalize_quote(q):
    sym = str(first(q, ["symbol"], "")).strip().upper()
    price = num(first(q, ["postMarketPrice", "preMarketPrice", "regularMarketPrice", "currentPrice", "price"]))
    if price is None or price <= 0:
        return None

    forward_eps = num(first(q, ["epsForward", "forwardEps", "epsNextYear"]))
    fpe = num(first(q, ["forwardPE", "forwardPeRatio", "forwardPERatio"]))
    if fpe is None and forward_eps and forward_eps > 0:
        fpe = price / forward_eps

    current_eps = num(first(q, ["epsCurrentYear", "epsTrailingTwelveMonths"]))
    next_eps = num(first(q, ["epsNextYear", "epsForward"]))
    eps_growth = num(first(q, ["earningsGrowth", "epsGrowth"]))
    if eps_growth is not None and abs(eps_growth) < 2:
        eps_growth *= 100
    if eps_growth is None and current_eps and next_eps and current_eps > 0:
        eps_growth = (next_eps / current_eps - 1) * 100

    change = num(first(q, ["regularMarketChangePercent", "percentchange", "changePercent"]))
    market_cap = num(first(q, ["marketCap", "intradaymarketcap"]))
    volume = num(first(q, ["regularMarketVolume", "dayvolume", "volume"])) or 0
    avg_volume = num(first(q, ["averageDailyVolume3Month", "avgdailyvol3m"])) or 0
    a50 = num(first(q, ["fiftyDayAverage", "priceAvg50"]))
    a200 = num(first(q, ["twoHundredDayAverage", "priceAvg200"]))

    if not market_cap or market_cap < 5_000_000_000:
        return None
    if price < 10:
        return None
    if volume < 300_000 and avg_volume < 300_000:
        return None
    if not sym or "." in sym or "^" in sym:
        return None

    analysts = int(num(first(q, ["numberOfAnalystOpinions", "numAnalystsEps"])) or 0)
    roe = num(first(q, ["returnOnEquity"]))
    margin = num(first(q, ["profitMargins", "netProfitMargin"]))
    debt = num(first(q, ["debtToEquity", "debtToEquityRatio"]))

    earnings_ts = first(q, ["earningsTimestampStart", "earningsTimestamp"])
    earnings_date = as_date(earnings_ts)

    return {
        "ticker": sym,
        "name": first(q, ["longName", "shortName", "displayName", "companyName", "name"], sym),
        "exchange": first(q, ["fullExchangeName", "exchange"], ""),
        "price": price,
        "changePct": change,
        "marketCap": market_cap,
        "volume": volume,
        "fpe": fpe,
        "forwardEps": forward_eps,
        "epsGrowthPct": eps_growth,
        "analysts": analysts,
        "priceAvg50": a50,
        "priceAvg200": a200,
        "roe": roe,
        "margin": margin,
        "debt": debt,
        "earningsDate": earnings_date,
    }

def score(row):
    price = row["price"] or 0
    chg = row["changePct"] or 0
    a50 = row["priceAvg50"] or 0
    a200 = row["priceAvg200"] or 0
    fpe = row["fpe"]
    growth = row["epsGrowthPct"]
    analysts = row["analysts"] or 0
    margin = row["margin"]
    roe = row["roe"]
    debt = row["debt"]

    pts = 45
    reasons = []
    risk = "低"

    if a200 and price > a200:
        pts += 14
        reasons.append("站上200日均線")
    if a50 and price > a50:
        pts += 12
        reasons.append("站上50日均線")
    if chg is not None:
        if 0.5 < chg < 6:
            pts += 11
            reasons.append("短線動能健康")
        elif chg >= 6:
            pts += 4
            risk = "中"
            reasons.append("單日漲幅偏大")
        elif chg <= -3:
            pts -= 8
            risk = "中"
            reasons.append("短線轉弱")

    if fpe is not None:
        if fpe < 15:
            pts += 18
            reasons.append("FPE偏低")
        elif fpe < 22:
            pts += 15
            reasons.append("FPE合理")
        elif fpe < 30:
            pts += 10
            reasons.append("FPE中性")
        elif fpe < 45:
            pts += 3
            risk = "中"
            reasons.append("FPE偏高")
        else:
            pts -= 10
            risk = "高"
            reasons.append("FPE很高")

    if growth is not None:
        if growth >= 25:
            pts += 15
            reasons.append("預估EPS高成長")
        elif growth >= 10:
            pts += 9
            reasons.append("預估EPS成長")
        elif growth < 0:
            pts -= 8
            risk = "高" if risk == "高" else "中"
            reasons.append("預估EPS下滑")

    if analysts >= 8:
        pts += 4
        reasons.append("分析師覆蓋度佳")
    if margin is not None and margin > 0.15:
        pts += 4
        reasons.append("獲利率佳")
    if roe is not None and roe > 0.15:
        pts += 4
        reasons.append("ROE佳")
    if debt is not None and debt > 250:
        pts -= 5
        risk = "高" if risk == "高" else "中"
        reasons.append("負債偏高")
    if row.get("earningsDate") == row.get("_today"):
        pts -= 5
        risk = "高"
        reasons.append("今日財報事件")

    pts = max(0, min(100, round(pts)))
    label = "優先觀察" if pts >= 88 else "值得研究" if pts >= 80 else "觀察" if pts >= 70 else "暫不優先"
    return pts, label, risk, reasons[:5]

def build_live():
    now = datetime.now(ET)
    day = now.date().isoformat()

    if now.weekday() >= 5 or market_closed(day):
        return {
            "status": "MARKET_CLOSED",
            "asOf": now.isoformat(),
            "date": day,
            "holiday": ["Weekend"] if now.weekday() >= 5 else ["NYSE market holiday"],
            "rows": [],
            "coverage": 0,
            "universeCount": 0,
            "focusCount": 0,
            "method": "Yahoo Finance screener via yfinance",
            "fpeFormula": "Yahoo Forward P/E, fallback to price / forward EPS",
            "note": "研究/監控工具，不構成投資建議。",
        }

    merged = {}
    for screen_name in SCREENS:
        for q in yahoo_screen(screen_name):
            sym = str(first(q, ["symbol"], "")).strip().upper()
            if sym:
                merged[sym] = q

    if not merged:
        print("Yahoo screener returned no rows; using fallback watch universe.")
        for q in fallback_info():
            sym = str(first(q, ["symbol"], "")).strip().upper()
            if sym:
                merged[sym] = q

    normalized = []
    for q in merged.values():
        row = normalize_quote(q)
        if row:
            normalized.append(row)

    today = day
    for row in normalized:
        row["_today"] = today
        sc, label, risk, reasons = score(row)
        row["score"] = sc
        row["label"] = label
        row["risk"] = risk
        row["reasons"] = reasons
        row.pop("_today", None)

    normalized.sort(
        key=lambda r: (
            -r["score"],
            r["fpe"] is None,
            r["fpe"] if r["fpe"] is not None else 999,
            -(r["marketCap"] or 0),
        )
    )
    rows = normalized[:32]
    fpe_count = sum(1 for r in rows if r["fpe"] is not None)

    return {
        "status": "LIVE",
        "asOf": now.isoformat(),
        "date": day,
        "rows": rows,
        "coverage": round(100 * fpe_count / max(1, len(rows))),
        "universeCount": len(normalized),
        "focusCount": len(rows),
        "method": "Yahoo Finance screener via yfinance",
        "fpeFormula": "Yahoo Forward P/E, fallback to price / forward EPS",
        "holiday": [],
        "note": "研究/監控工具，不構成投資建議。",
    }

def main():
    try:
        payload = build_live()
    except Exception as e:
        print(f"Fatal data build error: {e}", file=sys.stderr)
        payload = {
            "status": "DATA_ERROR",
            "asOf": datetime.now(ET).isoformat(),
            "date": datetime.now(ET).date().isoformat(),
            "rows": [],
            "coverage": 0,
            "universeCount": 0,
            "focusCount": 0,
            "error": str(e),
            "note": "研究/監控工具，不構成投資建議。",
        }

    with open("data.json", "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

if __name__ == "__main__":
    main()
