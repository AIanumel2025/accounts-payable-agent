import { MISSING_VALUE_DISPLAY, MISSING_VALUE_LABEL } from "@/lib/formatting/money";

const INTEGER_FORMATTER = new Intl.NumberFormat("en-GB", { maximumFractionDigits: 0 });

export interface FormattedNumber {
  display: string;
  isMissing: boolean;
  ariaLabel: string;
}

export function formatInteger(value: number | null | undefined): FormattedNumber {
  if (value === null || value === undefined || Number.isNaN(value)) {
    return { display: MISSING_VALUE_DISPLAY, isMissing: true, ariaLabel: `${MISSING_VALUE_LABEL}` };
  }
  const display = INTEGER_FORMATTER.format(value);
  return { display, isMissing: false, ariaLabel: display };
}

/** `value` is a proportion in [0, 1] unless `isPercentagePoints` is true (already 0-100). */
export function formatPercentage(
  value: number | null | undefined,
  options: { fractionDigits?: number; isPercentagePoints?: boolean } = {},
): FormattedNumber {
  if (value === null || value === undefined || Number.isNaN(value)) {
    return { display: MISSING_VALUE_DISPLAY, isMissing: true, ariaLabel: `${MISSING_VALUE_LABEL}` };
  }
  const fractionDigits = options.fractionDigits ?? 0;
  const points = options.isPercentagePoints ? value : value * 100;
  const display = `${points.toFixed(fractionDigits)}%`;
  return { display, isMissing: false, ariaLabel: `${display}` };
}
