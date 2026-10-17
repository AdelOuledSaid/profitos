# ProfitOS V1.7.7 — AI CFO Real Business Constraints

The AI CFO Planner now optimizes only across business levers explicitly authorized by the user.

- Optional supplier/partner payment delay with a maximum delay.
- Optional installment payments with a maximum number of installments.
- Optional external financing with an explicit maximum accepted amount.
- Hiring decisions are never presented as installment-payment plans.
- Conditional plans requiring delay or installments are explicitly marked as conditions to confirm with the supplier or partner.
- When no allowed combination preserves the target reserve, ProfitOS states that no sustainable solution was found instead of inventing a negotiable plan.
- Existing before/after, top-3, no-financing and consistency-guard calculations are retained.
