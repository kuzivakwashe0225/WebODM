"""
Weed Mapping service (Stage 3), adapted from coreplugins/weed_detect.

Clips the ortho to the boundary, computes NGRDI = (G-R)/(G+R), thresholds for
low-vegetation weeds, labels connected components (scipy.ndimage), filters tiny
blobs, and emits weed-point GeoJSON + a count.
"""
import json
import os
import shutil
import subprocess
import tempfile

import rasterio
from scipy import ndimage
from django.contrib.gis.geos import GEOSGeometry

from app.models import Task
from webodm import settings


def _crop_to_vrt(orthophoto, crop_wkt):
    tmpdir = tempfile.mkdtemp('_agri_weed', dir=settings.MEDIA_TMP)
    crop_geojson = os.path.join(tmpdir, "crop.geojson")
    ortho_vrt = os.path.join(tmpdir, "orthophoto.vrt")
    with open(crop_geojson, "w", encoding="utf-8") as f:
        f.write(GEOSGeometry(crop_wkt).geojson)
    subprocess.check_output(["gdalwarp", "-cutline", crop_geojson,
                             '--config', 'GDALWARP_DENSIFY_CUTLINE', 'NO',
                             '-crop_to_cutline', '-of', 'VRT', orthophoto, ortho_vrt])
    return ortho_vrt, tmpdir


def compute_weed_mapping(task, boundary, output_path, min_blob_px=10):
    ortho = task.assets_path(Task.ASSETS_MAP["orthophoto.tif"])
    vrt, tmpdir = _crop_to_vrt(ortho, boundary.geom.wkt)

    features = []
    try:
        with rasterio.open(vrt) as src:
            red = src.read(1).astype(float)
            green = src.read(2).astype(float)
            ngrdi = (green - red) / (green + red + 1e-10)
            mask = (ngrdi > 0) & (ngrdi < 0.3)

            labeled, num = ndimage.label(mask)
            if num > 0:
                sizes = ndimage.sum(mask, labeled, range(1, num + 1))
                centroids = ndimage.center_of_mass(mask, labeled, range(1, num + 1))
                for i, (y, x) in enumerate(centroids):
                    if sizes[i] >= min_blob_px:
                        lon, lat = src.xy(y, x)
                        features.append({
                            "type": "Feature",
                            "geometry": {"type": "Point", "coordinates": [lon, lat]},
                            "properties": {"class": "weed", "score": 1.0},
                        })
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    geojson = {"type": "FeatureCollection", "features": features,
               "properties": {"count": len(features)}}
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(geojson, f)

    return output_path, {"weed_count": len(features)}
