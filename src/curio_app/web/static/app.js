(function () {
  "use strict";

  var KATEX_OPTIONS = {
    delimiters: [
      { left: "$$", right: "$$", display: true },
      { left: "$", right: "$", display: false },
    ],
    throwOnError: false,
  };

  function renderMath(element) {
    if (window.renderMathInElement && element) {
      window.renderMathInElement(element, KATEX_OPTIONS);
    }
  }

  function init() {
    renderMath(document.body);
    if ("serviceWorker" in navigator) {
      navigator.serviceWorker.register("/sw.js").catch(function () {
        // Registration is best-effort; the app works without it.
      });
    }
  }

  document.addEventListener("DOMContentLoaded", init);
  document.body.addEventListener("htmx:afterSwap", function (event) {
    if (event.detail && event.detail.target) {
      renderMath(event.detail.target);
    }
  });
})();