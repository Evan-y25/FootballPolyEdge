"use strict";
const $ = (id) => document.getElementById(id);
let armed = false;
let minEdge = 0.008;
const expandedGames = new Set();

function card(k, v, cls = "") { return `<div class="card"><div class="k">${k}</div><div class="v ${cls}">${v}</div></div>`; }

function render(d) {
  armed = d.armed;
  const gate = d.enabled ? (d.ready ? (d.armed ? ["⚔️ 已武装", "bad"] : ["就绪·未武装", "warn"]) : ["未就绪", "bad"]) : ["LIVE_ENABLED=0", "muted"];
  const allowTxt = (v) => v === true ? "已授权✓" : v === false ? "未授权✗" : "—";
  const allowCls = (v) => v === true ? "ok" : v === false ? "bad" : "";
  $("cards").innerHTML =
    card("主闸 LIVE_ENABLED", d.enabled ? "ON" : "OFF", d.enabled ? "ok" : "bad") +
    card("连接状态", gate[0], gate[1]) +
    card("签名地址(EOA)", d.address || "—") +
    card("Funder (Safe)", d.funder || "—") +
    card("pUSD 余额", d.pusd != null ? "$" + d.pusd : "—", d.pusd ? "ok" : "bad") +
    card("授权 V2交易所", allowTxt(d.allow_exchange), allowCls(d.allow_exchange)) +
    card("授权 Neg-Risk交易所", allowTxt(d.allow_negrisk), allowCls(d.allow_negrisk)) +
    card("builder code", d.builder ? "有" : "无") +
    card("已投入(累计)", "$" + (d.deployed || 0), "") +
    card("上限/腿 · 总", d.caps ? `$${d.caps.per_leg} · $${d.caps.total}` : "—") +
    card("最低edge", d.caps ? (d.caps.min_edge * 100).toFixed(1) + "%" : "—");
  if (d.onchain_error) $("msg").innerHTML = `<span class="bad">链上查询: ${d.onchain_error}</span>`;
  // seed sizing inputs (only when not focused, so typing isn't overwritten)
  if (d.caps) {
    minEdge = d.caps.min_edge;
    const set = (id, v) => { const e = $(id); if (e && document.activeElement !== e) e.value = v; };
    set("cfg-leg", d.caps.per_leg);
    set("cfg-total", d.caps.total);
    set("cfg-edge", (d.caps.min_edge * 100).toFixed(1));
  }
  if (d.error) $("msg").innerHTML = `<span class="bad">${d.error}</span>`;

  // scan heartbeat
  const sc = d.scan || {};
  const now = Date.now() / 1000;
  const age = sc.ts ? Math.round(now - sc.ts) : null;
  const alive = age != null && age <= Math.max(10, (d.interval || 3) * 3);
  const beat = !d.armed ? ["未武装·未扫描", "muted"]
    : sc.ts == null || !sc.count ? ["等待首次扫描…", "warn"]
    : alive ? [`扫描中 · ${age}s 前`, "ok"] : [`已停 · ${age}s 前`, "bad"];
  const edgeCell = (e, lbl) => {
    if (!e) return card(lbl, "—", "muted");
    const pct = (e.edge * 100).toFixed(2) + "%";
    const cls = e.edge >= (d.caps ? d.caps.min_edge : 0.008) ? "ok" : "bad";
    return card(lbl, `<span class="${cls}">${pct}</span><div style="font-size:11px;color:var(--muted);font-weight:400;margin-top:2px">${e.game || ""}</div>`);
  };
  const scanEl = $("scan");
  if (scanEl) scanEl.innerHTML =
    card("状态", beat[0], beat[1]) +
    card("扫描轮次", sc.count || 0) +
    card("赛前场次 / 有深度", `${sc.games || 0} / ${sc.candidates || 0}`) +
    edgeCell(sc.best_back, "最佳正套 edge(需≥阈值)") +
    edgeCell(sc.best_lay, "最佳反套 edge(需≥阈值)");
  const ab = $("arm");
  ab.className = d.armed ? "on" : "off";
  ab.textContent = d.armed ? "⚔️ 已武装（点击解除）" : "未武装（点击武装真实下单）";
  ab.disabled = !d.ready && !d.armed;

  // baskets
  const bs = d.baskets || [];
  $("baskets").innerHTML = bs.length ? `<table><thead><tr>
    <th>时间</th><th>比赛</th><th>方向</th><th>股数</th><th>成交腿</th><th>成本</th><th>明细</th></tr></thead><tbody>` +
    bs.map((b) => {
      const t = new Date(b.ts * 1000).toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
      const legs = (b.legs || []).map((l) => `${l.leg}:${l.status || l.error || "?"}`).join(" ");
      let fill;
      if (b.complete) fill = `<span class="ok">3/3 ✅</span>`;
      else if (b.filled_legs === 0) fill = `<span class="muted">0/3 探路未成(无敞口)</span>`;
      else {
        const uw = (b.unwound || []).filter((u) => u.unwound).length;
        fill = `<span class="bad">${b.filled_legs}/3 → 平腿${uw}/${b.filled_legs}${uw === b.filled_legs ? '✓' : '⚠️'}</span>`;
      }
      return `<tr><td>${t}</td><td>${b.home} vs ${b.away}</td><td>${b.kind === "back" ? "正套YES" : "反套NO"}</td>
        <td>${b.shares}</td><td>${fill}</td><td>$${b.cost}</td><td class="muted">${legs}</td></tr>`;
    }).join("") + "</tbody></table>"
    : `<div class="muted" style="padding:0 20px">暂无实盘成交。武装后，发现 1X2 套利会自动下三腿 FOK 限价单。</div>`;

  $("log").innerHTML = (d.log || []).map((l) => {
    const t = new Date(l.ts * 1000).toLocaleTimeString("zh-CN");
    return `<div class="${l.level === "warn" ? "warn" : ""}">${t} ${l.desc}</div>`;
  }).join("");
}

