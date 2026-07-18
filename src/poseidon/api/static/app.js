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

async function errDetail(res, url) {
  // Surface the server's explanation (FastAPI puts it in .detail) instead of
  // a bare status code — "limit orders need a limit_price" beats "422".
  try {
    const data = await res.json();
    if (data && data.detail) return String(data.detail);
  } catch { /* not JSON */ }
  return `${url}: ${res.status}`;
}
async function getJSON(url) {
  const res = await fetch(url, { headers: authHeaders() });
  if (!res.ok) throw new Error(await errDetail(res, url));
  return res.json();
}
async function postJSON(url, body) {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify(body || {}),
  });
  if (!res.ok) throw new Error(await errDetail(res, url));
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
  overview:    { title: "Overview",    refresh: () => Promise.allSettled([refreshStatus(), refreshPortfolio(), refreshEquity(), refreshRiskMetrics(), refreshAlgorithms()]) },
  portfolio:   { title: "Portfolio",   refresh: () => Promise.allSettled([refreshStatus(), refreshPortfolio(), refreshOrders(), refreshExitPlans()]) },
  algorithms:  { title: "Algorithms",  refresh: () => Promise.allSettled([refreshStatus(), refreshAlgorithms()]) },
  ai:          { title: "AI Desk",     refresh: () => Promise.allSettled([refreshStatus(), refreshApprovals(), refreshDecisions(), refreshAiUsage(), refreshChat()]) },
  account:     { title: "Account",     refresh: () => Promise.allSettled([refreshStatus(), refreshAccount()]) },
  dryrun:      { title: "Dry Run",     refresh: () => refreshDryRun() },
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

  const cycleBtn = $("#btn-cycle");
  cycleBtn.disabled = !!s.cycle_running;
  cycleBtn.textContent = s.cycle_running ? "Cycle running…" : "Run cycle";

  renderRiskMeters(s.risk, "#risk-meters");
  renderRiskMeters(s.risk, "#risk-meters-2");
  renderSystem(s.health.components, s.broker);
  renderProviders(s.providers);
  renderRuntime(s);
  updateAutomationTile();
}

