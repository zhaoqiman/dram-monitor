"""
Fetch DRAM ETF holdings and NAV from Roundhill's website.

抓取策略（按优先级）：
  1. Roundhill 隐藏 CSV 端点（直接 HTTP 下载，无需 JS，最可靠）
       - 持仓: .../FilepointRoundhill.40RU.RU_Holdings_MMDDYYYY.csv
       - NAV:  .../FilepointRoundhill.40RU.RU_DailyNAV.csv
  2. Roundhill 页面 HTML 表格（通常失败，JS 动态渲染）
  3. 硬编码 fallback（config.FALLBACK_HOLDINGS）

CSV 持仓字段说明：
  Date, Account, StockTicker, CUSIP, SecurityName, Shares, Price,
  MarketValue, Weightings, NetAssets, SharesOutstanding, CreationUnits,
  MoneyMarketFlag

合并规则：
  - 含 " TRS " 的 Swap 仓位 → 按底层资产 CUSIP/ID 合并回对应股票
  - MoneyMarketFlag=Y 或美国国债 CUSIP（912797*）→ 归入 FGXXX（零收益）
  - FX 现金（KRW/TWD/…）、Cash&Other → 跳过（不影响 iNAV 计算）
"""

from __future__ import annotations

import json as _json
import logging
import re
import time
from datetime import date as _date, timedelta as _timedelta
from io import StringIO as _StringIO
from typing import Optional

import pandas as pd
import requests
from bs4 import BeautifulSoup

from config import ETF_TICKER, FALLBACK_HOLDINGS, NAME_TO_TICKER, ROUNDHILL_URL

logger = logging.getLogger(__name__)

# ── Roundhill CSV endpoints ────────────────────────────────────────────────────

_ROUNDHILL_BASE = (
    "https://www.roundhillinvestments.com/assets/data/FilepointRoundhill.40RU"
)
_ROUNDHILL_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Referer": "https://www.roundhillinvestments.com/etf/dram/",
    "Accept": "text/plain,text/csv,*/*",
    "Accept-Language": "en-US,en;q=0.5",
}

# CSV StockTicker market suffix → Yahoo Finance suffix
_CSV_MKT_SUFFIX: dict[str, str] = {
    " KS": ".KS",   # Korea KOSPI
    " TT": ".TW",   # Taiwan (CSV uses TT, Yahoo uses TW)
    " JP": ".T",    # Japan TSE  (CSV uses JP, Yahoo uses T)
    " AU": ".AX",   # Australia ASX
    " LN": ".L",    # London LSE
}

# Swap underlying ID (CUSIP or Bloomberg ID) → Yahoo Finance ticker
# Key = first token of a "XXXXX TRS YYMMDD GS/NM" StockTicker
_SWAP_UNDERLYING: dict[str, str] = {
    "595112103": "MU",          # Micron Technology CUSIP
    "6450267":   "000660.KS",   # SK Hynix Bloomberg ID
    "6771720":   "005930.KS",   # Samsung Electronics Bloomberg ID
}

# Canonical display names for swap-aggregated positions (override CSV swap names)
_CANONICAL_NAMES: dict[str, str] = {
    "MU":        "Micron Technology Inc (equity+swaps)",
    "000660.KS": "SK Hynix Inc (equity+swaps)",
    "005930.KS": "Samsung Electronics Co Ltd (equity+swaps)",
}

# FX cash positions and accounting entries to skip entirely
_SKIP_TICKERS: frozenset[str] = frozenset(
    {"KRW", "TWD", "EUR", "GBP", "JPY", "USD", "Cash&Other"}
)

# CUSIP prefixes indicating near-zero-return instruments (US T-bills)
_TBILL_CUSIP_PREFIXES: tuple[str, ...] = ("912797",)

# ── Page HTML headers (kept for Strategy 2) ──────────────────────────────────

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}

