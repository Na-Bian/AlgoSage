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
    if (comboTexts.get(combo) === text) return false;
    comboTexts.set(combo, text);
    return true;
  }

  function scheduleSubmit() {
    window.clearTimeout(submitTimer);
    submitTimer = window.setTimeout(function () {
      var confirm = document.getElementById("confirm-sidebar");
      var panel = surface();
      if (!confirm || !panel) return;
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
        if (suppressAutoSubmit) return;
        if (event.target.closest(".eg-settings-add, .eg-add-dialog")) return;
        scheduleSubmit();
      },
      true
    );
    panel.addEventListener(
      "click",
      function (event) {
        if (suppressAutoSubmit) return;
        var toggle = event.target.closest('[role="switch"]');
        if (!toggle || toggle.closest(".eg-settings-add, .eg-add-dialog")) return;
        scheduleSubmit();
      },
      true
    );
    snapshotCombos(panel);
    valueObserver = new MutationObserver(function (records) {
      if (suppressAutoSubmit) return;
      for (var i = 0; i < records.length; i++) {
        var record = records[i];
        if (record.attributeName === "aria-valuenow") {
          if (record.oldValue !== record.target.getAttribute("aria-valuenow")) {
            scheduleSubmit();
            return;
          }
        } else if (record.type === "characterData") {
          if (comboTextChanged(record.target.parentElement)) {
            scheduleSubmit();
            return;
          }
        } else if (record.type === "childList") {
          if (comboTextChanged(record.target)) {
            scheduleSubmit();
            return;
          }
        }
      }
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

  // 提供商管理对话框增删 provider 后，借一次原生提交让服务端重发面板设置。
  window.AlgoSageSettingsAutoSave = { refresh: scheduleSubmit };
})();
