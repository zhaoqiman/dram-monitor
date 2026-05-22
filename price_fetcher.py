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
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Optional

import pandas as pd
import pytz
import yfinance as yf

import overrides as _ov

from config import (
    ETF_TICKER, FX_TICKERS, MANUAL_NAV_OVERRIDE, MANUAL_PRICE_OVERRIDES,
    MARKET_HOURS, ZERO_RETURN_TICKERS,
)

logger = logging.getLogger(__name__)


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

    def set(self, key: str, val):
        self._store[key] = {"ts": time.time(), "val": val}


_price_cache = _TTLCache(ttl=55)
_fx_cache    = _TTLCache(ttl=120)
_nav_cache   = _TTLCache(ttl=300)


# ── FX rates ──────────────────────────────────────────────────────────────────

def get_fx_rates() -> dict[str, float]:
    cached = _fx_cache.get("fx")
    if cached:
        return cached
    rates: dict[str, float] = {"US": 1.0}
    for market, ticker in FX_TICKERS.items():
        if not ticker:
            rates[market] = 1.0
            continue
        try:
            hist = yf.Ticker(ticker).history(period="2d", interval="5m", auto_adjust=True)
            if not hist.empty:
                rates[market] = float(hist["Close"].dropna().iloc[-1])
                continue
        except Exception as e:
            logger.warning(f"FX {ticker}: {e}")
        rates[market] = 1.0
    _fx_cache.set("fx", rates)
    return rates


def get_fx_prev_close(market: str) -> float:
    ticker = FX_TICKERS.get(market)
    if not ticker:
        return 1.0
    try:
        hist = yf.Ticker(ticker).history(period="5d", interval="1d", auto_adjust=True)
        clean = hist["Close"].dropna()
        if len(clean) >= 2:
            return float(clean.iloc[-2])
        if not clean.empty:
            return float(clean.iloc[-1])
    except Exception:
        pass
    return 1.0


# ── ETF price & IIV ───────────────────────────────────────────────────────────

def get_etf_price(ticker: str = ETF_TICKER) -> Optional[float]:
    cached = _price_cache.get(f"etf_{ticker}")
    if cached is not None:
        return cached
    try:
        hist = yf.Ticker(ticker).history(period="1d", interval="1m", auto_adjust=True)
        if not hist.empty:
            p = float(hist["Close"].dropna().iloc[-1])
            _price_cache.set(f"etf_{ticker}", p)
            return p
    except Exception as e:
        logger.warning(f"ETF price {ticker}: {e}")
    return None


def get_etf_iiv(ticker: str = ETF_TICKER) -> Optional[float]:
    for sym in (f"{ticker}.IV", f"^{ticker}-IV"):
        try:
            hist = yf.Ticker(sym).history(period="1d", interval="1m", auto_adjust=True)
            if not hist.empty:
                return float(hist["Close"].dropna().iloc[-1])
        except Exception:
            pass
    return None


# ── Previous-day NAV ──────────────────────────────────────────────────────────

def get_previous_nav(nav_from_scraper: Optional[float] = None,
                     ticker: str = ETF_TICKER) -> Optional[float]:
    # Priority: live scrape → overrides.json → config.py → cache → None
    # NOTE: yfinance ETF price history is NOT used — market price ≠ official NAV.
    if nav_from_scraper and nav_from_scraper > 0:
        _nav_cache.set("nav", nav_from_scraper)
        return nav_from_scraper
    # Read fresh from overrides.json every call (fast, avoids stale values)
    ov = _ov.load()
    manual = ov.get("manual_nav") or (MANUAL_NAV_OVERRIDE if MANUAL_NAV_OVERRIDE else None)
    if manual and float(manual) > 0:
        return float(manual)
    cached = _nav_cache.get("nav")
    return cached  # None if nothing cached


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
                "data_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            }

    # 2. Manual overrides (overrides.json takes priority over config.py)
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
                "data_date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
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

    # 4. yfinance fetch
    foreign = [t for t in missing if _is_foreign(t)]
    us      = [t for t in missing if not _is_foreign(t)]

    if foreign:
        _fetch_foreign(foreign, results)
    if us:
        _fetch_us(us, results)

    for t, v in results.items():
        if v.get("status") == "ok" and v.get("source") not in ("manual", "mmf"):
            _price_cache.set(t, v)

    return results


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


