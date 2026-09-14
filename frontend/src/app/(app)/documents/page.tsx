"use client";

import { useRef, useState } from "react";
import { Download, FileText, RotateCw, Trash2, Upload } from "lucide-react";
import { toast } from "sonner";

import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { DocumentStatusBadge } from "@/components/documents/document-status-badge";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { useCurrentOrganization } from "@/hooks/use-current-organization";
import {
  useDeleteDocument,
  useDocuments,
  useDownloadDocument,
  useRetryDocument,
  useUploadDocument,
} from "@/hooks/use-documents";
import { ApiError } from "@/lib/api-client";
import { formatFileSize } from "@/lib/format";
import { ROLE_HIERARCHY, type Document } from "@/lib/types";
import { cn } from "@/lib/utils";

const ACCEPTED_EXTENSIONS = ".pdf,.docx,.txt,.md";

export default function DocumentsPage() {
  const { organization, isLoading: orgLoading } = useCurrentOrganization();
  const [page] = useState(1);
  const { data, isLoading } = useDocuments(organization?.id ?? null, page);
  const uploadDocument = useUploadDocument(organization?.id ?? "");
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [isDraggingOver, setIsDraggingOver] = useState(false);

  const canUpload = organization ? ROLE_HIERARCHY[organization.role] >= ROLE_HIERARCHY.MEMBER : false;
  const canDelete = organization ? ROLE_HIERARCHY[organization.role] >= ROLE_HIERARCHY.ADMIN : false;

  const doUpload = (file: File) => {
    uploadDocument.mutate(file, {
      onSuccess: () => toast.success(`${file.name} uploaded - processing has started.`),
      onError: (error) => {
        // Upload completing is distinct from processing finishing (Phase 9
        // Step 5) - a 201 here only means the file is durably stored and
        // queued; the document row's own status badge (PROCESSING -> READY/
        // FAILED) is what reports ingestion outcome, not this toast.
        if (error instanceof ApiError) toast.error(error.message);
      },
    });
  };

  const handleFileChange = (event: React.ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (file) doUpload(file);
  };

  const handleDrop = (event: React.DragEvent<HTMLDivElement>) => {
    event.preventDefault();
    setIsDraggingOver(false);
    if (!canUpload) return;
    const file = event.dataTransfer.files?.[0];
    if (file) doUpload(file);
  };

  if (orgLoading || !organization) {
    return <Skeleton className="h-64 w-full" />;
  }

  return (
    <div
      className="space-y-6"
      onDragOver={(e) => {
        if (!canUpload) return;
        e.preventDefault();
        setIsDraggingOver(true);
      }}
      onDragLeave={() => setIsDraggingOver(false)}
      onDrop={handleDrop}
    >
      <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h1 className="text-2xl font-semibold">Documents</h1>
          <p className="text-muted-foreground">
            Files uploaded to {organization.name}. Supported types: PDF, DOCX, TXT, Markdown.
          </p>
        </div>
        {canUpload && (
          <>
            <input
              ref={fileInputRef}
              type="file"
              accept={ACCEPTED_EXTENSIONS}
              className="hidden"
              onChange={handleFileChange}
            />
            <Button
              onClick={() => fileInputRef.current?.click()}
              disabled={uploadDocument.isPending}
              className="sm:w-auto"
            >
              <Upload className="h-4 w-4" />
              {uploadDocument.isPending ? "Uploading..." : "Upload document"}
            </Button>
          </>
        )}
      </div>

      <Card
        className={cn(
          "relative transition-colors",
          isDraggingOver && "border-primary ring-2 ring-primary/30"
        )}
      >
        {isDraggingOver && (
          <div className="pointer-events-none absolute inset-0 z-10 flex items-center justify-center rounded-xl bg-primary/5">
            <p className="rounded-md bg-background px-4 py-2 text-sm font-medium shadow">
              Drop to upload
            </p>
          </div>
        )}
        <CardHeader>
          <CardTitle>{data?.total ?? 0} documents</CardTitle>
          <CardDescription>
            {canUpload ? "Drag and drop a file anywhere on this page, or use Upload document. " : ""}
            Newly uploaded documents show as PROCESSING while they&apos;re parsed, chunked, and
            embedded. This list updates automatically until processing finishes.
          </CardDescription>
        </CardHeader>
        <CardContent>
          {isLoading ? (
            <Skeleton className="h-32 w-full" />
          ) : !data || data.items.length === 0 ? (
            <EmptyState canUpload={canUpload} onUploadClick={() => fileInputRef.current?.click()} />
          ) : (
            <>
              {/* Desktop/tablet: a real table. Mobile: a card list below -
                  Phase 9 Step 4 explicitly rejects forcing a desktop-width
                  table on small screens rather than just shrinking it. */}
              <Table className="hidden md:table">
                <TableHeader>
                  <TableRow>
                    <TableHead>Name</TableHead>
                    <TableHead>Size</TableHead>
                    <TableHead>Status</TableHead>
                    <TableHead>Uploaded</TableHead>
                    <TableHead className="w-28" />
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {data.items.map((document) => (
                    <DocumentRow
                      key={document.id}
                      document={document}
                      organizationId={organization.id}
                      canDelete={canDelete}
                      canRetry={canUpload}
                    />
                  ))}
                </TableBody>
              </Table>
              <ul className="space-y-3 md:hidden">
                {data.items.map((document) => (
                  <DocumentCard
                    key={document.id}
                    document={document}
                    organizationId={organization.id}
                    canDelete={canDelete}
                    canRetry={canUpload}
                  />
                ))}
              </ul>
            </>
          )}
        </CardContent>
      </Card>
    </div>
  );
}

