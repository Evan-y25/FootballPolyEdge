"use strict";

const EDGE_LABELS = {
  "1x2_back_arb": "1X2 正套",
  "1x2_lay_arb": "1X2 反套",
  "score_back_arb": "波胆 正套",
  "score_lay_arb": "波胆 反套",
};

const state = {
  games: new Map(),   // slug -> game data
  prevAsks: new Map(),// "slug|key" -> ask (for flash)
  matrixMetric: "diff", // "diff" | "model" | "market"
};

const $ = (id) => document.getElementById(id);

// ---------- formatting ----------
const cents = (p) => (p == null ? "–" : (p * 100).toFixed(0) + "¢");
function kickoffStr(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d)) return iso;
  return d.toLocaleString("zh-CN", {
    month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit",
  });
}
function scoreSortKey(label) {
  const m = String(label).match(/(\d+)\s*-\s*(\d+)/);
  if (!m) return [99, 99];                  // "Other" last
  return [parseInt(m[1]), parseInt(m[2])];
}

// ---------- rendering ----------
function legHtml(slug, key, name, q) {
  if (!q) return `<div class="leg"><div class="name">${name}</div><div class="ask">–</div></div>`;
  const flashKey = `${slug}|${key}`;
  const prev = state.prevAsks.get(flashKey);
  const flash = prev != null && prev !== q.ask ? " flash" : "";
  state.prevAsks.set(flashKey, q.ask);
  return `
    <div class="leg">
      <div class="name">${name}</div>
      <div class="ask${flash}">${cents(q.ask)}</div>
      <div class="bid">bid ${cents(q.bid)}</div>
      <div class="sz">×${q.ask_size || 0}</div>
    </div>`;
}

function overroundHtml(label, val) {
  if (val == null) return "";
  const cls = val < 1 ? "good" : "bad";
  const tag = val < 1 ? "（低于1，存在正套空间）" : "（含抽水）";
  return `<div class="overround">${label} ask 合计 <b class="${cls}">${val.toFixed(3)}</b> ${tag}</div>`;
}

function scoresHtml(slug, scores) {
  if (!scores || !scores.length) return "";
  const sorted = [...scores].sort((a, b) => {
    const ka = scoreSortKey(a.label), kb = scoreSortKey(b.label);
    return ka[0] - kb[0] || ka[1] - kb[1];
  });
  const maxAsk = Math.max(...sorted.map((s) => s.ask || 0));
  const tiles = sorted.map((s) => {
    const flashKey = `${slug}|sc|${s.label}`;
    const prev = state.prevAsks.get(flashKey);
    const flash = prev != null && prev !== s.ask ? " flash" : "";
    state.prevAsks.set(flashKey, s.ask);
    const top = s.ask && s.ask === maxAsk ? " top" : "";
    return `<div class="score${top}">
        <div class="lbl">${s.label}</div>
        <div class="px${flash}">${cents(s.ask)}</div>
      </div>`;
  }).join("");
  return `<div class="section-title">波胆 / Exact Score</div><div class="scores">${tiles}</div>`;
}

// ===== paper trading (模拟盘) =====
const SIGNAL_LABEL = {
  converged: ["sig-go", "建议平仓"],
  profit: ["sig-go", "可止盈"],
  stoploss: ["sig-stop", "止损"],
  settle: ["sig-settle", "持有至结算"],
  hold: ["sig-hold", "持有"],
};
function pnlCls(v) { return v > 0 ? "pos" : v < 0 ? "neg" : ""; }

async function fetchPaper() {
  try {
    const r = await fetch("/api/paper");
    renderPaper(await r.json());
  } catch (e) { /* ignore transient */ }
}

