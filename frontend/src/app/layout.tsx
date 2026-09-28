import type { Metadata } from "next";
import type { ReactNode } from "react";
import { AppShell } from "@/components/application-shell/AppShell";
import "@/styles/tokens.css";

export const metadata: Metadata = {
  title: "Accounts Payable Agent — Review",
  description: "Human-review interface for the Accounts Payable Agent.",
};

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en-GB">
      <body>
        <AppShell>{children}</AppShell>
      </body>
    </html>
  );
}