# Cache scrape result for 4 hours to avoid hammering the site
_cache: dict = {"ts": 0.0, "data": None}
_CACHE_TTL = 4 * 3600


# ═══════════════════════════════════════════════════════════════════════════════
# Public API
# ═══════════════════════════════════════════════════════════════════════════════

def get_holdings_and_nav(force: bool = False) -> dict:
    """
    Return {'nav': float | None, 'holdings': pd.DataFrame, 'source': str}.

    Tries in order:
      1. Roundhill CSV endpoints  (直接下载，无需 JS，最可靠)
      2. Roundhill page HTML table (通常失败，JS 渲染)
      3. Hardcoded fallback       (config.FALLBACK_HOLDINGS)
    """
    now = time.time()
    if not force and _cache["data"] and (now - _cache["ts"]) < _CACHE_TTL:
        return _cache["data"]

    # ── Strategy 1: CSV download ───────────────────────────────────────────────
    result = _try_csv_holdings()
    if result and result.get("holdings") is not None and len(result["holdings"]) >= 3:
        nav = _try_csv_nav()
        result["nav"] = nav
        _cache["ts"] = now
        _cache["data"] = result
        return result

    # ── Strategy 2: HTML scraping (usually fails, kept as fallback) ────────────
    result = _try_scrape_html()
    if result is None or result.get("holdings") is None or len(result["holdings"]) < 3:
        result = _use_fallback()

    _cache["ts"] = now
    _cache["data"] = result
    return result


# ═══════════════════════════════════════════════════════════════════════════════
# Strategy 1: Roundhill CSV endpoints
# ═══════════════════════════════════════════════════════════════════════════════

def _try_csv_nav() -> Optional[float]:
    """
    Fetch latest DRAM NAV from Roundhill's DailyNAV.csv.

    File URL: .../FilepointRoundhill.40RU.RU_DailyNAV.csv
    Columns:  Fund Name, Fund Ticker, CUSIP, Net Assets, Shares Outstanding,
              NAV, NAV Change Dollars, NAV Change Percentage,
              Market Price, ..., Rate Date
    """
    url = f"{_ROUNDHILL_BASE}.RU_DailyNAV.csv"
    try:
        resp = requests.get(url, headers=_ROUNDHILL_HEADERS, timeout=(10, 25))
        if resp.status_code != 200:
            logger.warning(f"DailyNAV CSV HTTP {resp.status_code}")
            return None
        df = pd.read_csv(_StringIO(resp.text))
        row = df[df["Fund Ticker"] == ETF_TICKER]
        if row.empty:
            logger.warning(f"DailyNAV CSV: no row for {ETF_TICKER}")
            return None
        nav = float(row.iloc[0]["NAV"])
        as_of = row.iloc[0].get("Rate Date", "?")
        if 1.0 < nav < 10_000:
            logger.info(f"CSV NAV: {ETF_TICKER} = {nav} (as of {as_of})")
            return nav
    except Exception as exc:
        logger.warning(f"CSV NAV fetch failed: {exc}")
    return None


