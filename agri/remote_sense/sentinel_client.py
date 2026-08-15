"""
Sentinel Hub / Copernicus Data Space Ecosystem client.

This server pulls satellite imagery and Sentinel's own pre-computed analysis
DIRECTLY from the Copernicus Data Space Ecosystem (CDSE) -- there is no separate
"remote-sense" partner system/team. See
precise-agric/stages/stage-9-satellite-monitoring.md for the product decisions
this implements (§7 on-demand pull, §9 comparative analysis).

Setup: a free account at https://dataspace.copernicus.eu/, then an OAuth client
(Dashboard -> User Settings -> OAuth clients -> Create) gives a Client ID + Client
Secret. Put them in .env as WO_SENTINEL_CLIENT_ID / WO_SENTINEL_CLIENT_SECRET --
never in chat/commit history (see agri/remote_sense docs for why).

Three capabilities, matching the things precise-agric needs from Sentinel:
  - fetch_field_imagery(): pulls one already-combined, reflectance-scaled,
    band-tagged multi-band GeoTIFF for a field/date via the Process API. Feeds
    straight into agri.capture.create_capture_from_orthophoto(source=SATELLITE).
  - fetch_field_statistics(): pulls Sentinel's OWN computed vegetation index
    (their engine, not ours; NDVI/GNDVI/NDRE/SAVI/EVI -- see SENTINEL_INDEX_DEFS)
    per acquisition date over a range, via the Statistical API -- no image
    download needed. This is the "Sentinel's own analysis engine" data source
    for the WebODM-vs-Sentinel comparison (stage-9 §9).
  - search_available_scenes(): lists actual available scenes (date + cloud%) via
    the Catalog API, so a user can see real options instead of blindly trusting
    fetch_field_imagery()'s automatic least-cloudy pick
    (stage-10-sentinel-roadmap.md Phase 2).

Both request Sentinel-2 L2A (atmospherically corrected -- surface reflectance,
not raw digital numbers) with units="REFLECTANCE", resolving the reflectance-vs-DN
question raised earlier in planning: we ask Sentinel Hub for the corrected value
directly instead of correcting it ourselves.

VERIFICATION NOTE (2026-08-06): both functions were live-tested against a real
CDSE account (Process API and Statistical API), over a real AOI near Norton,
Zimbabwe. Both succeeded and returned plausible data (13 real NDVI points over 45
days, mean ~0.29-0.36; a real 4-band image with values correctly in the 0-1
reflectance range). One real bug was found and fixed by this testing: the
built-in DataCollection.SENTINEL2_L2A carries its own default service_url (the
classic services.sentinel-hub.com host) that OVERRIDES config.sh_base_url and
causes every request to 401 against CDSE -- see _sentinel2_l2a_collection()
below, which redefines the collection explicitly (matching CDSE's own example
notebooks). This is what a plausible-looking, docs-based implementation missed
before being run for real.

CLOUD/QUALITY AWARENESS (2026-08-15, Stage 10 Phase 1) -- also live-verified,
two more real gotchas found before they became silent bugs:
  - The SCL (Scene Classification) band only supports units="DN" -- requesting
    it in the same input block as REFLECTANCE-unit bands is rejected outright by
    the server ("Band 'SCL' ... requested in unsupported units 'REFLECTANCE'!").
    So fetch_field_imagery() fetches SCL via a SEPARATE request (_fetch_scl),
    relying on Sentinel Hub's scene selection (LEAST_CC over the same geometry +
    date range) being a property of the resolved product, not the requested
    bands -- reasonable given LEAST_CC is scene/product-level metadata, but not
    independently proven; re-verify if valid_pixel_pct and the imported image
    ever look inconsistent in practice.
  - DO NOT hand-roll the DN->reflectance formula
    (rho = (DN + BOA_ADD_OFFSET) / QUANTIFICATION_VALUE) as a substitute for
    units="REFLECTANCE" -- live-tested side by side and it was off by a flat
    0.1 (i.e. the assumed BOA_ADD_OFFSET=-1000 does not match what this
    endpoint's REFLECTANCE conversion actually does internally). Always let
    Sentinel Hub do reflectance conversion itself; never replicate it locally.
  - fetch_field_statistics() escaped this trap entirely: its evalscript never
    requested units="REFLECTANCE" in the first place (NDVI is a scale-invariant
    ratio), so SCL-based cloud exclusion could be folded directly into its
    existing dataMask logic with no separate request -- live-verified, works.

SCENE AVAILABILITY (2026-08-15, Stage 10 Phase 2) -- search_available_scenes()
uses SentinelHubCatalog, live-verified against the real account: unlike Process/
Statistical API, the Catalog API worked identically with the bare
DataCollection.SENTINEL2_L2A and the CDSE-redefined collection (no 401 either
way) -- used the redefined one anyway for consistency with the rest of this
module rather than relying on an unverified assumption for other accounts.
Confirmed response shape: properties.datetime (ISO timestamp) and
properties.eo:cloud_cover (float 0-100).

BROADER INDICES (2026-08-15, Stage 10 Phase 4) -- fetch_field_statistics() no
longer hardcodes NDVI's formula; SENTINEL_INDEX_DEFS/_index_definition()
parametrize the evalscript by index name (NDVI/GNDVI/NDRE reuse Sentinel Hub's
index() helper; EVI/SAVI are written out directly, matching this codebase's own
drone-side formulas in app/api/formulas.py band-for-band). All 5 were
live-tested against the real CDSE account for the same AOI/date-range used in
Phase 1 -- all 5 returned 6 points each with plausible means (NDVI ~0.29,
matching Phase 1's original NDVI result exactly, confirming no regression;
GNDVI ~0.44, NDRE ~0.18, SAVI ~0.18, EVI ~0.17) and no evalscript errors. No new
bugs found this time -- the parametrization approach worked cleanly on the
first live run.
"""
import os

