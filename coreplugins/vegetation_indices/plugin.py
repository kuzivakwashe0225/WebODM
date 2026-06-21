from app.plugins import PluginBase
from app.plugins import MountPoint
from .api import TaskVegetation
from .api import TaskVegetationDownload


class Plugin(PluginBase):
    def include_js_files(self):
        return ['main.js']
        
    def build_jsx_components(self):
        return ['VegetationIndices.jsx']

    def api_mount_points(self):
        return [
            MountPoint('task/(?P<pk>[^/.]+)/vegetation', TaskVegetation.as_view()),
            MountPoint('task/[^/.]+/download/(?P<celery_task_id>.+)', TaskVegetationDownload.as_view()),
        ]