def _try_csv_holdings() -> Optional[dict]:
    """
    Fetch DRAM holdings from Roundhill's daily Holdings CSV.

    The file is named RU_Holdings_MMDDYYYY.csv (matching the JS logic in app.js
    function Yr()). We try today and up to 6 days back to cover weekends and
    holidays. On error the JS retries up to 15 days; we use 7 which covers any
    normal market holiday sequence.
    """
    today = _date.today()
    for days_back in range(7):
        d = today - _timedelta(days=days_back)
        date_str = d.strftime("%m%d%Y")
        url = f"{_ROUNDHILL_BASE}.RU_Holdings_{date_str}.csv"
        try:
            resp = requests.get(url, headers=_ROUNDHILL_HEADERS, timeout=(10, 30))
            if resp.status_code != 200:
                logger.debug(f"Holdings CSV {date_str}: HTTP {resp.status_code}")
                continue
            text = resp.text.strip()
            if len(text) < 200 or "StockTicker" not in text:
                logger.debug(f"Holdings CSV {date_str}: empty/invalid")
                continue

            raw_df = pd.read_csv(_StringIO(text))
            dram = raw_df[raw_df["Account"] == ETF_TICKER].copy()
            if dram.empty:
                logger.debug(f"Holdings CSV {date_str}: no {ETF_TICKER} rows")
                continue

            holdings_df = _parse_csv_holdings(dram)
            if holdings_df is None or len(holdings_df) < 3:
                logger.debug(f"Holdings CSV {date_str}: too few valid positions")
                continue

            as_of = dram["Date"].iloc[0] if "Date" in dram.columns else date_str
            logger.info(
                f"CSV holdings: {len(holdings_df)} positions for {ETF_TICKER}"
                f" as of {as_of}"
            )
            return {"nav": None, "holdings": holdings_df, "source": "csv",
                    "as_of": as_of}

        except Exception as exc:
            logger.debug(f"Holdings CSV {date_str}: {exc}")

    logger.warning("Could not fetch DRAM holdings from CSV (tried last 7 days)")
    return None


def _parse_csv_holdings(dram_df: pd.DataFrame) -> Optional[pd.DataFrame]:
    """
    Convert raw DRAM CSV rows to a holdings DataFrame [ticker, name, weight, market].

    Logic:
      - Swap positions ("XXX TRS YYY" in StockTicker) → merged into underlying
        equity using _SWAP_UNDERLYING mapping.
      - MoneyMarketFlag=Y or T-bill CUSIP → grouped under "FGXXX" (zero return).
      - FX cash / Cash&Other → skipped.
      - Market suffix conversion: " KS"→".KS", " TT"→".TW", " JP"→".T"
    """
    weight_map: dict[str, float] = {}
    name_map:   dict[str, str]   = {}
    market_map: dict[str, str]   = {}

    for _, row in dram_df.iterrows():
        sticker  = str(row.get("StockTicker",    "")).strip()
        cusip    = str(row.get("CUSIP",          "")).strip()
        sec_name = str(row.get("SecurityName",   "")).strip()
        mmf_flag = str(row.get("MoneyMarketFlag","")).strip().upper()

        # Parse weight (e.g. "25.94%")
        try:
            weight = float(
                str(row.get("Weightings", "0")).replace("%", "").strip()
            ) / 100.0
        except ValueError:
            continue
        if weight == 0.0:
            continue

        # ── Skip FX cash / accounting entries ─────────────────────────────────
        if sticker in _SKIP_TICKERS or cusip.startswith("CASH"):
            continue

        # ── Swap positions → merge into underlying equity ──────────────────────
        if " TRS " in sticker:
            underlying_id = sticker.split(" TRS ")[0].strip()
            final_ticker = _SWAP_UNDERLYING.get(underlying_id)
            if final_ticker is None:
                logger.debug(f"Unknown swap underlying: {underlying_id!r}")
                continue

        # ── Zero-return positions (MMF / T-bill) → group as FGXXX ─────────────
        elif mmf_flag == "Y" or any(cusip.startswith(p) for p in _TBILL_CUSIP_PREFIXES):
            final_ticker = "FGXXX"
            sec_name     = "Cash / MMF / T-bill"

        # ── Normal equity positions ────────────────────────────────────────────
        else:
            final_ticker = _csv_ticker_to_yf(sticker)
            if not final_ticker or not _is_valid_ticker(final_ticker):
                logger.debug(f"Skipping invalid CSV ticker: {sticker!r}")
                continue

        # Accumulate weight (swaps add on top of direct position)
        weight_map[final_ticker] = weight_map.get(final_ticker, 0.0) + weight
        # Prefer canonical name, then direct-equity name over swap-derived name
        if final_ticker in _CANONICAL_NAMES:
            name_map[final_ticker] = _CANONICAL_NAMES[final_ticker]
        elif final_ticker not in name_map or " TRS " in name_map.get(final_ticker, ""):
            name_map[final_ticker] = sec_name
        market_map[final_ticker] = _market_from_ticker(final_ticker)

    if not weight_map:
        return None

    rows = [
        {"ticker": t, "name": name_map[t], "weight": w, "market": market_map[t]}
        for t, w in weight_map.items()
    ]
    return (
        pd.DataFrame(rows)
        .sort_values("weight", ascending=False)
        .reset_index(drop=True)
    )


