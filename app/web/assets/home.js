// Home page choreography: Vanta fog hero, GSAP + SplitText reveals, pinned horizontal story,
// scrubbed pricing promise, live count-ups. Everything degrades to a static, readable page.
(() => {
  const M = window.Motion || {};
  const hasGsap = !!M.hasGsap;
  const reduce = !!M.reduce;
  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
  const done = () => document.documentElement.classList.remove("js-motion");

  // ---------- 1. Vanta fog: warm light drifting behind the hero ----------
  const heroBg = $("#hero-bg");
  const dark = window.matchMedia("(prefers-color-scheme: dark)");
  const PALETTES = {
    light: { highlightColor: 0xf3d9bb, midtoneColor: 0xe6c19c, lowlightColor: 0xd9a47c, baseColor: 0xf7f6f3 },
    dark: { highlightColor: 0x6e4f34, midtoneColor: 0x3d2c1f, lowlightColor: 0x231a13, baseColor: 0x161615 },
  };
  let fog = null;
  let paused = false;
  function webglOk() {
    try { const c = document.createElement("canvas"); return !!(c.getContext("webgl") || c.getContext("experimental-webgl")); }
    catch { return false; }
  }
  // The fog is heavily blurred, so it's drawn at half (desktop) or a third (phones) of CSS
  // resolution and stretched: it looks the same and costs a quarter of the GPU time or less.
  // Vanta renders at devicePixelRatio / scale.
  const dpr = Math.min(window.devicePixelRatio || 1, 3);
  function startFog() {
    if (fog || reduce || !window.VANTA?.FOG || !webglOk()) return;
    try {
      fog = VANTA.FOG({
        el: heroBg, mouseControls: true, touchControls: true, gyroControls: false,
        minHeight: 200, minWidth: 200, blurFactor: 0.62, speed: 0.85, zoom: 0.75, scale: dpr * 2, scaleMobile: dpr * 3,
        ...(dark.matches ? PALETTES.dark : PALETTES.light),
      });
      requestAnimationFrame(() => heroBg.classList.add("ready"));
    } catch { fog = null; }
  }
  // Pause the render loop off screen instead of destroying the effect: rebuilding the WebGL
  // context on every scroll back to the top caused a visible hitch.
  function pauseFog() {
    if (!fog || paused) return;
    if (typeof fog.req === "number" && typeof fog.animationLoop === "function") { cancelAnimationFrame(fog.req); paused = true; }
    else { fog.destroy(); fog = null; heroBg.classList.remove("ready"); }
  }
  let heroVisible = true, idle = false;
  function resumeFog() {
    if (!idle) return; // first start waits for the idle moment below
    if (!fog) return startFog();
    if (paused) { paused = false; fog.req = requestAnimationFrame(fog.animationLoop); }
  }
  dark.addEventListener?.("change", () => fog?.setOptions(dark.matches ? PALETTES.dark : PALETTES.light));
  if ("IntersectionObserver" in window) {
    new IntersectionObserver(([e]) => {
      heroVisible = e.isIntersecting;
      heroVisible ? resumeFog() : pauseFog();
    }, { threshold: 0.02 }).observe($(".hero-section"));
  }
  // The fog starts in a quiet moment so it never competes with the first paint and the intro.
  const whenIdle = window.requestIdleCallback || ((fn) => setTimeout(fn, 400));
  whenIdle(() => { idle = true; if (heroVisible) startFog(); }, { timeout: 1200 });

  // ---------- 2. "Start talking" ----------
  $("#start-talking")?.addEventListener("click", () => {
    const chat = $("#chat");
    if (window.matchMedia("(max-width: 960px)").matches) M.scrollTo?.("#chat");
    chat.classList.remove("glow"); void chat.offsetWidth; chat.classList.add("glow");
    const mic = $("#mic");
    if (mic && !mic.disabled && (window.SpeechRecognition || window.webkitSpeechRecognition)) mic.click();
    else $("#text")?.focus({ preventScroll: true });
  });

  // ---------- 3. Live numbers (real calls plus website demos, last 30 days) ----------
  let stats = null;
  const statsReady = fetch(new URL("/dashboard/api/metrics?include_demo=true", location.origin))
    .then((r) => (r.ok ? r.json() : null))
    .then((m) => {
      if (!m) return;
      const declined = Object.values(m.declines || {}).reduce((a, b) => a + b, 0);
      stats = { calls: m.funnel.calls, qualified: m.funnel.qualified, booked: m.funnel.booked, declined };
    })
    .catch(() => {});
  function showStats() {
    statsReady.then(() => $$("[data-stat]").forEach((el) => {
      const v = stats?.[el.dataset.stat];
      if (M.countUp) M.countUp(el, v ?? null, { duration: 1.8 }); else el.textContent = v ?? "—";
    }));
  }

  if (!hasGsap) { done(); showStats(); return; }

  // ---------- 4. Hero intro ----------
  const mm = gsap.matchMedia();
  const startDelay = document.documentElement.classList.contains("arriving") ? 0.55 : 0.1;
  const intro = gsap.timeline({ defaults: { ease: "expo.out" }, delay: startDelay });
  if (window.SplitText && !reduce) {
    let first = true;
    SplitText.create("#hero-title", {
      type: "lines, words", mask: "lines", autoSplit: true,
      onSplit(self) {
        // Re-splits (resize, font swap) just settle in place; only the first one animates.
        const tween = gsap.from(self.lines, { yPercent: 110, duration: first ? 1.3 : 0.01, stagger: first ? 0.12 : 0, ease: "expo.out", delay: first ? startDelay : 0 });
        first = false;
        return tween;
      },
    });
  }
  done(); // content is styled visible from here; GSAP owns the from-states
  if (!reduce) {
    intro
      .from(".hero-eyebrow", { autoAlpha: 0, y: 12, duration: 0.9 }, 0.05)
      .from(".hero-lede", { autoAlpha: 0, y: 18, duration: 1.1 }, 0.35)
      .from(".hero-actions > *", { autoAlpha: 0, y: 16, duration: 0.9, stagger: 0.08 }, 0.5)
      .from("#chat", { autoAlpha: 0, y: 60, scale: 0.97, duration: 1.4, clearProps: "transform" }, 0.25)
      .from(".hero-tries .try-title", { autoAlpha: 0, duration: 0.8 }, 0.8)
      .from(".tries .try", { autoAlpha: 0, x: -18, duration: 0.9, stagger: 0.07, clearProps: "transform" }, 0.85)
      .from(".scroll-cue", { autoAlpha: 0, duration: 1 }, 1.2);
  }

  // ---------- 5. Magnetic buttons (fine pointers only) ----------
  mm.add("(hover: hover) and (pointer: fine) and (prefers-reduced-motion: no-preference)", () => {
    const cleanups = $$(".magnetic").map((btn) => {
      const xTo = gsap.quickTo(btn, "x", { duration: 0.5, ease: "power3" });
      const yTo = gsap.quickTo(btn, "y", { duration: 0.5, ease: "power3" });
      const move = (e) => { const r = btn.getBoundingClientRect(); xTo((e.clientX - r.left - r.width / 2) * 0.25); yTo((e.clientY - r.top - r.height / 2) * 0.35); };
      const out = () => { xTo(0); yTo(0); };
      btn.addEventListener("pointermove", move); btn.addEventListener("pointerleave", out);
      return () => { btn.removeEventListener("pointermove", move); btn.removeEventListener("pointerleave", out); };
    });
    return () => cleanups.forEach((c) => c());
  });

  if (!window.ScrollTrigger) { showStats(); return; }

  // ---------- 6. Section headings: masked line reveals ----------
  if (window.SplitText && !reduce) {
    $$(".split-lines").forEach((h) => SplitText.create(h, {
      type: "lines", mask: "lines", autoSplit: true,
      onSplit: (self) => gsap.from(self.lines, {
        yPercent: 105, duration: 1.1, ease: "expo.out", stagger: 0.1,
        scrollTrigger: { trigger: h, start: "top 85%", once: true },
      }),
    }));
  }

  // Hero drifts back as you scroll away.
  if (!reduce) {
    gsap.to(".hero", { yPercent: -8, autoAlpha: 0.35, ease: "none", scrollTrigger: { trigger: ".hero-section", start: "top top", end: "bottom top", scrub: true } });
  }

  // ---------- 7. One call, start to finish ----------
  const track = $("#flow-track");
  const paths = (panel) => $$(".draw path", panel);
  function prepDraw(panel) {
    paths(panel).forEach((p) => { const len = p.getTotalLength(); gsap.set(p, { strokeDasharray: len, strokeDashoffset: len }); });
  }
  mm.add("(min-width: 961px) and (prefers-reduced-motion: no-preference)", () => {
    const panels = $$(".panel", track);
    panels.forEach(prepDraw);
    const distance = () => track.scrollWidth - window.innerWidth;
    const slide = gsap.to(track, {
      x: () => -distance(), ease: "none",
      scrollTrigger: {
        trigger: ".flow", pin: ".flow-pin", start: "top top", end: () => "+=" + distance(), scrub: 0.8,
        invalidateOnRefresh: true, anticipatePin: 1,
      },
    });
    gsap.to("#flow-bar", { scaleX: 1, ease: "none", scrollTrigger: { trigger: ".flow", start: "top top", end: () => "+=" + distance(), scrub: true, invalidateOnRefresh: true } });
    panels.forEach((panel) => {
      gsap.timeline({ scrollTrigger: { trigger: panel, containerAnimation: slide, start: "left 85%", toggleActions: "play none none reverse" } })
        .from(panel, { autoAlpha: 0.25, y: 30, duration: 0.6, ease: "power3.out" })
        .to(paths(panel), { strokeDashoffset: 0, duration: 1.1, ease: "power2.inOut", stagger: 0.12 }, 0.1)
        .from($$("h3, p", panel), { y: 14, autoAlpha: 0, duration: 0.6, stagger: 0.08, ease: "power3.out" }, 0.15);
    });
  });
  mm.add("(max-width: 960px) and (prefers-reduced-motion: no-preference)", () => {
    $$(".panel", track).forEach((panel) => {
      prepDraw(panel);
      gsap.timeline({ scrollTrigger: { trigger: panel, start: "top 85%", once: true } })
        .from(panel, { autoAlpha: 0, y: 40, duration: 0.8, ease: "expo.out" })
        .to(paths(panel), { strokeDashoffset: 0, duration: 1, ease: "power2.inOut", stagger: 0.1 }, 0.2);
    });
  });

  // ---------- 8. The pricing promise lights up word by word ----------
  if (window.SplitText && !reduce) {
    SplitText.create("#promise-text", {
      type: "words", wordsClass: "w", autoSplit: true,
      onSplit: (self) => gsap.to(self.words, {
        opacity: 1, ease: "none", stagger: 0.1,
        scrollTrigger: { trigger: ".promise", start: "top 70%", end: "bottom 60%", scrub: 0.6 },
      }),
    });
    gsap.from(".promise-note", { autoAlpha: 0, y: 16, duration: 1, ease: "expo.out", scrollTrigger: { trigger: ".promise-note", start: "top 90%", once: true } });
  }

  // ---------- 9. Five checks ----------
  if (!reduce) {
    ScrollTrigger.batch(".check-list li", {
      start: "top 88%", once: true,
      onEnter: (items) => gsap.from(items, { autoAlpha: 0, x: 40, duration: 1, ease: "expo.out", stagger: 0.1 }),
    });
  }

  // ---------- 10. Live numbers count up when seen ----------
  ScrollTrigger.create({ trigger: ".live-grid", start: "top 85%", once: true, onEnter: showStats });
  if (!reduce) {
    gsap.from(".stat", { autoAlpha: 0, y: 30, duration: 1, ease: "expo.out", stagger: 0.08, scrollTrigger: { trigger: ".live-grid", start: "top 85%", once: true } });
    gsap.from(".live-cta", { autoAlpha: 0, y: 16, duration: 0.9, ease: "expo.out", scrollTrigger: { trigger: ".live-cta", start: "top 95%", once: true } });
  }

  // ---------- 11. Footer wordmark rises letter by letter ----------
  if (window.SplitText && !reduce) {
    SplitText.create(".footer-mark", {
      type: "chars", mask: "chars", autoSplit: true,
      onSplit: (self) => gsap.from(self.chars, {
        yPercent: 100, duration: 1.4, ease: "expo.out", stagger: 0.05,
        scrollTrigger: { trigger: ".big-footer", start: "top 90%", once: true },
      }),
    });
  }

  window.addEventListener("load", () => ScrollTrigger.refresh());
})();
