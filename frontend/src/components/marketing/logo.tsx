import { cn } from "@/lib/utils";

/**
 * Shared brand mark for marketing/auth surfaces (landing, login, register).
 * `variant="light"` is for use on dark backgrounds (teal mark, white type);
 * `variant="dark"` (default) is for use on the cream/white surfaces.
 */
export function Logo({
  variant = "dark",
  className,
  wordmarkClassName,
}: {
  variant?: "dark" | "light";
  className?: string;
  wordmarkClassName?: string;
}) {
  const isLight = variant === "light";
  return (
    <div className={cn("flex items-center gap-2", className)}>
      <svg
        width="28"
        height="28"
        viewBox="0 0 28 28"
        fill="none"
        aria-hidden="true"
        className="shrink-0"
      >
        <rect
          width="28"
          height="28"
          rx="8"
          fill={isLight ? "#12A594" : "#0B1220"}
        />
        <path
          d="M9 20V8h5.2c2.4 0 4 1.4 4 3.6 0 1.7-.9 2.9-2.4 3.4L19 20h-2.9l-2.9-4.6H11.5V20H9Zm2.5-6.6h2.5c1.1 0 1.8-.6 1.8-1.7 0-1-.7-1.6-1.8-1.6h-2.5v3.3Z"
          fill={isLight ? "#0B1220" : "#12A594"}
        />
      </svg>
      <span
        className={cn(
          "text-lg font-semibold tracking-tight",
          isLight ? "text-white" : "text-[#0B1220]",
          wordmarkClassName
        )}
      >
        Retriva
      </span>
    </div>
  );
}
