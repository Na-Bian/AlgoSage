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
      // 程序化提交 → 抽屉收起 → 自动重开 → React 补渲染。整条链路的时长
      // 随负载波动，固定冷却窗（1.6s）在高负载下会提前失效，让渲染回放
      // （空值→初值）被当成用户编辑，引发面板收起/展开循环。改用确定性
      // 门闩：提交即进入 pending 状态，抽屉重新出现并稳定 3 个宏任务后
      // 才解除，期间所有观察到的"变化"一律只刷新基线。
      pendingSettle = true;
      confirm.click();
      // 抽屉卸载是异步的：轮询至其消失后再触发顶栏按钮重新打开。
      var tries = 0;
      window.clearInterval(reopenTimer);
      reopenTimer = window.setInterval(function () {
        if (surface()) return;
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

  // 抽屉重开后的渲染稳定检测：连续 3 个宏任务无 DOM 变更即视为稳定。
  function watchSettle(panel) {
    var quiet = 0;
    var check = function () {
      if (!surface() || surface() !== panel) { settleDone(); return; }
      if (++quiet >= 3) { settleDone(); return; }
      window.setTimeout(check, 120);
    };
    window.setTimeout(check, 120);
  }

  function settleDone() {
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
    if (pendingSettle) watchSettle(panel);
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
    if (panel.dataset.egAutosave !== "armed") armAutosave(panel);
    var confirm = document.getElementById("confirm-sidebar");
    var footer = confirm && confirm.parentElement;
    if (footer && !footer.classList.contains(CLASS)) {
      footer.classList.add(CLASS);
    }
  }

  new MutationObserver(scan).observe(document.body, { childList: true, subtree: true });
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", scan);
  } else {
    scan();
  }

  // 窄视口下设置抽屉是带全屏遮罩的 dialog：点遮罩（视觉上的"页面空白处"）
  // 不再关闭面板——即时保存下没有"取消丢弃"语义，快速点击周边极易误触。
  // 挂在 window 捕获阶段，先于 Radix DismissableLayer 的 document 监听。
  function swallowOverlayClose(event) {
    var panel = surface();
    if (!panel || !panel.matches('[role="dialog"]')) return;
    if (event.target !== panel.previousElementSibling) return;
    event.preventDefault();
    event.stopImmediatePropagation();
  }
  window.addEventListener("pointerdown", swallowOverlayClose, true);
  window.addEventListener("mousedown", swallowOverlayClose, true);
  window.addEventListener("click", swallowOverlayClose, true);

  // 提供商管理对话框增删 provider 后，借一次原生提交让服务端重发面板设置。
  window.AlgoSageSettingsAutoSave = { refresh: scheduleSubmit };
})();
