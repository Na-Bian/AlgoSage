/* User preference application. Stored locally; never changes server safety policy. */
(function () {
  "use strict";
  var STORAGE_KEY = "algosage-user-preferences";
  var DEFAULTS = {
    enter_behavior: "enter",
    autoscroll: true,
    keep_drafts: true,
    animations: true,
    font_size: "standard",
    audit_summary: true,
  };

  function readPreferences() {
    try {
      return Object.assign({}, DEFAULTS, JSON.parse(localStorage.getItem(STORAGE_KEY) || "{}"));
    } catch (_) {
      return Object.assign({}, DEFAULTS);
    }
  }

  function writePreferences(preferences) {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(preferences));
    apply(preferences);
  }

  function applyFont(value) {
    document.documentElement.style.fontSize = value === "small" ? "14px" : value === "large" ? "18px" : "";
  }

  function threadKey() {
    var match = location.pathname.match(/^\/thread\/([^/]+)/);
    return match ? match[1] : "new";
  }

  function saveDraft() {
    var input = document.getElementById("chat-input");
    if (!input) return;
    var prefs = readPreferences();
    var drafts = {};
    try { drafts = JSON.parse(localStorage.getItem("algosage-drafts") || "{}"); } catch (_) {}
    if (input.value) drafts[threadKey()] = input.value;
    else delete drafts[threadKey()];
    localStorage.setItem("algosage-drafts", JSON.stringify(drafts));
  }

  function restoreDraft() {
    if (!readPreferences().keep_drafts) return;
    var input = document.getElementById("chat-input");
    if (!input || input.value) return;
    try {
      var drafts = JSON.parse(localStorage.getItem("algosage-drafts") || "{}");
      var value = drafts[threadKey()];
      if (typeof value === "string" && value && !input.value) {
        var setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value").set;
        setter.call(input, value);
        input.dispatchEvent(new Event("input", { bubbles: true }));
      }
    } catch (_) {}
  }

  function apply(preferences) {
    document.body.classList.toggle("eg-preferences-no-motion", !preferences.animations);
    document.body.classList.toggle("eg-hide-audit-summary", !preferences.audit_summary);
    applyFont(preferences.font_size);
    try { window.localStorage.setItem("algosage-active-preferences", JSON.stringify(preferences)); } catch (_) {}
  }

  window.addEventListener("message", function (event) {
    var data = event.data;
    if (!data || data.type !== "algosage-preferences" || !data.preferences) return;
    var incoming = data.preferences || {};
    var current = readPreferences();
    Object.keys(DEFAULTS).forEach(function (key) {
      if (incoming[key] !== null && typeof incoming[key] !== "undefined") current[key] = incoming[key];
    });
    writePreferences(current);
  });

  document.addEventListener("input", function (event) {
    if (event.target && event.target.id === "chat-input") saveDraft();
  }, true);

  new MutationObserver(function () {
    if (readPreferences().keep_drafts) restoreDraft();
  }).observe(document.body, { childList: true, subtree: true });

  document.addEventListener("keydown", function (event) {
    if (event.key !== "Enter" || !event.target || event.target.id !== "chat-input") return;
    var mode = readPreferences().enter_behavior;
    if (mode !== "ctrl_enter" || event.ctrlKey || event.metaKey) return;
    if (event.shiftKey) return;
    event.preventDefault();
    event.stopImmediatePropagation();
  }, true);

  document.addEventListener("keydown", function (event) {
    if (event.key !== "Enter" || !event.ctrlKey && !event.metaKey) return;
    if (!event.target || event.target.id !== "chat-input" || readPreferences().enter_behavior !== "ctrl_enter") return;
    event.preventDefault();
    event.stopImmediatePropagation();
    document.getElementById("chat-submit")?.click();
  }, true);

  ["scrollTo", "scrollIntoView"].forEach(function (method) {
    var original = Element.prototype[method];
    if (!original) return;
    Element.prototype[method] = function () {
      if (document.body.classList.contains("eg-disable-autoscroll")) return this;
      return original.apply(this, arguments);
    };
  });

  function refreshAutoscroll() {
    document.body.classList.toggle("eg-disable-autoscroll", !readPreferences().autoscroll);
  }

  function refreshAll() {
    var prefs = readPreferences();
    apply(prefs);
    refreshAutoscroll();
    restoreDraft();
  }

  async function loadServerPreferences() {
    try {
      var response = await fetch("/auth/preferences", {
        credentials: "same-origin",
        headers: { Accept: "application/json" },
      });
      if (!response.ok) return;
      var payload = await response.json();
      var serverPrefs = payload && payload.preferences;
      if (!serverPrefs || typeof serverPrefs !== "object") return;
      var next = Object.assign({}, DEFAULTS);
      Object.keys(DEFAULTS).forEach(function (key) {
        if (serverPrefs[key] !== null && typeof serverPrefs[key] !== "undefined") {
          next[key] = serverPrefs[key];
        }
      });
      localStorage.setItem(STORAGE_KEY, JSON.stringify(next));
      apply(next);
      refreshAutoscroll();
    } catch (_) {
      // Stay in local-preference mode when the endpoint is unavailable.
    }
  }

  window.addEventListener("algosage-preferences-read", refreshAll);
  refreshAll();
  loadServerPreferences();
  new MutationObserver(function () { setTimeout(refreshAll, 30); }).observe(document.body, { childList: true, subtree: true });
  window.addEventListener("popstate", refreshAll);
})();
