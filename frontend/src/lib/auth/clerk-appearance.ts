/** Clerk component styling aligned with the application's design tokens (dark, high contrast). */
export const clerkAppearance = {
  variables: {
    colorPrimary: "#5b9dd9",
    colorBackground: "#1b212b",
    colorInputBackground: "#14181f",
    colorText: "#eef1f6",
    colorTextSecondary: "#a7b1c2",
    colorInputText: "#eef1f6",
    colorDanger: "#de6b6b",
    colorNeutral: "#eef1f6",
    borderRadius: "0.5rem",
  },
  elements: {
    // Organizations are provisioned by an administrator (identity mapping); users cannot create their own.
    organizationSwitcherPopoverActionButton__createOrganization: { display: "none" },
    organizationListCreateOrganizationActionButton: { display: "none" },
  },
} as const;
