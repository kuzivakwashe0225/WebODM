"""
Analysis orchestration (Stage 3 fan-out).

Runs all analysis services over the approved boundary, aggregates a report, and
marks the run PENDING_REVIEW.

NOTE (acknowledged implementation choice): the services run **sequentially**
within the single `run_analysis` Celery task. The agreed plan describes a Celery
`chord(group(...))(report_builder)` — that is a *parallelism* optimization only;
the functional outcome here (six results + report + PENDING_REVIEW, same
architecture and outputs) is identical. Parallelising via a real chord can be
added later without changing the model or API. See precise-agric/progress-log.md.
"""
import os

from django.utils import timezone

from agri.models import AnalysisRun, AnalysisResult
from agri.analysis.plant_health import compute_plant_health
from agri.analysis.rgb_index import compute_rgb_index
from agri.analysis.grid import compute_grid_analysis
from agri.analysis.canopy import compute_canopy_cover
from agri.analysis.weed import compute_weed_mapping
from agri.analysis.report import build_report


def execute_analysis(run):
    run.status = AnalysisRun.RUNNING
    run.save(update_fields=['status'])

    task = run.task
    base = os.path.join("agri", "analysis_%s" % run.id)

    def path(*parts):
        return task.assets_path(base, *parts)

    def rel(*parts):
        return os.path.join(base, *parts)

    try:
        # 1. Plant health (also sets index_used)
        formula, ph_stats = compute_plant_health(task, run.boundary, path("plant_health.tif"))
        AnalysisResult.objects.create(run=run, kind=AnalysisResult.PLANT_HEALTH,
                                      asset_path=rel("plant_health.tif"), stats=ph_stats)
        run.index_used = formula

        # 2. RGB indices (ExG / VARI / GLI)
        rgb_raster, rgb_stats = compute_rgb_index(task, run.boundary, path("rgb"))
        AnalysisResult.objects.create(run=run, kind=AnalysisResult.RGB_INDEX,
                                      asset_path=rel("rgb", "exg.tif") if rgb_raster else "",
                                      stats=rgb_stats)

        # 3. Grid analysis (zonal cells)
        _, grid_stats = compute_grid_analysis(task, run.boundary, path("grid.geojson"))
        AnalysisResult.objects.create(run=run, kind=AnalysisResult.GRID,
                                      asset_path=rel("grid.geojson"), stats=grid_stats)

        # 4. Canopy cover
        _, canopy_stats = compute_canopy_cover(task, run.boundary, path("canopy.tif"))
        AnalysisResult.objects.create(run=run, kind=AnalysisResult.CANOPY,
                                      asset_path=rel("canopy.tif"), stats=canopy_stats)

        # 5. Weed mapping
        _, weed_stats = compute_weed_mapping(task, run.boundary, path("weeds.geojson"))
        AnalysisResult.objects.create(run=run, kind=AnalysisResult.WEED,
                                      asset_path=rel("weeds.geojson"), stats=weed_stats)

        # 6. Report (aggregates the five above)
        _, report_stats = build_report(run, path("report.json"))
        AnalysisResult.objects.create(run=run, kind=AnalysisResult.REPORT,
                                      asset_path=rel("report.json"), stats=report_stats)

        run.status = AnalysisRun.PENDING_REVIEW
        run.completed_at = timezone.now()
        run.save()
    except Exception as e:
        run.status = AnalysisRun.FAILED
        run.error = str(e)
        run.save()
        raise

    return run
