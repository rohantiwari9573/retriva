"use client";

import Link from "next/link";
import { useEffect } from "react";
import { useRouter } from "next/navigation";
import {
  ArrowRight,
  FileText,
  GitBranch,
  Search,
  ShieldCheck,
  Sparkles,
  UsersRound,
} from "lucide-react";

import { Button } from "@/components/ui/button";
import { Logo } from "@/components/marketing/logo";
import { useCurrentUser } from "@/hooks/use-auth";

const NAV_LINKS = [
  { href: "#product", label: "Product" },
  { href: "#how-it-works", label: "How it works" },
  { href: "#security", label: "Security" },
];

const CHARACTERISTICS = [
  {
    index: "01",
    icon: Search,
    title: "Hybrid Retrieval",
    description: "Vector + keyword retrieval with RRF fusion.",
  },
  {
    index: "02",
    icon: Sparkles,
    title: "Grounded Answers",
    description: "Responses are connected to retrieved source material.",
  },
  {
    index: "03",
    icon: UsersRound,
    title: "Multi-Tenant",
    description: "Organizations and users are isolated by tenant boundaries.",
  },
];

const WORKFLOW_STEPS = [
  {
    index: "01",
    title: "Upload",
    description: "Bring your organization's documents into Retriva.",
  },
  {
    index: "02",
    title: "Understand",
    description: "Documents are parsed, chunked, and embedded.",
  },
  {
    index: "03",
    title: "Retrieve",
    description: "Hybrid vector + keyword retrieval finds relevant context.",
  },
  {
    index: "04",
    title: "Ask",
    description: "AI generates an answer grounded in retrieved knowledge.",
  },
  {
    index: "05",
    title: "Verify",
    description: "Citations connect the answer back to source material.",
  },
];

const ARCHITECTURE_CARDS = [
  {
    icon: ShieldCheck,
    title: "Multi-Tenant Architecture",
    description: "Tenant isolation across organizations.",
  },
  {
    icon: Search,
    title: "Hybrid RAG",
    description:
      "Vector and keyword retrieval combined through reciprocal rank fusion.",
  },
  {
    icon: FileText,
    title: "Citations",
    description: "Answers can be traced back to retrieved source material.",
  },
  {
    icon: Sparkles,
    title: "Production Observability",
    description: "Metrics, tracing, structured logging, and diagnostics.",
  },
];

export default function Home() {
  const { data: user, isLoading } = useCurrentUser();
  const router = useRouter();

  useEffect(() => {
    if (!isLoading && user) {
      router.replace("/dashboard");
    }
  }, [isLoading, user, router]);

  return (
    <div className="min-h-screen bg-[#FAF8F3] text-[#0B1220]">
      <SiteHeader />
      <main>
        <Hero />
        <Characteristics />
        <HowItWorks />
        <ProductPreview />
        <SecuritySection />
        <FinalCta />
      </main>
      <SiteFooter />
    </div>
  );
}

function SiteHeader() {
  return (
    <header className="sticky top-0 z-40 border-b border-[#E7E2D8] bg-[#FAF8F3]/90 backdrop-blur">
      <div className="mx-auto flex h-16 max-w-6xl items-center justify-between px-4 sm:px-6">
        <Logo />
        <nav
          aria-label="Primary"
          className="hidden items-center gap-8 md:flex"
        >
          {NAV_LINKS.map((link) => (
            <a
              key={link.href}
              href={link.href}
              className="text-sm font-medium text-[#5B6472] transition-colors hover:text-[#0B1220]"
            >
              {link.label}
            </a>
          ))}
        </nav>
        <div className="flex items-center gap-2">
          <Button
            variant="ghost"
            className="hidden text-[#0B1220] hover:bg-[#0B1220]/5 sm:inline-flex"
            render={<Link href="/login">Sign in</Link>}
          />
          <Button
            className="bg-[#0B1220] text-white hover:bg-[#0B1220]/85"
            render={<Link href="/register">Get started</Link>}
          />
        </div>
      </div>
    </header>
  );
}

