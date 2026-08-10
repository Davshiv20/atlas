# DESIGN.md: Atlas product website

## Source
- Reference URL: https://langfuse.com/
- Capture date: 2026-08-07
- Evidence: `.firecrawl/langfuse-branding.json`, `.firecrawl/langfuse-screenshot.png`
- Use: public visual reference for Atlas's own brand. Reuse structural DNA only; do not copy Langfuse trademarks, imagery, or copy.

## Reference Screenshot
![Full-page screenshot of Langfuse](./.firecrawl/langfuse-screenshot.png)

Use the screenshot as a reference for density, hierarchy, sharp borders, technical diagrams, and restrained product presentation—not as a pixel-copy target.

## Design Summary
A light, technical, developer-facing product site. It uses an off-white neutral canvas, near-black ink, one electric-blue interaction color, and a fluorescent-yellow highlight for important phrases. Layouts feel like product documentation crossed with an engineering dashboard: square corners, hairline borders, dense labels, compact calls to action, and large product UI demonstrations.

## Design Tokens

### Colors
- Canvas: `#EDEDE8`
- Surface: `#F7F7F4`
- Raised surface: `#FFFFFF`
- Ink: `#101113`
- Muted ink: `#5B5D58`
- Hairline: `#CFCFC9`
- Blue action: `#1863DC`
- Yellow highlight: `#F4F542`
- Positive: `#17735A`
- Warning: `#A66510`
- Danger: `#A23D3D`

### Typography
- Display: Inter, 700–800, tight tracking, 64–72px desktop
- Body/UI: Inter, 400–600
- Technical labels/code: JetBrains Mono, 400–600
- No decorative serif. No italic headings.

### Spacing And Layout
- Base unit: 4px
- Content width: 1120–1180px
- Main reading column: 760–900px
- Section padding: 88–128px desktop, 56–72px mobile
- Grid gaps: mostly 1px borders; 16–24px internal padding
- Corners: 2–4px, not pill-heavy
- Shadows: almost none; borders provide depth

## Components
- Announcement bar: black strip with compact link
- Navigation: wordmark left, section links center, dark primary CTA right
- Hero: centered, 2–3 lines, key phrase highlighted yellow
- Buttons: square 2px corners, dark primary, bordered secondary, optional monospace shortcut chip
- Product frame: bordered UI demonstration with header rail, side navigation, and main canvas
- Feature grid: connected bordered cells, not floating cards
- Technical lists: small monospace labels and visible dividers
- Trust/status chips: compact, tinted, square-radius
- FAQ: native `<details>` rows with plus-like disclosure behavior
- Footer: dense multi-column index with hairline rules

## Page Patterns
1. Announcement + compact navigation
2. Hero with highlighted phrase and two CTAs
3. Large product UI mockup proving the concept
4. Continuous-loop explanation
5. Connected feature grid
6. Canonical-model / architecture section
7. Trust and validation section
8. Agent interfaces and adapters
9. Security and drift
10. Roadmap
11. Final highlighted CTA
12. FAQ and index footer

## Content Style
- Declarative, technical, and specific
- Short headings; highlight one important phrase
- Explain the continuous loop rather than presenting isolated features
- Avoid invented customer logos, testimonials, or usage metrics
- Use the user's real product claims and clearly label illustrative examples

## Agent Build Instructions
- Keep the page single-file and dependency-free except Google Fonts.
- Use no JavaScript; rely on CSS and native `<details>`.
- Build product mockups from CSS/HTML, never by copying Langfuse screenshots.
- Preserve Atlas content and positioning while adopting the reference's visual grammar.
- Keep all colors and fonts in tokens.
- Ensure 320/375/414/768px layouts have no horizontal scroll.
- Verify text contrast and reduced-motion behavior.

## Rerun Inputs
workflow: firecrawl-website-design-clone
source_url: https://langfuse.com/
target_stack: self-contained HTML
output: DESIGN.md + product-agent-semantic-context-layer.html
