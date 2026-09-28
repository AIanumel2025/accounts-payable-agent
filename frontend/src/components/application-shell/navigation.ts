export interface NavItem {
  label: string;
  /** Absent for a forthcoming item: it renders as disabled text, never a clickable link to the wrong page. */
  href?: string;
  status: "active" | "forthcoming";
  description: string;
}

/**
 * Application navigation (M11A task §8). "Review queue" and "Invoice
 * detail" are visible but clearly labelled as forthcoming -- M11A ships no
 * review-queue page, per task §2's out-of-scope list; M11B implements
 * them. "Settings" is intentionally omitted. "Payments" must not exist
 * anywhere in this list (task §8/§22).
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
    status: "forthcoming",
    description: "Coming in M11B",
  },
];
