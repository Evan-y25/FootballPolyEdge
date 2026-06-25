"use strict";
const $ = (id) => document.getElementById(id);
let chart = null, current = null, selected = new Set();

// shade x-ranges where the signal series (idx 1) is > 0
function zeroPlugin() {
  return { hooks: { draw: (u) => {
    const ctx = u.ctx, y0 = u.valToPos(0, "y", true);
    ctx.save();
    ctx.strokeStyle = "#8b949e"; ctx.setLineDash([4, 3]); ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(u.bbox.left, y0); ctx.lineTo(u.bbox.left + u.bbox.width, y0); ctx.stroke();
    // green shading where signal>0
    const xs = u.data[0], sig = u.data[1];
    ctx.setLineDash([]); ctx.fillStyle = "rgba(46,160,67,0.18)";
    let runStart = null;
    for (let i = 0; i < xs.length; i++) {
      const on = sig[i] != null && sig[i] > 0;
      if (on && runStart == null) runStart = xs[i];
      else if (!on && runStart != null) {
        const x1 = u.valToPos(runStart, "x", true), x2 = u.valToPos(xs[i], "x", true);
        ctx.fillRect(x1, u.bbox.top, x2 - x1, u.bbox.height); runStart = null;
      }
    }
    if (runStart != null) {
      const x1 = u.valToPos(runStart, "x", true), x2 = u.bbox.left + u.bbox.width;
      ctx.fillRect(x1, u.bbox.top, x2 - x1, u.bbox.height);
    }
    if (current && current.kickoff_ts) {
      const x = u.valToPos(current.kickoff_ts, "x", true);
      ctx.strokeStyle = "#2ea043"; ctx.setLineDash([4, 3]);
      ctx.beginPath(); ctx.moveTo(x, u.bbox.top); ctx.lineTo(x, u.bbox.top + u.bbox.height); ctx.stroke();
      ctx.fillStyle = "#2ea043"; ctx.font = "10px sans-serif"; ctx.fillText("开赛", x + 3, u.bbox.top + 11);
    }
    ctx.restore();
  } } };
}

function computeSignal(d) {
  const n = d.x.length;
  const sig = new Array(n), synth = new Array(n), cap = new Array(n), usd = new Array(n);
  for (let i = 0; i < n; i++) {
    const da = d.draw_ask[i], daSz = d.draw_ask_size ? d.draw_ask_size[i] : null;
    if (da == null) { sig[i] = synth[i] = cap[i] = usd[i] = null; continue; }
    let s = 0, any = false, minSz = (daSz != null ? daSz : 0);
    for (const lab of selected) {
      const v = d.scores[lab] ? d.scores[lab][i] : null;
      const vs = d.scores_size && d.scores_size[lab] ? d.scores_size[lab][i] : null;
      if (v != null) { s += v; any = true; minSz = Math.min(minSz, vs != null ? vs : 0); }
    }
    synth[i] = any ? s : null;
    sig[i] = any ? (s - da) : null;
    cap[i] = any ? minSz : null;                       // executable shares (thinnest leg)
    usd[i] = (any && sig[i] > 0) ? sig[i] * minSz : 0;  // executable $ when arb live
  }
  return { sig, synth, cap, usd };
}

