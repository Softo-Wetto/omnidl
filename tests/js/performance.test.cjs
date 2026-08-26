const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const projectRoot = path.resolve(__dirname, "..", "..");

test("dashboard controls bind before metadata loads and requests start together", async () => {
  const { startDashboard } = require(path.join(
    projectRoot,
    "app",
    "static",
    "js",
    "startup.js",
  ));
  const events = [];
  let resolveMeta;
  let resolveSettings;
  const metaPromise = new Promise((resolve) => { resolveMeta = resolve; });
  const settingsPromise = new Promise((resolve) => { resolveSettings = resolve; });

  const started = startDashboard({
    bind() { events.push("bind"); },
    loadMeta() { events.push("meta-start"); return metaPromise; },
    loadSettings() { events.push("settings-start"); return settingsPromise; },
    hydrate(meta, settings) { events.push(["hydrate", meta, settings]); },
  });

  assert.deepEqual(events, ["bind", "meta-start", "settings-start"]);
  resolveSettings({ format: "opus" });
  await Promise.resolve();
  assert.deepEqual(events, ["bind", "meta-start", "settings-start"]);
  resolveMeta({ local: true });
  await started;
  assert.deepEqual(events.at(-1), [
    "hydrate",
    { local: true },
    { format: "opus" },
  ]);
});

function runBackground({ reduceMotion }) {
  const source = fs.readFileSync(
    path.join(projectRoot, "app", "static", "js", "bg.js"),
    "utf8",
  );
  const scheduled = [];
  let clearCount = 0;
  const context = {
    beginPath() {},
    arc() {},
    fill() {},
    stroke() {},
    moveTo() {},
    lineTo() {},
    clearRect() { clearCount += 1; },
    setTransform() {},
  };
  const canvas = {
    clientWidth: 320,
    clientHeight: 180,
    getContext() { return context; },
    getBoundingClientRect() { return { left: 0, top: 0 }; },
  };
  const document = {
    hidden: false,
    documentElement: {},
    getElementById(id) { return id === "bg-canvas" ? canvas : null; },
    addEventListener() {},
  };
  const window = {
    devicePixelRatio: 1,
    matchMedia() { return { matches: reduceMotion }; },
    addEventListener() {},
  };
  const sandbox = {
    document,
    window,
    MutationObserver: class { observe() {} },
    getComputedStyle() { return { getPropertyValue() { return "#5b8cff"; } }; },
    requestAnimationFrame(callback) { scheduled.push(callback); return scheduled.length; },
    cancelAnimationFrame() {},
    Math,
    parseInt,
  };

  vm.runInNewContext(source, sandbox, { filename: "bg.js" });
  return {
    scheduled,
    get clearCount() { return clearCount; },
  };
}

test("reduced motion draws once without starting an animation loop", () => {
  const background = runBackground({ reduceMotion: true });

  assert.equal(background.clearCount, 1);
  assert.equal(background.scheduled.length, 0);
});

test("animated background is capped near thirty frames per second", () => {
  const background = runBackground({ reduceMotion: false });
  assert.equal(background.scheduled.length, 1);

  background.scheduled.shift()(100);
  assert.equal(background.clearCount, 1);

  background.scheduled.shift()(110);
  assert.equal(background.clearCount, 1);

  background.scheduled.shift()(140);
  assert.equal(background.clearCount, 2);
});

test("landing-page reveal groups keep their stagger below one tenth of a second", () => {
  const source = fs.readFileSync(
    path.join(projectRoot, "app", "static", "js", "site.js"),
    "utf8",
  );
  const parent = {};
  const revealElements = Array.from({ length: 8 }, () => ({
    parentElement: parent,
    style: {},
    classList: { add() {} },
  }));
  let queryCount = 0;
  const classList = {
    contains(name) { return name === "js"; },
    toggle() {},
  };
  class Observer {
    observe() {}
    unobserve() {}
  }
  const window = {
    IntersectionObserver: Observer,
    addEventListener() {},
    matchMedia() { return { matches: false }; },
  };
  const document = {
    documentElement: {
      classList,
      scrollHeight: 100,
      clientHeight: 100,
      scrollTop: 0,
    },
    body: { appendChild() {} },
    createElement() { return { id: "", style: {} }; },
    getElementById() { return null; },
    querySelectorAll() {
      queryCount += 1;
      return queryCount === 1 ? [] : revealElements;
    },
  };
  vm.runInNewContext(source, {
    document,
    window,
    IntersectionObserver: Observer,
    Date,
    Map,
  }, { filename: "site.js" });

  const delays = revealElements.map((element) => parseInt(element.style.transitionDelay, 10));
  assert.equal(delays[0], 0);
  assert.ok(delays[1] > 0);
  assert.ok(Math.max(...delays) <= 80);
});
