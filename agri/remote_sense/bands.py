"""
Sentinel band stacking for remote-sense captures.

The remote-sense (Sentinel) system delivers each spectral band as a separate
single-band GeoTIFF (B02, B03, B04, B08, ...). WebODM's analysis pipeline, on the
other hand, works off a *single* multi-band orthophoto whose bands are TAGGED with
descriptions -- app/api/formulas.py matches bands by description ('red', 'green',
'blue', 'nir', 'rededge'), and agri/analysis/base.py auto-picks NDVI vs ExG from
those tags. An untagged stack (e.g. a bare `gdalbuildvrt -separate`) would make the
pipeline silently fall back to RGB-only indices even when NIR is present.

So this module does the load-bearing step: resample every band onto a common grid,
stack them into one multi-band GeoTIFF *with per-band descriptions + RGB
colorinterp*, and convert to a COG. The output drops straight into the existing
capture-import path (agri/capture.py) and everything downstream just works.

Band set for v1: B02/B03/B04/B08 (RGB + NIR, all native 10 m) -> NDVI + RGB
indices. B05 (red-edge) is accepted if present -> NDRE. Bands we don't use
(B01 aerosol, B11 SWIR -- no SWIR support in the formula engine, SCL, ...) are
ignored. See precise-agric discussion for why NDMI is out of scope.
"""
import os
import re

import numpy as np
import rasterio
from rasterio.enums import ColorInterp, Resampling
from rasterio.warp import reproject

from app.cogeo import assure_cogeo

# Sentinel-2 band token -> WebODM band-math role (must match app/api/formulas.py's
# vocabulary exactly, or get_auto_bands() can't resolve the band).
BAND_ROLE_BY_TOKEN = {
    'B02': 'blue',
    'B03': 'green',
    'B04': 'red',
    'B08': 'nir',
    'B05': 'rededge',
}

# Order bands are written into the stack. Only present roles are written; the RGB
# trio (when present) gets red/green/blue colorinterp so the tiler renders a
# natural-colour image, and every band gets its role as a description.
CANONICAL_ORDER = ['red', 'green', 'blue', 'nir', 'rededge']

# The minimum that makes a usable capture (natural-colour display + every RGB
# index needs red). NIR/red-edge are optional (they unlock NDVI/NDRE when present).
REQUIRED_ROLES = ('red', 'green', 'blue')

COLORINTERP_BY_ROLE = {
    'red': ColorInterp.red,
    'green': ColorInterp.green,
    'blue': ColorInterp.blue,
}

# A Sentinel band token as it appears in filenames like
# "..._Sentinel-2_L2A_B04_(Raw).tif" -- bounded by non-alphanumerics so the "2" in
# "Sentinel-2" or a date can't match.
_BAND_TOKEN_RE = re.compile(r'(?<![0-9A-Za-z])(B0[1-9]|B1[0-2]|B8A)(?![0-9A-Za-z])',
                            re.IGNORECASE)


class BandStackError(Exception):
    """The uploaded bands can't be stacked into a usable capture (maps to HTTP 400)."""


def detect_band_role(filename):
    """
    Map a Sentinel band filename to a WebODM band role, or None if the band is
    unknown / one we deliberately ignore.

    e.g. "..._Sentinel-2_L2A_B04_(Raw).tif" -> 'red'.
    """
    if not filename:
        return None
    m = _BAND_TOKEN_RE.search(os.path.basename(filename))
    if not m:
        return None
    return BAND_ROLE_BY_TOKEN.get(m.group(1).upper())


def apply_band_tags(path, roles):
    """
    (Re)write per-band descriptions + colorinterp on an existing multi-band raster,
    where band i (1-indexed) is roles[i-1]. Shared by stack_and_tag_bands() below
    and sentinel_client.py, which fetches an already-combined multi-band image
    directly from Sentinel Hub and only needs this tagging step, not the
    resample-and-stack logic.
    """
    with rasterio.open(path, 'r+') as dst:
        for i, role in enumerate(roles, start=1):
            dst.set_band_description(i, role)
        dst.colorinterp = [COLORINTERP_BY_ROLE.get(r, ColorInterp.undefined)
                           for r in roles]


def stack_and_tag_bands(role_to_path, output_path):
    """
    Stack single-band Sentinel GeoTIFFs into one multi-band, band-tagged COG.

    :param role_to_path: {'red': path, 'green': path, 'blue': path, 'nir': path, ...}
    :param output_path: destination .tif (converted to COG in place)
    :return: output_path

    Every band is resampled onto the red band's grid so bands at different native
    resolutions line up. Per-band descriptions ('red'/'green'/'blue'/'nir'/
    'rededge') + RGB colorinterp are written so downstream band-math resolves
    automatically.
    """
    missing = [r for r in REQUIRED_ROLES if r not in role_to_path]
    if missing:
        raise BandStackError(
            "missing required band(s): %s (need at least red=B04, green=B03, blue=B02)"
            % ", ".join(missing))

    roles = [r for r in CANONICAL_ORDER if r in role_to_path]

    with rasterio.open(role_to_path['red']) as ref:
        profile = ref.profile.copy()
        ref_transform = ref.transform
        ref_crs = ref.crs
        ref_width = ref.width
        ref_height = ref.height

    profile.update(count=len(roles), dtype='float32', driver='GTiff',
                   compress='deflate', tiled=True, blockxsize=256, blockysize=256)
    profile.pop('photometric', None)

    os.makedirs(os.path.dirname(output_path) or '.', exist_ok=True)
    with rasterio.open(output_path, 'w', **profile) as dst:
        for i, role in enumerate(roles, start=1):
            data = np.zeros((ref_height, ref_width), dtype='float32')
            with rasterio.open(role_to_path[role]) as src:
                reproject(
                    source=rasterio.band(src, 1),
                    destination=data,
                    src_transform=src.transform,
                    src_crs=src.crs,
                    dst_transform=ref_transform,
                    dst_crs=ref_crs,
                    resampling=Resampling.bilinear)
            dst.write(data, i)
            dst.set_band_description(i, role)
        dst.colorinterp = [COLORINTERP_BY_ROLE.get(r, ColorInterp.undefined)
                           for r in roles]

    assure_cogeo(output_path)

    # Some GDAL COG-driver versions drop band descriptions during translation;
    # re-assert them on the final file so the analysis pipeline can always find NIR.
    try:
        apply_band_tags(output_path, roles)
    except Exception:
        pass

    return output_path
