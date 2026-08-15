"""
AgriTrack push module (Stage 4).

On agronomist approval of an AnalysisRun, POSTs the aggregated report + result
metadata to the AgriTrack mobile API (settings.AGRITRACK_PUSH_URL). Follows the
tasknotification pattern of an outbound webhook on an app event. The URL is
None by default (push disabled / stub) — set WO_AGRITRACK_PUSH_URL or override
in settings to enable.
"""
import logging

import requests

from webodm import settings
from agri.models import AnalysisResult

logger = logging.getLogger('app.logger')


def build_push_payload(run):
    report = run.results.filter(kind=AnalysisResult.REPORT).first()
    return {
        'run_id': run.id,
        'capture': str(run.task_id),
        'farm': {'id': run.task.project_id, 'name': run.task.project.name},
        'boundary': run.boundary_id,
        'index_used': run.index_used,
        'approved_by': run.reviewed_by.username if run.reviewed_by else None,
        'completed_at': run.completed_at.isoformat() if run.completed_at else None,
        'report': report.stats if report else {},
        'results': [
            {'kind': r.kind, 'asset_path': r.asset_path, 'stats': r.stats}
            for r in run.results.all()
        ],
    }


def push_analysis(run, timeout=10):
    """
    POST the approved run to AgriTrack. If the run's boundary is linked to a
    synced AgriField, uses AgriTrack's real, currently-live results contract
    (POST /orthophoto/analysis/push -- see agri/agritrack/results.py). Otherwise
    falls back to the original generic webhook (AGRITRACK_PUSH_URL) for
    non-AgriTrack boundaries, which have no farmId/fieldId to report.
    Returns True if delivered, False if disabled/not applicable.
    """
    from agri.agritrack.results import resolve_agri_field, push_orthophoto_results
    if resolve_agri_field(run.boundary) is not None:
        return push_orthophoto_results(run, timeout=timeout)

    url = getattr(settings, 'AGRITRACK_PUSH_URL', None)
    if not url:
        logger.info("AGRITRACK_PUSH_URL not set; skipping push for run %s" % run.id)
        return False

    payload = build_push_payload(run)
    r = requests.post(url, json=payload, timeout=timeout)
    r.raise_for_status()
    logger.info("Pushed analysis run %s to AgriTrack (%s)" % (run.id, url))
    return True