function updateAutomationTile() {
  const tile = $("#t-auto");
  if (!tile || !lastStatus) return;
  const active = algoCache.filter((a) => a.status === "active").length;
  const mode = lastStatus.mode;
  tile.textContent = mode === "autonomous" ? "AUTO" : mode;
  tile.className = "tile-value tile-value-sm" + (mode === "autonomous" && active ? " neg" : "");
  $("#t-auto-sub").textContent =
    active ? `${active} algorithm${active === 1 ? "" : "s"} active` +
             (mode === "autonomous" ? " — executing" :
              mode === "approval" ? " — trades need your OK" : " — signals only")
           : "no algorithms active";
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

// Scale a stat tile's value down so a long number (e.g. a $42M paper balance)
// fits its tile instead of spilling past the box. Resets to the CSS base size,
// then, if the single-line text is wider than the tile, shrinks the font
// proportionally to fit — capped at the base and floored for legibility.
// Depends on white-space:nowrap (style.css) so scrollWidth reflects the real
// text width rather than a wrapped height.
function fitTileValue(el) {
  if (!el) return;
  const base = el.classList.contains("tile-value-sm") ? 19 : 25;
  el.style.fontSize = base + "px";
  const avail = el.clientWidth;
  if (avail > 0 && el.scrollWidth > avail) {
    el.style.fontSize = Math.max(13, Math.floor((base * avail) / el.scrollWidth)) + "px";
  }
}
function fitTiles() {
  $$(".tile-value").forEach(fitTileValue);
}
let _fitFrame = 0;
window.addEventListener("resize", () => {
  cancelAnimationFrame(_fitFrame);
  _fitFrame = requestAnimationFrame(fitTiles);
});

let lastPortfolio = null;

async function refreshPortfolio() {
  const p = await getJSON("/api/portfolio");
  lastPortfolio = p;
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
  fitTiles(); // scale any large balances (e.g. a $42M paper account) to fit their tiles

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
        <td class="num ${upl > 0 ? "pos" : upl < 0 ? "neg" : ""}">${fmtUsd(upl)}</td>
        <td><button class="btn btn-sm" data-close-pos="${esc(pos.symbol)}"
              data-close-qty="${esc(pos.quantity)}" title="Prefill the ticket to exit this position">Close</button></td></tr>`;
      }).join("")
    : '<tr><td colspan="7" class="empty">no positions</td></tr>';
  tbody.querySelectorAll("[data-close-pos]").forEach((btn) =>
    btn.addEventListener("click", () =>
      prefillCloseTicket(btn.dataset.closePos, btn.dataset.closeQty)));

  renderAllocation(positions, account.equity);
  renderFills(p.recent_fills || []);
  updateNotional();
}

function prefillCloseTicket(symbol, qty) {
  // Exiting a long = sell; covering a short = buy. Same risk-gated pipeline.
  const numQty = Number(qty);
  $("#tk-symbol").value = symbol;
  $("#tk-qty").value = String(Math.abs(numQty));
  $("#tk-side").value = numQty >= 0 ? "sell" : "buy";
  $("#tk-type").value = "limit";
  $("#tk-limit").value = ""; // let the fresh quote seed it
  ticketQuote();
  $("#tk-submit").scrollIntoView({ behavior: "smooth", block: "center" });
  toast(`Ticket prefilled to close ${symbol} — review and submit`, "warn");
}

function renderFills(fills) {
  const el = $("#fills");
  if (!el) return;
  el.innerHTML = fills.length
    ? [...fills].reverse().map((f) =>
        `<div class="kv"><span class="k">${f.filled_at ? new Date(f.filled_at).toLocaleTimeString() : "—"}
           <span class="sym">${esc(f.symbol)}</span></span>
         <span class="v">${esc(f.side)} ${esc(f.quantity)} @ ${fmtUsd(f.price)}</span></div>`).join("")
    : '<div class="empty">no fills yet</div>';
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

let workingOrderIds = [];

async function refreshOrders() {
  const data = await getJSON("/api/orders");
  const tbody = $("#orders-table tbody");
  const orders = data.orders || [];
  workingOrderIds = orders
    .filter((o) => ["submitted", "accepted", "partially_filled"].includes(o.status))
    .map((o) => o.id);
  $("#orders-hint").textContent = workingOrderIds.length
    ? `${workingOrderIds.length} working · most recent first` : "most recent first";
  $("#orders-cancel-all").hidden = workingOrderIds.length < 2;
  tbody.innerHTML = orders.length
    ? orders.map((o) => {
        const open = ["submitted", "accepted", "partially_filled"].includes(o.status);
        const slip = o.slippage_bps != null ? ` · ${Number(o.slippage_bps).toFixed(1)}bps` : "";
        const filledQty = Number(o.filled_quantity) || 0;
        const fill = filledQty > 0
          ? `${o.filled_quantity}/${o.quantity}` +
            (o.avg_fill_price ? ` @ ${fmtUsd(o.avg_fill_price)}` : "")
          : "—";
        return `<tr>
        <td>${o.created_at ? new Date(o.created_at).toLocaleTimeString() : "—"}</td>
        <td><span class="sym">${esc(o.symbol)}</span></td>
        <td>${esc(o.side)}</td>
        <td class="num">${esc(o.quantity)}</td>
        <td class="num">${o.limit_price ? fmtUsd(o.limit_price) : "mkt"}</td>
        <td class="num">${esc(fill)}</td>
        <td><span class="status-tag ${statusClass(o.status)}">${esc(o.status)}</span>${slip}</td>
        <td>${esc(o.status_reason || "")}</td>
        <td>${open ? `<button class="btn btn-sm" data-cancel="${esc(o.id)}">Cancel</button>` : ""}</td></tr>`;
      }).join("")
    : '<tr><td colspan="9" class="empty">no orders yet</td></tr>';
  tbody.querySelectorAll("[data-cancel]").forEach((btn) =>
    btn.addEventListener("click", () =>
      postJSON(`/api/orders/${btn.dataset.cancel}/cancel`)
        .then(() => { toast("Cancel requested", "warn"); refreshOrders(); })
        .catch((e) => toast("Cancel failed: " + e.message, "bad"))));
}

$("#orders-cancel-all").addEventListener("click", async () => {
  const ids = [...workingOrderIds];
  if (!ids.length) return;
  if (!window.confirm(`Cancel all ${ids.length} working orders?`)) return;
  const results = await Promise.allSettled(
    ids.map((id) => postJSON(`/api/orders/${id}/cancel`)));
  const failed = results.filter((r) => r.status === "rejected").length;
  toast(failed ? `Canceled ${ids.length - failed}/${ids.length} — ${failed} failed`
               : `Canceled all ${ids.length} working orders`, failed ? "bad" : "warn");
  refreshOrders().catch(() => {});
});

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
          <div class="expiry" data-deadline="${Date.now() + a.seconds_remaining * 1000}"></div>
          <div class="actions">
            <button class="btn btn-approve" data-approve="${esc(o.id)}">Approve</button>
            <button class="btn btn-reject" data-reject="${esc(o.id)}">Reject</button>
          </div></div>`;
      }).join("")
    : '<div class="empty">nothing awaiting approval</div>';
  tickApprovalCountdowns();
  el.querySelectorAll("[data-approve]").forEach((b) =>
    b.addEventListener("click", () =>
      postJSON(`/api/approvals/${b.dataset.approve}`, { approve: true })
        .then(() => { toast("Approved — revalidating and submitting", "good"); refreshApprovals(); })
        .catch((e) => toast("Approve failed: " + e.message, "bad"))));
  el.querySelectorAll("[data-reject]").forEach((b) =>
    b.addEventListener("click", () =>
      postJSON(`/api/approvals/${b.dataset.reject}`, { approve: false })
        .then(() => { toast("Rejected", "warn"); refreshApprovals(); })
        .catch((e) => toast("Reject failed: " + e.message, "bad"))));
}

