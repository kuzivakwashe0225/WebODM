import os
import shutil
import subprocess
import time
from unittest import mock

import worker
from django.contrib.auth.models import User, Group
from django.contrib.gis.geos import Polygon
from guardian.shortcuts import assign_perm
from rest_framework import status
from rest_framework.test import APIClient

from app.models import Project, Task, Setting, Theme
from app.tests.classes import BootTestCase, BootTransactionTestCase
from app.tests.utils import start_processing_node, clear_test_media_root
from app.branding import apply_branding, THEME_COLORS
from agri.models import Boundary
from nodeodm import status_codes
from nodeodm.models import ProcessingNode
from webodm import settings

GEOJSON_POLY = {
    "type": "Polygon",
    "coordinates": [[[0.0, 0.0], [0.0, 1.0], [1.0, 1.0], [1.0, 0.0], [0.0, 0.0]]],
}


def _poly():
    return Polygon(((0, 0), (0, 1), (1, 1), (1, 0), (0, 0)), srid=4326)


class TestAgri(BootTestCase):
    """Stage 0 (branding) + Stage 1 (Boundary API). One class => boot() runs once."""

    def setUp(self):
        super().setUp()
        from agri.roles import ROLE_TECHNICIAN, ROLE_AGRONOMIST

        self.user = User.objects.get(username="testuser")
        self.other = User.objects.get(username="testuser2")
        self.project = Project.objects.get(owner=self.user)
        self.task = Task.objects.create(project=self.project, name="Capture 1")

        # Roles (groups are seeded by boot() via agri.roles.setup_roles)
        self.user.groups.add(Group.objects.get(name=ROLE_TECHNICIAN))
        self.agronomist = User.objects.create_user(username="agrouser",
                                                   email="agro@test.com",
                                                   password="test1234")
        self.agronomist.groups.add(Group.objects.get(name=ROLE_AGRONOMIST))

    # ---- Boundary API ----

    def test_boundary_requires_auth(self):
        client = APIClient()
        self.assertEqual(client.get("/api/agri/boundaries/").status_code,
                         status.HTTP_403_FORBIDDEN)
        self.assertEqual(client.post("/api/agri/boundaries/", {}, format="json").status_code,
                         status.HTTP_403_FORBIDDEN)

    def test_boundary_create_list_approve(self):
        client = APIClient()
        client.login(username="testuser", password="test1234")

        res = client.post("/api/agri/boundaries/", {
            "task": str(self.task.id),
            "name": "Field A",
            "geom": GEOJSON_POLY,
        }, format="json")
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        self.assertEqual(res.data["status"], Boundary.DRAFT)
        self.assertEqual(res.data["geom"]["type"], "Polygon")
        bid = res.data["id"]
        b = Boundary.objects.get(pk=bid)
        self.assertEqual(b.created_by, self.user)
        self.assertEqual(b.geom.geom_type, "Polygon")

        res = client.get("/api/agri/boundaries/")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertIn(bid, [x["id"] for x in res.data])

        # Technician (creator) CANNOT approve — reviewers only (Stage 5)
        res = client.post("/api/agri/boundaries/%s/approve/" % bid)
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)

        # Agronomist approves -> APPROVED, approved_by/at populated
        client.login(username="agrouser", password="test1234")
        res = client.post("/api/agri/boundaries/%s/approve/" % bid)
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(res.data["status"], Boundary.APPROVED)
        b.refresh_from_db()
        self.assertEqual(b.approved_by, self.agronomist)
        self.assertIsNotNone(b.approved_at)

    def test_boundary_visibility_scoped_to_own_projects(self):
        b = Boundary.objects.create(task=self.task, name="Field A",
                                    geom=_poly(), created_by=self.user)
        client = APIClient()

        client.login(username="testuser2", password="test1234")
        res = client.get("/api/agri/boundaries/")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertNotIn(b.id, [x["id"] for x in res.data])

        client.login(username="testuser", password="test1234")
        res = client.get("/api/agri/boundaries/")
        self.assertIn(b.id, [x["id"] for x in res.data])

    def test_boundary_filter_by_task(self):
        b1 = Boundary.objects.create(task=self.task, geom=_poly(), created_by=self.user)
        other_task = Task.objects.create(project=self.project, name="Capture 2")
        Boundary.objects.create(task=other_task, geom=_poly(), created_by=self.user)

        client = APIClient()
        client.login(username="testuser", password="test1234")
        res = client.get("/api/agri/boundaries/?task=%s" % self.task.id)
        ids = [x["id"] for x in res.data]
        self.assertIn(b1.id, ids)
        self.assertEqual(len(ids), 1)

    def test_boundary_model_defaults(self):
        b = Boundary.objects.create(task=self.task, geom=_poly())
        self.assertEqual(b.status, Boundary.DRAFT)
        self.assertIn("DRAFT", str(b))

    # ---- Capture import (external-import of a lone orthophoto) ----

    def test_capture_import_from_orthophoto(self):
        from agri.capture import create_capture_from_orthophoto

        task = create_capture_from_orthophoto(
            self.project, "app/fixtures/orthophoto.tif", name="Ortho Capture", dispatch=False)

        # Created as an external import, no processing node involved
        self.assertEqual(task.import_url, "file://external")
        self.assertIsNone(task.processing_node)
        self.assertFalse(task.auto_processing_node)
        # File placed at the canonical asset path before processing
        self.assertTrue(os.path.exists(task.assets_path(Task.ASSETS_MAP["orthophoto.tif"])))

        # Drive processing synchronously (Celery is eager under TESTING)
        worker.tasks.process_task(task.id)
        task.refresh_from_db()

        # Completed with extent populated, still no node
        self.assertEqual(task.status, status_codes.COMPLETED)
        self.assertIsNotNone(task.orthophoto_extent)
        self.assertIsNone(task.processing_node)

    def test_capture_upload_endpoint(self):
        client = APIClient()

        # Auth required
        with open("app/fixtures/orthophoto.tif", "rb") as f:
            res = client.post("/api/agri/captures/",
                              {"project": self.project.id, "orthophoto": f}, format="multipart")
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)

        # Authenticated upload creates an external-import capture that completes
        client.login(username="testuser", password="test1234")
        with open("app/fixtures/orthophoto.tif", "rb") as f:
            res = client.post("/api/agri/captures/",
                              {"project": self.project.id, "orthophoto": f, "name": "Uploaded Capture"},
                              format="multipart")
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)

        task = Task.objects.get(id=res.data["id"])
        self.assertEqual(task.import_url, "file://external")
        self.assertEqual(task.name, "Uploaded Capture")
        task.refresh_from_db()
        self.assertEqual(task.status, status_codes.COMPLETED)

    def test_capture_upload_requires_file(self):
        client = APIClient()
        client.login(username="testuser", password="test1234")
        res = client.post("/api/agri/captures/", {"project": self.project.id}, format="multipart")
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)

    # ---- Analysis models (Stage 2) ----

    def test_analysis_run_and_result(self):
        from agri.models import AnalysisRun, AnalysisResult
        boundary = Boundary.objects.create(task=self.task, geom=_poly(),
                                           status=Boundary.APPROVED, created_by=self.user)
        run = AnalysisRun.objects.create(task=self.task, boundary=boundary, triggered_by=self.user)
        self.assertEqual(run.status, AnalysisRun.PENDING)
        self.assertIn("PENDING", str(run))

        result = AnalysisResult.objects.create(run=run, kind=AnalysisResult.PLANT_HEALTH,
                                               stats={"mean": 0.42})
        self.assertEqual(run.results.count(), 1)
        self.assertEqual(run.results.first().stats["mean"], 0.42)
        self.assertIn("plant_health", str(result))

    def test_plant_health_compute(self):
        from agri.capture import create_capture_from_orthophoto
        from agri.analysis.plant_health import compute_plant_health

        # Import a capture from the RGB fixture and process it to COMPLETED
        task = create_capture_from_orthophoto(
            self.project, "app/fixtures/orthophoto.tif", name="PH Capture", dispatch=False)
        worker.tasks.process_task(task.id)
        task.refresh_from_db()
        self.assertEqual(task.status, status_codes.COMPLETED)
        self.assertIsNotNone(task.orthophoto_extent)

        # Approved boundary = the full ortho extent (guaranteed overlap)
        boundary = Boundary.objects.create(task=task, geom=task.orthophoto_extent,
                                           status=Boundary.APPROVED, created_by=self.user)

        output = task.assets_path("agri", "plant_health.tif")
        formula, stats = compute_plant_health(task, boundary, output)

        # RGB fixture -> ExG; writes a raster; produces real stats over the field
        self.assertEqual(formula, "EXG")
        self.assertTrue(os.path.exists(output))
        self.assertIsNotNone(stats["mean"])
        self.assertGreater(stats["count"], 0)

    def _completed_capture(self, name="Capture"):
        from agri.capture import create_capture_from_orthophoto
        task = create_capture_from_orthophoto(
            self.project, "app/fixtures/orthophoto.tif", name=name, dispatch=False)
        worker.tasks.process_task(task.id)
        task.refresh_from_db()
        return task

    def test_run_analysis_orchestration(self):
        from agri.services import execute_analysis
        from agri.models import AnalysisRun, AnalysisResult

        task = self._completed_capture("Run Capture")
        boundary = Boundary.objects.create(task=task, geom=task.orthophoto_extent,
                                           status=Boundary.APPROVED, created_by=self.user)
        run = AnalysisRun.objects.create(task=task, boundary=boundary, triggered_by=self.user)

        execute_analysis(run)
        run.refresh_from_db()

        self.assertEqual(run.status, AnalysisRun.PENDING_REVIEW)
        self.assertEqual(run.index_used, "EXG")
        self.assertIsNotNone(run.completed_at)

        # Full 6-service fan-out produced all results
        kinds = set(run.results.values_list('kind', flat=True))
        self.assertEqual(kinds, {
            AnalysisResult.PLANT_HEALTH, AnalysisResult.RGB_INDEX, AnalysisResult.GRID,
            AnalysisResult.CANOPY, AnalysisResult.WEED, AnalysisResult.REPORT,
        })

        # Report aggregates the others
        report = run.results.get(kind=AnalysisResult.REPORT)
        self.assertIn("summary", report.stats)
        self.assertIn("plant_health_mean", report.stats["summary"])

        # Plant-health raster written on disk
        ph = run.results.get(kind=AnalysisResult.PLANT_HEALTH)
        self.assertTrue(os.path.exists(task.assets_path(ph.asset_path)))

    # ---- Stage 9: satellite-source gating in the analysis fan-out ----

    def _completed_capture_with_source(self, name, source):
        from agri.capture import create_capture_from_orthophoto
        task = create_capture_from_orthophoto(
            self.project, "app/fixtures/orthophoto.tif", name=name, dispatch=False, source=source)
        worker.tasks.process_task(task.id)
        task.refresh_from_db()
        return task

    def test_capture_meta_source_defaults_to_drone(self):
        from agri.models import CaptureMeta
        task = self._completed_capture("Default Source Capture")
        self.assertEqual(task.capture_meta.source, CaptureMeta.DRONE)

    def test_weed_mapping_skipped_for_satellite_capture(self):
        from agri.services import execute_analysis
        from agri.models import AnalysisRun, AnalysisResult, CaptureMeta

        task = self._completed_capture_with_source("Satellite Cap", CaptureMeta.SATELLITE)
        boundary = Boundary.objects.create(task=task, geom=task.orthophoto_extent,
                                           status=Boundary.APPROVED, created_by=self.user)
        run = AnalysisRun.objects.create(task=task, boundary=boundary, triggered_by=self.user)
        execute_analysis(run)

        weed = run.results.get(kind=AnalysisResult.WEED)
        self.assertTrue(weed.stats.get("skipped"))
        self.assertNotIn("weed_count", weed.stats)
        self.assertEqual(weed.asset_path, "")

        # A drone capture on the same fixture still gets a real weed run.
        drone_task = self._completed_capture("Drone Cap")
        drone_boundary = Boundary.objects.create(task=drone_task, geom=drone_task.orthophoto_extent,
                                                  status=Boundary.APPROVED, created_by=self.user)
        drone_run = AnalysisRun.objects.create(task=drone_task, boundary=drone_boundary,
                                               triggered_by=self.user)
        execute_analysis(drone_run)
        drone_weed = drone_run.results.get(kind=AnalysisResult.WEED)
        self.assertNotIn("skipped", drone_weed.stats)
        self.assertIn("weed_count", drone_weed.stats)

    def test_canopy_flagged_low_resolution_proxy_for_satellite(self):
        from agri.services import execute_analysis
        from agri.models import AnalysisRun, AnalysisResult, CaptureMeta

        task = self._completed_capture_with_source("Satellite Canopy Cap", CaptureMeta.SATELLITE)
        boundary = Boundary.objects.create(task=task, geom=task.orthophoto_extent,
                                           status=Boundary.APPROVED, created_by=self.user)
        run = AnalysisRun.objects.create(task=task, boundary=boundary, triggered_by=self.user)
        execute_analysis(run)

        canopy = run.results.get(kind=AnalysisResult.CANOPY)
        self.assertTrue(canopy.stats.get("low_resolution_proxy"))

        drone_task = self._completed_capture("Drone Canopy Cap")
        drone_boundary = Boundary.objects.create(task=drone_task, geom=drone_task.orthophoto_extent,
                                                  status=Boundary.APPROVED, created_by=self.user)
        drone_run = AnalysisRun.objects.create(task=drone_task, boundary=drone_boundary,
                                               triggered_by=self.user)
        execute_analysis(drone_run)
        drone_canopy = drone_run.results.get(kind=AnalysisResult.CANOPY)
        self.assertNotIn("low_resolution_proxy", drone_canopy.stats)

    def test_report_includes_capture_source(self):
        from agri.services import execute_analysis
        from agri.models import AnalysisRun, AnalysisResult, CaptureMeta

        task = self._completed_capture_with_source("Satellite Report Cap", CaptureMeta.SATELLITE)
        boundary = Boundary.objects.create(task=task, geom=task.orthophoto_extent,
                                           status=Boundary.APPROVED, created_by=self.user)
        run = AnalysisRun.objects.create(task=task, boundary=boundary, triggered_by=self.user)
        execute_analysis(run)

        report = run.results.get(kind=AnalysisResult.REPORT)
        self.assertEqual(report.stats["summary"]["capture_source"], CaptureMeta.SATELLITE)

    # ---- Stage 10 Phase 3: satellite eligibility table ----

    def test_satellite_eligible_table_covers_all_analysis_kinds(self):
        from agri.services import SATELLITE_ELIGIBLE
        from agri.models import AnalysisResult

        all_kinds = {kind for kind, _label in AnalysisResult.KIND_CHOICES}
        self.assertEqual(set(SATELLITE_ELIGIBLE.keys()), all_kinds)
        self.assertEqual(SATELLITE_ELIGIBLE[AnalysisResult.WEED], False)
        self.assertEqual(SATELLITE_ELIGIBLE[AnalysisResult.CANOPY], 'proxy')
        for kind in (AnalysisResult.PLANT_HEALTH, AnalysisResult.RGB_INDEX,
                     AnalysisResult.GRID, AnalysisResult.REPORT):
            self.assertEqual(SATELLITE_ELIGIBLE[kind], True)

    def test_analysis_trigger_requires_approved_boundary(self):
        from agri.models import AnalysisRun

        task = self._completed_capture("Gate Capture")
        boundary = Boundary.objects.create(task=task, geom=task.orthophoto_extent,
                                           status=Boundary.DRAFT, created_by=self.user)

        client = APIClient()
        client.login(username="testuser", password="test1234")

        # DRAFT boundary -> analysis is gated
        res = client.post("/api/agri/analysis/", {"boundary": boundary.id}, format="json")
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)

        # Approve, then it runs (eager) to PENDING_REVIEW
        boundary.status = Boundary.APPROVED
        boundary.save()
        res = client.post("/api/agri/analysis/", {"boundary": boundary.id}, format="json")
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        run = AnalysisRun.objects.get(id=res.data["id"])
        self.assertEqual(run.status, AnalysisRun.PENDING_REVIEW)

    # ---- Stage 3 services ----

    def test_rgb_index_compute(self):
        from agri.analysis.rgb_index import compute_rgb_index
        task = self._completed_capture("RGB Capture")
        boundary = Boundary.objects.create(task=task, geom=task.orthophoto_extent,
                                           status=Boundary.APPROVED, created_by=self.user)
        raster, stats = compute_rgb_index(task, boundary, task.assets_path("agri", "rgbtest"))
        for idx in ("EXG", "VARI", "GLI"):
            self.assertIn(idx, stats)
        self.assertIsNotNone(stats["EXG"].get("mean"))
        self.assertTrue(os.path.exists(raster))

    def test_canopy_cover_compute(self):
        from agri.analysis.canopy import compute_canopy_cover
        task = self._completed_capture("Canopy Capture")
        boundary = Boundary.objects.create(task=task, geom=task.orthophoto_extent,
                                           status=Boundary.APPROVED, created_by=self.user)
        output = task.assets_path("agri", "canopy", "canopy.tif")
        raster, stats = compute_canopy_cover(task, boundary, output)
        self.assertTrue(os.path.exists(output))
        self.assertIsNotNone(stats["canopy_pct"])
        self.assertGreaterEqual(stats["canopy_pct"], 0.0)
        self.assertLessEqual(stats["canopy_pct"], 100.0)

    def test_grid_analysis_compute(self):
        import json
        from agri.analysis.grid import compute_grid_analysis
        task = self._completed_capture("Grid Capture")
        boundary = Boundary.objects.create(task=task, geom=task.orthophoto_extent,
                                           status=Boundary.APPROVED, created_by=self.user)
        output = task.assets_path("agri", "grid", "grid.geojson")
        path, stats = compute_grid_analysis(task, boundary, output, cell_size_m=10.0)
        self.assertTrue(os.path.exists(output))
        self.assertGreaterEqual(stats["cell_count"], 1)
        with open(output) as f:
            gj = json.load(f)
        self.assertEqual(gj["type"], "FeatureCollection")

    def test_weed_mapping_compute(self):
        from agri.analysis.weed import compute_weed_mapping
        task = self._completed_capture("Weed Capture")
        boundary = Boundary.objects.create(task=task, geom=task.orthophoto_extent,
                                           status=Boundary.APPROVED, created_by=self.user)
        output = task.assets_path("agri", "weed", "weeds.geojson")
        path, stats = compute_weed_mapping(task, boundary, output)
        self.assertTrue(os.path.exists(output))
        self.assertIn("weed_count", stats)
        self.assertGreaterEqual(stats["weed_count"], 0)

    # ---- Stage 4: agronomist review -> AgriTrack push ----

    def _pending_review_run(self, name="Review Capture", execute=True):
        from agri.models import AnalysisRun
        from agri.services import execute_analysis
        task = self._completed_capture(name)
        boundary = Boundary.objects.create(task=task, geom=task.orthophoto_extent,
                                           status=Boundary.APPROVED, created_by=self.user)
        run = AnalysisRun.objects.create(task=task, boundary=boundary, triggered_by=self.user)
        if execute:
            execute_analysis(run)
        else:
            run.status = AnalysisRun.PENDING_REVIEW
            run.save()
        run.refresh_from_db()
        return run

    def test_analysis_review_approve_and_push(self):
        from agri.models import AnalysisRun
        run = self._pending_review_run("Approve Capture", execute=True)

        client = APIClient()
        client.login(username="agrouser", password="test1234")

        old_url = getattr(settings, 'AGRITRACK_PUSH_URL', None)
        settings.AGRITRACK_PUSH_URL = 'http://agritrack.test/api/push'
        try:
            with mock.patch('agri.push.requests.post') as mpost:
                mpost.return_value.status_code = 200
                res = client.post("/api/agri/analysis/%s/approve/" % run.id)
                self.assertEqual(res.status_code, status.HTTP_200_OK)
                self.assertEqual(res.data["status"], AnalysisRun.APPROVED)

                # Push fired once, with the aggregated report in the payload
                mpost.assert_called_once()
                payload = mpost.call_args.kwargs["json"]
                self.assertEqual(payload["run_id"], run.id)
                self.assertIn("summary", payload["report"])
                self.assertEqual(len(payload["results"]), 6)
        finally:
            settings.AGRITRACK_PUSH_URL = old_url

        run.refresh_from_db()
        self.assertEqual(run.status, AnalysisRun.APPROVED)
        self.assertEqual(run.reviewed_by, self.agronomist)

    def test_analysis_review_reject(self):
        from agri.models import AnalysisRun
        run = self._pending_review_run("Reject Capture", execute=False)

        client = APIClient()
        client.login(username="agrouser", password="test1234")

        old_url = getattr(settings, 'AGRITRACK_PUSH_URL', None)
        settings.AGRITRACK_PUSH_URL = 'http://agritrack.test/api/push'
        try:
            with mock.patch('agri.push.requests.post') as mpost:
                res = client.post("/api/agri/analysis/%s/reject/" % run.id)
                self.assertEqual(res.status_code, status.HTTP_200_OK)
                self.assertEqual(res.data["status"], AnalysisRun.REJECTED)
                # No push on rejection
                mpost.assert_not_called()
        finally:
            settings.AGRITRACK_PUSH_URL = old_url

        run.refresh_from_db()
        self.assertEqual(run.reviewed_by, self.agronomist)

    def test_analysis_review_requires_reviewer(self):
        run = self._pending_review_run("Reviewer Gate Capture", execute=False)
        client = APIClient()
        client.login(username="testuser", password="test1234")  # Technician
        res = client.post("/api/agri/analysis/%s/approve/" % run.id)
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)

    def test_analysis_review_requires_pending_review(self):
        from agri.models import AnalysisRun
        task = self._completed_capture("Pending Gate Capture")
        boundary = Boundary.objects.create(task=task, geom=task.orthophoto_extent,
                                           status=Boundary.APPROVED, created_by=self.user)
        run = AnalysisRun.objects.create(task=task, boundary=boundary,
                                         triggered_by=self.user)  # still PENDING

        client = APIClient()
        client.login(username="agrouser", password="test1234")
        res = client.post("/api/agri/analysis/%s/approve/" % run.id)
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)

    def test_push_skipped_without_url(self):
        from agri.models import AnalysisRun
        from agri.push import push_analysis
        run = self._pending_review_run("No URL Capture", execute=False)

        old_url = getattr(settings, 'AGRITRACK_PUSH_URL', None)
        settings.AGRITRACK_PUSH_URL = None
        try:
            with mock.patch('agri.push.requests.post') as mpost:
                self.assertFalse(push_analysis(run))
                mpost.assert_not_called()
        finally:
            settings.AGRITRACK_PUSH_URL = old_url

    # ---- AgriTrack live results push (POST /orthophoto/analysis/push) ----

    def test_classify_plant_health_ndvi(self):
        from agri.agritrack.results import classify_plant_health, GOOD, FAIR, POOR

        cls, score = classify_plant_health("NDVI", 0.7)
        self.assertEqual(cls, GOOD)
        self.assertGreater(score, 0.5)

        cls, score = classify_plant_health("NDVI", 0.3)
        self.assertEqual(cls, FAIR)

        cls, score = classify_plant_health("NDVI", 0.05)
        self.assertEqual(cls, POOR)
        self.assertGreaterEqual(score, 0.0)

    def test_classify_plant_health_exg(self):
        from agri.agritrack.results import classify_plant_health, GOOD, FAIR, POOR

        self.assertEqual(classify_plant_health("EXG", 40)[0], GOOD)
        self.assertEqual(classify_plant_health("EXG", 10)[0], FAIR)
        self.assertEqual(classify_plant_health("EXG", 0)[0], POOR)
        self.assertEqual(classify_plant_health("EXG", None), (None, None))

    def _agri_linked_run(self, name="AgriTrack Capture"):
        """A PENDING_REVIEW run whose boundary is linked to a synced AgriField."""
        from agri.models import AgriField, AnalysisRun
        from agri.services import execute_analysis
        import copy

        old_key = getattr(settings, 'AGRITRACK_INBOUND_API_KEY', None)
        settings.AGRITRACK_INBOUND_API_KEY = "test-shared-secret"
        try:
            client = APIClient()
            payload = copy.deepcopy(FARM_PAYLOAD)
            payload["farmId"] = 555
            payload["fields"][0]["fieldId"] = 556
            res = client.post(SYNC_URL, payload, format="json", HTTP_X_API_KEY="test-shared-secret")
            assert res.status_code == 200, "sync setup failed: %s %s" % (res.status_code, res.data)
        finally:
            settings.AGRITRACK_INBOUND_API_KEY = old_key
        agri_field = AgriField.objects.get(agritrack_field_id=556)

        task = self._completed_capture(name)
        boundary = Boundary.objects.create(task=task, geom=task.orthophoto_extent,
                                           status=Boundary.APPROVED, created_by=self.user,
                                           agri_field=agri_field)
        run = AnalysisRun.objects.create(task=task, boundary=boundary, triggered_by=self.user)
        execute_analysis(run)
        run.refresh_from_db()
        return run, agri_field

    def test_orthophoto_result_payload_maps_our_metrics(self):
        from agri.agritrack.results import build_orthophoto_result_payload

        run, agri_field = self._agri_linked_run()
        payload = build_orthophoto_result_payload(run)

        self.assertEqual(payload["sourceSystem"], "orthophoto")
        self.assertEqual(payload["fieldId"], agri_field.agritrack_field_id)
        self.assertEqual(payload["farmId"], agri_field.farm.agritrack_farm_id)
        self.assertEqual(payload["scope"], "field")
        self.assertIsNone(payload["subPlotId"])
        self.assertEqual(payload["extId"], "webodm-run-%s" % run.id)
        self.assertIn("vari_mean", payload["metrics"])
        self.assertIn("canopy_cover_pct", payload["metrics"])
        self.assertIn("weed_count", payload["metrics"])
        self.assertIn("health_score", payload["metrics"])
        self.assertIn("classification", payload["metrics"])
        self.assertIsInstance(payload["interpretation"]["recommendations"], list)
        # Not computed by this pipeline -- must not be invented
        self.assertNotIn("plant_count", payload["metrics"])
        self.assertNotIn("height_mean_m", payload["metrics"])
        # AgriTrack's live endpoint requires summary as a string, not our
        # internal report dict (contract mismatch caught via manual curl test)
        self.assertIsInstance(payload["interpretation"]["summary"], str)

    def test_resolve_agri_field_falls_back_to_persistent_field_link(self):
        # A boundary whose DIRECT agri_field link is null but whose persistent
        # agri.Field carries the AgriTrack link must still resolve (and push).
        from agri.agritrack.results import resolve_agri_field, build_orthophoto_result_payload
        from agri.models import Field

        run, agri_field = self._agri_linked_run()
        boundary = run.boundary
        field = Field.objects.create(project=boundary.task.project,
                                     name="Persistent field", agri_field=agri_field)
        boundary.agri_field = None
        boundary.field = field
        boundary.save(update_fields=['agri_field', 'field'])

        self.assertEqual(resolve_agri_field(boundary), agri_field)
        payload = build_orthophoto_result_payload(run)
        self.assertEqual(payload["fieldId"], agri_field.agritrack_field_id)

    def test_push_uses_agritrack_live_endpoint_when_field_linked(self):
        from agri.push import push_analysis

        run, agri_field = self._agri_linked_run()

        old_url = getattr(settings, 'AGRITRACK_RESULTS_PUSH_URL', None)
        old_key = getattr(settings, 'AGRITRACK_OUTBOUND_API_KEY', None)
        settings.AGRITRACK_RESULTS_PUSH_URL = 'https://agritrack.test/orthophoto/analysis/push'
        settings.AGRITRACK_OUTBOUND_API_KEY = 'their-issued-key'
        try:
            with mock.patch('agri.agritrack.results.requests.post') as mpost:
                mpost.return_value.status_code = 200
                self.assertTrue(push_analysis(run))
                mpost.assert_called_once()
                args, kwargs = mpost.call_args
                self.assertEqual(args[0], 'https://agritrack.test/orthophoto/analysis/push')
                self.assertEqual(kwargs['headers']['X-Api-Key'], 'their-issued-key')
                self.assertEqual(kwargs['json']['fieldId'], agri_field.agritrack_field_id)
        finally:
            settings.AGRITRACK_RESULTS_PUSH_URL = old_url
            settings.AGRITRACK_OUTBOUND_API_KEY = old_key

    def test_push_skipped_when_agritrack_not_configured(self):
        from agri.push import push_analysis
        run, _ = self._agri_linked_run()

        old_url = getattr(settings, 'AGRITRACK_RESULTS_PUSH_URL', None)
        settings.AGRITRACK_RESULTS_PUSH_URL = None
        try:
            with mock.patch('agri.agritrack.results.requests.post') as mpost:
                self.assertFalse(push_analysis(run))
                mpost.assert_not_called()
        finally:
            settings.AGRITRACK_RESULTS_PUSH_URL = old_url

    # ---- Stage 5: roles & permissions ----

    def test_roles_seeded(self):
        from agri.roles import AGRI_ROLES
        for name in AGRI_ROLES:
            self.assertTrue(Group.objects.filter(name=name).exists(),
                            "role group %s missing" % name)

    def test_reviewer_sees_all_boundaries_and_can_reject(self):
        b = Boundary.objects.create(task=self.task, name="Org Field",
                                    geom=_poly(), created_by=self.user)
        client = APIClient()
        client.login(username="agrouser", password="test1234")

        # Org-wide visibility: agronomist doesn't own the project but sees the boundary
        res = client.get("/api/agri/boundaries/")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertIn(b.id, [x["id"] for x in res.data])

        # And can reject it
        res = client.post("/api/agri/boundaries/%s/reject/" % b.id)
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(res.data["status"], Boundary.REJECTED)

    def test_boundary_create_scoped_to_project_rights(self):
        # testuser2 (no role, not the owner) cannot attach a boundary to testuser's capture
        client = APIClient()
        client.login(username="testuser2", password="test1234")
        res = client.post("/api/agri/boundaries/", {
            "task": str(self.task.id), "name": "Intruder", "geom": GEOJSON_POLY,
        }, format="json")
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)

    def test_agronomist_is_read_only(self):
        boundary = Boundary.objects.create(task=self.task, geom=_poly(),
                                           status=Boundary.APPROVED, created_by=self.user)
        client = APIClient()
        client.login(username="agrouser", password="test1234")

        # Cannot draw boundaries
        res = client.post("/api/agri/boundaries/", {
            "task": str(self.task.id), "name": "AgroDraw", "geom": GEOJSON_POLY,
        }, format="json")
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)

        # Cannot upload captures
        with open("app/fixtures/orthophoto.tif", "rb") as f:
            res = client.post("/api/agri/captures/",
                              {"project": self.project.id, "orthophoto": f},
                              format="multipart")
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)

        # Cannot trigger analysis (read-only), even on an approved boundary
        res = client.post("/api/agri/analysis/", {"boundary": boundary.id}, format="json")
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)

    # ---- Branding ----

    def test_settings_app_name(self):
        self.assertEqual(settings.APP_NAME, "Precise Agric System")

    def test_boot_applies_branding_to_fresh_install(self):
        s = Setting.objects.first()
        self.assertIsNotNone(s)
        self.assertEqual(s.app_name, "Precise Agric System")
        theme = s.theme
        for field, value in THEME_COLORS.items():
            self.assertEqual(getattr(theme, field), value)

    def test_apply_branding_repairs_existing_rows(self):
        theme = Theme.objects.first()
        theme.header_background = "#000000"
        theme.save()
        s = Setting.objects.first()
        s.app_name = "Something Else"
        s.save()

        apply_branding()

        theme.refresh_from_db()
        s.refresh_from_db()
        self.assertEqual(theme.header_background, THEME_COLORS["header_background"])
        self.assertEqual(s.app_name, "Precise Agric System")

    # ---- Stage 8: Field identity, capture date, heatmap tile URL, grid, seasonal ----

    def _analyzed_run(self, field=None, name="Cap"):
        """Completed capture + APPROVED boundary (optionally linked to a Field) + a
        fully executed AnalysisRun. Returns (task, boundary, run)."""
        from agri.services import execute_analysis
        from agri.models import AnalysisRun
        task = self._completed_capture(name)
        boundary = Boundary.objects.create(task=task, geom=task.orthophoto_extent,
                                           status=Boundary.APPROVED, created_by=self.user,
                                           field=field)
        run = AnalysisRun.objects.create(task=task, boundary=boundary, triggered_by=self.user)
        execute_analysis(run)
        run.refresh_from_db()
        return task, boundary, run

    def test_boundary_geom_axis_order_is_lng_lat(self):
        # GDAL 3's geom.geojson swaps EPSG:4326 to [lat, lng]; the API must still
        # return correct [lng, lat] so boundaries land on the orthophoto, not on
        # the far side of the planet. Distinct lng/lat so a swap is detectable.
        asym = {"type": "Polygon", "coordinates": [[
            [30.70, -17.80], [30.72, -17.80], [30.72, -17.78], [30.70, -17.78], [30.70, -17.80]]]}
        client = APIClient()
        client.login(username="testuser", password="test1234")
        res = client.post("/api/agri/boundaries/", {
            "task": str(self.task.id), "name": "Axis", "geom": asym}, format="json")
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        first = res.data["geom"]["coordinates"][0][0]
        self.assertAlmostEqual(first[0], 30.70, places=3)   # lng first
        self.assertAlmostEqual(first[1], -17.80, places=3)  # lat second
        # And on GET
        got = client.get("/api/agri/boundaries/?task=%s" % self.task.id)
        gfirst = [b for b in got.data if b["name"] == "Axis"][0]["geom"]["coordinates"][0][0]
        self.assertAlmostEqual(gfirst[0], 30.70, places=3)

    def test_boundaries_reused_on_subsequent_capture(self):
        from agri.capture import create_capture_from_orthophoto
        from agri.models import Boundary

        # First upload: no prior boundaries -> nothing auto-created. Operator draws
        # a boundary WITHOUT an explicit Field link (the common real-world case).
        cap1 = create_capture_from_orthophoto(self.project, "app/fixtures/orthophoto.tif",
                                              name="Cap1", dispatch=False)
        self.assertEqual(Boundary.objects.filter(task=cap1).count(), 0)
        src = Boundary.objects.create(task=cap1, name="Field A", geom=_poly(),
                                      status=Boundary.APPROVED, created_by=self.user)
        self.assertIsNone(src.field)

        # Second upload: inherited automatically as DRAFT, with a Field auto-created
        # from the boundary name (and back-filled onto the source for the season series).
        cap2 = create_capture_from_orthophoto(self.project, "app/fixtures/orthophoto.tif",
                                              name="Cap2", dispatch=False)
        reused = Boundary.objects.filter(task=cap2)
        self.assertEqual(reused.count(), 1)
        b = reused.first()
        self.assertEqual(b.status, Boundary.DRAFT)
        self.assertEqual(b.geom.extent, _poly().extent)
        self.assertIsNotNone(b.field)
        self.assertEqual(b.field.name, "Field A")
        src.refresh_from_db()
        self.assertEqual(src.field_id, b.field_id)

        # Third upload also reuses (cap2 had no approved analysis, that's fine)
        cap3 = create_capture_from_orthophoto(self.project, "app/fixtures/orthophoto.tif",
                                              name="Cap3", dispatch=False)
        self.assertEqual(Boundary.objects.filter(task=cap3).count(), 1)
        self.assertEqual(Boundary.objects.filter(task=cap3).first().field_id, b.field_id)

    def test_field_model_and_boundary_link(self):
        from agri.models import Field
        f = Field.objects.create(project=self.project, name="North Paddock")
        b = Boundary.objects.create(task=self.task, geom=_poly(), created_by=self.user, field=f)
        self.assertEqual(b.field, f)
        self.assertIn(b, f.boundaries.all())
        # unique_together (project, name)
        with self.assertRaises(Exception):
            Field.objects.create(project=self.project, name="North Paddock")

    def test_capture_meta_created_on_upload_with_date(self):
        import datetime
        from agri.models import CaptureMeta
        client = APIClient()
        client.login(username="testuser", password="test1234")
        with open("app/fixtures/orthophoto.tif", "rb") as fd:
            res = client.post("/api/agri/captures/", {
                "project": self.project.id, "name": "Dated capture",
                "capture_date": "2026-06-01", "orthophoto": fd,
            }, format="multipart")
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        cm = CaptureMeta.objects.get(task_id=res.data["id"])
        self.assertEqual(cm.capture_date, datetime.date(2026, 6, 1))

    def test_capture_meta_defaults_to_today_when_omitted(self):
        import datetime
        from agri.models import CaptureMeta
        client = APIClient()
        client.login(username="testuser", password="test1234")
        with open("app/fixtures/orthophoto.tif", "rb") as fd:
            res = client.post("/api/agri/captures/", {
                "project": self.project.id, "name": "Undated", "orthophoto": fd,
            }, format="multipart")
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        cm = CaptureMeta.objects.get(task_id=res.data["id"])
        self.assertEqual(cm.capture_date, datetime.date.today())

    def test_boundary_create_with_field_name_creates_field(self):
        from agri.models import Field
        client = APIClient()
        client.login(username="testuser", password="test1234")
        res = client.post("/api/agri/boundaries/", {
            "task": str(self.task.id), "name": "Field A",
            "geom": GEOJSON_POLY, "field_name": "West Block",
        }, format="json")
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        b = Boundary.objects.get(pk=res.data["id"])
        self.assertIsNotNone(b.field)
        self.assertEqual(b.field.name, "West Block")
        self.assertEqual(b.field.project, self.project)
        # Same name again -> reuse the same Field (get_or_create)
        res2 = client.post("/api/agri/boundaries/", {
            "task": str(self.task.id), "name": "Field A2",
            "geom": GEOJSON_POLY, "field_name": "West Block",
        }, format="json")
        self.assertEqual(Field.objects.filter(project=self.project, name="West Block").count(), 1)
        self.assertEqual(Boundary.objects.get(pk=res2.data["id"]).field_id, b.field_id)

    def test_boundary_rejects_field_from_other_farm(self):
        from agri.models import Field
        other_project = Project.objects.get(owner=self.other)
        foreign = Field.objects.create(project=other_project, name="Foreign")
        client = APIClient()
        client.login(username="testuser", password="test1234")
        res = client.post("/api/agri/boundaries/", {
            "task": str(self.task.id), "name": "Field A",
            "geom": GEOJSON_POLY, "field": foreign.id,
        }, format="json")
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)

    def test_plant_health_tile_url_present_and_shaped(self):
        _task, _b, run = self._analyzed_run(name="Heatmap cap")
        client = APIClient()
        client.login(username="testuser", password="test1234")
        res = client.get("/api/agri/analysis/%s/" % run.id)
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        url = res.data["plant_health_tile_url"]
        self.assertIsNotNone(url)
        self.assertIn("/tiles/{z}/{x}/{y}", url)
        self.assertIn("color_map=rdylgn", url)
        self.assertIn("formula=", url)
        self.assertIn("boundaries=", url)
        self.assertIn("tasks/%s/" % run.task_id, url)

    def test_plant_health_tile_url_none_before_analysis(self):
        from agri.models import AnalysisRun
        task = self._completed_capture("Pending cap")
        boundary = Boundary.objects.create(task=task, geom=task.orthophoto_extent,
                                           status=Boundary.APPROVED, created_by=self.user)
        run = AnalysisRun.objects.create(task=task, boundary=boundary, triggered_by=self.user)
        from agri.api.serializers import AnalysisRunSerializer
        self.assertIsNone(AnalysisRunSerializer(run).data["plant_health_tile_url"])

    def test_grid_endpoint_returns_featurecollection(self):
        _task, _b, run = self._analyzed_run(name="Grid cap")
        client = APIClient()
        client.login(username="testuser", password="test1234")
        res = client.get("/api/agri/analysis/%s/grid/" % run.id)
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(res.data["type"], "FeatureCollection")
        self.assertTrue(len(res.data["features"]) > 0)
        self.assertIn("mean_index", res.data["features"][0]["properties"])

    def test_seasonal_series_per_field_and_farm(self):
        import datetime
        from agri.models import Field, CaptureMeta
        field = Field.objects.create(project=self.project, name="Season Field")
        _t1, _b1, _r1 = self._analyzed_run(field=field, name="Cap Jun 1")
        _t2, _b2, _r2 = self._analyzed_run(field=field, name="Cap Jun 20")
        # Force two distinct capture dates
        CaptureMeta.objects.filter(task=_t1).update(capture_date=datetime.date(2026, 6, 1))
        CaptureMeta.objects.filter(task=_t2).update(capture_date=datetime.date(2026, 6, 20))

        client = APIClient()
        client.login(username="testuser", password="test1234")
        res = client.get("/api/agri/seasonal/?project=%s" % self.project.id)
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        fields = res.data["fields"]
        self.assertEqual(len(fields), 1)
        series = fields[0]["series"]
        self.assertEqual([p["date"] for p in series], ["2026-06-01", "2026-06-20"])
        self.assertIn("health_mean", series[0])
        # Farm aggregate has both dates
        farm_dates = [p["date"] for p in res.data["farm"]["series"]]
        self.assertEqual(farm_dates, ["2026-06-01", "2026-06-20"])

    def test_seasonal_same_date_drone_and_satellite_dont_overwrite(self):
        # Stage 9 regression: before keying points by (date, source, computed_by),
        # a same-day drone + satellite capture would silently overwrite each other
        # here, and their metrics would get blended into one misleading farm-level
        # average -- see stage-9-satellite-monitoring.md §5.
        import datetime
        from agri.models import Field, CaptureMeta, AnalysisRun

        field = Field.objects.create(project=self.project, name="Mixed Source Field")
        drone_task, _b1, _r1 = self._analyzed_run(field=field, name="Drone Same Day")

        sat_task = self._completed_capture_with_source("Satellite Same Day", CaptureMeta.SATELLITE)
        sat_boundary = Boundary.objects.create(task=sat_task, geom=sat_task.orthophoto_extent,
                                               status=Boundary.APPROVED, created_by=self.user,
                                               field=field)
        sat_run = AnalysisRun.objects.create(task=sat_task, boundary=sat_boundary,
                                             triggered_by=self.user)
        from agri.services import execute_analysis
        execute_analysis(sat_run)

        same_day = datetime.date(2026, 6, 15)
        CaptureMeta.objects.filter(task=drone_task).update(capture_date=same_day)
        CaptureMeta.objects.filter(task=sat_task).update(capture_date=same_day)

        client = APIClient()
        client.login(username="testuser", password="test1234")
        res = client.get("/api/agri/seasonal/?project=%s" % self.project.id)
        self.assertEqual(res.status_code, status.HTTP_200_OK)

        series = res.data["fields"][0]["series"]
        # Both points survive -- neither overwrote the other.
        self.assertEqual(len(series), 2)
        sources = {p["source"] for p in series}
        self.assertEqual(sources, {CaptureMeta.DRONE, CaptureMeta.SATELLITE})
        self.assertTrue(all(p["date"] == "2026-06-15" for p in series))

        # Farm-level series also keeps them separate rather than averaging across
        # sources into one number.
        farm_series = res.data["farm"]["series"]
        self.assertEqual(len(farm_series), 2)
        farm_sources = {p["source"] for p in farm_series}
        self.assertEqual(farm_sources, {CaptureMeta.DRONE, CaptureMeta.SATELLITE})

    def test_seasonal_excludes_boundaries_without_field(self):
        self._analyzed_run(field=None, name="Unfielded cap")
        client = APIClient()
        client.login(username="testuser", password="test1234")
        res = client.get("/api/agri/seasonal/?project=%s" % self.project.id)
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(res.data["fields"], [])



