/* ExamGuard 消息复制按钮、主题菜单状态与交互动画补充。
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

/* 账号头像：查看/修改个人信息，退出登录继续使用 Chainlit 原生菜单项。 */
(function enableStudentProfile() {
  const OVERLAY_ID = "eg-profile-overlay";

  function loadProfile() {
    return fetch("/auth/profile", { cache: "no-store" })
      .then((response) => (response.ok ? response.json() : null))
      .then((profile) => {
        document.documentElement.dataset.egAuthMode = profile ? profile.mode : "none";
        return profile;
      })
      .catch(() => {
        document.documentElement.dataset.egAuthMode = "none";
        return null;
      });
  }

  function dialog() {
    let surface = document.getElementById(OVERLAY_ID);
    if (surface) return surface;
    surface = document.createElement("div");
    surface.id = OVERLAY_ID;
    surface.setAttribute("role", "dialog");
    surface.setAttribute("aria-modal", "true");
    surface.setAttribute("aria-labelledby", "eg-profile-title");
    surface.innerHTML = `
      <div id="eg-profile-card">
        <h2 class="eg-profile-title" id="eg-profile-title">个人信息</h2>
        <p class="eg-profile-subtitle" id="eg-profile-subtitle"></p>
        <form id="eg-profile-form">
          <div class="eg-profile-field">
            <label for="eg-profile-nickname">昵称</label>
            <input id="eg-profile-nickname" maxlength="40" autocomplete="nickname" required>
          </div>
          <div class="eg-profile-field">
            <label for="eg-profile-student-id" id="eg-profile-account-label">学号</label>
            <input id="eg-profile-student-id" readonly>
          </div>
          <div class="eg-profile-field" id="eg-profile-email-field">
            <label for="eg-profile-email">教育邮箱</label>
            <input id="eg-profile-email" readonly>
          </div>
          <div class="eg-profile-password" id="eg-profile-password-section">
            <h3>修改密码</h3>
            <div class="eg-profile-field">
              <label for="eg-profile-current-password">当前密码</label>
              <input id="eg-profile-current-password" type="password" autocomplete="current-password">
            </div>
            <div class="eg-profile-field">
              <label for="eg-profile-new-password">新密码</label>
              <input id="eg-profile-new-password" type="password" autocomplete="new-password" minlength="12">
            </div>
            <div class="eg-profile-field">
              <label for="eg-profile-confirm-password">确认新密码</label>
              <input id="eg-profile-confirm-password" type="password" autocomplete="new-password" minlength="12">
            </div>
          </div>
          <p id="eg-profile-status"></p>
          <div class="eg-profile-actions">
            <button type="button" id="eg-profile-cancel">取消</button>
            <button type="submit" id="eg-profile-save">保存</button>
          </div>
        </form>
      </div>
    `;
    document.body.appendChild(surface);
    surface.querySelector("#eg-profile-cancel").addEventListener("click", closeProfile);
    surface.addEventListener("click", (event) => {
      if (event.target === surface) closeProfile();
    });
    surface.querySelector("#eg-profile-form").addEventListener("submit", saveProfile);
    return surface;
  }

  function status(message, kind) {
    const node = document.getElementById("eg-profile-status");
    node.textContent = message || "";
    if (kind) node.dataset.kind = kind;
    else delete node.dataset.kind;
  }

  async function openProfile() {
    const surface = dialog();
    const form = surface.querySelector("#eg-profile-form");
    form.reset();
    status("正在读取个人信息…");
    surface.dataset.open = "true";
    try {
      const response = await fetch("/auth/hust/profile", { cache: "no-store" });
      if (!response.ok) throw new Error("个人信息读取失败");
      const profile = await response.json();
      const hust = profile.mode === "hust";
      surface.dataset.mode = profile.mode;
      surface.querySelector("#eg-profile-subtitle").textContent = hust
        ? "学号和教育邮箱由学校账号绑定，不能在这里修改。"
        : "本地工作区身份保存在当前浏览器 Cookie 中。";
      surface.querySelector("#eg-profile-account-label").textContent = hust ? "学号" : "工作区标识";
      surface.querySelector("#eg-profile-email-field").style.display = hust ? "" : "none";
      surface.querySelector("#eg-profile-password-section").style.display = hust ? "" : "none";
      surface.querySelector("#eg-profile-nickname").value = profile.nickname || "";
      surface.querySelector("#eg-profile-student-id").value = hust ? profile.student_id : profile.identifier;
      if (hust) surface.querySelector("#eg-profile-email").value = profile.email || "";
      status("");
      setTimeout(() => surface.querySelector("#eg-profile-nickname").focus(), 30);
    } catch (error) {
      status(error.message || "个人信息读取失败", "error");
    }
  }

  function closeProfile() {
    const surface = document.getElementById(OVERLAY_ID);
    if (surface) surface.dataset.open = "false";
  }

  async function saveProfile(event) {
    event.preventDefault();
    const surface = document.getElementById(OVERLAY_ID);
    const nickname = surface.querySelector("#eg-profile-nickname").value.trim();
    const current = surface.querySelector("#eg-profile-current-password").value;
    const next = surface.querySelector("#eg-profile-new-password").value;
    const confirm = surface.querySelector("#eg-profile-confirm-password").value;
    if (!nickname) return status("请填写昵称", "error");
    if (!current && !next && !confirm) {
      // 只改昵称时不发送密码字段。
    } else {
      if (!current || !next || !confirm) return status("修改密码需要填写当前密码、新密码和确认密码", "error");
      if (next !== confirm) return status("两次输入的新密码不一致", "error");
    }
    status("正在保存…");
    try {
      const body = { nickname };
      if (current || next || confirm) {
        body.current_password = current;
        body.new_password = next;
      }
      const response = await fetch("/auth/profile", {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.detail || "保存未完成");
      const avatar = document.querySelector("#user-nav-button span > span:last-child");
      if (avatar) avatar.textContent = (data.nickname || nickname).slice(0, 1);
      closeProfile();
    } catch (error) {
      status(error.message || "保存未完成", "error");
    }
  }

  function addProfileMenuItem(menu) {
    if (menu.querySelector("#eg-profile-menu-item")) return;
    const logout = [...menu.querySelectorAll('[role="menuitem"]')].pop();
    const item = document.createElement("div");
    item.id = "eg-profile-menu-item";
    item.setAttribute("role", "menuitem");
    item.className = "relative flex cursor-default select-none items-center gap-2 rounded-sm px-2 py-2.5 text-sm outline-none transition-colors focus:bg-accent focus:text-accent-foreground data-[disabled]:pointer-events-none data-[disabled]:opacity-50";
    item.innerHTML = '<span class="eg-profile-dot"></span><span>个人信息</span>';
    item.addEventListener("click", (event) => {
      event.preventDefault();
      event.stopPropagation();
      document.dispatchEvent(new KeyboardEvent("keydown", {
        key: "Escape", bubbles: true, cancelable: true,
      }));
      setTimeout(openProfile, 40);
    }, true);
    if (logout && logout.parentElement === menu) menu.insertBefore(item, logout);
    else menu.appendChild(item);
  }

  function scanMenu() {
      if (!["hust", "local"].includes(document.documentElement.dataset.egAuthMode)) return;
    document.querySelectorAll('[role="menu"][data-state="open"]').forEach(addProfileMenuItem);
  }

  function startProfileUI() {
    document.addEventListener("keydown", (event) => {
      if (event.key === "Escape") closeProfile();
    });
    const observer = new MutationObserver(scanMenu);
    observer.observe(document.body, { childList: true, subtree: true, attributes: true, attributeFilter: ["data-state"] });
  }

  function bootProfile() {
    loadProfile().then((profile) => {
      if (profile) startProfileUI();
    });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", bootProfile);
  else bootProfile();
})();
