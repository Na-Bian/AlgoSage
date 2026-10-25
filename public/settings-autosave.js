/* AlgoSage 设置面板即时保存：
   隐藏 Chainlit 的“确认/取消”footer，控件变更后自动走原生确认提交。
   不复制 Chainlit 的表单逻辑——通过点击真实存在的 #confirm-sidebar 触发。
   Radix 滑块不派发 DOM change 事件，改观察 thumb 的 aria-valuenow。 */
(function settingsAutosave() {
  "use strict";

  var CLASS = "eg-autosave";
  var submitTimer = null;
  var suppressAutoSubmit = false;
  var valueObserver = null;

  function surface() {
    var title = document.getElementById("chat-settings-sidebar-title");
    if (!title) return null;
    return title.closest('[role="dialog"]') || title.closest("[data-panel]");
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
    // Radix Slider：值变化只反映在 thumb 的 aria-valuenow 上。
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
          // Radix Select 的选中值渲染在 combobox 文本里，不派发 change。
          if (record.target.parentElement && record.target.parentElement.closest('[role="combobox"]')) {
            scheduleSubmit();
            return;
          }
        } else if (record.type === "childList") {
          // 有些版本重建 combobox 内容节点而非改文本。
          var added = record.addedNodes;
          for (var j = 0; j < added.length; j++) {
            if (added[j].parentElement && added[j].parentElement.closest('[role="combobox"]')) {
              scheduleSubmit();
              return;
            }
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
})();
