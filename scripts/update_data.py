import json
import re
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

SECTOR_MAP = {
    "Technology": "科技",
    "Financial Services": "金融",
    "Healthcare": "醫療保健",
    "Consumer Cyclical": "非必需消費",
    "Consumer Defensive": "必需消費",
    "Industrials": "工業",
    "Communication Services": "通訊服務",
    "Energy": "能源",
    "Basic Materials": "原物料",
    "Real Estate": "房地產",
    "Utilities": "公用事業",
}

INDUSTRY_MAP = {
    "Semiconductors": "半導體",
    "Semiconductor Equipment & Materials": "半導體設備",
    "Electronic Components": "電子零組件",
    "Consumer Electronics": "消費電子",
    "Computer Hardware": "電腦硬體",
    "Software - Application": "應用軟體",
    "Software - Infrastructure": "基礎軟體",
    "Information Technology Services": "IT服務",
    "Banks - Diversified": "銀行",
    "Banks - Regional": "區域銀行",
    "Insurance - Diversified": "綜合保險",
    "Biotechnology": "生技",
    "Drug Manufacturers - General": "製藥",
    "Medical Devices": "醫療器材",
    "Oil & Gas Integrated": "石油能源",
    "Oil & Gas E&P": "油氣探勘",
    "Airlines": "航空",
    "Auto Manufacturers": "汽車",
    "Retail - Specialty": "零售",
    "Internet Retail": "電商",
    "Aerospace & Defense": "航太國防",
    "Steel": "鋼鐵",
    "Gold": "黃金礦業",
    "Copper": "銅礦",
    "Telecom Services": "電信",
    "REIT - Diversified": "REIT",
}

def sector_label(sector=None, industry=None, ticker=None, name=None, quote_type=None):
    industry = str(industry or "").strip()
    sector = str(sector or "").strip()
    ticker = str(ticker or "").strip().upper()
    name = str(name or "").strip()
    quote_type = str(quote_type or "").strip().upper()

    # ETFs / funds are displayed separately from operating-company sectors.
    if quote_type in {"ETF", "MUTUALFUND", "MUTUAL FUND"}:
        return "ETF"
    if (
        re.search(r"\bETF\b|\bINDEX FUND\b|\bFUND\b", name, re.I)
        or ticker.startswith("00")
        or any(k in name for k in ["標普500", "S&P 500", "美國500大", "高股息", "國債ETF", "債券ETF"])
    ):
        return "ETF"

    if industry in INDUSTRY_MAP:
        return INDUSTRY_MAP[industry]
    if sector in SECTOR_MAP:
        return SECTOR_MAP[sector]

    # Fallback classification from ticker/name when the provider does not
    # return Sector/Industry metadata.
    hay = f"{ticker} {name}".lower()
    keyword_groups = [
        (["semiconductor", "chip", "memory", "半導體", "矽"], "半導體"),
        (["broadcom", "super micro", "server", "computer", "hardware", "伺服器", "電腦"], "電腦硬體／AI伺服器"),
        (["electronic", "electronics", "celestica", "flex", "component", "電子", "零組件"], "電子零組件"),
        (["insurance", "insurance", "corebridge", "保險"], "保險"),
        (["bank", "financial", "mizuho", "nubank", "unibanco", "銀行", "金融"], "金融"),
        (["gold", "mining", "minerals", "黃金", "礦業"], "礦業／原物料"),
        (["airline", "airlines", "航空"], "航空"),
        (["software", "cloud", "saas", "軟體", "雲端"], "軟體／雲端"),
        (["biotech", "biotechnology", "drug", "pharma", "生技", "製藥"], "生技／製藥"),
        (["energy", "oil", "gas", "能源", "石油"], "能源"),
        (["retail", "store", "stores", "零售"], "零售"),
        (["telecom", "communications", "電信", "通訊"], "通訊"),
    ]
    for words, label in keyword_groups:
        if any(w in hay for w in words):
            return label

    if industry and industry.lower() not in {"none", "nan", "null"}:
        return industry
    if sector and sector.lower() not in {"none", "nan", "null"}:
        return sector
    return "未分類"

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

def nyse_latest_session(day):
    cal = mcal.get_calendar("NYSE")
    d = datetime.fromisoformat(day).date()
    start = datetime.fromordinal(max(1, d.toordinal() - 14)).date().isoformat()
    schedule = cal.schedule(start_date=start, end_date=day)
    return schedule.index[-1].date().isoformat() if not schedule.empty else day

def us_market_session(now):
    if now.weekday() >= 5 or market_closed(now.date().isoformat()):
        return "closed"
    mins = now.hour * 60 + now.minute
    if 240 <= mins < 570:
        return "pre"
    if 570 <= mins < 960:
        return "regular"
    if 960 <= mins < 1200:
        return "after"
    return "overnight"

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

