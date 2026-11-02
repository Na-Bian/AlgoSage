/* AlgoSage 输入区生成选项：深度思考 / 联网搜索（仿 DeepSeek 的 pill 开关）
   与「辅助等级」上限选择。状态存账号偏好（POST /chat/options），发送时由
   服务端透传；联网搜索依赖上游模型支持（如 SiliconFlow 的 enable_search）。
   辅助等级只能收紧服务端策略判定的上限（生效 = min(策略, 学生上限)）。 */
(function chatOptions() {
  "use strict";

  var OPTIONS = [
    { key: "deep_think", label: "思考模式", icon: "brain" },
    { key: "web_search", label: "联网搜索", icon: "globe" },
  ];
  var state = { deep_think: false, web_search: false };

  var LEVELS = [
    { value: "auto", label: "自动（跟随审核）" },
    { value: 1, label: "L1 · 只讲概念" },
    { value: 2, label: "L2 · 方向性提示" },
    { value: 3, label: "L3 · 局部代码诊断" },
  ];
  var levelCap = "auto";
  var levelMenu = null;

  function levelLabel(value) {
    for (var i = 0; i < LEVELS.length; i++) {
      if (String(LEVELS[i].value) === String(value)) return LEVELS[i].label;
    }
    return "自动（跟随审核）";
  }

  function closeLevelMenu() {
    if (levelMenu) {
      levelMenu.remove();
      levelMenu = null;
    }
  }

  function toggleLevelMenu(pill) {
    if (levelMenu) {
      closeLevelMenu();
      return;
    }
    var menu = document.createElement("div");
    menu.className = "eg-level-menu";
    LEVELS.forEach(function (level) {
      var item = document.createElement("button");
      item.type = "button";
      item.className = "eg-level-item";
      if (String(level.value) === String(levelCap)) {
        item.classList.add("eg-level-item-active");
      }
      item.textContent = level.label;
      item.addEventListener("click", function (event) {
        event.stopPropagation();
        setLevel(level.value);
        closeLevelMenu();
      });
      menu.append(item);
    });
    document.body.append(menu);
    var rect = pill.getBoundingClientRect();
    menu.style.left = Math.max(8, rect.left) + "px";
    menu.style.bottom = window.innerHeight - rect.top + 8 + "px";
    levelMenu = menu;
    setTimeout(function () {
      document.addEventListener("click", closeLevelMenu, { once: true });
    }, 0);
  }

  function levelPill() {
    var pill = document.createElement("button");
    pill.type = "button";
    pill.className = "eg-option-pill eg-level-pill";
    pill.dataset.option = "help_level_cap";
    pill.innerHTML =
      '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true">' +
      '<path d="M12 3v3M12 18v3M3 12h3M18 12h3M5.6 5.6l2.1 2.1M16.3 16.3l2.1 2.1M18.4 5.6l-2.1 2.1M7.7 16.3l-2.1 2.1"/>' +
      '<circle cx="12" cy="12" r="3.2"/>' +
      "</svg><span>辅助等级：自动</span>" +
      '<svg class="eg-level-chevron" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true"><path d="m6 9 6 6 6-6"/></svg>';
    pill.addEventListener("click", function (event) {
      event.stopPropagation();
      toggleLevelMenu(pill);
    });
    updateLevelPill(pill);
    return pill;
  }

  function updateLevelPill(pill) {
    var span = pill.querySelector("span");
    if (span) span.textContent = "辅助等级：" + levelLabel(levelCap);
    var active = levelCap !== "auto";
    pill.classList.toggle("eg-option-pill-on", active);
    pill.setAttribute("aria-pressed", String(active));
  }

  function setLevel(value) {
    levelCap = value;
    var pill = document.querySelector(".eg-level-pill");
    if (pill) updateLevelPill(pill);
    fetch("/chat/options", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify({ help_level_cap: value }),
    }).catch(function () {});
  }

  function pillsContainer() {
    var composer = document.getElementById("message-composer");
    if (!composer) return null;
    var existing = composer.querySelector(".eg-option-pills");
    if (existing && existing.isConnected) return existing;
    // 附件按钮是 composer 里带纸别针图标的行内按钮（首个 button 可能是
    // 其它控件）；pills 插到它右边、同行内联，不占独立一行。
    var attach = [...composer.querySelectorAll("button")].find(function (b) {
      var path = b.querySelector("svg path");
      return path && /M9 7C9 4/.test(path.getAttribute("d") || "");
    });
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
    wrap.append(levelPill());
    attach.insertAdjacentElement("afterend", wrap);
    syncPills(wrap);
    return wrap;
  }

  function syncPills(wrap) {
    (wrap || document).querySelectorAll(".eg-option-pill").forEach(function (pill) {
      if (pill.dataset.option === "help_level_cap") return; // 独立状态
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
        if (prefs.help_level_cap !== undefined) {
          levelCap = prefs.help_level_cap === null ? "auto" : prefs.help_level_cap;
          var levelPillEl = document.querySelector(".eg-level-pill");
          if (levelPillEl) updateLevelPill(levelPillEl);
        }
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

  // 能力感知显隐：当前模型不支持思考/联网时隐藏对应 pill。
  window.addEventListener(
    "eg-model-changed",
    function (event) {
      var caps = (event.detail && event.detail.capabilities) || {};
      var wrap = pillsContainer();
      if (!wrap) return;
      wrap.querySelectorAll(".eg-option-pill").forEach(function (pill) {
        if (pill.dataset.option === "help_level_cap") return; // 恒可用
        var key = pill.dataset.option;
        var supported =
          key === "deep_think" ? !!caps.thinking : !!caps.web_search;
        pill.style.display = supported ? "" : "none";
      });
    },
    false
  );
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
