import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { EmptyState } from "@/components/feedback/EmptyState";
import { LoadingState } from "@/components/feedback/LoadingState";
import { StaleDataBanner } from "@/components/feedback/StaleDataBanner";

describe("LoadingState", () => {
  it("announces itself to assistive technology via a status live region", () => {
    render(<LoadingState label="Loading dashboard data…" />);
    expect(screen.getByRole("status")).toHaveTextContent("Loading dashboard data…");
  });
});

describe("EmptyState", () => {
  it("renders a title and description", () => {
    render(<EmptyState title="Nothing here" description="No data to show." />);
    expect(screen.getByText("Nothing here")).toBeInTheDocument();
    expect(screen.getByText("No data to show.")).toBeInTheDocument();
  });
});

describe("StaleDataBanner", () => {
  it("shows the last-refreshed time and preserves the UTC instant in the time element", () => {
    render(<StaleDataBanner asOfIso="2026-06-15T12:00:00Z" />);
    const time = screen.getByRole("status").querySelector("time");
    expect(time).toHaveAttribute("dateTime", "2026-06-15T12:00:00Z");
  });
});
