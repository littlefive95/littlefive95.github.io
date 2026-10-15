import json
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
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

# Credentials are supplied as GitHub Actions Secrets. Never commit API keys.
MASSIVE_API_KEY = os.environ.get("MASSIVE_API_KEY", "").strip()
FMP_API_KEY = os.environ.get("FMP_API_KEY", "").strip()
# Public website display needs the provider's applicable data-display license.
# These flags must only be set to "true" after the account has the required rights.
if MASSIVE_API_KEY and os.environ.get("MASSIVE_DISPLAY_LICENSE_CONFIRMED", "").strip().lower() != "true":
    print("Massive API key present but display-license flag is not confirmed; provider disabled.")
    MASSIVE_API_KEY = ""
if FMP_API_KEY and os.environ.get("FMP_DISPLAY_LICENSE_CONFIRMED", "").strip().lower() != "true":
    print("FMP API key present but display-license flag is not confirmed; provider disabled.")
    FMP_API_KEY = ""
SEVEN_MAGNIFICENT = ("AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA")
FMP_CACHE_PATH = ".cache/us_fundamentals_cache.json"
FMP_CACHE_TTL_SECONDS = 24 * 60 * 60
FMP_UNIVERSE_CACHE_TTL_SECONDS = 50 * 60
YAHOO_US_BACKUP_CACHE_TTL_SECONDS = 12 * 60 * 60

def load_previous_ranks(path, limit=20):
    """Load the last published TOP-N ranks so the dashboard can show rank movement."""
    try:
        with open(path, encoding="utf-8") as f:
            payload = json.load(f)
        rows = payload.get("rows") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            return {}
        out = {}
        for index, row in enumerate(rows[:limit], 1):
            ticker = str(row.get("ticker") or "").strip().upper()
            if ticker:
                out[ticker] = int(row.get("rank") or index)
        return out
    except Exception:
        return {}


def apply_rank_history(rows, path, limit=20):
    """Attach current rank and movement versus the previous published snapshot."""
    previous = load_previous_ranks(path, limit=limit)
    for rank, row in enumerate(rows[:limit], 1):
        ticker = str(row.get("ticker") or "").strip().upper()
        old = previous.get(ticker)
        row["rank"] = rank
        row["previousRank"] = old
        row["rankChange"] = None if old is None else old - rank


def num(v):
    try:
        if v is None or v == "" or v == "null":
            return None
        return float(v)
    except Exception:
        return None

def as_series(value, symbol=None):
    """Normalize yfinance field output to one Series across pandas/yfinance shapes."""
    if not isinstance(value, pd.DataFrame):
        return value
    if value.empty or value.shape[1] == 0:
        return pd.Series(index=value.index, dtype="float64")
    if symbol is not None:
        wanted = str(symbol).upper()
        for column in value.columns:
            if str(column).upper() == wanted:
                return value[column]
    if value.shape[1] == 1:
        return value.iloc[:, 0]
    for column in value.columns:
        try:
            candidate = value[column]
            if candidate.notna().any():
                return candidate
        except Exception:
            continue
    return value.iloc[:, 0]

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

def provider_json(url, params=None, timeout=25):
    """Fetch a JSON provider endpoint without exposing credentials in logs."""
    try:
        response = requests.get(
            url,
            params=params or {},
            timeout=timeout,
            headers={
                "User-Agent": "AlphaWatch/1.0",
                "Accept": "application/json",
                "Accept-Encoding": "identity",
                "Connection": "close",
            },
        )
    except requests.RequestException as exc:
        # Do not include request URLs in errors: API credentials are query parameters.
        raise RuntimeError("provider network request failed") from None
    if not response.ok:
        raise RuntimeError(f"provider returned HTTP {response.status_code}")
    try:
        payload = response.json()
    except ValueError:
        raise RuntimeError("provider returned invalid JSON") from None
    if isinstance(payload, dict) and (payload.get("Error Message") or payload.get("error")):
        message = str(payload.get("Error Message") or payload.get("error"))
        raise RuntimeError(message[:180])
    return payload


def fmp_request(endpoint, params=None):
    if not FMP_API_KEY:
        raise RuntimeError("FMP_API_KEY is not configured")
    query = dict(params or {})
    query["apikey"] = FMP_API_KEY
    return provider_json("https://financialmodelingprep.com/stable/" + endpoint, query)


def fmp_us_universe():
    """Build a broad liquid US universe from FMP and cache it for 50 minutes."""
    cache = load_fmp_fundamental_cache()
    cached_universe = cache.get("universe") or {}
    cached_rows = cached_universe.get("rows") if isinstance(cached_universe, dict) else None
    fetched_at = float(cached_universe.get("fetchedAt") or 0) if isinstance(cached_universe, dict) else 0
    if isinstance(cached_rows, list) and cached_rows and time.time() - fetched_at < FMP_UNIVERSE_CACHE_TTL_SECONDS:
        print(f"FMP US screener cache hit: {len(cached_rows)} stocks")
        return cached_rows

    payload = fmp_request("company-screener", {
        "marketCapMoreThan": 5_000_000_000,
        "priceMoreThan": 10,
        "avgVolumeMoreThan": 300_000,
        "country": "US",
        "exchange": "NASDAQ,NYSE",
        "isActivelyTrading": "true",
        "limit": 1000,
    },)
    if not isinstance(payload, list) or not payload:
        raise RuntimeError("FMP company screener returned no rows")
    rows = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        symbol = str(first(item, ["symbol"], "")).strip().upper()
        exchange = str(first(item, ["exchangeShortName", "exchange"], "")).strip().upper()
        name = str(first(item, ["companyName", "name"], symbol)).strip()
        market_cap = num(first(item, ["marketCap", "market_cap"]))
        price = num(first(item, ["price", "regularMarketPrice"]))
        volume = num(first(item, ["volume", "regularMarketVolume"])) or 0
        avg_volume = num(first(item, ["avgVolume", "averageVolume", "volumeAvg"])) or 0
        if not symbol or not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,9}", symbol):
            continue
        if exchange and exchange not in {"NASDAQ", "NYSE", "NASDAQGS", "NASDAQGM", "NASDAQCM"}:
            continue
        if item.get("isEtf") is True or item.get("isFund") is True:
            continue
        if market_cap is None or market_cap < 5_000_000_000 or price is None or price < 10:
            continue
        if max(volume, avg_volume) < 300_000:
            continue
        rows.append({
            "symbol": symbol,
            "longName": name,
            "companyName": name,
            "exchange": exchange,
            "fullExchangeName": exchange,
            "marketCap": market_cap,
            "regularMarketPrice": price,
            "regularMarketChangePercent": num(first(item, ["changePercentage", "changesPercentage", "changePercent"])),
            "regularMarketVolume": volume,
            "averageDailyVolume3Month": avg_volume,
            "sectorDisp": first(item, ["sector"], ""),
            "industryDisp": first(item, ["industry"], ""),
            "quoteType": "EQUITY",
            "_quoteSource": "FMP",
        })
    if not rows:
        raise RuntimeError("FMP screener returned no eligible NYSE/Nasdaq stocks")
    print(f"FMP US screener: {len(rows)} eligible NYSE/Nasdaq stocks")
    cache["universe"] = {"fetchedAt": time.time(), "rows": rows}
    cache["updatedAt"] = datetime.now(ET).isoformat()
    try:
        os.makedirs(os.path.dirname(FMP_CACHE_PATH), exist_ok=True)
        with open(FMP_CACHE_PATH, "w", encoding="utf-8") as handle:
            json.dump(cache, handle, ensure_ascii=False, indent=2)
    except Exception as exc:
        print(f"FMP universe cache write warning: {exc}", file=sys.stderr)
    return rows


def massive_us_snapshot(session):
    """Fetch a broad market snapshot; FMP supplies reference/financial metadata."""
    if not MASSIVE_API_KEY:
        return {}
    payload = provider_json(
        "https://api.massive.com/v2/snapshot/locale/us/markets/stocks/tickers",
        {"apiKey": MASSIVE_API_KEY, "include_otc": "false"},
        timeout=35,
    )
    items = payload.get("tickers", []) if isinstance(payload, dict) else []
    if not isinstance(items, list) or not items:
        raise RuntimeError("Massive full-market snapshot returned no tickers")
    out = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        symbol = str(item.get("ticker") or "").strip().upper()
        if not symbol or not re.fullmatch(r"[A-Z][A-Z0-9.\-]{0,9}", symbol):
            continue
        day = item.get("day") or {}
        prev = item.get("prevDay") or {}
        minute = item.get("min") or {}
        trade = item.get("lastTrade") or {}
        details = item.get("details") or {}
        if session in {"pre", "regular", "after"}:
            price = num(trade.get("p")) or num(minute.get("c")) or num(day.get("c")) or num(prev.get("c"))
        else:
            price = num(day.get("c")) or num(prev.get("c")) or num(trade.get("p"))
        if price is None or price <= 0:
            continue
        volume = num(day.get("v")) or num(prev.get("v")) or 0
        row = {
            "symbol": symbol,
            "longName": first(details, ["name"], symbol),
            "fullExchangeName": first(details, ["primary_exchange"], ""),
            "marketCap": num(first(details, ["market_cap", "marketCap"])),
            "regularMarketPrice": price,
            "regularMarketChangePercent": num(item.get("todaysChangePerc")),
            "regularMarketVolume": volume,
            "quoteType": "EQUITY",
            "_quoteSource": "MASSIVE",
        }
        if session == "pre":
            row["preMarketPrice"] = price
            row["preMarketChangePercent"] = row["regularMarketChangePercent"]
        elif session == "after":
            row["postMarketPrice"] = price
            row["postMarketChangePercent"] = row["regularMarketChangePercent"]
        out[symbol] = row
    print(f"Massive full-market snapshot: {len(out)} stocks with usable prices")
    if not out:
        raise RuntimeError("Massive snapshot returned no usable prices")
    return out


