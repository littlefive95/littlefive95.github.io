import json
import sys
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pandas_market_calendars as mcal
import yfinance as yf
import requests

ET = ZoneInfo("America/New_York")
S = requests.Session()
S.headers.update({"User-Agent": "US-TW-Alpha-Watch/1.0", "Accept-Encoding": "identity", "Connection": "close"})

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

    pts = 0
    reasons = []
    risk = "低"

    if a200 and price > a200:
        pts += 25
        reasons.append("站上200日均線")
    if a50 and price > a50:
        pts += 10
        reasons.append("站上50日均線")
    if chg is not None:
        if 0.5 < chg < 6:
            pts += 15
            reasons.append("短線動能健康")
        elif 0 <= chg <= 0.5:
            pts += 8
            reasons.append("短線維持正向")
        elif chg >= 6:
            pts += 5
            risk = "中"
            reasons.append("單日漲幅偏大")
        elif chg <= -3:
            pts -= 8
            risk = "中"
            reasons.append("短線轉弱")

    if fpe is not None and fpe > 0:
        if fpe < 15:
            pts += 20
            reasons.append("FPE偏低")
        elif fpe < 22:
            pts += 16
            reasons.append("FPE合理")
        elif fpe < 30:
            pts += 10
            reasons.append("FPE中性")
        elif fpe < 45:
            pts += 4
            risk = "中"
            reasons.append("FPE偏高")
        else:
            risk = "高"
            reasons.append("FPE很高")
    elif fpe is not None and fpe <= 0:
        pts -= 10
        risk = "高"
        reasons.append("Forward EPS為負，FPE不具估值意義")

    if growth is not None:
        if growth >= 25:
            pts += 20
            reasons.append("預估EPS高成長")
        elif growth >= 10:
            pts += 12
            reasons.append("預估EPS成長")
        elif growth >= 0:
            pts += 5
        else:
            pts -= 8
            risk = "高" if risk == "高" else "中"
            reasons.append("預估EPS下滑")

    if analysts >= 8:
        pts += 5
        reasons.append("分析師覆蓋度佳")
    if margin is not None and margin > 0.15:
        pts += 5
        reasons.append("獲利率佳")
    if roe is not None and roe > 0.15:
        pts += 5
        reasons.append("ROE佳")
    if debt is not None and debt > 250:
        pts -= 5
        risk = "高" if risk == "高" else "中"
        reasons.append("負債偏高")

    pts = max(0, min(100, round(pts)))
    label = "進場候選" if pts >= 78 else "值得研究" if pts >= 68 else "觀察" if pts >= 55 else "暫不優先"
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
    rows = normalized[:10]
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

TWSE_URL = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
TWSE_VAL_URL = "https://openapi.twse.com.tw/v1/exchangeReport/BWIBBU_ALL"
TPEX_URL = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes"

def tw_num(v):
    try:
        x = str(v).strip().replace(",", "")
        if x in {"", "-", "--", "---", "N/A", "null"}:
            return None
        return float(x)
    except Exception:
        return None

def roc_date(v):
    x = str(v or "").strip()
    if len(x) == 7 and x.isdigit():
        return f"{int(x[:3]) + 1911:04d}-{x[3:5]}-{x[5:7]}"
    return x[:10] if len(x) >= 10 else x

def fetch_json(url, retries=4):
    last = None
    for attempt in range(retries):
        try:
            r = S.get(url, timeout=35, headers={"Accept-Encoding": "identity", "Connection": "close"})
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last = e
            if attempt < retries - 1:
                time.sleep(1.5 * (attempt + 1))
    raise last

