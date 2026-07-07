from django.core.management.base import BaseCommand
from app.branding import apply_branding


class Command(BaseCommand):
    help = "Apply Precise Agric System branding (app name + theme colors) to the current database."

    def handle(self, *args, **options):
        setting, theme = apply_branding()
        if setting is None:
            self.stdout.write(self.style.WARNING(
                "No Setting row found yet; branding will apply automatically on first boot."))
        else:
            self.stdout.write(self.style.SUCCESS(
                "Applied branding: name='%s'%s" % (
                    setting.app_name,
                    (", theme='%s'" % theme.name) if theme is not None else "")))