def fmp_symbol_fundamentals(symbol):
    """Fetch cached FMP fundamentals/estimates for one symbol."""
    profile = {}
    ratios = {}
    metrics = {}
    estimates = []
    statements = []
    errors = []
    calls = (
        ("ratios-ttm", {"symbol": symbol}),
        ("key-metrics-ttm", {"symbol": symbol}),
        ("analyst-estimates", {"symbol": symbol, "period": "annual", "page": 0, "limit": 5}),
        ("income-statement", {"symbol": symbol, "period": "annual", "limit": 2}),
    )
    responses = {}
    for endpoint, params in calls:
        try:
            data = fmp_request(endpoint, params)
            responses[endpoint] = data if isinstance(data, list) else []
        except Exception as exc:
            errors.append(f"{endpoint}:{type(exc).__name__}")
        finally:
            # Keep initial cache warm-up under common API requests/minute limits.
            time.sleep(0.45)
    profile = {}
    ratios = (responses.get("ratios-ttm") or [{}])[0]
    metrics = (responses.get("key-metrics-ttm") or [{}])[0]
    estimates = responses.get("analyst-estimates") or []
    statements = responses.get("income-statement") or []

    today = datetime.now(ET).date().isoformat()
    future_estimates = [
        row for row in estimates
        if isinstance(row, dict) and str(row.get("date") or "")[:10] >= today
        and num(row.get("epsAvg")) is not None and num(row.get("epsAvg")) > 0
    ]
    future_estimates.sort(key=lambda row: str(row.get("date") or "9999"))
    estimate = future_estimates[0] if future_estimates else {}
    forward_eps = num(estimate.get("epsAvg"))
    latest_statement = {}
    past_statements = [
        row for row in statements
        if isinstance(row, dict) and str(row.get("date") or "")[:10] <= today
    ]
    past_statements.sort(key=lambda row: str(row.get("date") or ""), reverse=True)
    if past_statements:
        latest_statement = past_statements[0]
    current_eps = num(first(latest_statement, ["epsDiluted", "eps"]))
    growth = None
    if forward_eps is not None and current_eps is not None and current_eps > 0:
        growth = (forward_eps / current_eps - 1) * 100
    margin = num(first(ratios, ["netProfitMarginTTM", "netProfitMargin"]))
    roe = num(first(metrics, ["returnOnEquityTTM", "returnOnEquity"]))
    debt_ratio = num(first(ratios, ["debtToEquityRatioTTM", "debtToEquityRatio"]))
    # FMP reports debt-to-equity as a ratio (e.g. 0.8); the score thresholds
    # are expressed as percent (e.g. 80), so normalize the unit.
    debt = debt_ratio * 100 if debt_ratio is not None else None
    fpe = None
    analysts = num(first(estimate, ["numAnalystsEps", "numAnalysts"]))
    return {
        "sector": first(profile, ["sector"], None),
        "industry": first(profile, ["industry"], None),
        "marketCap": num(first(profile, ["marketCap", "mktCap"])),
        "fpe": fpe,
        "forwardEps": forward_eps,
        "epsGrowthPct": growth,
        "analysts": int(analysts) if analysts is not None else None,
        "margin": margin,
        "roe": roe,
        "debt": debt,
        "fpeEstimateDate": estimate.get("date") if estimate else None,
        "source": "FMP" if any(responses.values()) else None,
        "errors": errors,
    }


def load_fmp_fundamental_cache():
    try:
        with open(FMP_CACHE_PATH, encoding="utf-8") as handle:
            payload = json.load(handle)
        if isinstance(payload, dict) and isinstance(payload.get("items"), dict):
            return payload
    except Exception:
        pass
    return {"items": {}}


def get_fmp_fundamentals(symbols):
    """Cache fundamentals for 24h so regular quote refreshes do not exhaust API quotas."""
    if not FMP_API_KEY or not symbols:
        return {}
    cache = load_fmp_fundamental_cache()
    items = cache.setdefault("items", {})
    now_epoch = time.time()
    missing = []
    for symbol in dict.fromkeys(symbols):
        entry = items.get(symbol)
        if not isinstance(entry, dict) or now_epoch - float(entry.get("fetchedAt") or 0) >= FMP_CACHE_TTL_SECONDS:
            missing.append(symbol)
    if missing:
        print(f"FMP fundamentals cache miss: {len(missing)} symbols")
        os.makedirs(os.path.dirname(FMP_CACHE_PATH), exist_ok=True)
        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = {executor.submit(fmp_symbol_fundamentals, symbol): symbol for symbol in missing}
            for future in as_completed(futures):
                symbol = futures[future]
                try:
                    result = future.result()
                    items[symbol] = {"fetchedAt": now_epoch, "data": result}
                except Exception as exc:
                    print(f"FMP fundamentals warning {symbol}: {exc}", file=sys.stderr)
        try:
            with open(FMP_CACHE_PATH, "w", encoding="utf-8") as handle:
                cache["updatedAt"] = datetime.now(ET).isoformat()
                cache["items"] = items
                json.dump(cache, handle, ensure_ascii=False, indent=2)
        except Exception as exc:
            print(f"FMP fundamentals cache write warning: {exc}", file=sys.stderr)
    out = {}
    for symbol in symbols:
        entry = items.get(symbol)
        if (
            isinstance(entry, dict)
            and isinstance(entry.get("data"), dict)
            and entry["data"].get("source")
        ):
            out[symbol] = entry["data"]
    return out



def get_yahoo_us_fallback_info(symbols):
    """Cache only the Yahoo backup fields needed by US scoring for 12 hours."""
    if not symbols:
        return {}
    cache = load_fmp_fundamental_cache()
    backup = cache.setdefault("yahooBackup", {})
    now_epoch = time.time()
    fields = (
        "sectorDisp", "sector", "industryDisp", "industry", "quoteType", "quoteTypeDisp",
        "profitMargins", "operatingMargins", "grossMargins", "netProfitMargin",
        "returnOnEquity", "debtToEquity", "debtToEquityRatio",
        "epsForward", "forwardEps", "epsNextYear", "forwardPE", "forwardPeRatio",
        "forwardPERatio", "earningsGrowth", "epsGrowth", "earningsQuarterlyGrowth",
        "numberOfAnalystOpinions", "numberOfAnalysts", "earningsTimestampStart", "earningsTimestamp",
        "marketCap",
    )
    missing = []
    for symbol in dict.fromkeys(symbols):
        entry = backup.get(symbol)
        if not isinstance(entry, dict) or now_epoch - float(entry.get("fetchedAt") or 0) >= YAHOO_US_BACKUP_CACHE_TTL_SECONDS:
            missing.append(symbol)
    if missing:
        def fetch_one(symbol):
            try:
                info = yf.Ticker(symbol).get_info() or {}
                return {
                    key: (value if isinstance(value, (str, int, float, bool)) or value is None else str(value))
                    for key, value in info.items()
                    if key in fields
                }
            except Exception as exc:
                print(f"Yahoo US backup warning {symbol}: {type(exc).__name__}", file=sys.stderr)
                return {}
        with ThreadPoolExecutor(max_workers=6) as executor:
            futures = {executor.submit(fetch_one, symbol): symbol for symbol in missing}
            for future in as_completed(futures):
                symbol = futures[future]
                try:
                    backup[symbol] = {"fetchedAt": now_epoch, "data": future.result()}
                except Exception:
                    backup[symbol] = {"fetchedAt": now_epoch, "data": {}}
        cache["updatedAt"] = datetime.now(ET).isoformat()
        try:
            os.makedirs(os.path.dirname(FMP_CACHE_PATH), exist_ok=True)
            with open(FMP_CACHE_PATH, "w", encoding="utf-8") as handle:
                json.dump(cache, handle, ensure_ascii=False, indent=2)
        except Exception as exc:
            print(f"Yahoo US backup cache write warning: {type(exc).__name__}", file=sys.stderr)
    return {
        symbol: backup[symbol].get("data", {})
        for symbol in symbols
        if isinstance(backup.get(symbol), dict)
    }


def yahoo_screen(screen_name):
    try:
        result = yf.screen(screen_name, count=250)
        quotes = result.get("quotes", []) if isinstance(result, dict) else []
        print(f"Yahoo screen {screen_name}: {len(quotes)} rows")
        return quotes
    except Exception as e:
        print(f"Yahoo screen warning {screen_name}: {e}", file=sys.stderr)
        return []

def fallback_info(symbols=None):
    rows = []
    for sym in (symbols or FALLBACK_SYMBOLS):
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
        "averageVolume": avg_volume,
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
        "quoteSource": first(q, ["_quoteSource"], "YAHOO"),
        "fundamentalSource": first(q, ["_fundamentalSource"], "QUOTE"),
        "dataQualityFlags": list(q.get("_dataQualityFlags") or []),
    }

def quality_profile(row):
    """Pure quality screen: profitability + capital efficiency + leverage.
    It intentionally does not use price trend or valuation, so the Quality
    tab stays independent from Momentum and Value.
    """
    margin = row.get("margin")
    roe = row.get("roe")
    debt = row.get("debt")

    margin_ok = margin is not None and margin >= 0.10
    roe_ok = roe is not None and roe >= 0.15
    leverage_ok = debt is not None and debt <= 200

    core_hits = sum([1 if margin_ok else 0, 1 if roe_ok else 0, 1 if leverage_ok else 0])
    quality_data = sum([1 if margin is not None else 0, 1 if roe is not None else 0, 1 if debt is not None else 0])

    if quality_data < 2:
        return False, 0

    # 100-point quality score: profitability, ROE and leverage only.
    qs = 0
    if margin is not None:
        qs += 25 if margin >= 0.15 else 18 if margin >= 0.10 else 8 if margin > 0 else 0
    if roe is not None:
        qs += 35 if roe >= 0.20 else 28 if roe >= 0.15 else 12 if roe > 0 else 0
    if debt is not None:
        qs += 40 if debt <= 50 else 32 if debt <= 100 else 22 if debt <= 200 else 5
    else:
        qs += 10

    return core_hits >= 2 and qs >= 55, round(qs)


