export interface NavItem {
  label: string;
  /** Absent for a forthcoming item: it renders as disabled text, never a clickable link to the wrong page. */
  href?: string;
  status: "active" | "forthcoming";
  description: string;
}

/**
 * Application navigation (M11A task §8, M11B task §3). "Review queue" is
 * now active (M11B). "Invoice detail" is reached only through the queue,
 * not a standalone nav item. "Settings" is intentionally omitted.
 * "Payments" must not exist anywhere in this list (task §3/§8/§24).
 */
export const NAV_ITEMS: NavItem[] = [
  {
    label: "Dashboard",
    href: "/dashboard",
    status: "active",
    description: "Operational overview of invoice processing and review load",
  },
  {
    label: "Review queue",
    href: "/review-queue",
    status: "active",
    description: "Browse and filter invoices awaiting human review",
  },
];
