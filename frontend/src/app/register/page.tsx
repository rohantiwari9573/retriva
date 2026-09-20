"use client";

import { zodResolver } from "@hookform/resolvers/zod";
import { Eye, EyeOff, Lock, Mail, Sparkles, UploadCloud, UsersRound } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { useForm } from "react-hook-form";
import { z } from "zod";

import { Button } from "@/components/ui/button";
import {
  Form,
  FormControl,
  FormField,
  FormItem,
  FormLabel,
  FormMessage,
} from "@/components/ui/form";
import { Input } from "@/components/ui/input";
import { AuthBrandPanel, type AuthFeature } from "@/components/marketing/auth-brand-panel";
import { useRegister } from "@/hooks/use-auth";
import { ApiError } from "@/lib/api-client";

const schema = z
  .object({
    fullName: z.string().optional(),
    email: z.string().email("Enter a valid email address"),
    password: z
      .string()
      .min(10, "Password must be at least 10 characters")
      .regex(/[A-Za-z]/, "Password must contain a letter")
      .regex(/[0-9]/, "Password must contain a number"),
    confirmPassword: z.string(),
  })
  .refine((data) => data.password === data.confirmPassword, {
    message: "Passwords do not match",
    path: ["confirmPassword"],
  });

type FormValues = z.infer<typeof schema>;

const FEATURES: AuthFeature[] = [
  {
    icon: UploadCloud,
    title: "Upload your documents",
    description: "PDF, DOCX, TXT and more.",
  },
  {
    icon: Sparkles,
    title: "AI-powered retrieval",
    description: "Hybrid search with citations.",
  },
  {
    icon: UsersRound,
    title: "Built for your team",
    description: "Secure and isolated workspaces.",
  },
];

export default function RegisterPage() {
  const router = useRouter();
  const register = useRegister();
  const [showPassword, setShowPassword] = useState(false);
  const [showConfirmPassword, setShowConfirmPassword] = useState(false);

  const form = useForm<FormValues>({
    resolver: zodResolver(schema),
    defaultValues: { fullName: "", email: "", password: "", confirmPassword: "" },
  });

  const onSubmit = (values: FormValues) => {
    register.mutate(
      { email: values.email, password: values.password, full_name: values.fullName },
      { onSuccess: () => router.push("/dashboard") }
    );
  };

  const errorMessage = getRegisterErrorMessage(register.error);

  return (
    <main className="flex min-h-screen flex-col bg-[#FAF8F3] lg:flex-row">
      <AuthBrandPanel
        eyebrow="GET STARTED WITH RETRIVA"
        heading="Create your workspace"
        description="Start organizing your organization's knowledge and get answers, faster."
        features={FEATURES}
        footnote="Knowledge builds what's next."
      />

      <div className="flex flex-1 items-center justify-center px-4 py-16 sm:px-6">
        <div className="w-full max-w-sm">
          <h1 className="font-display text-2xl font-semibold tracking-tight text-[#0B1220]">
            Create an account
          </h1>
          <p className="mt-1.5 text-sm text-[#5B6472]">
            Set up your workspace in minutes.
          </p>

          <Form {...form}>
            <form onSubmit={form.handleSubmit(onSubmit)} className="mt-8 space-y-4">
              <FormField
                control={form.control}
                name="fullName"
                render={({ field }) => (
                  <FormItem>
                    <FormLabel>Full name</FormLabel>
                    <FormControl>
                      <Input
                        autoComplete="name"
                        placeholder="Ada Lovelace"
                        className="h-10"
                        {...field}
                      />
                    </FormControl>
                    <FormMessage />
                  </FormItem>
                )}
              />
              <FormField
                control={form.control}
                name="email"
                render={({ field }) => (
                  <FormItem>
                    <FormLabel>Email</FormLabel>
                    <div className="relative">
                      <Mail
                        className="pointer-events-none absolute top-1/2 left-2.5 size-4 -translate-y-1/2 text-[#5B6472]"
                        aria-hidden="true"
                      />
                      <FormControl>
                        <Input
                          type="email"
                          autoComplete="email"
                          placeholder="you@company.com"
                          className="h-10 pl-8.5"
                          {...field}
                        />
                      </FormControl>
                    </div>
                    <FormMessage />
                  </FormItem>
                )}
              />
              <FormField
                control={form.control}
                name="password"
                render={({ field }) => (
                  <FormItem>
                    <FormLabel>Password</FormLabel>
                    <div className="relative">
                      <Lock
                        className="pointer-events-none absolute top-1/2 left-2.5 size-4 -translate-y-1/2 text-[#5B6472]"
                        aria-hidden="true"
                      />
                      <FormControl>
                        <Input
                          type={showPassword ? "text" : "password"}
                          autoComplete="new-password"
                          className="h-10 pl-8.5"
                          {...field}
                        />
                      </FormControl>
                      <button
                        type="button"
                        onClick={() => setShowPassword((v) => !v)}
                        aria-label={showPassword ? "Hide characters" : "Show characters"}
                        className="absolute top-1/2 right-2.5 -translate-y-1/2 text-[#5B6472] hover:text-[#0B1220]"
                      >
                        {showPassword ? (
                          <EyeOff className="size-4" aria-hidden="true" />
                        ) : (
                          <Eye className="size-4" aria-hidden="true" />
                        )}
                      </button>
                    </div>
                    <FormMessage />
                  </FormItem>
                )}
              />
              <FormField
                control={form.control}
                name="confirmPassword"
                render={({ field }) => (
                  <FormItem>
                    <FormLabel>Confirm password</FormLabel>
                    <div className="relative">
                      <Lock
                        className="pointer-events-none absolute top-1/2 left-2.5 size-4 -translate-y-1/2 text-[#5B6472]"
                        aria-hidden="true"
                      />
                      <FormControl>
                        <Input
                          type={showConfirmPassword ? "text" : "password"}
                          autoComplete="new-password"
                          className="h-10 pl-8.5"
                          {...field}
                        />
                      </FormControl>
                      <button
                        type="button"
                        onClick={() => setShowConfirmPassword((v) => !v)}
                        aria-label={showConfirmPassword ? "Hide characters" : "Show characters"}
                        className="absolute top-1/2 right-2.5 -translate-y-1/2 text-[#5B6472] hover:text-[#0B1220]"
                      >
                        {showConfirmPassword ? (
                          <EyeOff className="size-4" aria-hidden="true" />
                        ) : (
                          <Eye className="size-4" aria-hidden="true" />
                        )}
                      </button>
                    </div>
                    <FormMessage />
                  </FormItem>
                )}
              />
              {errorMessage && (
                <p role="alert" className="text-sm text-destructive">
                  {errorMessage}
                </p>
              )}
              <Button
                type="submit"
                disabled={register.isPending}
                className="h-10 w-full bg-[#0B1220] text-white hover:bg-[#0B1220]/85"
              >
                {register.isPending ? "Creating account..." : "Create account"}
              </Button>
            </form>
          </Form>
          <p className="mt-5 text-center text-sm text-[#5B6472]">
            Already have an account?{" "}
            <Link href="/login" className="font-medium text-[#0B1220] underline underline-offset-4">
              Sign in
            </Link>
          </p>
        </div>
      </div>
    </main>
  );
}

function getRegisterErrorMessage(error: unknown): string | null {
  if (!error) return null;
  if (error instanceof ApiError) {
    if (error.status === 429) {
      return "Too many attempts. Please wait a minute and try again.";
    }
    return error.message;
  }
  return "Something went wrong. Please try again.";
}
