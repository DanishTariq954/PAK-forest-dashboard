"""
Near-real-time (weekly) deforestation alerting for Pakistan using Sentinel-2.

Why not GLAD/RADD alerts: those near-real-time alert products (used by
Global Forest Watch) only cover humid tropical forest belts (Amazon, Congo
Basin, SE Asia) and do not include Pakistan. So this builds a lightweight
custom alert from Sentinel-2 NDVI instead:

  1. Restrict to the "still standing" forest mask (Hansen treecover2000,
     minus everything lost through the end of the historical record).
  2. Build a cloud-masked NDVI baseline composite from the trailing 8 weeks.
  3. Build a cloud-masked NDVI composite for the most recent 7-day window.
  4. Flag pixels that were healthy forest in the baseline (NDVI above a
     vegetation threshold) and dropped sharply in the current window.
  5. Vectorize the flagged pixels, compute acreage, and export a GeoJSON
     for the map plus a running weekly summary time series.

This is a heuristic screening tool, not an official/validated alert product
— flagged areas should be treated as "worth a closer look", not confirmed
clearances. Cloud cover, seasonal leaf-off, and agriculture harvest cycles
can all trigger false positives.

Run: python scripts/update_weekly_alerts.py
"""

import datetime
import json
import os
import ee
from common import init_earth_engine, get_study_area

M2_TO_ACRES = 0.000247105
BASELINE_WEEKS = 8
NDVI_HEALTHY_THRESHOLD = 0.5   # baseline must look like real vegetation
NDVI_DROP_THRESHOLD = 0.20     # absolute NDVI drop to count as "lost"
MIN_ALERT_PATCH_M2 = 900       # ignore single-pixel noise (~1 Sentinel-2 pixel)


def mask_s2_clouds(image):
    """Cloud-mask Sentinel-2 SR using the built-in QA60 band (simple, fast,
    good enough for a weekly screening product)."""
    qa = image.select("QA60")
    cloud_bit_mask = 1 << 10
    cirrus_bit_mask = 1 << 11
    mask = qa.bitwiseAnd(cloud_bit_mask).eq(0).And(qa.bitwiseAnd(cirrus_bit_mask).eq(0))
    return image.updateMask(mask).divide(10000).copyProperties(image, ["system:time_start"])


def ndvi_composite(study_area, start, end):
    coll = (
        ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
        .filterBounds(study_area)
        .filterDate(start, end)
        .filter(ee.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", 60))
        .map(mask_s2_clouds)
    )
    composite = coll.median()
    ndvi = composite.normalizedDifference(["B8", "B4"]).rename("NDVI")
    return ndvi, coll.size()


def main():
    init_earth_engine()
    study_area = get_study_area()

    hansen = ee.Image("UMD/hansen/global_forest_change_2025_v1_13")
    tree_cover_2000 = hansen.select("treecover2000").gt(0).selfMask().clip(study_area)
    loss = hansen.select("loss").clip(study_area)
    standing_forest_mask = tree_cover_2000.updateMask(loss.Not())  # forest not already logged by Hansen's record

    today = datetime.date.today()
    current_start = today - datetime.timedelta(days=7)
    current_end = today
    baseline_start = current_start - datetime.timedelta(weeks=BASELINE_WEEKS)
    baseline_end = current_start

    print(f"Baseline window: {baseline_start} to {baseline_end}")
    print(f"Current window:  {current_start} to {current_end}")

    ndvi_baseline, n_baseline = ndvi_composite(study_area, str(baseline_start), str(baseline_end))
    ndvi_current, n_current = ndvi_composite(study_area, str(current_start), str(current_end))

    print("Baseline images used:", n_baseline.getInfo())
    print("Current images used:", n_current.getInfo())

    ndvi_drop = ndvi_baseline.subtract(ndvi_current)

    alert_mask = (
        standing_forest_mask
        .And(ndvi_baseline.gte(NDVI_HEALTHY_THRESHOLD))
        .And(ndvi_drop.gte(NDVI_DROP_THRESHOLD))
    )

    pixel_area = ee.Image.pixelArea()
    alert_area_img = alert_mask.selfMask().multiply(pixel_area)

    total_alert_m2 = ee.Number(
        alert_area_img.reduceRegion(
            reducer=ee.Reducer.sum(), geometry=study_area, scale=20, maxPixels=1e13
        ).get("treecover2000", 0)
    )
    total_alert_acres = total_alert_m2.multiply(M2_TO_ACRES).getInfo()
    total_alert_acres = round(total_alert_acres, 2) if total_alert_acres else 0.0

    print(f"Flagged alert area this week: {total_alert_acres} acres")

    # Vectorize for the map (capped scale/complexity to stay within EE limits)
    vectors = alert_mask.selfMask().reduceToVectors(
        geometry=study_area,
        scale=30,
        geometryType="polygon",
        eightConnected=True,
        maxPixels=1e13,
        bestEffort=True,
    )

    geojson = {"type": "FeatureCollection", "features": []}
    try:
        fc_info = vectors.limit(2000).getInfo()  # safety cap on feature count
        for feat in fc_info.get("features", []):
            geojson["features"].append({
                "type": "Feature",
                "geometry": feat["geometry"],
                "properties": {"week_of": str(current_start)},
            })
    except Exception as e:
        print("Vectorization returned no/limited features:", e)

    data_dir = os.path.join(os.path.dirname(__file__), "..", "docs", "data")
    os.makedirs(data_dir, exist_ok=True)

    with open(os.path.join(data_dir, "weekly_alerts.geojson"), "w") as f:
        json.dump(geojson, f)
    print(f"Wrote {len(geojson['features'])} alert polygons to weekly_alerts.geojson")

    # Append to the running weekly time series
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
    print(f"Updated {summary_path}")


if __name__ == "__main__":
    main()
