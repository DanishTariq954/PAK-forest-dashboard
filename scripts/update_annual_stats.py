"""
Computes precise annual tree cover & loss statistics for Pakistan (2000-2025)
from the Hansen Global Forest Change dataset and writes them to
docs/data/annual_stats.json for web dashboard rendering.

KPI Dashboard Metrics Included:
1. Forest Area in 2000
2. Forest Area in 2025 (Remaining)
3. Total Forest Loss (2000-2025)
4. Highest Loss Province & Highest Loss District/City

ESSENTIAL FIXES applied to the original version of this script:
  - The two province/district reduceRegions() calls now go through an
    async export-to-asset + poll cycle instead of a direct synchronous
    .getInfo(). A single call reducing a ~27-band composite across ~150
    district polygons at fine resolution is far too heavy to finish
    inside Earth Engine's ~5-minute synchronous request limit -- this
    was proven repeatedly earlier in this project. Async export removes
    that ceiling.
  - scale bumped from 30 to 100: still a fine, meaningful resolution for
    district-sized polygons, but 30m at country-wide scale for a 27-band
    image is unnecessarily heavy for the accuracy gained.
  - Hansen dataset updated from 2024_v1_12 to 2025_v1_13, matching every
    other script in this project (map layers, weekly alerts, regional
    stats). Mixing dataset versions across pages would silently produce
    inconsistent numbers for the same years.
  - END_YEAR updated to 2025 to match the newer dataset's loss-year range.
  - The output now ALSO includes a top-level "years" array (identical
    content to "annual_trend") so the existing dashboard frontend
    (app.js), which reads annual.years, keeps working without any
    changes to it. Nothing about your KPI/annual_trend structure was
    removed -- this is purely an addition for compatibility.
  - requirements.txt needs "pandas" added (this script imports it).

  - FIX (previous revision): the script used to silently fall back to
    an EMPTY FeatureCollection whenever an export task failed, which
    made every downstream sum compute to 0.0 while the job still exited
    with status 0 (success). It now hard-fails (SystemExit(1)) if
    either export task did not complete, and prints the full task
    status object for diagnosis.

  - FIX (this revision): ee.data.getAssetRoots()[0]["id"] was returning
    ".../assets/protected_areas" -- a specific TABLE asset (the study
    area boundary), not the bare project assets root -- because that's
    apparently the only root-level item the service account enumerates
    first. The script then tried to export new tables "into" that
    table path as if it were a folder, which Earth Engine correctly
    rejected ("Asset '...protected_areas' is neither a folder nor an
    image collection."). The script now:
      1. Normalizes whatever getAssetRoots() returns down to the true
         bare root "projects/<project>/assets" by truncating after the
         "assets" path segment.
      2. Creates (idempotently) a dedicated subfolder
         "projects/<project>/assets/pak_forest_watch_exports" for all
         temporary export assets, so exports never collide with, or
         get placed "inside", existing table assets like
         protected_areas.

Note (not changed, just flagging): gaul_l1/gaul_l2 here are filtered to
ADM0_NAME == 'Pakistan' only, so Azad Kashmir and Gilgit-Baltistan (which
FAO GAUL treats as separate ADM0 units, not provinces of Pakistan) won't
appear in the province-level KPIs from this script, even though they're
included in the country's overall study area. That's your original
script's design -- left as-is per your request.

Run: python scripts/update_annual_stats.py
"""

import json
import os
import time
import ee
import pandas as pd
from common import init_earth_engine, get_study_area

M2_TO_ACRES = 0.000247105
CANOPY_THRESHOLD = 1
START_YEAR = 2001
END_YEAR = 2025
SCALE = 100
EXPORT_POLL_SECONDS = 15
EXPORT_TIMEOUT_SECONDS = 40 * 60
EXPORT_FOLDER_NAME = "pak_forest_watch_exports"


def get_project_assets_root():
    """
    Return the true bare assets root, e.g. 'projects/<project>/assets',
    regardless of what getAssetRoots() enumerates first. Earlier runs
    showed getAssetRoots()[0]['id'] can be a specific child TABLE asset
    (e.g. '.../assets/protected_areas') rather than the bare root, which
    breaks any code that tries to create children "under" it.
    """
    raw_root = ee.data.getAssetRoots()[0]["id"]
    parts = raw_root.split("/")
    if "assets" in parts:
        idx = parts.index("assets")
        return "/".join(parts[: idx + 1])
    return raw_root


