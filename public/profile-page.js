(() => {
  "use strict";

  const statusNode = document.getElementById("profile-status");
  const nicknameInput = document.getElementById("nickname");
  const saveRow = document.getElementById("profile-save-row");
  const saveButton = document.getElementById("save-profile");
  const avatarInput = document.getElementById("avatar-input");
  const avatarImage = document.getElementById("avatar-image");
  const avatarFallback = document.getElementById("avatar-initial");
  let savedNickname = "";
  let uploading = false;

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
    }
    avatarFallback.textContent = (profile.nickname || profile.student_id || "学").slice(0, 1).toUpperCase();
  }

  function fillProfile(profile) {
    savedNickname = profile.nickname || "";
    nicknameInput.value = savedNickname;
    document.getElementById("student-id").value = profile.student_id || "";
    document.getElementById("email").value = profile.email || "";
    refreshAvatar(profile);
    saveRow.hidden = true;
  }

  function nicknameChanged() {
    return nicknameInput.value.trim() !== savedNickname;
  }

  async function loadProfile() {
    status("正在读取个人信息…");
    const response = await fetch("/auth/profile", { cache: "no-store" });
    if (response.status === 401) location.replace("/login");
    if (!response.ok) throw new Error("个人信息读取失败");
    fillProfile(await response.json());
    status("");
  }

  async function saveProfile(event) {
    event.preventDefault();
    if (!nicknameChanged()) return;
    const nickname = nicknameInput.value.trim();
    if (!nickname) {
      status("昵称不能为空。", "error");
      return;
    }

    saveButton.disabled = true;
    saveButton.querySelector(".button-label").textContent = "保存中…";
    status("正在保存…");
    try {
      const response = await fetch("/auth/profile", {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ nickname }),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.detail || "保存未完成");
      fillProfile(data);
      status("资料已保存。", "success");
    } catch (error) {
      status(error.message || "保存未完成。", "error");
    } finally {
      saveButton.disabled = false;
      saveButton.querySelector(".button-label").textContent = "保存修改";
      saveRow.hidden = !nicknameChanged();
    }
  }

  function selectedAvatarFile(event) {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (!file || uploading) return;
    const validationError = window.AlgoSageAvatarCrop?.validate(file);
    if (validationError) {
      status(validationError, "error");
      return;
    }
    try {
      window.AlgoSageAvatarCrop.open(file, { onSave: uploadAvatar });
    } catch (error) {
      status(error.message || "头像编辑未完成。", "error");
    }
  }

  async function uploadAvatar(file) {
    if (!file || uploading) return;
    uploading = true;
    status("正在上传头像…");
    try {
      const response = await fetch("/auth/avatar", {
        method: "PUT",
        headers: { "Content-Type": file.type || "application/octet-stream" },
        body: file,
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.detail || "头像上传未完成");
      fillProfile(data);
      // Chat tabs listen for this version marker and refresh their native avatar.
      localStorage.setItem("algosage-avatar-version", String(Date.now()));
      status("头像已更新。", "success");
    } catch (error) {
      status(error.message || "头像上传未完成。", "error");
      throw error;
    } finally {
      uploading = false;
    }
  }

  window.AlgoSageTheme?.mountFloatingButton();
  avatarInput.addEventListener("change", selectedAvatarFile);
  document.getElementById("change-avatar").addEventListener("click", () => avatarInput.click());
  nicknameInput.addEventListener("input", () => {
    if (statusNode.dataset.kind === "success") status("");
    saveRow.hidden = !nicknameChanged();
  });
  document.getElementById("profile-form").addEventListener("submit", saveProfile);
  window.addEventListener("beforeunload", (event) => {
    if (nicknameChanged()) event.preventDefault();
  });

  loadProfile().catch((error) => status(error.message || "个人信息读取失败。", "error"));
})();
