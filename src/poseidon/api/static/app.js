/* Poseidon dashboard client.
   Vanilla JS app shell: hash-routed views, REST polling per view,
   websocket for live events, hand-rolled SVG chart. No external assets. */

"use strict";

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => document.querySelectorAll(sel);
const AUTH_TOKEN = new URLSearchParams(location.search).get("token");
const authHeaders = () => (AUTH_TOKEN ? { Authorization: "Bearer " + AUTH_TOKEN } : {});
const fmtUsd = (v) =>
  v == null ? "—" : Number(v).toLocaleString("en-US", { style: "currency", currency: "USD" });
const fmtPct = (v, dp = 2) => (v == null ? "—" : (Number(v) * 100).toFixed(dp) + "%");
const fmtNum = (v) => (v == null ? "—" : Number(v).toLocaleString());
const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

async function getJSON(url) {
  const res = await fetch(url, { headers: authHeaders() });
  if (!res.ok) throw new Error(`${url}: ${res.status}`);
  return res.json();
}
async function postJSON(url, body) {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify(body || {}),
  });
  if (!res.ok) throw new Error(`${url}: ${res.status}`);
  return res.json();
}

function toast(message, kind = "") {
  const el = document.createElement("div");
  el.className = "toast " + kind;
  el.textContent = message;
  $("#toasts").appendChild(el);
  setTimeout(() => el.remove(), 4200);
}

/* ================= routing ================= */

const VIEWS = {
  overview:    { title: "Overview",    refresh: () => Promise.allSettled([refreshStatus(), refreshPortfolio(), refreshEquity(), refreshRiskMetrics()]) },
  portfolio:   { title: "Portfolio",   refresh: () => Promise.allSettled([refreshStatus(), refreshPortfolio(), refreshOrders(), refreshExitPlans()]) },
  algorithms:  { title: "Algorithms",  refresh: () => Promise.allSettled([refreshStatus(), refreshAlgorithms()]) },
  ai:          { title: "AI Desk",     refresh: () => Promise.allSettled([refreshStatus(), refreshApprovals(), refreshDecisions(), refreshAiUsage()]) },
  risk:        { title: "Risk",        refresh: () => Promise.allSettled([refreshStatus(), refreshRiskMetrics()]) },
  performance: { title: "Performance", refresh: () => Promise.allSettled([refreshPerformance(), refreshExecution()]) },
  system:      { title: "System",      refresh: () => Promise.allSettled([refreshStatus(), refreshAudit()]) },
};

function currentView() {
  const name = (location.hash || "#/overview").replace("#/", "");
  return VIEWS[name] ? name : "overview";
}

function route() {
  const name = currentView();
  $$(".view").forEach((v) => (v.hidden = v.dataset.view !== name));
  $$("#nav a").forEach((a) => a.classList.toggle("active", a.dataset.view === name));
  $("#view-title").textContent = VIEWS[name].title;
  VIEWS[name].refresh();
  if (name === "overview") requestAnimationFrame(drawEquity);
}
window.addEventListener("hashchange", route);

/* ================= status / topbar ================= */

let lastStatus = null;