function EmptyState({
  canUpload,
  onUploadClick,
}: {
  canUpload: boolean;
  onUploadClick: () => void;
}) {
  return (
    <div className="flex flex-col items-center justify-center gap-3 py-16 text-center">
      <FileText className="h-10 w-10 text-muted-foreground" />
      <div>
        <p className="font-medium">No documents yet</p>
        <p className="text-sm text-muted-foreground">
          {canUpload
            ? "Upload your first document to start building this organization's knowledge base."
            : "Ask an admin or member to upload documents here."}
        </p>
      </div>
      {canUpload && (
        <Button variant="outline" onClick={onUploadClick}>
          <Upload className="h-4 w-4" />
          Upload document
        </Button>
      )}
    </div>
  );
}

/** Shared mutation wiring + dialogs between the table row (desktop) and
 * card (mobile) presentations - only the layout differs below. */
function useDocumentActions(document: Document, organizationId: string) {
  const deleteDocument = useDeleteDocument(organizationId);
  const downloadDocument = useDownloadDocument(organizationId);
  const retryDocument = useRetryDocument(organizationId);
  const [confirmOpen, setConfirmOpen] = useState(false);

  const handleDownload = () => {
    downloadDocument.mutate(document.id, {
      onSuccess: (result) => {
        window.open(result.url, "_blank", "noopener,noreferrer");
      },
      onError: (error) => {
        if (error instanceof ApiError) toast.error(error.message);
      },
    });
  };

  const handleDelete = () => {
    deleteDocument.mutate(document.id, {
      onSuccess: () => toast.success(`${document.original_filename} deleted`),
      onError: (error) => {
        if (error instanceof ApiError) toast.error(error.message);
      },
    });
    setConfirmOpen(false);
  };

  const handleRetry = () => {
    retryDocument.mutate(document.id, {
      onSuccess: () => toast.success(`Retrying ${document.original_filename}`),
      onError: (error) => {
        if (error instanceof ApiError) toast.error(error.message);
      },
    });
  };

  return {
    downloadDocument,
    retryDocument,
    confirmOpen,
    setConfirmOpen,
    handleDownload,
    handleDelete,
    handleRetry,
  };
}

