"""
Near-real-time (weekly) deforestation alerting for Pakistan using Sentinel-2.

Why not GLAD/RADD alerts: those near-real-time alert products (used by
Global Forest Watch) only cover humid tropical forest belts (Amazon, Congo
Basin, SE Asia) and do not include Pakistan. So this builds a lightweight
custom alert from Sentinel-2 NDVI instead.

Why an EXPORT TASK instead of a direct request: a country-wide area
computation over Sentinel-2 imagery is too heavy to finish inside Earth
Engine's ~5-minute synchronous request limit, no matter how the scale or
tileScale parameters are tuned -- that ceiling is a hard platform limit.
Submitting the work as a background export task removes that ceiling
entirely: the task runs for as long as it needs, and this script just
polls for completion, then reads the (already computed, so now cheap)
result.

This is a heuristic screening tool, not an official/validated alert
product -- flagged areas should be treated as "worth a closer look", not
confirmed clearances. Cloud cover, seasonal leaf-off, and agriculture
harvest cycles can all trigger false positives.

Run: python scripts/update_weekly_alerts.py
"""

import datetime
import json
import os
import time
import ee
from common import init_earth_engine, get_study_area

M2_TO_ACRES = 0.000247105
BASELINE_WEEKS = 8
NDVI_HEALTHY_THRESHOLD = 0.5
NDVI_DROP_THRESHOLD = 0.20
MAX_IMAGES_PER_COMPOSITE = 80
EXPORT_POLL_SECONDS = 15
EXPORT_TIMEOUT_SECONDS = 25 * 60


def mask_s2_clouds(image):
    qa = image.select("QA60")
    cloud_bit_mask = 1 << 10
    cirrus_bit_mask = 1 << 11
    mask = qa.bitwiseAnd(cloud_bit_mask).eq(0).And(qa.bitwiseAnd(cirrus_bit_mask).eq(0))
    return image.updateMask(mask).divide(10000).copyProperties(image, ["system:time_start"])