def taiwan_snapshot():
    try:
        twse = fetch_json(TWSE_URL)
    except Exception as e:
        print("TWSE daily snapshot warning", e, file=sys.stderr)
        twse = []
    try:
        tpex = fetch_json(TPEX_URL)
    except Exception as e:
        print("TPEx daily snapshot warning", e, file=sys.stderr)
        tpex = []
    if not twse and not tpex:
        raise RuntimeError("TWSE and TPEx daily snapshots unavailable")
    valuation = {}
    try:
        for x in fetch_json(TWSE_VAL_URL) or []:
            code = str(first(x, ["證券代號", "SecuritiesCompanyCode", "Code"], "")).strip()
            if code:
                valuation[code] = tw_num(first(x, ["本益比", "PEratio", "PER", "PE"]))
    except Exception as e:
        print("TWSE valuation warning", e, file=sys.stderr)

    rows = []
    for x in twse or []:
        code = str(first(x, ["證券代號", "SecuritiesCompanyCode", "Code"], "")).strip()
        if not code.isdigit() or len(code) < 4:
            continue
        close = tw_num(first(x, ["收盤價", "Close", "ClosingPrice"]))
        vol = tw_num(first(x, ["成交股數", "TradingShares", "Volume"])) or 0
        chg = tw_num(first(x, ["漲跌價差", "Change", "ChangePrice"]))
        sign = str(first(x, ["漲跌(+/-)", "Direction"], "")).strip()
        if sign in {"+", "-"} and chg is not None:
            chg = chg if sign == "+" else -abs(chg)
        turnover = tw_num(first(x, ["成交金額", "TradingValue", "TradingAmount"])) or 0
        if close and close >= 10 and vol >= 300_000:
            rows.append({
                "ticker": code,
                "yahoo": code + ".TW",
                "name": str(first(x, ["證券名稱", "CompanyName", "Name"], code)).strip(),
                "exchange": "TWSE",
                "price": close,
                "changePct": ((chg / (close - chg)) * 100) if chg is not None and close - chg else None,
                "volume": vol,
                "turnover": turnover,
                "pe": valuation.get(code),
            })

    for x in tpex or []:
        code = str(first(x, ["SecuritiesCompanyCode", "證券代號", "Code"], "")).strip()
        if not code.isdigit() or len(code) < 4:
            continue
        close = tw_num(first(x, ["Close", "收盤價", "ClosingPrice"]))
        vol = tw_num(first(x, ["TradingShares", "成交股數", "Volume"])) or 0
        turnover = tw_num(first(x, ["TradingValue", "成交金額", "TradingAmount"])) or 0
        change_pct = tw_num(first(x, ["ChangePercent", "漲跌幅", "ChangePct"]))
        change_price = tw_num(first(x, ["Change", "漲跌價差", "ChangePrice"]))
        if change_pct is None and change_price is not None and close and close - change_price:
            change_pct = (change_price / (close - change_price)) * 100
        if close and close >= 10 and vol >= 300_000:
            rows.append({
                "ticker": code,
                "yahoo": code + ".TWO",
                "name": str(first(x, ["CompanyName", "證券名稱", "Name"], code)).strip(),
                "exchange": "TPEx",
                "price": close,
                "changePct": change_pct,
                "volume": vol,
                "turnover": turnover,
                "pe": None,
            })

    dates = []
    for x in twse or []:
        d = roc_date(first(x, ["日期", "Date"]))
        if d:
            dates.append(d)
    for x in tpex or []:
        d = roc_date(first(x, ["Date", "日期"]))
        if d:
            dates.append(d)
    snapshot_date = max(dates) if dates else None
    return rows, snapshot_date

def taiwan_live_quotes(symbols):
    out = {}
    endpoint = "https://mis.twse.com.tw/stock/api/getStockInfo.jsp"
    for i in range(0, len(symbols), 40):
        chunk = symbols[i:i+40]
        ex = "|".join(
            ("tse_" + sym.split(".")[0] + ".tw") if sym.endswith(".TW")
            else ("otc_" + sym.split(".")[0] + ".tw")
            for sym in chunk
        )
        try:
            r = S.get(endpoint, params={"ex_ch": ex, "json": "1", "delay": "0"}, timeout=25)
            r.raise_for_status()
            data = r.json()
            for q in data.get("msgArray", []) or []:
                code = str(q.get("c") or "").strip()
                if not code:
                    continue
                price = tw_num(q.get("z"))
                prev = tw_num(q.get("y"))
                volume = tw_num(q.get("v")) or 0
                qdate = str(q.get("d") or "").strip()
                if price is None or price <= 0:
                    price = tw_num(q.get("pz"))
                if prev is None:
                    prev = tw_num(q.get("y"))
                chg = ((price - prev) / prev * 100) if price is not None and prev and prev > 0 else None
                if code:
                    out[code] = {
                        "price": price,
                        "prevClose": prev,
                        "changePct": chg,
                        "volume": volume,
                        "date": roc_date(qdate),
                        "time": str(q.get("t") or "").strip(),
                    }
        except Exception as e:
            print("Taiwan realtime warning", i, e, file=sys.stderr)
    return out

def yahoo_history(symbols):
    if not symbols:
        return {}
    try:
        raw = yf.download(
            tickers=symbols,
            period="1y",
            interval="1d",
            auto_adjust=False,
            progress=False,
            threads=True,
            group_by="ticker",
        )
    except Exception as e:
        print("Taiwan history warning", e, file=sys.stderr)
        return {}

    out = {}
    for sym in symbols:
        try:
            if len(symbols) == 1:
                close = raw["Close"].dropna()
                volume = raw["Volume"].dropna()
            else:
                close = raw[(sym, "Close")].dropna()
                volume = raw[(sym, "Volume")].dropna()
            if len(close) < 60:
                continue
            out[sym] = {
                "close": close,
                "volume": volume,
            }
        except Exception:
            continue
    return out

