import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter, useLocation, useNavigate } from "react-router-dom";
import { describe, expect, it } from "vitest";
import { useUrlFilters } from "../lib/urlFilters";

function Probe() {
  const location = useLocation();
  const navigate = useNavigate();
  const [, update] = useUrlFilters();
  return <>
    <div data-testid="search">{location.search}</div>
    <button onClick={() => update({ robot_id: "robot-a", status: null, offset: 0 })}>Filter</button>
    <button onClick={() => navigate(-1)}>Back</button>
  </>;
}

describe("useUrlFilters", () => {
  it("patches fields atomically, preserves other keys, and replaces the current history entry", () => {
    render(<MemoryRouter initialEntries={["/uploads?previous=yes", "/uploads?status=failed&offset=50&keep=yes"]} initialIndex={1}><Probe /></MemoryRouter>);
    fireEvent.click(screen.getByRole("button", { name: "Filter" }));
    const params = new URLSearchParams(screen.getByTestId("search").textContent ?? "");
    expect(params.get("robot_id")).toBe("robot-a");
    expect(params.has("status")).toBe(false);
    expect(params.get("offset")).toBe("0");
    expect(params.get("keep")).toBe("yes");
    fireEvent.click(screen.getByRole("button", { name: "Back" }));
    expect(screen.getByTestId("search")).toHaveTextContent("previous=yes");
  });
});
