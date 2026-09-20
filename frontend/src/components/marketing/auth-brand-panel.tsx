import type { LucideIcon } from "lucide-react";
import type { ReactNode } from "react";

import { Logo } from "@/components/marketing/logo";

export type AuthFeature = {
  icon: LucideIcon;
  title: string;
  description: string;
};

/** Shared dark brand/feature panel for the login and register split layouts. */
export function AuthBrandPanel({
  eyebrow,
  heading,
  description,
  features,
  footnote,
}: {
  eyebrow: string;
  heading: ReactNode;
  description: string;
  features: AuthFeature[];
  footnote: string;
}) {
  return (
    <div className="relative flex flex-col justify-between overflow-hidden bg-[#0B1220] px-8 py-8 sm:px-12 sm:py-14 lg:w-[46%] lg:px-14 lg:py-16">
      <div
        aria-hidden="true"
        className="pointer-events-none absolute inset-0 opacity-[0.06]"
        style={{
          backgroundImage:
            "radial-gradient(circle, rgba(255,255,255,0.8) 1px, transparent 1px)",
          backgroundSize: "18px 18px",
        }}
      />
      <div className="relative">
        <Logo variant="light" />
        <p className="mt-6 text-xs font-semibold tracking-[0.18em] text-[#12A594] lg:mt-10">
          {eyebrow}
        </p>
        <h2 className="mt-3 max-w-sm font-display text-2xl font-semibold tracking-tight text-white sm:text-3xl lg:text-4xl">
          {heading}
        </h2>
        <p className="mt-3 max-w-sm text-sm text-white/60 lg:mt-4 lg:text-[0.95rem]">
          {description}
        </p>

        {/* Kept compact on mobile per design spec - the branding panel sits
            above the form there, so the full feature list would push the
            form (the thing people actually came to use) below the fold. */}
        <ul className="mt-10 hidden space-y-5 lg:block">
          {features.map(({ icon: Icon, title, description: featureDescription }) => (
            <li key={title} className="flex gap-3">
              <div className="flex size-8 shrink-0 items-center justify-center rounded-lg bg-white/10 text-[#12A594]">
                <Icon className="size-4" aria-hidden="true" />
              </div>
              <div>
                <p className="text-sm font-medium text-white">{title}</p>
                <p className="text-sm text-white/50">{featureDescription}</p>
              </div>
            </li>
          ))}
        </ul>
      </div>
      <p className="relative hidden text-sm text-white/40 lg:block">{footnote}</p>
    </div>
  );
}
