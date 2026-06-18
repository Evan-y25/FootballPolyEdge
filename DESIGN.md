# FootballPolyEdge — 设计文档

> 目标：实时抓取 Polymarket 世界杯（FIFA World Cup）所有比赛的订单簿数据（**1X2 胜平负** + **波胆/Exact Score**），通过 WebSocket 实时更新并展示到网页上，同时计算并高亮 **edge / 套利机会**。

---

## 1. 项目目标

1. **实时数据**：参考 `ReferenceProject/polymarket-trading-bot` 的方案，以 WebSocket 方式实时获取 Polymarket CLOB 的订单簿数据。
2. **覆盖范围**：自动发现世界杯下所有比赛，对每场比赛抓取：
   - **1X2（胜平负）**：主胜 / 平局 / 客胜，3 个二元市场。
   - **波胆（Exact Score）**：每场 17 个比分二元市场（含 "Any Other Score"）。
3. **网页展示**：在网页上实时展示每场比赛的盘口（best bid / best ask / 深度），并标注套利信号。
4. **Edge 套利**：实时计算市场内与跨市场的套利空间并高亮。

---

## 2. 数据来源调研结论（已实测）

### 2.1 世界杯赛事定位

`GET https://gamma-api.polymarket.com/sports` 中：

```json
{ "id": 174, "sport": "fifwc", "series": "11433", "tags": "1, 100639, 100350, 102232" }
```

即 **FIFA World Cup → `series_id = 11433`**（前端对应 `polymarket.com/zh/sports/world-cup/games`）。

### 2.2 赛事发现接口

```
GET https://gamma-api.polymarket.com/events?series_id=11433&closed=false&limit=300
```

实测返回 100 个 event，分两类（按每个 event 的 market 数量区分）：

| 类型 | event slug 形式 | market 数 | 含义 |
|------|----------------|-----------|------|
| **1X2** | `fifwc-{home}-{away}-{date}` | **3** | 主胜 / 平 / 客胜 |
| **波胆** | `fifwc-{home}-{away}-{date}-exact-score` | **17** | 各比分 |

**配对规则**：波胆 event 的 slug = `{1X2 event slug}-exact-score`，据此把两类 event 合并为「一场比赛」。

### 2.3 单个 Market 的关键字段

```jsonc
{
  "id": "1897115",
  "question": "Will Czechia win on 2026-06-18?",
  "groupItemTitle": "Czechia",            // 用于分类：home/away 队名，或 "Draw (...)", 或比分 "Czechia 1 - 0 South Africa"
  "outcomes": "[\"Yes\", \"No\"]",        // 二元市场
  "clobTokenIds": "[\"<YES_token>\", \"<NO_token>\"]",  // ★ WebSocket 订阅用的 token id
  "outcomePrices": "[\"0.71\", \"0.29\"]",// 初始价（兜底）
  "bestBid": 0.71, "bestAsk": 0.72,        // 初始盘口（兜底）
  "conditionId": "0x...",
  "active": true, "closed": false, "acceptingOrders": true
}
```

### 2.4 分类逻辑（从 event 拆出 1X2 三腿）

event title 形如 `"Czechia vs. South Africa"` → `home="Czechia"`, `away="South Africa"`。

- `groupItemTitle` 以 `"Draw"` 开头 → **平局 (draw)**
- 否则按 `groupItemTitle` 与 home/away 队名匹配 → **主胜 / 客胜**

波胆 event：每个 market 的 `groupItemTitle` 即比分标签（如 `"Czechia 1 - 0 South Africa"`、`"Exact Score: Any Other Score"`）。

### 2.5 token 数量估算（订阅规模）

- 每场 1X2：3 market × 2 token = **6 token**
- 每场波胆：17 market × 2 token = **34 token**
- 每场合计约 **40 token**

为控制订阅规模与相关性，默认只订阅 **近 N 天内开赛 + 进行中** 的比赛（`SUBSCRIBE_WINDOW_DAYS`，默认 3 天），可配置为全部。

---

## 3. 实时数据通道（参考 ReferenceProject）

复用并裁剪 `ReferenceProject/.../src/websocket_client.py` 的 `MarketWebSocket`：

- 端点：`wss://ws-subscriptions-clob.polymarket.com/ws/market`
- 订阅消息：`{"assets_ids": [...token_ids...], "type": "MARKET"}`
- 事件类型：
  - `book` —— 全量订单簿快照（bids/asks），落入缓存
  - `price_change` —— 增量价格变动
  - `last_trade_price` —— 最近成交
- 自带 **自动重连 / ping-pong / 订阅恢复**。

> 单条 WS 连接订阅大量 asset 时可能有上限，订阅模块支持**分批**（chunk）成多个 token 组，必要时开多条连接。

---

## 4. 系统架构

