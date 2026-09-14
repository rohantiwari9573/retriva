"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";

import { MobileNav } from "@/components/layout/mobile-nav";
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
        <header className="flex h-14 shrink-0 items-center justify-between gap-2 border-b px-3 sm:px-4">
          <div className="flex min-w-0 items-center gap-2 sm:gap-4">
            <MobileNav />
            <span className="hidden shrink-0 font-semibold sm:inline">Nexus</span>
            <OrgSwitcher />
          </div>
          <UserMenu />
        </header>
        <div className="flex flex-1 overflow-hidden">
          <NavSidebar />
          <main className="flex-1 overflow-y-auto p-4 sm:p-6">{children}</main>
        </div>
      </div>
    </CurrentOrganizationProvider>
  );
}
