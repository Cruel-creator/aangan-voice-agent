// Shared motion layer: page curtain transitions, Lenis smooth scroll synced to GSAP,
// count-ups. Degrades to plain navigation and native scroll if a CDN script fails.
window.Motion = (() => {
  const hasGsap = typeof window.gsap !== "undefined";
  const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  let lenis = null;

  if (hasGsap && window.ScrollTrigger) gsap.registerPlugin(ScrollTrigger);
  if (hasGsap && window.SplitText) gsap.registerPlugin(SplitText);

  // ---------- smooth scroll (pages opt in with <body data-smooth>) ----------
  function initSmooth() {
    if (!document.body.hasAttribute("data-smooth") || reduce || !window.Lenis || !hasGsap) return null;
    lenis = new Lenis({ lerp: 0.09, anchors: true, allowNestedScroll: true, stopInertiaOnNavigate: true });
    if (window.ScrollTrigger) lenis.on("scroll", ScrollTrigger.update);
    gsap.ticker.add((t) => lenis.raf(t * 1000));
    gsap.ticker.lagSmoothing(0);
    return lenis;
  }

  function scrollTo(target, opts = {}) {
    const el = typeof target === "string" ? document.querySelector(target) : target;
    if (lenis) lenis.scrollTo(el ?? target, { offset: -24, duration: 1.2, ...opts });
    else if (el?.scrollIntoView) el.scrollIntoView({ behavior: reduce ? "auto" : "smooth", block: "start" });
  }

  // ---------- page curtain ----------
  const curtain = document.querySelector(".curtain");
  function arrive() {
    if (!curtain) return;
    const arriving = document.documentElement.classList.contains("arriving");
    try { sessionStorage.removeItem("aangan-nav"); } catch {}
    if (!arriving || !hasGsap || reduce) { document.documentElement.classList.remove("arriving"); return; }
    gsap.timeline({ onComplete: () => document.documentElement.classList.remove("arriving") })
      .to(curtain.querySelector(".curtain-mark"), { autoAlpha: 0, y: -12, duration: 0.35, ease: "power2.in" })
      .fromTo(curtain, { y: 0, yPercent: 0 }, { yPercent: -100, duration: 0.75, ease: "expo.inOut" }, "-=0.05")
      .set(curtain, { clearProps: "transform" });
  }
  function leave(href) {
    if (!curtain || !hasGsap || reduce) { location.href = href; return; }
    try { sessionStorage.setItem("aangan-nav", "1"); } catch {}
    lenis?.stop();
    gsap.timeline({ onComplete: () => { location.href = href; } })
      // y: 0 overrides the CSS translateY(100%) that GSAP would otherwise read as a pixel offset.
      .fromTo(curtain, { y: 0, yPercent: 100, visibility: "visible" }, { yPercent: 0, duration: 0.6, ease: "expo.inOut" })
      .fromTo(curtain.querySelector(".curtain-mark"), { autoAlpha: 0, y: 12 }, { autoAlpha: 1, y: 0, duration: 0.3 }, "-=0.2");
  }
  document.addEventListener("click", (e) => {
    const a = e.target.closest("a[href]");
    if (!a || e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    const url = new URL(a.href, location.href);
    if (url.origin !== location.origin || a.target === "_blank" || a.hasAttribute("download")) return;
    if (url.pathname === location.pathname && url.hash) return; // in-page anchor: Lenis handles it
    e.preventDefault();
    leave(url.href);
  });
  // Back/forward cache restores the page with the curtain down; lift it.
  window.addEventListener("pageshow", (e) => {
    if (!e.persisted || !curtain) return;
    if (hasGsap) gsap.set(curtain, { y: 0, yPercent: 100, visibility: "hidden" });
    lenis?.start();
  });

  // ---------- helpers ----------
  function countUp(el, value, { prefix = "", suffix = "", duration = 1.4, decimals = 0 } = {}) {
    const nf = new Intl.NumberFormat("en-IN", { maximumFractionDigits: decimals, minimumFractionDigits: decimals });
    if (value === null || value === undefined || Number.isNaN(Number(value))) { el.textContent = "—"; return; }
    if (!hasGsap || reduce) { el.textContent = prefix + nf.format(value) + suffix; return; }
    const obj = { v: 0 };
    gsap.to(obj, { v: Number(value), duration, ease: "expo.out", onUpdate: () => { el.textContent = prefix + nf.format(obj.v) + suffix; } });
  }

  // Nav: tuck away on scroll down, return on scroll up (only once past the hero top).
  function smartNav() {
    const nav = document.querySelector(".nav-bar");
    if (!nav || !hasGsap || !window.ScrollTrigger) return;
    // Track the whole page; decide from the actual scroll position, so short pages (where a
    // fixed start offset could lie beyond the end) never hide the nav at the top.
    let hidden = false;
    const show = (yes) => {
      if (reduce || hidden === !yes) return;
      hidden = !yes;
      gsap.to(nav, { yPercent: yes ? 0 : -110, duration: 0.35, ease: "power2.out", overwrite: "auto" });
    };
    ScrollTrigger.create({
      start: 0, end: "max",
      onUpdate: (self) => {
        const y = self.scroll();
        nav.classList.toggle("scrolled", y > 40);
        show(y < 140 || self.direction === -1);
      },
    });
  }

  initSmooth();
  arrive();
  smartNav();
  document.fonts?.ready?.then(() => window.ScrollTrigger?.refresh());
  return { hasGsap, reduce, get lenis() { return lenis; }, scrollTo, countUp, leave };
})();
