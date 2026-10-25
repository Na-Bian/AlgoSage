/* AlgoSage 输入区生成选项：深度思考 / 联网搜索（仿 DeepSeek 的 pill 开关）。
   状态存账号偏好（POST /chat/options），发送时由服务端透传给上游模型；
   联网搜索依赖上游模型支持（如 SiliconFlow 的 enable_search）。 */
(function chatOptions() {
  "use strict";

  var OPTIONS = [
    { key: "deep_think", label: "深度思考", icon: "brain" },
    { key: "web_search", label: "联网搜索", icon: "globe" },
  ];
  var state = { deep_think: false, web_search: false };

  function pillsContainer() {
    var composer = document.getElementById("message-composer");
    if (!composer) return null;
    var existing = composer.querySelector(".eg-option-pills");
    if (existing && existing.isConnected) return existing;
    var attach = composer.querySelector("button");
    var row = attach && attach.parentElement;
    if (!row) return null;
    var wrap = document.createElement("div");
    wrap.className = "eg-option-pills";
    OPTIONS.forEach(function (option) {
      var pill = document.createElement("button");
      pill.type = "button";
      pill.className = "eg-option-pill";
      pill.dataset.option = option.key;
      pill.innerHTML =
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true">' +
        (option.icon === "brain"
          ? '<path d="M9.5 3a3 3 0 0 0-3 3 3 3 0 0 0-2 5 3 3 0 0 0 1 4.5A3 3 0 0 0 9.5 21a2.5 2.5 0 0 0 2.5-2.5v-13A2.5 2.5 0 0 0 9.5 3Z"/><path d="M14.5 3a3 3 0 0 1 3 3 3 3 0 0 1 2 5 3 3 0 0 1-1 4.5A3 3 0 0 1 14.5 21 2.5 2.5 0 0 1 12 18.5v-13A2.5 2.5 0 0 1 14.5 3Z"/>'
          : '<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18"/>') +
        "</svg><span>" +
        option.label +
        "</span>";
      pill.addEventListener("click", function () {
        setOption(option.key, !state[option.key]);
      });
      wrap.append(pill);
    });
    row.insertBefore(wrap, attach);
    syncPills(wrap);
    return wrap;
  }

  function syncPills(wrap) {
    (wrap || document).querySelectorAll(".eg-option-pill").forEach(function (pill) {
      pill.classList.toggle("eg-option-pill-on", !!state[pill.dataset.option]);
      pill.setAttribute("aria-pressed", String(!!state[pill.dataset.option]));
    });
  }

  function setOption(key, value) {
    state[key] = value;
    syncPills();
    fetch("/chat/options", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify({ [key]: value }),
    }).catch(function () {});
  }

  function loadInitial() {
    fetch("/auth/preferences", {
      credentials: "same-origin",
      headers: { Accept: "application/json" },
    })
      .then(function (response) { return response.json(); })
      .then(function (prefs) {
        OPTIONS.forEach(function (option) {
          if (typeof prefs[option.key] === "boolean") state[option.key] = prefs[option.key];
        });
        syncPills();
      })
      .catch(function () {});
  }

  window.addEventListener("message", function (event) {
    var data = event.data;
    if (data && typeof data === "object" && data.type === "algosage-preferences") {
      var options = data.preferences || {};
      OPTIONS.forEach(function (option) {
        if (typeof options[option.key] === "boolean") state[option.key] = options[option.key];
      });
      syncPills();
    }
  });

  new MutationObserver(function () { pillsContainer(); }).observe(document.body, {
    childList: true,
    subtree: true,
  });
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () {
      pillsContainer();
      loadInitial();
    });
  } else {
    pillsContainer();
    loadInitial();
  }
})();
