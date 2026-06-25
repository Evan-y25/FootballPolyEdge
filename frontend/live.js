"use strict";
const $ = (id) => document.getElementById(id);
let armed = false;

function card(k, v, cls = "") { return `<div class="card"><div class="k">${k}</div><div class="v ${cls}">${v}</div></div>`; }

function render(d) {
  armed = d.armed;
  const gate = d.enabled ? (d.ready ? (d.armed ? ["⚔️ 已武装", "bad"] : ["就绪·未武装", "warn"]) : ["未就绪", "bad"]) : ["LIVE_ENABLED=0", "muted"];
  $("cards").innerHTML =
    card("主闸 LIVE_ENABLED", d.enabled ? "ON" : "OFF", d.enabled ? "ok" : "bad") +
    card("连接状态", gate[0], gate[1]) +
    card("签名地址(EOA)", d.address || "—") +
    card("Funder (Safe)", d.funder || "—") +
    card("USDC 余额", d.usdc != null ? "$" + d.usdc : "—", d.usdc ? "ok" : "") +
    card("已投入(累计)", "$" + (d.deployed || 0), "") +
    card("上限/腿 · 总", d.caps ? `$${d.caps.per_leg} · $${d.caps.total}` : "—") +
    card("最低edge", d.caps ? (d.caps.min_edge * 100).toFixed(1) + "%" : "—");
  if (d.error) $("msg").innerHTML = `<span class="bad">${d.error}</span>`;
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
      const fill = b.complete ? `<span class="ok">3/3 ✅</span>` : `<span class="bad">${b.filled_legs}/3 ⚠️敞口</span>`;
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

$("test").addEventListener("click", async () => {
  $("msg").textContent = "连接中…";
  const d = await (await fetch("/api/live/test", { method: "POST" })).json();
  $("msg").innerHTML = d.ok ? `<span class="ok">连接成功</span>` : `<span class="bad">${(d.status && d.status.error) || d.error || "失败"}</span>`;
  if (d.status) render(d.status);
});

$("arm").addEventListener("click", async () => {
  if (!armed) {
    if (!confirm("确认武装实盘？武装后系统会用真实资金自动下单（受 $/腿 与总额上限约束）。")) return;
  }
  const d = await (await fetch("/api/live/arm", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ armed: !armed }) })).json();
  if (!d.ok) $("msg").innerHTML = `<span class="bad">${d.error}</span>`;
  fetchLive();
});

fetchLive();
setInterval(fetchLive, 3000);
