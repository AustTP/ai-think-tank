// Interior rooms for World. Each active-room building maps to a room
// definition -- a whole-scene background image (see ../DESIGN.md, the room
// design pass) plus hand-mapped blocking rectangles in the image's own
// native (632x424) pixel coordinates. No extra scale factor here (unlike
// the outdoor SCALE=2): these room images already read at a reasonable
// size against the fixed 20x16 player hitbox, since they were generated
// with a human-scale desk/counter/shelf already in mind.
//
// The six-CRT-desk "workstations" image is shared by four
// buildings (Press Office, Media, Weather Station, Observatory) and uses
// ONE collision definition, not four separate copies -- if you edit the
// workstations layout later, it updates all four rooms at once.
//
// Buildings NOT listed here are dialog-only or not yet built: townhall
// (meeting chatroom, deferred), house (weekly-board dialog, not built),
// callbooth (call dialog, not built).

const ROOM_NATIVE_W = 632, ROOM_NATIVE_H = 424;

// Every room shares the same "open bottom edge" convention as the art
// itself (no door ever drawn -- see DESIGN.md) -- walking into this zone
// exits back outside to the building's own door trigger.
const ROOM_EXIT_TRIGGER = { x: ROOM_NATIVE_W / 2 - 20, y: ROOM_NATIVE_H - 24, w: 40, h: 24 };

const ROOM_COLLISIONS = {
  workstations: [
    { x: 84, y: 105, w: 120, h: 105 },
    { x: 255, y: 105, w: 120, h: 105 },
    { x: 425, y: 105, w: 120, h: 105 },
    { x: 84, y: 250, w: 120, h: 100 },
    { x: 255, y: 250, w: 120, h: 100 },
    { x: 425, y: 250, w: 120, h: 100 },
  ],
  library: [
    { x: 78, y: 41, w: 106, h: 112 },
    { x: 263, y: 41, w: 106, h: 112 },
    { x: 448, y: 41, w: 106, h: 112 },
    { x: 78, y: 212, w: 106, h: 122 },
    { x: 263, y: 212, w: 106, h: 122 },
    { x: 448, y: 212, w: 106, h: 122 },
  ],
  // Only the counters block -- the chains are a visual lane marker, not
  // collision: agents must not get stuck behind
  // someone at a teller, so the queue dividers stay walk-through.
  bank: [
    { x: 60, y: 95, w: 85, h: 65 },
    { x: 275, y: 95, w: 80, h: 65 },
    { x: 490, y: 95, w: 80, h: 65 },
  ],
  postoffice: [
    // Full-width wall that MATCHES the mailbox art: the visual mailbox bank
    // ends at ~y=238 in room_postoffice.png, so the collision bottom must sit
    // there too -- otherwise the player walks up into the lower mailboxes and
    // the mailbox interaction zone (derived just below this wall) lands on
    // top of the art instead of in front of it.
    { x: 5, y: 50, w: 620, h: 188 },
  ],
  // Hangout: an empty gathering room, same open layout as the old Outskirts
  // rooms -- just a top wall band, everything below is open walkable space
  // for agents to idle and mingle. No furniture, no terminals.
  hangout: [
    { x: 0, y: 0, w: 632, h: 136 },
  ],
};

const ROOMS = {
  pressoffice: { image: 'assets/rooms/room_workstations.png', collision: 'workstations', label: 'Work Room' },
  media: { image: 'assets/rooms/room_workstations.png', collision: 'workstations', label: 'Studio' },
  weatherstation: { image: 'assets/rooms/room_workstations.png', collision: 'workstations', label: 'Weather Station' },
  observatory: { image: 'assets/rooms/room_workstations.png', collision: 'workstations', label: 'Research Center' },
  library: { image: 'assets/rooms/room_library.png', collision: 'library', label: 'Library' },
  bank: { image: 'assets/rooms/room_bank.png', collision: 'bank', label: 'Bank' },
  postoffice: { image: 'assets/rooms/room_postoffice.png', collision: 'postoffice', label: 'Post Office' },
  // Not tied to a building on the outdoor map at all -- reachable only via
  // the HUD's Command Center button (like Call Meeting, callable from
  // anywhere), not by walking anywhere. House is a separate, physical
  // building with its own door trigger and its own purpose (the board).
  commandcenter: { image: 'assets/rooms/room_workstations.png', collision: 'workstations', label: 'Control Room' },
  // Hangout: the empty gathering room behind the Town Hall's multi-option
  // door. Same open "Outskirts" layout -- shared art with the outdoor
  // clearing look. Non-delegable: agents rest here, never get pushed work.
  hangout: { image: 'assets/rooms/room_hangout.png', collision: 'hangout', label: 'Hangout' },
};

// Door triggers on the OUTDOOR map. Authored in NATIVE image coordinates
// (688x384, same space as collision_grid.json) in door_triggers.json --
// edit them with door_editor.html rather than hand-typing pixel guesses --
// then scaled up to world-space (*SCALE, same convention as world.js's
// own collision math) here at load time.
let ROOM_DOOR_TRIGGERS = null;
let MAIN_DOOR_TRIGGERS = null;
let WINTER_DOOR_TRIGGERS = null;

async function loadDoorTriggers() {
  const [mainRes, winterRes] = await Promise.all([
    fetch('door_triggers.json?v=' + Date.now()),
    fetch('door_triggers_winter.json?v=' + Date.now()),
  ]);
  const native = await mainRes.json();
  MAIN_DOOR_TRIGGERS = {};
  for (const building in native) {
    const d = native[building];
    MAIN_DOOR_TRIGGERS[building] = { x: d.x * SCALE, y: d.y * SCALE, w: d.w * SCALE, h: d.h * SCALE };
  }
  // The Hangout shares the Town Hall's front door (multi-option entry) -- the
  // outskirt rooms each had their own door, but the hangout is behind the
  // same building, so exiting a hangout session drops the player back onto
  // the townhall door tile. Alias it so exitRoom()/movement resolve cleanly.
  if (native.townhall) {
    const d = native.townhall;
    MAIN_DOOR_TRIGGERS.hangout = { x: d.x * SCALE, y: d.y * SCALE, w: d.w * SCALE, h: d.h * SCALE };
  }
  WINTER_DOOR_TRIGGERS = {};
  try {
    const winterNative = await winterRes.json();
    for (const building in winterNative) {
      const d = winterNative[building];
      WINTER_DOOR_TRIGGERS[building] = { x: d.x * SCALE, y: d.y * SCALE, w: d.w * SCALE, h: d.h * SCALE };
    }
    if (winterNative.townhall) {
      const d = winterNative.townhall;
      WINTER_DOOR_TRIGGERS.hangout = { x: d.x * SCALE, y: d.y * SCALE, w: d.w * SCALE, h: d.h * SCALE };
    }
  } catch (e) {
    // If the winter door file is missing, the winter map simply has no
    // enterable buildings (still walkable).
  }
  ROOM_DOOR_TRIGGERS = MAIN_DOOR_TRIGGERS;
}

// Swap the active door-trigger set when the player crosses between the main
// village and the winter (adversarial) village. Called by world.js's
// applyScene().
function setSceneDoors(name) {
  ROOM_DOOR_TRIGGERS = (name === 'winter' && WINTER_DOOR_TRIGGERS)
    ? WINTER_DOOR_TRIGGERS : MAIN_DOOR_TRIGGERS;
}
