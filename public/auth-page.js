(() => {
  "use strict";

  const body = document.body;
  const view = body.dataset.view || "login";
  const statusNode = document.getElementById("auth-status");
  const titleNode = document.getElementById("auth-title");
  const titles = {
    login: "登录 AlgoSage",
    register: "注册 AlgoSage",
    reset: "找回密码",
  };

  let authFailed = false;
  let submitting = false;
  const countdowns = new Map();

  const activeForm = () => document.getElementById(`${view}-form`);
  const setError = (message, { authFailure = false } = {}) => {
    const feedback = document.getElementById("login-feedback");
    const error = document.getElementById("login-error");
    const studentInput = document.getElementById("login-student-id");
    const passwordInput = document.getElementById("login-password");
    authFailed = authFailure || (authFailed && view === "login");
    if (view === "login") {
      feedback.hidden = !(authFailure || Boolean(message));
      feedback.dataset.visible = String(authFailure || Boolean(message));
      const describedBy = authFailure || Boolean(message) ? "login-error" : null;
      if (describedBy) {
        studentInput.setAttribute("aria-describedby", describedBy);
        passwordInput.setAttribute("aria-describedby", describedBy);
      } else {
        studentInput.removeAttribute("aria-describedby");
        passwordInput.removeAttribute("aria-describedby");
      }
      error.textContent = message || (authFailure ? "学号或密码不正确，请重试。" : "");
      studentInput.setAttribute("aria-invalid", authFailure ? "true" : "false");
      passwordInput.setAttribute("aria-invalid", authFailure ? "true" : "false");
    } else {
      status(message, message ? "error" : "");
    }
  };

  const status = (message, kind = "") => {
    statusNode.textContent = message || "";
    if (kind) statusNode.dataset.kind = kind;
    else delete statusNode.dataset.kind;
  };

  const setViewTitle = () => {
    titleNode.textContent = titles[view] || titles.login;
    document.title = `${view === "login" ? "登录" : view === "register" ? "注册" : "找回密码"} · AlgoSage`;
  };

  function setBusy(form, busy) {
    const button = form.querySelector(".primary-button");
    const label = button?.querySelector(".button-label");
    if (!button) return;
    button.disabled = busy;
    button.setAttribute("aria-busy", String(busy));
    if (label && form.id === "login-form") label.textContent = busy ? "登录中…" : "登录";
    if (label && form.id === "register-form") label.textContent = busy ? "创建中…" : "创建账号";
    if (label && form.id === "reset-form") label.textContent = busy ? "重置中…" : "重置密码";
  }

  async function requestJSON(path, options) {
    const response = await fetch(path, options);
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      const detail = Array.isArray(data.detail)
        ? "请检查表单填写"
        : data.detail;
      const error = new Error(detail || "请求未完成");
      error.status = response.status;
      throw error;
    }
    return data;
  }

  function activatePasswordToggles() {
    document.querySelectorAll("[data-toggle-password]").forEach((button) => {
      button.addEventListener("click", () => {
        const input = document.getElementById(button.dataset.togglePassword);
        if (!input) return;
        const visible = input.type === "text";
        input.type = visible ? "password" : "text";
        button.setAttribute("aria-pressed", String(!visible));
        button.setAttribute("aria-label", visible ? "显示密码" : "隐藏密码");
      });
    });
  }

  async function login(event) {
    event.preventDefault();
    if (submitting) return;

    const form = event.currentTarget;
    const studentInput = document.getElementById("login-student-id");
    const passwordInput = document.getElementById("login-password");
    const studentId = studentInput.value.trim();
    const password = passwordInput.value;
    const feedback = document.getElementById("login-feedback");

    if (!studentId || !password) {
      setError("请输入学号和密码。");
      feedback.hidden = false;
      return;
    }

    submitting = true;
    setBusy(form, true);
    status("");
    try {
      const body = new URLSearchParams({ username: studentId, password });
      const response = await fetch("/login", { method: "POST", body });
      const data = await response.json().catch(() => ({}));

      if (response.status === 401) {
        setError("学号或密码不正确，请重试。", { authFailure: true });
        feedback.hidden = false;
        passwordInput.focus({ preventScroll: true });
        return;
      }

      if (!response.ok) {
        setError("登录服务暂时不可用，请稍后再试。");
        return;
      }

      authFailed = false;
      feedback.hidden = true;
      status("登录成功，正在进入应用…", "success");
      location.href = "/";
    } catch (error) {
      setError("网络连接异常，请检查网络后重试。");
      status("网络连接异常，请检查网络后重试。", "error");
    } finally {
      submitting = false;
      setBusy(form, false);
    }
  }

  async function sendCode(kind) {
    const form = document.getElementById(`${kind}-form`);
    const studentId = document.getElementById(`${kind}-student-id`).value.trim().toUpperCase();
    const password = document.getElementById(`${kind}-password`)?.value || "";
    const confirm = document.getElementById(`${kind}-confirm`)?.value || "";
    const button = document.getElementById(`${kind}-send`);
    if (!studentId) return status("请输入学号。", "error");
    if (kind === "register") {
      if (!password || password.length < 12) return status("密码至少 12 位。", "error");
      if (password !== confirm) return status("两次输入的密码不一致。", "error");
    }

    button.disabled = true;
    status("正在发送验证码…");
    try {
      if (kind === "register") {
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
      let seconds = 60;
      const original = button.textContent;
      countdowns.set(kind, setInterval(() => {
        button.textContent = `${seconds}s`;
        seconds -= 1;
        if (seconds < 0) {
          clearInterval(countdowns.get(kind));
          button.textContent = original;
          button.disabled = false;
        }
      }, 1000));
    } catch (error) {
      button.disabled = false;
      status(error.message || "验证码发送未完成。", "error");
    }
  }

  async function register(event) {
    event.preventDefault();
    if (submitting) return;
    const form = event.currentTarget;
    const studentId = document.getElementById("register-student-id").value.trim().toUpperCase();
    const password = document.getElementById("register-password").value;
    const confirm = document.getElementById("register-confirm").value;
    if (password !== confirm) return status("两次输入的密码不一致。", "error");

    submitting = true;
    setBusy(form, true);
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
      status("账号已创建，正在进入登录页…", "success");
      setTimeout(() => location.href = "/login?registered=1", 650);
    } catch (error) {
      status(error.message || "注册未完成。", "error");
    } finally {
      submitting = false;
      setBusy(form, false);
    }
  }

  async function reset(event) {
    event.preventDefault();
    if (submitting) return;
    const form = event.currentTarget;
    const password = document.getElementById("reset-password").value;
    const confirm = document.getElementById("reset-confirm").value;
    if (password !== confirm) return status("两次输入的新密码不一致。", "error");

    submitting = true;
    setBusy(form, true);
    status("正在重置密码…");
    try {
      await requestJSON("/auth/hust/password-reset/confirm", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          student_id: document.getElementById("reset-student-id").value.trim().toUpperCase(),
          code: document.getElementById("reset-code").value.trim(),
          new_password: password,
        }),
      });
      status("密码已重置，正在进入登录页…", "success");
      setTimeout(() => location.href = "/login?reset=1", 650);
    } catch (error) {
      status(error.message || "密码重置未完成。", "error");
    } finally {
      submitting = false;
      setBusy(form, false);
    }
  }

  setViewTitle();
  activeForm().dataset.active = "true";
  activatePasswordToggles();

  const form = activeForm();
  if (form) {
    form.addEventListener("submit", view === "login" ? login : view === "register" ? register : reset);
    if (view === "login") {
      ["login-student-id", "login-password"].forEach((id) => {
        document.getElementById(id).addEventListener("input", () => {
          if (authFailed) document.getElementById("login-error").textContent = "";
        });
      });
      if (new URLSearchParams(location.search).get("registered") === "1") {
        status("账号已创建，请登录。", "success");
      }
      if (new URLSearchParams(location.search).get("reset") === "1") {
        status("密码已重置，请使用新密码登录。", "success");
      }
    }
  }

  document.querySelectorAll("[data-send]").forEach((button) => {
    button.addEventListener("click", () => sendCode(button.dataset.send));
  });
})();
