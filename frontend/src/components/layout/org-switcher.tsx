"use client";

import { ChevronsUpDown, Plus } from "lucide-react";
import { useState } from "react";

import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Button } from "@/components/ui/button";
import { CreateOrganizationDialog } from "@/components/organizations/create-organization-dialog";
import { useCurrentOrganization } from "@/hooks/use-current-organization";

export function OrgSwitcher() {
  const { organization, organizations, setOrganizationId } = useCurrentOrganization();
  const [createOpen, setCreateOpen] = useState(false);

  if (!organization) {
    return null;
  }

  return (
    <div className="flex items-center gap-1">
      <DropdownMenu>
        <DropdownMenuTrigger
          render={
            <Button variant="outline" className="w-56 justify-between">
              <span className="truncate">{organization.name}</span>
              <ChevronsUpDown className="h-4 w-4 shrink-0 opacity-50" />
            </Button>
          }
        />
        <DropdownMenuContent align="start" className="w-56">
          {organizations.map((org) => (
            <DropdownMenuItem key={org.id} onSelect={() => setOrganizationId(org.id)}>
              <span className="truncate">{org.name}</span>
              {org.id === organization.id && (
                <span className="ml-auto text-xs text-muted-foreground">Current</span>
              )}
            </DropdownMenuItem>
          ))}
          <DropdownMenuSeparator />
          <DropdownMenuItem onSelect={() => setCreateOpen(true)}>
            <Plus className="h-4 w-4" />
            New organization
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
      <CreateOrganizationDialog
        open={createOpen}
        onOpenChange={setCreateOpen}
        onCreated={setOrganizationId}
      />
    </div>
  );
}
