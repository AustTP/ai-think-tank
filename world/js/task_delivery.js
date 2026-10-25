// Workroom/research/media dispatch, task completion, and spawn helpers.
// Split out of tasks.js to keep every script under 1,000 lines; classic
// script scope means these globals are shared exactly as before.


// Fix (hiring-to-shutdown audit): this used to run the
// exact SAME fixed helper.py health-check every single time, completely
// ignoring whatever the assigned task actually said -- confirmed,
// a real coding subtask generated from assignBigTask (e.g. "build a
// simple to-do list page") arrived here and produced nothing resembling
// it, because nothing about the task's own title/instructions was ever
// read. Uses the SAME shared sandbox every time (WORKROOM_SANDBOX_ID,
// index.html) -- agents should reuse it, not each get a
// throwaway one, so real work persists across visits. When the task
// actually came from a real project (projectLabel set -- see
// assignBigTask), this now calls the same real coding pipeline
// runCodingTask() uses (probe-before-writing, heredoc-continuation
// handling, orphaned-file detection), with a real listing of the sandbox's
// own current files as context instead of a guessed one. Falls back to the
// original fixed health-check only for a bare ambient task with no real
// project lineage at all. A
// taskType of "review" or "qa" instead runs runReviewTask (above) --
// this dispatch only ever writes/edits code.
async function runWorkroomTask(agentId, task) {
  const a = AGENTS[agentId];
  if (!a) return;
  const projectLabel = task && task.projectLabel;
  let note;
  if (projectLabel && (task.taskType === 'review' || task.taskType === 'qa')) {
    // A real review/QA pass, generally delegatable through assignBigTask --
    // see runReviewTask below.
    const result = await runReviewTask(agentId, task, projectLabel);
    note = result.note;
  } else if (projectLabel) {
    const backlogItem = task.instructions ? `${task.title} -- ${task.instructions}` : task.title;
    let contextSummary = '';
    try {
      const lsRes = await agentFetch('/api/execute', agentId, {
        method: 'POST',
        body: JSON.stringify({ agentId, sandboxId: WORKROOM_SANDBOX_ID, command: 'ls -la' }),
      });
      const lsData = await lsRes.json();
      if (lsData.allowed && lsData.stdout) contextSummary = `Current files in the shared Work Room sandbox:\n${lsData.stdout}`;
    } catch (e) { /* fine without it -- runCodingTask's own fallback text covers an empty summary */ }

    const result = await runCodingTask(agentId, WORKROOM_SANDBOX_ID, backlogItem, contextSummary, projectLabel);
    note = result.ok
      ? `Worked in the shared Work Room sandbox on: ${task.title}.`
      : `Tried to work on "${task.title}" in the Work Room, but ${result.note || 'the attempt did not succeed'}.`;
  } else {
    try {
      const res = await agentFetch('/api/pipeline', agentId, {
        method: 'POST',
        body: JSON.stringify({
          agentId,
          sandboxId: WORKROOM_SANDBOX_ID,
          steps: [
            { name: 'update helper script', command: "echo 'print(\"helper script checked and working\")' > helper.py" },
            { name: 'run it', command: 'python3 helper.py' },
          ],
        }),
      });
      const data = await res.json();
      note = data.failedStep
        ? `Worked in the shared sandbox -- hit a failure at "${data.failedStep}".`
        : 'Checked and ran the shared tooling in the Work Room -- all good.';
    } catch (e) {
      note = 'Tried to work in the Work Room sandbox, but the request failed.';
    }
  }
  if (AGENTS[agentId]) {
    AGENTS[agentId].profile.notes.push(note);
    if (AGENTS[agentId].profile.notes.length > 5) AGENTS[agentId].profile.notes.shift();
  }
}

// "A research agent will need to review the skill
// files and determine which are relevant and which should stick." Lists
// the real Library (listLibraryFile, world.js) rather than trusting any
// in-memory record of what's pending -- pending_review/skills/*.md is the
// one real source of truth for what's actually waiting, regardless of
// which task or topic wrote it. Capped at 5 per sweep, same "bound one
// pass's cost" reasoning as crawlAndCollect's own maxPages -- a real
// backlog drains over several ticks rather than one unbounded burst.
// Promote/reject reuse the exact real judgment-call shape runReviewTask
// already uses (a Jev pick between two named outcomes over real content),
// just a different question.
const SKILL_REVIEW_MAX_PER_SWEEP = 5;