let autoEnabled = false;
async function fetchAuto() {
  try {
    const r = await fetch("/api/auto");
    renderAuto(await r.json());
  } catch (e) { /* ignore */ }
}
// param key -> [label, kind] (pct = fraction shown ×100; int = integer)
const PARAM_META = {
  bankroll: ["本金 $", "num"],
  edge_threshold: ["价值阈值 %", "pct"],
  min_price: ["最低执行价 ¢", "pct"],
  take_profit: ["止盈 %", "pct"],
  stop_loss: ["止损 %", "pct"],
  max_positions: ["最多持仓", "int"],
  max_per_game: ["单场上限", "int"],
  max_exposure: ["最大敞口 %", "pct"],
  force_close_min: ["赛前强平(分)", "int"],
  hold_to_settle_price: ["持有到结算门槛 ¢", "pct"],
  max_spread: ["最大点差 %", "pct"],
  add_drop: ["补仓触发跌幅 %", "pct"],
  reentry_cooldown: ["重入冷却(秒)", "int"],
};
let paramsRendered = false;
function renderParamsEditor(p) {
  // Only (re)build inputs once so the user can type without being overwritten by polling.
  if (paramsRendered) return;
  const el = $("params-editor");
  const fields = Object.entries(PARAM_META).map(([k, [label, kind]]) => {
    const v = p[k];
    const disp = kind === "pct" ? Math.round(v * 1000) / 10 : v;
    const step = kind === "pct" ? "0.5" : kind === "num" ? "10" : "1";
    return `<label class="pf">${label}<input type="number" data-key="${k}" data-kind="${kind}" value="${disp}" step="${step}" /></label>`;
  }).join("");
  el.innerHTML = `<div class="pf-grid">${fields}</div>
    <div class="pf-actions"><button id="params-save" class="pbtn save">保存参数</button>
    <span class="muted">% 项按百分比填（如 3 = 3%）；¢ 项按美分填（如 50 = 0.50）。保存即时生效。</span></div>`;
  paramsRendered = true;
}
async function saveParams() {
  const inputs = $("params-editor").querySelectorAll("input[data-key]");
  const body = {};
  inputs.forEach((inp) => {
    const k = inp.dataset.key, kind = inp.dataset.kind;
    let v = parseFloat(inp.value);
    if (isNaN(v)) return;
    body[k] = kind === "pct" ? v / 100 : v;
  });
  const r = await fetch("/api/auto/params", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  await r.json();
  paramsRendered = false; // allow rebuild with clamped values
  fetchAuto();
  const btn = $("params-save");
  if (btn) { btn.textContent = "已保存 ✓"; setTimeout(() => { if (btn) btn.textContent = "保存参数"; }, 1500); }
}

function renderAuto(a) {
  autoEnabled = a.enabled;
  renderParamsEditor(a.params);
  const btn = $("auto-toggle");
  btn.textContent = "自动交易：" + (a.enabled ? "开" : "关");
  btn.className = "pbtn " + (a.enabled ? "auto-on" : "auto-off");
  const p = a.params;
  const bar = $("auto-bar");
  if (!a.enabled && !(a.log && a.log.length)) { bar.innerHTML = ""; return; }
  const holdC = p.hold_to_settle_price != null ? (p.hold_to_settle_price*100).toFixed(0) : "80";
  const rules = `规则：价值≥${(p.edge_threshold*100).toFixed(0)}% · 执行价≥${(p.min_price*100).toFixed(0)}¢ · 点差≤${p.max_spread!=null?(p.max_spread*100).toFixed(0):8}% · 止盈+${(p.take_profit*100).toFixed(0)}% · 止损−${(p.stop_loss*100).toFixed(0)}% · 最多${p.max_positions}笔/单场≤${p.max_per_game} · 敞口≤${(p.max_exposure*100).toFixed(0)}% · 退出：买NO≥${holdC}¢持有到结算，其余收敛/止盈/开赛前${p.force_close_min}分强平`;
  const recent = (a.log || []).slice(0, 4).map((l) => {
    const t = new Date(l.ts * 1000).toLocaleTimeString("zh-CN");
    const pnl = l.pnl != null ? ` <b class="${pnlCls(l.pnl)}">${l.pnl>=0?'+':''}${l.pnl}</b>` : "";
    const tag = l.kind === "open" ? "🟢开" : l.kind === "close" ? "🔴平" : l.kind === "add" ? "➕补" : "•";
    return `<div class="alog">${t} ${tag} ${l.desc}${pnl}</div>`;
  }).join("");
  bar.innerHTML = `<div class="arules muted">${rules}</div>${recent}`;
}
async function toggleAuto() {
  const r = await fetch("/api/auto", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ enabled: !autoEnabled }),
  });
  renderAuto(await r.json());
  fetchPaper();
}

