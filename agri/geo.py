"""
Geometry -> GeoJSON helpers that are axis-order-correct.

WHY THIS EXISTS: `GEOSGeometry.geojson` / `.json` delegate to GDAL/OGR, and under
GDAL 3+ OGR honors the *authority* axis order for EPSG:4326 -- which is
latitude, longitude. So `geom.geojson` emits coordinates as [lat, lng], even
though the geometry is physically stored (and .wkt / .coords / .extent all report
it) as [lng, lat]. GeoJSON (RFC 7946) and Leaflet/rio-tiler all expect
[lng, lat]. Using `.geojson` therefore silently swaps every boundary's axes,
placing it on the wrong side of the planet.

`geom.coords` returns the raw stored ordinates as (x, y) = (lng, lat) tuples,
which IS correct GeoJSON order -- so we build the dict from that instead.
"""


def geom_to_geojson(geom):
    """A GeoJSON geometry dict ([lng, lat]) for a GEOS Polygon/MultiPolygon."""
    return {"type": geom.geom_type, "coordinates": geom.coords}


def geom_to_feature(geom):
    """A GeoJSON Feature wrapping geom (what rio-tiler's create_cutline wants)."""
    return {"type": "Feature", "properties": {}, "geometry": geom_to_geojson(geom)}
