// Shared helpers for the Aangan pages. Plain JS, no build step.
// All dynamic text is inserted with textContent, never innerHTML.

const OUTCOMES = {
  QUALIFIED: ["Qualified", "green"],
  DECLINE_SCOPE: ["Declined · scope", "red"],
  DECLINE_AREA: ["Declined · outside area", "red"],
  DECLINE_TIMELINE: ["Declined · timeline", "yellow"],
  BUDGET_MISALIGNED: ["Budget below scope", "yellow"],
  HUMAN_REVIEW: ["Human review", "blue"],
  ESCALATE_URGENT: ["Urgent escalation", "red"],
  NOT_APPLICABLE: ["Not an enquiry", ""],
};
const FLAG_LABELS = {
  TIGHT: "Tight timeline", VIP: "VIP", RECOVERY: "Follow-up", DECISION_NOTE: "Decision-maker note",
  PRICING_ASKED: "Asked price", RESUMED_CALL: "Resumed call", PRICE_LEAK: "Price mentioned",
};

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) if (c !== null && c !== undefined) node.append(c);
  return node;
}

function outcomeBadge(outcome) {
  const [label, tone] = OUTCOMES[outcome] || ["In progress", ""];
  return el("span", { class: `badge ${tone}`, text: label });
}
function flagBadge(flag) {
  const tone = flag === "PRICE_LEAK" ? "red" : flag === "VIP" ? "blue" : "";
  return el("span", { class: `badge ${tone}`, text: FLAG_LABELS[flag] || flag });
}

const nf = new Intl.NumberFormat("en-IN");
function fmt(v, unit = "") {
  if (v === null || v === undefined) return "—";
  return (typeof v === "number" ? nf.format(v) : v) + unit;
}
function fmtDuration(seconds) {
  if (seconds === null || seconds === undefined) return "—";
  const s = Math.round(seconds), m = Math.floor(s / 60);
  return m ? `${m} min ${s % 60} s` : `${s} s`;
}
function fmtWhen(iso) {
  if (!iso) return "—";
  return new Date(iso).toLocaleString("en-IN", { day: "numeric", month: "short", hour: "numeric", minute: "2-digit" });
}

async function getJSON(path) {
  const resp = await fetch(new URL(path, location.origin));
  if (!resp.ok) throw new Error(`the server answered ${resp.status}`);
  return resp.json();
}

function revealOnScroll(root = document) {
  const items = root.querySelectorAll(".reveal:not(.in)");
  if (!("IntersectionObserver" in window)) { items.forEach((n) => n.classList.add("in")); return; }
  const io = new IntersectionObserver((entries) => {
    for (const e of entries) if (e.isIntersecting) { e.target.classList.add("in"); io.unobserve(e.target); }
  }, { rootMargin: "0px 0px -8% 0px" });
  items.forEach((n) => io.observe(n));
}

function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}
