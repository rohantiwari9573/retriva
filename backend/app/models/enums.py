"""Shared enums for RBAC.

ROLE_HIERARCHY encodes the "each higher role includes lower permissions" model
described in the project spec (OWNER=everything, ADMIN=manage users/documents/
analytics/ask, MEMBER=upload+ask+view, VIEWER=ask+view). require_role() compares
these integers rather than hardcoding role lists per permission.
"""

import enum


class OrgRole(enum.StrEnum):
    OWNER = "OWNER"
    ADMIN = "ADMIN"
    MEMBER = "MEMBER"
    VIEWER = "VIEWER"


ROLE_HIERARCHY: dict[OrgRole, int] = {
    OrgRole.VIEWER: 0,
    OrgRole.MEMBER: 1,
    OrgRole.ADMIN: 2,
    OrgRole.OWNER: 3,
}
