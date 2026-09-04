"""
Near-real-time (weekly) deforestation alerting for Pakistan using Sentinel-2.

This production-grade script detects localized canopy disturbances by monitoring 
weekly drops in Sentinel-2 NDVI across established forest baselines. 

Key Enhancements:
- Cloud Shadow & Cirrus masking via SCL (Scene Classification Layer) to eliminate false positives.
- Full-region spatial compositing (removed artificial collection truncation limits).
- Vectorization scale optimized to 30m for high-precision boundary extraction.
- Asynchronous Earth Engine batch task export to handle nationwide processing limits.

Run: python scripts/update_weekly_alerts.py
"""

import datetime
import json
import os
import time
import ee
from common import init_earth_engine, get_study_area

# ------------------------------------------------------------------------------
# CONSTANTS & CONFIGURATION
# ------------------------------------------------------------------------------
M2_TO_ACRES = 0.000247105
BASELINE_WEEKS = 8
CANOPY_COVER_THRESHOLD = 15      # Minimum % tree canopy cover in 2000 baseline
NDVI_HEALTHY_THRESHOLD = 0.40    # Adjusted for dry/coniferous mountain forests
NDVI_DROP_THRESHOLD = 0.20       # Significant drop threshold indicating disturbance
EXPORT_SCALE_METERS = 30         # High-resolution vectorization (30m spatial detail)
EXPORT_POLL_SECONDS = 15
EXPORT_TIMEOUT_SECONDS = 30 * 60 # 30-minute processing limit for batch export


def mask_s2_clouds_and_shadows(image):
    """
    Masks clouds, cloud shadows, and cirrus using Sentinel-2 SR Scene Classification (SCL).
    SCL Pixel Values:
      3: Cloud shadows
      8: Cloud medium probability
      9: Cloud high probability
     10: Thin cirrus
    """
    scl = image.select("SCL")
    
    # Keep clear pixels (exclude shadows, medium/high probability clouds, and cirrus)
    clear_mask = (
        scl.neq(3)
        .And(scl.neq(8))
        .And(scl.neq(9))
        .And(scl.neq(10))
    )
    
    return (
        image.updateMask(clear_mask)
        .select(["B4", "B8"])
        .divide(10000.0)
        .copyProperties(image, ["system:time_start"])
    )


