from app.plugins import PluginBase
from app.plugins import MountPoint
from .api import TaskHeight
from .api import TaskHeightDownload


class Plugin(PluginBase):
    def include_js_files(self):
        return ['main.js']
        
    def build_jsx_components(self):
        return ['PlantHeight.jsx']

    def api_mount_points(self):
        return [
            MountPoint('task/(?P<pk>[^/.]+)/height', TaskHeight.as_view()),
            MountPoint('task/[^/.]+/download/(?P<celery_task_id>.+)', TaskHeightDownload.as_view()),
        ]