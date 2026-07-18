/* Poseidon debug console — READ-ONLY observability.
   Self-contained IIFE loaded BEFORE app.js so the fetch wrapper is installed
   before app.js resolves window.fetch. Off by default; when off every capture
   path is one boolean test. Never logs secrets: request headers are never
   captured, and URLs + request/response bodies run the SECRET_KEY redaction
   walk before they enter the ring buffer. Matches app.js vanilla-JS idioms
   (no framework, no build step). */

(function () {
  "use strict";

  /* ---- shared config ---- */
  const MAX = 500;
  // LOAD-BEARING: any object key or query-param name matching this is redacted.
  const SECRET_KEY =
    /(token|secret|passphrase|password|api[_-]?key|app[_-]?key|app[_-]?secret|refresh[_-]?token|credential|authorization)/i;

  const DBG = { on: false, buf: [], seq: 0 };

  /* ---- redaction (pure, top-level, unit-testable) ---- */

  function isPlain(x) {
    if (Array.isArray(x)) return true;
    if (x === null || typeof x !== "object") return false;
    const proto = Object.getPrototypeOf(x);
    return proto === Object.prototype || proto === null;
  }

  function byteLen(x) {
    if (typeof x === "string") return x.length;
    if (x && typeof x.size === "number") return x.size; // Blob
    if (x && typeof x.byteLength === "number") return x.byteLength; // ArrayBuffer / TypedArray
    return 0;
  }

  // Deep-walk objects/arrays; replace the value of any secret-named key.
  function walk(obj) {
    if (Array.isArray(obj)) return obj.map(walk);
    if (obj && typeof obj === "object" && isPlain(obj)) {
      const out = {};
      for (const k of Object.keys(obj)) {
        out[k] = SECRET_KEY.test(k) ? "REDACTED" : walk(obj[k]);
      }
      return out;
    }
    return obj;
  }

  // Accepts a raw request body value (string/Blob/…) OR an already-parsed
  // object (e.g. a JSON-parsed response). Returns a redacted plain object, or
  // a bare "[body N bytes]" placeholder for anything non-JSON — never raw.
  function redactBody(x) {
    if (x == null) return x; // no body
    if (typeof x === "object") {
      if (isPlain(x)) return walk(x);
      return "[body " + byteLen(x) + " bytes]"; // Blob / FormData / ArrayBuffer …
    }
    if (typeof x === "string") {
      try {
        const parsed = JSON.parse(x);
        if (parsed && typeof parsed === "object") return walk(parsed);
      } catch { /* not JSON */ }
      return "[body " + x.length + " bytes]";
    }
    return "[body " + byteLen(x) + " bytes]";
  }

  // Replace the value of any secret-named query param with REDACTED.
  function redactUrl(u) {
    const s = String(u);
    const qi = s.indexOf("?");
    if (qi === -1) return s;
    let tail = s.slice(qi + 1);
    let hash = "";
    const hi = tail.indexOf("#");
    if (hi !== -1) { hash = tail.slice(hi); tail = tail.slice(0, hi); }
    const parts = tail.split("&").map((pair) => {
      const eq = pair.indexOf("=");
      const key = eq === -1 ? pair : pair.slice(0, eq);
      if (eq !== -1 && SECRET_KEY.test(key)) return key + "=REDACTED";
      return pair;
    });
    return s.slice(0, qi) + "?" + parts.join("&") + hash;
  }

  // Bounded, redacted summary of a fetch response. Reads a clone so the app
  // still consumes the original body normally. Parses the FULL text for
  // redaction (so a secret can never survive via a truncated-but-unparseable
  // slice) and only then bounds the output to ~2000 chars.
  async function summarize(res) {
    try {
      const text = await res.clone().text();
      try {
        const parsed = JSON.parse(text);
        if (parsed && typeof parsed === "object") {
          const s = JSON.stringify(redactBody(parsed));
          return s.length > 2000 ? s.slice(0, 2000) + "…[truncated]" : s;
        }
      } catch { /* not JSON — fall through to bounded raw text */ }
      return text.length > 2000 ? text.slice(0, 2000) + "…[truncated]" : text;
    } catch (err) {
      return "[unreadable body: " + String(err) + "]";
    }
  }

  // Deep-clone an event payload and cap its stringified size (~2000 chars).
  function bound(payload) {
    try {
      const clone = JSON.parse(JSON.stringify(payload));
      const s = JSON.stringify(clone);
      if (s.length > 2000) return s.slice(0, 2000) + "…[truncated]";
      return clone;
    } catch {
      return null;
    }
  }

  /* ---- ring buffer ---- */

  function push(e) {
    e.seq = DBG.seq++;
    e.ts = new Date().toISOString();
    DBG.buf.push(e);
    if (DBG.buf.length > MAX) DBG.buf.shift();
    scheduleRender();
  }

  /* ============================================================
     Everything below touches the DOM / globals and runs only in
     the browser. Under node (task-8 unit test) we just export the
     pure functions above.
     ============================================================ */

  let panel = null;
  let toggleBtn = null;
  let list = null;
  let countEl = null;
  let _raf = 0;
  let _fetch = null;

  const esc = (s) =>
    String(s ?? "").replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

  /* ---- source (a): delegated capture click listener ---- */

  function viewOf(t) {
    const v = t.closest(".view[data-view]");
    if (v) return v.dataset.view;
    if (t.closest(".modal, .modal-backdrop, [role='dialog']")) return "modal";
    if (t.closest(".sidebar")) return "sidebar";
    if (t.closest(".topbar")) return "topbar";
    return "";
  }

  function labelOf(t) {
    let label = (t.textContent || "").trim().replace(/\s+/g, " ");
    if (!label) label = t.getAttribute("aria-label") || t.getAttribute("title") || "";
    return label.slice(0, 80);
  }

  function onClick(e) {
    if (!DBG.on) return; // OFF ⇒ one boolean test, nothing else
    const t = e.target.closest(
      "button,a,[data-close-pos],[data-cancel],[data-approve],[data-reject],[data-algo],[data-broker]"
    );
    if (!t) return;
    if (t.closest("#dbg-panel, #dbg-toggle")) return; // don't log the console's own UI
    push({ kind: "click", id: t.id || "", label: labelOf(t), view: viewOf(t) });
    // never reads form values — avoids capturing typed secrets
  }

  /* ---- source (b): single fetch chokepoint ---- */

  async function dbgFetch(input, init) {
    if (!DBG.on) return _fetch(input, init);
    const method = (init?.method || "GET").toUpperCase();
    const url = redactUrl(String(input?.url || input));
    const reqBody = redactBody(init?.body); // NEVER touch init.headers
    const t0 = performance.now();
    try {
      const res = await _fetch(input, init);
      const resSummary = await summarize(res); // res.clone(); bounded + redacted
      push({
        kind: "api", method, url, reqBody,
        status: res.status, ok: res.ok,
        durationMs: Math.round(performance.now() - t0), resSummary,
      });
      return res; // original returned untouched
    } catch (err) {
      push({
        kind: "api", method, url, reqBody,
        status: 0, ok: false,
        durationMs: Math.round(performance.now() - t0), error: String(err),
      });
      throw err; // never swallow
    }
  }

  /* ---- source (c): /ws tap (no second socket) ---- */

  function debugTap(evt) {
    if (!DBG.on) return;
    push({ kind: "event", topic: evt.topic, payload: bound(evt.payload) });
  }

  /* ---- render ---- */

  function scheduleRender() {
    if (!DBG.on || !panel || panel.hidden) return; // only while panel is open
    if (_raf) return;
    _raf = requestAnimationFrame(() => { _raf = 0; render(); });
  }

  function summaryLine(e) {
    if (e.kind === "click") {
      const who = e.label || e.id || "(click)";
      return esc(who) + (e.view ? " · " + esc(e.view) : "");
    }
    if (e.kind === "api") {
      return (
        esc(e.method) + " " + esc(e.url) + " → " +
        (e.ok ? "" : "✖ ") + esc(String(e.status)) + " (" + e.durationMs + "ms)"
      );
    }
    return esc(e.topic || "");
  }

  function rowHtml(e) {
    const t = esc(e.ts.slice(11, 23)); // HH:MM:SS.mmm
    return (
      '<div class="dbg-row" data-seq="' + e.seq + '">' +
      '<div class="dbg-line"><time>' + t + "</time>" +
      '<span class="dbg-badge dbg-' + e.kind + '">' + e.kind + "</span>" +
      '<span class="dbg-sum">' + summaryLine(e) + "</span></div>" +
      '<pre class="dbg-detail" hidden>' + esc(JSON.stringify(e, null, 2)) + "</pre>" +
      "</div>"
    );
  }

  function render() {
    if (!list) return;
    list.innerHTML = [...DBG.buf].reverse().map(rowHtml).join("");
    if (countEl) countEl.textContent = DBG.buf.length + "/" + MAX;
  }

  /* ---- export ---- */

  function exportJson() {
    const blob = new Blob([JSON.stringify(DBG.buf, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const now = new Date();
    const pad = (n) => String(n).padStart(2, "0");
    const stamp =
      "" + now.getFullYear() + pad(now.getMonth() + 1) + pad(now.getDate()) +
      "-" + pad(now.getHours()) + pad(now.getMinutes()) + pad(now.getSeconds());
    const a = document.createElement("a");
    a.href = url;
    a.download = "poseidon-debug-" + stamp + ".json";
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  }

  /* ---- toggle / persistence ---- */

  function setOn(v) {
    DBG.on = v;
    try { localStorage.setItem("poseidon.debug", v ? "1" : "0"); } catch { /* ignore */ }
    if (panel) panel.hidden = !v;
    if (toggleBtn) toggleBtn.classList.toggle("on", v);
    if (v) render();
  }

  /* ---- DOM build ---- */

  function build() {
    toggleBtn = document.createElement("button");
    toggleBtn.id = "dbg-toggle";
    toggleBtn.type = "button";
    toggleBtn.textContent = "🐞";
    toggleBtn.title = "Toggle debug console";
    toggleBtn.setAttribute("aria-label", "Toggle debug console");
    if (DBG.on) toggleBtn.classList.add("on");
    toggleBtn.addEventListener("click", () => setOn(!DBG.on));

    panel = document.createElement("div");
    panel.id = "dbg-panel";
    panel.hidden = !DBG.on;
    panel.innerHTML =
      '<div id="dbg-head">' +
      '<span id="dbg-title">Debug console</span>' +
      '<span id="dbg-count"></span>' +
      '<button type="button" id="dbg-export">Export JSON</button>' +
      '<button type="button" id="dbg-clear">Clear</button>' +
      '<button type="button" id="dbg-close" aria-label="Close">✕</button>' +
      "</div>" +
      '<div id="dbg-list"></div>';

    document.body.appendChild(toggleBtn);
    document.body.appendChild(panel);

    list = panel.querySelector("#dbg-list");
    countEl = panel.querySelector("#dbg-count");
    panel.querySelector("#dbg-export").addEventListener("click", exportJson);
    panel.querySelector("#dbg-clear").addEventListener("click", () => {
      DBG.buf.length = 0;
      render();
    });
    panel.querySelector("#dbg-close").addEventListener("click", () => setOn(false));
    // expand/collapse a row's full JSON
    list.addEventListener("click", (e) => {
      const line = e.target.closest(".dbg-line");
      if (!line) return;
      const pre = line.parentElement.querySelector(".dbg-detail");
      if (pre) pre.hidden = !pre.hidden;
    });

    render();
  }

  /* ---- install ---- */

  function install() {
    // resolve initial on-state: ?debug=1 forces on, else localStorage
    let forced = false;
    try {
      forced = new URLSearchParams(location.search).get("debug") === "1";
    } catch { /* ignore */ }
    let stored = false;
    try {
      stored = localStorage.getItem("poseidon.debug") === "1";
    } catch { /* ignore */ }
    DBG.on = forced || stored;

    // install the fetch chokepoint before app.js runs route()/connectWebsocket()
    _fetch = window.fetch.bind(window);
    window.fetch = dbgFetch;

    // the /ws tap: app.js calls window.__debugTap(evt) inside ws.onmessage
    window.__debugTap = debugTap;

    // delegated capture-phase click listener (sees clicks even past stopPropagation)
    document.addEventListener("click", onClick, true);

    // expose for the documented manual check / debugging
    window.__DBG = DBG;

    const boot = () => build();
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", boot, { once: true });
    } else {
      boot();
    }
  }

  const isBrowser = typeof window !== "undefined" && typeof document !== "undefined";
  if (isBrowser) install();

  // Node/one-off unit test hook (task 8) — pure functions only.
  if (typeof module !== "undefined" && module.exports) {
    module.exports = { redactUrl, redactBody, summarize, bound, walk, SECRET_KEY };
  }
})();
