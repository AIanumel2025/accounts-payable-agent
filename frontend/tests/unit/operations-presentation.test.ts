import { describe, expect, it } from "vitest";
import {
  MAX_UPLOAD_BYTES,
  formatFileSize,
  isTerminalJob,
  jobErrorPresentation,
  jobOutcomeText,
  jobStatusPresentation,
  uploadErrorMessages,
  validateSelectedFile,
} from "@/lib/operations/presentation";
import type { JobPayload } from "@/types/api-payloads";

const job = (over: Partial<JobPayload> = {}): JobPayload =>
  ({ job_id: "1", job_type: "PROCESS_DOCUMENT", status: "QUEUED", source_name: "a.pdf", summary: { review_reasons: [], stages: [], executed_stages: [], corrected_fields: [] }, ...over }) as JobPayload;

describe("job status presentation", () => {
  it("pairs every status with a label and glyph (never colour alone)", () => {
    for (const status of ["QUEUED", "RUNNING", "SUCCEEDED", "REVIEW_REQUIRED", "FAILED"] as const) {
      const presentation = jobStatusPresentation(job({ status }));
      expect(presentation.label.length).toBeGreaterThan(2);
      expect(presentation.glyph.length).toBeGreaterThan(0);
    }
  });

  it("calls a resume that needs review 'Returned to review'", () => {
    expect(jobStatusPresentation(job({ job_type: "RESUME_WORKFLOW", status: "REVIEW_REQUIRED" })).label).toBe("Returned to review");
    expect(jobStatusPresentation(job({ status: "REVIEW_REQUIRED" })).label).toBe("Review required");
    expect(jobStatusPresentation(job({ status: "SUCCEEDED" })).label).toBe("Completed automatically");
  });

  it("only SUCCEEDED, REVIEW_REQUIRED and FAILED are terminal", () => {
    expect(["QUEUED", "RUNNING"].map((status) => isTerminalJob({ status } as JobPayload))).toEqual([false, false]);
    expect(["SUCCEEDED", "REVIEW_REQUIRED", "FAILED"].map((status) => isTerminalJob({ status } as JobPayload))).toEqual([true, true, true]);
  });

  it("never claims success before the worker has finished", () => {
    expect(jobOutcomeText(job({ status: "QUEUED" }))).toMatch(/waiting/i);
    expect(jobOutcomeText(job({ status: "RUNNING" }))).not.toMatch(/completed/i);
    expect(jobOutcomeText(job({ job_type: "RESUME_WORKFLOW", status: "QUEUED" }))).toMatch(/waiting/i);
  });
});

describe("error copy", () => {
  it("maps known and pattern-matched job codes, never echoing raw codes", () => {
    expect(jobErrorPresentation("ARTIFACT_HASH_MISMATCH").title).toMatch(/verification/i);
    expect(jobErrorPresentation("OCR_TRANSIENT_FAILURE").description).toMatch(/stage failed/i);
    const unknown = jobErrorPresentation("SELECT * FROM secrets");
    expect(JSON.stringify(unknown)).not.toContain("SELECT");
    expect(jobErrorPresentation(null).title).toBe("Processing failed");
  });

  it("maps upload codes and falls back generically", () => {
    expect(uploadErrorMessages(["FILE_TOO_LARGE"])[0]).toMatch(/10 MB/);
    expect(uploadErrorMessages(["PAYMENT_FIELD_PROHIBITED"])[0]).toMatch(/do not exist/);
    expect(uploadErrorMessages(["WHAT_IS_THIS"])).toEqual(["The upload was not accepted."]);
  });
});

describe("client-side file validation", () => {
  it("accepts supported types within the limit", () => {
    for (const name of ["a.pdf", "b.PNG", "c.jpg", "d.jpeg"]) expect(validateSelectedFile({ name, size: 10 })).toBeNull();
  });

  it("rejects empty, oversized and unsupported files", () => {
    expect(validateSelectedFile({ name: "a.pdf", size: 0 })).toMatch(/empty/);
    expect(validateSelectedFile({ name: "a.pdf", size: MAX_UPLOAD_BYTES + 1 })).toMatch(/too large/);
    expect(validateSelectedFile({ name: "a.exe", size: 10 })).toMatch(/supported/);
  });

  it("formats sizes", () => {
    expect([formatFileSize(10), formatFileSize(2048), formatFileSize(5 * 1024 * 1024)]).toEqual(["10 B", "2.0 KB", "5.0 MB"]);
  });
});
