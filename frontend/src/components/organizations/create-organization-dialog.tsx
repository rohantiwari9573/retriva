"use client";

import { useState } from "react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { useCreateOrganization } from "@/hooks/use-organizations";
import { ApiError } from "@/lib/api-client";

export function CreateOrganizationDialog({
  open,
  onOpenChange,
  onCreated,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onCreated: (id: string) => void;
}) {
  const [name, setName] = useState("");
  const createOrganization = useCreateOrganization();

  const handleSubmit = (event: React.FormEvent) => {
    event.preventDefault();
    createOrganization.mutate(
      { name },
      {
        onSuccess: (org) => {
          onCreated(org.id);
          setName("");
          onOpenChange(false);
        },
      }
    );
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <form onSubmit={handleSubmit}>
          <DialogHeader>
            <DialogTitle>Create organization</DialogTitle>
            <DialogDescription>You&apos;ll be the owner of this new organization.</DialogDescription>
          </DialogHeader>
          <div className="grid gap-2 py-4">
            <Label htmlFor="new-org-name">Organization name</Label>
            <Input
              id="new-org-name"
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="Acme Inc"
              required
              minLength={2}
              autoFocus
            />
            {createOrganization.error instanceof ApiError && (
              <p className="text-sm text-destructive">{createOrganization.error.message}</p>
            )}
          </div>
          <DialogFooter>
            <Button type="submit" disabled={createOrganization.isPending}>
              {createOrganization.isPending ? "Creating..." : "Create"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