def entry_plan(row, history=None, market="stock"):
    """Calculate a rule-based entry zone, stop and targets from ATR and levels.
    
    Entry invariants:
      * entryLow <= entryPrice <= entryHigh
      * "可考慮進場區" is emitted only when current price is inside the zone
      * pullback anchors use the highest valid support below current price
    """
    history = history or {}
    price = row.get("price")
    close = history.get("close")
    high = history.get("high")
    low = history.get("low")
    try:
        price = float(price)
    except Exception:
        price = None
    if price is None or price <= 0 or close is None or high is None or low is None or len(close) < 20:
        return {"atr": None, "atrPct": None, "support": None, "resistance": None, "entryLow": None, "entryPrice": None, "entryHigh": None, "stopLoss": None, "takeProfit1": None, "takeProfit2": None, "riskReward": None, "entryType": "資料不足", "entryStatus": "無法計算", "entryNote": "歷史高低點資料不足。"}
    c = [float(x) for x in list(close)]
    h = [float(x) for x in list(high)]
    l = [float(x) for x in list(low)]
    n = min(len(c), len(h), len(l))
    c, h, l = c[-n:], h[-n:], l[-n:]
    trs = []
    for i in range(1, n):
        prev = c[i - 1]
        trs.append(max(h[i] - l[i], abs(h[i] - prev), abs(l[i] - prev)))
    atr = sum(trs[-14:]) / max(1, min(14, len(trs))) if trs else None
    if not atr or atr <= 0:
        return {"atr": None, "atrPct": None, "support": None, "resistance": None, "entryLow": None, "entryPrice": None, "entryHigh": None, "stopLoss": None, "takeProfit1": None, "takeProfit2": None, "riskReward": None, "entryType": "資料不足", "entryStatus": "無法計算", "entryNote": "ATR 無法計算。"}

    def mv(key):
        try:
            v = float(row.get(key))
            return v if v > 0 else None
        except Exception:
            return None

    ma20, ma50, ma200 = mv("priceAvg20"), mv("priceAvg50"), mv("priceAvg200")
    prior20_high = max(h[-21:-1]) if len(h) >= 21 else max(h[:-1])
    prior20_low = min(l[-21:-1]) if len(l) >= 21 else min(l[:-1])
    recent50_high = max(h[-51:-1]) if len(h) >= 52 else max(h[:-1])
    recent50_low = min(l[-51:-1]) if len(l) >= 52 else min(l[:-1])
    try:
        vr = float(row.get("volumeRatio")) if row.get("volumeRatio") is not None else None
    except Exception:
        vr = None

    support_candidates = [
        ("20日線", ma20),
        ("50日線", ma50),
        ("200日線", ma200),
        ("近20日支撐", prior20_low),
        ("近50日支撐", recent50_low),
    ]
    valid_supports = [(name, level) for name, level in support_candidates if level and level < price]
    if valid_supports:
        anchor_name, support = max(valid_supports, key=lambda x: x[1])
    else:
        anchor_name, support = "ATR支撐", max(price - atr, 0)

    resistances = [x for x in [prior20_high, recent50_high] if x and x > price]
    resistance = min(resistances) if resistances else price + 2 * atr

    trend_ok = bool(ma200 and price > ma200 and (not ma50 or ma50 >= ma200))
    near_ma20 = bool(ma20 and abs(price - ma20) <= 0.9 * atr)
    breakout = bool(prior20_high and price >= prior20_high * 0.998 and (vr is None or vr >= 1.15))
    overextended = bool(ma20 and price > ma20 + 1.2 * atr)

    if breakout:
        entry_type = "突破"
        entry_low = prior20_high * 1.002
        entry_high = entry_low + 0.45 * atr
        entry_price = entry_low + 0.15 * atr
        stop = min(prior20_high - 0.75 * atr, entry_price - 1.25 * atr)
        note = "20日高點突破後確認；突破後拉離區間過大不追。"
    elif trend_ok and (near_ma20 or overextended):
        entry_type = "回踩20日線" if ma20 and abs(support - ma20) <= 0.25 * atr else "支撐回踩"
        base = support
        entry_low = max(base - 0.20 * atr, 0)
        entry_high = base + 0.35 * atr
        entry_price = base + 0.10 * atr
        stop = min(base - 0.55 * atr, entry_price - 1.15 * atr)
        note = "等待20日線／近端支撐回踩，不追高。"
    elif trend_ok:
        entry_type = "回踩50日線" if ma50 and abs(support - ma50) <= 0.25 * atr else "支撐回踩"
        base = support
        entry_low = max(base - 0.20 * atr, 0)
        entry_high = base + 0.30 * atr
        entry_price = base + 0.08 * atr
        stop = min(base - 0.60 * atr, entry_price - 1.20 * atr)
        note = "中期趨勢偏多，等待主要支撐／50日線回踩。"
    elif ma200 and price > ma200:
        entry_type = "深度回調"
        base = support
        entry_low = max(base - 0.20 * atr, 0)
        entry_high = base + 0.25 * atr
        entry_price = base + 0.05 * atr
        stop = min(base - 0.70 * atr, entry_price - 1.30 * atr)
        note = "等待更深支撐區才評估。"
    else:
        entry_type = "暫無訊號"
        entry_low = entry_price = entry_high = None
        stop = None
        note = "尚未形成可接受的多頭趨勢／回踩結構。"

    # Enforce a hard invariant so downstream UI and status logic can never
    # receive an entry price outside its own entry zone.
    if entry_price is not None:
        entry_low = min(float(entry_low), float(entry_price))
        entry_high = max(float(entry_high), float(entry_price))

    if entry_price is None or stop is None or stop <= 0 or entry_price <= stop:
        return {"atr": round(atr, 4), "atrPct": round(atr / price * 100, 2), "support": round(support, 4), "resistance": round(resistance, 4), "entryLow": None, "entryPrice": None, "entryHigh": None, "stopLoss": None, "takeProfit1": None, "takeProfit2": None, "riskReward": None, "entryType": entry_type, "entryStatus": "等待", "entryNote": note}

    risk_unit = entry_price - stop
    target_candidates = [x for x in [resistance, recent50_high] if x and x > entry_price + risk_unit * 1.5]
    target1 = min(target_candidates) if target_candidates else entry_price + risk_unit * 2.0
    target2 = max(entry_price + risk_unit * 3.0, target1 + risk_unit)
    rr = (target1 - entry_price) / risk_unit if risk_unit > 0 else None

    if price <= stop:
        status = "失效／跌破停損區"
    elif price < entry_low:
        status = "等待價格靠近"
    elif price <= entry_high:
        status = "可考慮進場區"
    elif price > entry_high + 0.5 * atr:
        status = "價格偏高／不追"
    else:
        status = "等待回踩"

    return {
        "atr": round(atr, 4),
        "atrPct": round(atr / price * 100, 2),
        "support": round(support, 4),
        "resistance": round(resistance, 4),
        "entryLow": round(entry_low, 4),
        "entryPrice": round(entry_price, 4),
        "entryHigh": round(entry_high, 4),
        "stopLoss": round(stop, 4),
        "takeProfit1": round(target1, 4),
        "takeProfit2": round(target2, 4),
        "riskReward": round(rr, 2) if rr is not None else None,
        "entryType": entry_type,
        "entryStatus": status,
        "entryNote": note,
    }

def stock_profiles(row):
    profiles = []
    price = row.get("price") or 0
    a50 = row.get("priceAvg50") or 0
    a200 = row.get("priceAvg200") or 0
    chg = row.get("changePct") or 0
    fpe = row.get("fpe")
    if (a200 and price > a200) or (a50 and price > a50) or chg > 0.5:
        profiles.append("momentum")
    if fpe is not None and 0 < fpe < 24:
        profiles.append("value")
    quality_ok, quality_score = quality_profile(row)
    row["qualityScore"] = quality_score
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

    if fpe is not None and 0 < fpe < 3:
        flags = row.setdefault("dataQualityFlags", [])
        flag = "Forward PE 低於3倍，需核對預估EPS"
        if flag not in flags:
            flags.append(flag)
        risk = "高" if risk == "高" else "中"
        addr("Forward PE 異常偏低，未給予低估值加分")
    elif fpe is not None and 3 <= fpe < 15:
        pts += 20
        addp(20, "FPE偏低")
    elif fpe is not None and 15 <= fpe < 22:
        pts += 16
        addp(16, "FPE合理")
    elif fpe is not None and 22 <= fpe < 30:
        pts += 10
        addp(10, "FPE中性")
    elif fpe is not None and 30 <= fpe < 45:
        pts += 4
        addp(4, "FPE偏高但仍有估值分")
        risk = "中"
        addr("FPE偏高")
    elif fpe is not None and fpe >= 100:
        risk = "高"
        addr("Forward PE 極高，需核對估值資料")
    elif fpe is not None and fpe >= 45:
        risk = "高"
        addr("FPE很高")
    elif fpe is not None and fpe <= 0:
        pts -= 10
        addn(-10, "Forward EPS為負")
        risk = "高"
        addr("FPE不具估值意義")

    if growth is not None and abs(growth) > 200:
        # Very large growth ratios are often denominator/base-effect anomalies.
        # Keep the raw value for transparency, but do not reward it in the score.
        flags = row.setdefault("dataQualityFlags", [])
        flag = "EPS成長極端值，已排除成長加分"
        if flag not in flags:
            flags.append(flag)
        risk = "高" if risk == "高" else "中"
        addr("EPS成長幅度極端，需核對估算基期")
    elif growth is not None:
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
    if roe is not None and 0.15 < roe <= 2.0:
        pts += 5
        addp(5, "ROE佳")
    elif roe is not None and roe > 2.0:
        flags = row.setdefault("dataQualityFlags", [])
        flag = "ROE超過200%，需核對股東權益基期"
        if flag not in flags:
            flags.append(flag)
        risk = "高" if risk == "高" else "中"
        addr("ROE極端值，未給予品質加分")
    if debt is not None and debt > 5000:
        pts -= 12
        addn(-12, "負債比率極端")
        risk = "高"
        flags = row.setdefault("dataQualityFlags", [])
        flag = "負債比率極端，可能是財報比例／負權益問題"
        if flag not in flags:
            flags.append(flag)
        addr("負債比率極端，禁止低風險標籤")
    elif debt is not None and debt > 250:
        pts -= 8
        addn(-8, "負債偏高")
        risk = "高" if risk == "高" else "中"
        addr("負債偏高")

    quality_missing = sum(v is None for v in (margin, roe, debt))
    if quality_missing >= 2 and risk == "低":
        risk = "中"
        addr("品質資料不足")

    pts = max(0, min(100, round(pts)))
    label = "進場候選" if pts >= 78 else "值得研究" if pts >= 68 else "觀察" if pts >= 55 else "暫不優先"

    pos_text = [x.split("｜", 1)[1] for x in positives if x.startswith("＋")][:2]
    neg_text = [x.split("｜", 1)[1] for x in positives if x.startswith("−")][:1]
    risk_text = [x.split("｜", 1)[1] for x in risks][:1]
    summary = "、".join(pos_text + neg_text) if (pos_text or neg_text) else "量化優勢目前有限"
    if risk_text:
        summary += "；注意：" + risk_text[0]
    judgement = "趨勢與基本面條件偏多" if pts >= 78 and risk == "低" else "條件偏多，但仍需確認風險" if pts >= 68 else "目前量化優勢有限"
    core_advantages = "、".join(pos_text) if pos_text else "目前量化優勢有限"
    primary_risk = "、".join(risk_text) if risk_text else "目前未偵測到明顯風險"
    reasons = ["摘要｜" + summary, "核心優勢｜" + core_advantages, "主要風險｜" + primary_risk, "判斷｜" + judgement] + positives[:3] + risks[:2]
    return pts, label, risk, reasons[:10]
