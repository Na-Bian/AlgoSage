/* Lightweight presentation-only enhancement for Chainlit's markdown welcome view.
   It groups the generated config table and usage list into the designed cards;
   no chat, auth, upload, or transport behavior is changed. */
(() => {
  "use strict";

  let scheduled = false;

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
      enhanceWelcome();
    });
  }

  scheduleEnhance();
  new MutationObserver(scheduleEnhance).observe(document.body, {
    childList: true,
    subtree: true,
  });
})();
