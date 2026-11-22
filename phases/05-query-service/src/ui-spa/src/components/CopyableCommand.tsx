/*
 * A shell command an operator is meant to copy, run elsewhere, and trust.
 *
 * Three rules from docs/UI_DESIGN_GUIDELINES.md are load-bearing here rather
 * than decorative:
 *
 *  - "Commands ... use mono at no less than the caption size."
 *  - "Long values may truncate visually only when copy access or an expanded
 *     view exposes the complete value." -- so the block scrolls, it never
 *     ellipsises, and the full text is always in the DOM for a screen reader
 *     and for a manual selection.
 *  - "Copy actions announce success without replacing the value." -- the button
 *     label does not become a checkmark; a separate polite live region says
 *     what happened.
 *
 * `navigator.clipboard` is absent on an insecure origin and can be refused by
 * permissions policy. Both are real on a vault an operator reached by IP
 * before TLS was sorted out, so the failure is reported rather than swallowed:
 * an operator who believes they copied a single-use enrollment code and pasted
 * nothing has lost the code.
 */
import { useEffect, useRef, useState } from "react";
import { Copy } from "lucide-react";
import { Button } from "@robot-vault/ui-kit";
import "./CopyableCommand.css";

export interface CopyableCommandProps {
  command: string;
  /** Names the block and the copy button; required for icon-adjacent controls. */
  label: string;
  className?: string;
}

type CopyOutcome = { ok: boolean; message: string } | null;

export default function CopyableCommand({ command, label, className }: CopyableCommandProps) {
  const [outcome, setOutcome] = useState<CopyOutcome>(null);
  const preRef = useRef<HTMLPreElement | null>(null);
  const timerRef = useRef<number | null>(null);

  useEffect(() => {
    return () => {
      if (timerRef.current !== null) window.clearTimeout(timerRef.current);
    };
  }, []);

  // A new command is a new thing to copy; a stale "Copied" beside it would be
  // a lie, and for a single-use code an expensive one.
  useEffect(() => {
    setOutcome(null);
  }, [command]);

  function announce(next: CopyOutcome) {
    setOutcome(next);
    if (timerRef.current !== null) window.clearTimeout(timerRef.current);
    timerRef.current = window.setTimeout(() => setOutcome(null), 6000);
  }

  async function copy() {
    try {
      if (!navigator.clipboard?.writeText) throw new Error("clipboard unavailable");
      await navigator.clipboard.writeText(command);
      announce({ ok: true, message: `${label} copied to the clipboard.` });
    } catch {
      // Select the text so the operator can copy it by hand rather than
      // hunting for the start of a wrapped command.
      const node = preRef.current;
      if (node && window.getSelection) {
        const range = document.createRange();
        range.selectNodeContents(node);
        const selection = window.getSelection();
        selection?.removeAllRanges();
        selection?.addRange(range);
      }
      announce({
        ok: false,
        message: `${label} could not be copied automatically. It is selected — copy it manually.`,
      });
    }
  }

  return (
    <div className={className ? `rv-command ${className}` : "rv-command"}>
      <div className="rv-command__bar">
        <span className="rv-command__label">{label}</span>
        <Button size="sm" variant="secondary" onClick={() => void copy()} aria-label={`Copy ${label}`}>
          <Copy size={14} aria-hidden="true" />
          <span>Copy</span>
        </Button>
      </div>
      {/*
        `role="group"` is load-bearing, not decoration. A bare `<pre>` maps to
        `role=generic`, on which ARIA PROHIBITS naming -- a conformant screen
        reader drops `aria-label` entirely, and a keyboard user lands in an
        unnamed focusable region holding a single-use enrollment code. `group`
        is a role that supports an accessible name, so the label survives.
      */}
      <pre ref={preRef} className="rv-command__body" tabIndex={0} role="group" aria-label={label}>
        {command}
      </pre>
      <p className="rv-command__status" role="status">
        {outcome ? outcome.message : ""}
      </p>
    </div>
  );
}
