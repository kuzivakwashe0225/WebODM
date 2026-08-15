"""
Report Builder service (Stage 3).

Aggregates the five analysis outputs of a run into a structured, human-readable
agronomic report (summary stats + simple recommendations) that the agronomist
reviews before AgriTrack push (Stage 4).
"""
import json
import os

from django.utils import timezone

from agri.models import AnalysisResult, CaptureMeta


def build_report(run, output_path):
    results = {r.kind: r for r in run.results.all()}
    summary = {}

    capture_meta = getattr(run.task, 'capture_meta', None)
    summary["capture_source"] = getattr(capture_meta, 'source', CaptureMeta.DRONE)

    ph = results.get(AnalysisResult.PLANT_HEALTH)
    if ph:
        summary["plant_health_index"] = ph.stats.get("index")
        summary["plant_health_mean"] = ph.stats.get("mean")
    canopy = results.get(AnalysisResult.CANOPY)
    if canopy:
        summary["canopy_pct"] = canopy.stats.get("canopy_pct")
    weed = results.get(AnalysisResult.WEED)
    if weed:
        summary["weed_count"] = weed.stats.get("weed_count")
    grid = results.get(AnalysisResult.GRID)
    if grid:
        summary["grid_cells"] = grid.stats.get("cell_count")
    rgb = results.get(AnalysisResult.RGB_INDEX)
    if rgb:
        summary["rgb_indices"] = {k: v.get("mean") for k, v in rgb.stats.items()
                                  if isinstance(v, dict) and "mean" in v}

    recommendations = []
    cp = summary.get("canopy_pct")
    if cp is not None and cp < 50:
        recommendations.append(
            "Low canopy cover (%.1f%%) — check crop establishment / consider replanting." % cp)
    wc = summary.get("weed_count")
    if wc:
        recommendations.append(
            "%d weed hotspot(s) detected — consider targeted herbicide application." % wc)
    if not recommendations:
        recommendations.append("No critical anomalies detected from automated analysis.")

    report = {
        "run_id": run.id,
        "capture": str(run.task_id),
        "boundary": run.boundary_id,
        "index_used": run.index_used,
        "generated_at": timezone.now().isoformat(),
        "summary": summary,
        "recommendations": recommendations,
    }

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w") as f:
        json.dump(report, f, indent=2)

    return output_path, {"summary": summary, "recommendations": recommendations}
