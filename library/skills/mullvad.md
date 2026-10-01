# Mullvad VPN

## Purpose
Reach for this whenever a task needs a foreign-country vantage point: a
`browse_page` call with a `country` param fetches a page through a real Mullvad
WireGuard exit in that country (e.g. content only visible to visitors in the
UK or Japan). Enabling `MULLVAD_COUNTRY_ALLOWLIST` in .env turns the feature on.

## Key facts
- The durable credential is the **account number itself** (`MULLVAD_ACCOUNT_NUMBER`
  in .env), not a per-call API key. `_mullvad_ensure_logged_in_sync` auto-logs
  the CLI in from it, so it doesn't depend on an interactive GUI session.
- **There is no per-request scoping like a bearer token**: `mullvad connect`
  changes the WHOLE MACHINE's default route, not just one process's. Every
  caller goes through `_MULLVAD_LOCK` and the connection is torn back down in a
  `finally` -- a crashed caller would otherwise leave the entire host routed
  through a VPN exit indefinitely. Treat connect as real shared global state.
- Host prerequisite (DONE on this machine 2026-09-30): split tunneling is ON
  and the server's own interpreter is excluded so a VPN connect mid-request
  can't tunnel the server's self-loopback calls:
  `mullvad split-tunnel set on` and
  `mullvad split-tunnel app add /opt/anaconda3/bin/python3.11`.
- Country allowlist (player's call, .env): `MULLVAD_COUNTRY_ALLOWLIST=de,jp,br,uk`.
  A `country` not on the list is refused before any connect attempt. An empty
  list = feature off.
- CLI commands that are real and used by the code:
  - `mullvad status` -- "Connected"/"Disconnected" + visible location.
  - `mullvad account get` / `account login <number>` -- verify/login.
  - `mullvad relay set location <country>` + `mullvad connect` -- pick exit,
    connect; `mullvad connect -w` waits until actually connected.
  - `mullvad disconnect`.
  - `mullvad split-tunnel set on/off` + `split-tunnel app add/remove <path>`.
- Exit verification: `GET https://am.i.mullvad.net/json` returns the real exit
  IP + `mullvad_exit_ip: true/false` (this is Mullvad's connection-check host).
- The Mullvad REST API (`api.mullvad.net`, e.g. `POST /auth/v1/token`,
  `/accounts/v1/...`) is for **account/device management and the relay list
  only -- it CANNOT proxy a fetch through a country exit.** A foreign-country
  fetch always requires an actual tunnel.
- Relay list for building raw WireGuard configs (if the CLI were ever
  unavailable): `GET https://api.mullvad.net/www/relays/all/` (per-relay
  `public_key`, `ipv4_addr_in`, hostname).

## Budget reality
- A Mullvad account number is subscription-based, not per-call. Check the
  current account's expiry with `mullvad account get` -- a lapsed account makes
  every country-param browse fail at connect time.
- This machine's account (`7780513574581539`) was verified logged-in and
  **expires 2026-10-03** (checked 2026-09-30) -- top it up before relying on
  VPN-routed browsing. A live connect attempt on 2026-09-30 also failed with
  `Error: Failed to connect` (daemon/system state, not a config error) -- the
  code fails closed with a clear reason rather than fetching over the old
  route, but the host-level daemon needs to actually be able to connect.

## Policy for this think tank
1. Only use a `country` on `browse_page` when the page genuinely needs a
   foreign vantage point; an ordinary fetch is faster and needs no VPN.
2. Never retry a refused country with a different one -- a refusal means the
   player hasn't allowlisted it.
3. The connect is host-global and always torn down in a `finally`; never
   leave a VPN connected after the fetch that needed it.

## Sources
- https://mullvad.net/en/help/how-use-mullvad-cli (CLI subcommands)
- https://github.com/mullvad/mullvadvpn-app (mullvad-cli source, split-tunnel
  commands, RPC socket /var/run/mullvad-vpn)
- https://am.i.mullvad.net/json (live exit-IP check, verified working)
- `mullvad split-tunnel get` / `account get` on this host (verified live)

## Lessons learned
- An early, ungrounded agent "spike" got a Mullvad fact wrong (token TTL) the
  same way it got DigitalOcean's powered-off billing wrong -- verify against
  the live CLI/docs before writing policy, never trust a spike's guess.
- The classic failure mode for VPN-routed work is NOT a wrong exit country; it
  is forgetting the connect is host-wide and leaving it connected. The code's
  lock + finally-disconnect is the protection; never bypass it.
