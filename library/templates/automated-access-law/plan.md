# Automated Access Law -- Research Plan

Status: DRAFT v0.1
Owner: Pieter res + think-tank review
Ends: end of Q4 2026 (target, adjustable)

## Objective

Establish what law and enforceable norms actually govern automated access to
websites, APIs, and scraped data (crawlers, bots, CAPTCHAs, bot-management
and circumvention, ToS, authentication boundaries), then convert the findings
into operational guardrails the think tank enforces mechanically before any
collection action.

## Research questions

1. Where is the lawful/lawful boundary for automated access: what turns
   "reading a page" into intrusion, breach, or a statutory violation?
2. Which boundaries are (a) statutory, (b) contractual (ToS/API terms), or
   (c) technical-norm (robots.txt, rate limits, pay-per-crawl, consent
   signals, platform crawler policy)?
3. Per context (public pull, pull behind auth, CAPTCHA solve, fingerprint
   spoof, bulk scale, biometric identifiers), what liability attaches and
   where?
4. For every blocked context, what is the legitimate acquisition path
   (API, license, partnership, permission letter)?
5. Which of those boundaries can be checked mechanically at runtime, and how
   should the check return ALLOW / ESCALATE / BLOCK before any request?

## Scope

- **Jurisdictions:** United States (federal + California, New York, New
  Jersey snapshots), European Union (GDPR, ePrivacy, Database Directive,
  Data Act, AI Act), United Kingdom (UK GDPR, DPA 2018, Computer Misuse Act,
  ICO posture). Watchlist: Canada, Australia, India, China.
- **Layers:** settled doctrine; active litigation; pending legislation;
  platform policy and enforcement practice.
- **Out of scope (explicit):** designing, building, testing, or advising on
  circumvention. This plan produces knowledge and guardrails, not capability.

## Evidence plan

**Layer 1: Statutes.** CFAA 18 USC 1030 (including the access /
exceeds-authorized-access split and scope for automated collection), state
CFAA equivalents, DMCA 1201 and the Copyright Office triennial exemption
cycle (security research, accessibility research), EU instruments noted
above, UK CMA 1990 and the Law Commission reform track.

**Layer 2: Cases.** Anchor set to verify, then finalize:

| Authority | Question it answers | Verify |
|---|---|---|
| hiQ v. LinkedIn (9th Cir.) | Public-page scraping vs CFAA; where auth begins | Yes |
| Meta v. Bright Data (N.D. Cal. 2024) | Contract/ToS theory vs scraped pages: boundary and surviving claims | Yes |
| Van Buren v. U.S. (S. Ct. 2021) | Excludes ToS-breach reading of "exceeds authorized access" | Yes |
| Facebook v. Power Ventures (9th Cir. 2016) | Bulk access after notice/cease-and-desist | Yes |
| eBay v. Bidder's Edge (N.D. Cal. 2000) | Trespass to chattels toll for server-load bots | Yes |
| Sandvig v. Barr (D.D.C.) | Research-driven scraping, First Amendment shade | Yes |
| Ryanair v. Booking.com (recent, appeal posture unclear) | Crawl-and-resell in OTA market | Verify status |
| CJEU C-30/14 Ryanair | Screen scraping under Unfair Commercial Practices rules | Yes |
| Clearview cluster (ICO, CNIL, Dutch DPA, Italian Garante) | Scraping + biometric identifiers enforcement hotspot | Yes |
| NYT v. OpenAI; Thomson v. Ross (if needed) | Use-of-content stakes shaping future rules | Optional |

**Layer 3: Regulator guidance.** EDPB legitimate-interest guidance on
processing public-source personal data and the 2024/2025 follow-ons; ICO and
CNIL guidance; FTC comparatively (dark patterns angle only).

**Layer 4: Platform policy.** ToS/ToS-of-service of the high-volume targets
this think tank might ever touch (LinkedIn, Reddit, X, search/API platforms),
plus published enforcement signals such as crawler-policy blocks or
pay-per-crawl offerings. Recorded as norm layer, not law.

**Secondary.** Law-review syntheses and bar publications for the moving
pieces (AI training data, anti-circumvention, authentication boundaries).

## Workstreams

- **W1 Statute map.** Per jurisdiction: instrument, scope, elements,
  penalties, and where automated access falls inside it.
- **W2 Case synthesis.** Align the anchor cases into a boundary map:
  identity, notice, auth state, volume, method.
- **W3 Guidance layer.** Convert regulator guidance into concrete
  do/don't rules.
- **W4 Policy layer.** Same as W3 for platform terms; mark each item as law
  vs contract vs norm.
- **W5 Bridge to guardrails.** Turn findings into runtime checks and triage
  hooks.

## Deliverables

- **D1 Inventory matrix.** jurisdiction × boundary × boundary-type ×
  authority-link. The structural base for everything else.
- **D2 Memo.** 25–35 pages, structured per the research questions above,
  each section with (i) holding or rule, (ii) authority, (iii) contested vs
  settled marker, (iv) practical rule in plain language.
- **D3 Runtime gate spec.** Pre-collection checkpoint returning
  ALLOW / ESCALATE / BLOCK on the measurable boundary set (auth state,
  robots.txt result, rate-limit posture, ToS flags), specced to obligation
  ledger format, wired to bridge into triage-gate.md's never-promote list.
- **D4 Source pack.** Curated folder; every memo claim cites a document in
  it.

## Sequence (rough)

1. Corpus assembly + W1 start (1 wk)
2. W2/W3/W4 mining and synthesis (2–3 wk)
3. D2 draft (1 wk)
4. D3/D4 (1 wk)
5. Adversarial review pass: independent check that every guard hook traces to
   an authority and that contested rulings are not stated as settled (1 wk)
6. Cut v1; quarterly refresh check goes on the cadence.

## Boundaries

- Research, not legal advice; final posture decisions go to licensed counsel
  per jurisdiction.
- No live probing of third-party systems, no scrapes during research, and no
  testing of circumvention paths. Anything that looks operational gets
  declined and logged per the triage gate.

## Risks & mitigations

- **Law moves fast on scraping/AI training.** Every claim carries an
  as-of date; refresh on a quarterly cadence.
- **Settled vs contested drift.** Two-column output: what courts have held
  vs what parties argue; the memo never blends them.
- **Scope creep.** Any question that drifts toward "how would one get around
  the control" is out of scope by definition and gets declined.

## Success criteria

- Every guard hook traces to ≥ 1 primary authority in the source pack.
- 100 % of bypass-adjacent categories named in the intake have a mapped,
  legitimate alternative (or an explicit "no legitimate path" finding).
- Memo passes the adversarial review round with zero unsupported claims.