function renderPaper(d) {
  const sum = $("paper-summary");
  const pc = pnlCls(d.total_pnl);
  sum.innerHTML =
    `本金 $${d.start_cash} · 现金 $${d.cash} · 净值 <b>$${d.equity}</b> · ` +
    `已实现 <b class="${pnlCls(d.realized_pnl)}">${d.realized_pnl>=0?'+':''}${d.realized_pnl}</b> · ` +
    `浮动 <b class="${pnlCls(d.unrealized_pnl)}">${d.unrealized_pnl>=0?'+':''}${d.unrealized_pnl}</b> · ` +
    `总盈亏 <b class="${pc}">${d.total_pnl>=0?'+':''}${d.total_pnl}</b> · 持仓 ${d.open_count}`;

  const openEl = $("paper-open");
  if (!d.open.length) {
    openEl.innerHTML = `<div class="muted ppad">暂无持仓 — 在下方比赛的价值表点「买$…」建立模拟仓位。</div>`;
  } else {
    const rows = d.open.map((p) => {
      const [scls, stxt] = SIGNAL_LABEL[p.signal] || SIGNAL_LABEL.hold;
      return `<tr>
        <td>#${p.id}</td>
        <td>${p.home} vs ${p.away}</td>
        <td>${p.market}:${p.label} <span class="side-${p.side}">${p.side.toUpperCase()}</span>${p.added ? ' <span class="added-tag" title="已逢低补仓一次，入场价为加权均价">已补</span>' : ''}</td>
        <td>${(p.entry_price*100).toFixed(1)}¢</td>
        <td>${(p.close_bid*100).toFixed(1)}¢</td>
        <td>${p.fair!=null?(p.fair*100).toFixed(1)+'¢':'--'}</td>
        <td>$${p.stake}</td>
        <td class="${pnlCls(p.unrealized_pnl)}">${p.unrealized_pnl>=0?'+':''}${p.unrealized_pnl} (${p.unrealized_pct>=0?'+':''}${p.unrealized_pct}%)</td>
        <td><span class="${scls}">${stxt}</span></td>
        <td><button class="pclose" data-id="${p.id}">平仓</button></td>
      </tr>`;
    }).join("");
    openEl.innerHTML = `<table class="ptable"><thead><tr>
      <th>#</th><th>比赛</th><th>标的</th><th>入场</th><th>现价</th><th>公平</th><th>注</th><th>浮动盈亏</th><th>信号</th><th></th>
      </tr></thead><tbody>${rows}</tbody></table>`;
  }

  const closedEl = $("paper-closed");
  if (!d.closed.length) { closedEl.innerHTML = ""; return; }
  const crows = d.closed.slice(0, 12).map((p) => `<tr>
      <td>#${p.id}</td><td>${p.home} vs ${p.away}</td>
      <td>${p.market}:${p.label} ${p.side.toUpperCase()}</td>
      <td>${(p.entry_price*100).toFixed(1)}¢→${(p.close_price*100).toFixed(1)}¢</td>
      <td>$${p.stake}</td>
      <td class="${pnlCls(p.realized_pnl)}">${p.realized_pnl>=0?'+':''}${p.realized_pnl}</td>
    </tr>`).join("");
  closedEl.innerHTML = `<div class="muted ppad">已平仓 (${d.closed.length})</div>
    <table class="ptable closed"><tbody>${crows}</tbody></table>`;
}

async function paperOpen(slug, market, label, side, stake) {
  const r = await fetch("/api/paper/open", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ slug, market, label, side, stake: parseFloat(stake) }),
  });
  const res = await r.json();
  if (!res.ok) alert("模拟买入失败：" + (res.error || "")); else fetchPaper();
}
async function paperClose(id) {
  const r = await fetch("/api/paper/close", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ id: parseInt(id) }),
  });
  const res = await r.json();
  if (!res.ok) alert("平仓失败：" + (res.error || "")); else fetchPaper();
}

// ----- score-matrix value model (SCORE_MATRIX.md) -----
const GRID_N = 4; // home/away goals 0..3 shown in grid; rest -> "Other"

// Executable = has real depth and not a proxy-priced NO leg.
function isExecutable(e) {
  return e.size > 0 && e.no_book !== false;
}
// Suggested stake: quarter-Kelly, capped at 20% of bankroll and by book liquidity.
// For a "pay price -> win 1" bet, full-Kelly fraction = edge / (1 - price).
function kellyStake(e, bankroll) {
  if (!(e.price > 0) || e.price >= 1) return 0;
  const fullKelly = e.edge / (1 - e.price);
  let stake = bankroll * 0.25 * fullKelly;
  stake = Math.min(stake, bankroll * 0.2);     // single-bet cap
  const liqUsd = e.size * e.price;             // max executable at this level
  if (liqUsd > 0) stake = Math.min(stake, liqUsd);
  return Math.max(0, stake);
}

