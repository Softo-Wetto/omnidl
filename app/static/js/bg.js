"use strict";
/* Animated "constellation" background for the homepage hero.
   Subtle drifting particles linked by faint lines, with a gentle parallax toward the
   cursor. Respects prefers-reduced-motion and pauses when the tab is hidden. */
(function () {
  const canvas = document.getElementById("bg-canvas");
  if (!canvas) return;
  const reduce = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const ctx = canvas.getContext("2d");
  let w = 0, h = 0, dpr = Math.min(window.devicePixelRatio || 1, 2);
  let particles = [];
  let raf = null;
  const mouse = { x: -9999, y: -9999 };

  function accent() {
    // pull the theme accent so it matches light/dark
    const c = getComputedStyle(document.documentElement).getPropertyValue("--accent").trim();
    return c || "#5b8cff";
  }
  let color = accent();

  function resize() {
    w = canvas.clientWidth; h = canvas.clientHeight;
    canvas.width = w * dpr; canvas.height = h * dpr;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    const target = Math.min(90, Math.round((w * h) / 16000));
    particles = [];
    for (let i = 0; i < target; i++) {
      particles.push({
        x: Math.random() * w, y: Math.random() * h,
        vx: (Math.random() - 0.5) * 0.25, vy: (Math.random() - 0.5) * 0.25,
        r: Math.random() * 1.8 + 0.6,
      });
    }
  }

  function hexA(hex, a) {
    const m = hex.replace("#", "");
    const n = m.length === 3 ? m.split("").map((c) => c + c).join("") : m;
    const r = parseInt(n.slice(0, 2), 16), g = parseInt(n.slice(2, 4), 16), b = parseInt(n.slice(4, 6), 16);
    return `rgba(${r},${g},${b},${a})`;
  }

  function frame() {
    ctx.clearRect(0, 0, w, h);
    const maxDist = 130;
    for (const p of particles) {
      p.x += p.vx; p.y += p.vy;
      // gentle parallax pull toward cursor
      const dx = mouse.x - p.x, dy = mouse.y - p.y;
      const d2 = dx * dx + dy * dy;
      if (d2 < 26000) { p.x += dx * 0.0009; p.y += dy * 0.0009; }
      if (p.x < -20) p.x = w + 20; else if (p.x > w + 20) p.x = -20;
      if (p.y < -20) p.y = h + 20; else if (p.y > h + 20) p.y = -20;
    }
    for (let i = 0; i < particles.length; i++) {
      const a = particles[i];
      ctx.beginPath();
      ctx.arc(a.x, a.y, a.r, 0, Math.PI * 2);
      ctx.fillStyle = hexA(color, 0.55);
      ctx.fill();
      for (let j = i + 1; j < particles.length; j++) {
        const b = particles[j];
        const dx = a.x - b.x, dy = a.y - b.y;
        const dist = Math.hypot(dx, dy);
        if (dist < maxDist) {
          ctx.strokeStyle = hexA(color, 0.16 * (1 - dist / maxDist));
          ctx.lineWidth = 1;
          ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y); ctx.stroke();
        }
      }
    }
    raf = requestAnimationFrame(frame);
  }

  function start() { if (!raf && !reduce) raf = requestAnimationFrame(frame); }
  function stop() { if (raf) { cancelAnimationFrame(raf); raf = null; } }

  resize();
  if (reduce) { frame(); }  // draw one static frame
  else start();

  window.addEventListener("resize", () => { dpr = Math.min(window.devicePixelRatio || 1, 2); resize(); });
  window.addEventListener("mousemove", (e) => { const r = canvas.getBoundingClientRect(); mouse.x = e.clientX - r.left; mouse.y = e.clientY - r.top; });
  window.addEventListener("mouseout", () => { mouse.x = mouse.y = -9999; });
  document.addEventListener("visibilitychange", () => { document.hidden ? stop() : start(); });
  // re-read accent when theme toggles
  const obs = new MutationObserver(() => { color = accent(); });
  obs.observe(document.documentElement, { attributes: true, attributeFilter: ["class"] });
})();
