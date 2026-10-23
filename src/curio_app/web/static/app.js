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

  function updateSelectedCount() {
    var badges = document.querySelectorAll("[data-selected-count]");
    if (!badges.length) {
      return;
    }
    var checked = document.querySelectorAll("input.suggestion-check:checked").length;
    badges.forEach(function (badge) {
      badge.textContent = String(checked);
    });
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
    updateSelectedCount();
  });
  document.addEventListener("change", function (event) {
    var input = event.target;
    if (input && input.classList && input.classList.contains("suggestion-check")) {
      updateSelectedCount();
    }
  });
})();