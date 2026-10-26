/* AlgoSage 设置面板即时保存：
   隐藏 Chainlit 的“确认/取消”footer，控件变更后自动走原生确认提交。
   不复制 Chainlit 的表单逻辑——通过点击真实存在的 #confirm-sidebar 触发。
   Radix 滑块不派发 DOM change 事件，观察 thumb 的 aria-valuenow；
   Radix Select 打开下拉也会重建触发器节点，因此对下拉框比对文本内容，
   只有选中值真正变化（文本改变）才提交。 */
(function settingsAutosave() {
  "use strict";

  var CLASS = "eg-autosave";
  var submitTimer = null;
  var valueObserver = null;
  var comboTexts = new WeakMap();

  function surface() {
    var title = document.getElementById("chat-settings-sidebar-title");
    if (!title) return null;
    return title.closest('[role="dialog"]') || title.closest("[data-panel]");
  }

  function snapshotCombos(panel) {
    panel.querySelectorAll('[role="combobox"]').forEach(function (combo) {
      comboTexts.set(combo, combo.textContent);
    });
  }

  function comboTextChanged(node) {
    var combo = node && node.closest ? node.closest('[role="combobox"]') : null;
    if (!combo) return false;
    var text = combo.textContent;
    if (!comboTexts.has(combo)) {
      comboTexts.set(combo, text);
      return false;
    }
    var previous = comboTexts.get(combo);
    comboTexts.set(combo, text);
    // 空基线 → 有值是抽屉重开后的补渲染，不是用户选择。
    if (!previous.trim()) return false;
    return previous !== text;
  }

  function scheduleSubmit() {
    window.clearTimeout(submitTimer);
    submitTimer = window.setTimeout(function () {
      var confirm = document.getElementById("confirm-sidebar");
      var panel = surface();
      if (!confirm || !panel) return;
      // 首选直发路径：借前端自身维护的设置镜像（chat_settings_edit 全量
      // 事件）算出增量，直接发 chat_settings_change——服务端合并并原位
      // 重发面板，抽屉全程不关，无收起/重开闪烁。
      var socket = window.AlgoSageSocket;
      if (socket) {
        var mirror = socket.settingsMirror();
        if (mirror && Object.keys(mirror).length) {
          var delta = {};
          if (!lastSent) {
            // 基线未建：首发改全量（幂等合并）。model_catalog 由打开面板
            // 时的选择承担，不掺进采样偏好提交，避免误触发默认模型分支。
            for (var k in mirror) if (k !== "model_catalog") delta[k] = mirror[k];
          } else {
            for (var key in mirror) {
              if (mirror[key] !== lastSent[key]) delta[key] = mirror[key];
            }
          }
          if (Object.keys(delta).length) {
            if (socket.emitSettingsChange(delta)) {
              lastSent = Object.assign(lastSent || {}, mirror);
              return;
            }
          } else {
            return; // 无真实增量（渲染回放误触发）：不提交也不闪面板
          }
        }
      }
      // 回退路径（无 WebSocket/镜像未就绪）：程序化提交 → 抽屉收起 →
      // 自动重开 → React 补渲染。整条链路的时长随负载波动，固定冷却窗
      // 会提前失效，让渲染回放（空值→初值）被当成用户编辑，引发面板收
      // 起/展开循环。改用确定性门闩：提交即进入 pending 状态，抽屉重新
      // 出现并稳定后（watchSettle）才解除。
      pendingSettle = true;
      confirm.click();
      if (socket) lastSent = socket.settingsMirror();
      // 抽屉卸载是异步的：轮询至其消失后再触发顶栏按钮重新打开。
      // 用户在周期内点了齿轮（明确要关闭）时轮询必须让位，否则会与
      // 用户形成"你关我开"的拉锯——这正是连点齿轮时鬼畜的来源。
      var tries = 0;
      window.clearInterval(reopenTimer);
      reopenTimer = window.setInterval(function () {
        if (surface()) return;
        if (Date.now() - lastGearClickAt < 1200) {
          // 用户刚刚主动关闭：放弃重开并解除门闩，尊重用户意图。
          window.clearInterval(reopenTimer);
          pendingSettle = false;
          var current = surface();
          if (current) snapshotCombos(current);
          return;
        }
        var trigger = document.getElementById("chat-settings-header-button");
        if (trigger) {
          window.clearInterval(reopenTimer);
          trigger.click();
        } else if (++tries > 20) {
          window.clearInterval(reopenTimer);
          pendingSettle = false; // 兜底：重开失败也不能永久卡死自动保存
        }
      }, 200);
    }, 400);
  }

  var reopenTimer = null;
  var pendingSettle = false;
  var lastGearClickAt = 0;
  // 直发路径的服务端已确认值（镜像与之 diff 出增量）；null=尚未建立基线。
  var lastSent = null;

  // 抽屉重开后的渲染稳定检测：面板稳定后解除门闩并重拍基线。
  // 同一面板只保留一条检测链，避免多链互相重置门闩。
  // 等待期改为固定静默窗：直发路径下面板不经历关闭重开，旧的
  // "轮询至面板消失"会永久等不到解除、拦死后续所有 observer 触发。
  var settlePanel = null;
  function watchSettle(panel) {
    if (settlePanel === panel) return;
    settlePanel = panel;
    window.setTimeout(function () {
      if (surface() !== panel) return; // 面板已换：由新面板的检测链接管
      settleDone();
    }, 700);
  }

  function settleDone() {
    settlePanel = null;
    if (!pendingSettle) return;
    pendingSettle = false;
    var panel = surface();
    if (panel) snapshotCombos(panel);
  }

  function armAutosave(panel) {
    panel.dataset.egAutosave = "armed";
    // 用户手动触发原生提交（键盘 Enter 等）时同样走门闩，避免双发。
    panel.addEventListener("submit", function () {
      window.clearTimeout(submitTimer);
      pendingSettle = true;
      watchSettle(panel);
    });
    // Select / 原生控件走 change；Radix Switch 走 click。
    panel.addEventListener(
      "change",
      function (event) {
        if (event.target.closest(".eg-settings-add, .eg-add-dialog")) return;
        scheduleSubmit();
      },
      true
    );
    panel.addEventListener(
      "click",
      function (event) {
        var toggle = event.target.closest('[role="switch"]');
        if (!toggle || toggle.closest(".eg-settings-add, .eg-add-dialog")) return;
        scheduleSubmit();
      },
      true
    );
    snapshotCombos(panel);
    // 面板挂载后 React 仍会补渲染一至两帧；挂载后 150ms 内观察到的差异
    // 一律视为渲染回放，只刷新基线（等待期用同一个门闩标志）。
    pendingSettle = true;
    watchSettle(panel);
    valueObserver = new MutationObserver(function (records) {
      var pending = false;
      for (var i = 0; i < records.length; i++) {
        var record = records[i];
        if (record.attributeName === "aria-valuenow") {
          // oldValue 缺失（null）说明是首次渲染赋值，不是用户拖动。
          if (
            typeof record.oldValue === "string"
            && record.oldValue !== record.target.getAttribute("aria-valuenow")
          ) {
            pending = true;
          }
        } else if (record.type === "characterData") {
          if (comboTextChanged(record.target.parentElement)) pending = true;
        } else if (record.type === "childList") {
          if (comboTextChanged(record.target)) pending = true;
        }
      }
      // 门闩期间（提交后的收起/重开/补渲染）只刷新基线，不触发提交；
      // 基线在 comboTextChanged 与下方无条件刷新。
      if (pending && !pendingSettle) scheduleSubmit();
    });
    valueObserver.observe(panel, {
      subtree: true,
      childList: true,
      attributes: true,
      attributeFilter: ["aria-valuenow"],
      attributeOldValue: true,
      characterData: true,
    });
  }

  function scan() {
    var panel = surface();
    if (!panel) return;
    if (panel.dataset.egAutosave !== "armed") {
      // 延迟一帧再挂监听和快照：面板刚挂载时 React 还没填值，立刻快照会
      // 把之后的补渲染（空→初值）误判为用户编辑（如点过下拉后重开面板）。
      panel.dataset.egAutosave = "arming";
      requestAnimationFrame(function () {
        if (surface() === panel) armAutosave(panel);
      });
    }
    var confirm = document.getElementById("confirm-sidebar");
    var footer = confirm && confirm.parentElement;
    if (footer && !footer.classList.contains(CLASS)) {
      footer.classList.add(CLASS);
    }
  }

  // 顶栏齿轮是面板开关的总闸：记录点击时间供重开轮询让位（用户主动关闭
  // 时轮询不得再拉起面板），并清掉遗留的提交防抖。
  document.addEventListener(
    "click",
    function (event) {
      var button = event.target.closest && event.target.closest("#chat-settings-header-button");
      if (!button) return;
      lastGearClickAt = Date.now();
      window.clearTimeout(submitTimer);
    },
    true
  );

  new MutationObserver(scan).observe(document.body, { childList: true, subtree: true });
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", scan);
  } else {
    scan();
  }

  // 窄视口下设置抽屉是带全屏遮罩的 dialog：点遮罩（视觉上的"页面空白处"）
  // 不再关闭面板——即时保存下没有"取消丢弃"语义，快速点击周边极易误触。
  // 同理适用于所有 Radix 遮罩弹窗（重命名对话等）：遮罩点击一律吞掉，
  // 关闭只走明确的关闭/取消按钮或 Esc。挂在 window 捕获阶段，先于
  // Radix DismissableLayer 的 document 监听。
  function swallowOverlayClose(event) {
    var target = event.target;
    if (!target || target.nodeType !== 1) return;
    var overlay =
      target.matches?.('[data-state].fixed.inset-0[class*="bg-black"]') && target;
    if (!overlay) return;
    // 只吞"已打开"的遮罩；关闭动画中的遮罩点击放行。
    if (overlay.getAttribute('data-state') !== 'open') return;
    event.preventDefault();
    event.stopImmediatePropagation();
  }
  window.addEventListener("pointerdown", swallowOverlayClose, true);
  window.addEventListener("mousedown", swallowOverlayClose, true);
  window.addEventListener("click", swallowOverlayClose, true);

  // 管理模型提供商对话框的"确认"：面板里其它控件变更都由即时保存提交，
  // 但提供商增删发生在 HTTP 路由里（服务端无法从那里推送 ChatSettings），
  // 需要借一次面板提交让服务端重发"默认模型"列表。
  window.AlgoSageSettingsAutoSave = { refresh: scheduleSubmit };

})();
