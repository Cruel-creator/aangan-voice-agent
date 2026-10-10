// Components map: draws the numbered connectors between step cards, fills the live status
// table, and reveals the flow in step order. Without JS the map is still fully readable.
(() => {
  const map = document.getElementById("map");
  const svg = document.getElementById("wires");
  const strip = document.getElementById("map-scroll");
  const hint = document.getElementById("scroll-hint");
  const steps = [...map.querySelectorAll(".step")];

  // from -> to, following the numbers
  const EDGES = [
    ["s1", "s2a"], ["s1", "s2b"], ["s2a", "s4a"], ["s3", "s4a"], ["s2b", "s4b"], ["s3", "s4b"],
    ["s4a", "s5"], ["s5", "s6"], ["s6", "s7"], ["s4b", "s7"], ["s7", "s8"], ["s7", "s9"], ["s8", "s10"], ["s9", "s10"],
  ];

  function box(el, origin) {
    const r = el.getBoundingClientRect();
    return { l: r.left - origin.left, t: r.top - origin.top, r: r.right - origin.left, b: r.bottom - origin.top,
      cx: r.left - origin.left + r.width / 2, cy: r.top - origin.top + r.height / 2 };
  }
  // Elbow connector: leave from the side facing the target, enter on the near side.
  function route(a, b) {
    if (b.l >= a.r - 4) { // target to the right
      const mx = a.r + Math.max(10, (b.l - a.r) / 2);
      return `M${a.r},${a.cy} H${mx} V${b.cy} H${b.l}`;
    }
    if (b.r <= a.l + 4) { // target to the left
      const mx = a.l - Math.max(10, (a.l - b.r) / 2);
      return `M${a.l},${a.cy} H${mx} V${b.cy} H${b.r}`;
    }
    // same column: straight down (or up)
    const down = b.t >= a.b;
    const x = Math.round((Math.max(a.l, b.l) + Math.min(a.r, b.r)) / 2);
    return down ? `M${x},${a.b} V${b.t}` : `M${x},${a.t} V${b.b}`;
  }
  function draw() {
    svg.querySelectorAll("path.wire").forEach((p) => p.remove());
    if (hint) hint.hidden = strip.scrollWidth <= strip.clientWidth + 2;
    const origin = map.getBoundingClientRect();
    svg.setAttribute("viewBox", `0 0 ${origin.width} ${origin.height}`);
    for (const [from, to] of EDGES) {
      const a = document.getElementById(from), b = document.getElementById(to);
      if (!a || !b) continue;
      const p = document.createElementNS("http://www.w3.org/2000/svg", "path");
      p.setAttribute("class", "wire");
      p.setAttribute("d", route(box(a, origin), box(b, origin)));
      p.setAttribute("marker-end", "url(#arrow)");
      svg.appendChild(p);
    }
  }
  let raf = 0;
  const redraw = () => { cancelAnimationFrame(raf); raf = requestAnimationFrame(draw); };
  new ResizeObserver(redraw).observe(map);
  new ResizeObserver(redraw).observe(strip);
  document.fonts?.ready?.then(redraw);
  draw();

  // Drag to pan with a mouse (touch and trackpads already scroll sideways natively).
  let drag = null;
  strip.addEventListener("pointerdown", (e) => {
    if (e.pointerType !== "mouse" || e.button !== 0 || e.target.closest("a")) return;
    drag = { x: e.clientX, left: strip.scrollLeft, moved: false };
  });
  window.addEventListener("pointermove", (e) => {
    if (!drag) return;
    const dx = e.clientX - drag.x;
    if (!drag.moved && Math.abs(dx) > 4) { drag.moved = true; strip.classList.add("dragging"); }
    if (drag.moved) strip.scrollLeft = drag.left - dx;
  });
  window.addEventListener("pointerup", () => { drag = null; strip.classList.remove("dragging"); });

  // ---------- live status ----------
  const tbody = document.getElementById("status");
  const badge = (tone, text) => `<span class="badge ${tone}">${text}</span>`;
  const row = (name, status, evidence) => `<tr><td>${name}</td><td>${status}</td><td>${evidence}</td></tr>`;
  fetch(new URL("/dashboard/api/status", location.origin))
    .then((r) => (r.ok ? r.json() : Promise.reject(r.status)))
    .then((s) => {
      const rows = [
        row("Vaani phone agent", badge("blue", "Configured"),
          "Riya's prompt, guardrails, FAQ and extractions are set; chat tests passed (pricing, call-back wording, " +
          "prompt injection, Hindi decline). Needs a phone number on the Vaani account."),
        row("Website receptionist", s.website.on ? badge("green", "Live") : badge("red", "Off"),
          s.website.on ? `Answers on the home page with ${s.website.provider === "gemini" ? "Gemini" : "Claude"}.` : "No AI model key on the server."),
        row("Five checks (code)", badge("green", "Live"), "Automated tests on every change, including every transcript fixture and the no-price checks."),
        row("Vaani to dashboard", s.vaani_sync ? badge("green", "Live") : badge("yellow", "Built"),
          s.vaani_sync ? "Finished calls are pulled when the dashboard opens and every 15 minutes." : "Waiting for the Vaani API key on the server."),
        row("Database", s.database ? badge("green", "Live") : badge("red", "Off"), "Neon Postgres in Singapore; calls, leads and demo sessions."),
        row("Call-back email", s.email.key && s.email.studio_head ? badge("green", "Live") : s.email.key ? badge("yellow", "Partial") : badge("red", "Not set up"),
          s.email.key && s.email.studio_head ? "Resend sends call-back requests to the studio head." : s.email.key ? "Resend key is set; needs the studio head's email address." : "No Resend key."),
        row("Telegram", s.telegram ? badge("green", "Live") : badge("grey", "Not set up"), s.telegram ? "Bot sends handoffs and alerts." : "Optional; email covers call backs."),
        row("Designers", s.designers > 0 ? badge("green", `${s.designers} active`) : badge("yellow", "None yet"), "Designer handoffs go to the designers in the database."),
        row("Calendar booking", s.calendar ? badge("green", "Live") : badge("grey", "Not connected"), "Riya takes consultation requests; the team confirms the time."),
        row("Dashboard", badge("green", "Live"), `<a href="/dashboard">${location.host}/dashboard</a>`),
      ];
      tbody.innerHTML = rows.join("");
    })
    .catch(() => { tbody.innerHTML = row("Status", badge("red", "Unavailable"), "The server didn't answer; try again shortly."); });

  // ---------- reveal in step order ----------
  const M = window.Motion || {};
  if (!M.hasGsap || M.reduce || !window.ScrollTrigger) return;
  const ordered = [...steps].sort((a, b) => a.dataset.step - b.dataset.step);
  gsap.from(ordered, { autoAlpha: 0, y: 14, duration: 0.6, ease: "expo.out", stagger: 0.07,
    scrollTrigger: { trigger: map, start: "top 80%", once: true }, onComplete: redraw });
  gsap.from(svg, { autoAlpha: 0, duration: 0.8, delay: 0.5, scrollTrigger: { trigger: map, start: "top 80%", once: true } });
})();
