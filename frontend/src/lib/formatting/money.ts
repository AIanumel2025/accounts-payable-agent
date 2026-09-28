/**
 * Monetary formatting (M11A task §11).
 *
 * FastAPI serializes every monetary value as an exact-precision JSON
 * *string* (`ap_agent/api/schemas.py`'s `DecimalString`: `format(value,
 * "f")`), specifically so a client never loses precision by round-tripping
 * through a JS `number`. This module therefore never parses the amount
 * into a `number` and reformats it -- it preserves the exact digit string
 * received and only prefixes the currency code. A missing amount is never
 * rendered as zero; it is rendered as an explicit "not available" marker.
 */

export const MISSING_VALUE_LABEL = "Not available";
export const MISSING_VALUE_DISPLAY = "—";

export interface FormattedMoney {
  display: string;
  isMissing: boolean;
  ariaLabel: string;
}

export function formatMoney(amount: string | null | undefined, currency: string | null | undefined): FormattedMoney {
  if (amount === null || amount === undefined) {
    return {
      display: MISSING_VALUE_DISPLAY,
      isMissing: true,
      ariaLabel: `Amount ${MISSING_VALUE_LABEL.toLowerCase()}`,
    };
  }

  const currencyLabel = currency ?? "";
  const display = currencyLabel ? `${currencyLabel} ${amount}` : amount;
  return {
    display,
    isMissing: false,
    ariaLabel: currencyLabel ? `${amount} ${currencyLabel}` : amount,
  };
}

export function formatCurrencyCode(currency: string | null | undefined): FormattedMoney {
  if (currency === null || currency === undefined || currency.trim() === "") {
    return { display: MISSING_VALUE_DISPLAY, isMissing: true, ariaLabel: `Currency ${MISSING_VALUE_LABEL.toLowerCase()}` };
  }
  return { display: currency, isMissing: false, ariaLabel: currency };
}
