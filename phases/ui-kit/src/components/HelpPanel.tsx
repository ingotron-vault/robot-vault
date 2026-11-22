import { ReactNode, useMemo, useState } from "react";
import { Drawer } from "./Drawer";

export interface HelpSection {
  id: string;
  title: string;
  body: ReactNode;
  keywords?: string[];
}

export interface HelpPanelProps {
  open: boolean;
  onClose: () => void;
  title?: string;
  sections: HelpSection[];
}

export function HelpPanel({ open, onClose, title = "Help", sections }: HelpPanelProps) {
  const [query, setQuery] = useState("");
  const normalized = query.trim().toLocaleLowerCase();
  const visible = useMemo(() => sections.filter((section) => {
    const terms = [section.title, ...(section.keywords || [])].join(" ").toLocaleLowerCase();
    return !normalized || terms.includes(normalized);
  }), [normalized, sections]);

  return (
    <Drawer open={open} onClose={onClose} title={title} size="lg">
      <label className="mb-4 flex flex-col gap-1 text-sm font-medium">
        Search help
        <input
          data-autofocus
          type="search"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder="Search tasks"
          className="rounded-md border border-border bg-background px-3 py-2 text-sm font-normal"
        />
      </label>
      <div className="flex flex-col gap-4">
        {visible.map((section) => (
          <section key={section.id} aria-labelledby={`help-${section.id}`}>
            <h3 id={`help-${section.id}`} className="text-sm font-semibold">{section.title}</h3>
            <div className="mt-1 text-sm text-muted-foreground">{section.body}</div>
          </section>
        ))}
        {!visible.length ? <p className="text-sm text-muted-foreground">No help topics match that search.</p> : null}
      </div>
    </Drawer>
  );
}
