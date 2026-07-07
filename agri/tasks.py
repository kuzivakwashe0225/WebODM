from celery.utils.log import get_task_logger

from worker.celery import app
from agri.models import AnalysisRun
from agri.services import execute_analysis

logger = get_task_logger("app.logger")


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
