"use strict";

(function (root, factory) {
  const startup = factory();
  if (typeof module === "object" && module.exports) module.exports = startup;
  else root.OmniStartup = startup;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  async function startDashboard({ bind, loadMeta, loadSettings, hydrate }) {
    bind();
    const [meta, settings] = await Promise.all([loadMeta(), loadSettings()]);
    hydrate(meta, settings);
    return { meta, settings };
  }

  return { startDashboard };
});