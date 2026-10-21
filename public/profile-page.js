(() => {
  "use strict";
  const statusNode = document.getElementById("profile-status");
  const saveButton = document.getElementById("save-profile");
  const avatarInput = document.getElementById("avatar-input");
  const avatarImage = document.getElementById("avatar-image");
  const avatarFallback = document.getElementById("avatar-initial");

  function status(message, kind = "") {
    statusNode.textContent = message || "";
    if (kind) statusNode.dataset.kind = kind;
    else delete statusNode.dataset.kind;
  }

  function refreshAvatar(profile) {
    const frame = document.getElementById("avatar-frame");
    if (profile.has_avatar) {
      avatarImage.src = `/auth/avatar/me?v=${encodeURIComponent(profile.avatar_updated_at || Date.now())}`;
      frame.classList.add("has-image");
    } else {
      avatarImage.removeAttribute("src");
      frame.classList.remove("has-image");
      avatarFallback.textContent = (profile.nickname || profile.student_id || "学").slice(0, 1).toUpperCase();
    }
  }

  function fillProfile(profile) {
    document.getElementById("nickname").value = profile.nickname || "";
    document.getElementById("student-id").value = profile.student_id || "";
    document.getElementById("email").value = profile.email || "";
    avatarFallback.textContent = (profile.nickname || profile.student_id || "学").slice(0, 1).toUpperCase();
    refreshAvatar(profile);
  }

  async function loadProfile() {
    status("正在读取个人信息…");
    const response = await fetch("/auth/profile", { cache: "no-store" });
    if (response.status === 401) location.replace("/login");
    if (!response.ok) throw new Error("个人信息读取失败");
    fillProfile(await response.json());
    status("");
  }

  function profileFromResponse(payload) {
    return {
      ...payload,
      has_avatar: payload.has_avatar,
      avatar_updated_at: payload.avatar_updated_at,
    };
  }

  async function saveProfile(event) {
    event.preventDefault();
    saveButton.disabled = true;
    status("正在保存…");
    try {
      const current = document.getElementById("current-password").value;
      const next = document.getElementById("new-password").value;
      const confirm = document.getElementById("confirm-password").value;
      const changingPassword = Boolean(current || next || confirm);
      if (changingPassword && (!current || !next || !confirm)) {
        throw new Error("修改密码需要填写当前密码、新密码和确认密码");
      }
      if (changingPassword && next !== confirm) throw new Error("两次输入的新密码不一致");
      const response = await fetch("/auth/profile", {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          nickname: document.getElementById("nickname").value.trim(),
          ...(changingPassword ? { current_password: current, new_password: next } : {}),
        }),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.detail || "保存未完成");
      fillProfile(profileFromResponse(data));
      status("资料已保存", "success");
      if (changingPassword) {
        document.getElementById("current-password").value = "";
        document.getElementById("new-password").value = "";
        document.getElementById("confirm-password").value = "";
      }
    } catch (error) {
      status(error.message || "保存未完成", "error");
    } finally {
      saveButton.disabled = false;
    }
  }

  async function uploadAvatar(event) {
    const file = event.target.files?.[0];
    if (!file) return;
    status("正在上传头像…");
    try {
      const response = await fetch("/auth/avatar", {
        method: "PUT",
        headers: { "Content-Type": file.type || "application/octet-stream" },
        body: file,
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.detail || "头像上传未完成");
      fillProfile(profileFromResponse(data));
      status("头像已更新", "success");
    } catch (error) {
      status(error.message || "头像上传未完成", "error");
    } finally {
      event.target.value = "";
    }
  }

  async function logout() {
    await fetch("/logout", { method: "POST" }).catch(() => {});
    location.href = "/login";
  }

  document.getElementById("profile-form").addEventListener("submit", saveProfile);
  avatarInput.addEventListener("change", uploadAvatar);
  document.getElementById("change-avatar").addEventListener("click", () => avatarInput.click());
  document.getElementById("logout-profile").addEventListener("click", logout);
  loadProfile().catch((error) => status(error.message || "个人信息读取失败", "error"));
})();