def normalize_quote(q, session="regular"):
    sym = str(first(q, ["symbol"], "")).strip().upper()
    price_keys = {
        "pre": ["preMarketPrice", "regularMarketPrice", "currentPrice", "price"],
        "regular": ["regularMarketPrice", "currentPrice", "price"],
        "after": ["postMarketPrice", "regularMarketPrice", "currentPrice", "price"],
        "overnight": ["regularMarketPrice", "currentPrice", "price"],
        "closed": ["regularMarketPrice", "currentPrice", "price"],
    }.get(session, ["regularMarketPrice", "currentPrice", "price"])
    change_keys = {
        "pre": ["preMarketChangePercent", "regularMarketChangePercent", "percentchange", "changePercent"],
        "after": ["postMarketChangePercent", "regularMarketChangePercent", "percentchange", "changePercent"],
    }.get(session, ["regularMarketChangePercent", "percentchange", "changePercent"])
    price = num(first(q, price_keys))
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

    change = num(first(q, change_keys))
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
        "sector": sector_label(
            first(q, ["sectorDisp", "sector"]),
            first(q, ["industryDisp", "industry"]),
            sym,
            first(q, ["longName", "shortName", "displayName", "companyName", "name"], sym),
            first(q, ["quoteType", "quoteTypeDisp"]),
        ),
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

def stock_profiles(row):
    profiles = []
    price = row.get("price") or 0
    a50 = row.get("priceAvg50") or 0
    a200 = row.get("priceAvg200") or 0
    chg = row.get("changePct") or 0
    fpe = row.get("fpe")
    growth = row.get("epsGrowthPct")
    margin = row.get("margin")
    roe = row.get("roe")
    debt = row.get("debt")
    if (a200 and price > a200) or (a50 and price > a50) or chg > 0.5:
        profiles.append("momentum")
    if fpe is not None and 0 < fpe < 24:
        profiles.append("value")
    quality_ok = (
        growth is not None and growth >= 10
        and ((margin is not None and margin >= 0.12) or (roe is not None and roe >= 0.12) or row.get("analysts", 0) >= 8)
        and (debt is None or debt <= 250)
    )
    if quality_ok:
        profiles.append("quality")
    return profiles or ["momentum"]

def historical_event_signals(price, close=None, volume_ratio=None, chg=None, a50=None, a200=None):
    signals = []
    if close is not None and len(close) >= 21:
        try:
            if price > max(close[-21:-1]) * 1.005:
                signals.append("突破20日高點")
        except Exception:
            pass
    if a50 and price < a50 * 0.97:
        signals.append("跌破50日線")
    if a200 and price < a200 * 0.97:
        signals.append("跌破200日線")
    if volume_ratio is not None and volume_ratio >= 1.5:
        signals.append("量能放大")
    if chg is not None and abs(chg) >= 8:
        signals.append("高波動")
    return signals[:3]

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
    positives = []
    risks = []
    risk = "低"

    def addp(points, text):
        positives.append(f"＋{points}｜{text}")

    def addn(points, text):
        positives.append(f"−{abs(points)}｜{text}")

    def addr(text):
        risks.append(f"⚠｜{text}")

    if a200 and price > a200:
        pts += 25
        addp(25, "站上200日均線")
    if a50 and price > a50:
        pts += 10
        addp(10, "站上50日均線")
    if chg is not None:
        if 0.5 < chg < 6:
            pts += 15
            addp(15, "短線動能健康")
        elif 0 <= chg <= 0.5:
            pts += 8
            addp(8, "短線維持正向")
        elif chg >= 6:
            pts += 5
            addp(5, "單日仍有正向動能")
            risk = "中"
            addr("單日漲幅偏大")
        elif chg <= -3:
            pts -= 8
            addn(-8, "短線轉弱")
            risk = "中"
            addr("短線轉弱")

    if fpe is not None and fpe > 0:
        if fpe < 15:
            pts += 20
            addp(20, "FPE偏低")
        elif fpe < 22:
            pts += 16
            addp(16, "FPE合理")
        elif fpe < 30:
            pts += 10
            addp(10, "FPE中性")
        elif fpe < 45:
            pts += 4
            addp(4, "FPE偏高但仍有估值分")
            risk = "中"
            addr("FPE偏高")
        else:
            risk = "高"
            addr("FPE很高")
    elif fpe is not None and fpe <= 0:
        pts -= 10
        addn(-10, "Forward EPS為負")
        risk = "高"
        addr("FPE不具估值意義")

    if growth is not None:
        if growth >= 25:
            pts += 20
            addp(20, "預估EPS高成長")
        elif growth >= 10:
            pts += 12
            addp(12, "預估EPS成長")
        elif growth >= 0:
            pts += 5
            addp(5, "預估EPS維持成長")
        else:
            pts -= 8
            addn(-8, "預估EPS下滑")
            risk = "高" if risk == "高" else "中"
            addr("EPS成長轉弱")

    if analysts >= 8:
        pts += 5
        addp(5, "分析師覆蓋度佳")
    if margin is not None and margin > 0.15:
        pts += 5
        addp(5, "獲利率佳")
    if roe is not None and roe > 0.15:
        pts += 5
        addp(5, "ROE佳")
    if debt is not None and debt > 250:
        pts -= 5
        addn(-5, "負債偏高")
        risk = "高" if risk == "高" else "中"
        addr("負債偏高")

    pts = max(0, min(100, round(pts)))
    label = "進場候選" if pts >= 78 else "值得研究" if pts >= 68 else "觀察" if pts >= 55 else "暫不優先"

    pos_text = [x.split("｜", 1)[1] for x in positives if x.startswith("＋")][:2]
    neg_text = [x.split("｜", 1)[1] for x in positives if x.startswith("−")][:1]
    risk_text = [x.split("｜", 1)[1] for x in risks][:1]
    summary = "、".join(pos_text + neg_text) if (pos_text or neg_text) else "量化優勢目前有限"
    if risk_text:
        summary += "；注意：" + risk_text[0]
    judgement = "趨勢與基本面條件偏多" if pts >= 78 and risk == "低" else "條件偏多，但仍需確認風險" if pts >= 68 else "目前量化優勢有限"
    reasons = ["摘要｜" + summary, "判斷｜" + judgement] + positives[:4] + risks[:2]
    return pts, label, risk, reasons[:8]
