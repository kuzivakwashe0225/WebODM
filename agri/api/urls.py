from django.conf.urls import url
from rest_framework.routers import DefaultRouter
from .views import (BoundaryViewSet, CaptureUploadView, AnalysisRunViewSet,
                    FieldListView, ReuseBoundariesView)
from .seasonal import SeasonalView
from .satellite import (SatelliteImageryPullView, SatelliteComparisonPullView,
                        SatelliteAvailabilityView)

router = DefaultRouter()
router.register(r'boundaries', BoundaryViewSet, basename='boundaries')
router.register(r'analysis', AnalysisRunViewSet, basename='analysis')

urlpatterns = router.urls + [
    url(r'^captures/$', CaptureUploadView.as_view()),
    url(r'^reuse-boundaries/$', ReuseBoundariesView.as_view()),
    url(r'^fields/$', FieldListView.as_view()),
    url(r'^seasonal/$', SeasonalView.as_view()),
    url(r'^satellite/imagery/$', SatelliteImageryPullView.as_view()),
    url(r'^satellite/compare/$', SatelliteComparisonPullView.as_view()),
    url(r'^satellite/availability/$', SatelliteAvailabilityView.as_view()),
]
