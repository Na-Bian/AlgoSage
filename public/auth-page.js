(() => {
  "use strict";
  const body = document.body;
  const statusNode = document.getElementById("auth-status");
  const forms = {
    login: document.getElementById("login-form"),
    register: document.getElementById("register-form"),
    reset: document.getElementById("reset-form"),
  };
  const titles = { login: "HUST 学生登录", register: "注册 HUST 账号", reset: "找回密码" };
  const timers = { register: null, reset: null };

  function applyTheme() {
    let preference = null;
    try {
      preference = localStorage.getItem("vite-ui-theme");
    } catch (_) {}
    const systemDark = window.matchMedia("(prefers-color-scheme: dark)").matches;
    const dark = preference === "dark" || ((preference === null || preference === "system") && systemDark);
    document.documentElement.classList.toggle("dark", dark);
    const logo = document.getElementById("auth-logo");
    if (logo) logo.src = `/logo?theme=${dark ? "dark" : "light"}&v=${dark ? "1" : "0"}`;
  }

  window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", applyTheme);
  window.addEventListener("storage", (event) => {
    if (event.key === "vite-ui-theme") applyTheme();
  });
  applyTheme();

  document.querySelectorAll("[data-toggle-password]").forEach((button) => {
    button.addEventListener("click", () => {
      const input = document.getElementById(button.dataset.togglePassword);
      const showing = input.type === "text";
      input.type = showing ? "password" : "text";
      button.setAttribute("aria-label", showing ? "显示密码" : "隐藏密码");
    });
  });

  function status(message, kind = "") {
    statusNode.textContent = message || "";
    if (kind) statusNode.dataset.kind = kind;
    else delete statusNode.dataset.kind;
  }

  function setMode(mode) {
    body.dataset.mode = mode;
    document.getElementById("auth-title").textContent = titles[mode];
    Object.entries(forms).forEach(([name, form]) => {
      form.dataset.active = String(name === mode);
    });
    [...document.querySelectorAll(".eg-auth-tab")].forEach((tab) => {
      tab.setAttribute("aria-selected", String(tab.dataset.target === mode));
    });
    status("");
  }

  async function requestJSON(path, options) {
    const response = await fetch(path, options);
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      const detail = Array.isArray(data.detail) ? "请检查表单填写" : data.detail;
      throw new Error(detail || "请求未完成");
    }
    return data;
  }

  async function login(event) {
    event.preventDefault();
    const studentId = document.getElementById("login-student-id").value.trim().toUpperCase();
    const password = document.getElementById("login-password").value;
    status("正在登录…");
    try {
      const body = new URLSearchParams({ username: studentId, password });
      const response = await fetch("/login", { method: "POST", body });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) {
        throw new Error(data.detail === "credentialssignin" ? "学号或密码不正确" : "登录未完成");
      }
      status("登录成功，正在进入应用…", "success");
      location.href = "/";
    } catch (error) {
      status(error.message || "登录未完成", "error");
    }
  }

  function passwordFields(formPrefix) {
    return {
      password: document.getElementById(`${formPrefix}-password`),
      confirm: formPrefix === "register" ? document.getElementById("register-confirm") : document.getElementById("reset-confirm"),
    };
  }

  async function sendCode(kind) {
    const studentId = document.getElementById(`${kind}-student-id`).value.trim().toUpperCase();
    const { password, confirm } = passwordFields(kind);
    status("正在发送验证码…");
    try {
      if (kind === "register") {
        if (!password || password.length < 12) throw new Error("密码至少 12 位");
        if (password !== confirm.value) throw new Error("两次输入的密码不一致");
        await requestJSON("/auth/hust/register/request-code", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            nickname: document.getElementById("register-nickname").value.trim(),
            student_id: studentId,
            password,
          }),
        });
      } else {
        await requestJSON("/auth/hust/password-reset/request-code", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ student_id: studentId }),
        });
      }
      status(`验证码已发送到 ${studentId}@hust.edu.cn`, "success");
      const button = document.querySelector(`[data-send="${kind}"]`);
      let seconds = 60;
      button.disabled = true;
      const original = button.textContent;
      timers[kind] = setInterval(() => {
        button.textContent = `${seconds}s`;
        seconds -= 1;
        if (seconds < 0) {
          clearInterval(timers[kind]);
          button.textContent = original;
          button.disabled = false;
        }
      }, 1000);
    } catch (error) {
      status(error.message || "验证码发送未完成", "error");
    }
  }

  async function register(event) {
    event.preventDefault();
    const studentId = document.getElementById("register-student-id").value.trim().toUpperCase();
    status("正在创建账号…");
    try {
      await requestJSON("/auth/hust/register/confirm", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          student_id: studentId,
          code: document.getElementById("register-code").value.trim(),
        }),
      });
      status("账号已创建，请登录", "success");
      setTimeout(() => setMode("login"), 700);
    } catch (error) {
      status(error.message || "注册未完成", "error");
    }
  }

  async function reset(event) {
    event.preventDefault();
    const studentId = document.getElementById("reset-student-id").value.trim().toUpperCase();
    status("正在重置密码…");
    try {
      await requestJSON("/auth/hust/password-reset/confirm", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          student_id: studentId,
          code: document.getElementById("reset-code").value.trim(),
          new_password: document.getElementById("reset-password").value,
        }),
      });
      status("密码已重置，请登录", "success");
      setTimeout(() => setMode("login"), 700);
    } catch (error) {
      status(error.message || "密码重置未完成", "error");
    }
  }

  document.querySelectorAll(".eg-auth-tab").forEach((tab) => {
    tab.addEventListener("click", () => setMode(tab.dataset.target));
  });
  document.querySelectorAll("[data-goto]").forEach((button) => {
    button.addEventListener("click", () => setMode(button.dataset.goto));
  });
  document.querySelectorAll("[data-send]").forEach((button) => {
    button.addEventListener("click", () => sendCode(button.dataset.send));
  });
  forms.login.addEventListener("submit", login);
  forms.register.addEventListener("submit", register);
  forms.reset.addEventListener("submit", reset);

  const initial = body.dataset.mode || "login";
  setMode(initial in forms ? initial : "login");
})();
