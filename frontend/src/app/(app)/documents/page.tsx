"use client";

import { useRef, useState } from "react";
import { Download, FileText, Trash2, Upload } from "lucide-react";
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
  useUploadDocument,
} from "@/hooks/use-documents";
import { ApiError } from "@/lib/api-client";
import { formatFileSize } from "@/lib/format";
import { ROLE_HIERARCHY, type Document } from "@/lib/types";

const ACCEPTED_EXTENSIONS = ".pdf,.docx,.txt,.md";

export default function DocumentsPage() {
  const { organization, isLoading: orgLoading } = useCurrentOrganization();
  const [page] = useState(1);
  const { data, isLoading } = useDocuments(organization?.id ?? null, page);
  const uploadDocument = useUploadDocument(organization?.id ?? "");
  const fileInputRef = useRef<HTMLInputElement>(null);

  const canUpload = organization ? ROLE_HIERARCHY[organization.role] >= ROLE_HIERARCHY.MEMBER : false;
  const canDelete = organization ? ROLE_HIERARCHY[organization.role] >= ROLE_HIERARCHY.ADMIN : false;

  const handleFileChange = (event: React.ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (!file) return;

    uploadDocument.mutate(file, {
      onSuccess: () => toast.success(`${file.name} uploaded`),
      onError: (error) => {
        if (error instanceof ApiError) {
          toast.error(error.message);
        }
      },
    });
  };

  if (orgLoading || !organization) {
    return <Skeleton className="h-64 w-full" />;
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
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
            <Button onClick={() => fileInputRef.current?.click()} disabled={uploadDocument.isPending}>
              <Upload className="h-4 w-4" />
              {uploadDocument.isPending ? "Uploading..." : "Upload document"}
            </Button>
          </>
        )}
      </div>

      <Card>
        <CardHeader>
          <CardTitle>{data?.total ?? 0} documents</CardTitle>
          <CardDescription>
            Newly uploaded documents show as PROCESSING - parsing, chunking, and retrieval
            arrive in a later phase.
          </CardDescription>
        </CardHeader>
        <CardContent>
          {isLoading ? (
            <Skeleton className="h-32 w-full" />
          ) : !data || data.items.length === 0 ? (
            <EmptyState canUpload={canUpload} onUploadClick={() => fileInputRef.current?.click()} />
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Name</TableHead>
                  <TableHead>Size</TableHead>
                  <TableHead>Status</TableHead>
                  <TableHead>Uploaded</TableHead>
                  <TableHead className="w-24" />
                </TableRow>
              </TableHeader>
              <TableBody>
                {data.items.map((document) => (
                  <DocumentRow
                    key={document.id}
                    document={document}
                    organizationId={organization.id}
                    canDelete={canDelete}
                  />
                ))}
              </TableBody>
            </Table>
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

function DocumentRow({
  document,
  organizationId,
  canDelete,
}: {
  document: Document;
  organizationId: string;
  canDelete: boolean;
}) {
  const deleteDocument = useDeleteDocument(organizationId);
  const downloadDocument = useDownloadDocument(organizationId);
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

  return (
    <TableRow>
      <TableCell className="font-medium">{document.original_filename}</TableCell>
      <TableCell>{formatFileSize(document.size_bytes)}</TableCell>
      <TableCell>
        <DocumentStatusBadge status={document.status} />
      </TableCell>
      <TableCell className="text-muted-foreground">
        {new Date(document.created_at).toLocaleDateString()}
      </TableCell>
      <TableCell>
        <div className="flex justify-end gap-1">
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
            <>
              <Button
                variant="ghost"
                size="icon"
                onClick={() => setConfirmOpen(true)}
                aria-label={`Delete ${document.original_filename}`}
              >
                <Trash2 className="h-4 w-4" />
              </Button>
              <AlertDialog open={confirmOpen} onOpenChange={setConfirmOpen}>
                <AlertDialogContent>
                  <AlertDialogHeader>
                    <AlertDialogTitle>Delete {document.original_filename}?</AlertDialogTitle>
                    <AlertDialogDescription>
                      This permanently removes the file and its stored copy. This can&apos;t be
                      undone.
                    </AlertDialogDescription>
                  </AlertDialogHeader>
                  <AlertDialogFooter>
                    <AlertDialogCancel>Cancel</AlertDialogCancel>
                    <AlertDialogAction onClick={handleDelete}>Delete</AlertDialogAction>
                  </AlertDialogFooter>
                </AlertDialogContent>
              </AlertDialog>
            </>
          )}
        </div>
      </TableCell>
    </TableRow>
  );
}