def build_live():
    now = datetime.now(ET)
    day = now.date().isoformat()
    session = us_market_session(now)
    closed = session == "closed"

    merged = {}
    fmp_universe_ok = False
    massive_ok = False
    provider_warnings = []

    # Preferred path: a broad NYSE/Nasdaq universe from FMP.
    if FMP_API_KEY:
        try:
            for q in fmp_us_universe():
                sym = str(first(q, ["symbol"], "")).strip().upper()
                if sym:
                    merged[sym] = q
            fmp_universe_ok = bool(merged)
        except Exception as exc:
            provider_warnings.append("FMP screener: " + str(exc))
            print("FMP screener unavailable; switching universe source", exc, file=sys.stderr)

    # Yahoo is a universe fallback, not the preferred source when FMP works.
    if not merged:
        for screen_name in SCREENS:
            for q in yahoo_screen(screen_name):
                sym = str(first(q, ["symbol"], "")).strip().upper()
                if sym:
                    quote = dict(q)
                    quote["_quoteSource"] = "YAHOO"
                    merged[sym] = quote

    # Massive full-market snapshots take precedence for live price/change/volume.
    massive_quotes = {}
    if MASSIVE_API_KEY:
        try:
            massive_quotes = massive_us_snapshot(session)
            massive_ok = bool(massive_quotes)
            for sym, q in list(merged.items()):
                market = massive_quotes.get(sym)
                if not market:
                    continue
                for key in (
                    "regularMarketPrice", "preMarketPrice", "postMarketPrice",
                    "regularMarketChangePercent", "preMarketChangePercent",
                    "postMarketChangePercent", "regularMarketVolume",
                ):
                    if market.get(key) is not None:
                        q[key] = market[key]
                if market.get("marketCap") and not q.get("marketCap"):
                    q["marketCap"] = market["marketCap"]
                if market.get("fullExchangeName") and not q.get("fullExchangeName"):
                    q["fullExchangeName"] = market["fullExchangeName"]
                q["_quoteSource"] = "MASSIVE"
        except Exception as exc:
            provider_warnings.append("Massive snapshot: " + str(exc))
            print("Massive snapshot unavailable; retaining current quote source", exc, file=sys.stderr)

    # These seven names must always be evaluated; they are not forced into TOP20.
    missing_magnificent = [sym for sym in SEVEN_MAGNIFICENT if sym not in merged]
    if missing_magnificent:
        for q in fallback_info(missing_magnificent):
            sym = str(first(q, ["symbol"], "")).strip().upper()
            if sym and sym not in merged:
                q["_quoteSource"] = "YAHOO_BACKUP"
                merged[sym] = q

    # When Massive is configured, add snapshot names with market-cap metadata if
    # provided by the API response; do not accept unknown-cap microcaps blindly.
    if massive_ok and not fmp_universe_ok:
        for sym, q in massive_quotes.items():
            if sym in merged:
                continue
            market_cap = num(q.get("marketCap"))
            if market_cap is None or market_cap < 5_000_000_000:
                continue
            q["_quoteSource"] = "MASSIVE"
            merged[sym] = q

    normalized = []
    for q in merged.values():
        row = normalize_quote(q, session=session)
        if row:
            row["dataQualityFlags"] = list(row.get("dataQualityFlags") or [])
            normalized.append(row)

    if not normalized:
        raise RuntimeError("No eligible US stocks after provider screening and quality filters")

    # Broad universe -> liquid evaluation pool -> full fundamental/technical checks.
    # The full source universe is counted separately from the 60-stock deep-evaluation pool.
    normalized_by_symbol = {row["ticker"]: row for row in normalized}
    normalized.sort(
        key=lambda row: (
            -((row.get("averageVolume") or row.get("volume") or 0) * (row.get("price") or 0)),
            -(row.get("marketCap") or 0),
        )
    )
    ranked_pool = normalized[:50]
    selected_symbols = {row["ticker"] for row in ranked_pool}
    for symbol in SEVEN_MAGNIFICENT:
        row = normalized_by_symbol.get(symbol)
        if row and symbol not in selected_symbols:
            ranked_pool.append(row)
            selected_symbols.add(symbol)
    print(
        f"US universe source={'FMP' if fmp_universe_ok else 'Yahoo fallback'}; "
        f"quote source={'Massive' if massive_ok else ('FMP' if fmp_universe_ok else 'Yahoo fallback')}; "
        f"eligible={len(normalized)}, deeply evaluated={len(ranked_pool)}, "
        f"seven-magnificent included={sum(1 for s in SEVEN_MAGNIFICENT if s in selected_symbols)}"
    )

    symbols = [row["ticker"] for row in ranked_pool]
    # Price history remains a fallback feed for technical indicators until a
    # history endpoint is configured for the provider's licensed data plan.
    hist_pool = yahoo_history(symbols)
    for row in ranked_pool:
        h = hist_pool.get(row["ticker"])
        if not h:
            flag = "歷史K線不足或未取得"
            if flag not in row["dataQualityFlags"]:
                row["dataQualityFlags"].append(flag)
            continue
        close = h["close"]
        vol = h["volume"]
        row["priceAvg20"] = float(close.tail(20).mean()) if len(close) >= 20 else None
        row["priceAvg50"] = float(close.tail(50).mean()) if len(close) >= 50 else None
        row["priceAvg200"] = float(close.tail(200).mean()) if len(close) >= 200 else None
        avg20vol = float(vol.tail(20).mean()) if len(vol) >= 20 else None
        row["volumeRatio"] = float(row["volume"] / avg20vol) if avg20vol and avg20vol > 0 else None
        row["historicalDataSource"] = "YAHOO_HISTORY_BACKUP"

    # Primary fundamentals from FMP are cached for 24 hours. Yahoo fills only
    # fields that the configured source did not return.
    fmp_fundamentals = get_fmp_fundamentals(symbols) if FMP_API_KEY else {}
    for row in ranked_pool:
        values = fmp_fundamentals.get(row["ticker"], {})
        if values:
            row["fundamentalSource"] = "FMP"
            if values.get("sector"):
                row["sector"] = sector_label(values.get("sector"), values.get("industry"), row["ticker"], row["name"])
            for field in ("margin", "roe", "debt", "forwardEps", "epsGrowthPct", "analysts"):
                if values.get(field) is not None:
                    row[field] = values[field]
            if values.get("marketCap") and not row.get("marketCap"):
                row["marketCap"] = values["marketCap"]
            if row.get("forwardEps") and row["forwardEps"] > 0:
                row["fpe"] = (row["price"] / row["forwardEps"])
            elif values.get("fpe") is not None:
                row["fpe"] = values["fpe"]
            if values.get("fpeEstimateDate"):
                row["fpeEstimateDate"] = values["fpeEstimateDate"]

    # When FMP is disabled or incomplete, Yahoo fundamentals are fallback-only.
    fallback_symbols = []
    for row in ranked_pool:
        missing_core = (
            row.get("margin") is None or row.get("roe") is None or row.get("debt") is None
            or row.get("fpe") is None or row.get("epsGrowthPct") is None
        )
        if not FMP_API_KEY or missing_core:
            fallback_symbols.append(row["ticker"])
    info = get_yahoo_us_fallback_info(fallback_symbols) if fallback_symbols else {}
    for row in ranked_pool:
        inf = info.get(row["ticker"], {})
        if not inf:
            continue
        row["sector"] = sector_label(
            first(inf, ["sectorDisp", "sector"]),
            first(inf, ["industryDisp", "industry"]),
            row["ticker"],
            row["name"],
            first(inf, ["quoteType", "quoteTypeDisp"]),
        )
        if row.get("margin") is None:
            row["margin"] = num(first(inf, ["profitMargins", "operatingMargins", "grossMargins", "netProfitMargin"]))
        if row.get("roe") is None:
            row["roe"] = num(first(inf, ["returnOnEquity"]))
        if row.get("debt") is None:
            row["debt"] = num(first(inf, ["debtToEquity", "debtToEquityRatio"]))
        if row.get("forwardEps") is None:
            row["forwardEps"] = num(first(inf, ["epsForward", "forwardEps", "epsNextYear"]))
        if row.get("fpe") is None:
            row["fpe"] = num(first(inf, ["forwardPE", "forwardPeRatio", "forwardPERatio"]))
            if row.get("fpe") is None and row.get("forwardEps") and row["forwardEps"] > 0:
                row["fpe"] = row["price"] / row["forwardEps"]
        if row.get("epsGrowthPct") is None:
            growth = num(first(inf, ["earningsGrowth", "epsGrowth", "earningsQuarterlyGrowth"]))
            if growth is not None:
                row["epsGrowthPct"] = growth * 100 if abs(growth) < 2 else growth
        if not row.get("analysts"):
            analysts = num(first(inf, ["numberOfAnalystOpinions", "numberOfAnalysts"]))
            if analysts is not None:
                row["analysts"] = int(analysts)
        if row.get("fundamentalSource") != "FMP":
            row["fundamentalSource"] = "YAHOO_BACKUP"

    for row in ranked_pool:
        flags = row.setdefault("dataQualityFlags", [])
        if row.get("fpe") is not None and (row["fpe"] <= 0 or row["fpe"] < 3 or row["fpe"] > 100):
            flag = "Forward PE 異常範圍，已標記供核對"
            if flag not in flags:
                flags.append(flag)
        if row.get("epsGrowthPct") is not None and abs(row["epsGrowthPct"]) > 200:
            flag = "EPS成長極端值，已排除成長加分"
            if flag not in flags:
                flags.append(flag)
        if row.get("roe") is not None and row["roe"] > 2:
            flag = "ROE超過200%，需核對股東權益基期"
            if flag not in flags:
                flags.append(flag)
        if row.get("debt") is not None and row["debt"] > 5000:
            flag = "負債比率極端，可能是財報比例／負權益問題"
            if flag not in flags:
                flags.append(flag)

        sc, label, risk, reasons = score(row)
        row["score"] = sc
        row["label"] = label
        row["risk"] = risk
        row["reasons"] = reasons

    ranked_pool.sort(
        key=lambda row: (
            -row["score"],
            row.get("entryStatus") != "可考慮進場區",
            row.get("risk") == "高",
            row["fpe"] is None,
            row["fpe"] if row["fpe"] is not None else 999,
            -(row.get("marketCap") or 0),
        )
    )
    top_rows = ranked_pool[:20]
    for row in top_rows:
        h = hist_pool.get(row["ticker"])
        if h:
            close = h["close"]
            vol = h["volume"]
            avg20vol = float(vol.tail(20).mean()) if len(vol) >= 20 else None
            row["volumeRatio"] = float(row["volume"] / avg20vol) if avg20vol and avg20vol > 0 else None
            row["eventSignals"] = historical_event_signals(
                row["price"], close, row.get("volumeRatio"), row["changePct"], row.get("priceAvg50"), row.get("priceAvg200")
            )
            row.update(entry_plan(row, h, "stock"))
        else:
            row["eventSignals"] = []
            row.update(entry_plan(row, None, "stock"))
        row["profiles"] = stock_profiles(row)

        flags = row.get("dataQualityFlags") or []
        if row["entryStatus"] == "可考慮進場區" and row["score"] >= 78 and row["risk"] != "高" and not flags:
            row["label"] = "進場候選"
        elif row["risk"] == "高":
            row["label"] = "高風險觀察"
        elif row["score"] >= 78:
            row["label"] = "高分觀察"
        elif row["score"] >= 68:
            row["label"] = "值得研究"
        elif row["score"] >= 55:
            row["label"] = "觀察"
        else:
            row["label"] = "暫不優先"
        if flags:
            row["reasons"] = list(row.get("reasons") or []) + ["資料品質｜" + "、".join(flags[:3])]
        row["dataQualityFlags"] = flags

    rows = top_rows
    apply_rank_history(rows, "data.json", limit=20)
    fpe_count = sum(1 for row in rows if row.get("fpe") is not None and 0 < row["fpe"] <= 100)
    source_mode = (
        "MASSIVE+FMP" if massive_ok and fmp_universe_ok
        else "FMP+YAHOO_HISTORY" if fmp_universe_ok
        else "MASSIVE_QUOTES+YAHOO_UNIVERSE" if massive_ok
        else "YAHOO_FALLBACK"
    )
    mode_note = (
        "Massive 提供優先行情、FMP 提供候選池與基本面；技術歷史暫由 Yahoo 備援"
        if massive_ok and fmp_universe_ok else
        "FMP 提供候選池／基本面；技術歷史暫由 Yahoo 備援"
        if fmp_universe_ok else
        "Massive 行情＋Yahoo 候選池／技術歷史；建議設定 FMP_API_KEY 擴大候選池"
        if massive_ok else
        "尚未設定 Massive/FMP API Key，目前使用 Yahoo 備援與強制納入七巨頭"
    )
    return {
        "status": "MARKET_CLOSED" if closed else "LIVE",
        "asOf": now.isoformat(),
        "date": day,
        "snapshotDate": nyse_latest_session(day),
        "dataMode": "LATEST_CLOSE" if session in {"closed", "overnight"} else "INTRADAY",
        "rows": rows,
        "coverage": round(100 * fpe_count / max(1, len(rows))),
        "universeCount": len(normalized),
        "evaluatedUniverseCount": len(ranked_pool),
        "focusCount": len(rows),
        "method": "FMP US stock screener + Massive full-market snapshot + FMP fundamentals (Yahoo backup where unavailable)" if massive_ok and fmp_universe_ok else "FMP US stock screener + Yahoo technical history" if fmp_universe_ok else "Yahoo fallback screener + seven-magnificent mandatory evaluation",
        "sourceMode": source_mode,
        "sourceWarnings": provider_warnings,
        "fpeFormula": "FMP analyst EPS estimate, fallback to validated Forward PE / price-to-forward-EPS",
        "fpeCoverage": round(100 * fpe_count / max(1, len(rows))),
        "epsGrowthCoverage": round(100 * sum(1 for row in rows if row.get("epsGrowthPct") is not None and abs(row["epsGrowthPct"]) <= 200) / max(1, len(rows))),
        "analystCoverage": round(100 * sum(1 for row in rows if (row.get("analysts") or 0) > 0) / max(1, len(rows))),
        "dataQualityFlagCount": sum(len(row.get("dataQualityFlags") or []) for row in rows),
        "sevenMagnificent": {symbol: symbol in normalized_by_symbol for symbol in SEVEN_MAGNIFICENT},
        "holiday": ["Weekend"] if now.weekday() >= 5 else ["NYSE market holiday"] if closed else [],
        "note": f"{mode_note}。高分不等於可買進；進場標籤要求進場區、風險與資料品質同時通過。研究/監控工具，不構成投資建議。",
    }