function DeleteConfirmDialog({
  document,
  open,
  onOpenChange,
  onConfirm,
}: {
  document: Document;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onConfirm: () => void;
}) {
  return (
    <AlertDialog open={open} onOpenChange={onOpenChange}>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>Delete {document.original_filename}?</AlertDialogTitle>
          <AlertDialogDescription>
            This permanently removes the file and its stored copy. This can&apos;t be undone.
          </AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel>Cancel</AlertDialogCancel>
          <AlertDialogAction onClick={onConfirm}>Delete</AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}

function DocumentRow({
  document,
  organizationId,
  canDelete,
  canRetry,
}: {
  document: Document;
  organizationId: string;
  canDelete: boolean;
  canRetry: boolean;
}) {
  const {
    downloadDocument,
    retryDocument,
    confirmOpen,
    setConfirmOpen,
    handleDownload,
    handleDelete,
    handleRetry,
  } = useDocumentActions(document, organizationId);

  return (
    <TableRow>
      <TableCell className="max-w-xs font-medium">
        <span className="block truncate">{document.original_filename}</span>
        {document.status === "FAILED" && document.failure_reason && (
          <p className="mt-0.5 text-xs font-normal text-destructive">{document.failure_reason}</p>
        )}
      </TableCell>
      <TableCell>{formatFileSize(document.size_bytes)}</TableCell>
      <TableCell>
        <DocumentStatusBadge status={document.status} />
      </TableCell>
      <TableCell className="text-muted-foreground">
        {new Date(document.created_at).toLocaleDateString()}
      </TableCell>
      <TableCell>
        <div className="flex justify-end gap-1">
          {document.status === "FAILED" && canRetry && (
            <Button
              variant="ghost"
              size="icon"
              onClick={handleRetry}
              disabled={retryDocument.isPending}
              aria-label={`Retry processing ${document.original_filename}`}
            >
              <RotateCw className="h-4 w-4" />
            </Button>
          )}
          <Button
            variant="ghost"
            size="icon"
            onClick={handleDownload}
            disabled={downloadDocument.isPending}
            aria-label={`Download ${document.original_filename}`}
          >
            <Download className="h-4 w-4" />
          </Button>
          {canDelete && (
            <Button
              variant="ghost"
              size="icon"
              onClick={() => setConfirmOpen(true)}
              aria-label={`Delete ${document.original_filename}`}
            >
              <Trash2 className="h-4 w-4" />
            </Button>
          )}
        </div>
      </TableCell>
      {canDelete && (
        <DeleteConfirmDialog
          document={document}
          open={confirmOpen}
          onOpenChange={setConfirmOpen}
          onConfirm={handleDelete}
        />
      )}
    </TableRow>
  );
}

function DocumentCard({
  document,
  organizationId,
  canDelete,
  canRetry,
}: {
  document: Document;
  organizationId: string;
  canDelete: boolean;
  canRetry: boolean;
}) {
  const {
    downloadDocument,
    retryDocument,
    confirmOpen,
    setConfirmOpen,
    handleDownload,
    handleDelete,
    handleRetry,
  } = useDocumentActions(document, organizationId);

  return (
    <li className="rounded-lg border p-3">
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <p className="truncate text-sm font-medium">{document.original_filename}</p>
          <p className="text-xs text-muted-foreground">
            {formatFileSize(document.size_bytes)} ·{" "}
            {new Date(document.created_at).toLocaleDateString()}
          </p>
        </div>
        <DocumentStatusBadge status={document.status} />
      </div>
      {document.status === "FAILED" && document.failure_reason && (
        <p className="mt-1 text-xs text-destructive">{document.failure_reason}</p>
      )}
      <div className="mt-2 flex justify-end gap-1 border-t pt-2">
        {document.status === "FAILED" && canRetry && (
          <Button
            variant="ghost"
            size="icon"
            onClick={handleRetry}
            disabled={retryDocument.isPending}
            aria-label={`Retry processing ${document.original_filename}`}
          >
            <RotateCw className="h-4 w-4" />
          </Button>
        )}
        <Button
          variant="ghost"
          size="icon"
          onClick={handleDownload}
          disabled={downloadDocument.isPending}
          aria-label={`Download ${document.original_filename}`}
        >
          <Download className="h-4 w-4" />
        </Button>
        {canDelete && (
          <Button
            variant="ghost"
            size="icon"
            onClick={() => setConfirmOpen(true)}
            aria-label={`Delete ${document.original_filename}`}
          >
            <Trash2 className="h-4 w-4" />
          </Button>
        )}
      </div>
      {canDelete && (
        <DeleteConfirmDialog
          document={document}
          open={confirmOpen}
          onOpenChange={setConfirmOpen}
          onConfirm={handleDelete}
        />
      )}
    </li>
  );
}