async function runSkillReviewTask(agentId) {
  const files = await listLibraryFiles(agentId);
  const pending = files.filter(f => f.path.includes('pending_review/skills/')).slice(0, SKILL_REVIEW_MAX_PER_SWEEP);
  if (pending.length === 0) return 'Checked for pending skill files -- nothing waiting right now.';

  let kept = 0, rejected = 0;
  for (const f of pending) {
    const content = await readLibraryFile(f.path, agentId);
    if (!content) continue; // genuinely unreadable -- leave it for a later sweep rather than guessing
    const verdict = (await requestJevChoice(
      `A candidate skill-reference file is waiting for review. Its content: "${content.slice(0, 1500)}" Is this accurate and genuinely useful as real reference material for future work, or should it be discarded?`,
      [
        { id: 'keep', description: 'Yes -- accurate and specific enough to be worth keeping as real reference material.' },
        { id: 'reject', description: 'No -- inaccurate, too vague/generic to be useful, or not actually relevant to real work here.' },
      ]
    ))?.choice;
    if (verdict === 'reject') {
      await writeLibraryFile(agentId, f.path, `${content}\n\n## Rejected\n\nReviewed and rejected on ${new Date().toISOString()} -- judged not accurate/relevant enough to keep as trusted reference material.\n`, 'firsthand');
      await rejectLibraryFile(agentId, f.path);
      rejected++;
    } else {
      await promoteLibraryFile(agentId, f.path);
      kept++;
    }
  }
  return `Reviewed ${pending.length} pending skill file(s): ${kept} promoted, ${rejected} rejected.`;
}

