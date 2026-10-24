(() => {
  "use strict";

  // Copy only what is already rendered; no storage, no network.
  document.querySelectorAll("[data-copy-target]").forEach((button) => {
    const target = document.getElementById(button.dataset.copyTarget);
    if (!target) {
      return;
    }

    const block = button.closest(".ai-block, .results");
    const feedback = block ? block.querySelector(".copy-feedback") : null;

    const announce = (text) => {
      if (feedback) {
        feedback.textContent = text;
      }
    };

    button.addEventListener("click", async () => {
      const text = target.textContent || "";

      try {
        await navigator.clipboard.writeText(text);
        announce("Copied to the clipboard.");
      } catch (error) {
        // Fallback for browsers without clipboard permission: select the text.
        const range = document.createRange();
        range.selectNodeContents(target);
        const selection = window.getSelection();
        if (selection) {
          selection.removeAllRanges();
          selection.addRange(range);
        }
        announce("Text selected — press ⌘C to copy.");
      }
    });
  });
})();