function tickApprovalCountdowns() {
  // A hard deadline must visibly move: tick every second, not every 30s poll.
  $$("#approvals .expiry[data-deadline]").forEach((el) => {
    const left = Math.floor((Number(el.dataset.deadline) - Date.now()) / 1000);
    if (left > 0) {
      el.textContent = `expires in ${Math.floor(left / 60)}m ${left % 60}s — then it is auto-rejected`;
    } else {
      el.textContent = "expired — auto-rejected";
      el.closest(".approval")?.querySelectorAll("button").forEach((b) => (b.disabled = true));
    }
  });
}
setInterval(tickApprovalCountdowns, 1000);

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
         <td><small>${esc(JSON.stringify(r.payload || {}).slice(0, 160))}</small></td></tr>`).join("")
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
    window.__debugTap && window.__debugTap(evt);
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
    if (evt.topic === "risk.circuit_opened") toast("Circuit breaker OPEN — trading halted", "bad");
    if (evt.topic === "ai.decision") {
      const trades = (evt.payload?.trades || []).length;
      toast(`Cycle complete: ${evt.payload?.action ?? "decision"}` +
            (trades ? ` — ${trades} trade${trades === 1 ? "" : "s"} proposed` : ""), "good");
      refreshStatus().catch(() => {});
      if (!$("#decisions").closest(".view").hidden) refreshDecisions().catch(() => {});
    }

    if (["ai.approval_requested", "order.filled", "order.rejected"].includes(evt.topic)) {
      refreshApprovals().catch(() => {});
      if (!$("#orders-table").closest(".view").hidden) refreshOrders().catch(() => {});
    }
    if (evt.topic === "portfolio.synced" && !$("#t-equity").closest(".view").hidden) {
      refreshPortfolio().catch(() => {});
    }
  };
  const keepalive = setInterval(() => { if (ws.readyState === 1) ws.send("ping"); }, 25000);
  ws.onclose = () => {
    clearInterval(keepalive);  // else every reconnect leaks another ping timer
    $("#conn-dot").className = "conn-dot bad"; $("#conn-label").textContent = "reconnecting…";
    setTimeout(connectWebsocket, 3000);
  };
}

/* ================= trade ticket ================= */

let tkLastPrice = null; // last/reference price for the notional readout

function updateNotional() {
  const el = $("#tk-notional");
  if (!el) return;
  const qty = Number($("#tk-qty").value);
  const px = Number($("#tk-limit").value) || tkLastPrice;
  if (!Number.isFinite(qty) || qty <= 0 || !px) { el.hidden = true; return; }
  const notional = qty * px;
  const bp = lastPortfolio && lastPortfolio.account
    ? Number(lastPortfolio.account.buying_power) : null;
  const over = bp != null && $("#tk-side").value === "buy" && notional > bp;
  el.hidden = false;
  el.innerHTML = `Est. notional <strong>${fmtUsd(notional)}</strong>` +
    (bp != null ? ` · buying power ${fmtUsd(bp)}` : "") +
    (over ? ' · <span class="neg">exceeds buying power</span>' : "");
}
["#tk-qty", "#tk-limit"].forEach((s) => $(s).addEventListener("input", updateNotional));
$("#tk-side").addEventListener("change", updateNotional);

async function ticketQuote() {
  const symbol = $("#tk-symbol").value.trim().toUpperCase();
  if (!symbol) return;
  const box = $("#tk-quote");
  box.textContent = "fetching live quote…";
  try {
    const q = await getJSON(`/api/quote/${encodeURIComponent(symbol)}`);
    tkLastPrice = q.last ? Number(q.last) : null;
    if (q.reference) {
      // Market not in regular session: this is the last real print, labeled.
      // Display only — it must not seed the order form.
      const at = q.as_of ? new Date(q.as_of).toLocaleString() : "unknown time";
      box.innerHTML = `<strong>${esc(symbol)}</strong> · last ${fmtUsd(q.last)}` +
        (q.bid || q.ask ? ` · bid ${fmtUsd(q.bid)} / ask ${fmtUsd(q.ask)}` : "") +
        ` <small>(${esc(q.source)})</small><br><small>market ${esc(q.session || "closed")} — ` +
        `last print ${esc(at)}. Reference only: orders always require a fresh quote.</small>`;
    } else {
      box.innerHTML = `<strong>${esc(symbol)}</strong> · bid ${fmtUsd(q.bid)} / ask ${fmtUsd(q.ask)}` +
        ` · last ${fmtUsd(q.last)} <small>(${esc(q.source)}, ${esc(q.freshness)})</small>`;
      if (!$("#tk-limit").value && q.last) $("#tk-limit").value = Number(q.last).toFixed(2);
    }
    updateNotional();
  } catch (e) {
    tkLastPrice = null;
    box.textContent = "no quote available — " + String(e.message).slice(0, 160);
  }
}

async function submitTicket(evt) {
  evt.preventDefault();
  // Catch the obvious misses before the server does, with focus put right.
  const type = $("#tk-type").value;
  if ((type === "limit" || type === "stop_limit") && !$("#tk-limit").value.trim()) {
    toast("Limit orders need a limit price", "warn");
    $("#tk-limit").focus();
    return;
  }
  if ((type === "stop" || type === "stop_limit") && !$("#tk-stop").value.trim()) {
    toast("Stop orders need a stop price", "warn");
    $("#tk-stop").focus();
    return;
  }
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
          ${a.status === "active" && lastStatus && lastStatus.mode === "autonomous"
            ? '<span class="chip auto">auto-investing</span>'
            : `<span class="chip ${esc(a.status)}">${esc(a.status)}</span>`}</div>
        ${a.description ? `<div class="desc">${esc(a.description)}</div>` : ""}
        <div class="meta">updated ${new Date(a.updated_at).toLocaleString()}</div>
      </div>`).join("")
    : '<div class="empty">nothing saved yet — write one in the editor, ask Claude during a cycle, or import below</div>';
  $("#algo-list").querySelectorAll("[data-algo]").forEach((row) =>
    row.addEventListener("click", () => selectAlgo(row.dataset.algo)));
  updateAutomationTile(); // status and algorithms load concurrently — render on whichever lands last
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
  renderAutoInvestState(a);
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
  ["#al-update", "#al-activate", "#al-deactivate", "#al-delete", "#al-new", "#al-test",
   "#al-bt-controls", "#al-autoinvest", "#al-autostate"]
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
    // Refresh the cache first: selectAlgo() looks the id up in algoCache,
    // and the just-created draft isn't there until we reload.
    await refreshAlgorithms();
    selectAlgo(res.algorithm.id);
  }, "Draft saved"));
