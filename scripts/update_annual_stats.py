"""
Computes independent, per-year tree cover & loss statistics for Pakistan
(2000-2025) from the Hansen Global Forest Change dataset, and writes them
to docs/data/annual_stats.json for the dashboard to read.

Each year is calculated separately (not derived from the prior year):
  - loss_acres: acreage lost specifically in that year
  - tree_cover_acres: acreage still forested as of that year (2000 baseline
    minus everything lost up to and including that year)

Years are requested one at a time in a loop (rather than bundled into a
single Earth Engine request) so that no single call is large enough to
risk hitting Earth Engine's ~5-minute synchronous computation limit, and
so progress prints live instead of going silent until the very end.

Run: python scripts/update_annual_stats.py
"""

import json
import os
import ee
from common import init_earth_engine, get_study_area

M2_TO_ACRES = 0.000247105
END_YEAR = 2025


def main():
    init_earth_engine()
    study_area = get_study_area()

    hansen = ee.Image("UMD/hansen/global_forest_change_2025_v1_13")
    tree_cover_2000 = hansen.select("treecover2000").gt(0).selfMask().clip(study_area)
    loss = hansen.select("loss").clip(study_area)
    loss_year = hansen.select("lossyear").clip(study_area)
    pixel_area = ee.Image.pixelArea()

    print("Computing 2000 baseline...", flush=True)
    total_acres_2000 = ee.Number(
        tree_cover_2000.multiply(pixel_area).reduceRegion(
            reducer=ee.Reducer.sum(), geometry=study_area, scale=100,
            maxPixels=1e13, bestEffort=True, tileScale=4
        ).get("treecover2000")
    ).multiply(M2_TO_ACRES).getInfo()
    print(f"  2000 baseline: {round(total_acres_2000, 2)} acres", flush=True)

    rows = [{"year": 2000, "loss_acres": 0, "tree_cover_acres": round(total_acres_2000, 2)}]

    for y in range(1, END_YEAR - 2000 + 1):
        year_val = 2000 + y
        print(f"Computing {year_val}...", flush=True)

        yearly_loss_img = loss_year.eq(y)
        acres_lost = ee.Number(
            yearly_loss_img.multiply(pixel_area).reduceRegion(
                reducer=ee.Reducer.sum(), geometry=study_area, scale=250,
                maxPixels=1e13, bestEffort=True, tileScale=4
            ).get("lossyear", 0)
        ).multiply(M2_TO_ACRES).getInfo()

        still_standing_mask = tree_cover_2000.And(loss.Not().Or(loss_year.gt(y)))
        acres_cover = ee.Number(
            still_standing_mask.multiply(pixel_area).reduceRegion(
                reducer=ee.Reducer.sum(), geometry=study_area, scale=250,
                maxPixels=1e13, bestEffort=True, tileScale=4
            ).get("treecover2000", 0)
        ).multiply(M2_TO_ACRES).getInfo()

        acres_lost = round(acres_lost, 2) if acres_lost else 0.0
        acres_cover = round(acres_cover, 2) if acres_cover else 0.0

        print(f"  {year_val}: loss={acres_lost} acres, cover={acres_cover} acres", flush=True)

        rows.append({"year": year_val, "loss_acres": acres_lost, "tree_cover_acres": acres_cover})

    rows.sort(key=lambda r: r["year"])

    out_path = os.path.join(os.path.dirname(__file__), "..", "docs", "data", "annual_stats.json")
    with open(out_path, "w") as f:
        json.dump({"source": "Hansen Global Forest Change v1.13 (UMD)", "unit": "acres", "years": rows}, f, indent=2)

    print(f"Wrote {len(rows)} yearly rows to {out_path}", flush=True)


if __name__ == "__main__":
    main()