async function fetchLive() { try { render(await (await fetch("/api/live")).json()); } catch (e) {} }

// ---- scanned-games list -------------------------------------------------
const f = (x, d = 3) => (x == null ? "—" : Number(x).toFixed(d));
const sz = (x) => (x ? Math.round(x).toLocaleString() : "0");
const LEGS = [["home", "主胜"], ["draw", "平"], ["away", "客胜"]];

function edgeSpan(label, edge, depthOk) {
  if (edge == null) return `${label} <span class="muted">—</span>`;
  const cls = edge >= minEdge && depthOk ? "ok" : "bad";
  return `${label} <span class="${cls}">${(edge * 100).toFixed(2)}%</span>`;
}

function gameDetail(o) {
  const rows = LEGS.map(([k, zh]) => {
    const l = o[k] || {};
    const xY = l.bid && l.ask && l.bid >= l.ask ? ' class="bad"' : "";  // crossed YES
    const xN = l.no_bid && l.no_ask && l.no_bid >= l.no_ask ? ' class="bad"' : "";
    return `<tr><td>${zh}</td>
      <td>${f(l.bid)}</td><td${xY}>${f(l.ask)} <span class="muted">(${sz(l.ask_size)})</span>${xY ? " ⚠️" : ""}</td>
      <td>${f(l.no_bid)}</td><td${xN}>${f(l.no_ask)} <span class="muted">(${sz(l.no_ask_size)})</span>${xN ? " ⚠️" : ""}</td></tr>`;
  }).join("");
  return `<table class="gtable"><thead><tr>
    <th>腿</th><th>YES买</th><th>YES卖(量)</th><th>NO买</th><th>NO卖(量)</th></tr></thead>
    <tbody>${rows}</tbody></table>`;
}