$("#al-update").addEventListener("click", () =>
  algoAction(() => putJSON(`/api/algorithms/${selectedAlgo}`, algoBody()), "Saved"));
function renderAutoInvestState(a) {
  const mode = lastStatus ? lastStatus.mode : null;
  const state = $("#al-autostate");
  const btn = $("#al-autoinvest");
  state.hidden = false;
  if (a.status === "active" && mode === "autonomous") {
    btn.hidden = true;
    state.textContent = "Auto-investing is ON: this algorithm's signals feed every review cycle "
      + "and Claude executes trades within the risk limits. Stop / deactivate ends it.";
  } else if (a.status === "active" && mode === "approval") {
    btn.hidden = false;
    state.textContent = "Active — signals feed every cycle; proposed trades wait for your approval "
      + "(approval mode). Start auto-investing switches the platform to autonomous.";
  } else if (a.status === "active") {
    btn.hidden = false;
    state.textContent = "Active — signals only: research mode never trades. "
      + "Start auto-investing switches the platform to autonomous execution.";
  } else {
    btn.hidden = false;
    state.textContent = "Not scanning yet. Activate makes its signals feed review cycles; "
      + "Start auto-investing also puts the platform in autonomous mode so trades execute.";
  }
}

$("#al-autoinvest").addEventListener("click", async () => {
  const a = algoCache.find((x) => x.id === selectedAlgo);
  if (!a) return;
  const mode = lastStatus ? lastStatus.mode : "research";
  const broker = (lastStatus && lastStatus.broker) || {};
  const liveNote = broker.paper === false
    ? `\n\nACTIVE BROKER IS LIVE (${broker.name}) — trades will use real money.` : "";
  if (mode !== "autonomous" && !window.confirm(
      `Start auto-investing with "${a.name}"?\n\n` +
      "This activates the algorithm AND switches Poseidon to AUTONOMOUS mode: Claude will " +
      "execute trades from its signals within every risk limit, without asking first." + liveNote +
      "\n\n(Prefer confirming each trade yourself? Cancel here, Activate the algorithm, and " +
      "use Approval mode in the header instead.)")) return;
  const btn = $("#al-autoinvest");
  btn.disabled = true;
  try {
    if (a.status !== "active") await postJSON(`/api/algorithms/${a.id}/activate`);
    if (mode !== "autonomous") await postJSON("/api/mode", { mode: "autonomous" });
    toast(`Auto-investing: ${a.name} is live`, "warn");
    await refreshStatus();
    await refreshAlgorithms();
    selectAlgo(a.id);
  } catch (e) {
    toast("Could not start auto-investing: " + String(e.message).slice(0, 200), "bad");
  } finally {
    btn.disabled = false;
  }
});

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
    const res = await fetch(`/api/algorithms/${selectedAlgo}`, { method: "DELETE", headers: authHeaders() });
    if (!res.ok) {
      const detail = await res.json().catch(() => ({}));
      throw new Error(detail.detail || `delete failed: ${res.status}`);
    }
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

/* ================= AI chat ================= */

let chatPending = false;

function chatBubble(role, content, meta) {
  const div = document.createElement("div");
  div.className = "chat-msg " + role;
  div.textContent = content;
  if (meta) {
    const m = document.createElement("span");
    m.className = "chat-meta";
    m.textContent = meta;
    div.appendChild(m);
  }
  return div;
}

async function refreshChat() {
  if (chatPending) return; // don't clobber the optimistic bubbles mid-send
  const data = await getJSON("/api/chat?limit=200");
  if (chatPending) return; // a send started while the GET was in flight
  const logEl = $("#chat-log");
  const msgs = data.messages || [];
  // Skip the rebuild when nothing changed — the 30s auto-refresh must not
  // destroy the reader's scroll position or text selection.
  const sig = msgs.length + ":" + (msgs.length ? msgs[msgs.length - 1].at : "");
  if (logEl.dataset.sig === sig) return;
  const atBottom = logEl.scrollHeight - logEl.scrollTop - logEl.clientHeight < 8;
  logEl.dataset.sig = sig;
  logEl.innerHTML = msgs.length ? "" :
    '<div class="empty">Ask about your portfolio, a symbol, risk, strategy ideas, or how Poseidon itself works.</div>';
  for (const m of msgs) {
    logEl.appendChild(chatBubble(m.role, m.content, m.at ? new Date(m.at).toLocaleTimeString() : ""));
  }
  if (atBottom || !logEl.dataset.hadContent) logEl.scrollTop = logEl.scrollHeight;
  logEl.dataset.hadContent = "1";
}

