# Result Card Overflow Fix Design

## Problem

Production job descriptions stop at `72ch`, leaving a large unused area on the right side of otherwise full-width result cards. Some source descriptions also contain long uninterrupted strings such as repeated encoded dash text, which can extend beyond the card boundary.

## Approved behavior

- Job descriptions use the full content width available inside each result card.
- Ordinary Chinese and Latin text keeps its natural wrapping behavior.
- A continuous string wider than the card may break at any point necessary to remain inside the card.
- The frontend preserves the API response text exactly; it does not decode, remove, or otherwise rewrite job content.
- Search controls, result fields, card spacing, and mobile layout remain unchanged.

## Implementation boundary

The fix is limited to the result-description presentation rules in `apps/web/src/routes/+page.svelte`. Remove the `72ch` maximum width and add emergency wrapping to `.description`. Add source-level regression assertions in the existing branding/layout test so both requirements remain protected.

## Verification

- First demonstrate that the new assertions fail against the current CSS.
- Run the focused Web test after the CSS fix.
- Run formatting, lint, Svelte diagnostics, the complete Web test suite, and the production build.
- Confirm the result description has no fixed reading-width cap and contains `overflow-wrap: anywhere`.
