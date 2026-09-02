# Pakistan Forest Watch

A live-updating dashboard tracking tree cover, annual loss, and weekly
deforestation screening for Pakistan (including Azad Kashmir and
Gilgit-Baltistan), built on Google Earth Engine data and hosted free on
GitHub Pages.

- **Annual view (2000–present):** tree cover and loss acreage, calculated
  independently for each year from the Hansen Global Forest Change dataset.
- **Weekly view:** a Sentinel-2 NDVI change screen that flags places where
  healthy forest showed a sharp vegetation drop in the last 7 days. This is
  a heuristic screening layer, not an official alert product — see the
  "Why not GLAD/RADD" note below.

Live dashboard: `https://<your-username>.github.io/<your-repo>/`

---

## 1. One-time setup

### 1.1 Create a Google Earth Engine service account

GitHub Actions can't do the interactive browser login `ee.Authenticate()`
normally uses, so it needs a service account key instead.

1. Go to the [Google Cloud Console](https://console.cloud.google.com/) and
   select (or create) the GCP project you already registered for Earth
   Engine (yours is `trans-equator-468607-p0` based on your notebook).
2. **IAM & Admin → Service Accounts → Create Service Account.**
   Give it a name like `forest-watch-ci`.
3. Grant it the **Earth Engine Resource Viewer** role (and **Service
   Usage Consumer** if prompted).
4. Open the new service account → **Keys → Add Key → Create new key → JSON**.
   This downloads a `.json` file — keep it private, never commit it.
5. Register the service account for Earth Engine access: go to
   [signup.earthengine.google.com/#!/service_accounts](https://signup.earthengine.google.com/#!/service_accounts)
   and register the service account's email address.

### 1.2 Add the key as a GitHub secret

1. In your GitHub repo: **Settings → Secrets and variables → Actions → New
   repository secret.**
2. Name: `EE_SERVICE_ACCOUNT_KEY`
3. Value: paste the **entire contents** of the JSON key file you downloaded.
4. Save.

### 1.3 Enable GitHub Pages

1. **Settings → Pages.**
2. Under "Build and deployment", set **Source: Deploy from a branch**.
3. Branch: `main` (or whichever is your default), folder: **`/docs`**.
4. Save. Your dashboard will be live at
   `https://<your-username>.github.io/<your-repo>/` within a minute or two.

### 1.4 Push this repo to GitHub

```bash
git init
git add .
git commit -m "Initial Pakistan Forest Watch dashboard"
git branch -M main
git remote add origin https://github.com/<your-username>/<your-repo>.git
git push -u origin main
```

---

## 2. Running it

Once the secret and Pages are set up, everything is automatic:

| Workflow | Schedule | What it does |
|---|---|---|
| `Weekly Deforestation Alert Scan` | Every Monday, 06:00 UTC | Runs `scripts/update_weekly_alerts.py`, commits fresh `docs/data/weekly_alerts.geojson` and `weekly_summary.json` |
| `Annual Hansen Stats Refresh` | Feb 1st yearly | Runs `scripts/update_annual_stats.py`, commits fresh `docs/data/annual_stats.json` |

Both can also be triggered manually any time from the repo's **Actions**
tab → select the workflow → **Run workflow**. Do this once right after
setup so the dashboard has real data instead of the placeholder empty
files it ships with.

---

## 3. Running it locally / in Colab first (recommended before your first push)

```bash
pip install earthengine-api
```

```python
import os
os.environ["EE_PROJECT_ID"] = "trans-equator-468607-p0"  # only used for local interactive auth

import sys
sys.path.insert(0, "scripts")
import update_annual_stats
update_annual_stats.main()

import update_weekly_alerts
update_weekly_alerts.main()
```

This will prompt an interactive Earth Engine login (fine for local
testing) and write into `docs/data/`. Open `docs/index.html` with a local
server (e.g. `python -m http.server` from the repo root, then visit
`http://localhost:8000/docs/`) to preview before pushing.

---

## 4. Why not GLAD or RADD near-real-time alerts?

Global Forest Watch's daily/near-real-time alert layers (GLAD-L, GLAD-S2,
RADD) only cover humid tropical forest biomes — the Amazon, the Congo
Basin, and Southeast Asia. Pakistan's forests fall outside that coverage,
so those datasets return nothing useful here. This project instead builds
a custom weekly NDVI-drop screen directly from Sentinel-2 imagery
(`scripts/update_weekly_alerts.py`), restricted to standing forest per the
Hansen 2000 baseline. It's a reasonable proxy, but it hasn't been
validated against ground truth — treat flagged areas as leads, not
confirmed clearances. Cloud cover, seasonal leaf changes, and agricultural
harvest cycles near forest edges can all trigger false positives.

---

## 5. Repo structure

```
.github/workflows/
  weekly-alerts.yml       # scheduled: Sentinel-2 weekly screen
  annual-stats.yml        # scheduled: Hansen annual stats
scripts/
  common.py               # shared EE auth + study-area geometry
  update_annual_stats.py  # per-year loss/cover, independently calculated
  update_weekly_alerts.py # weekly NDVI change screen
docs/                     # GitHub Pages site root
  index.html
  style.css
  app.js
  data/
    annual_stats.json
    weekly_alerts.geojson
    weekly_summary.json
requirements.txt
```

## 6. Tuning the weekly screen

The thresholds in `scripts/update_weekly_alerts.py` are a reasonable
starting point, not tuned to ground truth:

- `NDVI_HEALTHY_THRESHOLD = 0.5` — how "forested" a pixel must look in the
  baseline period to be eligible for an alert.
- `NDVI_DROP_THRESHOLD = 0.20` — how much NDVI must fall to flag a pixel.
- `BASELINE_WEEKS = 8` — length of the rolling comparison window.

If you're seeing too much noise (agriculture, seasonal effects) or too few
real alerts, adjust these and re-run.
