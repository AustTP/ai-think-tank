// Room object interactions -- started with the computer terminals (Work
// Room, Weather Station, Studio, Control Room, Research Center, every room
// built on the shared six-desk "workstations" layout, see rooms.js) and
// grew to cover Post Office's mailbox wall, the Bank's three tellers, and
// the Library's bookshelves -- walking up to any shelf
// opens the same file-review modal the Library building already wired up.
// The terminal itself is the same everywhere for now; what
// each room's terminal actually has access to (room-gated tools) is a
// later pass.
//
// ROOM_INTERACTABLES is keyed by collision-set name, same convention as
// ROOM_COLLISIONS. Each zone is a thin strip directly in front of (south
// of) the relevant collision rectangle, derived from ROOM_COLLISIONS
// rather than hand-typed again, so the two can never drift out of sync if
// a layout changes.
const TERMINAL_ZONE_DEPTH = 15;

const ROOM_INTERACTABLES = {
  workstations: ROOM_COLLISIONS.workstations.map(desk => ({
    type: 'terminal',
    zone: { x: desk.x, y: desk.y + desk.h, w: desk.w, h: TERMINAL_ZONE_DEPTH },
  })),
  postoffice: ROOM_COLLISIONS.postoffice.map(wall => ({
    type: 'mailbox',
    zone: { x: wall.x, y: wall.y + wall.h, w: wall.w, h: TERMINAL_ZONE_DEPTH },
  })),
  bank: ROOM_COLLISIONS.bank.map((counter, i) => ({
    type: 'teller',
    tellerIndex: i,
    zone: { x: counter.x, y: counter.y + counter.h, w: counter.w, h: TERMINAL_ZONE_DEPTH },
  })),
  // The Library's bookshelves -- the file-review interaction the shelves
  // were always meant to front (the Library building already wires
  // openLibrary via openTerminal's building check), but the room had no
  // entry here, so walking up to a shelf never triggered anything. Same
  // collision-derived-zones pattern as every other room -- each of the
  // six shelves (3 per bank of shelving) gets a thin walk-in strip in
  // front of it. Any one of them opens the file review.
  library: ROOM_COLLISIONS.library.map(shelf => ({
    type: 'bookshelf',
    zone: { x: shelf.x, y: shelf.y + shelf.h, w: shelf.w, h: TERMINAL_ZONE_DEPTH },
  })),
};
