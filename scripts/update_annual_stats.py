"""
Computes precise annual tree cover & loss statistics for Pakistan (2000-2025)
from the Hansen Global Forest Change dataset and writes them to
docs/data/annual_stats.json for web dashboard rendering.

KPI Dashboard Metrics Included:
1. Forest Area in 2000
2. Forest Area in 2025 (Remaining)
3. Total Forest Loss (2000-2025)
4. Highest Loss Province & Highest Loss District/City

FIX HISTORY:
  - Async export-to-asset + poll cycle instead of synchronous .getInfo()
    (avoids the ~5 minute synchronous request ceiling).
  - Hansen dataset pinned to 2025_v1_13, matching every other script in
    this project.
  - Hard-fail (SystemExit(1)) instead of silently writing zeroed-out
    stats when an export task doesn't complete -- a failed CI run is
    far better than quietly publishing wrong numbers.
  - Asset-root bug fixed: ee.data.getAssetRoots()[0]["id"] can return a
    specific child TABLE asset (e.g. ".../assets/protected_areas")
    rather than the bare project assets root, which broke every export.
    Now normalized to the true root, with a dedicated
    "pak_forest_watch_exports" folder created/reused for all temp
    assets.

  - THIS REVISION: the previous run's log showed both export tasks
    stuck at state=READY for the FULL 40-minute timeout window --
    never once transitioning to RUNNING. Combined with the
    "noncommercial compute quota... restricted mode" warning also
    present in that log, this means Earth Engine could not schedule a
    job this large (27-band composite x ~150 district polygons x 100m
    resolution) within the available quota at all, regardless of how
    long we wait.

    The only lever we control from the script side is the size of the
    computation itself, so:
      * SCALE increased from 100m to 500m. Pixel count (and therefore
        compute) scales with scale^2, so this is roughly a 25x
        reduction in total work -- while still being a standard,
        defensible resolution for province/district-level AREA TOTALS
        (as opposed to fine boundary shapes). Very small or narrow
        districts may see reduced precision; total acreage sums remain
        meaningful.
      * tileScale reduced from 16 to 4. tileScale trades more
        (smaller) tiles for lower per-tile memory use; at 100m it was
        needed to avoid out-of-memory errors on the huge composite, but
        at 500m the per-tile memory footprint is already ~25x smaller,
        so a lower tileScale is sufficient and avoids adding needless
        tile-management overhead on top of an already
        quota-constrained job.
      * EXPORT_TIMEOUT_SECONDS increased from 40 to 60 minutes as a
        safety margin -- the job should now be small enough to actually
        get scheduled and finish well inside that window, but a bit of
        headroom is cheap insurance.
      * The poll loop now explicitly calls out if a task is still
        showing READY (as opposed to RUNNING) once it's been waiting a
        while, so future log output makes this queuing-vs-executing
        distinction obvious without having to eyeball repeated lines.

    IMPORTANT CAVEAT: if your Earth Engine project is still deep in
    restricted mode (i.e. the noncommercial compute quota is
    essentially exhausted for the billing period), even this smaller
    job may still queue for a while or fail to schedule. That is a
    project-level quota issue Earth Engine controls, not something any
    script-side change can fully guarantee around. If this run still
    times out, the practical next steps are: (a) wait for the quota
    window to reset and re-run, or (b) request a compute quota increase
    / move the project to a paid tier, per the URL Earth Engine printed
    in the warning:
    https://developers.google.com/earth-engine/guides/noncommercial_tiers#restricted_mode

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
SCALE = 500                      # was 100 -- see fix note above
TILE_SCALE = 4                   # was 16 -- see fix note above
EXPORT_POLL_SECONDS = 15
EXPORT_TIMEOUT_SECONDS = 60 * 60  # was 40 * 60 -- extra safety margin
EXPORT_FOLDER_NAME = "pak_forest_watch_exports"


def get_project_assets_root():
    """
    Return the true bare assets root, e.g. 'projects/<project>/assets',
    regardless of what getAssetRoots() enumerates first (it can return a
    specific child TABLE asset instead of the bare root).
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
    stuck_in_ready_warned = False
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
            if state == "READY" and waited >= 300 and not stuck_in_ready_warned:
                print(
                    "  NOTE: task has been in READY (queued, not yet "
                    "executing) for 5+ minutes. This usually means Earth "
                    "Engine hasn't allocated compute capacity to it yet -- "
                    "often due to noncommercial compute quota / restricted "
                    "mode, not a problem with the job itself.",
                    flush=True,
                )
                stuck_in_ready_warned = True

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

    print(f"Running province-level spatial reduction at {SCALE}m (async export)...", flush=True)
    prov_asset_id = f"{export_folder}/pak_forest_watch_annual_prov_tmp"
    prov_reduced_fc = composite_metrics.reduceRegions(collection=gaul_l1, reducer=ee.Reducer.sum(), scale=SCALE, tileScale=TILE_SCALE)
    prov_success = run_export_and_wait(prov_reduced_fc, "pak_forest_watch_annual_prov", prov_asset_id)

    print(f"Running district-level spatial reduction at {SCALE}m (async export)...", flush=True)
    dist_asset_id = f"{export_folder}/pak_forest_watch_annual_dist_tmp"
    dist_reduced_fc = composite_metrics.reduceRegions(collection=gaul_l2, reducer=ee.Reducer.sum(), scale=SCALE, tileScale=TILE_SCALE)
    dist_success = run_export_and_wait(dist_reduced_fc, "pak_forest_watch_annual_dist", dist_asset_id)

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
