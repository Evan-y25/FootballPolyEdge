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
  const n = d.x.length, sig = new Array(n), synth = new Array(n);
  for (let i = 0; i < n; i++) {
    const da = d.draw_ask[i];
    if (da == null) { sig[i] = null; synth[i] = null; continue; }
    let s = 0, any = false;
    for (const lab of selected) {
      const v = d.scores[lab] ? d.scores[lab][i] : null;
      if (v != null) { s += v; any = true; }
    }
    synth[i] = any ? s : null;
    sig[i] = any ? (s - da) : null;
  }
  return { sig, synth };
}

function build(d) {
  current = d;
  if (chart) { chart.destroy(); chart = null; }
  const { sig, synth } = computeSignal(d);
  const showComp = $("show-comp").checked;
  const cents = (u, vals) => vals.map((v) => v == null ? "" : (v * 100).toFixed(1) + "¢");

  const data = [d.x, sig];
  const series = [{}, { label: "信号 Σbid−ask平", stroke: "#d4a017", width: 2, value: (u, v) => v == null ? "" : (v * 100).toFixed(2) + "¢" }];
  if (showComp) {
    data.push(synth, d.draw_ask);
    series.push({ label: "合成(Σbid)", stroke: "#2f81f7", width: 1 });
    series.push({ label: "平局 ask", stroke: "#e5534b", width: 1 });
  }
  chart = new uPlot({
    width: Math.max(900, window.innerWidth - 60),
    height: Math.max(420, window.innerHeight - 320),
    scales: { x: { time: true } },
    series,
    axes: [{ stroke: "#8b949e", grid: { stroke: "#2a314055" } },
           { stroke: "#8b949e", grid: { stroke: "#2a314055" }, values: cents }],
    legend: { live: true },
    plugins: [zeroPlugin()],
  }, data, $("chart"));

  // stats
  const pos = sig.filter((v) => v != null && v > 0);
  const maxv = sig.reduce((m, v) => (v != null && v > m ? v : m), -1);
  const span = d.x.length > 1 ? (d.x[d.x.length - 1] - d.x[0]) / (d.x.length - 1) : 0;
  const posSecs = Math.round(pos.length * span);
  $("info").innerHTML = `<b>${d.home} vs ${d.away}</b> · 纳入比分 [${[...selected].join(", ") || "无"}] · ` +
    (pos.length
      ? `<b class="pos">信号转正 ${pos.length} 个采样点（约 ${posSecs}s），最大 +${(maxv*100).toFixed(2)}¢</b> → 有套利窗口`
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
window.addEventListener("resize", () => { if (current) build(current); });
init();