function build(d) {
  current = d;
  if (chart) { chart.destroy(); chart = null; }
  const { sig, synth, cap, usd } = computeSignal(d);
  const showComp = $("show-comp").checked;
  const mode = document.querySelector('input[name="metric"]:checked').value; // 'sig' | 'usd'
  const cents = (u, vals) => vals.map((v) => v == null ? "" : (v * 100).toFixed(1) + "¢");
  const dollars = (u, vals) => vals.map((v) => v == null ? "" : "$" + v.toFixed(0));

  let data, series, axisVals;
  if (mode === "usd") {
    data = [d.x, usd];
    series = [{}, { label: "可成交$", stroke: "#2ea043", width: 2, fill: "rgba(46,160,67,0.15)",
                    value: (u, v) => v == null ? "" : "$" + v.toFixed(1) }];
    axisVals = dollars;
  } else {
    data = [d.x, sig];
    series = [{}, { label: "信号 Σbid−ask平", stroke: "#d4a017", width: 2,
                    value: (u, v) => v == null ? "" : (v * 100).toFixed(2) + "¢" }];
    if (showComp) {
      data.push(synth, d.draw_ask);
      series.push({ label: "合成(Σbid)", stroke: "#2f81f7", width: 1 });
      series.push({ label: "平局 ask", stroke: "#e5534b", width: 1 });
    }
    axisVals = cents;
  }
  chart = new uPlot({
    width: Math.max(900, window.innerWidth - 60),
    height: Math.max(420, window.innerHeight - 320),
    scales: { x: { time: true } },
    series,
    axes: [{ stroke: "#8b949e", grid: { stroke: "#2a314055" } },
           { stroke: "#8b949e", grid: { stroke: "#2a314055" }, values: axisVals }],
    legend: { live: true },
    plugins: mode === "usd" ? [] : [zeroPlugin()],
  }, data, $("chart"));

  // stats: signal + capacity/$ together
  const span = d.x.length > 1 ? (d.x[d.x.length - 1] - d.x[0]) / (d.x.length - 1) : 0;
  let posPts = 0, maxv = -1, maxUsd = 0, maxCapAtPos = 0;
  for (let i = 0; i < sig.length; i++) {
    if (sig[i] != null && sig[i] > 0) {
      posPts++;
      if (sig[i] > maxv) { maxv = sig[i]; maxCapAtPos = cap[i] || 0; }
      if (usd[i] > maxUsd) maxUsd = usd[i];
    }
  }
  $("info").innerHTML = `<b>${d.home} vs ${d.away}</b> · 纳入比分 [${[...selected].join(", ") || "无"}] · ` +
    (posPts
      ? `<b class="pos">套利窗口 ${posPts} 点（约 ${Math.round(posPts*span)}s）· 最大信号 +${(maxv*100).toFixed(2)}¢（该刻容量约 ${(maxCapAtPos/1000).toFixed(1)}k股）· 最大可成交 ≈ $${maxUsd.toFixed(0)}</b>`
      : `<b class="neg">信号全程 ≤0（无套利窗口，符合常态）</b>`);
}

function renderToggles(avail) {
  $("score-toggles").innerHTML = avail.map((lab) =>
    `<label style="margin-right:10px"><input type="checkbox" class="sc" value="${lab}" ${selected.has(lab) ? "checked" : ""}/> ${lab}</label>`).join("");
  document.querySelectorAll(".sc").forEach((cb) => cb.addEventListener("change", () => {
    cb.checked ? selected.add(cb.value) : selected.delete(cb.value);
    if (current) build(current);
  }));
}

async function load(slug) {
  $("info").textContent = "加载中…";
  const r = await fetch("/api/drawarb?slug=" + encodeURIComponent(slug));
  if (!r.ok) { $("info").textContent = "无数据"; return; }
  const d = await r.json();
  selected = new Set(d.available);  // default: all draw scores
  renderToggles(d.available);
  build(d);
}

async function init() {
  const { games } = await (await fetch("/api/replay/games")).json();
  const sel = $("game");
  if (!games.length) { $("info").textContent = "暂无数据"; return; }
  sel.innerHTML = games.map((g) => {
    const nm = (g.home && g.away) ? `${g.home} vs ${g.away}` : g.slug;
    return `<option value="${g.slug}">${nm} · ${g.resolved ? "✓已结算" : "进行中"} · ${(g.ticks/1000).toFixed(0)}k</option>`;
  }).join("");
  sel.addEventListener("change", () => load(sel.value));
  const def = games.find((g) => g.resolved) || games[0];
  sel.value = def.slug; load(def.slug);
}
$("show-comp").addEventListener("change", () => { if (current) build(current); });
document.querySelectorAll('input[name="metric"]').forEach((r) =>
  r.addEventListener("change", () => { if (current) build(current); }));
window.addEventListener("resize", () => { if (current) build(current); });
init();
