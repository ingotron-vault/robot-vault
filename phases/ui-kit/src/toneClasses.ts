import type { Tone } from "./status";

/*
 * The tone -> Tailwind class tables the badge-shaped surfaces share.
 *
 * WHY THE CLASSES ARE LITERAL STRINGS IN A TABLE
 * ==============================================
 * Tailwind's JIT reads source text, so `bg-${token}/10` produces nothing. Every
 * class a surface can use has to appear literally somewhere in a scanned file,
 * which makes a lookup table the only shape available -- and one table shared
 * by every SPA (this directory is in both `tailwind.config.cjs` content globs)
 * beats one per page, which is what this replaced.
 *
 * WHY THREE VARIANTS AND NOT ONE
 * ==============================
 * Because there really are three badges in the product, and they were arrived
 * at separately for reasons that still hold:
 *
 *   `pill`  border + 10% tint + a full-saturation DOT, label `text-foreground`.
 *           Status pills, where the dot is doing the
 *           colour-carrying work so the label can stay maximally readable.
 *   `chip`  border + 15% tint, NO dot, label `text-foreground`. Filter/status
 *           chips and Home's posture chips: dense, many per row,
 *           where a dot per chip becomes visual noise, so the slightly stronger
 *           tint carries the tone instead.
 *   `hued`  border + 10% tint + dot, label in the tone's own `-foreground`
 *           token. `StatusPill`'s own look -- the only one that tints the text,
 *           which is why `--success-foreground`/`--warning-foreground`/
 *           `--danger-foreground` exist (see tokens.css).
 *
 * They differ only in tint weight, the dot, and the label colour, and merging
 * them would change pixels on shipped pages. Keeping all three HERE is still
 * the consolidation: there is now one place to look, one place to fix a
 * contrast bug, and no page holds a colour of its own.
 *
 * CONTRAST
 * --------
 * `pill` and `chip` keep the label at `text-foreground` because putting the
 * tone on the text over a 10% tint of itself measures ~1.8:1 for `warning` on
 * the light theme. `hued` is safe only because it uses the tuned
 * `-foreground` tokens rather than the raw hue.
 */

export type ToneVariant = "pill" | "chip" | "hued";

export interface ToneClasses {
  /** Border + tint + label colour for the badge body. */
  readonly wrapper: string;
  /**
   * The tone at full saturation, as a background: a pill's leading dot, a
   * progress bar's filled portion. Identical across variants -- it is the hue
   * itself, undiluted -- and unused by `chip`, which carries no dot.
   */
  readonly fill: string;
}

export const TONE_CLASSES: Record<ToneVariant, Record<Tone, ToneClasses>> = {
  pill: {
    neutral: {
      wrapper: "border-border bg-muted-foreground/10 text-muted-foreground",
      fill: "bg-muted-foreground",
    },
    info: { wrapper: "border-accent/40 bg-accent/10 text-foreground", fill: "bg-accent" },
    success: { wrapper: "border-success/40 bg-success/10 text-foreground", fill: "bg-success" },
    warning: { wrapper: "border-warning/50 bg-warning/10 text-foreground", fill: "bg-warning" },
    danger: { wrapper: "border-danger/40 bg-danger/10 text-foreground", fill: "bg-danger" },
  },
  chip: {
    neutral: {
      wrapper: "border-muted-foreground/30 bg-muted-foreground/10 text-foreground",
      fill: "bg-muted-foreground",
    },
    info: { wrapper: "border-accent/40 bg-accent/15 text-foreground", fill: "bg-accent" },
    success: { wrapper: "border-success/40 bg-success/15 text-foreground", fill: "bg-success" },
    warning: { wrapper: "border-warning/40 bg-warning/15 text-foreground", fill: "bg-warning" },
    danger: { wrapper: "border-danger/40 bg-danger/15 text-foreground", fill: "bg-danger" },
  },
  hued: {
    neutral: {
      wrapper: "border-muted-foreground/30 bg-muted-foreground/10 text-muted-foreground",
      fill: "bg-muted-foreground",
    },
    info: { wrapper: "border-accent/40 bg-accent/10 text-accent", fill: "bg-accent" },
    success: {
      wrapper: "border-success/40 bg-success/10 text-success-foreground",
      fill: "bg-success",
    },
    warning: {
      wrapper: "border-warning/40 bg-warning/10 text-warning-foreground",
      fill: "bg-warning",
    },
    danger: { wrapper: "border-danger/40 bg-danger/10 text-danger-foreground", fill: "bg-danger" },
  },
};

/**
 * Classes for `tone` in `variant`, falling back to `neutral` for a tone that
 * is not one (a value crossing a JS boundary untyped).
 */
export function toneClasses(tone: Tone, variant: ToneVariant = "pill"): ToneClasses {
  const table = TONE_CLASSES[variant] ?? TONE_CLASSES.pill;
  return table[tone] ?? table.neutral;
}
