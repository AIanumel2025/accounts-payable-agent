import Link from "next/link";
import styles from "./Breadcrumb.module.css";

export interface BreadcrumbTrailItem {
  label: string;
  href: string;
}

/**
 * Breadcrumb navigation back to the review queue (M11B task §2/§8: "detail
 * pages reached from the queue with breadcrumb navigation back"). `trail`
 * holds every ancestor page (each an actual link); `current` is the
 * present page and is never a link, per standard breadcrumb semantics.
 */
export function Breadcrumb({ trail, current }: { trail: BreadcrumbTrailItem[]; current: string }) {
  return (
    <nav aria-label="Breadcrumb" className={styles.nav}>
      <ol className={styles.list}>
        {trail.map((item) => (
          <li key={item.href} className={styles.item}>
            <Link href={item.href} className={styles.link}>
              {item.label}
            </Link>
            <span aria-hidden="true" className={styles.separator}>
              /
            </span>
          </li>
        ))}
        <li className={styles.item}>
          <span aria-current="page" className={styles.current}>
            {current}
          </span>
        </li>
      </ol>
    </nav>
  );
}
