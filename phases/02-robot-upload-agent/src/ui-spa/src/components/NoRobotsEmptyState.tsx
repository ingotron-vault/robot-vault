import { useState } from "react";
import { Button, EmptyState } from "@robot-vault/ui-kit";
import { useRobot } from "../context/RobotContext";
import { BASE_ROOT } from "../lib/api";

// Edge state: no robots discovered under BASE_ROOT at all (e.g. the host
// bind-mount is missing or empty). Offers the same custom-root escape hatch
// as the status strip so the agent is still usable without discovery.
export function NoRobotsEmptyState() {
  const { setRoot } = useRobot();
  const [value, setValue] = useState(BASE_ROOT);

  return (
    <EmptyState
      title="No robots discovered"
      description={
        <>
          No robot directories were found under <code className="rounded bg-muted-foreground/10 px-1">{BASE_ROOT}</code>.
          Make sure the host data directory is mounted (see <code className="rounded bg-muted-foreground/10 px-1">ROBOT_DATA_HOST_DIR</code> in
          the deployment config), or enter a root path directly below.
        </>
      }
      action={
        <form
          className="flex gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            if (value.trim()) setRoot(value.trim());
          }}
        >
          <input
            className="rounded-md border border-border bg-background px-2 py-1.5 text-sm text-foreground"
            value={value}
            onChange={(e) => setValue(e.target.value)}
            placeholder={BASE_ROOT}
          />
          <Button type="submit" size="sm">
            Use this root
          </Button>
        </form>
      }
    />
  );
}
