"use strict";
/* Autoplaying terminal demo for the hero — loops through a realistic OmniDL download. */
(function () {
  const el = document.getElementById("demo");
  if (!el) return;
  const reduce = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const TRACKS = [
    "Ariana Grande — Break Free",
    "Rage Against The Machine — Killing…",
    "Carly Rae Jepsen — Call Me Maybe",
    "Daft Punk — One More Time",
  ];

  function row(html) { const d = document.createElement("div"); d.className = "demo-ln"; d.innerHTML = html; el.appendChild(d); return d; }

  if (reduce) {
    row('<span class="c-blue">🔎 Resolving Spotify playlist…</span>');
    row('<span class="c-green">📋 Poppy Rock Type Beat — 64 tracks</span>');
    TRACKS.forEach((t, i) => row('<span class="c-cyan">[' + (i + 1) + '/64]</span> ' + t + ' <span class="c-green">✓ saved</span>'));
    row('<span class="c-green">✅ 64 downloaded</span>');
    return;
  }

  let jobs = [];
  const wait = (ms, fn) => { const t = setTimeout(fn, ms); jobs.push(() => clearTimeout(t)); };
  const reset = () => { jobs.forEach((c) => c()); jobs = []; el.innerHTML = ""; };

  function track(idx, name) {
    const node = row('<span class="c-cyan">[' + idx + '/64]</span> ' + name +
      ' <span class="bar2"><i></i></span><span class="pct c-mag">0%</span>');
    const fill = node.querySelector("i"), pct = node.querySelector(".pct");
    let p = 0;
    const iv = setInterval(() => {
      p += 8 + Math.random() * 16;
      if (p >= 100) {
        clearInterval(iv);
        fill.style.width = "100%";
        const bar = node.querySelector(".bar2"); if (bar) bar.remove();
        pct.className = "c-green"; pct.textContent = "✓ saved";
      } else { fill.style.width = p + "%"; pct.textContent = Math.round(p) + "%"; }
    }, 170);
    jobs.push(() => clearInterval(iv));
  }

  function run() {
    reset();
    row('<span class="c-blue">🔎 Resolving Spotify playlist…</span> <span class="c-dim">(no API · no Premium)</span>');
    wait(650, () => row('<span class="c-green">📋 Poppy Rock Type Beat — 64 tracks</span>'));
    wait(1150, () => row('<span class="c-dim">   ↳ downloading 4 at a time</span>'));
    wait(1650, () => track(1, TRACKS[0]));
    wait(1950, () => track(2, TRACKS[1]));
    wait(2250, () => track(3, TRACKS[2]));
    wait(2550, () => track(4, TRACKS[3]));
    wait(6200, () => row('<span class="c-green">✅ 64 downloaded · 0 needs review</span>'));
    wait(9200, run); // loop
  }

  // The demo is the hero centerpiece — start it right away (don't gate on scroll).
  run();
})();
