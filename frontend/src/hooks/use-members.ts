"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiFetch } from "@/lib/api-client";
import type { Member, OrgRole } from "@/lib/types";

export function membersQueryKey(organizationId: string) {
  return ["members", organizationId] as const;
}

export function useMembers(organizationId: string | null) {
  return useQuery<Member[]>({
    queryKey: membersQueryKey(organizationId ?? ""),
    queryFn: () => apiFetch<Member[]>(`/api/v1/organizations/${organizationId}/members`),
    enabled: !!organizationId,
  });
}

export function useAddMember(organizationId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (input: { email: string; role: OrgRole }) =>
      apiFetch<Member>(`/api/v1/organizations/${organizationId}/members`, {
        method: "POST",
        body: JSON.stringify(input),
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: membersQueryKey(organizationId) });
    },
  });
}

export function useUpdateMemberRole(organizationId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({ memberId, role }: { memberId: string; role: OrgRole }) =>
      apiFetch<Member>(`/api/v1/organizations/${organizationId}/members/${memberId}`, {
        method: "PATCH",
        body: JSON.stringify({ role }),
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: membersQueryKey(organizationId) });
    },
  });
}

export function useRemoveMember(organizationId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (memberId: string) =>
      apiFetch<void>(`/api/v1/organizations/${organizationId}/members/${memberId}`, {
        method: "DELETE",
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: membersQueryKey(organizationId) });
    },
  });
}