async function sendChat(evt) {
  evt.preventDefault();
  if (chatPending) return;
  const input = $("#chat-input");
  const text = input.value.trim();
  if (!text) return;
  chatPending = true;
  $("#chat-send").disabled = true;
  input.value = "";
  const logEl = $("#chat-log");
  const empty = logEl.querySelector(".empty");
  if (empty) empty.remove();
  logEl.appendChild(chatBubble("user", text));
  const wait = chatBubble("assistant", "Claude is looking at live data…");
  wait.classList.add("pending");
  logEl.appendChild(wait);
  logEl.scrollTop = logEl.scrollHeight;
  try {
    const res = await postJSON("/api/chat", { message: text });
    wait.remove();
    const tools = (res.tool_calls || []).length
      ? "checked: " + [...new Set(res.tool_calls)].join(", ") : "";
    logEl.appendChild(chatBubble("assistant", res.reply || "(no reply)", tools));
  } catch (e) {
    wait.remove();
    logEl.appendChild(chatBubble("assistant", "Error: " + String(e.message).slice(0, 300)));
  } finally {
    chatPending = false;
    $("#chat-send").disabled = false;
    logEl.dataset.sig = ""; // bubbles were added optimistically; resync next refresh
    logEl.scrollTop = logEl.scrollHeight;
    // refreshAiUsage reads the cached status — refetch it first so the
    // usage card shows the tokens this chat turn just spent.
    refreshStatus().then(refreshAiUsage).catch(() => {});
  }
}

$("#chat-form").addEventListener("submit", sendChat);
$("#chat-clear").addEventListener("click", async () => {
  if (chatPending) return;
  try {
    const res = await fetch("/api/chat", { method: "DELETE", headers: authHeaders() });
    if (!res.ok) throw new Error(await errDetail(res, "/api/chat"));
    await refreshChat();
    toast("Conversation cleared", "good");
  } catch (e) {
    toast("Clear failed: " + e.message, "bad");
  }
});

/* ================= account / broker connect ================= */

let brokerCatalog = [];
let selectedBroker = null;

/* ================= Dry Run ================= */

async function refreshDryRun() {
  let s;
  try { s = await getJSON("/api/dryrun"); }
  catch (e) { $("#dryrun-summary").textContent = "Could not load dry-run state: " + e.message; return; }
  renderDryRun(s);
}

function renderDryRun(s) {
  const on = (ok) => ok ? "✅" : "▫️";
  const brokerOk = s.broker_is_paper;
  const algosOk = s.active_algo_count > 0;
  const autoOk = s.mode === "autonomous";
  $("#dryrun-steps").innerHTML = `
    <div class="form-row dryrun-step"><span>${on(brokerOk)} Broker = Paper simulator</span>
      <button type="button" class="btn btn-ghost" id="dryrun-broker-toggle" ${brokerOk ? "disabled" : ""}>
        ${brokerOk ? "Active" : "Switch to paper"}</button>
      <small>${brokerOk ? "The safe simulator is active." : "Currently: " + esc(s.active_broker) + ". Switch to paper to dry-run safely."}</small></div>
    <div class="form-row dryrun-step"><span>${on(algosOk)} Built-in algorithms active (${s.active_algo_count} on)</span>
      <button type="button" class="btn btn-ghost" id="dryrun-algos-activate" ${s.bundled_draft_count ? "" : "disabled"}>
        ${s.bundled_draft_count ? "Activate the " + s.bundled_draft_count + " built-in algorithm(s)" : "None pending"}</button>
      <small>Their signals feed each review cycle alongside Claude.</small></div>
    <div class="form-row dryrun-step"><span id="dryrun-mode-state">${on(autoOk)} Autonomous mode (${esc(s.mode)})</span>
      <button type="button" class="btn btn-ghost" id="dryrun-mode-toggle" ${brokerOk ? "" : "disabled"}>
        ${autoOk ? "On" : "Turn on"}</button>
      <small>${brokerOk ? "Safe on paper — Claude executes its own trades." : "Switch to paper first."}</small></div>`;
  const m = s.market;
  $("#dryrun-market").textContent = m.is_open
    ? "Market open — paper trades can execute now."
    : `Market closed — the dry run will start trading at the next open (${m.opens_hint}).`;
  $("#dryrun-summary").textContent = (brokerOk && algosOk && autoOk)
    ? "✅ Dry run active — Claude and your algorithms are trading the paper account."
    : "Turn on all three steps above to start the dry run.";
  $("#dryrun-broker-toggle")?.addEventListener("click", dryrunSwitchToPaper);
  $("#dryrun-algos-activate")?.addEventListener("click", () => dryrunActivateStarters(s));
  $("#dryrun-mode-toggle")?.addEventListener("click", () => dryrunSetMode(autoOk ? "research" : "autonomous"));
}

async function dryrunSwitchToPaper() {
  try { await postJSON("/api/brokers/connect", { name: "paper", paper: true }); toast("Switched to the paper simulator", "good"); }
  catch (e) { toast("Could not switch to paper: " + e.message, "bad"); }
  refreshDryRun();
}

