import { Badge } from "@/components/ui/badge";
import type { DocumentStatus } from "@/lib/types";

const STATUS_STYLES: Record<DocumentStatus, string> = {
  UPLOADING: "bg-muted text-muted-foreground",
  PROCESSING: "bg-amber-500/15 text-amber-700 dark:text-amber-400",
  READY: "bg-emerald-500/15 text-emerald-700 dark:text-emerald-400",
  FAILED: "bg-destructive/15 text-destructive",
  DELETED: "bg-muted text-muted-foreground",
};

export function DocumentStatusBadge({ status }: { status: DocumentStatus }) {
  return (
    <Badge variant="secondary" className={STATUS_STYLES[status]}>
      {status}
    </Badge>
  );
}