function legSums(o) {
  const ya = LEGS.map(([k]) => (o[k] || {}).ask);
  const na = LEGS.map(([k]) => (o[k] || {}).no_ask);
  const ys = LEGS.map(([k]) => (o[k] || {}).ask_size);
  const ns = LEGS.map(([k]) => (o[k] || {}).no_ask_size);
  const yb = LEGS.map(([k]) => (o[k] || {}).bid);
  const nb = LEGS.map(([k]) => (o[k] || {}).no_bid);
  const ok = (arr) => arr.every((x) => x && x > 0);
  // crossed book (bid >= ask) on any leg => that ask is stale/phantom
  const crossed = (asks, bids) => asks.some((a, i) => a && bids[i] && bids[i] >= a);
  const yesAskSum = ok(ya) ? ya.reduce((a, b) => a + b, 0) : null;
  const noAskSum = ok(na) ? na.reduce((a, b) => a + b, 0) : null;
  const crossedYes = crossed(ya, yb), crossedNo = crossed(na, nb);
  return {
    yesAskSum, noAskSum, crossedYes, crossedNo,
    backEdge: yesAskSum == null ? null : 1 - yesAskSum,
    layEdge: noAskSum == null ? null : 2 - noAskSum,
    // "real" only if every leg has depth AND no leg is crossed
    depthYes: ok(ys) && !crossedYes, depthNo: ok(ns) && !crossedNo,
  };
}

function renderGames(snap) {
  const el = $("games"); if (!el) return;
  const games = (snap.games || []).filter((g) => g.onex2 && g.onex2.home && g.onex2.draw && g.onex2.away);
  // pre-match candidates with the best back edge float to the top
  const score = (g) => {
    const s = legSums(g.onex2);
    const pre = g.status !== "live";
    const best = Math.max(s.backEdge ?? -9, s.layEdge ?? -9);
    return (pre ? 0 : 1) * 1e6 - best; // pre-match first, then by closeness to arb
  };
  games.sort((a, b) => score(a) - score(b));
  const cnt = $("games-count"); if (cnt) cnt.textContent = `(${games.length} 场)`;
  el.innerHTML = games.map((g) => {
    const o = g.onex2, s = legSums(o), open = expandedGames.has(g.slug);
    const live = g.status === "live";
    const isCand = !live && (s.depthYes || s.depthNo);
    const pill = (crossed, depthOk, sum) =>
      crossed ? '<span class="pill">⚠️交叉/失效</span>'
      : (sum != null && !depthOk ? '<span class="pill">THIN</span>' : "");
    const sum = `<div class="gsum">
      <span>YES卖价和 <b>${s.yesAskSum == null ? "—" : f(s.yesAskSum)}</b> → ${edgeSpan("正套", s.backEdge, s.depthYes)} ${pill(s.crossedYes, s.depthYes, s.yesAskSum)}</span>
      <span>NO卖价和 <b>${s.noAskSum == null ? "—" : f(s.noAskSum)}</b> → ${edgeSpan("反套", s.layEdge, s.depthNo)} ${pill(s.crossedNo, s.depthNo, s.noAskSum)}</span>
      <span class="muted">overround ${f(o.overround, 3)}</span>
    </div>`;
    return `<div class="gitem ${open ? "open" : ""}" data-slug="${g.slug}">
      <div class="ghead">
        <span class="garrow">▸</span>
        <span class="gname">${g.home} vs ${g.away}</span>
        <span class="gedge">正套 <span class="${(s.backEdge ?? -9) >= minEdge && s.depthYes ? "ok" : "muted"}">${s.backEdge == null ? "—" : (s.backEdge * 100).toFixed(1) + "%"}</span></span>
        <span class="gedge">反套 <span class="${(s.layEdge ?? -9) >= minEdge && s.depthNo ? "ok" : "muted"}">${s.layEdge == null ? "—" : (s.layEdge * 100).toFixed(1) + "%"}</span></span>
        <span class="gstat ${live ? "live" : ""}">${live ? "进行中" : (isCand ? "赛前·扫描中" : "赛前·无深度")}</span>
      </div>
      <div class="gbody">${gameDetail(o)}${sum}</div>
    </div>`;
  }).join("") || `<div class="muted" style="padding:4px 0">暂无已配对的 1X2 比赛。</div>`;
}