async function refreshStatus() {
  const s = await getJSON("/api/status");
  lastStatus = s;
  $("#app-version").textContent = "v" + s.version + (s.update_available ? " · update available" : "");

  $$("#mode-seg button").forEach((b) => b.classList.toggle("active", b.dataset.mode === s.mode));
  const session = $("#pill-session");
  session.textContent = "session " + s.market_session.replace("_", " ");
  session.className = "pill " + (s.market_session === "regular" ? "ok" : "info");

  const health = $("#pill-health");
  health.textContent = "health " + s.health.overall;
  health.className = "pill " + (s.health.overall === "healthy" ? "ok" : s.health.overall === "degraded" ? "warn" : "bad");

  const circuit = $("#pill-circuit");
  circuit.textContent = s.risk.circuit_open ? "circuit OPEN" : "circuit closed";
  circuit.className = "pill " + (s.risk.circuit_open ? "bad" : "ok");
  circuit.title = s.risk.circuit_reason || "trading permitted";
  $("#btn-resume").hidden = !s.risk.circuit_open;
  $("#btn-halt").hidden = !!s.risk.circuit_open;

  renderRiskMeters(s.risk, "#risk-meters");
  renderRiskMeters(s.risk, "#risk-meters-2");
  renderSystem(s.health.components, s.broker);
  renderProviders(s.providers);
  renderRuntime(s);
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

function renderRiskMeters(risk, sel) {
  const el = $(sel);
  if (!el) return;
  const limits = risk.limits || {};
  el.innerHTML =
    meter("Daily loss", risk.day_loss_pct, limits.max_daily_loss_pct, "new risk halts at the limit; exits stay allowed") +
    meter("Weekly loss", risk.week_loss_pct, limits.max_weekly_loss_pct) +
    meter("Drawdown", risk.drawdown_pct, limits.max_drawdown_pct) +
    `<div class="kv"><span class="k">Orders today</span>
      <span class="v">${risk.orders_today} / ${risk.max_orders_per_day}</span></div>`;
}

function renderSystem(components, broker) {
  const el = $("#system-health");
  if (!el) return;
  const rows = Object.values(components || {}).map((c) => {
    const cls = c.state === "healthy" ? "ok" : "bad";
    const latency = c.latency_ms != null ? ` · ${c.latency_ms}ms` : "";
    return `<div class="kv"><span class="k">${esc(c.name)}</span>
      <span class="v ${cls}">${esc(c.state)}${latency}</span></div>`;
  });
  rows.unshift(`<div class="kv"><span class="k">broker</span>
    <span class="v">${esc(broker.name)}${broker.paper ? " (paper)" : ""}</span></div>`);
  el.innerHTML = rows.join("") || '<div class="empty">no probes yet</div>';
}

function renderProviders(providers) {
  const el = $("#providers");
  if (!el) return;
  el.innerHTML = (providers || [])
    .map((p) => {
      const cls = p.available ? "ok" : "bad";
      const latency = p.last_latency_ms != null ? `${Math.round(p.last_latency_ms)}ms` : "—";
      return `<div class="kv"><span class="k">${esc(p.name)} <small>p${p.priority}</small></span>
        <span class="v ${cls}">${p.available ? "up" : "penalized"} · ${latency}</span></div>`;
    })
    .join("") || '<div class="empty">no providers configured</div>';
}

function renderRuntime(s) {
  const el = $("#runtime");
  if (!el) return;
  const guardian = s.guardian || {};
  const runs = Object.entries(s.scheduler || {}).slice(-8);
  el.innerHTML =
    `<div class="kv"><span class="k">version</span><span class="v">${esc(s.version)}</span></div>` +
    `<div class="kv"><span class="k">update</span><span class="v">${s.update_available ? "available" : "current"}</span></div>` +
    `<div class="kv"><span class="k">guardian</span><span class="v">${guardian.enabled ? "enabled" : "off"} · ${(guardian.active_plans || []).length} plans</span></div>` +
    runs.map(([job, at]) =>
      `<div class="kv"><span class="k">${esc(job)}</span>
       <span class="v">${at ? new Date(at).toLocaleTimeString() : "—"}</span></div>`).join("");
}

/* ================= portfolio ================= */

async function refreshPortfolio() {
  const p = await getJSON("/api/portfolio");
  const account = p.account || {};
  const equity = Number(account.equity) || 0;
  $("#t-equity").textContent = fmtUsd(account.equity);
  $("#t-equity-sub").textContent = p.synced_at ? "synced " + new Date(p.synced_at).toLocaleTimeString() : "never synced";
  const day = account.day_pnl != null ? Number(account.day_pnl) : null;
  const dayEl = $("#t-daypnl");
  dayEl.textContent = fmtUsd(day);
  dayEl.className = "tile-value " + (day > 0 ? "pos" : day < 0 ? "neg" : "");
  $("#t-daypnl-sub").textContent = "day loss used " + fmtPct(p.day_loss_pct);
  $("#t-cash").textContent = fmtUsd(account.cash);
  $("#t-cash-sub").textContent = "buying power " + fmtUsd(account.buying_power);
  $("#t-dd").textContent = fmtPct(p.drawdown_pct);
  $("#t-exposure").textContent = fmtUsd(p.gross_exposure);
  $("#t-exposure-sub").textContent = "options " + fmtUsd(p.options_exposure);

  const positions = p.positions || [];
  const countEl = $("#positions-count");
  if (countEl) countEl.textContent = positions.length ? positions.length + " open" : "";
  const tbody = $("#positions-table tbody");
  tbody.innerHTML = positions.length
    ? positions.map((pos) => {
        const upl = pos.unrealized_pnl != null ? Number(pos.unrealized_pnl) : null;
        const value = Math.abs(Number(pos.market_value) || 0);
        return `<tr><td><span class="sym">${esc(pos.symbol)}</span> <small>${esc(pos.asset_class)}</small></td>
        <td class="num">${esc(pos.quantity)}</td>
        <td class="num">${fmtUsd(pos.avg_entry_price)}</td>
        <td class="num">${fmtUsd(pos.market_value)}</td>
        <td class="num">${equity > 0 ? fmtPct(value / equity, 1) : "—"}</td>
        <td class="num ${upl > 0 ? "pos" : upl < 0 ? "neg" : ""}">${fmtUsd(upl)}</td></tr>`;
      }).join("")
    : '<tr><td colspan="6" class="empty">no positions</td></tr>';

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
     <span class="alloc-val">${fmtUsd(r.v)} · ${fmtPct(r.v / eq, 1)}</span></div>`
  ).join("");
}

async function refreshExitPlans() {
  const data = await getJSON("/api/exit-plans");
  const plans = data.plans || [];
  $("#exit-plans").innerHTML = plans.length
    ? plans.map((pl) =>
        `<div class="kv"><span class="k"><strong>${esc(pl.symbol)}</strong> × ${esc(pl.quantity)}</span>
         <span class="v">stop ${pl.stop_loss ? fmtUsd(pl.stop_loss) : "—"} · target ${pl.take_profit ? fmtUsd(pl.take_profit) : "—"}</span></div>`
      ).join("")
    : '<div class="empty">no armed exit plans</div>';
}

/* ================= equity chart ================= */

let equityPoints = [];

async function refreshEquity() {
  const data = await getJSON("/api/equity");
  equityPoints = data.points || [];
  drawEquity();
}

function drawEquity() {
  const svg = $("#equity-chart");
  if (!svg || svg.closest(".view").hidden) return;
  const { width } = svg.getBoundingClientRect();
  const height = 300, padL = 60, padR = 12, padT = 14, padB = 28;
  svg.setAttribute("viewBox", `0 0 ${width} ${height}`);
  svg.innerHTML = "";
  if (equityPoints.length < 2) {
    svg.innerHTML = `<text x="${width / 2}" y="${height / 2}" fill="#898781" text-anchor="middle" font-size="13">not enough equity history yet — the curve appears after a few syncs</text>`;
    return;
  }
  const first = equityPoints[0], last = equityPoints[equityPoints.length - 1];
  $("#equity-range").textContent =
    `${new Date(first.at).toLocaleDateString()} → ${new Date(last.at).toLocaleDateString()}`;
  const values = equityPoints.map((p) => p.equity);
  const min = Math.min(...values), max = Math.max(...values);
  const span = max - min || 1;
  const x = (i) => padL + (i / (equityPoints.length - 1)) * (width - padL - padR);
  const y = (v) => padT + (1 - (v - min) / span) * (height - padT - padB);

  let grid = "", labels = "";
  for (let g = 0; g <= 4; g++) {
    const value = min + (span * g) / 4;
    const gy = y(value);
    grid += `<line x1="${padL}" y1="${gy}" x2="${width - padR}" y2="${gy}" stroke="#2c2c2a" stroke-width="1"/>`;
    labels += `<text x="${padL - 8}" y="${gy + 4}" fill="#898781" font-size="11" text-anchor="end" style="font-variant-numeric:tabular-nums">${Math.round(value).toLocaleString()}</text>`;
  }
  // Sparse time labels along the x-axis.
  const ticks = Math.min(6, equityPoints.length);
  for (let t = 0; t < ticks; t++) {
    const idx = Math.round((t / (ticks - 1)) * (equityPoints.length - 1));
    labels += `<text x="${x(idx)}" y="${height - 8}" fill="#898781" font-size="10.5" text-anchor="middle">${new Date(equityPoints[idx].at).toLocaleDateString(undefined, { month: "short", day: "numeric" })}</text>`;
  }
  const line = values.map((v, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join("");
  const area = `${line}L${x(values.length - 1).toFixed(1)},${height - padB}L${padL},${height - padB}Z`;
  svg.innerHTML = `
    <defs><linearGradient id="eqfill" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0%" stop-color="#3987e5" stop-opacity="0.22"/>
      <stop offset="100%" stop-color="#3987e5" stop-opacity="0"/>
    </linearGradient></defs>
    ${grid}${labels}
    <line x1="${padL}" y1="${height - padB}" x2="${width - padR}" y2="${height - padB}" stroke="#383835" stroke-width="1"/>
    <path d="${area}" fill="url(#eqfill)"/>
    <path d="${line}" fill="none" stroke="#3987e5" stroke-width="2" stroke-linejoin="round"/>
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
    svg.querySelector("#xhair").setAttribute("x1", px);
    svg.querySelector("#xhair").setAttribute("x2", px);
    svg.querySelector("#xhair").setAttribute("visibility", "visible");
    const dot = svg.querySelector("#xdot");
    dot.setAttribute("cx", px); dot.setAttribute("cy", py);
    dot.setAttribute("visibility", "visible");
    tip.hidden = false;
    tip.style.left = Math.min(px + 12, width - 180) + "px";
    tip.style.top = Math.max(py - 44, 0) + "px";
    tip.innerHTML = `<strong>${fmtUsd(point.equity)}</strong><br>${new Date(point.at).toLocaleString()}`;
  };
  svg.onmouseleave = () => {
    tip.hidden = true;
    svg.querySelector("#xhair")?.setAttribute("visibility", "hidden");
    svg.querySelector("#xdot")?.setAttribute("visibility", "hidden");
  };
}

