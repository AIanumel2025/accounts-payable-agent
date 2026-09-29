import { ActorRoleIndicator } from "@/components/application-shell/ActorRoleIndicator";
import { BackendHealthIndicator, type BackendHealthState } from "@/components/application-shell/BackendHealthIndicator";
import { TenantContextIndicator } from "@/components/application-shell/TenantContextIndicator";
import { ValidationOnlyIndicator } from "@/components/application-shell/ValidationOnlyIndicator";
import styles from "./PageHeader.module.css";

export interface PageHeaderProps {
  title: string;
  description?: string;
  actorRole: string;
  backendHealth: BackendHealthState;
  commandMode: "COMMIT" | "VALIDATION_ONLY" | "UNKNOWN";
  /** M11C: only the invoice-detail page passes this. */
  frontendMode?: "disabled" | "validation_only" | "commit";
}

/** Page header (M11A task §8): title plus the four required status indicators. */
export function PageHeader({ title, description, actorRole, backendHealth, commandMode, frontendMode }: PageHeaderProps) {
  return (
    <header className={styles.header}>
      <div>
        <h1 className={styles.title}>{title}</h1>
        {description ? <p className={styles.description}>{description}</p> : null}
      </div>
      <div className={styles.indicators}>
        <TenantContextIndicator />
        <ActorRoleIndicator role={actorRole} />
        <BackendHealthIndicator initial={backendHealth} />
        <ValidationOnlyIndicator commandMode={commandMode} frontendMode={frontendMode} />
      </div>
    </header>
  );
}
