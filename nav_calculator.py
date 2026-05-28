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
from datetime import datetime, timezone
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
    mkt_statuses: dict | None = None,
) -> dict:
    """
    计算估算 iNAV。

    Args:
        holdings_df  : [ticker, name, weight, market]
        prices       : {ticker: {change_pct, price, prev_close, currency, status, has_intraday}}
        fx_today     : {market: spot_to_USD}  当前汇率
        fx_prev      : {market: spot_to_USD}  前日收盘汇率
        previous_nav : 上一交易日官方 NAV
        mkt_statuses : {market: "open"/"pre-market"/"closed"} 各市场当前状态

    时区感知逻辑
    ───────────
    官方 NAV 每日在美股收盘后发布，已经包含了当日美股和亚洲股的收盘价。

    • 美股开市时段（ET 9:30-16:00）：
        - 美股：使用实时价格 vs 昨收，贡献计入 ✓
        - 日韩台：已于当日收盘（美股开市前），用今日亚洲收盘 vs 昨收 ✓

    • 亚洲开市时段（美股休市）：
        - 日韩台：使用当日实时 / 最新收盘 vs 昨收，贡献计入 ✓
        - 美股：休市，最新报价 = 昨日收盘 = 已记入 NAV 的价格
                → 贡献强制为 0，避免双重计入昨日美股涨跌

    判断依据（双重保险）：
        1. mkt_statuses["US"] ∈ {"open","pre-market"}  → 美股正在交易，计入
        2. has_intraday = True  → price_fetcher 确认今日有盘中数据（如美股已收盘），计入
        否则 → 美股贡献 = 0
    """
    if previous_nav is None or previous_nav <= 0:
        return _empty_result("No previous NAV available")

    df = holdings_df.copy()

    # ── 附加价格字段 ─────────────────────────────────────────────────────────
    def _get(ticker, field, default=None):
        rec = prices.get(ticker)
        return rec.get(field, default) if isinstance(rec, dict) else default

    df["local_chg"]  = df["ticker"].apply(lambda t: _get(t, "change_pct", None))
    df["status"]     = df["ticker"].apply(lambda t: _get(t, "status", "missing"))
    df["price"]      = df["ticker"].apply(lambda t: _get(t, "price"))
    df["prev_close"] = df["ticker"].apply(lambda t: _get(t, "prev_close"))
    df["data_date"]  = df["ticker"].apply(lambda t: _get(t, "data_date", ""))

    # has_intraday 含义：
    #   True  = 价格来自今日交易（市场当前开市，或已有今日完整收盘数据）
    #   False = 价格是昨日（或更早）收盘价，已经记入官方 NAV，不应再叠加
    #   注：外国股票（KR/JP/TW）不设此字段，默认 True（始终计入最新价）
    df["has_intraday"] = df["ticker"].apply(lambda t: _get(t, "has_intraday", True))

    # ── 美股市场状态 ─────────────────────────────────────────────────────────
    _statuses  = mkt_statuses or {}
    _us_status = _statuses.get("US", "unknown")
    _us_active = _us_status in ("open", "pre-market")   # 美股当前有效交易

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

    # ── 每只持仓的 USD return ─────────────────────────────────────────────────
    def _usd_return(row: pd.Series) -> float:
        if row["status"] != "ok" or row["local_chg"] is None:
            return 0.0

        if row["market"] == "US":
            # 美股贡献有效的条件（满足其一即可）：
            #   (a) 美股市场当前处于开市 / 盘前状态，OR
            #   (b) price_fetcher 通过 Finnhub 时间戳或 yfinance 确认今日已有交易数据
            # 若两条均不满足 → 报价是昨收（已记入 NAV），贡献归零，避免双重叠加。
            if not _us_active and not row["has_intraday"]:
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
    us_contrib = df.loc[df["market"] == "US", "contribution"].sum()
    kr_contrib = df.loc[df["market"] == "KR", "contribution"].sum()
    jp_contrib = df.loc[df["market"] == "JP", "contribution"].sum()
    tw_contrib = df.loc[df["market"] == "TW", "contribution"].sum()

    df["usd_return_pct"]   = (df["usd_return"]   * 100).round(3)
    df["contribution_pct"] = (df["contribution"] * 100).round(4)

    missing  = df.loc[~ok_mask, "ticker"].tolist()
    coverage = ok_mask.sum() / len(df) * 100 if len(df) > 0 else 0.0

    # 标记哪些市场的贡献当前被纳入计算
    contributing = []
    if _us_active or df.loc[df["market"] == "US", "has_intraday"].any():
        contributing.append("US")
    if _statuses.get("KR") in ("open", "pre-market") or kr_contrib != 0:
        contributing.append("KR")
    if _statuses.get("JP") in ("open", "pre-market") or jp_contrib != 0:
        contributing.append("JP")
    if _statuses.get("TW") in ("open", "pre-market") or tw_contrib != 0:
        contributing.append("TW")

    return {
        "estimated_nav":         round(estimated_nav, 4),
        "previous_nav":          previous_nav,
        "weighted_return_pct":   round(weighted_return * 100, 4),
        "holdings_detail":       df.sort_values("contribution", ascending=False).reset_index(drop=True),
        "us_contribution_pct":   round(us_contrib * 100, 4),
        "kr_contribution_pct":   round(kr_contrib * 100, 4),
        "jp_contribution_pct":   round(jp_contrib * 100, 4),
        "tw_contribution_pct":   round(tw_contrib * 100, 4),
        "intl_contribution_pct": round((kr_contrib + jp_contrib + tw_contrib) * 100, 4),
        "data_coverage":         round(coverage, 1),
        "missing_tickers":       missing,
        "contributing_markets":  contributing,   # 当前实际参与计算的市场
        "us_active":             _us_active,
        "calc_time":             datetime.now(timezone.utc).isoformat() + "Z",
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
        "calc_time":             datetime.now(timezone.utc).isoformat() + "Z",
        "error":                 reason,
    }
