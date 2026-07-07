"""
Roles & permissions (Stage 5).

Four seeded groups per the agreed plan (§9):
  - SuperAdmin  full platform control
  - Admin       manages farms/users, can approve outputs, org-wide visibility
  - Technician  uploads captures, draws boundaries, triggers analysis; CANNOT approve
  - Agronomist  read-only for most actions; the authority who approves boundaries
                and analysis runs; org-wide visibility

Users with no role group (legacy accounts) keep owner-scoped contributor
behaviour but can never approve. Django superusers are treated as SuperAdmin.
"""
from django.contrib.auth.models import Group

ROLE_SUPERADMIN = 'SuperAdmin'
ROLE_ADMIN = 'Admin'
ROLE_TECHNICIAN = 'Technician'
ROLE_AGRONOMIST = 'Agronomist'

AGRI_ROLES = (ROLE_SUPERADMIN, ROLE_ADMIN, ROLE_TECHNICIAN, ROLE_AGRONOMIST)


def setup_roles():
    """Idempotently create the four role groups. Called from app.boot.boot()."""
    created = []
    for name in AGRI_ROLES:
        _, was_created = Group.objects.get_or_create(name=name)
        if was_created:
            created.append(name)
    return created


def get_role(user):
    """The user's agri role, or None. Django superusers count as SuperAdmin."""
    if user.is_superuser:
        return ROLE_SUPERADMIN
    names = set(user.groups.values_list('name', flat=True))
    for role in (ROLE_SUPERADMIN, ROLE_ADMIN, ROLE_AGRONOMIST, ROLE_TECHNICIAN):
        if role in names:
            return role
    return None


def is_reviewer(user):
    """Reviewers (org-wide visibility + approval authority): SuperAdmin, Admin, Agronomist."""
    return get_role(user) in (ROLE_SUPERADMIN, ROLE_ADMIN, ROLE_AGRONOMIST)


def can_contribute(user, project):
    """
    May the user upload captures / draw boundaries / trigger analysis on this project?
    SuperAdmin/Admin: anywhere. Agronomist: never (read-only). Technician/legacy: own projects.
    """
    role = get_role(user)
    if role in (ROLE_SUPERADMIN, ROLE_ADMIN):
        return True
    if role == ROLE_AGRONOMIST:
        return False
    return project.owner_id == user.id
