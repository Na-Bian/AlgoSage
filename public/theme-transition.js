/* AlgoSage global theme runtime.
   Standalone pages initialize the persisted Chainlit preference before CSS paints;
   the Chainlit UI only binds this runtime to its existing theme menu. */
(function () {
  "use strict";

  const THEME_KEY = "vite-ui-theme";
  const THEME_CHANGE_EVENT = "algosage-theme-change";
  const systemMedia = window.matchMedia("(prefers-color-scheme: dark)");
  const root = document.documentElement;
  let transitionBusy = false;

  function storedPreference() {
    try {
      const value = localStorage.getItem(THEME_KEY);
      return value === "light" || value === "dark" || value === "system" ? value : null;
    } catch (_) {
      return null;
    }
  }

  function savePreference(value) {
    try {
      localStorage.setItem(THEME_KEY, value);
    } catch (_) {
      // Private browsing can still switch the current document.
    }
  }

  function shouldUseDark(preference = storedPreference()) {
    if (preference === "light") return false;
    if (preference === "dark") return true;
    if (preference === "system") return systemMedia.matches;
    return root.dataset.themeDefault !== "light";
  }

  function applyPreference(preference) {
    root.classList.toggle("dark", shouldUseDark(preference));
    root.setAttribute("data-effective-theme", shouldUseDark(preference) ? "dark" : "light");
    updateFloatingButton();
  }

  function initializeStandalone() {
    // 独立页面（登录/注册/找回等，html 带 data-theme-default）始终跟随
    // 系统主题：忽略浏览器里保存的手动偏好，系统切换时实时跟随；也不
    // 提供切换入口——应用内的手动选择只作用于登录后的界面。
    applyPreference("system");
    systemMedia.addEventListener("change", () => applyPreference("system"));
  }

  function originFromElement(element) {
    const rect = element.getBoundingClientRect();
    const x = rect.left + rect.width / 2;
    const y = rect.top + rect.height / 2;
    return { x, y };
  }

  function revealRadius({ x, y }) {
    const left = Math.max(x, 0);
    const top = Math.max(y, 0);
    return Math.hypot(
      Math.max(left, window.innerWidth - left),
      Math.max(top, window.innerHeight - top)
    ) + 28;
  }

  function setOrigin(origin) {
    root.style.setProperty("--theme-origin-x", `${origin.x}px`);
    root.style.setProperty("--theme-origin-y", `${origin.y}px`);
    root.style.setProperty("--theme-reveal-radius", `${revealRadius(origin)}px`);
  }

  function setPreference(preference, origin = { x: window.innerWidth / 2, y: window.innerHeight / 2 }) {
    if (!["light", "dark", "system"].includes(preference)) {
      return Promise.resolve(false);
    }
    if (transitionBusy) return Promise.resolve(false);

    const nextDark = shouldUseDark(preference);
    const currentDark = root.classList.contains("dark");
    savePreference(preference);
    // Preference changes can leave the effective theme unchanged (for example
    // light -> system while the OS is light). Chainlit menu state still needs
    // to refresh, so do not rely only on an html.dark mutation.
    document.dispatchEvent(new CustomEvent(THEME_CHANGE_EVENT));

    if (nextDark === currentDark) {
      applyPreference(preference);
      return Promise.resolve(true);
    }

    const transitionKind = nextDark ? "to-dark" : "to-light";
    const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (reducedMotion || typeof document.startViewTransition !== "function") {
      transitionBusy = true;
      root.dataset.themeTransition = transitionKind;
      applyPreference(preference);
      // Keep the brief lock even in fallback so a noisy pointer cannot double-fire.
      return new Promise((resolve) => setTimeout(() => {
        delete root.dataset.themeTransition;
        transitionBusy = false;
        resolve(true);
      }, 60));
    }

    transitionBusy = true;
    setOrigin(origin);
    root.dataset.themeTransition = transitionKind;
    const transition = document.startViewTransition(() => applyPreference(preference));
    const finish = () => {
      transitionBusy = false;
      delete root.dataset.themeTransition;
      root.style.removeProperty("--theme-origin-x");
      root.style.removeProperty("--theme-origin-y");
      root.style.removeProperty("--theme-reveal-radius");
    };
    Promise.allSettled([transition.finished, transition.updateCallbackDone]).then(finish, finish);
    return transition.finished.catch(() => false);
  }

  function updateFloatingButton() {
    const button = document.getElementById("eg-theme-toggle");
    if (!button) return;
    const dark = root.classList.contains("dark");
    button.setAttribute("aria-label", dark ? "切换浅色主题" : "切换深色主题");
    button.setAttribute("aria-describedby", "eg-theme-toggle-tooltip");
    const tooltip = button.querySelector(".eg-theme-tooltip");
    if (tooltip) tooltip.textContent = dark ? "切换浅色主题" : "切换深色主题";
    button.dataset.icon = dark ? "sun" : "moon";
  }

  function mountFloatingButton() {
    if (document.getElementById("eg-theme-toggle")) return;
    const button = document.createElement("button");
    button.id = "eg-theme-toggle";
    button.className = "eg-theme-toggle";
    button.type = "button";
    button.innerHTML = `
      <svg class="icon-sun" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
        <circle cx="12" cy="12" r="4"></circle>
        <path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M6.34 17.66l-1.41 1.41M19.07 4.93l-1.41 1.41"></path>
      </svg>
      <svg class="icon-moon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
        <path d="M12 3a6 6 0 0 0 9 9 9 9 0 1 1-9-9Z"></path>
      </svg>
      <span id="eg-theme-toggle-tooltip" class="eg-theme-tooltip" role="tooltip"></span>
    `;
    button.addEventListener("click", (event) => {
      const next = root.classList.contains("dark") ? "light" : "dark";
      setPreference(next, originFromElement(button));
    });
    document.body.appendChild(button);
    updateFloatingButton();
  }

  function bindChainlitMenu() {
    document.addEventListener("click", (event) => {
      const item = event.target instanceof Element && event.target.closest("[data-eg-theme-option]");
      if (!item) return;
      const preference = item.getAttribute("data-eg-theme-option");
      if (!["light", "dark", "system"].includes(preference)) return;

      event.preventDefault();
      event.stopImmediatePropagation();
      setPreference(preference, originFromElement(item));
      // Close Chainlit's popover explicitly because its own click handler is bypassed.
      const trigger = document.getElementById("theme-toggle");
      if (trigger && trigger.getAttribute("aria-expanded") === "true") {
        requestAnimationFrame(() => trigger.click());
      }
    }, true);
  }

  window.AlgoSageTheme = {
    setPreference,
    mountFloatingButton,
    storedPreference,
    shouldUseDark,
    bindChainlitMenu,
  };

  // Suppress browser-native focus outlines that were reached with a pointer.
  // Keyboard focus is cleared from this marker and keeps a themed focus ring.
  let pointerFocusControl = null;
  document.addEventListener("pointerdown", (event) => {
    if (!(event.target instanceof Element)) return;
    const control = event.target.closest("button, a, [tabindex]:not(input):not(textarea):not(select)");
    if (pointerFocusControl) delete pointerFocusControl.dataset.egPointerFocus;
    pointerFocusControl = control || null;
    if (pointerFocusControl) pointerFocusControl.dataset.egPointerFocus = "1";
  }, true);
  document.addEventListener("keydown", (event) => {
    if (event.key !== "Tab") return;
    if (pointerFocusControl) delete pointerFocusControl.dataset.egPointerFocus;
    pointerFocusControl = null;
  }, true);

  if (root.dataset.themeDefault) initializeStandalone();
  bindChainlitMenu();
})();