def _fetch_foreign(tickers: list[str], out: dict) -> None:
    """
    For markets already closed when US is open.
    today's close  ← intraday 1d/60m last bar
    prev_close     ← daily 5d/1d most-recent close BEFORE today's intraday date
    """
    # Parallel batch fetches
    with ThreadPoolExecutor(max_workers=2) as ex:
        f_daily = ex.submit(
            yf.download, tickers,
            period="5d", interval="1d",
            group_by="ticker", auto_adjust=True, progress=False, threads=True,
        )
        f_intra = ex.submit(
            yf.download, tickers,
            period="1d", interval="60m",
            group_by="ticker", auto_adjust=True, progress=False, threads=True,
        )
        daily_raw = f_daily.result()
        intra_raw = f_intra.result()

    for ticker in tickers:
        try:
            daily_df = _extract(daily_raw, ticker, tickers)
            intra_df = _extract(intra_raw, ticker, tickers)

            daily_cl = daily_df["Close"].dropna() if daily_df is not None else pd.Series(dtype=float)
            intra_cl = intra_df["Close"].dropna() if intra_df is not None else pd.Series(dtype=float)

            if intra_cl.empty and daily_cl.empty:
                out[ticker] = _err(ticker)
                continue

            if not intra_cl.empty:
                today_close = float(intra_cl.iloc[-1])
                today_dt    = intra_cl.index[-1]

                # prev_close = latest daily bar whose DATE is strictly before today_dt's date
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
                # intraday unavailable — use daily
                if len(daily_cl) < 2:
                    out[ticker] = _err(ticker)
                    continue
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
                "ts":         datetime.utcnow().isoformat(),
            }
        except Exception as e:
            logger.warning(f"Foreign fetch {ticker}: {e}")
            out[ticker] = _err(ticker)


_ET = pytz.timezone("America/New_York")


def _intraday_is_today(intra_cl: pd.Series) -> bool:
    """Return True only if the latest 1-min bar is from today (ET).

    yfinance period='1d' interval='1m' returns the last *session's* bars even
    when the market is closed.  We must confirm the data is actually from the
    current US trading day before treating it as live intraday.
    """
    if intra_cl.empty:
        return False
    today_et = datetime.now(pytz.UTC).astimezone(_ET).date()
    last_ts   = intra_cl.index[-1]
    # Normalise to ET date regardless of whether the index is tz-aware
    if hasattr(last_ts, "tzinfo") and last_ts.tzinfo is not None:
        bar_date = pd.Timestamp(last_ts).tz_convert(_ET).date()
    else:
        bar_date = pd.Timestamp(last_ts).date()
    return bar_date == today_et


def _fetch_us(tickers: list[str], out: dict) -> None:
    """US stocks: prev_close from daily; today's price from 1-min intraday."""
    # Parallel daily + intraday
    with ThreadPoolExecutor(max_workers=2) as ex:
        f_daily = ex.submit(
            yf.download, tickers,
            period="5d", interval="1d",
            group_by="ticker", auto_adjust=True, progress=False, threads=True,
        )
        f_intra = ex.submit(
            yf.download, tickers,
            period="1d", interval="1m",
            group_by="ticker", auto_adjust=True, progress=False, threads=True,
        )
        daily_raw = f_daily.result()
        intra_raw = f_intra.result()

    for ticker in tickers:
        try:
            daily_df = _extract(daily_raw, ticker, tickers)
            intra_df = _extract(intra_raw, ticker, tickers)

            daily_cl = daily_df["Close"].dropna() if daily_df is not None else pd.Series(dtype=float)
            intra_cl = intra_df["Close"].dropna() if intra_df is not None else pd.Series(dtype=float)

            if daily_cl.empty:
                out[ticker] = _err(ticker)
                continue

            # has_intraday = True only when the 1-min bars are from TODAY (ET).
            # After close, yfinance still returns yesterday's bars → False.
            live_today = _intraday_is_today(intra_cl)

            # prev_close = confirmed previous-session close (second-to-last daily bar)
            prev_close = float(daily_cl.iloc[-2]) if len(daily_cl) >= 2 else float(daily_cl.iloc[-1])

            if live_today:
                # Market open (or just closed today): use real-time 1-min price
                today_price = float(intra_cl.iloc[-1])
                data_date   = str(pd.Timestamp(intra_cl.index[-1]).date())
            else:
                # Market closed; latest available = yesterday's close (= NAV base price)
                # change_pct must be 0 — nav_calculator will enforce this via has_intraday
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
                # nav_calculator zeroes the return when has_intraday is False
                "has_intraday": live_today,
                "ts":           datetime.utcnow().isoformat(),
            }
        except Exception as e:
            logger.warning(f"US fetch {ticker}: {e}")
            out[ticker] = _err(ticker)


def _extract(raw: pd.DataFrame, ticker: str, all_tickers: list[str]) -> Optional[pd.DataFrame]:
    """
    yfinance (>=0.2.x) always returns MultiIndex columns: (ticker, column_name).
    Level 0 = ticker, level 1 = OHLCV column name.
    """
    if raw is None or raw.empty:
        return None
    try:
        if isinstance(raw.columns, pd.MultiIndex):
            lvl0 = raw.columns.get_level_values(0)
            return raw.xs(ticker, axis=1, level=0) if ticker in lvl0 else None
        # Fallback: flat columns (very old yfinance single-ticker)
        return raw if len(all_tickers) == 1 else (raw[ticker] if ticker in raw.columns else None)
    except Exception:
        return None


def _err(ticker: str) -> dict:
    return {
        "price": None, "prev_close": None, "change_pct": 0.0,
        "currency": _currency_for(ticker), "status": "error",
        "source": "error", "data_date": None,
        "ts": datetime.utcnow().isoformat(),
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