function diffColor(diff) {
  // red = 市场低估应买 (diff>0); blue = 市场高估应卖 (diff<0)
  const norm = Math.max(-1, Math.min(1, diff / 0.08));
  const a = Math.abs(norm) * 0.78;
  return norm >= 0 ? `rgba(229,83,75,${a})` : `rgba(47,129,247,${a})`;
}
function seqColor(v) {
  const a = Math.max(0, Math.min(1, v / 0.2)) * 0.8;
  return `rgba(46,160,67,${a})`;
}
function cellValue(m, metric) {
  const v = m ? m[metric] : null;
  if (v == null) return ["", "transparent"];
  const txt = (v * 100).toFixed(1);
  const color = metric === "diff" ? diffColor(v) : seqColor(v);
  return [txt, color];
}

function scoreModelHtml(g) {
  const sm = g.score_model;
  if (!sm) return "";
  const metric = state.matrixMetric;
  const mx = sm.matrix || {};
  // header rows/cols
  let head = `<div class="mx-corner"></div>`;
  for (let j = 0; j < GRID_N; j++) head += `<div class="mx-h">客${j}</div>`;
  let cells = head;
  for (let i = 0; i < GRID_N; i++) {
    cells += `<div class="mx-h">主${i}</div>`;
    for (let j = 0; j < GRID_N; j++) {
      const m = mx[`${i} - ${j}`];
      const [txt, color] = cellValue(m, metric);
      const tip = m ? `${i}-${j}  模型 ${(m.model*100).toFixed(1)}% / 市场 ${(m.market*100).toFixed(1)}% / Δ ${(m.diff*100).toFixed(1)}%` : `${i}-${j} 无数据`;
      cells += `<div class="mx-c" style="background:${color}" title="${tip}">${txt}</div>`;
    }
  }
  const other = mx["Other"];
  const [otxt, ocolor] = cellValue(other, metric);
  const otherTip = other ? `Other 模型 ${(other.model*100).toFixed(1)}% / 市场 ${(other.market*100).toFixed(1)}% / Δ ${(other.diff*100).toFixed(1)}%` : "";

  const checks = sm.checks || {};
  const recOk = checks.onex2_reconstruct_ok ? "✓" : "✗";

  const bankroll = parseFloat($("bankroll").value) || 100;
  const execOnly = $("exec-only").checked;
  const minPrice = (parseFloat($("min-price").value) || 0) / 100; // ¢ -> prob
  const all = sm.value_edges || [];
  let evs = all.filter((e) => (!execOnly || isExecutable(e)) && e.price >= minPrice);
  const hidden = all.length - evs.length;

  const edges = evs.slice(0, 8).map((e) => {
    let sideTxt;
    if (e.side === "buy_yes") {
      sideTxt = '<span class="buy" title="买入该比分的 YES：押该比分会发生">买YES</span>';
    } else {
      // buy_no == sell yes (equivalent payoff)
      const proxy = e.no_book === false ? '<sup title="NO盘口缺失，价格用 1−bid(YES) 估算">*</sup>' : "";
      sideTxt = `<span class="sell" title="买入该比分的 NO（=卖出YES，收益等价）：押该比分不会发生">买NO${proxy}</span>`;
    }
    const stake = isExecutable(e) ? kellyStake(e, bankroll) : 0;
    const stakeTxt = stake >= 1 ? `$${stake.toFixed(stake < 10 ? 2 : 0)}` : (stake > 0 ? "<$1" : "—");
    const tokenSide = e.side === "buy_yes" ? "yes" : "no";
    const buyStake = stake > 0 ? stake : 0;
    const btn = isExecutable(e)
      ? `<button class="pbuy" data-slug="${g.slug}" data-market="score" data-label="${e.label}" data-side="${tokenSide}" data-stake="${buyStake.toFixed(2)}">买$${(buyStake>=1?buyStake.toFixed(0):buyStake.toFixed(2))}</button>`
      : "";
    const spTxt = e.spread_pct != null
      ? `<sup class="${e.spread_pct > 0.08 ? "wide" : "ok"}" title="点差(ask-bid)/ask；过宽=买入即被点差吃掉、edge不可兑现">±${(e.spread_pct*100).toFixed(0)}%</sup>`
      : "";
    return `<tr>
      <td>${e.label}</td><td>${sideTxt}</td>
      <td>${(e.fair*100).toFixed(1)}¢</td>
      <td>${(e.price*100).toFixed(1)}¢${spTxt}</td>
      <td class="ev">+${(e.edge*100).toFixed(2)}%</td>
      <td>${e.size}</td>
      <td class="stake">${stakeTxt}</td>
      <td>${btn}</td>
    </tr>`;
  }).join("");
  const hiddenNote = hidden ? `<span class="muted">（已隐藏 ${hidden} 条：不可成交 / 执行价过低）</span>` : "";

  return `
    <div class="section-title">
      模型 vs 市场 · 波胆价值 <span class="value-tag">价值(模型)</span>
    </div>
    <div class="mx-meta">
      ${sm.model} · λ主=${sm.lambda_home} λ客=${sm.lambda_away}
      · 市场抽水 ${checks.market_overround} · 1X2回算 ${recOk}
      <span class="mx-toggle">
        <button data-metric="diff" class="${metric==='diff'?'on':''}">Δ差值</button>
        <button data-metric="model" class="${metric==='model'?'on':''}">模型</button>
        <button data-metric="market" class="${metric==='market'?'on':''}">市场</button>
      </span>
    </div>
    <div class="matrix">${cells}</div>
    <div class="mx-other" title="${otherTip}">
      其它比分(Other)：<b style="background:${ocolor}">${otxt}%</b>
      <span class="muted">（模型尾部 vs 市场长尾，差异是重要信号）</span>
    </div>
    ${edges ? `<table class="vtable">
      <thead><tr><th>比分</th>
      <th title="买YES=押发生；买NO=押不发生(=卖YES，收益等价)。公平价与执行价均按该方向标的。">方向ⓘ</th>
      <th title="模型给出的该方向公平价（买YES=模型概率；买NO=1−模型概率）">公平</th>
      <th title="真实盘口执行价：买YES用ask(YES)，买NO用ask(NO)">执行价</th>
      <th>价值</th><th title="该价位可成交份额(shares)">量</th>
      <th title="¼ Kelly 建议注额：单注≤本金20%，且不超过该价位可成交金额(量×价)">建议注</th>
      <th title="按建议注额在模拟盘买入(以当前ask成交)">模拟</th></tr></thead>
      <tbody>${edges}</tbody></table>
      <div class="muted mx-noedge">${hiddenNote} <sup>*</sup>=NO盘口缺失,价格用1−bid(YES)估算</div>`
      : `<div class="muted mx-noedge">无符合条件的价值机会${hidden ? `（${hidden} 条已按"可成交/最低执行价"过滤）` : ""}</div>`}
  `;
}