TWSE_URL = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
TWSE_VAL_URL = "https://openapi.twse.com.tw/v1/exchangeReport/BWIBBU_ALL"
TPEX_URL = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes"
TWSE_COMPANY_URL = "https://openapi.twse.com.tw/v1/opendata/t187ap03_L"
TPEX_COMPANY_URL = "https://www.tpex.org.tw/openapi/v1/mopsfin_t187ap03_O"

def taiwan_company_name_map():
    """Load official Chinese company abbreviations so provider English names never leak into the TW UI."""
    names = {}
    sources = (
        (
            "TWSE",
            TWSE_COMPANY_URL,
            ["公司代號", "證券代號", "SecuritiesCompanyCode", "Code"],
            ["公司簡稱", "公司名稱", "證券名稱", "CompanyAbbreviation", "CompanyName", "Name"],
        ),
        (
            "TPEx",
            TPEX_COMPANY_URL,
            ["SecuritiesCompanyCode", "公司代號", "證券代號", "Code"],
            ["CompanyAbbreviation", "CompanyName", "公司簡稱", "公司名稱", "證券名稱", "Name"],
        ),
    )
    for exchange, url, code_keys, name_keys in sources:
        try:
            response = S.get(url, timeout=15, headers={
                "Accept": "application/json",
                "Accept-Encoding": "identity",
                "Connection": "close",
                "User-Agent": "Mozilla/5.0 (compatible; AlphaWatch/1.0)",
            })
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, list):
                raise ValueError("expected a JSON list")
            count = 0
            for item in payload:
                if not isinstance(item, dict):
                    continue
                code = str(first(item, code_keys, "")).strip()
                name = str(first(item, name_keys, "")).strip()
                name = re.sub(r"\\s+", " ", name).strip()
                if code.isdigit() and name and name not in {"-", "－", "--", "—"}:
                    names[(code, exchange)] = name
                    count += 1
            print(f"{exchange} Chinese company-name map: {count} rows")
        except Exception as e:
            print(f"{exchange} company-name map warning", e, file=sys.stderr)
    return names

def tw_num(v):
    try:
        x = str(v).strip().replace(",", "")
        if x in {"", "-", "--", "---", "N/A", "null", "nan", "NaN", "None"}:
            return None
        value = float(x)
        if value != value or value in (float("inf"), float("-inf")):
            return None
        return value
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

def yahoo_taiwan_snapshot(max_rows=1000):
    """Fallback universe feed when the exchange OpenAPI is empty or blocked."""
    try:
        from yfinance import EquityQuery

        query = EquityQuery("and", [
            EquityQuery("eq", ["region", "tw"]),
            EquityQuery("is-in", ["exchange", "TAI", "TWO"]),
        ])
        quotes_by_symbol = {}
        dates = []
        offset = 0
        page_size = 250
        while offset < max_rows:
            result = yf.screen(
                query,
                offset=offset,
                size=page_size,
                sortField="dayvolume",
                sortAsc=False,
            )
            quotes = result.get("quotes", []) if isinstance(result, dict) else []
            print(f"Yahoo Taiwan screener offset={offset}: {len(quotes)} rows")
            if not quotes:
                break
            for q in quotes:
                symbol = str(first(q, ["symbol"], "")).strip().upper()
                if symbol.endswith(".TWO"):
                    code, exchange = symbol[:-4], "TPEx"
                elif symbol.endswith(".TW"):
                    code, exchange = symbol[:-3], "TWSE"
                else:
                    continue

                price = tw_num(first(q, [
                    "regularMarketPrice", "intradayprice", "price",
                    "regularMarketPreviousClose",
                ]))
                volume = tw_num(first(q, [
                    "regularMarketVolume", "dayvolume", "eodvolume", "volume",
                ])) or 0
                if not code.isdigit() or len(code) < 4 or price is None or price < 10 or volume < 300_000:
                    continue

                change_pct = tw_num(first(q, [
                    "regularMarketChangePercent", "percentchange", "changePercent",
                ]))
                row = {
                    "ticker": code,
                    "yahoo": symbol,
                    "name": str(first(q, ["shortName", "longName", "displayName", "name"], code)).strip(),
                    "exchange": exchange,
                    "price": price,
                    "changePct": change_pct,
                    "volume": volume,
                    # Yahoo screener does not consistently provide turnover;
                    # price x shares is used only as a relative liquidity proxy.
                    "turnover": price * volume,
                    "pe": None,
                }
                previous = quotes_by_symbol.get(symbol)
                if previous is None or row["volume"] > previous["volume"]:
                    quotes_by_symbol[symbol] = row

                timestamp = tw_num(first(q, ["regularMarketTime", "marketTime"]))
                if timestamp:
                    if timestamp > 10_000_000_000:
                        timestamp /= 1000
                    try:
                        dates.append(datetime.fromtimestamp(timestamp, ZoneInfo("Asia/Taipei")).date().isoformat())
                    except (OverflowError, OSError, ValueError):
                        pass

            if len(quotes) < page_size:
                break
            offset += len(quotes)

        rows = list(quotes_by_symbol.values())
        snapshot_date = max(dates) if dates else None
        if not rows:
            raise RuntimeError("Yahoo Finance Taiwan screener returned no liquid TWSE/TPEx shares")

        if not snapshot_date:
            try:
                history = yf.Ticker(rows[0]["yahoo"]).history(period="5d", auto_adjust=False)
                if history is not None and not history.empty:
                    snapshot_date = history.index[-1].date().isoformat()
            except Exception as e:
                print("Yahoo Taiwan fallback date warning", e, file=sys.stderr)

        print(f"Yahoo Taiwan fallback produced {len(rows)} liquid rows; snapshot={snapshot_date}")
        return rows, snapshot_date
    except Exception as e:
        print("Yahoo Taiwan screener warning", e, file=sys.stderr)
        raise RuntimeError(f"Yahoo Finance Taiwan fallback unavailable: {e}") from e


def taiwan_snapshot():
    try:
        twse = fetch_json(TWSE_URL)
        if not isinstance(twse, list) or not twse:
            print("TWSE daily snapshot warning: endpoint returned no rows", file=sys.stderr)
            twse = []
    except Exception as e:
        print("TWSE daily snapshot warning", e, file=sys.stderr)
        twse = []
    try:
        tpex = fetch_json(TPEX_URL)
        if not isinstance(tpex, list) or not tpex:
            print("TPEx daily snapshot warning: endpoint returned no rows", file=sys.stderr)
            tpex = []
    except Exception as e:
        print("TPEx daily snapshot warning", e, file=sys.stderr)
        tpex = []

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

    # Mark the provider tier explicitly so a backup feed can never displace a
    # usable exchange-native candidate during the first-stage screening.
    for row in rows:
        row["_feedSource"] = "OFFICIAL"
    official_rows = list(rows)
    official_has_twse = any(row.get("exchange") == "TWSE" for row in official_rows)
    official_has_tpex = any(row.get("exchange") == "TPEx" for row in official_rows)
    used_yahoo_fallback = False
    yahoo_snapshot_date = None

    # A non-empty JSON response is not enough: require usable candidates from
    # both boards. Cloud runners may receive HTTP 403 or empty payloads from TPEx.
    if not official_has_twse or not official_has_tpex:
        missing = []
        if not official_has_twse:
            missing.append("TWSE")
        if not official_has_tpex:
            missing.append("TPEx")
        print(f"Official Taiwan feed incomplete ({', '.join(missing)} missing); trying Yahoo Finance screener", file=sys.stderr)
        missing_exchanges = set(missing)
        company_names = taiwan_company_name_map()
        for row in rows:
            chinese_name = company_names.get((row["ticker"], row["exchange"]))
            if chinese_name:
                row["name"] = chinese_name

        yahoo_rows, yahoo_snapshot_date = yahoo_taiwan_snapshot()
        by_key = {(row["ticker"], row["exchange"]): row for row in rows}
        for row in yahoo_rows:
            key = (row["ticker"], row["exchange"])
            # Only use Yahoo to backfill an exchange whose official feed failed.
            # Do not mix a Yahoo volume/turnover proxy into an exchange already
            # covered by official market data.
            if row["exchange"] not in missing_exchanges:
                continue
            chinese_name = company_names.get(key)
            if chinese_name:
                row["name"] = chinese_name
            # Official exchange values win when both providers cover a symbol.
            if key not in by_key:
                row["_feedSource"] = "YAHOO_FALLBACK"
                by_key[key] = row
        rows = list(by_key.values())
        used_yahoo_fallback = True

    has_twse = any(row.get("exchange") == "TWSE" for row in rows)
    has_tpex = any(row.get("exchange") == "TPEx" for row in rows)
    if not rows or not has_twse or not has_tpex:
        raise RuntimeError(
            f"Incomplete Taiwan universe after fallback: TWSE rows={sum(r.get('exchange') == 'TWSE' for r in rows)}, "
            f"TPEx rows={sum(r.get('exchange') == 'TPEx' for r in rows)}"
        )

    dates = []
    for x in twse or []:
        d = roc_date(first(x, ["日期", "Date"]))
        if d:
            dates.append(d)
    for x in tpex or []:
        d = roc_date(first(x, ["Date", "日期"]))
        if d:
            dates.append(d)
    if yahoo_snapshot_date:
        dates.append(yahoo_snapshot_date)
    snapshot_date = max(dates) if dates else None
    if used_yahoo_fallback:
        source_mode = "MIXED" if official_rows else "YAHOO_FALLBACK"
    else:
        source_mode = "OFFICIAL"
    print(f"Taiwan universe: TWSE={sum(r.get('exchange') == 'TWSE' for r in rows)}, TPEx={sum(r.get('exchange') == 'TPEx' for r in rows)}, source={source_mode}")
    return rows, snapshot_date, source_mode

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
                close = as_series(raw["Close"], sym).dropna()
                volume = as_series(raw["Volume"], sym).fillna(0)
            else:
                close = as_series(raw[(sym, "Close")], sym).dropna()
                volume = as_series(raw[(sym, "Volume")], sym).fillna(0)
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