/* ================= risk metrics / regime ================= */

async function refreshRiskMetrics() {
  let m;
  try {
    m = await getJSON("/api/risk-metrics");
  } catch {
    setRegimePill(null);
    const card = $("#regime-card");
    if (card) card.innerHTML = '<div class="empty">risk metrics unavailable (no live benchmark history right now)</div>';
    return;
  }
  setRegimePill(m.regime);

  const tile = $("#t-regime");
  if (tile && m.regime) {
    tile.textContent = (m.regime.state || "—").replace("_", " ");
    $("#t-regime-sub").textContent = m.regime.trend ? m.regime.trend + " · " + (m.regime.benchmark || "") : "";
  }
  if (!$("#r-var95")) return;
  $("#r-var95").textContent = fmtPct(m.var_95_pct);
  $("#r-var99").textContent = fmtPct(m.var_99_pct);
  $("#r-es-sub").textContent = "ES₉₅ " + fmtPct(m.expected_shortfall_95_pct);
  $("#r-beta").textContent = m.portfolio_beta != null ? m.portfolio_beta.toFixed(2) : "—";
  $("#r-beta-sub").textContent = "vs " + (m.benchmark || "benchmark");
  $("#r-vol").textContent = fmtPct(m.annualized_volatility, 1);
  $("#r-corr").textContent = m.max_pairwise_correlation != null ? m.max_pairwise_correlation.toFixed(2) : "—";
  $("#r-corr-sub").textContent = m.most_correlated_pair ? m.most_correlated_pair.join(" ↔ ") : "";

  const r = m.regime;
  $("#regime-benchmark").textContent = r ? "benchmark " + (r.benchmark || "") : "";
  $("#regime-card").innerHTML = r
    ? `<div class="regime-state ${esc(r.state)}">${esc((r.state || "").replace("_", " "))}</div>
       <div class="kv"><span class="k">Trend</span><span class="v">${esc(r.trend)}</span></div>
       <div class="kv"><span class="k">Close / 50d / 200d</span>
         <span class="v">${fmtNum(r.close)} / ${fmtNum(r.sma_50)} / ${fmtNum(r.sma_200)}</span></div>
       <div class="kv"><span class="k">Realized vol (ann.)</span><span class="v">${fmtPct(r.realized_vol_annualized, 1)}</span></div>
       <div class="kv"><span class="k">Vol percentile (1y)</span><span class="v">${fmtPct(r.vol_percentile, 0)}</span></div>
       <div class="kv"><span class="k">Off 1y high</span><span class="v">${fmtPct(r.drawdown_from_high, 1)}</span></div>`
    : '<div class="empty">no regime reading</div>';

  $("#risk-coverage").innerHTML =
    `<div class="kv"><span class="k">Positions covered</span>
      <span class="v">${m.positions_covered} / ${m.positions_total}</span></div>` +
    `<div class="kv"><span class="k">Observations</span><span class="v">${m.observations} days</span></div>` +
    ((m.uncovered_symbols || []).length
      ? `<div class="kv"><span class="k">Uncovered</span><span class="v">${esc(m.uncovered_symbols.join(", "))}</span></div>
         <div class="meter-note">uncovered positions are excluded, never estimated</div>`
      : "") +
    `<div class="meter-note">as of ${m.as_of ? new Date(m.as_of).toLocaleTimeString() : "—"}</div>`;
}

