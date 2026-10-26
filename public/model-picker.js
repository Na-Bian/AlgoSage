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
    rightGroup.insertBefore(button, submit);
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
      empty.textContent = "尚未添加模型提供商：添加你的 API 后即可开始对话。";
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

  function defaultSelection() {
    var group = state.providers.find(function (item) { return item.id !== "custom"; });
    return (group && group.models[0] && group.models[0].selection) || "";
  }

  /* 设置面板入口："管理模型提供商"挂在"默认模型"控件下方。
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
    // 确认：关闭对话框，并借一次面板提交让服务端重发"默认模型"列表
    // （提供商增删发生在 HTTP 路由里，服务端无法从那里推送 ChatSettings）。
    addDialog.querySelector('[data-action="confirm"]').addEventListener("click", function () {
      closeAddDialog();
      if (window.AlgoSageSettingsAutoSave) window.AlgoSageSettingsAutoSave.refresh();
    });
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
    // 面板下拉的选项由服务端在 /models/preset 成功后直接重发
    // （send_provider_settings），无需前端再触发提交收起抽屉。
  }

  async function removeCustomEntries(selections) {
    for (var i = 0; i < selections.length; i++) {
      var done = await deleteCustomSelection(selections[i]);
      if (!done) throw new Error("正在切换模型，请稍后再试");
    }
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
      renderDiscoveredModels(payload.models || [], fields, payload.probed === true);
    } catch (error) {
      showResultsNotice(error.message || "获取模型列表失败");
    }
  }

  function showResultsNotice(message) {
    if (!addDialog) return;
    var results = addDialog.querySelector(".eg-add-results");
    results.hidden = false;
    results.textContent = message;
  }

  function renderDiscoveredModels(models, fields, probed) {
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
    var probePending = !probed;
    function appendItem(item) {
      var row = document.createElement("button");
      row.type = "button";
      row.className = "eg-add-model" + (item.allowed ? "" : " eg-add-model-blocked");
      row.disabled = !item.allowed;
      var caps = item.capabilities || {};
      var capTags = [];
      if (caps.thinking === true) capTags.push("思考");
      else if (caps.thinking === false) capTags.push("不支持思考");
      if (caps.multimodal === true) capTags.push("多模态");
      if (caps.web_search === true) capTags.push("联网");
      var capChips = probePending
        ? ["能力探测中…"]
        : capTags;
      var capHtml = capChips.length
        ? `<span class="eg-add-model-caps">${capChips
            .map(function (chip) {
              return `<span>${chip}</span>`;
            })
            .join("")}</span>`
        : "";
      var sizeText = item.params_b == null ? "参数量未知" : `${item.params_b}B`;
      var blockReason = caps.is_chat === false
        ? "非对话模型"
        : item.params_b == null
          ? "参数量未知"
          : `${sizeText} · 超出限制`;
      row.innerHTML = `
        <span class="eg-add-model-name">${shortModelName(item.id)}${capHtml}</span>
        <span class="eg-add-model-size">${item.allowed ? sizeText : blockReason}</span>
      `;
      if (item.allowed) {
        row.addEventListener("click", function () {
          if (selected[item.id]) {
            delete selected[item.id];
            row.classList.remove("eg-add-model-selected");
          } else {
            selected[item.id] = item;
            row.classList.add("eg-add-model-selected");
          }
          syncConfirm();
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
    toast.textContent = "尚未添加模型提供商：点击「未配置模型」按钮，添加你的 API 提供商后即可开始对话。";
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
