"""
Computes independent, per-year tree cover & loss statistics for Pakistan
(2000-2025) from the Hansen Global Forest Change dataset, and writes them
to data/annual_stats.json for the dashboard to read.

Each year is calculated separately (not derived from the prior year):
  - Loss_Acres: acreage lost specifically in that year
  - Tree_Cover_Acres: acreage still forested as of that year (2000 baseline
    minus everything lost up to and including that year)

Run: python scripts/update_annual_stats.py
"""

import json
import os
import ee
from common import init_earth_engine, get_study_area

M2_TO_ACRES = 0.000247105
END_YEAR = 2025  # change if a newer Hansen release adds another year
NUM_YEARS = END_YEAR - 2000  # years 1..NUM_YEARS correspond to 2001..END_YEAR


def main():
    init_earth_engine()
    study_area = get_study_area()

    hansen = ee.Image("UMD/hansen/global_forest_change_2025_v1_13")
    tree_cover_2000 = hansen.select("treecover2000").gt(0).selfMask().clip(study_area)
    loss = hansen.select("loss").clip(study_area)
    loss_year = hansen.select("lossyear").clip(study_area)
    pixel_area = ee.Image.pixelArea()

    # Baseline year 2000 total (its own independent row)
    total_acres_2000 = ee.Number(
        tree_cover_2000.multiply(pixel_area).reduceRegion(
            reducer=ee.Reducer.sum(), geometry=study_area, scale=100, maxPixels=1e13
        ).get("treecover2000")
    ).multiply(M2_TO_ACRES)

    def yearly_stats(y):
        y = ee.Number(y)
        year_val = y.add(2000)

        yearly_loss_img = loss_year.eq(y)
        acres_lost = ee.Number(
            yearly_loss_img.multiply(pixel_area).reduceRegion(
                reducer=ee.Reducer.sum(), geometry=study_area, scale=250, maxPixels=1e13
            ).get("lossyear", 0)
        ).multiply(M2_TO_ACRES)

        still_standing_mask = tree_cover_2000.And(loss.Not().Or(loss_year.gt(y)))
        acres_cover = ee.Number(
            still_standing_mask.multiply(pixel_area).reduceRegion(
                reducer=ee.Reducer.sum(), geometry=study_area, scale=250, maxPixels=1e13
            ).get("treecover2000", 0)
        ).multiply(M2_TO_ACRES)

        return ee.Feature(None, {
            "Year": year_val,
            "Loss_Acres": acres_lost,
            "Tree_Cover_Acres": acres_cover,
        })

    years = ee.List.sequence(1, NUM_YEARS)
    annual_stats = ee.FeatureCollection(years.map(yearly_stats))

    print("Requesting annual stats from Earth Engine (this can take a few minutes)...")
    result = annual_stats.getInfo()["features"]

    rows = [
        {"year": 2000, "loss_acres": 0, "tree_cover_acres": round(total_acres_2000.getInfo(), 2)}
    ]
    for f in result:
        p = f["properties"]
        rows.append({
            "year": int(p["Year"]),
            "loss_acres": round(p["Loss_Acres"], 2),
            "tree_cover_acres": round(p["Tree_Cover_Acres"], 2),
        })
    rows.sort(key=lambda r: r["year"])

    out_path = os.path.join(os.path.dirname(__file__), "..", "docs", "data", "annual_stats.json")
    with open(out_path, "w") as f:
        json.dump({"source": "Hansen Global Forest Change v1.13 (UMD)", "unit": "acres", "years": rows}, f, indent=2)

    print(f"Wrote {len(rows)} yearly rows to {out_path}")


if __name__ == "__main__":
    main()
