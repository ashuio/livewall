/**
 * The accent-colour vocabulary, whitelisted at the one place it is interpolated.
 *
 * A block's `color` reaches the DOM as a CSS `var(--accent-<name>)` token
 * (`components.tsx`). Interpolating an arbitrary string there is an injection
 * sink: `color: "mint) url(//evil"` yields the declaration
 * `background: var(--accent-mint) url(//evil)` — valid CSS, since the
 * `background` shorthand accepts a colour and an image in either order — and
 * every viewer's browser then fetches the attacker's URL. The backend contract
 * refuses any non-accent `color`, and this whitelist is the client's
 * defence-in-depth for boards stored before that fix: anything outside the
 * vocabulary degrades to `''`, which the callers fall back to violet on.
 *
 * Must equal `contract.ACCENTS` (backend/contract.py) and the `--accent-*`
 * tokens in `components.css` — `backend/tests/vocab_parity_test.py` keeps the
 * three in step.
 */
export const ACCENTS = [
  'violet',
  'pink',
  'lime',
  'mint',
  'muted',
  'ok',
  'warn',
  'error',
] as const;

export function accentName(value: unknown): string {
  const v = typeof value === 'string' ? value.toLowerCase() : '';
  return (ACCENTS as readonly string[]).includes(v) ? v : '';
}
