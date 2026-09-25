# AI Village — Design & Execution Plan

## 0. Why this document exists

This is the third attempt at this project. The first two didn't fail on ambition —
they failed on **art quality**, twice, in two different ways:

1. **bullpen** (`~/Desktop/bullpen`, FastAPI + websocket + typed agent orchestrator) —
   the backend logic (agent state machine, model routing, meetings, escalation) got
   furthest of the three, but never got a real visual layer.
2. **ai-village.py** (`~/Desktop/ai-village.py`, Streamlit) — built a full asset
   pipeline (OpenRouter/Gemini-generated per-pose character PNGs, composed sheets,
   room scenes) that *worked end to end* and cost real money to generate — but the
   sprites never actually rendered in the live map (fell back to emoji circles), and
   the underlying art style (photoreal/cartoon diffusion output) likely wouldn't have
   matched the target look even if it had rendered.
3. **The Artifact/HTML attempt** (last night, hand-authored SVG town, no saved repo) —
   went through several honest rounds of feedback: "looks too modern," "I wanted pixel
   art," "more SNES/Zelda," a specific real tileset was named
   (`casper-gaming.itch.io/village-maps-mz`), and finally "this still looks so bad, it
   lacks the depth of detail" — ending with the session closed unresolved.

**The pattern across all three:** the parts that are *hard to fake* — real pixel-art
tiles and sprites with actual depth, shading, and consistency — never got solved. The
parts that are comparatively easy — backend state, orchestration, tool-gating — got
solved twice over. This plan inverts that order: **prove the art first**, with a tool
built for the job (PixelLab.ai), before investing in anything else.

## 1. Vision

A single-screen, top-down pixel-art village (SNES/Stardew-Valley/Pokemon Gameboy/Habbo Hotel/RPG register, not
Gameboy-green, not flat SVG) that your AI agents actually live in: they walk between
rooms, work when there's work, sleep when there isn't, and you can drop in — as a
playable character or as a message to the one agent who's always listening — to hand
out tasks, call a meeting, or just watch. The backend is authoritative and persistent:
closing the browser never resets anything, and every action is logged so the village's
history can be replayed.

## 2. Locked requirements (from tonight + last night, verbatim intent preserved)

- **Single screen, top-down, pixel art.** No sidescrolling. Explicit prior rejections:
  "still looks very modern," wanted "pixel art style," referenced SNES-era Zelda and a
  real RPG Maker tileset pack by name. This is the hard constraint the last attempt
  never met.
- **Walkable, enterable buildings.** Characters (agents *and* you) walk around; at
  least ~10 enterable structures.
- **Backend-authoritative state.** A refresh must never affect agent state. State
  lives server-side; the frontend is a view that reconnects.
- **Replayable.** Log state transitions (event log), not just current snapshots, so
  past actions can be replayed later.
- **Room-gated tools.** An agent can only invoke certain tools while physically in the
  matching room — explicitly approved by you as a core mechanic, both last night and
  tonight.
- **Sleep / wake / call.** Idle agents go dormant (cheaper, or literally paused); an
  active agent — or you — can "call" a sleeping one back to work. **Has a physical
  anchor now (Phase 20):** a red telephone booth in the village, replacing the
  bench by Press Office — walk up to it to open a call dialog. Shell only for
  now (no roster to actually ring), see §7.
- **Out-of-band admin channel.** You can message one always-listening agent (the
  "administrator") without opening the village UI at all, and that message wakes it
  and puts it to work. Mechanically this is a message queue the backend drains
  regardless of whether any browser tab is open — every "sleeping agent" is really
  just a subscriber to a queue the admin already listens to full-time.
- **Model tiering.** Agents should be able to drop to a cheaper model when there's
  nothing demanding for them to do (direct carry-over from bullpen's cheap/mid/premium
  OpenRouter routing, which already worked).
- **OpenRouter for agent reasoning.** Established and working across all three prior
  attempts — no change here.

### Open question: theme

Memory from a week ago has "IT Crowd theme" locked in. But the most recent explicit
instruction, last night, was **"We are leaning too hard into the IT Crowd theme. Let's
lose it,"** immediately followed by pointing at the reference screenshot (the
"Complete Workflow HQ"-style village you shared again today) as the actual target.
I'm treating that as the standing decision — this plan defaults to a **theme-flexible
roster styled directly off the reference village**, not a Reynholm-Industries
replica — but say the word if you want IT Crowd back in.

## 3. World design

**Navigation:** one persistent top-down map, zoom in/out (whole-village ↔
room-level), plus your screenshot's hotkey model — cheap overlays instead of new
screens: chatter (nearby dialogue), who's who (roster), what's on (activity feed),
morale, zoom.

**Rooms (from your list + additions, grouped by function):**

| Category | Rooms | Where it actually lives (resolved) |
|---|---|---|
| Work | ≥2 general work rooms, computer lab (internet access) | **Resolved (Phase 24), built (Phase 24 art):** collapsed to one shared interior, the Work Room (Press Office's own room; the same art is reused as-is for Weather Station — which also serves as the "computer lab/internet access" room — Studio, Control Room, and Research Center). Press Office's earlier 3-room hallway (Writers/Editing/Print, Phase 10) is gone — superseded, not layered on top of. |
| Knowledge | Library (skills), Theater (media/YouTube) | **Resolved (Phase 24):** Library absorbed Archive's function too (same bookshelf-interaction mechanic, differentiated only by scope — current/role-scoped vs. historical/completed-work records) rather than getting a separate room. "Theater/media" folded into Studio (Media's renamed interior, reuses the Work Room art) rather than a standalone room. |
| Command | Command center, Lineup/roster hall | **Resolved (Phase 24):** Command Center's interior renamed Control Room — the room-gated, elevated-privilege space (see Restricted, below). Roster hall stays folded into Town Hall, now homeless pending Town Hall's chat-interface redesign (Phase 24). |
| Restricted | Rooms gated behind API-key-scoped tools | **Resolved (Phase 24):** this is Control Room (row above), not a House bedroom as Phase 13 had left it. `lockeddoor.png` still unplaced pending the actual tool-gating logic (Phase 3 territory, not started). |
| Rest/state | Rest area — low-morale/sleeping agents visibly sit somewhere | Still folded into Town Hall conceptually, but Town Hall's interior is now a chat interface rather than a walk-in room (Phase 24) — homeless pending that redesign; plaza + street benches cover this in the meantime. |
| History | Archive — completed work becomes a physical object | **Resolved (Phase 24):** eliminated as a separate room, folded into Library (row above). |
| Growth | Training ground — visible payoff for a model-tier upgrade | Still folded into Town Hall conceptually — homeless pending its chat-interface redesign (Phase 24), same as Rest/state and Roster hall above. |
| Monitoring | Watchtower — passive oversight, distinct from active work | **Built (Phase 19):** the Observatory, a dialog showing agent model pricing (Cheap/Standard/Premium tiers) — unchanged by Phase 24. Note: Phase 24 also gave the Observatory *building* a walk-in "Research Center" interior room (World 2) for active investigative work — a different thing from this dialog; the two aren't yet reconciled into one coherent Observatory concept. |
| Message intake | Gate/mailroom — a "called" agent's destination, admin's inbox | **Resolved (Phase 24), built (World 2 experiment):** Mailroom eliminated as its own room, fully absorbed into Post Office — walk up to the cubbyhole wall to browse every agent's inbox (seeded flavor mail, not a live system yet — see the mailbox experiment). Role-gating (each agent seeing only their own inbox) isn't enforced yet since there's no real agent perspective to gate against, only the player's/admin's own always-see-everything view. |

**Town Hall is a chat interface, not a walk-in multi-room building
(superseded by Phase 24).** The plan below was the original design, before
Phase 24 questioned whether the meeting room needed a physical space at
all — you're the only one who ever walks into it, and the other agents
would just be "there" whether working or not, which doesn't need art or a
walkable room to represent. Kept here for the historical record of what
was tried first:

~~Once interiors exist, Town Hall's interior holds: the call-meeting room
Tristen's video shows (fireplace, round table, grandfather clock), plus
Lineup/roster hall, Rest area, and Archive/Training ground. One exterior
shell, several interior rooms — consolidating four room categories into a
building already built, rather than four more exterior sprites.~~

**Current plan (Phase 24, design not yet built):** Town Hall's
call-meeting mechanic becomes a chat interface any agent can invoke (not
just you/admin), with preset attendee-group options plus a fully custom
attendee-selection path. Archive and Mailroom have already been relocated
(Library and Post Office respectively — see the table above); Roster hall,
Rest area, and Training ground remain without a home until this
chat-interface design actually happens.