from webodm import settings
from agri.remote_sense.bands import apply_band_tags

# WebODM band role -> Sentinel-2 band token (the reverse of
# bands.BAND_ROLE_BY_TOKEN), used to build evalscripts.
SENTINEL_TOKEN_BY_ROLE = {'red': 'B04', 'green': 'B03', 'blue': 'B02',
                          'nir': 'B08', 'rededge': 'B05'}

# Sen2Cor SCL classification codes treated as usable for agricultural analysis:
# vegetation, bare soil, water, snow. Everything else (no-data, saturated, dark
# area, cloud shadow, unclassified, cloud low/thin, no-data, cloud probability,
# cirrus) is excluded -- see https://custom-scripts.sentinel-hub.com for the
# standard cloud-exclusion set (8, 9, 10) this extends conservatively.
SCL_VALID_CLASSES = {4, 5, 6, 11}
SCL_EXCLUDED_CLASSES = {0, 1, 2, 3, 7, 8, 9, 10}

# Indices fetch_field_statistics() can ask Sentinel Hub's Statistical API to
# compute (Stage 10 Phase 4). Formulas and band roles mirror this codebase's own
# drone-side algos (app/api/formulas.py's N/R/G/B/Re shorthand -> Sentinel-2
# B08/B04/B03/B02/B05) so a Sentinel value is comparable to our own analysis for
# the same index, not just a different index that happens to also be a ratio.
# NDVI/GNDVI/NDRE reuse Sentinel Hub's built-in index() helper (normalized
# difference); EVI/SAVI don't fit that shape and are written out directly.
SENTINEL_INDEX_DEFS = {
    'NDVI': {'bands': ('B04', 'B08'), 'formula': 'index(samples.B08, samples.B04)'},
    'GNDVI': {'bands': ('B03', 'B08'), 'formula': 'index(samples.B08, samples.B03)'},
    'NDRE': {'bands': ('B05', 'B08'), 'formula': 'index(samples.B08, samples.B05)'},
    'SAVI': {'bands': ('B04', 'B08'),
             'formula': '(1.5 * (samples.B08 - samples.B04)) / (samples.B08 + samples.B04 + 0.5)'},
    'EVI': {'bands': ('B02', 'B04', 'B08'),
            'formula': '2.5 * (samples.B08 - samples.B04) / '
                       '(samples.B08 + 6.0 * samples.B04 - 7.5 * samples.B02 + 1.0)'},
}


