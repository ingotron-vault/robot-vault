import { HTMLAttributes } from "react";
import { cn } from "../cn";

/** A non-semantic placeholder; pair it with a nearby loading label when needed. */
export function Skeleton({ className, ...props }: HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      aria-hidden="true"
      className={cn("animate-pulse rounded-md bg-muted-foreground/15", className)}
      {...props}
    />
  );
}