class TestAgriRawImageCapture(BootTransactionTestCase):
    """
    Proves the "raw drone images" capture path (WebODM's native multi-image upload,
    processed by a real NodeODM instance) integrates with the agri workflow exactly
    like the pre-stitched-orthophoto import path used elsewhere in this file.

    No agri code changes are needed for this: Boundary/AnalysisRun hold a plain FK
    to app.Task and never inspect how it reached COMPLETED (import_url vs a real
    processing_node) — Task.extract_assets_and_complete() populates orthophoto_extent
    / orthophoto_bands identically either way. This test is the proof, not an assumption.

    Uses a separate test class (BootTransactionTestCase, matching WebODM's own
    app/tests/test_api_task_import.py pattern) because it drives a real local NodeODM
    subprocess — slower and isolated from the main TestAgri suite on purpose.
    """
    def setUp(self):
        super().setUp()
        clear_test_media_root()
        self._kill_stray_test_nodeodm()

    @staticmethod
    def _kill_stray_test_nodeodm():
        """
        Root-caused (2026-07-06): app.tests.utils.start_processing_node()'s cleanup
        (SIGTERM + 1s wait) does not reliably kill this Node.js process in this
        container, so a leftover instance from an earlier test run can still be
        listening on port 11223. A second instance then starts once the port frees
        up mid-test, with an empty in-memory task table, and deletes the *original*
        instance's still-in-progress task directory as "orphaned" -> spurious
        "<uuid> not found" failures. Force a clean slate before every run.
        """
        subprocess.call(
            "pkill -9 -f 'node index.js.*--port 11223' || true", shell=True)
        time.sleep(1)

    def test_raw_image_task_flows_through_boundary_and_analysis(self):
        from agri.models import AnalysisRun
        from agri.services import execute_analysis

        with start_processing_node():
            user = User.objects.get(username="testuser")
            project = Project.objects.create(owner=user, name="Raw Image Farm")

            pnode = ProcessingNode.objects.create(hostname="localhost", port=11223)
            assign_perm('view_processingnode', user, pnode)

            # The node subprocess can still be starting up when the post_save signal's
            # one-shot update_node_info() fires (observed: fails silently, leaving the
            # node permanently "offline" since nothing else ever retries it). Retry
            # explicitly instead of trusting a fixed sleep.
            # Measured directly: a cold Node.js start takes 5-10s in isolation on this
            # container's slow Windows bind-mount, and longer under the concurrent I/O
            # load of this test's own BootTransactionTestCase setup — so budget 60s,
            # not a tight guess.
            for _ in range(60):
                if pnode.update_node_info():
                    break
                time.sleep(1)
            self.assertTrue(pnode.is_online(), "local test NodeODM never came online")

            client = APIClient()
            client.login(username="testuser", password="test1234")

            # This is WebODM's native raw-image upload endpoint — unchanged, untouched.
            with open("app/fixtures/tiny_drone_image.jpg", 'rb') as img1, \
                 open("app/fixtures/tiny_drone_image_2.jpg", 'rb') as img2:
                res = client.post("/api/projects/%s/tasks/" % project.id,
                                  {'images': [img1, img2]}, format="multipart")
            self.assertEqual(res.status_code, status.HTTP_201_CREATED)
            task = Task.objects.get(id=res.data['id'])

            # Drive processing to completion (real NodeODM test-mode stitching)
            c = 0
            while c < 20:
                worker.tasks.process_pending_tasks()
                task.refresh_from_db()
                if task.status == status_codes.COMPLETED:
                    break
                c += 1
                time.sleep(1)

            self.assertEqual(task.status, status_codes.COMPLETED,
                            "task did not complete; last_error=%r" % task.last_error)
            self.assertIsNotNone(task.processing_node)  # real node was used, unlike external-import
            self.assertIsNotNone(task.orthophoto_extent)

            # From here on it is IDENTICAL to the pre-stitched-orthophoto capture path:
            boundary = Boundary.objects.create(task=task, geom=task.orthophoto_extent,
                                               status=Boundary.APPROVED, created_by=user)
            run = AnalysisRun.objects.create(task=task, boundary=boundary, triggered_by=user)
            execute_analysis(run)
            run.refresh_from_db()

            self.assertEqual(run.status, AnalysisRun.PENDING_REVIEW)
            self.assertEqual(run.results.count(), 6)


