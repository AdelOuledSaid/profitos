# ProfitOS V1.4 — Commercial Production

## Stripe
Create three recurring monthly Prices in Stripe and set on Render:
- STRIPE_SECRET_KEY
- STRIPE_PUBLISHABLE_KEY
- STRIPE_WEBHOOK_SECRET
- STRIPE_PRICE_STARTER_ID
- STRIPE_PRICE_PRO_ID
- STRIPE_PRICE_BUSINESS_ID

Webhook endpoint: `https://profitos.onrender.com/billing/webhook`
Subscribe at least to `checkout.session.completed`, `customer.subscription.updated`, `customer.subscription.deleted`.

The UI prices are currently Starter 49 EUR, Pro 99 EUR, Business 249 EUR. Create matching Stripe prices or edit the UI before launch.

## Email
Set SMTP_HOST, SMTP_PORT=587, SMTP_USER, SMTP_PASSWORD, SMTP_FROM. Verification, password reset, invitations and weekly reports then send real email instead of dry-run.

## Release checklist
1. Deploy and verify /healthz and /readyz.
2. Sign up with a new email and confirm the verification message arrives.
3. Test forgot-password.
4. Use Stripe test mode to buy each plan.
5. Confirm organization plan/status changes after webhook.
6. Open Stripe Customer Portal and cancel a test subscription.
7. Never commit Stripe/SMTP secrets to Git.