async function fetchGames() {
  try { renderGames(await (await fetch("/api/games")).json()); } catch (e) {}
}

// expand/collapse via delegation (survives the 3s refresh)
const gamesEl = $("games");
if (gamesEl) gamesEl.addEventListener("click", (ev) => {
  const item = ev.target.closest(".gitem"); if (!item) return;
  const slug = item.dataset.slug;
  if (expandedGames.has(slug)) expandedGames.delete(slug); else expandedGames.add(slug);
  item.classList.toggle("open");
});

$("test").addEventListener("click", async () => {
  $("msg").textContent = "连接中…";
  const d = await (await fetch("/api/live/test", { method: "POST" })).json();
  $("msg").innerHTML = d.ok ? `<span class="ok">连接成功</span>` : `<span class="bad">${(d.status && d.status.error) || d.error || "失败"}</span>`;
  if (d.status) render(d.status);
});

$("testbuy").addEventListener("click", async () => {
  if (!confirm("测试买入：用真实资金小额买入某场 1X2 三条 YES 腿（验证下单链路，非套利，net 成本≈抽水）。继续？")) return;
  const btn = $("testbuy"); btn.disabled = true; btn.textContent = "买入中…";
  $("msg").textContent = "";
  try {
    const d = await (await fetch("/api/live/testbuy", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" })).json();
    if (d.ok) {
      const legs = (d.legs || []).map((l) => `${l.leg}:${l.status || l.error || "?"}`).join(" ");
      $("msg").innerHTML = `<span class="${d.filled === 3 ? "ok" : "bad"}">${d.game} · ${d.filled}/3 成交 · ≈$${d.cost} · ${legs}</span>`;
    } else {
      $("msg").innerHTML = `<span class="bad">测试失败: ${d.error || ""}</span>`;
    }
    fetchLive();
  } finally {
    btn.disabled = false; btn.textContent = "🧪 测试买入(3腿)";
  }
});

$("arm").addEventListener("click", async () => {
  if (!armed) {
    if (!confirm("确认武装实盘？武装后系统会用真实资金自动下单（受 $/腿 与总额上限约束）。")) return;
  }
  const d = await (await fetch("/api/live/arm", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ armed: !armed }) })).json();
  if (!d.ok) $("msg").innerHTML = `<span class="bad">${d.error}</span>`;
  fetchLive();
});

$("save-cfg").addEventListener("click", async () => {
  const body = {
    max_per_leg: parseFloat($("cfg-leg").value),
    max_total: parseFloat($("cfg-total").value),
    min_edge: parseFloat($("cfg-edge").value) / 100,
  };
  const d = await (await fetch("/api/live/config", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) })).json();
  $("msg").innerHTML = d.ok ? `<span class="ok">已保存: 单腿$${d.caps.per_leg} 总$${d.caps.total} edge${(d.caps.min_edge*100).toFixed(1)}%</span>` : `<span class="bad">${d.error||"失败"}</span>`;
  fetchLive();
});

$("copy-log").addEventListener("click", async () => {
  const text = $("log").innerText || "";
  try {
    await navigator.clipboard.writeText(text);
    $("copy-msg").textContent = "已复制 ✓";
  } catch (e) {
    // fallback: select the log text for manual copy
    const r = document.createRange(); r.selectNodeContents($("log"));
    const s = window.getSelection(); s.removeAllRanges(); s.addRange(r);
    $("copy-msg").textContent = "已选中，按 Cmd/Ctrl+C 复制";
  }
  setTimeout(() => { $("copy-msg").textContent = ""; }, 3000);
});

function hasSelection() {
  const s = window.getSelection && window.getSelection();
  return !!(s && String(s).length > 0);
}
fetchLive();
fetchGames();
// don't re-render while the user is selecting/copying text (it would clear the selection)
setInterval(() => { if (!hasSelection()) { fetchLive(); fetchGames(); } }, 3000);
