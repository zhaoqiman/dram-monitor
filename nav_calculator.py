"""
iNAV 和溢价率计算。

公式：
    USD_return_i = (1 + local_return_i) × (1 + fx_return_i) − 1
    iNAV = previous_NAV × (1 + Σ weight_i × USD_return_i)

说明：
  - local_return_i  = 当日本地货币涨跌幅（直接取 price_fetcher 的 change_pct，绝不用原始价格×汇率重算）
  - fx_return_i     = 当日 FX 涨跌幅（fx_today/fx_prev − 1），仅 KR/JP/TW 生效
  - 若 FX 数据异常（日内变幅 >5%），视为 0（保守处理，避免爆炸）
  - US 股票：fx_return = 0（本来就是 USD）
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)

_MAX_FX_DAILY_MOVE = 0.05   # 单日 FX 超过 ±5% 视为数据异常


def calculate_inav(
    holdings_df: pd.DataFrame,
    prices: dict[str, dict],
    fx_today: dict[str, float],
    fx_prev: dict[str, float],
    previous_nav: float,
) -> dict:
    """
    计算估算 iNAV。

    Args:
        holdings_df : [ticker, name, weight, market]
        prices      : {ticker: {change_pct, price, prev_close, currency, status}}
        fx_today    : {market: spot_to_USD}  当前汇率
        fx_prev     : {market: spot_to_USD}  前日收盘汇率
        previous_nav: 上一交易日官方 NAV
    """
    if previous_nav is None or previous_nav <= 0:
        return _empty_result("No previous NAV available")

    df = holdings_df.copy()

    # ── 附加价格字段 ─────────────────────────────────────────────────────────
    def _get(ticker, field, default=None):
        rec = prices.get(ticker)
        return rec.get(field, default) if isinstance(rec, dict) else default

    df["local_chg"]    = df["ticker"].apply(lambda t: _get(t, "change_pct", None))
    df["status"]       = df["ticker"].apply(lambda t: _get(t, "status", "missing"))
    df["price"]        = df["ticker"].apply(lambda t: _get(t, "price"))
    df["prev_close"]   = df["ticker"].apply(lambda t: _get(t, "prev_close"))
    df["data_date"]    = df["ticker"].apply(lambda t: _get(t, "data_date", ""))
    # has_intraday: False = yfinance only returned a stale daily close (US market
    # was closed); that price is already baked into the previous NAV → return = 0.
    # Non-US stocks don't set this flag (default True = always count their move).
    df["has_intraday"] = df["ticker"].apply(lambda t: _get(t, "has_intraday", True))

    # ── FX 日内涨跌幅（二阶修正） ─────────────────────────────────────────
    def _fx_return(market: str) -> float:
        fx_t = fx_today.get(market, 1.0) or 1.0
        fx_p = fx_prev.get(market, 1.0)  or 1.0
        if market == "US":
            return 0.0
        r = (fx_t / fx_p) - 1.0
        if abs(r) > _MAX_FX_DAILY_MOVE:
            logger.warning(f"FX return for {market} = {r:.2%} exceeds threshold, using 0")
            return 0.0
        return r

    fx_returns = {m: _fx_return(m) for m in ("US", "KR", "JP", "TW")}

    # ── USD return per holding ────────────────────────────────────────────────
    def _usd_return(row: pd.Series) -> float:
        if row["status"] != "ok" or row["local_chg"] is None:
            return 0.0
        # US stocks with no intraday data: market is closed and their last price
        # equals the close already used to compute the previous official NAV.
        # Counting it would double-add yesterday's US move → force to 0.
        if row["market"] == "US" and not row["has_intraday"]:
            return 0.0
        local_r = float(row["local_chg"])
        fx_r    = fx_returns.get(row["market"], 0.0)
        return (1.0 + local_r) * (1.0 + fx_r) - 1.0

    df["usd_return"]   = df.apply(_usd_return, axis=1)
    df["contribution"] = df["weight"] * df["usd_return"]

    # ── 加权合计（对无数据持仓按 0 处理，并做权重归一化） ──────────────────
    ok_mask       = df["status"] == "ok"
    ok_weight_sum = df.loc[ok_mask, "weight"].sum()
    total_weight  = df["weight"].sum()

    if ok_weight_sum > 0:
        # 若有部分持仓缺数据，按已知权重归一化（假设缺数据部分与平均持仓等幅）
        scale = total_weight / ok_weight_sum if ok_weight_sum < total_weight * 0.95 else 1.0
        weighted_return = df["contribution"].sum() * scale
    else:
        weighted_return = 0.0

    estimated_nav = previous_nav * (1.0 + weighted_return)

    # ── 市场贡献拆分 ──────────────────────────────────────────────────────────
    us_contrib   = df.loc[df["market"] == "US", "contribution"].sum()
    kr_contrib   = df.loc[df["market"] == "KR", "contribution"].sum()
    jp_contrib   = df.loc[df["market"] == "JP", "contribution"].sum()

    df["usd_return_pct"]   = (df["usd_return"]   * 100).round(3)
    df["contribution_pct"] = (df["contribution"] * 100).round(4)

    missing = df.loc[~ok_mask, "ticker"].tolist()
    coverage = ok_mask.sum() / len(df) * 100 if len(df) > 0 else 0.0

    return {
        "estimated_nav":         round(estimated_nav, 4),
        "previous_nav":          previous_nav,
        "weighted_return_pct":   round(weighted_return * 100, 4),
        "holdings_detail":       df.sort_values("contribution", ascending=False).reset_index(drop=True),
        "us_contribution_pct":   round(us_contrib   * 100, 4),
        "kr_contribution_pct":   round(kr_contrib   * 100, 4),
        "jp_contribution_pct":   round(jp_contrib   * 100, 4),
        "intl_contribution_pct": round((kr_contrib + jp_contrib) * 100, 4),
        "data_coverage":         round(coverage, 1),
        "missing_tickers":       missing,
        "calc_time":             datetime.utcnow().isoformat() + "Z",
        "error":                 None,
    }


def calculate_premium_discount(
    etf_market_price: Optional[float],
    estimated_nav: Optional[float],
) -> dict:
    if not etf_market_price or not estimated_nav or estimated_nav == 0:
        return {
            "market_price":  etf_market_price,
            "estimated_nav": estimated_nav,
            "premium_pct":   None,
            "label":         "数据不足",
            "color":         "gray",
            "delta_usd":     None,
        }

    premium_pct = (etf_market_price - estimated_nav) / estimated_nav * 100
    delta_usd   = etf_market_price - estimated_nav

    if premium_pct > 3.0:
        label, color = "极高溢价 ⚠️⚠️", "#FF0000"
    elif premium_pct > 1.5:
        label, color = "高溢价 ⚠️",     "#FF4444"
    elif premium_pct > 0.5:
        label, color = "轻度溢价",       "#FF9900"
    elif premium_pct > -0.5:
        label, color = "接近净值 ✓",    "#00CC00"
    elif premium_pct > -1.5:
        label, color = "轻度折价",       "#66BBFF"
    elif premium_pct > -3.0:
        label, color = "折价",           "#0088FF"
    else:
        label, color = "深度折价 ⚠️",   "#0044CC"

    return {
        "market_price":  etf_market_price,
        "estimated_nav": round(estimated_nav, 4),
        "premium_pct":   round(premium_pct, 4),
        "label":         label,
        "color":         color,
        "delta_usd":     round(delta_usd, 4),
    }


def _empty_result(reason: str) -> dict:
    return {
        "estimated_nav":         None,
        "previous_nav":          None,
        "weighted_return_pct":   0.0,
        "holdings_detail":       pd.DataFrame(),
        "us_contribution_pct":   0.0,
        "kr_contribution_pct":   0.0,
        "jp_contribution_pct":   0.0,
        "intl_contribution_pct": 0.0,
        "data_coverage":         0.0,
        "missing_tickers":       [],
        "calc_time":             datetime.utcnow().isoformat() + "Z",
        "error":                 reason,
    }