def taiwan_info(symbols):
    out = {}
    for sym in symbols:
        try:
            info = yf.Ticker(sym).get_info()
            out[sym] = info or {}
        except Exception as e:
            print("Taiwan Yahoo info warning", sym, e, file=sys.stderr)
    return out

def taiwan_score(row):
    price = row["price"] or 0
    chg = row["changePct"] or 0
    a20 = row.get("priceAvg20") or 0
    a50 = row.get("priceAvg50") or 0
    a200 = row.get("priceAvg200") or 0
    vr = row.get("volumeRatio") or 0
    fpe = row.get("fpe")
    growth = row.get("epsGrowthPct")
    pe = row.get("pe")

    pts = 0
    reasons = []
    risk = "低"

    if a200 and price > a200:
        pts += 20
        reasons.append("站上200日均線")
    if a50 and price > a50:
        pts += 10
        reasons.append("站上50日均線")
    if a20 and price > a20:
        pts += 5
        reasons.append("站上20日均線")

    if 0.5 <= chg < 6:
        pts += 15
        reasons.append("短線動能健康")
    elif 0 <= chg < 0.5:
        pts += 8
        reasons.append("短線維持正向")
    elif chg >= 6:
        pts += 4
        risk = "中"
        reasons.append("單日漲幅偏大")
    elif chg <= -3:
        pts -= 8
        risk = "中"
        reasons.append("短線轉弱")

    if vr >= 1.5:
        pts += 15
        reasons.append("成交量放大")
    elif vr >= 1.15:
        pts += 8
        reasons.append("成交量增溫")

    valuation = fpe if fpe is not None and fpe > 0 else pe if pe is not None and pe > 0 else None
    if valuation is not None:
        if valuation < 15:
            pts += 20
            reasons.append("估值偏低")
        elif valuation < 22:
            pts += 15
            reasons.append("估值合理")
        elif valuation < 30:
            pts += 8
            reasons.append("估值中性")
        elif valuation < 45:
            pts += 3
            risk = "中"
            reasons.append("估值偏高")
        else:
            risk = "高"
            reasons.append("估值很高")

    if growth is not None:
        if growth >= 25:
            pts += 15
            reasons.append("EPS高成長")
        elif growth >= 10:
            pts += 10
            reasons.append("EPS成長")
        elif growth >= 0:
            pts += 4
        else:
            pts -= 7
            risk = "高" if risk == "高" else "中"
            reasons.append("EPS成長轉弱")

    if a200 and price < a200 * 0.92:
        pts -= 6
        risk = "高" if risk == "高" else "中"
        reasons.append("距200日均線偏遠")

    pts = max(0, min(100, round(pts)))
    label = "進場候選" if pts >= 78 else "值得研究" if pts >= 68 else "觀察" if pts >= 55 else "暫不優先"
    return pts, label, risk, reasons[:5]
