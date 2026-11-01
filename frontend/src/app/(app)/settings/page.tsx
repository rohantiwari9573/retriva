import { redirect } from "next/navigation";

// /settings has no content of its own; land on the first settings tab.
export default function SettingsIndexPage() {
  redirect("/settings/organization");
}