def build_live():
    now = datetime.now(ET)
    day = now.date().isoformat()
    session = us_market_session(now)
    closed = session == "closed"

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
        row = normalize_quote(q, session=session)
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
    # Enrich the final ranked TOP 10 only.
    top_rows = normalized[:10]
    info = yahoo_info([r["ticker"] for r in top_rows])
    for row in top_rows:
        inf = info.get(row["ticker"], {})
        row["sector"] = sector_label(
            first(inf, ["sectorDisp", "sector"]),
            first(inf, ["industryDisp", "industry"]),
            row["ticker"],
            row["name"],
            first(inf, ["quoteType", "quoteTypeDisp"]),
        )

    hist = yahoo_history([r["ticker"] for r in top_rows])
    for row in top_rows:
        h = hist.get(row["ticker"])
        if h:
            close = h["close"]
            vol = h["volume"]
            row["priceAvg20"] = float(close.tail(20).mean()) if len(close) >= 20 else None
            avg20vol = float(vol.tail(20).mean()) if len(vol) >= 20 else None
            row["volumeRatio"] = float(row["volume"] / avg20vol) if avg20vol and avg20vol > 0 else None
            row["eventSignals"] = historical_event_signals(
                row["price"], close, row["volumeRatio"], row["changePct"], row["priceAvg50"], row["priceAvg200"]
            )
        else:
            row["eventSignals"] = []
        row["profiles"] = stock_profiles(row)

    rows = top_rows
    fpe_count = sum(1 for r in rows if r["fpe"] is not None)
    return {
        "status": "MARKET_CLOSED" if closed else "LIVE",
        "asOf": now.isoformat(),
        "date": day,
        "snapshotDate": nyse_latest_session(day),
        "dataMode": "LATEST_CLOSE" if session in {"closed", "overnight"} else "INTRADAY",
        "rows": rows,
        "coverage": round(100 * valuation_count / max(1, len(rows))),
        "universeCount": len(normalized),
        "focusCount": len(rows),
        "method": "Yahoo Finance screener via yfinance",
        "fpeFormula": "Yahoo Forward P/E, fallback to price / forward EPS",
        "holiday": ["Weekend"] if now.weekday() >= 5 else ["NYSE market holiday"] if closed else [],
        "note": "休市／隔夜顯示最近交易日 TOP 10；盤前、盤中、盤後依時段選用對應行情。研究/監控工具，不構成投資建議。",
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

def yahoo_intraday(symbols):
    if not symbols:
        return {}
    try:
        raw = yf.download(
            tickers=symbols,
            period="1d",
            interval="5m",
            auto_adjust=False,
            prepost=True,
            progress=False,
            threads=True,
            group_by="ticker",
        )
    except Exception as e:
        print("Taiwan intraday warning", e, file=sys.stderr)
        return {}

    out = {}
    for sym in symbols:
        try:
            if len(symbols) == 1:
                close = raw["Close"].dropna()
                volume = raw["Volume"].fillna(0)
            else:
                close = raw[(sym, "Close")].dropna()
                volume = raw[(sym, "Volume")].fillna(0)
            if len(close) == 0:
                continue
            idx = close.index[-1]
            out[sym] = {
                "price": float(close.iloc[-1]),
                "volume": float(volume.loc[close.index].sum()),
                "date": idx.date().isoformat(),
                "time": str(idx),
            }
        except Exception:
            continue
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

def yahoo_info(symbols):
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
    positives = []
    risks = []
    risk = "低"

    def addp(points, text):
        positives.append(f"＋{points}｜{text}")

    def addn(points, text):
        positives.append(f"−{abs(points)}｜{text}")

    def addr(text):
        risks.append(f"⚠｜{text}")

    if a200 and price > a200:
        pts += 20
        addp(20, "站上200日均線")
    if a50 and price > a50:
        pts += 10
        addp(10, "站上50日均線")
    if a20 and price > a20:
        pts += 5
        addp(5, "站上20日均線")

    if 0.5 <= chg < 6:
        pts += 15
        addp(15, "短線動能健康")
    elif 0 <= chg < 0.5:
        pts += 8
        addp(8, "短線維持正向")
    elif chg >= 6:
        pts += 4
        addp(4, "單日仍有正向動能")
        risk = "中"
        addr("單日漲幅偏大")
    elif chg <= -3:
        pts -= 8
        addn(-8, "短線轉弱")
        risk = "中"
        addr("短線轉弱")

    if vr >= 1.5:
        pts += 15
        addp(15, "成交量放大")
    elif vr >= 1.15:
        pts += 8
        addp(8, "成交量增溫")
    elif vr < 0.8:
        addr("成交量偏低")

    valuation = fpe if fpe is not None and fpe > 0 else pe if pe is not None and pe > 0 else None
    if valuation is not None:
        if valuation < 15:
            pts += 20
            addp(20, "估值偏低")
        elif valuation < 22:
            pts += 15
            addp(15, "估值合理")
        elif valuation < 30:
            pts += 8
            addp(8, "估值中性")
        elif valuation < 45:
            pts += 3
            addp(3, "估值偏高但仍有估值分")
            risk = "中"
            addr("估值偏高")
        else:
            risk = "高"
            addr("估值很高")

    if growth is not None:
        if growth >= 25:
            pts += 15
            addp(15, "EPS高成長")
        elif growth >= 10:
            pts += 10
            addp(10, "EPS成長")
        elif growth >= 0:
            pts += 4
            addp(4, "EPS維持正成長")
        else:
            pts -= 7
            addn(-7, "EPS成長轉弱")
            risk = "高" if risk == "高" else "中"
            addr("EPS成長轉弱")

    if a200 and price < a200 * 0.92:
        pts -= 6
        addn(-6, "距200日線偏遠")
        risk = "高" if risk == "高" else "中"
        addr("距200日線偏遠")

    pts = max(0, min(100, round(pts)))
    label = "進場候選" if pts >= 78 else "值得研究" if pts >= 68 else "觀察" if pts >= 55 else "暫不優先"
    pos_text = [x.split("｜", 1)[1] for x in positives if x.startswith("＋")][:2]
    neg_text = [x.split("｜", 1)[1] for x in positives if x.startswith("−")][:1]
    risk_text = [x.split("｜", 1)[1] for x in risks][:1]
    summary = "、".join(pos_text + neg_text) if (pos_text or neg_text) else "量化優勢目前有限"
    if risk_text:
        summary += "；注意：" + risk_text[0]
    judgement = "趨勢、量能與估值條件偏多" if pts >= 78 and risk == "低" else "條件偏多，但仍需確認風險" if pts >= 68 else "目前量化優勢有限"
    reasons = ["摘要｜" + summary, "判斷｜" + judgement] + positives[:4] + risks[:2]
    return pts, label, risk, reasons[:8]
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

    # Always keep the latest completed trading-day snapshot available.
    # During a live session, replace it with same-day intraday quotes when
    # Yahoo has current-day bars. Outside the session, continue screening
    # the latest close instead of returning an empty table.
    snapshot.sort(key=lambda x: (x.get("turnover") or 0), reverse=True)
    shortlist = snapshot[:80]
    using_intraday = False

    if now.weekday() < 5:
        symbols = [x["yahoo"] for x in shortlist]
        live = yahoo_intraday(symbols)
        live_today = sum(1 for q in live.values() if q.get("date") == day)
        if live_today > 0:
            current_rows = []
            for row in shortlist:
                q = live.get(row["yahoo"], {})
                if q.get("date") != day or q.get("price") is None:
                    continue
                prev_close = row.get("price")
                row = dict(row)
                row["price"] = q["price"]
                row["changePct"] = ((row["price"] - prev_close) / prev_close * 100) if prev_close else row.get("changePct")
                if q.get("volume"):
                    row["volume"] = q["volume"]
                current_rows.append(row)
            if current_rows:
                shortlist = current_rows
                using_intraday = True

    symbols = [x["yahoo"] for x in shortlist]
    hist = yahoo_history(symbols)

    enriched = []
    for row in shortlist:
        h = hist.get(row["yahoo"])
        if not h:
            continue
        close = h["close"]
        vol = h["volume"]
        row["_close_series"] = close
        row["priceAvg20"] = float(close.tail(20).mean()) if len(close) >= 20 else None
        row["priceAvg50"] = float(close.tail(50).mean()) if len(close) >= 50 else None
        row["priceAvg200"] = float(close.tail(200).mean()) if len(close) >= 200 else None
        avg20vol = float(vol.tail(20).mean()) if len(vol) >= 20 else None
        row["volumeRatio"] = float(row["volume"] / avg20vol) if avg20vol and avg20vol > 0 else None

        # During live trading, preserve the intraday quote. Outside the
        # session, use the latest completed daily close from Yahoo history.
        if not using_intraday and len(close):
            row["price"] = float(close.iloc[-1])
            row["changePct"] = row.get("changePct")
        enriched.append(row)

    enriched.sort(key=lambda x: ((x.get("volumeRatio") or 0), (x.get("turnover") or 0)), reverse=True)
    info = yahoo_info([x["yahoo"] for x in enriched[:10]])

    for row in enriched[:10]:
        inf = info.get(row["yahoo"], {})
        fpe = tw_num(first(inf, ["forwardPE", "forwardPeRatio"]))
        fwd_eps = tw_num(first(inf, ["epsForward", "forwardEps", "epsNextYear"]))
        growth = tw_num(first(inf, ["earningsGrowth", "earningsQuarterlyGrowth", "epsGrowth"]))
        if growth is not None and abs(growth) < 2:
            growth *= 100
        if fpe is None and fwd_eps and fwd_eps > 0:
            fpe = row["price"] / fwd_eps
        row["fpe"] = fpe
        row["valuation"] = fpe if fpe is not None else row.get("pe")
        row["valuationSource"] = "FPE" if fpe is not None else "PE" if row.get("pe") is not None else None
        row["forwardEps"] = fwd_eps
        row["epsGrowthPct"] = growth
        row["analysts"] = int(tw_num(first(inf, ["numberOfAnalystOpinions", "numberOfAnalysts"])) or 0)
        row["earningsDate"] = as_date(first(inf, ["earningsTimestampStart", "earningsTimestamp"]))
        row["quality"] = tw_num(first(inf, ["returnOnEquity", "profitMargins"]))
        row["sector"] = sector_label(
            first(inf, ["sectorDisp", "sector"]),
            first(inf, ["industryDisp", "industry"]),
            row["ticker"],
            row["name"],
            first(inf, ["quoteType", "quoteTypeDisp"]),
        )

    for row in enriched:
        sc, label, risk, reasons = taiwan_score(row)
        row["score"] = sc
        row["label"] = label
        row["risk"] = risk
        row["reasons"] = reasons
        row["profiles"] = []
        if ((row.get("priceAvg200") or 0) and row["price"] > row["priceAvg200"]) or (row.get("changePct") or 0) > 0.5:
            row["profiles"].append("momentum")
        if row.get("valuation") is not None and row["valuation"] > 0 and row["valuation"] < 24:
            row["profiles"].append("value")
        if (row.get("epsGrowthPct") is not None and row["epsGrowthPct"] >= 10) and ((row.get("fpe") is not None and row["fpe"] < 35) or (row.get("pe") is not None and row["pe"] < 35)):
            row["profiles"].append("quality")
        if not row["profiles"]:
            row["profiles"] = ["momentum"]
        row["eventSignals"] = historical_event_signals(row["price"], row.get("_close_series"), row.get("volumeRatio"), row.get("changePct"), row.get("priceAvg50"), row.get("priceAvg200"))
        row.pop("_close_series", None)
        row.pop("yahoo", None)
        row.pop("turnover", None)

    enriched.sort(key=lambda r: (-r["score"], -(r.get("volumeRatio") or 0), r.get("fpe") is None, r.get("fpe") or 999))
    rows = enriched[:10]
    valuation_count = sum(1 for r in rows if r.get("valuation") is not None)

    if using_intraday:
        status = "LIVE"
        note = "盤中使用當日即時行情；依趨勢、動能、量能、FPE/PE、EPS成長篩選進場候選；研究/監控工具，不構成投資建議。"
        data_mode = "INTRADAY"
    else:
        status = "MARKET_CLOSED"
        note = f"目前非台股即時交易時段；顯示最近交易日 {snapshot_date} 收盤 TOP 10，並維持量化排序，研究/監控工具，不構成投資建議。"
        data_mode = "LATEST_CLOSE"

    return {
        "status": status,
        "asOf": now.isoformat(),
        "date": day,
        "snapshotDate": snapshot_date,
        "dataMode": data_mode,
        "rows": rows,
        "coverage": round(100 * valuation_count / max(1, len(rows))),
        "valuationCoverage": round(100 * valuation_count / max(1, len(rows))),
        "universeCount": len(enriched),
        "focusCount": len(rows),
        "method": "TWSE + TPEx daily snapshot + Yahoo Finance 5-minute intraday/technical/valuation data",
        "fpeFormula": "Yahoo Forward P/E, fallback to latest price / forward EPS",
        "holiday": [] if using_intraday else ["Outside Taiwan cash-session; latest completed trading day shown"],
        "note": note,
    }

CRYPTO_API = "https://data-api.binance.vision/api/v3"
CMC_API = "https://pro-api.coinmarketcap.com/public-api/v3"

CRYPTO_CATEGORY = {
    "BTC": "比特幣／價值儲存",
    "ETH": "Layer 1／智能合約",
    "SOL": "Layer 1／智能合約",
    "BNB": "Layer 1／交易所生態",
    "XRP": "支付／跨境金融",
    "ADA": "Layer 1／智能合約",
    "AVAX": "Layer 1／智能合約",
    "DOT": "Layer 1／互操作",
    "LINK": "預言機",
    "AAVE": "DeFi／借貸",
    "UNI": "DeFi／DEX",
    "LTC": "支付／價值儲存",
    "DOGE": "Meme",
    "SHIB": "Meme",
    "PEPE": "Meme",
    "TRX": "Layer 1／支付",
    "SUI": "Layer 1／智能合約",
    "TON": "Layer 1／智能合約",
    "NEAR": "Layer 1／智能合約",
    "APT": "Layer 1／智能合約",
    "TAO": "AI／去中心化算力",
    "FET": "AI／去中心化算力",
    "RENDER": "AI／GPU算力",
    "INJ": "DeFi／金融基礎設施",
}

CRYPTO_EXCLUDE = (
    "UP", "DOWN", "BULL", "BEAR", "2L", "2S", "3L", "3S", "5L", "5S", "ETF",
)

CRYPTO_GROUP = {
    "BTC": "大型幣",
    "ETH": "大型幣",
    "BNB": "大型幣",
    "XRP": "大型幣",
    "SOL": "Layer 1",
    "ADA": "Layer 1",
    "AVAX": "Layer 1",
    "DOT": "Layer 1",
    "SUI": "Layer 1",
    "TRX": "Layer 1",
    "TON": "Layer 1",
    "NEAR": "Layer 1",
    "APT": "Layer 1",
    "AAVE": "DeFi",
    "UNI": "DeFi",
    "LINK": "DeFi",
    "INJ": "DeFi",
    "TAO": "AI",
    "FET": "AI",
    "RENDER": "AI",
    "DOGE": "Meme",
    "SHIB": "Meme",
    "PEPE": "Meme",
}

def crypto_group(symbol, tags=None, cmc_rank=None):
    base = symbol.replace("USDT", "")
    if base in CRYPTO_GROUP:
        return CRYPTO_GROUP[base]
    tag_text = " ".join([str(t.get("slug") if isinstance(t, dict) else t or "") for t in (tags or [])]).lower()
    if any(k in tag_text for k in ("meme", "memes", "dog-themed")):
        return "Meme"
    if any(k in tag_text for k in ("artificial-intelligence", "ai-big-data", "ai")):
        return "AI"
    if any(k in tag_text for k in ("decentralized-finance", "defi")):
        return "DeFi"
    if any(k in tag_text for k in ("layer-1", "smart-contract-platform")):
        return "Layer 1"
    if cmc_rank is not None:
        try:
            if int(cmc_rank) <= 20:
                return "大型幣"
        except Exception:
            pass
    return "其他"

def crypto_category(symbol):
    base = symbol.replace("USDT", "")
    if base in CRYPTO_CATEGORY:
        return CRYPTO_CATEGORY[base]
    if base in {"USDC","FDUSD","TUSD","USDE","DAI","USDD"}:
        return "穩定幣"
    return "加密貨幣"

def crypto_get(path, params=None):
    try:
        r = S.get(CRYPTO_API + path, params=params or {}, timeout=20)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"Crypto API warning {path}: {e}", file=sys.stderr)
        return None

