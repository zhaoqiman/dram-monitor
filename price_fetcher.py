"""
Fetch real-time and daily prices.

Foreign (KR/JP/TW) stocks — already closed when US is open:
  today's close  → intraday 1d/60m (last bar of today's session)
  prev_close     → daily 5d/1d    (most recent close BEFORE today)

US stocks:
  prev_close     → daily 5d/1d [-2]
  today's price  → intraday 1d/1m overlay

FGXXX / money-market tickers → always 0% change.
Manual overrides in config.MANUAL_PRICE_OVERRIDES take priority over API.
"""

from __future__ import annotations

import logging
import re as _re
import time
from datetime import datetime, timezone as _tz
from typing import Optional

import pandas as pd
import pytz
import requests as _req
import yfinance as yf
from bs4 import BeautifulSoup

import overrides as _ov

from config import (
    ETF_TICKER, FINNHUB_API_KEY,
    FX_TICKERS, MANUAL_NAV_OVERRIDE, MANUAL_PRICE_OVERRIDES,
    MARKET_HOURS, ZERO_RETURN_TICKERS,
)

logger = logging.getLogger(__name__)


# ── Global rate limiter ─────────────────────────────────────────────────────────
# Yahoo rate-limits by IP; we enforce a minimum gap between ANY yfinance call.
_last_yf: float = 0.0
_YF_GAP = 12.0  # seconds between yfinance calls

def _yf_throttle():
    global _last_yf
    elapsed = time.time() - _last_yf
    if _last_yf > 0 and elapsed < _YF_GAP:
        gap = _YF_GAP - elapsed
        time.sleep(gap)
    _last_yf = time.time()

def _yf_download(tickers, *, period, interval, **kwargs):
    """yf.download with throttling + retry. Returns empty DataFrame on failure."""
    _yf_throttle()
    delays = [10, 20]
    for attempt, delay in enumerate(delays, 1):
        try:
            result = yf.download(
                tickers, period=period, interval=interval,
                auto_adjust=True, progress=False, threads=False,
                **kwargs,
            )
            if result is not None and not result.empty:
                return result
        except Exception:
            pass
        logger.info(f"yf.download attempt {attempt} failed, retry in {delay}s")
        time.sleep(delay)
        _yf_throttle()
    logger.warning(f"yf.download failed after {len(delays)} retries")
    return pd.DataFrame()


def _yf_ticker_history(ticker, *, period, interval, **kwargs):
    """yf.Ticker.history with throttling + retry. Returns empty DataFrame on failure."""
    _yf_throttle()
    delays = [10, 20]
    for attempt, delay in enumerate(delays, 1):
        try:
            result = yf.Ticker(ticker).history(
                period=period, interval=interval, auto_adjust=True, **kwargs,
            )
            if result is not None and not result.empty:
                return result
        except Exception:
            pass
        logger.info(f"yf.Ticker({ticker}) attempt {attempt} failed, retry in {delay}s")
        time.sleep(delay)
        _yf_throttle()
    logger.warning(f"yf.Ticker({ticker}) failed after {len(delays)} retries")
    return pd.DataFrame()


# ── TTL cache ─────────────────────────────────────────────────────────────────

class _TTLCache:
    def __init__(self, ttl: float):
        self._ttl = ttl
        self._store: dict = {}

    def get(self, key: str):
        e = self._store.get(key)
        if e and (time.time() - e["ts"]) < self._ttl:
            return e["val"]
        return None

    def get_stale(self, key: str):
        """Return cached value even if TTL expired, or None if never set."""
        e = self._store.get(key)
        return e["val"] if e else None

    def set(self, key: str, val):
        self._store[key] = {"ts": time.time(), "val": val}


_price_cache = _TTLCache(ttl=300)
_fx_cache    = _TTLCache(ttl=600)
_nav_cache   = _TTLCache(ttl=600)


# ── FX rates via free API (no Yahoo, no rate limit) ────────────────────────────

# open.er-api.com is a free FX API that does NOT require an API key.
# Response: {"result":"success","base_code":"USD","rates":{"KRW":...,"JPY":...,"TWD":...}}
_FX_API_URL = "https://open.er-api.com/v6/latest/USD"
_CURRENCY_MAP = {"KR": "KRW", "JP": "JPY", "TW": "TWD"}

