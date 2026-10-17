/* ExamGuard 消息复制按钮（对照 DeepSeek 的消息级复制体验）。
   通过 Chainlit custom_js 注入；只复制页面上已渲染的最终文本，
   不访问任何密钥、系统提示词或未送达内容。
   DOM 结构依据 Chainlit 2.11 实测：每条消息顶层为 div[id^='step-']，
   助手正文在 .message-content，用户气泡为 .bg-accent.rounded-3xl。
   防御式查询：前端升级时按钮可能失效，但不影响聊天与安全功能。 */
(function () {
  "use strict";

  function isMessageBlock(el) {
    return (
      el.tagName === "DIV" &&
      /^step-/.test(el.id || "") &&
      !!(el.querySelector(".message-content") || el.querySelector(".bg-accent.rounded-3xl"))
    );
  }

  function messageText(block) {
    var content =
      block.querySelector(".message-content") ||
      block.querySelector(".bg-accent.rounded-3xl");
    return ((content && content.innerText) || "").trim();
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
    if (block.dataset.egCopy === "1") return;
    block.dataset.egCopy = "1";
    block.appendChild(makeButton(block));
  }

  var pending = null;
  function scheduleScan() {
    if (pending) return;
    pending = setTimeout(function () {
      pending = null;
      scanAll();
    }, 150);
  }

  function scanAll() {
    var blocks = document.querySelectorAll("main div[id^='step-']");
    for (var i = 0; i < blocks.length; i++) {
      if (isMessageBlock(blocks[i])) decorate(blocks[i]);
    }
  }

  function start() {
    var observer = new MutationObserver(scheduleScan);
    observer.observe(document.body, { childList: true, subtree: true });
    scanAll();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }
})();