def _index_definition(index):
    """
    Look up the Sentinel-2 band list + evalscript formula for a supported index.
    Pure function, no network -- unit-testable directly against the constant above.

    :raises NotImplementedError: for an index not in SENTINEL_INDEX_DEFS
    """
    try:
        return SENTINEL_INDEX_DEFS[index]
    except KeyError:
        raise NotImplementedError(
            "Unsupported index '%s' -- supported: %s" % (index, ', '.join(sorted(SENTINEL_INDEX_DEFS))))


# CDSE's fixed public endpoints. Not secrets -- unlike the client id/secret these
# never change per-deployment, so they're constants here rather than settings.
SH_BASE_URL = 'https://sh.dataspace.copernicus.eu'
SH_TOKEN_URL = 'https://identity.dataspace.copernicus.eu/auth/realms/CDSE/protocol/openid-connect/token'


class SentinelNotConfiguredError(Exception):
    """WO_SENTINEL_CLIENT_ID / WO_SENTINEL_CLIENT_SECRET are not set."""


class SentinelRequestError(Exception):
    """The Sentinel Hub API call itself failed (network, quota, no scene found, ...)."""


def _config():
    from sentinelhub import SHConfig

    client_id = getattr(settings, 'SENTINEL_CLIENT_ID', None)
    client_secret = getattr(settings, 'SENTINEL_CLIENT_SECRET', None)
    if not client_id or not client_secret:
        raise SentinelNotConfiguredError(
            "WO_SENTINEL_CLIENT_ID / WO_SENTINEL_CLIENT_SECRET are not set -- create a "
            "free account and OAuth client at https://dataspace.copernicus.eu/ "
            "(Dashboard -> User Settings -> OAuth clients -> Create)")

    config = SHConfig()
    config.sh_client_id = client_id
    config.sh_client_secret = client_secret
    config.sh_base_url = SH_BASE_URL
    config.sh_token_url = SH_TOKEN_URL
    return config


def _sentinel2_l2a_collection():
    """
    The built-in DataCollection.SENTINEL2_L2A carries its own default service_url
    (the classic services.sentinel-hub.com host), which OVERRIDES config.sh_base_url
    and causes every request to 401 against CDSE -- confirmed live (see stage-9 doc
    §7). CDSE's own example notebooks redefine the collection explicitly; this
    mirrors that fix.
    """
    from sentinelhub import DataCollection
    return DataCollection.SENTINEL2_L2A.define_from('cdse_s2l2a', service_url=SH_BASE_URL)


def _evalscript_for_bands(roles):
    """
    An evalscript requesting `roles` (in order) as float32 reflectance bands.
    REFLECTANCE units + L2A (see fetch_field_imagery) means these values are
    already atmospherically corrected -- comparable across dates/scenes with no
    correction needed on our side.
    """
    tokens = [SENTINEL_TOKEN_BY_ROLE[r] for r in roles]
    input_bands = ", ".join('"%s"' % t for t in tokens)
    return_bands = ", ".join("samples.%s" % t for t in tokens)
    return """
        //VERSION=3
        function setup() {
            return {
                input: [{bands: [%s], units: "REFLECTANCE"}],
                output: {bands: %d, sampleType: "FLOAT32"}
            };
        }
        function evaluatePixel(samples) {
            return [%s];
        }
    """ % (input_bands, len(tokens), return_bands)


def _valid_pixel_pct(scl_array):
    """
    Fraction (0-100) of an SCL array classified as usable for agricultural
    analysis -- see SCL_VALID_CLASSES. Pure function, no network calls, so it's
    unit-testable directly against synthetic arrays.
    """
    import numpy as np
    total = scl_array.size
    if total == 0:
        return 0.0
    valid = np.isin(scl_array, list(SCL_VALID_CLASSES)).sum()
    return round(100.0 * float(valid) / total, 1)


