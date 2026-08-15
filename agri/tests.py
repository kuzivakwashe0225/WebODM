import os
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

        self.assertEqual(payload["field_id"], agri_field.agritrack_field_id)
        self.assertEqual(payload["farm_id"], agri_field.farm.agritrack_farm_id)
        self.assertEqual(payload["scope"], "field")
        self.assertIn("vari_mean", payload["metrics"])
        self.assertIn("canopy_cover_pct", payload["metrics"])
        self.assertIn("weed_count", payload["metrics"])
        self.assertIn("health_score", payload["metrics"])
        self.assertIn("classification", payload["metrics"])
        self.assertIsInstance(payload["recommendations"], list)
        # Not computed by this pipeline -- must not be invented
        self.assertNotIn("plant_count", payload["metrics"])
        self.assertNotIn("height_mean_m", payload["metrics"])
        # AgriTrack's live endpoint requires summary as a string, not our
        # internal report dict (contract mismatch caught via manual curl test)
        self.assertIsInstance(payload["summary"], str)

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
                self.assertEqual(kwargs['json']['field_id'], agri_field.agritrack_field_id)
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
        self.assertEqual(CaptureMeta.objects.get(task=task).capture_date,
                         datetime.date(2026, 5, 1))
        # The synced field was seeded as a DRAFT boundary linked back to the
        # AgriField (so its analysis is pushable to AgriTrack per-field).
        boundaries = list(Boundary.objects.filter(task=task))
        self.assertEqual(len(boundaries), 1)
        self.assertEqual(boundaries[0].agri_field_id, agri_field.id)
        self.assertEqual(boundaries[0].status, Boundary.DRAFT)
        self.assertTrue(boundaries[0].geom.equals(agri_field.boundary))

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