// Fix (hiring-to-shutdown audit): this used to run the
// exact SAME fixed log-and-tally action every time, regardless of what
// the assigned task actually asked for -- same gap class as
// runWorkroomTask's own fix. The Weather Station room now routes
// through this same open-ended research path. Uses the SAME persistent
// sandbox every time (RESEARCH_SANDBOX_ID) -- agents should reuse it, not
// each get a throwaway one. When the task has real project lineage
// (projectLabel set), this now makes a real model call reasoning about
// the SPECIFIC subtask and writes a genuine finding -- to findings.log
// via a quoted heredoc (never a raw string interpolated into a shell
// command; a heredoc with a quoted delimiter is immune to whatever
// quotes/`$()`/backticks the model's own prose happens to contain,
// same safe-write convention runCodingTask's own system prompt already
// teaches agents to use) and to the Library, so it's a real, findable
// record, not just a private note. Falls back to the original fixed
// log-and-tally only for a bare ambient task with no real project
// lineage at all.
async function runResearchTask(agentId, task) {
  const a = AGENTS[agentId];
  if (!a) return;
  const projectLabel = task && task.projectLabel;
  let note;
  if (task && task.skillReview) {
    // The skill-curation sweep -- see runSkillReviewTask
    // below. Checked ahead of task.research: this is its own standing
    // job, unrelated to any specific research topic.
    note = await runSkillReviewTask(agentId);
  } else if (task && task.research) {
    // The scheduled-topic path: crawlAndCollect/writeSkillFile/
    // readLibraryFile/skillSlug/SKILL_FILE_FORMAT_GUIDE all live in
    // world.js, loaded before tasks.js (index.html) -- real globals by the
    // time this ever runs, not a forward reference.
    const topic = RESEARCH_TOPICS.find(t => t.id === task.research.topicId);
    if (!topic) {
      note = `Scheduled research arrived with no matching topic record (it may have been removed) -- nothing to do.`;
    } else {
      const crawl = await crawlAndCollect(agentId, RESEARCH_SANDBOX_ID, topic.startUrl, `scheduled research: ${topic.topic}`, {
        maxPages: 6, linkKeyword: topic.linkKeyword, pageKeyword: topic.pageKeyword, skipUrls: topic.seenUrls,
        since: task.research.since || 0,
      });
      if (crawl.pagesKept === 0) {
        note = `Ran scheduled research for "${topic.topic}" -- nothing new since last time (checked ${crawl.pagesVisited} page(s)).`;
      } else {
        for (const p of crawl.pages) {
          if (!topic.seenUrls.includes(p.url)) topic.seenUrls.push(p.url);
        }
        const existingContent = await readLibraryFile(`skills/${skillSlug(topic.topic)}.md`, agentId);
        const tier = await pickModelTierForAction(a, `update the "${topic.topic}" skill file from freshly collected research`);
        const sourcesText = crawl.pages.map(p => `### ${p.url}\n${(p.text || '').slice(0, 3000)}`).join('\n\n');
        let updated = null;
        try {
          const res = await agentFetch('/api/chat', agentId, {
            method: 'POST',
            body: JSON.stringify({
              model: tier.slug,
              messages: [
                {
                  role: 'system',
                  content: `You are ${a.name}, updating a real skill-reference file for "${topic.topic}". ${SKILL_FILE_FORMAT_GUIDE} `
                    + (existingContent
                      ? `Here is the EXISTING skill file -- preserve what still holds, update what changed, add what is genuinely new:\n\n${existingContent.slice(0, 6000)}`
                      : `No existing skill file yet -- write one from scratch.`),
                },
                { role: 'user', content: `Freshly collected sources:\n\n${sourcesText}` },
              ],
              max_tokens: 900,
              agentId,
            }),
          });
          const data = await res.json();
          if (res.ok && !data.error && data.reply) updated = data.reply.trim();
        } catch (e) { /* falls through to the failure note below */ }

        if (updated) {
          await writeSkillFile(agentId, topic.topic, updated, 'external');
          note = `Ran scheduled research for "${topic.topic}" -- collected ${crawl.pagesKept} new page(s) and wrote an updated skill file (pending review).`;
        } else {
          note = `Collected ${crawl.pagesKept} new page(s) for "${topic.topic}", but the skill-file synthesis call didn't produce anything usable.`;
        }
      }
    }
  } else if (projectLabel) {
    const backlogItem = task.instructions ? `${task.title} -- ${task.instructions}` : task.title;
    const tier = await pickModelTierForAction(a, `research: ${backlogItem}`);
    let finding = null;
    try {
      const res = await agentFetch('/api/chat', agentId, {
        method: 'POST',
        body: JSON.stringify({
          model: tier.slug,
          messages: [
            { role: 'system', content: `You are ${a.name}, researching for a real project: ${projectLabel}. Your specific task right now: ${backlogItem}. Write 2-4 honest, concrete sentences of real findings or analysis -- no filler like "I will research this," an actual answer or set of concrete points.` },
            { role: 'user', content: 'Go ahead.' },
          ],
          max_tokens: 400,
          agentId,
        }),
      });
      const data = await res.json();
      if (res.ok && !data.error && data.reply) finding = data.reply.trim();
    } catch (e) { /* falls through to the failure note below */ }

    if (finding) {
      try {
        // The real timestamp is computed here, in JS, and embedded as
        // plain literal text -- NOT left as a shell $(date) inside the
        // heredoc body, which a quoted delimiter (below) would write out
        // as the literal four characters "$(date)" instead of actually
        // running it. The quoted delimiter itself is what keeps the
        // MODEL's own finding text safe regardless of quotes/`$()`/
        // backticks it happens to contain.
        await agentFetch('/api/pipeline', agentId, {
          method: 'POST',
          body: JSON.stringify({
            agentId,
            sandboxId: RESEARCH_SANDBOX_ID,
            steps: [{ name: 'log this finding', command: `cat >> findings.log << 'FINDING_EOF'\n${new Date().toISOString()}: ${task.title}\n${finding}\nFINDING_EOF` }],
          }),
        });
      } catch (e) { /* the Library write below still records it even if this fails */ }
      writeLibraryFile(agentId, `archive/${Date.now()}-research-${task.id || 'adhoc'}.md`, `# ${task.title}\n\nProject: ${projectLabel}\nBy: ${a.name}\n\n${finding}\n`);
      note = `Researched "${task.title}" for real and logged the finding.`;
    } else {
      note = `Tried to research "${task.title}", but the model call didn't produce anything usable.`;
    }
  } else {
    try {
      const res = await agentFetch('/api/pipeline', agentId, {
        method: 'POST',
        body: JSON.stringify({
          agentId,
          sandboxId: RESEARCH_SANDBOX_ID,
          steps: [
            { name: 'log this pass', command: "echo \"$(date): reviewed findings\" >> findings.log" },
            { name: 'tally findings so far', command: 'wc -l findings.log' },
          ],
        }),
      });
      const data = await res.json();
      note = data.failedStep
        ? `Worked in the Research Center sandbox -- hit a failure at "${data.failedStep}".`
        : 'Reviewed and logged this week\'s findings in the Research Center.';
    } catch (e) {
      note = 'Tried to work in the Research Center sandbox, but the request failed.';
    }
  }
  if (AGENTS[agentId]) {
    AGENTS[agentId].profile.notes.push(note);
    if (AGENTS[agentId].profile.notes.length > 5) AGENTS[agentId].profile.notes.shift();
  }
}