SYNC_URL = "/api/v1/mobile/sync"

FARM_PAYLOAD = {
    "farmerId": 12,
    "farmId": 3,
    "farm": {"name": "Makuni Test Farm 2", "location": "Norton"},
    "boundaries": {"farm": {
        "type": "Polygon",
        "coordinates": [[[29.1001, -17.8001], [29.1011, -17.8001],
                         [29.1011, -17.8011], [29.1001, -17.8011], [29.1001, -17.8001]]]
    }},
    "fields": [
        {"fieldId": 7, "name": "Field 7", "crop": "maize", "area_ha": 4.8,
         "boundary": {"type": "Polygon",
                      "coordinates": [[[29.1002, -17.8002], [29.1008, -17.8002],
                                       [29.1008, -17.8008], [29.1002, -17.8008], [29.1002, -17.8002]]]}}
    ],
}


class TestAgriTrackSync(BootTestCase):
    """
    Inbound structure sync from the AgriTrack mobile app (contract §6).
    Farm+Field only for v1 (locked decision -- SubPlots accepted-but-ignored).

    NOTE: agri/agritrack/views.py reads `from webodm import settings` (the raw
    settings module -- matching this codebase's established convention, e.g.
    agri/push.py), not `django.conf.settings`. Django's @override_settings only
    patches the latter, so it has no effect here -- mutate the module directly
    instead, matching the proven pattern in test_analysis_review_approve_and_push.
    """

    def setUp(self):
        super().setUp()
        self._orig_api_key = getattr(settings, 'AGRITRACK_INBOUND_API_KEY', None)
        settings.AGRITRACK_INBOUND_API_KEY = "test-shared-secret"

    def tearDown(self):
        settings.AGRITRACK_INBOUND_API_KEY = self._orig_api_key
        super().tearDown()

    def test_sync_requires_api_key(self):
        client = APIClient()
        res = client.post(SYNC_URL, FARM_PAYLOAD, format="json")
        self.assertEqual(res.status_code, status.HTTP_401_UNAUTHORIZED)

        res = client.post(SYNC_URL, FARM_PAYLOAD, format="json",
                          HTTP_X_API_KEY="wrong-key")
        self.assertEqual(res.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_sync_creates_farm_and_field(self):
        from agri.models import AgriFarm, AgriField

        client = APIClient()
        res = client.post(SYNC_URL, FARM_PAYLOAD, format="json",
                          HTTP_X_API_KEY="test-shared-secret")
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(res.data["farmId"], 3)

        agri_farm = AgriFarm.objects.get(agritrack_farm_id=3)
        self.assertEqual(agri_farm.name, "Makuni Test Farm 2")
        self.assertEqual(agri_farm.location, "Norton")
        self.assertIsNotNone(agri_farm.boundary)
        self.assertEqual(agri_farm.project.name, "Makuni Test Farm 2")

        agri_field = AgriField.objects.get(agritrack_field_id=7)
        self.assertEqual(agri_field.farm, agri_farm)
        self.assertEqual(agri_field.name, "Field 7")
        self.assertEqual(agri_field.crop, "maize")
        self.assertEqual(agri_field.area_ha, 4.8)
        self.assertIsNotNone(agri_field.boundary)

        # A persistent agri.Field is auto-created + linked, so synced fields also
        # get seasonal trends alongside hand-drawn ones.
        from agri.models import Field
        persistent = Field.objects.get(agri_field=agri_field)
        self.assertEqual(persistent.project, agri_farm.project)
        self.assertEqual(persistent.name, "Field 7")

    def test_sync_is_idempotent_and_updates(self):
        from agri.models import AgriFarm, AgriField
        import copy

        client = APIClient()
        client.post(SYNC_URL, FARM_PAYLOAD, format="json", HTTP_X_API_KEY="test-shared-secret")

        updated = copy.deepcopy(FARM_PAYLOAD)
        updated["farm"]["name"] = "Renamed Farm"
        updated["fields"][0]["crop"] = "soybean"
        res = client.post(SYNC_URL, updated, format="json", HTTP_X_API_KEY="test-shared-secret")
        self.assertEqual(res.status_code, status.HTTP_200_OK)

        self.assertEqual(AgriFarm.objects.filter(agritrack_farm_id=3).count(), 1)
        self.assertEqual(AgriField.objects.filter(agritrack_field_id=7).count(), 1)

        agri_farm = AgriFarm.objects.get(agritrack_farm_id=3)
        self.assertEqual(agri_farm.name, "Renamed Farm")
        agri_field = AgriField.objects.get(agritrack_field_id=7)
        self.assertEqual(agri_field.crop, "soybean")

    def test_sync_rejects_missing_farm_id(self):
        client = APIClient()
        res = client.post(SYNC_URL, {"farm": {"name": "No ID"}}, format="json",
                          HTTP_X_API_KEY="test-shared-secret")
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)

    def test_sync_rejects_new_field_without_boundary(self):
        import copy
        client = APIClient()
        payload = copy.deepcopy(FARM_PAYLOAD)
        payload["farmId"] = 999
        del payload["fields"][0]["boundary"]
        res = client.post(SYNC_URL, payload, format="json", HTTP_X_API_KEY="test-shared-secret")
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)

    def test_boundary_created_from_synced_agri_field(self):
        from agri.models import AgriField

        client = APIClient()
        client.post(SYNC_URL, FARM_PAYLOAD, format="json", HTTP_X_API_KEY="test-shared-secret")
        agri_field = AgriField.objects.get(agritrack_field_id=7)

        user = User.objects.get(username="testuser")
        project = agri_field.farm.project
        # testuser isn't the owner of the synced project (the sync owner is) --
        # make them the owner so can_contribute() allows the create below.
        project.owner = user
        project.save()
        task = Task.objects.create(project=project, name="Synced Farm Capture")

        client.login(username="testuser", password="test1234")
        res = client.post("/api/agri/boundaries/", {
            "task": str(task.id), "agri_field": agri_field.id
        }, format="json")
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        self.assertEqual(res.data["name"], "Field 7")
        boundary = Boundary.objects.get(pk=res.data["id"])
        self.assertEqual(boundary.agri_field, agri_field)
        self.assertTrue(boundary.geom.equals(agri_field.boundary))

    def test_boundary_agri_field_must_match_task_farm(self):
        from agri.models import AgriField

        client = APIClient()
        client.post(SYNC_URL, FARM_PAYLOAD, format="json", HTTP_X_API_KEY="test-shared-secret")
        agri_field = AgriField.objects.get(agritrack_field_id=7)

        user = User.objects.get(username="testuser")
        other_project = Project.objects.create(owner=user, name="Unrelated Project")
        task = Task.objects.create(project=other_project, name="Unrelated Capture")

        client.login(username="testuser", password="test1234")
        res = client.post("/api/agri/boundaries/", {
            "task": str(task.id), "agri_field": agri_field.id
        }, format="json")
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)


