/* Aegis Trader dashboard client.
   Vanilla JS: REST polling for state, websocket for the live event feed,
   hand-rolled SVG line chart with crosshair + tooltip (single series). */

"use strict";

const $ = (sel) => document.querySelector(sel);
const fmtUsd = (v) =>
  v == null ? "—" : Number(v).toLocaleString("en-US", { style: "currency", currency: "USD" });
const fmtPct = (v) => (v == null ? "—" : (Number(v) * 100).toFixed(2) + "%");
const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

async function getJSON(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`${url}: ${res.status}`);
  return res.json();
}
async function postJSON(url, body) {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  if (!res.ok) throw new Error(`${url}: ${res.status}`);
  return res.json();
}

/* ---------- top bar / status ---------- */

async function refreshStatus() {
  const s = await getJSON("/api/status");
  const modePill = $("#pill-mode");
  modePill.textContent = "mode: " + s.mode;
  modePill.className = "pill " + (s.mode === "autonomous" ? "warn" : "ok");
  $("#mode-select").value = s.mode;

  $("#pill-session").textContent = "session: " + s.market_session;
  const health = $("#pill-health");
  health.textContent = "health: " + s.health.overall;
  health.className = "pill " + (s.health.overall === "healthy" ? "ok" : s.health.overall === "degraded" ? "warn" : "bad");
  const circuit = $("#pill-circuit");
  circuit.textContent = s.risk.circuit_open ? "circuit: OPEN" : "circuit: closed";
  circuit.className = "pill " + (s.risk.circuit_open ? "bad" : "ok");
  circuit.title = s.risk.circuit_reason || "trading permitted";

  renderRiskMeters(s.risk);
  renderSystem(s.health.components, s.broker);
  renderProviders(s.providers);
}

function meter(label, used, limit, note) {
  const ratio = limit > 0 ? Math.min(used / limit, 1) : 0;
  const cls = ratio >= 1 ? "bad" : ratio >= 0.7 ? "warn" : "";
  return `<div class="meter">
    <div class="meter-head"><span>${esc(label)}</span>
      <span class="val">${fmtPct(used)} / ${fmtPct(limit)}</span></div>
    <div class="meter-track"><div class="meter-fill ${cls}" style="width:${(ratio * 100).toFixed(1)}%"></div></div>
    ${note ? `<div class="meter-note">${esc(note)}</div>` : ""}
  </div>`;
}

function renderRiskMeters(risk) {
  const limits = risk.limits || {};
  $("#risk-meters").innerHTML =
    meter("Daily loss", risk.day_loss_pct, limits.max_daily_loss_pct, "halts trading for the day at limit") +
    meter("Weekly loss", risk.week_loss_pct, limits.max_weekly_loss_pct) +
    meter("Drawdown", risk.drawdown_pct, limits.max_drawdown_pct) +
    `<div class="kv"><span class="k">Orders today</span>
      <span class="v">${risk.orders_today} / ${risk.max_orders_per_day}</span></div>`;
}

function renderSystem(components, broker) {
  const rows = Object.values(components || {}).map((c) => {
    const cls = c.state === "healthy" ? "ok" : "bad";
    const latency = c.latency_ms != null ? ` · ${c.latency_ms}ms` : "";
    return `<div class="kv"><span class="k">${esc(c.name)}</span>
      <span class="v ${cls}">${esc(c.state)}${latency}</span></div>`;
  });
  rows.unshift(`<div class="kv"><span class="k">broker</span>
    <span class="v">${esc(broker.name)}${broker.paper ? " (paper)" : ""}</span></div>`);
  $("#system-health").innerHTML = rows.join("") || '<div class="empty">no probes yet</div>';
}

function renderProviders(providers) {
  $("#providers").innerHTML = (providers || [])
    .map((p) => {
      const cls = p.available ? "ok" : "bad";
      const latency = p.last_latency_ms != null ? `${Math.round(p.last_latency_ms)}ms` : "—";
      return `<div class="kv"><span class="k">${esc(p.name)} <small>(p${p.priority})</small></span>
        <span class="v ${cls}">${p.available ? "up" : "penalized"} · ${latency}</span></div>`;
    })
    .join("") || '<div class="empty">no providers configured</div>';
}

/* ---------- portfolio ---------- */

