/**
 * The agents endpoint answers `success_rate: null` for an agent with no recorded run
 * (every agent, right after a restart). The card must show such an agent, not throw.
 */
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { AgentCard } from "./AgentCard";

const agent = (figures: {
  success_rate?: number | null;
  total_runs?: number | null;
}) => ({
  name: "Agent One",
  description: "An agent",
  status: "idle" as const,
  ...figures,
});

describe("AgentCard and an agent that never ran", () => {
  it("renders the card when the success rate is null", () => {
    render(<AgentCard agent={agent({ success_rate: null, total_runs: 0 })} />);

    expect(screen.getByText("Agent One")).toBeInTheDocument();
    expect(screen.getByText(/Total runs: 0/)).toBeInTheDocument();
    expect(screen.queryByText(/Success:/)).toBeNull();
  });

  it("shows no figures row when both figures are null", () => {
    render(
      <AgentCard agent={agent({ success_rate: null, total_runs: null })} />,
    );

    expect(screen.getByText("Agent One")).toBeInTheDocument();
    expect(screen.queryByText(/Success:/)).toBeNull();
    expect(screen.queryByText(/Total runs:/)).toBeNull();
  });

  it("shows both figures of an agent that ran, as before", () => {
    render(<AgentCard agent={agent({ success_rate: 87.5, total_runs: 8 })} />);

    expect(screen.getByText(/Success: 87\.5%/)).toBeInTheDocument();
    expect(screen.getByText(/Total runs: 8/)).toBeInTheDocument();
  });

  it("shows a success rate of zero", () => {
    render(<AgentCard agent={agent({ success_rate: 0, total_runs: 3 })} />);

    expect(screen.getByText(/Success: 0\.0%/)).toBeInTheDocument();
  });
});
