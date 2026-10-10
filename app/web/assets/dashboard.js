// Dashboard: metrics, charts and recent calls. Public page; data comes from /dashboard/api/*.
(() => {
  const $ = (id) => document.getElementById(id);
  const startEl = $("start"), endEl = $("end"), demoEl = $("demo");
  const charts = {};

  try { demoEl.checked = localStorage.getItem("aangan-dash-demo") === "1"; } catch {}
  const today = new Date();
  const iso = (d) => new Date(d.getTime() - d.getTimezoneOffset() * 6e4).toISOString().slice(0, 10);
  endEl.value = iso(today);
  startEl.value = iso(new Date(today.getTime() - 30 * 864e5));

  function notice(text, isError) {
    $("notice").textContent = text || "";
    $("notice").className = "notice" + (isError ? " error" : "");
  }

  function kpi(label, value, sub, i) {
    return el("div", { class: "card kpi" },  // no fade: these redraw on every refresh
      el("p", { class: "eyebrow", text: label }),
      el("div", { class: "num", text: value }),
      el("div", { class: "sub", text: sub }));
  }

  function tiles(id, items) {
    $(id).replaceChildren(...items.map(([label, v, unit = "", warn = false]) => {
      const na = v === null || v === undefined;
      return el("div", { class: "card tile" },
        el("div", { class: `num${na ? " na" : ""}${warn && v ? " warn" : ""}`, text: na ? "No data yet" : fmt(v, unit) }),
        el("div", { class: "label", text: label }));
    }));
  }

  function bar(id, labels, data, horizontal = false) {
    if (!window.Chart) return;
    charts[id]?.destroy();
    const ink = cssVar("--ink-strong"), muted = cssVar("--muted"), line = cssVar("--line");
    charts[id] = new Chart($(id), {
      type: "bar",
      data: { labels, datasets: [{ data, backgroundColor: ink, borderRadius: 4, maxBarThickness: 44 }] },
      options: {
        indexAxis: horizontal ? "y" : "x", maintainAspectRatio: false, animation: { duration: 500 },
        plugins: { legend: { display: false }, tooltip: { displayColors: false } },
        scales: {
          x: { grid: { display: horizontal, color: line }, ticks: { color: muted, precision: 0 }, border: { display: false }, beginAtZero: true },
          y: { grid: { display: !horizontal, color: line }, ticks: { color: muted, precision: 0 }, border: { display: false }, beginAtZero: true },
        },
      },
    });
  }

  function callsTable(calls) {
    const body = $("calls");
    $("calls-count").textContent = calls.length ? `${calls.length} shown, newest first` : "";
    if (!calls.length) {
      body.replaceChildren(el("tr", { class: "empty-row" }, el("td", { colspan: 5, text: "No calls in this range yet." })));
      return;
    }
    body.replaceChildren(...calls.map((c) => {
      const flags = c.flags || [];
      const open = () => (location.href = `/dashboard/calls/${c.id}`);
      return el("tr", { tabindex: 0, onclick: open, onkeydown: (e) => e.key === "Enter" && open() },
        el("td", { text: fmtWhen(c.started_at) }),
        el("td", {}, outcomeBadge(c.outcome)),
        el("td", { class: "hide-sm" }, el("div", { class: "flags" }, flags.map(flagBadge))),
        el("td", { class: "hide-sm mono small", text: fmtDuration(c.duration_seconds) }),
        el("td", { class: "source", text: c.is_demo ? "Website demo" : "Phone" }));
    }));
  }

  async function load() {
    const q = new URLSearchParams({ start: startEl.value, end: endEl.value, include_demo: demoEl.checked });
    let m, calls;
    try {
      [m, { calls }] = await Promise.all([getJSON(`/dashboard/api/metrics?${q}`), getJSON(`/dashboard/api/calls?${q}`)]);
    } catch (err) {
      notice(`Couldn't load the numbers (${err.message}). Refresh the page to try again.`, true);
      return;
    }
    const f = m.funnel;
    notice(f.calls === 0
      ? (demoEl.checked ? "No calls in this date range yet." : "No phone calls in this date range yet. Switch on “Include website demo” to see conversations from the receptionist page.")
      : "");

    const rate = (a, b) => (b ? `${Math.round((100 * a) / b)}% of enquiries` : "—");
    $("kpis").replaceChildren(
      kpi("Calls", fmt(f.calls), `${fmt(f.enquiries)} were project enquiries`, 0),
      kpi("Qualified", fmt(f.qualified), rate(f.qualified, f.enquiries), 1),
      kpi("Consultations booked", fmt(f.booked), `${fmt(f.held)} held · ${fmt(f.won)} won`, 2),
      kpi("Cost per call", m.cost.per_call_inr === null ? "—" : `₹${fmt(m.cost.per_call_inr)}`, `₹${fmt(m.cost.monthly_inr)} a month`, 3),
    );

    bar("funnel", ["Calls", "Enquiries", "Qualified", "Booked", "Held", "Won"], [f.calls, f.enquiries, f.qualified, f.booked, f.held, f.won]);
    const d = m.declines;
    bar("declines", ["Scope", "Outside area", "Timeline", "Budget"],
      ["DECLINE_SCOPE", "DECLINE_AREA", "DECLINE_TIMELINE", "BUDGET_MISALIGNED"].map((k) => d[k] || 0), true);

    callsTable(calls);
    tiles("speed", [
      ["Answered within 5 min", m.speed.pct_answered_under_5_min, "%"],
      ["Median pickup", m.speed.median_pickup_seconds, " s"],
      ["After-hours share", m.speed.after_hours_share_pct, "%"],
      ["Median time to designer reply", m.speed.median_minutes_to_designer_ack, " min"],
    ]);
    const ql = m.quality;
    tiles("quality", [
      ["Sent to human review", ql.human_review_rate_pct, "%"],
      ["Urgent escalations", ql.escalations],
      ["Transferred to the studio", ql.transfers],
      ["Price mentioned by the AI", ql.price_leak_flags, "", true],
      ["Calls the AI didn't log", ql.unlogged_calls, "", true],
      [`Reviewers agreed (${ql.audits} reviewed)`, ql.audit_agreement_pct, "%"],
    ]);
    const c = m.cost;
    tiles("cost", [
      ["Cost this period", c.total_inr],
      ["Per booked consultation", c.per_booked_consultation_inr],
      ["Win rate", c.win_rate_pct, "%"],
      ["Average project value", c.avg_project_value_inr],
      ["Pipeline value", c.pipeline_value_inr],
    ]);
    revealOnScroll();
  }

  $("range").addEventListener("submit", (e) => { e.preventDefault(); load(); });
  demoEl.addEventListener("change", () => { try { localStorage.setItem("aangan-dash-demo", demoEl.checked ? "1" : "0"); } catch {} load(); });
  // Don't wait for window "load": that waits on fonts and the chart CDN. Chart.js is a
  // deferred script, so it is ready by DOMContentLoaded; if it failed, bar() just skips.
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", load);
  else load();
  revealOnScroll();
})();
