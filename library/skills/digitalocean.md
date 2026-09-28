# DigitalOcean

## Purpose
Reach for this before any real DigitalOcean work: creating, using, or cleaning up a
Droplet (or any other billed resource) through the vault-held API token. Read this
FIRST, every time -- the cost model here is easy to get wrong in a way that keeps
charging real money after you think you've stopped.

## Key facts
- **Powering off a Droplet does NOT stop billing.** CPU and RAM stay reserved on the
  hypervisor whether the Droplet is running or not, so a powered-off Droplet keeps
  accruing the exact same charge as a running one.
- **The only way to stop billing is to DESTROY the Droplet** (or snapshot it first,
  then delete the Droplet -- the snapshot itself has its own small storage cost, but
  it is far cheaper than a live Droplet). "Spin down" means destroy, not power off.
- Billing is **per-second**, minimum 60 seconds or $0.01 (whichever is higher).
  Bundled CPU plans cap usage at 672 hours (28 days)/month -- an always-on Droplet
  never exceeds its advertised monthly price, but a short-lived one is billed only
  for the seconds it actually existed.
- API calls themselves are free and unmetered; what costs money is the underlying
  infrastructure those calls create or leave running (Droplets, volumes, load
  balancers, reserved IPs, etc.).
- The vault-held token is a full-account bearer credential -- treat any capability
  built on it as high-risk by default. Separate read-only operations (list/get
  Droplets, check account status) from destructive ones (create, delete, power
  off/on) at the capability-handle level, per the existing confused-deputy vault
  pattern (see Gmail SMTP for the reference shape: encrypted credential + scoped,
  revocable handle, secret never leaves the server process).

## Policy for this village
0. **The master switch.** The village can only reach DigitalOcean at all when
   `SANDBOX_EXECUTION=digitalocean` in `.env` (default is `local`). While it
   reads `local`, every agent-facing entry point hard-refuses -- no capability
   handle can be minted or resolved, the live balance check never runs, and the
   sandbox stays on local Docker. Do NOT flip the switch until a real remote
   execution backend is provisioned (the current `digitalocean` sandbox branch
   fails closed with an explicit error rather than silently running anywhere).
   This is a deliberate, paid decision; local Docker is the safe default.
1. Never create a real Droplet (or any other billed resource) without an explicit,
   logged, player-authorized capability grant for that specific action.
2. The moment the work that needed the Droplet is done -- code pushed, tests run,
   whatever the task was -- **destroy it immediately.** Do not leave it "just in
   case," do not power it off and consider the job done. Powered-off is still
   billing.
3. If a Droplet needs to survive past one task (a genuinely long-running service),
   that's a deliberate, separately-approved decision, not a default.
4. Log every create and every destroy through the normal action log, same as any
   other real-world-affecting action, so a Droplet nobody destroyed is discoverable
   before it becomes a surprise bill.

## Sources
- https://docs.digitalocean.com/products/droplets/details/pricing/ (per-second
  billing, the 672-hour monthly cap, powered-off Droplets still billed)
- https://www.digitalocean.com/community/questions/powered-off-droplets-bill
  (community confirmation: destroy or snapshot+delete to actually stop billing)
- Verified live 2026-09-24 by the developer (not by an agent spike -- a spike has
  no real internet access and an earlier ungrounded spike on this same topic got a
  comparable fact wrong for a different service, Mullvad's token TTL).

## Lessons learned
- (2026-09-24) An early agent "spike" on DigitalOcean correctly guessed the general
  shape (pay for infra, not calls) but never surfaced the powered-off-still-bills
  trap at all -- the single most costly thing to get wrong here. Spikes are a single
  ungrounded model call with no browsing; don't trust one for anything with a real
  dollar cost without verifying it first, the way this file was.