def build_taiwan_live():
    now = datetime.now(ZoneInfo("Asia/Taipei"))
    day = now.date().isoformat()
    try:
        snapshot, snapshot_date = taiwan_snapshot()
    except Exception as e:
        return {
            "status": "DATA_ERROR",
            "asOf": now.isoformat(),
            "date": day,
            "rows": [],
            "coverage": 0,
            "universeCount": 0,
            "focusCount": 0,
            "error": str(e),
            "method": "TWSE + TPEx daily snapshot + Yahoo Finance valuation/technical data",
            "note": "研究/監控工具，不構成投資建議。",
        }

    if now.weekday() >= 5:
        return {
            "status": "MARKET_CLOSED",
            "asOf": now.isoformat(),
            "date": day,
            "snapshotDate": snapshot_date,
            "holiday": ["Weekend"],
            "rows": [],
            "coverage": 0,
            "universeCount": 0,
            "focusCount": 0,
            "method": "TWSE/TPEx realtime quotes + daily snapshot + Yahoo Finance technical/valuation data",
            "note": "台股休市時不產生進場候選。",
        }

    snapshot.sort(key=lambda x: (x.get("turnover") or 0), reverse=True)
    shortlist = snapshot[:80]
    symbols = [x["yahoo"] for x in shortlist]
    live = taiwan_live_quotes(symbols)
    live_today = sum(1 for q in live.values() if q.get("date") == day)
    if live_today == 0:
        return {
            "status": "MARKET_CLOSED",
            "asOf": now.isoformat(),
            "date": day,
            "snapshotDate": snapshot_date,
            "holiday": ["No current TWSE/TPEx realtime quote"],
            "rows": [],
            "coverage": 0,
            "universeCount": 0,
            "focusCount": 0,
            "method": "TWSE/TPEx realtime quotes + daily snapshot + Yahoo Finance technical/valuation data",
            "note": "休市或當日即時行情尚未提供時，不產生進場候選。",
        }

    for row in shortlist:
        q = live.get(row["ticker"], {})
        if q.get("price") is not None:
            row["price"] = q["price"]
        if q.get("changePct") is not None:
            row["changePct"] = q["changePct"]
        if q.get("volume"):
            row["volume"] = q["volume"]

    hist = yahoo_history(symbols)

    enriched = []
    for row in shortlist:
        h = hist.get(row["yahoo"])
        if not h:
            continue
        close = h["close"]
        vol = h["volume"]
        row["priceAvg20"] = float(close.tail(20).mean()) if len(close) >= 20 else None
        row["priceAvg50"] = float(close.tail(50).mean()) if len(close) >= 50 else None
        row["priceAvg200"] = float(close.tail(200).mean()) if len(close) >= 200 else None
        avg20vol = float(vol.tail(20).mean()) if len(vol) >= 20 else None
        row["volumeRatio"] = float(row["volume"] / avg20vol) if avg20vol and avg20vol > 0 else None
        row["price"] = float(close.iloc[-1]) if len(close) else row["price"]
        enriched.append(row)

    enriched.sort(key=lambda x: ((x.get("volumeRatio") or 0), (x.get("turnover") or 0)), reverse=True)
    info = taiwan_info([x["yahoo"] for x in enriched[:20]])

    for row in enriched[:20]:
        inf = info.get(row["yahoo"], {})
        fpe = tw_num(first(inf, ["forwardPE", "forwardPeRatio"]))
        fwd_eps = tw_num(first(inf, ["epsForward", "forwardEps", "epsNextYear"]))
        growth = tw_num(first(inf, ["earningsGrowth", "earningsQuarterlyGrowth", "epsGrowth"]))
        if growth is not None and abs(growth) < 2:
            growth *= 100
        if fpe is None and fwd_eps and fwd_eps > 0:
            fpe = row["price"] / fwd_eps
        row["fpe"] = fpe
        row["forwardEps"] = fwd_eps
        row["epsGrowthPct"] = growth
        row["analysts"] = int(tw_num(first(inf, ["numberOfAnalystOpinions", "numberOfAnalysts"])) or 0)
        row["earningsDate"] = as_date(first(inf, ["earningsTimestampStart", "earningsTimestamp"]))
        row["quality"] = tw_num(first(inf, ["returnOnEquity", "profitMargins"]))
    
    for row in enriched:
        sc, label, risk, reasons = taiwan_score(row)
        row["score"] = sc
        row["label"] = label
        row["risk"] = risk
        row["reasons"] = reasons
        row.pop("yahoo", None)
        row.pop("turnover", None)
        row.pop("pe", None)

    enriched.sort(key=lambda r: (-r["score"], -(r.get("volumeRatio") or 0), r.get("fpe") is None, r.get("fpe") or 999))
    rows = enriched[:10]
    fpe_count = sum(1 for r in rows if r.get("fpe") is not None)

    return {
        "status": "LIVE",
        "asOf": now.isoformat(),
        "date": day,
        "snapshotDate": snapshot_date,
        "rows": rows,
        "coverage": round(100 * fpe_count / max(1, len(rows))),
        "universeCount": len(enriched),
        "focusCount": len(rows),
        "method": "TWSE + TPEx daily snapshot + Yahoo Finance valuation/technical data",
        "fpeFormula": "Yahoo Forward P/E, fallback to latest price / forward EPS",
        "holiday": [],
        "note": "依趨勢、動能、量能、FPE/PE、EPS成長篩選進場候選；研究/監控工具，不構成投資建議。",
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
    try:
        taiwan_payload = build_taiwan_live()
    except Exception as e:
        taiwan_payload = {
            "status": "DATA_ERROR",
            "asOf": datetime.now(ZoneInfo("Asia/Taipei")).isoformat(),
            "date": datetime.now(ZoneInfo("Asia/Taipei")).date().isoformat(),
            "rows": [],
            "coverage": 0,
            "universeCount": 0,
            "focusCount": 0,
            "error": str(e),
            "note": "研究/監控工具，不構成投資建議。",
        }
    with open("taiwan_data.json", "w", encoding="utf-8") as f:
        json.dump(taiwan_payload, f, ensure_ascii=False, indent=2)
