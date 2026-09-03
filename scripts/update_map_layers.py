"""
Generates static PNG map-layer overlays from the Hansen dataset, matching
the three map layers from the original GEE Code Editor script:
  - Tree Cover 2000 (green, hidden by default -- matches original)
  - Current Tree Cover (bright green, shown by default)
  - Binary Loss Map (red, shown by default)

Why static PNGs instead of live Earth Engine tiles: the dashboard is a
static GitHub Pages site with no authenticated Earth Engine session
running in the visitor's browser, so it can't request live EE map tiles
the way the Code Editor does. Rendering each layer once per run (via
getThumbURL, which does per-pixel visualization -- much lighter than a
country-wide reduceRegion) and saving it as a transparent PNG lets the
static site display the same layers without needing a live backend.

Run: python scripts/update_map_layers.py
"""

import json
import os
import ee
import requests
from common import init_earth_engine, get_study_area

THUMB_DIMENSIONS = 2048


def export_png(image, out_path, palette, min_val, max_val, region):
    url = image.getThumbURL({
        "region": region,
        "dimensions": THUMB_DIMENSIONS,
        "format": "png",
        "min": min_val,
        "max": max_val,
        "palette": palette,
    })
    resp = requests.get(url, timeout=120)
    resp.raise_for_status()
    with open(out_path, "wb") as f:
        f.write(resp.content)


def main():
    init_earth_engine()
    study_area = get_study_area()
    region = study_area.bounds(maxError=100)

    hansen = ee.Image("UMD/hansen/global_forest_change_2025_v1_13")
    tree_cover_2000 = hansen.select("treecover2000").gt(0).selfMask().clip(study_area)
    loss = hansen.select("loss").clip(study_area)
    tree_cover_current = tree_cover_2000.updateMask(loss.eq(0)).selfMask()
    binary_loss = loss.selfMask()

    data_dir = os.path.join(os.path.dirname(__file__), "..", "docs", "data")
    os.makedirs(data_dir, exist_ok=True)

    print("Exporting Tree Cover 2000 layer...", flush=True)
    export_png(tree_cover_2000, os.path.join(data_dir, "layer_treecover2000.png"),
               palette=["228B22"], min_val=0, max_val=1, region=region)

    print("Exporting Current Tree Cover layer...", flush=True)
    export_png(tree_cover_current, os.path.join(data_dir, "layer_treecover_current.png"),
               palette=["00FF00"], min_val=0, max_val=1, region=region)

    print("Exporting Binary Loss layer...", flush=True)
    export_png(binary_loss, os.path.join(data_dir, "layer_loss.png"),
               palette=["FF0000"], min_val=0, max_val=1, region=region)

    coords = region.coordinates().getInfo()[0]
    lons = [c[0] for c in coords]
    lats = [c[1] for c in coords]
    bounds = {"south": min(lats), "north": max(lats), "west": min(lons), "east": max(lons)}

    with open(os.path.join(data_dir, "map_layers_bounds.json"), "w") as f:
        json.dump(bounds, f, indent=2)

    print("Map layers exported.", flush=True)


if __name__ == "__main__":
    main()
