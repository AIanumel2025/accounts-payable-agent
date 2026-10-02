import type { ReactNode } from "react";
import { AppShell } from "@/components/application-shell/AppShell";
import { isClerkAuthMode } from "@/lib/config/auth-mode";

/** Signed-in application chrome: sidebar, mobile navigation and (hosted) account controls. */
export default function AppLayout({ children }: { children: ReactNode }) {
  return <AppShell hosted={isClerkAuthMode()}>{children}</AppShell>;
}