def cmc_listings(limit=250):
    try:
        r = S.get(
            CMC_API + "/cryptocurrency/listings/latest",
            params={"start": 1, "limit": limit, "convert": "USD"},
            timeout=20,
        )
        r.raise_for_status()
        payload = r.json()
        return payload.get("data", []) if isinstance(payload, dict) else []
    except Exception as e:
        print(f"CMC warning: {e}", file=sys.stderr)
        return []

def cmc_quote(item):
    q = item.get("quote")
    if isinstance(q, list):
        return q[0] if q else {}
    if isinstance(q, dict):
        return q.get("USD") or q.get("usd") or (next(iter(q.values())) if q else {})
    return {}

def cmc_tags(item):
    tags = item.get("tags") or []
    return tags if isinstance(tags, list) else []

def crypto_klines(symbol, limit=210):
    data = crypto_get("/klines", {
        "symbol": symbol,
        "interval": "1d",
        "limit": limit,
    })
    if not isinstance(data, list) or len(data) < 20:
        return None
    closes = []
    quote_vols = []
    for k in data:
        try:
            closes.append(float(k[4]))
            quote_vols.append(float(k[7]))
        except Exception:
            pass
    if len(closes) < 20:
        return None
    return {"close": closes, "quoteVolume": quote_vols}

