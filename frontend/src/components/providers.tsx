"use client";

import { MutationCache, QueryCache, QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useState } from "react";
import { toast } from "sonner";
import { Toaster } from "@/components/ui/sonner";
import { TooltipProvider } from "@/components/ui/tooltip";
import { ApiError } from "@/lib/api-client";

// 401/403/404/409/422/429 are meaningful outcomes each page already renders
// inline (redirect to login, form field errors, "already a member", etc.) -
// only surface a generic toast for failures no component was expecting to
// handle, so real bugs are loud instead of silently swallowed.
const HANDLED_STATUSES = new Set([401, 403, 404, 409, 422, 429]);

function reportUnexpectedError(error: unknown) {
  if (error instanceof ApiError) {
    if (HANDLED_STATUSES.has(error.status)) return;
    toast.error(error.message || "Something went wrong. Please try again.");
    return;
  }
  toast.error("Network error. Check your connection and try again.");
}

export function Providers({ children }: { children: React.ReactNode }) {
  const [queryClient] = useState(
    () =>
      new QueryClient({
        defaultOptions: {
          queries: {
            retry: 1,
            staleTime: 30_000,
          },
        },
        queryCache: new QueryCache({
          onError: reportUnexpectedError,
        }),
        mutationCache: new MutationCache({
          onError: reportUnexpectedError,
        }),
      })
  );

  return (
    <QueryClientProvider client={queryClient}>
      <TooltipProvider>
        {children}
        <Toaster />
      </TooltipProvider>
    </QueryClientProvider>
  );
}