def ndvi_composite(study_area, start, end, max_images=MAX_IMAGES_PER_COMPOSITE):
    coll = (
        ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
        .filterBounds(study_area)
        .filterDate(start, end)
        .filter(ee.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", 60))
        .select(["B4", "B8", "QA60"])
        .sort("CLOUDY_PIXEL_PERCENTAGE")
        .limit(max_images)
        .map(mask_s2_clouds)
    )
    composite = coll.median()
    ndvi = composite.normalizedDifference(["B8", "B4"]).rename("NDVI")
    return ndvi, coll.size()


def run_export_and_wait(collection, description, asset_id):
    try:
        ee.data.deleteAsset(asset_id)
        print(f"  Deleted stale asset from a previous run: {asset_id}", flush=True)
    except Exception:
        pass

    task = ee.batch.Export.table.toAsset(
        collection=collection, description=description, assetId=asset_id
    )
    task.start()
    print(f"Started background export task '{description}' -> {asset_id}", flush=True)

    waited = 0
    while waited < EXPORT_TIMEOUT_SECONDS:
        status = task.status()
        state = status.get("state")
        print(f"  [{waited}s] export task state: {state}", flush=True)
        if state == "COMPLETED":
            return True
        if state in ("FAILED", "CANCELLED"):
            print("  Task error details:", status.get("error_message"), flush=True)
            return False
        time.sleep(EXPORT_POLL_SECONDS)
        waited += EXPORT_POLL_SECONDS

    print("  Export did not finish within the timeout window.", flush=True)
    return False


def main():
    init_earth_engine()
    study_area = get_study_area()

    hansen = ee.Image("UMD/hansen/global_forest_change_2025_v1_13")
    tree_cover_2000 = hansen.select("treecover2000").gt(0).selfMask().clip(study_area)
    loss = hansen.select("loss").clip(study_area)
    standing_forest_mask = tree_cover_2000.updateMask(loss.Not())

    today = datetime.date.today()
    current_start = today - datetime.timedelta(days=7)
    current_end = today
    baseline_start = current_start - datetime.timedelta(weeks=BASELINE_WEEKS)
    baseline_end = current_start

    print(f"Baseline window: {baseline_start} to {baseline_end}", flush=True)
    print(f"Current window:  {current_start} to {current_end}", flush=True)

    ndvi_baseline, n_baseline = ndvi_composite(study_area, str(baseline_start), str(baseline_end))
    ndvi_current, n_current = ndvi_composite(study_area, str(current_start), str(current_end))

    print("Baseline images used:", n_baseline.getInfo(), flush=True)
    print("Current images used:", n_current.getInfo(), flush=True)

    ndvi_drop = ndvi_baseline.subtract(ndvi_current)

    alert_mask = (
        standing_forest_mask
        .And(ndvi_baseline.gte(NDVI_HEALTHY_THRESHOLD))
        .And(ndvi_drop.gte(NDVI_DROP_THRESHOLD))
    )

    vectors = alert_mask.selfMask().reduceToVectors(
        geometry=study_area,
        scale=200,
        geometryType="polygon",
        eightConnected=True,
        maxPixels=1e13,
        bestEffort=True,
        tileScale=4,
    ).map(lambda f: f.set("area_acres", f.geometry().area(maxError=30).multiply(M2_TO_ACRES)))

    asset_root = ee.data.getAssetRoots()[0]["id"]
    asset_id = f"{asset_root}/pak_forest_watch_weekly_tmp"

    success = run_export_and_wait(vectors, "pak_forest_watch_weekly_export", asset_id)

    geojson = {"type": "FeatureCollection", "features": []}
    total_alert_acres = 0.0

    if success:
        print("Export completed -- reading results (cheap now, already computed)...", flush=True)
        fc_info = ee.FeatureCollection(asset_id).getInfo()
        for feat in fc_info.get("features", []):
            props = feat.get("properties", {})
            geojson["features"].append({
                "type": "Feature",
                "geometry": feat["geometry"],
                "properties": {"week_of": str(current_start)},
            })
            total_alert_acres += props.get("area_acres", 0) or 0
        total_alert_acres = round(total_alert_acres, 2)

        try:
            ee.data.deleteAsset(asset_id)
            print("Cleaned up temporary asset.", flush=True)
        except Exception as e:
            print("Could not delete temp asset (non-fatal):", e, flush=True)
    else:
        print("Export did not complete successfully -- writing empty results for this week.", flush=True)

    print(f"Flagged alert area this week: {total_alert_acres} acres", flush=True)

    data_dir = os.path.join(os.path.dirname(__file__), "..", "docs", "data")
    os.makedirs(data_dir, exist_ok=True)

    with open(os.path.join(data_dir, "weekly_alerts.geojson"), "w") as f:
        json.dump(geojson, f)
    print(f"Wrote {len(geojson['features'])} alert polygons to weekly_alerts.geojson", flush=True)

    summary_path = os.path.join(data_dir, "weekly_summary.json")
    if os.path.exists(summary_path):
        with open(summary_path) as f:
            summary = json.load(f)
    else:
        summary = {"unit": "acres", "method": "Sentinel-2 NDVI drop screening (not an official alert product)", "weeks": []}

    summary["weeks"] = [w for w in summary["weeks"] if w["week_of"] != str(current_start)]
    summary["weeks"].append({
        "week_of": str(current_start),
        "alert_acres": total_alert_acres,
        "baseline_images": n_baseline.getInfo(),
        "current_images": n_current.getInfo(),
    })
    summary["weeks"].sort(key=lambda w: w["week_of"])
    summary["last_updated"] = str(today)

    with open(summary_path, "w") as f:
        json.dump(summary, f, indent=2)
    print(f"Updated {summary_path}", flush=True)


if __name__ == "__main__":
    main()
