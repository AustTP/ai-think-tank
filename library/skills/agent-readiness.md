# Agent-readiness audit

`audit_agent_readiness(url)` scans a public page for the five actions an agent
needs and returns a deterministic score.

## Checks
1. Plain-text offers: price, pricing, fees, plans.
2. Shipping and returns: delivery, returns, refunds, warranty.
3. Bot and agent policy: bot, crawler, agent, automation, terms, robots.
4. Agent channel: API, MCP, webhook, feed, developer integration.
5. Pay-rung approval: approval, authorization, confirmation, checkout.
6. Structured offer data: JSON-LD, schema.org, SKU, inventory, variants.

## Output
- `score` out of `maxScore`.
- `checks` with found signals.
- `missing` and `nextActions`.
- Ten buying questions to ask a human or a merchant.

## Use it
Run the audit on a target page before recommending a commerce integration.
The audit is a surface scan. It does not test checkout, payment, or legal
compliance. Pair it with a real `browser_step` test on an allowlisted page
when the player asks for a live check.
