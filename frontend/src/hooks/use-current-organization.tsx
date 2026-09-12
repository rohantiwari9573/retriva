"use client";

import { createContext, useContext, useEffect, useMemo, useState } from "react";
import { useOrganizations } from "@/hooks/use-organizations";
import type { Organization } from "@/lib/types";

const STORAGE_KEY = "nexus:currentOrganizationId";

type CurrentOrganizationContextValue = {
  organization: Organization | null;
  organizations: Organization[];
  isLoading: boolean;
  setOrganizationId: (id: string) => void;
};

const CurrentOrganizationContext = createContext<CurrentOrganizationContextValue | null>(
  null
);

export function CurrentOrganizationProvider({ children }: { children: React.ReactNode }) {
  const { data: organizations, isLoading } = useOrganizations();
  const [organizationId, setOrganizationIdState] = useState<string | null>(null);

  // Restore the last-selected org from this browser (non-sensitive - just an
  // id, never a credential) once the org list has loaded.
  useEffect(() => {
    if (!organizations || organizations.length === 0) return;
    let stored: string | null = null;
    try {
      stored = window.localStorage.getItem(STORAGE_KEY);
    } catch {
      // Private browsing / blocked storage - fall back to the first org.
    }
    const validStored = organizations.find((org) => org.id === stored);
    setOrganizationIdState(validStored ? validStored.id : organizations[0].id);
  }, [organizations]);

  const setOrganizationId = (id: string) => {
    setOrganizationIdState(id);
    try {
      window.localStorage.setItem(STORAGE_KEY, id);
    } catch {
      // Ignore - selection still works for this session via state.
    }
  };

  const organization = useMemo(
    () => organizations?.find((org) => org.id === organizationId) ?? null,
    [organizations, organizationId]
  );

  return (
    <CurrentOrganizationContext.Provider
      value={{
        organization,
        organizations: organizations ?? [],
        isLoading,
        setOrganizationId,
      }}
    >
      {children}
    </CurrentOrganizationContext.Provider>
  );
}

export function useCurrentOrganization() {
  const ctx = useContext(CurrentOrganizationContext);
  if (!ctx) {
    throw new Error(
      "useCurrentOrganization must be used within a CurrentOrganizationProvider"
    );
  }
  return ctx;
}
