/* Lightweight presentation-only enhancement for Chainlit's markdown welcome view.
   It groups the generated config table and usage list into the designed cards;
   no chat, auth, upload, or transport behavior is changed. */
(() => {
  "use strict";

  document.body.classList.add("eg-chat-shell");
  let scheduled = false;

  let sidebarRefs = null;
  let collapsedBar = null;

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
      '<rect x="3" y="4" width="18" height="16" rx="3"/><path d="M9.5 4v16"/>',
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
      const searchIcon = '<circle cx="11" cy="11" r="7"/><path d="m20 20-3.8-3.8"/>';
      const collapseIcon = '<rect x="3" y="4" width="18" height="16" rx="3"/><path d="M9.5 4v16"/>';
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
      plus.innerHTML = '<path d="M12 5v14M5 12h14"/>';
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

    const collapsed = group.dataset.state === "collapsed";
    document.body.classList.toggle("eg-sidebar-collapsed", collapsed);
    if (collapsedBar) {
      collapsedBar.id = "eg-collapsed-bar";
      collapsedBar.classList.toggle("eg-collapsed-bar", collapsed);
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
    const expandButton = makeIconControl("展开侧栏", '<rect x="3" y="4" width="18" height="16" rx="3"/><path d="M9.5 4v16"/>', () => sidebarRefs?.trigger.click());
    const searchButton = makeIconControl("搜索会话", '<circle cx="11" cy="11" r="7"/><path d="m20 20-3.8-3.8"/>', () => sidebarRefs?.search.click());
    const newButton = makeIconControl("新建对话", '<path d="M12 5v14M5 12h14"/>', () => sidebarRefs?.new.click());
    pill.append(expandButton, searchButton, newButton);
    collapsedBar.append(logo, pill);
    document.body.append(collapsedBar);
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
    welcome.classList.add("eg-welcome");

    // Chainlit puts a 48rem max-width inline on this container. Use the wider
    // workspace column without fighting the inline declaration from CSS.
    const container = welcome.parentElement;
    if (container) {
      container.style.width = "min(58rem, 100%)";
      container.style.maxWidth = "min(58rem, 100%)";
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

  buildCollapsedBar();

  function scheduleEnhance() {
    if (scheduled) return;
    scheduled = true;
    requestAnimationFrame(() => {
      scheduled = false;
      enhanceSidebar();
      enhanceWelcome();
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
