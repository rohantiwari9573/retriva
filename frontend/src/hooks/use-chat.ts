"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiFetch } from "@/lib/api-client";
import type {
  ChatResponse,
  ConversationDetail,
  ConversationListResponse,
} from "@/lib/types";

export function conversationsQueryKey(organizationId: string) {
  return ["conversations", organizationId] as const;
}

export function conversationQueryKey(organizationId: string, conversationId: string) {
  return ["conversations", organizationId, conversationId] as const;
}

export function useConversations(organizationId: string | null) {
  return useQuery<ConversationListResponse>({
    queryKey: conversationsQueryKey(organizationId ?? ""),
    queryFn: () =>
      apiFetch<ConversationListResponse>(
        `/api/v1/organizations/${organizationId}/conversations?page=1&page_size=50`
      ),
    enabled: !!organizationId,
  });
}

export function useConversation(organizationId: string | null, conversationId: string | null) {
  return useQuery<ConversationDetail>({
    queryKey: conversationQueryKey(organizationId ?? "", conversationId ?? ""),
    queryFn: () =>
      apiFetch<ConversationDetail>(
        `/api/v1/organizations/${organizationId}/conversations/${conversationId}`
      ),
    enabled: !!organizationId && !!conversationId,
  });
}

export function useSendMessage(organizationId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({
      conversationId,
      message,
    }: {
      conversationId: string | null;
      message: string;
    }) =>
      apiFetch<ChatResponse>(`/api/v1/organizations/${organizationId}/chat`, {
        method: "POST",
        body: JSON.stringify({ conversation_id: conversationId, message }),
      }),
    onSuccess: (data) => {
      queryClient.invalidateQueries({ queryKey: conversationsQueryKey(organizationId) });
      queryClient.invalidateQueries({
        queryKey: conversationQueryKey(organizationId, data.conversation_id),
      });
    },
  });
}

export function useDeleteConversation(organizationId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (conversationId: string) =>
      apiFetch<void>(`/api/v1/organizations/${organizationId}/conversations/${conversationId}`, {
        method: "DELETE",
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: conversationsQueryKey(organizationId) });
    },
  });
}