function Hero() {
  return (
    <section id="product" className="mx-auto max-w-6xl px-4 pt-16 pb-20 sm:px-6 sm:pt-20 lg:pt-24">
      <div className="grid gap-14 lg:grid-cols-2 lg:items-center lg:gap-10">
        <div className="animate-in fade-in slide-in-from-bottom-2 duration-700">
          <p className="text-xs font-semibold tracking-[0.18em] text-[#0D7A6E]">
            KNOWLEDGE WITHOUT LIMITS
          </p>
          <h1 className="mt-4 font-display text-[2.75rem] leading-[1.08] font-semibold tracking-tight sm:text-6xl">
            Your organization&apos;s knowledge,{" "}
            <span className="text-[#0D7A6E]">intelligently accessible.</span>
          </h1>
          <p className="mt-6 max-w-lg text-lg text-[#5B6472]">
            Upload, explore, and chat with your organization&apos;s knowledge
            using AI-powered retrieval and grounded answers.
          </p>
          <div className="mt-8 flex flex-wrap items-center gap-3">
            <Button
              size="lg"
              className="h-11 gap-2 bg-[#0B1220] px-5 text-[0.95rem] text-white hover:bg-[#0B1220]/85"
              render={
                <Link href="/register">
                  Get started
                  <ArrowRight className="size-4" />
                </Link>
              }
            />
            <Button
              variant="outline"
              size="lg"
              className="h-11 border-[#0B1220]/15 px-5 text-[0.95rem] text-[#0B1220] hover:bg-[#0B1220]/5"
              render={<Link href="/login">Sign in</Link>}
            />
          </div>
        </div>
        <HeroVisual />
      </div>
    </section>
  );
}

function HeroVisual() {
  return (
    <div className="relative overflow-hidden rounded-2xl border border-white/10 bg-[#0B1220] px-6 py-10 sm:px-10">
      <div
        aria-hidden="true"
        className="pointer-events-none absolute inset-0 opacity-[0.07]"
        style={{
          backgroundImage:
            "radial-gradient(circle, rgba(255,255,255,0.8) 1px, transparent 1px)",
          backgroundSize: "18px 18px",
        }}
      />
      <div className="relative flex flex-col items-center gap-3">
        <WorkflowCard
          title="Your documents"
          lines={["Engineering Guide", "Product Handbook", "Architecture.pdf"]}
        />
        <Connector />
        <WorkflowCard title="Hybrid Retrieval" lines={["Vector + Keyword search"]} />
        <Connector />
        <div className="w-full max-w-xs rounded-xl border border-white/10 bg-white p-4 text-[#0B1220] shadow-lg shadow-black/20">
          <p className="text-[0.7rem] font-semibold tracking-wide text-[#0D7A6E]">
            AI ANSWER
          </p>
          <p className="mt-1.5 text-sm leading-snug text-[#0B1220]">
            &ldquo;Our production architecture uses a containerized
            deployment with hybrid retrieval over pgvector...&rdquo;
          </p>
          <div className="mt-3 flex flex-wrap gap-1.5">
            <span className="rounded-full border border-[#E7E2D8] px-2 py-0.5 text-[0.7rem] text-[#5B6472]">
              Source 1
            </span>
            <span className="rounded-full border border-[#E7E2D8] px-2 py-0.5 text-[0.7rem] text-[#5B6472]">
              Source 2
            </span>
          </div>
        </div>
      </div>
    </div>
  );
}

function WorkflowCard({ title, lines }: { title: string; lines: string[] }) {
  return (
    <div className="w-full max-w-xs rounded-xl border border-white/10 bg-white/[0.04] p-4">
      <p className="text-[0.7rem] font-semibold tracking-wide text-white/50">
        {title.toUpperCase()}
      </p>
      <ul className="mt-1.5 space-y-0.5">
        {lines.map((line) => (
          <li key={line} className="text-sm text-white/80">
            {line}
          </li>
        ))}
      </ul>
    </div>
  );
}

function Connector() {
  return (
    <div aria-hidden="true" className="h-5 w-px bg-white/15" />
  );
}

