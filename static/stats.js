// Wire Chart.js charts to the JSON payload inlined on /stats.
//
// Charts: volume per muscle/week (multi-line), strength trend (line),
// bodyweight + waist (dual-axis line), and pain frequency (bar).

(function () {
  function ready(fn) {
    if (document.readyState !== "loading") fn();
    else document.addEventListener("DOMContentLoaded", fn);
  }

  ready(function () {
    if (typeof Chart === "undefined" || !window.__statsData) return;

    // Theme-aware defaults: read the live ink/hairline tokens off :root so
    // chart text stays legible whether the user is in light (cream) or dark
    // mode. Falling back to safe values if the CSS isn't available yet.
    const css = getComputedStyle(document.documentElement);
    const ink = css.getPropertyValue("--ink-soft").trim() || "#4A463F";
    const grid = css.getPropertyValue("--hairline").trim() || "rgba(0,0,0,0.08)";
    Chart.defaults.color = ink;
    Chart.defaults.borderColor = grid;

    const data = window.__statsData;
    drawVolume(data);
    drawStrength(data.strength);
    drawBodyweight(data.bodyweight);
    drawPain(data.pain);
  });

  // ---- volume ------------------------------------------------------------

  // Distinct-ish hues per muscle. Manual palette — Chart.js's default is fine
  // for prototypes but the full muscle list overlaps badly without tweaking.
  const MUSCLE_COLORS = {
    chest:        "#ef5350",
    back:         "#26a69a",
    quads:        "#5c6bc0",
    hamstrings:   "#ffa726",
    glutes:       "#8d6e63",
    front_delt:   "#ffd54f",
    side_delt:    "#42a5f5",
    rear_delt:    "#ab47bc",
    biceps:       "#9ccc65",
    brachialis:   "#7cb342",
    triceps:      "#ff7043",
    abs:          "#bdbdbd",
    calves:       "#78909c",
    upper_back:   "#00897b",
  };

  function colorFor(muscle) {
    return MUSCLE_COLORS[muscle] || "#9e9e9e";
  }

  function drawVolume(data) {
    const canvas = document.getElementById("chart-volume");
    if (!canvas || !data.volume || Object.keys(data.volume).length === 0) return;
    const muscles = Object.keys(data.volume);
    const labels = Object.keys(data.volume[muscles[0]]);
    const datasets = muscles.map((m) => ({
      label: m,
      data: labels.map((l) => data.volume[m][l] || 0),
      borderColor: colorFor(m),
      backgroundColor: colorFor(m),
      tension: 0.2,
      pointRadius: 3,
      borderWidth: 2,
    }));
    new Chart(canvas, {
      type: "line",
      data: { labels, datasets },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: { position: "bottom", labels: { boxWidth: 12 } },
          tooltip: { mode: "index" },
        },
        scales: {
          y: { beginAtZero: true, ticks: { precision: 0 }, title: { display: true, text: "sets / week" } },
          x: { ticks: { maxRotation: 0, autoSkip: true } },
        },
      },
    });
  }

  // ---- strength trend ----------------------------------------------------

  function drawStrength(strength) {
    const canvas = document.getElementById("chart-strength");
    if (!canvas || !strength || strength.length === 0) return;
    const labels = strength.map((p) => p.date || `S${p.day_number}`);
    new Chart(canvas, {
      type: "line",
      data: {
        labels,
        datasets: [
          {
            label: "top set (lb)",
            data: strength.map((p) => p.top_weight),
            borderColor: "#4f9cff",
            backgroundColor: "#4f9cff",
            tension: 0.2,
            pointRadius: 4,
            yAxisID: "y",
          },
          {
            label: "est. 1RM (Epley)",
            data: strength.map((p) => p.e1rm),
            borderColor: "#ce93d8",
            backgroundColor: "#ce93d8",
            tension: 0.2,
            borderDash: [5, 5],
            pointRadius: 3,
            yAxisID: "y",
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: {
          legend: { position: "bottom" },
          tooltip: {
            callbacks: {
              afterBody: (items) => {
                const idx = items[0].dataIndex;
                const pt = strength[idx];
                return `${pt.top_reps} reps · S${pt.day_number}`;
              },
            },
          },
        },
        scales: {
          y: { title: { display: true, text: "weight (lb)" }, beginAtZero: false },
        },
      },
    });
  }

  // ---- bodyweight + waist ------------------------------------------------

  function drawBodyweight(rows) {
    const canvas = document.getElementById("chart-bodyweight");
    if (!canvas || !rows || rows.length === 0) return;
    const hasWaist = rows.some((r) => r.waist_in !== null);
    const labels = rows.map((r) => r.date);
    const datasets = [
      {
        label: "weight (lb)",
        data: rows.map((r) => r.weight_lb),
        borderColor: "#4f9cff",
        backgroundColor: "#4f9cff",
        tension: 0.2,
        pointRadius: 3,
        yAxisID: "y",
      },
    ];
    if (hasWaist) {
      datasets.push({
        label: "waist (in)",
        data: rows.map((r) => r.waist_in),
        borderColor: "#ffb74d",
        backgroundColor: "#ffb74d",
        tension: 0.2,
        borderDash: [5, 5],
        pointRadius: 3,
        yAxisID: "y2",
      });
    }
    new Chart(canvas, {
      type: "line",
      data: { labels, datasets },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: { legend: { position: "bottom" } },
        scales: {
          y: { title: { display: true, text: "weight (lb)" }, beginAtZero: false },
          y2: hasWaist ? {
            position: "right",
            title: { display: true, text: "waist (in)" },
            grid: { drawOnChartArea: false },
          } : undefined,
        },
      },
    });
  }

  // ---- pain frequency ----------------------------------------------------

  function drawPain(pain) {
    const canvas = document.getElementById("chart-pain");
    if (!canvas || !pain || Object.keys(pain).length === 0) return;
    const labels = Object.keys(pain);
    const data = labels.map((l) => pain[l]);
    new Chart(canvas, {
      type: "bar",
      data: {
        labels,
        datasets: [{
          label: "issues opened",
          data,
          backgroundColor: "#ef5350",
        }],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        plugins: { legend: { display: false } },
        scales: { y: { beginAtZero: true, ticks: { precision: 0 } } },
      },
    });
  }
})();
