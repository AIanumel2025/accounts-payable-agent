"use client";

import type { ReasonCodeOption } from "@/lib/commands/reason-codes";
import styles from "./ReviewActions.module.css";

interface Props {
  legend: string;
  options: ReasonCodeOption[];
  selected: string[];
  onChange: (next: string[]) => void;
  error?: string | null;
  idPrefix: string;
}

export function ReasonCodePicker({ legend, options, selected, onChange, error, idPrefix }: Props) {
  return (
    <fieldset className={styles.fieldset} aria-describedby={error ? `${idPrefix}-error` : undefined}>
      <legend className={styles.legend}>{legend}</legend>
      {options.map((option) => (
        <label key={option.code} className={styles.checkRow}>
          <input
            type="checkbox"
            checked={selected.includes(option.code)}
            onChange={(event) =>
              onChange(event.target.checked ? [...selected, option.code] : selected.filter((code) => code !== option.code))
            }
          />
          <span>{option.label}</span>
        </label>
      ))}
      {error ? (
        <p id={`${idPrefix}-error`} className={styles.fieldError}>{error}</p>
      ) : null}
    </fieldset>
  );
}
