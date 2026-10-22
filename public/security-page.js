(() => {
  "use strict";

  const statusNode = document.getElementById("security-status");
  const closureStatus = document.getElementById("closure-status");
  const saveRow = document.getElementById("password-save-row");
  const saveButton = document.getElementById("save-password");
  const passwordFields = ["current-password", "new-password", "confirm-password"]
    .map((id) => document.getElementById(id));
  const overlay = document.getElementById("closure-overlay");
  const closurePassword = document.getElementById("closure-password");
  const closureConfirm = document.getElementById("closure-confirm");
  const confirmClosure = document.getElementById("confirm-closure");
  let savedNickname = "";
  let closing = false;

  function status(message, kind = "") {
    statusNode.textContent = message || "";
    if (kind) statusNode.dataset.kind = kind;
    else delete statusNode.dataset.kind;
  }

  function closureMessage(message, kind = "") {
    closureStatus.textContent = message || "";
    if (kind) closureStatus.dataset.kind = kind;
    else delete closureStatus.dataset.kind;
  }

  function canConfirm() {
    return Boolean(closurePassword.value) && closureConfirm.value === "注销账号";
  }

  function refreshClosureButton() {
    confirmClosure.disabled = closing || !canConfirm();
  }

  async function loadProfile() {
    status("正在读取安全设置…");
    const response = await fetch("/auth/profile", { cache: "no-store" });
    if (response.status === 401) location.replace("/login");
    if (!response.ok) throw new Error("安全设置读取失败");
    const profile = await response.json();
    savedNickname = profile.nickname || "";
    status("");
  }

  function passwordChanged() {
    return passwordFields.some((field) => Boolean(field.value));
  }

  function refreshSaveRow() {
    const shouldShow = passwordChanged();
    const wasHidden = saveRow.hidden;
    saveRow.hidden = !shouldShow;
    if (!shouldShow || !wasHidden) return;
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    // Explicit Web Animations keeps the reveal reliable even when display
    // toggling is batched by the browser.
    saveRow.animate(
      [
        { opacity: 0, transform: "translateY(12px) scale(.98)" },
        { opacity: 1, transform: "translateY(0) scale(1)" },
      ],
      { duration: 280, easing: "cubic-bezier(.22, 1, .36, 1)", fill: "both" }
    );
  }

  async function changePassword(event) {
    event.preventDefault();
    const current = document.getElementById("current-password").value;
    const next = document.getElementById("new-password").value;
    const confirm = document.getElementById("confirm-password").value;
    if (!current) return status("请输入当前密码。", "error");
    if (next.length < 12) return status("新密码至少 12 位。", "error");
    if (next !== confirm) return status("两次输入的新密码不一致。", "error");

    saveButton.disabled = true;
    saveButton.querySelector(".button-label").textContent = "更新中…";
    status("正在更新密码…");
    try {
      const response = await fetch("/auth/profile", {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          nickname: savedNickname,
          current_password: current,
          new_password: next,
        }),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.detail || "密码更新未完成");
      document.getElementById("current-password").value = "";
      document.getElementById("new-password").value = "";
      document.getElementById("confirm-password").value = "";
      setPressed(false);
      refreshSaveRow();
      status("密码已更新。", "success");
    } catch (error) {
      status(error.message || "密码更新未完成。", "error");
    } finally {
      saveButton.disabled = false;
      saveButton.querySelector(".button-label").textContent = "更新密码";
    }
  }

  function setPressed(visible) {
    document.querySelectorAll(".eye-button").forEach((button) => {
      const input = document.getElementById(button.dataset.togglePassword);
      if (!input) return;
      input.type = visible ? "text" : "password";
      button.setAttribute("aria-pressed", String(visible));
      button.setAttribute("aria-label", visible ? "隐藏密码" : "显示密码");
    });
  }

  document.querySelectorAll(".eye-button").forEach((button) => {
    button.addEventListener("click", () => {
      const input = document.getElementById(button.dataset.togglePassword);
      if (!input) return;
      setPressed(input.type === "password");
    });
  });

  async function logout() {
    await fetch("/logout", { method: "POST" }).catch(() => {});
    location.href = "/login";
  }

  function openClosure() {
    closurePassword.value = "";
    closureConfirm.value = "";
    closureMessage("");
    confirmClosure.disabled = true;
    overlay.hidden = false;
    closurePassword.focus();
  }

  function closeClosure() {
    if (closing) return;
    overlay.hidden = true;
  }

  async function closeAccount() {
    if (closing || !canConfirm()) return;
    closing = true;
    confirmClosure.disabled = true;
    confirmClosure.textContent = "注销中…";
    closureMessage("正在注销账号…");
    try {
      const response = await fetch("/auth/profile/close", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          current_password: closurePassword.value,
          confirmation: closureConfirm.value,
        }),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.detail || "账号注销未完成");
      location.href = data.redirect_url || "/login";
    } catch (error) {
      closureMessage(error.message || "账号注销未完成。", "error");
      closing = false;
      confirmClosure.textContent = "确认注销";
      refreshClosureButton();
    }
  }

  passwordFields.forEach((field) => {
    field.addEventListener("input", refreshSaveRow);
  });
  document.getElementById("password-form").addEventListener("keydown", (event) => {
    const isEnter = event.key === "Enter" || event.keyCode === 13;
    const fromInput = event.target instanceof HTMLInputElement;
    if (!isEnter || !fromInput || event.shiftKey) return;
    event.preventDefault();
    if (!passwordChanged() || saveButton.disabled) return;
    saveButton.click();
  });
  document.getElementById("password-form").addEventListener("submit", changePassword);
  document.getElementById("logout-security").addEventListener("click", logout);
  document.getElementById("open-closure").addEventListener("click", openClosure);
  document.getElementById("cancel-closure").addEventListener("click", closeClosure);
  overlay.addEventListener("click", (event) => {
    if (event.target === overlay) closeClosure();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !overlay.hidden) closeClosure();
  });
  closurePassword.addEventListener("input", refreshClosureButton);
  closureConfirm.addEventListener("input", refreshClosureButton);
  confirmClosure.addEventListener("click", closeAccount);
  window.AlgoSageTheme?.mountFloatingButton();

  loadProfile().catch((error) => status(error.message || "安全设置读取失败。", "error"));
})();
