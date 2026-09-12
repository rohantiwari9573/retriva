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
    // Real polling, not a fake progress bar: PROCESSING is the only status
    // that can still change on its own, so only poll while at least one
    // document is in that state. No WebSockets for this yet - see
    // docs/document-ingestion.md.
    refetchInterval: (query) => {
      const data = query.state.data;
      const stillProcessing = data?.items.some((doc) => doc.status === "PROCESSING");
      return stillProcessing ? 3000 : false;
    },
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

export function useRetryDocument(organizationId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (documentId: string) =>
      apiFetch<Document>(
        `/api/v1/organizations/${organizationId}/documents/${documentId}/retry`,
        { method: "POST" }
      ),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["documents", organizationId] });
    },
  });
}