function setRegimePill(regime) {
  const pill = $("#pill-regime");
  if (!regime || !regime.state || regime.state === "unknown") {
    pill.textContent = "regime —";
    pill.className = "pill";
    return;
  }
  pill.textContent = "regime " + regime.state.replace("_", " ");
  pill.className = "pill " + ({ risk_on: "ok", neutral: "info", risk_off: "warn", stress: "bad" }[regime.state] || "");
  pill.title = regime.detail || "";
}

/* ================= orders / decisions / approvals ================= */

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
        const slip = o.slippage_bps != null ? ` · ${Number(o.slippage_bps).toFixed(1)}bps` : "";
        return `<tr>
        <td>${o.created_at ? new Date(o.created_at).toLocaleTimeString() : "—"}</td>
        <td><span class="sym">${esc(o.symbol)}</span></td>
        <td>${esc(o.side)}</td>
        <td class="num">${esc(o.quantity)}</td>
        <td class="num">${o.limit_price ? fmtUsd(o.limit_price) : "mkt"}</td>
        <td><span class="status-tag ${statusClass(o.status)}">${esc(o.status)}</span>${slip}</td>
        <td>${esc(o.status_reason || "")}</td>
        <td>${open ? `<button class="btn btn-sm" data-cancel="${esc(o.id)}">Cancel</button>` : ""}</td></tr>`;
      }).join("")
    : '<tr><td colspan="8" class="empty">no orders yet</td></tr>';
  tbody.querySelectorAll("[data-cancel]").forEach((btn) =>
    btn.addEventListener("click", () =>
      postJSON(`/api/orders/${btn.dataset.cancel}/cancel`)
        .then(() => { toast("Cancel requested", "warn"); refreshOrders(); })
        .catch((e) => toast("Cancel failed: " + e.message, "bad"))));
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
        const conf = r && r.confidence != null
          ? `<span class="conf">confidence ${fmtPct(r.confidence, 0)}
             <span class="conf-track"><span class="conf-fill" style="width:${(r.confidence * 100).toFixed(0)}%"></span></span></span>`
          : "";
        return `<div class="decision">
          <div class="head"><span>${esc(d.action)}${trades ? " — " + esc(trades) : ""}</span>${conf}</div>
          <div class="meta">cycle ${esc(d.cycle_id)} · ${d.created_at ? new Date(d.created_at).toLocaleString() : ""}
            · sources: ${esc((d.data_sources || []).join(", ") || "n/a")}
            ${(d.data_gaps || []).length ? " · gaps: " + esc(d.data_gaps.join("; ")) : ""}</div>
          ${r ? `<p><strong>Thesis:</strong> ${esc(r.thesis)}</p>
                 <p><strong>Why now:</strong> ${esc(r.timing)}</p>
                 <p><strong>Risk:</strong> ${esc(r.risk)}</p>` : ""}
          ${d.summary ? `<p>${esc(d.summary)}</p>` : ""}
        </div>`;
      }).join("")
    : '<div class="empty">no decisions yet — run a cycle or wait for the schedule</div>';
}

async function refreshApprovals() {
  const data = await getJSON("/api/approvals");
  const el = $("#approvals");
  const approvals = data.approvals || [];
  const badge = $("#nav-badge-ai");
  badge.hidden = approvals.length === 0;
  badge.textContent = approvals.length;
  document.title = (approvals.length ? `(${approvals.length}) ` : "") + "Poseidon";
  el.innerHTML = approvals.length
    ? approvals.map((a) => {
        const o = a.order, r = a.rationale;
        return `<div class="approval">
          <div class="head">${esc(o.side)} ${esc(o.quantity)} ${esc(o.symbol)} @ ${o.limit_price ? fmtUsd(o.limit_price) : "mkt"}</div>
          ${r ? `<p>${esc(r.thesis)}</p><p class="expiry">confidence ${fmtPct(r.confidence, 0)} · max loss ${esc(r.max_expected_loss)}</p>` : ""}
          <div class="expiry">expires in ${Math.floor(a.seconds_remaining / 60)}m ${a.seconds_remaining % 60}s — then it is auto-rejected</div>
          <div class="actions">
            <button class="btn btn-approve" data-approve="${esc(o.id)}">Approve</button>
            <button class="btn btn-reject" data-reject="${esc(o.id)}">Reject</button>
          </div></div>`;
      }).join("")
    : '<div class="empty">nothing awaiting approval</div>';
  el.querySelectorAll("[data-approve]").forEach((b) =>
    b.addEventListener("click", () =>
      postJSON(`/api/approvals/${b.dataset.approve}`, { approve: true })
        .then(() => { toast("Approved — revalidating and submitting", "good"); refreshApprovals(); })));
  el.querySelectorAll("[data-reject]").forEach((b) =>
    b.addEventListener("click", () =>
      postJSON(`/api/approvals/${b.dataset.reject}`, { approve: false })
        .then(() => { toast("Rejected", "warn"); refreshApprovals(); })));
}

/* ================= performance / execution / AI usage ================= */

const kvRow = (k, v, cls) =>
  `<div class="kv"><span class="k">${k}</span><span class="v ${cls || ""}">${v}</span></div>`;

