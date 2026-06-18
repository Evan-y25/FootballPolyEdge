# FootballPolyEdge

实时抓取 Polymarket **世界杯（FIFA World Cup）** 所有比赛的订单簿数据（**1X2 胜平负** + **波胆 / Exact Score**），
通过 WebSocket 实时更新展示到网页，并计算高亮 **edge / 套利机会**。

完整设计见 [`DESIGN.md`](./DESIGN.md)。

## 工作原理

1. **赛事发现**（`app/gamma.py`）：从 Gamma API `events?series_id=11433`（世界杯）拉取所有比赛，
   把 `fifwc-{主}-{客}-{日期}`（1X2）与同 slug 加 `-exact-score`（波胆）的 event 配对成一场比赛，
   解析每个市场的 `clobTokenIds`。
2. **实时盘口**（`app/ws_client.py`，裁剪自 `ReferenceProject`）：连接 CLOB
   `wss://ws-subscriptions-clob.polymarket.com/ws/market`，订阅所有 token，维护实时订单簿。
3. **聚合 + Edge**（`app/state.py`）：计算 1X2 / 波胆 的正套·反套与抽水指标。
4. **网页**（`app/server.py` + `frontend/`）：aiohttp 提供页面与 `/ws`，浏览器实时刷新。

## 运行

```bash
pip install -r requirements.txt
python run.py
# 打开 http://localhost:8080
```

## 配置（环境变量）

| 变量 | 默认 | 说明 |
|------|------|------|
| `PORT` | `8080` | HTTP 端口 |
| `SERIES_ID` | `11433` | Gamma 赛事系列（世界杯） |
| `SUBSCRIBE_WINDOW_DAYS` | `3` | 只订阅近 N 天内开赛 + 进行中的比赛；`0` = 全部 |
| `REFRESH_INTERVAL` | `60` | 重新发现赛事的间隔（秒） |
| `WS_SUBSCRIBE_CHUNK` | `400` | 每条订阅消息最多 token 数 |
| `PUSH_THROTTLE_MS` | `250` | 推送前端的节流间隔 |

## Edge 说明

仅标记**风险可控、定义明确**的套利：

- **1X2 正套**：`ask(主)+ask(平)+ask(客) < 1` → 全买 YES，到期必得 1。
- **1X2 反套**：`bid(主)+bid(平)+bid(客) > 1` → 全卖 YES。
- **波胆 正套 / 反套**：17 个比分互斥且穷尽（含 "Other"），同理。

> ⚠️ 所有收益按最优盘口价估算，**未扣手续费、gas、滑点**，且需要可同时成交，仅供观测参考。

## 波胆价值模型（SCORE_MATRIX.md）

除无风险套利外，另实现「从 1X2 反解比分概率矩阵」的**模型价值**对比（`app/score_model.py`）：

1. 1X2 mid 价 → 去抽水（de-vig）得公平概率
2. 拟合进球模型 `(λH, λA)`（默认 Dixon-Coles 低分修正，纯 Python 2D Newton 求解）
3. 展开成完整比分矩阵 → 聚合到 17 个波胆桶
4. 与市场波胆盘口逐格对比，输出 **模型 / 市场 / 差值** 三套概率 + value edge 排行

> ⚠️ 与无风险套利不同：value edge **依赖泊松/DC 模型假设**，是「模型价值」而非「套利」。
> 前端用蓝色「价值(模型)」徽章与金色「套利」徽章明确区分；矩阵 Δ 配色：红=市场低估(买) / 蓝=市场高估(卖)。
> 模型在**赛前**最有意义；进行中(live)比赛因不含已发生比分/时间信息，偏差较大（见 §9）。

相关配置：

| 变量 | 默认 | 说明 |
|------|------|------|
| `SCORE_MODEL` | `dixon_coles` | `dixon_coles` / `poisson` |
| `SCORE_RHO` | `-0.13` | Dixon-Coles 低分相关参数（全局校准值） |
| `DEVIG_METHOD` | `proportional` | `proportional` / `power` |
| `VALUE_EDGE_THRESHOLD` | `0.02` | 标记 value edge 的最小 \|edge\| |

单元测试：`python3 tests/test_score_model.py`（λ 还原、矩阵归一、1X2 回算、桶聚合等 9 项）。

## 接口

- `GET /api/games` — 全量快照（JSON，每场含 `score_model` 字段）
- `WS /ws` — 连接后先发快照，之后推送增量更新