def yahoo_history(symbols, retry_missing=True):
    if not symbols:
        return {}
    out = {}
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
        for sym in symbols:
            try:
                if len(symbols) == 1:
                    close = as_series(raw["Close"], sym).dropna()
                    volume = as_series(raw["Volume"], sym).fillna(0)
                    high = as_series(raw["High"], sym).reindex(close.index).dropna()
                    low = as_series(raw["Low"], sym).reindex(close.index).dropna()
                else:
                    close = as_series(raw[(sym, "Close")], sym).dropna()
                    volume = as_series(raw[(sym, "Volume")], sym).fillna(0)
                    high = as_series(raw[(sym, "High")], sym).reindex(close.index).dropna()
                    low = as_series(raw[(sym, "Low")], sym).reindex(close.index).dropna()
                aligned = close.index.intersection(high.index).intersection(low.index)
                close = close.reindex(aligned).dropna()
                volume = volume.reindex(close.index).fillna(0)
                high = high.reindex(close.index).dropna()
                low = low.reindex(close.index).dropna()
                if len(close) >= 60 and len(high) == len(close) and len(low) == len(close):
                    out[sym] = {"close": close, "high": high, "low": low, "volume": volume}
            except Exception:
                continue
    except Exception as e:
        print("Yahoo history bulk warning", e, file=sys.stderr)

    # Retry missing symbols one-by-one. Bulk yfinance occasionally fails
    if retry_missing:
        # individual tickers with transient cache/database-lock errors.
        for sym in symbols:
            if sym in out:
                continue
            try:
                raw = yf.download(
                    tickers=sym,
                    period="1y",
                    interval="1d",
                    auto_adjust=False,
                    progress=False,
                    threads=False,
                    group_by="column",
                )
                if raw is None or len(raw) == 0:
                    continue
                close = as_series(raw["Close"], sym).dropna()
                volume = as_series(raw["Volume"], sym).fillna(0)
                high = as_series(raw["High"], sym).reindex(close.index).dropna()
                low = as_series(raw["Low"], sym).reindex(close.index).dropna()
                aligned = close.index.intersection(high.index).intersection(low.index)
                close = close.reindex(aligned).dropna()
                volume = volume.reindex(close.index).fillna(0)
                high = high.reindex(close.index).dropna()
                low = low.reindex(close.index).dropna()
                if len(close) >= 60 and len(high) == len(close) and len(low) == len(close):
                    out[sym] = {"close": close, "high": high, "low": low, "volume": volume}
            except Exception as e:
                print(f"Yahoo history retry warning {sym}: {e}", file=sys.stderr)

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

def yahoo_earnings_estimates(symbols):
    """Fetch forward EPS consensus estimates for a batch of symbols.
    
    yfinance exposes 0y/+1y consensus EPS, analyst count, low/high and growth.
    We keep this separate from get_info() because analyst-estimate coverage is
    materially different from quote/info coverage.
    """
    out = {}
    for sym in symbols:
        try:
            ticker = yf.Ticker(sym)
            df = ticker.get_earnings_estimate()
            if df is None or getattr(df, "empty", True):
                continue
            row = None
            for key in ("+1y", "+1Y", 1):
                try:
                    if key in df.index:
                        row = df.loc[key]
                        break
                except Exception:
                    pass
            if row is None:
                # Defensive fallback for index representations such as strings.
                for idx in getattr(df, "index", []):
                    if str(idx).strip().lower() == "+1y":
                        row = df.loc[idx]
                        break
            if row is None:
                continue

            def cell(name):
                try:
                    v = row.get(name)
                except Exception:
                    try:
                        v = row[name]
                    except Exception:
                        v = None
                return v

            out[sym] = {
                "numberOfAnalysts": int(tw_num(cell("numberOfAnalysts")) or 0),
                "avg": tw_num(cell("avg")),
                "low": tw_num(cell("low")),
                "high": tw_num(cell("high")),
                "yearAgoEps": tw_num(cell("yearAgoEps")),
                "growth": tw_num(cell("growth")),
            }
        except Exception as e:
            print("Taiwan Yahoo earnings estimate warning", sym, e, file=sys.stderr)
    return out

def taiwan_score(row):
    price = row["price"] or 0
    chg = row["changePct"] or 0
    a20 = row.get("priceAvg20") or 0
    a50 = row.get("priceAvg50") or 0
    a200 = row.get("priceAvg200") or 0
    vr = row.get("volumeRatio")
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
            raw_points = 20
            valuation_label = "估值偏低"
        elif valuation < 22:
            raw_points = 15
            valuation_label = "估值合理"
        elif valuation < 30:
            raw_points = 8
            valuation_label = "估值中性"
        elif valuation < 45:
            raw_points = 3
            valuation_label = "估值偏高但仍有估值分"
            risk = "中"
            addr("估值偏高")
        else:
            raw_points = 0
            valuation_label = "估值很高"
            risk = "高"
            addr("估值很高")

        # Do not give a low-confidence PE fallback the same weight as
        # consensus/direct Forward P/E. Confidence is produced by the
        # valuation enrichment layer (100 direct FPE, 65-95 consensus FPE,
        # 50 trailing/official PE fallback).
        confidence = int(row.get("valuationConfidence") or 50)
        adjusted_points = round(raw_points * max(0, min(100, confidence)) / 100)
        if adjusted_points:
            pts += adjusted_points
            addp(adjusted_points, valuation_label + (f" · 信心{confidence}%" if confidence < 100 else ""))
        elif raw_points and confidence < 100:
            addr(f"估值資料信心偏低（{confidence}%）")

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
    core_advantages = "、".join(pos_text) if pos_text else "目前量化優勢有限"
    primary_risk = "、".join(risk_text) if risk_text else "目前未偵測到明顯風險"
    reasons = ["摘要｜" + summary, "核心優勢｜" + core_advantages, "主要風險｜" + primary_risk, "判斷｜" + judgement] + positives[:3] + risks[:2]
    return pts, label, risk, reasons[:10]
