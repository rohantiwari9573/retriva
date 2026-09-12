export type OrgRole = "OWNER" | "ADMIN" | "MEMBER" | "VIEWER";

export const ROLE_HIERARCHY: Record<OrgRole, number> = {
  VIEWER: 0,
  MEMBER: 1,
  ADMIN: 2,
  OWNER: 3,
};

export type User = {
  id: string;
  email: string;
  full_name: string | null;
  created_at: string;
};

export type Organization = {
  id: string;
  name: string;
  slug: string;
  created_at: string;
  role: OrgRole;
};

export type Member = {
  id: string;
  user_id: string;
  email: string;
  full_name: string | null;
  role: OrgRole;
  created_at: string;
};
