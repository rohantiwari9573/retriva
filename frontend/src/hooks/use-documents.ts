"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiFetch } from "@/lib/api-client";
import type { Document, DocumentListResponse } from "@/lib/types";

export function documentsQueryKey(organizationId: string, page: number) {
  return ["documents", organizationId, page] as const;
}

export function useDocuments(organizationId: string | null, page: number) {
  return useQuery<DocumentListResponse>({
    queryKey: documentsQueryKey(organizationId ?? "", page),
    queryFn: () =>
      apiFetch<DocumentListResponse>(
        `/api/v1/organizations/${organizationId}/documents?page=${page}&page_size=20`
      ),
    enabled: !!organizationId,
  });
}

export function useUploadDocument(organizationId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (file: File) => {
      const formData = new FormData();
      formData.append("file", file);
      return apiFetch<Document>(`/api/v1/organizations/${organizationId}/documents`, {
        method: "POST",
        body: formData,
      });
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["documents", organizationId] });
    },
  });
}

export function useDeleteDocument(organizationId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (documentId: string) =>
      apiFetch<void>(`/api/v1/organizations/${organizationId}/documents/${documentId}`, {
        method: "DELETE",
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["documents", organizationId] });
    },
  });
}

export function useDownloadDocument(organizationId: string) {
  return useMutation({
    mutationFn: (documentId: string) =>
      apiFetch<{ url: string; expires_in: number }>(
        `/api/v1/organizations/${organizationId}/documents/${documentId}/download`
      ),
  });
}