def build_taiwan_live():
    now = datetime.now(ZoneInfo("Asia/Taipei"))
    day = now.date().isoformat()
    try:
        snapshot, snapshot_date, snapshot_source = taiwan_snapshot()
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
    # Official TWSE/TPEx rows are the primary candidate pool. Yahoo fallback
    # rows are only used to fill the pool if official sources yielded fewer
    # than 160 candidates; otherwise a proxy turnover value cannot change the
    # composition of the primary screening universe.
    official_candidates = sorted(
        (x for x in snapshot if x.get("_feedSource") == "OFFICIAL"),
        key=lambda x: (x.get("turnover") or 0),
        reverse=True,
    )
    fallback_candidates = sorted(
        (x for x in snapshot if x.get("_feedSource") != "OFFICIAL"),
        key=lambda x: (x.get("turnover") or 0),
        reverse=True,
    )
    shortlist = official_candidates[:160]
    if len(shortlist) < 160:
        selected = {(x["ticker"], x["exchange"]) for x in shortlist}
        for candidate in fallback_candidates:
            key = (candidate["ticker"], candidate["exchange"])
            if key not in selected:
                shortlist.append(candidate)
                selected.add(key)
            if len(shortlist) >= 160:
                break
    print(
        f"Taiwan screening pool: official={min(len(official_candidates), 160)}, "
        f"fallback={max(0, len(shortlist) - min(len(official_candidates), 160))}, "
        f"total={len(shortlist)}"
    )
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
    # Enrich a wider candidate pool before final scoring so valuation and EPS
    # coverage are not limited to the pre-existing TOP 10.
    fundamental_pool = enriched[:60]
    info = yahoo_info([x["yahoo"] for x in fundamental_pool])
    estimates = yahoo_earnings_estimates([x["yahoo"] for x in fundamental_pool])

    for row in enriched:
        inf = info.get(row["yahoo"], {})
        est = estimates.get(row["yahoo"], {})

        # Prefer direct Yahoo Forward P/E. Otherwise derive FPE from the
        # strongest available forward EPS estimate, starting with +1y consensus.
        direct_fpe = tw_num(first(inf, ["forwardPE", "forwardPeRatio", "forwardPERatio"]))
        trailing_pe = tw_num(first(inf, ["trailingPE", "trailingPeRatio", "trailingPERatio"]))
        info_fwd_eps = tw_num(first(inf, ["epsForward", "forwardEps", "epsNextYear"]))
        consensus_fwd_eps = tw_num(est.get("avg"))
        fwd_eps = info_fwd_eps or consensus_fwd_eps
        trailing_eps = tw_num(first(inf, ["trailingEps", "epsTrailingTwelveMonths"]))
        current_eps = tw_num(first(inf, ["epsCurrentYear", "currentYearEps"]))
        growth = tw_num(first(inf, ["earningsGrowth", "earningsQuarterlyGrowth", "epsGrowth"]))

        if growth is not None and abs(growth) < 2:
            growth *= 100

        fpe = direct_fpe
        fpe_source = "YAHOO_FORWARD_PE" if direct_fpe is not None else None
        forward_eps_source = "YAHOO_INFO" if info_fwd_eps is not None else None
        forward_eps_analysts = int(tw_num(first(inf, ["numberOfAnalystOpinions", "numberOfAnalysts"])) or 0)

        if fpe is None and consensus_fwd_eps and consensus_fwd_eps > 0:
            fpe = row["price"] / consensus_fwd_eps
            fpe_source = "CONSENSUS_1Y_EPS"
            forward_eps_source = "YAHOO_EARNINGS_ESTIMATE_1Y"
            forward_eps_analysts = int(est.get("numberOfAnalysts") or 0)

        valuation = fpe if fpe is not None else trailing_pe if trailing_pe is not None and trailing_pe > 0 else row.get("pe")
        valuation_source = "FPE" if fpe is not None else "PE" if valuation is not None else None
        if valuation is None and trailing_eps and trailing_eps > 0:
            valuation = row["price"] / trailing_eps
            valuation_source = "PE"

        if growth is None:
            est_growth = tw_num(est.get("growth"))
            if est_growth is not None:
                growth = est_growth * 100 if abs(est_growth) < 2 else est_growth
        if growth is None:
            baseline_eps = current_eps if current_eps and current_eps > 0 else trailing_eps
            if fwd_eps and fwd_eps > 0 and baseline_eps and baseline_eps > 0:
                growth = (fwd_eps / baseline_eps - 1) * 100

        # Estimate-quality metadata used by the dashboard.
        valuation_confidence = 0
        if direct_fpe is not None:
            valuation_confidence = 100
        elif consensus_fwd_eps and consensus_fwd_eps > 0:
            n_analysts = int(est.get("numberOfAnalysts") or 0)
            valuation_confidence = 95 if n_analysts >= 5 else 85 if n_analysts >= 3 else 75 if n_analysts >= 1 else 65
        elif trailing_pe is not None or row.get("pe") is not None:
            valuation_confidence = 50

        row["fpe"] = fpe
        row["valuation"] = valuation
        row["valuationSource"] = valuation_source
        row["fpeSource"] = fpe_source
        row["valuationConfidence"] = valuation_confidence
        row["forwardEps"] = fwd_eps
        row["forwardEpsSource"] = forward_eps_source
        row["forwardEpsAnalysts"] = forward_eps_analysts
        row["forwardEpsLow"] = tw_num(est.get("low"))
        row["forwardEpsHigh"] = tw_num(est.get("high"))
        row["epsGrowthPct"] = growth
        row["analysts"] = forward_eps_analysts
        row["earningsDate"] = as_date(first(inf, ["earningsTimestampStart", "earningsTimestamp"]))
        row["margin"] = tw_num(first(inf, ["profitMargins", "operatingMargins", "grossMargins"]))
        row["roe"] = tw_num(first(inf, ["returnOnEquity"]))
        row["debt"] = tw_num(first(inf, ["debtToEquity"]))
        row["quality"] = row["roe"] if row["roe"] is not None else row["margin"]
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
        quality_ok, quality_score = quality_profile(row)
        row["qualityScore"] = quality_score
        if quality_ok:
            row["profiles"].append("quality")
        if not row["profiles"]:
            row["profiles"] = ["momentum"]
        row["eventSignals"] = historical_event_signals(row["price"], row.get("_close_series"), row.get("volumeRatio"), row.get("changePct"), row.get("priceAvg50"), row.get("priceAvg200"))
        row.update(entry_plan(row, hist.get(row["yahoo"], {}), "stock"))
        row.pop("_close_series", None)
        row.pop("_feedSource", None)
        row.pop("yahoo", None)
        row.pop("turnover", None)

    enriched.sort(key=lambda r: (-r["score"], -(r.get("volumeRatio") or 0), r.get("valuation") is None, r.get("valuation") or 999))
    rows = enriched[:20]
    apply_rank_history(rows, "taiwan_data.json", limit=20)
    valuation_count = sum(1 for r in rows if r.get("valuation") is not None)
    fpe_count = sum(1 for r in rows if r.get("fpe") is not None)
    pe_count = sum(1 for r in rows if r.get("valuationSource") == "PE")
    eps_growth_count = sum(1 for r in rows if r.get("epsGrowthPct") is not None)

    if using_intraday:
        status = "LIVE"
        note = "盤中使用當日即時行情；依趨勢、動能、量能、FPE/PE、EPS成長篩選進場候選；研究/監控工具，不構成投資建議。"
        data_mode = "INTRADAY"
    else:
        status = "MARKET_CLOSED"
        note = f"目前非台股即時交易時段；顯示最近交易日 {snapshot_date} 收盤 TOP 20，並維持量化排序，研究/監控工具，不構成投資建議。"
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
        "fpeCoverage": round(100 * fpe_count / max(1, len(rows))),
        "peCoverage": round(100 * pe_count / max(1, len(rows))),
        "epsGrowthCoverage": round(100 * eps_growth_count / max(1, len(rows))),
        "directFpeCoverage": round(100 * sum(1 for r in rows if r.get("fpeSource") == "YAHOO_FORWARD_PE") / max(1, len(rows))),
        "derivedFpeCoverage": round(100 * sum(1 for r in rows if r.get("fpeSource") == "CONSENSUS_1Y_EPS") / max(1, len(rows))),
        "valuationFallbackCoverage": round(100 * sum(1 for r in rows if r.get("valuationSource") == "PE") / max(1, len(rows))),
        "universeCount": len(enriched),
        "focusCount": len(rows),
        "method": ("TWSE + TPEx daily snapshot + Yahoo Finance 5-minute intraday/technical/valuation data" if snapshot_source == "OFFICIAL" else "TWSE/TPEx official snapshots supplemented by Yahoo Finance screener + Yahoo Finance technical/valuation data" if snapshot_source == "MIXED" else "Yahoo Finance Taiwan equity screener + Yahoo Finance technical/valuation data (official exchange snapshots unavailable)"),
        "sourceMode": snapshot_source,
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
    "WLD": "數位身分／AI",
    "INJ": "DeFi／金融基礎設施",
}

CRYPTO_EXCLUDE = (
    "UP", "DOWN", "BULL", "BEAR", "2L", "2S", "3L", "3S", "5L", "5S", "ETF",
)
CRYPTO_STABLECOINS = {
    "USDT", "USDC", "FDUSD", "TUSD", "DAI", "USDE", "BUSD", "RLUSD",
    "USDS", "USDP", "GUSD", "PYUSD", "USD1",
}

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
    "WLD": "AI",
    "DOGE": "Meme",
    "SHIB": "Meme",
    "PEPE": "Meme",
}

def crypto_group(symbol, tags=None, cmc_rank=None):
    """Map coins into stable, mutually intelligible dashboard groups.
    
    Explicit symbol mappings win first; CMC tags are matched by exact slug
    instead of substring matching so coins like ZEC cannot be misclassified
    as AI just because a tag happens to contain an 'ai' substring.
    """
    base = symbol.replace("USDT", "").upper()
    if base in CRYPTO_STABLECOINS:
        return "Stablecoin"
    if base in CRYPTO_GROUP:
        return CRYPTO_GROUP[base]

    slugs = {
        str(t.get("slug") if isinstance(t, dict) else t or "").strip().lower()
        for t in (tags or [])
    }
    if slugs.intersection({"memes", "meme", "dog-themed-coins", "dog-themed"}):
        return "Meme"
    if slugs.intersection({"artificial-intelligence", "ai-big-data", "ai-agents"}):
        return "AI"
    if slugs.intersection({"decentralized-finance", "defi", "decentralized-exchange"}):
        return "DeFi"
    if slugs.intersection({"smart-contract-platform", "layer-1", "layer-2"}):
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

CRYPTO_API_FALLBACK = "https://api.binance.com/api/v3"

def crypto_get(path, params=None):
    last_error = None
    for base in (CRYPTO_API, CRYPTO_API_FALLBACK):
        try:
            r = S.get(base + path, params=params or {}, timeout=20)
            r.raise_for_status()
            return r.json()
        except Exception as e:
            last_error = e
    print(f"Crypto API warning {path}: {last_error}", file=sys.stderr)
    return None

def cmc_listings(limit=250):
    last_error = None
    for attempt in range(4):
        try:
            r = S.get(
                CMC_API + "/cryptocurrency/listings/latest",
                params={"start": 1, "limit": limit, "convert": "USD"},
                timeout=30,
            )
            r.raise_for_status()
            payload = r.json()
            data = payload.get("data", []) if isinstance(payload, dict) else []
            if data:
                return data
        except Exception as e:
            last_error = e
            status = getattr(getattr(e, "response", None), "status_code", None)
            if status == 429:
                wait = 5 * (2 ** attempt)
                print(f"CMC rate limited; retrying in {wait}s", file=sys.stderr)
                time.sleep(wait)
            else:
                time.sleep(2 * (attempt + 1))
    print(f"CMC warning: {last_error}", file=sys.stderr)
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
    params = {
        "symbol": symbol,
        "interval": "1d",
        "limit": limit,
    }
    data = crypto_get("/klines", params)
    # Retry one short candle response once; some public API edges temporarily
    # return a partial payload even for established spot pairs.
    if isinstance(data, list) and len(data) < 20:
        time.sleep(1)
        data = crypto_get("/klines", params)
    if not isinstance(data, list):
        kind = type(data).__name__
        print(f"Crypto kline invalid response {symbol}: {kind}", file=sys.stderr)
        return None
    if len(data) < 20:
        print(f"Crypto kline history too short {symbol}: {len(data)} candles; need at least 20", file=sys.stderr)
        return None
    closes = []
    highs = []
    lows = []
    quote_vols = []
    for k in data:
        try:
            highs.append(float(k[2]))
            lows.append(float(k[3]))
            closes.append(float(k[4]))
            quote_vols.append(float(k[7]))
        except Exception:
            pass
    if len(closes) < 20 or len(highs) != len(closes) or len(lows) != len(closes):
        print(
            f"Crypto kline parse incomplete {symbol}: closes={len(closes)}, highs={len(highs)}, lows={len(lows)}",
            file=sys.stderr,
        )
        return None
    return {"close": closes, "high": highs, "low": lows, "quoteVolume": quote_vols}

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

    if vr is None:
        addr("歷史量能資料不足")
    elif vr >= 2:
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

    if row.get("historyAvailable") is False:
        pts -= 10
        addn(10, "歷史K線缺失，評分信心降低")
        addr("歷史K線資料不足，均線與入場區無法完整計算")

    pts = max(0, min(100, round(pts)))
    label = "進場候選" if pts >= 85 else "優先研究" if pts >= 78 else "值得觀察" if pts >= 68 else "中性" if pts >= 58 else "偏弱"
    pos_text = [x.split("｜", 1)[1] for x in positives if x.startswith("＋")][:2]
    neg_text = [x.split("｜", 1)[1] for x in positives if x.startswith("−")][:1]
    risk_text = [x.split("｜", 1)[1] for x in risks][:1]
    summary = "、".join(pos_text + neg_text) if (pos_text or neg_text) else "量化優勢目前有限"
    if risk_text:
        summary += "；注意：" + risk_text[0]
    judgement = "趨勢與24H動能偏多" if pts >= 78 and risk == "低" else "動能尚可，但波動與量能需留意" if pts >= 68 else "目前量化優勢有限"
    core_advantages = "、".join(pos_text) if pos_text else "目前量化優勢有限"
    primary_risk = "、".join(risk_text) if risk_text else "目前未偵測到明顯風險"
    reasons = ["摘要｜" + summary, "核心優勢｜" + core_advantages, "主要風險｜" + primary_risk, "判斷｜" + judgement] + positives[:3] + risks[:2]
    return pts, label, risk, reasons[:10]
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
        if not base or base in CRYPTO_STABLECOINS:
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
        if price <= 0 or qv < 25_000_000:
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
    print(
        f"Crypto candidate diagnostics: ticker_pairs={len(tickers)}, "
        f"eligible_spot_pairs={len(candidates)}, shortlist={len(shortlist)}"
    )

    enriched = []
    history_count = 0
    history_missing = 0
    for row in shortlist:
        h = crypto_klines(row["symbol"], limit=210)
        if h:
            history_count += 1
            close = h["close"]
            qv = h["quoteVolume"]
            row["priceAvg20"] = sum(close[-20:]) / 20 if len(close) >= 20 else None
            row["priceAvg50"] = sum(close[-50:]) / 50 if len(close) >= 50 else None
            row["priceAvg200"] = sum(close[-200:]) / 200 if len(close) >= 200 else None
            avg20 = sum(qv[-20:]) / 20 if len(qv) >= 20 else None
            row["volumeRatio"] = row["quoteVolume24h"] / avg20 if avg20 and avg20 > 0 else None
            row["historyAvailable"] = True
            row["historyNote"] = None
            row["_close_series"] = close
            row.update(entry_plan(row, h, "crypto"))
            row["eventSignals"] = historical_event_signals(
                row["price"], row.get("_close_series"), row.get("volumeRatio"),
                row.get("changePct"), row.get("priceAvg50"), row.get("priceAvg200")
            )
        else:
            # Do not silently drop a liquid, live-priced coin because historical
            # candles are temporarily missing. Include it transparently, with a
            # lower-confidence score and no fabricated MA/ATR/entry price.
            history_missing += 1
            row["historyAvailable"] = False
            row["historyNote"] = "歷史K線不足；暫無法計算均線、ATR 與進場區"
            row["priceAvg20"] = None
            row["priceAvg50"] = None
            row["priceAvg200"] = None
            row["volumeRatio"] = None
            row["eventSignals"] = ["歷史K線資料不足"]
            row.update(entry_plan(row, None, "crypto"))

        sc, label, risk, reasons = crypto_score(row)
        row["score"] = sc
        row["label"] = label
        row["risk"] = risk
        if not h:
            reasons.append("資料品質｜" + row["historyNote"])
        row["reasons"] = reasons[:10]
        row.pop("_close_series", None)
        row.pop("symbol", None)
        enriched.append(row)

    print(
        f"Crypto history diagnostics: valid_history={history_count}, "
        f"missing_history={history_missing}, ranked_rows={len(enriched)}"
    )
    enriched.sort(key=lambda r: (-r["score"], -(r.get("quoteVolume24h") or 0)))
    rows = enriched[:20]
    apply_rank_history(rows, "crypto_data.json", limit=20)

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
        "universeCount": len(candidates),
        "candidateCount": len(candidates),
        "shortlistCount": len(shortlist),
        "historyCount": history_count,
        "historyMissingCount": history_missing,
        "historyCoverage": round(100 * history_count / max(1, len(enriched))),
        "focusCount": len(rows),
        "categoryStats": category_stats,
        "cmcCoverage": round(100 * sum(1 for r in enriched if r.get("cmcRank") is not None) / max(1, len(enriched))),
        "method": "Binance public spot market data + CoinMarketCap market reference",
        "fpeFormula": "不適用；加密貨幣不使用 FPE / Forward EPS",
        "holiday": [],
        "note": "24/7 加密貨幣市場；候選池使用 Binance 現貨 USDT 對且 24H 成交額至少 2,500 萬 USDT，缺歷史 K 線的幣種會保留並明確標示資料不足，不會靜默消失。",
    }