function edgesHtml(edges) {
  if (!edges || !edges.length) return "";
  const rows = edges.map((e) => `
    <div class="edge-row">
      <span class="etype">${EDGE_LABELS[e.type] || e.type}</span>
      · 收益 <span class="eval">+${(e.edge * 100).toFixed(2)}%</span>
      · 可成交 ${e.size}
      <div class="muted">${e.detail}</div>
    </div>`).join("");
  return `<div class="edges">${rows}</div>`;
}

function cardHtml(g) {
  const live = g.status === "live";
  const badge = live ? `<span class="badge-live">进行中</span>` : `<span class="badge-up">未开始</span>`;
  const hasArb = g.edges && g.edges.length;
  const hasValue = g.score_model && g.score_model.value_edges && g.score_model.value_edges.length;
  const ox = g.onex2 || {};
  const arbBadge = hasArb ? `<span class="badge-arb">套利×${g.edges.length}</span>` : "";
  const valBadge = hasValue ? `<span class="badge-val">价值×${g.score_model.value_edges.length}</span>` : "";
  return `
    <div class="card${hasArb ? " has-edge" : ""}${hasValue ? " has-value" : ""}" data-slug="${g.slug}">
      <div class="card-head">
        <div>
          <span class="match">${g.home} vs ${g.away}</span>${badge}${arbBadge}${valBadge}
        </div>
        <div class="kickoff">${kickoffStr(g.kickoff)}</div>
      </div>
      <div class="section-title">1X2 / 胜平负</div>
      <div class="onex2">
        ${legHtml(g.slug, "home", g.home, ox.home)}
        ${legHtml(g.slug, "draw", "平局", ox.draw)}
        ${legHtml(g.slug, "away", g.away, ox.away)}
      </div>
      ${overroundHtml("1X2", ox.overround)}
      ${scoresHtml(g.slug, g.scores)}
      ${overroundHtml("波胆", g.scores_overround)}
      ${edgesHtml(g.edges)}
      ${scoreModelHtml(g)}
    </div>`;
}

