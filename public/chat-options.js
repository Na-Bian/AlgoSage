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
    { value: "auto", label: "自动" },
    { value: 1, label: "L1" },
    { value: 2, label: "L2" },
    { value: 3, label: "L3" },
  ];
  var levelCap = null; // null = 尚未载入；"auto"/1/2/3
  var levelMenu = null;
  var levelPillEl = null;

  function levelLabel(value) {
    for (var i = 0; i < LEVELS.length; i++) {
      if (String(LEVELS[i].value) === String(value)) return LEVELS[i].label;
    }
    return "自动";
  }

  function closeLevelMenu() {
    if (!levelMenu) return;
    var closing = levelMenu;
    levelMenu = null;
    if (levelPillEl) levelPillEl.setAttribute("aria-expanded", "false");
    // 与模型菜单一致：播放退出动画后再移除。
    var duration = 0;
    try {
      duration = parseFloat(getComputedStyle(closing).animationDuration) || 0;
      closing.classList.add("eg-menu-closing");
    } catch (_) {}
    var remove = function () { closing.remove(); };
    if (duration > 0) setTimeout(remove, duration * 1000 + 30);
    else remove();
  }

  function toggleLevelMenu(pill) {
    if (levelMenu) {
      closeLevelMenu();
      return;
    }
    levelPillEl = pill;
    var menu = document.createElement("div");
    menu.className = "eg-model-menu";
    menu.setAttribute("role", "listbox");
    menu.setAttribute("aria-label", "辅助等级");
    menu.innerHTML =
      '<div class="eg-model-menu-header"><div class="eg-model-menu-title">辅助等级</div></div>';
    var list = document.createElement("div");
    list.className = "eg-model-list";
    LEVELS.forEach(function (level) {
      var option = document.createElement("button");
      option.type = "button";
      option.className = "eg-model-option";
      option.setAttribute("role", "option");
      var selected = String(level.value) === String(levelCap);
      option.setAttribute("aria-selected", String(selected));
      option.innerHTML =
        '<span class="eg-model-option-name">' + level.label + "</span>" +
        '<span class="eg-model-option-state">' + (selected ? "✓" : "") + "</span>";
      option.addEventListener("click", function (event) {
        event.stopPropagation();
        setLevel(level.value);
        closeLevelMenu();
      });
      list.append(option);
    });
    menu.append(list);
    document.body.append(menu);
    // 定位与模型菜单同款：锚点上方 8px，放不下落到下方。
    var rect = pill.getBoundingClientRect();
    var width = Math.max(220, Math.min(300, window.innerWidth - 24));
    menu.style.maxHeight = Math.min(320, window.innerHeight - 24) + "px";
    var left = Math.min(Math.max(12, rect.left), window.innerWidth - width - 12);
    var top = rect.top - (menu.offsetHeight || 200) - 8;
    if (top < 12) top = Math.min(rect.bottom + 8, window.innerHeight - 220);
    menu.style.left = Math.round(left) + "px";
    menu.style.top = Math.round(Math.max(12, top)) + "px";
    menu.style.width = Math.round(width) + "px";
    levelMenu = menu;
    pill.setAttribute("aria-expanded", "true");
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
    var shown = levelCap === null ? "自动" : levelLabel(levelCap);
    if (span) span.textContent = shown;
    var active = levelCap !== null && levelCap !== "auto";
    pill.classList.toggle("eg-option-pill-on", active);
    pill.setAttribute("aria-pressed", String(active));
  }

  function currentThreadId() {
    var match = location.pathname.match(/^\/thread\/([0-9a-f-]{16,})/i);
    return match ? match[1] : "";
  }

  function setLevel(value) {
    levelCap = value;
    var pill = document.querySelector(".eg-level-pill");
    if (pill) updateLevelPill(pill);
    var threadId = currentThreadId();
    fetch("/chat/options", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify(
        threadId
          ? { help_level_cap: value, scope: "thread", thread_id: threadId }
          : { help_level_cap: value, help_level_cap_default: value }
      ),
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
        syncPills();
        // 有效辅助等级 = 当前对话覆盖 ?? 账号默认（"默认辅助等级"设置）。
        fetch("/chat/options?thread_id=" + encodeURIComponent(currentThreadId()), {
          credentials: "same-origin",
          headers: { Accept: "application/json" },
        })
          .then(function (response) { return response.json(); })
          .then(function (opts) {
            if (opts && "help_level_cap" in opts) {
              levelCap = opts.help_level_cap === null ? "auto" : opts.help_level_cap;
              var levelPillEl = document.querySelector(".eg-level-pill");
              if (levelPillEl) updateLevelPill(levelPillEl);
            }
          })
          .catch(function () {});
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
