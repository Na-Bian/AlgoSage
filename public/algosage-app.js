/* Chainlit entrypoint: load theme transition first, then existing UI enhancements. */

/* 模型名占位变量必须赶在 React 首帧之前：composer 重挂载窗口里注入按钮
   缺位时，custom.css 的占位伪元素用同一文案顶住，避免模型名闪变。 */
try {
  document.documentElement.style.setProperty(
    "--eg-model-label",
    `"${window.localStorage.getItem("algosage-model-label") || "选择模型"}"`
  );
} catch (_) {}

/* Socket.io 的 WebSocket 捕获：供设置面板即时保存直发 chat_settings_change
   事件（面板不关、无收起/重开闪烁）。本文件最先执行，必须赶在应用建连之前。
   前端在每次控件变更时都会全量 emit chat_settings_edit，借它维护设置镜像。 */
(function () {
  "use strict";

  const NativeWS = window.WebSocket;
  const sockets = new Set();
  function liveSocket() {
    for (const ws of sockets) if (ws.readyState === 1) return ws;
    return null;
  }
  function WrappedWebSocket(url, protocols) {
    const ws =
      protocols !== undefined ? new NativeWS(url, protocols) : new NativeWS(url);
    if (String(url).indexOf("/ws/socket.io") !== -1) sockets.add(ws);
    return ws;
  }
  WrappedWebSocket.prototype = NativeWS.prototype;
  window.WebSocket = WrappedWebSocket;

  const mirror = {};
  const nativeSend = NativeWS.prototype.send;
  NativeWS.prototype.send = function (data) {
    try {
      if (typeof data === "string" && data.indexOf("42") === 0) {
        const parsed = JSON.parse(data.slice(2));
        if (parsed && parsed[0] === "chat_settings_edit" && parsed[1]) {
          Object.assign(mirror, parsed[1]);
        }
      }
    } catch (_) {}
    return nativeSend.call(this, data);
  };

  window.AlgoSageSocket = {
    // 直发设置增量（服务端合并并重发面板，抽屉全程保持打开）。
    // 失败返回 false，调用方回退原生确认路径。
    emitSettingsChange(delta) {
      const ws = liveSocket();
      if (!ws) return false;
      try {
        ws.send("42" + JSON.stringify(["chat_settings_change", delta]));
        return true;
      } catch (_) {
        return false;
      }
    },
    // 直发任意 socket 事件（如提供商增删后请求服务端重发面板设置）。
    emitEvent(name, payload) {
      const ws = liveSocket();
      if (!ws) return false;
      try {
        ws.send("42" + JSON.stringify([name, payload || {}]));
        return true;
      } catch (_) {
        return false;
      }
    },
    settingsMirror() {
      return Object.assign({}, mirror);
    },
  };

  /* 删除当前会话后回到新建对话页：Chainlit 的删除成功回调里虽然有
     路由跳转，但实测不生效（URL 不变、消息留在页面上）。切换必须发生在
     删除请求"发出"的瞬间——等响应回来再切，转圈期间旧会话会一直挂着。
     在 fetch 入口处拦截 DELETE /project/thread：请求一发起（不必等响应）
     就淡出主区域并触发原生"新建对话"，删除在后台完成；响应成功后再补
     一次触发兜底（覆盖请求瞬间按钮尚未挂载的极端情况）。任何删除入口
     （侧栏菜单、搜索对话框等）都经过这条请求，统一生效。 */
  const NativeFetch = window.fetch;
  let egDeleteFadeTimer = 0;
  let egDeleteNavigationTimer = 0;
  let egDeleteThreadId = "";

  const currentThreadIdFromUrl = () => {
    const match = location.pathname.match(/^\/thread\/([^/]+)/);
    if (!match) return "";
    try {
      return decodeURIComponent(match[1]);
    } catch (_) {
      return match[1];
    }
  };

  function parseDeleteBody(body) {
    if (!body || typeof body !== "string") return "";
    try {
      return JSON.parse(body).threadId || "";
    } catch (_) {
      return "";
    }
  }

  async function readDeleteRequest(input, init) {
    const method = String(init?.method || input?.method || "GET").toUpperCase();
    const rawUrl = typeof input === "string" ? input : input?.url || "";
    const url = (() => {
      try {
        return new URL(rawUrl, location.href).pathname;
      } catch (_) {
        return rawUrl;
      }
    })();
    let body = init?.body;
    if (body == null && typeof Request !== "undefined" && input instanceof Request) {
      try {
        body = await input.clone().text();
      } catch (_) {}
    }
    return { method, url, threadId: parseDeleteBody(body) };
  }

  function isVisibleControl(control) {
    return Boolean(control && (control.offsetWidth || control.offsetHeight));
  }

  function findNewChatControl() {
    const enhanced = document.querySelector(
      '.eg-new-chat-action[aria-label="新建对话"], .eg-sidebar-icon-button[aria-label="新建对话"]'
    );
    if (isVisibleControl(enhanced)) return enhanced;
    const native = document.getElementById("new-chat-button");
    return isVisibleControl(native) ? native : null;
  }

  function stopDeleteNavigation() {
    if (egDeleteNavigationTimer) {
      clearTimeout(egDeleteNavigationTimer);
      egDeleteNavigationTimer = 0;
    }
  }

  function finishDeleteTransition() {
    stopDeleteNavigation();
    if (egDeleteFadeTimer) {
      clearInterval(egDeleteFadeTimer);
      egDeleteFadeTimer = 0;
    }
    document.body.classList.remove("eg-deleting");
    egDeleteThreadId = "";
  }

  function waitForNewChatReady(threadId) {
    if (egDeleteFadeTimer) clearInterval(egDeleteFadeTimer);
    const started = Date.now();
    egDeleteFadeTimer = setInterval(() => {
      const leftThread = currentThreadIdFromUrl() !== threadId;
      const welcomeMounted = Boolean(document.getElementById("welcome-screen"));
      if (
        (leftThread && welcomeMounted) ||
        (leftThread && Date.now() - started > 450) ||
        Date.now() - started > 2200
      ) {
        finishDeleteTransition();
      }
    }, 40);
  }

  function requestNewChat(threadId) {
    if (!threadId || egDeleteThreadId !== threadId) return;
    stopDeleteNavigation();
    const started = Date.now();
    let lastClick = 0;
    const attempt = () => {
      if (currentThreadIdFromUrl() !== threadId) {
        waitForNewChatReady(threadId);
        return;
      }
      const now = Date.now();
      const control = findNewChatControl();
      if (control && now - lastClick >= 120) {
        lastClick = now;
        control.click();
      }
      if (now - started < 2200) {
        egDeleteNavigationTimer = setTimeout(attempt, 60);
      } else {
        egDeleteNavigationTimer = 0;
      }
    };
    attempt();
  }

  function beginDeleteTransition(threadId) {
    if (!threadId || currentThreadIdFromUrl() !== threadId) return;
    egDeleteThreadId = threadId;
    document.body.classList.add("eg-deleting");
    requestNewChat(threadId);
  }

  window.fetch = async function (input, init) {
    const deleteRequest = await readDeleteRequest(input, init);
    const isCurrentDelete =
      deleteRequest.method === "DELETE" &&
      deleteRequest.url.includes("/project/thread") &&
      deleteRequest.threadId &&
      deleteRequest.threadId === currentThreadIdFromUrl();
    if (isCurrentDelete) beginDeleteTransition(deleteRequest.threadId);

    const response = await NativeFetch.call(this, input, init);
    if (isCurrentDelete) {
      if (response.ok) {
        requestNewChat(deleteRequest.threadId);
      } else {
        finishDeleteTransition();
      }
    }
    return response;
  };
  document.addEventListener(
    "click",
    (event) => {
      const options = event.target?.closest?.("#thread-options");
      if (options) {
        const li = options.closest('li[id^="thread-"]');
        egDeleteThreadId = li ? li.id.slice("thread-".length) : "";
        return;
      }
      const dialog = event.target?.closest?.('[role="alertdialog"]');
      if (!dialog || !/确认删除/.test(dialog.textContent || "")) return;
      const isConfirmButton =
        event.target?.closest?.("button")?.textContent.trim() === "确认";
      const tracked = egDeleteThreadId;
      if (!isConfirmButton || !tracked || tracked !== currentThreadIdFromUrl()) return;
      // 确认瞬间先遮住旧内容；fetch 入口会用同一个 thread id 接管并反复
      // 尝试切换，避免确认处理器与侧栏重绘发生竞态。
      beginDeleteTransition(tracked);
    },
    true
  );
})();
(function () {
  "use strict";

  /* 头部设置齿轮占位：Chainlit 要等 chat settings 经 socket 到货才挂载
     设置按钮（刷新后 ~530ms、新建对话后 ~220ms 缺口），期间"说明"和主题
     按钮之间空一格、整行左移。本文件是最早执行的自定义脚本，观察器在
     React 首帧前就位；插入与真按钮同宽的占位元素顶住原位，真按钮挂载
     的同帧（微任务、绘制前）移除，布局零跳动。 */
  function ensureHeaderGear() {
    const readme = document.getElementById("readme-button");
    if (!readme || !readme.isConnected) return;
    const tools = readme.parentElement;
    if (!tools) return;
    const real = document.getElementById("chat-settings-header-button");
    const placeholder = tools.querySelector(":scope > .eg-gear-placeholder");
    if (real && real.isConnected) {
      placeholder?.remove();
      return;
    }
    if (!window.matchMedia("(pointer: fine)").matches || placeholder) return;
    const span = document.createElement("span");
    span.className = "eg-gear-placeholder";
    span.setAttribute("aria-hidden", "true");
    readme.insertAdjacentElement("afterend", span);
  }
  function watchHeaderGear() {
    ensureHeaderGear();
    new MutationObserver(ensureHeaderGear).observe(document.body, {
      childList: true,
      subtree: true,
    });
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", watchHeaderGear, { once: true });
  } else {
    watchHeaderGear();
  }

  function loadScript(src) {
    return new Promise((resolve, reject) => {
      const script = document.createElement("script");
      script.src = src;
      script.onload = resolve;
      script.onerror = () => reject(new Error(`Unable to load ${src}`));
      document.head.appendChild(script);
    });
  }

  loadScript("/public/theme-transition.js?v=20261026-refresh-all-1")
    .then(() => loadScript("/public/account-profile.js?v=20261026-refresh-all-1"))
    .then(() => loadScript("/public/user-preferences.js?v=20261028-draft-clear-2"))
    .then(() => loadScript("/public/model-picker.js?v=20261027-composer-retry-2"))
    .then(() => loadScript("/public/chat-ui.js?v=20261028-resume-snap-1"))
    .then(() => loadScript("/public/settings-autosave.js?v=20261029-model-default-1"))
    .then(() => loadScript("/public/chat-options.js?v=20261026-refresh-all-1"))
    .catch(() => {
      // Existing behavior remains usable even if optional enhancements fail.
      loadScript("/public/account-profile.js?v=20261026-refresh-all-1");
    });
})();
