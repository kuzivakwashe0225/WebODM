from app.plugins import PluginBase
from app.plugins import MountPoint
from .api import TaskWeedDetect
from .api import TaskWeedDownload


class Plugin(PluginBase):
    def include_js_files(self):
        return ['main.js']
        
    def build_jsx_components(self):
        return ['WeedDetect.jsx']

    def api_mount_points(self):
        return [
            MountPoint('task/(?P<pk>[^/.]+)/weeddetect', TaskWeedDetect.as_view()),
            MountPoint('task/[^/.]+/weeddownload/(?P<celery_task_id>.+)', TaskWeedDownload.as_view()),
        ]