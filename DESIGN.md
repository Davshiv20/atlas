# Atlas product-report design

## Purpose

This document defines the visual system for
`product-agent-semantic-context-layer.html`. The artifact is a structured product report
for reviewers, not a marketing landing page and not the Atlas application UI.

The report must let a product or engineering reviewer understand the thesis, ratify the
important decisions, inspect implementation details, and navigate a long specification
without reading it linearly.

## Visual reference

- Reference URL: https://langfuse.com/
- Capture date: 2026-08-07
- Original evidence: `.firecrawl/langfuse-branding.json` and
  `.firecrawl/langfuse-screenshot.png` when locally available
- Rule: reuse structural design DNA only; do not copy Langfuse trademarks, imagery,
  layout verbatim, or product copy

Borrow the reference's technical density, off-white palette, sharp borders, compact
labels, and dashboard-like widgets. Apply them to a document structure rather than a
website structure.

## Design character

A technical product specification presented like an internal research report:

- off-white paper and near-black ink;
- electric blue for navigation and structural emphasis;
- fluorescent yellow for one consequential phrase or review point;
- dense tables, diagrams, code blocks, and status chips;
- sharp corners and hairline borders;
- minimal shadow;
- restrained typography;
- no marketing hero, announcement bar, CTA section, testimonial strip, pricing pattern,
  or promotional footer.

## Tokens

### Color

- Canvas: `#EDEDE8`
- Paper: `#F7F7F4`
- Raised surface: `#FFFFFF`
- Ink: `#101113`
- Secondary ink: `#3D3F3B`
- Muted ink: `#62645F`
- Faint ink: `#92948D`
- Hairline: `#CFCFC9`
- Strong hairline: `#AAA9A2`
- Blue: `#1863DC`
- Dark blue: `#104EAD`
- Blue tint: `#E5EDFB`
- Yellow highlight: `#F4F542`
- Positive: `#17735A`
- Warning: `#8B5913`
- Danger: `#9F3636`

### Typography

- Headings and body: Inter, 400–800
- Technical labels and code: JetBrains Mono, 400–600
- Document title: 38–62px, tight tracking
- Section heading: approximately 30px desktop
- Body: 14px with generous line height
- Labels and metadata: 8–10px monospace
- No decorative serif and no italic display headings

### Geometry

- Desktop sidebar: approximately 248px
- Main reading measure: approximately 940px
- Section spacing: 40–54px
- Base spacing unit: 4px
- Borders: 1px, with a heavier report-cover rule
- Corners: 2–4px
- Avoid floating card shadows; use rules and surface contrast

## Document structure

1. Persistent contents sidebar
2. Report cover with document type, title, summary, and metadata strip
3. Executive summary
4. Decisions or assumptions for reviewers to ratify
5. Numbered product sections in the same order as the specification
6. Compact reference tables and diagrams before explanatory prose
7. Collapsible appendices for lower-priority detail
8. Small document footer with title and date

The page should feel printable and coherent without navigation chrome.

## Component vocabulary

### Report cover

A strong top rule, compact eyebrow, document title, one-paragraph summary, and metadata
strip. It introduces a report; it must not behave like a conversion hero.

### Contents sidebar

Numbered, compact, and sticky on desktop. On smaller screens it becomes a horizontally
scrollable contents rail. It contains no account, pricing, signup, or promotional links.

### Section header

A two-column row with monospace section number and title. Optional one-sentence lede
explains what the section decides.

### Callout

Use at most four semantic styles:

- dark: core thesis or north star;
- blue: architectural rule or example objective;
- yellow: reviewer decision or consequential warning;
- neutral: supporting context.

Each callout communicates one concept.

### Data widgets

Use bordered, connected cells for:

- compression funnels;
- process loops;
- three-layer architecture;
- typed-check flow;
- review-priority formula;
- context compiler pipeline;
- evaluation conditions;
- roadmap phases.

Widgets support the report. They must not turn sections into feature marketing cards.

### Compact table

Use for trust states, workbench surfaces, evaluation conditions, success criteria, and
product principles. Header rows are near-black with small monospace labels.

### Code card

Near-black panel containing JSON, interfaces, or compiled context. Syntax color is
restrained and accessible.

### Details disclosure

Use native `<details>/<summary>` for appendices and explanations that would otherwise
interrupt the main review path. No JavaScript is required.

## Content rules

- Preserve the latest claims in `PRODUCT.md`; do not invent product status or customer
  evidence.
- Distinguish current implementation from target product behavior.
- Lead with decisions, tables, or diagrams rather than prose walls.
- Keep paragraphs short and place lower-priority detail behind disclosures.
- Use illustrative quantities only when the product specification labels them as an
  example or target.
- Avoid generic marketing language, social proof, and calls to action.

## Responsive and print behavior

- No horizontal page overflow at 320, 375, 414, or 768px.
- Sidebar becomes a compact top contents rail below desktop width.
- Multi-column cards and flows collapse without changing reading order.
- Wide tables may scroll inside their own container.
- Print mode removes navigation and canvas treatment, uses the full page width, and
  avoids breaking small widgets where practical.
- Respect `prefers-reduced-motion`; the report should not depend on animation.

## Build constraints

- One self-contained HTML file.
- Inline CSS; no external JavaScript.
- Google Fonts may be loaded from the CDN.
- No copied screenshots or third-party product imagery in the final report.
- All colors and typography come from tokens.
- Native HTML semantics and visible focus states are required.

## Relationship to the console

This design system does not govern `console/`. The application workbench has its own
interaction and product requirements. Shared brand tokens are acceptable, but report
components such as the cover and document sidebar should not be copied into the product
UI without a separate design decision.