async function refreshPerformance() {
  const perf = await getJSON("/api/performance");
  $("#perf-portfolio").innerHTML =
    kvRow("Total return", fmtPct(perf.total_return), perf.total_return > 0 ? "ok" : perf.total_return < 0 ? "bad" : "") +
    kvRow("CAGR", fmtPct(perf.cagr)) +
    kvRow("Sharpe", perf.sharpe) +
    kvRow("Sortino", perf.sortino) +
    kvRow("Calmar", perf.calmar) +
    kvRow("Max drawdown", fmtPct(perf.max_drawdown)) +
    kvRow("Volatility (ann.)", fmtPct(perf.annualized_volatility));
  $("#perf-trades").innerHTML =
    kvRow("Closed trades", perf.trades) +
    kvRow("Win rate", fmtPct(perf.win_rate, 1)) +
    kvRow("Profit factor", perf.profit_factor) +
    kvRow("Avg win / loss", `${fmtUsd(perf.avg_win)} / ${fmtUsd(perf.avg_loss)}`) +
    kvRow("Expectancy / trade", fmtUsd(perf.expectancy)) +
    kvRow("Avg holding", (perf.avg_holding_days ?? 0) + "d") +
    kvRow("Realized P&L", fmtUsd(perf.realized_pnl), perf.realized_pnl > 0 ? "ok" : perf.realized_pnl < 0 ? "bad" : "");

  const strategies = Object.entries(perf.by_strategy || {});
  $("#strategy-table tbody").innerHTML = strategies.length
    ? strategies.map(([name, s]) =>
        `<tr><td><span class="sym">${esc(name)}</span></td>
         <td class="num">${s.trades}</td>
         <td class="num">${fmtPct(s.win_rate, 1)}</td>
         <td class="num ${s.realized_pnl > 0 ? "pos" : s.realized_pnl < 0 ? "neg" : ""}">${fmtUsd(s.realized_pnl)}</td>
         <td class="num">${s.avg_holding_days}d</td></tr>`).join("")
    : '<tr><td colspan="5" class="empty">no closed trades yet</td></tr>';

  const months = Object.entries(perf.monthly_returns || {});
  const maxAbs = Math.max(0.0001, ...months.map(([, v]) => Math.abs(v)));
  $("#monthly-returns").innerHTML = months.length
    ? months.map(([month, v]) => {
        const w = Math.min(Math.abs(v) / maxAbs * 50, 50);
        return `<div class="month-row"><span>${esc(month)}</span>
          <span class="bar-track"><span class="bar ${v >= 0 ? "pos" : "neg"}" style="width:${w.toFixed(1)}%"></span></span>
          <span class="pct ${v > 0 ? "pos" : v < 0 ? "neg" : ""}">${fmtPct(v)}</span></div>`;
      }).join("")
    : '<div class="empty">appears after the first full month</div>';
}

async function refreshExecution() {
  const e = await getJSON("/api/execution");
  const bySide = e.avg_slippage_bps_by_side || {};
  $("#execution").innerHTML =
    kvRow("Fill rate", e.fill_rate != null ? fmtPct(e.fill_rate, 1) : "—") +
    kvRow("Fills measured", `${e.orders_measured} / ${e.orders_filled}`) +
    kvRow("Avg slippage", e.avg_slippage_bps != null ? e.avg_slippage_bps + " bps" : "—",
          e.avg_slippage_bps > 5 ? "bad" : e.avg_slippage_bps != null && e.avg_slippage_bps <= 0 ? "ok" : "") +
    kvRow("Median / worst", `${e.median_slippage_bps ?? "—"} / ${e.worst_slippage_bps ?? "—"} bps`) +
    kvRow("Buys / sells", `${bySide.buy ?? "—"} / ${bySide.sell ?? "—"} bps`) +
    kvRow("Avg time to fill", e.avg_seconds_to_fill != null ? e.avg_seconds_to_fill + "s" : "—") +
    (e.worst_fill ? kvRow("Worst fill", `${esc(e.worst_fill.symbol)} ${esc(e.worst_fill.side)} · ${e.worst_fill.slippage_bps} bps`) : "");
}

async function refreshAiUsage() {
  const s = lastStatus || (await getJSON("/api/status"));
  const u = s.ai_usage || {};
  const budget = u.monthly_budget_usd;
  $("#ai-usage").innerHTML =
    kvRow("Review cycles", u.cycles ?? 0) +
    kvRow("API calls", u.api_calls ?? 0) +
    kvRow("Input tokens", fmtNum(u.input_tokens ?? 0)) +
    kvRow("Output tokens", fmtNum(u.output_tokens ?? 0)) +
    kvRow("Cache read", fmtNum(u.cache_read_tokens ?? 0)) +
    kvRow("Cache write", fmtNum(u.cache_write_tokens ?? 0)) +
    kvRow("Estimated spend", `$${u.month_cost_usd ?? 0}` + (budget ? ` / $${budget}` : ""),
          budget && u.month_cost_usd >= budget ? "bad" : "");
}

/* ================= audit ================= */

async function refreshAudit() {
  const data = await getJSON("/api/audit?limit=100");
  const rows = data.audit || [];
  $("#audit-table tbody").innerHTML = rows.length
    ? rows.map((r) =>
        `<tr><td>${r.at ? new Date(r.at).toLocaleString() : "—"}</td>
         <td>${esc(r.actor)}</td>
         <td><span class="sym">${esc(r.action)}</span></td>
         <td><small>${esc(JSON.stringify(r.details || {}).slice(0, 160))}</small></td></tr>`).join("")
    : '<tr><td colspan="4" class="empty">no audit records</td></tr>';
}

/* ================= live events ================= */