def _fetch_scl(geom, date_from, date_to):
    """
    Raw SCL (Scene Classification) array for the least-cloudy Sentinel-2 L2A
    scene over [geom, date_from, date_to] -- the SAME selection criteria
    fetch_field_imagery() uses. A SEPARATE request from the imagery pull because
    Sentinel Hub rejects requesting SCL in the same input block as
    REFLECTANCE-unit bands (confirmed live -- see module docstring). Raises on
    failure; callers that want "flag, don't block" behaviour should catch and
    degrade to valid_pixel_pct=None rather than failing the whole pull.
    """
    from sentinelhub import (BBox, CRS, Geometry, MimeType,
                             MosaickingOrder, SentinelHubRequest, bbox_to_dimensions)

    config = _config()
    sh_geometry = Geometry(geom.wkt, crs=CRS.WGS84)
    bbox = BBox(geom.extent, crs=CRS.WGS84)
    size = bbox_to_dimensions(bbox, resolution=10)

    evalscript = """
        //VERSION=3
        function setup() {
            return {
                input: [{bands: ["SCL"], units: "DN"}],
                output: {bands: 1, sampleType: "UINT8"}
            };
        }
        function evaluatePixel(samples) {
            return [samples.SCL];
        }
    """
    request = SentinelHubRequest(
        evalscript=evalscript,
        input_data=[SentinelHubRequest.input_data(
            data_collection=_sentinel2_l2a_collection(),
            time_interval=(date_from.isoformat(), date_to.isoformat()),
            mosaicking_order=MosaickingOrder.LEAST_CC)],
        responses=[SentinelHubRequest.output_response('default', MimeType.TIFF)],
        geometry=sh_geometry,
        bbox=bbox,
        size=size,
        config=config)

    try:
        images = request.get_data()
    except Exception as e:
        raise SentinelRequestError("Sentinel Hub SCL request failed: %s" % str(e))
    if not images:
        raise SentinelRequestError("No Sentinel-2 scene found for that area/date range")

    # A single-band Process API response comes back 2D (height, width) -- unlike
    # fetch_field_imagery's multi-band request, which comes back 3D (height,
    # width, bands). Confirmed live: assuming 3D unconditionally here raised
    # "IndexError: too many indices" the first time this was run for real.
    arr = images[0]
    return arr if arr.ndim == 2 else arr[:, :, 0]


