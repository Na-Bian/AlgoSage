/* Lightweight presentation-only enhancement for Chainlit's markdown welcome view.
   It groups the generated config table and usage list into the designed cards;
   no chat, auth, upload, or transport behavior is changed. */
(() => {
  "use strict";

  document.body.classList.add("eg-chat-shell");

  // Chainlit always mounts with defaultOpen=true, so a stored collapsed choice
  // has to be replayed after React mounts. Mark the first restore so the replay
  // changes layout instantly instead of looking like a user-triggered collapse.
  let sidebarRestorePending = storedSidebarOpen() === false;
  if (sidebarRestorePending) {
    document.body.classList.add("eg-sidebar-restoring");
  }

  let scheduled = false;

  let sidebarRefs = null;
  let collapsedBar = null;
  const STEP_LABELS = {
    "generation-safety-review": "生成与安全审核",
    "security-audit-policy": "安全审计与策略",
    "model-reasoning": "思考过程",
  };
  // 动态思考标题（服务端实时推送“思考中 · N 字 / 思考完成”）原样保留。
  const DYNAMIC_REASONING_RE = /^(思考中 · \d+ 字|思考完成)$/;

  // 思考步骤识别：创建时的内部名（model-reasoning）与服务端收口后的动态名
  // （思考中 · N 字 / 思考完成）都是同一条思考行。名字首次更新后就不再是
  // 内部名，只按内部名识别会让放置/展开逻辑对运行中的思考行全部失效。
  function isReasoningName(safeName) {
    return safeName === "model-reasoning" || DYNAMIC_REASONING_RE.test(safeName);
  }

  function stepName(button) {
    try {
      return decodeURIComponent(button.id.slice("step-".length));
    } catch (_) {
      return button.id.slice("step-".length);
    }
  }

  function translateStepLabels() {
    // 有内容的步骤渲染成可展开的 button，生成中尚无内容的步骤渲染成
    // 同 id 的 <p>（“使用中 …”）；两种都要把内部名换成中文。
    document.querySelectorAll('main [id^="step-"]').forEach((button) => {
      const safeName = stepName(button);
      const reasoning = isReasoningName(safeName);
      const label = STEP_LABELS[safeName]
        || (reasoning ? safeName : null);
      if (!label || button.dataset.egStepLabel === label) return;

      button.dataset.egStepLabel = label;
      button.childNodes.forEach((node) => {
        if (node.nodeType === Node.TEXT_NODE && node.textContent.trim()) {
          node.textContent = node.textContent.replace(safeName, label);
        }
      });
      // 置于正式回答上方；展开/收起交给 Chainlit 自己的折叠机制
      //（见 ensureReasoningExpanded），这里只做标记。
      if (reasoning && button.tagName === "BUTTON") {
        button.parentElement?.classList.add("eg-reasoning-step");
      }
    });
  }

  /* 思考过程自动展开/自动折叠：Chainlit 把步骤渲染成折叠态
     （aria-expanded=false，只显示"已使用 思考过程"摘要），且正文晚于
     步骤头挂载——旧的 dataset/display 方案只在首帧生效、之后无人再展开，
     点击时还会和 Chainlit 自己的开关互相打架（弹出即收回）。改为驱动
     Chainlit 自己的折叠机制：检测到折叠就模拟点击由它自己展开；正式
     回答开始出字（= 思考必然已结束）就自动折叠一次；用户手动收起
     （isTrusted 点击）则记录并尊重。展开期间跟随滚动到底部。 */
  /* 思考过程自动展开/折叠（驱动 Chainlit 自身的折叠机制）：
     - 页面首轮扫描时已存在的思考步骤 = 历史回合，保持 Chainlit 默认折叠
       （刷新正在生成的页面属于此列，重载后不自动展开是可接受降级）；
     - 之后新出现的思考步骤节点 = 新回合：自动展开并跟随滚动；正式回答
       元素出现（思考必然结束）或思考停更 3 秒时自动折叠一次；
     - 用户手动收起（isTrusted 点击）永远优先，不再自动展开。 */
  const reasoningUserCollapsed = new WeakSet();
  const reasoningAutoFolded = new WeakSet();
  const reasoningSeenNodes = new WeakSet();
  const reasoningHistoricalNodes = new WeakSet();
  const reasoningMeta = new WeakMap();
  let reasoningScanDone = false;
  const reasoningHistoricalCollapsed = new WeakSet();
  const reasoningUserControlled = new WeakSet();
  let reasoningFollowPaused = false;
  document.addEventListener(
    "click",
    (event) => {
      // 思考行的名字会从内部名变为动态名（思考中 · N 字 / 思考完成），
      // 且刷新后以动态名持久化——按内部名匹配会漏掉全部新历史芯片，
      // 用户的展开会在下一帧被历史分支的自动收起弹回去。
      const button = event.target?.closest?.('button[id^="step-"]');
      if (!button || !event.isTrusted || !isReasoningName(stepName(button))) return;
      // 用户真实点击过 = 该步骤由用户掌管：自动展开/收起一律退场。
      // （否则“每帧一次的强制收起”会覆盖用户刚展开的历史思考。）
      reasoningUserControlled.add(button);
      reasoningAutoFolded.delete(button);
      reasoningHistoricalCollapsed.add(button);
      // 展开回看时暂停自动跟随，防止思考期被拉回底部。
      if (button.getAttribute("aria-expanded") === "false") {
        reasoningFollowPaused = true;
      }
    },
    true
  );
  // 自动跟随只在用户位于思考正文底部附近时进行：监听滚动位置（覆盖滚轮、
  // 滚动条拖动、触摸板、键盘等一切滚动方式），离开底部即暂停跟随，滚回
  // 底部（32px 容差）自动恢复。此前只看滚轮方向，拖动滚动条回看时仍会被
  // 强制拉回底部。
  const REASONING_FOLLOW_RESUME_PX = 32;
  document.addEventListener(
    "scroll",
    (event) => {
      const body = event.target;
      if (!(body instanceof Element) || !body.classList.contains("eg-reasoning-body")) return;
      const fromBottom = body.scrollHeight - body.scrollTop - body.clientHeight;
      reasoningFollowPaused = fromBottom > REASONING_FOLLOW_RESUME_PX;
    },
    { capture: true, passive: true }
  );
  // 本页会话里发起过提问、且该回合尚未收口 = 正在生成。历史会话（打开
  // 历史对话、刷新后恢复）在本页从未发起提问，其中的思考步骤一律历史折叠。
  // 用「是否发送过」而不是「停止按钮是否在」，避免按钮晚于步骤渲染的竞态
  // 把实时回合误判成历史。
  let liveTurnPending = false;
  let liveTurnTimer = 0;
  function armLiveTurn() {
    liveTurnPending = true;
    clearTimeout(liveTurnTimer);
    liveTurnTimer = setTimeout(() => { liveTurnPending = false; }, 15 * 60 * 1000);
  }
  document.addEventListener(
    "click",
    (event) => {
      // 编辑历史消息后的重发走消息气泡内的确认键（Chainlit 的
      // button.confirm-edit），不在主输入区——同样属于本页发起的实时回合，
      // 不置位的话重生成的思考芯片会被误判成历史渲染而保持折叠。
      if (event.target?.closest?.("button.confirm-edit")) {
        armLiveTurn();
        return;
      }
      const composer = document.getElementById("message-composer");
      if (!composer) return;
      const button = event.target?.closest?.("button");
      if (!button) return;
      const rect = button.getBoundingClientRect();
      // 输入区右下角的圆形发送键。
      if (Math.round(rect.width) !== 40) return;
      const box = composer.getBoundingClientRect();
      if (rect.x < box.x + box.width - 220) return;
      armLiveTurn();
    },
    true
  );
  document.addEventListener(
    "keydown",
    (event) => {
      if (event.key !== "Enter" || event.shiftKey) return;
      if (event.target?.id === "edit-chat-input") {
        // 编辑框内回车：Chainlit 提交编辑重发（若上游不支持，仅多一个
        // 无害的 live 标志）。
        armLiveTurn();
        return;
      }
      if (!event.target?.closest?.("#message-composer")) return;
      armLiveTurn();
    },
    true
  );

  function turnInFlight() {
    return liveTurnPending;
  }

  function reasoningButtons() {
    return [...document.querySelectorAll('button[id^="step-"]')].filter(
      (button) => isReasoningName(stepName(button))
    );
  }

  function ensureReasoningExpanded() {
    const buttons = reasoningButtons();
    if (!reasoningScanDone) {
      // 页面加载后的首轮扫描：现存按钮都视为历史渲染
      buttons.forEach((b) => {
        reasoningSeenNodes.add(b);
        reasoningHistoricalNodes.add(b);
      });
      reasoningScanDone = true;
    }
    // 多轮历史里每条思考行都要有限高正文样式（与展开状态无关）。正在
    // 流式输出的正文在其滚动容器末尾放一个自有哨兵元素：粘性定位的渐变
    // 遮罩让最新浮现的内容呈淡入态、随增长固化——纯 CSS 视觉，不触碰
    // React 管理的子节点（往 react-markdown 的子列表里插元素会破坏协调，
    // 曾导致消息树崩溃白屏）。思考结束后遮罩移除。
    reasoningButtons().forEach((b) => {
      const body = b.parentElement?.nextElementSibling;
      if (!body) return;
      if (!body.classList.contains("eg-reasoning-body")) {
        body.classList.add("eg-reasoning-body");
      }
      const live = turnInFlight() && (decodeURIComponent(b.id.slice(5)) || "").startsWith("思考中");
      const sentinel = body.querySelector(".eg-fade-sentinel");
      if (live && !sentinel) {
        const el = document.createElement("div");
        el.className = "eg-fade-sentinel";
        body.append(el);
      } else if (!live && sentinel) {
        sentinel.remove();
      }
    });
    const button = buttons[buttons.length - 1];
    if (!button) return;
    const container = button.parentElement;
    const body = container?.nextElementSibling;
    // 思考正文的限高/独立滚动样式挂在 .eg-reasoning-body 上；正文晚于
    // 步骤头挂载，所以每个 tick 幂等补挂（重写节点跟踪时曾把这个赋值弄丢）。
    if (body && !body.classList.contains("eg-reasoning-body")) {
      body.classList.add("eg-reasoning-body");
    }
    if (!reasoningSeenNodes.has(button)) {
      reasoningSeenNodes.add(button);
      const firstName = stepName(button);
      if (turnInFlight() && firstName !== "思考完成") {
        // 本回合正在生成：新出现的思考步骤自动展开并跟随。
        reasoningFollowPaused = false;
      } else if (turnInFlight()) {
        // 「思考完成」是思考结束后收口改名生成的新节点：思考已结束，
        // 不再自动展开；若 Chainlit 以展开态渲染则折叠一次。
        reasoningAutoFolded.add(button);
      } else {
        // 打开历史会话（含会话恢复）时挂载的思考步骤：保持折叠。
        reasoningHistoricalNodes.add(button);
        reasoningScanDone = true;
      }
    }
    if (reasoningUserControlled.has(button)) return;
    if (reasoningHistoricalNodes.has(button)) {
      // 历史步骤：初始挂载时若为展开态（恢复的旧状态）收起一次；之后完全
      // 交还给 Chainlit 与用户——用户点击展开的历史思考必须保持展开。
      if (
        button.getAttribute("aria-expanded") === "true" &&
        !reasoningHistoricalCollapsed.has(button)
      ) {
        reasoningHistoricalCollapsed.add(button);
        button.click();
      }
      return;
    }
    if (reasoningUserCollapsed.has(button)) return;
    const len = body ? (body.textContent || "").length : 0;
    // 超长思考冻结后正文不再增长，但标题字数持续更新——用标题计数作为
    // 主增长信号，否则流式中的思考会被"停滞"误判自动收起。
    const titleCount = (() => {
      const m = button.innerText.match(/思考中 · (\d+) 字/);
      return m ? parseInt(m[1]) : null;
    })();
    const now = performance.now();
    let meta = reasoningMeta.get(button);
    if (!meta) {
      meta = { lastGrowth: titleCount ?? len, lastLen: len, stableAt: now, answersAtStart: document.querySelectorAll('[data-step-type="assistant_message"]').length };
      reasoningMeta.set(button, meta);
    }
    // 标题字数骤降 = 审核否决触发重生成：思考重新开始，恢复展开与跟随
    // （首次回答出字时的自动折叠不该延续到重生成后的新一轮思考）。
    if (
      titleCount !== null
      && meta.lastGrowth !== null
      && meta.lastGrowth - titleCount > 100
    ) {
      reasoningAutoFolded.delete(button);
      reasoningFollowPaused = false;
      meta.answersAtStart = document.querySelectorAll(
        '[data-step-type="assistant_message"]'
      ).length;
    }
    if (titleCount !== meta.lastGrowth || len !== meta.lastLen) {
      meta.lastGrowth = titleCount ?? len;
      meta.lastLen = len;
      meta.stableAt = now;
    }
    if (button.getAttribute("aria-expanded") === "false") {
      // 自动折叠造成的折叠态不能重新展开（否则两个分支互相打架，
      // 思考过程会在回答开始后反复弹开）；只有新回合的初始折叠才展开。
      if (!reasoningAutoFolded.has(button)) {
        button.click();
      }
      return;
    }
    const answerStarted =
      document.querySelectorAll('[data-step-type="assistant_message"]').length
        > meta.answersAtStart;
    const stalled = meta.lastGrowth > 0 && now - meta.stableAt > 3000;
    if ((answerStarted || stalled) && !reasoningAutoFolded.has(button)) {
      reasoningAutoFolded.add(button);
      liveTurnPending = false;
      button.click();
      return;
    }
    let sib = body;
    while (sib) {
      const fromBottom = sib.scrollHeight - sib.scrollTop - sib.clientHeight;
      if (!reasoningFollowPaused && fromBottom > 4) {
        sib.scrollTop = sib.scrollHeight;
      }
      sib = sib.nextElementSibling;
      if (!sib || sib.querySelector?.('button[id^="step-"]')) break;
    }
  }

  const SIDEBAR_ICONS = {
    panel: '<rect x="3" y="4" width="18" height="16" rx="3"/><path d="M9.5 4v16"/>',
    search: '<circle cx="11" cy="11" r="7"/><path d="m20 20-3.8-3.8"/>',
    squarePen: '<path d="M12 3h5a2 2 0 0 1 2 2v5"/><path d="M18.4 3.6a2.1 2.1 0 0 1 3 3L11 17l-4 1 1-4Z"/>',
    shieldCheck: '<path d="M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1z"/><path d="m9 12 2 2 4-4"/>',
  };

  function makeIconControl(label, svgPath, onClick) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "eg-sidebar-icon-button";
    button.setAttribute("aria-label", label);
    button.title = label;
    const icon = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    icon.setAttribute("viewBox", "0 0 24 24");
    icon.setAttribute("fill", "none");
    icon.setAttribute("stroke", "currentColor");
    icon.setAttribute("stroke-width", "1.8");
    icon.setAttribute("stroke-linecap", "round");
    icon.setAttribute("stroke-linejoin", "round");
    icon.setAttribute("aria-hidden", "true");
    icon.innerHTML = svgPath;
    button.append(icon);
    button.addEventListener("click", onClick);
    return button;
  }

  function storedSidebarOpen() {
    const match = document.cookie.match(
      /(?:^|;\s*)sidebar:state=(true|false)(?:;|$)/
    );
    return match ? match[1] === "true" : null;
  }

  function restoreSidebarPreference(group, trigger) {
    const preferredOpen = storedSidebarOpen();
    if (preferredOpen === null) return false;

    const currentOpen = group.dataset.state !== "collapsed";
    if (currentOpen === preferredOpen) return false;

    // Chainlit writes this cookie on manual collapse, but its SidebarProvider
    // does not read it after `/thread/:id` and `/` remount the page layout.
    // Restore the user's choice before updating the custom presentation.
    trigger.click();
    return true;
  }

  function enhanceSidebar() {
    const sidebar = document.querySelector('[data-sidebar="sidebar"]');
    const mobile = sidebar?.dataset.mobile === "true";
    const group = sidebar?.closest(".group.peer");
    const header = sidebar?.querySelector('[data-sidebar="header"] > .flex.items-center.justify-between');
    if (!sidebar || (!group && !mobile) || !header) return false;

    const sourceTrigger = header.querySelector("#sidebar-trigger-button");
    const sourceSearch = header.querySelector("#search-chats-button");
    const sourceNew = header.querySelector("#new-chat-button");
    if (!sourceTrigger || !sourceSearch || !sourceNew) return false;

    const refsChanged = !sidebarRefs || !sidebarRefs.trigger.isConnected
      || !sidebarRefs.search.isConnected || !sidebarRefs.new.isConnected;
    // 呈现层可能被 React 重渲染单独移除（弹窗关闭等），与源按钮是否变化
    // 无关——两种情况都要重建。
    const presentationMissing = !header.querySelector(".eg-sidebar-presentation");
    if (refsChanged || presentationMissing) {
      const collapseControl = makeIconControl(
      !mobile && group.dataset.state === "collapsed" ? "展开侧栏" : "收起侧栏",
      SIDEBAR_ICONS.panel,
      () => sidebarRefs?.trigger.click()
    );
      if (refsChanged) {
      sidebarRefs = {
        trigger: sourceTrigger,
        search: sourceSearch,
        new: sourceNew,
        collapseControl,
      };
      }
      // Native header controls stay in React's tree, but are represented by
      // themed controls. Clicks are delegated back to the source buttons.
      header.classList.add("eg-original-sidebar-header");
      header.querySelector(".eg-sidebar-presentation")?.remove();

      const presentation = document.createElement("div");
      presentation.className = "eg-sidebar-presentation";

      const brand = document.createElement("div");
      brand.className = "eg-sidebar-brand";
      brand.innerHTML = `
        <img class="eg-logo-dark" src="/public/brand-logo.png" alt="AlgoSage">
        <img class="eg-logo-light" src="/public/brand-logo-light.png" alt="">
      `;

      const actions = document.createElement("div");
      actions.className = "eg-sidebar-actions";
      const searchIcon = SIDEBAR_ICONS.search;
      const collapseIcon = SIDEBAR_ICONS.panel;
      const searchControl = makeIconControl("搜索会话", searchIcon, () => sidebarRefs.search.click());
      actions.append(searchControl, collapseControl);
      presentation.append(brand, actions);

      const newChatAction = document.createElement("button");
      newChatAction.type = "button";
      newChatAction.className = "eg-new-chat-action";
      newChatAction.setAttribute("aria-label", "新建对话");
      const plus = document.createElementNS("http://www.w3.org/2000/svg", "svg");
      plus.setAttribute("viewBox", "0 0 24 24");
      plus.setAttribute("fill", "none");
      plus.setAttribute("stroke", "currentColor");
      plus.setAttribute("stroke-width", "2");
      plus.setAttribute("stroke-linecap", "round");
      plus.setAttribute("aria-hidden", "true");
      plus.innerHTML = SIDEBAR_ICONS.squarePen;
      const newLabel = document.createElement("span");
      newLabel.textContent = "新建对话";
      newChatAction.append(plus, newLabel);
      newChatAction.addEventListener("click", () => {
        const { new: newControl, trigger } = sidebarRefs;
        newControl.click();
        // Starting an empty chat can stay on the same route, so the drawer
        // needs closing explicitly when navigation has not unmounted it.
        if (mobile && sidebar.isConnected && sidebar.dataset.state === "open") trigger.click();
      });

      presentation.append(newChatAction);
      header.append(presentation);
    }

    sidebarRefs.collapseControl.setAttribute(
      "aria-label",
      !mobile && group.dataset.state === "collapsed" ? "展开侧栏" : "收起侧栏"
    );
    sidebarRefs.collapseControl.title = sidebarRefs.collapseControl.getAttribute("aria-label");

    // A mobile drawer has no desktop peer/state or persisted desktop width.
    // Reuse its header controls without replaying the desktop collapse cookie.
    if (mobile) return true;

    if (restoreSidebarPreference(group, sourceTrigger)) return false;

    if (sidebarRestorePending) {
      // Keep motion suppressed through React's post-click paint, then remove
      // the flag after the longest sidebar transition would have completed.
      sidebarRestorePending = false;
      window.setTimeout(() => {
        document.body.classList.remove("eg-sidebar-restoring");
      }, 360);
    }

    const collapsed = group.dataset.state === "collapsed";
    document.body.classList.toggle("eg-sidebar-collapsed", collapsed);
    if (collapsedBar) {
      if (!collapsed && collapsedBar.contains(document.activeElement)) {
        document.activeElement.blur();
      }
      collapsedBar.id = "eg-collapsed-bar";
      collapsedBar.className = "eg-collapsed-bar";
      collapsedBar.classList.toggle("eg-sidebar-collapsed", collapsed);
      collapsedBar.setAttribute("aria-hidden", String(!collapsed));
    }
    return true;
  }

  function buildCollapsedBar() {
    collapsedBar = document.createElement("div");
    collapsedBar.id = "eg-collapsed-bar";
    collapsedBar.className = "eg-collapsed-bar";
    collapsedBar.setAttribute("aria-hidden", "true");

    const logo = document.createElement("img");
    logo.src = "/public/favicon.png";
    logo.alt = "AlgoSage";
    logo.className = "eg-collapsed-logo";

    const pill = document.createElement("div");
    pill.className = "eg-collapsed-pill";
    const expandButton = makeIconControl("展开侧栏", SIDEBAR_ICONS.panel, () => sidebarRefs?.trigger.click());
    const searchButton = makeIconControl("搜索会话", SIDEBAR_ICONS.search, () => sidebarRefs?.search.click());
    const newButton = makeIconControl("新建对话", SIDEBAR_ICONS.squarePen, () => sidebarRefs?.new.click());
    pill.append(expandButton, searchButton, newButton);
    collapsedBar.append(logo, pill);
    document.body.append(collapsedBar);
  }

  function enhanceMobileHeader() {
    const header = document.getElementById("header");
    const navigation = header?.firstElementChild;
    const tools = header?.lastElementChild;
    if (!navigation || !tools || !navigation.querySelector("#sidebar-trigger-button")) return;
    if (!navigation.classList.contains("eg-header-navigation")) navigation.classList.add("eg-header-navigation");
    if (!tools.classList.contains("eg-header-tools")) tools.classList.add("eg-header-tools");

    if (!navigation.querySelector(".eg-mobile-navigation")) {
      // Presentation controls delegate to the original React-owned actions.
      // Keep these in header flow; the desktop floating bar stays separate.
      const mobileNavigation = document.createElement("div");
      mobileNavigation.className = "eg-mobile-navigation";
      const logo = document.createElement("img");
      logo.src = "/public/favicon.png";
      logo.alt = "AlgoSage";
      const actions = document.createElement("div");
      actions.className = "eg-mobile-actions";
      const nativeAction = (id) => header.querySelector(`#${id}`)?.click();
      actions.append(
        makeIconControl("展开侧栏", SIDEBAR_ICONS.panel, () => nativeAction("sidebar-trigger-button")),
        makeIconControl("新建对话", SIDEBAR_ICONS.squarePen, () => nativeAction("new-chat-button"))
      );
      mobileNavigation.append(logo, actions);
      navigation.append(mobileNavigation);
    }

    const readme = header.querySelector("#readme-button");
    if (readme && !readme.querySelector(".eg-readme-icon")) {
      const icon = document.createElementNS("http://www.w3.org/2000/svg", "svg");
      icon.classList.add("eg-readme-icon");
      icon.setAttribute("viewBox", "0 0 24 24");
      icon.setAttribute("fill", "none");
      icon.setAttribute("stroke", "currentColor");
      icon.setAttribute("stroke-width", "1.8");
      icon.setAttribute("stroke-linecap", "round");
      icon.setAttribute("stroke-linejoin", "round");
      icon.setAttribute("aria-hidden", "true");
      icon.innerHTML = '<path d="M12 7v14m0-14C9 4 5 4 2 5v14c3-1 7-1 10 2m0-14c3-3 7-3 10-2v14c-3-1-7-1-10 2"/>';
      readme.append(icon);
      readme.setAttribute("aria-label", "说明");
      readme.title = "说明";
    }
    const settings = header.querySelector("#chat-settings-header-button");
    if (settings && !settings.hasAttribute("aria-label")) settings.setAttribute("aria-label", "设置");
    const userMenu = header.querySelector("#user-nav-button");
    if (userMenu && !userMenu.hasAttribute("aria-label")) userMenu.setAttribute("aria-label", "用户菜单");
  }

  function enhanceStepStatusIcons() {
    document.querySelectorAll('main button[id^="step-"]').forEach((button) => {
      const row = button.closest(".ai-message");
      const host = row?.querySelector(":scope > span");
      if (!host) return;

      const frame = host.firstElementChild || host;
      host.classList.add("eg-step-avatar");
      frame.classList.add("eg-step-avatar-shell");
      frame.setAttribute("aria-hidden", "true");

      const nativeVisual = frame.querySelector(
        "svg:not(.eg-step-status-icon), img"
      );
      const existing = frame.querySelector(".eg-step-status-icon");
      if (nativeVisual) {
        existing?.remove();
        return;
      }
      if (existing) return;

      const icon = document.createElementNS("http://www.w3.org/2000/svg", "svg");
      icon.classList.add("eg-step-status-icon");
      icon.setAttribute("viewBox", "0 0 24 24");
      icon.setAttribute("fill", "none");
      icon.setAttribute("stroke", "currentColor");
      icon.setAttribute("stroke-width", "2");
      icon.setAttribute("stroke-linecap", "round");
      icon.setAttribute("stroke-linejoin", "round");
      icon.innerHTML = SIDEBAR_ICONS.shieldCheck;
      frame.append(icon);
    });
  }

  function enhanceWelcome() {
    const welcome = document.getElementById("welcome-screen");
    if (!welcome) return false;
    const alreadyEnhanced = welcome.dataset.egChatUi === "1";
    let heading = welcome.querySelector(".prose > h3");
    let lede = heading?.nextElementSibling;
    let configCard = welcome.querySelector(".prose > div:has(table)");
    let usageHeading = configCard?.nextElementSibling;
    let usageList = usageHeading?.nextElementSibling;

    // Chainlit can reuse this DOM after returning from a thread while resetting
    // React-controlled classes. Restore presentation state idempotently; never
    // key the layout only on dataset.
    if (!alreadyEnhanced) {
      if (!heading || !lede || !configCard || !usageHeading || !usageList) return false;
      welcome.dataset.egChatUi = "1";
      heading.classList.add("eg-welcome-title");
      lede.classList.add("eg-welcome-lede");
      configCard.classList.add("eg-config-card");

      // The markdown renderer emits the usage heading and list as siblings.
      // Move only these presentation siblings into one card wrapper.
      const usageCard = document.createElement("section");
      usageCard.className = "eg-usage-card";
      usageHeading.parentNode.insertBefore(usageCard, usageHeading);
      usageCard.append(usageHeading, usageList);
      usageHeading.classList.add("eg-card-heading");

      const icon = document.createElementNS("http://www.w3.org/2000/svg", "svg");
      icon.setAttribute("viewBox", "0 0 24 24");
      icon.setAttribute("fill", "none");
      icon.setAttribute("stroke", "currentColor");
      icon.setAttribute("stroke-width", "1.7");
      icon.setAttribute("stroke-linecap", "round");
      icon.setAttribute("stroke-linejoin", "round");
      icon.setAttribute("aria-hidden", "true");
      icon.classList.add("eg-card-icon");
      icon.innerHTML = '<path d="M4 19.5V6a2 2 0 0 1 2-2h11a1 1 0 0 1 1 1v14a1 1 0 0 1-1 1H6.5A2.5 2.5 0 0 1 4 17.5Z"/><path d="M8 8h7M8 12h6"/>';
      usageHeading.prepend(icon);
    }

    welcome.classList.add("eg-welcome", "eg-empty-welcome");
    welcome.setAttribute("aria-label", "AlgoSage 新会话");

    // #message-composer is already mounted inside #welcome-screen. Keep it in
    // React's original tree so drafts, uploads, disabled state and submit all
    // share exactly one component; this is a layout transform, not a clone.
    const details = heading.closest(".prose")?.parentElement;
    if (details) details.hidden = true;

    // The brand is drawn by #welcome-screen::before. Injected logo DOM used to
    // be removed and restored while React re-rendered during IME input, which
    // made it flash. A pseudo-element belongs to the stable layout row instead.

    // Chainlit puts a 48rem max-width inline on this container. Use the compact
    // welcome column without fighting the inline declaration from CSS.
    const container = welcome.parentElement;
    if (container) {
      container.style.width = "min(54rem, 100%)";
      container.style.maxWidth = "min(54rem, 100%)";
    }
    return true;
  }

  // 说明弹窗不再注入“当前运行配置”卡片：运行配置已在设置面板与输入区
  // 控件上直接可见，弹窗只保留说明文案。
  function enhanceReadmeDetails() {
    return false;
  }

  buildCollapsedBar();

  // Chainlit remounts the empty-state and chat-state composers. Submission
  // events only arm the transition and measure the retiring node. The node is
  // promoted to a FLIP actor when Chainlit actually removes it, so Enter and
  // click handlers never compete with the real submit flow.
  const composerMotion = {
    phase: "idle",
    actor: null,
    stage: null,
    portal: null,
    candidate: null,
    firstRect: null,
    sourceParent: null,
    sourceNext: null,
    sourceStyle: null,
    ghost: null,
    timer: 0,
    guardUntil: 0,
    direction: "none",
    reverseEligible: false,
  };
  const reduceMotionQuery = window.matchMedia("(prefers-reduced-motion: reduce)");
  function motionDisabled() {
    return reduceMotionQuery.matches || document.body.classList.contains("eg-preferences-no-motion");
  }
  window.__egComposerMotion = composerMotion;

  function activeWelcome() {
    return document.getElementById("welcome-screen");
  }

  function activeComposer() {
    return document.getElementById("message-composer");
  }

  function composerHasPayload(composer) {
    const input = composer.querySelector("#chat-input");
    const files = composer.querySelector('input[type="file"]')?.files;
    return Boolean(input?.value.trim() || files?.length);
  }

  function isCoarsePointer() {
    return window.matchMedia("(pointer: coarse)").matches;
  }

  function ensureMotionPortal() {
    if (composerMotion.portal?.isConnected) return composerMotion.portal;
    const portal = document.createElement("div");
    portal.id = "eg-composer-motion-portal";
    portal.setAttribute("aria-hidden", "false");
    document.body.append(portal);
    composerMotion.portal = portal;
    return portal;
  }

  function brandRectForComposer(composer) {
    const composerRect = composer.getBoundingClientRect();
    const width = Math.max(196, Math.min(262, window.innerWidth * 0.22));
    const height = width * (724 / 2172);
    return {
      left: composerRect.left + composerRect.width / 2 - width / 2,
      top: composerRect.top - 30 - height,
      width,
      height,
      right: composerRect.left + composerRect.width / 2 + width / 2,
      bottom: composerRect.top - 30,
    };
  }

  function captureBrandGhost(rect) {
    const dark = document.documentElement.classList.contains("dark");
    const ghost = document.createElement("div");
    ghost.className = "eg-brand-exit-ghost";
    ghost.setAttribute("aria-hidden", "true");
    Object.assign(ghost.style, {
      position: "fixed",
      left: `${rect.left}px`,
      top: `${rect.top}px`,
      width: `${rect.width}px`,
      height: `${rect.height}px`,
      margin: "0",
      pointerEvents: "none",
      backgroundImage: `url('/public/brand-logo-${dark ? "dark" : "light"}.png')`,
      backgroundSize: "contain",
      backgroundRepeat: "no-repeat",
      backgroundPosition: "center",
    });
    ensureMotionPortal().append(ghost);
    composerMotion.ghost = ghost;
    return ghost;
  }

  function captureGhost(node, className) {
    if (!node) return null;
    const rect = node.getBoundingClientRect();
    const ghost = node.cloneNode(true);
    ghost.className = `${className} ${node.className || ""}`;
    ghost.removeAttribute("id");
    ghost.setAttribute("aria-hidden", "true");
    Object.assign(ghost.style, {
      position: "fixed",
      left: `${rect.left}px`,
      top: `${rect.top}px`,
      width: `${rect.width}px`,
      height: `${rect.height}px`,
      margin: "0",
      pointerEvents: "none",
    });
    ensureMotionPortal().append(ghost);
    composerMotion.ghost = ghost;
    return ghost;
  }

  function removeMotionGhost() {
    composerMotion.ghost?.remove();
    composerMotion.ghost = null;
  }

  function captureComposerActor(candidate, firstRect) {
    const rect = firstRect || candidate.getBoundingClientRect();
    composerMotion.sourceParent = candidate.parentNode;
    composerMotion.sourceNext = candidate.nextSibling;
    composerMotion.sourceStyle = candidate.getAttribute("style");

    const stage = document.createElement("div");
    stage.className = "eg-composer-motion-stage";
    stage.style.left = `${rect.left}px`;
    stage.style.top = `${rect.top}px`;
    stage.style.width = `${rect.width}px`;
    stage.style.height = `${rect.height}px`;

    candidate.classList.add("eg-composer-motion-actor");
    candidate.dataset.egOriginalId = candidate.id || "message-composer";
    candidate.removeAttribute("id");
    candidate.style.width = "100%";
    stage.append(candidate);
    ensureMotionPortal().append(stage);
    composerMotion.actor = candidate;
    composerMotion.stage = stage;
    return rect;
  }

  function releaseComposerActor(target) {
    if (target) target.classList.remove("eg-motion-hidden");
    composerMotion.stage?.remove();
    composerMotion.portal?.remove();
    if (composerMotion.actor) {
      composerMotion.actor.classList.remove("eg-composer-motion-actor");
      delete composerMotion.actor.dataset.egMotionCandidate;
      delete composerMotion.actor.dataset.egOriginalId;
      composerMotion.actor.style.removeProperty("width");
      composerMotion.actor.style.removeProperty("height");
    }
    composerMotion.actor = null;
    composerMotion.stage = null;
    composerMotion.portal = null;
    composerMotion.candidate = null;
    composerMotion.firstRect = null;
    composerMotion.sourceParent = null;
    composerMotion.sourceNext = null;
    composerMotion.sourceStyle = null;
    removeMotionGhost();
    document.getElementById('welcome-screen')?.classList.remove("eg-brand-reveal");
    composerMotion.phase = "idle";
    composerMotion.timer = 0;
    composerMotion.reverseEligible = composerMotion.direction === "to-chat";
    composerMotion.direction = "none";
  }

  function disarmComposerCandidate() {
    const candidate = composerMotion.candidate;
    if (candidate?.isConnected) {
      delete candidate.dataset.egMotionCandidate;
      if (candidate.dataset.egOriginalId) {
        candidate.id = candidate.dataset.egOriginalId;
        delete candidate.dataset.egOriginalId;
      }
    }
    const welcome = activeWelcome();
    delete welcome?.dataset.egBrandExit;
    welcome?.classList.remove("eg-brand-reveal");
    removeMotionGhost();
    composerMotion.candidate = null;
    composerMotion.firstRect = null;
    composerMotion.sourceParent = null;
    composerMotion.sourceNext = null;
    composerMotion.sourceStyle = null;
    composerMotion.phase = "idle";
    composerMotion.direction = "none";
    composerMotion.timer = 0;
  }

  function settleMotionImmediately() {
    if (composerMotion.phase === "waiting-chat" || composerMotion.phase === "waiting-welcome") {
      const target = activeComposer();
      if (target && target !== composerMotion.actor) releaseComposerActor(target);
      else if (composerMotion.actor) releaseComposerActor(null);
      else disarmComposerCandidate();
      return;
    }
    if (composerMotion.phase === "armed-chat" || composerMotion.phase === "armed-welcome") {
      disarmComposerCandidate();
    }
  }

  function animateComposerActor(first, target, duration) {
    const dx = target.left - first.left;
    const dy = target.top - first.top;
    const scaleX = target.width / Math.max(first.width, 1);
    const scaleY = target.height / Math.max(first.height, 1);
    return composerMotion.stage.animate(
      [
        { transform: "none" },
        {
          transform: `translate3d(${dx}px, ${dy}px, 0) scale(${scaleX}, ${scaleY})`,
        },
      ],
      { duration, easing: "cubic-bezier(0.22, 1, 0.36, 1)", fill: "forwards" }
    ).finished;
  }

  function activateForwardActor(candidate) {
    if (composerMotion.phase !== "armed-chat") return;
    window.clearTimeout(composerMotion.timer);
    const firstRect = captureComposerActor(candidate, composerMotion.firstRect);
    composerMotion.phase = "waiting-chat";

    const started = performance.now();
    const duration = isCoarsePointer() ? 320 : 520;
    composerMotion.timer = window.setInterval(() => {
      const target = activeComposer();
      if (!target || target === composerMotion.actor) {
        if (performance.now() - started > 2400) {
          window.clearInterval(composerMotion.timer);
          releaseComposerActor(null);
        }
        return;
      }
      window.clearInterval(composerMotion.timer);
      target.classList.add("eg-motion-hidden");
      const targetRect = target.getBoundingClientRect();
      const userMessage = document.querySelector('[data-step-type="user_message"]');
      if (userMessage) userMessage.classList.add("eg-first-message-reveal");

      window.setTimeout(async () => {
        if (composerMotion.phase !== "waiting-chat") return;
        try {
          if (!motionDisabled()) {
            await animateComposerActor(firstRect, targetRect, duration);
          }
          releaseComposerActor(target);
          document.getElementById("welcome-screen")
            ?.removeAttribute("data-eg-brand-exit");
          target.querySelector("#chat-input")?.focus({ preventScroll: true });
        } catch (_) {
          settleMotionImmediately();
        }
      }, 0);
    }, 24);
  }

  function activateReverseActor(candidate) {
    if (composerMotion.phase !== "armed-welcome") return;
    window.clearTimeout(composerMotion.timer);
    const firstRect = captureComposerActor(candidate, composerMotion.firstRect);
    composerMotion.phase = "waiting-welcome";

    const started = performance.now();
    const duration = isCoarsePointer() ? 220 : 350;
    composerMotion.timer = window.setInterval(() => {
      const welcome = activeWelcome();
      const target = activeComposer();
      if (!welcome || !target || target === composerMotion.actor) {
        if (performance.now() - started > 2000) {
          window.clearInterval(composerMotion.timer);
          releaseComposerActor(null);
        }
        return;
      }
      window.clearInterval(composerMotion.timer);
      target.classList.add("eg-motion-hidden");
      if (!motionDisabled()) welcome.classList.add("eg-brand-reveal");
      const targetRect = target.getBoundingClientRect();

      window.setTimeout(async () => {
        if (composerMotion.phase !== "waiting-welcome") return;
        try {
          if (!motionDisabled()) {
            await animateComposerActor(firstRect, targetRect, duration);
          }
          releaseComposerActor(target);
          target.querySelector("#chat-input")?.focus({ preventScroll: true });
        } catch (_) {
          settleMotionImmediately();
        }
      }, 0);
    }, 24);
  }

  // Only the readme covers the app. The settings panel must leave navigation
  // available. Use the trigger's dialog ID instead of treating any h1 as readme.
  // 关闭判定不能只看元素移除：Radix 关闭会先进入 data-state=closed 的退出
  // 动画（dialog 仍在 DOM 数百毫秒），期间 eg-readme-open 若持续挂着，侧栏
  // 呈现层一直隐藏——表现为关闭说明后侧栏闪成裸列表。改为「不可见即复位」，
  // 侧栏在退出动画期间就恢复显示。
  function syncReadmeVisibility() {
    const btn = document.getElementById("readme-button");
    const dialogId = btn?.getAttribute("aria-controls");
    const dialog = dialogId ? document.getElementById(dialogId) : null;
    const open =
      !!dialog &&
      dialog.getAttribute("role") === "dialog" &&
      getComputedStyle(dialog).display !== "none" &&
      dialog.getAttribute("data-state") !== "closed";
    document.body.classList.toggle("eg-readme-open", open);
  }
  /* 思考过程步骤：跨容器移到正式回答上方。
     首个思考增量到达时页面可能仍在欢迎视图，思考 Step 会渲染进欢迎
     容器；而回答流式渲染在消息容器——两者是兄弟容器，必须跨容器移动。 */
  function syncReasoningPlacement() {
    // NodeList 没有 .find；归位查找需要真数组。
    const answers = [
      ...document.querySelectorAll('[data-step-type="assistant_message"]'),
    ];
    if (!answers.length) return;
    // 每个思考行归位到它自己回合的回答上方（历史会话里可能有多条）。
    document.querySelectorAll(".eg-reasoning-step").forEach((container) => {
      const row = container.closest(".step") || container.parentElement;
      if (!row) return;
      const aMsg = answerForReasoningRow(row, answers);
      if (!aMsg) return;
      const aContainer = aMsg.parentElement;
      const aMsgRow = aMsg.closest(".step") || aMsg;
      if (row.parentElement === aContainer) {
        // 同容器：确保思考行紧邻回答行之前即可。
        if (row.nextElementSibling === aMsgRow) return;
        // 与目标回答之间若只隔着其它思考行，说明已经就位——再插入会与
        // 后面的思考行互相争位、每帧来回搬动（React 节点被外部反复移动
        // 后协调崩溃：点击失效/白屏）。
        let sib = row.nextElementSibling;
        while (sib && sib !== aMsgRow) {
          if (!sib.querySelector?.(".eg-reasoning-step")) break;
          sib = sib.nextElementSibling;
        }
        if (sib === aMsgRow) return;
        aContainer.insertBefore(row, aMsgRow);
        return;
      }
      // 跨容器：思考行与其后的输出片段整体移到回答行之前。
      const moving = [];
      let sib = row.nextElementSibling;
      while (sib) {
        moving.push(sib);
        sib = sib.nextElementSibling;
      }
      aContainer.insertBefore(row, aMsgRow);
      moving.forEach((n) => aContainer.insertBefore(n, aMsgRow));
    });
    // 审计归位放在思考归位之后：思考行插入回答前会把审计挤离回答相邻位。
    syncAuditPlacement();
  }

  /* 审计面板行归位到它回合的回答之后。回合收口时审计步骤在服务端最后
     创建，React 实时渲染的顺序可能把它插到思考行甚至回答之前。 */
  function syncAuditPlacement() {
    const answers = [
      ...document.querySelectorAll('[data-step-type="assistant_message"]'),
    ];
    if (!answers.length) return;
    document
      .querySelectorAll('main button[id^="step-"]')
      .forEach((button) => {
        let name = "";
        try {
          name = decodeURIComponent(button.id.slice("step-".length));
        } catch (_) {
          return;
        }
        if (name !== "security-audit-policy") return;
        const row = button.closest(".step");
        if (!row || !row.parentElement) return;
        // 审计面板属于同回合的回答：先找前面最近的回答（创建顺序）；实时
        // 渲染可能把审计插到回答上方，此时向后找最近的回答并移到它后面。
        let prev = row;
        let aMsg = null;
        while ((prev = prev.previousElementSibling)) {
          // 向后查找限定在同回合内（跨过 user_message 即进入别的回合）。
          if (prev.querySelector?.('[data-step-type="user_message"]')) break;
          aMsg = answers.find((a) => prev === a || (prev.contains && prev.contains(a)));
          if (aMsg) break;
        }
        if (!aMsg) {
          let sib = row;
          while ((sib = sib.nextElementSibling)) {
            // 向前兜底只在本回合内：跨过 user_message 就是别的回合了，
            // 误配对会把审计搬到其它回合的回答后面。
            if (sib.querySelector?.('[data-step-type="user_message"]')) break;
            aMsg = answers.find((a) => sib === a || (sib.contains && sib.contains(a)));
            if (aMsg) break;
          }
        }
        if (!aMsg) return;
        const aRow = aMsg.closest(".step") || aMsg;
        if (row.previousElementSibling === aRow) return; // 已就位
        if (row.parentElement !== aRow.parentElement) return; // 只处理同容器的平铺布局
        aRow.parentElement.insertBefore(row, aRow.nextElementSibling);
      });
  }

  /* 以 user_message 行为界确定思考行所属的回合组，返回组内第一个回答。
     孤儿芯片（回合被中断/失败清理，组内没有回答）返回 null 并原地保留——
     跨组兜底配对会让多条孤儿芯片互相争位、反复搬动。 */
  function answerForReasoningRow(row, answers) {
    let start = row;
    while (start.previousElementSibling) {
      const prev = start.previousElementSibling;
      if (prev.querySelector?.('[data-step-type="user_message"]')) break;
      start = prev;
    }
    for (let sib = start; sib; sib = sib.nextElementSibling) {
      for (const a of answers) {
        if (sib === a || (sib.contains && sib.contains(a))) return a;
      }
      if (sib.querySelector?.('[data-step-type="user_message"]')) break;
    }
    return null;
  }

  syncReadmeVisibility();
  new MutationObserver(syncReadmeVisibility).observe(document.body, {
    childList: true,
    subtree: true,
    attributes: true,
    attributeFilter: ["aria-expanded", "data-state"],
  });

  const composerRemovalObserver = new MutationObserver((mutations) => {
    if (composerMotion.phase !== "armed-chat" && composerMotion.phase !== "armed-welcome") {
      // 发送真正的标志：欢迎页连同其中的 composer 一起被 React 移除。
      // 此刻捕获旧 composer 首帧并武装正向转场。
      if (composerMotion.phase !== "idle") return;
      for (const mutation of mutations) {
        for (const removedRoot of mutation.removedNodes) {
          if (removedRoot.nodeType !== 1) continue;
          if (!removedRoot.querySelector?.("#welcome-screen")
            && removedRoot.id !== "welcome-screen") continue;
          const composer = removedRoot.querySelector?.("#message-composer");
          if (composer && composerHasPayload(composer)) {
            composerMotion.candidate = composer;
            composerMotion.firstRect = composer.getBoundingClientRect();
            armOnWelcomeRemoval(composer);
          }
          return;
        }
      }
      return;
    }
    for (const mutation of mutations) {
      for (const removedRoot of mutation.removedNodes) {
        if (removedRoot.nodeType !== 1) continue;
        const candidate = removedRoot.dataset?.egMotionCandidate === "1"
          ? removedRoot
          : removedRoot.querySelector?.('[data-eg-motion-candidate="1"]');
        if (!candidate) continue;
        if (composerMotion.phase === "armed-chat") activateForwardActor(candidate);
        else if (composerMotion.phase === "armed-welcome") activateReverseActor(candidate);
        return;
      }
    }
  });
  composerRemovalObserver.observe(document.body, { childList: true, subtree: true });

  /* 发送转场由"欢迎页被卸载"驱动：消息真正发出、React 移除欢迎页时，
     composerRemovalObserver 捕获旧 composer 首帧并播放飞入动画。
     不再由回车/按钮预触发——按键不代表消息真的发出，预触发会让 logo
     淡出后又淡回（未发送时页面留在欢迎页，兜底定时器到期复位）。 */
  function armOnWelcomeRemoval(removedComposer) {
    if (motionDisabled()) return false;
    const composer = composerMotion.candidate || removedComposer;
    if (!composer) return false;
    composerMotion.phase = "armed-chat";
    composerMotion.direction = "to-chat";
    composerMotion.candidate = composer;
    composer.dataset.egMotionCandidate = "1";
    const welcome = activeWelcome();
    if (welcome) welcome.dataset.egBrandExit = "1";
    captureBrandGhost(composerMotion.firstRect || composer.getBoundingClientRect());
    composerMotion.timer = window.setTimeout(() => {
      if (composerMotion.phase === "armed-chat") disarmComposerCandidate();
    }, 2000);
    return true;
  }

  function armReverseTransition() {
    const composer = activeComposer();
    if (!composer || activeWelcome()) return false;
    composerMotion.phase = "armed-welcome";
    composerMotion.direction = "to-welcome";
    composerMotion.candidate = composer;
    composerMotion.firstRect = composer.getBoundingClientRect();
    composer.dataset.egMotionCandidate = "1";
    composerMotion.timer = window.setTimeout(() => {
      if (composerMotion.phase === "armed-welcome") disarmComposerCandidate();
    }, 2000);

    const messageSurface = document.querySelector(
      "main div.relative.flex.flex-col.flex-grow.overflow-y-auto"
    );
    if (messageSurface) {
      captureGhost(messageSurface, "eg-chat-exit-ghost");
      composerMotion.ghost?.classList.add("eg-chat-exit-ghost");
    }
    return true;
  }

  function beginReverseTransition() {
    if (reduceMotionQuery.matches) {
      composerMotion.reverseEligible = false;
      return false;
    }
    const result = armReverseTransition();
    if (!result) composerMotion.direction = "none";
    return result;
  }

  function visibleNewChatButton(eventTarget) {
    const button = eventTarget?.closest?.('[aria-label="新建对话"]');
    return button && Boolean(button.offsetWidth || button.offsetHeight) ? button : null;
  }

  document.addEventListener(
    "click",
    (event) => {
      const now = performance.now();
      if (now < composerMotion.guardUntil) return;

      if (visibleNewChatButton(event.target)) {
        if (composerMotion.phase === "idle" && !activeWelcome() && composerMotion.reverseEligible) {
          composerMotion.guardUntil = now + 900;
          beginReverseTransition();
        }
      }
    },
    true
  );

  document.addEventListener(
    "click",
    (event) => {
      if (event.target?.closest?.('a[href^="/thread/"]')) {
        composerMotion.reverseEligible = false;
      }
    },
    true
  );

  window.addEventListener("resize", () => {
    if (composerMotion.phase !== "idle") settleMotionImmediately();
  });

  new MutationObserver(() => {
    if (composerMotion.phase !== "idle") settleMotionImmediately();
  }).observe(document.documentElement, {
    attributes: true,
    attributeFilter: ["class"],
  });


  function scheduleEnhance() {
    if (scheduled) return;
    scheduled = true;
    requestAnimationFrame(() => {
      scheduled = false;
      syncRouteFlag();
      enhanceMobileHeader();
      enhanceSidebar();
      enhanceStepStatusIcons();
      translateStepLabels();
      enhanceWelcome();
      enhanceReadmeDetails();
      ensureReasoningExpanded();
      syncReasoningPlacement();
    });
  }

  // 路由标记：/thread/* 时隐藏欢迎屏（它只属于首页“新建对话”视图）。
  // SPA 切换线程时 Chainlit 会先渲染欢迎屏再渲染历史（数据未到的骨架），
  // 深色主题下表现为闪现大字欢迎屏——这里按路由隐藏。
  function syncRouteFlag() {
    document.body.classList.toggle(
      "eg-in-thread",
      location.pathname.startsWith("/thread/")
    );
  }
  syncRouteFlag();
  window.addEventListener("popstate", syncRouteFlag);

  /* 编辑历史消息时隐藏输入区的附件上传：Chainlit 的编辑提交只发送文字
     （edit_message 仅携带 message.output），选了附件也会被静默丢弃——
     与其让学生以为附件已随编辑发出，不如直接隐藏入口。监听编辑态
     composer 出现（消息气泡内嵌输入框），对底部上传按钮置灰并提示。 */
  const editUploadObserver = new MutationObserver(() => {
    const editing = !!document.getElementById("edit-chat-input");
    const upload = document.getElementById("upload-button");
    if (!upload || upload.disabled === editing) return;
    upload.disabled = editing;
    upload.title = editing
      ? "编辑消息暂不支持添加附件；附件请作为新消息发送"
      : "";
    upload.style.opacity = editing ? ".45" : "";
  });
  editUploadObserver.observe(document.body, {
    childList: true,
    subtree: true,
    attributes: true,
  });
  /* 顶部 toast（如“对话已删除”）加一个 ✕ 关闭钮：sonner 由框架渲染，
     不带关闭按钮，用户只能干等自动消失。观察到新 toast 时注入按钮，
     点击立即移除该 toast（sonner 会同步清理对应状态）。 */
  const toastCloseObserver = new MutationObserver(() => {
    document.querySelectorAll("[data-sonner-toast]").forEach((toast) => {
      if (toast.querySelector(".eg-toast-close")) return;
      const button = document.createElement("button");
      button.type = "button";
      button.className = "eg-toast-close";
      button.setAttribute("aria-label", "关闭提示");
      button.innerHTML =
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M18 6 6 18"/><path d="m6 6 12 12"/></svg>';
      button.addEventListener("click", (event) => {
        event.stopPropagation();
        toast.style.transition = "opacity 150ms ease, translate 150ms ease";
        toast.style.opacity = "0";
        setTimeout(() => {
          toast.remove();
        }, 160);
      });
      toast.append(button);
    });
  });
  toastCloseObserver.observe(document.body, { childList: true, subtree: true });

  scheduleEnhance();
  new MutationObserver(scheduleEnhance).observe(document.body, {
    childList: true,
    subtree: true,
    attributes: true,
    attributeFilter: ["data-state", "class"],
  });
  // 弹窗关闭等触发的 React 重渲染会移除自定义侧栏呈现层（品牌 logo、
  // 图标与「新建对话」）。rAF 去抖的恢复与重渲染相位不对齐时会绘制出
  // 空窗（侧栏闪成裸列表）——在同一 mutation 批次里同步重建，浏览器
  // 不会绘制中间态。
  new MutationObserver(function () {
    const sidebar = document.querySelector('[data-sidebar="sidebar"]');
    const header = sidebar?.querySelector(
      '[data-sidebar="header"] > .flex.items-center.justify-between'
    );
    if (
      header &&
      header.querySelector("#sidebar-trigger-button") &&
      !header.querySelector(".eg-sidebar-presentation")
    ) {
      enhanceSidebar();
    }
  }).observe(document.body, { childList: true, subtree: true });

    /* 会话切换交由 Chainlit 原生 SPA 处理（配合服务端 v2 恢复补丁）。
     压测见 commit 历史；如竞态复发再考虑回退整页导航。 */



})();