def crypto_score(row):
    price = row.get("price") or 0
    chg = row.get("changePct") or 0
    a20 = row.get("priceAvg20") or 0
    a50 = row.get("priceAvg50") or 0
    a200 = row.get("priceAvg200") or 0
    vr = row.get("volumeRatio") or 0
    qv = row.get("quoteVolume24h") or 0
    cmc_rank = row.get("cmcRank")

    pts = 0
    positives = []
    risks = []
    risk = "低"

    def addp(points, text):
        positives.append(f"＋{points}｜{text}")

    def addn(points, text):
        positives.append(f"−{abs(points)}｜{text}")

    def addr(text):
        risks.append(f"⚠｜{text}")

    if a200 and price > a200:
        pts += 20
        addp(20, "站上200日均線")
    if a50 and price > a50:
        pts += 10
        addp(10, "站上50日均線")
    if a20 and price > a20:
        pts += 5
        addp(5, "站上20日均線")

    if 1 <= chg < 6:
        pts += 25
        addp(25, "24H強勢動能")
    elif 0.5 <= chg < 1:
        pts += 20
        addp(20, "24H動能健康")
    elif 0 <= chg < 0.5:
        pts += 14
        addp(14, "24H維持正向")
    elif -1 <= chg < 0:
        pts += 8
        addp(8, "24H小幅回檔")
    elif -3 <= chg < -1:
        pts += 4
        addp(4, "24H跌勢有限")
        risk = "中"
        addr("24H短線偏弱")
    elif chg <= -3:
        addr("24H短線轉弱")
        risk = "中"
    elif chg >= 6:
        pts += 18
        addp(18, "24H漲幅仍有動能")
        risk = "中"
        addr("24H漲幅偏大")

    if vr >= 2:
        pts += 20
        addp(20, "成交量明顯放大")
    elif vr >= 1.5:
        pts += 16
        addp(16, "24H成交量放大")
    elif vr >= 1.15:
        pts += 10
        addp(10, "成交量增溫")
    elif vr >= 0.8:
        pts += 5
    else:
        addr("成交量偏低")

    if qv >= 1_000_000_000:
        pts += 5
        addp(5, "Binance流動性高")
    elif qv >= 250_000_000:
        pts += 4
        addp(4, "Binance流動性佳")
    elif qv >= 100_000_000:
        pts += 3
    elif qv >= 50_000_000:
        pts += 1

    if cmc_rank is not None:
        try:
            rank = int(cmc_rank)
            if rank <= 20:
                pts += 5
                addp(5, "CMC市值前20")
            elif rank <= 50:
                pts += 4
                addp(4, "CMC市值前50")
            elif rank <= 100:
                pts += 3
            elif rank <= 250:
                pts += 2
        except Exception:
            pass

    if a200 and price > a200:
        distance = (price / a200 - 1) * 100
        if 0 <= distance <= 20:
            pts += 10
            addp(10, "位於200日線上方合理區")
        elif distance <= 40:
            pts += 7
            addp(7, "高於200日線")
        elif distance <= 70:
            pts += 4
            addp(4, "距200日線較遠")
            risk = "中"
            addr("價格距200日線偏遠")
        else:
            pts += 2
            addp(2, "價格遠離200日線")
            risk = "高"
            addr("價格遠離200日線")
    elif a200 and price < a200 * 0.92:
        risk = "高" if risk == "高" else "中"
        addr("跌破200日線較多")

    if abs(chg) >= 10:
        risk = "高"
        addr("單日波動達兩位數")

    pts = max(0, min(100, round(pts)))
    label = "進場候選" if pts >= 85 else "優先研究" if pts >= 78 else "值得觀察" if pts >= 68 else "中性" if pts >= 58 else "偏弱"
    pos_text = [x.split("｜", 1)[1] for x in positives if x.startswith("＋")][:2]
    neg_text = [x.split("｜", 1)[1] for x in positives if x.startswith("−")][:1]
    risk_text = [x.split("｜", 1)[1] for x in risks][:1]
    summary = "、".join(pos_text + neg_text) if (pos_text or neg_text) else "量化優勢目前有限"
    if risk_text:
        summary += "；注意：" + risk_text[0]
    judgement = "趨勢與24H動能偏多" if pts >= 78 and risk == "低" else "動能尚可，但波動與量能需留意" if pts >= 68 else "目前量化優勢有限"
    reasons = ["摘要｜" + summary, "判斷｜" + judgement] + positives[:4] + risks[:2]
    return pts, label, risk, reasons[:8]
