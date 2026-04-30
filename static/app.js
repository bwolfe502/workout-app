// Live-session helpers: rest timer + step buttons.
//
// The rest timer is intentionally simple — kicks off after every set save and
// counts down in the topbar. No persistence; refresh resets it. Phase 5 will
// move to a service worker if gym-wifi gets flaky.

(function () {
  const REST_DEFAULT_SECONDS = 120; // 2 min — overridden per-exercise via data attr

  let restInterval = null;
  let restRemaining = 0;

  function ensureRestBar() {
    let bar = document.getElementById("rest-bar");
    if (bar) return bar;
    bar = document.createElement("div");
    bar.id = "rest-bar";
    bar.className = "rest-bar hidden";
    bar.innerHTML = `
      <span class="rest-label">Rest</span>
      <span class="rest-time" id="rest-time">0:00</span>
      <button type="button" class="rest-skip" id="rest-skip">Skip</button>
    `;
    document.body.appendChild(bar);
    bar.querySelector("#rest-skip").addEventListener("click", stopRest);
    return bar;
  }

  function fmt(secs) {
    const m = Math.floor(secs / 60);
    const s = secs % 60;
    return `${m}:${s.toString().padStart(2, "0")}`;
  }

  function tick() {
    restRemaining -= 1;
    if (restRemaining <= 0) {
      stopRest();
      return;
    }
    const el = document.getElementById("rest-time");
    if (el) el.textContent = fmt(restRemaining);
  }

  function startRest(seconds) {
    stopRest();
    const bar = ensureRestBar();
    bar.classList.remove("hidden");
    restRemaining = seconds || REST_DEFAULT_SECONDS;
    document.getElementById("rest-time").textContent = fmt(restRemaining);
    restInterval = window.setInterval(tick, 1000);
  }

  function stopRest() {
    if (restInterval) {
      window.clearInterval(restInterval);
      restInterval = null;
    }
    const bar = document.getElementById("rest-bar");
    if (bar) bar.classList.add("hidden");
  }

  // Listen for htmx swap completion. The server includes a
  // `data-rest-seconds` attr on the swapped exercise block when the swap
  // followed a successful set save.
  document.addEventListener("htmx:afterSwap", (evt) => {
    const target = evt.target;
    if (!target) return;
    const rest = target.querySelector("[data-rest-trigger]");
    if (rest) {
      const secs = parseInt(rest.dataset.restSeconds, 10) || REST_DEFAULT_SECONDS;
      startRest(secs);
    }
  });

  // Step buttons for weight/reps inputs (delegated; works after htmx swaps).
  document.addEventListener("click", (evt) => {
    const btn = evt.target.closest("[data-step]");
    if (!btn) return;
    evt.preventDefault();
    const inputId = btn.dataset.target;
    const step = parseFloat(btn.dataset.step);
    const input = document.getElementById(inputId);
    if (!input) return;
    const cur = parseFloat(input.value) || 0;
    const next = Math.max(0, cur + step);
    // Format: integer if step is integer, else 1 decimal
    input.value = Number.isInteger(step) ? String(Math.round(next)) : next.toFixed(1);
    input.dispatchEvent(new Event("input", { bubbles: true }));
  });
})();