async function dryrunActivateStarters(s) {
  const starters = (s.algorithms || []).filter((a) => a.bundled && a.status === "draft");
  for (const a of starters) {
    try { await postJSON(`/api/algorithms/${a.id}/activate`, {}); }
    catch (e) { toast(`Could not activate ${a.name}: ${e.message}`, "bad"); }
  }
  toast(`Activated ${starters.length} built-in algorithm(s)`, "good");
  refreshDryRun();
}

async function dryrunSetMode(mode) {
  try { await postJSON("/api/mode", { mode }); toast("Mode: " + mode, mode === "autonomous" ? "warn" : "good"); }
  catch (e) { toast("Mode change failed: " + e.message, "bad"); }
  refreshDryRun();
}

$("#dryrun-run-now").addEventListener("click", () =>
  postJSON("/api/cycle").then(() => toast("Review cycle started"))
    .catch((e) => toast("Review cycle failed: " + e.message, "bad")));
$("#dryrun-stop").addEventListener("click", () => dryrunSetMode("research"));

async function refreshAccount() {
  const data = await getJSON("/api/brokers");
  brokerCatalog = data.brokers || [];
  const cur = data.current || {};
  $("#acct-sync-hint").textContent = cur.synced_at
    ? "synced " + new Date(cur.synced_at).toLocaleTimeString() : "not synced yet";
  $("#acct-current").innerHTML =
    `<div class="kv"><span class="k">Broker</span><span class="v">${esc(cur.display_name || "—")}
       <span class="acct-badge ${cur.paper ? "paper" : "live"}">${cur.paper ? "paper" : "LIVE"}</span></span></div>` +
    `<div class="kv"><span class="k">Status</span>
       <span class="v ${cur.connected ? "ok" : "bad"}">${cur.connected ? "connected" : "disconnected"}</span></div>` +
    `<div class="kv"><span class="k">Account</span><span class="v">${esc(cur.account_id || "—")}</span></div>` +
    `<div class="kv"><span class="k">Equity</span><span class="v">${fmtUsd(cur.equity)}</span></div>` +
    `<div class="kv"><span class="k">Cash</span><span class="v">${fmtUsd(cur.cash)}</span></div>` +
    `<div class="kv"><span class="k">Buying power</span><span class="v">${fmtUsd(cur.buying_power)}</span></div>` +
    `<div class="kv"><span class="k">Operating mode</span><span class="v">${esc(data.mode || "")}</span></div>`;

  const connectable = brokerCatalog.filter((b) => b.connectable);
  $("#broker-list").innerHTML = connectable.map((b) => `
    <button type="button" class="broker-tile ${selectedBroker === b.name ? "selected" : ""}" data-broker="${esc(b.name)}">
      <span class="name">${esc(b.display_name)}${b.is_current ? " ✓" : ""}</span>
      <span class="sub">${b.paper_choice === "live_only" ? "live only"
        : b.paper_choice === "always" ? "simulation" : "paper or live"}${b.credential_saved ? " · key saved" : ""}${b.cost_note ? " · fees may apply" : ""}</span>
    </button>`).join("");
  $("#broker-list").querySelectorAll("[data-broker]").forEach((el) =>
    el.addEventListener("click", () => selectBroker(el.dataset.broker)));
}

$("#acct-sync").addEventListener("click", async () => {
  const btn = $("#acct-sync");
  btn.disabled = true;
  btn.textContent = "Syncing…";
  try {
    await postJSON("/api/sync");
    toast("Portfolio synced", "good");
    refreshAccount().catch(() => {});
    refreshPortfolio().catch(() => {});
  } catch (e) {
    toast("Sync failed: " + String(e.message).slice(0, 200), "bad");
  } finally {
    btn.disabled = false;
    btn.textContent = "Sync now";
  }
});

function selectBroker(name) {
  const b = brokerCatalog.find((x) => x.name === name);
  if (!b) return;
  selectedBroker = name;
  $$("#broker-list .broker-tile").forEach((t) =>
    t.classList.toggle("selected", t.dataset.broker === name));
  const form = $("#broker-form");
  form.hidden = false;
  $("#bf-title").textContent = "Connect " + b.display_name;
  $("#bf-notes").textContent = b.notes || "";
  const costEl = $("#bf-cost");
  costEl.hidden = !b.cost_note;
  costEl.textContent = b.cost_note ? "Cost note: " + b.cost_note : "";
  $("#bf-result").hidden = true;
  $("#bf-options").innerHTML = (b.option_fields || []).map((f) => `
    <div class="form-row"><label class="wide">${esc(f.label)}${f.optional ? " <small>(optional)</small>" : ""}
      <input data-opt="${esc(f.key)}" type="text" inputmode="decimal"
        placeholder="${esc(f.placeholder || "")}" autocomplete="off">
      ${f.help ? `<small>${esc(f.help)}</small>` : ""}</label></div>`).join("");
  const saved = b.credential_saved;
  $("#bf-fields").innerHTML = (b.fields || []).map((f) => `
    <div class="form-row"><label class="wide">${esc(f.label)}${f.optional ? " <small>(optional)</small>" : ""}
      <input data-cred="${esc(f.key)}" type="${f.secret ? "password" : "text"}"
        placeholder="${esc(f.placeholder || (saved ? "saved — leave blank to reuse" : ""))}" autocomplete="off">
      ${f.help ? `<small>${esc(f.help)}</small>` : ""}</label></div>`).join("")
    + (saved && (b.fields || []).length
        ? '<p class="meter-note">A credential for this broker is already in the vault — leave every field blank to reuse it, or fill them all to replace it.</p>'
        : "");
  // In-app OAuth login (Schwab): the "Log in with…" button opens the
  // brokerage login page and the returned code is exchanged for a refresh
  // token that fills the credential fields above.
  const oauth = $("#bf-oauth");
  oauth.hidden = !b.oauth;
  if (b.oauth) {
    $("#bf-oauth-login").textContent = "Log in with " + b.display_name + " →";
    $("#bf-oauth-redirect").value = "";
  }
  const paperRow = $("#bf-paper-row");
  const paperBox = $("#bf-paper");
  if (b.paper_choice === "toggle") { paperRow.hidden = false; paperBox.checked = true; paperBox.disabled = false; }
  else if (b.paper_choice === "always") { paperRow.hidden = false; paperBox.checked = true; paperBox.disabled = true; }
  else { paperRow.hidden = true; paperBox.checked = false; paperBox.disabled = true; }
  updateLiveWarning();
}

