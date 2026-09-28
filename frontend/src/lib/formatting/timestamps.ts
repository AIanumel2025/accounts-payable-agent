/**
 * Timestamp formatting (M11A task §11). Displays in the user-facing
 * Europe/London timezone while preserving the underlying UTC ISO-8601
 * string for `<time dateTime="...">`/accessible text, so a screen reader
 * or a copy-paste never loses the unambiguous UTC instant.
 */

const LONDON_TIME_ZONE = "Europe/London";

const ABSOLUTE_FORMATTER = new Intl.DateTimeFormat("en-GB", {
  timeZone: LONDON_TIME_ZONE,
  year: "numeric",
  month: "short",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  hour12: false,
  timeZoneName: "short",
});

export interface FormattedTimestamp {
  /** Human-readable, Europe/London local time, e.g. "28 Sep 2026, 17:03 GMT". */
  display: string;
  /** The original UTC instant, unchanged, for `<time dateTime>` / accessible text. */
  isoUtc: string;
  isValid: boolean;
}

export function formatAbsoluteTimestamp(isoUtc: string | null | undefined): FormattedTimestamp {
  if (!isoUtc) {
    return { display: "Not available", isoUtc: "", isValid: false };
  }
  const date = new Date(isoUtc);
  if (Number.isNaN(date.getTime())) {
    return { display: "Not available", isoUtc, isValid: false };
  }
  return { display: ABSOLUTE_FORMATTER.format(date), isoUtc, isValid: true };
}

const RELATIVE_FORMATTER = new Intl.RelativeTimeFormat("en-GB", { numeric: "auto" });

const RELATIVE_UNITS: Array<{ unit: Intl.RelativeTimeFormatUnit; seconds: number }> = [
  { unit: "year", seconds: 31536000 },
  { unit: "month", seconds: 2592000 },
  { unit: "week", seconds: 604800 },
  { unit: "day", seconds: 86400 },
  { unit: "hour", seconds: 3600 },
  { unit: "minute", seconds: 60 },
  { unit: "second", seconds: 1 },
];

/** `now` is injectable for deterministic tests; defaults to the real current time. */
export function formatRelativeTimestamp(isoUtc: string | null | undefined, now: Date = new Date()): string {
  if (!isoUtc) return "Not available";
  const date = new Date(isoUtc);
  if (Number.isNaN(date.getTime())) return "Not available";

  const diffSeconds = (date.getTime() - now.getTime()) / 1000;
  const absSeconds = Math.abs(diffSeconds);

  if (absSeconds < 5) return "just now";

  for (const { unit, seconds } of RELATIVE_UNITS) {
    if (absSeconds >= seconds || unit === "second") {
      const value = Math.round(diffSeconds / seconds);
      return RELATIVE_FORMATTER.format(value, unit);
    }
  }
  return "just now";
}
