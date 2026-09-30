// Meeting manager for Town Hall's chat-interface "call" mechanic.
//
// Any number of independent meetings can run at the same
// time (one person calling three others doesn't block someone else from
// immediately calling two different people); an agent already in a meeting
// is "busy" and is never pulled out of their current call into a new one;
// everyone in a finished meeting reappears somewhere new on the map, never
// back at the exact spot they vanished from and never on top of someone
// else who's there now.
//
// The player is a participant like any other, identified by the id
// 'player' -- their busy-state is state.location.kind === 'meeting' rather
// than an AGENTS entry, since their position lives in state.player.

let MEETINGS = {}; // id -> { id, initiator, participants: [ids], startedAt }
let nextMeetingId = 1;

let MEETING_PRESETS = {}; // name -> [agent ids], see meeting_presets.json

async function loadMeetingPresets() {
  const res = await fetch('meeting_presets.json?v=' + Date.now());
  MEETING_PRESETS = await res.json();
}

function isAgentBusy(id) {
  if (id === 'player') return state.location.kind === 'meeting';
  const a = AGENTS[id];
  return !!(a && a.busy);
}

// participantIds should NOT include the initiator. Busy agents are silently
// dropped rather than pulled out of their current meeting. Returns the new
// meeting record, or null if the initiator is themselves already busy, or
// no one eligible was left to call.
function startMeeting(initiatorId, participantIds) {
  if (isAgentBusy(initiatorId)) return null;

  const eligible = participantIds.filter(id => id !== initiatorId && !isAgentBusy(id));
  if (eligible.length === 0) return null;

  const id = 'meeting-' + (nextMeetingId++);
  const meeting = {
    id, initiator: initiatorId, participants: [initiatorId, ...eligible], startedAt: Date.now(),
    // groupLog is the whole-call thread everyone sees. dmLogs holds private
    // side-channels, keyed by dmKey() so a thread between two people is the
    // same object regardless of who's asking -- this pass has no AI replies
    // (see DESIGN.md), so only the player ever actually posts right now,
    // but the shape already supports any participant as `fromId`/`toId` for
    // when real agents can post here too.
    groupLog: [],
    dmLogs: {},
  };
  MEETINGS[id] = meeting;

  // The initiator is a participant too and disappears just like anyone
  // they called -- for 'player' this is handled separately (state.location
  // in index.html, since the player isn't an AGENTS entry), but an agent
  // initiator needs marking here or they'd stay visible and un-busy despite
  // supposedly being in their own call.
  for (const pid of meeting.participants) {
    if (pid === 'player') continue;
    const a = AGENTS[pid];
    a.busy = true;
    a.visible = false;
    a.meetingId = id;
  }
  return meeting;
}

// Repositions every non-player participant to a fresh, unoccupied spot and
// clears their busy state. Returns the list of now-occupied points so the
// caller can place the player (not tracked in AGENTS) using the same
// avoid-list before adding the player's own new spot to the map.
function endMeeting(meetingId) {
  const meeting = MEETINGS[meetingId];
  if (!meeting) return [];

  const occupied = Object.values(AGENTS).filter(a => a.visible).map(a => ({ x: a.x, y: a.y }));
  for (const pid of meeting.participants) {
    if (pid === 'player') continue;
    const a = AGENTS[pid];
    occupied.push({ x: a.x, y: a.y }); // their pre-call spot -- don't respawn back onto it either
  }

  for (const pid of meeting.participants) {
    if (pid === 'player') continue;
    const a = AGENTS[pid];
    const spot = pickFreeSpot(occupied);
    a.x = spot.x; a.y = spot.y;
    a.busy = false; a.visible = true; a.meetingId = null;
    occupied.push(spot);
  }

  delete MEETINGS[meetingId];
  return occupied;
}

// Stable, order-independent key for the private thread between two
// participants -- the same DM thread whether it's fetched as (player, ada)
// or (ada, player).
function dmKey(idA, idB) {
  return [idA, idB].sort().join('|');
}

// toId omitted (or null) posts to the whole-call group thread; set it to
// direct-message just that one participant, without leaving or interrupting
// the group thread -- both keep going in parallel for the rest of the call.
//
// Also the one live signal morale.js's neglect penalty reads (markContacted,
// agents.js) -- a group message counts as contact for everyone else in the
// call, a DM only for its one recipient.
function postMessage(meetingId, fromId, text, toId = null) {
  const meeting = MEETINGS[meetingId];
  if (!meeting || !text.trim()) return;
  const entry = { fromId, toId, text: text.trim(), ts: Date.now() };
  if (toId) {
    const key = dmKey(fromId, toId);
    (meeting.dmLogs[key] ||= []).push(entry);
    markContacted(toId, entry.ts);
  } else {
    meeting.groupLog.push(entry);
    for (const pid of meeting.participants) {
      if (pid !== fromId) markContacted(pid, entry.ts);
    }
  }
}
