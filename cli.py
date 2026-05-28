"""
DRAM ETF 净值监控 — CLI 版本
用法:
    python cli.py              # 单次输出
    python cli.py --watch 60   # 每 60 秒循环刷新
    python cli.py --json       # 输出 JSON
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone

import pandas as pd
import pytz

import data_fetcher as df_mod
import nav_calculator as nav_mod
import price_fetcher as pf
from config import ETF_TICKER

logging.basicConfig(level=logging.WARNING)


def _clear():
    os.system("cls" if os.name == "nt" else "clear")


def run_once(args) -> dict:
    """Fetch all data and return a result dict."""
    print("🔄 获取数据中…", end="\r")

    holdings_data = df_mod.get_holdings_and_nav()
    holdings_df   = holdings_data["holdings"]
    scraped_nav   = holdings_data["nav"]
    source        = holdings_data["source"]

    previous_nav  = pf.get_previous_nav(scraped_nav)
    tickers       = holdings_df["ticker"].tolist()
    prices        = pf.get_stock_prices(tickers)
    fx_today      = pf.get_fx_rates()
    fx_prev       = pf.get_fx_prev_closes(["KR", "JP", "TW"])
    fx_prev["US"] = 1.0

    inav_result = nav_mod.calculate_inav(
        holdings_df, prices, fx_today, fx_prev, previous_nav
    )
    etf_price    = pf.get_etf_price(ETF_TICKER)
    official_iiv = pf.get_etf_iiv(ETF_TICKER)
    pd_result    = nav_mod.calculate_premium_discount(etf_price, inav_result["estimated_nav"])
    mkt_status   = pf.all_market_statuses()

    return {
        "source":       source,
        "previous_nav": previous_nav,
        "inav":         inav_result,
        "etf_price":    etf_price,
        "official_iiv": official_iiv,
        "premium":      pd_result,
        "mkt_status":   mkt_status,
        "holdings_df":  holdings_df,
        "prices":       prices,
    }


def print_report(data: dict) -> None:
    """Pretty-print the monitoring report to terminal."""
    now_et = datetime.now(pytz.timezone("America/New_York"))
    now_cn = datetime.now(pytz.timezone("Asia/Shanghai"))

    inav    = data["inav"]
    premium = data["premium"]
    mkt     = data["mkt_status"]

    STATUS = {"open": "🟢 开市", "pre-market": "🟡 盘前", "closed": "🔴 休市"}

    sep = "=" * 64

    print(f"\n{sep}")
    print(f"  💾  DRAM ETF 净值监控  |  {now_et.strftime('%Y-%m-%d %H:%M:%S ET')}")
    print(f"  北京时间: {now_cn.strftime('%Y-%m-%d %H:%M:%S')}")
    print(sep)

    # ── Key numbers ────────────────────────────────────────────────────────────
    prev_nav_str = f"${data['previous_nav']:.4f}" if data["previous_nav"] else "未设置 (请在 config.py 填写 MANUAL_NAV_OVERRIDE)"
    inav_str     = (f"${inav['estimated_nav']:.4f}  ({inav['weighted_return_pct']:+.3f}%)"
                    if inav["estimated_nav"] else "N/A")
    price_str    = f"${data['etf_price']:.4f}" if data["etf_price"] else "N/A"
    iiv_str      = f"${data['official_iiv']:.4f}" if data["official_iiv"] else "不可用"
    prem_str     = (f"{premium['premium_pct']:+.3f}%  {premium['label']}"
                    if premium["premium_pct"] is not None else "N/A")

    print(f"  前日官方 NAV    : {prev_nav_str:>20}  (来源: {data['source']})")
    print(f"  估算 iNAV       : {inav_str:>20}")
    print(f"  官方 IIV         : {iiv_str:>20}")
    print(f"  ETF 市价         : {price_str:>20}")
    print(f"  溢/折价率        : {prem_str:>20}")
    print()

    # ── Market status ──────────────────────────────────────────────────────────
    print(f"  市场状态: 美国 {STATUS.get(mkt['US'],'?')}  |  韩国 {STATUS.get(mkt['KR'],'?')}  |  日本 {STATUS.get(mkt['JP'],'?')}")

    # ── Contribution breakdown ─────────────────────────────────────────────────
    print()
    print(f"  贡献拆分:")
    print(f"    美股持仓贡献: {inav['us_contribution_pct']:+.3f}%")
    print(f"    韩股持仓贡献: {inav['kr_contribution_pct']:+.3f}%")
    print(f"    日股持仓贡献: {inav['jp_contribution_pct']:+.3f}%")
    print(f"    数据覆盖率:   {inav['data_coverage']:.1f}%")

    if inav["missing_tickers"]:
        print(f"  ⚠️  缺数据: {', '.join(inav['missing_tickers'])}")

    # ── Holdings detail ────────────────────────────────────────────────────────
    detail = inav["holdings_detail"]
    if not detail.empty:
        print()
        print("  持仓明细 (按贡献排序):")
        print(f"  {'代码':<14} {'名称':<24} {'权重':>6}  {'涨跌幅':>8}  {'贡献':>8}  {'市场':>4}")
        print("  " + "-" * 70)
        for _, row in detail.iterrows():
            name  = str(row.get("name", row["ticker"]))[:22]
            wgt   = row.get("weight", 0)
            ret   = row.get("usd_return_pct", 0)
            ctb   = row.get("contribution_pct", 0)
            mkt_r = row.get("market", "")
            print(f"  {row['ticker']:<14} {name:<24} {wgt*100:>5.2f}%  {ret:>+7.2f}%  {ctb:>+7.4f}  {mkt_r:>4}")

    print(sep)


def to_json_safe(data: dict) -> dict:
    """Convert data to JSON-serialisable dict."""
    inav = dict(data["inav"])
    if not inav["holdings_detail"].empty:
        inav["holdings_detail"] = inav["holdings_detail"].to_dict(orient="records")
    else:
        inav["holdings_detail"] = []

    return {
        "timestamp":    datetime.now(timezone.utc).isoformat() + "Z",
        "etf_ticker":   ETF_TICKER,
        "source":       data["source"],
        "previous_nav": data["previous_nav"],
        "inav":         inav,
        "etf_price":    data["etf_price"],
        "official_iiv": data["official_iiv"],
        "premium":      data["premium"],
        "mkt_status":   data["mkt_status"],
    }


def main():
    parser = argparse.ArgumentParser(description="DRAM ETF 净值监控 CLI")
    parser.add_argument("--watch",  type=int, default=0,
                        metavar="SEC", help="循环刷新间隔秒数（0 = 单次）")
    parser.add_argument("--json",   action="store_true", help="输出 JSON 格式")
    parser.add_argument("--log",    action="store_true", help="显示调试日志")
    args = parser.parse_args()

    if args.log:
        logging.getLogger().setLevel(logging.INFO)

    try:
        if args.watch > 0:
            while True:
                if not args.json:
                    _clear()
                data = run_once(args)
                if args.json:
                    print(json.dumps(to_json_safe(data), indent=2, ensure_ascii=False))
                else:
                    print_report(data)
                    print(f"\n  下次刷新倒计时 {args.watch}s … (Ctrl+C 退出)")
                time.sleep(args.watch)
        else:
            data = run_once(args)
            if args.json:
                print(json.dumps(to_json_safe(data), indent=2, ensure_ascii=False))
            else:
                print_report(data)

    except KeyboardInterrupt:
        print("\n  退出。")
        sys.exit(0)


if __name__ == "__main__":
    main()
