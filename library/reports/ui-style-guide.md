# AI Think Tank UI style guide (derived from the operator's saved design references)

**Provenance.** Derived 2026-11-07 by a vision-model pass
(`gemini-flash-lite-latest`, Gemini API) over the 8 most recent images the
operator saved to iCloud Photos (Nov 4-7 2026; two Nov-6 16:08 saves were
iCloud-only placeholders with no local bytes, substituted by the
next-most-recent locally available saves). Only what is visible in the
images informed this guide. This is the tank's UI contract until the
operator revises it.

## 1. Color palette

| Role | Hex (estimate) |
|---|---|
| Background | `#0a0b0e` (deep midnight / space black) |
| Surface / card | `#12141a` (dark charcoal) |
| Primary accent | `#10b981` / `#00ffcc` (electric cyan / mint) |
| Secondary accent | `#f59e0b` (warm amber / gold) |
| Tertiary accent | `#a78bfa` (soft violet, headers/tags/section labels) |
| Text primary | `#e2e8f0` (off-white / crisp silver) |
| Text muted | `#94a3b8` / `#64748b` (cool slate) |
| Borders | `#1e293b` (subtle dark blue-gray) |
| Success / warn / error | `#22c55e` / `#eab308` / `#ef4444` |

## 2. Typography

- Monospace (JetBrains Mono / Fira Code / system mono) for code views and
  logs; clean sans (Inter) for headers.
- Weights: 400 body/logs, 500 interactive labels, 700-800 metrics and
  primary headers.
- Hierarchy: H1 24-28px bold (uppercase for data headers); card header
  16-18px semibold; body/code 13-14px at 1.5 line-height; metadata/badges
  11-12px mono.

## 3. Layout and spacing

- Dense, data-heavy multi-column grid (bento / kanban swimlanes).
- Card-based modular layout floating on a full-bleed dark background;
  crisp structural accent lines (solid primary-accent left border on
  active cards).
- Padding on an 8px base grid: 8px inner gaps, 12-16px container padding.
- Radii: 4px inner badges/code blocks, 8-12px cards and modals.

## 4. Iconography and imagery

- Minimalist line-art/glyph icons (terminal prompts `>_`, close `×`,
  plus `+`, keyboard glyphs).
- Zero decorative fluff: data visualizations, structured tables, and code
  snippets are the primary visual elements.

## 5. Interaction cues

- Flat or subtly bordered dark buttons; high-contrast text; glowing
  accent hover states; mint/cyan fills for primary triggers.
- Active/thinking states highlighted in amber/gold or pulsing accent
  borders; disabled states in muted slate at ~50% opacity.
- Pill-shaped categorical chips with subtle fills; status badges and
  telemetry pills with glowing indicators.

## 6. Overall mood

Autonomous, tactical, high-velocity, analytical, immersive.
