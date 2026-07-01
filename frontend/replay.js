"use strict";
const $ = (id) => document.getElementById(id);
let chart = null;
let current = null;                 // last loaded payload
const shownGroups = new Set(["1x2"]); // which market groups are plotted

// base hue per market group; lines within a group vary in lightness
const GROUP_HUE = {
  "1x2": 0, "score": 280, "team_to_advance": 40, "spread": 120, "totals": 200,
  "team_totals": 170, "btts": 320, "first_to_score": 60, "halves": 240,
  "extra_time": 20, "penalty": 350, "more_other": 100,
};

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

  (d.groups || []).forEach((grp) => {
    if (!shownGroups.has(grp.key)) return;
    const mk = (d.markets && d.markets[grp.key]) || {};
    if (grp.key === "1x2") {
      [["主胜 " + d.home, "home", "#e5534b"], ["平局", "draw", "#8b949e"], ["客胜 " + d.away, "away", "#2f81f7"]]
        .forEach(([lab, k, col]) => { if (mk[k]) { series.push({ label: lab, stroke: col, width: 2 }); data.push(mk[k]); } });
      return;
    }
    const labels = grp.labels || Object.keys(mk).sort();
    const hue = GROUP_HUE[grp.key] ?? 100;
    const tag = (grp.title.split(" ")[0]) || grp.key;
    labels.forEach((lab, i) => {
      if (!mk[lab]) return;
      const light = 45 + Math.round((i / Math.max(1, labels.length)) * 32);
      series.push({ label: `${tag}·${lab}`, stroke: `hsl(${hue},62%,${light}%)`, width: 1 });
      data.push(mk[lab]);
    });
  });

  const pct = (u, vals) => vals.map((v) => v == null ? "" : (v * 100).toFixed(0) + "%");
  const opts = {
    width: Math.max(900, window.innerWidth - 60),
    height: Math.max(420, window.innerHeight - 340),
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

function renderGroupToggles(d) {
  const box = $("groups");
  box.innerHTML = (d.groups || []).map((g) => {
    const n = (g.labels || []).length;
    const ck = shownGroups.has(g.key) ? "checked" : "";
    return `<label><input type="checkbox" data-k="${g.key}" ${ck}/> ${g.title}${n > 1 ? ` (${n})` : ""}</label>`;
  }).join("") || `<span class="muted">该场无盘口分组</span>`;
  box.querySelectorAll("input[type=checkbox]").forEach((cb) => {
    cb.addEventListener("change", () => {
      if (cb.checked) shownGroups.add(cb.dataset.k); else shownGroups.delete(cb.dataset.k);
      if (current) buildChart(current);
    });
  });
}

async function loadGame(slug) {
  $("info").textContent = "加载中…";
  const r = await fetch("/api/replay?slug=" + encodeURIComponent(slug));
  if (!r.ok) { $("info").textContent = "无数据"; return; }
  const d = await r.json();
  const res = d.resolution || {};
  let finalScore = "";
  for (const k in res) { if (k.startsWith("score|") && res[k] === "yes") finalScore = k.split("|")[1]; }
  const winLeg = res["1x2|home"] === "yes" ? "主胜" : res["1x2|draw"] === "yes" ? "平局" : res["1x2|away"] === "yes" ? "客胜" : "?";
  const nGroups = (d.groups || []).length;
  $("info").innerHTML = `<b>${d.home} vs ${d.away}</b> · ${d.x.length} 采样点 · ${nGroups} 类盘口 · 检测到疑似进球 <b>${(d.goals || []).length}</b> 次` +
    (finalScore ? ` · 终场比分 <b class="res">${finalScore}</b>（${winLeg}）` : " · 未结算");
  renderGroupToggles(d);
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
    const multi = g.ngroups >= 3 ? ` · ${g.ngroups}组盘口` : " · 仅1X2+波胆";
    return `<option value="${g.slug}">${nm} · ${tag} · ${(g.ticks / 1000).toFixed(0)}k点${multi}</option>`;
  }).join("");
  sel.addEventListener("change", () => loadGame(sel.value));
  // prefer a game that actually has the extra market groups (post-deploy games)
  const def = games.find((g) => g.ngroups >= 3) || games.find((g) => g.resolved) || games[0];
  sel.value = def.slug;
  loadGame(def.slug);
}

window.addEventListener("resize", () => { if (current) buildChart(current); });
init();