function _credInput(key) {
  return $(`#bf-fields [data-cred="${key}"]`);
}

// Step 1 of OAuth: open the brokerage login screen for the entered app key.
$("#bf-oauth-login").addEventListener("click", async () => {
  const appKey = (_credInput("app_key")?.value || "").trim();
  if (!appKey) { toast("Enter the app key first", "bad"); return; }
  try {
    const res = await postJSON("/api/brokers/schwab/authorize-url", { app_key: appKey });
    window.open(res.url, "_blank", "noopener");
    toast("Opened the login page in a new tab", "good");
  } catch (e) { toast("Could not build the login URL: " + e.message, "bad"); }
});

// Step 2 of OAuth: exchange the pasted redirect URL for a refresh token.
$("#bf-oauth-exchange").addEventListener("click", async () => {
  const appKey = (_credInput("app_key")?.value || "").trim();
  const appSecret = (_credInput("app_secret")?.value || "").trim();
  const pasted = ($("#bf-oauth-redirect").value || "").trim();
  if (!appKey || !appSecret || !pasted) {
    toast("Need the app key, app secret, and the pasted redirect URL", "bad"); return;
  }
  try {
    const res = await postJSON("/api/brokers/schwab/exchange",
      { app_key: appKey, app_secret: appSecret, redirect_response: pasted });
    const rt = _credInput("refresh_token"); if (rt) rt.value = res.refresh_token;
    const ah = _credInput("account_hash"); if (ah && res.account_hash) ah.value = res.account_hash;
    toast("Refresh token retrieved — you can Test connection now", "good");
  } catch (e) { toast("Login exchange failed: " + e.message, "bad"); }
});

function updateLiveWarning() {
  const b = brokerCatalog.find((x) => x.name === selectedBroker);
  if (!b) return;
  const paper = b.paper_choice === "live_only" ? false
    : b.paper_choice === "always" ? true : $("#bf-paper").checked;
  $("#bf-live-warning").hidden = paper;
}
$("#bf-paper").addEventListener("change", updateLiveWarning);

function brokerPayload() {
  const b = brokerCatalog.find((x) => x.name === selectedBroker);
  if (!b) throw new Error("pick a broker first");
  const creds = {};
  let any = false;
  $$("#bf-fields [data-cred]").forEach((inp) => {
    const v = inp.value.trim();
    if (v) { creds[inp.dataset.cred] = v; any = true; }
  });
  const required = (b.fields || []).filter((f) => !f.optional);
  if (any) {
    const missing = required.filter((f) => !creds[f.key]);
    if (missing.length) throw new Error("missing: " + missing.map((f) => f.label).join(", "));
  } else if (required.length && !b.credential_saved) {
    throw new Error("enter the credential fields first");
  }
  const paper = b.paper_choice === "live_only" ? false
    : b.paper_choice === "always" ? true : $("#bf-paper").checked;
  // A broker whose fields are all optional (IBKR) with nothing saved yet:
  // send an explicit empty credentials object so the server doesn't try a
  // vault lookup that cannot succeed.
  const sendCreds = any || (!required.length && !b.credential_saved);
  const options = {};
  $$("#bf-options [data-opt]").forEach((inp) => {
    const v = inp.value.trim();
    if (v) options[inp.dataset.opt] = v;
  });
  if (b.name === "paper" && options.starting_cash) {
    const cash = Number(options.starting_cash.replace(/[$,\s]/g, ""));
    if (!Number.isFinite(cash) || cash <= 0) throw new Error("starting cash must be a positive number");
    options.starting_cash = String(cash);
    options.reset = true; // setting an amount means: fresh simulator at that balance
  }
  return { name: b.name, paper,
           ...(sendCreds ? { credentials: creds } : {}),
           ...(Object.keys(options).length ? { options } : {}) };
}