function passesFilter(g) {
  if ($("only-edges").checked && !(g.edges && g.edges.length)) return false;
  if ($("only-value").checked && !(g.score_model && g.score_model.value_edges && g.score_model.value_edges.length)) return false;
  if ($("only-live").checked && g.status !== "live") return false;
  return true;
}

function renderAll() {
  const container = $("games");
  const games = [...state.games.values()]
    .filter(passesFilter)
    .sort((a, b) => (a.kickoff || "").localeCompare(b.kickoff || ""));
  if (!games.length) {
    container.innerHTML = `<div class="empty">暂无比赛数据（或筛选条件下无结果）。</div>`;
    return;
  }
  container.innerHTML = games.map(cardHtml).join("");
}

function updateGames(games) {
  for (const g of games) state.games.set(g.slug, g);
  // Re-render only changed cards when possible; fall back to full render.
  const container = $("games");
  if (!container.children.length || container.querySelector(".empty")) {
    renderAll();
    return;
  }
  for (const g of games) {
    const existing = container.querySelector(`.card[data-slug="${CSS.escape(g.slug)}"]`);
    if (existing && passesFilter(g)) {
      existing.outerHTML = cardHtml(g);
    } else {
      renderAll();
      return;
    }
  }
}

function setMeta(d) {
  if (d.ws_connected != null) {
    $("ws-dot").classList.toggle("on", !!d.ws_connected);
    $("ws-text").textContent = d.ws_connected ? "CLOB 已连接" : "CLOB 未连接";
  }
  if (d.subscribed_tokens != null) $("token-count").textContent = `tokens: ${d.subscribed_tokens}`;
  $("game-count").textContent = `games: ${state.games.size}`;
  let edges = 0;
  for (const g of state.games.values()) edges += (g.edges || []).length;
  $("edge-count").textContent = `edges: ${edges}`;
  if (d.updated_at) $("updated").textContent = "更新 " + new Date(d.updated_at * 1000).toLocaleTimeString("zh-CN");
}

// ---------- websocket ----------
let ws;
function connect() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.onmessage = (ev) => {
    const d = JSON.parse(ev.data);
    if (d.type === "snapshot") {
      state.games.clear();
      for (const g of d.games) state.games.set(g.slug, g);
      renderAll();
    } else if (d.type === "update") {
      updateGames(d.games);
    }
    setMeta(d);
  };
  ws.onclose = () => {
    $("ws-dot").classList.remove("on");
    $("ws-text").textContent = "断开，重连中…";
    setTimeout(connect, 2000);
  };
  ws.onerror = () => ws.close();
}

$("only-edges").addEventListener("change", renderAll);
$("only-value").addEventListener("change", renderAll);
$("only-live").addEventListener("change", renderAll);
$("exec-only").addEventListener("change", renderAll);
$("bankroll").addEventListener("input", renderAll);
$("min-price").addEventListener("input", renderAll);

// Delegated clicks within the games area: matrix toggle + 模拟买入.
$("games").addEventListener("click", (ev) => {
  const toggle = ev.target.closest(".mx-toggle button");
  if (toggle) { state.matrixMetric = toggle.dataset.metric; renderAll(); return; }
  const buy = ev.target.closest(".pbuy");
  if (buy) {
    paperOpen(buy.dataset.slug, buy.dataset.market, buy.dataset.label, buy.dataset.side, buy.dataset.stake);
  }
});

// Paper panel: close buttons + reset + collapse.
$("paper-open").addEventListener("click", (ev) => {
  const c = ev.target.closest(".pclose");
  if (c) paperClose(c.dataset.id);
});
$("paper-reset").addEventListener("click", async () => {
  if (!confirm("清空模拟盘所有持仓与记录？")) return;
  await fetch("/api/paper/reset", { method: "POST" });
  fetchPaper();
});
$("paper-toggle").addEventListener("click", () => {
  const body = $("paper-body");
  const hidden = body.style.display === "none";
  body.style.display = hidden ? "" : "none";
  $("paper-toggle").textContent = hidden ? "收起" : "展开";
});
$("auto-toggle").addEventListener("click", toggleAuto);
$("params-toggle").addEventListener("click", () => {
  const el = $("params-editor");
  el.style.display = el.style.display === "none" ? "" : "none";
});
$("params-editor").addEventListener("click", (ev) => {
  if (ev.target.id === "params-save") saveParams();
});

connect();
fetchPaper();
fetchAuto();
setInterval(() => { fetchPaper(); fetchAuto(); }, 2000);
