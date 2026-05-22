# 💾 DRAM ETF 实时净值监控

> **Roundhill Memory ETF（CBOE: DRAM）** 盘中 iNAV 估算与溢价率监控工具

DRAM ETF 持仓横跨美国、韩国、日本、台湾四个市场，官方 IIV 数据经常不可用，导致盘中很难判断是否在溢价成交。本工具通过抓取各成分股实时价格，自行估算 iNAV，并与 ETF 市价对比，帮助在正确价位下单。

![主界面](docs/screenshot-dashboard.png)

![持仓贡献分解](docs/screenshot-holdings.png)

---

## ✨ 功能

| 功能 | 说明 |
|------|------|
| **iNAV 实时估算** | 按持仓权重加权各成分股涨跌幅，推算当前净值 |
| **溢/折价率** | `(ETF市价 − iNAV) / iNAV`，彩色警示标签 |
| **多市场时区处理** | 美股休市时自动归零美股贡献，只计入韩/日/台当日波动 |
| **持仓贡献拆分** | 每只股票对 iNAV 的贡献 (pp)，横向柱状图直观展示 |
| **自动抓取持仓** | 从 Roundhill 官网获取最新权重，失败时使用内置备用数据 |
| **网页端输入 NAV** | 侧边栏直接填写前日官方 NAV，点击保存立即生效 |
| **手动价格覆盖** | 侧边栏可覆盖任意 ticker 的价格，yfinance 失效时备用 |
| **60 秒自动刷新** | 页面自动刷新，无需手动操作 |
| **CLI 模式** | 终端输出，支持循环刷新和 JSON 输出 |

---

## 🖥️ Streamlit Dashboard 界面

启动后页面分为以下区域：

**顶部指标栏**
```
前日官方 NAV  │  估算 iNAV (+7.07%)  │  ETF 市价  │  溢价率 +1.23% ⚠️  │  官方 IIV
```

**溢价率警示**
- 溢价 > 1.5%：红色警告横幅，提示避免追高
- 折价 < -1.5%：蓝色提示，可能存在买入机会
- ±0.5% 以内：绿色确认，接近净值

**持仓贡献图**（按贡献绝对值排序）
- 横向柱状图，红色 = 正贡献，蓝色 = 负贡献
- 右侧按市场拆分：美股 / 韩股 / 日股贡献汇总

**完整持仓明细表**
```
代码        名称              市场   权重     现价      昨收      涨跌幅    贡献(pp)  状态   数据日期
000660.KS   SK Hynix          KR    26.75%   194100   194000   +0.31%   +0.083    ok    2026-05-22
MU          Micron(equity+…   US    26.91%   …        …         0.00%   +0.000    ok    2026-05-21  ← 美股休市时归零
```

**左侧边栏（⚙️ 手动输入）**
- 前日官方 NAV 输入框
- 价格手动覆盖表格（可动态增删行）
- 「保存并刷新」按钮 — 写入本地 `overrides.json`，即时生效

---

## 📋 前置要求

- Python **3.10+**
- 网络能访问 Yahoo Finance（yfinance）
- 不需要任何 API Key

---

## 🚀 快速启动

```bash
# 1. 克隆项目
git clone https://github.com/<your-username>/dram-monitor.git
cd dram-monitor

# 2. 安装依赖（建议用 venv）
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# 3. 启动
streamlit run app.py
```

浏览器自动打开 `http://localhost:8501`。

---

## 📅 每日使用流程

每个美股交易日只需一步操作：

