# Design — Atlas console

The locked design system for the review workbench. Every console redesign reads
this file before emitting code; extend or amend it rather than regenerating a
system per screen.

Scoped to `console/` deliberately. The root [`DESIGN.md`](../DESIGN.md) governs
the product report artifact and states at its end that it does not govern the
console. Neither file overrides the other.

## Genre

modern-minimal. An instrument for a working reviewer, not a marketing surface.

## Macrostructure family

- **App pages** — Workbench: a persistent left ledger, one working surface, no
  page-level hero, no enrichment of any kind.
- **Setup pages** (Connections) — centred single column, dialogs for
  configuration rather than full-page forms.

There are no marketing or content pages in this package.

## Theme

Preserved from the existing token block in `src/index.css` — not re-picked. The
palette was already coherent; how colour was *used* was the defect.

- Surfaces step in real increments: `--color-canvas` → `surface` → `raised` →
  `sunken`. Depth comes from a step plus a hairline, never a shadow.
- One saturated accent, `--color-cta` `#3b4fe4`, reserved for actions.
- Status hues (`teal` / `amber` / `red` / `violet`) report state and never drive
  an action. The accent never reports a state.

## Typography

- Display and labels: Geist Mono 600, tracking `-0.02em`
- Body prose: Inter 400
- Identifiers: Geist Mono via `.ident`

Mono leads on purpose: nearly every noun on these screens is an identifier.

## Colour discipline — the rule this redesign exists to enforce

**Colour marks; it does not fill.** Risk is carried by a 3px edge marker and a
single chip. Saturated backgrounds never wash a whole row, cell, or panel.

The prior sheet tinted the entire row for any flagged field. On a table with
twenty flagged fields the screen became a solid amber panel, at which point the
colour located nothing — it was everywhere. Keep saturated colour under roughly
5% of any viewport.

**One state, one encoding.** A row's condition is said once. The prior sheet
said it three times — row tint, a chip, and a prose reason — and the chip
printed the internal enum (`yellow`, `red`, `quiet`), which names a colour
rather than a condition and conveys nothing to a reader who cannot see it.

## The four row states

| State | Meaning | Mark | Action offered |
|---|---|---|---|
| `red` — Conflict | Evidence contradicts the claim | red edge + filled chip | Confirm / Edit / Reject |
| `yellow` — Needs you | Consequential claim awaiting a decision | amber edge + filled chip | Confirm / Edit / Reject |
| `ungenerated` — Not generated | **No claim was ever made for this field** | grey edge + dashed outline chip | none — regenerate the table |
| `none` — Settled | Confirmed, accepted, or routine | no edge, teal dot | revise on hover |

`ungenerated` is not a degree of risk and must never be styled as one. It is the
absence of a claim. Conflating it with `yellow` is what made a fully reviewed
workspace still read as unfinished work — reviewers concluded their approvals
had not saved. It stays visible (unknown meaning is never hidden — product
invariant 5) but it is not a task, carries no button, and is counted separately
from review work everywhere it appears.

## Density

- Row height is set by content, not padding. Vertical padding stays at 10px.
- Disclosures render only where there is something to disclose.
- Secondary actions reveal on `group-hover` / `group-focus-within` and remain in
  the DOM and the tab order.
- The sheet must fit beside the 268px ledger without horizontal scroll at
  1280px. Five columns is the ceiling.

## Motion

Motion-cut project — no animation library, and none should be added.

- Easing: `--ease-out` only.
- Permitted: `background-color`, `opacity`, and the button press `scale(0.97)`.
- Focus rings never animate; they appear instantly.
- `prefers-reduced-motion` collapses all transitions (already in `index.css`).

## Microinteraction stance

- Silent success. A row leaving the queue is the confirmation; no toasts.
- Errors render in place, next to the control that failed.
- Destructive actions read quiet until hovered.

## CTA voice

All buttons go through `src/components/ui/Button.tsx`. Variants: `primary`
(accent fill) · `secondary` (outline) · `ghost` · `danger` (quiet until hover).
Sizes `sm` (h-7) and `md` (h-34px). Height is fixed per size so a row of buttons
aligns regardless of label length.

Never write a bespoke button class string. That duplication is what `Button`
exists to end, and it had already crept back into the review sheet once.

## What every console screen MUST share

- The token block in `src/index.css` — no local colour or font values.
- The accent reserved for actions; status hues reserved for state.
- Mono for identifiers, Inter for prose.
- `Button` for every pressable control.
- The four row states and their marks.

## What screens MAY differ on

- Layout within the Workbench family (ledger + sheet, ledger + pane, centred).
- Which columns a sheet shows.
- Whether a ledger is present at all (Connections has none).

## Per-page allowances

App pages MUST NOT use hero enrichment, illustration, or decorative gradient.
Function carries these screens.
