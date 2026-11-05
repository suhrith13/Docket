# Seed case law: the record from the first two trials (cases 1 and 2 on docket-demo-shop).
# In a live room this lives in the Switch room document "docket-decisions", kept by the prosecutor.

Docket decisions

Case 1: PR #1, pr-1-order-history
Judge: user (admin). Fix commit: ac7c53e.

Charge 1: sustained, fixed. order_history treated page numbers as starting at 0 (start = page * per_page), so page 1 skipped the newest orders.
Rule: page numbers start at 1, and paging must go through paginate(), which rejects page < 1.

Charge 2: sustained, fixed. apply_loyalty_discount had no cap, so totals went negative after 20 years.
Rule: discounts must go through apply_discount, which keeps them within 0–100%.
Product decision: keep the loyalty cap at 100% for now (orders are free from 20 years on).

Outcome: PR #1 may be merged.

Case 2: PR #2, pr-2-order-search
Judge: user (admin). Fix commit: b2e0893.

Charge 1: sustained, fixed, following case 1, charge 1. search_orders used start = page * per_page, so page 1 skipped the first results.
Rule: as case 1, charge 1.

Charge 2: sustained, fixed. The PR's only test passed on an empty page, so it could never fail.
Rule: a test must check the expected results, not just pass on an empty page.

Outcome: PR #2 may be merged.
