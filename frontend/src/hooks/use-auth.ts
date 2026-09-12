"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiFetch, ApiError } from "@/lib/api-client";
import type { User } from "@/lib/types";

export const CURRENT_USER_QUERY_KEY = ["currentUser"] as const;

/**
 * Source of truth for "am I logged in" - the access token lives in an
 * httpOnly cookie the frontend can't read, so this is the only way the UI
 * knows the auth state. A 401 here means "not logged in", not an error to
 * surface, so it resolves to `null` instead of throwing.
 */
export function useCurrentUser() {
  return useQuery<User | null>({
    queryKey: CURRENT_USER_QUERY_KEY,
    queryFn: async () => {
      try {
        return await apiFetch<User>("/api/v1/users/me");
      } catch (error) {
        if (error instanceof ApiError && error.status === 401) {
          return null;
        }
        throw error;
      }
    },
    retry: false,
    staleTime: 60_000,
  });
}

export function useLogin() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (input: { email: string; password: string }) =>
      apiFetch<User>("/api/v1/auth/login", {
        method: "POST",
        body: JSON.stringify(input),
      }),
    onSuccess: (user) => {
      queryClient.setQueryData(CURRENT_USER_QUERY_KEY, user);
    },
  });
}

export function useRegister() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (input: { email: string; password: string; full_name?: string }) =>
      apiFetch<User>("/api/v1/auth/register", {
        method: "POST",
        body: JSON.stringify(input),
      }),
    onSuccess: (user) => {
      queryClient.setQueryData(CURRENT_USER_QUERY_KEY, user);
    },
  });
}

export function useLogout() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () => apiFetch<void>("/api/v1/auth/logout", { method: "POST" }),
    onSuccess: () => {
      queryClient.setQueryData(CURRENT_USER_QUERY_KEY, null);
      queryClient.clear();
    },
  });
}

export function useUpdateProfile() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (input: { full_name: string }) =>
      apiFetch<User>("/api/v1/users/me", {
        method: "PATCH",
        body: JSON.stringify(input),
      }),
    onSuccess: (user) => {
      queryClient.setQueryData(CURRENT_USER_QUERY_KEY, user);
    },
  });
}
