from app.models.enums import ROLE_HIERARCHY, OrgRole


def test_role_hierarchy_ordering():
    assert ROLE_HIERARCHY[OrgRole.VIEWER] < ROLE_HIERARCHY[OrgRole.MEMBER]
    assert ROLE_HIERARCHY[OrgRole.MEMBER] < ROLE_HIERARCHY[OrgRole.ADMIN]
    assert ROLE_HIERARCHY[OrgRole.ADMIN] < ROLE_HIERARCHY[OrgRole.OWNER]


def test_every_role_is_ranked():
    assert set(ROLE_HIERARCHY.keys()) == set(OrgRole)