def _csv_ticker_to_yf(sticker: str) -> Optional[str]:
    """Convert a Roundhill CSV StockTicker to Yahoo Finance format."""
    for csv_sfx, yf_sfx in _CSV_MKT_SUFFIX.items():
        if sticker.endswith(csv_sfx):
            return sticker[: -len(csv_sfx)] + yf_sfx
    return sticker   # US ticker – keep as-is


def _market_from_ticker(ticker: str) -> str:
    if ticker.endswith((".KS", ".KQ")):
        return "KR"
    if ticker.endswith(".T"):
        return "JP"
    if ticker.endswith(".TW"):
        return "TW"
    return "US"


# ═══════════════════════════════════════════════════════════════════════════════
# Strategy 2: parse HTML (kept as fallback; usually fails due to JS rendering)
# ═══════════════════════════════════════════════════════════════════════════════

def _try_scrape_html() -> Optional[dict]:
    try:
        resp = requests.get(ROUNDHILL_URL, headers=_HEADERS, timeout=15)
        resp.raise_for_status()
    except Exception as e:
        logger.warning(f"HTML fetch failed: {e}")
        return None

    soup = BeautifulSoup(resp.text, "lxml")
    nav = _parse_nav(soup)
    df = _parse_holdings_table(soup)

    if df is not None and not df.empty:
        df = _enrich(df)
        logger.info(f"Scraped {len(df)} holdings from HTML (NAV={nav})")
        return {"nav": nav, "holdings": df, "source": "roundhill_html"}

    # Try embedded JSON (some sites ship data as window.__INITIAL_STATE__ etc.)
    df = _parse_json_blob(resp.text)
    if df is not None and not df.empty:
        df = _enrich(df)
        return {"nav": nav, "holdings": df, "source": "roundhill_json"}

    logger.warning("No holdings table found in HTML")
    return None


def _parse_nav(soup: BeautifulSoup) -> Optional[float]:
    """Extract previous-day NAV value from the page."""
    for elem in soup.find_all(attrs={"data-nav": True}):
        try:
            return float(re.sub(r"[^0-9.]", "", elem["data-nav"]))
        except Exception:
            pass

    text = soup.get_text(" ")
    patterns = [
        r"(?:NAV|Net Asset Value)[^$\d]{0,20}\$\s*([\d,]+\.\d{2,4})",
        r"\$\s*([\d,]+\.\d{2,4})\s*(?:NAV|net asset value)",
        r"(?:as of|previous)[^$\d]{0,30}\$\s*([\d,]+\.\d{2,4})",
    ]
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            try:
                val = float(m.group(1).replace(",", ""))
                if 1.0 < val < 10_000:
                    return val
            except Exception:
                pass
    return None


def _parse_holdings_table(soup: BeautifulSoup) -> Optional[pd.DataFrame]:
    """Find the holdings <table> and return a normalized DataFrame."""
    for table in soup.find_all("table"):
        headers = [th.get_text(strip=True).lower() for th in table.find_all("th")]
        header_str = " ".join(headers)

        has_ticker = any(k in header_str for k in ["ticker", "symbol"])
        has_weight = any(k in header_str for k in ["weight", "% weight", "allocation", "etf weight"])
        if not (has_ticker and has_weight):
            continue

        rows = []
        for tr in table.find_all("tr"):
            cells = [td.get_text(strip=True) for td in tr.find_all("td")]
            if cells:
                rows.append(cells)

        if not rows:
            continue

        n = max(len(headers), max(len(r) for r in rows))
        padded = [r + [""] * (n - len(r)) for r in rows]
        df = pd.DataFrame(padded, columns=(headers + [""] * (n - len(headers)))[:n])
        return df

    return None


