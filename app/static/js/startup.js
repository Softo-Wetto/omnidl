"use strict";

(function (root, factory) {
  const startup = factory();
  if (typeof module === "object" && module.exports) module.exports = startup;
  else root.OmniStartup = startup;
})(typeof globalThis !== "undefined" ? globalThis : this, function () {
  // Controls are bound first, then metadata and settings load together. If either fails,
  // nothing half-loaded is applied: onError gets the error (the page shows Retry) and the
  // caller carries on with its safe defaults — a failed request used to throw out of startup
  // before the live connection was ever opened.
  async function startDashboard({ bind, loadMeta, loadSettings, hydrate, onError }) {
    bind();
    try {
      const [meta, settings] = await Promise.all([loadMeta(), loadSettings()]);
      hydrate(meta, settings);
      return { ok: true, meta, settings };
    } catch (error) {
      if (!onError) throw error;
      onError(error);
      return { ok: false, error };
    }
  }

  return { startDashboard };
});