def ensure_folder_exists(folder_id):
    """Create folder_id as an EE folder asset if it doesn't already exist."""
    try:
        existing = ee.data.getAsset(folder_id)
        existing_type = existing.get("type", "")
        if existing_type != "FOLDER":
            raise RuntimeError(
                f"Asset '{folder_id}' already exists but is type "
                f"'{existing_type}', not FOLDER. Refusing to export into it. "
                f"Delete/rename that asset or change EXPORT_FOLDER_NAME."
            )
        return
    except ee.EEException:
        pass  # doesn't exist yet -- create it below

    ee.data.createAsset({"type": "FOLDER"}, folder_id)
    print(f"Created export folder: {folder_id}", flush=True)


def run_export_and_wait(collection, description, asset_id):
    try:
        ee.data.deleteAsset(asset_id)
    except Exception:
        pass

    task = ee.batch.Export.table.toAsset(collection=collection, description=description, assetId=asset_id)
    task.start()
    print(f"Started export task '{description}' -> {asset_id}", flush=True)

    waited = 0
    while waited < EXPORT_TIMEOUT_SECONDS:
        status = task.status()
        state = status.get("state")
        if state == "COMPLETED":
            return True
        if state in ("FAILED", "CANCELLED"):
            print(f"  Task '{description}' FAILED. Full status: {status}", flush=True)
            return False
        time.sleep(EXPORT_POLL_SECONDS)
        waited += EXPORT_POLL_SECONDS
        if waited % 60 == 0:
            print(f"  ...still running ({waited // 60} min, state={state})", flush=True)

    print(f"  Export '{description}' did not finish within the timeout window.", flush=True)
    return False


