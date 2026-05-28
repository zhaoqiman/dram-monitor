"""
DRAM ETF 实时净值与溢价率监控 — Streamlit Dashboard
运行: streamlit run app.py
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pandas as pd
import plotly.graph_objects as go
import pytz
import streamlit as st
from streamlit_autorefresh import st_autorefresh

import data_fetcher as df_mod
import nav_calculator as nav_mod
import overrides as ov_mod
import price_fetcher as pf
from config import ETF_TICKER, MANUAL_NAV_OVERRIDE, REFRESH_INTERVAL_SEC

logging.basicConfig(level=logging.WARNING,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

st.set_page_config(
    page_title="DRAM ETF 净值监控",
    page_icon="💾",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Auto refresh ───────────────────────────────────────────────────────────────
count = st_autorefresh(interval=REFRESH_INTERVAL_SEC * 1000, limit=None, key="autorefresh")

# ── Custom CSS ─────────────────────────────────────────────────────────────────
st.markdown("""
<style>
    .metric-card {
        background: #1e1e2e;
        border-radius: 12px;
        padding: 18px 22px;
        border: 1px solid #333;
    }
    .metric-label  { font-size: 13px; color: #888; margin-bottom: 4px; }
    .metric-value  { font-size: 30px; font-weight: 700; color: #f0f0f0; }
    .metric-sub    { font-size: 13px; color: #aaa; margin-top: 4px; }
    .premium-high  { color: #ff4444 !important; }
    .premium-low   { color: #66bb6a !important; }
    .market-open   { color: #66bb6a; font-size: 11px; }
    .market-closed { color: #888;    font-size: 11px; }
    .stDataFrame   { font-size: 13px; }
    div[data-testid="stMetricValue"] { font-size: 28px; }
</style>
""", unsafe_allow_html=True)


# ── Sidebar: Manual overrides ──────────────────────────────────────────────────
with st.sidebar:
    st.header("⚙️ 手动输入")

    _ov_data = ov_mod.load()

    st.markdown("**前日官方 NAV ($)**")
    st.caption("系统已自动从 Roundhill 抓取。填入非 0 值可强制覆盖自动值。")
    _nav_default = float(_ov_data.get("manual_nav") or MANUAL_NAV_OVERRIDE or 0.0)
    nav_input = st.number_input(
        "NAV",
        value=_nav_default,
        min_value=0.0,
        max_value=10_000.0,
        step=0.01,
        format="%.4f",
        label_visibility="collapsed",
        help="输入 0 表示未知（iNAV 将无法计算）",
    )

    st.divider()
    st.markdown("**价格手动覆盖**")
    st.caption("仅在对应 ticker 状态为 error 时填写")

    _existing_prices = _ov_data.get("manual_prices", {})
    _price_rows = [
        {"ticker": k,
         "price":      float(v.get("price",      0)),
         "prev_close": float(v.get("prev_close",  0)),
         "currency":   v.get("currency", "USD")}
        for k, v in _existing_prices.items()
    ] or [{"ticker": "", "price": 0.0, "prev_close": 0.0, "currency": "USD"}]

    edited_prices = st.data_editor(
        pd.DataFrame(_price_rows),
        num_rows="dynamic",
        width="stretch",
        column_config={
            "ticker":     st.column_config.TextColumn("代码",  width="small"),
            "price":      st.column_config.NumberColumn("现价",  format="%.2f"),
            "prev_close": st.column_config.NumberColumn("昨收",  format="%.2f"),
            "currency":   st.column_config.SelectboxColumn(
                "币种", options=["USD", "KRW", "JPY", "TWD"], width="small"),
        },
    )

    st.markdown("")
    if st.button("💾 保存并刷新", type="primary", width="stretch"):
        new_ov = dict(_ov_data)
        new_ov["manual_nav"] = float(nav_input) if nav_input > 0 else None
        new_prices: dict = {}
        for _, row in edited_prices.iterrows():
            t = str(row.get("ticker", "")).strip().upper()
            if t and float(row.get("price", 0)) > 0:
                new_prices[t] = {
                    "price":      float(row["price"]),
                    "prev_close": float(row.get("prev_close", 0)),
                    "currency":   str(row.get("currency", "USD")),
                }
        new_ov["manual_prices"] = new_prices
        ov_mod.save(new_ov)
        st.cache_data.clear()
        st.success("已保存！")
        time.sleep(0.4)
        st.rerun()


# ── Header ─────────────────────────────────────────────────────────────────────
col_title, col_refresh = st.columns([5, 1])
with col_title:
    st.title("💾 DRAM ETF 实时净值监控")
    st.caption("Roundhill Memory ETF (CBOE: DRAM) · 自动刷新每 60 秒")
with col_refresh:
    st.metric("刷新次数", count)
    now_et = datetime.now(pytz.timezone("America/New_York"))
    st.caption(f"🕐 {now_et.strftime('%H:%M:%S ET')}")


# ── Load data (cached in session state to survive reruns between refreshes) ────

@st.cache_data(ttl=REFRESH_INTERVAL_SEC - 5, show_spinner=False)
def load_all():
    """
    Fetch everything in two parallel batches:
      Batch 1 (parallel): holdings, FX rates, ETF price, IIV
      Batch 2 (parallel): stock prices + prev NAV (after batch 1 for holdings list)
    """
    # ── Batch 1: holdings + FX + ETF price + IIV (parallel) ─────────────────
    with ThreadPoolExecutor(max_workers=5) as ex:
        f_holdings  = ex.submit(df_mod.get_holdings_and_nav)
        f_fx_today  = ex.submit(pf.get_fx_rates)
        f_fx        = ex.submit(pf.get_fx_prev_closes, ["KR", "JP", "TW"])
        f_etf_price = ex.submit(pf.get_etf_price, ETF_TICKER)
        f_iiv       = ex.submit(pf.get_etf_iiv, ETF_TICKER)

    holdings_data = f_holdings.result()
    holdings_df   = holdings_data["holdings"]
    scraped_nav   = holdings_data["nav"]
    data_source   = holdings_data["source"]
    holdings_as_of = holdings_data.get("as_of", "")
    fx_today      = f_fx_today.result()
    fx_prev       = f_fx.result()
    fx_prev["US"] = 1.0
    etf_price     = f_etf_price.result()
    official_iiv  = f_iiv.result()

    # ── Batch 2: stock prices + prev NAV ─────────────────────────────────────
    tickers = holdings_df["ticker"].tolist()
    prices       = pf.get_stock_prices(tickers)
    previous_nav = pf.get_previous_nav(scraped_nav)
    mkt_status   = pf.all_market_statuses()   # 需先于 calculate_inav，传入时区感知逻辑

    # ── Compute iNAV and premium ──────────────────────────────────────────────
    inav_result = nav_mod.calculate_inav(
        holdings_df, prices, fx_today, fx_prev, previous_nav,
        mkt_statuses=mkt_status,
    )
    pd_result   = nav_mod.calculate_premium_discount(etf_price, inav_result["estimated_nav"])

    return {
        "holdings_df":   holdings_df,
        "data_source":   data_source,
        "holdings_as_of": holdings_as_of,
        "previous_nav":  previous_nav,
        "prices":        prices,
        "fx_today":      fx_today,
        "inav":          inav_result,
        "etf_price":     etf_price,
        "official_iiv":  official_iiv,
        "premium":       pd_result,
        "mkt_status":    mkt_status,
        "loaded_at":     datetime.now(timezone.utc).isoformat(),
    }


with st.spinner("获取最新数据…"):
    try:
        data = load_all()
    except Exception as e:
        st.error(f"数据加载失败：{e}")
        st.stop()


inav    = data["inav"]
premium = data["premium"]
mkt     = data["mkt_status"]

# ── Data source banner ─────────────────────────────────────────────────────────
_src    = data.get("data_source", "")
_as_of  = data.get("holdings_as_of", "")
_as_of_str = f"（持仓日期：{_as_of}）" if _as_of else ""
if _src == "csv":
    st.success(
        f"✅ 持仓与 NAV 已从 **Roundhill CSV 端点**自动抓取{_as_of_str}，每日自动更新。",
        icon="📡",
    )
elif _src == "fallback":
    st.info(
        "⚠️ 持仓数据来自 **本地备用配置**（CSV 端点暂时不可访问）。"
        " 权重使用上次已知数据，计算结果仍有参考价值。",
        icon="📋",
    )

# ── Row 1: Key metrics ─────────────────────────────────────────────────────────
st.divider()
c1, c2, c3, c4, c5 = st.columns(5)

with c1:
    nav_val = f"${data['previous_nav']:.4f}" if data['previous_nav'] else "未设置"
    st.metric("前日官方 NAV", nav_val,
              help="上一个美股交易日 Roundhill 公布的净值")
    if not data['previous_nav']:
        st.caption("⚠️ 请在左侧边栏输入")

with c2:
    inav_val = f"${inav['estimated_nav']:.4f}" if inav["estimated_nav"] else "N/A"
    delta_nav = (
        f"{inav['weighted_return_pct']:+.2f}%" if inav["estimated_nav"] else None
    )
    st.metric("估算 iNAV", inav_val, delta=delta_nav,
              help="基于持仓实时/最新涨跌幅推算的盘中净值")

with c3:
    price_val = f"${data['etf_price']:.4f}" if data["etf_price"] else "N/A"
    st.metric("ETF 市价", price_val,
              help="DRAM 当前二级市场成交价（1分钟延迟）")

with c4:
    prem = premium["premium_pct"]
    if prem is not None:
        prem_str = f"{prem:+.3f}%"
        delta_color = "inverse" if prem > 0 else "normal"
    else:
        prem_str = "N/A"
        delta_color = "off"
    st.metric(f"溢价率  {premium['label']}", prem_str,
              help="(ETF市价 − iNAV) / iNAV × 100%")

with c5:
    if data["official_iiv"]:
        iiv_val = f"${data['official_iiv']:.4f}"
        help_text = "Yahoo Finance DRAM.IV / Cboe IOPV"
    else:
        iiv_val = "N/A（新品无数据）"
        help_text = "DRAM 为新产品，Yahoo Finance 尚无 IIV 数据；iNAV 已提供实时估算"
    st.metric("官方 IIV (DRAM.IV)", iiv_val, help=help_text)


# ── Premium alert ──────────────────────────────────────────────────────────────
prem = premium["premium_pct"]
if prem is not None:
    if prem > 1.5:
        st.error(f"⚠️  **高溢价警告：{prem:+.3f}%**  ·  ETF 市价比估算净值高 ${premium['delta_usd']:.4f}。"
                 f"  建议使用**限价单**，避免追高。", icon="🔴")
    elif prem < -1.5:
        st.info(f"💡  **折价机会：{prem:+.3f}%**  ·  ETF 市价比估算净值低 ${abs(premium['delta_usd']):.4f}。",
                icon="🔵")
    elif -0.5 <= prem <= 0.5:
        st.success(f"✅  当前溢价率 {prem:+.3f}%，接近净值，适合操作。", icon="✅")


# ── Row 2: Market status + FX ──────────────────────────────────────────────────
st.divider()
st.subheader("市场状态 & 汇率")
col_mkt, col_fx = st.columns([2, 3])

STATUS_EMOJI = {"open": "🟢 开市", "pre-market": "🟡 盘前", "closed": "🔴 休市", "unknown": "⚪ 未知"}

with col_mkt:
    mkt_df = pd.DataFrame([
        {"市场": "美国 🇺🇸", "状态": STATUS_EMOJI.get(mkt["US"], mkt["US"])},
        {"市场": "韩国 🇰🇷", "状态": STATUS_EMOJI.get(mkt["KR"], mkt["KR"])},
        {"市场": "日本 🇯🇵", "状态": STATUS_EMOJI.get(mkt["JP"], mkt["JP"])},
    ])
    st.dataframe(mkt_df, width="stretch", hide_index=True)

with col_fx:
    fx = data["fx_today"]
    fx_df = pd.DataFrame([
        {"货币对":    "KRW/USD", "汇率": f"{fx.get('KR', 0):.6f}",
         "参考": f"1 美元 ≈ {1/fx.get('KR',0.00073):.0f} 韩元" if fx.get("KR") else ""},
        {"货币对":    "JPY/USD", "汇率": f"{fx.get('JP', 0):.5f}",
         "参考": f"1 美元 ≈ {1/fx.get('JP',0.0067):.1f} 日元"   if fx.get("JP") else ""},
    ])
    st.dataframe(fx_df, width="stretch", hide_index=True)


# ── Row 3: Contribution breakdown bar chart ────────────────────────────────────
st.divider()
st.subheader("持仓贡献分解")

detail_df = inav["holdings_detail"]

if not detail_df.empty:
    col_chart, col_split = st.columns([3, 1])

    with col_chart:
        # Top 15 by absolute contribution
        chart_df = detail_df.head(15).copy()
        colors = [
            "#FF4444" if v > 0 else "#4488FF"
            for v in chart_df["contribution_pct"]
        ]
        fig = go.Figure(go.Bar(
            x=chart_df["contribution_pct"],
            y=chart_df.get("name", chart_df["ticker"]),
            orientation="h",
            marker_color=colors,
            text=[f"{v:+.3f}%" for v in chart_df["contribution_pct"]],
            textposition="outside",
        ))
        fig.update_layout(
            title="各持仓对 iNAV 的贡献 (Top 15)",
            xaxis_title="贡献百分点 (%)",
            yaxis=dict(autorange="reversed"),
            height=420,
            margin=dict(l=10, r=10, t=40, b=10),
            paper_bgcolor="#0e1117",
            plot_bgcolor="#0e1117",
            font=dict(color="#ccc"),
        )
        st.plotly_chart(fig, width="stretch")

    with col_split:
        st.markdown("**市场贡献拆分**")

        # 根据 calculate_inav 返回的 contributing_markets 判断各市场是否计入
        _ctb = set(inav.get("contributing_markets", []))
        def _mkt_tag(code: str) -> str:
            return "✅" if code in _ctb else "⏸️"

        split_df = pd.DataFrame([
            {"市场": f"{_mkt_tag('US')} 🇺🇸 美国",
             "贡献": f"{inav['us_contribution_pct']:+.3f}%"},
            {"市场": f"{_mkt_tag('KR')} 🇰🇷 韩国",
             "贡献": f"{inav['kr_contribution_pct']:+.3f}%"},
            {"市场": f"{_mkt_tag('JP')} 🇯🇵 日本",
             "贡献": f"{inav['jp_contribution_pct']:+.3f}%"},
            {"市场": f"{_mkt_tag('TW')} 🇹🇼 台湾",
             "贡献": f"{inav.get('tw_contribution_pct', 0.0):+.3f}%"},
            {"市场": "🌍 合计",
             "贡献": f"{inav['weighted_return_pct']:+.3f}%"},
        ])
        st.dataframe(split_df, hide_index=True, width="stretch")

        # 提示当前哪个时段，帮助用户理解 iNAV 计算模式
        _us_st = mkt.get("US", "unknown")
        if _us_st == "open":
            st.caption("🟢 美股开市：日韩台 + 美股贡献均已计入")
        elif _us_st in ("pre-market",):
            st.caption("🟡 美股盘前：仅日韩台贡献已固定，美股数据有限")
        else:
            _any_asian_open = any(mkt.get(m) == "open" for m in ("KR", "JP", "TW"))
            if _any_asian_open:
                st.caption("⏸️ 美股休市：仅计入日韩台实时贡献，美股已包含于官方 NAV")
            else:
                st.caption("🔴 各市场均休市：贡献数据为最后收盘状态")

        st.metric("数据覆盖率", f"{inav['data_coverage']:.1f}%")
        if inav["missing_tickers"]:
            st.warning("缺数据:\n" + ", ".join(inav["missing_tickers"]))


# ── Row 4: Full holdings table ─────────────────────────────────────────────────
st.divider()
st.subheader("完整持仓明细")

if not detail_df.empty:
    show_cols = ["ticker", "name", "market", "weight",
                 "price", "prev_close", "usd_return_pct", "contribution_pct", "status", "data_date"]
    show_cols = [c for c in show_cols if c in detail_df.columns]
    display_df = detail_df[show_cols].copy()

    display_df.columns = [
        c.replace("usd_return_pct", "涨跌幅(%USD)")
         .replace("contribution_pct", "贡献(pp)")
         .replace("prev_close", "昨收")
         .replace("weight", "权重")
         .replace("market", "市场")
         .replace("status", "状态")
         .replace("price", "现价")
         .replace("ticker", "代码")
         .replace("name", "名称")
         .replace("data_date", "数据日期")
        for c in display_df.columns
    ]

    # Format numerics
    for col in ["权重"]:
        if col in display_df.columns:
            display_df[col] = display_df[col].apply(lambda x: f"{x*100:.2f}%" if pd.notna(x) else "")

    def _color_contrib(val):
        try:
            v = float(val)
            if v > 0:   return "color: #FF6666"
            if v < 0:   return "color: #66AAFF"
        except Exception:
            pass
        return ""

    def _color_status(val):
        if str(val) == "ok": return "color: #66bb6a"
        return "color: #ff7043"

    styled = (
        display_df.style
        .map(_color_contrib, subset=["贡献(pp)"] if "贡献(pp)" in display_df.columns else [])
        .map(_color_status,  subset=["状态"]     if "状态"     in display_df.columns else [])
        .format({"涨跌幅(%USD)": "{:+.3f}", "贡献(pp)": "{:+.4f}",
                 "现价": "{:.4f}", "昨收": "{:.4f}"},
                na_rep="—")
    )
    st.dataframe(styled, width="stretch", height=420)
else:
    st.warning("持仓数据暂不可用")


# ── Row 5: Calculation detail (expandable) ────────────────────────────────────
with st.expander("📐 计算说明 & 数据来源"):
    st.markdown(f"""
**iNAV 估算公式**
```
estimated_iNAV = previous_NAV × (1 + Σᵢ weight_i × USD_return_i)

USD_return_i = (price_today_i × FX_today) / (prev_close_i × FX_prev) − 1
```

**数据来源**
- 持仓权重：Roundhill 官网（`{data['data_source']}`）
- 前日 NAV：Roundhill / yfinance 前日收盘
- 美股实时价格：Yahoo Finance（1分钟延迟）
- 日韩股价：Yahoo Finance `.KS` / `.T`（当日收盘）
- 汇率：Yahoo Finance KRWUSD=X / JPYUSD=X
- 市价：yfinance `{ETF_TICKER}`

**多市场时区感知逻辑**
- **美股开市（ET 9:30–16:00）**：日韩台已于当日收盘，用今日亚洲收盘 vs 昨收；美股用实时价格 vs 昨收 → 全部计入
- **亚洲开市（美股休市）**：日韩台用实时价格 vs 昨收；美股最新报价 = 昨收 = 已记入官方 NAV → 美股贡献强制归零，避免双重计入
- **美股收盘后至亚洲开市前**：美股用今日收盘 vs 昨收（今日数据已确认），日韩台固定于当日收盘 → 全部计入
- FX 汇率日内微调对 iNAV 有小幅影响

**溢价率含义**
- 溢价 > 1.5%：做市商或流动性因素推高价格，高追风险
- 折价 < −1.5%：可能存在买入机会，但需核实无特殊事件

数据更新时间（UTC）：`{data['loaded_at']}`
    """)


# ── Footer ──────────────────────────────────────────────────────────────────────
st.divider()
st.caption(
    "⚠️ 本工具仅供交易参考，iNAV 为估算值，非官方净值。"
    "投资决策请自行判断风险。"
    f"  |  数据源: Yahoo Finance / Roundhill  |  下次刷新: {REFRESH_INTERVAL_SEC}s"
)
