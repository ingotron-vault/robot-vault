import { ReactNode } from "react";
import { cn } from "../cn";

export interface StepperStep {
  title: ReactNode;
  description?: ReactNode;
}

export interface StepperProps {
  steps: StepperStep[];
  current: number;
  className?: string;
}

export function Stepper({ steps, current, className }: StepperProps) {
  return (
    <ol className={cn("flex flex-col gap-3 sm:flex-row", className)} aria-label="Progress">
      {steps.map((step, index) => {
        const isCurrent = index === current;
        const isDone = index < current;
        return (
          <li key={index} aria-current={isCurrent ? "step" : undefined} data-state={isDone ? "complete" : isCurrent ? "current" : "upcoming"} className="flex min-w-0 flex-1 items-start gap-2">
            <span
              aria-hidden="true"
              className={cn(
                "flex h-6 w-6 shrink-0 items-center justify-center rounded-full border text-xs font-semibold",
                isDone || isCurrent ? "border-accent bg-accent text-accent-foreground" : "border-border text-muted-foreground",
              )}
            >
              {isDone ? "✓" : index + 1}
            </span>
            <span className="min-w-0 text-sm">
              <span className={cn("block font-medium", !isCurrent && !isDone && "text-muted-foreground")}>{step.title}</span>
              {step.description ? <span className="block text-xs text-muted-foreground">{step.description}</span> : null}
            </span>
          </li>
        );
      })}
    </ol>
  );
}
