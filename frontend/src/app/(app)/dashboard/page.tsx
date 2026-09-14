"use client";

import { useState } from "react";
import { Building2, FileText, MessageSquare, Plus, Upload } from "lucide-react";
import Link from "next/link";

import { Button, buttonVariants } from "@/components/ui/button";
import { cn } from "@/lib/utils";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { DocumentStatusBadge } from "@/components/documents/document-status-badge";
import { Skeleton } from "@/components/ui/skeleton";
import { CreateOrganizationDialog } from "@/components/organizations/create-organization-dialog";
import { useCurrentUser } from "@/hooks/use-auth";
import { useCurrentOrganization } from "@/hooks/use-current-organization";
import { useConversations } from "@/hooks/use-chat";
import { useDocuments } from "@/hooks/use-documents";
import type { DocumentStatus } from "@/lib/types";

export default function DashboardPage() {
  const { data: user } = useCurrentUser();
  const { organization, organizations, isLoading, setOrganizationId } = useCurrentOrganization();
  const [createOpen, setCreateOpen] = useState(false);

  // page_size=100 (the backend's documented max - see
  // app/core/config.py::DOCUMENTS_PAGE_SIZE_MAX) rather than a dedicated
  // stats endpoint: there isn't one, and the Phase 9 spec is explicit that
  // fabricating numbers to fill a card is worse than using what's actually
  // available. `total` below is always the real, backend-reported count;
  // the per-status breakdown is computed from whatever page of items this
  // fetched, which is the complete set for any organization with 100 or
  // fewer documents and an honest most-recent-100 slice otherwise (see the
  // note rendered near the status cards).
  const documents = useDocuments(organization?.id ?? null, 1);
  const conversations = useConversations(organization?.id ?? null);

  if (isLoading) {
    return (
      <div className="space-y-4">
        <Skeleton className="h-8 w-64" />
        <Skeleton className="h-32 w-full" />
      </div>
    );
  }

  if (organizations.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center gap-4 rounded-lg border border-dashed py-24 text-center">
        <Building2 className="h-10 w-10 text-muted-foreground" />
        <div>
          <h2 className="text-lg font-semibold">Create your first organization</h2>
          <p className="mt-1 text-sm text-muted-foreground">
            Organizations hold your team&apos;s documents, conversations, and members.
          </p>
        </div>
        <Button onClick={() => setCreateOpen(true)}>
          <Plus className="h-4 w-4" />
          Create organization
        </Button>
        <CreateOrganizationDialog
          open={createOpen}
          onOpenChange={setCreateOpen}
          onCreated={setOrganizationId}
        />
      </div>
    );
  }

  const items = documents.data?.items ?? [];
  const statusCounts: Record<DocumentStatus, number> = {
    UPLOADING: 0,
    PROCESSING: 0,
    READY: 0,
    FAILED: 0,
    DELETED: 0,
  };
  for (const doc of items) statusCounts[doc.status] += 1;
  const totalDocuments = documents.data?.total ?? 0;
  const countIsPartial = totalDocuments > items.length;

  const hasNoDocuments = !documents.isLoading && totalDocuments === 0;
  const hasNoConversations = !conversations.isLoading && (conversations.data?.items.length ?? 0) === 0;

  const recentDocuments = [...items]
    .sort((a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime())
    .slice(0, 5);
  const recentConversations = [...(conversations.data?.items ?? [])]
    .sort((a, b) => new Date(b.updated_at).getTime() - new Date(a.updated_at).getTime())
    .slice(0, 5);

  return (
    <div className="space-y-6">
      <div className="flex flex-col gap-4 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h1 className="text-2xl font-semibold">
            Welcome back{user?.full_name ? `, ${user.full_name}` : ""}
          </h1>
          <p className="text-muted-foreground">
            {organization ? `You're working in ${organization.name}.` : "Select an organization to get started."}
          </p>
        </div>
        <div className="flex gap-2">
          <Link href="/documents" className={cn(buttonVariants({ variant: "outline" }), "gap-1.5")}>
            <Upload className="h-4 w-4" />
            Upload document
          </Link>
          <Link href="/chat" className={cn(buttonVariants({ variant: "default" }), "gap-1.5")}>
            <MessageSquare className="h-4 w-4" />
            Start a conversation
          </Link>
        </div>
      </div>

      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-5">
        <StatCard label="Total documents" value={documents.isLoading ? undefined : totalDocuments} />
        <StatCard
          label="Ready"
          value={documents.isLoading ? undefined : statusCounts.READY}
          valueClassName="text-emerald-600 dark:text-emerald-400"
        />
        <StatCard
          label="Processing"
          value={documents.isLoading ? undefined : statusCounts.PROCESSING}
          valueClassName="text-amber-600 dark:text-amber-400"
        />
        <StatCard
          label="Failed"
          value={documents.isLoading ? undefined : statusCounts.FAILED}
          valueClassName={statusCounts.FAILED > 0 ? "text-destructive" : undefined}
        />
        <StatCard
          label="Conversations"
          value={conversations.isLoading ? undefined : (conversations.data?.total ?? 0)}
        />
      </div>
      {countIsPartial && (
        <p className="-mt-4 text-xs text-muted-foreground">
          Status breakdown reflects the {items.length} most recently uploaded documents out of{" "}
          {totalDocuments} total.
        </p>
      )}

      <div className="grid gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle>Recent documents</CardTitle>
            <CardDescription>The latest files uploaded to this organization.</CardDescription>
          </CardHeader>
          <CardContent>
            {documents.isLoading ? (
              <div className="space-y-2">
                <Skeleton className="h-9 w-full" />
                <Skeleton className="h-9 w-full" />
              </div>
            ) : hasNoDocuments ? (
              <EmptyCard
                icon={FileText}
                title="No documents yet"
                description="Upload your first knowledge source to start asking questions."
                actionHref="/documents"
                actionLabel="Upload document"
              />
            ) : (
              <ul className="divide-y">
                {recentDocuments.map((doc) => (
                  <li key={doc.id} className="flex items-center justify-between gap-3 py-2.5 text-sm">
                    <span className="truncate font-medium">{doc.original_filename}</span>
                    <DocumentStatusBadge status={doc.status} />
                  </li>
                ))}
              </ul>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Recent conversations</CardTitle>
            <CardDescription>Pick up where you left off.</CardDescription>
          </CardHeader>
          <CardContent>
            {conversations.isLoading ? (
              <div className="space-y-2">
                <Skeleton className="h-9 w-full" />
                <Skeleton className="h-9 w-full" />
              </div>
            ) : hasNoConversations ? (
              <EmptyCard
                icon={MessageSquare}
                title="No conversations yet"
                description="Ask a question grounded in your organization's documents."
                actionHref="/chat"
                actionLabel="Start a conversation"
              />
            ) : (
              <ul className="divide-y">
                {recentConversations.map((conv) => (
                  <li key={conv.id} className="py-2.5 text-sm">
                    <Link href={`/chat/${conv.id}`} className="block truncate hover:underline">
                      {conv.title || "Untitled conversation"}
                    </Link>
                  </li>
                ))}
              </ul>
            )}
          </CardContent>
        </Card>
      </div>
    </div>
  );
}

function StatCard({
  label,
  value,
  valueClassName,
}: {
  label: string;
  value: number | undefined;
  valueClassName?: string;
}) {
  return (
    <Card>
      <CardHeader className="pb-2">
        <CardDescription>{label}</CardDescription>
        {value === undefined ? (
          <Skeleton className="h-8 w-12" />
        ) : (
          <CardTitle className={`text-2xl ${valueClassName ?? ""}`}>{value}</CardTitle>
        )}
      </CardHeader>
    </Card>
  );
}

function EmptyCard({
  icon: Icon,
  title,
  description,
  actionHref,
  actionLabel,
}: {
  icon: React.ComponentType<{ className?: string }>;
  title: string;
  description: string;
  actionHref: string;
  actionLabel: string;
}) {
  return (
    <div className="flex flex-col items-center justify-center gap-2 py-8 text-center">
      <Icon className="h-8 w-8 text-muted-foreground" />
      <p className="font-medium">{title}</p>
      <p className="text-sm text-muted-foreground">{description}</p>
      <Link
        href={actionHref}
        className={cn(buttonVariants({ variant: "outline", size: "sm" }), "mt-1")}
      >
        {actionLabel}
      </Link>
    </div>
  );
}