def _fetch_fx_from_api() -> Optional[dict[str, float]]:
    """Fetch all FX rates from free API in a single call. Returns {market: rate_usd_per_unit}."""
    try:
        resp = _req.get(_FX_API_URL, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if data.get("result") != "success":
            return None
        raw = data.get("rates", {})
        rates: dict[str, float] = {"US": 1.0}
        for market, currency in _CURRENCY_MAP.items():
            if currency in raw:
                rates[market] = 1.0 / raw[currency]
            else:
                rates[market] = 1.0
        return rates
    except Exception as exc:
        logger.warning(f"FX API: {exc}")
    return None


def get_fx_rates() -> dict[str, float]:
    cached = _fx_cache.get("fx")
    if cached:
        return cached
    rates = _fetch_fx_from_api()
    if rates is None:
        rates = {"US": 1.0, "KR": 1.0, "JP": 1.0, "TW": 1.0}
    _fx_cache.set("fx", rates)
    return rates


def get_fx_prev_close(market: str) -> float:
    # FX rates barely move day-to-day; use current rate as approximation
    rates = get_fx_rates()
    return rates.get(market, 1.0)


def get_fx_prev_closes(markets: list[str]) -> dict[str, float]:
    """Fetch all FX prev-close rates. Uses current rate (FX moves are negligible for iNAV)."""
    rates = get_fx_rates()
    return {m: rates.get(m, 1.0) for m in markets}


# ── Alternative data sources (when yfinance is blocked) ────────────────────────

_FINNHUB_API = "https://finnhub.io/api/v1"

def _finnhub_quote(ticker: str) -> Optional[dict]:
    """Fetch {c:current_price, pc:prev_close} from Finnhub, or None."""
    if not FINNHUB_API_KEY:
        return None
    url = f"{_FINNHUB_API}/quote?symbol={ticker}&token={FINNHUB_API_KEY}"
    try:
        resp = _req.get(url, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if data.get("c", 0) > 0:
            return data
    except Exception as exc:
        logger.warning(f"Finnhub {ticker}: {exc}")
    return None


def _finnhub_prices(tickers: list[str]) -> dict[str, dict]:
    """Fetch prices for multiple tickers from Finnhub. Returns {ticker: {price, prev_close, ...}}."""
    if not FINNHUB_API_KEY or not tickers:
        return {}
    results: dict[str, dict] = {}
    for t in tickers:
        q = _finnhub_quote(t)
        if q:
            p, pc = float(q["c"]), float(q.get("pc", q["c"]))
            change_pct = (p - pc) / pc if pc else 0.0
            results[t] = {
                "price": p, "prev_close": pc, "change_pct": change_pct,
                "currency": "USD", "status": "ok", "source": "finnhub",
                "data_date": datetime.now(_tz.utc).strftime("%Y-%m-%d"),
                "ts": datetime.now(_tz.utc).isoformat(),
            }
        time.sleep(0.3)
    return results


_NAVER_H = {"User-Agent": "Mozilla/5.0", "Referer": "https://finance.naver.com/"}


def _naver_stock(ticker: str) -> Optional[dict]:
    """Fetch Korean stock price from Naver Finance."""
    code = ticker.replace(".KS", "").replace(".KQ", "")
    url = f"https://finance.naver.com/item/main.nhn?code={code}"
    try:
        resp = _req.get(url, headers=_NAVER_H, timeout=10)
        resp.encoding = "euc-kr"
        soup = BeautifulSoup(resp.text, "lxml")
        today = soup.select_one(".no_today .blind")
        # Naver duplicates text in em; grab first match from the parent td
        yesterday_td = soup.select_one("td.first")
        if today and yesterday_td:
            tp = today.get_text(strip=True).replace(",", "")
            nums = _re.findall(r"[\d,]+", yesterday_td.get_text())
            if nums:
                yp = nums[0].replace(",", "")
            else:
                return None
            try:
                p, pc = float(tp), float(yp)
                return {"c": p, "pc": pc}
            except ValueError:
                pass
    except Exception as exc:
        logger.warning(f"Naver {ticker}: {exc}")
    return None


_MINKABU_H = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

def _minkabu_stock(ticker: str) -> Optional[dict]:
    """Fetch Japanese stock price from Minkabu."""
    code = ticker.replace(".T", "").replace(".TO", "")
    url = f"https://minkabu.jp/stock/{code}"
    try:
        resp = _req.get(url, headers=_MINKABU_H, timeout=10)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")

        price_el = soup.select_one(".stock_price")
        if not price_el:
            return None
        current = price_el.get_text(strip=True).replace("円", "").replace(",", "")

        # Prev close: <th>前日終値（MM/DD）</th><td>62,460.0円</td>
        # soup.find(string=) requires exact .string match; use get_text search instead
        prev = current
        for th in soup.find_all("th"):
            if "前日終値" in th.get_text():
                td = th.find_next_sibling("td")
                if td:
                    prev = td.get_text(strip=True).replace("円", "").replace(",", "")
                break

        return {"c": float(current), "pc": float(prev)}
    except Exception as exc:
        logger.warning(f"Minkabu {ticker}: {exc}")
    return None


_TWSE_H = {"User-Agent": "Mozilla/5.0", "Referer": "https://tw.stock.yahoo.com/"}


def _twse_stock(ticker: str) -> Optional[dict]:
    """Fetch Taiwan stock price from TWSE."""
    code = ticker.replace(".TW", "")
    url = f"https://mis.twse.com.tw/stock/api/getStockInfo.jsp?ex_ch=tse_{code}.tw"
    try:
        resp = _req.get(url, headers=_TWSE_H, timeout=10)
        data = resp.json()
        for msg in data.get("msgArray", []):
            z = msg.get("z", "0")
            if z == "-":
                z = msg.get("y", "0")
            y = msg.get("y", "0")
            if z and y:
                return {"c": float(z), "pc": float(y)}
    except Exception as exc:
        logger.warning(f"TWSE {ticker}: {exc}")
    return None


def _any_stock(ticker: str) -> Optional[dict]:
    """Try each data source based on market. Returns {c, pc} or None."""
    if not ticker.endswith((".KS", ".KQ", ".T", ".TW", ".HK")):
        # US stock → Finnhub
        return _finnhub_quote(ticker)
    if ticker.endswith((".KS", ".KQ")):
        # Korean → Naver
        q = _naver_stock(ticker)
        if q:
            return q
        return _finnhub_quote(ticker)
    if ticker.endswith(".T"):
        # Japanese → Minkabu
        q = _minkabu_stock(ticker)
        if q:
            return q
        return _finnhub_quote(ticker)
    if ticker.endswith(".TW"):
        # Taiwan → TWSE
        q = _twse_stock(ticker)
        if q:
            return q
        return _finnhub_quote(ticker)
    return None


def _market_prices(tickers: list[str]) -> dict[str, dict]:
    """Fetch prices for any tickers from appropriate source."""
    if not tickers:
        return {}
    results: dict[str, dict] = {}

    # ── Determine US market status once for all tickers ───────────────────────
    # When US market hasn't opened yet today, Finnhub returns yesterday's close as
    # "c" and the day-before as "pc".  That yesterday→day-before change is ALREADY
    # baked into the official NAV, so we must NOT add it again.
    # We detect this by comparing Finnhub's quote timestamp ("t") against today's
    # ET date.  If the quote is from a prior day the return must be zeroed out;
    # we signal this via has_intraday=False (same convention used by _process_us).
    _us_mkt    = market_status("US")
    _today_et  = datetime.now(_ET).date()

    for t in tickers:
        q = _any_stock(t)
        if q:
            p, pc = float(q["c"]), float(q.get("pc", q["c"]))
            change_pct = (p - pc) / pc if pc else 0.0

            extra: dict = {}
            if not _is_foreign(t):
                # US stock: mark has_intraday so nav_calculator knows whether
                # today's price data actually exists.
                if _us_mkt in ("open", "pre-market"):
                    # Market is live right now — quote is definitely fresh.
                    extra["has_intraday"] = True
                else:
                    # Market is closed.  Use Finnhub's "t" (Unix epoch of last
                    # trade) to decide if the quote is from today's session.
                    ts_unix = q.get("t", 0)
                    if ts_unix:
                        try:
                            quote_date = datetime.fromtimestamp(ts_unix, tz=_ET).date()
                            extra["has_intraday"] = (quote_date == _today_et)
                        except Exception:
                            extra["has_intraday"] = False
                    else:
                        extra["has_intraday"] = False
            # Foreign stocks (KR/JP/TW): always include their latest session.
            # has_intraday is intentionally omitted; nav_calculator defaults True.

            results[t] = {
                "price": p, "prev_close": pc, "change_pct": change_pct,
                "currency": _currency_for(t), "status": "ok", "source": "market",
                "data_date": datetime.now(_tz.utc).strftime("%Y-%m-%d"),
                "ts": datetime.now(_tz.utc).isoformat(),
                **extra,
            }
        time.sleep(0.5)
    return results


# ── ETF price & IIV ───────────────────────────────────────────────────────────

def get_etf_price(ticker: str = ETF_TICKER) -> Optional[float]:
    cached = _price_cache.get(f"etf_{ticker}")
    if cached is not None:
        return cached
    # Try Finnhub first (fast and avoids yfinance rate limits)
    q = _finnhub_quote(ticker)
    if q and q.get("c"):
        p = float(q["c"])
        _price_cache.set(f"etf_{ticker}", p)
        return p
    # Fallback to yfinance
    hist = _yf_ticker_history(ticker, period="1d", interval="1m")
    if not hist.empty:
        p = float(hist["Close"].dropna().iloc[-1])
        _price_cache.set(f"etf_{ticker}", p)
        return p
    # Fallback to stale cache when all APIs are blocked
    stale = _price_cache.get_stale(f"etf_{ticker}")
    if stale is not None:
        logger.info(f"ETF price {ticker}: using stale cache")
        return stale
    return None


_iiv_cache = _TTLCache(ttl=300)

def get_etf_iiv(ticker: str = ETF_TICKER) -> Optional[float]:
    """获取官方 IIV（盘中介值指示性净值）。
    DRAM 是新产品，Yahoo Finance 上尚无 DRAM.IV 数据，静默返回 None。
    结果（含 None）缓存 5 分钟，避免每次都耗时重试。"""
    key = f"iiv_{ticker}"
    sentinel = "__not_found__"
    cached = _iiv_cache.get(key)
    if cached is not None:
        return None if cached == sentinel else cached

    yf_logger = logging.getLogger("yfinance")
    prev_level = yf_logger.level
    yf_logger.setLevel(logging.CRITICAL)  # suppress 404 noise for known-missing symbols
    try:
        for sym in (f"{ticker}.IV", f"^{ticker}-IV"):
            _yf_throttle()
            try:
                t = yf.Ticker(sym)
                hist = t.history(period="1d", interval="1m", auto_adjust=True)
                if hist is not None and not hist.empty:
                    val = float(hist["Close"].dropna().iloc[-1])
                    _iiv_cache.set(key, val)
                    return val
            except Exception:
                pass
    finally:
        yf_logger.setLevel(prev_level)
    _iiv_cache.set(key, sentinel)
    return None


# ── Previous-day NAV ──────────────────────────────────────────────────────────

def get_previous_nav(nav_from_scraper: Optional[float] = None,
                     ticker: str = ETF_TICKER) -> Optional[float]:
    # Priority: live scrape → overrides.json → config.py → cache → None
    if nav_from_scraper and nav_from_scraper > 0:
        _nav_cache.set("nav", nav_from_scraper)
        return nav_from_scraper
    ov = _ov.load()
    manual = ov.get("manual_nav") or (MANUAL_NAV_OVERRIDE if MANUAL_NAV_OVERRIDE else None)
    if manual and float(manual) > 0:
        return float(manual)
    cached = _nav_cache.get("nav")
    return cached


# ── Stock prices ──────────────────────────────────────────────────────────────

def get_stock_prices(tickers: list[str]) -> dict[str, dict]:
    """
    Returns {ticker: {price, prev_close, change_pct, currency, status, data_date, source}}.
    Priority: zero-return special case → manual override → cache → yfinance.
    """
    if not tickers:
        return {}

    results: dict[str, dict] = {}

    # 1. Zero-return MMF/cash positions
    for t in tickers:
        if t in ZERO_RETURN_TICKERS:
            results[t] = {
                "price": 1.0, "prev_close": 1.0, "change_pct": 0.0,
                "currency": "USD", "status": "ok", "source": "mmf",
                "data_date": datetime.now(_tz.utc).strftime("%Y-%m-%d"),
            }

    # 2. Manual overrides
    _runtime_ov = _ov.load().get("manual_prices", {})
    _all_price_overrides = {**MANUAL_PRICE_OVERRIDES, **_runtime_ov}

    api_tickers = []
    for t in tickers:
        if t in results:
            continue
        ov = _all_price_overrides.get(t)
        if ov and ov.get("price") and ov.get("prev_close"):
            p, prev = float(ov["price"]), float(ov["prev_close"])
            results[t] = {
                "price": p, "prev_close": prev,
                "change_pct": (p - prev) / prev if prev else 0.0,
                "currency": ov.get("currency", _currency_for(t)),
                "status": "ok", "source": "manual",
                "data_date": datetime.now(_tz.utc).strftime("%Y-%m-%d"),
            }
        else:
            api_tickers.append(t)

    # 3. Cache
    missing = []
    for t in api_tickers:
        v = _price_cache.get(t)
        if v is not None:
            results[t] = v
        else:
            missing.append(t)

    if not missing:
        return results

    # 4. Try alternative sources (Finnhub for US, Naver for KR, Kabutan for JP, TWSE for TW)
    market_results = _market_prices(missing)
    still_missing = [t for t in missing if t not in market_results]

    for t, v in market_results.items():
        results[t] = v

    if not still_missing:
        for t, v in results.items():
            if v.get("status") == "ok" and v.get("source") not in ("manual", "mmf"):
                _price_cache.set(t, v)
        return results

    # 5. yfinance fallback — combine ALL daily into ONE batch, then intraday
    foreign = [t for t in still_missing if _is_foreign(t)]
    us      = [t for t in still_missing if not _is_foreign(t)]

    daily_raw = _yf_download(foreign + us, period="5d", interval="1d", group_by="ticker")
    intra_foreign_raw = None
    if foreign:
        time.sleep(2)
        intra_foreign_raw = _yf_download(foreign, period="1d", interval="60m", group_by="ticker")
    intra_us_raw = None
    if us:
        time.sleep(2)
        intra_us_raw = _yf_download(us, period="1d", interval="1m", group_by="ticker")

    all_tickers = foreign + us
    for ticker in all_tickers:
        try:
            if _is_foreign(ticker):
                _process_foreign(ticker, daily_raw, intra_foreign_raw, all_tickers, foreign, results)
            else:
                _process_us(ticker, daily_raw, intra_us_raw, all_tickers, us, results)
        except Exception as e:
            logger.warning(f"Fetch {ticker}: {e}")
            results[ticker] = _err(ticker)

    for t, v in results.items():
        if v.get("status") == "ok" and v.get("source") not in ("manual", "mmf"):
            _price_cache.set(t, v)

    return results


def _process_foreign(ticker, daily_raw, intra_raw, all_tickers, batch, out):
    daily_df = _extract(daily_raw, ticker, all_tickers)
    intra_df = _extract(intra_raw, ticker, batch) if intra_raw is not None else None

    daily_cl = daily_df["Close"].dropna() if daily_df is not None else pd.Series(dtype=float)
    intra_cl = intra_df["Close"].dropna() if intra_df is not None else pd.Series(dtype=float)

    if intra_cl.empty and daily_cl.empty:
        out[ticker] = _err(ticker)
        return

    if not intra_cl.empty:
        today_close = float(intra_cl.iloc[-1])
        today_dt    = intra_cl.index[-1]

        today_date_only = pd.Timestamp(today_dt).normalize().tz_localize(None)
        daily_dates = daily_cl.index.normalize().tz_localize(None)
        mask = daily_dates < today_date_only
        before = daily_cl[mask]

        if not before.empty:
            prev_close = float(before.iloc[-1])
        elif len(daily_cl) >= 2:
            prev_close = float(daily_cl.iloc[-2])
        else:
            prev_close = today_close

        data_date = str(pd.Timestamp(today_dt).date())
    else:
        if len(daily_cl) < 2:
            out[ticker] = _err(ticker)
            return
        today_close = float(daily_cl.iloc[-1])
        prev_close  = float(daily_cl.iloc[-2])
        data_date   = str(pd.Timestamp(daily_cl.index[-1]).date())

    change_pct = (today_close - prev_close) / prev_close if prev_close else 0.0
    out[ticker] = {
        "price":      today_close,
        "prev_close": prev_close,
        "change_pct": change_pct,
        "currency":   _currency_for(ticker),
        "status":     "ok",
        "source":     "yfinance",
        "data_date":  data_date,
        "ts":         datetime.now(_tz.utc).isoformat(),
    }


def _process_us(ticker, daily_raw, intra_raw, all_tickers, batch, out):
    daily_df = _extract(daily_raw, ticker, all_tickers)
    intra_df = _extract(intra_raw, ticker, batch) if intra_raw is not None else None

    daily_cl = daily_df["Close"].dropna() if daily_df is not None else pd.Series(dtype=float)
    intra_cl = intra_df["Close"].dropna() if intra_df is not None else pd.Series(dtype=float)

    if daily_cl.empty:
        out[ticker] = _err(ticker)
        return

    live_today = _intraday_is_today(intra_cl)

    prev_close = float(daily_cl.iloc[-2]) if len(daily_cl) >= 2 else float(daily_cl.iloc[-1])

    if live_today:
        today_price = float(intra_cl.iloc[-1])
        data_date   = str(pd.Timestamp(intra_cl.index[-1]).date())
    else:
        today_price = float(daily_cl.iloc[-1])
        data_date   = str(pd.Timestamp(daily_cl.index[-1]).date())

    change_pct = (today_price - prev_close) / prev_close if prev_close else 0.0
    out[ticker] = {
        "price":        today_price,
        "prev_close":   prev_close,
        "change_pct":   change_pct,
        "currency":     "USD",
        "status":       "ok",
        "source":       "yfinance",
        "data_date":    data_date,
        "has_intraday": live_today,
        "ts":           datetime.now(_tz.utc).isoformat(),
    }


def _is_foreign(ticker: str) -> bool:
    return any(ticker.endswith(s) for s in (".KS", ".KQ", ".T", ".TW", ".HK"))


def _currency_for(ticker: str) -> str:
    if ticker.endswith((".KS", ".KQ")):
        return "KRW"
    if ticker.endswith(".T"):
        return "JPY"
    if ticker.endswith(".TW"):
        return "TWD"
    if ticker.endswith(".HK"):
        return "HKD"
    return "USD"


_ET = pytz.timezone("America/New_York")


def _intraday_is_today(intra_cl: pd.Series) -> bool:
    if intra_cl.empty:
        return False
    today_et = datetime.now(pytz.UTC).astimezone(_ET).date()
    last_ts   = intra_cl.index[-1]
    if hasattr(last_ts, "tzinfo") and last_ts.tzinfo is not None:
        bar_date = pd.Timestamp(last_ts).tz_convert(_ET).date()
    else:
        bar_date = pd.Timestamp(last_ts).date()
    return bar_date == today_et


def _extract(raw: pd.DataFrame, ticker: str, all_tickers: list[str]) -> Optional[pd.DataFrame]:
    if raw is None or raw.empty:
        return None
    try:
        if isinstance(raw.columns, pd.MultiIndex):
            # yfinance 1.x: level 0 = field (Close/High/…), level 1 = ticker
            lvl1 = raw.columns.get_level_values(1)
            if ticker in lvl1:
                return raw.xs(ticker, axis=1, level=1)
            # yfinance 0.2.x legacy: level 0 = ticker, level 1 = field
            lvl0 = raw.columns.get_level_values(0)
            if ticker in lvl0:
                return raw.xs(ticker, axis=1, level=0)
            return None
        return raw if len(all_tickers) == 1 else (raw[ticker] if ticker in raw.columns else None)
    except Exception:
        return None


def _err(ticker: str) -> dict:
    return {
        "price": None, "prev_close": None, "change_pct": 0.0,
        "currency": _currency_for(ticker), "status": "error",
        "source": "error", "data_date": None,
        "ts": datetime.now(_tz.utc).isoformat(),
    }


# ── Market status ─────────────────────────────────────────────────────────────

def market_status(market: str) -> str:
    cfg = MARKET_HOURS.get(market)
    if not cfg:
        return "unknown"
    tz  = pytz.timezone(cfg["tz"])
    now = datetime.now(pytz.UTC).astimezone(tz)
    if now.weekday() > 4:
        return "closed"
    o = now.replace(hour=cfg["open"][0],  minute=cfg["open"][1],  second=0, microsecond=0)
    c = now.replace(hour=cfg["close"][0], minute=cfg["close"][1], second=0, microsecond=0)
    p = o.replace(hour=max(cfg["open"][0] - 1, 0))
    if now < p:
        return "closed"
    if now < o:
        return "pre-market"
    if now <= c:
        return "open"
    return "closed"


def all_market_statuses() -> dict[str, str]:
    return {m: market_status(m) for m in ("US", "KR", "JP", "TW")}
