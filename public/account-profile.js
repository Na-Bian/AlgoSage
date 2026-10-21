/* AlgoSage 消息复制按钮、主题菜单状态与交互动画补充。
   通过 Chainlit custom_js 注入；只复制页面上已渲染的最终文本，
   不访问任何密钥、系统提示词或未送达内容。
   DOM 结构依据 Chainlit 2.11 实测：每条消息顶层为 div[id^='step-']，
   助手正文在 .message-content，用户气泡为 .bg-accent.rounded-3xl。
   防御式查询：前端升级时按钮可能失效，但不影响聊天与安全功能。 */
(function () {
  "use strict";

  function isMessageBlock(el) {
    return (
      el.tagName === "DIV" &&
      /^step-/.test(el.id || "") &&
      !!(el.querySelector(".message-content") || el.querySelector(".bg-accent.rounded-3xl"))
    );
  }

  function messageText(block) {
    var content =
      block.querySelector(".message-content") ||
      block.querySelector(".bg-accent.rounded-3xl");
    return ((content && content.innerText) || "").trim();
  }

  function makeButton(block) {
    var btn = document.createElement("button");
    btn.type = "button";
    btn.className = "eg-copy-btn";
    btn.textContent = "复制";
    btn.addEventListener("click", function (ev) {
      ev.stopPropagation();
      ev.preventDefault();
      var text = messageText(block);
      if (!text) return;
      var done = function (label) {
        btn.textContent = label;
        setTimeout(function () {
          btn.textContent = "复制";
        }, 1500);
      };
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(
          function () {
            done("已复制 ✓");
          },
          function () {
            done("复制失败");
          }
        );
      } else {
        done("浏览器不支持");
      }
    });
    return btn;
  }

  function decorate(block) {
    if (block.dataset.egCopy === "1") return;
    block.dataset.egCopy = "1";
    block.appendChild(makeButton(block));
  }

  var pending = null;
  function scheduleScan() {
    if (pending) return;
    pending = setTimeout(function () {
      pending = null;
      scanAll();
    }, 150);
  }

  function scanAll() {
    var blocks = document.querySelectorAll("main div[id^='step-']");
    for (var i = 0; i < blocks.length; i++) {
      if (isMessageBlock(blocks[i])) decorate(blocks[i]);
    }
    syncThemeMenu();
  }

  function syncThemeMenu() {
    var trigger = document.getElementById("theme-toggle");
    var menuId = trigger && trigger.getAttribute("aria-controls");
    var menu = menuId && document.getElementById(menuId);
    if (!menu || menu.getAttribute("data-state") !== "open") return;
    var items = menu.querySelectorAll('[role="menuitem"], [role="menuitemradio"]');
    // Chainlit 2.11 原生菜单顺序；限定到主题触发器关联的菜单，不影响其他菜单。
    if (items.length !== 3) return;
    var values = ["light", "dark", "system"];
    var preference;
    try {
      preference = localStorage.getItem("vite-ui-theme");
    } catch (_) {}
    if (values.indexOf(preference) === -1) {
      preference = document.documentElement.classList.contains("dark") ? "dark" : "light";
    }
    for (var i = 0; i < items.length; i++) {
      items[i].setAttribute("role", "menuitemradio");
      items[i].setAttribute("data-eg-theme-option", values[i]);
      items[i].setAttribute("aria-checked", String(values[i] === preference));
    }
  }

  // Chainlit 设置组件在关闭时直接卸载，CSS 无法单独完成退出动画。
  // 只延迟关闭/确认按钮的原生操作，不复制业务逻辑或修改表单值。
  var replayingButton = null;
  function settingsSurface() {
    var title = document.getElementById("chat-settings-sidebar-title");
    return title && (title.closest('[role="dialog"]') || title.closest("[data-panel]"));
  }

  function settingsCloseButton(surface) {
    var header = surface.querySelector("#chat-settings-sidebar-title button");
    if (header) return header;
    var label = surface.querySelector("button > .sr-only");
    return label && label.closest("button");
  }

  function activateSettingsButton(button, surface) {
    if (button.isConnected) {
      replayingButton = button;
      try { button.click(); } finally { replayingButton = null; }
    }
    if (surface.hasAttribute("data-panel")) requestAnimationFrame(function () {
      var trigger = document.getElementById("chat-settings-header-button");
      if (trigger && !settingsSurface() && !document.querySelector('[role="dialog"]')) trigger.focus();
    });
  }

  function animateSettingsExit(surface, button) {
    if (!button || !surface.animate || matchMedia("(prefers-reduced-motion: reduce)").matches) {
      return false;
    }
    if (surface.hasAttribute("data-eg-motion-closing")) return true;
    surface.setAttribute("data-eg-motion-closing", "");
    var style = getComputedStyle(surface);
    var raw = style.getPropertyValue("--eg-motion-exit").trim();
    var duration = parseFloat(raw) * (raw.endsWith("ms") ? 1 : 1000) || 260;
    var ease = style.getPropertyValue("--eg-motion-ease").trim() || "ease-out";
    var from = { opacity: style.opacity, translate: "0 0" };
    var distance = style.getPropertyValue("--eg-motion-drawer-distance").trim() || "12px";
    var to = { opacity: 0, translate: distance + " 0" };
    if (surface.hasAttribute("data-panel")) {
      from.flexGrow = style.flexGrow;
      to.flexGrow = "0";
    }
    var animations = [surface.animate([from, to], { duration: duration, easing: ease, fill: "forwards" })];
    var overlay = surface.previousElementSibling;
    if (overlay && overlay.matches('[data-state].fixed.inset-0')) {
      animations.push(overlay.animate([{ opacity: 1 }, { opacity: 0 }], {
        duration: duration, easing: ease, fill: "forwards"
      }));
    }
    var done = false;
    function finish() {
      if (done) return;
      done = true;
      clearTimeout(fallback);
      activateSettingsButton(button, surface);
      animations.forEach(function (animation) { animation.cancel(); });
      surface.removeAttribute("data-eg-motion-closing");
    }
    var fallback = setTimeout(finish, duration + 80);
    Promise.allSettled(animations.map(function (animation) { return animation.finished; })).then(finish);
    return true;
  }

  function interceptSettingsClose(event) {
    var surface = settingsSurface();
    if (!surface) return;
    var dialogs = document.querySelectorAll('[role="dialog"][data-state="open"]');
    if (dialogs.length && dialogs[dialogs.length - 1] !== surface) return;
    var button = event.target.closest && event.target.closest("button");
    if (button === replayingButton && button) return;
    var close = settingsCloseButton(surface);
    var confirm = surface.querySelector("#confirm-sidebar");
    var cancel = confirm && confirm.previousElementSibling;
    var headerToggle = event.type === "click" && button && button.id === "chat-settings-header-button";
    if (event.type === "keydown") {
      if (event.key !== "Escape" || document.querySelector('[role="listbox"][data-state="open"], [role="menu"][data-state="open"]')) return;
      button = close;
    } else if (event.type === "pointerdown") {
      if (event.target !== surface.previousElementSibling || !surface.matches('[role="dialog"]')) return;
      button = close;
    } else if (headerToggle) {
      // 顶栏原生按钮只负责打开；再次点击时改走取消，避免提交未确认的修改。
      button = close;
    } else if (!surface.contains(button) || (button !== close && button !== confirm && button !== cancel)) {
      return;
    }
    var animated = animateSettingsExit(surface, button);
    // Escape/遮罩/顶栏切换需代为点击关闭；减少动态效果时也要阻止原按钮重新打开。
    if (animated || (button && (event.type !== "click" || headerToggle))) {
      event.preventDefault();
      event.stopImmediatePropagation();
      if (!animated) activateSettingsButton(button, surface);
    }
  }

  function start() {
    document.addEventListener("click", interceptSettingsClose, true);
    document.addEventListener("keydown", interceptSettingsClose, true);
    document.addEventListener("pointerdown", interceptSettingsClose, true);
    var observer = new MutationObserver(scheduleScan);
    observer.observe(document.body, { childList: true, subtree: true });
    // class 变化包含主题切换；storage 处理其他窗口修改偏好的情况。
    new MutationObserver(scheduleScan).observe(document.documentElement, {
      attributes: true, attributeFilter: ["class"]
    });
    window.addEventListener("storage", function (event) {
      if (event.key === "vite-ui-theme") scheduleScan();
    });
    scanAll();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }
})();


/* HUST 账号菜单：个人信息进入独立页面；退出登录保持 Chainlit 原生行为。 */
(function studentProfileMenu() {
  "use strict";

  function addMenuItem(menu) {
    if (menu.querySelector("#eg-profile-menu-item")) return;
    const logout = [...menu.querySelectorAll('[role="menuitem"]')].pop();
    const link = document.createElement("a");
    link.id = "eg-profile-menu-item";
    link.href = "/profile";
    link.setAttribute("role", "menuitem");
    link.className = "relative flex cursor-default select-none items-center gap-2 rounded-sm px-2 py-2.5 text-sm outline-none transition-colors focus:bg-accent focus:text-accent-foreground data-[disabled]:pointer-events-none data-[disabled]:opacity-50";
    link.textContent = "个人信息";
    if (logout && logout.parentElement === menu) menu.insertBefore(link, logout);
    else menu.appendChild(link);
  }

  function scan() {
    document.querySelectorAll('[role="menu"][data-state="open"]').forEach(addMenuItem);
  }

  function start() {
    const observer = new MutationObserver(scan);
    observer.observe(document.body, { childList: true, subtree: true, attributes: true, attributeFilter: ["data-state"] });
    scan();
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
  else start();
})();
