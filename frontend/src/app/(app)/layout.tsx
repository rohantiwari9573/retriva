"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";

import { NavSidebar } from "@/components/layout/nav-sidebar";
import { OrgSwitcher } from "@/components/layout/org-switcher";
import { UserMenu } from "@/components/layout/user-menu";
import { Skeleton } from "@/components/ui/skeleton";
import { CurrentOrganizationProvider } from "@/hooks/use-current-organization";
import { useCurrentUser } from "@/hooks/use-auth";

export default function AppLayout({ children }: { children: React.ReactNode }) {
  const { data: user, isLoading } = useCurrentUser();
  const router = useRouter();

  useEffect(() => {
    // Covers the case middleware's cookie-presence check missed: an access
    // token cookie that exists but has expired. The API call in
    // useCurrentUser is the real authority here.
    if (!isLoading && user === null) {
      router.replace("/login");
    }
  }, [isLoading, user, router]);

  if (isLoading || !user) {
    return (
      <div className="flex min-h-screen items-center justify-center">
        <Skeleton className="h-8 w-48" />
      </div>
    );
  }

  return (
    <CurrentOrganizationProvider>
      <div className="flex min-h-screen flex-col">
        <header className="flex h-14 shrink-0 items-center justify-between border-b px-4">
          <div className="flex items-center gap-4">
            <span className="font-semibold">Nexus</span>
            <OrgSwitcher />
          </div>
          <UserMenu />
        </header>
        <div className="flex flex-1">
          <NavSidebar />
          <main className="flex-1 p-6">{children}</main>
        </div>
      </div>
    </CurrentOrganizationProvider>
  );
}