function connectWebsocket() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws` + (AUTH_TOKEN ? `?token=${encodeURIComponent(AUTH_TOKEN)}` : ""));
  const feed = $("#events");
  ws.onopen = () => { $("#conn-dot").className = "conn-dot ok"; $("#conn-label").textContent = "live"; };
  ws.onmessage = (msg) => {
    let evt;
    try { evt = JSON.parse(msg.data); } catch { return; }
    const row = document.createElement("div");
    row.className = "evt";
    const summary = typeof evt.payload === "object" && evt.payload
      ? esc(JSON.stringify(evt.payload).slice(0, 240)) : esc(evt.payload);
    row.innerHTML = `<time>${new Date().toLocaleTimeString()}</time><span class="topic">${esc(evt.topic)}</span><span class="body">${summary}</span>`;
    feed.prepend(row);
    while (feed.children.length > 200) feed.lastChild.remove();

    if (evt.topic === "order.filled") toast(`Filled: ${evt.payload?.order?.symbol ?? "order"}`, "good");
    if (evt.topic === "order.rejected") toast(`Rejected: ${evt.payload?.reason ?? ""}`.slice(0, 120), "bad");
    if (evt.topic === "ai.approval_requested") toast("New trade awaiting your approval", "warn");
    if (evt.topic === "risk.violation") toast(`Risk: ${evt.payload?.rule ?? "violation"}`, "warn");
    if (evt.topic === "circuit.opened") toast("Circuit breaker OPEN — trading halted", "bad");

    if (["ai.approval_requested", "order.filled", "order.rejected"].includes(evt.topic)) {
      refreshApprovals().catch(() => {});
      if (!$("#orders-table").closest(".view").hidden) refreshOrders().catch(() => {});
    }
    if (evt.topic === "portfolio.synced" && !$("#t-equity").closest(".view").hidden) {
      refreshPortfolio().catch(() => {});
    }
  };
  ws.onclose = () => {
    $("#conn-dot").className = "conn-dot bad"; $("#conn-label").textContent = "reconnecting…";
    setTimeout(connectWebsocket, 3000);
  };
  setInterval(() => { if (ws.readyState === 1) ws.send("ping"); }, 25000);
}

/* ================= trade ticket ================= */

async function ticketQuote() {
  const symbol = $("#tk-symbol").value.trim().toUpperCase();
  if (!symbol) return;
  const box = $("#tk-quote");
  box.textContent = "fetching live quote…";
  try {
    const q = await getJSON(`/api/quote/${encodeURIComponent(symbol)}`);
    box.innerHTML = `<strong>${esc(symbol)}</strong> · bid ${fmtUsd(q.bid)} / ask ${fmtUsd(q.ask)}` +
      ` · last ${fmtUsd(q.last)} <small>(${esc(q.source)}, ${esc(q.freshness)})</small>`;
    if (!$("#tk-limit").value && q.last) $("#tk-limit").value = Number(q.last).toFixed(2);
  } catch (e) {
    box.textContent = "no fresh quote available — the platform will not trade without one";
  }
}

async function submitTicket(evt) {
  evt.preventDefault();
  const btn = $("#tk-submit");
  btn.disabled = true;
  try {
    const body = {
      symbol: $("#tk-symbol").value.trim(),
      side: $("#tk-side").value,
      order_type: $("#tk-type").value,
      quantity: $("#tk-qty").value.trim(),
      limit_price: $("#tk-limit").value.trim() || null,
      stop_price: $("#tk-stop").value.trim() || null,
      time_in_force: $("#tk-tif").value,
      extended_hours: $("#tk-ext").checked,
    };
    const res = await postJSON("/api/trade", body);
    const o = res.order;
    if (res.accepted) {
      toast(`Order ${o.status}: ${o.side} ${o.quantity} ${o.symbol}`, "good");
      $("#ticket").reset();
      $("#tk-quote").textContent = "quote appears when you enter a symbol";
    } else {
      toast(`Refused: ${o.status_reason || o.status}`.slice(0, 140), "bad");
    }
    refreshOrders().catch(() => {});
    refreshPortfolio().catch(() => {});
  } catch (e) {
    toast("Order failed: " + e.message, "bad");
  } finally {
    btn.disabled = false;
  }
}

$("#ticket").addEventListener("submit", submitTicket);
$("#tk-refresh").addEventListener("click", ticketQuote);
$("#tk-symbol").addEventListener("change", ticketQuote);

/* ================= algorithm workshop ================= */

let algoCache = [];
let selectedAlgo = null;

async function refreshAlgorithms() {
  const data = await getJSON("/api/algorithms");
  algoCache = data.algorithms || [];
  const active = algoCache.filter((a) => a.status === "active").length;
  $("#algo-count").textContent = algoCache.length
    ? `${algoCache.length} saved · ${active} active` : "";
  $("#algo-list").innerHTML = algoCache.length
    ? algoCache.map((a) => `
      <div class="algo-row ${selectedAlgo === a.id ? "selected" : ""}" data-algo="${esc(a.id)}">
        <div class="head"><span class="name">${esc(a.name)}</span>
          ${a.created_by === "claude" ? '<span class="chip claude">claude</span>' : ""}
          ${a.sleeve_pct ? `<span class="chip">sleeve ${(a.sleeve_pct * 100).toFixed(0)}%</span>` : ""}
          <span class="chip ${esc(a.status)}">${esc(a.status)}</span></div>
        ${a.description ? `<div class="desc">${esc(a.description)}</div>` : ""}
        <div class="meta">updated ${new Date(a.updated_at).toLocaleString()}</div>
      </div>`).join("")
    : '<div class="empty">nothing saved yet — write one in the editor, ask Claude during a cycle, or import below</div>';
  $("#algo-list").querySelectorAll("[data-algo]").forEach((row) =>
    row.addEventListener("click", () => selectAlgo(row.dataset.algo)));
}

function selectAlgo(id) {
  const a = algoCache.find((x) => x.id === id);
  if (!a) return;
  selectedAlgo = id;
  $("#al-name").value = a.name;
  $("#al-name").disabled = true; // names are identity; edit source/desc instead
  $("#al-desc").value = a.description || "";
  $("#al-symbols").value = (a.symbols || []).join(", ");
  $("#al-sleeve").value = a.sleeve_pct ? (a.sleeve_pct * 100).toFixed(0) : "";
  $("#al-source").value = a.source;
  const notes = $("#al-notes");
  notes.hidden = !a.review_notes;
  notes.textContent = a.review_notes || "";
  $("#al-save").hidden = true;
  $("#al-test").hidden = false;
  $("#al-bt-controls").hidden = false;
  $("#al-testout").hidden = true;
  $("#al-update").hidden = false;
  $("#al-activate").hidden = a.status === "active";
  $("#al-deactivate").hidden = a.status !== "active";
  $("#al-delete").hidden = false;
  $("#al-new").hidden = false;
  refreshAlgorithms();
}

function clearAlgoEditor() {
  selectedAlgo = null;
  $("#al-name").value = ""; $("#al-name").disabled = false;
  $("#al-desc").value = ""; $("#al-symbols").value = ""; $("#al-source").value = "";
  $("#al-sleeve").value = "";
  $("#al-notes").hidden = true;
  $("#al-save").hidden = false;
  $("#al-testout").hidden = true;
  ["#al-update", "#al-activate", "#al-deactivate", "#al-delete", "#al-new", "#al-test", "#al-bt-controls"]
    .forEach((s) => ($(s).hidden = true));
  refreshAlgorithms();
}

function algoBody() {
  return {
    name: $("#al-name").value.trim(),
    description: $("#al-desc").value.trim(),
    symbols: $("#al-symbols").value.split(",").map((s) => s.trim()).filter(Boolean),
    source: $("#al-source").value,
    sleeve_pct: ($("#al-sleeve").value.trim() ? Number($("#al-sleeve").value) / 100 : 0),
  };
}

async function algoAction(fn, okMessage) {
  try {
    await fn();
    if (okMessage) toast(okMessage, "good");
    await refreshAlgorithms();
  } catch (e) {
    toast(String(e.message).slice(0, 200), "bad");
  }
}

$("#al-save").addEventListener("click", () =>
  algoAction(async () => {
    const res = await postJSON("/api/algorithms", algoBody());
    selectAlgo(res.algorithm.id);
    selectedAlgo = res.algorithm.id;
  }, "Draft saved"));
$("#al-update").addEventListener("click", () =>
  algoAction(() => putJSON(`/api/algorithms/${selectedAlgo}`, algoBody()), "Saved"));
$("#al-activate").addEventListener("click", () =>
  algoAction(async () => {
    await postJSON(`/api/algorithms/${selectedAlgo}/activate`);
    selectAlgo(selectedAlgo);
  }, "Activated — scanning next cycle"));
$("#al-deactivate").addEventListener("click", () =>
  algoAction(async () => {
    await postJSON(`/api/algorithms/${selectedAlgo}/deactivate`);
    selectAlgo(selectedAlgo);
  }, "Deactivated"));
$("#al-delete").addEventListener("click", () =>
  algoAction(async () => {
    await fetch(`/api/algorithms/${selectedAlgo}`, { method: "DELETE", headers: authHeaders() });
    clearAlgoEditor();
  }, "Deleted"));
$("#al-new").addEventListener("click", clearAlgoEditor);
$("#al-bt-period").addEventListener("change", () => {
  const custom = $("#al-bt-period").value === "custom";
  $("#al-bt-start").hidden = !custom;
  $("#al-bt-end").hidden = !custom;
});

$("#al-backtest").addEventListener("click", async () => {
  const btn = $("#al-backtest");
  const period = $("#al-bt-period").value;
  const body = { period };
  if (period === "custom") {
    if (!$("#al-bt-start").value) { toast("Custom range needs a start date", "warn"); return; }
    body.start = $("#al-bt-start").value;
    if ($("#al-bt-end").value) body.end = $("#al-bt-end").value;
  }
  btn.disabled = true;
  btn.textContent = "Backtesting…";
  try {
    const r = await postJSON(`/api/algorithms/${selectedAlgo}/backtest`, body);
    const out = $("#al-testout");
    out.hidden = false;
    const yearRows = Object.entries(r.annual_returns || {})
      .map(([y, v]) => `<div class="kv"><span class="k">${esc(y)}</span>
        <span class="v ${v > 0 ? "ok" : v < 0 ? "bad" : ""}">${fmtPct(v)}</span></div>`).join("");
    out.innerHTML = `<div class="rv-block"><h3>Backtest — ${esc(r.start)} → ${esc(r.end)} (${r.days_tested} days, real history)</h3>
      <div class="kv"><span class="k">Total return</span><span class="v ${r.total_return > 0 ? "ok" : "bad"}">${fmtPct(r.total_return)}</span></div>
      <div class="kv"><span class="k">CAGR</span><span class="v">${fmtPct(r.cagr)}</span></div>
      <div class="kv"><span class="k">Sharpe</span><span class="v">${r.sharpe}</span></div>
      <div class="kv"><span class="k">Max drawdown</span><span class="v">${fmtPct(r.max_drawdown)}</span></div>
      <div class="kv"><span class="k">Rebalances / orders</span><span class="v">${r.rebalances} / ${r.orders_simulated}</span></div>
      <div class="kv"><span class="k">Final equity</span><span class="v">${fmtUsd(r.final_equity)}</span></div>
      ${yearRows}
      ${(r.symbols_skipped_no_history || []).length ? `<p class="meter-note">no history available: ${esc(r.symbols_skipped_no_history.join(", "))}</p>` : ""}
      <p class="meter-note">${esc(r.note)}</p></div>`;
  } catch (e) {
    toast("Backtest failed: " + String(e.message).slice(0, 200), "bad");
  } finally {
    btn.disabled = false;
    btn.textContent = "Backtest";
  }
});

$("#al-test").addEventListener("click", async () => {
  const btn = $("#al-test");
  btn.disabled = true;
  btn.textContent = "Running…";
  try {
    const res = await postJSON(`/api/algorithms/${selectedAlgo}/test`);
    const out = $("#al-testout");
    out.hidden = false;
    out.innerHTML = `<div class="rv-block"><h3>Dry run — ${res.count} signal${res.count === 1 ? "" : "s"} (live data, nothing traded)</h3>` +
      ((res.signals || []).length
        ? `<pre>${esc(JSON.stringify(res.signals, null, 2))}</pre>`
        : "<p>No signals under current market conditions — that can be correct behavior.</p>") +
      "</div>";
  } catch (e) {
    toast("Test run failed: " + String(e.message).slice(0, 160), "bad");
  } finally {
    btn.disabled = false;
    btn.textContent = "Test run";
  }
});

async function putJSON(url, body) {
  const res = await fetch(url, {
    method: "PUT",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify(body || {}),
  });
  if (!res.ok) {
    const detail = await res.json().catch(() => ({}));
    throw new Error(detail.detail || `${url}: ${res.status}`);
  }
  return res.json();
}

$("#rv-go").addEventListener("click", async () => {
  const source = $("#rv-source").value.trim();
  if (!source) { toast("Paste an algorithm first", "warn"); return; }
  const btn = $("#rv-go"), status = $("#rv-status");
  btn.disabled = true;
  status.textContent = "Claude is reviewing — typically 15–60 seconds…";
  try {
    const res = await postJSON("/api/algorithms/review",
      { source, instructions: $("#rv-instructions").value.trim() });
    renderReview(res.review);
  } catch (e) {
    toast("Review failed: " + e.message, "bad");
    status.textContent = "";
  } finally {
    btn.disabled = false;
    status.textContent = "";
  }
});

function renderReview(r) {
  const box = $("#rv-result");
  box.hidden = false;
  const list = (items) => (items || []).map((x) => `<li>${esc(x)}</li>`).join("") || "<li>none noted</li>";
  box.innerHTML = `
    <div class="rv-block"><h3>Analysis</h3><p>${esc(r.analysis)}</p></div>
    <div class="rv-block"><h3>Risks</h3><ul>${list(r.risks)}</ul></div>
    <div class="rv-block"><h3>Recommendations</h3><ul>${list(r.recommendations)}</ul></div>
    ${r.conversion_notes ? `<div class="rv-block"><h3>Conversion notes</h3><p>${esc(r.conversion_notes)}</p></div>` : ""}
    ${(r.validation_errors || []).length
      ? `<div class="rv-block"><h3>Validation</h3><ul>${list(r.validation_errors)}</ul></div>` : ""}
    ${r.convertible && r.poseidon_source
      ? `<div class="rv-block"><h3>Poseidon implementation</h3><pre>${esc(r.poseidon_source)}</pre>
         <div class="ticket-actions"><button class="btn" id="rv-load">Load into editor</button></div></div>`
      : '<div class="rv-block"><h3>Verdict</h3><p>Claude judged this not convertible to a screener — see the analysis above.</p></div>'}`;
  const load = $("#rv-load");
  if (load) load.addEventListener("click", () => {
    clearAlgoEditor();
    $("#al-name").value = r.suggested_name || "";
    $("#al-desc").value = r.suggested_description || "";
    $("#al-source").value = r.poseidon_source || "";
    window.scrollTo({ top: 0, behavior: "smooth" });
    toast("Loaded — review, then save as draft", "good");
  });
}

/* ================= actions & boot ================= */

$("#mode-seg").addEventListener("click", (e) => {
  const btn = e.target.closest("button[data-mode]");
  if (!btn) return;
  const mode = btn.dataset.mode;
  const go = () =>
    postJSON("/api/mode", { mode })
      .then(() => { toast("Mode: " + mode, mode === "autonomous" ? "warn" : "good"); refreshStatus(); })
      .catch((err) => { toast("Mode change failed: " + err.message, "bad"); refreshStatus(); });
  go();
});

$("#btn-halt").addEventListener("click", () => { $("#halt-modal").hidden = false; });
$("#halt-cancel").addEventListener("click", () => { $("#halt-modal").hidden = true; });
$("#halt-modal").addEventListener("click", (e) => { if (e.target.id === "halt-modal") $("#halt-modal").hidden = true; });
$("#halt-confirm").addEventListener("click", () => {
  $("#halt-modal").hidden = true;
  postJSON("/api/halt", { reason: "manual halt from dashboard" })
    .then(() => { toast("Trading halted", "bad"); refreshStatus(); });
});
$("#btn-resume").addEventListener("click", () =>
  postJSON("/api/resume").then(() => { toast("Trading resumed", "good"); refreshStatus(); }));
$("#btn-cycle").addEventListener("click", () =>
  postJSON("/api/cycle").then(() => toast("Review cycle started")));

window.addEventListener("resize", drawEquity);
route();
refreshApprovals().catch(() => {});
connectWebsocket();
setInterval(() => { VIEWS[currentView()].refresh(); refreshApprovals().catch(() => {}); }, 30000);
