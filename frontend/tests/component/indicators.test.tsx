import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ActorRoleIndicator } from "@/components/application-shell/ActorRoleIndicator";
import { TenantContextIndicator } from "@/components/application-shell/TenantContextIndicator";
import { ValidationOnlyIndicator } from "@/components/application-shell/ValidationOnlyIndicator";

describe("TenantContextIndicator", () => {
  it("never renders a UUID-shaped value (task §16: no development tenant id in browser output)", () => {
    render(<TenantContextIndicator />);
    expect(document.body.textContent).not.toMatch(/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/i);
    expect(screen.getByText("Development (single-tenant)")).toBeInTheDocument();
  });
});

describe("ActorRoleIndicator", () => {
  it("humanizes a known role", () => {
    render(<ActorRoleIndicator role="READ_ONLY_AUDITOR" />);
    expect(screen.getByText("Read-Only Auditor")).toBeInTheDocument();
  });

  it("falls back to the raw role string for an unrecognized role rather than throwing", () => {
    render(<ActorRoleIndicator role="SOME_NEW_ROLE" />);
    expect(screen.getByText("SOME_NEW_ROLE")).toBeInTheDocument();
  });
});

describe("ValidationOnlyIndicator", () => {
  it("shows read-only/validation-only for VALIDATION_ONLY", () => {
    render(<ValidationOnlyIndicator commandMode="VALIDATION_ONLY" />);
    expect(screen.getByText("Read-only / validation-only")).toBeInTheDocument();
  });

  it("defaults to the safe validation-only presentation when the mode is unknown", () => {
    render(<ValidationOnlyIndicator commandMode="UNKNOWN" />);
    expect(screen.getByText("Read-only / validation-only")).toBeInTheDocument();
  });

  it("visibly flags when commands are enabled", () => {
    render(<ValidationOnlyIndicator commandMode="COMMIT" />);
    expect(screen.getByText("Commands enabled")).toBeInTheDocument();
  });
});
