/* Thread-local model selector. The browser only sees the server-authorized
   catalog; provider credentials and authorization remain server-side.
   Users may additionally register their own OpenAI-compatible endpoint
   (BYOK): the key lives in this browser's localStorage and an in-memory
   per-account server entry — never in chat history or logs. */
(function () {
  "use strict";

  var CUSTOM_STORE_KEY = "algosage-custom-providers";

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

  function readCustomStore() {
    try {
      var raw = JSON.parse(localStorage.getItem(CUSTOM_STORE_KEY) || "[]");
      return Array.isArray(raw) ? raw.filter(function (item) {
        return item && item.base_url && item.api_key && item.model;
      }) : [];
    } catch (_) {
      return [];
    }
  }

  function writeCustomStore(entries) {
    localStorage.setItem(CUSTOM_STORE_KEY, JSON.stringify(entries));
  }

  function rememberCustom(entry) {
    var entries = readCustomStore().filter(function (item) {
      return !(item.base_url === entry.base_url && item.model === entry.model);
    });
    entries.push({ label: entry.label, base_url: entry.base_url, api_key: entry.api_key, model: entry.model });
    writeCustomStore(entries);
  }

  function forgetCustomBySelection(selection) {
    var doomed = null;
    state.providers.forEach(function (group) {
      if (group.id !== "custom") return;
      group.models.forEach(function (model) {
        if (model.selection === selection) doomed = model;
      });
    });
    if (!doomed) return;
    writeCustomStore(readCustomStore().filter(function (item) {
      return item.model !== doomed.model;
    }));
  }

  async function reRegisterCustom() {
    // Re-register this browser's BYOK entries with the in-memory server store
    // (keys never persist server-side, so a restart needs a silent re-add).
    var entries = readCustomStore();
    for (var i = 0; i < entries.length; i += 1) {
      try {
        await fetch("/models/custom", {
          method: "POST",
          credentials: "same-origin",
          headers: { "Content-Type": "application/json", Accept: "application/json" },
          body: JSON.stringify(entries[i]),
        });
      } catch (_) { /* 静默：目录刷新后以服务端成功注册的为准 */ }
    }
    if (entries.length) {
      try { await loadCatalog(); } catch (_) {}
    }
  }

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
          <span class="eg-model-option-name">${model.model}${group.id === "custom" ? '<em class="eg-model-custom-badge">自定义</em>' : ""}</span>
          <span class="eg-model-option-state">${model.selection === state.currentSelection ? "✓" : model.available ? "" : "不可用"}</span>
        `;
        option.addEventListener("click", function () {
          selectModel(model.selection);
        });
        if (group.id === "custom") {
          var remove = document.createElement("span");
          remove.className = "eg-model-remove";
          remove.setAttribute("role", "button");
          remove.title = "删除该自定义模型";
          remove.textContent = "×";
          remove.addEventListener("click", function (event) {
            event.preventDefault();
            event.stopPropagation();
            removeCustomModel(model.selection);
          });
          option.append(remove);
        }
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
      state.switching = false;
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
      forgetCustomBySelection(selection);
      if (state.currentSelection === selection) {
        // selectModel refuses to run while a switch is in flight; release the
        // removal lock so the fallback switch back to a builtin model executes.
        state.switching = false;
        await selectModel(defaultSelection(), { force: true });
      }
      // 两个分支都要刷新目录：管理对话框的列表与回退后的标签都依赖它。
      await loadCatalog();
      return true;
    } finally {
      state.switching = false;
      renderMenu();
    }
  }

  async function removeCustomModel(selection) {
    if (state.switching) return;
    setStatus("正在删除…");
    try {
      await deleteCustomSelection(selection);
      setStatus("已删除", false);
    } catch (error) {
      setStatus(error.message || "删除失败", true);
    }
  }

  function defaultSelection() {
    var group = state.providers.find(function (item) { return item.id !== "custom"; });
    return (group && group.models[0] && group.models[0].selection) || "";
  }

  /* 设置面板入口："添加自定义模型"挂在"模型与 Provider"控件下方。
     面板由 React 渲染，注入节点消失时由观察器补回；对话框复用本文件的实现。 */
  function ensureSettingsAddEntry() {
    if (!document.getElementById("chat-settings-sidebar-title")) return;
    var label = document.querySelector('aside label[for="model_catalog"]');
    if (!label) return;
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
    entry.innerHTML = `
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
        <path d="M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2z"/>
        <circle cx="12" cy="12" r="3"/>
      </svg>
      管理模型提供商
    `;
    entry.addEventListener("click", function (event) {
      event.preventDefault();
      event.stopPropagation();
      openAddDialog();
    });
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
        <div class="eg-add-hint">统一管理可用的模型提供商：列表中可移除不再使用的（预置的仅对你隐藏，可随时恢复）；用下方表单以 Base URL + API Key 接入 OpenAI 兼容接口（如 SiliconFlow / DeepSeek / 本地 vLLM），仅支持参数量 ≤27B 的模型，密钥只保存在你的浏览器与本服务进程内存中。</div>
        <div class="eg-add-managed">
          <div class="eg-add-managed-head">提供商 <span data-managed-count></span></div>
          <div data-managed-list></div>
        </div>
        <div data-removed-list></div>
        <div class="eg-add-divider">添加提供商</div>
        <label class="eg-add-field">显示名称（可选）<input type="text" data-field="label" placeholder="例如：我的 SiliconFlow" maxlength="40"></label>
        <label class="eg-add-field">Base URL<input type="text" data-field="base_url" placeholder="https://api.siliconflow.cn/v1"></label>
        <label class="eg-add-field">API Key<input type="password" data-field="api_key" placeholder="sk-…" autocomplete="off"></label>
        <div class="eg-add-actions">
          <button type="button" class="eg-add-secondary" data-action="cancel">取消</button>
          <button type="button" class="eg-add-primary" data-action="discover">获取模型列表</button>
        </div>
        <div class="eg-add-results" hidden></div>
        <div class="eg-add-status" aria-live="polite"></div>
      </div>
    `;
    document.body.append(addDialog);
    addDialog.addEventListener("click", function (event) {
      if (event.target === addDialog) closeAddDialog();
    });
    addDialog.querySelector('[data-action="cancel"]').addEventListener("click", closeAddDialog);
    addDialog.querySelector('[data-action="discover"]').addEventListener("click", function () {
      discoverInDialog();
    });
    renderManagedModels();
    // 拉一次目录以带上 label；完成后重渲染列表。
    loadCatalog().then(function () {
      if (addDialog) renderManagedModels();
    }).catch(function () {});
    var first = addDialog.querySelector('[data-field="label"]');
    if (first) first.focus();
  }

  function customEntries() {
    var group = state.providers.find(function (item) { return item.id === "custom"; });
    return (group && group.models) || [];
  }

  function presetGroups() {
    return state.providers.filter(function (item) { return item.id !== "custom"; });
  }

  async function hidePresetProvider(providerId) {
    var response = await fetch("/models/preset", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify({ provider_id: providerId, hidden: true }),
    });
    if (!response.ok) {
      var payload = await response.json().catch(function () { return {}; });
      throw new Error(payload.detail || `移除失败（${response.status}）`);
    }
    // 正在使用的提供商被移除时，先切回默认模型再刷新目录。
    var stillSelected = state.providers.some(function (group) {
      return group.id === providerId && group.models.some(function (model) {
        return model.selection === state.currentSelection;
      });
    });
    if (stillSelected && state.currentSelection !== defaultSelection()) {
      await selectModel(defaultSelection(), { force: true });
    }
    await loadCatalog();
  }

  async function restorePresetProvider(providerId) {
    var response = await fetch("/models/preset", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify({ provider_id: providerId, hidden: false }),
    });
    if (!response.ok) {
      var payload = await response.json().catch(function () { return {}; });
      throw new Error(payload.detail || `恢复失败（${response.status}）`);
    }
    await loadCatalog();
  }

  async function removeCustomEntries(selections) {
    for (var i = 0; i < selections.length; i++) {
      var done = await deleteCustomSelection(selections[i]);
      if (!done) throw new Error("正在切换模型，请稍后再试");
    }
  }

  function managedRow(name, options) {
    var row = document.createElement("div");
    row.className = "eg-add-managed-row";
    var label = document.createElement("span");
    label.className = "eg-add-managed-name";
    label.textContent = name;
    label.title = name;
    row.append(label);
    var action = document.createElement("button");
    action.type = "button";
    action.className = options.restore
      ? "eg-add-managed-restore"
      : "eg-add-managed-remove";
    action.textContent = options.text;
    if (options.disabled) {
      action.disabled = true;
      action.title = options.title || "";
    } else {
      action.addEventListener("click", async function () {
        action.disabled = true;
        setDialogStatus(options.pending);
        try {
          await options.run();
          renderManagedModels();
          setDialogStatus(options.done, false);
        } catch (error) {
          action.disabled = false;
          setDialogStatus(error.message || options.failed, true);
        }
      });
    }
    row.append(action);
    return row;
  }

  function renderManagedModels() {
    if (!addDialog) return;
    var count = addDialog.querySelector("[data-managed-count]");
    var presets = presetGroups();
    var customCount = customEntries().length;
    if (count) count.textContent = `（预置 ${presets.length} · 自定义 ${customCount}/8）`;
    var list = addDialog.querySelector("[data-managed-list]");
    var removedList = addDialog.querySelector("[data-removed-list]");
    if (!list || !removedList) return;
    list.textContent = "";
    removedList.textContent = "";
    removedList.hidden = true;

    presets.forEach(function (group) {
      if (group.hidden) return; // 已移除的显示在下方恢复区
      var name = `${group.label} · ${group.models.length} 个模型`;
      if (group.locked) {
        list.append(managedRow(name, {
          text: "移除", disabled: true, title: "默认提供商，不可移除",
          pending: "", done: "", failed: "", run: async function () {},
        }));
        return;
      }
      list.append(managedRow(name, {
        text: "移除", pending: "正在移除…", done: "已移除；它不再出现在模型选择里，可随时在下方恢复",
        failed: "移除失败",
        run: function () { return hidePresetProvider(group.id); },
      }));
    });
    customEntries().forEach(function (model) {
      list.append(managedRow(`${model.label || "自定义 API"} · ${model.model}`, {
        text: "删除", pending: "正在删除…", done: "已删除；若它正被使用，已自动切回默认模型",
        failed: "删除失败",
        run: function () { return removeCustomEntries([model.selection]); },
      }));
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
        removedList.append(managedRow(`${group.label} · ${group.models.length} 个模型`, {
          text: "恢复", restore: true, pending: "正在恢复…", done: "已恢复",
          failed: "恢复失败",
          run: function () { return restorePresetProvider(group.id); },
        }));
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

  function setDialogStatus(message, isError) {
    if (!addDialog) return;
    var node = addDialog.querySelector(".eg-add-status");
    node.textContent = message || "";
    node.classList.toggle("eg-model-error", Boolean(isError));
  }

  async function discoverInDialog() {
    var fields = dialogFields();
    if (!fields.base_url || !fields.api_key) {
      setDialogStatus("请先填写 Base URL 和 API Key", true);
      return;
    }
    setDialogStatus("正在获取模型列表…");
    try {
      var response = await fetch("/models/discover", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify(fields),
      });
      var payload = await response.json().catch(function () { return {}; });
      if (!response.ok) throw new Error(payload.detail || `获取失败（${response.status}）`);
      renderDiscoveredModels(payload.models || [], fields);
      setDialogStatus((payload.models || []).length ? "点击要添加的模型" : "该接口未返回任何模型");
    } catch (error) {
      setDialogStatus(error.message || "获取模型列表失败", true);
    }
  }

  function renderDiscoveredModels(models, fields) {
    var results = addDialog.querySelector(".eg-add-results");
    results.hidden = false;
    results.textContent = "";
    var allowed = models.filter(function (item) { return item.allowed; });
    var blocked = models.filter(function (item) { return !item.allowed; });
    function appendItem(item) {
      var row = document.createElement("button");
      row.type = "button";
      row.className = "eg-add-model" + (item.allowed ? "" : " eg-add-model-blocked");
      row.disabled = !item.allowed;
      var sizeText = item.params_b == null ? "参数量未知" : `${item.params_b}B`;
      row.innerHTML = `
        <span class="eg-add-model-name">${item.id}</span>
        <span class="eg-add-model-size">${item.allowed ? sizeText : `${sizeText} · 超出限制`}</span>
      `;
      if (item.allowed) {
        row.addEventListener("click", function () {
          saveCustomModel(fields, item);
        });
      }
      results.append(row);
    }
    allowed.slice(0, 30).forEach(appendItem);
    blocked.slice(0, 8).forEach(appendItem);
    if (blocked.length > 8) {
      var more = document.createElement("div");
      more.className = "eg-add-more";
      more.textContent = `另有 ${blocked.length - 8} 个模型因超过 27B 或参数量未知被隐藏`;
      results.append(more);
    }
    if (!allowed.length && !blocked.length) {
      var empty = document.createElement("div");
      empty.className = "eg-add-more";
      empty.textContent = "没有获取到模型";
      results.append(empty);
    }
  }

  async function saveCustomModel(fields, item) {
    setDialogStatus("正在添加…");
    try {
      var response = await fetch("/models/custom", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({
          label: fields.label,
          base_url: fields.base_url,
          api_key: fields.api_key,
          model: item.id,
        }),
      });
      var payload = await response.json().catch(function () { return {}; });
      if (!response.ok) throw new Error(payload.detail || `添加失败（${response.status}）`);
      rememberCustom({
        label: (payload.entry && payload.entry.label) || fields.label,
        base_url: fields.base_url,
        api_key: fields.api_key,
        model: item.id,
      });
      await loadCatalog();
      // 不自动切换：设置面板里的"模型与 Provider"下拉在确认保存前仍是旧值，
      // 自动切换会被随后的确认用陈旧选择悄悄覆盖。添加后由用户在面板中显式选择。
      renderManagedModels();
      setDialogStatus(
        `已添加 ${item.id}；在"模型与 Provider"中选择它并点确认即可切换`,
        false,
      );
    } catch (error) {
      setDialogStatus(error.message || "添加失败", true);
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
    reRegisterCustom();
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