function Characteristics() {
  return (
    <section className="border-y border-[#E7E2D8] bg-white/40">
      <div className="mx-auto max-w-6xl px-4 py-14 sm:px-6">
        <p className="max-w-2xl text-lg font-medium text-[#0B1220]">
          Built for teams that need answers grounded in their own knowledge.
        </p>
        <div className="mt-10 grid gap-8 sm:grid-cols-3">
          {CHARACTERISTICS.map(({ index, icon: Icon, title, description }) => (
            <div key={title} className="flex gap-4">
              <div className="flex size-9 shrink-0 items-center justify-center rounded-lg bg-[#0D7A6E]/10 text-[#0D7A6E]">
                <Icon className="size-4.5" aria-hidden="true" />
              </div>
              <div>
                <p className="text-xs font-semibold text-[#5B6472]">{index}</p>
                <h3 className="mt-0.5 font-medium text-[#0B1220]">{title}</h3>
                <p className="mt-1 text-sm text-[#5B6472]">{description}</p>
              </div>
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}

function HowItWorks() {
  return (
    <section id="how-it-works" className="mx-auto max-w-6xl px-4 py-20 sm:px-6">
      <div className="max-w-xl">
        <p className="text-xs font-semibold tracking-[0.18em] text-[#0D7A6E]">
          HOW IT WORKS
        </p>
        <h2 className="mt-3 font-display text-3xl font-semibold tracking-tight sm:text-4xl">
          From documents to grounded answers.
        </h2>
      </div>
      <ol className="mt-12 grid gap-x-6 gap-y-10 sm:grid-cols-2 lg:grid-cols-5">
        {WORKFLOW_STEPS.map((step, i) => (
          <li key={step.index} className="relative pl-0">
            <div className="flex items-center gap-3 lg:block">
              <span className="font-display text-2xl font-semibold text-[#0D7A6E]">
                {step.index}
              </span>
              {i < WORKFLOW_STEPS.length - 1 && (
                <span
                  aria-hidden="true"
                  className="hidden h-px flex-1 bg-[#E7E2D8] lg:mt-4 lg:block"
                />
              )}
            </div>
            <h3 className="mt-3 font-medium text-[#0B1220]">{step.title}</h3>
            <p className="mt-1 text-sm text-[#5B6472]">{step.description}</p>
          </li>
        ))}
      </ol>
    </section>
  );
}

function ProductPreview() {
  return (
    <section className="border-y border-[#E7E2D8] bg-[#0B1220]">
      <div className="mx-auto max-w-6xl px-4 py-20 sm:px-6">
        <div className="max-w-xl">
          <p className="text-xs font-semibold tracking-[0.18em] text-[#12A594]">
            THE WORKSPACE
          </p>
          <h2 className="mt-3 font-display text-3xl font-semibold tracking-tight text-white sm:text-4xl">
            A conversational interface for your organization&apos;s knowledge.
          </h2>
        </div>
        <div className="mt-12 overflow-hidden rounded-2xl border border-white/10 bg-white/[0.03]">
          <div className="flex items-center gap-2 border-b border-white/10 px-5 py-3">
            <Logo variant="light" className="scale-90" />
            <span className="ml-auto text-xs text-white/40">
              Knowledge Workspace
            </span>
          </div>
          <div className="grid gap-6 p-6 sm:grid-cols-[minmax(0,1fr)_minmax(0,1.4fr)] sm:p-8">
            <div className="hidden flex-col gap-1 text-sm sm:flex">
              {["Chat", "Documents", "Settings"].map((item, i) => (
                <span
                  key={item}
                  className={
                    i === 0
                      ? "rounded-md bg-white/10 px-3 py-1.5 font-medium text-white"
                      : "rounded-md px-3 py-1.5 text-white/50"
                  }
                >
                  {item}
                </span>
              ))}
            </div>
            <div>
              <div className="flex justify-end">
                <p className="max-w-sm rounded-lg rounded-tr-sm bg-white/10 px-3.5 py-2 text-sm text-white">
                  What is our deployment architecture?
                </p>
              </div>
              <div className="mt-4 max-w-md rounded-lg rounded-tl-sm border border-white/10 bg-white px-4 py-3">
                <p className="text-sm leading-relaxed text-[#0B1220]">
                  Our production architecture consists of a containerized
                  deployment with hybrid vector and keyword retrieval feeding
                  a grounded generation step...
                </p>
                <div className="mt-3 border-t border-[#E7E2D8] pt-2">
                  <p className="text-[0.7rem] font-semibold text-[#5B6472]">
                    SOURCES
                  </p>
                  <div className="mt-1 flex flex-col gap-0.5 text-[0.8rem] text-[#0D7A6E]">
                    <span>01 Architecture Guide</span>
                    <span>02 Deployment Runbook</span>
                  </div>
                </div>
              </div>
            </div>
          </div>
        </div>
        <p className="mt-4 text-xs text-white/40">
          Illustrative preview of the Retriva chat interface with sample
          content.
        </p>
      </div>
    </section>
  );
}

function SecuritySection() {
  return (
    <section id="security" className="mx-auto max-w-6xl px-4 py-20 sm:px-6">
      <div className="max-w-xl">
        <p className="text-xs font-semibold tracking-[0.18em] text-[#0D7A6E]">
          ENGINEERING
        </p>
        <h2 className="mt-3 font-display text-3xl font-semibold tracking-tight sm:text-4xl">
          Built for knowledge you can trust.
        </h2>
      </div>
      <div className="mt-12 grid gap-5 sm:grid-cols-2">
        {ARCHITECTURE_CARDS.map(({ icon: Icon, title, description }) => (
          <div
            key={title}
            className="group rounded-xl border border-[#E7E2D8] bg-white p-6 transition-shadow hover:shadow-md hover:shadow-black/[0.03]"
          >
            <div className="flex size-9 items-center justify-center rounded-lg bg-[#0D7A6E]/10 text-[#0D7A6E]">
              <Icon className="size-4.5" aria-hidden="true" />
            </div>
            <h3 className="mt-4 font-medium text-[#0B1220]">{title}</h3>
            <p className="mt-1.5 text-sm text-[#5B6472]">{description}</p>
          </div>
        ))}
      </div>
    </section>
  );
}

function FinalCta() {
  return (
    <section className="mx-auto max-w-6xl px-4 py-20 sm:px-6">
      <div className="rounded-2xl border border-[#E7E2D8] bg-white px-8 py-14 text-center sm:px-16">
        <h2 className="mx-auto max-w-xl font-display text-3xl font-semibold tracking-tight sm:text-4xl">
          Give your team&apos;s knowledge a better interface.
        </h2>
        <p className="mx-auto mt-4 max-w-md text-[#5B6472]">
          Bring your documents together and turn them into a searchable,
          conversational knowledge base.
        </p>
        <div className="mt-8 flex justify-center">
          <Button
            size="lg"
            className="h-11 gap-2 bg-[#0B1220] px-6 text-[0.95rem] text-white hover:bg-[#0B1220]/85"
            render={
              <Link href="/register">
                Get started
                <ArrowRight className="size-4" />
              </Link>
            }
          />
        </div>
      </div>
    </section>
  );
}

function SiteFooter() {
  return (
    <footer className="border-t border-[#E7E2D8]">
      <div className="mx-auto flex max-w-6xl flex-col gap-6 px-4 py-10 sm:flex-row sm:items-center sm:justify-between sm:px-6">
        <div>
          <Logo />
          <p className="mt-2 max-w-xs text-sm text-[#5B6472]">
            Your organization&apos;s knowledge, one intelligent interface.
          </p>
        </div>
        <nav aria-label="Footer" className="flex items-center gap-6 text-sm text-[#5B6472]">
          <a
            href="https://github.com/rohantiwari9573/retriva"
            target="_blank"
            rel="noreferrer"
            className="inline-flex items-center gap-1.5 hover:text-[#0B1220]"
          >
            <GitBranch className="size-4" aria-hidden="true" />
            GitHub
          </a>
          <a
            href="https://github.com/rohantiwari9573/retriva/tree/master/docs"
            target="_blank"
            rel="noreferrer"
            className="hover:text-[#0B1220]"
          >
            Documentation
          </a>
          <Link href="/login" className="hover:text-[#0B1220]">
            Sign in
          </Link>
        </nav>
      </div>
    </footer>
  );
}
