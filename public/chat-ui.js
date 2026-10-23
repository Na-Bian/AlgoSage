/* Lightweight presentation-only enhancement for Chainlit's markdown welcome view.
   It groups the generated config table and usage list into the designed cards;
   no chat, auth, upload, or transport behavior is changed. */
(() => {
  "use strict";

  document.body.classList.add("eg-chat-shell");

  // Chainlit always mounts with defaultOpen=true, so a stored collapsed choice
  // has to be replayed after React mounts. Mark the first restore so the replay
  // changes layout instantly instead of looking like a user-triggered collapse.
  let sidebarRestorePending = storedSidebarOpen() === false;
  if (sidebarRestorePending) {
    document.body.classList.add("eg-sidebar-restoring");
  }

  let scheduled = false;

  let sidebarRefs = null;
  let collapsedBar = null;
  const STEP_LABELS = {
    "generation-safety-review": "生成与安全审核",
    "security-audit-policy": "安全审计与策略",
  };

  function translateStepLabels() {
    document.querySelectorAll('main button[id^="step-"]').forEach((button) => {
      const safeName = decodeURIComponent(button.id.slice("step-".length));
      const label = STEP_LABELS[safeName];
      if (!label || button.dataset.egStepLabel === label) return;

      button.dataset.egStepLabel = label;
      button.childNodes.forEach((node) => {
        if (node.nodeType === Node.TEXT_NODE && node.textContent.trim()) {
          node.textContent = node.textContent.replace(safeName, label);
        }
      });
    });
  }

  const SIDEBAR_ICONS = {
    panel: '<rect x="3" y="4" width="18" height="16" rx="3"/><path d="M9.5 4v16"/>',
    search: '<circle cx="11" cy="11" r="7"/><path d="m20 20-3.8-3.8"/>',
    squarePen: '<path d="M12 3h5a2 2 0 0 1 2 2v5"/><path d="M18.4 3.6a2.1 2.1 0 0 1 3 3L11 17l-4 1 1-4Z"/>',
    shieldCheck: '<path d="M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1z"/><path d="m9 12 2 2 4-4"/>',
  };

  function makeIconControl(label, svgPath, onClick) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "eg-sidebar-icon-button";
    button.setAttribute("aria-label", label);
    button.title = label;
    const icon = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    icon.setAttribute("viewBox", "0 0 24 24");
    icon.setAttribute("fill", "none");
    icon.setAttribute("stroke", "currentColor");
    icon.setAttribute("stroke-width", "1.8");
    icon.setAttribute("stroke-linecap", "round");
    icon.setAttribute("stroke-linejoin", "round");
    icon.setAttribute("aria-hidden", "true");
    icon.innerHTML = svgPath;
    button.append(icon);
    button.addEventListener("click", onClick);
    return button;
  }

  function storedSidebarOpen() {
    const match = document.cookie.match(
      /(?:^|;\s*)sidebar:state=(true|false)(?:;|$)/
    );
    return match ? match[1] === "true" : null;
  }

  function restoreSidebarPreference(group, trigger) {
    const preferredOpen = storedSidebarOpen();
    if (preferredOpen === null) return false;

    const currentOpen = group.dataset.state !== "collapsed";
    if (currentOpen === preferredOpen) return false;

    // Chainlit writes this cookie on manual collapse, but its SidebarProvider
    // does not read it after `/thread/:id` and `/` remount the page layout.
    // Restore the user's choice before updating the custom presentation.
    trigger.click();
    return true;
  }

  function enhanceSidebar() {
    const sidebar = document.querySelector('[data-sidebar="sidebar"]');
    const group = sidebar?.closest(".group.peer");
    const header = sidebar?.querySelector('[data-sidebar="header"] > .flex.items-center.justify-between');
    if (!sidebar || !group || !header) return false;

    const sourceTrigger = header.querySelector("#sidebar-trigger-button");
    const sourceSearch = header.querySelector("#search-chats-button");
    const sourceNew = header.querySelector("#new-chat-button");
    if (!sourceTrigger || !sourceSearch || !sourceNew) return false;

    const refsChanged = !sidebarRefs || !sidebarRefs.trigger.isConnected
      || !sidebarRefs.search.isConnected || !sidebarRefs.new.isConnected;
    if (refsChanged) {
      const collapseControl = makeIconControl(
      group.dataset.state === "collapsed" ? "展开侧栏" : "收起侧栏",
      SIDEBAR_ICONS.panel,
      () => sidebarRefs?.trigger.click()
    );
    sidebarRefs = {
      trigger: sourceTrigger,
      search: sourceSearch,
      new: sourceNew,
      collapseControl,
    };
      // Native header controls stay in React's tree, but are represented by
      // themed controls. Clicks are delegated back to the source buttons.
      header.classList.add("eg-original-sidebar-header");
      header.querySelector(".eg-sidebar-presentation")?.remove();

      const presentation = document.createElement("div");
      presentation.className = "eg-sidebar-presentation";

      const brand = document.createElement("div");
      brand.className = "eg-sidebar-brand";
      brand.innerHTML = `
        <img class="eg-logo-dark" src="/public/brand-logo.png" alt="AlgoSage">
        <img class="eg-logo-light" src="/public/brand-logo-light.png" alt="">
      `;

      const actions = document.createElement("div");
      actions.className = "eg-sidebar-actions";
      const searchIcon = SIDEBAR_ICONS.search;
      const collapseIcon = SIDEBAR_ICONS.panel;
      const searchControl = makeIconControl("搜索会话", searchIcon, () => sidebarRefs.search.click());
      actions.append(searchControl, collapseControl);
      presentation.append(brand, actions);

      const newChatAction = document.createElement("button");
      newChatAction.type = "button";
      newChatAction.className = "eg-new-chat-action";
      newChatAction.setAttribute("aria-label", "新建对话");
      const plus = document.createElementNS("http://www.w3.org/2000/svg", "svg");
      plus.setAttribute("viewBox", "0 0 24 24");
      plus.setAttribute("fill", "none");
      plus.setAttribute("stroke", "currentColor");
      plus.setAttribute("stroke-width", "2");
      plus.setAttribute("stroke-linecap", "round");
      plus.setAttribute("aria-hidden", "true");
      plus.innerHTML = SIDEBAR_ICONS.squarePen;
      const newLabel = document.createElement("span");
      newLabel.textContent = "新建对话";
      newChatAction.append(plus, newLabel);
      newChatAction.addEventListener("click", () => sidebarRefs.new.click());

      presentation.append(newChatAction);
      header.append(presentation);
    }

    sidebarRefs.collapseControl.setAttribute(
      "aria-label",
      group.dataset.state === "collapsed" ? "展开侧栏" : "收起侧栏"
    );
    sidebarRefs.collapseControl.title = sidebarRefs.collapseControl.getAttribute("aria-label");

    if (restoreSidebarPreference(group, sourceTrigger)) return false;

    if (sidebarRestorePending) {
      // Keep motion suppressed through React's post-click paint, then remove
      // the flag after the longest sidebar transition would have completed.
      sidebarRestorePending = false;
      window.setTimeout(() => {
        document.body.classList.remove("eg-sidebar-restoring");
      }, 360);
    }

    const collapsed = group.dataset.state === "collapsed";
    document.body.classList.toggle("eg-sidebar-collapsed", collapsed);
    if (collapsedBar) {
      if (!collapsed && collapsedBar.contains(document.activeElement)) {
        document.activeElement.blur();
      }
      collapsedBar.id = "eg-collapsed-bar";
      collapsedBar.className = "eg-collapsed-bar";
      collapsedBar.classList.toggle("eg-sidebar-collapsed", collapsed);
      collapsedBar.setAttribute("aria-hidden", String(!collapsed));
    }
    return true;
  }

  function buildCollapsedBar() {
    collapsedBar = document.createElement("div");
    collapsedBar.id = "eg-collapsed-bar";
    collapsedBar.className = "eg-collapsed-bar";
    collapsedBar.setAttribute("aria-hidden", "true");

    const logo = document.createElement("img");
    logo.src = "/public/favicon.png";
    logo.alt = "AlgoSage";
    logo.className = "eg-collapsed-logo";

    const pill = document.createElement("div");
    pill.className = "eg-collapsed-pill";
    const expandButton = makeIconControl("展开侧栏", SIDEBAR_ICONS.panel, () => sidebarRefs?.trigger.click());
    const searchButton = makeIconControl("搜索会话", SIDEBAR_ICONS.search, () => sidebarRefs?.search.click());
    const newButton = makeIconControl("新建对话", SIDEBAR_ICONS.squarePen, () => sidebarRefs?.new.click());
    pill.append(expandButton, searchButton, newButton);
    collapsedBar.append(logo, pill);
    document.body.append(collapsedBar);
  }

  function enhanceStepStatusIcons() {
    document.querySelectorAll('main button[id^="step-"]').forEach((button) => {
      const row = button.closest(".ai-message");
      const host = row?.querySelector(":scope > span");
      if (!host) return;

      const frame = host.firstElementChild || host;
      host.classList.add("eg-step-avatar");
      frame.classList.add("eg-step-avatar-shell");
      frame.setAttribute("aria-hidden", "true");

      const nativeVisual = frame.querySelector(
        "svg:not(.eg-step-status-icon), img"
      );
      const existing = frame.querySelector(".eg-step-status-icon");
      if (nativeVisual) {
        existing?.remove();
        return;
      }
      if (existing) return;

      const icon = document.createElementNS("http://www.w3.org/2000/svg", "svg");
      icon.classList.add("eg-step-status-icon");
      icon.setAttribute("viewBox", "0 0 24 24");
      icon.setAttribute("fill", "none");
      icon.setAttribute("stroke", "currentColor");
      icon.setAttribute("stroke-width", "2");
      icon.setAttribute("stroke-linecap", "round");
      icon.setAttribute("stroke-linejoin", "round");
      icon.innerHTML = SIDEBAR_ICONS.shieldCheck;
      frame.append(icon);
    });
  }

  function enhanceWelcome() {
    const welcome = document.getElementById("welcome-screen");
    if (!welcome || welcome.dataset.egChatUi === "1") return false;

    const heading = welcome.querySelector(".prose > h3");
    const lede = heading?.nextElementSibling;
    const configCard = welcome.querySelector(".prose > div:has(table)");
    const usageHeading = configCard?.nextElementSibling;
    const usageList = usageHeading?.nextElementSibling;
    if (!heading || !lede || !configCard || !usageHeading || !usageList) return false;

    welcome.dataset.egChatUi = "1";
    welcome.classList.add("eg-welcome", "eg-empty-welcome");
    welcome.setAttribute("aria-label", "AlgoSage 新会话");

    // #message-composer is already mounted inside #welcome-screen. Keep it in
    // React's original tree so drafts, uploads, disabled state and submit all
    // share exactly one component; this is a layout transform, not a clone.
    const details = heading.closest(".prose")?.parentElement;
    if (details) details.hidden = true;

    const brand = document.createElement("div");
    brand.className = "eg-welcome-brand";
    brand.innerHTML = `
      <img class="eg-logo-dark" src="/public/brand-logo.png" alt="AlgoSage">
      <img class="eg-logo-light" src="/public/brand-logo-light.png" alt="">
    `;
    const nativeComposer = welcome.querySelector("#message-composer");
    if (nativeComposer) {
      welcome.insertBefore(brand, nativeComposer);
    } else {
      welcome.append(brand);
    }

    // Chainlit puts a 48rem max-width inline on this container. Use the compact
    // welcome column without fighting the inline declaration from CSS.
    const container = welcome.parentElement;
    if (container) {
      container.style.width = "min(54rem, 100%)";
      container.style.maxWidth = "min(54rem, 100%)";
    }
    heading.classList.add("eg-welcome-title");
    lede.classList.add("eg-welcome-lede");
    configCard.classList.add("eg-config-card");

    // The markdown renderer emits the usage heading and list as siblings. Move
    // only these presentation siblings into one card wrapper.
    const usageCard = document.createElement("section");
    usageCard.className = "eg-usage-card";
    usageHeading.parentNode.insertBefore(usageCard, usageHeading);
    usageCard.append(usageHeading, usageList);
    usageHeading.classList.add("eg-card-heading");

    const icon = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    icon.setAttribute("viewBox", "0 0 24 24");
    icon.setAttribute("fill", "none");
    icon.setAttribute("stroke", "currentColor");
    icon.setAttribute("stroke-width", "1.7");
    icon.setAttribute("stroke-linecap", "round");
    icon.setAttribute("stroke-linejoin", "round");
    icon.setAttribute("aria-hidden", "true");
    icon.classList.add("eg-card-icon");
    icon.innerHTML = '<path d="M4 19.5V6a2 2 0 0 1 2-2h11a1 1 0 0 1 1 1v14a1 1 0 0 1-1 1H6.5A2.5 2.5 0 0 1 4 17.5Z"/><path d="M8 8h7M8 12h6"/>';
    usageHeading.prepend(icon);

    return true;
  }

  function enhanceReadmeDetails() {
    const dialog = Array.from(document.querySelectorAll('[role="dialog"]')).find(
      (node) => node.querySelector(':scope > div > div > h2')?.textContent.trim() === "说明"
    );
    if (!dialog || dialog.dataset.egRuntimeDetails === "1") return false;

    const source = document.querySelector(
      '#welcome-screen.eg-empty-welcome > div:first-child'
    );
    const prose = dialog.querySelector(':scope > div > .prose');
    if (!source || !prose) return false;

    const section = document.createElement("section");
    section.className = "eg-readme-runtime";
    const title = document.createElement("h2");
    title.textContent = "当前运行配置";
    const configCard = source.querySelector(".eg-config-card");
    if (!configCard) return false;
    section.append(title, configCard.cloneNode(true));
    prose.append(section);
    dialog.dataset.egRuntimeDetails = "1";
    return true;
  }

  buildCollapsedBar();

  function scheduleEnhance() {
    if (scheduled) return;
    scheduled = true;
    requestAnimationFrame(() => {
      scheduled = false;
      enhanceSidebar();
      enhanceStepStatusIcons();
      translateStepLabels();
      enhanceWelcome();
      enhanceReadmeDetails();
    });
  }

  scheduleEnhance();
  new MutationObserver(scheduleEnhance).observe(document.body, {
    childList: true,
    subtree: true,
    attributes: true,
    attributeFilter: ["data-state", "class"],
  });
})();