def fetch_field_imagery(geom, date_from, date_to, output_path, roles=None):
    """
    Download one combined, reflectance-scaled, band-tagged multi-band GeoTIFF for
    `geom` (a django.contrib.gis.geos geometry, WGS84) over [date_from, date_to],
    using the least-cloudy Sentinel-2 L2A scene in that window. Writes to
    output_path, ready for
    agri.capture.create_capture_from_orthophoto(..., source=CaptureMeta.SATELLITE).

    :param date_from, date_to: datetime.date
    :param roles: band roles to fetch (agri/api/formulas.py vocabulary), default
                 red/green/blue/nir -- true colour + NDVI.
    :return: {'path': output_path, 'valid_pixel_pct': float or None}. None means
             the SCL quality check itself failed (network hiccup, etc) -- the
             imagery pull still succeeded and is NOT discarded because of it
             ("flag, don't block" -- stage-10-sentinel-roadmap.md Phase 1).
    :raises SentinelNotConfiguredError, SentinelRequestError (imagery pull only)
    """
    from sentinelhub import (BBox, CRS, Geometry, MimeType,
                             MosaickingOrder, SentinelHubRequest, bbox_to_dimensions)

    roles = roles or ['red', 'green', 'blue', 'nir']
    config = _config()

    sh_geometry = Geometry(geom.wkt, crs=CRS.WGS84)
    bbox = BBox(geom.extent, crs=CRS.WGS84)
    size = bbox_to_dimensions(bbox, resolution=10)

    request = SentinelHubRequest(
        evalscript=_evalscript_for_bands(roles),
        input_data=[SentinelHubRequest.input_data(
            data_collection=_sentinel2_l2a_collection(),
            time_interval=(date_from.isoformat(), date_to.isoformat()),
            mosaicking_order=MosaickingOrder.LEAST_CC)],
        responses=[SentinelHubRequest.output_response('default', MimeType.TIFF)],
        geometry=sh_geometry,
        bbox=bbox,
        size=size,
        config=config)

    try:
        images = request.get_data()
    except Exception as e:
        raise SentinelRequestError("Sentinel Hub imagery request failed: %s" % str(e))
    if not images:
        raise SentinelRequestError("No Sentinel-2 scene found for that area/date range")

    import rasterio
    from rasterio.transform import from_bounds

    arr = images[0]  # (height, width, bands)
    height, width, band_count = arr.shape
    transform = from_bounds(*bbox, width=width, height=height)

    os.makedirs(os.path.dirname(output_path) or '.', exist_ok=True)
    with rasterio.open(output_path, 'w', driver='GTiff', height=height, width=width,
                       count=band_count, dtype='float32', crs='EPSG:4326',
                       transform=transform) as dst:
        for i in range(band_count):
            dst.write(arr[:, :, i].astype('float32'), i + 1)

    apply_band_tags(output_path, roles)

    # Best-effort cloud/quality check -- a failure here must never discard an
    # otherwise-successful imagery pull (see docstring: "flag, don't block").
    try:
        scl = _fetch_scl(geom, date_from, date_to)
        valid_pixel_pct = _valid_pixel_pct(scl)
    except SentinelRequestError:
        valid_pixel_pct = None

    return {'path': output_path, 'valid_pixel_pct': valid_pixel_pct}


def fetch_field_statistics(geom, date_from, date_to, index='NDVI'):
    """
    Sentinel Hub's OWN computed statistic (their engine, not ours) for `geom` over
    [date_from, date_to], one point per acquisition date with data. This is the
    "Sentinel's own analysis" side of the WebODM-vs-Sentinel comparison
    (stage-9-satellite-monitoring.md §9) -- no image download needed.

    :param date_from, date_to: datetime.date
    :return: [{'date': 'YYYY-MM-DD', 'mean': float, 'stddev': float or None,
              'valid_pixel_pct': float or None}, ...]
    :raises SentinelNotConfiguredError, SentinelRequestError, NotImplementedError
    """
    from sentinelhub import CRS, Geometry, SentinelHubStatistical

    index_def = _index_definition(index)

    config = _config()
    sh_geometry = Geometry(geom.wkt, crs=CRS.WGS84)

    # No units="REFLECTANCE" here -- every supported index is a scale-invariant
    # ratio, so this evalscript never hits the SCL/REFLECTANCE units conflict
    # fetch_field_imagery does (see module docstring). That means SCL-based cloud
    # exclusion folds straight into the existing dataMask logic, live-verified,
    # no separate request needed. dataMask=0 pixels are excluded from Sentinel
    # Hub's own sampleCount/noDataCount aggregation, which is what
    # valid_pixel_pct reads.
    excluded = ", ".join(str(c) for c in sorted(SCL_EXCLUDED_CLASSES))
    input_bands = list(index_def['bands']) + ['SCL', 'dataMask']
    bands_js = "[%s]" % ", ".join('"%s"' % b for b in input_bands)
    evalscript = """
        //VERSION=3
        function setup() {
            return {
                input: [{bands: %s}],
                output: [{id: "value", bands: 1}, {id: "dataMask", bands: 1}]
            };
        }
        function evaluatePixel(samples) {
            var cloudClasses = [%s];
            var valid = samples.dataMask;
            for (var i = 0; i < cloudClasses.length; i++) {
                if (samples.SCL == cloudClasses[i]) { valid = 0; }
            }
            return {value: [%s], dataMask: [valid]};
        }
    """ % (bands_js, excluded, index_def['formula'])

    request = SentinelHubStatistical(
        aggregation=SentinelHubStatistical.aggregation(
            evalscript=evalscript,
            time_interval=(date_from.isoformat(), date_to.isoformat()),
            aggregation_interval='P1D'),
        input_data=[SentinelHubStatistical.input_data(_sentinel2_l2a_collection())],
        geometry=sh_geometry,
        config=config)

    try:
        response = request.get_data()[0]
    except Exception as e:
        raise SentinelRequestError("Sentinel Hub statistics request failed: %s" % str(e))

    # Generic walk over outputs/bands (rather than hardcoding a band key we
    # haven't confirmed against a live response -- see module docstring).
    points = []
    for entry in response.get('data', []):
        date_str = (entry.get('interval', {}).get('from') or '')[:10]
        for output in entry.get('outputs', {}).values():
            for band_stats in output.get('bands', {}).values():
                stats = band_stats.get('stats')
                if date_str and stats and stats.get('mean') is not None:
                    sample_count = stats.get('sampleCount')
                    no_data_count = stats.get('noDataCount')
                    valid_pixel_pct = None
                    if sample_count:
                        valid_pixel_pct = round(
                            100.0 * (sample_count - (no_data_count or 0)) / sample_count, 1)
                    points.append({'date': date_str, 'mean': stats['mean'],
                                  'stddev': stats.get('stDev'),
                                  'valid_pixel_pct': valid_pixel_pct})
    return points


