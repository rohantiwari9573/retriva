import { describe, expect, it } from "vitest";

import { ROLE_HIERARCHY, type OrgRole } from "@/lib/types";

// This ordering is the sole basis for every RBAC-visibility check across the
// frontend (documents/page.tsx's canUpload/canDelete, settings/members'
// canManage, conversation-sidebar's canDelete, etc.) - a wrong ordering
// here would silently show or hide controls incorrectly everywhere at
// once. The backend remains the actual authorization boundary (see
// docs/security.md); this only verifies the frontend's own copy of the
// hierarchy is internally consistent.
describe("ROLE_HIERARCHY", () => {
  it("orders roles from least to most privileged", () => {
    expect(ROLE_HIERARCHY.VIEWER).toBeLessThan(ROLE_HIERARCHY.MEMBER);
    expect(ROLE_HIERARCHY.MEMBER).toBeLessThan(ROLE_HIERARCHY.ADMIN);
    expect(ROLE_HIERARCHY.ADMIN).toBeLessThan(ROLE_HIERARCHY.OWNER);
  });

  it("assigns every OrgRole a distinct rank", () => {
    const roles: OrgRole[] = ["OWNER", "ADMIN", "MEMBER", "VIEWER"];
    const ranks = roles.map((role) => ROLE_HIERARCHY[role]);
    expect(new Set(ranks).size).toBe(roles.length);
  });

  it("a MEMBER does not meet the ADMIN threshold used to gate delete/manage controls", () => {
    expect(ROLE_HIERARCHY.MEMBER >= ROLE_HIERARCHY.ADMIN).toBe(false);
  });

  it("an ADMIN does meet the ADMIN threshold", () => {
    expect(ROLE_HIERARCHY.ADMIN >= ROLE_HIERARCHY.ADMIN).toBe(true);
  });
});
