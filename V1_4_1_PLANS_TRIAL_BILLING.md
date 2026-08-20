# ProfitOS V1.4.1 — Plans, Trial & Billing Flow

Paid plans: Starter 49 EUR/month, Pro 99 EUR/month, Business 249 EUR/month.

Required production variables:
- STRIPE_SECRET_KEY
- STRIPE_WEBHOOK_SECRET
- STRIPE_PRICE_STARTER_ID
- STRIPE_PRICE_PRO_ID
- STRIPE_PRICE_BUSINESS_ID
- APP_BASE_URL=https://app.profitos.fr

Webhook: https://app.profitos.fr/billing/webhook

Subscribe to: checkout.session.completed, customer.subscription.created, customer.subscription.updated, customer.subscription.deleted, invoice.payment_failed, invoice.payment_succeeded.

Security rules: plan keys are server-whitelisted; browser success never activates access; webhook signature is mandatory; webhook event IDs are stored for idempotency; existing paid subscriptions are routed to Customer Portal instead of creating a duplicate subscription.