def _dedupe_scenes_by_date(catalog_results):
    """
    Collapse raw Catalog API results (one entry per scene/tile) into one entry
    per calendar day, keeping the least-cloudy scene when more than one falls on
    the same day (e.g. overlapping tile edges), sorted newest first. Pure
    function, no network calls -- unit-testable directly against a list of
    dicts shaped like Catalog API's `properties.datetime`/`properties.eo:cloud_cover`.

    :param catalog_results: [{'properties': {'datetime': iso_str, 'eo:cloud_cover': float}}, ...]
    :return: [{'date': 'YYYY-MM-DD', 'cloud_cover_pct': float}, ...], newest first
    """
    by_date = {}
    for r in catalog_results:
        props = r.get('properties', {})
        dt = props.get('datetime')
        cloud = props.get('eo:cloud_cover')
        if not dt or cloud is None:
            continue
        date_str = dt[:10]
        if date_str not in by_date or cloud < by_date[date_str]:
            by_date[date_str] = cloud

    scenes = [{'date': d, 'cloud_cover_pct': round(c, 1)} for d, c in by_date.items()]
    scenes.sort(key=lambda s: s['date'], reverse=True)
    return scenes


def search_available_scenes(geom, date_from, date_to):
    """
    List actually-available Sentinel-2 L2A scenes for `geom` over
    [date_from, date_to] via the Catalog API, so a user can pick a specific
    scene instead of blindly trusting fetch_field_imagery()'s automatic
    least-cloudy pick (stage-10-sentinel-roadmap.md Phase 2).

    :param date_from, date_to: datetime.date
    :return: [{'date': 'YYYY-MM-DD', 'cloud_cover_pct': float}, ...], newest
             first. If more than one scene falls on the same day (e.g.
             overlapping tile edges), keeps the least-cloudy one for that day.
    :raises SentinelNotConfiguredError, SentinelRequestError
    """
    from sentinelhub import BBox, CRS, SentinelHubCatalog

    config = _config()
    bbox = BBox(geom.extent, crs=CRS.WGS84)

    try:
        catalog = SentinelHubCatalog(config=config)
        search_iterator = catalog.search(
            _sentinel2_l2a_collection(),
            bbox=bbox,
            time=(date_from.isoformat(), date_to.isoformat()),
            fields={'include': ['properties.datetime', 'properties.eo:cloud_cover'], 'exclude': []})
        results = list(search_iterator)
    except Exception as e:
        raise SentinelRequestError("Sentinel Hub catalog search failed: %s" % str(e))

    return _dedupe_scenes_by_date(results)
