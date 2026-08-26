"use strict";
// Shared behaviour for the marketing/info pages.
(function () {
  const root = document.documentElement;
  const toggle = document.getElementById("theme-toggle");

  function applyTheme(theme) {
    const light = theme === "light";
    root.classList.toggle("light", light);
    if (toggle) toggle.textContent = light ? "🌙" : "☀";
  }
  applyTheme(root.classList.contains("light") ? "light" : "dark");

  if (toggle) {
    toggle.addEventListener("click", () => {
      const next = root.classList.contains("light") ? "dark" : "light";
      try { localStorage.setItem("omnidl-theme", next); } catch (_) {}
      applyTheme(next);
    });
  }

  const year = document.getElementById("year");
  if (year) year.textContent = new Date().getFullYear();

  // Scroll progress bar (site-wide).
  const bar = document.createElement("div");
  bar.id = "scroll-progress";
  document.body.appendChild(bar);
  const onScroll = () => {
    const h = document.documentElement;
    const max = h.scrollHeight - h.clientHeight;
    bar.style.width = (max > 0 ? (h.scrollTop / max) * 100 : 0) + "%";
  };
  window.addEventListener("scroll", onScroll, { passive: true });
  window.addEventListener("resize", onScroll, { passive: true });
  onScroll();

  // Animated stat counters — count up when the stats row scrolls into view.
  const nums = document.querySelectorAll(".stat .num[data-target]");
  if (nums.length && "IntersectionObserver" in window) {
    const reduceM = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const animate = (node) => {
      const target = parseFloat(node.dataset.target);
      const suffix = node.dataset.suffix || "";
      if (reduceM) { node.textContent = target + suffix; return; }
      const dur = 1100; const t0 = performance.now();
      const tick = (now) => {
        const k = Math.min((now - t0) / dur, 1);
        const eased = 1 - Math.pow(1 - k, 3);
        node.textContent = Math.round(target * eased) + suffix;
        if (k < 1) requestAnimationFrame(tick);
      };
      requestAnimationFrame(tick);
    };
    const sio = new IntersectionObserver((entries) => {
      entries.forEach((e) => { if (e.isIntersecting) { animate(e.target); sio.unobserve(e.target); } });
    }, { threshold: 0.4 });
    nums.forEach((n) => sio.observe(n));
  }

  // Scroll-reveal: fade/slide sections + cards into view as you scroll.
  if (root.classList.contains("js")) {
    window.__omnidlReveal = true;  // signals the inline safety-net that reveal initialised
    const els = document.querySelectorAll(
      ".block .section-head, .block .app-shot, .block .card, .block .step, .block .engine, .block .cta-band, .block details"
    );
    const reduce = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (reduce || !("IntersectionObserver" in window)) {
      els.forEach((el) => el.classList.add("in"));
    } else {
      const io = new IntersectionObserver((entries) => {
        entries.forEach((e) => {
          if (e.isIntersecting) { e.target.classList.add("in"); io.unobserve(e.target); }
        });
      }, { threshold: 0.12, rootMargin: "0px 0px -6% 0px" });
      const counts = new Map();
      els.forEach((el) => {
        const p = el.parentElement;          // stagger siblings in the same grid/row
        const i = counts.get(p) || 0;
        el.style.transitionDelay = Math.min(i * 16, 80) + "ms";
        counts.set(p, i + 1);
        io.observe(el);
      });
    }
  }
})();