REMOTE_SENSE_URL = "/api/v1/remote-sense/push"


class TestRemoteSensePush(BootTestCase):
    """
    Inbound Sentinel/remote-sense GeoTIFF delivery (agri/remote_sense/).

    Same settings-module caveat as TestAgriTrackSync: the view reads
    `from webodm import settings`, so we mutate the module directly rather than
    using @override_settings.
    """

    def setUp(self):
        super().setUp()
        self._orig_rs_key = getattr(settings, 'REMOTE_SENSE_INBOUND_API_KEY', None)
        self._orig_agri_key = getattr(settings, 'AGRITRACK_INBOUND_API_KEY', None)
        settings.REMOTE_SENSE_INBOUND_API_KEY = "rs-shared-secret"
        settings.AGRITRACK_INBOUND_API_KEY = "test-shared-secret"

    def tearDown(self):
        settings.REMOTE_SENSE_INBOUND_API_KEY = self._orig_rs_key
        settings.AGRITRACK_INBOUND_API_KEY = self._orig_agri_key
        super().tearDown()

    def _sync_farm(self):
        APIClient().post(SYNC_URL, FARM_PAYLOAD, format="json",
                         HTTP_X_API_KEY="test-shared-secret")

    def _tif_upload(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        with open("app/fixtures/orthophoto.tif", "rb") as f:
            return SimpleUploadedFile("sentinel.tif", f.read(), content_type="image/tiff")

    def test_push_requires_api_key(self):
        client = APIClient()
        res = client.post(REMOTE_SENSE_URL, {"farm_id": 3, "orthophoto": self._tif_upload()},
                          format="multipart")
        self.assertEqual(res.status_code, status.HTTP_401_UNAUTHORIZED)

        res = client.post(REMOTE_SENSE_URL, {"farm_id": 3, "orthophoto": self._tif_upload()},
                          format="multipart", HTTP_X_API_KEY="wrong")
        self.assertEqual(res.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_push_unknown_farm_returns_404(self):
        # Valid key, but farm was never synced -> 404 (before any capture is made).
        client = APIClient()
        res = client.post(REMOTE_SENSE_URL, {"farm_id": 999999, "orthophoto": self._tif_upload()},
                          format="multipart", HTTP_X_API_KEY="rs-shared-secret")
        self.assertEqual(res.status_code, status.HTTP_404_NOT_FOUND)

    def test_push_happy_path_returns_201(self):
        # Isolated from the (eager) import pipeline by mocking the ingest service.
        self._sync_farm()
        from agri.models import AgriFarm
        project = AgriFarm.objects.get(agritrack_farm_id=3).project
        fake_task = Task.objects.create(project=project, name="Fake")

        with mock.patch("agri.remote_sense.views.ingest_capture",
                        return_value=fake_task) as m:
            client = APIClient()
            res = client.post(REMOTE_SENSE_URL,
                              {"farm_id": 3, "orthophoto": self._tif_upload(),
                               "capture_date": "2026-05-01"},
                              format="multipart", HTTP_X_API_KEY="rs-shared-secret")
        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        self.assertEqual(res.data["captureId"], str(fake_task.id))
        self.assertEqual(res.data["projectId"], project.id)
        # farm_id + file + parsed date were forwarded to the service
        _, kwargs = m.call_args
        self.assertEqual(kwargs["farm_id"], "3")
        self.assertIsNotNone(kwargs["orthophoto_file"])
        self.assertEqual(str(kwargs["capture_date"]), "2026-05-01")

    def test_ingest_creates_capture_and_seeds_agrifields(self):
        from agri.remote_sense.ingest import ingest_capture
        from agri.models import AgriFarm, AgriField, CaptureMeta
        import datetime

        self._sync_farm()
        farm = AgriFarm.objects.get(agritrack_farm_id=3)
        agri_field = AgriField.objects.get(agritrack_field_id=7)

        with open("app/fixtures/orthophoto.tif", "rb") as f:
            task = ingest_capture(farm_id=3, orthophoto_file=f, image_url=None,
                                  name="Sentinel A",
                                  capture_date=datetime.date(2026, 5, 1),
                                  dispatch=False)

        # Capture landed on the farm's project, with the given acquisition date.
        self.assertEqual(task.project_id, farm.project_id)
        capture_meta = CaptureMeta.objects.get(task=task)
        self.assertEqual(capture_meta.capture_date, datetime.date(2026, 5, 1))
        # Every remote-sense import is tagged SATELLITE (Stage 9 §5) -- this is
        # what lets the seasonal API and analysis fan-out treat it differently
        # from a drone capture instead of silently mixing the two.
        self.assertEqual(capture_meta.source, CaptureMeta.SATELLITE)
        # The synced field was seeded as a DRAFT boundary linked back to the
        # AgriField (so its analysis is pushable to AgriTrack per-field).
        boundaries = list(Boundary.objects.filter(task=task))
        self.assertEqual(len(boundaries), 1)
        self.assertEqual(boundaries[0].agri_field_id, agri_field.id)
        self.assertEqual(boundaries[0].status, Boundary.DRAFT)

    def test_ingest_from_bands_tags_satellite_source(self):
        from agri.remote_sense.ingest import ingest_capture_from_bands
        from agri.models import CaptureMeta

        self._sync_farm()
        uploads = [self._make_band_upload(t, v) for t, v in
                  [("B02", 0.1), ("B03", 0.2), ("B04", 0.3), ("B08", 0.8)]]
        task = ingest_capture_from_bands(farm_id=3, band_files=uploads, dispatch=False)
        self.assertEqual(CaptureMeta.objects.get(task=task).source, CaptureMeta.SATELLITE)

    def test_ingest_unknown_farm_raises(self):
        from agri.remote_sense.ingest import ingest_capture, FarmNotFoundError
        with open("app/fixtures/orthophoto.tif", "rb") as f:
            with self.assertRaises(FarmNotFoundError):
                ingest_capture(farm_id=424242, orthophoto_file=f, dispatch=False)

    def test_ingest_requires_file_or_url(self):
        from agri.remote_sense.ingest import ingest_capture, RemoteSenseValidationError
        self._sync_farm()
        with self.assertRaises(RemoteSenseValidationError):
            ingest_capture(farm_id=3, orthophoto_file=None, image_url=None,
                           dispatch=False)

    # --- Separate Sentinel band files -> stack + tag (Option B) ---

    def _make_band_upload(self, token, value):
        """A synthetic single-band Sentinel GeoTIFF upload, named like the real
        export (so detect_band_role() picks the band from the filename)."""
        import numpy as np
        import rasterio
        from rasterio.transform import from_origin
        from django.core.files.uploadedfile import SimpleUploadedFile

        os.makedirs(settings.MEDIA_TMP, exist_ok=True)
        path = os.path.join(settings.MEDIA_TMP, "src_%s.tif" % token)
        transform = from_origin(29.10, -17.80, 0.0001, 0.0001)
        data = np.full((16, 16), value, dtype='float32')
        with rasterio.open(path, 'w', driver='GTiff', height=16, width=16, count=1,
                           dtype='float32', crs='EPSG:4326', transform=transform) as dst:
            dst.write(data, 1)
        with open(path, 'rb') as f:
            content = f.read()
        os.remove(path)
        name = "2026-07-04-00_00_2026-07-04-23_59_Sentinel-2_L2A_%s_(Raw).tif" % token
        return SimpleUploadedFile(name, content, content_type="image/tiff")

    def test_detect_band_role(self):
        from agri.remote_sense.bands import detect_band_role
        self.assertEqual(detect_band_role("x_Sentinel-2_L2A_B04_(Raw).tif"), "red")
        self.assertEqual(detect_band_role("x_Sentinel-2_L2A_B08_(Raw).tif"), "nir")
        self.assertEqual(detect_band_role("x_B02.tif"), "blue")
        self.assertEqual(detect_band_role("x_B05_(Raw).tif"), "rededge")
        self.assertIsNone(detect_band_role("x_B01_(Raw).tif"))       # aerosol -> ignored
        self.assertIsNone(detect_band_role("Sentinel-2_L2A.tif"))    # no band token

    def test_stack_and_tag_bands_writes_descriptions(self):
        import rasterio
        from agri.remote_sense.bands import stack_and_tag_bands, detect_band_role

        # Materialise the four band uploads to disk keyed by role.
        role_to_path = {}
        for token, value in [("B02", 0.1), ("B03", 0.2), ("B04", 0.3), ("B08", 0.8)]:
            up = self._make_band_upload(token, value)
            path = os.path.join(settings.MEDIA_TMP, "in_%s.tif" % token)
            with open(path, 'wb') as out:
                out.write(up.read())
            role_to_path[detect_band_role(up.name)] = path

        out_path = os.path.join(settings.MEDIA_TMP, "stacked.tif")
        stack_and_tag_bands(role_to_path, out_path)

        with rasterio.open(out_path) as ds:
            self.assertEqual(ds.count, 4)
            # Canonical order + descriptions that the formula engine matches on.
            self.assertEqual(list(ds.descriptions), ["red", "green", "blue", "nir"])

    def test_ingest_from_bands_creates_tagged_capture(self):
        import datetime
        import rasterio
        from agri.remote_sense.ingest import ingest_capture_from_bands
        from agri.analysis.base import has_nir

        self._sync_farm()
        uploads = [self._make_band_upload(t, v) for t, v in
                   [("B02", 0.1), ("B03", 0.2), ("B04", 0.3), ("B08", 0.8)]]
        task = ingest_capture_from_bands(
            farm_id=3, band_files=uploads, name="Sentinel bands",
            capture_date=datetime.date(2026, 7, 4), dispatch=False)

        ortho = task.assets_path(Task.ASSETS_MAP["orthophoto.tif"])
        self.assertTrue(os.path.isfile(ortho))
        with rasterio.open(ortho) as ds:
            self.assertEqual(ds.count, 4)
            self.assertEqual(list(ds.descriptions), ["red", "green", "blue", "nir"])

        # The whole point: NIR is now discoverable, so analysis auto-picks NDVI.
        task.update_orthophoto_bands_field()
        self.assertTrue(has_nir(task))

    def test_ingest_from_bands_missing_red_raises(self):
        from agri.remote_sense.ingest import (ingest_capture_from_bands,
                                              RemoteSenseValidationError)
        self._sync_farm()
        # Green + blue only (no red=B04) -> nothing computable, reject.
        uploads = [self._make_band_upload("B03", 0.2), self._make_band_upload("B02", 0.1)]
        with self.assertRaises(RemoteSenseValidationError):
            ingest_capture_from_bands(farm_id=3, band_files=uploads, dispatch=False)

    def test_ingest_from_bands_no_recognised_bands_raises(self):
        from agri.remote_sense.ingest import (ingest_capture_from_bands,
                                              RemoteSenseValidationError)
        self._sync_farm()
        uploads = [self._make_band_upload("B01", 0.1)]  # aerosol only -> unusable
        with self.assertRaises(RemoteSenseValidationError):
            ingest_capture_from_bands(farm_id=3, band_files=uploads, dispatch=False)

    def test_push_with_band_files_returns_201(self):
        # Endpoint routes multi-file uploads to the band-stacking path. Mocked so
        # this test stays independent of GDAL/COG (exercised in the ingest test).
        self._sync_farm()
        from agri.models import AgriFarm
        project = AgriFarm.objects.get(agritrack_farm_id=3).project
        fake_task = Task.objects.create(project=project, name="Fake")

        with mock.patch("agri.remote_sense.views.ingest_capture_from_bands",
                        return_value=fake_task) as m:
            client = APIClient()
            res = client.post(REMOTE_SENSE_URL, {
                "farm_id": 3,
                "b1": self._make_band_upload("B02", 0.1),
                "b2": self._make_band_upload("B03", 0.2),
                "b3": self._make_band_upload("B04", 0.3),
                "b4": self._make_band_upload("B08", 0.8),
                "capture_date": "2026-07-04",
            }, format="multipart", HTTP_X_API_KEY="rs-shared-secret")

        self.assertEqual(res.status_code, status.HTTP_201_CREATED)
        _, kwargs = m.call_args
        self.assertEqual(kwargs["farm_id"], "3")
        self.assertEqual(len(kwargs["band_files"]), 4)
        self.assertEqual(str(kwargs["capture_date"]), "2026-07-04")


class TestSatellitePull(BootTestCase):
    """
    On-demand satellite pull (Stage 9 SS7/SS9): a logged-in user requesting
    satellite imagery for a farm's already-onboarded boundary, and requesting
    Sentinel's own reference index for an APPROVED boundary to compare against
    this system's own analysis. Network calls are mocked at the
    agri.remote_sense.sentinel_client boundary -- these tests never hit the
    real Copernicus/Sentinel Hub API.
    """

    def setUp(self):
        super().setUp()
        from agri.roles import ROLE_TECHNICIAN, ROLE_AGRONOMIST
        self.user = User.objects.get(username="testuser")
        self.project = Project.objects.get(owner=self.user)
        self.task = Task.objects.create(project=self.project, name="Sat Capture Host")
        self.user.groups.add(Group.objects.get(name=ROLE_TECHNICIAN))
        self.agronomist = User.objects.create_user(username="satagro", email="satagro@test.com",
                                                    password="test1234")
        self.agronomist.groups.add(Group.objects.get(name=ROLE_AGRONOMIST))

    def _fake_fetch_imagery(self, geom, date_from, date_to, output_path, roles=None):
        shutil.copyfile("app/fixtures/orthophoto.tif", output_path)
        return {'path': output_path, 'valid_pixel_pct': 92.5}

    # ---- pull_satellite_capture (business logic) ----

    def test_pull_satellite_capture_uses_agrifarm_boundary(self):
        import datetime
        from agri.models import AgriFarm, CaptureMeta
        from agri.remote_sense.sentinel_pull import pull_satellite_capture

        AgriFarm.objects.create(agritrack_farm_id=501, project=self.project,
                                name="Sat Farm", boundary=_poly())

        with mock.patch("agri.remote_sense.sentinel_client.fetch_field_imagery",
                        side_effect=self._fake_fetch_imagery) as m:
            task = pull_satellite_capture(self.project.id, datetime.date(2026, 7, 1),
                                          datetime.date(2026, 7, 10))

        self.assertEqual(task.project_id, self.project.id)
        self.assertEqual(CaptureMeta.objects.get(task=task).source, CaptureMeta.SATELLITE)
        geom_arg = m.call_args[0][0]
        self.assertTrue(geom_arg.equals(_poly()))

    def test_pull_satellite_capture_falls_back_to_boundary_union(self):
        import datetime
        from agri.remote_sense.sentinel_pull import pull_satellite_capture

        Boundary.objects.create(task=self.task, geom=_poly(), status=Boundary.APPROVED,
                                created_by=self.user)

        with mock.patch("agri.remote_sense.sentinel_client.fetch_field_imagery",
                        side_effect=self._fake_fetch_imagery) as m:
            task = pull_satellite_capture(self.project.id, datetime.date(2026, 7, 1),
                                          datetime.date(2026, 7, 10))
        self.assertIsNotNone(task)
        self.assertTrue(m.called)

    def test_pull_satellite_capture_no_boundary_raises(self):
        import datetime
        from agri.remote_sense.sentinel_pull import pull_satellite_capture, SatellitePullError
        with self.assertRaises(SatellitePullError):
            pull_satellite_capture(self.project.id, datetime.date(2026, 7, 1),
                                   datetime.date(2026, 7, 10))

    # ---- Stage 10 Phase 1: cloud/quality awareness + field-level targeting ----

    def test_valid_pixel_pct_pure_function(self):
        # No network, no mocking -- a real numpy array walking every excluded
        # and included SCL class, matching the live-verified classification set
        # (stage-10-sentinel-roadmap.md Phase 1 / sentinel_client.py docstring).
        import numpy as np
        from agri.remote_sense.sentinel_client import _valid_pixel_pct

        # All valid: vegetation(4), bare soil(5), water(6), snow(11)
        all_valid = np.array([4, 5, 6, 11, 4, 5], dtype='uint8')
        self.assertEqual(_valid_pixel_pct(all_valid), 100.0)

        # All excluded: no-data(0), saturated(1), dark(2), cloud shadow(3),
        # unclassified(7), cloud medium(8), cloud high(9), cirrus(10)
        all_cloud = np.array([0, 1, 2, 3, 7, 8, 9, 10], dtype='uint8')
        self.assertEqual(_valid_pixel_pct(all_cloud), 0.0)

        # Half and half
        mixed = np.array([4, 4, 8, 9], dtype='uint8')
        self.assertEqual(_valid_pixel_pct(mixed), 50.0)

        # Empty array -> 0.0, not a crash
        self.assertEqual(_valid_pixel_pct(np.array([], dtype='uint8')), 0.0)

    def test_index_definition_pure_function(self):
        # No network -- pure lookup, matching the real supported set live-tested
        # against Sentinel Hub (stage-10-sentinel-roadmap.md Phase 4).
        from agri.remote_sense.sentinel_client import _index_definition, SENTINEL_INDEX_DEFS

        for name in ('NDVI', 'GNDVI', 'NDRE', 'SAVI', 'EVI'):
            definition = _index_definition(name)
            self.assertIn('bands', definition)
            self.assertIn('formula', definition)
            self.assertTrue(len(definition['bands']) >= 2)
            self.assertIn('B08', definition['bands'])  # every supported index uses NIR

        self.assertEqual(set(SENTINEL_INDEX_DEFS.keys()), {'NDVI', 'GNDVI', 'NDRE', 'SAVI', 'EVI'})

        with self.assertRaises(NotImplementedError):
            _index_definition('BOGUS')

    def test_pull_satellite_capture_stores_valid_pixel_pct(self):
        import datetime
        from agri.models import AgriFarm, CaptureMeta
        from agri.remote_sense.sentinel_pull import pull_satellite_capture

        AgriFarm.objects.create(agritrack_farm_id=503, project=self.project,
                                name="Sat Farm 3", boundary=_poly())

        with mock.patch("agri.remote_sense.sentinel_client.fetch_field_imagery",
                        side_effect=self._fake_fetch_imagery):
            task = pull_satellite_capture(self.project.id, datetime.date(2026, 7, 1),
                                          datetime.date(2026, 7, 10))

        self.assertEqual(CaptureMeta.objects.get(task=task).valid_pixel_pct, 92.5)

    def test_pull_satellite_capture_degrades_gracefully_when_quality_check_fails(self):
        # "Flag, don't block": fetch_field_imagery returning valid_pixel_pct=None
        # (its own best-effort SCL fetch failed) must NOT fail the whole pull --
        # the image was already fetched successfully.
        import datetime
        from agri.models import AgriFarm, CaptureMeta
        from agri.remote_sense.sentinel_pull import pull_satellite_capture

        AgriFarm.objects.create(agritrack_farm_id=504, project=self.project,
                                name="Sat Farm 4", boundary=_poly())

        def fake_fetch_no_quality(geom, date_from, date_to, output_path, roles=None):
            shutil.copyfile("app/fixtures/orthophoto.tif", output_path)
            return {'path': output_path, 'valid_pixel_pct': None}

        with mock.patch("agri.remote_sense.sentinel_client.fetch_field_imagery",
                        side_effect=fake_fetch_no_quality):
            task = pull_satellite_capture(self.project.id, datetime.date(2026, 7, 1),
                                          datetime.date(2026, 7, 10))

        self.assertIsNotNone(task)
        self.assertIsNone(CaptureMeta.objects.get(task=task).valid_pixel_pct)

    def test_pull_satellite_capture_targets_single_field(self):
        # "both as options" (UX review, 2026-08-14): a user can target one field
        # instead of the whole farm. Uses the Field's most recent Boundary geom,
        # not the farm-wide union.
        import datetime
        from agri.models import Field, CaptureMeta
        from agri.remote_sense.sentinel_pull import pull_satellite_capture

        field = Field.objects.create(project=self.project, name="North Block")
        target_geom = _poly()
        Boundary.objects.create(task=self.task, geom=target_geom, field=field,
                                status=Boundary.APPROVED, created_by=self.user)
        # A DIFFERENT, unrelated boundary on the same farm -- must be ignored
        # when a specific field is targeted.
        other_geom = Polygon(((10, 10), (10, 11), (11, 11), (11, 10), (10, 10)), srid=4326)
        Boundary.objects.create(task=self.task, geom=other_geom,
                                status=Boundary.APPROVED, created_by=self.user)

        with mock.patch("agri.remote_sense.sentinel_client.fetch_field_imagery",
                        side_effect=self._fake_fetch_imagery) as m:
            task = pull_satellite_capture(self.project.id, datetime.date(2026, 7, 1),
                                          datetime.date(2026, 7, 10), field_id=field.id)

        self.assertIsNotNone(task)
        geom_arg = m.call_args[0][0]
        self.assertTrue(geom_arg.equals(target_geom))
        self.assertFalse(geom_arg.equals(other_geom))

    def test_pull_satellite_capture_unknown_field_raises(self):
        import datetime
        from agri.remote_sense.sentinel_pull import pull_satellite_capture, SatellitePullError
        with self.assertRaises(SatellitePullError):
            pull_satellite_capture(self.project.id, datetime.date(2026, 7, 1),
                                   datetime.date(2026, 7, 10), field_id=999999)

    def test_pull_satellite_capture_field_with_no_boundary_raises(self):
        import datetime
        from agri.models import Field
        from agri.remote_sense.sentinel_pull import pull_satellite_capture, SatellitePullError
        field = Field.objects.create(project=self.project, name="Empty Block")
        with self.assertRaises(SatellitePullError):
            pull_satellite_capture(self.project.id, datetime.date(2026, 7, 1),
                                   datetime.date(2026, 7, 10), field_id=field.id)

    # ---- pull_satellite_comparison (business logic) ----

    def test_pull_satellite_comparison_requires_approved_boundary(self):
        from agri.remote_sense.sentinel_pull import pull_satellite_comparison, SatellitePullError
        boundary = Boundary.objects.create(task=self.task, geom=_poly(), status=Boundary.DRAFT,
                                           created_by=self.user)
        with self.assertRaises(SatellitePullError):
            pull_satellite_comparison(boundary.id)

    def test_pull_satellite_comparison_creates_sentinel_run(self):
        import datetime
        from agri.models import AnalysisRun, AnalysisResult, CaptureMeta
        from agri.remote_sense.sentinel_pull import pull_satellite_comparison

        CaptureMeta.objects.create(task=self.task, capture_date=datetime.date(2026, 7, 5))
        boundary = Boundary.objects.create(task=self.task, geom=_poly(), status=Boundary.APPROVED,
                                           created_by=self.user)

        with mock.patch("agri.remote_sense.sentinel_client.fetch_field_statistics",
                        return_value=[{'date': '2026-07-05', 'mean': 0.42, 'stddev': 0.05,
                                      'valid_pixel_pct': 88.0}]) as m:
            run = pull_satellite_comparison(boundary.id)

        self.assertEqual(run.computed_by, AnalysisRun.SENTINEL)
        self.assertEqual(run.status, AnalysisRun.PENDING_REVIEW)
        result = run.results.get(kind=AnalysisResult.PLANT_HEALTH)
        self.assertEqual(result.stats['mean'], 0.42)
        self.assertEqual(result.stats['index'], 'NDVI')
        self.assertEqual(result.stats['valid_pixel_pct'], 88.0)
        date_from_arg = m.call_args[0][1]
        self.assertEqual(date_from_arg, datetime.date(2026, 7, 5))

    def test_pull_satellite_comparison_missing_quality_degrades_gracefully(self):
        # A point without valid_pixel_pct (older client, or the quality check
        # itself failed upstream) must not crash the comparison -- stats simply
        # carries None, same "flag, don't block" spirit as the imagery path.
        import datetime
        from agri.models import AnalysisResult, CaptureMeta
        from agri.remote_sense.sentinel_pull import pull_satellite_comparison

        CaptureMeta.objects.create(task=self.task, capture_date=datetime.date(2026, 7, 5))
        boundary = Boundary.objects.create(task=self.task, geom=_poly(), status=Boundary.APPROVED,
                                           created_by=self.user)

        with mock.patch("agri.remote_sense.sentinel_client.fetch_field_statistics",
                        return_value=[{'date': '2026-07-05', 'mean': 0.42, 'stddev': 0.05}]):
            run = pull_satellite_comparison(boundary.id)

        result = run.results.get(kind=AnalysisResult.PLANT_HEALTH)
        self.assertIsNone(result.stats['valid_pixel_pct'])

    def test_pull_satellite_comparison_no_data_raises(self):
        from agri.remote_sense.sentinel_pull import pull_satellite_comparison, SatellitePullError
        boundary = Boundary.objects.create(task=self.task, geom=_poly(), status=Boundary.APPROVED,
                                           created_by=self.user)
        with mock.patch("agri.remote_sense.sentinel_client.fetch_field_statistics", return_value=[]):
            with self.assertRaises(SatellitePullError):
                pull_satellite_comparison(boundary.id)

    def test_pull_satellite_comparison_accepts_index_choice(self):
        # Stage 10 Phase 4: the caller can ask for a non-NDVI index; it must
        # reach fetch_field_statistics and land on both index_used and stats.
        import datetime
        from agri.models import AnalysisResult, CaptureMeta
        from agri.remote_sense.sentinel_pull import pull_satellite_comparison

        CaptureMeta.objects.create(task=self.task, capture_date=datetime.date(2026, 7, 5))
        boundary = Boundary.objects.create(task=self.task, geom=_poly(), status=Boundary.APPROVED,
                                           created_by=self.user)

        with mock.patch("agri.remote_sense.sentinel_client.fetch_field_statistics",
                        return_value=[{'date': '2026-07-05', 'mean': 0.17, 'stddev': 0.02,
                                      'valid_pixel_pct': 95.0}]) as m:
            run = pull_satellite_comparison(boundary.id, index='EVI')

        self.assertEqual(run.index_used, 'EVI')
        result = run.results.get(kind=AnalysisResult.PLANT_HEALTH)
        self.assertEqual(result.stats['index'], 'EVI')
        self.assertEqual(m.call_args.kwargs.get('index'), 'EVI')

    def test_pull_satellite_comparison_unsupported_index_raises(self):
        from agri.remote_sense.sentinel_pull import pull_satellite_comparison, SatellitePullError
        boundary = Boundary.objects.create(task=self.task, geom=_poly(), status=Boundary.APPROVED,
                                           created_by=self.user)
        with mock.patch("agri.remote_sense.sentinel_client.fetch_field_statistics",
                        side_effect=NotImplementedError("Unsupported index 'BOGUS'")):
            with self.assertRaises(SatellitePullError):
                pull_satellite_comparison(boundary.id, index='BOGUS')

    # ---- API endpoints ----

    def test_satellite_imagery_endpoint_requires_auth(self):
        client = APIClient()
        res = client.post("/api/agri/satellite/imagery/", {
            "project": self.project.id, "date_from": "2026-07-01", "date_to": "2026-07-10"
        }, format="json")
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)

    def test_satellite_imagery_endpoint_blocks_agronomist(self):
        client = APIClient()
        client.login(username="satagro", password="test1234")
        res = client.post("/api/agri/satellite/imagery/", {
            "project": self.project.id, "date_from": "2026-07-01", "date_to": "2026-07-10"
        }, format="json")
        self.assertEqual(res.status_code, status.HTTP_403_FORBIDDEN)

    def test_satellite_imagery_endpoint_requires_valid_dates(self):
        client = APIClient()
        client.login(username="testuser", password="test1234")
        res = client.post("/api/agri/satellite/imagery/", {"project": self.project.id}, format="json")
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)

        res = client.post("/api/agri/satellite/imagery/", {
            "project": self.project.id, "date_from": "2026-07-10", "date_to": "2026-07-01"
        }, format="json")
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)

    def test_satellite_imagery_endpoint_dispatches_and_creates_capture(self):
        from agri.models import AgriFarm, CaptureMeta
        AgriFarm.objects.create(agritrack_farm_id=502, project=self.project,
                                name="Sat Farm 2", boundary=_poly())
        client = APIClient()
        client.login(username="testuser", password="test1234")

        with mock.patch("agri.remote_sense.sentinel_client.fetch_field_imagery",
                        side_effect=self._fake_fetch_imagery):
            res = client.post("/api/agri/satellite/imagery/", {
                "project": self.project.id, "date_from": "2026-07-01", "date_to": "2026-07-10"
            }, format="json")
        self.assertEqual(res.status_code, status.HTTP_202_ACCEPTED)
        self.assertIn("celery_task_id", res.data)

        # CELERY_TASK_ALWAYS_EAGER in tests -> the task already ran synchronously.
        check = client.get("/api/workers/check/%s" % res.data["celery_task_id"])
        self.assertEqual(check.status_code, status.HTTP_200_OK)
        self.assertTrue(check.data["ready"])
        self.assertNotIn("error", check.data)
        self.assertTrue(CaptureMeta.objects.filter(source=CaptureMeta.SATELLITE).exists())

    def test_satellite_imagery_endpoint_accepts_field_target(self):
        from agri.models import Field
        field = Field.objects.create(project=self.project, name="South Block")
        Boundary.objects.create(task=self.task, geom=_poly(), field=field,
                                status=Boundary.APPROVED, created_by=self.user)
        client = APIClient()
        client.login(username="testuser", password="test1234")

        with mock.patch("agri.remote_sense.sentinel_client.fetch_field_imagery",
                        side_effect=self._fake_fetch_imagery):
            res = client.post("/api/agri/satellite/imagery/", {
                "project": self.project.id, "date_from": "2026-07-01", "date_to": "2026-07-10",
                "field": field.id
            }, format="json")
        self.assertEqual(res.status_code, status.HTTP_202_ACCEPTED)
        check = client.get("/api/workers/check/%s" % res.data["celery_task_id"])
        self.assertNotIn("error", check.data)

    def test_satellite_imagery_endpoint_rejects_field_from_other_farm(self):
        from agri.models import Field
        other_project = Project.objects.create(owner=self.user, name="Other Farm")
        other_field = Field.objects.create(project=other_project, name="Not This Farm")
        client = APIClient()
        client.login(username="testuser", password="test1234")
        res = client.post("/api/agri/satellite/imagery/", {
            "project": self.project.id, "date_from": "2026-07-01", "date_to": "2026-07-10",
            "field": other_field.id
        }, format="json")
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)

    def test_satellite_compare_endpoint_requires_approved_boundary(self):
        boundary = Boundary.objects.create(task=self.task, geom=_poly(), status=Boundary.DRAFT,
                                           created_by=self.user)
        client = APIClient()
        client.login(username="testuser", password="test1234")
        res = client.post("/api/agri/satellite/compare/", {"boundary": boundary.id}, format="json")
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)

    def test_satellite_compare_endpoint_dispatches_and_creates_run(self):
        import datetime
        from agri.models import AnalysisRun, CaptureMeta
        CaptureMeta.objects.create(task=self.task, capture_date=datetime.date(2026, 7, 5))
        boundary = Boundary.objects.create(task=self.task, geom=_poly(), status=Boundary.APPROVED,
                                           created_by=self.user)
        client = APIClient()
        client.login(username="testuser", password="test1234")

        with mock.patch("agri.remote_sense.sentinel_client.fetch_field_statistics",
                        return_value=[{'date': '2026-07-05', 'mean': 0.5, 'stddev': 0.1}]):
            res = client.post("/api/agri/satellite/compare/", {"boundary": boundary.id}, format="json")
        self.assertEqual(res.status_code, status.HTTP_202_ACCEPTED)

        run = AnalysisRun.objects.get(boundary=boundary, computed_by=AnalysisRun.SENTINEL)
        self.assertEqual(run.status, AnalysisRun.PENDING_REVIEW)

    def test_serializer_exposes_computed_by(self):
        from agri.models import AnalysisRun
        from agri.api.serializers import AnalysisRunSerializer
        boundary = Boundary.objects.create(task=self.task, geom=_poly(), status=Boundary.APPROVED,
                                           created_by=self.user)
        run = AnalysisRun.objects.create(task=self.task, boundary=boundary,
                                         computed_by=AnalysisRun.SENTINEL)
        data = AnalysisRunSerializer(run).data
        self.assertEqual(data["computed_by"], "SENTINEL")

    def test_fields_endpoint_accepts_project_param(self):
        # Stage 10 Phase 1: the "Get Satellite Imagery" Import-menu modal needs a
        # field picker before any capture/task exists yet, so /api/agri/fields/
        # must also work with ?project= (not just the original ?task=).
        from agri.models import Field
        Field.objects.create(project=self.project, name="East Block")
        Field.objects.create(project=self.project, name="West Block")
        client = APIClient()
        client.login(username="testuser", password="test1234")

        res = client.get("/api/agri/fields/?project=%s" % self.project.id)
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual({f["name"] for f in res.data}, {"East Block", "West Block"})

        # Original ?task= path still works unchanged.
        res = client.get("/api/agri/fields/?task=%s" % self.task.id)
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual({f["name"] for f in res.data}, {"East Block", "West Block"})

    def test_fields_endpoint_requires_task_or_project(self):
        client = APIClient()
        client.login(username="testuser", password="test1234")
        res = client.get("/api/agri/fields/")
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)

    # ---- Stage 10 Phase 2: scene-availability picker ----

    def test_dedupe_scenes_by_date_pure_function(self):
        # No network, no mocking -- matches _valid_pixel_pct's pattern.
        from agri.remote_sense.sentinel_client import _dedupe_scenes_by_date

        results = [
            {'properties': {'datetime': '2026-08-11T08:25:01.759Z', 'eo:cloud_cover': 1.9}},
            {'properties': {'datetime': '2026-08-08T08:25:18.255Z', 'eo:cloud_cover': 0.0}},
            # Two scenes on the same day (overlapping tiles) -- keep the least cloudy.
            {'properties': {'datetime': '2026-08-01T08:25:03.478Z', 'eo:cloud_cover': 40.0}},
            {'properties': {'datetime': '2026-08-01T09:10:00.000Z', 'eo:cloud_cover': 5.5}},
            # Missing fields -- must be skipped, not crash.
            {'properties': {'datetime': None, 'eo:cloud_cover': 10.0}},
            {'properties': {'datetime': '2026-07-01T00:00:00Z', 'eo:cloud_cover': None}},
        ]
        scenes = _dedupe_scenes_by_date(results)

        self.assertEqual([s['date'] for s in scenes], ['2026-08-11', '2026-08-08', '2026-08-01'])
        self.assertEqual(scenes[0]['cloud_cover_pct'], 1.9)
        self.assertEqual(scenes[2]['cloud_cover_pct'], 5.5)  # least-cloudy of the two Aug-01 scenes

    def test_dedupe_scenes_by_date_empty(self):
        from agri.remote_sense.sentinel_client import _dedupe_scenes_by_date
        self.assertEqual(_dedupe_scenes_by_date([]), [])

    def test_satellite_availability_endpoint_requires_valid_dates(self):
        client = APIClient()
        client.login(username="testuser", password="test1234")
        res = client.get("/api/agri/satellite/availability/?project=%s" % self.project.id)
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)

        res = client.get("/api/agri/satellite/availability/?project=%s&date_from=2026-07-10&date_to=2026-07-01"
                         % self.project.id)
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)

    def test_satellite_availability_endpoint_returns_scenes(self):
        from agri.models import AgriFarm
        AgriFarm.objects.create(agritrack_farm_id=505, project=self.project,
                                name="Sat Farm 5", boundary=_poly())
        client = APIClient()
        client.login(username="testuser", password="test1234")

        fake_scenes = [{'date': '2026-08-11', 'cloud_cover_pct': 1.9},
                       {'date': '2026-08-08', 'cloud_cover_pct': 0.0}]
        with mock.patch("agri.remote_sense.sentinel_client.search_available_scenes",
                        return_value=fake_scenes) as m:
            res = client.get("/api/agri/satellite/availability/?project=%s&date_from=2026-07-01&date_to=2026-08-11"
                             % self.project.id)
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        self.assertEqual(res.data, fake_scenes)
        geom_arg = m.call_args[0][0]
        self.assertTrue(geom_arg.equals(_poly()))

    def test_satellite_availability_endpoint_targets_single_field(self):
        from agri.models import Field
        field = Field.objects.create(project=self.project, name="Availability Block")
        target_geom = _poly()
        Boundary.objects.create(task=self.task, geom=target_geom, field=field,
                                status=Boundary.APPROVED, created_by=self.user)
        client = APIClient()
        client.login(username="testuser", password="test1234")

        with mock.patch("agri.remote_sense.sentinel_client.search_available_scenes",
                        return_value=[]) as m:
            res = client.get(
                "/api/agri/satellite/availability/?project=%s&date_from=2026-07-01&date_to=2026-08-11&field=%s"
                % (self.project.id, field.id))
        self.assertEqual(res.status_code, status.HTTP_200_OK)
        geom_arg = m.call_args[0][0]
        self.assertTrue(geom_arg.equals(target_geom))

    def test_satellite_availability_endpoint_no_boundary_raises_validation_error(self):
        # SatellitePullError from AOI resolution must surface as 400, not 500.
        client = APIClient()
        client.login(username="testuser", password="test1234")
        res = client.get("/api/agri/satellite/availability/?project=%s&date_from=2026-07-01&date_to=2026-08-11"
                         % self.project.id)
        self.assertEqual(res.status_code, status.HTTP_400_BAD_REQUEST)