1. 打开 [Roundhill 官网](https://www.roundhillinvestments.com/etf/dram/) → 找到 **NAV** 数值（如 `$49.77`）
2. 在 Dashboard **左侧边栏** 的「前日官方 NAV」输入框填入该数值
3. 点击「💾 保存并刷新」

之后页面每 60 秒自动刷新，iNAV 和溢价率会持续更新，无需再操作。

> NAV 数值保存在本地 `overrides.json`，重启后仍然有效，下个交易日更新一次即可。

---

## 💻 CLI 用法

```bash
# 单次输出（查看当前快照）
python cli.py

# 每 N 秒循环刷新（类 watch 命令）
python cli.py --watch 60

# 输出 JSON（接管道、写日志）
python cli.py --json

# 输出 JSON 并循环
python cli.py --json --watch 120

# 显示调试日志（排查数据问题）
python cli.py --log
```

CLI 输出示例：
```
================================================================
  💾  DRAM ETF 净值监控  |  2026-05-22 10:35:12 ET
  北京时间: 2026-05-22 22:35:12
================================================================
  前日官方 NAV    :             $49.7700
  估算 iNAV       :    $50.2134  (+0.890%)
  官方 IIV         :              不可用
  ETF 市价         :             $50.4500
  溢/折价率        :    +0.469%  接近净值 ✓

  市场状态: 美国 🟢 开市  |  韩国 🔴 休市  |  日本 🔴 休市

  持仓明细 (按贡献排序):
  代码           名称                       权重    涨跌幅    贡献   市场
  -----------------------------------------------------------------------
  000660.KS      SK Hynix                  26.75%  +0.31%  +0.0830   KR
  …
```

---

## 🧮 iNAV 计算原理

```
estimated_iNAV = previous_NAV × (1 + Σ weight_i × USD_return_i)

USD_return_i = (1 + local_return_i) × (1 + fx_return_i) − 1
```

| 变量 | 含义 |
|------|------|
| `local_return_i` | 成分股本地货币当日涨跌幅（直接取 yfinance 的 change_pct） |
| `fx_return_i` | 当日汇率变动，仅 KRW/JPY/TWD 生效（USD 为 0） |
| `previous_NAV` | 上一交易日官方净值（用户每日从 Roundhill 官网更新） |

**多市场时区关键处理**

yfinance 的 `period='1d', interval='1m'` 在美股收盘后仍会返回当日历史分钟线。程序会检查最后一根 K 线的时间戳是否属于今天（ET 时间），若不是则视为无新行情，强制 `return = 0`，避免将昨日美股涨跌重复计入已更新的官方 NAV。

---

## 📊 数据来源

| 数据 | 来源 | 更新频率 |
|------|------|----------|
| 持仓权重 | Roundhill 官网（程序自动抓取，4h 缓存）| 每个交易日 |
| 美股实时价 | yfinance 1min | 约 1 分钟延迟 |
| 韩/日/台股价 | yfinance 60min 盘中 | 当日收盘后固定 |
| 汇率（KRW/JPY/TWD）| yfinance 5min | 约 5 分钟延迟 |
| ETF 市价 | yfinance 1min | 约 1 分钟延迟 |
| 前日官方 NAV | 用户手动输入 | 每个交易日 |

> 所有数据均通过 [yfinance](https://github.com/ranaroussi/yfinance) 免费获取，无需 API Key。

---

## ⚙️ 配置说明

`config.py` 中可调整：

```python
REFRESH_INTERVAL_SEC = 60          # 刷新间隔（秒）

ZERO_RETURN_TICKERS = {"FGXXX", …} # 现金/货币基金，固定 0% 日收益

MANUAL_PRICE_OVERRIDES = {          # 备用：yfinance 无法获取时手动填写
    # "285A.T": {"price": 55670.0, "prev_close": 51290.0, "currency": "JPY"},
}

FALLBACK_HOLDINGS = [               # 官网抓取失败时的备用持仓（定期更新）
    {"ticker": "000660.KS", "name": "SK Hynix", "weight": 0.2675, "market": "KR"},
    …
]
```

---

## 🌐 VPS 部署

```bash
# screen 快速挂载
screen -S dram
streamlit run app.py --server.port 8501 --server.headless true
# Ctrl+A D 挂起，连接断开后继续运行
```

<details>
<summary>systemd service（推荐生产环境）</summary>

```ini
# /etc/systemd/system/dram-monitor.service
[Unit]
Description=DRAM ETF Monitor
After=network.target

[Service]
User=ubuntu
WorkingDirectory=/path/to/dram-monitor
ExecStart=streamlit run app.py --server.port 8501 --server.headless true
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable dram-monitor
sudo systemctl start  dram-monitor
sudo systemctl status dram-monitor
```

</details>

---

## ⚠️ 免责声明

本工具仅供个人学习与交易参考，iNAV 为估算值，非官方净值，与实际净值存在误差。市场数据有延迟，任何投资决策请自行承担风险。
