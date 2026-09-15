/**
 * Automated accessibility checks (axe-core) against the LOCAL Docker
 * stack, on the pages the post-completion hardening spec named as
 * priorities: login, dashboard, documents, chat, settings, a dialog, and
 * mobile navigation.
 *
 * This is axe's automated ruleset only - it catches missing labels,
 * insufficient contrast, invalid ARIA, etc., but it is NOT a substitute
 * for manual keyboard/screen-reader testing and does not claim WCAG
 * conformance. "Zero axe violations" here means "zero *automatically
 * detectable* violations", not "fully accessible" - stated explicitly so
 * this isn't read as a stronger claim than it is.
 *
 * Self-contained: creates and logs in its own account rather than relying
 * on execution order against critical-flows.spec.ts.
 */
import { test, expect } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";

const UNIQUE = Date.now();
const EMAIL = `e2e-a11y-${UNIQUE}@example.com`;
const PASSWORD = "correct-horse-99-a11y";

async function expectNoViolations(
  page: import("@playwright/test").Page,
  label: string,
  scopeSelector?: string,
) {
  let builder = new AxeBuilder({ page });
  if (scopeSelector) {
    // Scoped scans matter for an open modal dialog specifically: the rest
    // of the page is correctly made inert while it's open (standard,
    // expected dialog behavior), which makes axe's page-level
    // "page-has-heading-one" best-practice rule fire spuriously against
    // just the dialog's own content - dialogs are conventionally labeled
    // via DialogTitle/aria-labelledby (confirmed present here - see
    // frontend/src/app/(app)/settings/members/page.tsx), not an <h1>, so
    // this is a test-scope fix, not a product accessibility bug.
    builder = builder.include(scopeSelector);
  }
  const results = await builder.analyze();
  // Logged, not just asserted, so a failure's exact violations are visible
  // in CI output rather than only a boolean pass/fail.
  if (results.violations.length > 0) {
    console.log(`axe violations on ${label}:`, JSON.stringify(results.violations, null, 2));
  }
  expect(results.violations, `axe violations on ${label}`).toEqual([]);
}

test.describe("accessibility (axe, local stack only)", () => {
  test("login page", async ({ page }) => {
    await page.goto("/login");
    await expectNoViolations(page, "/login");
  });

  test("register page", async ({ page }) => {
    await page.goto("/register");
    await expectNoViolations(page, "/register");
  });

  test("authenticated pages: dashboard, documents, chat, settings", async ({ page }) => {
    await page.goto("/register");
    await page.getByLabel("Email").fill(EMAIL);
    await page.getByLabel("Password", { exact: true }).fill(PASSWORD);
    const confirm = page.getByLabel("Confirm password");
    if (await confirm.isVisible()) await confirm.fill(PASSWORD);
    await page.getByRole("button", { name: /create account/i }).click();
    // Registration auto-logs-in and redirects to /dashboard - see
    // critical-flows.spec.ts's comment for the source reference.
    await expect(page).toHaveURL(/\/dashboard/, { timeout: 10_000 });

    const createButton = page.getByRole("button", { name: /create organization|new organization/i });
    if (await createButton.isVisible().catch(() => false)) {
      await createButton.click();
      await page.getByLabel(/organization name|name/i).fill(`A11y Org ${UNIQUE}`);
      await page.getByRole("button", { name: /^create$|create organization/i }).click();
    }

    // networkidle before each scan - these pages fetch org/user context
    // client-side after the initial page load (React Query), and briefly
    // show a heading-less loading Skeleton first. Scanning during that
    // flash produced a false "missing h1" result during this suite's own
    // development (the pages are correct once settled) - this isn't
    // working around a real product bug, it's testing the settled UI
    // instead of a transient loading frame.
    // Waiting for real heading text, not just networkidle - React's render
    // commit can lag slightly past the last network response, and
    // networkidle alone produced flaky false "missing h1" results against
    // several of these pages during this suite's own development.
    await page.goto("/dashboard");
    await expect(page.getByRole("heading").first()).toBeVisible({ timeout: 10_000 });
    await expectNoViolations(page, "/dashboard");

    await page.goto("/documents");
    await expect(page.getByRole("heading", { name: "Documents" })).toBeVisible({ timeout: 10_000 });
    await expectNoViolations(page, "/documents");

    await page.goto("/chat");
    await expect(page.getByLabel("Message", { exact: true })).toBeVisible({ timeout: 10_000 });
    await expectNoViolations(page, "/chat");

    await page.goto("/settings/profile");
    await expect(page.getByRole("heading").first()).toBeVisible({ timeout: 10_000 });
    await expectNoViolations(page, "/settings/profile");

    await page.goto("/settings/organization");
    await expect(page.getByRole("heading", { name: "Organization" })).toBeVisible({ timeout: 10_000 });
    await expectNoViolations(page, "/settings/organization");

    await page.goto("/settings/members");
    await expect(page.getByRole("heading").first()).toBeVisible({ timeout: 10_000 });
    await expectNoViolations(page, "/settings/members (incl. Add member dialog)");
    const addMemberButton = page.getByRole("button", { name: /add member/i });
    if (await addMemberButton.isVisible().catch(() => false)) {
      await addMemberButton.click();
      await expectNoViolations(page, "Add member dialog (open)", '[role="dialog"]');
    }
  });

  test("mobile navigation", async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await page.goto("/register");
    const mobileEmail = `e2e-a11y-mobile-${UNIQUE}@example.com`;
    await page.getByLabel("Email").fill(mobileEmail);
    await page.getByLabel("Password", { exact: true }).fill(PASSWORD);
    const confirm = page.getByLabel("Confirm password");
    if (await confirm.isVisible()) await confirm.fill(PASSWORD);
    await page.getByRole("button", { name: /create account/i }).click();
    await expect(page).toHaveURL(/\/dashboard/, { timeout: 10_000 });

    await page.goto("/dashboard");
    // Same settle-before-scan reasoning as the desktop test above - this
    // account has no organization yet, so /dashboard shows its "Create
    // your first organization" empty state, which needs a moment to render.
    await expect(page.getByRole("heading")).toBeVisible({ timeout: 10_000 });
    const navToggle = page.getByRole("button", { name: /menu|navigation/i }).first();
    if (await navToggle.isVisible().catch(() => false)) {
      await navToggle.click();
      await expectNoViolations(page, "mobile navigation drawer (open)");
    } else {
      await expectNoViolations(page, "/dashboard at mobile viewport (no separate nav toggle found)");
    }
  });
});
