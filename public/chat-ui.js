/* Lightweight presentation-only enhancement for Chainlit's markdown welcome view.
   It groups the generated config table and usage list into the designed cards;
   no chat, auth, upload, or transport behavior is changed. */
(() => {
  "use strict";

  let scheduled = false;

  function enhanceSidebar() {
    const sidebar = document.querySelector('[data-sidebar="sidebar"]');
    const group = sidebar?.closest(".group.peer");
    const header = sidebar?.querySelector('[data-sidebar="header"] > .flex.items-center.justify-between');
    if (!sidebar || !group || !header) return false;

    sidebar.classList.toggle("eg-sidebar-collapsed", group.dataset.state === "collapsed");

    const trigger = header.querySelector("#sidebar-trigger-button");
    const search = header.querySelector("#search-chats-button");
    const newChat = header.querySelector("#new-chat-button");
    if (!trigger || !search || !newChat) return false;

    // React may replace its header children after navigation or state changes,
    // so verify the presentation nodes instead of trusting a one-time marker.
    if (header.dataset.egSidebar === "1" && header.contains(search) && header.contains(trigger)) {
      return true;
    }

    header.dataset.egSidebar = "1";
    header.classList.add("eg-sidebar-header");

    const brand = document.createElement("a");
    brand.className = "eg-sidebar-brand";
    brand.href = "/";
    brand.setAttribute("aria-label", "AlgoSage");
    const brandImage = document.createElement("img");
    brandImage.src = "/public/favicon.png";
    brandImage.alt = "";
    // Pin the mark because Chainlit's generated image reset can otherwise make
    // an only-child grid item collapse before the raster has layout.
    brand.style.setProperty("width", "32px", "important");
    brand.style.setProperty("height", "32px", "important");
    brand.style.setProperty("min-width", "32px", "important");
    brandImage.style.setProperty("width", "22px", "important");
    brandImage.style.setProperty("height", "22px", "important");
    brand.append(brandImage);

    const actions = document.createElement("div");
    actions.className = "eg-sidebar-actions";
    actions.append(search, trigger);

    const top = document.createElement("div");
    top.className = "eg-sidebar-top";
    top.append(brand, actions);

    // append/preload moves the original button nodes, so Chainlit's listeners
    // for search, collapse, and new-chat continue to work.
    header.classList.add("eg-sidebar-header-grid");
    header.replaceChildren(top, newChat);
    newChat.classList.add("eg-sidebar-new-chat");
    newChat.setAttribute("aria-label", "开启新对话");
    newChat.title = "开启新对话";
    if (!newChat.querySelector(".eg-sidebar-new-label")) {
      const label = document.createElement("span");
      label.className = "eg-sidebar-new-label";
      label.textContent = "开启新对话";
      newChat.append(label);
    }

    search.setAttribute("aria-label", "搜索会话");
    search.title = "搜索会话";
    trigger.setAttribute("aria-label", group.dataset.state === "collapsed" ? "展开侧栏" : "收起侧栏");
    trigger.title = trigger.getAttribute("aria-label");

    return true;
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