def build_crypto_live():
    now = datetime.now(ZoneInfo("Asia/Taipei"))
    tickers = crypto_get("/ticker/24hr")
    if not isinstance(tickers, list):
        return {
            "status": "DATA_ERROR",
            "asOf": now.isoformat(),
            "date": now.date().isoformat(),
            "rows": [],
            "coverage": 0,
            "universeCount": 0,
            "focusCount": 0,
            "error": "Binance market-data endpoint unavailable",
            "method": "Binance public spot market data",
            "note": "加密貨幣為 24/7 市場；資料來自公開市場行情。",
        }

    cmc_rows = cmc_listings(250)
    cmc_map = {}
    for item in cmc_rows:
        sym = str(item.get("symbol") or "").upper().strip()
        if not sym:
            continue
        quote = cmc_quote(item)
        cmc_map[sym] = {
            "rank": item.get("cmc_rank"),
            "marketCap": quote.get("market_cap"),
            "volume24h": quote.get("volume_24h"),
            "change24h": quote.get("percent_change_24h"),
            "tags": cmc_tags(item),
            "name": item.get("name") or sym,
        }

    candidates = []
    for q in tickers:
        sym = str(q.get("symbol") or "").upper()
        if not sym.endswith("USDT"):
            continue
        base = sym[:-4]
        if not base or base in {"USDT", "USDC", "FDUSD", "TUSD", "DAI", "USDE", "BUSD"}:
            continue
        if any(base.endswith(x) for x in CRYPTO_EXCLUDE):
            continue
        try:
            price = float(q.get("lastPrice") or 0)
            chg = float(q.get("priceChangePercent") or 0)
            qv = float(q.get("quoteVolume") or 0)
            count = int(float(q.get("count") or 0))
        except Exception:
            continue
        if price <= 0 or qv < 50_000_000:
            continue
        cmc = cmc_map.get(base, {})
        cmc_rank = cmc.get("rank")
        candidates.append({
            "ticker": base,
            "symbol": sym,
            "name": cmc.get("name") or base,
            "exchange": "Binance Spot + CMC",
            "sector": crypto_category(sym),
            "category": crypto_group(sym, cmc.get("tags"), cmc_rank),
            "cmcRank": cmc_rank,
            "cmcMarketCap": cmc.get("marketCap"),
            "cmcVolume24h": cmc.get("volume24h"),
            "cmcChange24h": cmc.get("change24h"),
            "price": price,
            "changePct": chg,
            "quoteVolume24h": qv,
            "trades24h": count,
            "fpe": None,
            "forwardEps": None,
            "epsGrowthPct": None,
            "analysts": None,
            "risk": "中",
        })

    candidates.sort(key=lambda x: x["quoteVolume24h"], reverse=True)
    shortlist = candidates[:60]

    enriched = []
    for row in shortlist:
        h = crypto_klines(row["symbol"], limit=210)
        if not h:
            continue
        close = h["close"]
        qv = h["quoteVolume"]
        row["priceAvg20"] = sum(close[-20:]) / 20 if len(close) >= 20 else None
        row["priceAvg50"] = sum(close[-50:]) / 50 if len(close) >= 50 else None
        row["priceAvg200"] = sum(close[-200:]) / 200 if len(close) >= 200 else None
        avg20 = sum(qv[-20:]) / 20 if len(qv) >= 20 else None
        row["volumeRatio"] = row["quoteVolume24h"] / avg20 if avg20 and avg20 > 0 else None
        row["_close_series"] = close
        sc, label, risk, reasons = crypto_score(row)
        row["score"] = sc
        row["label"] = label
        row["risk"] = risk
        row["reasons"] = reasons
        row["eventSignals"] = historical_event_signals(row["price"], row.get("_close_series"), row.get("volumeRatio"), row.get("changePct"), row.get("priceAvg50"), row.get("priceAvg200"))
        row.pop("_close_series", None)
        row.pop("symbol", None)
        enriched.append(row)

    enriched.sort(key=lambda r: (-r["score"], -(r.get("quoteVolume24h") or 0)))
    rows = enriched[:10]

    category_stats = {}
    for row in enriched:
        g = row.get("category") or "其他"
        stat = category_stats.setdefault(g, {"count": 0, "scoreSum": 0, "bestScore": 0})
        stat["count"] += 1
        stat["scoreSum"] += float(row.get("score") or 0)
        stat["bestScore"] = max(stat["bestScore"], int(row.get("score") or 0))
    for stat in category_stats.values():
        stat["avgScore"] = round(stat["scoreSum"] / max(1, stat["count"]), 1)
        stat.pop("scoreSum", None)

    return {
        "status": "LIVE",
        "asOf": now.isoformat(),
        "date": now.date().isoformat(),
        "rows": rows,
        "coverage": 100,
        "universeCount": len(enriched),
        "focusCount": len(rows),
        "categoryStats": category_stats,
        "cmcCoverage": round(100 * sum(1 for r in enriched if r.get("cmcRank") is not None) / max(1, len(enriched))),
        "method": "Binance public spot market data + CoinMarketCap market reference",
        "fpeFormula": "不適用；加密貨幣不使用 FPE / Forward EPS",
        "holiday": [],
        "note": "24/7 加密貨幣市場；依均線、24H動能、量能、流動性與波動風險排序，僅供研究與監控。",
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
    try:
        crypto_payload = build_crypto_live()
    except Exception as e:
        crypto_payload = {
            "status": "DATA_ERROR",
            "asOf": datetime.now(ZoneInfo("Asia/Taipei")).isoformat(),
            "date": datetime.now(ZoneInfo("Asia/Taipei")).date().isoformat(),
            "rows": [],
            "coverage": 0,
            "universeCount": 0,
            "focusCount": 0,
            "error": str(e),
            "note": "加密貨幣資料建置失敗。",
        }
    with open("crypto_data.json", "w", encoding="utf-8") as f:
        json.dump(crypto_payload, f, ensure_ascii=False, indent=2)