// Studio's real capability: you tell it what outside
// sources to watch (media/feeds.md, edited via the same Library UI as
// anything else -- no new config surface needed), and an agent assigned
// here fetches one, boils it down to a few honest sentences, and files the
// result at media/digests/ where the Library already makes it browsable.
// One line per feed in media/feeds.md, "url -- why it matters" or just a
// bare URL; blank lines and anything starting with # are ignored, so the
// player can leave themselves comments in the same file.
// NOTE: this only ever sees whatever a plain text fetch of the page
// returns -- there is no video-transcription capability here. A YouTube
// URL will get back page metadata, not the spoken content of the video.
const MEDIA_FEEDS_PATH = 'media/feeds.md';

function parseFeedUrls(text) {
  return text.split('\n')
    .map(l => l.trim())
    .filter(l => l && !l.startsWith('#'))
    .map(l => (l.match(/https?:\/\/\S+/) || [])[0])
    .filter(Boolean);
}

async function runMediaDigestTask(agentId) {
  const a = AGENTS[agentId];
  if (!a) return;
  let note;
  try {
    const feedsRes = await agentFetch('/api/library/file?path=' + encodeURIComponent(MEDIA_FEEDS_PATH), agentId);
    const feedsData = await feedsRes.json();
    const urls = feedsRes.ok ? parseFeedUrls(feedsData.content || '') : [];
    if (urls.length === 0) {
      note = 'No feeds configured yet -- waiting on media/feeds.md in the Library.';
    } else {
      const url = urls[Math.floor(Math.random() * urls.length)];
      // fetchPageSmart (world.js), not a raw /api/browse call: a
      // subscribed feed is exactly where a JS-rendered source (Reddit,
      // Twitter/X) would show up once you add one, and this automatically
      // falls back to a real rendered fetch only when the plain one comes
      // back looking like an empty shell, rather than silently filing a
      // digest of nothing.
      const browseData = await fetchPageSmart(agentId, url, 'Fetching a subscribed feed source to digest for the player.');
      if (!browseData.allowed) {
        note = `Tried to check a subscribed feed, but it wasn't approved: ${browseData.reason || 'no reason given'}`;
      } else if (!browseData.text) {
        note = `Checked ${url}, but the page came back empty.`;
      } else {
        const chatRes = await agentFetch('/api/chat', agentId, {
          method: 'POST',
          body: JSON.stringify({
            model: (MODEL_TIERS[a.model] || MODEL_TIERS.small).slug,
            messages: [
              { role: 'system', content: 'Summarize the following page in 2-3 short, honest sentences for someone who has not read it. Only report what is actually in the text -- do not invent detail.' },
              { role: 'user', content: browseData.text.slice(0, 6000) },
            ],
            max_tokens: 200,
            agentId,
          }),
        });
        const chatData = await chatRes.json();
        const summary = (chatData.reply || '').trim();
        if (!summary) {
          note = `Fetched ${url}, but couldn't summarize it this time.`;
        } else {
          const slug = url.replace(/^https?:\/\//, '').replace(/[^a-z0-9]+/gi, '-').slice(0, 40).toLowerCase();
          const path = `media/digests/${Date.now()}-${slug}.md`;
          const content = `# Digest -- ${new Date().toISOString()}\n\nSource: ${url}\nBy: ${a.name}\n\n${summary}\n`;
          await writeLibraryFile(agentId, path, content, 'firsthand');
          logThinkTankAction(agentId, 'media_digest_filed', { url, path });
          note = `Filed a digest on ${url} for the player.`;
        }
      }
    }
  } catch (e) {
    note = 'Tried to check a subscribed feed, but the request failed.';
  }
  if (AGENTS[agentId]) {
    AGENTS[agentId].profile.notes.push(note);
    if (AGENTS[agentId].profile.notes.length > 5) AGENTS[agentId].profile.notes.shift();
  }
}

function finishTask(id) {
  const a = AGENTS[id];
  const task = TASKS[a.task];
  if (task) { task.status = 'done'; lastTaskCompletedAt[task.room] = Date.now(); }

  a.task = null;
  a.busy = false;
  a.inRoom = null;
  // Bug: arriveAtTask() sets a.visible = false
  // on arrival (she's "inside" working, so the map shouldn't show her
  // outdoors) and nothing ever set it back. Since tickAgentMovement skips
  // invisible agents outright, that left EVERY agent who ever finished a
  // real task permanently invisible from this point on -- her own
  // off-duty walk home (sendAgentOffDuty) or a handoff walk (attemptHandoff,
  // handoffs.js) would get a real path assigned and then never actually
  // move, frozen forever with a path that just sits there. The "staying
  // visible ... until [the handoff] resolves" comment a few lines below
  // describes what was SUPPOSED to happen, not what the code did.
  a.visible = true;
  a.approvedCount++; // real completed work feeds morale, same signal as everything else

  // Reappear right where she walked in, not a random reachable spot --
  // She should walk out of the door she used. Falls back
  // to pickFreeSpot() only if this task somehow has no recorded entry
  // point (shouldn't happen via assignTask, but arriveAtTask() can still
  // be reached with a stale/missing task in edge cases).
  if (task && task.entryX != null) {
    // Deadlock: two agents finishing the SAME room's
    // task land on the identical entryX/entryY pixel. tickAgentMovement's
    // co-located exemption (agentBlockedAt) only ever buys the first one
    // a single step of separation -- nowhere near enough to clear
    // AGENT_W (20px) at real per-frame movement speed, so a SUBSEQUENT
    // walk from that exact shared point (off duty, a handoff) can
    // genuinely deadlock for real: confirmed, stuckTimer cycling
    // 0->TASK_STUCK_TIMEOUT->0 forever, replanning to the same nearby
    // cell every time, real position never moving. Checking for a real
    // occupant here and stepping aside by a full agent-width closes this
    // at the source, instead of relying on real-time collision recovery
    // to escape a full box overlap it was never built to escape.
    let ex = task.entryX, ey = task.entryY;
    for (const oid in AGENTS) {
      if (oid === id) continue;
      const other = AGENTS[oid];
      if (other && other.visible && other.x === ex && other.y === ey) {
        ex += AGENT_W + 8;
        break;
      }
    }
    a.x = ex; a.y = ey;
  } else {
    const occupied = Object.values(AGENTS).filter(v => v.visible && v.id !== id).map(v => ({ x: v.x, y: v.y }));
    const spot = pickFreeSpot(occupied);
    a.x = spot.x; a.y = spot.y;
  }

  // A plain local log entry, not a model call --
  // this shouldn't spend anything.
  if (task) {
    a.profile.notes.push(`${task.title} -- completed and logged.`);
    if (a.profile.notes.length > 5) a.profile.notes.shift();
  }

  showToast(`${a.name} finished: ${task ? task.title : 'a task'}.`, 4000);
  if (task) {
    logThinkTankAction(id, 'task_completed', { taskId: task.id, title: task.title, room: task.room });
    // Completed tasks get archived in the Library's shared
    // directory, not just logged privately -- a real record other agents
    // (or you) could actually read later, not just a database row.
    const record = `# ${task.title}\n\nCompleted by: ${a.name} (${id})\nRoom: ${task.room}\nCompleted: ${new Date().toISOString()}\n`;
    writeLibraryFile(id, `archive/${Date.now()}-${task.id}.md`, record);
  }

  // After logging it, she clocks off for the rest of this
  // session rather than immediately queuing for another task -- UNLESS
  // this task's room is something else's dependency (handoffs.js), in
  // which case she walks over and hands it off FIRST, staying visible and
  // on duty until that resolves. offDuty is excluded from
  // assignTaskViaJev's candidate list, and persists across a page reload
  // (initAgents(), agents.js) rather than resetting -- resting is a real,
  // deliberate state now, not something a reload undoes.
  if (task) {
    attemptHandoff(id, task.room, task.title).then(started => {
      if (!started) sendAgentOffDuty(id);
    });
  } else {
    sendAgentOffDuty(id);
  }
}

// 25 (MAX_ACTIVE_AGENTS, hiring.js)
// is a hard ceiling on how many agents may be online or freshly called
// in for a scheduled item AT ONCE -- not on the total inventory
// (MAX_TOTAL_AGENTS, which can be much larger, e.g. 100+, since most of
// it sits fully dormant). A dormant agent with nothing current or
// imminent counts toward neither number. Counts everyone on-duty,
// admins included, since the active ceiling is about real-time
// footprint on the map, not worker headcount specifically.
function activeAgentCount() {
  return AGENT_ROSTER.filter(d => !AGENTS[d.id].offDuty).length;
}
function canActivateAnother() {
  return activeAgentCount() < MAX_ACTIVE_AGENTS;
}

// The whole point of a larger hireable
// roster with only some agents "active" at once is that calling someone
// in is a real, visible event -- she appears at a free spot in the main
// think tank (outskirts rooms are gone) and walks from there, not an instant
// pop-in wherever her stale x/y happened to be left. Used by
// assignTaskViaJev/assignPairTask whenever a wake actually happens; the
// real walk to wherever she's needed is just assignTask's own ordinary
// pathfinding from this point, same as any other agent starting a task.
function appearFromOutskirts(agent) {
  agent.offDuty = false;
  agent.visible = true;
  agent.inRoom = null;
  const occupied = Object.values(AGENTS).filter(v => v.visible && v.id !== agent.id).map(v => ({ x: v.x, y: v.y }));
  const spot = pickFreeSpot(occupied);
  agent.x = spot.x;
  agent.y = spot.y;
}

// An agent going off duty VANISHES WHERE SHE
// STANDS -- no trek to a trailhead door (the outskirts rooms are gone).
// Mirrors the server's send_agent_off_duty so the client and server agree:
// an idle agent rests in place, invisible, inRoom cleared so she can't
// render inside a room the player walks into. Refusing anyone busy/mid-
// task/mid-pair/mid-handoff is what makes it safe to call on the WHOLE
// roster at once (a real "send everyone home" action) regardless of who's
// mid-task right now -- finishTask() calls this itself the instant a real
// task actually finishes, so refusing here just defers the request until
// the agent is genuinely idle.
function sendAgentOffDuty(id) {
  const a = AGENTS[id];
  if (!a) return;
  if (a.busy || a.task || a.pairWith || a.handoff) return;
  a.path = null;
  a.pathIndex = 0;
  a.pathTarget = null;
  a.headingOffDuty = null;
  a.offDuty = true;
  a.visible = false;
  a.inRoom = null;
}

// The other half: standing up for the first time (a brand-new hire) or
// coming back on duty appears at a free spot in the main think tank, not at
// a trailhead door (the outskirts rooms are gone). Mirrors the server's
// appear_from_outskirts / pick_free_spot so the client and server wake an
// agent identically. `occupied` is the same avoid-list shape pickFreeSpot()
// already takes elsewhere, so callers can pass in every other visible
// agent's position to avoid spawning her right on top of someone. The
// walk-to-target is a bonus (she visibly arrives where she'll wait), not a
// door-required step; if no path exists she stays at the free spot.
function spawnAgentAtFreeSpot(id, occupied = []) {
  const a = AGENTS[id];
  if (!a) return false;
  a.visible = true;
  a.inRoom = null;
  const spot = pickFreeSpot(occupied);
  a.x = spot.x;
  a.y = spot.y;
  // A short walk to a nearby free target makes her visibly arrive, but
  // standing on the free spot is already valid -- don't force a walk that
  // could stall her. Skip findPath entirely; she can pathfind when she's
  // actually assigned a task (assignTask's own machinery).
  return true;
}
