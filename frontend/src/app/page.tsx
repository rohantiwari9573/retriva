"use client";

import Link from "next/link";
import { useEffect } from "react";
import { useRouter } from "next/navigation";

import { Button } from "@/components/ui/button";
import { useCurrentUser } from "@/hooks/use-auth";

// Minimal placeholder - the full marketing landing page (hero, features,
// architecture explainer, etc.) is Phase 9 scope, not Phase 2.
export default function Home() {
  const { data: user, isLoading } = useCurrentUser();
  const router = useRouter();

  useEffect(() => {
    if (!isLoading && user) {
      router.replace("/dashboard");
    }
  }, [isLoading, user, router]);

  return (
    <div className="flex min-h-screen flex-col items-center justify-center gap-6 px-4 text-center">
      <div>
        <h1 className="text-3xl font-semibold tracking-tight">Nexus</h1>
        <p className="mt-2 max-w-md text-muted-foreground">
          Your organization&apos;s knowledge, one intelligent interface.
        </p>
      </div>
      <div className="flex gap-3">
        <Button render={<Link href="/register">Get started</Link>} />
        <Button variant="outline" render={<Link href="/login">Sign in</Link>} />
      </div>
    </div>
  );
}