def _parse_json_blob(html: str) -> Optional[pd.DataFrame]:
    """Try to find JSON holdings arrays embedded in the page source."""
    patterns = [
        r"holdings\s*[:=]\s*(\[.*?\])",
        r"\"holdings\"\s*:\s*(\[.*?\])",
    ]
    for pat in patterns:
        m = re.search(pat, html, re.DOTALL | re.IGNORECASE)
        if m:
            try:
                data = _json.loads(m.group(1))
                return pd.DataFrame(data)
            except Exception:
                pass
    return None


# ═══════════════════════════════════════════════════════════════════════════════
# Strategy 3: hardcoded fallback
# ═══════════════════════════════════════════════════════════════════════════════

def _use_fallback() -> dict:
    logger.warning("Using hardcoded fallback holdings (live CSV and HTML scrape unavailable)")
    df = pd.DataFrame(FALLBACK_HOLDINGS)
    return {"nav": None, "holdings": df, "source": "fallback"}


# ═══════════════════════════════════════════════════════════════════════════════
# Shared helpers
# ═══════════════════════════════════════════════════════════════════════════════

_TICKER_RE = re.compile(r'^[A-Z0-9]{1,6}(\.(KS|KQ|T|TW|HK|L|AX))?$')

def _is_valid_ticker(t: str) -> bool:
    """Return True only if the string looks like a real stock ticker."""
    t = str(t).strip().upper()
    if not t or len(t) > 12:
        return False
    return bool(_TICKER_RE.match(t))


def _enrich(df: pd.DataFrame) -> pd.DataFrame:
    """
    Ensure columns: ticker, name, weight, market.
    Used by the HTML scraping path.
    """
    if "ticker" not in df.columns:
        df["ticker"] = ""

    if "name" in df.columns:
        mask = df["ticker"].isna() | (df["ticker"] == "")
        df.loc[mask, "ticker"] = df.loc[mask, "name"].str.strip().str.lower().map(NAME_TO_TICKER)

    df = df.dropna(subset=["ticker"])
    df = df[df["ticker"].astype(str).str.strip() != ""]
    df = df[df["ticker"].apply(_is_valid_ticker)].copy()

    if "weight" not in df.columns:
        if "market_value" in df.columns:
            mv = pd.to_numeric(
                df["market_value"].astype(str).str.replace(r"[^0-9.]", "", regex=True),
                errors="coerce",
            )
            total = mv.sum()
            df["weight"] = mv / total if total > 0 else 0.0
        else:
            df["weight"] = 1.0 / len(df)
    else:
        df["weight"] = (
            df["weight"].astype(str).str.replace(r"[%,\s]", "", regex=True)
        )
        df["weight"] = pd.to_numeric(df["weight"], errors="coerce").fillna(0.0)
        if df["weight"].max() > 1.5:
            df["weight"] = df["weight"] / 100.0

    def _infer_market(row: pd.Series) -> str:
        t = str(row.get("ticker", ""))
        if t.endswith(".KS") or t.endswith(".KQ"):
            return "KR"
        if t.endswith(".T"):
            return "JP"
        if t.endswith(".TW"):
            return "TW"
        if "country" in row.index:
            c = str(row["country"]).strip().upper()
            if c in ("KR", "KOREA", "SOUTH KOREA"):
                return "KR"
            if c in ("JP", "JAPAN"):
                return "JP"
        return "US"

    df["market"] = df.apply(_infer_market, axis=1)
    keep = [c for c in ["ticker", "name", "weight", "market"] if c in df.columns]
    return df[keep].reset_index(drop=True)
