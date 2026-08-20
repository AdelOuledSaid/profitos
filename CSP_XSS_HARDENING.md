# ProfitOS V1.3.4 — CSP / XSS Hardening

## Implemented

- JavaScript inline execution disabled by CSP (`script-src 'self'`).
- Inline event handlers disabled (`script-src-attr 'none'`).
- Service-worker bootstrap moved from `base.html` to `static/app.js`.
- `onchange="this.form.submit()"` handlers replaced by `data-auto-submit` and a delegated listener in `static/app.js`.
- External stylesheet elements are restricted to same-origin (`style-src-elem 'self'`).
- Existing HTML `style=...` attributes remain temporarily permitted through `style-src-attr 'unsafe-inline'` so the current UI is not broken. This is intentionally narrower than the previous global `style-src 'unsafe-inline'`.
- Email templates keep inline styles because email clients require them; they are not rendered as normal application pages.

## Important note about `dso_svg|safe`

The dashboard SVG is generated server-side by `sparkline_svg()` from numeric values and a server-side default color. It does not directly interpolate user-provided HTML. Keep this invariant if the chart helper is changed later.

## Production validation

After deployment:

1. `curl -I https://app.profitos.fr/login`
2. Confirm CSP contains `script-src 'self'` and `script-src-attr 'none'`.
3. Open Dashboard, Recover, Save, Grow, Team, Settings and Billing.
4. Confirm Team/Bid status auto-submit still works.
5. Browser DevTools Console should show no CSP violations during normal navigation.

## Next optional hardening

Move remaining web `style=...` attributes into CSS classes, then change `style-src-attr` from `'unsafe-inline'` to `'none'`.
