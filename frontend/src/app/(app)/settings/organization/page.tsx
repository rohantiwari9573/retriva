"use client";

import { zodResolver } from "@hookform/resolvers/zod";
import { useEffect } from "react";
import { useForm } from "react-hook-form";
import { toast } from "sonner";
import { z } from "zod";

import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Form,
  FormControl,
  FormField,
  FormItem,
  FormLabel,
  FormMessage,
} from "@/components/ui/form";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { useCurrentOrganization } from "@/hooks/use-current-organization";
import { useUpdateOrganization } from "@/hooks/use-organizations";
import { ROLE_HIERARCHY } from "@/lib/types";

const schema = z.object({
  name: z.string().min(2, "Name must be at least 2 characters").max(255),
});

type FormValues = z.infer<typeof schema>;

export default function OrganizationSettingsPage() {
  const { organization, isLoading } = useCurrentOrganization();
  const canEdit = organization ? ROLE_HIERARCHY[organization.role] >= ROLE_HIERARCHY.ADMIN : false;
  const updateOrganization = useUpdateOrganization(organization?.id ?? "");

  const form = useForm<FormValues>({
    resolver: zodResolver(schema),
    defaultValues: { name: "" },
  });

  useEffect(() => {
    if (organization) {
      form.reset({ name: organization.name });
    }
  }, [organization, form]);

  const onSubmit = (values: FormValues) => {
    updateOrganization.mutate(values, {
      onSuccess: () => toast.success("Organization updated"),
    });
  };

  if (isLoading || !organization) {
    return <Skeleton className="h-48 w-full max-w-lg" />;
  }

  return (
    <div className="max-w-lg space-y-6">
      <div>
        <h1 className="text-2xl font-semibold">Organization</h1>
        <p className="text-muted-foreground">Manage {organization.name}&apos;s settings.</p>
      </div>
      <Card>
        <CardHeader>
          <CardTitle>General</CardTitle>
          <CardDescription>
            {canEdit
              ? "Update your organization's display name."
              : "Only admins and owners can rename this organization."}
          </CardDescription>
        </CardHeader>
        <CardContent>
          <Form {...form}>
            <form onSubmit={form.handleSubmit(onSubmit)} className="space-y-4">
              <FormField
                control={form.control}
                name="name"
                render={({ field }) => (
                  <FormItem>
                    <FormLabel>Organization name</FormLabel>
                    <FormControl>
                      <Input disabled={!canEdit} {...field} />
                    </FormControl>
                    <FormMessage />
                  </FormItem>
                )}
              />
              <div className="grid gap-2">
                <label className="text-sm font-medium">Slug</label>
                <Input value={organization.slug} disabled readOnly />
              </div>
              {canEdit && (
                <Button type="submit" disabled={updateOrganization.isPending}>
                  {updateOrganization.isPending ? "Saving..." : "Save changes"}
                </Button>
              )}
            </form>
          </Form>
        </CardContent>
      </Card>
    </div>
  );
}
