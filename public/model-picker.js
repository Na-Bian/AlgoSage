/* Thread-local model selector. The browser only sees the server-authorized
   catalog; provider credentials and authorization remain server-side.
   Users may additionally register their own OpenAI-compatible endpoint
   (BYOK): the key never stays in the browser — after registration the
   server keeps it (runtime memory + Fernet-sealed copy in the local .env
   managed block) and never echoes it back. */
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
  var addDialog = null;
  var query = "";

  function currentThreadId() {
    if (state.threadId) return state.threadId;
    var match = location.pathname.match(/^\/thread\/([^/]+)/);
    return match ? decodeURIComponent(match[1]) : "";
  }

  function shortModelName(modelId) {
    // 去掉组织前缀（Qwen/Qwen3-8B → Qwen3-8B）。
    return String(modelId || "").split("/").pop().trim();
  }

  function modelCapabilities(selection) {
    var found = null;
    state.providers.forEach(function (group) {
      group.models.forEach(function (model) {
        if (model.selection === selection) found = model;
      });
    });
    return (found && found.capabilities) || null;
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
    return model ? shortModelName(model.model) : selection;
  }

  function normalizeCatalog(data) {
    if (!data || typeof data !== "object") return;
    state.threadId = data.thread_id || currentThreadId();
    state.currentSelection = data.current_selection || state.currentSelection;
    state.currentLabel = data.current_label || safeModelLabel(state.currentSelection);
    state.providers = Array.isArray(data.providers) ? data.providers : [];
    state.loaded = true;
    updateButton();
    syncNoModelState();
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
    syncNoModelState();
    renderMenu();
  }

  function hasUsableModels() {
    return state.providers.some(function (group) {
      return !group.hidden && group.models.length > 0;
    });
  }

  function syncNoModelState() {
    var none = state.loaded && !hasUsableModels();
    document.body.classList.toggle("eg-no-models", none);
    if (none && button) {
      button.classList.add("eg-model-none");
    } else if (button) {
      button.classList.remove("eg-model-none");
    }
    var caps = modelCapabilities(state.currentSelection);
    window.dispatchEvent(
      new CustomEvent("eg-model-changed", { detail: { capabilities: caps } })
    );
  }

  function updateButton() {
    if (!button) return;
    var none = state.loaded && !hasUsableModels();
    var label = button.querySelector(".eg-model-name");
    if (label) {
      label.textContent = none
        ? "未配置模型"
        : state.currentLabel || state.currentSelection || "选择模型";
      button.title = none ? "尚未添加模型提供商，点击添加" : state.currentLabel || "选择模型";
    }
    button.setAttribute(
      "aria-label",
      none ? "未配置模型：点击添加" : `当前模型：${state.currentLabel || "选择模型"}`
    );
    button.classList.toggle("eg-model-loading", Boolean(state.loading));
    button.disabled = Boolean(state.switching);
    publishModelLabel(none ? "未配置模型" : state.currentLabel || state.currentSelection || "选择模型");
  }

  /* 重挂载窗口里真按钮缺位时，占位伪元素靠这个变量显示同样的模型名，
     否则用户会看到模型名凭空变成"选择模型"，观感等同于按钮消失。 */
  function publishModelLabel(labelText) {
    try {
      window.localStorage.setItem("algosage-model-label", labelText);
    } catch (_) {}
    document.documentElement.style.setProperty("--eg-model-label", `"${labelText}"`);
  }
  try {
    document.documentElement.style.setProperty(
      "--eg-model-label",
      `"${window.localStorage.getItem("algosage-model-label") || "选择模型"}"`
    );
  } catch (_) {}

  var retryScheduled = false;
  var retryBudget = 0;
  var lastComposer = null;

  /* UI 时序埋点：composer 注入的毫秒级时间线上报到 /debug/ui-trace（服务端
     只写日志）。用于远程诊断"设置按钮在重挂载窗口消失"类帧时序问题。
     默认关闭——生成期间每次 DOM 变动都会记一条，常开会持续刷请求和日志；
     排查时在浏览器控制台执行 localStorage.setItem("algosage-ui-trace", "1")。 */
  var traceEnabled = false;
  try { traceEnabled = localStorage.getItem("algosage-ui-trace") === "1"; } catch (_) {}
  var traceBuffer = [];
  var traceTimer = 0;
  function trace(name, detail) {
    if (!traceEnabled) return;
    traceBuffer.push({ t: Math.round(performance.now()), name: name, d: detail || "" });
    if (traceTimer) return;
    traceTimer = window.setTimeout(flushTrace, 1200);
  }
  function flushTrace() {
    traceTimer = 0;
    if (!traceBuffer.length) return;
    var events = traceBuffer.splice(0, traceBuffer.length);
    var body = JSON.stringify({ events: events });
    try {
      if (navigator.sendBeacon
        && navigator.sendBeacon("/debug/ui-trace", new Blob([body], { type: "application/json" }))) {
        return;
      }
    } catch (_) {}
    fetch("/debug/ui-trace", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: body,
      keepalive: true,
    }).catch(function () {});
  }

  function scheduleComposerRetry() {
    // 水合期会把动作行短暂拆散/换父，此时注入必然失败。MutationObserver
    // 要等下一次 DOM 变动才会再跑，中间可能隔着多帧绘制——按钮肉眼可见地
    // 消失。rAF 回调在本帧绘制前执行，紧跟着重试最多再等一帧。
    if (retryScheduled || retryBudget > 30) return;
    retryScheduled = true;
    retryBudget += 1;
    requestAnimationFrame(function () {
      retryScheduled = false;
      ensureComposerControl();
    });
  }

  function ensureComposerControl() {
    var composer = document.getElementById("message-composer");
    if (!composer) return;
    if (composer !== lastComposer) {
      // 新一轮挂载重新给重试预算；同一节点只配额一次，防永久性失败空转。
      lastComposer = composer;
      retryBudget = 0;
      trace("composer-mount");
    }
    var actionRow = composer.querySelector(":scope > .flex.items-center.justify-between");
    var rightGroup = actionRow?.querySelector(":scope > .flex.items-center:last-child");
    var submit = document.getElementById("chat-submit");
    if (button && button.isConnected && rightGroup && rightGroup.contains(button)) {
      retryBudget = 0;
      return;
    }
    // A framework hydration pass can temporarily detach the action row.
    // Retry on the next animation frame instead of waiting for an
    // unrelated mutation to re-trigger this observer.
    if (!actionRow || !rightGroup || !submit
      || submit.parentElement !== rightGroup || !rightGroup.isConnected || !composer.isConnected) {
      trace("inject-defer",
        !actionRow ? "no-row" : !rightGroup ? "no-group"
          : !submit ? "no-submit" : submit.parentElement !== rightGroup ? "submit-detached"
            : !rightGroup.isConnected ? "group-detached" : "composer-detached");
      scheduleComposerRetry();
      return;
    }

    trace("injected", "budget=" + retryBudget);
    button = document.createElement("button");
    button.type = "button";
    button.id = "model-selector-button";
    button.className = "eg-model-button";
    button.setAttribute("aria-haspopup", "listbox");
    button.setAttribute("aria-expanded", "false");
    // 只保留模型名称与下拉箭头（用户偏好：无图标、无框、无阴影）。
    button.innerHTML = `
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
    // 辅助等级控件（chat-options.js 挂在同一个 rightGroup）保持在模型
    // 按钮左侧：顺序为 等级钮、模型钮、发送键。必须先插入模型钮，才能以它
    // 为参照放等级钮——参照节点不在 rightGroup 里时 insertBefore 会抛错，
    // 导致输入区重挂载后模型钮再也插不回去。
    rightGroup.insertBefore(button, submit);
    const levelControl = document.querySelector(".eg-level-pill");
    if (levelControl) rightGroup.insertBefore(levelControl, button);
    updateButton();
  }

  function closeMenu() {
    if (!menu) return;
    var closing = menu;
    menu = null;
    searchInput = null;
    listElement = null;
    statusElement = null;
    if (button) button.setAttribute("aria-expanded", "false");
    // 播放退出动画后再移除；动画不可用时立即清理。
    var duration = 0;
    try {
      duration = parseFloat(getComputedStyle(closing).animationDuration) || 0;
      closing.classList.add("eg-menu-closing");
    } catch (_) {}
    var remove = function () { closing.remove(); };
    if (duration > 0) setTimeout(remove, duration * 1000 + 30);
    else remove();
  }

  function menuPosition() {
    if (!button || !menu) return;
    var rect = button.getBoundingClientRect();
    var width = Math.max(260, Math.min(360, window.innerWidth - 24));
    var maxHeight = Math.min(360, window.innerHeight - 24);
    menu.style.maxHeight = `${Math.round(maxHeight)}px`;
    var left = Math.min(Math.max(12, rect.right - width), window.innerWidth - width - 12);
    // 紧贴锚点：优先在按钮上方 8px；上方放不下（含菜单实际高度）则落到下方。
    var menuHeight = menu.offsetHeight || maxHeight;
    var top = rect.top - menuHeight - 8;
    if (top < 12) top = Math.min(rect.bottom + 8, window.innerHeight - menuHeight - 12);
    menu.style.left = `${Math.round(left)}px`;
    menu.style.top = `${Math.round(Math.max(12, top))}px`;
    menu.style.width = `${Math.round(width)}px`;
  }

  function visibleProviders() {
    var queryText = query.trim().toLowerCase();
    var groups = state.providers
      .filter(function (group) { return !group.hidden; })
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
    // 自定义模型按提供商显示名称拆分成独立分组（组名如"我的 SiliconFlow"）。
    var expanded = [];
    groups.forEach(function (group) {
      if (group.id !== "custom") {
        expanded.push(group);
        return;
      }
      var byLabel = {};
      group.models.forEach(function (model) {
        var label = model.label || group.label;
        (byLabel[label] = byLabel[label] || []).push(model);
      });
      Object.keys(byLabel).forEach(function (label) {
        expanded.push(
          Object.assign({}, group, { label: label, models: byLabel[label] })
        );
      });
    });
    return expanded;
  }

  function renderMenu() {
    if (!menu || !listElement) return;
    var groups = visibleProviders();
    listElement.textContent = "";
    if (!state.providers.length || !hasUsableModels()) {
      var empty = document.createElement("div");
      empty.className = "eg-model-menu-empty";
      empty.textContent = "尚未添加模型提供商，添加你的 API 提供商后即可开始对话。";
      listElement.append(empty);
      var addBtn = document.createElement("button");
      addBtn.type = "button";
      addBtn.className = "eg-model-add";
      addBtn.innerHTML = `
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" aria-hidden="true">
          <path d="M12 5v14M5 12h14"/>
        </svg>
        添加模型提供商
      `;
      addBtn.addEventListener("click", function () {
        closeMenu();
        openAddDialog();
      });
      listElement.append(addBtn);
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
        // 自定义模型行附加能力文字（思考 · 多模态 · 联网）。
        var capText = "";
        if (group.id === "custom") {
          var optionCaps = model.capabilities || {};
          var optionTags = [];
          if (optionCaps.thinking) optionTags.push("思考");
          if (optionCaps.multimodal) optionTags.push("多模态");
          if (optionCaps.web_search) optionTags.push("联网");
          capText = optionTags.join(" · ");
        }
        var capHtml = capText
          ? `<span class="eg-model-caps">${optionTags
              .map(function (tag) {
                return `<span>${tag}</span>`;
              })
              .join("")}</span>`
          : "";
        option.innerHTML = `
          <span class="eg-model-option-name">${model.short_name || shortModelName(model.model)}${capHtml}</span>
          <span class="eg-model-option-state">${model.selection === state.currentSelection ? "✓" : model.available ? "" : "不可用"}</span>
        `;
        option.addEventListener("click", function () {
          selectModel(model.selection);
        });
        // 删除入口统一在「管理模型提供商」对话框，菜单里不放删除按钮避免误触。
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
    // renderMenu 之后列表高度才确定；下一帧按实际高度重新定位。
    requestAnimationFrame(menuPosition);
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

  async function selectModel(selection, options) {
    var force = Boolean(options && options.force);
    if (!selection || state.switching || (selection === state.currentSelection && !force)) {
      closeMenu();
      return;
    }
    state.switching = true;
    updateButton();
    renderMenu();
    setStatus("正在切换模型…");
    // 欢迎页（尚无线程归属）：选择落为账号级默认，让即将创建的新对话继承；
    // state.threadId 此时残留上一个线程的 id，绑定到它对新对话无效。
    var threadId = document.getElementById("welcome-screen")
      ? ""
      : currentThreadId();
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
      state.switching = false;
      updateButton();
      syncNoModelState();
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

  async function deleteCustomSelection(selection) {
    if (state.switching) return false;
    state.switching = true;
    try {
      var response = await fetch("/models/custom", {
        method: "DELETE",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({ selection }),
      });
      if (!response.ok) throw new Error(`删除失败（${response.status}）`);
      if (state.currentSelection === selection) {
        // selectModel refuses to run while a switch is in flight; release the
        // removal lock so the fallback switch back to a builtin model executes.
        state.switching = false;
        await selectModel(defaultSelection(), { force: true });
      }
      // 两个分支都要刷新目录：管理对话框的列表与回退后的标签都依赖它。
      await loadCatalog();
      // 设置面板的"默认模型"列表同样要去掉被删的模型（被删的若是账号
      // 默认，服务端会回退到剩余的第一个模型）。
      requestProviderViewsRefresh();
      return true;
    } finally {
      state.switching = false;
      renderMenu();
    }
  }

  function defaultSelection() {
    var group = state.providers.find(function (item) { return item.id !== "custom"; });
    return (group && group.models[0] && group.models[0].selection) || "";
  }

  /* 设置面板入口："管理模型提供商"挂在"默认模型"控件下方。
     无可用提供商时"默认模型"控件不渲染，入口改挂到设置面板标题下方，
     保证删除全部提供商后仍能从这里重新添加。 */
  function entryHTML() {
    return `
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
        <path d="M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 0-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2z"/>
        <circle cx="12" cy="12" r="3"/>
      </svg>
      管理模型提供商
    `;
  }

  function bindEntry(entry) {
    entry.addEventListener("click", function (event) {
      event.preventDefault();
      event.stopPropagation();
      openAddDialog();
    });
  }

  function ensureSettingsAddEntry() {
    if (!document.getElementById("chat-settings-sidebar-title")) return;
    var label = document.querySelector('aside label[for="model_catalog"]');
    if (!label) {
      // 无"默认模型"控件（无任何提供商）：入口插到控件滚动容器的首位，
      // 与各控件包装块同级同宽——插在标题旁或外层容器会大出一圈边距。
      var anchor = document.getElementById("chat-settings-sidebar-title");
      var headerBlock = anchor && anchor.closest("div");
      var panel = anchor && (anchor.closest('[role="dialog"]') || anchor.closest("[data-panel]"));
      if (!headerBlock || !panel) return;
      var body = headerBlock.nextElementSibling;
      if (!(body && body.querySelector("label"))) return;
      // 控件真正的父级：body 的直接子元素中包含控件包装的那个。
      var container = body.querySelector("label").closest("div");
      while (container && container.parentElement !== body) {
        container = container.parentElement;
      }
      if (!container) return;
      // 本函数由全局 MutationObserver 驱动，插入本身又会触发观察器：
      // 不检查已存在就会"插入→触发→再插入"无限微任务循环，页面卡死。
      var entry = document.getElementById("eg-settings-add-model");
      if (
        entry
        && entry.parentElement === container
        && container.firstElementChild === entry
      ) {
        return;
      }
      if (!entry) {
        entry = document.createElement("button");
        entry.type = "button";
        entry.id = "eg-settings-add-model";
        entry.className = "eg-settings-add";
        entry.innerHTML = entryHTML();
        bindEntry(entry);
      }
      container.insertBefore(entry, container.firstChild);
      return;
    }
    var wrapper = label.closest("div.space-y-2") || label.parentElement;
    if (!wrapper || !wrapper.parentElement) return;
    var existing = document.getElementById("eg-settings-add-model");
    if (existing && existing.parentElement === wrapper.parentElement) {
      // React 重渲染可能重建控件包装；错位时把入口挪回控件正后方。
      if (existing.previousElementSibling !== wrapper) {
        wrapper.insertAdjacentElement("afterend", existing);
      }
      return;
    }
    var entry = document.createElement("button");
    entry.type = "button";
    entry.id = "eg-settings-add-model";
    entry.className = "eg-settings-add";
    entry.innerHTML = entryHTML();
    bindEntry(entry);
    wrapper.insertAdjacentElement("afterend", entry);
  }

  function closeAddDialog() {
    if (!addDialog) return;
    addDialog.remove();
    addDialog = null;
  }

  function openAddDialog() {
    closeAddDialog();
    addDialog = document.createElement("div");
    addDialog.className = "eg-add-dialog-backdrop";
    addDialog.innerHTML = `
      <div class="eg-add-dialog" role="dialog" aria-label="管理模型提供商">
        <div class="eg-add-title">管理模型提供商</div>
        <div class="eg-add-managed">
          <div class="eg-add-managed-head">提供商 <span data-managed-count></span></div>
          <div data-managed-list></div>
        </div>
        <div data-removed-list></div>
        <div class="eg-add-divider">添加提供商</div>
        <div class="eg-add-field">预设<span class="eg-add-presets" data-presets><span class="eg-add-presets-loading">加载中…</span></span></div>
        <label class="eg-add-field">显示名称（可选）<input type="text" data-field="label" placeholder="例如：我的 SiliconFlow" maxlength="40"></label>
        <label class="eg-add-field">Base URL<input type="text" data-field="base_url" placeholder="https://api.siliconflow.cn/v1"></label>
        <label class="eg-add-field">API Key<input type="password" data-field="api_key" placeholder="sk-…" autocomplete="off"></label>
        <div class="eg-add-actions">
          <button type="button" class="eg-add-secondary" data-action="cancel">取消</button>
          <button type="button" class="eg-add-secondary" data-action="discover">获取模型列表</button>
          <button type="button" class="eg-add-primary" data-action="confirm">确认</button>
        </div>
        <div class="eg-add-results" hidden></div>
      </div>
    `;
    document.body.append(addDialog);
    // 不做"点空白关闭"：填到一半的 Base URL/API Key 不该被一次误点清空。
    // 关闭只走"取消"/"确认"按钮或 Esc。
    addDialog.addEventListener("click", function (event) {
      if (event.target === addDialog) {
        event.preventDefault();
        event.stopPropagation();
      }
    });
    addDialog.querySelector('[data-action="cancel"]').addEventListener("click", closeAddDialog);
    // 确认：关闭对话框，并请服务端重发设置，让面板的"默认模型"列表包含
    // 刚增删的模型。不能借即时保存的提交来触发：没有控件改动时它不会发出
    // 任何请求，列表就停留在旧值，只有刷新页面才更新。
    addDialog.querySelector('[data-action="confirm"]').addEventListener("click", function () {
      closeAddDialog();
      requestProviderViewsRefresh();
    });
    addDialog.querySelector('[data-action="discover"]').addEventListener("click", function () {
      discoverInDialog();
    });
    renderManagedModels();
    // 拉一次目录以带上 label；完成后重渲染列表。
    loadCatalog().then(function () {
      if (addDialog) renderManagedModels();
    }).catch(function () {});
    // 预设：点击即预填 Base URL 与显示名，用户只需补 API Key。
    renderPresetChips();
    var first = addDialog.querySelector('[data-field="label"]');
    if (first) first.focus();
  }

  var presetsCache = null;

  function renderPresetChips() {
    var host = addDialog && addDialog.querySelector("[data-presets]");
    if (!host) return;
    var apply = function (presets) {
      if (!addDialog) return;
      host.textContent = "";
      presets.forEach(function (preset) {
        var chip = document.createElement("button");
        chip.type = "button";
        chip.className = "eg-add-preset";
        chip.textContent = preset.label;
        chip.title = preset.hint + " ｜ " + preset.base_url;
        chip.addEventListener("click", function () {
          setField("base_url", preset.base_url);
          setField("label", preset.label);
          host.querySelectorAll(".eg-add-preset").forEach(function (other) {
            other.classList.remove("eg-add-preset-on");
          });
          chip.classList.add("eg-add-preset-on");
          // Base URL 已填好，直接把焦点交给 API Key。
          var keyInput = addDialog.querySelector('[data-field="api_key"]');
          if (keyInput) keyInput.focus();
        });
        host.append(chip);
      });
    };
    if (presetsCache) {
      apply(presetsCache);
      return;
    }
    fetch("/models/presets", {
      credentials: "same-origin",
      headers: { Accept: "application/json" },
    })
      .then(function (response) { return response.json(); })
      .then(function (payload) {
        presetsCache = payload.presets || [];
        apply(presetsCache);
      })
      .catch(function () {
        if (host) host.textContent = "预设加载失败，可手动填写";
      });
  }

  function setField(name, value) {
    var input = addDialog.querySelector('[data-field="' + name + '"]');
    if (!input) return;
    // 走原生 setter 触发框架可感知的 input 事件。
    var setter = Object.getOwnPropertyDescriptor(
      window.HTMLInputElement.prototype,
      "value"
    ).set;
    setter.call(input, value);
    input.dispatchEvent(new Event("input", { bubbles: true }));
  }

  function customEntries() {
    var group = state.providers.find(function (item) { return item.id === "custom"; });
    return (group && group.models) || [];
  }

  function presetGroups() {
    return state.providers.filter(function (item) { return item.id !== "custom"; });
  }

  // 提供商增删成功后请求服务端重发面板设置（HTTP 路由里没有 ws 上下文，
  // 服务端自身的推送发不出来；走 socket 事件在 ws 上下文里执行才有上下文）。
  function requestProviderViewsRefresh() {
    if (window.AlgoSageSocket) {
      window.AlgoSageSocket.emitEvent("algosage_refresh_provider_views");
    }
  }

  async function presetProviderAction(providerId, action) {
    var response = await fetch("/models/preset", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify({ provider_id: providerId, action: action }),
    });
    if (!response.ok) {
      var payload = await response.json().catch(function () { return {}; });
      var labels = { hide: "移除", restore: "恢复", delete: "删除" };
      throw new Error(payload.detail || `${labels[action] || "操作"}失败（${response.status}）`);
    }
    // 正在使用的提供商被移除时，先切回默认模型再刷新目录。
    if (action === "hide") {
      var stillSelected = state.providers.some(function (group) {
        return group.id === providerId && group.models.some(function (model) {
          return model.selection === state.currentSelection;
        });
      });
      if (stillSelected && state.currentSelection !== defaultSelection()) {
        await selectModel(defaultSelection(), { force: true });
      }
    }
    await loadCatalog();
    // 面板"默认模型"下拉由服务端经 socket 重发（send_provider_settings）。
    requestProviderViewsRefresh();
  }

  async function removeCustomEntries(selections) {
    for (var i = 0; i < selections.length; i++) {
      var done = await deleteCustomSelection(selections[i]);
      if (!done) throw new Error("正在切换模型，请稍后再试");
    }
    requestProviderViewsRefresh();
  }

  function managedRow(name, specs, chips) {
    var row = document.createElement("div");
    row.className = "eg-add-managed-row";
    var label = document.createElement("span");
    label.className = "eg-add-managed-name";
    label.textContent = name;
    label.title = name;
    row.append(label);
    if (chips && chips.length) {
      var chipsWrap = document.createElement("span");
      chipsWrap.className = "eg-add-managed-chips";
      chips.forEach(function (chipText) {
        var chip = document.createElement("span");
        chip.textContent = chipText;
        chipsWrap.append(chip);
      });
      row.append(chipsWrap);
    }
    var actions = document.createElement("span");
    actions.className = "eg-add-managed-actions";
    specs.forEach(function (options) {
      var action = document.createElement("button");
      action.type = "button";
      action.className = options.style === "primary"
        ? "eg-add-managed-restore"
        : "eg-add-managed-remove";
      action.textContent = options.text;
      if (options.disabled) {
        action.disabled = true;
        action.title = options.title || "";
      } else {
        action.addEventListener("click", async function () {
          action.disabled = true;
          try {
            await options.run();
            renderManagedModels();
          } catch (error) {
            // 失败恢复按钮可用，错误原因放进悬停提示。
            action.disabled = false;
            action.title = error.message || options.failed;
          }
        });
      }
      actions.append(action);
    });
    row.append(actions);
    return row;
  }

  function renderManagedModels() {
    if (!addDialog) return;
    var count = addDialog.querySelector("[data-managed-count]");
    if (count) count.remove();
    var presets = presetGroups();
    var customCount = customEntries().length;
    var list = addDialog.querySelector("[data-managed-list]");
    var removedList = addDialog.querySelector("[data-removed-list]");
    if (!list || !removedList) return;
    list.textContent = "";
    removedList.textContent = "";
    removedList.hidden = true;

    presets.forEach(function (group) {
      // 默认 Provider 是服务端兜底配置，不属于用户可管理的提供商，不展示。
      if (group.hidden || group.locked) return;
      list.append(managedRow(group.label, [{
        text: "移除", pending: "正在移除…", done: "已移除；它不再出现在模型选择里，可随时在下方恢复",
        failed: "移除失败",
        run: function () { return presetProviderAction(group.id, "hide"); },
      }]));
    });
    customEntries().forEach(function (model) {
      var caps = model.capabilities || {};
      var capTags = [];
      if (caps.thinking) capTags.push("思考");
      if (caps.multimodal) capTags.push("多模态");
      if (caps.web_search) capTags.push("联网");
      var name =
        (model.label && model.label !== "自定义 API"
          ? model.label + " · "
          : "") + shortModelName(model.model);
      list.append(managedRow(name, [{
        text: "删除", pending: "正在删除…", done: "已删除；若它正被使用，已自动切回默认模型",
        failed: "删除失败",
        run: function () { return removeCustomEntries([model.selection]); },
      }], capTags));
    });
    if (!presets.length && !customCount) {
      var empty = document.createElement("div");
      empty.className = "eg-add-more";
      empty.textContent = "没有可用的提供商，用下方表单添加。";
      list.append(empty);
    }

    var hiddenPresets = presets.filter(function (group) { return group.hidden; });
    if (hiddenPresets.length) {
      removedList.hidden = false;
      var head = document.createElement("div");
      head.className = "eg-add-managed-head";
      head.textContent = "已移除的预置提供商";
      removedList.append(head);
      hiddenPresets.forEach(function (group) {
        removedList.append(managedRow(group.label, [
          {
            text: "恢复", style: "primary", pending: "正在恢复…", done: "已恢复",
            failed: "恢复失败",
            run: function () { return presetProviderAction(group.id, "restore"); },
          },
          {
            text: "删除", pending: "正在删除…",
            done: "已删除；该提供商不再出现在列表中",
            failed: "删除失败",
            run: function () { return presetProviderAction(group.id, "delete"); },
          },
        ]));
      });
    }
  }

  function dialogFields() {
    var values = {};
    addDialog.querySelectorAll("[data-field]").forEach(function (input) {
      values[input.dataset.field] = input.value.trim();
    });
    return values;
  }

  async function discoverInDialog() {
    var fields = dialogFields();
    if (!fields.base_url || !fields.api_key) {
      showResultsNotice("请先填写 Base URL 和 API Key");
      return;
    }
    var results = addDialog.querySelector(".eg-add-results");
    results.hidden = false;
    results.textContent = "正在获取模型列表…";
    try {
      var response = await fetch("/models/discover", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify(fields),
      });
      var payload = await response.json().catch(function () { return {}; });
      if (!response.ok) throw new Error(payload.detail || `获取失败（${response.status}）`);
      renderDiscoveredModels(payload.models || [], fields, payload.limit_b);
    } catch (error) {
      showResultsNotice(error.message || "获取模型列表失败");
    }
  }

  var probedCapabilities = {};
  var probingModels = {};

  function renderRowCaps(row, capabilities, pending) {
    var capsNode = row.querySelector(".eg-add-model-caps");
    if (!capsNode) return;
    var tags = [];
    var unknown = false;
    if (pending) {
      tags.push("能力探测中…");
      unknown = true;
    } else if (capabilities) {
      if (capabilities.thinking === true) tags.push("思考");
      if (capabilities.multimodal === true) tags.push("多模态");
      if (capabilities.web_search === true) tags.push("联网");
      // 全部探测不明确（超时/上游异常）时给出弱反馈，不冒充“不支持”。
      if (!tags.length && ["thinking", "multimodal", "web_search"].some(function (key) {
        return capabilities[key] == null;
      })) {
        tags.push("能力未知");
        unknown = true;
      }
    }
    capsNode.classList.toggle("eg-caps-pending", unknown);
    capsNode.innerHTML = tags
      .map(function (tag) { return `<span>${tag}</span>`; })
      .join("");
  }

  function probeCapabilities(fields, item) {
    if (probedCapabilities[item.id]) return Promise.resolve();
    if (probingModels[item.id]) return probingModels[item.id];
    var row = [...document.querySelectorAll(".eg-add-model")].find(function (r) {
      return r.dataset.modelId === item.id;
    });
    if (row) renderRowCaps(row, null, true);
    var promise = fetch("/models/probe", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify({
        base_url: fields.base_url,
        api_key: fields.api_key,
        model: item.id,
      }),
    })
      .then(function (response) { return response.json(); })
      .then(function (payload) {
        var capabilities = payload.capabilities;
        if (!capabilities) return;
        probedCapabilities[item.id] = capabilities;
        item.capabilities = capabilities;
        if (row) renderRowCaps(row, capabilities, false);
      })
      .catch(function () {})
      .finally(function () { delete probingModels[item.id]; });
    probingModels[item.id] = promise;
    return promise;
  }

  function showResultsNotice(message) {
    if (!addDialog) return;
    var results = addDialog.querySelector(".eg-add-results");
    results.hidden = false;
    results.textContent = message;
  }

  function renderDiscoveredModels(models, fields, limitB) {
    var results = addDialog.querySelector(".eg-add-results");
    results.hidden = false;
    results.textContent = "";
    var allowed = models.filter(function (item) { return item.allowed; });
    var blocked = models.filter(function (item) { return !item.allowed; });
    var selected = {};
    var confirmButton = null;
    function syncConfirm() {
      var count = Object.keys(selected).length;
      if (confirmButton) {
        confirmButton.disabled = count === 0;
        confirmButton.textContent = count ? `添加所选（${count}）` : "添加所选";
      }
    }
    function appendItem(item) {
      var row = document.createElement("button");
      row.type = "button";
      row.className = "eg-add-model";
      var sizeText = item.params_b == null ? "参数量未知" : `${item.params_b}B`;
      row.dataset.modelId = item.id;
      row.innerHTML = `
        <span class="eg-add-model-name">${shortModelName(item.id)}<span class="eg-add-model-caps"></span></span>
        <span class="eg-add-model-size">${sizeText}</span>
      `;
      row.addEventListener("click", function () {
        if (selected[item.id]) {
          delete selected[item.id];
          row.classList.remove("eg-add-model-selected");
        } else {
          selected[item.id] = item;
          row.classList.add("eg-add-model-selected");
          // 点选即探测真实能力：默认无标签，探测完成后就地更新。
          if (probedCapabilities[item.id]) {
            renderRowCaps(row, probedCapabilities[item.id], false);
          } else {
            probeCapabilities(fields, item);
          }
        }
        syncConfirm();
      });
      results.append(row);
    }
    allowed.slice(0, 100).forEach(appendItem);
    // 不可添加的模型（超限/参数量未知/非对话）一律不渲染，仅计数。
    if (blocked.length) {
      var more = document.createElement("div");
      more.className = "eg-add-more";
      more.textContent = "另有 " + blocked.length + " 个模型"
        + (limitB ? "因超过 " + limitB + "B 或参数量未知被隐藏" : "因参数量未知被隐藏");
      results.append(more);
    }
    if (!allowed.length && !blocked.length) {
      var empty = document.createElement("div");
      empty.className = "eg-add-more";
      empty.textContent = "没有获取到模型";
      results.append(empty);
      return;
    }
    // 多选确认：逐个注册已选模型，全部成功后刷新列表一次。
    confirmButton = document.createElement("button");
    confirmButton.type = "button";
    confirmButton.className = "eg-add-primary eg-add-confirm";
    confirmButton.disabled = true;
    confirmButton.textContent = "添加所选";
    confirmButton.addEventListener("click", async function () {
      var picked = Object.keys(selected).map(function (id) { return selected[id]; });
      if (!picked.length) return;
      confirmButton.disabled = true;
      // 等待已选模型在途的能力探测，让注册携带真实探测结果。
      await Promise.all(
        picked.map(function (item) { return probeCapabilities(fields, item); })
      );
      confirmButton.textContent = "正在添加…";
      var failed = [];
      for (var i = 0; i < picked.length; i++) {
        try {
          await registerCustomModel(fields, picked[i]);
        } catch (error) {
          failed.push(`${picked[i].id}（${error.message}）`);
        }
      }
      // 清空已成功添加的选择
      picked.forEach(function (item) {
        if (!failed.some(function (f) { return f.startsWith(item.id); })) {
          delete selected[item.id];
        }
      });
      await loadCatalog();
      // 设置面板的"默认模型"下拉由服务端经 socket 重发，即时包含新模型。
      requestProviderViewsRefresh();
      renderManagedModels();
      selected = {};
      confirmButton.textContent = "添加所选";
      if (failed.length) {
        showResultsNotice(`以下模型添加失败：${failed.join("、")}`);
        // 清除选择态，保留结果列表让用户重试失败项。
        [...results.querySelectorAll(".eg-add-model-selected")].forEach(function (row) {
          row.classList.remove("eg-add-model-selected");
        });
        confirmButton.disabled = true;
      } else {
        // 全部成功：收起结果区即可——新模型已出现在上方提供商列表中。
        results.hidden = true;
        results.textContent = "";
      }
    });
    results.append(confirmButton);
  }

  async function registerCustomModel(fields, item) {
    var response = await fetch("/models/custom", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify({
        label: fields.label,
        base_url: fields.base_url,
        api_key: fields.api_key,
        model: item.id,
        capabilities: probedCapabilities[item.id],
      }),
    });
    var payload = await response.json().catch(function () { return {}; });
    if (!response.ok) throw new Error(payload.detail || `添加失败（${response.status}）`);
    return payload;
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
  // 无模型时拦截发送（按钮点击 / 输入框 Enter），给出添加引导。
  function showNoModelGuidance() {
    var existing = document.querySelector(".eg-no-model-toast");
    if (existing) existing.remove();
    var toast = document.createElement("div");
    toast.className = "eg-no-model-toast";
    toast.setAttribute("role", "alert");
    toast.textContent = "尚未添加模型提供商，添加你的 API 提供商后即可开始对话。";
    document.body.append(toast);
    window.setTimeout(function () { toast.remove(); }, 4500);
  }
  function interceptSend(event) {
    if (!document.body.classList.contains("eg-no-models")) return;
    var submit = event.target.closest && event.target.closest("#chat-submit");
    var isEnter =
      event.type === "keydown" &&
      event.key === "Enter" &&
      !event.shiftKey &&
      event.target &&
      (event.target.id === "chat-input" || event.target.closest?.("#message-composer"));
    if (!submit && !isEnter) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    showNoModelGuidance();
  }
  document.addEventListener("click", interceptSend, true);
  document.addEventListener("keydown", interceptSend, true);
  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape") {
      if (addDialog) {
        event.stopPropagation();
        closeAddDialog();
        return;
      }
      if (menu) {
        event.stopPropagation();
        closeMenu();
        button?.focus();
      }
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
    ensureSettingsAddEntry();
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
