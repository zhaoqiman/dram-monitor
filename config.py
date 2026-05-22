"""
Central configuration: tickers, market metadata, holdings.
"""

# ── ETF settings ──────────────────────────────────────────────────────────────
ETF_TICKER = "DRAM"
ETF_IIV_TICKER = "DRAM.IV"
ROUNDHILL_URL = "https://www.roundhillinvestments.com/etf/dram/"

# ── Refresh interval (seconds) ────────────────────────────────────────────────
REFRESH_INTERVAL_SEC = 60

# ── FX tickers (Yahoo Finance) ─────────────────────────────────────────────────
FX_TICKERS = {
    "KR": "KRWUSD=X",
    "JP": "JPYUSD=X",
    "TW": "TWDUSD=X",
    "US": None,
}

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

# ── Fallback holdings (from Roundhill DRAM ETF website, 2026-05-20 disclosure) ──
# Updated from actual holdings page:
#   https://www.roundhillinvestments.com/etf/dram/
#
# MU exposure: 5.27% direct + 12.42% TRS swap + 9.22% TRS swap = 26.91% total
#   → all collapsed into one MU entry (use MU price change as proxy for swaps)
# FGXXX: money market fund used as swap collateral → 0% daily return
FALLBACK_HOLDINGS = [
    {"ticker": "000660.KS", "name": "SK Hynix",             "weight": 0.2675, "market": "KR"},
    {"ticker": "005930.KS", "name": "Samsung Electronics",  "weight": 0.2057, "market": "KR"},
    {"ticker": "MU",        "name": "Micron (equity+swaps)","weight": 0.2691, "market": "US"},
    {"ticker": "FGXXX",     "name": "Cash / MMF (FGXXX)",  "weight": 0.1307, "market": "US"},
    {"ticker": "285A.T",    "name": "Kioxia Holdings",      "weight": 0.0649, "market": "JP"},
    {"ticker": "SNDK",      "name": "SanDisk",              "weight": 0.0532, "market": "US"},
    {"ticker": "STX",       "name": "Seagate Technology",   "weight": 0.0453, "market": "US"},
    {"ticker": "WDC",       "name": "Western Digital",      "weight": 0.0406, "market": "US"},
    {"ticker": "2408.TW",   "name": "Nanya Technology",     "weight": 0.0295, "market": "TW"},
    {"ticker": "2344.TW",   "name": "Winbond Electronics",  "weight": 0.0167, "market": "TW"},
]
