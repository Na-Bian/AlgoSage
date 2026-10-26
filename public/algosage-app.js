/* Chainlit entrypoint: load theme transition first, then existing UI enhancements. */

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
})();
(function () {
  "use strict";

  function loadScript(src) {
    return new Promise((resolve, reject) => {
      const script = document.createElement("script");
      script.src = src;
      script.onload = resolve;
      script.onerror = () => reject(new Error(`Unable to load ${src}`));
      document.head.appendChild(script);
    });
  }

  loadScript("/public/theme-transition.js?v=20261025-options-1")
    .then(() => loadScript("/public/account-profile.js?v=20261025-focus-return-3"))
    .then(() => loadScript("/public/user-preferences.js?v=20261024-model-picker-2"))
    .then(() => loadScript("/public/model-picker.js?v=20261026-ws-refresh-1"))
    .then(() => loadScript("/public/chat-ui.js?v=20261026-doc-hide-1"))
    .then(() => loadScript("/public/settings-autosave.js?v=20261026-settle-timeout-1"))
    .then(() => loadScript("/public/chat-options.js?v=20261026-pills-inline-1"))
    .catch(() => {
      // Existing behavior remains usable even if optional enhancements fail.
      loadScript("/public/account-profile.js?v=20261025-focus-return-3");
    });
})();
