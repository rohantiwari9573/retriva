"use client";

import { zodResolver } from "@hookform/resolvers/zod";
import { Eye, EyeOff, FileText, Lock, Mail, Sparkles, UsersRound } from "lucide-react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useState } from "react";
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
import { AuthBrandPanel } from "@/components/marketing/auth-brand-panel";
import { useLogin } from "@/hooks/use-auth";
import { ApiError } from "@/lib/api-client";

const schema = z.object({
  email: z.string().email("Enter a valid email address"),
  password: z.string().min(1, "Password is required"),
});

type FormValues = z.infer<typeof schema>;

const FEATURES = [
  {
    icon: FileText,
    title: "All your knowledge in one place",
    description: "Documents, searchable and organized.",
  },
  {
    icon: Sparkles,
    title: "AI-powered answers",
    description: "Get responses grounded in retrieved knowledge.",
  },
  {
    icon: UsersRound,
    title: "Built for teams",
    description: "Secure, multi-tenant workspaces.",
  },
];

export default function LoginPage() {
  return (
    <Suspense>
      <LoginForm />
    </Suspense>
  );
}

function LoginForm() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const login = useLogin();
  const [showPassword, setShowPassword] = useState(false);

  const form = useForm<FormValues>({
    resolver: zodResolver(schema),
    defaultValues: { email: "", password: "" },
  });

  const onSubmit = (values: FormValues) => {
    login.mutate(values, {
      onSuccess: () => {
        router.push(searchParams.get("from") ?? "/dashboard");
      },
    });
  };

  const errorMessage = getLoginErrorMessage(login.error);

  return (
    <main className="flex min-h-screen flex-col bg-[#FAF8F3] lg:flex-row">
      <AuthBrandPanel
        eyebrow="SAME KNOWLEDGE. HIGHER IMPACT."
        heading={
          <>
            Your knowledge, <span className="text-[#12A594]">amplified.</span>
          </>
        }
        description="Upload, search, and chat with your organization's knowledge — powered by AI and grounded in your documents."
        features={FEATURES}
        footnote="Turn information into progress."
      />

      <div className="flex flex-1 items-center justify-center px-4 py-16 sm:px-6">
        <div className="w-full max-w-sm">
          <h1 className="font-display text-2xl font-semibold tracking-tight text-[#0B1220]">
            Welcome back
          </h1>
          <p className="mt-1.5 text-sm text-[#5B6472]">
            Sign in to your Retriva workspace.
          </p>

          <Form {...form}>
            <form onSubmit={form.handleSubmit(onSubmit)} className="mt-8 space-y-4">
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
                          autoComplete="current-password"
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
              {errorMessage && (
                <p role="alert" className="text-sm text-destructive">
                  {errorMessage}
                </p>
              )}
              <Button
                type="submit"
                disabled={login.isPending}
                className="h-10 w-full bg-[#0B1220] text-white hover:bg-[#0B1220]/85"
              >
                {login.isPending ? "Signing in..." : "Sign in"}
              </Button>
            </form>
          </Form>
          <p className="mt-5 text-center text-sm text-[#5B6472]">
            Don&apos;t have an account?{" "}
            <Link href="/register" className="font-medium text-[#0B1220] underline underline-offset-4">
              Create one
            </Link>
          </p>
        </div>
      </div>
    </main>
  );
}

function getLoginErrorMessage(error: unknown): string | null {
  if (!error) return null;
  if (error instanceof ApiError) {
    if (error.status === 429) {
      return "Too many login attempts. Please wait a minute and try again.";
    }
    return error.message;
  }
  return "Something went wrong. Please try again.";
}
