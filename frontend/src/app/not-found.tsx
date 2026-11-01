import Link from "next/link";

import { Logo } from "@/components/marketing/logo";
import { buttonVariants } from "@/components/ui/button";
import { cn } from "@/lib/utils";

export default function NotFound() {
  return (
    <main className="flex min-h-screen flex-col items-center justify-center gap-6 bg-[#FAF8F3] px-6 text-center">
      <Logo />
      <div className="space-y-2">
        <p className="text-sm font-semibold tracking-widest text-[#12A594]">404</p>
        <h1 className="text-3xl font-semibold tracking-tight text-[#0B1220]">Page not found</h1>
        <p className="max-w-sm text-muted-foreground">
          The page you&apos;re looking for doesn&apos;t exist or may have moved.
        </p>
      </div>
      <div className="flex gap-3">
        <Link href="/dashboard" className={cn(buttonVariants())}>
          Go to dashboard
        </Link>
        <Link href="/" className={cn(buttonVariants({ variant: "outline" }))}>
          Back to home
        </Link>
      </div>
    </main>
  );
}