```
                ┌────────────────────────────────────────────┐
                │                Backend (Python)             │
                │                                            │
  Gamma REST ──▶│  gamma.py     发现世界杯比赛 / 解析 token   │
                │      │                                      │
                │      ▼                                      │
                │  state.py     比赛&token映射 + 订单簿缓存    │◀── 计算 Edge
                │      ▲                                      │
  CLOB WS    ──▶│  ws_client.py 实时 book / price_change     │
                │      │                                      │
                │      ▼                                      │
                │  server.py (aiohttp)                        │
                │   - GET /            前端页面                │
                │   - GET /api/games   全量快照(JSON)          │
                │   - WS  /ws          实时推送给浏览器         │
                └───────────────┬────────────────────────────┘
                                │  WebSocket / JSON
                                ▼
                ┌────────────────────────────────────────────┐
                │            Frontend (原生 HTML/JS)           │
                │  - 比赛卡片：1X2 盘口 + 波胆比分网格           │
                │  - 实时更新（WS 推送）                        │
                │  - Edge / 套利信号高亮                        │
                └────────────────────────────────────────────┘
```

**技术选型**：后端 Python + `aiohttp`（已安装，HTTP+WS 一体，无需 FastAPI）+ `websockets`（已安装）。前端原生 HTML/CSS/JS（零构建，开箱即用）。

---

## 5. 后端模块设计

### 5.1 `app/gamma.py` —— 赛事发现
- `async fetch_world_cup_games() -> List[Game]`
  1. 拉取 `events?series_id=11433&closed=false`（分页）。
  2. 按 slug 把 1X2 event 与 `-exact-score` event 配对成 `Game`。
  3. 解析每个 market 的 `clobTokenIds`、`groupItemTitle`，构建 token 映射与初始价。
- 数据结构：
  ```python
  Outcome(market_id, label, yes_token, no_token, init_yes, init_no)
  OneX2(home: Outcome, draw: Outcome, away: Outcome)
  ScoreBook(scores: List[Outcome])          # 17 个比分
  Game(slug, home, away, kickoff, status, onex2: OneX2, scores: ScoreBook)
  ```

### 5.2 `app/ws_client.py` —— 实时订单簿
- 裁剪自 ReferenceProject 的 `MarketWebSocket`。
- 维护 `orderbooks: Dict[token_id, Orderbook]`。
- 暴露 `on_update(callback)`：每次 book/price_change 后触发，用于增量推送前端。
- 支持 `subscribe(token_ids)` 分批订阅。

### 5.3 `app/state.py` —— 全局状态 + Edge 计算
- 持有：`games`、`token_index`（token → (game, leg, side)）、`orderbook` 引用。
- `snapshot()`：把当前所有比赛 + 实时价格 + edge 序列化成前端 JSON。
- **Edge 计算**（见 §6）。

### 5.4 `app/server.py` —— Web 服务
- `GET /`：返回前端页面。
- `GET /api/games`：返回全量快照（首屏 / 刷新用）。
- `WS /ws`：浏览器连上后，先发全量快照，之后按节流（如 250ms）推送变化。

### 5.5 `app/main.py` —— 启动编排
1. 发现比赛 → 2. 建立 CLOB WS 并订阅所有 token → 3. 启动 aiohttp server → 4. 定时（如每 60s）重新发现比赛以纳入新开赛/移除已结束。

---

## 6. Edge / 套利计算逻辑

记 `ask(x)` = 买入 x 的 YES 的最优卖价；`bid(x)` = 卖出 x 的 YES 的最优买价。每个二元市场赔付为 1。

### 6.1 1X2 市场内套利
- **正套（全买）**：`cost = ask(home)+ask(draw)+ask(away)`。若 `cost < 1` → 无风险套利，`edge = 1 - cost`。
- **反套（全卖）**：`credit = bid(home)+bid(draw)+bid(away)`。若 `credit > 1` → 套利，`edge = credit - 1`。
- **Overround（抽水）指标**：`ask(home)+ask(draw)+ask(away)` 与 1 的偏离，用于展示市场是否“便宜”。

### 6.2 波胆市场内套利
- 17 个比分互斥且穷尽（含 "Any Other Score"）。
- **正套**：`Σ ask(score_i) < 1` → `edge = 1 - Σ`。
- **反套**：`Σ bid(score_i) > 1` → `edge = Σ - 1`。

### 6.3 跨市场一致性 Edge（1X2 ↔ 波胆）
比分可聚合回 1X2：
- 平局 = 比分中 `0-0,1-1,2-2,3-3,...`（主客比分相等）之和。
- 主胜 = 主进球 > 客进球 的比分之和。
- 客胜 = 反之。

对每条腿 `L`：
- 若 `ask(L_via_1x2) < Σ bid(对应比分)` → 买 1X2、卖对应波胆组合，存在 edge。
- 若 `Σ ask(对应比分) < bid(L_via_1x2)` → 买波胆组合、卖 1X2，存在 edge。

