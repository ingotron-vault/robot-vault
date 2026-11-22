import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import {
  Callout,
  HelpPanel,
  KpiTile,
  ProgressBar,
  SegmentedControl,
  Skeleton,
  Stepper,
  Tour,
  TrendSpark,
  useTour,
} from "../index";

const steps = [
  { targetSelector: "#first-target", title: "First", body: "First step" },
  { targetSelector: "#second-target", title: "Second", body: "Second step" },
];

function TourHarness({ missingSecond = false }: { missingSecond?: boolean }) {
  const tour = useTour("test-tour");
  return <><button id="first-target">First target</button>{!missingSecond ? <button id="second-target">Second target</button> : null}<button onClick={tour.start}>Relaunch</button><Tour tourId="test-tour" steps={steps} open={tour.isOpen} onClose={tour.close} onComplete={tour.complete} /></>;
}

function MissingTargetHarness() {
  const tour = useTour("missing-tour");
  return <Tour tourId="missing-tour" steps={[{ targetSelector: "#never-mounted", title: "Missing", body: "Missing target" }]} open={tour.isOpen} onClose={tour.close} onComplete={tour.complete} />;
}

describe("Tour", () => {
  it("advances through steps and persists completion", () => {
    render(<TourHarness />);
    expect(screen.getByRole("heading", { name: "First" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    expect(screen.getByRole("heading", { name: "Second" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Done" }));
    expect(localStorage.getItem("robot-vault:tour:test-tour:completed")).toBe("true");
  });

  it("persists Esc, accepts arrow navigation, and restores the opener focus", async () => {
    localStorage.setItem("robot-vault:tour:test-tour:completed", "true");
    render(<TourHarness />);
    const opener = screen.getByRole("button", { name: "Relaunch" });
    opener.focus();
    fireEvent.click(opener);
    localStorage.removeItem("robot-vault:tour:test-tour:completed");
    await waitFor(() => expect(screen.getByRole("dialog", { name: "First" })).toHaveFocus());
    fireEvent.keyDown(document, { key: "ArrowRight" });
    expect(screen.getByRole("heading", { name: "Second" })).toBeInTheDocument();
    fireEvent.keyDown(document, { key: "ArrowLeft" });
    expect(screen.getByRole("heading", { name: "First" })).toBeInTheDocument();
    fireEvent.keyDown(document, { key: "Escape" });
    expect(localStorage.getItem("robot-vault:tour:test-tour:completed")).toBe("true");
    await waitFor(() => expect(opener).toHaveFocus());
  });

  it("stays closed when a completed tour is remounted", () => {
    const rendered = render(<TourHarness />);
    fireEvent.click(screen.getByRole("button", { name: "Skip tour" }));
    rendered.unmount();
    render(<TourHarness />);
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("completes when no tour target can be mounted", async () => {
    render(<MissingTargetHarness />);
    await waitFor(() => expect(localStorage.getItem("robot-vault:tour:missing-tour:completed")).toBe("true"));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });
});

describe("new ui-kit components", () => {
  it("renders semantic controls and searchable help", async () => {
    const change = vi.fn();
    render(
      <>
        <SegmentedControl
          value="one"
          onChange={change}
          options={[{ value: "one", label: "One" }, { value: "two", label: "Two" }]}
        />
        <Skeleton className="h-4" />
        <Callout tone="warning" title="Heads up">Check this.</Callout>
        <Stepper current={1} steps={[{ title: "Select" }, { title: "Upload" }]} />
        <KpiTile label="Uploads" value="12" trend={[1, 2, 3]} />
        <ProgressBar label="Upload abc progress" value={50} />
        <HelpPanel
          open
          onClose={() => {}}
          sections={[{ id: "upload", title: "Upload files", body: "Choose files.", keywords: ["queue"] }]}
        />
      </>
    );
    fireEvent.click(screen.getByRole("button", { name: "Two" }));
    expect(change).toHaveBeenCalledWith("two");
    expect(screen.getByRole("note")).toHaveTextContent("Heads up");
    expect(screen.getByRole("progressbar", { name: "Upload abc progress" })).toHaveAttribute("aria-valuenow", "50");
    expect(screen.getByRole("img", { name: "Uploads trend" })).toBeInTheDocument();
    expect(screen.getByText("Select").closest("li")).toHaveAttribute("data-state", "complete");
    expect(screen.getByText("Upload").closest("li")).toHaveAttribute("aria-current", "step");
    await waitFor(() => expect(screen.getByRole("searchbox")).toHaveFocus());
    fireEvent.change(screen.getByRole("searchbox"), { target: { value: "queue" } });
    expect(screen.getByText("Upload files")).toBeInTheDocument();
  });

  it("uses alert semantics for danger callouts and has an honest sparse-trend fallback", () => {
    render(<><Callout tone="danger">Connection failed</Callout><TrendSpark values={[1]} label="Sparse trend" /></>);
    expect(screen.getByRole("alert")).toHaveTextContent("Connection failed");
    expect(screen.getByText("No trend")).toBeInTheDocument();
  });
});
