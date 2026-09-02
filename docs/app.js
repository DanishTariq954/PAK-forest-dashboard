const PINE = "#2F5233";
const ALERT = "#B8551E";
const GOLD = "#B8922E";
const INK_SOFT = "#4C5A4F";

async function loadJSON(path) {
  const res = await fetch(path + "?t=" + Date.now()); // cache-bust so updates show immediately
  if (!res.ok) throw new Error("Failed to load " + path);
  return res.json();
}

function formatAcres(n) {
  return Math.round(n).toLocaleString("en-US");
}

async function init() {
  // ---------- Map ----------
  const map = L.map("map", { scrollWheelZoom: false }).setView([30.5, 70.5], 5.3);
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    attribution: '&copy; OpenStreetMap contributors',
    maxZoom: 18,
  }).addTo(map);

  let weeklyGeo = { type: "FeatureCollection", features: [] };
  try {
    weeklyGeo = await loadJSON("data/weekly_alerts.geojson");
  } catch (e) {
    console.warn(e);
  }

  const alertLayer = L.geoJSON(weeklyGeo, {
    style: { color: ALERT, weight: 1, fillColor: ALERT, fillOpacity: 0.55 },
  }).addTo(map);

  if (weeklyGeo.features.length > 0) {
    map.fitBounds(alertLayer.getBounds(), { padding: [20, 20] });
  }

  // ---------- KPIs + annual chart ----------
  let annual = { years: [] };
  try {
    annual = await loadJSON("data/annual_stats.json");
  } catch (e) {
    console.warn(e);
  }

  if (annual.years.length > 0) {
    const first = annual.years[0];
    const last = annual.years[annual.years.length - 1];
    document.getElementById("kpiBaseline").textContent = formatAcres(first.tree_cover_acres);
    document.getElementById("kpiCurrent").textContent = formatAcres(last.tree_cover_acres);
    document.getElementById("kpiTotalLoss").textContent = formatAcres(first.tree_cover_acres - last.tree_cover_acres);

    new Chart(document.getElementById("annualChart"), {
      type: "bar",
      data: {
        labels: annual.years.map(y => y.year),
        datasets: [
          {
            type: "line",
            label: "Standing forest (acres)",
            data: annual.years.map(y => y.tree_cover_acres),
            borderColor: PINE,
            backgroundColor: PINE,
            yAxisID: "y1",
            tension: 0.25,
            pointRadius: 0,
          },
          {
            type: "bar",
            label: "Loss that year (acres)",
            data: annual.years.map(y => y.loss_acres),
            backgroundColor: GOLD,
            yAxisID: "y",
          },
        ],
      },
      options: {
        responsive: true,
        interaction: { mode: "index", intersect: false },
        scales: {
          y: { position: "left", title: { display: true, text: "Annual loss (acres)" } },
          y1: { position: "right", title: { display: true, text: "Standing forest (acres)" }, grid: { drawOnChartArea: false } },
        },
        plugins: { legend: { position: "bottom", labels: { color: INK_SOFT } } },
      },
    });
  } else {
    document.getElementById("annualChart").parentElement.insertAdjacentHTML(
      "beforeend",
      '<p style="color:#4C5A4F;font-size:0.85rem;">No annual data yet — runs after the first Annual Hansen Stats Refresh workflow.</p>'
    );
  }

  // ---------- Weekly summary + chart ----------
  let weekly = { weeks: [], last_updated: null };
  try {
    weekly = await loadJSON("data/weekly_summary.json");
  } catch (e) {
    console.warn(e);
  }

  document.getElementById("lastUpdated").textContent = weekly.last_updated
    ? "Last updated " + weekly.last_updated
    : "Not yet run";

  if (weekly.weeks.length > 0) {
    document.getElementById("kpiWeekly").textContent = formatAcres(weekly.weeks[weekly.weeks.length - 1].alert_acres);

    new Chart(document.getElementById("weeklyChart"), {
      type: "line",
      data: {
        labels: weekly.weeks.map(w => w.week_of),
        datasets: [{
          label: "Flagged acres",
          data: weekly.weeks.map(w => w.alert_acres),
          borderColor: ALERT,
          backgroundColor: ALERT,
          tension: 0.2,
        }],
      },
      options: {
        responsive: true,
        plugins: { legend: { display: false } },
        scales: { y: { title: { display: true, text: "Acres flagged" } } },
      },
    });
  } else {
    document.getElementById("kpiWeekly").textContent = "0";
    document.getElementById("weeklyChart").parentElement.insertAdjacentHTML(
      "beforeend",
      '<p style="color:#4C5A4F;font-size:0.85rem;">No weekly data yet — runs after the first Weekly Deforestation Alert Scan.</p>'
    );
  }
}

init();
