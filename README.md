# 💾 DRAM ETF 实时净值监控

Roundhill Memory ETF（CBOE: DRAM）盘中 iNAV 估算与溢价率监控工具。

**功能亮点**
- 实时估算 iNAV，与 ETF 市价对比，秒算溢/折价率
- 自动从 Roundhill 官网抓取持仓权重（失败时使用内置备用数据）
- 正确处理多市场时区：美股/韩股/日股/台股分开计算，避免重复计入已入账涨跌幅
- 网页侧边栏直接输入前日官方 NAV，无需改代码
- 60 秒自动刷新，带 TTL 缓存防触发限流

## 项目结构

```
dram_monitor/
├── app.py                 # Streamlit Dashboard（主界面）
├── cli.py                 # 纯命令行版本
├── config.py              # Ticker 映射、备用持仓、市场时区
├── data_fetcher.py        # 持仓 & NAV 抓取（Roundhill 官网）
├── price_fetcher.py       # 实时/收盘价格 & 汇率（yfinance）
├── nav_calculator.py      # iNAV 计算引擎 & 溢价率
├── overrides.py           # 运行时用户覆盖（读写 overrides.json）
├── overrides.example.json # overrides.json 模板（复制后重命名使用）
├── requirements.txt
└── .env.example
```

> `overrides.json` 在首次运行时自动创建，已加入 `.gitignore`，不会提交到仓库。

## 快速启动

```bash
# 1. 克隆并安装依赖
git clone https://github.com/<your-username>/dram-monitor.git
cd dram-monitor
pip install -r requirements.txt

# 2. 启动 Streamlit（推荐）
streamlit run app.py
```

浏览器打开后，在**左侧边栏**输入前日官方 NAV（从 [Roundhill 官网](https://www.roundhillinvestments.com/etf/dram/) 获取），点击「保存并刷新」即可。

```bash
# 或使用 CLI
python cli.py             # 单次输出
python cli.py --watch 60  # 每 60 秒刷新
python cli.py --json      # JSON 格式（接管道/日志）
```

## iNAV 计算逻辑

```
estimated_iNAV = previous_NAV × (1 + Σ weight_i × USD_return_i)

USD_return_i = (1 + local_return_i) × (1 + fx_return_i) − 1

local_return_i  — 各成分股本地货币当日涨跌幅
fx_return_i     — 当日汇率变动（仅 KR/JP/TW 生效；USD 为 0）
```

**多市场时区处理关键点**

| 时段 | 美股 | 韩/日/台 |
|------|------|---------|
| 亚洲开市（美股休市）| `return = 0`（收盘价已入上日 NAV）| 使用当日实时/收盘涨跌幅 |
| 美股开盘中 | 使用分钟级实时涨跌幅 | 使用当日收盘涨跌幅（已固定）|

> 判断依据：`yfinance` 1-min 数据是否属于今日（ET 时间）。若盘中数据来自上一交易日，则视为无新行情，`return = 0`，避免重复计入。

## 数据来源

| 数据 | 来源 | 延迟 |
|------|------|------|
| 持仓权重 | Roundhill 官网（每日更新，缓存 4h）| T+1 |
| 美股实时价 | yfinance 1min | ~1 分钟 |
| 韩/日/台股价 | yfinance 60min 盘中 | 当日收盘后固定 |
| 汇率 | yfinance 5min | ~5 分钟 |
| ETF 市价 | yfinance 1min | ~1 分钟 |
| 前日官方 NAV | 用户在侧边栏手动输入 | 每交易日更新一次 |

## 溢价率参考

| 溢价率 | 提示 |
|--------|------|
| > 3% | 极高溢价，严禁市价追单 |
| 1.5% ~ 3% | 高溢价，建议限价或等回落 |
| 0.5% ~ 1.5% | 轻度溢价，可操作，控制仓位 |
| ±0.5% | 正常区间，限价买入较安全 |
| -1.5% ~ -0.5% | 轻度折价，合理介入 |
| < -1.5% | 折价，关注是否有利空消息 |

## 部署到 VPS

```bash
# screen 后台运行
screen -S dram
streamlit run app.py --server.port 8501 --server.headless true
# Ctrl+A D 挂起
```

systemd service 示例：

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

[Install]
WantedBy=multi-user.target
```

```bash
systemctl enable dram-monitor && systemctl start dram-monitor
```

## 扩展到其他 ETF

修改 `config.py` 中以下字段，并替换 `data_fetcher.py` 中的 `ROUNDHILL_URL`：

```python
ETF_TICKER      = "SMH"
ROUNDHILL_URL   = "https://www.vaneck.com/etf/equity/smh/..."
FALLBACK_HOLDINGS = [...]
```

## 免责声明

本工具仅供个人学习与交易参考，iNAV 为估算值，非官方净值。所有投资决策请自行判断风险。
