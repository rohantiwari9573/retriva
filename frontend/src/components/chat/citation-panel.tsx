"use client";

import { FileText } from "lucide-react";

import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import type { Citation } from "@/lib/types";

export function CitationPanel({
  citation,
  onOpenChange,
}: {
  citation: Citation | null;
  onOpenChange: (open: boolean) => void;
}) {
  return (
    <Sheet open={!!citation} onOpenChange={onOpenChange}>
      <SheetContent side="right">
        {citation && (
          <>
            <SheetHeader>
              <div className="flex items-center gap-2 text-primary">
                <FileText className="h-4 w-4" />
                <span className="text-xs font-semibold uppercase tracking-wide">
                  Source {citation.id.replace("SOURCE-", "")}
                </span>
              </div>
              <SheetTitle>{citation.document_name}</SheetTitle>
              <SheetDescription>
                {[
                  citation.page !== null ? `Page ${citation.page}` : null,
                  citation.section,
                ]
                  .filter(Boolean)
                  .join(" · ") || "No additional location metadata"}
              </SheetDescription>
            </SheetHeader>
            <div className="mx-4 rounded-md border-l-4 border-primary bg-muted/40 p-4 text-sm leading-relaxed text-foreground">
              <p className="mb-2 text-xs font-medium uppercase tracking-wide text-muted-foreground">
                Evidence supporting this answer
              </p>
              &ldquo;{citation.excerpt}&rdquo;
            </div>
          </>
        )}
      </SheetContent>
    </Sheet>
  );
}
