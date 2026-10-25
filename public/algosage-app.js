/* Chainlit entrypoint: load theme transition first, then existing UI enhancements. */
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

  loadScript("/public/theme-transition.js?v=20261022-sidebar-polish-1")
    .then(() => loadScript("/public/account-profile.js?v=20261025-focus-return-3"))
    .then(() => loadScript("/public/user-preferences.js?v=20261024-model-picker-2"))
    .then(() => loadScript("/public/model-picker.js?v=20261024-model-picker-5"))
    .then(() => loadScript("/public/chat-ui.js?v=20261024-model-picker-1"))
    .then(() => loadScript("/public/settings-autosave.js?v=20261025-autosave-1"))
    .catch(() => {
      // Existing behavior remains usable even if optional enhancements fail.
      loadScript("/public/account-profile.js?v=20261025-focus-return-3");
    });
})();
