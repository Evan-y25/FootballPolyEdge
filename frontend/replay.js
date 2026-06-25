"use strict";
const $ = (id) => document.getElementById(id);
let chart = null;
let current = null; // last loaded payload

function scoreColor(i, n) {
  const h = Math.round((i / Math.max(1, n)) * 320);
  return `hsl(${h},55%,60%)`;
}

// vertical markers for kickoff + detected goals
function markersPlugin() {
  return {
    hooks: {
      draw: (u) => {
        const d = current;
        if (!d) return;
        const ctx = u.ctx, top = u.bbox.top, bot = u.bbox.top + u.bbox.height;
        const vline = (ts, color, dash, label) => {
          const x = u.valToPos(ts, "x", true);
          if (x < u.bbox.left || x > u.bbox.left + u.bbox.width) return;
          ctx.save();
          ctx.strokeStyle = color; ctx.lineWidth = 1; ctx.setLineDash(dash);
          ctx.beginPath(); ctx.moveTo(x, top); ctx.lineTo(x, bot); ctx.stroke();
          if (label) { ctx.setLineDash([]); ctx.fillStyle = color; ctx.font = "10px sans-serif";
            ctx.fillText(label, x + 3, top + 11); }
          ctx.restore();
        };
        if (d.kickoff_ts) vline(d.kickoff_ts, "#2ea043", [4, 3], "开赛");
        (d.goals || []).forEach((g) =>
          vline(g.ts, g.dir === "home" ? "#e5534b" : "#2f81f7", [2, 3], null));
      },
    },
  };
}

function buildChart(d) {
  current = d;
  if (chart) { chart.destroy(); chart = null; }

  const data = [d.x];
  const series = [{}];
  const show1x2 = $("show-1x2").checked;
  const showScores = $("show-scores").checked;

  const ox = d.onex2 || {};
  [["主胜 " + d.home, "home", "#e5534b"], ["平局", "draw", "#8b949e"], ["客胜 " + d.away, "away", "#2f81f7"]]
    .forEach(([lab, k, col]) => {
      if (ox[k]) { series.push({ label: lab, stroke: col, width: 2, show: show1x2 }); data.push(ox[k]); }
    });

  const scoreLabels = Object.keys(d.scores || {}).sort();
  scoreLabels.forEach((lab, i) => {
    series.push({ label: lab, stroke: scoreColor(i, scoreLabels.length), width: 1, show: showScores });
    data.push(d.scores[lab]);
  });

  const pct = (u, vals) => vals.map((v) => v == null ? "" : (v * 100).toFixed(0) + "%");
  const opts = {
    width: Math.max(900, window.innerWidth - 60),
    height: Math.max(420, window.innerHeight - 320),
    scales: { x: { time: true }, y: { range: [0, 1] } },
    series,
    axes: [
      { stroke: "#8b949e", grid: { stroke: "#2a314055" }, ticks: { stroke: "#2a3140" } },
      { stroke: "#8b949e", grid: { stroke: "#2a314055" }, ticks: { stroke: "#2a3140" }, values: pct },
    ],
    legend: { live: true },
    plugins: [markersPlugin()],
  };
  chart = new uPlot(opts, data, $("chart"));
}

async function loadGame(slug) {
  $("info").textContent = "加载中…";
  const r = await fetch("/api/replay?slug=" + encodeURIComponent(slug));
  if (!r.ok) { $("info").textContent = "无数据"; return; }
  const d = await r.json();
  const res = d.resolution || {};
  // final score from resolution: the score label whose winner == 'yes'
  let finalScore = "";
  for (const k in res) { if (k.startsWith("score|") && res[k] === "yes") finalScore = k.split("|")[1]; }
  const winLeg = res["1x2|home"] === "yes" ? "主胜" : res["1x2|draw"] === "yes" ? "平局" : res["1x2|away"] === "yes" ? "客胜" : "?";
  $("info").innerHTML = `<b>${d.home} vs ${d.away}</b> · ${d.x.length} 采样点 · 检测到疑似进球 <b>${(d.goals||[]).length}</b> 次` +
    (finalScore ? ` · 终场比分 <b class="res">${finalScore}</b>（${winLeg}）` : " · 未结算");
  buildChart(d);
}

async function init() {
  const r = await fetch("/api/replay/games");
  const { games } = await r.json();
  const sel = $("game");
  if (!games.length) { $("info").textContent = "暂无任何比赛的盘口数据"; return; }
  sel.innerHTML = games.map((g) => {
    const tag = g.resolved ? "✓已结算" : "进行/未结算";
    const nm = (g.home && g.away) ? `${g.home} vs ${g.away}` : g.slug;
    return `<option value="${g.slug}">${nm} · ${tag} · ${(g.ticks/1000).toFixed(0)}k点</option>`;
  }).join("");
  sel.addEventListener("change", () => loadGame(sel.value));
  // default: first resolved game (most interesting — has goals), else first
  const def = games.find((g) => g.resolved) || games[0];
  sel.value = def.slug;
  loadGame(def.slug);
}

["show-1x2", "show-scores"].forEach((id) =>
  $(id).addEventListener("change", () => { if (current) buildChart(current); }));
window.addEventListener("resize", () => { if (current) buildChart(current); });
init();
