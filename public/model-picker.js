/* Thread-local model selector. The browser only sees the server-authorized
   catalog; provider credentials and authorization remain server-side. */
(function () {
  "use strict";

  var state = {
    threadId: "",
    currentSelection: "",
    currentLabel: "",
    providers: [],
    loading: false,
    switching: false,
    loaded: false,
  };
  var button = null;
  var menu = null;
  var searchInput = null;
  var listElement = null;
  var statusElement = null;
  var query = "";

  function currentThreadId() {
    if (state.threadId) return state.threadId;
    var match = location.pathname.match(/^\/thread\/([^/]+)/);
    return match ? decodeURIComponent(match[1]) : "";
  }

  function safeModelLabel(selection) {
    var provider = state.providers.find(function (group) {
      return group.models.some(function (model) {
        return model.selection === selection;
      });
    });
    if (!provider) return selection;
    var model = provider.models.find(function (item) {
      return item.selection === selection;
    });
    return model ? model.model : selection;
  }

  function normalizeCatalog(data) {
    if (!data || typeof data !== "object") return;
    state.threadId = data.thread_id || currentThreadId();
    state.currentSelection = data.current_selection || state.currentSelection;
    state.currentLabel = data.current_label || safeModelLabel(state.currentSelection);
    state.providers = Array.isArray(data.providers) ? data.providers : [];
    state.loaded = true;
    updateButton();
    renderMenu();
  }

  function applyServerState(data) {
    if (!data || typeof data !== "object") return;
    state.threadId = data.thread_id || state.threadId;
    state.currentSelection = data.current_selection || state.currentSelection;
    state.currentLabel = data.current_label || safeModelLabel(state.currentSelection);
    state.providers = Array.isArray(data.providers) ? state.providers : state.providers;
    state.loaded = state.providers.length > 0 || state.loaded;
    updateButton();
    renderMenu();
  }

  function updateButton() {
    if (!button) return;
    var label = button.querySelector(".eg-model-name");
    if (label) {
      label.textContent = state.currentLabel || state.currentSelection || "选择模型";
      button.title = state.currentLabel || "选择模型";
    }
    button.setAttribute("aria-label", `当前模型：${state.currentLabel || "选择模型"}`);
    button.classList.toggle("eg-model-loading", Boolean(state.loading));
    button.disabled = Boolean(state.switching);
  }

  function ensureComposerControl() {
    var composer = document.getElementById("message-composer");
    if (!composer) return;
    var actionRow = composer.querySelector(":scope > .flex.items-center.justify-between");
    var rightGroup = actionRow?.querySelector(":scope > .flex.items-center:last-child");
    var submit = document.getElementById("chat-submit");
    if (!actionRow || !rightGroup || !submit) return;
    // A framework hydration pass can temporarily detach the action row.
    // Wait for the next mutation rather than inserting into a stale parent.
    if (submit.parentElement !== rightGroup || !rightGroup.isConnected || !composer.isConnected) return;
    if (button && button.isConnected && rightGroup.contains(button)) return;

    button = document.createElement("button");
    button.type = "button";
    button.id = "model-selector-button";
    button.className = "eg-model-button";
    button.setAttribute("aria-haspopup", "listbox");
    button.setAttribute("aria-expanded", "false");
    button.innerHTML = `
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true">
        <rect x="4" y="4" width="16" height="16" rx="3"/>
        <path d="M9 9h6v6H9z"/>
        <path d="M12 2v2M12 20v2M2 12h2M20 12h2"/>
      </svg>
      <span class="eg-model-name">选择模型</span>
      <svg class="eg-model-chevron" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
        <path d="m6 9 6 6 6-6"/>
      </svg>
    `;
    button.addEventListener("click", function (event) {
      event.preventDefault();
      event.stopPropagation();
      toggleMenu();
    });
    rightGroup.insertBefore(button, submit);
    updateButton();
  }

  function closeMenu() {
    if (!menu) return;
    menu.remove();
    menu = null;
    searchInput = null;
    listElement = null;
    statusElement = null;
    if (button) button.setAttribute("aria-expanded", "false");
  }

  function menuPosition() {
    if (!button) return;
    var rect = button.getBoundingClientRect();
    var width = Math.max(260, Math.min(360, window.innerWidth - 24));
    var left = Math.min(Math.max(12, rect.right - width), window.innerWidth - width - 12);
    var estimatedHeight = Math.min(360, window.innerHeight - 24);
    var top = rect.top - estimatedHeight - 8;
    if (top < 12) top = Math.min(rect.bottom + 8, window.innerHeight - estimatedHeight - 12);
    menu.style.left = `${Math.round(left)}px`;
    menu.style.top = `${Math.round(Math.max(12, top))}px`;
    menu.style.width = `${Math.round(width)}px`;
  }

  function visibleProviders() {
    var queryText = query.trim().toLowerCase();
    return state.providers
      .map(function (group) {
        var models = group.models.filter(function (model) {
          return !queryText
            || group.label.toLowerCase().includes(queryText)
            || model.model.toLowerCase().includes(queryText);
        });
        return Object.assign({}, group, { models });
      })
      .filter(function (group) {
        return group.models.length > 0;
      });
  }

  function renderMenu() {
    if (!menu || !listElement) return;
    var groups = visibleProviders();
    listElement.textContent = "";
    if (!state.providers.length) {
      var empty = document.createElement("div");
      empty.className = "eg-model-menu-empty";
      empty.textContent = "暂无可用模型";
      listElement.append(empty);
      return;
    }
    if (!groups.length) {
      var noMatch = document.createElement("div");
      noMatch.className = "eg-model-menu-empty";
      noMatch.textContent = "没有匹配的模型";
      listElement.append(noMatch);
      return;
    }
    groups.forEach(function (group) {
      var heading = document.createElement("div");
      heading.className = "eg-model-group-label";
      heading.textContent = group.label;
      listElement.append(heading);
      group.models.forEach(function (model) {
        var option = document.createElement("button");
        option.type = "button";
        option.className = "eg-model-option";
        option.setAttribute("role", "option");
        option.setAttribute("aria-selected", String(model.selection === state.currentSelection));
        option.disabled = !model.available || state.switching;
        option.innerHTML = `
          <span class="eg-model-option-name">${model.model}</span>
          <span class="eg-model-option-state">${model.selection === state.currentSelection ? "✓" : model.available ? "" : "不可用"}</span>
        `;
        option.addEventListener("click", function () {
          selectModel(model.selection);
        });
        listElement.append(option);
      });
    });
  }

  function toggleMenu() {
    if (menu) {
      closeMenu();
      return;
    }
    ensureComposerControl();
    menu = document.createElement("div");
    menu.className = "eg-model-menu";
    menu.setAttribute("role", "listbox");
    menu.setAttribute("aria-label", "选择模型");
    menu.innerHTML = `
      <div class="eg-model-menu-header">
        <div class="eg-model-menu-title">选择模型</div>
      </div>
    `;
    if (state.providers.reduce(function (total, group) { return total + group.models.length; }, 0) > 0) {
      searchInput = document.createElement("input");
      searchInput.type = "search";
      searchInput.className = "eg-model-search";
      searchInput.placeholder = "搜索模型或 Provider";
      searchInput.setAttribute("aria-label", "搜索模型或 Provider");
      searchInput.addEventListener("input", function () {
        query = searchInput.value;
        renderMenu();
      });
      menu.append(searchInput);
    }
    listElement = document.createElement("div");
    listElement.className = "eg-model-list";
    menu.append(listElement);
    statusElement = document.createElement("div");
    statusElement.className = "eg-model-menu-status";
    statusElement.setAttribute("aria-live", "polite");
    menu.append(statusElement);
    document.body.append(menu);
    menuPosition();
    button?.setAttribute("aria-expanded", "true");
    renderMenu();
    searchInput?.focus();
  }

  function setStatus(message, isError) {
    if (!statusElement) return;
    statusElement.textContent = message || "";
    statusElement.classList.toggle("eg-model-error", Boolean(isError));
  }

  async function loadCatalog() {
    var threadId = currentThreadId();
    var url = "/models/catalog";
    if (threadId) url += `?thread_id=${encodeURIComponent(threadId)}`;
    var response = await fetch(url, {
      credentials: "same-origin",
      headers: { Accept: "application/json" },
    });
    if (!response.ok) throw new Error(`目录加载失败（${response.status}）`);
    normalizeCatalog(await response.json());
  }

  async function selectModel(selection) {
    if (state.switching || selection === state.currentSelection) {
      closeMenu();
      return;
    }
    state.switching = true;
    updateButton();
    renderMenu();
    setStatus("正在切换模型…");
    var threadId = currentThreadId();
    try {
      var response = await fetch("/models/select", {
        method: "POST",
        credentials: "same-origin",
        headers: {
          "Content-Type": "application/json",
          Accept: "application/json",
        },
        body: JSON.stringify({ thread_id: threadId, selection }),
      });
      var payload = await response.json().catch(function () { return {}; });
      if (!response.ok) throw new Error(payload.detail || `切换失败（${response.status}）`);
      state.currentSelection = payload.selection || selection;
      state.currentLabel = payload.label || safeModelLabel(selection);
      updateButton();
      renderMenu();
      setStatus("模型已切换", false);
      window.setTimeout(closeMenu, 500);
    } catch (error) {
      state.switching = false;
      updateButton();
      renderMenu();
      setStatus(error.message || "切换失败，已保留原模型", true);
    }
  }

  window.addEventListener("message", function (event) {
    var data = event.data;
    if (!data || typeof data !== "object") return;
    if (data.type === "algosage-model-state") applyServerState(data);
    if (data.type === "algosage-thread-state") {
      state.threadId = data.thread_id || state.threadId;
    }
  });

  document.addEventListener("click", function (event) {
    if (menu && !menu.contains(event.target) && !button?.contains(event.target)) closeMenu();
  });
  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape" && menu) {
      event.stopPropagation();
      closeMenu();
      button?.focus();
    }
  });
  window.addEventListener("resize", function () {
    if (menu) menuPosition();
  });
  window.addEventListener("scroll", function () {
    if (menu) menuPosition();
  }, true);

  new MutationObserver(function () {
    ensureComposerControl();
  }).observe(document.body, { childList: true, subtree: true });

  async function initialLoad() {
    try {
      await loadCatalog();
    } catch (_) {
      // The server also pushes authorized state after chat initialization.
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () {
      ensureComposerControl();
      initialLoad();
    });
  } else {
    ensureComposerControl();
    initialLoad();
  }
})();