async function refreshPortfolio() {
  const p = await getJSON("/api/portfolio");
  const account = p.account || {};
  $("#t-equity").textContent = fmtUsd(account.equity);
  $("#t-equity-sub").textContent = p.synced_at ? "synced " + new Date(p.synced_at).toLocaleTimeString() : "never synced";
  const day = account.day_pnl != null ? Number(account.day_pnl) : null;
  const dayEl = $("#t-daypnl");
  dayEl.textContent = fmtUsd(day);
  dayEl.className = "tile-value " + (day > 0 ? "pos" : day < 0 ? "neg" : "");
  $("#t-daypnl-sub").textContent = "day loss used: " + fmtPct(p.day_loss_pct);
  $("#t-cash").textContent = fmtUsd(account.cash);
  $("#t-cash-sub").textContent = "buying power " + fmtUsd(account.buying_power);
  $("#t-dd").textContent = fmtPct(p.drawdown_pct);
  $("#t-exposure").textContent = fmtUsd(p.gross_exposure);
  $("#t-exposure-sub").textContent = "options " + fmtUsd(p.options_exposure);

  const tbody = $("#positions-table tbody");
  const positions = p.positions || [];
  tbody.innerHTML = positions.length
    ? positions.map((pos) => {
        const upl = pos.unrealized_pnl != null ? Number(pos.unrealized_pnl) : null;
        return `<tr><td><span class="sym">${esc(pos.symbol)}</span> <small>${esc(pos.asset_class)}</small></td>
        <td class="num">${esc(pos.quantity)}</td>
        <td class="num">${fmtUsd(pos.avg_entry_price)}</td>
        <td class="num">${fmtUsd(pos.market_value)}</td>
        <td class="num ${upl > 0 ? "pos" : upl < 0 ? "neg" : ""}">${fmtUsd(upl)}</td></tr>`;
      }).join("")
    : '<tr><td colspan="5" class="empty">no positions</td></tr>';

  renderAllocation(positions, account.equity);
}

function renderAllocation(positions, equity) {
  const el = $("#allocation");
  const eq = Number(equity) || 0;
  if (!positions.length || eq <= 0) { el.innerHTML = '<div class="empty">no positions</div>'; return; }
  const rows = positions
    .map((p) => ({ sym: p.symbol, v: Math.abs(Number(p.market_value) || 0) }))
    .sort((a, b) => b.v - a.v)
    .slice(0, 12);
  const max = rows[0].v || 1;
  el.innerHTML = rows.map((r) =>
    `<div class="alloc-row"><span class="alloc-sym">${esc(r.sym)}</span>
     <div class="alloc-bar"><div class="alloc-fill" style="width:${((r.v / max) * 100).toFixed(1)}%"></div></div>
     <span class="alloc-val">${fmtUsd(r.v)} · ${fmtPct(r.v / eq)}</span></div>`
  ).join("");
}

/* ---------- equity chart (single series line, crosshair + tooltip) ---------- */

let equityPoints = [];

async function refreshEquity() {
  const data = await getJSON("/api/equity");
  equityPoints = data.points || [];
  drawEquity();
}

