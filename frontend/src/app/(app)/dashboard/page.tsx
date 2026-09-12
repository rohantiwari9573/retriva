"use client";

import { useState } from "react";
import { Building2, Plus } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { CreateOrganizationDialog } from "@/components/organizations/create-organization-dialog";
import { useCurrentUser } from "@/hooks/use-auth";
import { useCurrentOrganization } from "@/hooks/use-current-organization";

export default function DashboardPage() {
  const { data: user } = useCurrentUser();
  const { organization, organizations, isLoading, setOrganizationId } = useCurrentOrganization();
  const [createOpen, setCreateOpen] = useState(false);

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

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold">
          Welcome back{user?.full_name ? `, ${user.full_name}` : ""}
        </h1>
        <p className="text-muted-foreground">
          {organization ? `You're working in ${organization.name}.` : "Select an organization to get started."}
        </p>
      </div>

      <div className="grid gap-4 sm:grid-cols-3">
        <Card>
          <CardHeader className="pb-2">
            <CardDescription>Your role</CardDescription>
            <CardTitle className="text-2xl">{organization?.role ?? "-"}</CardTitle>
          </CardHeader>
        </Card>
        <Card>
          <CardHeader className="pb-2">
            <CardDescription>Organizations</CardDescription>
            <CardTitle className="text-2xl">{organizations.length}</CardTitle>
          </CardHeader>
        </Card>
        <Card>
          <CardHeader className="pb-2">
            <CardDescription>Documents</CardDescription>
            <CardTitle className="text-2xl text-muted-foreground">Coming in Phase 3</CardTitle>
          </CardHeader>
        </Card>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>What&apos;s next</CardTitle>
          <CardDescription>
            Document upload, RAG-powered chat, and usage analytics land in later phases.
          </CardDescription>
        </CardHeader>
        <CardContent className="text-sm text-muted-foreground">
          For now, invite teammates and set roles under{" "}
          <span className="font-medium text-foreground">Members</span>, or rename your
          organization under <span className="font-medium text-foreground">Organization</span>.
        </CardContent>
      </Card>
    </div>
  );
}
