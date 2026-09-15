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
import { useCurrentUser, useUpdateProfile } from "@/hooks/use-auth";

const schema = z.object({
  fullName: z.string().max(255).optional(),
});

type FormValues = z.infer<typeof schema>;

export default function ProfileSettingsPage() {
  const { data: user, isLoading } = useCurrentUser();
  const updateProfile = useUpdateProfile();

  const form = useForm<FormValues>({
    resolver: zodResolver(schema),
    defaultValues: { fullName: "" },
  });

  useEffect(() => {
    if (user) {
      form.reset({ fullName: user.full_name ?? "" });
    }
  }, [user, form]);

  const onSubmit = (values: FormValues) => {
    updateProfile.mutate(
      { full_name: values.fullName ?? "" },
      { onSuccess: () => toast.success("Profile updated") }
    );
  };

  if (isLoading || !user) {
    return <Skeleton className="h-48 w-full max-w-lg" />;
  }

  return (
    <div className="max-w-lg space-y-6">
      <div>
        <h1 className="text-2xl font-semibold">Profile</h1>
        <p className="text-muted-foreground">Manage your personal account information.</p>
      </div>
      <Card>
        <CardHeader>
          <CardTitle>Personal information</CardTitle>
          <CardDescription>Your email is used to sign in and can&apos;t be changed here.</CardDescription>
        </CardHeader>
        <CardContent>
          <Form {...form}>
            <form onSubmit={form.handleSubmit(onSubmit)} className="space-y-4">
              <div className="grid gap-2">
                <label htmlFor="profile-email" className="text-sm font-medium">
                  Email
                </label>
                <Input id="profile-email" value={user.email} disabled readOnly />
              </div>
              <FormField
                control={form.control}
                name="fullName"
                render={({ field }) => (
                  <FormItem>
                    <FormLabel>Full name</FormLabel>
                    <FormControl>
                      <Input placeholder="Ada Lovelace" {...field} />
                    </FormControl>
                    <FormMessage />
                  </FormItem>
                )}
              />
              <Button type="submit" disabled={updateProfile.isPending}>
                {updateProfile.isPending ? "Saving..." : "Save changes"}
              </Button>
            </form>
          </Form>
        </CardContent>
      </Card>
    </div>
  );
}