$("#bf-test").addEventListener("click", async () => {
  let body;
  try { body = brokerPayload(); } catch (e) { toast(e.message, "warn"); return; }
  const btn = $("#bf-test");
  const out = $("#bf-result");
  btn.disabled = true;
  btn.textContent = "Testing…";
  try {
    const res = await postJSON("/api/brokers/test", body);
    out.hidden = false;
    out.innerHTML = res.ok
      ? `✓ Connected to <strong>${esc(res.account.display_name)}</strong> — account ${esc(res.account.account_id)},
         equity ${fmtUsd(res.account.equity)}, buying power ${fmtUsd(res.account.buying_power)}`
      : "✗ " + esc(res.error || "connection failed");
  } catch (e) {
    out.hidden = false;
    out.textContent = "✗ " + String(e.message).slice(0, 300);
  } finally {
    btn.disabled = false;
    btn.textContent = "Test connection";
  }
});

$("#broker-form").addEventListener("submit", async (evt) => {
  evt.preventDefault();
  let body;
  try { body = brokerPayload(); } catch (e) { toast(e.message, "warn"); return; }
  const b = brokerCatalog.find((x) => x.name === selectedBroker);
  if (!body.paper && !window.confirm(
      `Switch to your LIVE ${b.display_name} account?\n\nOrders (in approval/autonomous mode) will use real money. ` +
      (b.cost_note ? "\n\nCost note: " + b.cost_note + " " : "") +
      "The operating mode is not changed by connecting.")) return;
  if (body.options && body.options.reset && !window.confirm(
      `Reset the paper simulator to $${Number(body.options.starting_cash).toLocaleString()}?\n\n` +
      "Paper positions and history start over. No real account is affected.")) return;
  const btn = $("#bf-connect");
  btn.disabled = true;
  btn.textContent = "Connecting…";
  try {
    const res = await postJSON("/api/brokers/connect", body);
    toast(`Switched to ${res.broker.display_name}` + (res.broker.paper ? " (paper)" : " (LIVE)"),
          res.broker.paper ? "good" : "warn");
    if (res.broker.provider_note) toast(res.broker.provider_note, "good");
    $("#broker-form").hidden = true;
    selectedBroker = null;
    refreshAccount().catch(() => {});
    refreshStatus().catch(() => {});
    refreshPortfolio().catch(() => {});
  } catch (e) {
    toast("Connect failed: " + String(e.message).slice(0, 250), "bad");
  } finally {
    btn.disabled = false;
    btn.textContent = "Connect & switch";
  }
});

$("#bf-cancel").addEventListener("click", () => {
  $("#broker-form").hidden = true;
  selectedBroker = null;
  $$("#broker-list .broker-tile").forEach((t) => t.classList.remove("selected"));
});

/* ================= actions & boot ================= */

$("#mode-seg").addEventListener("click", (e) => {
  const btn = e.target.closest("button[data-mode]");
  if (!btn) return;
  const mode = btn.dataset.mode;
  const go = () =>
    postJSON("/api/mode", { mode })
      .then(() => { toast("Mode: " + mode, mode === "autonomous" ? "warn" : "good"); refreshStatus(); })
      .catch((err) => { toast("Mode change failed: " + err.message, "bad"); refreshStatus(); });
  // Entering autonomous means Claude trades real money without asking —
  // gate it behind a confirm, like auto-invest and live-broker connect.
  // Research/Approval switches stay one-click.
  if (mode === "autonomous" && (!lastStatus || lastStatus.mode !== "autonomous")) {
    const broker = (lastStatus && lastStatus.broker) || {};
    const liveNote = broker.paper === false
      ? `\n\nACTIVE BROKER IS LIVE (${broker.name}) — trades will use real money.` : "";
    if (!window.confirm(
        "Switch to AUTONOMOUS mode?\n\nClaude will execute trades from review cycles within " +
        "every risk limit, without asking first." + liveNote)) return;
  }
  go();
});

$("#btn-halt").addEventListener("click", () => { $("#halt-modal").hidden = false; });
$("#halt-cancel").addEventListener("click", () => { $("#halt-modal").hidden = true; });
$("#halt-modal").addEventListener("click", (e) => { if (e.target.id === "halt-modal") $("#halt-modal").hidden = true; });
$("#halt-confirm").addEventListener("click", () => {
  $("#halt-modal").hidden = true;
  // The emergency stop must never fail silently: surface a failed halt so the
  // operator doesn't believe trading is stopped while the circuit stays closed.
  postJSON("/api/halt", { reason: "manual halt from dashboard" })
    .then(() => { toast("Trading halted", "bad"); refreshStatus(); })
    .catch((e) => { toast("HALT FAILED: " + e.message, "bad"); refreshStatus(); });
});
$("#btn-resume").addEventListener("click", () =>
  postJSON("/api/resume")
    .then(() => { toast("Trading resumed", "good"); refreshStatus(); })
    .catch((e) => { toast("Resume failed: " + e.message, "bad"); refreshStatus(); }));
$("#btn-cycle").addEventListener("click", () =>
  postJSON("/api/cycle")
    .then(() => toast("Review cycle started"))
    .catch((e) => toast("Review cycle failed: " + e.message, "bad")));

window.addEventListener("resize", drawEquity);
route();
refreshApprovals().catch(() => {});
connectWebsocket();
setInterval(() => { VIEWS[currentView()].refresh(); refreshApprovals().catch(() => {}); }, 30000);