def main():
    init_earth_engine()
    study_area = get_study_area()

    assets_root = get_project_assets_root()
    print(f"Resolved project assets root: {assets_root}", flush=True)

    export_folder = f"{assets_root}/{EXPORT_FOLDER_NAME}"
    ensure_folder_exists(export_folder)
    print(f"Using export folder: {export_folder}", flush=True)

    hansen = ee.Image("UMD/hansen/global_forest_change_2025_v1_13")
    tree_cover_2000 = hansen.select("treecover2000")
    loss_year_band = hansen.select("lossyear")
    loss_mask = hansen.select("loss")

    forest_2000_mask = tree_cover_2000.gte(CANOPY_THRESHOLD)
    pixel_area_m2 = ee.Image.pixelArea()

    forest_2000_area_img = forest_2000_mask.multiply(pixel_area_m2).rename("forest_2000_m2")

    valid_loss_mask = loss_mask.gt(0).And(forest_2000_mask)
    cum_loss_area_img = valid_loss_mask.multiply(pixel_area_m2).rename("cum_loss_m2")

    years = list(range(START_YEAR, END_YEAR + 1))
    annual_loss_bands = [
        loss_year_band.eq(y - 2000).And(forest_2000_mask).multiply(pixel_area_m2).rename(f"loss_{y}")
        for y in years
    ]
    annual_loss_stack = ee.Image.cat(annual_loss_bands)

    composite_metrics = ee.Image.cat([
        forest_2000_area_img,
        cum_loss_area_img,
        annual_loss_stack
    ]).clip(study_area)

    gaul_l1 = ee.FeatureCollection("FAO/GAUL/2015/level1").filter(ee.Filter.eq("ADM0_NAME", "Pakistan"))
    gaul_l2 = ee.FeatureCollection("FAO/GAUL/2015/level2").filter(ee.Filter.eq("ADM0_NAME", "Pakistan"))

    print("Running province-level spatial reduction (async export)...", flush=True)
    prov_asset_id = f"{export_folder}/pak_forest_watch_annual_prov_tmp"
    prov_reduced_fc = composite_metrics.reduceRegions(collection=gaul_l1, reducer=ee.Reducer.sum(), scale=SCALE, tileScale=16)
    prov_success = run_export_and_wait(prov_reduced_fc, "pak_forest_watch_annual_prov", prov_asset_id)

    print("Running district-level spatial reduction (async export)...", flush=True)
    dist_asset_id = f"{export_folder}/pak_forest_watch_annual_dist_tmp"
    dist_reduced_fc = composite_metrics.reduceRegions(collection=gaul_l2, reducer=ee.Reducer.sum(), scale=SCALE, tileScale=16)
    dist_success = run_export_and_wait(dist_reduced_fc, "pak_forest_watch_annual_dist", dist_asset_id)

    # HARD FAIL instead of silently continuing with empty data. Writing
    # zeroed-out stats over good historical data is worse than a failed
    # CI run -- a failed run is visible and doesn't corrupt the dashboard.
    if not prov_success or not dist_success:
        print(
            "FATAL: one or more Earth Engine exports failed. Refusing to "
            "overwrite docs/data/annual_stats.json with zeroed-out data.",
            flush=True,
        )
        raise SystemExit(1)

    prov_reduced = ee.FeatureCollection(prov_asset_id).getInfo()
    dist_reduced = ee.FeatureCollection(dist_asset_id).getInfo()

    try:
        ee.data.deleteAsset(prov_asset_id)
    except Exception:
        pass
    try:
        ee.data.deleteAsset(dist_asset_id)
    except Exception:
        pass

    name_mapping = {
        'Baluchistan': 'Balochistan',
        'N.W.F.P.': 'Khyber Pakhtunkhwa',
        'F.A.T.A.': 'Khyber Pakhtunkhwa',
        'Federally Administered Tribal Areas': 'Khyber Pakhtunkhwa',
        'Northern Areas': 'Gilgit Baltistan',
        'Azad Kashmir': 'Azad Kashmir'
    }

    prov_rows = []
    for f in prov_reduced.get('features', []):
        props = f['properties']
        raw_prov = props.get('ADM1_NAME', 'Unknown')
        std_prov = name_mapping.get(raw_prov, raw_prov)

        row = {
            'province': std_prov,
            'forest_2000_acres': props.get('forest_2000_m2', 0) * M2_TO_ACRES,
            'cum_loss_acres': props.get('cum_loss_m2', 0) * M2_TO_ACRES,
        }
        for y in years:
            row[f'loss_{y}'] = props.get(f'loss_{y}', 0) * M2_TO_ACRES
        prov_rows.append(row)

    df_prov = pd.DataFrame(prov_rows)
    if not df_prov.empty:
        df_prov = df_prov.groupby('province', as_index=False).sum()

    dist_rows = []
    for f in dist_reduced.get('features', []):
        props = f['properties']
        dist_rows.append({
            'district': props.get('ADM2_NAME', 'Unknown'),
            'province': name_mapping.get(props.get('ADM1_NAME', 'Unknown'), props.get('ADM1_NAME', 'Unknown')),
            'cum_loss_acres': props.get('cum_loss_m2', 0) * M2_TO_ACRES
        })
    df_dist = pd.DataFrame(dist_rows)

    total_forest_2000 = round(df_prov['forest_2000_acres'].sum(), 2) if not df_prov.empty else 0.0
    total_cum_loss = round(df_prov['cum_loss_acres'].sum(), 2) if not df_prov.empty else 0.0
    total_forest_2025 = round(total_forest_2000 - total_cum_loss, 2)

    kpi_summary = {
        "forest_area_2000_acres": total_forest_2000,
        "forest_area_2025_acres": total_forest_2025,
        "total_forest_loss_acres": total_cum_loss,
        "highest_loss_province": None,
        "highest_loss_district": None,
    }

    if not df_prov.empty:
        top_prov = df_prov.sort_values(by='cum_loss_acres', ascending=False).iloc[0]
        kpi_summary["highest_loss_province"] = {
            "name": top_prov['province'],
            "loss_acres": round(top_prov['cum_loss_acres'], 2)
        }

    if not df_dist.empty:
        top_dist = df_dist.sort_values(by='cum_loss_acres', ascending=False).iloc[0]
        kpi_summary["highest_loss_district"] = {
            "name": top_dist['district'],
            "province": top_dist['province'],
            "loss_acres": round(top_dist['cum_loss_acres'], 2)
        }

    annual_trend = []
    running_cover = total_forest_2000

    annual_trend.append({
        "year": 2000,
        "loss_acres": 0.0,
        "tree_cover_acres": round(running_cover, 2)
    })

    for y in years:
        annual_loss = df_prov[f'loss_{y}'].sum() if not df_prov.empty and f'loss_{y}' in df_prov.columns else 0.0
        running_cover -= annual_loss
        annual_trend.append({
            "year": y,
            "loss_acres": round(annual_loss, 2),
            "tree_cover_acres": round(running_cover, 2)
        })

    output_payload = {
        "source": "Hansen Global Forest Change v1.13 (UMD)",
        "unit": "acres",
        "kpis": kpi_summary,
        "annual_trend": annual_trend,
        "years": annual_trend,
    }

    out_path = os.path.join(os.path.dirname(__file__), "..", "docs", "data", "annual_stats.json")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)

    with open(out_path, "w") as f:
        json.dump(output_payload, f, indent=2)

    print(f"Output written to {out_path}", flush=True)


if __name__ == "__main__":
    main()