> 注意 "Any Other Score" 无法归到具体某条腿，跨市场计算时作为不确定项单列，仅在可严格判定时才标记套利（避免误报）。

### 6.4 展示口径
- 每条 edge 标注：类型（1X2正套/反套/波胆/跨市场）、所需操作、理论收益（按 best price，**不计**手续费/滑点）、可成交量（取相关腿盘口 size 的最小值）。
- 明确提示：**未扣除手续费、gas、滑点，且需可同时成交**；为信号参考而非自动下单。

---

## 7. 前端展示设计

单页应用（`frontend/`）：

- **顶部状态条**：WS 连接状态、已订阅 token 数、最后更新时间、套利机会计数。
- **比赛列表**：按开赛时间排序，每场一张卡片：
  - 标题：`主队 vs 客队`、开赛时间、状态（进行中/未开始）。
  - **1X2 区**：主胜 / 平 / 客胜，各显示 best bid / best ask（¢）、深度，正反套 sum 实时标色。
  - **波胆区**：比分网格（如按主队进球×客队进球排布 + Any Other），显示每格 YES 价，最优比分高亮。
  - **Edge 徽章**：该场存在套利时高亮整张卡片并列出机会明细。
- **实时性**：通过 `/ws` 接收增量，变动的数字做闪烁提示。
- **筛选**：按日期 / 仅显示有 edge 的比赛。

---

## 8. API 设计

### `GET /api/games`
```jsonc
{
  "updated_at": 1750000000,
  "ws_connected": true,
  "subscribed_tokens": 1280,
  "games": [
    {
      "slug": "fifwc-cze-rsa-2026-06-18",
      "home": "Czechia", "away": "South Africa",
      "kickoff": "2026-06-18T...Z", "status": "live",
      "onex2": {
        "home": {"bid": 0.71, "ask": 0.72, "bid_size": 1200, "ask_size": 800},
        "draw": {"bid": 0.20, "ask": 0.21, ...},
        "away": {"bid": 0.07, "ask": 0.08, ...}
      },
      "scores": [ {"label": "1 - 0", "bid": .., "ask": ..}, ... ],
      "edges": [
        {"type": "1x2_back_arb", "edge": 0.012, "detail": "buy H+D+A asks sum=0.988", "size": 800}
      ]
    }
  ]
}
```

### `WS /ws`
- 连接后推送一帧 `{"type":"snapshot", ...}`（同上）。
- 之后推送 `{"type":"update","games":[变动的比赛...]}`，节流 250ms。

---

## 9. 目录结构

```
FootballPolyEdge/
├── DESIGN.md                 # 本文档
├── README.md                 # 运行说明
├── requirements.txt
├── run.py                    # 入口（python run.py）
├── app/
│   ├── __init__.py
│   ├── config.py             # 端口/series_id/订阅窗口等
│   ├── gamma.py              # 世界杯赛事发现 + token 解析
│   ├── ws_client.py          # CLOB 市场 WebSocket（裁剪自 ReferenceProject）
│   ├── state.py              # 全局状态 + Edge 计算
│   ├── server.py             # aiohttp: / , /api/games , /ws
│   └── main.py               # 启动编排
└── frontend/
    ├── index.html
    ├── styles.css
    └── app.js
```

---

## 10. 运行方式

```bash
pip install -r requirements.txt     # aiohttp, websockets, requests
python run.py                       # 启动后访问 http://localhost:8080
```

可配置环境变量：
- `PORT`（默认 8080）
- `SERIES_ID`（默认 11433，世界杯）
- `SUBSCRIBE_WINDOW_DAYS`（默认 3；设为 0 表示全部）
- `REFRESH_INTERVAL`（赛事重新发现间隔，默认 60s）

---

## 11. 风险与注意事项

1. **仅供观测/信号**：edge 基于 best price，未扣手续费、gas、滑点，且需要可同时成交；不构成自动下单。
2. **订阅规模**：全量订阅可能数千 token，必要时分批/多连接；默认按时间窗口收敛。
3. **数据一致性**：`book` 为全量快照，`price_change` 为增量；以快照为准、增量更新最优价。
4. **赛事生命周期**：定时重新发现，纳入新开赛、剔除已结束（closed）比赛。
5. **"Any Other Score"** 在跨市场套利中按不确定项处理，避免误报。

---

## 12. 实施步骤（待确认后执行）

1. `requirements.txt` + 项目骨架。
2. `gamma.py`：实测接口已通，实现发现 + 配对 + token 解析。
3. `ws_client.py`：裁剪 ReferenceProject 的 WS 客户端，加 `on_update`。
4. `state.py`：状态聚合 + Edge 计算（§6）。
5. `server.py` + 前端：先全量展示，再接 WS 增量。
6. 联调：本地启动，验证 1X2 与波胆实时刷新与 edge 高亮。
</content>
</invoke>
