"""
Precise Agric System branding.

Single source of truth for the app name (from settings.APP_NAME) and the
brand-carrying theme colors. Used in two places:
  - app/boot.py            -> brands a *fresh* install when the default Theme is created
  - manage.py apply_branding -> re-applies to an *existing* database on demand

Only brand-carrying colors are overridden; all other Theme colors keep
WebODM's defaults for contrast/readability. apply_* functions are idempotent
and never force-override on every boot, so manual /admin tweaks are preserved.
"""

from webodm import settings

# Crop-green palette. Keys must match ColorField names on app.models.Theme.
THEME_COLORS = {
    'primary': '#1b3a2b',            # most text, icons, borders
    'tertiary': '#2e7d32',           # navigation links
    'button_primary': '#2e7d32',     # primary buttons
    'header_background': '#2e7d32',  # site header background
    'highlight': '#f2f7f2',          # panel backgrounds
}


def apply_theme_colors(theme):
    """Set the brand colors on a Theme instance. Saves only if something changed."""
    changed = False
    for field, value in THEME_COLORS.items():
        if getattr(theme, field, None) != value:
            setattr(theme, field, value)
            changed = True
    if changed:
        theme.save()
    return changed


def apply_branding():
    """
    Apply name + theme colors to the singleton Setting and active Theme of the
    current database. Idempotent. Returns (setting, theme); either may be None
    if the rows don't exist yet (branding will then apply on first boot).
    """
    from app.models import Setting, Theme

    setting = Setting.objects.first()
    theme = None

    if setting is not None:
        if setting.app_name != settings.APP_NAME:
            setting.app_name = settings.APP_NAME
            setting.save()
        theme = setting.theme

    if theme is None:
        theme = Theme.objects.first()
    if theme is not None:
        apply_theme_colors(theme)

    return setting, theme