function drawEquity() {
  const svg = $("#equity-chart");
  const { width } = svg.getBoundingClientRect();
  const height = 260, padL = 56, padR = 12, padT = 12, padB = 24;
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svg.innerHTML = "";
  if (equityPoints.length < 2) {
    svg.innerHTML = `<text x="${width / 2}" y="${height / 2}" fill="#898781" text-anchor="middle" font-size="13">not enough equity history yet</text>`;
    return;
  }
  const values = equityPoints.map((p) => p.equity);
  const min = Math.min(...values), max = Math.max(...values);
  const span = max - min || 1;
  const x = (i) => padL + (i / (equityPoints.length - 1)) * (width - padL - padR);
  const y = (v) => padT + (1 - (v - min) / span) * (height - padT - padB);

  let grid = "", labels = "";
  for (let g = 0; g <= 3; g++) {
    const value = min + (span * g) / 3;
    const gy = y(value);
    grid += `<line x1="${padL}" y1="${gy}" x2="${width - padR}" y2="${gy}" stroke="#2c2c2a" stroke-width="1"/>`;
    labels += `<text x="${padL - 8}" y="${gy + 4}" fill="#898781" font-size="11" text-anchor="end" style="font-variant-numeric:tabular-nums">${Math.round(value).toLocaleString()}</text>`;
  }
  const path = values.map((v, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join("");
  svg.innerHTML = `${grid}${labels}
    <line x1="${padL}" y1="${height - padB}" x2="${width - padR}" y2="${height - padB}" stroke="#383835" stroke-width="1"/>
    <path d="${path}" fill="none" stroke="#3987e5" stroke-width="2" stroke-linejoin="round"/>
    <line id="xhair" x1="0" y1="${padT}" x2="0" y2="${height - padB}" stroke="#898781" stroke-width="1" stroke-dasharray="3,3" visibility="hidden"/>
    <circle id="xdot" r="4" fill="#3987e5" stroke="#1a1a19" stroke-width="2" visibility="hidden"/>`;

  const tip = $("#equity-tip");
  svg.onmousemove = (evt) => {
    const rect = svg.getBoundingClientRect();
    const mx = evt.clientX - rect.left;
    const idx = Math.max(0, Math.min(equityPoints.length - 1,
      Math.round(((mx - padL) / (width - padL - padR)) * (equityPoints.length - 1))));
    const point = equityPoints[idx];
    const px = x(idx), py = y(point.equity);
    const xhair = svg.querySelector("#xhair"), dot = svg.querySelector("#xdot");
    xhair.setAttribute("x1", px); xhair.setAttribute("x2", px);
    xhair.setAttribute("visibility", "visible");
    dot.setAttribute("cx", px); dot.setAttribute("cy", py);
    dot.setAttribute("visibility", "visible");
    tip.hidden = false;
    tip.style.left = Math.min(px + 12, width - 170) + "px";
    tip.style.top = Math.max(py - 40, 0) + "px";
    tip.innerHTML = `<strong>${fmtUsd(point.equity)}</strong><br>${new Date(point.at).toLocaleString()}`;
  };
  svg.onmouseleave = () => {
    tip.hidden = true;
    svg.querySelector("#xhair")?.setAttribute("visibility", "hidden");
    svg.querySelector("#xdot")?.setAttribute("visibility", "hidden");
  };
}

/* ---------- orders / decisions / approvals ---------- */

function statusClass(status) {
  if (status === "filled") return "filled";
  if (status.startsWith("rejected") || status === "error") return "rejected";
  if (status === "pending_approval") return "pending";
  return "";
}

async function refreshOrders() {
  const data = await getJSON("/api/orders");
  const tbody = $("#orders-table tbody");
  const orders = data.orders || [];
  tbody.innerHTML = orders.length
    ? orders.map((o) => {
        const open = ["submitted", "accepted", "partially_filled"].includes(o.status);
        return `<tr>
        <td>${o.created_at ? new Date(o.created_at).toLocaleTimeString() : "—"}</td>
        <td><span class="sym">${esc(o.symbol)}</span></td>
        <td>${esc(o.side)}</td>
        <td class="num">${esc(o.quantity)}</td>
        <td class="num">${o.limit_price ? fmtUsd(o.limit_price) : "mkt"}</td>
        <td><span class="status-tag ${statusClass(o.status)}">${esc(o.status)}</span></td>
        <td>${esc(o.status_reason || "")}</td>
        <td>${open ? `<button class="btn" data-cancel="${esc(o.id)}">Cancel</button>` : ""}</td></tr>`;
      }).join("")
    : '<tr><td colspan="8" class="empty">no orders yet</td></tr>';
  tbody.querySelectorAll("[data-cancel]").forEach((btn) =>
    btn.addEventListener("click", () => postJSON(`/api/orders/${btn.dataset.cancel}/cancel`).then(refreshOrders)));
}

async function refreshDecisions() {
  const data = await getJSON("/api/decisions");
  const el = $("#decisions");
  const decisions = data.decisions || [];
  el.innerHTML = decisions.length
    ? decisions.map((d) => {
        const r = d.rationale;
        const trades = (d.trades || [])
          .map((t) => `${t.side} ${t.quantity} ${t.symbol} @ ${t.limit_price ?? "mkt"}`).join("; ");
        return `<div class="decision">
          <div class="head">${esc(d.action)} ${trades ? "— " + esc(trades) : ""}</div>
          <div class="meta">cycle ${esc(d.cycle_id)} · ${d.created_at ? new Date(d.created_at).toLocaleString() : ""}
            · sources: ${esc((d.data_sources || []).join(", ") || "n/a")}</div>
          ${r ? `<p><strong>Thesis:</strong> ${esc(r.thesis)}</p>
                 <p><strong>Why now:</strong> ${esc(r.timing)}</p>
                 <p><strong>Risk:</strong> ${esc(r.risk)} · <strong>Confidence:</strong> ${fmtPct(r.confidence)}</p>` : ""}
        </div>`;
      }).join("")
    : '<div class="empty">no decisions yet</div>';
}

async function refreshApprovals() {
  const data = await getJSON("/api/approvals");
  const el = $("#approvals");
  const approvals = data.approvals || [];
  el.innerHTML = approvals.length
    ? approvals.map((a) => {
        const o = a.order, r = a.rationale;
        return `<div class="approval">
          <div class="head">${esc(o.side)} ${esc(o.quantity)} ${esc(o.symbol)} @ ${o.limit_price ? fmtUsd(o.limit_price) : "mkt"}</div>
          ${r ? `<p>${esc(r.thesis)}</p><p><small>confidence ${fmtPct(r.confidence)} · max loss: ${esc(r.max_expected_loss)}</small></p>` : ""}
          <div><small>expires in ${Math.floor(a.seconds_remaining / 60)}m ${a.seconds_remaining % 60}s</small></div>
          <div class="actions">
            <button class="btn btn-approve" data-approve="${esc(o.id)}">Approve</button>
            <button class="btn btn-reject" data-reject="${esc(o.id)}">Reject</button>
          </div></div>`;
      }).join("")
    : '<div class="empty">nothing awaiting approval</div>';
  el.querySelectorAll("[data-approve]").forEach((b) =>
    b.addEventListener("click", () => postJSON(`/api/approvals/${b.dataset.approve}`, { approve: true }).then(refreshApprovals)));
  el.querySelectorAll("[data-reject]").forEach((b) =>
    b.addEventListener("click", () => postJSON(`/api/approvals/${b.dataset.reject}`, { approve: false }).then(refreshApprovals)));
}

/* ---------- live event feed ---------- */

function connectWebsocket() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws`);
  const feed = $("#events");
  ws.onmessage = (msg) => {
    let evt;
    try { evt = JSON.parse(msg.data); } catch { return; }
    const row = document.createElement("div");
    row.className = "evt";
    const summary = typeof evt.payload === "object" && evt.payload
      ? esc(JSON.stringify(evt.payload).slice(0, 220)) : esc(evt.payload);
    row.innerHTML = `<time>${new Date().toLocaleTimeString()}</time><span class="topic">${esc(evt.topic)}</span>${summary}`;
    feed.prepend(row);
    while (feed.children.length > 200) feed.lastChild.remove();
    if (["ai.approval_requested", "order.filled", "order.rejected"].includes(evt.topic)) {
      refreshApprovals(); refreshOrders();
    }
    if (evt.topic === "portfolio.synced") refreshPortfolio();
  };
  ws.onclose = () => setTimeout(connectWebsocket, 3000);
  setInterval(() => { if (ws.readyState === 1) ws.send("ping"); }, 25000);
}

/* ---------- actions & boot ---------- */

$("#mode-select").addEventListener("change", (e) =>
  postJSON("/api/mode", { mode: e.target.value }).then(refreshStatus).catch(() => refreshStatus()));
$("#btn-halt").addEventListener("click", () => {
  if (confirm("Halt all trading? The circuit breaker stays open until you resume."))
    postJSON("/api/halt", { reason: "manual halt from dashboard" }).then(refreshStatus);
});
$("#btn-resume").addEventListener("click", () => postJSON("/api/resume").then(refreshStatus));
$("#btn-cycle").addEventListener("click", () => postJSON("/api/cycle"));

async function refreshAll() {
  await Promise.allSettled([
    refreshStatus(), refreshPortfolio(), refreshEquity(),
    refreshOrders(), refreshDecisions(), refreshApprovals(),
  ]);
}
window.addEventListener("resize", drawEquity);
refreshAll();
connectWebsocket();
setInterval(refreshAll, 30000);
