import { HTMLAttributes, ReactNode } from "react";
import { cn } from "../cn";
import { Tone } from "../status";
import { toneClasses } from "../toneClasses";

export type CalloutTone = Extract<Tone, "info" | "warning" | "danger">;

export interface CalloutProps extends Omit<HTMLAttributes<HTMLDivElement>, "title"> {
  tone?: CalloutTone;
  title?: ReactNode;
}

export function Callout({ tone = "info", title, className, children, ...props }: CalloutProps) {
  return (
    <div
      role={tone === "danger" ? "alert" : "note"}
      className={cn("rounded-md border px-3 py-2 text-sm", toneClasses(tone, "chip").wrapper, className)}
      {...props}
    >
      {title ? <div className="mb-1 font-semibold">{title}</div> : null}
      <div>{children}</div>
    </div>
  );
}