def update_us_signal_history(payload):
    """Archive one end-of-day TOP20 cohort and evaluate forward close-to-close returns."""
    archive_path = "us_signal_history.json"
    summary_path = "us_backtest.json"
    try:
        try:
            with open(archive_path, encoding="utf-8") as handle:
                archive = json.load(handle)
        except Exception:
            archive = {"records": []}
        if not isinstance(archive, dict) or not isinstance(archive.get("records"), list):
            archive = {"records": []}
        records = archive["records"]
        new_record = False
        snapshot_date = str(payload.get("snapshotDate") or "")[:10]
        if (
            payload.get("dataMode") == "LATEST_CLOSE"
            and payload.get("status") != "DATA_ERROR"
            and snapshot_date
            and isinstance(payload.get("rows"), list)
            and payload.get("rows")
            and not any(str(record.get("date") or "")[:10] == snapshot_date for record in records if isinstance(record, dict))
        ):
            signals = []
            for row in payload.get("rows", [])[:20]:
                if not isinstance(row, dict) or not row.get("ticker") or row.get("price") is None:
                    continue
                signals.append({
                    "ticker": str(row["ticker"]).upper(),
                    "score": row.get("score"),
                    "label": row.get("label"),
                    "risk": row.get("risk"),
                    "entryStatus": row.get("entryStatus"),
                    "entryType": row.get("entryType"),
                    "signalPrice": row.get("price"),
                    "entryLow": row.get("entryLow"),
                    "entryPrice": row.get("entryPrice"),
                    "entryHigh": row.get("entryHigh"),
                    "stopLoss": row.get("stopLoss"),
                    "takeProfit1": row.get("takeProfit1"),
                    "dataQualityFlags": row.get("dataQualityFlags") or [],
                    "returnsPct": {},
                })
            records.append({
                "date": snapshot_date,
                "asOf": payload.get("asOf"),
                "sourceMode": payload.get("sourceMode"),
                "signals": signals,
            })
            records.sort(key=lambda record: str(record.get("date") or ""))
            records[:] = records[-180:]
            new_record = True

        archive["updatedAt"] = datetime.now(ET).isoformat()
        archive["records"] = records

        if new_record:
            unresolved_symbols = set()
            for record in records[-35:]:
                date = str(record.get("date") or "")[:10]
                if not date:
                    continue
                for signal in record.get("signals") or []:
                    returns = signal.setdefault("returnsPct", {})
                    if any(str(horizon) not in returns for horizon in (1, 5, 20)):
                        unresolved_symbols.add(signal.get("ticker"))
            unresolved_symbols = sorted(symbol for symbol in unresolved_symbols if symbol)
            histories = yahoo_history(unresolved_symbols, retry_missing=False) if unresolved_symbols else {}

            for record in records[-35:]:
                record_date = str(record.get("date") or "")[:10]
                for signal in record.get("signals") or []:
                    ticker = signal.get("ticker")
                    history = histories.get(ticker)
                    if not history or not record_date:
                        continue
                    close = history.get("close")
                    high = history.get("high")
                    low = history.get("low")
                    if close is None or len(close) < 2:
                        continue
                    date_values = [
                        (index.date().isoformat() if hasattr(index, "date") else str(index)[:10])
                        for index in close.index
                    ]
                    candidates = [i for i, value in enumerate(date_values) if value <= record_date]
                    if not candidates:
                        continue
                    index0 = candidates[-1]
                    base_price = float(close.iloc[index0])
                    if base_price <= 0:
                        continue
                    signal["referenceClose"] = round(base_price, 6)
                    returns = signal.setdefault("returnsPct", {})
                    for horizon in (1, 5, 20):
                        target_idx = index0 + horizon
                        key = str(horizon)
                        if key not in returns and target_idx < len(close):
                            target_close = float(close.iloc[target_idx])
                            returns[key] = round((target_close / base_price - 1) * 100, 4) if base_price > 0 else None
                    future_end = min(index0 + 21, len(close))
                    if future_end > index0 + 1:
                        future_high = high.iloc[index0 + 1:future_end]
                        future_low = low.iloc[index0 + 1:future_end]
                        if len(future_high) and len(future_low):
                            signal["maxRunup20dPct"] = round((float(future_high.max()) / base_price - 1) * 100, 4)
                            signal["maxDrawdown20dPct"] = round((float(future_low.min()) / base_price - 1) * 100, 4)
                            stop = num(signal.get("stopLoss"))
                            target = num(signal.get("takeProfit1"))
                            signal["stopHitWithin20d"] = bool(stop is not None and float(future_low.min()) <= stop)
                            signal["targetHitWithin20d"] = bool(target is not None and float(future_high.max()) >= target)

            archive["records"] = records
            summaries = {}
            for horizon in (1, 5, 20):
                all_returns = []
                entry_returns = []
                for record in records:
                    for signal in record.get("signals") or []:
                        value = num((signal.get("returnsPct") or {}).get(str(horizon)))
                        if value is None:
                            continue
                        all_returns.append(value)
                        if signal.get("entryStatus") == "可考慮進場區" and signal.get("label") == "進場候選":
                            entry_returns.append(value)
                def pack(values):
                    return {
                        "samples": len(values),
                        "averageReturnPct": round(sum(values) / len(values), 4) if values else None,
                        "positiveRatePct": round(sum(1 for value in values if value > 0) / len(values) * 100, 2) if values else None,
                    }
                summaries[f"{horizon}d"] = {
                    "allTop20": pack(all_returns),
                    "actionableEntrySignals": pack(entry_returns),
                }
            completed_entry_signals = [
                signal
                for record in records
                for signal in (record.get("signals") or [])
                if signal.get("entryStatus") == "可考慮進場區"
                and signal.get("label") == "進場候選"
                and signal.get("returnsPct", {}).get("20") is not None
            ]
            summaries["entrySignals20d"] = {
                "samples": len(completed_entry_signals),
                "stopHitRatePct": round(sum(bool(signal.get("stopHitWithin20d")) for signal in completed_entry_signals) / len(completed_entry_signals) * 100, 2) if completed_entry_signals else None,
                "targetHitRatePct": round(sum(bool(signal.get("targetHitWithin20d")) for signal in completed_entry_signals) / len(completed_entry_signals) * 100, 2) if completed_entry_signals else None,
            }
            summary = {
                "asOf": datetime.now(ET).isoformat(),
                "source": "Yahoo historical daily bars used for forward outcome measurement",
                "records": len(records),
                "method": "Daily TOP20 signal cohorts; close-to-close forward returns; not a simulated fill engine and excludes fees/slippage.",
                "results": summaries,
            }
            with open(summary_path, "w", encoding="utf-8") as handle:
                json.dump(summary, handle, ensure_ascii=False, indent=2)
        else:
            # Always create a valid summary artifact before the first full close.
            try:
                with open(summary_path, encoding="utf-8") as handle:
                    json.load(handle)
            except Exception:
                with open(summary_path, "w", encoding="utf-8") as handle:
                    json.dump({
                        "asOf": datetime.now(ET).isoformat(),
                        "source": "Yahoo historical daily bars used for forward outcome measurement",
                        "records": len(records),
                        "method": "Daily TOP20 signal cohorts; close-to-close forward returns; not a simulated fill engine and excludes fees/slippage.",
                        "results": {},
                    }, handle, ensure_ascii=False, indent=2)
        with open(archive_path, "w", encoding="utf-8") as handle:
            json.dump(archive, handle, ensure_ascii=False, indent=2)
    except Exception as exc:
        print(f"US signal history update warning: {exc}", file=sys.stderr)
        for path, default in (
            (archive_path, {"updatedAt": datetime.now(ET).isoformat(), "records": []}),
            (summary_path, {
                "asOf": datetime.now(ET).isoformat(), "source": "Yahoo historical daily bars",
                "records": 0, "method": "Forward returns require daily signal history.", "results": {},
            }),
        ):
            try:
                with open(path, "w", encoding="utf-8") as handle:
                    json.dump(default, handle, ensure_ascii=False, indent=2)
            except Exception:
                pass


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

    update_us_signal_history(payload)
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
