import datetime

from celery.utils.log import get_task_logger

import worker
from worker.celery import app
from agri.models import AnalysisRun
from agri.services import execute_analysis
from webodm import settings

logger = get_task_logger("app.logger")

# Under TESTING, CELERY_TASK_ALWAYS_EAGER runs tasks synchronously but does NOT
# write their result to the real Celery/Redis backend (task_store_eager_result
# defaults to False) -- so a task's own result would be invisible to
# /api/workers/check|get/<id> polling in tests even though the task itself
# already ran. worker/tasks.py's own pollable tasks (export_raster, ...) work
# around this with the same pattern: explicitly write into a mock backend under
# TESTING. Mirrored here so pull_satellite_capture/pull_satellite_comparison are
# pollable in both production and tests.
TestSafeAsyncResult = worker.celery.MockAsyncResult if settings.TESTING else app.AsyncResult


@app.task
def run_analysis(run_id):
    try:
        run = AnalysisRun.objects.get(pk=run_id)
    except AnalysisRun.DoesNotExist:
        logger.warning("AnalysisRun %s no longer exists" % run_id)
        return
    execute_analysis(run)


@app.task(bind=True, max_retries=3)
def push_analysis(self, run_id):
    from agri.push import push_analysis as do_push
    try:
        run = AnalysisRun.objects.get(pk=run_id)
    except AnalysisRun.DoesNotExist:
        logger.warning("AnalysisRun %s no longer exists" % run_id)
        return
    try:
        do_push(run)
    except Exception as e:
        logger.warning("AgriTrack push failed for run %s: %s" % (run_id, str(e)))
        raise self.retry(exc=e, countdown=30)


@app.task(bind=True)
def pull_satellite_capture(self, project_id, date_from_iso, date_to_iso, name=None, field_id=None):
    """
    Fetch satellite imagery for a farm's onboarded boundary (or, when field_id
    is given, just that one field -- stage-10-sentinel-roadmap.md Phase 1) and
    import it as a Capture. Result shape matches app/api/workers.py's
    CheckTask/GetTaskResult contract so the frontend can poll
    /api/workers/check|get/<celery_task_id> exactly like any other long-running
    WebODM task -- no bespoke polling endpoint needed.
    """
    from agri.remote_sense.sentinel_pull import (pull_satellite_capture as do_pull,
                                                 SatellitePullError)
    try:
        date_from = datetime.date.fromisoformat(date_from_iso)
        date_to = datetime.date.fromisoformat(date_to_iso)
        task = do_pull(project_id, date_from, date_to, name=name, field_id=field_id)
        result = {'output': {'task_id': str(task.id), 'project_id': task.project_id}}
    except SatellitePullError as e:
        result = {'error': str(e)}
    except Exception as e:
        logger.exception("Satellite imagery pull failed for project %s" % project_id)
        result = {'error': 'Unexpected error fetching satellite imagery: %s' % str(e)}

    if settings.TESTING:
        TestSafeAsyncResult.set(self.request.id, result)
    return result


@app.task(bind=True)
def pull_satellite_comparison(self, boundary_id):
    """
    Fetch Sentinel's own computed index for an approved boundary and record it as
    a comparison AnalysisRun (stage-9-satellite-monitoring.md §9). Result shape
    matches app/api/workers.py's polling contract, same as pull_satellite_capture.
    """
    from agri.remote_sense.sentinel_pull import (pull_satellite_comparison as do_pull,
                                                 SatellitePullError)
    try:
        run = do_pull(boundary_id)
        result = {'output': {'run_id': run.id}}
    except SatellitePullError as e:
        result = {'error': str(e)}
    except Exception as e:
        logger.exception("Satellite comparison pull failed for boundary %s" % boundary_id)
        result = {'error': 'Unexpected error fetching satellite reference data: %s' % str(e)}

    if settings.TESTING:
        TestSafeAsyncResult.set(self.request.id, result)
    return result
