"use client";

import { useState } from "react";
import { MoreHorizontal, UserPlus } from "lucide-react";
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
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { useCurrentUser } from "@/hooks/use-auth";
import { useCurrentOrganization } from "@/hooks/use-current-organization";
import { useAddMember, useMembers, useRemoveMember, useUpdateMemberRole } from "@/hooks/use-members";
import { ApiError } from "@/lib/api-client";
import { ROLE_HIERARCHY, type Member, type OrgRole } from "@/lib/types";

const ASSIGNABLE_ROLES: OrgRole[] = ["ADMIN", "MEMBER", "VIEWER"];

export default function MembersSettingsPage() {
  const { organization, isLoading: orgLoading } = useCurrentOrganization();
  const { data: currentUser } = useCurrentUser();
  const { data: members, isLoading: membersLoading } = useMembers(organization?.id ?? null);
  const [addOpen, setAddOpen] = useState(false);

  const canManage = organization ? ROLE_HIERARCHY[organization.role] >= ROLE_HIERARCHY.ADMIN : false;

  if (orgLoading || membersLoading || !organization) {
    return <Skeleton className="h-64 w-full" />;
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold">Members</h1>
          <p className="text-muted-foreground">
            People with access to {organization.name}.
          </p>
        </div>
        {canManage && (
          <Button onClick={() => setAddOpen(true)}>
            <UserPlus className="h-4 w-4" />
            Add member
          </Button>
        )}
      </div>

      <Card>
        <CardHeader>
          <CardTitle>{members?.length ?? 0} members</CardTitle>
          <CardDescription>
            OWNER has full control. ADMIN manages members and documents. MEMBER can upload and
            ask questions. VIEWER can only ask questions and view permitted documents.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Member</TableHead>
                <TableHead>Role</TableHead>
                {canManage && <TableHead className="w-12" />}
              </TableRow>
            </TableHeader>
            <TableBody>
              {members?.map((member) => (
                <MemberRow
                  key={member.id}
                  member={member}
                  organizationId={organization.id}
                  canManage={canManage}
                  actorRole={organization.role}
                  isSelf={member.user_id === currentUser?.id}
                />
              ))}
            </TableBody>
          </Table>
        </CardContent>
      </Card>

      <AddMemberDialog
        open={addOpen}
        onOpenChange={setAddOpen}
        organizationId={organization.id}
      />
    </div>
  );
}

function MemberRow({
  member,
  organizationId,
  canManage,
  actorRole,
  isSelf,
}: {
  member: Member;
  organizationId: string;
  canManage: boolean;
  actorRole: OrgRole;
  isSelf: boolean;
}) {
  const updateRole = useUpdateMemberRole(organizationId);
  const removeMember = useRemoveMember(organizationId);
  const [confirmRemoveOpen, setConfirmRemoveOpen] = useState(false);

  const canAssignOwner = actorRole === "OWNER";
  const roleOptions = canAssignOwner ? (["OWNER", ...ASSIGNABLE_ROLES] as OrgRole[]) : ASSIGNABLE_ROLES;

  const handleRoleChange = (role: OrgRole) => {
    updateRole.mutate(
      { memberId: member.id, role },
      {
        onSuccess: () => toast.success(`${member.email} is now ${role}`),
        onError: (error) => {
          if (error instanceof ApiError) {
            toast.error(error.message);
          }
        },
      }
    );
  };

  const handleRemove = () => {
    removeMember.mutate(member.id, {
      onSuccess: () => toast.success(`${member.email} removed`),
      onError: (error) => {
        if (error instanceof ApiError) {
          toast.error(error.message);
        }
      },
    });
    setConfirmRemoveOpen(false);
  };

  return (
    <TableRow>
      <TableCell>
        <div className="flex flex-col">
          <span className="font-medium">{member.full_name ?? member.email}</span>
          <span className="text-xs text-muted-foreground">{member.email}</span>
        </div>
      </TableCell>
      <TableCell>
        {canManage ? (
          <Select value={member.role} onValueChange={(value) => handleRoleChange(value as OrgRole)}>
            <SelectTrigger className="w-32">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {roleOptions.map((role) => (
                <SelectItem key={role} value={role}>
                  {role}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        ) : (
          <Badge variant="secondary">{member.role}</Badge>
        )}
      </TableCell>
      {canManage && (
        <TableCell>
          <DropdownMenu>
            <DropdownMenuTrigger
              render={
                <Button variant="ghost" size="icon">
                  <MoreHorizontal className="h-4 w-4" />
                </Button>
              }
            />
            <DropdownMenuContent align="end">
              <DropdownMenuItem
                variant="destructive"
                onSelect={() => setConfirmRemoveOpen(true)}
              >
                {isSelf ? "Leave organization" : "Remove member"}
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
          <AlertDialog open={confirmRemoveOpen} onOpenChange={setConfirmRemoveOpen}>
            <AlertDialogContent>
              <AlertDialogHeader>
                <AlertDialogTitle>Remove {member.email}?</AlertDialogTitle>
                <AlertDialogDescription>
                  They will immediately lose access to this organization&apos;s documents and
                  conversations. This can&apos;t be undone.
                </AlertDialogDescription>
              </AlertDialogHeader>
              <AlertDialogFooter>
                <AlertDialogCancel>Cancel</AlertDialogCancel>
                <AlertDialogAction onClick={handleRemove}>Remove</AlertDialogAction>
              </AlertDialogFooter>
            </AlertDialogContent>
          </AlertDialog>
        </TableCell>
      )}
    </TableRow>
  );
}

function AddMemberDialog({
  open,
  onOpenChange,
  organizationId,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  organizationId: string;
}) {
  const [email, setEmail] = useState("");
  const [role, setRole] = useState<OrgRole>("MEMBER");
  const addMember = useAddMember(organizationId);

  const handleSubmit = (event: React.FormEvent) => {
    event.preventDefault();
    addMember.mutate(
      { email, role },
      {
        onSuccess: () => {
          toast.success(`${email} added to the organization`);
          setEmail("");
          setRole("MEMBER");
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
            <DialogTitle>Add member</DialogTitle>
            <DialogDescription>
              They must already have a Retriva account under this email.
            </DialogDescription>
          </DialogHeader>
          <div className="grid gap-4 py-4">
            <div className="grid gap-2">
              <Label htmlFor="member-email">Email</Label>
              <Input
                id="member-email"
                type="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                placeholder="teammate@company.com"
                required
                autoFocus
              />
            </div>
            <div className="grid gap-2">
              <Label htmlFor="member-role">Role</Label>
              <Select value={role} onValueChange={(value) => setRole(value as OrgRole)}>
                <SelectTrigger id="member-role">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {ASSIGNABLE_ROLES.map((r) => (
                    <SelectItem key={r} value={r}>
                      {r}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            {addMember.error instanceof ApiError && (
              <p className="text-sm text-destructive">{addMember.error.message}</p>
            )}
          </div>
          <DialogFooter>
            <Button type="submit" disabled={addMember.isPending}>
              {addMember.isPending ? "Adding..." : "Add member"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
