import { ActorRoleIndicator } from "@/components/application-shell/ActorRoleIndicator";
import { BackendHealthIndicator, type BackendHealthState } from "@/components/application-shell/BackendHealthIndicator";
import { TenantContextIndicator } from "@/components/application-shell/TenantContextIndicator";
import { ValidationOnlyIndicator } from "@/components/application-shell/ValidationOnlyIndicator";
import styles from "./PageHeader.module.css";

export interface PageHeaderProps {
  title: string;
  description?: string;
  actorRole: string;
  /** M11E: the organization's display name from the database mapping (hosted mode only). */
  tenantName?: string | null;
  /** M11E: true when identity comes from the hosted sign-in path rather than development headers. */
  hosted?: boolean;
  backendHealth: BackendHealthState;
  commandMode: "COMMIT" | "VALIDATION_ONLY" | "UNKNOWN";
  /** M11C: only the invoice-detail page passes this. */
  frontendMode?: "disabled" | "validation_only" | "commit";
}

/** Page header (M11A task §8): title plus the four required status indicators. */
export function PageHeader({
  title,
  description,
  actorRole,
  tenantName,
  hosted = false,
  backendHealth,
  commandMode,
  frontendMode,
}: PageHeaderProps) {
  return (
    <header className={styles.header}>
      <div>
        <h1 className={styles.title}>{title}</h1>
        {description ? <p className={styles.description}>{description}</p> : null}
      </div>
      <div className={styles.indicators}>
        <TenantContextIndicator tenantName={tenantName} hosted={hosted} />
        <ActorRoleIndicator role={actorRole} hosted={hosted} />
        <BackendHealthIndicator initial={backendHealth} />
        <ValidationOnlyIndicator commandMode={commandMode} frontendMode={frontendMode} />
      </div>
    </header>
  );
}
