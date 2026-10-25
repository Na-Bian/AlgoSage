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
  var suppressAutoSubmit = false;
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
      // 程序化提交后进入冷却：重开 + React 补渲染期间产生的"变化"只是
      // 渲染回放（空值→初值），不能再次触发提交，否则面板会收起/展开循环。
      suppressAutoSubmit = true;
      window.clearTimeout(suppressTimer);
      suppressTimer = window.setTimeout(function () {
        suppressAutoSubmit = false;
        // 冷却期内的真实用户操作不被吞掉：冷却结束后补交一次。
        if (pendingUserAction) {
          pendingUserAction = false;
          scheduleSubmit();
        }
      }, 1600);
      confirm.click();
      // Chainlit 提交后会收起并卸载设置抽屉；即时保存场景需要重新打开。
      // 抽屉卸载是异步的：轮询至其消失后再触发顶栏按钮，避免被面板状态吞掉。
      var tries = 0;
      window.clearInterval(reopenTimer);
      reopenTimer = window.setInterval(function () {
        if (surface()) return; // 仍打开（未来版本行为变化时）则无需处理
        var trigger = document.getElementById("chat-settings-header-button");
        if (trigger) {
          window.clearInterval(reopenTimer);
          trigger.click();
        } else if (++tries > 20) {
          window.clearInterval(reopenTimer);
        }
      }, 200);
    }, 400);
  }

  var reopenTimer = null;
  var suppressTimer = null;
  var pendingUserAction = false;

  function armAutosave(panel) {
    panel.dataset.egAutosave = "armed";
    // 用户手动触发原生提交（键盘 Enter 等）时跳过本轮自动提交，避免双发。
    panel.addEventListener("submit", function () {
      window.clearTimeout(submitTimer);
      suppressAutoSubmit = true;
      window.setTimeout(function () { suppressAutoSubmit = false; }, 800);
    });
    // Select / 原生控件走 change；Radix Switch 走 click。
    panel.addEventListener(
      "change",
      function (event) {
        if (event.target.closest(".eg-settings-add, .eg-add-dialog")) return;
        if (suppressAutoSubmit) { pendingUserAction = true; return; }
        scheduleSubmit();
      },
      true
    );
    panel.addEventListener(
      "click",
      function (event) {
        var toggle = event.target.closest('[role="switch"]');
        if (!toggle || toggle.closest(".eg-settings-add, .eg-add-dialog")) return;
        if (suppressAutoSubmit) { pendingUserAction = true; return; }
        scheduleSubmit();
      },
      true
    );
    snapshotCombos(panel);
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
      // 基线已在上面无条件刷新；冷却期（提交后的渲染回放）内不触发新提交。
      if (pending && !suppressAutoSubmit) scheduleSubmit();
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
