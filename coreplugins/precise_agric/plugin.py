from django.contrib.auth.decorators import login_required
from django.shortcuts import render
from django.utils.translation import gettext as _

from app.plugins import PluginBase, Menu, MountPoint


class Plugin(PluginBase):
    def include_js_files(self):
        return ['main.js']

    def build_jsx_components(self):
        return ['app.jsx']

    def main_menu(self):
        # Side-menu entry for the season-long progress dashboard. It's farm/field
        # level over many captures, so it lives outside any single map view.
        return [Menu(_("Season Progress"), self.public_url(""), "fa fa-chart-line fa-fw")]

    def app_mount_points(self):
        @login_required
        def season(request):
            # Projects (farms) and their per-field/whole-farm trends are fetched
            # client-side (/api/projects/, /api/agri/seasonal/) so guardian
            # permissions are enforced by the existing API, not re-implemented here.
            return render(request, self.template_path("season.html"),
                          {'title': _("Season Progress")})

        return [MountPoint('$', season)]
