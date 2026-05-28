"""
Central configuration: tickers, market metadata, holdings.
"""

import os

from dotenv import load_dotenv
load_dotenv()

# ── ETF settings ──────────────────────────────────────────────────────────────
ETF_TICKER = "DRAM"
ETF_IIV_TICKER = "DRAM.IV"
ROUNDHILL_URL = "https://www.roundhillinvestments.com/etf/dram/"

# ── Refresh interval (seconds) ────────────────────────────────────────────────
REFRESH_INTERVAL_SEC = 60

# ── FX tickers ─────────────────────────────────────────────────────────────────
FX_TICKERS = {
    "KR": "KRWUSD=X",
    "JP": "JPYUSD=X",
    "TW": "TWDUSD=X",
    "US": None,
}

# ── Finnhub API (backup when yfinance is blocked) ─────────────────────────────
# Get a free API key at https://finnhub.io (60 calls/min free tier)
# Set env var FINNHUB_API_KEY or add to .env
FINNHUB_API_KEY: str = os.getenv("FINNHUB_API_KEY", "")  # noqa: SIM115

# Finnhub uses the same ticker format as Yahoo for these markets;
# only customise when they diverge.
FINNHUB_TICKER_MAP: dict[str, str] = {}

# ── Market trading hours (local time, 24h) ─────────────────────────────────────
MARKET_HOURS = {
    "US": {"tz": "America/New_York", "open": (9, 30), "close": (16, 0)},
    "KR": {"tz": "Asia/Seoul",       "open": (9, 0),  "close": (15, 30)},
    "JP": {"tz": "Asia/Tokyo",       "open": (9, 0),  "close": (15, 30)},
    "TW": {"tz": "Asia/Taipei",      "open": (9, 0),  "close": (13, 30)},
}

# ── Zero-return tickers (money market funds / cash equivalents) ────────────────
# These positions are treated as 0% daily return in iNAV calculation.
ZERO_RETURN_TICKERS: set[str] = {"FGXXX", "SHV", "VMFXX", "SGOV"}

# ── Known name → Yahoo Finance ticker map ──────────────────────────────────────
NAME_TO_TICKER: dict[str, str] = {
    # Korean
    "samsung electronics":          "005930.KS",
    "samsung electronics co":       "005930.KS",
    "samsung electronics co ltd":   "005930.KS",
    "sk hynix":                     "000660.KS",
    "sk hynix inc":                 "000660.KS",
    # Japanese
    "kioxia holdings":              "285A.T",
    "kioxia holdings corp":         "285A.T",
    "kioxia":                       "285A.T",
    "tokyo electron":               "8035.T",
    "tokyo electron ltd":           "8035.T",
    "advantest":                    "6857.T",
    "advantest corp":               "6857.T",
    # Taiwan
    "nanya technology":             "2408.TW",
    "nanya technology corp":        "2408.TW",
    "winbond electronics":          "2344.TW",
    "winbond electronics corp":     "2344.TW",
    # US
    "micron technology":            "MU",
    "micron technology inc":        "MU",
    "micron":                       "MU",
    "sandisk":                      "SNDK",
    "sandisk corp":                 "SNDK",
    "seagate technology":           "STX",
    "seagate technology holdings":  "STX",
    "western digital":              "WDC",
    "western digital corp":         "WDC",
    "nvidia":                       "NVDA",
    "first american government obligations fund": "FGXXX",
}

# ── Manual NAV override ───────────────────────────────────────────────────────
# Set to the previous trading day's official NAV from Roundhill website.
# The live scraper often fails (JS-rendered page); fill this in manually each day.
# Example: MANUAL_NAV_OVERRIDE = 49.77
MANUAL_NAV_OVERRIDE: float | None = None

# ── Manual price overrides (for any ticker yfinance can't serve) ────────────────
# Format: {"TICKER": {"price": float, "prev_close": float, "currency": "JPY/KRW/USD"}}
# NOTE: 285A.T (Kioxia) now works via yfinance intraday — no manual override needed.
MANUAL_PRICE_OVERRIDES: dict[str, dict] = {
    # "285A.T": {"price": 55670.0, "prev_close": 51290.0, "currency": "JPY"},
}

# ── Fallback holdings (from Roundhill DRAM ETF CSV, 2026-05-27 disclosure) ────
# This is the STATIC BACKUP used only when all live CSV fetches fail.
# Under normal operation data_fetcher._try_csv_holdings() updates this daily.
#
# Weights are already MERGED (direct equity + all TRS swap positions combined):
#   MU:        5.31% direct + 13.35% TRS-NM + 9.62% TRS-GS  = 28.28%
#   000660.KS: 25.94% direct + 1.10% TRS + 0.00% TRS        = 27.04%
#   005930.KS: 18.38% direct + 0.78% TRS + 0.00% TRS        = 19.16%
#   FGXXX:     11.93% money-market + 6.40% T-bill collateral = 18.33%
FALLBACK_HOLDINGS = [
    {"ticker": "MU",        "name": "Micron (equity+swaps)", "weight": 0.2828, "market": "US"},
    {"ticker": "000660.KS", "name": "SK Hynix",              "weight": 0.2704, "market": "KR"},
    {"ticker": "005930.KS", "name": "Samsung Electronics",   "weight": 0.1916, "market": "KR"},
    {"ticker": "FGXXX",     "name": "Cash / MMF / T-bill",   "weight": 0.1833, "market": "US"},
    {"ticker": "285A.T",    "name": "Kioxia Holdings",       "weight": 0.0676, "market": "JP"},
    {"ticker": "SNDK",      "name": "SanDisk",               "weight": 0.0521, "market": "US"},
    {"ticker": "STX",       "name": "Seagate Technology",    "weight": 0.0438, "market": "US"},
    {"ticker": "WDC",       "name": "Western Digital",       "weight": 0.0398, "market": "US"},
    {"ticker": "2408.TW",   "name": "Nanya Technology",      "weight": 0.0282, "market": "TW"},
    {"ticker": "2344.TW",   "name": "Winbond Electronics",   "weight": 0.0176, "market": "TW"},
]
