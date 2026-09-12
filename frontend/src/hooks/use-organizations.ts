"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiFetch } from "@/lib/api-client";
import type { Organization } from "@/lib/types";

export const ORGANIZATIONS_QUERY_KEY = ["organizations"] as const;

export function useOrganizations() {
  return useQuery<Organization[]>({
    queryKey: ORGANIZATIONS_QUERY_KEY,
    queryFn: () => apiFetch<Organization[]>("/api/v1/organizations"),
  });
}

export function useOrganization(organizationId: string | null) {
  return useQuery<Organization>({
    queryKey: ["organization", organizationId],
    queryFn: () => apiFetch<Organization>(`/api/v1/organizations/${organizationId}`),
    enabled: !!organizationId,
  });
}

export function useCreateOrganization() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (input: { name: string }) =>
      apiFetch<Organization>("/api/v1/organizations", {
        method: "POST",
        body: JSON.stringify(input),
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ORGANIZATIONS_QUERY_KEY });
    },
  });
}

export function useUpdateOrganization(organizationId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (input: { name: string }) =>
      apiFetch<Organization>(`/api/v1/organizations/${organizationId}`, {
        method: "PATCH",
        body: JSON.stringify(input),
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ORGANIZATIONS_QUERY_KEY });
      queryClient.invalidateQueries({ queryKey: ["organization", organizationId] });
    },
  });
}
