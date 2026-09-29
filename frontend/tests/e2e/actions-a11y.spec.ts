import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";
import { APPROVE_CASE, CLAIM_CASE, CORRECT_CASE, URLS, claimViaUi, control, openCase, resetMock, result, summary, watchConsole } from "./support/actions";

test.beforeEach(async ({ request }) => resetMock(request));

async function assertNoSeriousViolations(page: import("@playwright/test").Page, label: string) {
  const scan = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"]).analyze();
  const serious = scan.violations.filter((v) => v.impact === "serious" || v.impact === "critical");
  expect(serious.map((v) => `${v.id}: ${v.nodes.map((n) => n.target.join(" ")).join(", ")}`), label).toEqual([]);
}

test.describe("axe-core (no serious or critical violations)", () => {
  test("open case, claimed case, correction editor, dialog and results", async ({ page }) => {
    await openCase(page, URLS.commit, CORRECT_CASE);
    await assertNoSeriousViolations(page, "open case");

    await claimViaUi(page);
    await assertNoSeriousViolations(page, "claimed case + result banner");

    await page.getByRole("button", { name: "Correct", exact: true }).click();
    await assertNoSeriousViolations(page, "correction editor");

    await page.getByRole("button", { name: "Review and submit correction" }).click(); // triggers the error summary
    await assertNoSeriousViolations(page, "correction errors");

    await page.getByRole("button", { name: "Discard correction form" }).click();
    await page.getByRole("button", { name: "Approve", exact: true }).click();
    await assertNoSeriousViolations(page, "approve dialog");
  });

  test("disabled, validation-only, mismatch and read-only states", async ({ page, request }) => {
    await openCase(page, URLS.disabled, CLAIM_CASE);
    await assertNoSeriousViolations(page, "disabled");
    await control(request, { health_mode: "VALIDATION_ONLY" });
    await openCase(page, URLS.validation, CLAIM_CASE);
    await assertNoSeriousViolations(page, "validation-only");
    await openCase(page, URLS.commit, CLAIM_CASE);
    await assertNoSeriousViolations(page, "mismatch");
    await openCase(page, URLS.auditor, CLAIM_CASE);
    await assertNoSeriousViolations(page, "read-only");
  });
});

test.describe("keyboard-only workflow", () => {
  test("claim and approve using only the keyboard", async ({ page }) => {
    const problems = watchConsole(page);
    await openCase(page, URLS.commit, APPROVE_CASE);

    const claim = page.getByRole("button", { name: "Claim this case" });
    await claim.focus();
    await expect(claim).toBeFocused();
    await page.keyboard.press("Enter");
    await expect(result(page)).toHaveAttribute("data-result", "executed");
    await expect(result(page)).toBeFocused(); // focus moved to the result summary

    const approve = page.getByRole("button", { name: "Approve", exact: true });
    await approve.focus();
    await page.keyboard.press("Enter");

    const dialog = page.getByRole("dialog", { name: "Approve this invoice?" });
    await expect(dialog).toBeVisible();
    await expect(dialog.getByRole("button", { name: "Cancel" })).toBeFocused(); // safe default focus
    await expect(dialog.getByRole("button", { name: "Approve invoice" })).toBeDisabled();
    await page.keyboard.press("Tab"); // Approve is disabled, so focus wraps from Cancel to the first reason code
    await expect(dialog.getByRole("checkbox").first()).toBeFocused();
    await page.keyboard.press("Space");
    await expect(dialog.getByRole("checkbox").first()).toBeChecked();
    await dialog.getByRole("button", { name: "Approve invoice" }).focus();
    await page.keyboard.press("Enter");

    await expect(summary(page)).toContainText("Resolved");
    expect(problems).toEqual([]);
  });

  test("the confirmation dialog traps focus, closes on Escape and restores focus to its opener", async ({ page }) => {
    await openCase(page, URLS.commit, APPROVE_CASE);
    await claimViaUi(page);
    const approve = page.getByRole("button", { name: "Approve", exact: true });
    await approve.focus();
    await page.keyboard.press("Enter");
    const dialog = page.getByRole("dialog");
    await expect(dialog).toBeVisible();

    // Tab many times: focus must never leave the dialog.
    for (let i = 0; i < 14; i += 1) {
      await page.keyboard.press("Tab");
      const inside = await page.evaluate(() => document.activeElement?.closest("dialog") !== null);
      expect(inside, `tab #${i + 1}`).toBe(true);
    }
    for (let i = 0; i < 14; i += 1) {
      await page.keyboard.press("Shift+Tab");
      const inside = await page.evaluate(() => document.activeElement?.closest("dialog") !== null);
      expect(inside, `shift-tab #${i + 1}`).toBe(true);
    }

    await page.keyboard.press("Escape");
    await expect(dialog).toBeHidden();
    await expect(approve).toBeFocused();
    await expect(result(page)).not.toContainText("Approved");
  });

  test("the correction editor is reachable and usable by keyboard, with an announced error summary", async ({ page }) => {
    await openCase(page, URLS.commit, CORRECT_CASE);
    await claimViaUi(page);
    await page.getByRole("button", { name: "Correct", exact: true }).focus();
    await page.keyboard.press("Enter");
    await expect(page.getByLabel("Field to correct")).toBeFocused();
    await page.getByRole("button", { name: "Review and submit correction" }).focus();
    await page.keyboard.press("Enter");
    const alert = page.getByTestId("correction-error-summary");
    await expect(alert).toBeFocused();
    await expect(alert).toHaveAttribute("role", "alert");
  });
});

test.describe("responsive and motion", () => {
  test.use({ viewport: { width: 390, height: 844 } });

  test("mobile: actions stack, nothing overflows horizontally, dialogs fit the screen", async ({ page }) => {
    await openCase(page, URLS.commit, APPROVE_CASE);
    await claimViaUi(page);
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    expect(overflow).toBeLessThanOrEqual(1);

    await page.getByRole("button", { name: "Approve", exact: true }).click();
    const box = await page.getByRole("dialog").boundingBox();
    expect(box).not.toBeNull();
    expect(box!.x).toBeGreaterThanOrEqual(0);
    expect(box!.x + box!.width).toBeLessThanOrEqual(390 + 1);
    await assertNoSeriousViolations(page, "mobile dialog");
  });

  test("reduced motion is respected", async ({ page }) => {
    await page.emulateMedia({ reducedMotion: "reduce" });
    await openCase(page, URLS.commit, CLAIM_CASE);
    const matches = await page.evaluate(() => matchMedia("(prefers-reduced-motion: reduce)").matches);
    expect(matches).toBe(true);
    const animated = await page.evaluate(() =>
      [...document.querySelectorAll("*")].filter((el) => {
        const style = getComputedStyle(el);
        return parseFloat(style.animationDuration) > 0.01 || parseFloat(style.transitionDuration) > 0.01;
      }).length,
    );
    expect(animated).toBe(0);
  });
});