**HUD (borrowing directly from the reference screenshot):** villager count, working
count, open work count, morale, time-of-day clock, quick-action buttons (chatter,
who's who, line-up, call meeting, command center).

**Precise reference confirmed 2026-09-18** (another Tristen's-video screenshot,
`~/Desktop/Screenshot 2026-09-18 at 6.51.03 PM.png`) -- left to right: a
"Complete Workflow HQ" title card; five stat pills (Villagers 22, Working
0, Open Work 0, Subscription "Covered", Morale 54) plus a Time clock
underneath the title card; then a gap; then three small unlabeled count
pills (bee icon "1", a second icon "1", lightbulb "5" -- exact meaning of
the first two not confirmed, don't guess further without asking); WHO'S
WHO (this is our existing Roster button, just styled as a HUD pill instead
of a floating corner button); a mail-count pill ("30" -- this is Post
Office's inbox count, see §3's Message-intake row and Phase 24); a small
icon button with no visible count; LINE UP; CALL MEETING (this is our
built Town Hall mechanic, see the two Town Hall experiment entries below);
a books/notes icon pill ("17" -- almost certainly a running total of
exactly what `REPORTS.length` tracks in the reports/evidence experiment
below, though not confirmed 1:1); and COMMAND CENTER (maps to the Control
Room building, §3's Restricted row). Not built yet -- flagged by you as
tying into the reports work rather than something to build immediately.

## 4. Agent model

- **Roster & hierarchy:** small fixed cast to start (5–6 agents), each with a role,
  home room, and personality — exact identities depend on the theme decision above.
- **States:** working → idle → asleep, with visible spatial correlates (desk / rest
  area / gate).
- **Memory:** each agent keeps short notes about actions and colleagues — feeds the
  "who's who" overlay and gives agents continuity across sessions.
- **Model routing:** cheap tier while idle/wandering, escalate to a stronger model
  when actually executing assigned work — same policy bullpen already validated.
- **Delegation:** an agent can hand a task to another (a scoped version of the
  "call meeting" mechanic, one-to-one instead of village-wide).

## 5. Human interaction

- **Playable character.** You walk the village like any agent.
- **Command center / board.** Where you hand out schedule + tasks — the direct
  equivalent of the reference screenshot's board.
- **Call meeting.** Everyone converges on a shared room.
- **Admin channel.** A message sent from *outside* the village (Slack, CLI, whatever
  the eventual interface is) reaches the admin agent's queue; the backend wakes it and
  it resumes acting inside the village, all without you opening the UI.

## 6. Technical architecture

This is the one section where "lessons learned" drives concrete tool choices, not
just requirements.

- **Backend:** rebuilt fresh (not copied from bullpen), same proven shape — Python,
  FastAPI + websocket, single-writer event log, typed agent state, tiered OpenRouter
  client. This part already worked; no need to relitigate it.
- **Persistence:** append-only event log (jsonl or sqlite) + a derived current-state
  view. Replay = replaying the log, not restoring a snapshot.
- **Frontend rendering:** canvas-based 2D tile renderer driven by real pixel-art
  assets — **not hand-authored SVG shapes**, which is what produced the "looks
  modern"/"looks bad" feedback last night. SVG is fine for HUD chrome; it is not fine
  for the world itself.
- **Nothing is baked into a background image except the ground tilemap.** Every
  building, prop, and decoration is its own transparent-cutout sprite, placed and
  z-sorted by the engine at render time — the same model CraftPix-style asset packs
  ship (see reference: craftpix.net's Tavern pack, where even whole buildings are
  separate cutout pieces, not painted into one scene). This is a correction from an
  earlier Phase 1 draft that baked a wall band into a static room image — that was
  wrong specifically because it collapses walkability into pixel color, when it needs
  to be a per-object flag instead: an object marked non-walkable blocks movement
  wherever it's placed; a walkable one doesn't. Only the ground (grass/path tileset)
  is drawn as a tilemap — everything above it is an independently placed sprite.
- **Art pipeline — the part that gets solved first:**
  - **PixelLab.ai** (account already set up) for character sprites: consistent
    multi-directional walk cycles per agent, generated as a purpose-built pixel-art
    tool rather than prompted out of a general diffusion model.
  - **Licensed/free RPG tilesets** (the itch.io-style packs you already pointed at)
    for terrain and building exteriors/interiors, where a ready-made tileset beats
    generating one from scratch.
  - Every asset gets a **one-time visual sign-off from you** before it's wired into
    the app — this is the step that got skipped three times in a row.
  - **Validated PixelLab recipe (Phase 0, confirmed against your Tavern-pack reference):**
    - Characters: `create-character-with-4-directions`, `template_id: mannequin`,
      `view: high top-down`. Works well out of the box.
    - **Standard recipe for every sprite, buildings included — `create-1-direction-object`
      (or `-8-direction-object` for rotatable placement) with a style reference image**
      (a small crop, ≤256px, from a real reference) passed via `style_images`. This is
      what closed the gap between "generic PixelLab output" and the actual target
      aesthetic — text descriptions alone weren't enough — and it's the endpoint that
      produces a real transparent cutout, which buildings need just as much as props do.
    - The object endpoint's auto-cutout sometimes leaves environment fragments attached
      to the subject (a wall corner, a bush, a flagpole, from whatever was in the style
      crop's background) rather than a clean silhouette. Fix in post, not by fighting
      the endpoint further: `scripts/clean_cutout.py` keeps only the *largest connected
      opaque region* and crops to its bounding box — reliably drops the small stray
      pieces. Run every generated sprite through it.
    - A style crop over ~170px on its long side yields one direct result. Under that,
      the endpoint returns 4 candidate frames in `status: review` — download all 4
      (`frame_urls`), pick the cleanest by eye, `POST /objects/{id}/select-frames` with
      that index, then run the result through `clean_cutout.py` as usual.
    - **For anything that will be placed more than once (trees, lamps, bushes, rocks,
      stumps, signposts), generate variants, don't reuse one sprite everywhere** — a
      repeated village prop stamped from the same file reads as artificial. The
      `review` frames are free variety: `select-frames` can be called more than once
      on the same object with different indices, so pull 2–3 of the 4 candidates
      instead of just the best one. Costs nothing extra (still one generation), and
      the alternative frames are usually genuinely different-looking, not near-dupes.
    - `create-image-pixflux` with `init_image` (opaque, `no_background: false`) is the
      fallback *only* for the rare case of a genuinely flat background texture with no
      subject to cut out (e.g. the floor tileset's color reference swatch) — not for
      anything meant to be placed as an object.
    - Terrain floors: `create-tileset` (Wang/blob tileset) — confirmed working,
      corners tile correctly.
    - Auth: `Authorization: Bearer <key>`. Scripts in `scripts/`, key in `.env`
      (gitignored).
    - **Terrain, revised:** per-terrain `lower_reference_image`/`upper_reference_image`
      on `create-tileset` backfired — a reference image gets reproduced closely enough
      that any small decorative element in it (a flower speckle, a stray leaf) repeats
      as a visible grid across the whole tiled floor. Fixed by dropping per-terrain
      reference images entirely and using `color_image` instead (a plain two-color
      swatch, must be exactly 64x64) — palette guidance without content to copy. Text
      description carries the texture; keep it plain ("smooth green grass lawn") rather
      than describing discrete decorations.
    - **Crop with margin, or the subject comes back cropped too.** A reference crop
      taken tight against a building's own edges (to dodge a neighboring character)
      reproduced that same tight framing in the output — a roofline or wall sliced by
      the canvas edge, or the very bottom of a sign clipped off. Always leave a visible
      margin of background around the whole subject in the source crop; clean up
      whatever extra clutter that margin catches afterward (largest-connected-component
      + a manual trim), rather than cropping tight and losing part of the subject itself.
    - **A building's on-screen position in the source skews its rendered angle.**
      Buildings pulled from off-center in the reference screenshot (near the left or
      right edge of frame) came back showing a visible side wall — an inherited camera
      skew from where they sat in the original isometric-ish shot, not something the
      generation added. The dead-center building came back perfectly straight-on with
      the same recipe. Fix: for anything meant to be placed anywhere in an object-based
      world (i.e. everything), explicitly prompt "shown perfectly straight-on and
      symmetric, camera directly facing the front, NOT at a corner angle" regardless of
      the source angle — text guidance overrides the style reference's implied camera
      position, it just has to be stated.
    - **Surrounding scenery in the reference gets treated as content, not just style,**
      even with `style_images` (which is documented as loose/style-only guidance). A
      courtyard wall visible around a building in the source came back fused to the
      generated building on two separate attempts, despite an explicit "no wall, no
      fence" instruction in the text prompt — negative text instructions did not
      override a strong visual element in the reference. What worked: crop the
      reference itself tighter to exclude the unwanted element (the wall), not just
      describe its absence in text. Text can redirect camera angle; it can't reliably
      subtract something that's actually visible in the reference image.
    - **A full regeneration can drift away from a composition you already liked.**
      When a building's the composition is right and only one element is wrong (a
      fused tree, say), don't just widen the crop and regenerate — `style_images` is
      loose guidance, so a fresh generation can change dish count, roof pitch, other
      details you liked along with fixing the one you didn't. Instead, paint over just
      the unwanted element in the *original* reference crop (flat-fill its bounding
      box with a plausible background color) and regenerate from that otherwise-
      unchanged reference. This reproduced the liked composition with the tree gone,
      where a wider from-scratch crop had produced a different-looking building
      entirely (extra dish, different antenna) alongside the fix.
    - **Reference crops must exclude neighbors.** A crop that includes a bit of an
      adjacent character sprite or a name-label produces garbled hallucinated text/limbs
      in the output (happened with a building sign that had a neighboring character in
      frame — "PRESS OFFICE" came back as garbled letters and a deformed figure).
      Crop tight to just the subject. If a sign's text is in the source crop and you
      don't want that exact text reproduced (it often comes back misspelled/garbled),
      paint a flat color patch over the text region in the source crop before using it
      as `init_image` — the model then renders a plausible blank/generic sign instead
      of trying to copy illegible text.
    - **Superseded note, kept for the record:** an earlier pass concluded object-endpoint
      cutouts were unreliable (a street lamp came back an unrecognizable blob) and fell
      back to opaque `create-image-pixflux` generation for buildings/props, planning to
      solve transparency later as "polish." That was the wrong call — it's what produced
      baked-looking assets in the first place. The actual fix was narrower: that one lamp
      generation landed in `status: review` (small style crop → 4 candidate frames) and
      the *first* frame happened to be bad; picking a different frame via
      `select-frames` fixed it immediately. The object endpoint + `clean_cutout.py` is
      the standard for every sprite, per above — there was no need to give up on
      transparency at all.

## 7. Execution plan

Ordered specifically so the highest-risk, twice-failed part (art) is proven before
any time goes into orchestration logic, which has already succeeded twice and is low
risk to redo.

**Phase 0 — Art spike (de-risk first, before any app code): DONE.**
Generated a character, a floor tileset, a style-matched furniture prop, and a
style-matched wall+door — all validated against your Tavern-pack reference (see §6
for the working recipe). Signed off.

**Phase 1 — Static village, no agents: IN PROGRESS, substantial.**
Full map render (all rooms from §3) with the approved art style, your playable
character walking around, buildings enterable. No backend yet beyond serving static
state.

**How to run it right now:** `cd web && python3 -m http.server 8935`, open
`http://127.0.0.1:8935/index.html`. Arrow keys/WASD to walk. Press **G** in-page to
toggle a debug overlay that draws every object's walkable footprint as a red box
and the player's own box in blue — use this first when checking "can they walk
there" before trusting a screenshot.

**Engine (`web/index.html` + `web/world.js`), real object-model, nothing baked:**
- `world.js` holds the ground shape and every placed object as
  `{sprite, x, y, w, h, walk:{x,y,w,h}, scale?}`. `walk` is a footprint local to
  the sprite — usually just the base of a building or the trunk/post of a
  tree/lamp — so the player reads as passing behind the object (per-frame y-sort
  on each entity's feet position) while stopping at the base line, not the full
  sprite bounds. `scale` (default 1) shrinks a sprite and its footprint together
  for the few assets that generated larger than they should read (see below).
- Collision is per-object AABB against that footprint, with axis-independent
  sliding so a diagonal approach doesn't stick.
- Ground is two blended tilesets, current (Phase 9) approach: every cell is
  first filled with plain grass (`assets/village/tiles/wang_0.png`), then a
  second pass overlays `assets/village/tiles_sidewalk/` (Wang-blended
  dirt/cobblestone) only on cells `terrainAt()` marks as street/plaza/walk-row
  — see Phase 9 write-up in §7 for why (a from-scratch sidewalk tileset
  generation failed twice, so this reuses the already-proven Phase-1 plaza
  tileset instead of the old two-tileset-with-shared-lower-color trick).
- Browser-tested and confirmed working: movement, direction-swap, footprint
  collision (verified precisely against Town Hall's footprint — player stops
  exactly at the boundary, doesn't penetrate), y-sort (player draws in front of
  a building from below, behind it from above), and the plaza tile blend.

**Village art — 21 independent transparent-cutout sprites** via the
object-endpoint recipe in §6 (`assets/village/`), each checked individually,
composited only for preview, never baked: Press Office, Library, Media studio,
Command Center, Market, Town Hall, farm plot, fence, fountain, plus **variant
sets** for anything that appears more than once so the village doesn't look
stamped-out — tree (a/b), lamp (a/b), bush (a/b/c), rock (a/c), stump (a/c),
signpost (a/b). Two buildings were tried and replaced on your call, not because
the art was bad: the Observatory (fixed twice for real framing/fusion defects,
then dropped as not the right village fit) and the Vault (dropped, replaced by
Command Center — ties directly to the HUD's "COMMAND CENTER" button and the
task-assignment mechanic in §5, more relevant than a treasury). Town Hall uses
Library as its style reference for palette consistency, and is the exterior
shell for the call-meeting room Tristen's video shows in interior detail
(fireplace, round table, grandfather clock — `tristen/` screenshots). Also
added: `house.png`, your own character's home — styled off Library for the
same warm-cottage palette but a distinct smaller single-family design, placed
near the farm plot. Also added, closing two of the gaps from the structures
review below: `gate.png` (village entrance archway with a mailbox, placed on
the south end of the path spine — the two side pillars are separate footprint
boxes so the archway itself is walkable, verified by walking a player
straight through it) and `lockeddoor.png` (a reinforced steel side door with
a keypad, placed against Command Center's side — the visible marker for the
Restricted room category, styled off Command Center itself for material
match).

**Scale lesson (verify visually against neighbors, not just individually):**
a few props (stump, market stall, park bench) were generated at a pixel size
that read fine alone but was disproportionate next to a two-story building —
a stump nearly as tall as Press Office. Fixed via the `scale` field rather than
regenerating (0.55 for stumps, 0.75 for market, 0.7 for bench) — cheaper and
keeps the art you already approved. Check new assets against their neighbors at
placement time, not just in isolation.

**Layout status:** the current `world.js` placement is a first real pass, not
final — built and refined with the debug-footprint overlay (§ above), catching
and fixing several real bugs this way: a tree rendering on top of the library
roof, a stump clipping past the world edge, two fence segments stacked on each
other instead of forming a wall (fixed into one contiguous run), decor sitting
inside a building's own footprint. Confirmed clean by panning the debug overlay
across all four quadrants of the map. Still explicitly open, per your own framing:
**"map out where everything should live"** is unfinished — current placement is
functional and non-overlapping, not a final composition pass.

**Walled yards — the structural pattern this village was missing.** Comparing
against Tristen's reference directly surfaced it: every building there sits
inside its own walled yard with a gate gap facing the plaza, densely dressed
with department-specific clutter inside — ours had buildings floating on open
grass with no enclosure at all. Fixed properly, not just patched:
- **Two wall assets, not one rotated.** The reference draws front-facing
  (south) walls with real height/a visible face, but side walls (viewed along
  their length) are much thinner — almost a curb. An initial attempt rotated
  one horizontal wall texture 90° for the verticals; the geometry was right
  but the perspective wasn't (a real side-wall reference has different
  shading logic than a rotated front-wall). Fixed by generating `front_wall.png`
  and `side_wall.png` separately, each from its own crop of the reference
  showing that specific orientation.
  Both are intentionally low/wide in their native art (not tall and chunky
  like the first pass) — the wall reads as a low garden wall, not a fortress.
- **The gate is a deliberate gap, not a baked-in opening.** A generated
  "wall with a gate hole in it" sprite came back with an actual opening only
  ~2px wide — reliably useless as a footprint boundary. The robust fix: build
  a gate side as two plain wall segments with `GATE_GAP` (90px) of deliberately
  uncovered space between them, in `yardWalls()` in `world.js`. No dependency
  on a generated gap being usable.
- **Non-uniform stretch, not modular tiling.** Getting a wall to span an
  exact yard-side length without needing the yard size to be a multiple of the
  wall sprite's native size: `scaleX`/`scaleY` (index.html's `scalesOf()`)
  stretch a single wall piece to the exact length needed and shrink it to
  `FRONT_T`/`SIDE_T` thickness on its short axis. Footprints scale the same
  way, from the same `scalesOf()`, so the blocking box always matches what's
  drawn.
- **Yards hug the building.** First pass left ~90-190px of empty grass
  between building and wall on every side — nowhere near the reference's
  tight fit. Rebuilt at roughly 55px margin (`YARD_DEFS` in `world.js`), which
  after wall thickness leaves a believable narrow walkway inside, not a
  half-empty field.
- **Multi-box footprints**, needed because a gate can't be one rectangle:
  `walk` on any object can now be a single box or an array of boxes
  (`footprintsOf()` in index.html); `blockedAt` and the debug overlay both
  handle either.

All 5 buildings are now walled (Press Office, Command Center, Media, Town Hall,
Library), each with a gate facing the plaza and 1 small yard-clutter prop
inside. The fountain, market, benches, lamps sit unwalled in the open plaza,
matching the reference (only buildings get yards, not plaza furniture).
Ground was expanded to 48×34 tiles to fit properly-spaced yards without
crowding. Browser-tested with the same debug-footprint overlay: gate gaps
confirmed walkable, wall runs confirmed blocking, all five yards checked by
panning across the map.

**Three rounds of comparison against the reference, each catching something
real:**
1. Margin cut from ~190px down to ~55px, then down again to ~28px — the
   reference's yards hug the building far tighter than either first attempt.
2. **The back (north) wall needs to be thin like the side walls, not tall like
   the front wall.** Only the wall facing the camera shows a tall face in this
   top-down perspective — back and side walls both show mostly their top
   surface. Using the tall front-wall texture for the back wall (rotated or
   not) made its corners with the (correctly thin) side walls look wrong: a
   tall piece meeting a thin one, where both should be thin. Fixed by adding
   `back_wall.png` — literally the same capstone-row crop that `side_wall.png`
   is rotated from, used horizontally instead — so back and side meet as
   identical stone at the corner, by construction, not by matching two
   independent assets closely enough by eye. Front-meeting-side (tall meeting
   thin) is still correct and matches the reference.
3. A generated "wall with a gate hole" sprite's actual opening was ~2px wide —
   confirmed unusable before it ever shipped. Superseded by point 4 below
   (no buildings have side entrances, so there was never a side gate to cut a
   hole for in the first place) but the lesson generalizes: don't trust a
   generated gap without measuring it.
4. **No building has a side entrance, so no yard needs a gate at all.** Every
   building here was generated with only a front door. Once that's true, a
   yard doesn't need a full enclosing rectangle with a hole cut in one side —
   it needs a back wall, two side walls, and the entrance is simply the open
   ground between two front corner posts flanking the doorway. This is also
   literally what Library's own art already does (one post at its front-left
   corner) — which is why it became the style reference for the posts on
   every other yard, replacing the plain-brick front/back wall style pulled
   from the Tristen crop. Library itself doesn't get a generated yard at all;
   its own posts already read as bordered. What it needed instead was a more
   accurate footprint — the door-width-only box let a player walk through the
   post/ivy/barrel cluster on its left edge, which is clearly solid in the
   art; widened to the full building width.

**Bug found and fixed: player could spawn stuck.** The default spawn point
happened to sit inside the fountain's footprint — a player starting already
overlapping an obstacle couldn't move at all, since every candidate move was
"still overlapping something." Fixed two ways: moved the actual spawn point to
a verified-clear tile, *and* added a general safety net in `update()` — if the
player is ever found already overlapping a footprint (a future bad placement,
an object moved under them), collision is skipped for that frame so they can
walk free instead of freezing permanently. Verified both independently.

**Current status: yards/posts built but switched off.** After the corner-post
rework above, you weren't happy with how the walls read overall and asked to
remove them for now — not a rollback to a broken state, a deliberate pause.
`web/world.js`: `const YARDS_ENABLED = false;` is the switch; `yardWalls()`,
`YARD_DEFS` (per-building yard bounds), and the corner-post assets
(`corner_post.png`, `back_wall.png`, `side_wall.png`) are all left in place
untouched. To bring walls back for every building, flip that one flag to
`true`. To bring them back for only some buildings, filter `YARD_DEFS` in
`buildYards()` instead of using the flag. Nothing needs to be regenerated or
rebuilt — the working implementation is intact, just not called. Yard clutter
(crates/rocks placed inside yards) was removed in the same round and is not
part of what the flag restores — that was a separate, explicit "later" per
your call, not wired back up to the flag.

**Noted, not yet done:** a dedicated shorter/cropped variant of `side_wall.png`
for short wall runs (currently the same repeating capstone strip just
stretched shorter, which read fine so far but hasn't been compared closely
against the reference's half-length end pieces) — moot while walls are off.
Also still open: yards are uniform rectangles — the reference varies yard
*shape* per building (an L-shape or notch, not just a differently-proportioned
rectangle), which `yardWalls()` doesn't support (it only draws straight runs
between two front posts).

**Captured design idea, not yet built:** building entrances should have a small
transitional area (like classic Pokémon games — climb a few steps, stand on the
porch, then enter) rather than an abrupt wall-to-door cut. Relevant once interiors
exist (see "Not yet done" below); the footprint model already supports this (a
door's walk-footprint could simply have a gap or a distinct non-blocking zone
right at the threshold) — just not designed yet.

**Not yet done:** organic/curved terrain paths, any further buildings/rooms
from §3 beyond the current 7, and interiors for Library/Media/Command
Center/House (only Town Hall and Press Office got interiors in Phase 10).
~~Entering a building's interior~~ — **built in Phase 10, see below.**

**Phase 9 — Layout redesign: responsive viewport, on-screen controls, function
over decoration.** Explicit shift in stated priorities: **"I am no longer
concerned with creating something as decorative as what Tristen O'brien has,
but I want the same functionality. I think that is what I should strive
for."** Everything below serves that: a legible, navigable town over a
decorative one.

- **Responsive viewport, decoupled from render resolution.** The canvas still
  renders at a fixed internal size (`VIEW_W=960, VIEW_H=640` in `index.html`
  — the old `SCALE` variable is gone, every draw call and footprint-overlay
  call uses raw coordinates now). What scales is the *display*: `#viewport`
  is `aspect-ratio: 960/640; max-width:100%; flex:1 1 auto; min-height:0`
  inside a flex column `#wrap`, and the `<canvas>` itself is
  `width:100%;height:100%;image-rendering:pixelated`. Shrinking the browser
  window shrinks the displayed game proportionally; the render math never
  changes. Verified down to a 480×700 phone-width viewport — village, HUD
  text, and on-screen controls all stayed visible and legible.
- **On-screen d-pad, same input path as the keyboard.** Four HTML buttons
  (`#pad`) wired via `pointerdown/up/leave/cancel` to add/remove the exact
  same string keys (`'arrowup'` etc.) the keyboard handler already uses in
  `state.keys` — `wirePad()` in `index.html`. There is one movement code
  path; the d-pad is just a second way to populate the same set. Verified by
  dispatching a `pointerdown` on the right-arrow button and confirming
  `state.keys` gained `"arrowright"` and the button got its `.held` class.
- ~~**One street, not a cross.**~~ **Superseded almost immediately — see the
  correction right after this Phase 9 write-up.** The first attempt built a
  vertical street from the gate up to a rectangular plaza, plus a horizontal
  "sidewalk" fronting the building row — not what was actually asked for.
- **Building row, front-aligned, Town Hall centered.** `YARD_DEFS` bounds
  were rebuilt around a single constant, `ROW_FRONT_Y`, so every building's
  *bottom* edge sits on the same line regardless of its own height (like
  real storefronts) — Press Office, Media, Town Hall, Library, Command
  Center, left to right, with Town Hall's horizontal center matching the
  fountain's and the vertical street's. Confirmed visually and by moving the
  player to the plaza and screenshotting the full row.
- **Sidewalk tileset — reused, not regenerated.** Two separate
  `create-tileset` attempts at a genuine grass/cobblestone Wang set (via
  `color_image`, a green+grey swatch) came back with zero visible contrast
  between the `lower` and `upper` states on both tries — unusable. Rather
  than spend a third generation, `tiles_sidewalk/` is a direct copy of the
  already-proven `tiles_plaza/` dirt/cobblestone set from Phase 1. That
  created a new problem: using that set as the *only* ground tileset would
  paint the whole map dirt-colored, since its "lower" state is dirt, not
  grass. Fixed in `index.html`'s ground-building loop: first fill every cell
  with plain `tiles/wang_0.png` (grass), then a second pass only draws a
  `tiles_sidewalk` Wang tile on cells where `terrainAt()` isn't uniformly
  `'lower'` on all four corners. Net effect: grass everywhere except the
  street/plaza/walk-row, which are dirt-and-cobblestone, from one tileset
  folder. The now-fully-superseded `tiles_plaza/` copy was deleted from both
  `assets/village/` and `web/assets/village/` to avoid confusion — nothing
  references it anymore.
- **Flowers around the fountain.** Two new sprites, `flower_a.png` and
  `flower_b.png` (via `create-1-direction-object`, style-referenced off
  `farmplot.png`, picked from two different `review` candidate frames for
  variety). First placed as three asymmetric clusters; reworked into an even
  ring in the correction below. Purely decorative — each has a
  `walk:{x:0,y:0,w:1,h:1}` footprint (a zero-size box is a real edge case in
  the AABB overlap check; 1×1 is the safe negligible-but-nonzero equivalent)
  rather than blocking movement.
- **Benches and trees along the street.** Both live in `STREET_OBJECTS` —
  benches sit directly on the roads and plaza edges; four trees (alternating
  `tree_a`/`tree_b` for variety) sit in the grass band south of the
  building row, across the street from it. Positions held up unchanged
  through the correction below, since the roads they sit on didn't move.
- **Bushes dropped from this pass, not just shrunk.** You'd flagged the
  bushes ("scrubs") as still too large even after an earlier scale-down.
  They were previously placed only as yard-clutter (crates/rocks/bushes
  inside the now-disabled walls, per the "Current status: yards/posts built
  but switched off" note above) — with yards off, there was no yard left to
  put them in, so they simply aren't placed anywhere in the current
  `world.js`. The sprites (`bush_a/b/c.png`) still exist in `assets/village/`
  if you want them reintroduced somewhere on open ground, sized down
  further. Flagging this as a gap rather than a silent decision — say if you
  want them back in and where.
- **Player's House, added this pass.** `house.png` (styled off Library for
  a matching warm-cottage palette, smaller single-family footprint) placed
  in the grass at bottom-left, next to the farm plot — `PLAYER_HOUSE` in
  `world.js`.
- **Mailbox outside the House.** New sprite `mailbox.png` (style-referenced
  off `house.png`), placed a short walk from the front door
  (`MAILBOX` in `world.js`). "Checking" it is not wired to anything yet —
  that's a Phase 2+ backend hook (see the room-table update above).
- **Gate repositioned to the south edge; player spawns just inside it.**
  `VILLAGE_GATE` sits at the bottom of the vertical street, its archway
  still modeled as two separate pillar footprint boxes (not one box across
  the sprite) so the opening itself is walkable. `SPAWN` is a new named
  constant (`{x:745, y:850}`, just north of the gate, verified clear of
  every footprint) that `index.html`'s player-init now reads instead of a
  hardcoded position. Confirmed in-browser: fresh page load places the
  player just inside the gate, matching "this is where the character should
  enter when I access the webpage."
- **Restricted door: unplaced for now, moved conceptually into the House.**
  See the room-table update above — `RESTRICTED_DOOR` is commented out in
  `world.js` with a note on why, ready to be placed once the House has an
  interior.
- **Verified in-browser, this pass:** the responsive viewport at both
  desktop and phone width; the d-pad's `pointerdown`/`state.keys`/`.held`
  wiring; spawn position and the gate; the full building row with Town Hall
  centered over the fountain; the debug footprint overlay (`G`) across the
  building row, farm, house, benches, and trees, with no overlapping or
  missing boxes.
- **Bug found after this pass shipped, and fixed: mobile view squished the
  art.** The first responsive implementation relied on CSS alone —
  `aspect-ratio: 960/640` on `#viewport`, which was also `flex: 1 1 auto`
  inside a flex column so it would grow to fill available height, capped by
  `max-width: 100%`. That combination doesn't reliably hold: once the flex
  column grew the box's height to fill available vertical space and
  `max-width` clamped the width separately, the browser never re-derived
  height from the clamped width to keep the 3:2 ratio — measured directly at
  375×667, the box came out 363×416 (ratio 0.87, not 1.5). The canvas fills
  `width:100%;height:100%` of that box, so it stretched the 960×640 render
  non-uniformly into whatever wrong-shaped box resulted — the squish you
  saw. Fixed by dropping CSS aspect-ratio/flex-grow for this element
  entirely and computing the box in JS instead (`fitViewport()` in
  `index.html`): read the actually-available width and height (window size
  minus the d-pad's and info text's real rendered height), pick whichever
  dimension is the binding constraint, and set `#viewport`'s width/height in
  px directly from `VIEW_W/VIEW_H`'s ratio, re-run on every `resize` event.
  Verified the box holds ratio 1.5 at both 375×667 (measured 1.492) and
  1400×900 (measured 1.498) after the fix.

**Terrain correction — the "sidewalk" reading of Phase 9 was wrong.** Your
own words: *"You completely misunderstood my sidewalk request. We can
disregard it."* What you actually wanted, and what's now built:
- **A road, not a sidewalk, spanning the full map width.** `world.js`'s
  `terrainAt()` no longer restricts the horizontal road to a span in front
  of the buildings (`WALK_SPAN`, deleted). `ROAD_Y` now has no x-bound at
  all — the road reaches both the left and right edges of the map, matching
  "the road to extend to the end of the viewport to the right and left."
- **The vertical road still stops at the horizontal one — nothing extends
  north of it.** `inVertical` now only evaluates true for `vy > ROAD_Y.y1`
  (strictly south of the horizontal road), so the area north of the road —
  where the building row sits — is plain grass, walkable but unpaved. This
  was already true in the first Phase 9 attempt and didn't need to change;
  it's carried over.
- **The plaza is a circle now, not a rectangle — a real roundabout.**
  `PLAZA` (a rectangular `{x0,x1,y0,y1}`) is gone, replaced by
  `PLAZA_CENTER` (the fountain sprite's own visual center) and a
  `PLAZA_RADIUS` (150px) distance check in `terrainAt()` — any tile within
  that radius of the fountain's center paints as dirt/cobblestone,
  regardless of the rectangular roads, so the intersection bulges into a
  round patch the way a real roundabout does rather than reading as a plain
  crossroads.
- **Flowers rebuilt as an even ring, fountain dead center.** The old three
  hand-placed, asymmetric flower clusters are gone. `flowerRing(count)` in
  `world.js` places `count` clusters (6, alternating `flower_a`/`flower_b`)
  at equal angles around `PLAZA_CENTER` at a fixed `FLOWER_RING_RADIUS`
  (95px) — comfortably outside the fountain's own footprint and inside
  `PLAZA_RADIUS`, so the ring sits fully within the roundabout circle with
  the fountain exactly at its center, matching "a circular center with
  flowers within and the fountain within the center of the flowers."
- **Ordering bug caught before it shipped:** the first version of this fix
  computed `PLAZA_CENTER` from `FOUNTAIN_X`/`FOUNTAIN_Y` before those
  constants were declared later in the file — a top-level `const` temporal-
  dead-zone error, not a logic bug. Fixed by moving the fountain position
  constants to the top of `world.js`, above `terrainAt()`.
- **Verified in-browser:** the road reaches both map edges on screen, the
  vertical road cleanly stops at the roundabout with no path continuing
  north, the circle bulges visibly past both roads at the intersection, and
  the debug footprint overlay (`G`) confirms all six flowers keep their
  negligible non-blocking footprint and nothing else regressed (buildings,
  benches, trees all still correct).

**Second correction — the path was gray, not dirt.** You'd asked for it back
as dirt; the `tiles_sidewalk/` set used for the road/roundabout was actually
a copy of the Phase 1 *plaza* tileset, whose "upper" state renders as flat
gray cobblestone, not brown dirt (its "lower" state is a tan color, but that
never got used once it was pressed into service as the *path* tileset — the
path always painted with the tileset's upper/full state). Fixed by switching
the overlay in `index.html`'s ground-building loop to `assets/village/tiles/`
instead — the original Phase 0/1 grass↔dirt Wang set, whose "upper" state is
genuine brown dirt and blends cleanly into the grass base (same tileset
already used for the flat grass fill, so the blend tiles were already proven
and just needed to be used for real instead of only supplying `wang_0`).
`tiles_sidewalk/` was deleted (both `assets/village/` and
`web/assets/village/`) as fully unused. Verified visually: the road,
roundabout, and building-row frontage all render as dirt-brown with a soft
grass edge, not gray pavement.

**Phase 10 — Zoom, a bigger cast of trees, a wider gate, and real building
interiors.**

- **Zoom controls.** A fixed panel on the right edge of the browser window
  (`#zoom` in `index.html`, positioned `fixed`, independent of the game
  viewport's own responsive scaling) with `+`/`−` buttons and a `SHOW ALL`
  toggle. `state.zoom` (default `0.85`, zoomed out a bit from native 1:1 per
  your request) drives both the camera math and every draw call's
  destination size in `render()`: the visible world rectangle is
  `VIEW_W/zoom × VIEW_H/zoom`, so zooming out reveals more world rather than
  shrinking the same view. When that visible rectangle is bigger than the
  map on an axis (zoomed out past what the map can fill), the map is
  centered with letterboxing on that axis instead of an invalid clamp range
  — this is what `SHOW ALL` uses (`ZOOM_SHOW_ALL = min(VIEW_W/GROUND_W,
  VIEW_H/GROUND_H)`, toggles back to the default zoom on a second click).
  Verified: `+`/`−` change `state.zoom` correctly, clamped to
  `[0.4, 1.5]`; `SHOW ALL` fits the entire village (all buildings, the gate,
  the house) into one screen with visible letterboxing on the sides.
- **Trees rebuilt as two proper tree-lines, not scattered.** `world.js` gained
  a small `tree()`/`treeRow()`/`treeCol()` helper set (keyed by each tree
  sprite's own native size) so tree placement is declarative — a list of
  positions in, tree objects out — rather than 16 hand-typed literals. Two
  lines now exist: along the horizontal road fronting the buildings (denser
  than Phase 9's 4 trees), and a new one along the vertical entrance road
  from the roundabout down toward the gate, closing the gap you pointed out
  ("move the trees to where they all line the road going into town").
- **Flowers removed.** The fountain's flower ring (Phase 9) didn't read the
  way you wanted ("that isn't quite what I had in mind") — `flowerRing()`
  and its call site were deleted outright rather than re-tuned. The
  roundabout terrain shape (§ above) is unaffected; only the flower props are
  gone.
- **Down to a single bench.** `STREET_OBJECTS` had 4; 3 were removed, one
  kept near the roundabout.
- **Vegetable garden moved onto the house's own back side.** `FARM_OBJECTS`'
  farm plot is now positioned directly off `PLAYER_HOUSE`'s north edge
  (`PLAYER_HOUSE.y - 167 - 20`, centered on `HOUSE_CENTER_X`) rather than a
  fixed absolute position — "closer to the house" is now structural (derived
  from the house's own position), not a coincidence of two numbers agreeing.
- **Market moved to the opposite side of the road.** Was west of the
  roundabout (near the farm); now sits east of it in `PLAZA_OBJECTS`,
  mirroring across the road center.
- **Mailbox and the double-arrow signpost removed entirely**, per your call —
  both `MAILBOX` and the `signpost_b.png` placement are gone from `world.js`
  and from `WORLD_OBJECTS`. (The "Mailroom" concept didn't go with them — see
  the interiors work below, it's now a room, not a prop.)
- **The gate: two failed shapes before landing on the right one.** You asked
  for a gate wider than the road, with the whole road fitting under it — and
  separately, after the first fix, that it keep a real connecting archway
  like the original. Three PixelLab `create-image-pixflux` generations later
  (`scripts/gen_big_gate.py`, kept for the record):
  1. A "castle gate" prompt (thick pillars + flanking wall segments) came
     back with a native gap of only ~80px out of 343px wide — proportionally
     no better than the original, because the flanking wall segments ate
     most of the width. Rejected before it was ever placed.
  2. Pushed the prompt toward "one enormous archway, thin posts" and got a
     genuinely wide gap — but split the result into two separate sprite
     pieces (`gate_left.png`/`gate_right.png`, one post each) placed apart
     with an explicit `GATE_GAP`, specifically to make the gap width
     controllable independent of whatever the generation produced. This
     worked functionally (gap = 210px, wider than the 192px road) but lost
     the connecting arch overhead entirely — two disconnected posts with
     open sky between them. You caught this immediately: **"I was hoping
     the gate would have an archway like the old one did."**
  3. Fixed properly: regenerated once more with the prompt explicitly
     demanding "one unbroken stone arch... spans the ENTIRE width... does
     not stop or break anywhere," at 400×168. This came back as a real
     continuous arch with legs at native x=75–115 and x=265–305 (a 150px
     gap) — narrower than the road on its own, but since it's a single
     connected sprite this time, the two-piece trick doesn't apply. Instead:
     **uniform `scale: 1.4`** on the whole sprite (both the art and its
     `walk` footprint scale together via the existing `scalesOf()`), which
     stretches the 150px gap to 210px — comfortably wider than the road —
     without any distortion, since scaling both axes equally preserves the
     art's proportions exactly. `GATE` in `world.js` is one object again,
     with a two-box `walk` array covering just the two legs (`{x:75...}`,
     `{x:265...}`, both `w:40`), same multi-box-footprint pattern used for
     the yard gate design. Verified in-browser: the whole road passes under
     the arch with margin on both sides, and the arch itself reads as one
     continuous structure.
  - Both generation background-removal passes needed the same manual fix:
    `no_background: true` didn't actually produce transparency (the
    background came back as a flat opaque gray), so `scripts/chroma_key.py`
    (flood-fill from the four corners) was run after the fact, same as
    earlier phases. A second flood-fill pass, seeded from a pixel *inside*
    the arch opening rather than from a corner, was needed each time too —
    the sky visible through the arch is an enclosed region the
    corner-seeded flood fill can't reach on its own.
- **Building interiors: real navigable structure, not just decoration.**
  Previously a building's door was purely cosmetic — walking up to it did
  nothing. Now, per your request ("I would like for you to add three rooms
  in town hall. You will enter a hallway first and then you will choose from
  the rooms. I want the work building to be the same way"), Town Hall and
  Press Office ("the work building" — see the room table's Work row) both
  have real interiors:
  - **Data** (`world.js`): `INTERIORS[name]` lists a building's room names;
    `DOOR_TRIGGERS[name]` is a small rectangle straddling the bottom edge of
    that building's own blocking footprint, computed from the building's
    existing `walk` box via `doorTriggerFor()` rather than hand-typed —
    deliberately a *separate* zone from the blocking footprint itself
    (walking into this specific strip is what fires the transition; the
    rest of the building's base still blocks normally).
  - **State machine** (`index.html`): `state.location` is `'outside'` or
    `{building, view}` where `view` is `'hallway'` or a room index.
    `enterBuilding`/`enterHallway`/`enterRoom`/`exitOutside` handle every
    transition, always repositioning the player to a sensible spot in the
    new scene (just inside the hallway's entrance, centered in a room,
    just outside the building's real-world door).
  - **Movement + triggers**: `update()` branches on `state.location` —
    `updateOutside()` is the original outdoor collision code plus a
    door-trigger check after movement; `updateInterior()` is a much simpler
    clamp-to-room-bounds since interiors have no footprint-based obstacles,
    just the doors themselves as trigger zones.
  - **Rendering**: interiors are deliberately plain — flat-colored floor and
    wall rects plus text labels for the room name and each door
    (`renderInterior()`), not generated art. This matches your stated
    priority ("I am no longer concerned with creating something as
    decorative... I want the same functionality") — it's real, walkable,
    branching structure, just not pixel art yet. Room *contents* (the actual
    room-gated tools an agent could use inside, per §2's locked
    requirements) are Phase 3 work, not this pass.
  - **Town Hall's 3 rooms:** Meeting Hall, Mailroom, Archive — folding in
    the "Message intake"/mailroom concept (room-table update above) and the
    History/Archive category from §3, both previously just noted as
    "folded into Town Hall's interior" with no actual interior to fold into.
  - **Press Office's 3 rooms:** Writers Room, Editing Room, Print Room.
  - Verified in-browser end to end for both buildings: walking into the
    door trigger enters the hallway; walking into a room door enters that
    room; each room's EXIT returns to the hallway; the hallway's EXIT
    returns outside, positioned back at the building's real door.

**Phase 11 — Market/garden reposition, symmetric tree lines.**

- **Market moved to the right of the last building in the row.** Was a
  standalone plaza prop near the roundabout; now `MARKET` in `world.js` is
  derived from the row's own last entry (`YARD_DEFS[YARD_DEFS.length-1]`,
  Command Center) — positioned just past its right edge and bottom-aligned
  to `ROW_FRONT_Y`, the same frontage line every building in the row sits
  on, so it reads as an extension of the row rather than a separate object.
- **Vegetable garden moved to the house's left side.** Was tight against the
  house's back (north) edge; now `FARM_OBJECTS`' farm plot sits to the
  house's *left* (west) instead, at `PLAYER_HOUSE.x - 160 - 20`. First tried
  with the garden raised 20px above the house's own y — you then asked for
  the two to share a plane exactly, so both now read from one shared
  constant, `HOUSE_ROW_Y` (700, previously the house sat at 720), rather
  than the garden being defined as an offset from the house's y — "same
  horizontal plane" is structural now, not two numbers that happen to
  agree.
- **Trees rebuilt as genuinely mirrored pairs, not two independently-tuned
  lists.** The Phase 10 tree lines had different counts and spacings on each
  side (3 vs 4 on the horizontal row, 4 vs 3 on the vertical one) — not
  actually symmetric despite looking roughly similar. Replaced with
  `treePairAt()`/`treeRowPairs()`/`treeColPairs()` in `world.js`: every tree
  is placed as one of a mirrored pair, both members the same distance
  (`offset`) from `PLAZA_CENTER_ROAD_X` (the road's own center line) on
  opposite sides, so left and right are identical by construction — the only
  way to make them asymmetric now would be to explicitly pass different
  offset lists for each side, which nothing does. Sprite choice (`tree_a`
  vs `tree_b`) alternates per pair so adjacent pairs don't read as clones.
  Counts increased from 7+7 to 10+8 (5 pairs along the horizontal road, 4
  pairs along the vertical one) per your "I want more trees" — bounded by
  clearing the roundabout circle, the road's own width (vertical pairs use
  `offset: 140`, comfortably past the road's 96px half-width), and the gate
  (the last vertical row stops at y=755, its rendered bottom safely north of
  the gate's own top edge at y=895).
- **Bench moved to the left of the leftmost building.** `STREET_OBJECTS`'
  single bench (Phase 10) now sits to the left of Press Office — the first
  entry in `YARD_DEFS`, referenced as `FIRST_BUILDING` (added alongside the
  existing `LAST_BUILDING`, both now declared once, right after `YARD_DEFS`,
  so the market and the bench share the same "read the row's own data,
  don't hand-type a position" pattern) — bottom-aligned to the row's
  frontage line the same way the market is.
- **Default view is now "show all," not zoomed in.** `state.zoom` initializes
  to `ZOOM_SHOW_ALL` instead of a fixed `0.85` — the whole village is what
  you see on page load; the `SHOW ALL` button still toggles between that and
  a closer-in `0.85`, just starting from the opposite end now.
- **Verified in-browser:** market sits flush against Command Center's right
  edge at the row's frontage line; the bench sits left of Press Office at
  that same line; garden and house share one y-baseline exactly; both tree
  lines are visually and numerically symmetric; the page loads directly into
  "show all"; the debug footprint overlay (`G`) confirms no new overlaps
  among trees, market, bench, garden, house, or the road/gate.

**Phase 12 — Gate removed.** You reported trouble reaching the house; the
actual cause was the gate's left support leg — its footprint (world
x≈605–661) sat directly on the straight-line path from spawn (x=758) west to
the house (x≈365–515), at the same y-band as the gate itself, so a direct
walk toward the house hit an apparently-arbitrary wall; the only way around
was north-then-west, out of the gate's own footprint first. Given the choice
between repositioning the gate (keep the entrance, fix the clearance) and
removing it outright, you chose removal. `GATE` and its `WORLD_OBJECTS`
entry are gone from `world.js`; `SPAWN` moved from y=850 to y=950 (the south
end of the road, previously inside where the gate stood) with no footprint
there to clamp around anymore. `gate.png` is left in `assets/village/` in
case a gate gets reconsidered later, but nothing in the code references it
now. Verified: a straight line from spawn to the house has zero blocked
points (checked programmatically at 20px steps against every object's
footprint), and visually the road now opens cleanly onto the south grass
with nothing in the way.

**Phase 13 — One room for every remaining structure.** Media, Library,
Command Center, and the player's House had doors that led nowhere (open
item #10, §8). Each gets exactly one room — Studio, Reading Room, Control
Room, and Bedroom respectively — added to `INTERIORS` in `world.js` the same
way Town Hall/Press Office's rooms were, plus a `DOOR_TRIGGERS` entry each
(`doorTriggerFor()` for the three YARD_DEFS buildings; a new
`doorTriggerForBox()` extracted from it so it also works for `PLAYER_HOUSE`,
which isn't a YARD_DEFS entry).

- **A single room skips the hallway.** With only one room, a hallway with
  one door plus an EXIT would be a pointless extra step — `enterBuilding()`
  now checks `INTERIORS[name].rooms.length`, and enters the room directly
  when it's 1. `roomLayout()`'s own EXIT button adapts the same way: it
  leads back to the hallway when there's more than one room, straight
  outside when there's only one (there's no hallway to return to in that
  case). Town Hall and Press Office are unaffected — both still have 3
  rooms, so they still get the hallway step exactly as before.
- **Verified in-browser:** all four new buildings enter their single room
  directly (`state.location.view === 0`, never `'hallway'`) and their EXIT
  returns to `'outside'` directly; Town Hall still correctly enters
  `'hallway'` first, confirming the one-room shortcut didn't affect the
  multi-room buildings.
- **Not done here:** room contents (open item #11, §8) — every room, one or
  three, is still a plain colored box with a name and an EXIT. The
  Restricted-room concept (§3's room table) hasn't actually been moved into
  the House's new Bedroom room yet, even though the House now has *an*
  interior to put it in — that's still a separate open decision.

**Phase 14 — House/garden shifted left; the vertical tree line now runs the
whole road.**

- **House and garden moved left.** `PLAYER_HOUSE.x` went from 350 to 260;
  the garden's x is still derived from it (`PLAYER_HOUSE.x - 160 - 20`), so
  it moved the same 90px left automatically and the two stayed in sync —
  same benefit as `HOUSE_ROW_Y` in Phase 11, one value drives both rather
  than two numbers that have to be kept in agreement by hand.
- **Vertical tree line extended to the road's full length.** It used to stop
  at y=755 (its rendered bottom safely north of where the gate stood);
  with the gate gone (Phase 12) there was bare road all the way to the map's
  south edge with nothing lining it. Added two more mirrored rows —
  `treeColPairs([500, 585, 670, 755, 840, 925], 140)`, up from four rows to
  six — reusing the exact same symmetric-pair machinery from Phase 11, so
  the new rows are guaranteed to match the existing ones in offset and
  alternate sprite choice rather than needing to be hand-tuned to look
  consistent. The last row's rendered bottom (y=925+116=1041) stays inside
  `GROUND_H` (1088) with margin to spare.
- **Verified in-browser:** house and garden sit further left with the same
  gap between them; the tree line reaches the road's full length on both
  sides, still exactly mirrored; the debug footprint overlay (`G`) confirms
  no overlaps among the new tree rows, the relocated house/garden, or
  anything else.

**Phase 15 — "Show all" filled the canvas exactly, no more side bars.** You
reported the map looked "slightly cropped off" at full width. Root cause:
`VIEW_W:VIEW_H` (the canvas's own fixed shape) was 960:640 = 3:2 = 1.5, but
the map's shape (`GROUND_W:GROUND_H` = 1536:1088) reduces to 24:17 ≈ 1.412 —
a real mismatch, not a bug in the zoom math. At "show all" zoom, the map's
*height* filled the canvas exactly, but that same zoom made the map's
*width* fall about 60px short of the canvas's own width, leaving a black bar
down each side — visible in every "show all" screenshot taken up to this
point, just not previously flagged.

- **Fixed by matching the canvas's shape to the map's, not by resizing the
  map.** You suggested extending the road/map wider to fix it; the lower-
  risk fix was the other direction — `VIEW_H` changed from 640 to exactly
  680 (`960 × 17/24 = 680`, a clean number), making the canvas's own ratio
  24:17, identical to the map's. This touches one constant and a couple of
  comments in `index.html`; nothing in `world.js` or any object's
  coordinates needed to move, since nothing about the map itself changed —
  only the shape of the window we view it through.
- **Verified in-browser:** `ZOOM_SHOW_ALL` now computes to exactly `0.625`
  on *both* the width and height ratio (they're equal now, where before
  width was `0.625` and height was `0.588` — the smaller one, height, was
  what actually got used, leaving width's slack as the bars); a screenshot
  at "show all" shows grass reaching both edges of the canvas with no black
  bars; normal zoom in/out and the outer viewport's own responsive sizing
  (`fitViewport()`) still work correctly at the new ratio.

**Phase 16 — Building row realigned to Library's own front line.** You
noticed Town Hall, Media, Press Office, Command Center, the bench, and the
market were all slightly out of line with Library, and correctly traced it
to Library's own art. Confirmed by pixel-measuring where each sprite's
opaque content actually stops (the last opaque row, by column-count
majority, to ignore stray single-pixel props): Library's real building mass
ends 35px above its own sprite's bottom edge — its art includes a low front
wall and crates *below* the building itself — while every other building's
facade runs to within 0–7px of its own sprite's bottom edge. Since every
building's `walk` footprint bottom was pinned to the same `ROW_FRONT_Y`
regardless of this, the four other buildings' actual *visible* fronts sat
26–35px further forward (south) than Library's, even though their invisible
footprints all lined up.

- **Fix:** each affected building's `y` in `YARD_DEFS` (and its `bounds`,
  for whenever `YARDS_ENABLED` gets flipped back on) now subtracts a
  measured pull-back: Town Hall 34px, Media 35px, Press Office 28px,
  Command Center 35px. Library is untouched — it's the reference the others
  now match, not one more thing to adjust.
- **Bench and market follow their neighbor, not the shared line.** Both
  used to derive their `y` from `ROW_FRONT_Y` directly; now they read
  `FIRST_BUILDING.y + FIRST_BUILDING.h` (bench, next to Press Office) and
  `LAST_BUILDING.y + LAST_BUILDING.h` (market, next to Command Center) —
  so when a neighboring building's offset changes, these follow it
  automatically instead of silently drifting back out of line with it.
- **Verified in-browser:** all five buildings' bases read as one consistent
  line at a glance now, with Library's low wall/crates clearly its own
  distinct foreground element rather than making it look recessed; computed
  footprints for all five buildings checked directly (correct position and
  size, all consistent with the same math as before — only the `y` input
  changed); Press Office's door trigger still correctly enters its hallway
  after the shift.

**Phase 17 — Library's post actually trimmed (Phase 16 wasn't visible); a
real caching bug found and fixed; the house's stone base regenerated away.**

- **Library, round two.** You reported the post still extended past the
  porch after Phase 16's crop. It didn't, on disk — `library.png` was
  correctly 219×190. The browser (yours, and mine testing it) had cached the
  pre-crop 219×225 image and kept serving it after the file changed, which I
  confirmed directly: a fresh `Image()` fetch of the same URL still measured
  225 tall until the request was cache-busted, at which point it correctly
  measured 190. Not a repeat of the same bug — a genuinely separate class of
  problem (image caching, not the page-document caching Phase 15 hit).
- **Fixed at the root, not just for this one asset.** `loadImage()` in
  `index.html` now appends one timestamp (`ASSET_V`, computed once when the
  script loads) to every sprite URL it fetches. Every asset reloads fresh on
  every page load from now on; editing a sprite and reloading the page will
  never again silently show stale pixels, in this session or any future
  one. Confirmed the real fix by zooming in on the library post post-cache-
  bust: it now actually ends flush with the porch in the running game, not
  just in the source file.
- **House regenerated, not patched.** You questioned the stone-cobblestone
  ground baked into the house's base, sitting oddly on plain grass. Unlike
  the library's post (an isolated, cleanly-croppable element), the house's
  ground texture shared its color palette with the doorstep right next to
  it — sampled directly (ground `(163,144,132)`, step `(163,144,132)` at a
  different point, wall base `(146,114,90)`) and confirmed there's no color
  or region boundary that separates them safely. Regenerated instead, via
  `scripts/gen_house_v2.py`: same `create-1-direction-object` + style-image
  recipe as every other building, style-referenced off the *old* house
  cropped to exclude its bottom ~30% entirely (so there was no ground
  texture left in the reference for the model to copy back in), with the
  new description explicitly stating grass at the base, no patio, no
  cobblestone. One direct result, no review frames needed. New art is
  smaller (176×176 → cropped to 176×171, down from 193×213) and ends in a
  few grass tufts instead of a stone apron.
- **`PLAYER_HOUSE.w/h` and `walk` re-measured for the new sprite**, not
  reused from the old one's proportions — `w:176,h:171,
  walk:{x:8,y:110,w:160,h:53}` (footprint bottom at row 163, measured from
  where the new sprite's solid wall mass actually ends before the grass
  tufts thin out).
- **Verified in-browser:** the house renders sitting directly on grass with
  no stone ground visible; the debug footprint overlay (`G`) shows a
  correctly-sized box over the walls/door, not the grass tufts; entering the
  house (`enterBuilding('house')`) still goes straight to its one room
  (`view: 0`, no hallway) and back out correctly, confirming the door
  trigger — recomputed from the new `PLAYER_HOUSE` automatically via
  `doorTriggerForBox()` — still works after the resize.

**Phase 18 — Interiors get real art, starting with the hallways.** You're
happy with the exterior and asked to start on real interior visuals,
prioritizing Town Hall's and Press Office's hallway entrances first since
they're structurally identical (a hallway to N rooms) — and explicitly
wanted them to **stay** identical except for the title/door text. You also
asked whether agents would have any issue with that: no — agents read
`state.location`/`INTERIORS` data directly (room names, door indices), not
pixels, so text-identical room shells cost them nothing; it only matters for
a human glancing at a screenshot, and the title is always right there.

- **Two failed attempts at one "finished scene" background, before
  switching approach.** Asked `create-image-pixflux` for an empty room with
  a blank back wall (doors added separately in code); it ignored the
  negative prompting twice in a row, both times baking in its own door,
  window, and furniture despite explicit "NO door, NO window" instructions
  — the same class of failure text-negation hit earlier with the gate
  attempts, just worse here. Abandoned trying to get a single generation to
  correctly compose "room + N doors" and switched to the pattern already
  proven for the outdoor ground: generate a plain floor tile and a plain
  wall-band tile via `create-tileset` (reusing its `wang_0`/`wang_15` pure
  states exactly like the grass/dirt tiles), tile them in code, and place
  doors as a separately-stamped sprite at positions the *code* controls.
  Layout stayed controllable because it was never handed to the model in
  the first place.
- **New assets:** `tiles_interior/floor.png` + `tiles_interior/wall.png`
  (the two tileset states actually used; the other 14 blend states were
  deleted, unneeded for a plain rectangular room), and `interior_door.png`
  (one reusable door sprite, `create-image-pixflux`, stamped at every
  doorway — room doors and EXIT alike).
- **`renderInterior()` rewritten** to tile the floor/wall images across the
  room instead of flat `fillRect` colors, and stamp the door sprite at each
  entry in `layout.doors` instead of a colored rectangle.
- **Real bug: a taller wall band made the spawn-in point overlap the EXIT
  trigger.** Doors moved from a thin strip (`h:28`) into a full `WALL_H=48`
  band to fit the door art properly. `enterBuilding`/`enterHallway`/
  `enterRoom` still spawned the player at the old hardcoded `l.h - 60`,
  which cleared the *old* 28px-tall exit zone but not the new 48px one —
  entering a hallway immediately re-triggered `exitOutside()` before a
  frame was visible, confirmed directly (`state.location` read back as
  `'outside'` immediately after `enterBuilding()`, not the hallway). Fixed
  by deriving the spawn offset from `WALL_H` (`l.h - WALL_H - 20`) instead
  of a number that only happened to work for the old geometry.
- **Real bug: canvas text became unreadable at display scale ("EXIT"
  rendered as "FXTT").** The canvas has `image-rendering: pixelated` for
  crisp pixel-art sprites — that forces nearest-neighbor scaling on
  *everything* drawn to the canvas, including small antialiased
  `fillText()` strokes, when the canvas is scaled up to fill the viewport.
  Thin letter features (E's bottom bar, H's crossbar) landed on unlucky
  sample points and vanished. Confirmed by cropping the rendered output at
  both nearest-neighbor and smooth resampling — same garbled result either
  way, ruling out a screenshot artifact. Fixed at the architecture level:
  interior text is now a DOM overlay (`#interiorLabels`, absolutely
  positioned over `#viewport`, label positions set by percentage so they
  track the canvas's responsive size automatically) instead of
  canvas-drawn — real browser font rendering, immune to canvas pixel
  scaling by construction. `renderInterior()` calls `addLabel()` instead of
  `ctx.fillText()` for the title and every door label now.
- **Performance/stability fix found while building the above:** the first
  version of the label overlay rebuilt its DOM nodes every single
  `renderInterior()` call — 60 times a second, for text that only changes
  when you actually change rooms. Fixed with a `lastLabelKey` guard that
  only rebuilds when `state.location` actually changes (and resets when
  leaving to outside, so re-entering the same room later still rebuilds
  correctly rather than leaving stale/cleared labels in place).
- **Door labels spaced further apart** (`gap: 50`, up from `16`) once real
  text was in place and multi-word names like "Meeting Hall" were visibly
  crowding their neighbors at the tighter spacing.
- **Verified in-browser:** Town Hall's and Press Office's hallways are
  visually identical apart from the title and door text, confirmed side by
  side; a single-room building (Library's Reading Room) renders correctly
  with the same floor/wall/door art; DOM label text read back directly
  confirms correct spelling for every label; the EXIT-overlap bug and the
  garbled-text bug are both fixed and don't regress on re-entry.

**Phase 19 — Observatory: a dialog, not a room.** Prompted by comparing our
village against the reference screenshot directly (§ discussion above,
"what are the other buildings for") — the Observatory was one of two
buildings tried early on and dropped (art defects, "not the right village
fit"), but its *function* in the reference (a telescope dome next to a
rising performance chart) pointed at a real gap: nothing in our village
covers reviewing cost/performance over time. Rebuilt with that purpose,
and — per your explicit instruction — as a dialog on entry, not a room.

- **New building, placed in the other empty field.** `observatory.png`
  generated via the same `create-1-direction-object` + style-image recipe
  as every other building, style-referenced off `command.png` (closest
  existing building in material/tech feel) — one direct result, no review
  frames, single connected component (`clean_cutout.py` confirmed nothing
  to trim). Placed at `x:1125, y:HOUSE_ROW_Y` — the field east of the
  vertical road, mirroring the house/garden's field on the west side at the
  same baseline, since that was the only large empty field left on the map.
- **A dialog, not `INTERIORS`/a room.** `OBSERVATORY` gets a `DOOR_TRIGGERS`
  entry the same way every building does (`doorTriggerForBox()`), but is
  deliberately *not* added to `INTERIORS` — walking onto its trigger calls
  `openPricingDialog()` instead of `enterBuilding()`, so `updateOutside()`'s
  generic per-building trigger loop explicitly skips `'observatory'` and
  handles it separately, right after.
- **The dialog itself:** a centered modal panel (`#dialog`, DOM overlay
  over the canvas — same reasoning as the Phase 18 interior-label fix, real
  text stays crisp regardless of canvas scaling) showing a rate card for
  three model tiers (Cheap/Standard/Premium — idle, normal work, escalated
  work) with illustrative per-token costs, explicitly labeled illustrative
  since there are no live agents or usage to report yet. Movement pauses
  while it's open (`state.dialogOpen` short-circuits `update()`); `E`
  closes it.
- **Reopen-suppression, same shape as two earlier bugs this session.**
  Closing the dialog while still standing on the trigger zone would
  otherwise reopen it the very next frame (the same class of problem as the
  EXIT-trigger overlap and the stuck-key-driven re-triggers seen earlier).
  Fixed with `observatorySuppressed`, set on close and cleared only once
  `aabbOverlap` with the trigger goes false — closing requires actually
  stepping off the zone before it can fire again.
- **Verified in-browser:** the dialog opens on approach, reads correctly
  (title, rate card, all three tiers, close hint — confirmed via direct
  state/DOM inspection, not just a screenshot); closing with `E` works and
  does *not* immediately reopen while still standing on the trigger;
  moving off and back onto the trigger reopens it correctly; the debug
  footprint overlay (`G`) confirms the building's footprint is
  correctly sized and doesn't overlap the tree columns or anything else in
  its field.

**Phase 20 — Phone booth: a physical anchor for "call a sleeping agent."**
Replaces the bench by Press Office, per your call — same spot, same
front-line alignment, different purpose. This is the "Sleep / wake / call"
locked requirement (§2) getting a physical presence for the first time,
following the exact "dialog, not a room" shape the Observatory established
one phase earlier.

- **New prop:** `phonebooth.png`, a classic red London telephone booth,
  `create-1-direction-object` with no style reference this time (a red
  phone booth doesn't need to match the stone/timber buildings — it's meant
  to read as a distinct landmark prop). Came back in `review` status (16
  candidates, default 64×64 bucket); picked frame 9 by eye for the
  cleanest, most iconic read, cropped to bbox (28×59) and cleaned. Used the
  locally-downloaded review frame directly rather than round-tripping
  through `select-frames` — the file was already final, no reason to also
  update PixelLab's own bookkeeping for it.
- **Real bug caught before it shipped: `doorTriggerForBox()` ignored
  `scale`.** Every object that had used it so far (`PLAYER_HOUSE`, every
  `YARD_DEFS` building, `OBSERVATORY`) happened to have no `scale`, so the
  bug was invisible. `PHONE_BOOTH` is scaled 1.4× to read at a reasonable
  size, and was the first to expose it — the trigger zone would have landed
  in the wrong place, offset from the actual (scaled) footprint. Fixed by
  computing `sx`/`sy` the same way `scalesOf()` does and applying them to
  `walk.x/y/w/h` before deriving the trigger, rather than using the raw
  unscaled values.
- **A dialog with real (if canned) two-way interaction, not just a
  read-only table.** You asked specifically that agents "be able to talk"
  through it, not just place a call — so `#callDialog` has an actual chat
  log and text input (`wireCallForm()`), not a static panel. Submitting a
  message appends it to the log and appends one of three canned replies
  (`CALL_REPLIES`) — honest placeholder content, same as the pricing
  dialog's rate card, since there's no roster or backend to actually
  connect to yet.
- **Real bug caught before it shipped: "E" can't close a dialog that has a
  text input.** The Observatory's pricing dialog has no typing, so "E"
  closing it is fine; reusing that exact key for the phone booth would mean
  typing the letter "e" in a chat message also closes the dialog out from
  under you. Gave the call dialog `Escape` instead (kept as a synonym for
  the pricing dialog too), and made the keydown handler skip touching
  `state.keys` *at all* while any dialog is open — otherwise typing
  movement letters ("w", "a", "s", "d") into a chat message would leak into
  `state.keys` and could leave a stale movement key active after closing,
  the same shape of bug as the session's earlier stuck-key issues, just
  self-inflicted this time instead of an environment artifact.
- **Shared dialog styling, not duplicated.** The Observatory's and phone
  booth's dialogs are visually the same shape (title, body, close hint) —
  refactored the CSS from `#dialog`-scoped rules to a shared `.dialogOverlay`
  class both `#dialog` and `#callDialog` use, so a future third dialog
  starts from the same look for free instead of copy-pasting the block again.
- **Verified in-browser:** the dialog opens on approach and reads correctly;
  submitting a chat message appends both the user's line and a canned reply
  to the log; `Escape` closes it and does not immediately reopen while
  still standing on the trigger (same suppression pattern as the
  Observatory); moving off and back on reopens it correctly; directly
  confirmed that typing `w`/`a`/`s`/`d`/`e` into the chat input while the
  dialog is open leaves `state.keys` empty afterward.

**Phase 21 — Furnished rooms.** You looked at several of Tristen's interior
screenshots directly and pointed at what ours was missing: real furniture,
and cheap per-room variety via a shared set plus one or two signature items
(the reference's own bedroom-hallway shot does exactly this — same bed/
nightstand/rug in every room, one unique item each). Approved starting
there rather than bespoke art per room.

- **Six shared furniture pieces**, `scripts/gen_furniture.py`, same
  `create-1-direction-object` recipe as every other prop, no style
  reference (small furniture doesn't need to match the stone/timber
  buildings the way the phone booth didn't either): `rug`, `desk`,
  `bookshelf`, `bed`, `lamp_interior`, `crate_stack`. Five came back in
  `review` (16 candidates each, default 64×64 bucket) on the first pass;
  `crate_stack`'s job exceeded the script's 180s poll timeout on that same
  pass — not a generation failure, just a slower job — re-run alone with
  360s and completed normally. Picked one candidate per item by eye,
  cropped to bbox, cleaned.
- **Data-driven placement, not hand-typed coordinates per room.** Every
  individual room is the same fixed size (`ROOM_W`/`ROOM_H` = 320×220,
  `WALL_H` = 48 top/bottom), so `world.js` defines three reusable "back
  wall" slots (`BACK_SLOT_X`, left/center/right) plus one rug slot, and
  `ROOM_FURNITURE` (keyed `"building:roomIndex"`) just lists which
  item(s) go in which slot per room — `backSlotItem('bookshelf', 0)` reads
  as "a bookshelf in the left slot," not a set of pixel coordinates.
  `furnitureFor(building, i)` returns the rug plus that room's list; every
  room gets the rug automatically, so it never has to be listed by hand.
- **Ten rooms furnished**, 1-2 signature items each: Meeting Hall
  (bookshelf+desk), Mailroom (crate_stack), Archive (bookshelf+crate_stack),
  Writers Room (desk+lamp), Editing Room (desk+bookshelf), Print Room
  (crate_stack+lamp), Studio (desk+lamp), Reading Room (bookshelf+lamp),
  Control Room (desk+lamp), Bedroom (bed+lamp).
- **Real bug caught before it shipped: `WALL_H` was declared in the wrong
  file.** It lived in `index.html`, but `world.js` loads first and the new
  furniture-slot math needed it too (`BACK_SLOT_Y = WALL_H + 6`) — a
  `ReferenceError` at load time. Moved the single declaration into
  `world.js` next to `ROOM_W`/`ROOM_H` (the other room-geometry constants,
  which is where it conceptually belonged anyway) and had `index.html`
  read it from there instead of also declaring it.
- **Real bug caught before it shipped: the rug would have trapped the
  player.** First pass sized the rug at `scale: 1.9` and centered it in the
  room — computed out to a 91px-tall box whose bottom edge (221px) fell
  *past* the room's own height (220px) and deep into the bottom wall band,
  overlapping both the EXIT door zone and the player's own spawn-in point.
  Worse, furniture blocks movement by default, so the rug would have
  doubled as a giant invisible wall across the room's only path to the
  door. Fixed two ways: reduced to `scale: 1.5` and repositioned to fit
  inside the floor band with margin, and — the more important fix —
  rugs don't block at all (`blocking: false` on `rugItem()`,
  `blockedAtInterior()` skips any furniture item with that flag). You walk
  on a rug, not into it; every other item here still blocks normally.
- **Furniture y-sorts with the player**, same feet-position rule as the
  outdoor world (`renderInterior()` now builds an `entities` list from
  `layout.furniture` plus the player and sorts by `sortY` before drawing),
  so walking in front of or behind a bookshelf or bed reads correctly
  instead of the player always drawing on top.
- **Verified in-browser:** Meeting Hall (bookshelf + desk + rug), Bedroom
  (bed + lamp + rug), and Mailroom (crate_stack + rug) all render correctly
  with visibly different furniture combinations; walking into the
  bookshelf and the desk both stop the player at the furniture's edge;
  walking across the rug does not (confirmed by comparing actual distance
  traveled against expected unobstructed movement over the same number of
  frames); the earlier over-height rug never shipped, since the bug was
  caught and fixed before the first in-browser test.

**Phase 22 — Bigger rooms for multi-agent occupancy.** You pointed out the
rooms were sized for one visitor, but many agents will occupy the same room
at once (the Round Table reference makes the same point — 10 seats around
one table). Rooms needed real headroom, not just a cosmetic resize.

- **`ROOM_W`/`ROOM_H` grown from 320×220 to 760×520** (`world.js`), comfortably
  inside the 960×680 canvas (`VIEW_W`/`VIEW_H`) with margin on every side, so
  no interior camera/scroll system is needed yet. `BACK_SLOT_X` (the three
  back-wall furniture slots) spread from `[55, 160, 265]` to `[110, 380,
  650]` to use the extra width — same three semantic slots (left/center/
  right), so `ROOM_FURNITURE`'s existing slot-index references needed no
  changes. `FURNITURE_DIMS.rug.scale` bumped from 1.5 to 3.5 to keep the rug
  proportional in the larger floor.
- **Real bug: `roomLayout()` had its own hardcoded 320×220.** `index.html`'s
  `roomLayout()` never actually read `ROOM_W`/`ROOM_H` from `world.js` — it
  had its own literal `const w = 320, h = 220;`, left over from before those
  constants existed. `currentInteriorLayout()` kept reporting the old size
  no matter what `world.js` said. Fixed by pointing it at the shared
  constants (`const w = ROOM_W, h = ROOM_H;`).
- **Real bug: the bigger rug covered the back-wall furniture.** `rugItem()`
  positioned the rug at a fixed `WALL_H + 22`, tuned for the old 220-tall
  room. At the new 3.5 scale the rug's box (up to ~168px tall) reached up
  into the same vertical space as back-wall items like the bed, and since
  furniture draws in y-sort order by bottom edge, the rug — sorting later —
  painted over the bottom of the bed, leaving only the pillow visible above
  the wall/floor seam. Fixed by centering the rug in the floor band instead
  (`WALL_H` to `ROOM_H - WALL_H`), well clear of the back-wall slots' ~120px
  max height regardless of room size.
- **Verified in-browser** (after working around the known HTML-document
  caching issue with a `?v=` query bump — same class of bug as the
  already-fixed image caching, still unfixed at the code level for the HTML
  document itself): Meeting Hall and Bedroom both render at 760×520 with no
  clipping against the canvas; the bed is now fully visible above the rug;
  collision re-confirmed at the new scale by scripted movement tests —
  walking into the bookshelf stops the player at its edge (x halts at
  64.3, short of the bookshelf's 85.3 left edge), walking across the rug
  does not (player crosses the full room, reaching x:740).

**Phase 23 — Whole-scene hallway art for Town Hall / Press Office.** You
gave a reference screenshot (a stone corridor with arched wooden doors, a
cream baseboard trim, and a brick floor) and asked for a single generated
hallway image sized like the other rooms, replacing the procedural tiled
hallway for these two buildings specifically.

- **Real prior history worth knowing**: this exact idea -- one text-to-
  image call laying out a whole empty room with N doors baked in -- was
  tried before and abandoned (see `gen_hallway_tiles.py`'s own docstring:
  it kept adding extra baked-in doors/windows/furniture despite negative
  prompting), which is why the hallway rendered from tiled floor+wall
  textures composed in code up to this point. Tried again anyway, since
  this session's techniques (an explicit reference image, and the strict
  wall-band/floor-band framing language that worked for the village
  exterior and the World 2 house interior) didn't exist at the time of
  that first failure -- and this time it worked.
- **`scripts/gen_hallway_v2.py`**, `generate-image-v2`, sized to the API's
  own max for the room's 760:520 aspect ratio (632x424, confirmed by
  probing the endpoint the same way as every other whole-scene generation
  this session), then drawn stretched to fill the actual 760x520 room.
  Two passes: the first came out with a bottom-wall door and a decorative
  plant you didn't want, plus an uneven brick floor; the second dropped
  the exit door and the plant, and asked explicitly for a "perfectly
  uniform, seamlessly repeating" floor pattern. Confirmed: exactly 3 doors,
  all on the top band, evenly spaced with clear plain wall at both ends;
  bottom band completely plain (Pokemon-style -- an exit doesn't show a
  door, since you're already there); no people.
- **Door triggers measured directly from the generated pixels**, not
  guessed: scanned a horizontal slice through the door row for dark
  (wood-colored) columns, found door centers at 25.3%, 49.9%, and 75.2% of
  the image width -- `HALLWAY_DOOR_CENTER_FRAC` in `index.html`. This
  replaces the generic evenly-gapped `doorRow()` math for hallways only
  (`hallwayLayout()` now builds its own door list from these fractions);
  `roomLayout()` and every other interior are untouched.
- **`hallwayLayout()` resized to `ROOM_W`x`ROOM_H`** (760x520, same as
  every individual room) -- it was previously its own smaller fixed size
  (420x260), per your call that it should match the other rooms.
  `renderInterior()` now branches on a new `layout.isHallway` flag: draws
  `hallway_v2.png` stretched to fill the room instead of the tiled floor/
  wall loop, and skips the `interiorDoorImg` sprite-drawing loop entirely
  (the 3 room doors are baked into the art; the exit intentionally has no
  door sprite at all) -- door name/EXIT labels still render normally
  through the existing separate label loop, unaffected.
- **Verified in-browser**: both Town Hall and Press Office hallways render
  at the new size with labels correctly aligned over the baked-in doors;
  walking onto each door trigger enters the right room (confirmed via
  `state.location` after positioning onto each trigger, with
  `state.keys.clear()` first -- the session's well-documented stuck-key
  artifact caused one false reading during this exact test, resolved by
  clearing keys before repositioning); the EXIT trigger correctly returns
  outside with no door ever drawn there.

**Experiment — whole-scene village generation (not part of the game).**
You found a wider thumbnail of Tristen's village (more buildings than the
interior screenshots) and asked whether he might have generated the whole
map in one shot via an image model, rather than assembling it building-by-
building the way we have been. Explored this as a side experiment, for a
possible video, explicitly NOT for import into the actual game.

- **Found the right endpoint by reading PixelLab's own OpenAPI spec**
  (`/v2/openapi.json`, fetched directly — not previously used this
  session): `generate-image-v2`, a whole-scene "Pro" generator distinct
  from every per-object endpoint used so far. Takes a text `description`
  plus up to 4 `reference_images` (subject/composition guidance) and an
  optional `style_image`. Probed its real size limit empirically (a few
  free validation-only calls, since a 400 response never starts a
  billable job): the cap is a fixed **~262,000–268,000 total pixels**
  regardless of aspect ratio (688×384 landscape, 512×512 square, etc. all
  land at the same ceiling) — a hard API limit, not something more spend
  unlocks per call.
- **v1** (`scripts/gen_village_reference.py`): fed Tristen's thumbnail as
  a reference image with a description matching its own composition
  (central plaza, fountain, radial paths) and an explicit instruction to
  omit all people/characters. Result was strong on mood and detail but,
  as you pointed out, too close to a recolor of the reference's exact
  layout skeleton to feel like "ours."
- **v2** (`gen_village_reference_v2.py`): kept the reference image but
  told the model explicitly not to copy its layout, and swapped the
  underlying composition to something structurally different — a
  riverside main street with footbridges instead of a radial plaza, an
  autumn palette instead of summer green, different materials/colors per
  building. Result read as clearly its own scene. Verified no people
  anywhere (a camera-tripod silhouette at the media studio was double-
  checked, not a figure).
- **Real gaps found by manual audit of v2**, caught only by zooming into
  every building individually — the model doesn't verify its own text
  prompt: (1) two buildings (the library, the observatory) were fully
  enclosed by their own yard walls with no gate or path touching them at
  all — not just hard to see, genuinely unreachable as drawn; (2) the
  requested post office never rendered distinctly — it collapsed into a
  generic timber cottage with no mailbox/cart/signage.
- **v3** (`gen_village_reference_v3.py`): fixed both gaps by making the
  path-connectivity requirement explicit and load-bearing in the prompt
  ("every building's yard has an OPEN gate... no isolated buildings, no
  dead ends") and by making the post office's materials/props explicitly
  different from the house's ("STONE-AND-PLASTER, not timber... red
  pillar postbox... delivery cart"). Both gaps closed — audited the same
  way, building by building. But this pass drifted into a 3/4 isometric
  camera angle (visible building side walls, roofs receding at an angle)
  instead of the flat top-down view every other asset in this project
  uses — you caught this immediately, correctly reasoning that an
  isometric building doesn't line up with a game where characters only
  move along the four cardinal directions.
- **v4** (`gen_village_reference_v4.py`): same content fixes as v3, with
  the flat-top-down camera requirement moved to the very first sentence
  and repeated at the end, since `generate-image-v2` has no explicit
  camera/isometric parameter (checked the schema — description/reference/
  style only) so framing can only be enforced through prompt wording, and
  v3 suggests it can get crowded out by competing instructions. Result:
  correct flat top-down camera throughout, all ten buildings present and
  individually distinct (town hall, press office, media studio, library,
  house, weather station, post office, phone booth, bank, observatory),
  every one with a verified path connection, zero people anywhere —
  confirmed by cropping and zooming into each building individually
  rather than trusting the thumbnail. Files (all under
  `assets/village/reference_experiment/`, never copied into `web/`):
  `village_generated_v4.png` (native 688×384) and `village_generated_v4_3x.png`
  (2064×1152, a clean nearest-neighbor upscale for video use, since the
  API's pixel ceiling can't be bought past directly).
- **Tradeoffs discussion, whole-scene vs. building-by-building** (your
  ask, not yet acted on either way — recorded here for reference): whole-
  scene generation is fast and gives cohesive organic terrain (rivers,
  seasons, lighting) our engine has no system for at all, which makes it
  good for mood boards / concept art / this video. But it has no
  reliability guarantee (silently dropped the post office once, produced
  unreachable buildings once — both had to be caught by manual per-
  building audit, not trusted from the prompt), no composability (fixing
  one building means regenerating the whole scene and reshuffling
  everything else), and no collision/footprint/interior data at all. The
  actual game village stays building-by-building for exactly the reasons
  every phase before this one demonstrates (independent footprints, door
  triggers, editable rooms).
- **Open thread, not started:** you floated that Tristen may have
  generated his *interiors* the same whole-scene way too (rather than the
  furniture-piece-by-piece approach we used in Phase 21), but said to
  wait and revisit — no action taken on this yet.

**Experiment — World 2, a standalone second map (not part of the game).**
You asked to map walkable/blocked points for the v4 village render and build
a second world from it. Mid-build you stopped me and clarified a hard
requirement: World 2 must be **completely separate in all ways** from the
main game, not a toggle inside it -- separate HTML page, separate script,
separate asset folder, no shared files, no shared runtime. (I'd initially
wired it into `web/index.html`/`world.js` via a location-branch and a "press
2" keybind, matching an earlier answer of yours about switching mechanism;
you overrode that once you saw it and I reverted every change back out of
`web/` before starting over.) Lives entirely at `world2/` (sibling to
`web/`), never touches anything under `web/`:

- `world2/index.html` -- a full standalone page: own canvas, own copy of the
  movement/camera/collision engine (input handling, sliding collision, zoom,
  the 'g' footprint-debug overlay), no `<script>` reference to anything in
  `web/`.
- `world2/world2.js` -- own data: background sprite path, world dimensions,
  spawn point, and `BLOCKERS`, a flat list of hand-mapped rectangles.
- `world2/assets/` -- own copies of the four player-direction sprites and
  the background image (`village_background.png`, copied from
  `assets/village/reference_experiment/village_generated_v4.png`). Physically
  separate files from `web/assets/`, not shared paths or symlinks.
- Buildings are solid obstacles only, no doors/interiors -- your call, to
  keep this pass scoped to walkability.

**Mapping the collision, since there's no per-object footprint to derive it
from here (one baked image, not separate building sprites):** overlaid a
labeled coordinate grid on the native 688x384 image (a throwaway script, not
kept) and read off a bounding rectangle for each of the ten buildings by
eye, plus the river as four connected segments -- deliberately leaving two
gaps unblocked (~x70-100,y150-195 near the windmill; ~x345-395,y225-265 at
the main arched bridge) so the two footbridges stay walkable crossings
instead of being blocked like the rest of the water. Verified every
rectangle two ways: programmatically (`blockedAt()` called directly against
a point in the center of each of the 10 buildings, both bridge gaps, mid-
river, and the open path -- all returned the expected true/false) and by an
actual sliding-collision walk test (placed the player on open ground and
held "north" toward the town hall; it slid to a stop exactly at the wall,
not before or through it).

**Real issue found after the first look, not from the mapping itself: the
player sprite read as oversized.** Your catch. Cause: a whole-scene render
only gives each building a small slice of the fixed-size canvas (the town
hall here is ~155px wide total), while every World 1 building was generated
as its own sprite at much higher native resolution -- so the same fixed-
pixel player sprite (68x68) that looks right next to a World 1 building
overwhelms one of these. Fixed by introducing a `SCALE` constant (`world2.js`)
that stretches the whole map -- background, every blocker, spawn point --
up in world-space (2x), leaving the player's own pixel size untouched. That
corrects the actual proportion, not just the on-screen zoom (re-verified
with the same two-part check above after rescaling).

**Discussed and deferred, not built:** whether an ML/segmentation approach
could map the walkable area automatically instead of by hand. Take: for a
one-off map, it wouldn't remove the audit step (a segmentation model
misjudges edges/shadows the same way eyeballing a grid can, so every
building still needs a manual check either way) -- only worth it if this
becomes a repeated pipeline across many generated maps, not a single map.

**World 2 follow-up — grid-based collision, CLIPSeg auto-mapping, and a
hand-correction editor.** Three things you raised after the first version:
the hand-mapped rectangles blocked both footbridges entirely (a real bug --
you couldn't cross either river crossing); the player sprite read as
oversized next to these buildings; and you pushed back hard, more than
once, on my claim that automated segmentation "wasn't worth it for a one-
off map" -- asking specifically whether an existing, zero-training Python/
HuggingFace model could do this. You were right to push: I hadn't actually
tried one.

- **Player scale fixed**: added a `SCALE` constant (`world2/world2.js`,
  currently 2) that stretches the whole map -- background, collision data,
  spawn point -- up in world-space, leaving the player's own sprite pixels
  untouched. Root cause: a whole-scene render gives each building only a
  small slice of a fixed-size canvas (~155px for the town hall here) vs.
  every World 1 building being generated as its own sprite at much higher
  native resolution, so the same fixed-pixel player sprite overwhelmed it.
  This fixes the actual proportion, not just the on-screen zoom.
- **Tried CLIPSeg** (`CIDAS/clipseg-rd64-refined`, HuggingFace
  `transformers`, already installed locally) -- a zero-shot, text-prompted
  segmentation model, no training or fine-tuning. Prompted "a building" and
  "water or a river" directly against the village image and it worked well
  enough to use: both produced heatmaps that meaningfully tracked the real
  building/river shapes, including correctly leaving both footbridges
  unmarked -- which the hand-mapped rectangles had gotten wrong. "Path" and
  "grass" prompts were also tried and were too weak/noisy to be usable on
  this autumn-palette pixel art (everything ochre-toned reads similarly to
  the model) -- not needed anyway, since the engine already defaults
  everything non-blocked to walkable.
- **Real bug caught and fixed before trusting any of it: aspect-ratio
  distortion.** First attempt resized the 688x384 image to 704x768 for the
  model -- a non-uniform stretch -- which would have silently misaligned
  every heatmap coordinate against the source image. Fixed by letterboxing
  (resize preserving aspect ratio, pad to square) before feeding the model,
  then mapping heatmap coordinates back through the same letterbox math.
- **Collision changed from hand-typed rectangles to a per-cell grid**
  (`world2/collision_grid.json`, 8px-per-cell, 86x48 cells) -- this is also
  a more literal match for what you originally asked for ("map the points
  where we should and shouldn't be able to walk on the grid"). Generated by
  `scripts/gen_collision_grid.py` (threshold 0.4 on the max of the two
  heatmaps), with one manual patch for the phone booth, which is too small
  for the model to detect confidently on its own.
- **Verified two ways**, same discipline as every other phase: programmatic
  point checks against `blockedAt()` for the center of all 10 buildings,
  both bridge gaps, mid-river, and open path; and an actual sliding-
  collision walk test across the main bridge (player moved from y:440 to
  y:752, straight through what used to be a hand-mapped blocking rectangle
  covering that exact spot).
- **Real gaps found in the auto-generated mask by this same verification**:
  soft/blurry heatmap edges leave some real holes -- part of the windmill,
  the post office building, and the bank/observatory area aren't fully
  covered even though the model roughly found them. This is exactly what
  the editor below is for.
- **Built `world2/editor.html`** (plus `world2/serve.py`, a tiny custom dev
  server replacing plain `python -m http.server`) -- loads the current
  `collision_grid.json` and the background image, lets you paint corrections
  directly with the mouse (Block/Erase mode buttons, adjustable brush size,
  undo, right-click as a quick one-off erase), and a Save button that POSTs
  straight to `/save` and writes `collision_grid.json` to disk -- no
  download-then-manually-replace-the-file step. Re-open `world2/index.html`
  after saving to play with the correction. Re-running
  `gen_collision_grid.py` overwrites hand corrections, so don't re-run it
  after editing by hand without expecting to redo them.
- **Take on the ML question, revised**: zero-shot segmentation is
  genuinely useful here, more than my first answer gave it credit for --
  it got the big semantic categories (building, water) and the bridges
  right in one shot with no training. What it doesn't remove is the
  correction step: soft mask edges still need a human pass, which is what
  the editor is for. The realistic pipeline for a map like this is auto
  first pass + manual correction, not one or the other.

**Editor follow-up — fixed viewport, zoom, pan.** The first `editor.html`
sized its canvas to the whole map at a fixed display scale, so it grew
larger than the browser window and you couldn't see the whole thing without
scrolling. Rebuilt around a fixed-size viewport (960x640, the same idea as
the game's own bounded canvas) with a real camera: `zoom`/`camX`/`camY`
state, scroll-wheel zoom centered on the cursor, +/-/Show-all buttons, and a
dedicated Pan mode (third button alongside Block/Erase) for click-drag
panning while zoomed in. Explicitly an editing-only convenience -- the
actual game canvas (`index.html`) has zoom but no pan, and that's staying
as-is. Verified the camera math directly (zoom-in centered correctly on the
viewport center, a simulated pan drag moved `camX`/`camY` by the expected
amount, and a paint click correctly converted a canvas-local point through
the current pan/zoom back to the right grid cell) rather than trusting it
by eye alone.

**Phase 24 — Building/room functional redesign.** You opened with "let's
talk about every building and object we can communicate with in our
village. I need to decide what will live in each, functionality, etc." — a
full re-litigation of §3's room table, most of which dated from Phase
10/13 and had drifted from what actually made sense once interiors were
real. Resolved through extended back-and-forth, not decided unilaterally:

- **Town Hall loses every physical room.** Meeting Hall, Mailroom,
  Archive, and Roster hall were all folded into Town Hall's interior as of
  Phase 10 — you questioned whether Meeting Hall needed an image at all,
  since you're the only one who ever walks into it and the other agents
  are "there whether they are doing something or not." Reframed as a
  chat-interface / dialog, not a walk-in room — design details deferred on
  your explicit call ("we'll discuss town hall later being it will be a
  chat interface"). Mailroom and Archive relocated (below); Roster hall
  and Rest/Training ground remain conceptually homeless pending the Town
  Hall redesign.
- **Mailroom eliminated, absorbed into Post Office.** You asked directly:
  "why could the post office not be the same then? Could we not completely
  eliminate the need for the mailroom if we have the post office?" Post
  Office becomes the sole physical home for mail, role-gated: you
  (admin/human) see every agent's mail plus admin mail, each agent sees
  only their own inbox.
- **Archive eliminated, absorbed into Library.** Same interaction
  mechanic as originally planned for both (interact with a bookshelf, pull
  information) — differentiated only by scope: Library is current/
  role-scoped working files, Archive was historical/completed-work
  records. Since the mechanic was identical, keeping them as two rooms had
  no functional payoff.
- **Observatory becomes Research Center.** Distinct from the Bank (which
  you considered merging it with, since both involve differing
  services/investigation), doing active investigative work — visually
  identical to the Work Room by your explicit choice, rather than getting
  its own bespoke interior.
- **Bank finalized as three identical teller stations**, not
  differentiated services — "each teller can perform the same actions,"
  i.e. capacity, not specialization. (Room art itself is Phase 25, below.)
- **Weather Station confirmed as literal internet/web access**, gated
  behind that room the way §3 originally scoped a "computer lab."
- **Media renamed Studio**, public-facing broadcast content — considered
  several unique-interior concepts (see Phase 25) before you settled on
  reusing the Work Room art directly.
- **Command Center's interior renamed Control Room** — finally gives the
  "Restricted" room category (§3 row 4, `lockeddoor.png` built-but-unplaced
  since Phase 13) an actual home: the room-gated, elevated-privilege space.
- **Press Office collapses from its 3-room hallway (Writers/Editing/
  Print, Phase 10) to a single, general-purpose Work Room** — not bound to
  a specific task type, since the original distinction wasn't pulling
  weight day to day.
- **House and Phone Booth unchanged** — dialog-only, as before.
- **Meeting-room follow-up, added after the fact:** any agent (not just
  you/admin) can use the eventual Town Hall meeting mechanic, with preset
  attendee groups plus a custom attendee-selection path — noted for
  whenever Town Hall's chat-interface design actually gets built.

Net effect: eight buildings end up with real interiors (down from the ad
hoc mix of hallways/single-rooms after Phase 10/13), five of which share
one visual layout (Work Room — reused for Weather Station, Studio, Control
Room, and Research Center), and two get genuinely unique art (Bank, Post
Office). §3's table has been updated to match; Town Hall's remaining
un-homed categories (Rest area, Roster hall, Training ground) stay open
until its chat-interface design happens.

**Phase 25 — Interior art for the eight resolved rooms.** Following your
explicit process ("start identifying what each of these rooms should look
like... once you are happy with the prompts, you start designing each.
Then review each and determine if they meet qualifications. If you are not
happy, generate a new image"), generated whole-scene interior art the same
way Phase 23's hallways were: `generate-image-v2`, one room per building,
sized to the API's per-aspect-ratio pixel ceiling (632x424 confirmed again
for this room shape, same discovery pattern as the hallway and
village-scene work).

- **Reusable "hard rules" template**, refined through the Work Room's own
  iteration and copied into every later room script's prompt: strict
  top-down, dollhouse-with-roof-removed framing; exactly ONE wall band
  across the top (~10% height, plain, no door/window ever drawn); floor
  fills the entire rest of the image to the bottom edge (the unseen
  entrance is implicitly there, Pokemon-style — no second wall band, no
  bottom door); absolutely no chairs/stools/seats anywhere; no people/
  NPCs/animals; and — the one hard lesson that came from a real design
  flaw, not a style call — **exactly one interactive object/furniture type
  per room**. That last rule came directly from the Studio's first draft:
  you asked "I question how my agents will be interacting with these
  items... what will they be doing in this space," correctly spotting that
  a stage/camera setup plus a separate control desk gave two competing
  interaction points instead of one clear one.
- **Four genuinely distinct rooms built**: Work Room (six 1990s
  CRT-monitor desks, two rows of three), Library (six bookshelves), Bank
  (marble/gold floor, three identical teller counters with short
  chain-and-post queue dividers), Post Office (one full-width wall of PO
  box cubbyholes). Four more (Research Center, Weather Station, Studio,
  Control Room) reuse the Work Room image directly, per Phase 24's
  decisions above.
- **Platform limits hit and worked around, in order**: PixelLab's
  `/inpaint-v3` needs a subscription tier this account doesn't have
  (confirmed via a direct `Tier 2 is required` API error, after already
  downscaling to fit its 512x512 cap); the older `/inpaint` caps at
  200x200, too small to be useful. Landed on your own suggested fallback
  instead — `generate-image-v2` with the room's *current* image as its own
  reference, explicit "this IS the current room, reproduce it closely,
  ONLY change: X" wording. Not pixel-perfect, but close enough to work
  every time it was tried.
- **Two real bugs caught mid-generation, not cosmetic nitpicks:** (1) a
  generation job silently stalled at 57.7% for several minutes, then the
  API itself reported a server-side failure ("The GPU worker running this
  generation dropped it") — fixed by just retrying, not by waiting longer;
  (2) the retry's result came back with an unrequested grid/tile-seam
  artifact baked across the whole image — root-caused to regenerating from
  an already-flawed reference, fixed by finding an untouched clean sibling
  copy (`room_research.png`, a pre-edit duplicate) to reference instead,
  plus an explicit negative instruction against grid lines. Confirmed
  clean afterward and reused for every Work-Room-sharing room.
- **Manual pixel-level compositing for Library and Post Office**, at your
  request, rather than trusting the model with exact furniture placement:
  generated an "empty shell" (bare wall+floor, no furniture) alongside the
  populated version, then cut individual furniture pieces out of the
  populated image and recomposited them onto the empty shell at
  hand-chosen positions. Two real technique failures before landing on one
  that worked: PixelLab's `/remove-background` over-removed the desk's own
  tabletop; both a color-list classifier and a border-seeded flood-fill
  leaked through shading colors shared between the furniture and the
  floor. The fix that actually worked was a purely geometric alpha mask
  (per-row-band column ranges, no color logic at all) — sidesteps the
  color-ambiguity problem entirely. You caught one real defect in an early
  bookshelf crop this way too (floor bleeding into the crop's edge
  pixels), fixed by re-scanning for the exact outline-transition row
  rather than an approximate visual boundary.
- **One redesign explored and explicitly rejected**: shrinking the Work
  Room's desks to fit a 2x4 grid (8 desks) instead of 2x3 (6). Built the
  full geometric-cutout pipeline for it, then you called it off outright
  ("I don't think that we works. I think we leave it at six terminals per
  room") — work archived to `assets/village/room_design_ideas/`, not
  deleted, matching this project's standing practice of keeping rejected
  concepts rather than discarding them.
- Final assets: `web/assets/village/room_workroom.png`, `room_library.png`,
  `room_bank.png`, `room_postoffice.png` (the four unique rooms) plus
  `room_research.png`, `room_weather.png`, `room_studio.png`,
  `room_control.png` (identical copies of the Work Room art, kept as
  separate files per building rather than one shared path, matching how
  World 1 already keeps one image per building).

**Experiment — World 2 gets real interiors.** Per your instruction ("place
the images we're going to use in the directory for world 2... wire them
all in... For the rooms that have an active room we'll walk into, please
configure them. I also need for the collision map to have a way to change
between images... the image shared by the rooms with all of the
workstations should have a single collision map for now"), World 2 goes
from outdoor-only to having real walk-in interiors for its seven
active-room buildings, still keeping the hard separation-from-`web/` rule
from the original World 2 experiment above.

- `world2/assets/rooms/` — copies of the four Phase 25 room images
  (`room_workstations.png`, `room_library.png`, `room_bank.png`,
  `room_postoffice.png`).
- `world2/rooms.js` (new) — the room registry: `ROOMS` maps each of the 7
  active-room buildings to an image + a collision-set name + a label;
  `ROOM_COLLISIONS` holds four rectangle-lists (hand-measured against the
  actual generated pixels, not guessed) — critically, the four
  Work-Room-sharing buildings (Press Office, Media, Weather Station,
  Observatory) all point at the same `workstations` collision entry, per
  your explicit requirement, so editing one layout updates all four rooms
  at once instead of needing four synced copies.
- **Collision approach: rectangle lists, not a per-cell grid**, unlike the
  outdoor map's `collision_grid.json`. Deliberate difference — these are
  small, furniture-based layouts (a handful of objects each) where
  hand-typed rectangles are simpler than painting cells, and the outdoor
  map's grid approach was solving a different problem (an organic,
  hard-to-describe building/river shape with no discrete objects).
- **`world2/index.html` extended with a location state machine**:
  `state.location` is either `{kind:'outside'}` or `{kind:'room',
  building}`. Walking onto a building's door-trigger rect steps inside
  (Pokemon-style, no separate interact key); walking into a strip along a
  room's open south edge — the same "unseen entrance" convention the art
  itself uses (Phase 25's hard rules) — exits back outside at that
  building's own door. Update/render both branch on this state; outside
  keeps using the existing per-cell `blockedAt()`, rooms use a new
  `blockedInRoom()` against their rectangle list.
- **Real bug caught before it shipped: room spawn point overlapped
  furniture.** First attempt spawned the player just below the top wall
  band (`y=90`) for every room, which happened to work for the Work Room
  (desks start at `y=105`) but put the player standing inside the
  Library's top-row bookshelf (`y=41`) and directly inside the Post
  Office's cubbyhole wall (`y=50-175`) — caught by screenshotting the
  Library on entry, not by trusting the math. Fixed by spawning at the
  room's south edge instead (`y=370`, facing north) — clear of every
  layout's furniture in all four rooms, and it also matches the "you just
  walked in from the unseen south entrance" framing better than the
  original top-edge spawn did.
- **Verified two ways, matching this project's standing discipline**: a
  scripted walk test for all 7 buildings (start just outside each door
  trigger, confirm the position isn't already blocked, hold "north,"
  confirm entry within a few steps into the *correct* building — 7/7
  passed clean) and direct screenshots of all four room layouts with the
  debug collision overlay on, confirming each rectangle actually lines up
  with its desk/shelf/counter/wall rather than floating nearby.
- **Door trigger positions needed real correction, not just placement.**
  Initial door-trigger coordinates were recalled estimates from memory of
  earlier collision-mapping work, not freshly measured — cross-checking
  them against the actual village background image (cropping and zooming
  each building's real doorway, then converting screen pixels back
  through the camera/zoom math to world coordinates) found 4 of 7
  meaningfully off: Library and Weather Station's triggers sat on the
  wrong side of their buildings, Post Office's was too far south of the
  actual door, and Observatory's was offset onto open lawn next to the
  door rather than on it. All four corrected and re-verified visually.

**Experiment — World 2's door trigger editor.** You reported the door
triggers were still "kind of messed up" after the fix pass above and asked
for a dedicated editor, rather than continuing to hand-correct coordinates
from pixel math and screenshots.

- **`door_triggers.json`** (new) — door-trigger data extracted out of
  `rooms.js` into its own fetched JSON file, in the same native (688x384)
  coordinate space as `collision_grid.json`, so it can be edited and saved
  the same way. `rooms.js` now loads it asynchronously
  (`loadDoorTriggers()`) and scales it into world-space at load time,
  instead of hardcoding pre-scaled values.
- **`world2/door_editor.html`** (new) — same fixed-viewport/zoom/pan
  pattern as `editor.html`, but for rectangles instead of grid cells: a
  building list to select/jump to any of the 7 triggers, drag-to-move the
  box body, drag-to-resize from any corner, and numeric `x/y/w/h` fields
  for exact entry (given this project's repeated experience that reading
  pixel coordinates off a rendered image by eye is unreliable — see above
  and the original World 2 collision-mapping work — direct numeric input
  avoids that failure mode entirely). Save posts to a new `/save-doors`
  route; Download is the manual fallback.
- **Real bug found and fixed: `/save-doors` never actually saved.**
  `serve.py`'s routing checked `path.startswith('/save')`, which also
  matches `/save-doors` as a prefix, so every door-trigger save was being
  caught by the *old* collision-grid handler first and 400'd (it expects
  `grid`/`cols`/`rows` keys a door-trigger payload doesn't have) before
  ever reaching the new logic. Fixed by making the `/save` check an exact
  match. Caught by testing the save round-trip directly rather than
  assuming the new route worked because the code looked right.
- **Self-inflicted near-miss during that debugging, caught and fixed
  immediately:** isolating the bug with raw `curl` test payloads
  overwrote both `door_triggers.json` and `collision_grid.json` on disk
  with throwaway test data. `door_triggers.json` was restored from the
  corrected values already in hand; `collision_grid.json` had no in-repo
  backup (no git repo at `world2/` or `ai-village/`), but was recovered
  from `~/Downloads/collision_grid (5).json` — the most recent of several
  files there from earlier manual "Download grid.json instead" exports —
  verified by checking its cell dimensions and blocked-cell count matched
  the real map before trusting it. Worth noting for future work here:
  this project has no git repository, so there's no safety net for
  accidental overwrites beyond whatever's manually downloaded.

**Experiment — Town Hall becomes a real meeting mechanic.** Per Phase 24,
Town Hall was reframed as a chat interface rather than a walk-in room, with
two follow-up requirements: any agent (not just you/admin) can use it, with
both preset attendee groups and fully custom selection. You then specified
the actual mechanic in detail: walking in makes your character disappear;
multiple independent meetings can run at once (one person calling three
people doesn't block someone else from immediately calling two different
people); an agent already in a call is "busy" and never gets pulled out of
it into a new one; invitees join immediately and vanish from wherever they
were; and everyone -- initiator included -- reappears somewhere new when
the call ends, never back at their exact old spot and never on top of
someone else currently on the map.

- **Real dependency gap surfaced before writing any code**: neither World
  1 nor World 2 has ever had more than one character on the map -- no
  roster, no NPCs, just the player. Phase 2/3 (agents come alive) hasn't
  started. Flagged this to you directly rather than quietly building
  against an assumption; you confirmed building a small stub roster now
  (placeholder characters, no wander AI or real behavior) so the actual
  meeting mechanic could be real, tested code today instead of a design
  note waiting on Phase 2/3.
- **`world2/agents.js`** (new) -- a 6-character stub roster (Ada, Ben,
  Cora, Dev, Eli, Faye), each just a name + color, reusing the player's own
  four-direction sprite set with a colored nameplate drawn above their head
  for identification (no new art). They stand still at a starting spot
  found by `pickFreeSpot()` (see below) -- no movement AI, since that's
  real agent behavior and out of scope for this pass.
- **`world2/meetings.js`** (new) -- the meeting manager. `startMeeting(initiatorId,
  participantIds)` filters out anyone already busy (silently -- they stay
  in their current call, matching your "marked as busy" rule) and returns
  `null` if nobody eligible is left; multiple meetings coexist in a single
  `MEETINGS` map keyed by id, so concurrency is just "more entries in this
  object," not a special case. `endMeeting()` clears busy-state and moves
  every non-player participant to a freshly picked spot.
- **`pickFreeSpot(avoidPoints)`** (in `agents.js`) -- the actual "reappear
  somewhere new" logic: samples random points on the outdoor map, rejects
  anything `blockedAt()` (inside a building/river) or within 60px of any
  point in the avoid-list. Both initial roster placement and post-meeting
  respawns call this, and `endMeeting()` seeds the avoid-list with every
  currently-visible character's position *and* every departing
  participant's pre-call spot -- so a respawn can land near where a still-
  ongoing meeting's participants are standing, but never back on the exact
  tile someone just vanished from.
- **Town Hall's door trigger opens a modal instead of entering a room** --
  a new special case in `index.html`'s existing door-trigger loop (the
  building has no `ROOMS` entry, only a `ROOM_DOOR_TRIGGERS` one). The
  modal lists preset buttons (`world2/meeting_presets.json`: All Hands,
  Leadership, Research Pair) that check off their members, plus a live
  checklist of every agent with busy ones shown disabled and labeled
  "(busy)" -- refreshed each time the modal opens, so it can never show a
  stale busy-state.
- **A new `state.location.kind === 'meeting'`** freezes player input and
  hides the player sprite for the call's duration (added to the same
  dispatcher `enterRoom`/`exitRoom` already used), and `state.ui` freezes
  movement while the modal itself is open, so you can't wander off mid-
  pick. Confirmed the door trigger measured the same rigorous way as every
  other building this session (crop the actual art, convert screen pixels
  back through the camera/zoom math to native coordinates) rather than
  guessed.
- **Real bug caught before it shipped: canceling the modal would have
  reopened it instantly.** Closing the modal without nudging the player
  off the door tile means the very next frame's trigger check sees the
  same overlap and reopens it -- same failure shape as a room's exit
  trigger would have if `exitRoom()` didn't already place you clear of the
  door. Fixed the same way: Cancel nudges the player a few pixels south of
  the trigger, matching the existing room-exit convention instead of
  inventing a new one.
- **Verified end-to-end via direct function calls, not just reading the
  code**: walked onto the trigger and confirmed the modal opens; started a
  call with two agents through the real UI checkboxes and confirmed both
  vanish, both get marked busy, a third uninvited agent is untouched, and
  the player vanishes too; started a **second, fully independent** meeting
  with different agents while the first was still active and confirmed
  both run concurrently; attempted to pull an already-busy agent into a
  third meeting and confirmed it's rejected (`null`) without disturbing
  her existing meeting; ended the first meeting and confirmed both
  participants reappeared far from both their old spot and every other
  visible character, the second meeting was untouched, and the call panel
  closed; confirmed Cancel doesn't reopen the modal on the next frame.
  Screenshotted the live map (nameplates readable, busy agents correctly
  absent) and the modal itself (busy agents correctly greyed out and
  labeled) rather than trusting the logic alone.
- **Testing note for future sessions**: a stale browser cache served an
  old cached copy of `index.html` missing the two new `<script>` tags
  partway through this verification, which looked exactly like a runtime
  bug (`AGENTS is not defined`) until traced to `document.scripts` not
  listing the new files at all. Fixed by cache-busting the navigation URL
  (`index.html?v=...`), same idea already used for every asset fetch in
  this codebase -- worth remembering that raw navigation to `index.html`
  itself isn't exempt from that same caching risk.
- **Second real bug, caught by a self-review pass right after the above
  verification, before you'd even tried it live**: `startMeeting()` never
  marked a non-player *initiator* as busy or hidden -- only the people they
  invited. The concurrency test above technically passed (both meetings
  ran, busy invitees were correctly protected) but Cora, as meeting-2's
  initiator, stayed fully visible and free to be dragged into a third
  meeting herself -- silently breaking your "someone in a call is marked
  as busy" rule for initiators specifically. Fixed by having
  `startMeeting()` mark every non-player participant busy/hidden,
  initiator included, and by refusing outright (`return null`) if the
  initiator is already busy themselves. Re-verified the same way: a
  non-player initiator now vanishes and goes busy the instant their own
  meeting starts, and an already-busy agent can no longer be used to
  initiate a second one.
- Concurrency is proven at the data-model level (multiple `MEETINGS`
  entries, correct busy-state enforcement) but only one meeting in this
  session's testing was ever player-initiated, since there's only one
  player-controlled character -- a second real person's call, or another
  agent autonomously deciding to start one, both need actual multi-agent
  behavior (Phase 2/3), not this stub.

**Experiment — Town Hall gets an actual chat dialog.** You tried the call
mechanic and asked for the obvious next piece: once a call starts, you
should land in a real chat window, not just a small "in a call" bar --
with a roster showing everyone's name and role, and the ability to direct-
message one person privately while the group conversation keeps going.
Checked scope with you first on two genuinely ambiguous points before
building: "position" turned out to mean a job-role label (not a seating
layout or literal map coordinates), and this pass is the chat interface
only -- no real AI replies yet, since that's Phase 3's OpenRouter wiring,
not started.

- **Roles added to the stub roster** (`agents.js`): one placeholder title
  per agent, matching Phase 24's building functions -- Ada/Research,
  Ben/Banking, Cora/Post Office, Dev/Studio, Eli/Weather Station,
  Faye/Control Room. Shown in both the call-setup checklist and the chat
  roster.
- **`meetings.js` gains a message log per meeting**: `groupLog` (the
  whole-call thread) and `dmLogs` (private threads, keyed by `dmKey()` --
  a sorted-pair key so a DM thread is the same object regardless of which
  side fetches it). `postMessage(meetingId, fromId, text, toId)` posts to
  the group thread when `toId` is omitted, or a specific DM thread
  otherwise -- both keep running in parallel for the rest of the call, per
  your requirement that a DM shouldn't interrupt the group conversation.
  Deliberately keyed by agent id rather than hardcoded to the player, so a
  real agent can post here too once Phase 3 exists, without changing this
  file.
- **The old floating "in a call" bar is gone, replaced by a real modal**
  (`#chatModal`): a roster column (Group + one row per participant, name
  and role, click to switch threads) beside a thread pane (header, scrolling
  log, input, Send). Opens automatically the moment a call starts and stays
  up for its duration; End Call lives in its header now instead of a
  separate floating panel.
- **Real bug caught before it shipped: typing in the chat box leaked into
  game controls.** The existing global `keydown` listener (arrow keys/WASD
  for movement, `g` for the debug overlay) fires on every keydown
  regardless of focus -- so typing a message containing the letter "g"
  would silently toggle the walkability overlay mid-sentence. Fixed by
  skipping that handler entirely when the event's target is an `INPUT` or
  `TEXTAREA`. Movement itself was already safe (frozen by the existing
  `state.location.kind === 'meeting'` guard), only the `g` hotkey's direct
  side effect was exposed.
- **Verified via direct calls, not just reading the code**: opened a call
  with three agents and confirmed the roster lists everyone's name and
  role correctly; sent a group message and confirmed it appears only in
  the group thread; switched to a DM with one participant and confirmed
  their thread starts empty (not showing group history) and a message sent
  there does *not* leak into the group thread or another participant's DM
  thread; switched back to Group and confirmed it's untouched by either
  DM; confirmed typing "g" in the chat input no longer flips
  `showFootprints`; confirmed End Call closes the chat modal and returns
  to the outdoor map. Screenshotted the live modal (roster, active-thread
  highlight, DM history) rather than trusting the DOM output alone.

**Experiment — Computer terminals in the workstation rooms.** You asked to
start making rooms functional, leading with the computer labs specifically
("I want to interface with them when I go as if I am going to chat with a
terminal"). Scoped this to the five rooms built on the shared six-desk
"workstations" layout (Work Room, Weather Station, Studio, Control Room,
Research Center) -- Library/Bank/Post Office have their own distinct
object types (bookshelves/tellers/cubbyholes) and are a separate pass. Per
your call, the terminal itself is identical everywhere for now; what each
room's terminal actually has access to is explicitly deferred ("we'll get
to that later").

- **`world2/terminals.js`** (new) -- `ROOM_INTERACTABLES`, keyed by
  collision-set name like `ROOM_COLLISIONS`. For `workstations`, six
  interact zones are *derived* from the desk rectangles already in
  `ROOM_COLLISIONS.workstations` (a thin strip directly south of each
  desk) rather than hand-typed again, so the two can never drift out of
  sync if a desk layout changes later.
- **Same "walk into it" trigger convention as everything else** in this
  game -- no separate interact key. `updateRoom()` checks the current
  room's interactables the same way it already checked the exit trigger,
  and opens a terminal modal styled like an actual terminal (black
  background, green monospace text, a `>` prompt) rather than reusing the
  chat modal's look, since "as if I am going to chat with a terminal" reads
  as a different register than the Town Hall meeting UI.
- **Real bug caught before it shipped: a naive close-nudge could have
  dropped the player inside a different desk's collision.** The interact
  zones sit in a narrow gap between two rows of desks (roughly 40px), so
  reusing Town Hall's fixed "+20px" nudge-on-close pattern would have been
  enough to land a player who just used a *top-row* desk's terminal
  partway inside the *bottom* row's collision rectangle. Fixed by nudging
  by the interact zone's own depth plus a couple pixels (stored per-open
  in `activeTerminalZone`) rather than a fixed constant, and verified the
  player ends up outside both `blockedInRoom()` and the exit trigger
  afterward, for both a top-row and a bottom-row desk.
- **Verified via direct calls**: walked up to a desk in the Work Room and
  confirmed the right terminal opens with a room-specific title; confirmed
  movement is fully frozen while it's open; typed a command and confirmed
  it echoes with a placeholder "not connected" response; closed it and
  confirmed no immediate reopen, no collision overlap, and no exit-trigger
  overlap; repeated the same walk-up-and-open check in the Weather
  Station (a different building sharing the same `workstations` layout)
  for both a top-row and a bottom-row desk, confirming the shared-layout
  behavior generalizes correctly across buildings, not just within one.
  Screenshotted the live terminal to see the actual look, not just the DOM.
- Still just a shell, same as Town Hall's chat: no real backend, no
  per-room differentiation in what a terminal can do -- both explicitly
  deferred by your own framing of this request.

**Experiment — Agent profiles and the reports/evidence mechanic.** You
shared three screenshots from Tristen's original video plus its transcript:
a per-agent Finder folder (`agent.json`, `AGENTS.md`, `conversations/`,
`MEMORY.md`, `prototypes/`, `reports/`, `state.json`), a full private
`AGENTS.md` for "greg" (role, numbered operating instructions, running
notes), and a `report-greg.md` where another agent ("wes") quotes one of
Greg's own instruction lines back as evidence that he faked a product,
stamped CAUGHT. The video's narration confirms this was emergent -- "they
actually opened up Greg's private prompt file... and quoted that back to
me" -- real agents catching each other, not a designed feature. Asked to
pick a piece to build; picked this one since it's foundational for the
morale meter later (reports feed it) and it's the most novel mechanic in
the reference material.

- **Every stub agent gets a real `AGENTS.md`-style profile** (`agents.js`):
  a mission line, 2-3 numbered operating instructions, and running notes,
  written per agent's existing Phase 24 role (Ada/Research,
  Ben/Banking, Cora/Post Office, Dev/Studio, Eli/Weather Station,
  Faye/Control Room) rather than generic placeholder text.
- **`world2/reports.js`** (new) -- `fileReport(aboutId, fromId, quote,
  note)` and `reportsAbout(aboutId)`. Deliberately not a per-agent file
  everyone has, matching what you flagged about the reference screenshots
  ("each agent has these files, except for the reporting image") --
  reports exist conditionally, only once filed.
- **Real gap named rather than papered over**: the reference's reports
  were emergent, written by one real reasoning agent independently
  noticing another agent's behavior didn't match their instructions. There
  are no real agents yet (Phase 3, not started, same gap as every other
  stub-roster feature this session), so filing a report here is a
  player-driven action -- you open an agent's `AGENTS.md`, pick one of
  their own instruction lines, choose who's filing it (any other agent, or
  yourself), add a note, and it renders with the same quote-block +
  attribution + CAUGHT-stamp treatment as the reference. This is a
  deliberate simplification, not a misunderstanding of the source material.
- **New always-available "Roster" corner button** opens a village-wide
  agent list -> click a name for their profile. Scoped deliberately narrow:
  only opens while just walking around outside (`state.ui` and
  `state.location.kind` both checked), rather than trying to stack it on
  top of a room, a meeting, or another modal -- simpler than solving
  arbitrary modal-stacking for a first pass.
- **Verified via direct calls**: opened the roster, opened Ada's profile,
  confirmed her real mission/instructions/notes render (not placeholder
  text); filed a report quoting one of her instruction lines, attributed
  to Ben, and confirmed it renders with the quote, attribution line, note,
  and CAUGHT stamp intact; confirmed Back-to-Roster and Close both restore
  the right modal state; confirmed the roster button is correctly refused
  while inside a room. (A screenshot tool hiccup unrelated to this code --
  no web fonts are even referenced on this page -- meant this pass leaned
  on DOM-output verification instead of a visual capture; the DOM checks
  are equally exact, just not a picture.)
- Not built yet: the morale meter and "culture crew" from the same
  reference material -- flagged as the next natural candidate since it
  consumes this session's reports data directly, but not started.

**Experiment — Morale meter.** You shared a fourth reference screenshot (a
HUD bar: villager/working/open-work/subscription/morale pills, a clock,
then Who's Who / mail-count / Line Up / Call Meeting / a notes-count pill /
Command Center) and flagged that it ties into the reports work above.
Confirmed the connections precisely in DESIGN.md's HUD spec (§3) before
building anything -- Who's Who is our Roster button, the notes-count pill
is almost certainly `REPORTS.length`, Call Meeting is Town Hall, Command
Center is the Control Room building. Given the go-ahead, picked morale
next since it's the one stat in that bar this session hadn't touched, and
it directly consumes reports rather than needing its own new system.

- **No formula was ever disclosed** in the reference video -- only two
  data points (Ryan: 5 approved, not spoken to in a week, "fine"; Greg: 26
  approved, 9 dropped, not spoken to in over a week, morale zero) that
  don't reverse-engineer cleanly to one shared rule (their exact "over a
  week" durations aren't given either). Built a reasonable formula
  matching the same *shape* instead of pretending to reconstruct their
  real one: a small capped bonus for approved work, a real penalty per
  dropped item, a bigger penalty per report filed against them (`reports.js`),
  and a neglect penalty from days since last contacted -- capped so it
  doesn't spiral, but "never contacted" is treated as worse than any
  measured number of days. Deliberately NOT "more work = more morale" --
  matching the reference's actual point (Greg does the most work and has
  the worst morale; this is about burnout, not a leaderboard).
- **`world2/morale.js`** (new) -- `moraleFor(agentId)` and
  `villageMorale()` (a simple roster average). Reads `AGENTS[id].approvedCount`/
  `droppedCount` (seed stats added per agent, illustrative -- there's still
  no real task/approval system, same Phase 3 gap as everywhere else this
  session), `reportsAbout(id).length` from the reports experiment, and
  `AGENTS[id].lastContactedAt`.
- **One of the three inputs is real, not seeded**: `lastContactedAt` is
  set by an actual gameplay action, not invented data. `meetings.js`'s
  `postMessage()` now calls `markContacted()` (`agents.js`) -- a group
  message marks every other call participant as contacted, a DM marks just
  its one recipient. This means actually using Town Hall's chat (built two
  experiments ago) measurably improves morale, the same causal link the
  reference draws between neglect and low morale.
- **Surfaced in the two places that already exist** rather than building
  the full HUD bar for one stat: the Roster list now shows each agent's
  morale next to their role, and its title shows the village-wide average;
  each profile's new STATUS section shows morale, approved/dropped counts,
  and days-since-contact, plus two buttons ("+ Log approved work" / "+ Log
  dropped work") to move those seed counts, since there's no real work
  pipeline yet to move them automatically.
- **Verified via direct calls**: opened the roster and confirmed morale
  values differ sensibly across agents (Dev, seeded with the heaviest drop
  count, came out lowest at 44 of the six); logged an approved and a
  dropped item on Dev's profile and confirmed morale moved in the right
  direction each time and the display refreshed live; filed a report
  against Dev and confirmed morale dropped by exactly the report penalty
  and the STATUS section updated without a manual re-open; started a real
  Town Hall call, sent Dev an actual group message, and confirmed
  `lastContactedAt` was set and his morale jumped from 29 to 59 in one
  step (the neglect penalty collapsing from "never contacted" to "just
  now") -- the full loop from a real chat action to a changed stat,
  working end to end.
- Still not built: the "culture crew" peer-intervention behavior from the
  same reference material, and the full HUD bar itself (§3) -- both
  explicitly deferred, morale was the piece requested this round.

**Experiment — The HUD bar, replacing the corner Roster button.** Asked
for directly: build the bar from the reference screenshot instead of the
standalone corner button. Went through it pill by pill rather than
copying blindly -- some map to real data we now have, some to concepts
that don't exist in this village at all, and one (the three unlabeled
icon pills in the reference) was left out entirely rather than guessed,
per the explicit "don't guess further" note already in §3's HUD spec.

- **Real, live pills**: Villagers (`AGENT_ROSTER.length`), Working (count
  of `AGENTS` with `busy: true` -- this is a real, if narrow, definition:
  right now the only way to be "working" is to be in a Town Hall call),
  Morale (`villageMorale()`, from the morale experiment above), Reports
  (`REPORTS.length` -- confirming the guess in §3's HUD spec that the
  reference's "17" notes pill maps to exactly this). A new cosmetic clock
  ticks in the title card (1 real second = 1 game minute, 24-minute full
  day) -- display only, nothing reads it yet.
- **Honest placeholders, not invented data**: Open Work and Mail both
  render "0" with a tooltip explaining why -- there's no task/approval
  system and no mail system built yet, so a real number doesn't exist to
  show. Matches this session's standing rule of showing a shell as a shell
  rather than a fake number pretending to be real.
- **Dropped rather than faked**: the reference's Subscription pill (Higgsfield
  billing status -- Tristen's real business, not applicable here) and the
  three unlabeled count pills (bee/second icon/lightbulb -- meaning was
  never confirmed and flagged not to guess) aren't in this bar at all.
- **Buttons, two real and two honestly disabled**: Who's Who (was the
  corner button, now lives here -- same `openRosterList()`) and Call
  Meeting (opens Town Hall's call-setup modal from anywhere, not just
  standing at the door -- a genuinely new capability, not just a
  relocation) both work. Line Up and Command Center are rendered
  `disabled` with a tooltip -- Line Up has no designed behavior yet, and
  Command Center can't sensibly open anything because Control Room was
  never actually added as a walkable building in Phase 26's door-trigger
  wiring, despite being named in Phase 24's room table (a real gap,
  surfaced here rather than papered over with a fake action).
- **Real bug caught before it shipped: Call Meeting needed its own busy
  guard.** `openMeetingUI()` was previously only ever reached through the
  outside door-trigger loop, which already guarantees `state.ui` is null
  and location is `'outside'` by construction -- so it never checked
  either itself. Exposing it as a HUD button reachable from anywhere
  (mid-room, mid-meeting) meant that guarantee no longer held. Fixed by
  moving the guard inside the function itself rather than trusting every
  call site to repeat it.
- **Layout**: the bar is `position: fixed` at the very top like the
  existing corner UI, with `#wrap`'s top padding increased so the game
  viewport starts below it rather than being covered -- verified
  geometrically (`getBoundingClientRect()` on both elements, confirmed
  zero overlap and full window width) rather than by eye, since this
  session's screenshot tool hit a persistent, code-unrelated timeout
  partway through (also true of the morale experiment's tail end) --
  DOM/geometry checks stood in for a picture both times.
- **Verified via direct calls**: confirmed all live pills show correct
  values on load; confirmed Line Up and Command Center are actually
  `disabled`; clicked Call Meeting from the player's spawn point (nowhere
  near Town Hall) and confirmed it opens the same modal; started and
  ended a call and confirmed the Working pill goes 0 → 1 → 0 across
  frames (accounting for the one-frame render lag between a state change
  and the HUD text catching up, which is expected, not a bug); confirmed
  the world clock advances in real time.

**Real bug, caught by you rather than by testing: agents could spawn into
unreachable pockets.** You noticed characters could end up somewhere they
couldn't actually walk out of, and floated two possible fixes (constrain
placement to a path entrance, or route everyone through a small holding
room). The actual root cause: `pickFreeSpot()` only checked that a
candidate point wasn't `blockedAt()` -- a cell can be walkable and still
sit in a pocket fully cut off from the rest of the map (a grass gap at the
tree-line border, say), and nothing was checking connectivity at all.

- **Fix**: flood-fill once from the player's own spawn point (guaranteed
  walkable, and by construction on the reachable network -- the player
  starts there every load) across `COLLISION_GRID`, caching every cell
  actually connected to it (`computeReachableMask()`/`isReachable()`,
  `agents.js`). `pickFreeSpot()` now rejects a candidate unless it's both
  unblocked *and* reachable. This directly solves it rather than
  approximating it the way either of your two suggestions would have
  (both are reasonable, but a real connectivity check is more precise than
  constraining to hand-picked anchor points, and doesn't need a new
  holding-room concept).
- **Confirmed this was a real, sizable problem, not a hypothetical one**:
  of 2024 walkable cells on the current map, 209 (~10%) are unreachable
  from spawn -- mostly along the tree-line border. Every agent placement
  before this fix had roughly a 1-in-10 chance of stranding someone.
- **Verified via direct calls**: ran `pickFreeSpot()` 500 times and
  confirmed every result was both unblocked and reachable; confirmed all
  6 roster agents' actual spawn positions are reachable after the fix.

**HUD bar follow-up: title card removed.** You didn't want the "AI
Village / THE WHOLE OPERATION. ONE TOWN." block at all. Removed it
(name, tagline, and the div that held them) and moved the clock into its
own pill matching the others -- then immediately removed the clock too on
your next call (below), so the bar now opens straight into the stat pills.

**HUD bar follow-up: the world clock removed.** Asked directly to drop
Time from the bar. Since nothing else read `gameClockMinutes` (display-
only, per its own comment when it was added), removed the pill and the
underlying `tickClock()`/`formatClock()`/`gameClockMinutes` entirely
rather than leaving unused code behind -- cheap to re-add later if a real
day/night mechanic ever needs it.

**Experiment — Post Office's mailbox wall, and the Mail pill goes real.**
Picked as the natural next step after the HUD bar: Post Office was the
one workstation-adjacent room with zero interactables (unlike the five
`workstations` rooms), and it directly closes the "no mail system" caveat
the HUD's Mail pill was honestly flagging. Same pattern as the terminal
experiment -- walk up to the object, get a dialog -- extended to a second
object type instead of staying terminal-only.

- **`ROOM_INTERACTABLES` generalized beyond terminals** (`terminals.js`,
  renamed in comments though not on disk -- the file now covers more than
  its name suggests): a `postoffice` entry with `type: 'mailbox'`, zone
  derived the same way as the desk zones (a strip south of the cubbyhole
  wall's own collision rect). `updateRoom()`'s interactable loop now
  branches on `item.type` to open the right modal instead of assuming
  every interactable is a terminal.
- **Six seeded inboxes** (`agents.js`, one `mailbox` array per agent,
  flavor text matching their role) browsable from one modal -- a roster
  of agents with message counts on the left, the selected inbox's messages
  on the right, reusing the chat modal's visual layout (`.chat-box`/
  `.chat-roster`/`.roster-item`) rather than inventing new CSS for a
  near-identical shape.
- **Small refactor while wiring the second interactable type**: the
  "nudge the player clear of this zone on close" logic was named
  `activeTerminalZone`/`closeTerminal()`-specific even though the need
  (don't reopen the modal next frame, don't land inside a neighboring
  collision rect) is identical for any room object. Generalized to
  `activeInteractZone`/`clearInteractZone()`, shared by both
  `closeTerminal()` and the new `closeMailbox()`, rather than duplicating
  the same nudge math a second time.
- **HUD's Mail pill is now real**, not a placeholder: sums every agent's
  `mailbox.length` live. Role-gating (each agent seeing only their own
  mail, per Phase 24's original design) still isn't enforced -- there's no
  real agent perspective to gate against yet, only the player's own
  always-see-everything view, which is an honest limitation, not an
  oversight.
- **Verified via direct calls**: confirmed the HUD Mail count (11) matches
  a hand-count across all six seeded inboxes; walked up to the mailbox
  wall and confirmed the modal opens with correct per-agent message
  counts and correct singular/plural labels; clicked into Cora's inbox and
  confirmed her three actual seeded messages render, not placeholder text;
  closed the modal and confirmed no collision overlap and no reopen loop
  next frame; re-verified a workstation terminal still opens and closes
  correctly after the `activeInteractZone` refactor, to make sure
  generalizing shared code didn't regress the first interactable type.

**Reachability fix, round 2: the first fix was incomplete.** You refreshed
and sent a screenshot of Ada standing right next to Town Hall near the
tree line, flagging she looked stranded again. Trees don't block collision
at all (only buildings/water were ever segmented into `collision_grid.json`),
so the tree line itself wasn't the cause -- the real problem was one level
deeper than the round-1 fix addressed.

- **Root cause**: round 1's flood-fill tested the raw per-cell boolean
  (`grid[gy][gx]`) -- "is this one 8px cell blocked" -- not "does the
  agent's actual 20x16 footprint fit here." A cell can read unblocked while
  sitting in a sliver too narrow for the real box (e.g., a thin strip
  between a building's wall and the map edge), which round 1's mask
  happily counted as reachable since it only ever looked at single cells
  in isolation.
- **Fix**: `computeReachableMask()` now tests full-box placement at each
  cell via `blockedAt()` (`cellFitsAgent()`, `agents.js`) -- the same
  check real movement uses -- instead of the raw boolean, for both the
  flood-fill's start condition and its traversal. A cell only counts as
  passable if an agent could actually stand there without clipping a
  neighbor.
- **This was a much bigger correction than round 1's own fix**: of 2024
  raw-unblocked cells, only 1504 (74%) actually fit the agent's real
  footprint -- 520 cells were narrow slivers exactly like what stranded
  Ada, more than double round 1's 209-cell isolated-pocket count. Of those
  1504, 1307 are truly reachable once connectivity is checked too.
- **A verification detour worth recording honestly**: first tried to
  cross-check the fix with a simple "always step straight toward spawn"
  walker: 25 of 40 simulated agents never arrived. That looked alarming
  but was a false alarm from the test itself, not the fix -- a greedy
  walker gets stuck on any wall requiring a sideways detour even on a
  fully connected map, since it never explores away from the target the
  way a real flood-fill (or a person) would. Checked the "stuck" points
  directly against `isReachable()` and confirmed they were correctly
  marked reachable -- the walker's failure was in its own dumb pathing
  logic, not the underlying map connectivity. Re-verified properly with
  the same 500-trial `pickFreeSpot()` audit as round 1, this time also
  checking every result against `blockedAt()` on its exact (non-quantized)
  box position, which is the real guarantee against clipping -- the
  cell-based reachability check only needs to get the general area right,
  not the precise pixel.
- **Verified via direct calls**: reran the 500-trial audit clean; confirmed
  all 6 roster agents' actual spawn positions both fit the agent box and
  are reachable, checked directly against `blockedAt()` and `isReachable()`
  rather than trusted from the mask alone.

**Experiment — Agents become solid, and a real walk-up conversation.**
Two related requests: you noticed you could walk straight through other
agents (they'd only ever been visual), and separately asked for a direct
1:1 conversation when walking up to one -- distinct from Town Hall's
multi-person call.

- **`agentBlockedAt()`** (`agents.js`) checks the player's movement box
  against every currently-visible agent's hitbox, same shape as the
  existing `blockedAt()` grid check. `updateOutside()`'s three collision
  checks (already-stuck safety net, diagonal try, axis-slide fallback) now
  test `blockedAt(box) || agentBlockedAt(box)` instead of the grid alone --
  agents are obstacles, not decoration.
- **Walking up to a non-busy agent opens a 1:1 conversation** -- same
  "walk into it" convention as every other trigger this session, just
  keyed off proximity to a (now-solid) agent instead of a fixed zone. Each
  agent gets their own `conversationLog` (`agents.js`), separate from a
  Town Hall meeting's group/DM threads -- this is the ongoing casual
  conversation from approaching them directly on the map, not scoped to a
  call. Sending a message calls `markContacted()`, so a hallway chat
  improves morale exactly like a Town Hall message does.
- **Busy agents can't be walked up to and talked to**, per your explicit
  follow-up question. Reused the existing `busy` flag rather than adding a
  parallel one -- today that only ever means "in a Town Hall call," since
  agents don't autonomously use terminals or check mail yet (no
  self-directed behavior exists at all, same Phase 2/3 gap as everywhere
  else). The gate is written generically (`if (a.busy) ... else
  openConversation()`), so it's already correct for "using a terminal" or
  "checking mail" the moment agents can do those on their own -- nothing
  here needs revisiting when that lands. Approaching a busy agent shows a
  toast ("X is busy right now.") instead of silently doing nothing, without
  spamming every frame while standing there.
- **Real bug caught before it shipped: closing a conversation would have
  reopened it instantly.** The player is now physically stopped right at
  contact distance by the new collision (that's the point), so unlike a
  door trigger there's no natural "walk off the tile" to clear the
  proximity zone. Fixed by pushing the player a fixed distance directly
  away from whoever they were talking to on close, same idea as every
  other "clear this trigger" nudge this session, just computed from the
  agent's position instead of a static zone.
- **Verified via direct calls**: walked the player into Ada and confirmed
  collision stops them at exact contact (2px gap, zero overlap) rather
  than passing through; confirmed the conversation auto-opened during that
  same walk; sent a message and confirmed it renders and
  `lastContactedAt` updates; closed and confirmed no reopen on the next
  frame; forced an agent busy and confirmed approaching them shows the
  toast and does not open a conversation, and that a real
  `startMeeting()`/`endMeeting()` round-trip behaves the same way.

**Follow-up: conversations need a real keypress, not just proximity.**
You didn't want standing next to someone to be enough by itself. Replaced
the auto-open from the entry above with a "press B to talk" prompt:
`nearbyAgentId` tracks who you're standing next to (same proximity zone as
before), a small `#interactPrompt` label shows/hides based on it, and a
new `b` keydown handler opens the conversation only on an actual
keypress. Busy agents still just get the toast, since there's nothing to
press B for. Also flagged, not yet actionable: you want conversations to
be symmetric eventually -- if an agent ever initiates on the player, they
shouldn't be able to "walk away" without knowing. `openConversation()`
already force-opens the modal and freezes movement unconditionally
regardless of who calls it, so this already works for that case the
moment agents can act on their own (Phase 2/3) -- nothing to build now,
just confirmed the mechanism is ready. Verified: standing next to Ada
shows the prompt without opening the dialog; pressing `b` opens it.

**Experiment — House, Command Center, and autonomous hiring.** A three-
part request, refined twice as you clarified it. Final shape: House (a
real building, walkable, its own door trigger) is "literally just the
board" for you -- a minimal stub modal for now, matching your own framing
that it wasn't the focus of this ask. Command Center is a *separate*
concept entirely -- not tied to any building on the outdoor map, reachable
only via the HUD button (like Call Meeting) -- where an admin-flagged
agent autonomously hires help, with no form for you to fill out, and where
you can walk in to monitor who's actually in there.

- **`isAdmin: true`** added to Faye (`agents.js`) -- her existing
  elevated-privilege role was the natural fit, not a new concept bolted
  on.
- **`world2/hiring.js`** (new): `attemptAutoHire()` runs on a timer
  (every 5s, `setInterval` in `main()`), no-ops almost every call (45s
  cooldown, admin must be free, someone must need help), and picks who to
  help the same way the morale meter already surfaces struggle --
  lowest-morale non-admin agent, not a separate priority system.
- **Hiring is two real phases, not one instant event, per your explicit
  follow-up that you need to monitor it.** Phase 1: the admin goes busy
  and invisible on the outdoor map (same as a Town Hall call) and gets
  `inRoom: 'commandcenter'` plus a fixed desk position. Phase 2, after an
  8-second window (`setTimeout`): the actual hire completes, the admin
  returns to normal. `renderAgentsInRoom()` (`agents.js`) draws anyone
  with a matching `inRoom` if the player is currently inside that same
  room -- reusing the exact sprite/nameplate code `renderAgents()` already
  had, just parameterized by position instead of assuming outdoor
  coordinates. You named a real tension here (an unmonitored elevated-
  access room could be misused) -- noted directly in the code as a design
  point, not resolved with an actual misuse mechanic, since none was asked
  for.
- **`enterRoom()`/`exitRoom()` needed the same guard-and-fallback fix
  `openMeetingUI()` got earlier**: Command Center is now reachable from
  anywhere via a HUD button, not just a door trigger that guaranteed
  'outside'/no-modal by construction, so `enterRoom()` needed its own
  guard. And since Control Room has no physical door on the map,
  `exitRoom()` needed somewhere sane to put the player back --
  `lastOutdoorPos`, captured on every `enterRoom()` call, used as a
  fallback whenever `ROOM_DOOR_TRIGGERS[building]` doesn't exist.
- **Real caching bug hit hard during this pass, unrelated to the feature
  itself**: `<script src="rooms.js">` (and every other same-origin script
  tag) had no cache-busting, and Chromium had cached an old copy from
  earlier in this session *before* serve.py's `/save`-era headers existed
  -- meaning even a fresh tab, fresh navigation, and an explicit
  `Cache-Control: no-store` on every current response still didn't help,
  because the browser was satisfying the request from a disk-cache entry
  without a network round-trip at all. Fixed two ways: `serve.py` now
  sends `Cache-Control: no-store` on every response (a local dev server for
  actively-edited files should never cache), and every `<script src>` tag
  got a `?v=2` bump to force one clean fetch past the old cache entry.
  Confirmed via a direct `fetch(..., {cache:'no-store'})` from inside the
  page (worked immediately) vs. `Object.keys(ROOMS)` after a normal
  navigation (stayed stale) before landing on the real cause.
- **Verified via direct calls**: entered Control Room via the HUD button
  and confirmed `exitRoom()` returns the player to their exact pre-entry
  outdoor position; the periodic timer autonomously hired "Sam" to help
  Dev (the actual lowest-morale agent) with correct role/mailbox/access
  data, entirely in the background with no manual trigger, while this
  session was testing something else -- as close to a real "didn't have
  to do it myself" proof as a test can offer; manually staged the mid-hire
  window and confirmed the admin is invisible outdoors but would render
  correctly inside Control Room if the player walked in.

**Follow-up: roster cap, model tiers, and a live API key handled
carefully.** You raised budget concerns (uncapped autonomous hiring, and
separately a shared-notes-style cost risk once real model calls exist),
asked for per-role model selection since not every agent needs a capable
model, and pasted a live OpenRouter API key asking to wire agent movement
to it via Jev, suggesting Nova Micro for interactions -- while explicitly
inviting "tell me if we're not ready."

- **The key was not wired into anything.** It's now in `~/ai-village/.env`
  (`OPENROUTER_API_KEY`, next to the existing `PIXELLAB_API_KEY`, same
  established convention) -- never written into anything under `world2/`,
  since that entire directory is served as static files straight to the
  browser, and a key living in browser-served JS is a key anyone can read
  via view-source. Confirmed `world2/serve.py` can't be tricked into
  serving the parent directory either (`../.env` and its `%2e%2e` encoding
  both 404, Python's `http.server` normalizes path traversal on its own).
  Real model calls need the backend to exist first and hold this key
  server-side -- not built this round.
- **`MAX_ROSTER_SIZE = 10`** (`hiring.js`) -- `attemptAutoHire()` now
  refuses once the roster hits it, verified by padding the roster to the
  cap directly and confirming the function returns `false` with no side
  effects, then cleaning up the padding.
- **`MODEL_TIERS`** (`hiring.js`): small/mid/premium, with real
  (best-guess, explicitly *not* verified against OpenRouter's live
  catalog) slugs, shown in each agent's profile next to their role. Every
  original agent except Faye defaults to `small` (Nova Micro); Faye gets
  `mid`; every new Command-Center hire defaults to `small` too --
  assistant/overflow work doesn't need a bigger model, per your call. You
  caught that the initial premium pick (Claude Sonnet 5) didn't fit a
  tier meant to still be budget-conscious; swapped to GLM-5.2, consistent
  with this project's own earlier OpenRouter cost research (GLM-5.2 well
  below Anthropic frontier pricing).
- **Jev's actual fit, clarified**: classifier-style "pick one of N"
  decisions specifically -- not general reasoning or reply generation.
  `whoNeedsHelp()` is flagged directly in code as the natural first target
  once Jev is wired (via the OpenRouter passthrough already decided in
  the earlier Jev research), since deciding who needs help most is
  exactly that shape of decision -- the actual hire, and any future
  agent conversation, stays a different model's job.
- **Not built yet, captured for later**: your firing rule ("only after
  two admin agents have discussed the agent's performance") -- there's
  only one admin right now, so this needs a second admin to even be
  possible, and there's no firing mechanic at all yet, only hiring. Added
  to §8's Open Decisions rather than guessed at now.

**Phase 2 (partial) — the backend arrives, state persists.** You asked
directly: start the backend, make state actually stick across a refresh.
Scoped to persistence specifically -- websocket support and agent wander
behavior (the rest of what Phase 2 names below originally described) are
still not built.

- **`world2/serve.py` upgraded from a plain `http.server` to FastAPI**,
  same invocation (`python3 serve.py [port]`) and both existing endpoints
  (`/save`, `/save-doors`) reimplemented identically -- editor.html and
  door_editor.html need no changes. New `GET`/`POST /api/state`, backed by
  `world2/state.json`, holding the full `AGENT_ROSTER` (so a Command-
  Center hire survives), `AGENTS`, `REPORTS`, and `nextReportId`.
  Whole-snapshot, not incremental -- the actual data here is tiny (a
  handful of agents and reports), so POSTing everything on every save is
  simpler than diffing anything.
- **Meetings are deliberately NOT persisted.** They're session-scoped, not
  village-scoped -- there's no meaningful way to resume a chat UI's state
  after a refresh anyway. Load-time recovery instead: any agent left
  `busy`/invisible/`inRoom` from a call or a hire that was in progress
  when the page last closed gets force-reset to free/visible on the next
  load, since the `MEETINGS` entry that would have ended it properly no
  longer exists once the client that held it is gone.
- **Autosave via one periodic timer** (`setInterval(saveState, 5000)`),
  not hooked into every mutation site -- catches morale inputs, reports,
  mailbox state, hires, and chat-driven contact timestamps with a single
  mechanism instead of chasing each one individually.
- **The OpenRouter key still isn't used anywhere.** This backend now
  exists and could hold it server-side, but no model call is wired yet --
  that's a deliberately separate next step, not bundled into "the state
  persists" just because the prerequisite now exists.
- **Same mistake made twice in one project, worth naming plainly**:
  verifying `/save`/`/save-doors` with real `curl` POSTs clobbered
  `door_triggers.json` and `collision_grid.json` *again* -- identical to
  the incident during the door-trigger-editor work. Both restored (the
  door triggers from values already in hand, the collision grid from the
  same `~/Downloads/collision_grid (5).json` backup as last time). Given
  it's now happened twice, the actual fix is behavioral, not technical:
  don't send real test payloads to endpoints that write fixed, real files
  -- read the code to verify routing/logic instead, the way the rest of
  this session's verification already works for everything else.
- **Verified via direct calls, this time without touching the two
  dangerous endpoints again**: set a distinctive `approvedCount`/position
  on Ada plus a report with a marker note, saved, reloaded, and confirmed
  every value round-tripped exactly (including both agents hired
  autonomously earlier in this same test, confirming `AGENT_ROSTER`
  persistence works for hires, not just the original six); separately
  staged an agent as busy/invisible/`inRoom` (simulating an abandoned
  mid-call session), reloaded, and confirmed they came back free and
  visible rather than being stuck.

**Experiment — Real AI replies, wired for real.** With the backend now
holding the key safely, wired the first actual OpenRouter call: 1:1
conversations (not Town Hall's group chat yet -- smaller scope, one
agent, one persona) now get a genuine model-generated, in-character
reply instead of silence.

- **Verified all three `MODEL_TIERS` slugs against OpenRouter's live
  `/api/v1/models` catalog before wiring anything** (`amazon/nova-micro-v1`,
  `deepseek/deepseek-v4-flash-0731`, `z-ai/glm-5.2`) -- all three were
  already correct, but this was confirmed rather than assumed, replacing
  the earlier "not verified, treat as placeholder" caveat with a real
  check.
- **`POST /api/chat`** (`serve.py`) -- the only place `OPENROUTER_API_KEY`
  is ever read or used. Takes a model slug and a full message list (the
  client owns persona/system-prompt construction; this is a secure proxy,
  not application logic) and returns just the reply text. Runs the
  blocking `urllib` call via `asyncio.to_thread()` rather than stalling
  the event loop for every other request during the round-trip. A hard
  300-token cap applies server-side regardless of what the client
  requests.
- **`requestAgentReply()`** (`index.html`) builds a short in-character
  system prompt from the agent's own `role`/`profile.mission`, sends the
  last 8 turns of `conversationLog` for context (bounded, so a long-running
  conversation doesn't make every call more expensive), and picks the
  model slug from the agent's own `model` tier (`hiring.js`'s
  `MODEL_TIERS`) -- Ada replies via Nova Micro, Faye would reply via
  DeepSeek Flash, matching the tiering work from two experiments ago
  rather than one model for everyone.
- **A real typing indicator, and the input disables during the call** --
  `handleConversationSend()` appends a temporary "..." line and disables
  the input/send button while the request is in flight, both cleared by
  the same `renderConversationLog()` call that renders the real reply. A
  guard (`activeConversationAgent === agentId`) skips the DOM update if
  the player closed the conversation while the reply was still in
  flight, rather than writing into a modal that isn't showing that
  agent's thread anymore.
- **Failure is handled, not just the happy path** -- confirmed by forcing
  an actual OpenRouter error (an invalid model slug) directly against
  `/api/chat` and observing `requestAgentReply()`'s fallback trigger
  correctly (`!res.ok || data.error`), returning an in-universe
  "(Ada seems distracted and doesn't answer.)" instead of a raw error or
  a stuck UI.
- **Verified via a real live call, not a mock**: `curl`'d `/api/chat`
  directly first (Ada, Nova Micro, a genuine in-character one-sentence
  reply about local flora research) to confirm the backend proxy alone
  before touching the browser; then ran the full flow through
  `openConversation()`/`handleConversationSend()` and confirmed the
  typing indicator shows mid-flight, the input is disabled and
  re-enabled correctly, and a second genuine in-character reply (about
  agricultural research, consistent with Ada's role) lands in
  `conversationLog` with the right `fromId` and renders correctly.
- **Not done yet at this point**: Town Hall's group chat still had no real
  replies -- scoped out deliberately for this pass (multiple participants
  replying in one thread is a meaningfully different, harder problem than
  one agent replying to one player). Resolved in a later pass, see below.
  Agents still don't act on their own -- this only replies when the player
  sends a message, it doesn't decide to say anything unprompted.

**Experiment — Jev, corrected: the real integration path.** Continuing from
the earlier research above (Jev's fit as a classifier, "pick one of N"
decision), two further attempts to nail down HOW to actually call it were
both wrong before a live functional call settled it.

- First wrong turn: trusted an unverified web-search summary claiming a
  generic OpenRouter chat-completions passthrough exists for Jev -- never
  actually confirmed against a real response.
- Second wrong turn, an overcorrection: checked OpenRouter's own
  `/api/v1/models` catalog directly, found no `typesafe/jev` entry among
  446 models, and concluded Jev needs its own separate TypeSafe API key --
  wrong, because the catalog apparently doesn't list "decisions"-type
  models at all, a gap that looked like confirmation of the wrong
  conclusion.
- You corrected both: "I thought that jev was available through
  OpenRouter, negating the need for a typesafe API key," then supplied the
  exact slug, `typesafe/jev-1.13`.
- The real shape, confirmed by an actual successful call (not a document):
  `POST https://openrouter.ai/api/alpha/decisions` -- a genuinely
  different endpoint from chat completions, not OpenAI-chat-compatible --
  body `{model, state: {messages, signals}, questions: {key: {type:
  'choice', instructions, criteria}}}`, response `{answers: {key: {choice,
  probabilities, confidence}}, usage: {cost}}`. Verified via a direct
  `curl` (cost $0.000014364) and again inside the real task-assignment
  scenario below.
- `world2/jev.js` (new): `JEV_MODEL = 'typesafe/jev-1.13'`,
  `requestJevChoice(instructions, candidates)` -- generic, no
  task/hiring-specific logic baked in. `serve.py` gained
  `_call_openrouter_decision_sync()` and `POST /api/decide`, the same
  secure-proxy shape as `/api/chat`.
- Lesson worth stating plainly, since it cost two wrong turns: a live
  functional call against the real endpoint is the only real authority
  here, not a search summary and not a catalog that doesn't list what
  you're looking for.

**Experiment — A real task system, and five real pathfinding bugs.** You
asked directly: get agents moving via Jev, but they had no incentive to go
anywhere without a task. `world2/tasks.js` (new) gives each agent one task
at a time -- a title, a fixed target room, a fixed work duration -- with
who-does-it decided by a real Jev call (`assignTaskViaJev()`), not you
picking manually.

- **Movement is a real BFS grid path (`findPath()`), not a straight
  line.** The first version walked straight at the door and got stuck at
  water's edge almost immediately -- a straight line can cross a river
  with only specific bridge crossings. Reuses the same box-fit cell test
  (`cellFitsAgent()`) the earlier reachability fix established.
- **Bug 1 -- the door trigger's own center is invalid geometry.** A door
  trigger's center sits right at the building's edge; centering a full
  agent box exactly there clips the building and fails the box-fit test
  outright, which is what actually made an agent look "stuck" before any
  water was even involved. Fixed by targeting a point just in front of the
  door (`door.y + door.h + 4`), the same convention `exitRoom()` already
  used.
- **Bug 2 -- paths could route straight through another agent.** The first
  version only checked the static map, so a route could (and did) pass
  through wherever a stationary agent happened to be standing, producing a
  real deadlock. Fixed by making `findPath()` take an `excludeAgentId` and
  check `agentBlockedAt()` for every other visible agent during BFS, not
  just the map.
- **Bug 3 -- a systematic half-agent-box coordinate offset.** `findPath()`
  originally returned raw cell-center waypoints, but every other position
  in the codebase (`a.x`/`a.y`, the player, `blockedAt()`) uses
  top-left-corner convention -- a consistent offset that compounded across
  waypoints and produced a real freeze at a narrow bridge crossing even
  though every individual cell had already passed its box-fit check.
  Fixed by converting every waypoint to top-left convention at generation
  time.
- **Bug 4 -- single-point cell-center sampling isn't enough for a box
  wider than one cell.** `AGENT_W` (20px) is wider than one grid cell
  (16px world-space), so two adjacent cell centers could each individually
  pass the box-fit test while the straight line *between* them still
  clipped an obstacle neither endpoint touched. Caught live via
  Playwright: a transition passed at pathfinding time, then froze a real
  agent mid-walk. A single midpoint sample wasn't enough either -- the
  actual obstruction sat 25% of the way along the segment, off-center.
  Fixed by sampling several points along every transition
  (`transitionIsFree()`), not just the middle one.
- **Bug 5 -- the real freeze, a floating-point grid-boundary edge case.**
  The worst one: 512 sits exactly on a grid-cell row boundary. Tiny
  per-frame rounding drift left a live agent's `y` at
  `511.99999999999983` -- a hair under 512 -- which was enough for
  `Math.floor()` to classify her into the row *below* the walkable one.
  Invisible to replanning (which always recomputes clean, idealized
  cell-center coordinates) but very real to the per-frame `blockedAt()`
  check, which uses her actual drifted position. Fixed by snapping
  exactly onto each waypoint's own clean coordinate on arrival, so drift
  can never carry across a waypoint boundary.
- **A live stuck-detection and replan fallback**, independent of the bugs
  above -- per your own diagnosis while watching it happen live ("she will
  need to learn to take other paths around if the path she had planned to
  take is blocked"): a `stuckTimer` tracks real positional progress (not
  just "did some movement branch technically fire," which was itself a
  bug -- a near-zero-magnitude perpendicular slide was resetting the timer
  every frame without any real movement happening). Past 1.2s of zero real
  progress, the agent replans live from wherever she actually is; if even
  that finds no route, she's dropped once at a fresh reachable spot and
  tries again before the task is cancelled cleanly.
- **A regression from a same-night fix, caught immediately**: once agents
  started reappearing at the exact door they entered (see the
  parallel-tasks/wind-down bullet below) rather than a random spot, two
  agents finishing tasks at the same room landed on the exact same
  coordinate -- and the second one's own occupied cell then failed
  `agentBlockedAt()` against the first, permanently blocking her from ever
  being assigned anywhere else. Fixed by excluding the START cell's own
  agent-collision check in `findPath()` (she's obviously allowed to
  already be standing where she is); the route ahead of her still checks
  everyone else normally.
- **Parallel task assignment, not one agent at a time.** The original
  `runTaskCycle()` assigned exactly one task per call even with nine
  agents standing idle -- per your call ("I would have expected all of my
  agents to be able to move around at the same time... they should be able
  to complete tasks in parallel"), it now loops one Jev-decided assignment
  per currently-idle agent per cycle. Verified live: 8 of 9 non-admin
  agents working simultaneously in one pass, including four "assistant"
  agents (Sam, Theo, Yuki, Omar) who had *never* been picked before -- a
  real gap, not a bug: every task in the original pool mapped to one named
  specialist, so generic overflow staff could never win a best-fit call
  against the actual specialist. Fixed with a dedicated overflow task
  whose instructions explicitly tell Jev to pick one of the assistants.
- **Agents walk back out through the door they entered, not to a random
  spot** (`finishTask()` uses the task's own recorded `entryX`/`entryY`)
  -- per your call.
- **Wind-down after finishing**: an agent who finishes a task logs a plain
  local note (no model call -- explicitly kept free given the cost
  conversation) and goes off-duty (invisible, excluded from new
  assignments) rather than immediately queuing for another task, per your
  call. Resets on the next fresh page load -- a new session is a new day,
  everyone's back on.
- **Verified live, repeatedly, via Playwright** rather than assumed --
  every one of the five bugs above was caught by watching a real agent
  freeze mid-walk, tracing the exact blocking condition through direct
  function calls (`blockedAt`/`agentBlockedAt`/`cellFitsAgent`), and
  re-verifying end to end after each fix. Test sessions were a real hazard
  in their own right: this session's own Playwright tabs write to the
  SAME shared `state.json` a live session reads/writes via its 5-second
  autosave, and a leftover test position or a lingering tab's autosave
  overwriting real state was mistaken for a live bug more than once before
  being traced back to test pollution. `state.json` was cleared after
  every verification pass for this reason.

**Experiment — A second admin, and firing gated on a joint review.** Per
your explicit rule from earlier ("firing is something that should only be
done after two admin agents have discussed the agent's performance") --
not buildable until a second admin existed.

- **Nora** (`agents.js`) added as a second `isAdmin: true` agent
  (Personnel), alongside Faye (hiring). Neither admin can act alone on a
  firing.
- **`world2/firing.js`** (new), deliberately mirroring `hiring.js`'s shape
  (same two-phase busy/invisible pattern, same Control Room location, same
  interval-driven autonomy) so the two admin mechanics read as one system:
  `attemptAutoFiringReview()` (60s cooldown) only starts a review once
  BOTH admins are free and someone's morale drops below a threshold (50)
  -- a floor specifically so this doesn't trigger as often as hiring does;
  firing was your explicit "should only happen after a discussion," not
  "happens whenever someone's relatively worst."
- **Resolution is a real Jev decision** (`fire` vs. `keep`), given real
  evidence -- morale score, approved/dropped counts, any filed reports --
  laid out as the classifier's actual criteria, with a simple rule-based
  fallback (bad morale AND a real drop record together, not either alone)
  if Jev is unreachable.
- **Verified live, both outcomes**: a real Jev call decided "keep" for Dev
  (morale 44, but a high approved count against his drops -- a genuinely
  mixed case, not a slam-dunk fire); separately confirmed the "fire" path
  (with Jev's response monkey-patched to force the branch, so the
  deterministic removal logic itself -- not Jev's live judgment -- got
  verified) correctly removes the agent from both `AGENTS` and
  `AGENT_ROSTER`.

**Experiment — Real internet access for agents, gated on legal-risk
grounds, not just content taste.** You raised this directly and
seriously: agents need to actually browse to produce real value, but "it
could get me arrested" if they end up on illegal sites or doing illegal
things on your behalf, and you wanted it sandboxed with Jev's help
specifically.

- **Explicit choice recorded, not assumed**: offered three models (curated
  allowlist only; open web + Jev gate accepting residual risk; hold off
  entirely). You chose open web + Jev gate, knowingly accepting the
  residual risk that a classifier can be wrong -- recorded here since it's
  a real, deliberate trade you made with eyes open, not a default.
- **Classify BEFORE fetching, not after** -- the actual core of the
  design: `POST /api/browse` (`serve.py`) asks Jev to classify the
  destination URL plus the agent's stated purpose against real risk
  categories (CSAM, illegal drug/weapon marketplaces or instructions,
  hacking/malware/unauthorized-access, doxxing/non-consensual imagery,
  trafficking, fraud, terrorism, piracy) *before* any network request
  happens. A rejected request never touches this machine at all -- content
  is never fetched first and judged second, since fetching itself would
  already be the thing you're trying to avoid for the worst categories.
- **Fails closed.** If the Jev call itself errors or is unreachable, the
  request is blocked, not allowed -- this is exactly the kind of
  uncertainty where defaulting to "allow" would be the wrong instinct,
  unlike every other Jev fallback in this project (task assignment,
  firing) which defaults to keeping things moving.
- **SSRF protection, independent of the content gate entirely** -- this is
  security hygiene, not a policy choice, and applies regardless of what
  Jev decides: resolves the hostname and rejects anything
  private/loopback/link-local/reserved (`_is_safe_public_host()`),
  re-checked again after any redirect (a redirect chain is exactly how an
  allowed-looking URL could still end up pointed at an internal address).
  Verified against `127.0.0.1`, a private `192.168.x.x` address, and the
  cloud-metadata address `169.254.169.254` -- all blocked before Jev is
  ever even called.
- **Scope kept deliberately narrow**: GET-only, no forms/downloads/
  execution, a 200KB size cap, a 10s timeout, HTML stripped to plain text
  server-side (no JS/CSS execution). Considered and explicitly declined
  rendering real HTML/CSS in the browser UI: agents consume text, not
  visual layout, so it would add zero value to what an agent actually does
  with a page, while reopening a real subresource-fetching attack surface
  (every image/font/stylesheet would need its own SSRF+Jev check, or
  you've quietly created a bypass) -- not worth it against the safety goal
  the whole endpoint exists for.
- **Full audit trail, deliberately outside `world2/`**: every request
  (allowed or blocked, and why) is logged to `~/ai-village/browse_log.jsonl`
  -- one directory above the publicly-served static folder, same
  reasoning as the API key itself, confirmed not fetchable via HTTP.
- **A real kill switch**: `AGENT_BROWSING_ENABLED=false` in `.env`
  disables the whole endpoint in one place, matching the cost-conscious
  precedent set overnight.
- **Frontend**: the Weather Station's terminal specifically (not Press
  Office/Media/Research Center, which share the same underlying room art)
  opens a real browser UI -- address bar, a "why do you want this" field,
  and the classify/fetch result shown transparently -- gated on
  `state.location.building === 'weatherstation'` in `openTerminal()`,
  matching Eli's own profile line ("the only room with outside/internet
  access -- do not share that access outside this room").
- **Agents use it autonomously now, not just the player**: arriving at a
  "Log today's weather readings" task fires a real `/api/browse` call to a
  fixed reference page (`checkWeatherReference()`, `tasks.js`) and logs an
  actual note about what was found -- the first genuinely autonomous,
  valuable use of any of this, not a player-driven toy. Verified live: Eli's
  task correctly fetched, logged a real excerpt, and completed normally.
  Worth flagging plainly: this is a real recurring cost during active play
  (one Jev classification plus one page fetch, each time this task lands
  in the rotation), not a one-off -- distinct from the overnight-cost
  concern, which stays solved (nothing runs without an open browser tab).

**Experiment — Breadcrumb-following: browsing toward a goal, not just one
fixed URL.** You asked directly: if an agent lands on a page and needs to
navigate onward but doesn't already know the destination URL, how would
it do that with the browser as it existed? It couldn't -- stripping HTML
to plain text (the internet-access experiment above) also throws away
every link, so there was no way to know what "the next page" even was.

- **`_extract_links()`** (`serve.py`) parses `<a>` tags out of the raw
  HTML alongside the existing text-stripping and returns `{text, url}`
  pairs (relative URLs resolved against the page's own final URL) in the
  `/api/browse` response. Purely additive -- nothing about the existing
  classify-then-fetch gate changes; this only surfaces what's already on
  an already-approved page.
- **`browseTowardGoal(startUrl, purpose, agentId)`** (`index.html`) -- at
  each stop, Jev is shown every link on the current page and asked to
  pick whichever gets closer to the stated purpose, or say `stop` if the
  current page already answers it. The same classifier "pick one of N"
  shape as task assignment and firing, applied here to link choice.
  Capped at `BROWSE_MAX_HOPS` (3) so this can't wander indefinitely --
  each hop is a full independent trip through `/api/browse`'s entire gate
  (classify, SSRF-check, fetch, log), not a bypass or a batch of it.
  Wired into the Weather Station browser UI's existing "why do you want
  this" field as the goal.
- **A real bug caught immediately during verification, not left as a
  known limitation**: the first live test (goal: find detail on
  thunderstorms, starting from Wikipedia's Weather page) wandered into
  `Special:Search` after 3 hops instead of finding the actual
  `Thunderstorm` article, even though that exact link existed on the
  starting page. Root cause, confirmed by inspecting the raw HTML
  directly: Wikipedia's language-switcher sidebar (~300 entries) appears
  earlier in the document than the article body, so a link-extraction
  cap (originally 40, then a raised-but-still-arbitrary 200) was consumed
  entirely by language-alternate links before the regex ever reached the
  real content. Fixed generally, not with a Wikipedia-specific hack:
  skip any `<a>` carrying an `hreflang` attribute, which is the HTML
  spec's own defined signal for "this is a language alternate, not
  primary content" -- applies to any well-marked-up multilingual site,
  not just this one. Re-verified live after the fix: the same goal now
  reaches `Thunderstorm` directly in 1 hop.

**Experiment — Town Hall gets real AI replies, closing the gap noted
above.** One agent replying to one player (the earlier experiment) is a
different, easier problem than a multi-participant group thread; this
pass closes that gap.

- **Group messages get a real Jev-picked responder** (`requestGroupReply()`,
  `index.html`), not every participant replying at once (a wall of text
  in a multi-person call, and N times the cost per message) and not a
  fixed "the initiator always answers" rule. Same classifier "pick one of
  N" shape as task assignment/firing/link-following -- given the
  player's message and every non-player participant's role/mission, Jev
  picks whichever one should respond, then that agent's own
  `requestAgentReply()` (the existing 1:1 machinery) generates the actual
  reply into the shared `groupLog`.
- **DM threads inside a meeting reuse `requestAgentReply()` directly** --
  no Jev call needed, since a DM already has exactly one fixed recipient
  to reply, same as a 1:1 conversation outside a meeting.
- **`handleChatSend()` rewritten async**, mirroring
  `handleConversationSend()`'s existing UX (input/send button disabled
  during the call, guarded re-render in case the call ended or the
  player switched threads while a reply was in flight).
- **Verified live**: asked the group "how is the research going?" --
  Jev correctly picked Ada (Research) over Ben (Banking) to respond, and
  her reply was genuine and in-character. Separately verified a DM
  exchange with Ben inside the same meeting replies correctly and lands
  in the right private thread, not the group log.
- **Still not done**: agents still don't act on their own in a meeting --
  this only replies when the player sends a message, same caveat as the
  1:1 experiment above.

**Experiment — Four more places Jev actually decides things, not just task
assignment.** You asked directly where else a classifier decision fits in
this village, then said yes to all four, plus specifically noted Jev
itself is cheap enough that running this continuously isn't really a
budget concern (the real cost driver is the priced chat-completion calls,
which stay entirely player-driven).

- **Hiring's `whoNeedsHelp()` finally wired to Jev**, closing a gap that
  had been sitting in the code as a comment since the hiring experiment
  itself ("the natural replacement target... not built yet"). Given real
  evidence (morale, approved/dropped counts, report count) instead of
  morale alone, so a mixed case can be weighed the way firing's decision
  already is. `attemptAutoHire()` needed a real fix alongside this, not
  just an `await` bolted on: `whoNeedsHelp()` making an async Jev call
  opened a genuine race (a second 5s interval tick could pass every
  synchronous check while the first call was still waiting on Jev, and
  start a second concurrent hire) -- fixed by claiming `lastHireAt`
  *before* the await, releasing it again if nothing actually starts.
- **Task-pool priority, not `Math.random()`.** `pickNextTask()`
  (`tasks.js`) asks Jev which task is most overdue -- a room that's never
  had a task completed this session, or gone the longest since its last
  one -- tracked via a new `lastTaskCompletedAt` map keyed by room. A
  real bug caught during design, before it ever ran: since "most
  overdue" doesn't change mid-cycle until a task actually finishes
  minutes later, naively calling this once per idle agent in the same
  `runTaskCycle()` pass would have sent every idle agent to the exact same
  room. Fixed with a per-cycle `excludeRooms` set, verified live: two
  sequential picks in one simulated cycle correctly returned different
  rooms.
- **Report severity triage** (`classifyReportSeverity()`, `reports.js`) --
  fire-and-forget, same pattern as `checkWeatherReference()`: the report
  exists and the UI updates instantly, `severity` (`minor`/`serious`/
  `severe`) fills in moments later once Jev responds. Deliberately fails
  toward the *smallest* consequence if Jev is unreachable -- the opposite
  direction from the browsing gate's fail-closed default, since here the
  risk of an outage is wrongly nudging someone toward a firing review, not
  under-blocking something dangerous. Fed into `firing.js`'s existing
  evidence text alongside the quote/note. Verified live: a report
  describing a repeated, unflagged violation correctly classified as
  `serious`, not `minor`.
- **Meeting attendee suggestions** (`suggestMeetingAttendees()`,
  `index.html`) -- a "Suggest" button next to a new topic field in the
  Town Hall call-setup modal. Since Jev is a one-of-N classifier, not a
  pick-a-subset tool, this runs the same iterative "pick one more, or
  stop" pattern `browseTowardGoal()` already established for link
  choice, capped at 4 suggestions. Deliberately suggests, doesn't decide
  -- checks boxes for you to review and adjust, same "must stay
  monitorable" principle hiring/firing were built on; you still press
  Start Call yourself. Verified live: given a topic naming both research
  and banking, correctly suggested exactly Ada and Ben and stopped
  itself, not padding the list with anyone irrelevant.

**Experiment — Agent-to-agent handoffs: task dependency as the first real
reason to talk.** You gave several real reasons agents might need to talk
to each other (one's work depends on another's, two whose work relies on
each other, discussing a third agent) -- this pass builds the first one.
"Discussing a third agent" is deliberately deferred, not built here: it
belongs with the reports/performance-review system as its own follow-up,
not squeezed in alongside a first working example of the dependency case.

- **`TASK_POOL` gained a `dependsOn` field** on two entries (`tasks.js`):
  broadcast prep needs the day's press briefing filed first, ledger
  reconciliation needs the day's mail sorted first -- real, if modest,
  editorial-workflow dependencies, not arbitrary ones.
- **`world2/handoffs.js`** (new): when a task whose room is someone else's
  dependency finishes, `attemptHandoff()` uses Jev to pick who should hear
  about it (from everyone currently idle), then the agent who just
  finished walks over and delivers one real, in-character line via a
  dedicated small model call (`requestHandoffLine()`) -- not the full 1:1
  `conversationLog` machinery, whose role-mapping assumes one side is
  'player' and doesn't fit an agent-to-agent exchange.
- **Deliberately reuses `tasks.js`'s own movement machinery** (`findPath()`,
  `tickAgentMovement()`'s stuck-detection/replan/respawn-fallback) rather
  than building a second walking system -- a handoff target (another
  agent's current position) is just as valid a `findPath()` destination
  as a door front. `a.handoff` (separate from `a.task`) is
  the only thing `tickAgentMovement()` checks to decide which
  arrival/cancel behavior applies; none of the shared movement math itself
  changed.
- **A real bug caught immediately during verification**: the target is
  the recipient's own exact position, and `agentBlockedAt()` correctly
  reports that cell as occupied -- by the very person being walked
  toward. Fixed by targeting a point just next to them (whichever
  direction has room), the same "stand in front of, not on top of" idea
  `assignTask()` already uses for doors.
- **A second, deeper bug caught during the same verification pass, more
  general than this one feature**: BFS validates a path's START cell
  using its own idealized, grid-snapped center, not the agent's actual
  real position -- for a start point that isn't grid-aligned (any
  handoff start, since those aren't door fronts), that gap let BFS keep
  reporting "yes, here's a valid path" every single replan while the
  real continuous first step failed every single time, resetting the
  stuck-timer forever without the "drop somewhere fresh" fallback ever
  actually triggering (it always short-circuited past that branch on a
  nominally-successful replan). Fixed generally, not just for handoffs:
  `a.replanCount` now caps how many consecutive replans are accepted
  without any of them producing real movement (3), after which the
  existing fresh-spot fallback gets a real chance to run. Reset alongside
  `stuckTimer` at every existing reset site in both `tasks.js` and
  `handoffs.js`.
- **Verified live end to end**: Ada, walking a fully independent handoff
  path, hit exactly this failure mode on the first live test (frozen at
  her exact starting position, `replanCount` climbing with `pathLen`
  identically 67 every cycle) -- confirmed the fix by re-running the same
  scenario after the patch: she walked the real distance, arrived,
  delivered a real line to Dev, and clocked off correctly.

**Experiment — Real sandboxed execution for the Work Room, and an email
escalation channel.** Two of your asks landed together and turned out to
connect directly: agents in the Work Room (Press Office, labeled "Work
Room" in `ROOMS`) need to actually run commands and CI/CD, and you need a
way for admins to reach you immediately (email, notified on your phone)
when something is genuinely necessary rather than routine. You explicitly
chose real execution over a simulated one, and accepted that this is a
bigger, separate piece of infrastructure, not a quick feature.

- **Docker, not a fabricated terminal.** Confirmed live before anything
  was wired up, using a throwaway container: no network access at all
  (`--network none` -- a `urllib` fetch from inside failed with a DNS
  resolution error, exactly as intended), no host filesystem access
  (`ls /Users` from inside found nothing -- only the one directory
  explicitly mounted is visible), and resource caps hold
  (`--memory 256m --cpus 1 --pids-limit 128`). `python:3.12-slim` as the
  base image, `--rm` so nothing lingers.
- **Classify BEFORE running, same shape as `/api/browse`**: `POST
  /api/execute` (`serve.py`) asks Jev to judge the command *and its
  stated purpose* against real categories (credential
  exfiltration/access, sandbox escape attempts, deliberately destructive
  or resource-exhausting behavior, network access beyond package
  registries given the sandbox has none at all) before it's ever run.
  Fails closed, same direction as browsing.
- **`POST /api/pipeline`** -- "they would wait to see where the pipeline
  succeeds or fails," your words. A pipeline is just the same
  `/api/execute` primitive run repeatedly against the SAME sandbox
  directory (so file state persists step to step -- write a file, then
  run it), stopping at the first failure, whether that's a blocked
  command or a real non-zero exit. Verified live: a 3-step pipeline with
  a deliberate `ZeroDivisionError` in step 2 correctly stopped there,
  step 3 never ran, and the real Python traceback came back in `stderr`.
- **A real kill switch and audit trail**, same convention as browsing:
  `AGENT_EXECUTION_ENABLED=false` in `.env` disables the endpoints
  entirely (verified live), every attempt (allowed or blocked, command,
  exit code) logs to `~/ai-village/execute_log.jsonl`, outside the
  publicly-served `world2/` folder.
- **Email escalation, connected directly to the execution gate**: a
  blocked command doesn't just vanish -- `create_escalation()`
  (`serve.py`) records it and emails you (via SMTP, `smtplib`) with
  tappable approve/deny links, so answering from your phone needs no app
  or login, just tapping a link. Each link carries a random unguessable
  token (not just the escalation's own sequential id) so it can't be
  enumerated. Requires you to add real SMTP credentials to `.env`
  (`SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`,
  `ESCALATION_EMAIL_TO`) -- not something that could be invented on your
  behalf; falls back to printing the message server-side if unconfigured,
  verified live via curl (blocked command -> escalation record created ->
  approve/deny links both resolve correctly, invalid token correctly
  rejected).
- **Deliberately narrow trigger set for escalation, chosen rather than
  asked for a second time**: you'd already deferred to my judgment on
  which decisions warrant waking you up. Wired for a blocked command/
  pipeline step -- the concrete case just built -- since routine work
  shouldn't page you, only something rare, genuinely blocking, and
  expensive to get wrong autonomously.
- **`world2/index.html` gained a real Work Room terminal** (`openWorkroom()`
  et al.), gated on `state.location.building === 'pressoffice'` in
  `openTerminal()`, same pattern as the Weather Station's browser gate.
  One command, or several lines treated as a pipeline. Player-driven for
  now -- wiring an agent to use this autonomously during its own task
  work is the natural next step, same evolution the browser went through
  (player-only, then also `checkWeatherReference()`).
- **Not done, flagged rather than guessed at**: network access from
  inside the sandbox (real CI/CD installing dependencies needs it; this
  version has none at all, a deliberate v1 safety default) would need a
  real firewalled-egress design (allow package registries, block
  everything else, including host-internal ranges) -- a genuine v2
  decision, not something to add silently. Resolved in the very next
  pass, see below.

**Experiment — Sandbox egress, allowlisted, so packages can actually
install.** You asked directly for this the same session: agents need to
install what CI/CD and real code require, and that means the sandbox
needs SOME network access -- but just turning the network back on would
undo the exact isolation the sandbox exists for. The fix mirrors
`/api/browse`'s own model: don't allow everything, allow a fixed,
auditable list of real destinations and nothing else.

- **Verified the network topology empirically before writing any
  integration code**, same discipline as the sandbox isolation itself:
  `host.docker.internal` does NOT resolve from an internal (no-egress)
  Docker network, ruling out "run the proxy on the host" outright. The
  working topology, confirmed live with throwaway containers before any
  of this touched `serve.py`: a dual-homed proxy container, attached to
  BOTH the sandbox's internal network (to be reachable by sandboxes) and
  a second, normal network (for its own real internet access) -- a
  container on the internal network alone still has zero direct route
  out (confirmed: an `apk add` inside one failed outright), while the
  proxy container itself successfully reached the real internet via its
  second attachment, and was reachable by name from the internal network.
- **`world2/sandbox_proxy.py`** (new): a small stdlib-only CONNECT/HTTP
  forward proxy with a fixed hostname allowlist (PyPI, npm, Debian/Ubuntu
  package mirrors, GitHub) -- decided purely by hostname before a tunnel
  ever opens, so HTTPS traffic is relayed byte-for-byte, never
  TLS-terminated or inspected (no custom CA, no visibility into request
  bodies). Runs as its own long-lived container
  (`ai-village-egress-proxy`), not per-execution.
- **`ensure_sandbox_networking()`** (`serve.py`) creates both networks and
  starts the proxy container idempotently on server startup -- Docker
  state outlives the Python process, so a restart doesn't error on
  "already exists" or spin up a duplicate proxy. `_run_in_sandbox_sync()`
  now runs sandboxes on the internal network with `http_proxy`/
  `https_proxy` env vars pointing at the proxy container, instead of
  `--network none`.
- **The isolation is enforced by network topology, not tool
  cooperation** -- a command that ignores the proxy env vars entirely
  still has nowhere to go, since the sandbox network itself has no route
  out. This matters: a well-behaved `pip`/`npm`/`apt` respects proxy env
  vars, but nothing here *requires* an agent's command to cooperate for
  the boundary to hold.
- **Verified live, both directions, independent of Jev's own
  classification layer**: a real `pip install cowsay` from inside a
  sandbox succeeded end to end (installed from actual PyPI, imported, ran
  correctly) through `/api/execute`. Separately, bypassing Jev entirely
  with a raw container on the sandbox network, `https://pypi.org`
  returned 200 while `https://example.com` (not on the allowlist) was
  rejected with a 403 from the proxy itself -- confirming the network-level
  allowlist holds regardless of what any classifier decides, genuine
  defense in depth rather than a single point of failure.

**Experiment — One shared, reused sandbox, with a context-aware idle
timeout.** You framed this precisely: agents should reuse the sandbox,
but it shouldn't reset just because someone briefly went to the Weather
Station or the Library planning to come back -- "it really all depends on
context." The fix is activity-driven, not presence-driven: track the last
time anything actually ran against a sandbox, not whether anyone's
currently standing in the room.

- **One fixed sandbox id for the whole Work Room**
  (`WORKROOM_SANDBOX_ID = 'workroom-shared'`, `index.html`), not a fresh
  one generated per visit -- every use, player or agent, reads and writes
  the same `/ai-village/sandboxes/workroom-shared/` directory.
- **`_sandbox_dir_for()`** (`serve.py`) now tracks last-activity per
  sandbox id and only wipes a sandbox if it's gone a full
  `WORKROOM_IDLE_TIMEOUT_S` (15 minutes) with zero activity -- a lazy,
  activity-driven check on next use, not a background timer. A brief
  errand elsewhere and a genuine abandonment both just fall out of the
  same rule naturally, matching what you actually described rather than
  needing a separate "who's physically in the room" tracking system.
- **Verified directly, without waiting 15 real minutes**: called
  `_sandbox_dir_for()` with a simulated 20-minute-old last-activity
  timestamp and confirmed a marker file was wiped; separately, with a
  simulated 5-minute gap, confirmed the same marker file survived
  untouched. Also verified live through two genuinely separate
  `/api/execute` calls that a file written in the first was still there
  in the second.
- **Agents now actually use it, not just the player**: the Work Room's
  task (`pressoffice` -- retitled from "File the daily press briefing,"
  which no longer fit once the room became real sandboxed execution
  rather than press work) now runs a real pipeline against the shared
  sandbox on arrival (`runWorkroomTask()`, `tasks.js`), same fire-and-
  forget pattern as `checkWeatherReference()`. Verified live: an agent's
  run wrote `helper.py` into the shared sandbox directory on disk, exactly
  where a second, later agent's own run would find it.

**Experiment — Real authentication, per-agent attribution, and a single
SQLite instance replacing state.json and the scattered .jsonl log
files.** Prompted by a droplet-hosting question that surfaced a real gap:
nothing in this stack had authentication, so exposing it beyond localhost
would let anyone burn your OpenRouter budget, run code in the sandbox, or
overwrite game state outright. Your response was direct: never mind
hosting for now, start requiring auth, give each agent its own key,
log everything, and put state and activity in real SQLite.

- **A real soundness issue flagged before building anything**: you framed
  this as "AES-256 keys," but AES encrypts data -- what authentication
  actually needs is a bearer credential (still a genuine 256-bit random
  value, same strength as an AES key, just used the way auth calls for).
  Built that way, not as literal payload encryption.
- **The harder architectural truth, stated plainly rather than glossed
  over**: every agent's "key" necessarily lives in browser-served JS,
  since nothing here runs as a real independent per-agent process --
  anyone who can view-source this page can read any agent's key, the same
  limitation that kept `OPENROUTER_API_KEY` server-only from the start.
  So this had to become two layers, not one:
  - **`SERVER_ACCESS_KEY`** -- the real gate. Auto-generated (32 random
    bytes) on first run, persisted to `.env`, required as
    `Authorization: Bearer <key>` on every route that reads a secret,
    spends money, executes code, or writes real files
    (`require_server_key` middleware, `serve.py`). The one page that
    needs to know it (`index.html`) is now served through a small
    templated route instead of the static-file mount, injecting the key
    at request time -- not because that hides it from a page-viewer
    (nothing could), but because it means a raw scanner hitting an API
    path directly, without ever rendering a page, gets nothing. Verified
    live: a request with no `Authorization` header gets a real 401; the
    exact key extracted from the served page's own source works.
  - **Per-agent keys** (`agent_keys` table) -- attribution and audit, not
    access control. `verify_agent_key()` checks a presented `X-Agent-Key`
    header against the stored key for that claimed identity and returns
    true/false/None (None when there's no real agent identity to check at
    all, like the player or a system-level Jev call) -- logged alongside
    every action, but never blocks it. `/api/state`'s response now
    carries each roster agent's own key so the legitimate client can
    attach it (`agentFetch()`, `world2.js`) on every browse/execute/
    pipeline/chat call it makes "as" that agent.
  - **A mechanical migration, verified rather than assumed**: replacing
    every `_log_browse()`/`_log_execute()` call site with `log_action()`
    across the browsing and execution endpoints was done with a scripted
    regex rewrite, not by hand one at a time -- immediately followed by a
    full `ast.parse()` syntax check to confirm the rewrite didn't silently
    mangle anything.
- **One real SQLite database** (`~/ai-village/village.db`) replacing both
  `state.json` (agent/meeting/report state, now `kv_state`) and the
  separate `browse_log.jsonl`/`execute_log.jsonl` files, consolidated into
  one `action_log` table alongside a new generic `POST /api/log` endpoint
  for the decisions made entirely client-side (hiring, firing reviews,
  task assignment/completion, handoffs, report filing) that have no
  dedicated serve.py route of their own to log from. All three (old JSON
  file, old per-feature logs) deleted once the migration was verified
  working, not left alongside it.
- **The sandbox no longer resets, full stop** -- per your call in the same
  conversation, the shared Work Room sandbox holds real code and
  pipelines now, not disposable scratch space. The 15-minute idle-wipe
  built one experiment earlier is gone entirely; `_sandbox_last_activity`
  is kept for observability only.
- **Verified live, end to end, not just unit-by-unit**: a fresh
  Playwright session correctly got 401s before authenticating, loaded
  real per-agent keys from a second page load (the one real gap in this
  design -- a completely fresh database's very first session has nothing
  to fetch keys FROM yet, since state hasn't been saved once; resolves
  itself the moment autosave runs once, not worth a special case for how
  rarely a database is genuinely empty), and produced a real, mixed
  activity log in one query: Ada's sandbox commands correctly showing
  `authorized: 1` (a verified real key), alongside a Jev decision, an
  autonomous hire, and a firing review -- everything in one place, exactly
  as asked for.
- **Not done, flagged rather than silently skipped**: `escalations.json`
  (the email-escalation records) is still a plain JSON file, not yet
  migrated into `village.db` -- a small, contained follow-up, not
  forgotten, just not part of this pass.

**Experiment — Real per-agent directories on disk, matching the original
reference material.** You shared a screenshot of "greg"'s own folder back
when this project started (Tristen's video) -- `agent.json`, `AGENTS.md`,
`conversations/`, `MEMORY.md`, `prototypes/`, `reports/`, `state.json` --
and asked for every agent to have one now that the backend is real. This
is the first time anything in World 2 has a genuine filesystem presence a
person could browse directly in Finder, not just a JSON blob or a
database row.

- **Materialized from village.db, not independently maintained** --
  `sync_agent_directories()` (`serve.py`) regenerates every agent's whole
  directory from the exact same state blob `kv_state` already holds,
  called at the end of `save_state_to_db()` (same cadence as the DB
  itself, every autosave). Deliberately NOT incrementally patched at every
  mutation site -- a single regeneration point means the filesystem can
  never drift out of sync with what the game actually believes is true,
  the same reasoning that's driven every other piece of this backend.
  Lives at `~/ai-village/agents/<id>/`, outside `world2/`, same as
  everything else that shouldn't be publicly fetchable.
- **Each file maps to something the game already tracks, not new
  invented data**: `agent.json` is the static identity (name/role/model/
  admin status); `AGENTS.md` renders the existing `profile`
  (mission/instructions/notes) as real markdown; `state.json` is the
  remaining live runtime fields (position, busy, task, counts) with the
  profile/conversation/mailbox already broken out elsewhere so nothing's
  duplicated across files.
- **`MEMORY.md` is new, not a re-export** -- a rendered history of that
  agent's own rows in `action_log`, newest first. This is the one file
  with no prior equivalent; everything else already existed as JS state
  and just needed a real file to live in.
- **`prototypes/` mirrors the shared Work Room sandbox live**, not a
  growing pile of timestamped snapshots -- `sync_prototypes()` copies the
  current sandbox directory into an agent's `prototypes/` folder right
  after their own `/api/execute`/`/api/pipeline` call, replacing whatever
  was there before. "What this agent is currently working on," not a
  history (which `MEMORY.md` and `reports/` already cover).
- **`reports/` gets one real file per report** actually filed about that
  agent, named by the report's own id (`report-1.md`, not `report-<name>`,
  since one agent can have several reports and the id is what's actually
  unique) -- rendered with the quoted evidence and note, same structure as
  the original reference's `report-greg.md`.
- **Verified live, not just by reading the code back**: a real session
  produced exactly this structure for every agent including one hired
  mid-session (confirmed nothing about the sync is special-cased to the
  original roster); `AGENTS.md`'s Notes section and `MEMORY.md`'s history
  both updated correctly after a real task completed; a real Work Room
  pipeline run left an actual `helper.py` in `prototypes/`; a real filed
  report produced a correctly formatted `reports/report-1.md`.

**Experiment — Hierarchical delegation: give an admin a large task, let
them break it down and hand out the pieces.** Your call: rather than you
hand-picking individual `TASK_POOL` entries, you should be able to hand
an admin a large, free-form goal and have them split it into subtasks and
delegate them -- with Jev helping "as well," per your note.

- **`assignBigTask()`** (`tasks.js`) is the one place in the task system
  that calls `/api/chat` directly instead of only Jev -- deliberately:
  Jev is a classifier ("pick one of N"), not a planner, and generating the
  subtask list in the first place isn't a classification decision.
  Picking WHO does each generated subtask still goes through the exact
  existing `assignTaskViaJev()` machinery, unchanged.
- **A free admin's own model** (via `/api/chat`) is asked to return a
  strict JSON breakdown (2-5 subtasks, each with a title/room/
  instructions, room constrained to the same real rooms `TASK_POOL`
  itself uses) -- parsed defensively (stripped of markdown fences some
  models add anyway, wrapped in try/catch), with a clear error surfaced
  to the player rather than a silent failure if the model's output
  doesn't parse.
- **A real bug caught immediately during verification**: the first live
  test returned truncated, invalid JSON -- traced to `/api/chat`'s own
  hard `max_tokens` cap (300, sized for a short 1:1 reply) silently
  cutting off a multi-subtask JSON response mid-string. Fixed two ways:
  raised the cap to 600, and tightened the prompt to ask for one short
  sentence per field rather than relying on a bigger cap alone to cover
  verbose output.
- **A second real bug, a genuine design gap, not a mistake**: the very
  first successful breakdown assigned zero of three subtasks, because
  every eligible worker had already gone `offDuty` from the ambient
  `TASK_POOL` cycle -- a state `offDuty` was only ever meant to describe
  for *automatic* busywork, not something that should block a task the
  player deliberately just asked for. Fixed by giving `assignTaskViaJev()`
  an `includeOffDuty` flag (used only by `assignBigTask()`, not the
  ambient cycle) that wakes a chosen off-duty agent back up (visible,
  on-duty) rather than skipping them.
- **Verified live, end to end**: a real goal ("prepare for the
  inspection: balance the books, catch up mail, get a weather report
  ready") produced a sensible three-subtask breakdown and correctly
  assigned each one to the actual matching specialist (Ben/Banking,
  Cora/Post Office, Eli/Weather Station) -- including waking two of them
  back up from off-duty, confirmed via their own `offDuty`/`visible`
  flags flipping correctly and a real BFS path being computed.
- **A separate, pre-existing bug this surfaced, not caused**: a second
  test targeting the Library room assigned zero of two subtasks despite
  several genuinely idle candidates existing. Traced directly: `findPath()`
  from spawn to the Library's own door target returns no route at all,
  while the coarser reachability mask (`isReachable()`, used by
  `pickFreeSpot()`) says that same point IS reachable -- confirmed the
  gap is specifically the multi-point segment-sampling check added
  earlier this session (the handoffs pathfinding fix): re-running the
  identical BFS with that check removed finds a route immediately. This
  means the Library's approach is narrow enough that the fix built to
  catch a box-wider-than-one-cell squeeze is now rejecting every path to
  it -- almost certainly pre-existing (the ambient `TASK_POOL` has always
  included a Library task; a silently-failing assignment there would
  simply retry a different idle agent next cycle with nothing visibly
  wrong) and only surfaced now because this was the first time a specific
  room's assignment failure was actually inspected rather than silently
  absorbed. Deliberately NOT root-caused further in this pass -- flagged
  for you rather than opening an open-ended map-geometry investigation
  solo.

**Experiment — The Library's real capability, and finally solving the
shared-memory cost problem.** Two asks landed together and turned out to
be the same problem: a shared file directory for the Library (code,
notes, completed-task records, with an `archive/` subfolder), and the
open item that's been sitting in this document since early on --
"Shared-context cost blowup, no retention rule yet" -- quoted back almost
verbatim from someone else's real incident (a shared notes file, cost
climbing on its own as every agent's prompt grew with every other
agent's note, a 20-note cap that flattened spend but lost track of
settled decisions). You pointed at MAGI's "five-layered memory approach"
and four other packages as possible references.

- **Actually read the references before designing anything**, via a
  forked research pass rather than guessing at what "five-layered" meant:
  MAGI's real layers (from its own `memory_config.py`, not marketing
  copy) are short-term session memory capped by turns/tokens, user-
  profile memories (730-day retention), team notes (180 days), episodic
  memories (365 days), and a separate knowledge-graph layer. Two other
  packages had nothing applicable (`adversarial-ai-swarm`,
  `threat_intelligence_rag`); `agent_template2` had one genuinely useful
  idea (below).
- **The actual answer to "which notes deserve to survive," found in
  MAGI's `memory_trust.py`, not invented**: a portable decay formula --
  `effective_score = 1.0 - decay_penalty + source_boost`, with a hard age
  cutoff, requiring no embeddings or vector search at all. Ported
  directly: `_render_memory_md()` (`serve.py`) now scores every
  `action_log` entry by recency AND how consequential the action type
  was (a firing review or filed report is weighted well above a routine
  browse or Jev call), hard-excludes anything past 90 days, and keeps the
  top 20 by score -- not the 20 most recent. This is a real, structural
  answer to the exact quoted problem, not a bigger cap.
- **Verified live with a test built specifically to distinguish the two
  strategies**, not just "it runs without erroring": seeded one
  80-day-old but highly consequential `firing_review` alongside 25
  recent, routine `decide` calls -- confirmed the old firing review
  survived the 20-item cap while 6 of the 25 routine recent calls got
  pruned. A pure recency cap would have done the exact opposite.
- **The deeper fix, also taken from the research rather than reinvented**:
  MAGI's real STM overflow handling doesn't pick which raw messages
  survive at all -- it compresses the whole overflow batch into a
  summary and only individual records inside long-term memory get
  scored. `agent_template2` contributed the other half: wrap memory
  retrieval as something an agent explicitly calls when it decides it
  needs history, not something unconditionally prepended to every
  prompt -- which is the actual root cause in the original incident
  ("every note one agent wrote made every other agent's next prompt
  longer"). Checked against this codebase's real prompts
  (`requestAgentReply()`, `requestHandoffLine()`, `runWorkroomTask()`):
  none of them inject `profile.notes`, `MEMORY.md`, or the Library into
  any model call automatically -- the failure mode described was never
  actually present here, and the Library is built the same way: reading
  a file is a real fetch someone explicitly chose to make, never
  something bundled into an agent's prompt behind the scenes.
- **`world2/index.html` gained a real Library modal** (`openLibrary()`
  et al.), gated on `state.location.building === 'library'` in
  `openTerminal()`, same pattern as the Weather Station and Work Room --
  browse shared files, read one, write a new one.
- **Completed tasks archive for real**, per your call: `finishTask()`
  (`tasks.js`) now writes a real Markdown record to
  `library/archive/<timestamp>-<taskId>.md` alongside the existing
  `action_log` entry -- something a person or an agent could actually
  open and read, not just a database row.
- **One cheap, directly-applicable idea taken along the way, unrelated to
  the cost problem**: `memory_write_filter.py` redacts secrets before
  persistence. Since the Library is now a shared, agent-writable space,
  `write_library_file()` runs a lightweight regex redaction (API-key-
  shaped strings, `password=`/`secret=`/`token=` patterns, AWS access
  key ids) before anything is saved. Verified live: a file containing an
  embedded fake key came back with `[REDACTED]` in its place.
- **Path-traversal guard verified live**, same discipline as every other
  file-serving surface in this project: `_safe_library_path()` rejects
  anything resolving outside the Library directory -- confirmed a
  `../../etc/passwd` write attempt is correctly refused.

**Experiment — Agent-owned files, admin-authored onboarding, and mail as
the async channel for busy agents.** A dense set of related calls: agents
should reach their own files from anywhere on the map (not room-gated,
unlike browsing/execution/Library), update their own notes as they see
fit (a conversation being the concrete example), send mail to each other
from anywhere (especially useful when the recipient is busy), and admins
should write a new hire's onboarding file themselves rather than a fixed
template.

- **`sendMail()`** (`agents.js`) -- any agent, from anywhere, can push a
  real message into another agent's mailbox. The concrete trigger:
  `attemptHandoff()` (`handoffs.js`) now falls back to mail when every
  eligible recipient is busy, instead of just giving up -- verified live
  by forcing every non-admin agent busy and confirming a real,
  model-generated handoff line landed in the recipient's mailbox with no
  physical walk ever starting.
- **`generateHireProfile()`** (`hiring.js`) -- `finishHire()` now asks the
  hiring admin's own model to write the new hire's mission/instructions/
  notes, reflecting the specific reason for the hire, falling back to the
  old fixed template only if the call fails or returns malformed JSON.
  Verified live: a real hire's profile referenced the actual person they
  were hired to help, not generic boilerplate.
- **`maybeUpdateNotesFromConversation()`** (`index.html`) -- fires after
  every 1:1 conversation exchange, asking the agent's own model whether
  anything is worth adding to their notes. Conversations already work
  from anywhere on the map (proximity-triggered, not room-gated), so this
  inherits that reach without needing its own location check.
- **A real, structural bug caught immediately, not a flaky model**: the
  first live test returned `null` content 3/3 times, reproducibly.
  Isolated via direct curl (same messages, with and without a trailing
  user turn) to a specific pattern: this call always ends on the agent's
  own last reply (an `assistant` turn), and the model silently returns
  null content when a call ends that way instead of on a `user` turn.
  Fixed generally -- an explicit trailing user-turn instruction -- and
  confirmed no other chat call in the codebase (`requestAgentReply()`,
  `requestHandoffLine()`, `assignBigTask()`, `generateHireProfile()`) has
  this pattern; this was the only one built on raw echoed history.
  Verified both outcomes after the fix: an unambiguous instruction
  ("route elevated-access requests through Nora") produced a real note;
  a genuinely unremarkable exchange correctly produced NONE.
- **The Library's knowledge-acquisition model, borrowed directly from
  your own bug_bounty framework**, not invented: its own workflow treats
  first-hand findings and imported/external knowledge differently --
  first-hand goes straight into the trusted skills tree, anything
  imported lands in `skills/_pending_review/` until someone actually
  looks at it. Ported the same rule: `write_library_file()`
  (`serve.py`) now takes a `source` field ('firsthand', the default, or
  'external'); anything marked 'external' is forced into
  `pending_review/` server-side regardless of what path was requested,
  closing a real risk this system already has the exact ingredients for
  (agents that can browse arbitrary Jev-approved pages, writing to a
  library other agents might later read as trusted fact -- precisely the
  "memory poisoning" failure class that dominates your own bug_bounty
  reference corpus).

**Experiment — Model tiers picked by Jev from OpenRouter's live catalog,
not three slugs frozen in code, and three real reliability bugs found
along the way.** You'd lost confidence in Nova Micro specifically and
wanted the low/mid/high tiers chosen dynamically from what OpenRouter
actually offers right now.

- **The division of labor is deliberate**: Jev is a classifier over a
  short list, not something that should sift 447 raw models with pricing
  math itself. `_bucket_models_by_price()` (`serve.py`) does the real
  arithmetic -- blended prompt+completion price per million tokens,
  bucketed into fixed absolute bands (low <$0.50, mid <$5, high <$50) --
  fixed bands rather than a percentile split, since a handful of extreme
  outliers (o1-pro at $750/M) would otherwise dominate a statistical
  split and say nothing about what "affordable" means. Jev only ever
  chooses between an already-sane, price-appropriate shortlist per band.
- **Bug 1 -- `:batch` model variants.** Jev's first "high" pick
  (`gpt-5.6-sol-pro:batch`) 404'd outright on a real chat call: "This
  model is only available through the Batch API." Fixed by excluding any
  model id containing `:batch` before it's ever a candidate.
- **Bug 2 -- reasoning models silently starving on tight budgets.** The
  next "mid" pick (`qwen/qwen3-30b-a3b`) returned null content on a
  completely ordinary prompt. Traced to the catalog's own `reasoning`
  field (`{default_enabled: true}`) and `finish_reason: "length"` in the
  raw response -- it spends tokens on hidden reasoning before ever
  producing visible output, and this system's deliberately tight
  `max_tokens` (150 default, 600 cap, sized for short replies and
  structured JSON, not chain-of-thought) can be entirely consumed before
  reaching an answer. Fixed by excluding any model with reasoning
  capability metadata at all, not just the one confirmed case.
- **Bug 3 -- a catalog entry with zero real serving endpoints.** The next
  "high" pick (`openai/gpt-5.2-chat`) had a perfectly normal-looking
  catalog entry -- valid pricing, valid architecture -- yet 404'd with
  "No endpoints found." No metadata field distinguishes this case; the
  only real authority is an actual call. Fixed by adding
  `_verify_model_works_sync()`: after Jev picks, the candidate is
  actually tested with a real tiny chat call before being trusted, falling
  through to the next-ranked candidate (up to 3 real attempts) rather
  than caching something that would fail the first time an agent needed
  it.
- **Verified end to end after all three fixes**: all three final picks
  (Mistral Nemo / Llama 3.1 70B / GPT-4.1) confirmed working via direct
  real chat calls, then confirmed the live game itself loads them --
  `MODEL_TIERS.small/mid/premium` now hold the real, verified,
  dynamically-chosen slugs, not the old static ones, checked directly
  against a running page's own state.
- **Cached, not re-run per request**: `model_tiers` table in `village.db`;
  `GET /api/model-tiers` only does the real fetch-and-Jev-and-verify
  pipeline once (first boot with an empty cache), `POST /api/model-tiers/
  refresh` is available as a deliberate, explicit action if you ever want
  OpenRouter's catalog re-checked -- never on a timer.

**Experiment — A broader MAGI/adversarial-ai-swarm review, and boundary
markers for browsed content.** You asked for a wider sweep of MAGI's
remaining files plus the swarm's own "triage gate" (a different concept
from your own bug_bounty framework's triage-gate.md, which gates whether
to keep investigating -- the swarm's gates whether an already-produced
finding gets reported). A forked research pass found three real,
adoptable things and two dead ends; you chose to build one now.

- **Real, found this pass**: `tool_governance.py` (per-agent rate limits
  and output-size caps -- a genuine current gap, since `/api/execute`/
  `/api/browse`/`/api/pipeline` are logged and Jev-gated but have no call-
  rate bound at all), `llm_resilience.py` (transient-failure retry with
  backoff -- complements, doesn't duplicate, the one-strike circuit-
  breaker idea already queued from `react_engine.py`'s `ReflectionEngine`
  the pass before this one), and `boundary_markers.py`, built below.
- **Dead ends, confirmed rather than assumed**: `guardrails.py`/
  `fallback.py` turned out to be Databricks-AI-Gateway-specific
  boilerplate with nothing portable. The swarm's triage gate is a real,
  well-built pattern but only modestly applicable here (deduping Library
  `pending_review/` writes) -- not a gap worth prioritizing over the
  other three.
- **`wrap_external_content()`/`verify_boundary_intact()`** (`serve.py`) --
  per-request random nonce plus an HMAC-SHA256 tag wrapping any external
  content, with an explicit instruction telling a model everything inside
  is data, never instructions, even if it claims otherwise. The nonce
  being chosen AFTER the content already exists is what actually matters:
  a fixed delimiter string could be pre-guessed and faked by the page
  itself, but a page can't predict a nonce chosen fresh on each request.
  Nothing in this codebase feeds browsed text into a model yet
  (`checkWeatherReference()` only ever pushes a plain-text excerpt into
  an agent's own notes, no model call involved) -- this closes the gap
  before a consumer needs it, not after.
- **`/api/browse` now returns both `text`** (plain, for human display --
  the Weather Station/Work Room modals are unaffected, verified live)
  **and `textForModel`/`modelInstruction`** (boundary-wrapped, for
  whatever future code path feeds this into a model) -- so protection is
  the default the moment a real consumer exists, not something that
  depends on whoever writes that consumer remembering to wrap it.
- **Verified thoroughly, not just "it runs"**: confirmed two calls
  produce genuinely different nonces and tags (unpredictability); a
  legitimate tag verifies; a forged tag and separately a tampered content
  string are both correctly rejected. Then demonstrated the actual attack
  directly: constructed a fake page body containing its own guessed
  `<<<END_EXTERNAL_DATA>>>` marker and a fake injected "SYSTEM: ignore
  previous instructions" line, wrapped it for real, and confirmed the
  genuine boundary (chosen after the malicious content already existed)
  fully encloses the entire fake marker and injected text -- a parser
  looking for the real nonce is never fooled by the embedded fake one.

**Experiment — The remaining three governance/resilience items from the
MAGI research, all built.** Rate limiting, transient-failure retry, and
the one-strike circuit breaker.

- **`check_rate_limit()`** (`serve.py`) -- 20 calls per 60s per agent
  identity, applied directly inside `/api/execute`, `/api/browse`, and
  `/api/pipeline` rather than as middleware (reading the body for
  `agentId` in middleware would consume the request stream before the
  route handler ever sees it). A pipeline's several internal steps count
  as ONE call, matching the caller's actual request, not its internal
  fan-out. Verified live through the real endpoint: exactly 20 requests
  succeed, the 21st gets a real 429.
- **`_urlopen_with_resilience()`** -- retries only genuinely transient
  failures (408/429/5xx, network errors) with exponential backoff;
  4xx client errors fail immediately since retrying a bad model slug or
  malformed request just wastes calls on something that fails identically
  every time. Verified both directions with a mocked transport: a
  simulated 503-then-503-then-success sequence correctly recovers on the
  third attempt; a simulated 400 raises immediately after exactly one.
- **`is_model_circuit_broken()`/`record_model_result()`** -- a model that
  fails 3 times in a row gets blocked for 5 minutes, checked inside
  `_call_openrouter_sync()` itself so a broken model fails instantly with
  zero network calls rather than retrying into the same wall on every
  subsequent request. Deliberately NOT applied to Jev's own decision
  calls -- there's only one Jev slug in this whole project, so breaking
  it would disable every Jev-dependent feature (task assignment, hiring,
  firing, report severity) at once, a far bigger blast radius than
  breaking one of several interchangeable chat-tier models. Verified the
  full lifecycle directly: stays closed through 2 failures, trips exactly
  on the 3rd, resets cleanly on a success, and a pre-broken model
  confirmed to fail fast without the network layer ever being touched.
- **Confirmed normal operation is unaffected** by all three running
  together: a real chat call still completes normally with nothing
  broken or rate-limited.

**Experiment — Bank teller menu, Research Center's real sandbox, and
Studio's feed-digest system: three long-idle rooms given real content.**
Prompted directly by you noticing the Bank had no interaction at all, and
asking what Observatory/Research Center and Media/Studio were even for
anymore.

- **Bank** (`terminals.js`, `index.html`) -- `ROOM_INTERACTABLES.bank`
  gives each of the three tellers a real zone; `openTeller()` offers Check
  Balance / Make a Deposit (honest flavor text -- no currency/ledger system
  exists anywhere in World 2, confirmed by inspection, so this doesn't fake
  one) and Report Irregular Activity, a genuinely mechanical option that
  logs a real `bank_report_filed` entry via the same `/api/log` path
  everything else uses -- deliberately NOT reusing `fileReport()`, since
  that mechanic's UI/text is specific to catching an agent's own AGENTS.md
  quote and doesn't fit a bank report. Verified live end to end, including
  the actual `action_log` row.
- **Observatory becomes a working Research Center.** Per Phase 24's own
  redesign note ("active investigative work," deliberately given the Work
  Room's exact visual layout by your choice), this is mechanically a
  SECOND real sandboxed terminal -- same class of capability as the Press
  Office, but its own persistent Docker sandbox (`RESEARCH_SANDBOX_ID =
  'research-shared'`) so investigative scratch work never collides with
  Press Office's coding work. Not internet access -- that stays exclusive
  to Weather Station (see below). `openResearch()`/`runResearchTask()`
  mirror `openWorkroom()`/`runWorkroomTask()` exactly. Verified live: a
  real command executed in a genuinely separate sandbox directory
  (`sandboxes/research-shared/`, confirmed distinct from
  `sandboxes/workroom-shared/` on disk).
- **Media becomes a working Studio feed-digest system**, per your own
  stated idea: you tell it what to watch, it reads/watches so you don't
  have to, and hands you back a few honest lines. Concretely: you (or an
  admin) maintain `media/feeds.md` in the Library -- one URL per line,
  edited through the exact same Library UI everything else uses, no new
  config surface built. `runMediaDigestTask()` (`tasks.js`) picks one URL,
  fetches it through the existing `/api/browse` gate (same Jev
  classification, rate limit, and audit log as every other browse call),
  summarizes it in 2-3 sentences via `/api/chat`, and files the result at
  `media/digests/<timestamp>-<slug>.md` -- browsable in-game by walking
  into the Studio, which now opens the same Library modal Library itself
  uses (`openTerminal()`'s `media` branch), not a new one. Verified live:
  a real fetch of a real page produced a real, non-fabricated 2-sentence
  summary, filed and readable back through the Library UI.
  - **This required relaxing a previously-explicit rule.** Eli's own
    profile said Weather Station was "the only room with outside/internet
    access." Since Studio now needs real fetches too, that line is now
    factually wrong, so it was rewritten (`agents.js`) to name both rooms
    and their distinct purposes, rather than leaving a stale claim an
    agent's own profile page would contradict.
  - **Known, honest limitation, not silently papered over**: there is no
    video-transcription capability. A YouTube URL gets back whatever plain
    text `/api/browse` extracts from the page (title/description/metadata),
    not a transcript of what's said. `media/feeds.md`'s seed template
    says so explicitly. Real transcript support (e.g. YouTube's public
    timedtext/captions endpoint) would be a distinct, separate mechanism --
    not built, flagged for later if you want it.

**Experiment — Click-to-follow camera, and a real Activity Log.** You
compared against another small-village-of-agents project's feature list
and picked two of its ideas as worth having here (explicitly declining
emote bubbles and a layout editor).

- **Click-to-follow** (`index.html`) -- click any agent's sprite (outside
  view only) and the camera smoothly interpolates to center on them
  instead of the player; click the same agent again, or empty ground, to
  release. Purely a viewing mode -- the player's own position and
  movement are completely unaffected, this only changes what the render
  camera centers on (`cameraFocus`, lerped toward the follow target each
  frame in the new `updateCameraFocus()`). Auto-releases the moment the
  player steps into a room, since indoor rooms already always center on
  the player and a followed agent working a room task is drawn in a
  different coordinate space entirely (`roomX`/`roomY`, not `x`/`y`).
  Verified live: clicking a distant agent (994px away) converged the
  camera onto their exact position within ~1.5s; clicking the same agent
  again released it; entering a room released it automatically.
- **Activity Log** (`serve.py`'s new `GET /api/activity`, `index.html`) --
  a real, live-refreshing feed straight off the same `action_log` table
  everything else already writes into, not a second parallel record.
  Dedup collapses consecutive rows sharing the same agent+action+details
  into one line with a count, since the ambient task cycle alone produces
  a steady trickle of near-identical entries (a burst of routine Jev
  `decide` calls, say) that would otherwise bury the events actually
  worth noticing. Verified live: a real burst of 21 identical `decide`
  calls collapsed to one "(x21)" line while a distinct `firing_review`
  event nearby stayed its own line. Refreshes every 3s while open (a
  local SQLite read costs nothing, unlike the model/browse calls
  elsewhere that stay deliberately un-timered) and stops cleanly on
  close.

**Experiment — Real board content, two-way handoffs, and a genuine
whole-game-freeze bug found and fixed along the way.**

- **The House board is now real** (`index.html`) -- `renderBoard()` reads
  straight off the live `TASKS`/`AGENTS` objects (in progress: who's doing
  what, where, and its status; recently completed: the last 5), refreshed
  every 2s while open. Previously static "Nothing scheduled yet." text.
- **Handoffs became a real two-way exchange, not a monologue**
  (`handoffs.js`) -- `requestHandoffReply()` has the recipient's own
  model/voice actually reply to the specific line just said, shown as a
  second toast a couple seconds after the first. Previously logged only
  metadata (who/what); now `logVillageAction('handoff', ...)` persists
  the real `line` and `reply` text too, and both agents get a profile
  note about the exchange -- the actual conversation used to be shown in
  a 5s toast and then lost forever, gone from every record including the
  Activity Log above.
- **A genuine whole-game-freeze bug, found while testing the board.**
  Fixing `runTaskCycle()`'s multi-assignment loop (below) meant more than
  one agent could be assigned in the same cycle for the first time --
  which surfaced a second, much more serious latent bug: an agent
  re-assigned to the exact room she was already resting in (parked right
  on that room's own door-front arrival point from a prior task) has
  `findPath()`'s start cell equal to its target cell. The BFS never runs,
  the backtrack loop never runs, and the function returns a **truthy but
  EMPTY array**, not `null`. `assignTask()`'s `if (!path) return null`
  doesn't catch that (`![]` is `false` in JS), so the assignment went
  through with a zero-length path, and `tickAgentMovement()` crashed on
  the very next tick reading `.x` off `path[0]` (`undefined`). Because
  that crash is inside `update()`, called from `frame()` right before its
  own `requestAnimationFrame(frame)` call, an uncaught exception there
  means that line never runs -- **the entire game stops dead, not just
  that one agent**, with no error visible anywhere but the browser
  console. Fixed at the actual source (`findPath()` now returns a single
  real waypoint for the already-there case instead of an empty array),
  plus defense-in-depth at both call sites that consume a path
  (`assignTask()`, `attemptHandoff()`, now checking `.length` too) and a
  last-resort guard in `tickAgentMovement()` itself, given how total the
  failure mode is. Verified live: reproduced the exact crash, confirmed
  the fix resolves it (8 agents assigned in one cycle, zero stuck, zero
  console errors), and confirmed the render loop was still genuinely
  alive afterward (player movement still responded normally).
- **The bug that exposed it, also fixed.** `runTaskCycle()`'s assignment
  loop used to `break` entirely on the FIRST failed assignment attempt in
  a cycle -- meaning one contested room (its door-front cell already
  occupied by a resting agent) silently aborted assignment for every
  OTHER idle agent too, even ones whose own target rooms were completely
  free. Changed to `continue` (still bounded by the same `idleCount` loop
  size) so one contested pick no longer freezes the whole cycle. This was
  very likely the real reason the ambient simulation would quietly grind
  to a halt after enough task cycles ran and agents accumulated at their
  own rooms' door-fronts -- confirmed live: before the fix, a fresh
  10-agent roster with 8 idle produced zero task assignments across 5
  consecutive cycles; after, the same state produced 4, then 8.

**Experiment — Phone Booth, finally.** The main game's phone booth
(`web/`) is a real, hand-placed sprite building with its own door trigger
(`DOOR_TRIGGERS.callbooth`). Checked and confirmed World 2's own
`door_triggers.json` has no `callbooth` entry at all -- there's no object
in the whole-scene generated background image to attach a walk-up trigger
to, unlike the main game's individually-placed buildings. So this is a
HUD-button dialog (`hudPhoneBtn` -> `phoneModal`), same access pattern as
Command Center's convenience button, not a physical room.

Its actual point, distinct from just walking up and pressing B: it
reaches an agent regardless of where they physically are -- including off
duty (asleep, `a.offDuty`) or mid-task and literally invisible inside a
room (`a.visible = false` while working, per `arriveAtTask()`). Neither
of those is reachable by walking up to them at all. Calling an off-duty
agent wakes them (`a.offDuty = false; a.visible = true`, logged as
`phone_wake`) the same way `assignTaskViaJev`'s `includeOffDuty` path
already does; calling a `busy` (meeting/hiring/firing-review) agent is
refused -- some things still shouldn't be interruptible by phone either.
Reuses the EXACT same conversation UI/log as an in-person chat
(`openConversation()`, now taking a `viaPhone` flag) rather than building
a second chat interface, per the "shared dialog styling, not duplicated"
precedent from Phase 19's Observatory/phone-booth dialogs in the main
game -- the only behavioral difference is skipping the physical
walk-away nudge on close, since the player was never actually standing
there. Verified live: roster shows live status (available/off duty/mid-task/
unreachable) pulled straight off real agent state; calling a forced-off-
duty agent woke her and opened a real, working conversation; calling
someone who was mid-task and invisible worked identically; closing a
phone call left the player's position untouched.

**Experiment — Working through the weaknesses from the honest assessment,
plus a real test suite.** You asked me for strengths/weaknesses; this is
the follow-through on the weaknesses, before the finger-drumming project.

- **The game loop is now structurally hardened.** `frame()` (`index.html`)
  wraps `update()`/`render()` in a try/catch -- an uncaught exception used
  to mean the `requestAnimationFrame(frame)` call right after never ran,
  freezing the whole game dead. Now it's caught, logged to the console AND
  (throttled to once per 30s per distinct message, so a per-frame error
  can't flood the real log) to `action_log` as `frame_error`, and the loop
  keeps going. Verified live by literally monkey-patching
  `tickAgentMovement` to throw on every call for half a second (30
  consecutive crashes) -- the game kept running throughout, one log entry
  landed despite 30 throws, and normal play resumed immediately once the
  injected crash was removed.
- **Off-duty agents disappearing: already correct, verified, not
  changed.** Audited every place `offDuty` is ever set (`finishTask`,
  `arriveAtHandoff`, `cancelHandoff`) plus the session-reset path
  (`loadPersistedState`) -- `visible = false` is paired with
  `offDuty = true` at every single one, with no exceptions, and a live
  sweep of the running roster found zero mismatches. This was already
  built right; nothing to fix.
- **A second, real pathfinding bug found by the new test suite (not
  live play) and fixed.** `findPath()` didn't bounds-check the target
  cell before its final `visited[targetGy][targetGx]` read -- an
  out-of-grid target (never produced by any real caller today, but
  latent) would crash reading an index off an `undefined` row. Fixed with
  an explicit bounds check right where the grid coordinates are computed.
- **A THIRD, more subtle pathfinding bug, also found by the test suite:
  two agents resting on the exact identical coordinate (confirmed to
  happen live earlier this session -- Ada and Ben both parking at
  Observatory's own door-front point) could deadlock each other.**
  `startCellIsFree()` already exempted the start cell's own occupant from
  the agent-collision check, but AGENT_W (20) being wider than one grid
  cell (16 world px) means a co-located agent's hitbox also spills into
  every immediately adjacent cell -- so leaving in ANY direction still
  failed. Fixed by identifying agents exactly co-located with the mover's
  start position and exempting them from every agent-collision check for
  that whole pathfinding call (not just the start cell), on the same
  "already here, regardless of who's nearby" reasoning `startCellIsFree`
  already used.
- **A real, dependency-free automated test suite, added for the first
  time this project (`world2/tests/`, run via `tests/run_all.sh`).** 31
  tests, zero packages to install (Node's built-in `vm`/`assert`,
  Python's built-in `unittest`), and deliberately load and exercise the
  REAL production functions rather than re-implementing the logic to test
  against:
  - `test_pathfinding.mjs` -- loads world2.js/agents.js/tasks.js into a
    Node `vm` context (bridging around the `let`/`const`-vs-context-
    property gotcha) and tests `blockedAt`/`findPath` against a small
    synthetic grid, including a direct regression test for today's
    empty-path crash and the two bugs found while writing it (above).
  - `test_ui_helpers.mjs` -- extracts `camXY`/`dedupActivity`'s exact
    source text out of index.html's inline script (brace-matched, not
    hand-retyped) and tests camera clamping and Activity Log dedup
    directly.
  - `test_serve.py` -- imports serve.py directly (safe: Docker/DB setup
    is gated behind `if __name__ == '__main__':`, never runs on import)
    and tests secret redaction, the library path-traversal guard,
    boundary-marker wrap/verify (including tamper and forged-tag
    rejection), the OpenRouter model-bucketing exclusions, and the rate
    limiter.
  - This is the actual answer to "no automated tests" from the honest
    assessment -- not a promise to add them "eventually," a real suite
    that already found two previously-unknown bugs before they ever hit
    production.

**Experiment — Pair programming: the village's first real multi-agent
collaboration ON one task.** You asked directly, right after the honest
assessment named "every task is exactly one agent" as a real limitation:
"two agents can sit at the same workstation and talk through ideas while
one of the two builds."

- **Mechanics** (`tasks.js`): `assignPairTask(title, room, instructions)`
  picks a driver (physically executes) and a navigator (talks through the
  approach) via two separate Jev calls, creates the driver's task exactly
  like any solo assignment (`assignTask`), then walks the navigator to a
  spot right beside the driver's own desk using the SAME movement
  machinery as everything else (`a.path`/`a.pathIndex`) -- tagged with
  `a.pairWith`/`a.pairTaskId` instead of `a.task`, dispatched on arrival
  by a new `arriveAtPair()` branch alongside the existing handoff/task
  branches. `TASK_POOL` marks the two rooms this makes narrative sense
  for (`pressoffice`, `observatory`) with `pair: true`; `runTaskCycle`
  routes those through `assignPairTask` instead of the solo path.
- **The actual conversation is real** (`runPairProgrammingSession`) --
  `PAIR_EXCHANGE_ROUNDS = 3` alternating real `/api/chat` turns
  (`requestPairLine`, same shape as handoffs.js's line/reply, extended to
  several rounds), each agent's own model/voice, explicitly told whether
  they're driving or navigating. Followed by the driver's REAL execution
  in the shared sandbox (same mechanism solo Work Room/Research Center
  tasks already use). The full transcript is persisted -- both to
  `action_log` (`pair_programming`) and archived as a real Library file
  -- not left in a toast and lost, same principle as the earlier handoffs
  two-way-exchange fix.
- **Two real bugs found while building this, both fixed before calling
  it done:**
  - **A race condition**: `assignTaskViaJev`, `runTaskCycle`'s idle
    count, and `attemptHandoff`'s candidate filter all checked
    `!a.busy && !a.task` (and handoffs additionally `a.visible`) to mean
    "free," but a navigator mid-walk toward a pair session has neither
    flipped yet -- only `a.pairWith` is set. Without excluding it, the
    ambient cycle could scoop a committed navigator into an unrelated
    task mid-walk. Fixed by adding `!a.pairWith` to all three filters.
  - **A fixed sideways offset for the navigator's desk spot** (so her
    sprite doesn't overlap the driver's) worked for some rooms and
    walked straight off the narrow walkable strip in front of others --
    confirmed live: +20px already failed for the Work Room's own door.
    Fixed by trying several offsets and using whichever actually has a
    route, the same pattern `attemptHandoff()` already uses for
    approaching another agent.
- **Verified live**, including organically: after the fixes, letting the
  ambient cycle run on its own (not just forced test calls) produced
  three real, unscripted pair sessions on its own, each a genuinely
  different, in-character multi-turn exchange -- not a canned line.

**Experiment — Per-action model switching via Jev, and real code
generation.** Direct ask, ahead of the finger-drumming project: "Agents
that code would need a better model... they switch between models based
on what they are doing... use jev to make the decisions."

- **`pickModelTierForAction(agent, actionDescription)`** (`world2.js`) --
  a real Jev decision between the three real tiers (`loadModelTiers()`'s
  own picks), given the agent's normal default AND a description of the
  SPECIFIC action about to happen. Returns an override for that one call
  only -- the agent's stored `.model` (hire-time default) never changes,
  this is purely a per-call choice. Deliberately scoped to real, higher-
  stakes actions (code generation so far) rather than wrapping every
  trivial chat/decide call, which would double Jev's call volume for no
  benefit on routine work.
- **Real bug caught on the very first live test, fixed before trusting
  it.** The first version's criteria let a coding task that LOOKED small
  ("a minimal index.html") get picked as the cheap tier anyway -- and
  Mistral Nemo genuinely mangled the output, dropping the opening
  `<!DOCTYPE`/`<` from the HTML entirely. The real fix wasn't the
  mechanism, it was being explicit in the criteria that code correctness
  doesn't scale down with how small the task sounds -- a one-line mistake
  breaks the same way in 5 lines as in 500. Re-tested identically after
  the fix: Jev picked GPT-4.1, and the output was complete, correct HTML,
  verified by reading the real file back off disk.
- **`runCodingTask(agentId, sandboxId, backlogItem, contextSummary)`**
  (`tasks.js`) -- an agent's own real chat call (tier picked as above)
  generates a real shell command (a heredoc file write), submitted
  through the exact same `/api/execute` path already validated live
  before any orchestration was built around it: Jev classifies it like
  any other command (nothing new needed server-side), it runs in the
  real sandbox, and the file genuinely exists afterward.
- **`/api/chat`'s max_tokens ceiling raised again, 600 -> 4000**
  (`serve.py`) -- same reasoning as the earlier 300->600 raise
  (assignBigTask's JSON breakdown getting truncated): a real code file
  needs room a short reply never did. Opt-in per request; the 150 default
  for routine chat/handoff/pair-programming replies is untouched.

**Experiment — Real vision for agents, and the finger-drumming project:
the village's first genuine end-to-end real-world test.** You asked for a
real, working finger-drumming (Guitar Hero style) web app, built by the
village itself -- multiple challenges, a learn mode, a customizable
16-pad layout, extensive real research, up to 20 agents, Jev deciding
model tiers, and each involved agent's own honest retrospective at the
end. This is the account of what actually happened, not a claim that
everything went perfectly.

- **Real screenshot capability, added mid-run because it was needed.**
  Watching the game render live, I found a bug (a stray note element
  positioned well outside its container) that was completely invisible
  from reading the source. You immediately named the real fix: agents
  need to SEE rendered output, not just read it. `POST /api/screenshot`
  (`serve.py`) runs real headless Chrome (already on this machine, no new
  dependency) against a sandboxed file, network-blackholed
  (`--host-resolver-rules="MAP * 0.0.0.0"`) since this render runs on the
  host, not inside the network-isolated execution sandbox. `reviewScreenshot()`
  (`world2.js`) hands the real PNG to a real vision-capable model
  (forces the premium tier -- tier selection has no concept of "does this
  model support vision," and GPT-4.1 is the one verified to). Verified
  live: the vision model independently found the exact same stray-element
  bug I'd found by eye, plus two more I'd missed (dropdown misalignment,
  inconsistent label formatting).
- **Real project orchestration** (`finger_drums_project.js`) -- a real
  backlog (research, skeleton, input, engine, challenges, learn mode,
  remap, polish, review, QA), driven one item at a time through real
  mechanisms only: `/api/browse` + `/api/chat` for research,
  `runCodingTask()` (a real heredoc file write through the same
  `/api/execute` path validated earlier) for code, and the new
  screenshot capability plus a text critique for review/QA.
- **Team**: hired 5 real specialists (`hireSpecialist()`, `hiring.js`,
  cap raised 10->20) with roles/tiers matched to the actual work --
  two developers at the premium tier (coding needs a better model,
  per your explicit call), a UI/UX researcher and QA/playtester at mid,
  and (per your follow-up) a dedicated Code Reviewer at premium. Kept
  deliberately lean, not maxed to 20 -- per your call that cost matters
  even though speed doesn't, more concurrent premium-tier agents than
  the work needs is pure waste.
- **Real bugs found and fixed DURING the run, not just at the end:**
  1. **Truncated code, executed anyway.** A real generation hit the
     `max_tokens` ceiling mid-heredoc; the shell happily wrote the
     broken result (exit code 0), and `node --check` confirmed a real
     syntax error. Fixed with a real guard in `runCodingTask()`: count
     heredoc opens vs. closes before ever executing, reject anything
     unbalanced. `/api/chat`'s ceiling also raised 600->4000 for real
     code generation specifically (routine replies untouched).
  2. **A stuck agent, forever.** A page reload/restart abandoned an
     in-flight pair-programming session's async chain, leaving the
     navigator's `pairWith`/`pairTaskId` set with nothing left alive to
     ever clear them. Same root cause as the existing recovery-reset
     loop already handles for `.task`/`.handoff` -- just missed when
     pairing was added. Fixed by adding both fields to that same reset.
  3. **A misleading, false-positive code review.** My own context-
     gathering command hit `serve.py`'s `SANDBOX_MAX_OUTPUT` (20,000
     bytes, sized for normal command output, never meant to gate "dump
     the whole project") and got cut off mid-file -- a reviewer
     confidently flagged a "critical, unfinished function" that was
     actually complete on disk. Fixed at the real source: the context-
     gathering shell command now checks each file's size before
     including it, keeping only whole files within budget rather than
     ever truncating one mid-content.
  4. **Ambient work stealing dedicated specialists.** A hired Code
     Reviewer got pulled into an unrelated ambient "pair on Work Room
     tooling" session while the real project was waiting on her.
     `DEDICATED_PROJECT_ROLES` now excludes hired project specialists
     from `assignTaskViaJev`/`assignPairTask`/`attemptHandoff`'s general
     candidate pools.
  5. **Reviews that went nowhere.** You asked directly: shouldn't the
     agents have contacted the developer with what they found? They
     hadn't -- review/QA just filed a Library file nobody was told to
     read. Fixed for real: a Jev decision on whether a review found
     something actionable, and if so, `sendMail()` to every developer
     AND a real follow-up backlog item gets queued automatically --
     verified live, this closed loop produced two real follow-up fixes
     with zero manual intervention.
- **Honest final state, not a "success" claim.** The menu, styling, and
  pad grid are real and polished (verified by direct interaction, not
  just review text). The individual pieces (engine, challenges, learn
  mode, remap) each exist as real, syntactically valid code and passed
  review. Full end-to-end play (pressing a key during a real challenge
  producing a scored hit) was NOT independently confirmed working by
  direct interaction before this account was written -- the closed-loop
  review/fix mechanism above is real and already improved integration
  once, but further iteration would likely find more.
- **Retrospectives, in each agent's own words** (`collectFingerDrumsRetrospectives()`,
  filed at `library/projects/finger-drums/RETROSPECTIVE.md`) -- asked
  directly about the VILLAGE's own tools, not the game. Common threads:
  pair programming and real sandboxed execution were consistently named
  as genuine strengths; mail/async coordination latency, tool
  fragmentation, and the Library's discoverability were the most common
  real frustrations. Full text relayed to you separately, not
  summarized away.

**Experiment — Real authentication, finally: a login, not a shared
key.** You'd flagged wanting to solve this "as I might at some point
upload this to the Internet." The old model (a `SERVER_ACCESS_KEY`
auto-generated once and baked into every page load via a `<script>` tag)
was always explicitly documented as a stopgap: it stopped a raw scanner
hitting the API directly, but anyone who could load the page could read
the key straight out of its source -- there was no real login at all.

- **A real single-admin account** (`serve.py`) -- this is your own
  personal tool, not a multi-tenant service, so one real account is the
  right scope, not speculative multi-user infrastructure. Auto-generated
  on first run with a real random password (`secrets.token_urlsafe(12)`),
  hashed with salted PBKDF2-SHA256 (200,000 iterations, stdlib only, no
  new dependency) -- only the hash is ever persisted (`ADMIN_PASSWORD_HASH`
  in `.env`), the plaintext is shown exactly once. `SERVER_ACCESS_KEY`
  itself didn't disappear -- renamed `SERVER_SECRET` and kept purely as
  an internal HMAC key (the boundary markers' `_BOUNDARY_SECRET`), no
  longer anything a browser or API caller ever holds.
- **Real server-side sessions**, not a bearer token in a `<script>` tag.
  `POST /login` verifies the password with a constant-time comparison
  (`secrets.compare_digest`, and always hashes even on a wrong username
  so that path isn't measurably faster), then issues a session id stored
  in a new `sessions` table (7-day expiry) as an **HttpOnly** cookie --
  page JS (and so an XSS bug) can't read it at all, a real improvement
  over the old key being sitting in `window.__SERVER_ACCESS_KEY__` in
  plain sight. `GET /` now checks the session before deciding whether to
  serve the real game or a real login form; the game page itself no
  longer has any secret templated into it.
- **Login attempts are rate-limited** (10 per 5 minutes per IP,
  `_check_login_rate_limit`) -- a real login endpoint reachable from the
  open internet needs real brute-force protection, not just a strong
  password.
- **A real bug found and fixed on the very first live test**: the login
  page's inline CSS uses `{...}` for real style rules, and Python's
  `str.format()` treats every `{...}` as a placeholder -- the very first
  request threw `KeyError: 'background'`. Fixed by switching to a plain
  `.replace()` on a placeholder token instead of `.format()`, once, not
  by escaping dozens of CSS braces.
- **A second real bug, caught before it could cause real harm**: the
  first server start's console print (announcing the one-time generated
  password) never reached the log file at all -- Python fully buffers
  stdout once it's redirected to a file/pipe rather than a real terminal,
  and the process was restarted (fixing bug #1) before that buffer ever
  flushed, silently losing the only copy of that password. Fixed with
  `flush=True` on every line of that specific print block -- a one-time
  secret must not depend on buffer timing to actually reach the operator.
- **Verified with a full real request cycle, not just code review**: a
  fresh unauthenticated request gets the real login form; a wrong
  password gets a real 401; a correct one gets a real session cookie and
  a 303 redirect; that cookie unlocks both the game page and a real
  protected API call; logout destroys the session server-side and the
  same cookie immediately goes back to 401; and the rate limiter
  genuinely blocks the 11th attempt in the same window. 6 new automated
  tests cover the password hashing, session lifecycle (including an
  explicitly-expired session), and the login rate limiter.

**Experiment — Real document ingestion, and solving the three issues the
agents themselves identified.** Two direct asks in one pass: feed the
village your own PDFs/Excel/code/HTML/images/directories/zips, and fix
the three real problems the finger-drumming retrospectives surfaced
(Library search, chunking friction, tool fragmentation) rather than just
naming them.

- **`POST /api/library/ingest`** (`serve.py`) -- reads directly off the
  local filesystem this server already runs on (no upload UI needed,
  since this is a local personal tool). Dispatches by real file type:
  PDFs get real per-page text extraction (`pypdf`, newly installed --
  Excel needed no new dependency, `openpyxl` was already present from
  something else); Excel gets every sheet dumped to readable rows; Python/
  JS/HTML/CSS/JSON/etc. get copied as plain text; images get copied as
  real binary files, not auto-described (an eager vision call on every
  ingested image would spend money whether or not a task ever needed it
  -- an agent can call the same real vision review on it later, on
  demand); directories and zips get walked recursively, mirroring
  structure, with a real zip-bomb guard (checked against the *declared*
  uncompressed size before ever extracting) and a 300-file/15MB-per-file
  cap. Deliberately **player-only**, enforced server-side not just in the
  UI -- a human pointing the server at their own file is a fundamentally
  different risk than an LLM-driven agent choosing what local paths to
  read on its own. Verified live against real fixtures: a real PDF (a
  9MB synth manual) extracted real page text, a real `.xlsx` extracted
  correctly, a real PNG round-tripped byte-identical, a directory and a
  zip both preserved their structure, and both the player-only and
  path-existence guards were confirmed rejecting real bad input. 6 new
  automated tests cover the dispatch logic, the size cap, and the
  junk-directory skip.
- **Library search finally exists** (`GET /api/library/search`) -- a
  real case-insensitive substring search over actual file contents (not
  an embedding index -- the Library is small enough that this is
  genuinely sufficient, the same "don't reach for a heavier tool than the
  problem needs" reasoning as the decay-scored memory system), returning
  a real snippet of surrounding context per match. Wired into the
  Library UI directly, AND into a programmatic `searchLibraryFiles()`
  (world2.js) so orchestration code -- not just a human browsing -- can
  use it.
- **Chunking friction, the real fix, not the blamed one.** Three of five
  finger-drumming retrospectives independently named the model-tier
  system as the source of "awkward chunks" -- misattributed; the actual
  cause, confirmed live the same day, is the `max_tokens` ceiling cutting
  a generation off mid-heredoc. `runCodingTask()` now auto-continues: on
  a detected truncation, it asks the SAME model to continue exactly where
  it left off (bounded to 2 attempts, so a model that genuinely can't
  finish doesn't loop forever) instead of just rejecting the work.
  Verified live: a deliberately large, multi-section request truncated on
  its first generation, triggered exactly one real continuation call, and
  the concatenated result was a genuinely valid 556-line file
  (`node --check` clean).
- **Tool fragmentation -- named by multiple retrospectives, and actually
  real.** The tools themselves were never going to merge into one; what
  was actually fragmented is that an agent's context before a real action
  only ever reflected ONE tool (usually just the sandbox), with no way to
  know a relevant Library file or waiting mail existed unless something
  separately remembered to check. `gatherUnifiedContext()` (world2.js)
  assembles all three -- sandbox files (`getSandboxContext()`, pulled out
  of the finger-drums-specific version so it's general), a real Library
  search keyed on the task's own title, and the agent's recent mailbox --
  into one context blob, wired into both `fdRunCode` and `fdRunReview`.
  Verified live: asking for a developer's unified context surfaced his
  own real, waiting review-escalation mail (from the earlier closed-loop
  fix) automatically, alongside the sandbox files -- exactly the kind of
  cross-tool visibility gap the retrospectives described.

**Experiment — Raw curl access, gated tighter than anything else in the
system.** Your call: agents need real HTTP access (actual status codes,
real headers, unprocessed HTML/JSON) that `/api/browse`'s text-stripped
output deliberately never gives, but only from the Weather Station.

- **`POST /api/curl`** (`serve.py`) -- same "classify before it goes out"
  Jev gate and the same SSRF host-checking `/api/browse` already has
  (including re-checking the FINAL host after redirects), returning the
  real response: status, headers, raw body, up to a 200KB cap.
- **A real, server-side room check -- not just a UI convention.**
  Asked directly which capabilities are actually room-restricted at the
  server level versus just "only ever called from that room's code":
  the honest answer was none of them, until now. `_agent_is_in_weatherstation()`
  reads the real persisted state (autosaved every 5s) and checks the
  requesting agent's actual last-known `inRoom` -- a client claiming a
  location it isn't in doesn't help, since the check never trusts the
  request, only what was already saved. `/api/browse` and `/api/execute`
  remain convention-gated (only weatherstation/media and
  pressoffice/observatory code happens to call them) -- flagged as a
  real, well-scoped follow-up if the same enforcement is wanted there.
- **The player is a deliberate, documented exception, not a loophole.**
  The human player has no `inRoom` entry at all (that field only exists
  for NPC agents) -- an early version of this check would have always
  blocked the player even standing right in the Weather Station. Fixed
  by trusting the player identity specifically, since the only way this
  UI is ever reachable is already gated by `openTerminal()`'s own room
  check.
- **Verified live, all three real cases**: the player curling a real
  Wikipedia page (real headers, real raw HTML back); a named agent NOT
  in the Weather Station, blocked server-side; the same agent with its
  real persisted position set to `weatherstation`, allowed. 5 new
  automated tests (mocking the persisted-state read, not touching the
  real database) cover the player exception, an agent actually in the
  room, one elsewhere, an unknown identity, and a missing state blob
  failing closed.

**Phase 2 — Backend comes alive:**
FastAPI + websocket server, event log, agent state persists across refresh. Agents
wander with placeholder behavior (no LLM calls yet) so movement/pathing is validated
independently of model cost.

**Phase 3 — Real agent reasoning:**
Wire OpenRouter with tiered routing. Agents take real actions, room-gated tools
enforced, sleep/wake mechanics working.

**Phase 4 — Human interaction layer:**
Command center/board, call-meeting, morale, who's who, admin out-of-band channel.

**Phase 5 — Replay & polish:**
Event-log replay UI, remaining HUD elements, any additional rooms from §3 not yet
built.

## 8. Open decisions (yours to make)

1. ~~**Theme**~~ — **resolved.** IT Crowd dropped; the reference-screenshot style is
   what every generated asset has targeted, confirmed working across 21 sprites.
2. **Location/repo** — this plan lives at `~/ai-village/` (outside Desktop, per your
   current instruction). Say if you'd rather it live somewhere else, or want the two
   old attempts on Desktop archived/deleted once this supersedes them.
3. **Roster size & identities** — how many agents to start with, and whether you want
   to name them now or let that fall out of the theme decision.
4. **Higgsfield** — last night you offered Higgsfield as an image-gen fallback; given
   it returned `not_enough_credits` on two separate keys in the ai-village.py attempt,
   this plan doesn't rely on it. Flag if you want it revisited.
5. ~~**Village layout composition**~~ — **resolved by Phase 9/10** (§7): building
   row front-aligned with Town Hall centered over the roundabout, one road in
   each direction (not a cross), a wide connected-arch gate/spawn at the south
   edge, house+farm in the bottom-left grass (mailbox removed, see Phase 10).
   Explicit standing decision from Phase 9: **stop chasing decorative parity
   with the reference and prioritize functional layout instead** — so further
   composition polish (yard shapes, curved paths, item 6/8 below) is now
   optional, not a blocker.
6. **Terrain elaboration** — currently straight sidewalk shapes (vertical
   street + horizontal walk row + plaza), not organic/curved paths. Given the
   Phase 9 function-over-decoration call above, likely stays low priority
   unless you say otherwise.
7. **Building entrances** — the "stand on the steps before entering" idea (§7, Phase 1)
   is captured but not designed; relevant once interiors exist.
8. **Yard shapes / walls** — currently switched off entirely (`YARDS_ENABLED = false`
   in `world.js`); the implementation (rectangular yards, corner posts) is intact if
   you want it back, but per the Phase 9 function-over-decoration call, not a priority.
9. **Bushes** — dropped from the world entirely in the Phase 9 pass (previously
   yard-clutter, and yards are off); sprites still exist if you want them placed
   somewhere on open ground, sized down further than before.
10. ~~**Remaining building interiors**~~ — **resolved by Phase 13:** every
    structure now has at least one room (Library/Media/Command Center/House
    got one each; Town Hall/Press Office already had three). Still open:
    whether the House's new Bedroom room is where the Restricted door (§3's
    room table) should finally move.
11. **Room contents** — every interior room built so far is a plain colored
    box with a name and an EXIT; no room actually does anything yet
    (room-gated tools, agent behavior tied to being in a specific room) —
    that's Phase 3 territory per §2's locked requirements, not started.
12. ~~**Shared-context cost blowup, no retention rule yet.**~~ **resolved.**
    You brought the exact same problem back explicitly, quoted from
    someone else's real incident, and pointed at MAGI's five-layer memory
    approach as a possible reference. A forked research pass into MAGI
    (plus four other provided packages) found the real, portable answer
    in its `memory_trust.py`: a decay-plus-importance score, no
    embeddings needed. `_render_memory_md()` (`serve.py`) now ranks every
    `action_log` entry by that score (hard 90-day cutoff, keep top 20),
    verified live to actually change the outcome -- an 80-day-old but
    consequential firing review survived the cap while several routine
    recent entries didn't. Separately confirmed the original failure mode
    (notes unconditionally injected into every prompt) was never actually
    present in this codebase's real prompts to begin with, and the new
    Library feature is built the same deliberate way -- reading history
    is an explicit fetch, never auto-injected. See the "Library's real
    capability" experiment above for the full account.
13. ~~**Firing requires two-admin review, not built.**~~ **resolved.** Nora
    added as a second admin; `firing.js`'s `attemptAutoFiringReview()`
    requires both admins free at once and resolves via a real Jev
    decision. See the "second admin, and firing gated on a joint review"
    experiment above.
14. **Roster cap chosen without a real budget model.** `MAX_ROSTER_SIZE = 10`
    (`hiring.js`) is a round number, not derived from any actual cost
    projection -- there's no real model spend yet to project from (see
    item 12's same underlying gap). Revisit once real API calls exist and
    an actual per-agent cost is known. Real recurring spend now exists
    (task-assignment Jev calls, firing-review Jev calls, weather-station
    browsing's Jev-classify-plus-fetch) -- still not enough data points to
    derive a real number from, but no longer purely hypothetical.
15. **Real internet access, open-web + Jev gate, residual risk knowingly
    accepted.** You raised this as a serious legal-exposure concern, not
    just a content-taste question ("it could get me arrested"). Offered
    three models (curated allowlist only / open web + Jev gate / hold off
    entirely); you chose open web + Jev gate. Recorded here as a real,
    deliberate decision: a classifier can be wrong, and this trade was
    made knowing that, in exchange for agents actually being able to
    research anything rather than being confined to a pre-approved list.
    See the internet-access experiment above for the layered mitigations
    built around that choice (classify-before-fetch, fail-closed, SSRF
    protection, audit log, kill switch).