def ndvi_composite(study_area, start_date, end_date):
    """
    Generates a cloud-free median NDVI composite over the study area without spatial limits.
    """
    coll = (
        ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
        .filterBounds(study_area)
        .filterDate(start_date, end_date)
        .filter(ee.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", 70))
        .map(mask_s2_clouds_and_shadows)
    )
    
    composite = coll.median().clip(study_area)
    ndvi = composite.normalizedDifference(["B8", "B4"]).rename("NDVI")
    
    return ndvi, coll.size()


def run_export_and_wait(collection, description, asset_id):
    """
    Submits an asynchronous Earth Engine export task and polls for status.
    """
    try:
        ee.data.deleteAsset(asset_id)
        print(f"  Deleted stale temporary asset: {asset_id}", flush=True)
    except Exception:
        pass

    task = ee.batch.Export.table.toAsset(
        collection=collection, 
        description=description, 
        assetId=asset_id
    )
    task.start()
    print(f"Started server-side export task '{description}' -> {asset_id}", flush=True)

    waited = 0
    while waited < EXPORT_TIMEOUT_SECONDS:
        status = task.status()
        state = status.get("state")
        print(f"  [{waited}s] Task State: {state}", flush=True)
        
        if state == "COMPLETED":
            return True
        if state in ("FAILED", "CANCELLED"):
            print("  Task Execution Error:", status.get("error_message"), flush=True)
            return False
            
        time.sleep(EXPORT_POLL_SECONDS)
        waited += EXPORT_POLL_SECONDS

    print("  Export operation timed out.", flush=True)
    return False


def main():
    init_earth_engine()
    study_area = get_study_area()

    # Ingest Hansen Global Forest Change to isolate existing standing forest
    hansen = ee.Image("UMD/hansen/global_forest_change_2024_v1_12")
    tree_cover_2000 = hansen.select("treecover2000").gte(CANOPY_COVER_THRESHOLD)
    historical_loss = hansen.select("loss")
    
    # Mask: Must be baseline forest and NOT previously deforested
    standing_forest_mask = tree_cover_2000.And(historical_loss.Not()).selfMask().clip(study_area)

    # Date temporal window setup
    today = datetime.date.today()
    current_start = today - datetime.timedelta(days=7)
    current_end = today
    baseline_start = current_start - datetime.timedelta(weeks=BASELINE_WEEKS)
    baseline_end = current_start

    print(f"Baseline Window: {baseline_start} to {baseline_end}", flush=True)
    print(f"Current Window:  {current_start} to {current_end}", flush=True)

    # Compute NDVI composites
    ndvi_baseline, n_baseline = ndvi_composite(study_area, str(baseline_start), str(baseline_end))
    ndvi_current, n_current = ndvi_composite(study_area, str(current_start), str(current_end))

    print("Baseline Images Evaluated:", n_baseline.getInfo(), flush=True)
    print("Current Images Evaluated:", n_current.getInfo(), flush=True)

    # Calculate NDVI reduction
    ndvi_drop = ndvi_baseline.subtract(ndvi_current)

    # Flag alerts within valid forest canopy only
    alert_mask = (
        standing_forest_mask
        .And(ndvi_baseline.gte(NDVI_HEALTHY_THRESHOLD))
        .And(ndvi_drop.gte(NDVI_DROP_THRESHOLD))
    )

    # Vectorize alert masks at high resolution (30m)
    vectors = alert_mask.selfMask().reduceToVectors(
        geometry=study_area,
        scale=EXPORT_SCALE_METERS,
        geometryType="polygon",
        eightConnected=True,
        maxPixels=1e13,
        bestEffort=True,
        tileScale=8
    ).map(lambda f: f.set("area_acres", f.geometry().area(maxError=10).multiply(M2_TO_ACRES)))

    # Set temporary Earth Engine asset location for task output
    asset_root = ee.data.getAssetRoots()[0]["id"]
    asset_id = f"{asset_root}/pak_forest_watch_weekly_tmp"

    success = run_export_and_wait(vectors, "pak_forest_watch_weekly_export", asset_id)

    geojson = {"type": "FeatureCollection", "features": []}
    total_alert_acres = 0.0

    if success:
        print("Export complete. Reading computed vector features...", flush=True)
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
            print("Temporary asset cleaned up successfully.", flush=True)
        except Exception as e:
            print("Non-fatal error during asset deletion:", e, flush=True)
    else:
        print("Export failed. Outputting empty FeatureCollection for this cycle.", flush=True)

    print(f"Total Detected Deforestation Area: {total_alert_acres} acres", flush=True)

    # File Output Management
    data_dir = os.path.join(os.path.dirname(__file__), "..", "docs", "data")
    os.makedirs(data_dir, exist_ok=True)

    # Save GeoJSON
    with open(os.path.join(data_dir, "weekly_alerts.geojson"), "w") as f:
        json.dump(geojson, f)
    print(f"Exported {len(geojson['features'])} alert features to weekly_alerts.geojson", flush=True)

    # Update Summary JSON
    summary_path = os.path.join(data_dir, "weekly_summary.json")
    if os.path.exists(summary_path):
        with open(summary_path) as f:
            summary = json.load(f)
    else:
        summary = {
            "unit": "acres",
            "method": "Sentinel-2 Cloud/Shadow Masked NDVI Drop Screening",
            "weeks": []
        }

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
    print(f"Summary updated at {summary_path}", flush=True)


if __name__ == "__main__":
    main()
