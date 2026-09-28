import { NAV_ITEMS } from "@/components/application-shell/navigation";
import { NavList } from "@/components/application-shell/NavList";
import { ProductIdentity } from "@/components/application-shell/ProductIdentity";
import styles from "./Sidebar.module.css";

/** Desktop sidebar navigation (M11A task §8). Hidden below the tablet breakpoint in favour of MobileNav. */
export function Sidebar() {
  return (
    <nav className={styles.sidebar} aria-label="Primary">
      <ProductIdentity />
      <NavList items={NAV_ITEMS} />
    </nav>
  );
}
