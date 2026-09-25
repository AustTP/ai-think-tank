// Agent-on-agent reports, modeled on the reference screenshots you shared
// (Tristen's "report-greg.md -- wes", quoting greg's own AGENTS.md as
// evidence, stamped CAUGHT). Not a per-agent file everyone has -- reports
// are filed conditionally, only when something's flagged, same as the
// reference.
//
// There's no real agent reasoning yet (Phase 3, not started) to notice a
// violation on its own, so filing is a player-driven action for now: you
// review an agent's profile, pick one of their own operating-instruction
// lines as the quoted evidence, choose who's filing it (any other agent,
// or yourself), and add a note. The reference's "742 notes about each
// other" were emergent; this is the same mechanic without the emergent
// part -- Phase 3 territory once agents can actually catch each other.

let REPORTS = []; // { id, aboutId, fromId, quote, note, ts, severity }
let nextReportId = 1;

// Fire-and-forget -- fills in `severity` moments after the report already
// exists, so filing itself stays instant rather than waiting on a model
// call. Fails toward the SMALLEST consequence ('minor'), not the
// largest -- the opposite direction from serve.py's browsing gate, which
// fails toward blocking. Here, an unreachable classifier wrongly nudging
// someone toward a firing review is the actual risk, not the reverse.
async function classifyReportSeverity(report) {
  const decision = await requestJevChoice(
    `A report was filed quoting one of an agent's own operating instructions as evidence: "${report.quote}" -- with this note from the filer: "${report.note}". Classify how serious this is.`,
    [
      { id: 'minor', description: 'A small, low-stakes slip -- the normal morale penalty is enough on its own.' },
      { id: 'serious', description: 'A real, repeated, or higher-stakes failure admins should be aware of.' },
      { id: 'severe', description: 'Serious enough on its own that it should weigh toward a performance review.' },
    ]
  );
  report.severity = (decision && decision.choice) || 'minor';
}

function fileReport(aboutId, fromId, quote, note) {
  const report = { id: 'report-' + (nextReportId++), aboutId, fromId, quote, note: note.trim(), ts: Date.now(), severity: null };
  REPORTS.push(report);
  classifyReportSeverity(report);
  notifySupervisorOfReport(report);
  return report;
}

// Per your call: a report shouldn't silently pile up into a firing review --
// the SUBJECT'S OWN MANAGER has to be notified first. Reaching the manager
// also fixes an asymmetry: the queued firing review only ever hears about a
// candidate from the Jev prompt (firer's say-so + the peer review), never
// from the person who actually oversees the candidate day to day. So filing
// a report now routes a line to the candidate's direct manager (AGENT_ROSTER
// `director`, the walk-the-chain supervisor), unread in their mailbox, so
// the manager knows and can act BEFORE any firing review happens.
// Safe by construction: if the roster/manager can't be resolved (test
// harness, or a player-adjacent subject), this is a no-op, never a throw.
function notifySupervisorOfReport(report) {
  if (typeof AGENT_ROSTER === 'undefined' || typeof AGENTS === 'undefined') return;
  const subjectDef = AGENT_ROSTER.find(d => d.id === report.aboutId);
  if (!subjectDef || !subjectDef.director) return; // no supervisor on record (an admin/director tops out the chain) -- nothing to route
  const supervisor = AGENTS[subjectDef.director];
  if (!supervisor) return; // manager not currently instantiated -- nothing to deliver to
  const fromName = report.fromId === 'player' ? 'the player' : (AGENT_ROSTER.find(d => d.id === report.fromId)?.name || report.fromId);
  supervisor.mailbox = supervisor.mailbox || [];
  supervisor.mailbox.push({
    text: `Report filed against ${subjectDef.name} (${subjectDef.role}) -- someone you oversee. Filed by ${fromName}: "${report.quote}" ${report.note ? '-- ' + report.note : ''} A firing review that touches them will now consult you.`,
    read: false,
    ts: Date.now(),
  });
}

function reportsAbout(aboutId) {
  return REPORTS.filter(r => r.aboutId === aboutId);
}
