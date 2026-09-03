"""
Computes precise annual tree cover & loss statistics for Pakistan (2000-2025)
from the Hansen Global Forest Change dataset and writes them to 
docs/data/annual_stats.json for web dashboard rendering.

KPI Dashboard Metrics Included:
1. Forest Area in 2000
2. Forest Area in 2025 (Remaining)
3. Total Forest Loss (2000–2025)
4. Highest Loss Province & Highest Loss District/City

Run: python scripts/update_annual_stats.py
"""

import json
import os
import ee
import pandas as pd
import numpy as np
from common import init_earth_engine, get_study_area

# ------------------------------------------------------------------------------
# CONSTANTS & METRIC CONVERSIONS
# ------------------------------------------------------------------------------
M2_TO_ACRES = 0.000247105
CANOPY_THRESHOLD = 1  # Minimum 10% tree canopy density
START_YEAR = 2001
END_YEAR = 2024        # Latest complete dataset year in Hansen v1.12


def main():
    init_earth_engine()
    study_area = get_study_area()

    # 1. Dataset Ingestion & Preprocessing
    hansen = ee.Image("UMD/hansen/global_forest_change_2024_v1_12")
    tree_cover_2000 = hansen.select("treecover2000")
    loss_year_band = hansen.select("lossyear")
    loss_mask = hansen.select("loss")

    # Filter baseline forest cover by canopy threshold
    forest_2000_mask = tree_cover_2000.gte(CANOPY_THRESHOLD)
    pixel_area_m2 = ee.Image.pixelArea()

    # Baseline 2000 forest area image
    forest_2000_area_img = forest_2000_mask.multiply(pixel_area_m2).rename("forest_2000_m2")
    
    # Constrain loss strictly to baseline forest canopy
    valid_loss_mask = loss_mask.gt(0).And(forest_2000_mask)
    cum_loss_area_img = valid_loss_mask.multiply(pixel_area_m2).rename("cum_loss_m2")

    # Construct annual loss band stack
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
    ])

    # 2. Administrative Boundaries Ingestion (GAUL L1 & L2)
    gaul_l1 = ee.FeatureCollection("FAO/GAUL/2015/level1").filter(ee.Filter.eq("ADM0_NAME", "Pakistan"))
    gaul_l2 = ee.FeatureCollection("FAO/GAUL/2015/level2").filter(ee.Filter.eq("ADM0_NAME", "Pakistan"))

    print("Executing server-side spatial reductions at 30m resolution...", flush=True)

    # Server-side spatial reductions
    prov_reduced = composite_metrics.reduceRegions(
        collection=gaul_l1,
        reducer=ee.Reducer.sum(),
        scale=30,
        tileScale=16
    ).getInfo()

    dist_reduced = composite_metrics.reduceRegions(
        collection=gaul_l2,
        reducer=ee.Reducer.sum(),
        scale=30,
        tileScale=16
    ).getInfo()

    # 3. Administrative Standardization (FATA -> KPK Integration)
    name_mapping = {
        'Baluchistan': 'Balochistan',
        'N.W.F.P.': 'Khyber Pakhtunkhwa',
        'F.A.T.A.': 'Khyber Pakhtunkhwa',
        'Federally Administered Tribal Areas': 'Khyber Pakhtunkhwa',
        'Northern Areas': 'Gilgit Baltistan',
        'Azad Kashmir': 'Azad Kashmir'
    }

    prov_rows = []
    for f in prov_reduced['features']:
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

    df_prov = pd.DataFrame(prov_rows).groupby('province', as_index=False).sum()

    dist_rows = []
    for f in dist_reduced['features']:
        props = f['properties']
        dist_rows.append({
            'district': props.get('ADM2_NAME', 'Unknown'),
            'province': name_mapping.get(props.get('ADM1_NAME', 'Unknown'), props.get('ADM1_NAME', 'Unknown')),
            'cum_loss_acres': props.get('cum_loss_m2', 0) * M2_TO_ACRES
        })
    df_dist = pd.DataFrame(dist_rows)

    # 4. Compute 4 Executive Dashboard KPIs
    total_forest_2000 = round(df_prov['forest_2000_acres'].sum(), 2)
    total_cum_loss = round(df_prov['cum_loss_acres'].sum(), 2)
    total_forest_2025 = round(total_forest_2000 - total_cum_loss, 2)

    top_prov = df_prov.sort_values(by='cum_loss_acres', ascending=False).iloc[0]
    top_dist = df_dist.sort_values(by='cum_loss_acres', ascending=False).iloc[0]

    kpi_summary = {
        "forest_area_2000_acres": total_forest_2000,
        "forest_area_2025_acres": total_forest_2025,
        "total_forest_loss_acres": total_cum_loss,
        "highest_loss_province": {
            "name": top_prov['province'],
            "loss_acres": round(top_prov['cum_loss_acres'], 2)
        },
        "highest_loss_district": {
            "name": top_dist['district'],
            "province": top_dist['province'],
            "loss_acres": round(top_dist['cum_loss_acres'], 2)
        }
    }

    # 5. Build Annual Time-Series Sequence
    annual_trend = []
    running_cover = total_forest_2000

    annual_trend.append({
        "year": 2000,
        "loss_acres": 0.0,
        "tree_cover_acres": round(running_cover, 2)
    })

    for y in years:
        annual_loss = df_prov[f'loss_{y}'].sum()
        running_cover -= annual_loss
        annual_trend.append({
            "year": y,
            "loss_acres": round(annual_loss, 2),
            "tree_cover_acres": round(running_cover, 2)
        })

    # 6. Save Formatted JSON
    output_payload = {
        "source": "Hansen Global Forest Change v1.12 (UMD)",
        "unit": "acres",
        "kpis": kpi_summary,
        "annual_trend": annual_trend
    }

    out_path = os.path.join(os.path.dirname(__file__), "..", "docs", "data", "annual_stats.json")
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    
    with open(out_path, "w") as f:
        json.dump(output_payload, f, indent=2)

    print(f"✓ Output written to {out_path}", flush=True)


if __name__ == "__main__":
    main()