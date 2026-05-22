"""
Scrape DRAM ETF holdings and NAV from Roundhill's website.
Falls back to hardcoded holdings if the page is JS-rendered or unavailable.
"""

from __future__ import annotations

import io
import logging
import re
import time
from functools import lru_cache
from typing import Optional

import pandas as pd
import requests
from bs4 import BeautifulSoup

from config import FALLBACK_HOLDINGS, NAME_TO_TICKER, ROUNDHILL_URL

logger = logging.getLogger(__name__)

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


def get_holdings_and_nav(force: bool = False) -> dict:
    """
    Return {'nav': float | None, 'holdings': pd.DataFrame, 'source': str}.

    Tries in order:
      1. Roundhill page HTML table
      2. Roundhill CSV download endpoint
      3. Hardcoded fallback
    """
    now = time.time()
    if not force and _cache["data"] and (now - _cache["ts"]) < _CACHE_TTL:
        return _cache["data"]

    for attempt in (_try_scrape_html, _try_csv_download):
        r = attempt()
        if r and r.get("holdings") is not None and len(r["holdings"]) >= 3:
            result = r
            break
    else:
        result = _use_fallback()

    _cache["ts"] = now
    _cache["data"] = result
    return result


# ── Strategy 1: parse HTML ────────────────────────────────────────────────────

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
    # Try structured data attributes first
    for elem in soup.find_all(attrs={"data-nav": True}):
        try:
            return float(re.sub(r"[^0-9.]", "", elem["data-nav"]))
        except Exception:
            pass

    # Regex over visible text
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

        # Must look like a holdings table
        if not any(k in header_str for k in ["ticker", "symbol", "weight", "% weight", "holding"]):
            continue

        rows = []
        for tr in table.find_all("tr"):
            cells = [td.get_text(strip=True) for td in tr.find_all("td")]
            if cells:
                rows.append(cells)

        if not rows:
            continue

        # Pad / trim rows to header length
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
                import json
                data = json.loads(m.group(1))
                return pd.DataFrame(data)
            except Exception:
                pass
    return None


# ── Strategy 2: CSV download ──────────────────────────────────────────────────

def _try_csv_download() -> Optional[dict]:
    candidates = [
        ROUNDHILL_URL.rstrip("/") + "/holdings.csv",
        ROUNDHILL_URL.rstrip("/") + "/download",
        ROUNDHILL_URL.rstrip("/") + "/etf/dram/download",
    ]
    for url in candidates:
        try:
            resp = requests.get(url, headers=_HEADERS, timeout=15)
            if resp.status_code == 200 and "," in resp.text[:200]:
                df = pd.read_csv(io.StringIO(resp.text))
                df = _normalize_columns(df)
                if not df.empty:
                    df = _enrich(df)
                    logger.info(f"Downloaded {len(df)} holdings from CSV: {url}")
                    return {"nav": None, "holdings": df, "source": "roundhill_csv"}
        except Exception as e:
            logger.debug(f"CSV attempt {url} failed: {e}")
    return None


# ── Strategy 3: fallback ──────────────────────────────────────────────────────

def _use_fallback() -> dict:
    logger.warning("Using hardcoded fallback holdings (live scrape unavailable)")
    df = pd.DataFrame(FALLBACK_HOLDINGS)
    return {"nav": None, "holdings": df, "source": "fallback"}


# ── Normalization helpers ─────────────────────────────────────────────────────

_COL_ALIASES = {
    "ticker":       ["ticker", "symbol", "code", "cusip"],
    "name":         ["name", "security", "holding", "company", "description"],
    "weight":       ["weight", "% weight", "weight%", "allocation", "% of fund",
                     "% of portfolio", "portfolio weight", "weightings"],
    "shares":       ["shares", "quantity", "units", "shares held"],
    "market_value": ["market value", "value", "mv", "market val"],
    "country":      ["country", "market", "exchange", "region"],
}


def _normalize_columns(df: pd.DataFrame) -> pd.DataFrame:
    rename = {}
    for canonical, aliases in _COL_ALIASES.items():
        for col in df.columns:
            if col.strip().lower() in aliases:
                rename[col] = canonical
                break
    return df.rename(columns=rename)


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
    Fills missing tickers from the NAME_TO_TICKER lookup.
    """
    df = _normalize_columns(df)

    # ── ticker ────────────────────────────────────────────────────────────────
    if "ticker" not in df.columns:
        df["ticker"] = ""

    if "name" in df.columns:
        mask = df["ticker"].isna() | (df["ticker"] == "")
        df.loc[mask, "ticker"] = df.loc[mask, "name"].str.strip().str.lower().map(NAME_TO_TICKER)

    df = df.dropna(subset=["ticker"])
    df = df[df["ticker"].astype(str).str.strip() != ""]

    # Drop rows whose "ticker" is clearly not a stock symbol
    df = df[df["ticker"].apply(_is_valid_ticker)].copy()

    # ── weight ────────────────────────────────────────────────────────────────
    if "weight" not in df.columns:
        # Try to derive from market_value if present
        if "market_value" in df.columns:
            mv = pd.to_numeric(
                df["market_value"].astype(str).str.replace(r"[^0-9.]", "", regex=True),
                errors="coerce"
            )
            total = mv.sum()
            df["weight"] = mv / total if total > 0 else 0.0
        else:
            df["weight"] = 1.0 / len(df)
    else:
        df["weight"] = (
            df["weight"]
            .astype(str)
            .str.replace(r"[%,\s]", "", regex=True)
        )
        df["weight"] = pd.to_numeric(df["weight"], errors="coerce").fillna(0.0)
        if df["weight"].max() > 1.5:          # values are in percent (e.g. 25.5)
            df["weight"] = df["weight"] / 100.0

    # ── market ────────────────────────────────────────────────────────────────
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

    # ── keep only useful columns ───────────────────────────────────────────────
    keep = [c for c in ["ticker", "name", "weight", "market"] if c in df.columns]
    return df[keep].reset_index(drop=True)
