(function () {
  "use strict";

  var KATEX_OPTIONS = {
    delimiters: [
      { left: "$$", right: "$$", display: true },
      { left: "$", right: "$", display: false },
    ],
    throwOnError: false,
  };

  var SCROLL_MARGIN = 90;

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

  function nearBottom(element) {
    return element.scrollHeight - element.scrollTop - element.clientHeight < SCROLL_MARGIN;
  }

  function scrollChat(force) {
    var chat = document.getElementById("chat");
    if (!chat) {
      return;
    }
    var scrollable = chat.scrollHeight > chat.clientHeight;
    var container = scrollable ? chat : document.scrollingElement;
    if (!container) {
      return;
    }
    if (force || nearBottom(container)) {
      container.scrollTop = container.scrollHeight;
    }
  }

  function autogrow(textarea) {
    textarea.style.height = "auto";
    textarea.style.height = textarea.scrollHeight + "px";
  }

  function syncComposer() {
    var composer = document.getElementById("composer");
    if (!composer) {
      return;
    }
    var streaming = document.querySelector("#chat .bubble.is-streaming") !== null;
    var textarea = composer.querySelector("textarea");
    var button = composer.querySelector("button");
    [textarea, button].forEach(function (element) {
      if (element) {
        element.disabled = streaming;
      }
    });
  }

  function init() {
    renderMath(document.body);
    syncComposer();
    scrollChat(true);
    var fields = document.querySelectorAll("textarea[data-autogrow]");
    fields.forEach(function (textarea) {
      autogrow(textarea);
      textarea.addEventListener("input", function () {
        autogrow(textarea);
      });
    });
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
      syncComposer();
    }
    updateSelectedCount();
  });
  document.body.addEventListener("htmx:sseMessage", function (event) {
    if (event.detail && event.detail.elt) {
      renderMath(event.detail.elt);
    }
    scrollChat(false);
  });
  document.body.addEventListener("htmx:afterRequest", function (event) {
    var form = event.detail ? event.detail.elt : null;
    if (!form || !form.classList || !form.classList.contains("composer")) {
      return;
    }
    if (event.detail.successful && event.target === form) {
      var textarea = form.querySelector("textarea");
      if (textarea) {
        textarea.value = "";
        autogrow(textarea);
      }
      scrollChat(true);
    }
  });
  document.addEventListener("change", function (event) {
    var input = event.target;
    if (input && input.classList && input.classList.contains("suggestion-check")) {
      updateSelectedCount();
    }
  });
})();