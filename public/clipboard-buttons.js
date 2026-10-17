/* ExamGuard 消息复制按钮（对照 DeepSeek 的消息级复制体验）。
   通过 Chainlit custom_js 注入；只复制页面上已渲染的最终文本，
   不访问任何密钥、系统提示词或未送达内容。防御式 DOM 查询，
   Chainlit 前端升级时按钮可能失效但不影响聊天与安全功能。 */
(function () {
  "use strict";

  function messageText(block) {
    var bubble = block.querySelector(".bg-accent.rounded-3xl");
    if (!bubble) {
      bubble = block.querySelector("[id^='message-']");
    }
    var root = bubble || block;
    return (root.innerText || "").trim();
  }

  function makeButton(block) {
    var btn = document.createElement("button");
    btn.type = "button";
    btn.className = "eg-copy-btn";
    btn.textContent = "复制";
    btn.addEventListener("click", function (ev) {
      ev.stopPropagation();
      ev.preventDefault();
      var text = messageText(block);
      if (!text) return;
      var done = function (label) {
        btn.textContent = label;
        setTimeout(function () {
          btn.textContent = "复制";
        }, 1500);
      };
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(
          function () {
            done("已复制 ✓");
          },
          function () {
            done("复制失败");
          }
        );
      } else {
        done("浏览器不支持");
      }
    });
    return btn;
  }

  function decorate(block) {
    if (block.dataset.egCopy === "1" || block.querySelector(".eg-copy-btn")) return;
    block.dataset.egCopy = "1";
    block.appendChild(makeButton(block));
  }

  function scanAll() {
    var list = document.querySelectorAll("#messages > div");
    for (var i = 0; i < list.length; i++) decorate(list[i]);
  }

  function waitForMessages(attempt) {
    var target = document.getElementById("messages");
    if (target) {
      var observer = new MutationObserver(scanAll);
      observer.observe(target, { childList: true });
      scanAll();
    } else if (attempt < 40) {
      setTimeout(function () {
        waitForMessages(attempt + 1);
      }, 250);
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () {
      waitForMessages(0);
    });
  } else {
    waitForMessages(0);
  }
})();
