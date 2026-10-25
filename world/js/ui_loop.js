// Input handling, per-frame updates, and the main loop.
// Split out of main.js. main() runs at the end, after every module loads.

function readInput(p) {
  let dx = 0, dy = 0;
  if (state.keys.has('arrowup') || state.keys.has('w')) { dy -= 1; p.dir = 'north'; }
  if (state.keys.has('arrowdown') || state.keys.has('s')) { dy += 1; p.dir = 'south'; }
  if (state.keys.has('arrowleft') || state.keys.has('a')) { dx -= 1; p.dir = 'west'; }
  if (state.keys.has('arrowright') || state.keys.has('d')) { dx += 1; p.dir = 'east'; }
  if (!dx && !dy) return null;
  const len = Math.hypot(dx, dy);
  return { dx: dx / len, dy: dy / len };
}

function update(dt) {
  // Agents move independently of whatever the player is doing. Under the
  // Phase-2 flip the SERVER is the mover (sim.owner == 'server'): it advances
  // positions every SIM_TICK_S and the client only smooths toward them, so
  // the think tank keeps simulating with the browser closed. Before the flip the
  // client drives tickAgentMovement directly (today's behavior). Both sides
  // agree on authority by keying off the same owner flag.
  const serverOwns = typeof SIM_STATUS !== 'undefined' && SIM_STATUS && SIM_STATUS.owner === 'server';
  if (serverOwns && typeof applyServerPositions === 'function') {
    applyServerPositions(AGENTS);
  } else {
    tickAgentMovement(dt);
  }
  updateCameraFocus(dt);
  if (state.location.kind === 'room') updateRoom(dt);
  else updateOutside(dt);
}

function updateOutside(dt) {
  // Frozen while the Town Hall call-setup modal is open, or while the
  // player is themselves a meeting participant (vanished:
  // someone in a call doesn't get pulled away by walking, they're just not
  // on the map to move).
  if (state.ui || state.location.kind === 'meeting') return;

  const p = state.player;
  const input = readInput(p);
  if (input) {
    const { dx, dy } = input;

    // Visible agents block movement too, not just the static map -- see
    // agentBlockedAt() in agents.js.
    const outsideBlocked = box => blockedAt(box) || agentBlockedAt(box);

    // safety net for a bad/edited spawn point overlapping a footprint --
    // same pattern as the main game's outdoor collision.
    const alreadyStuck = outsideBlocked(p);

    const tryBoth = { x: p.x + dx * p.speed * dt, y: p.y + dy * p.speed * dt, w: p.w, h: p.h };
    clampTo(tryBoth, GROUND_W, GROUND_H);
    if (alreadyStuck || !outsideBlocked(tryBoth)) {
      p.x = tryBoth.x; p.y = tryBoth.y;
    } else {
      const tryX = { x: p.x + dx * p.speed * dt, y: p.y, w: p.w, h: p.h };
      clampTo(tryX, GROUND_W, GROUND_H);
      if (!outsideBlocked(tryX)) p.x = tryX.x;

      const tryY = { x: p.x, y: p.y + dy * p.speed * dt, w: p.w, h: p.h };
      clampTo(tryY, GROUND_W, GROUND_H);
      if (!outsideBlocked(tryY)) p.y = tryY.y;
    }
  }

  // Walking onto a building's door tile prompts entry, Pokemon-style -- no
  // separate interact key. Town Hall is now a MULTI-OPTION building:
  // its door opens a menu letting you choose between the physical
  // Hangout room and starting a meeting. House keeps opening the board.
  // The Phone Booth is a dialog-only trigger.
  for (const building in ROOM_DOOR_TRIGGERS) {
    if (overlaps(p, ROOM_DOOR_TRIGGERS[building])) {
      if (building === 'south_outlet') openSouthMenu();
      else if (building === 'townhall') openDoorMenu();
      else if (building === 'house') openBoard();
      else if (building === 'phonebooth') openPhoneBooth();
      else enterRoom(building);
      return;
    }
  }

  // The south village's north gate leads back to the main village, landing
  // just above the south outlet (not the default spawn).
  if (state.scene === 'south' && overlaps(p, SOUTH_GATE)) {
    returnToMainFromSouth();
    return;
  }

  // Scene transition at the shared connection path: walking off the main
  // map's right edge enters the winter (adversarial) village, and walking
  // off winter's left edge returns to the main village. Only while winter
  // is ENABLED -- otherwise the boundary is simply a wall. If winter was
  // disabled while the player was inside it, pull them back to the main
  // village rather than leaving them in a now-unwalkable map.
  if (state.scene === 'main') {
    if (WINTER_ENABLED) {
      const inBand = state.player.y >= CONNECTION_BAND.y0 && state.player.y <= CONNECTION_BAND.y1;
      if (inBand && state.player.x + state.player.w >= GROUND_W - 2) { switchScene('winter'); return; }
    }
  } else if (state.scene === 'winter') {
    if (WINTER_ENABLED) {
      const inBand = state.player.y >= CONNECTION_BAND.y0 && state.player.y <= CONNECTION_BAND.y1;
      if (inBand && state.player.x <= 2) { switchScene('main'); return; }
    } else {
      switchScene('main'); return;
    }
  }

  // Standing next to a (now-solid) agent shows a "press B to talk" prompt
  // rather than opening the conversation automatically --
  // you decide when the dialog pops up, walking up to someone shouldn't be
  // enough by itself. Busy agents (in a Town Hall call right now; the only
  // real busy-state that exists today) get a toast instead of a prompt --
  // there's nothing to press B for. A small buffer beyond their own
  // hitbox catches "standing right next to them," which collision now
  // naturally stops the player at.
  const CONVO_BUFFER = 4;
  let foundNearby = null, nearBusyAgent = null;
  for (const id in AGENTS) {
    const a = AGENTS[id];
    if (!agentIsDrawn(a)) continue;
    const zone = { x: a.x - CONVO_BUFFER, y: a.y - CONVO_BUFFER, w: AGENT_W + CONVO_BUFFER * 2, h: AGENT_H + CONVO_BUFFER * 2 };
    if (!overlaps(p, zone)) continue;
    if (a.busy) { nearBusyAgent = a; continue; }
    foundNearby = id;
    break;
  }
  if (foundNearby !== nearbyAgentId) {
    nearbyAgentId = foundNearby;
    updateInteractPrompt();
  }
  if (nearBusyAgent) showBusyToast(nearBusyAgent);
  else busyToastAgent = null; // re-arm so walking away and back shows it again
}

function updateRoom(dt) {
  if (state.ui) return; // frozen while the terminal dialog is open

  const p = state.player;
  const collisionKey = ROOMS[state.location.building].collision;
  const rects = ROOM_COLLISIONS[collisionKey];
  const input = readInput(p);
  if (input) {
    const { dx, dy } = input;
    const alreadyStuck = blockedInRoom(p, rects);

    const tryBoth = { x: p.x + dx * p.speed * dt, y: p.y + dy * p.speed * dt, w: p.w, h: p.h };
    clampTo(tryBoth, ROOM_NATIVE_W, ROOM_NATIVE_H);
    if (alreadyStuck || !blockedInRoom(tryBoth, rects)) {
      p.x = tryBoth.x; p.y = tryBoth.y;
    } else {
      const tryX = { x: p.x + dx * p.speed * dt, y: p.y, w: p.w, h: p.h };
      clampTo(tryX, ROOM_NATIVE_W, ROOM_NATIVE_H);
      if (!blockedInRoom(tryX, rects)) p.x = tryX.x;

      const tryY = { x: p.x, y: p.y + dy * p.speed * dt, w: p.w, h: p.h };
      clampTo(tryY, ROOM_NATIVE_W, ROOM_NATIVE_H);
      if (!blockedInRoom(tryY, rects)) p.y = tryY.y;
    }
  }

  // The room's own art has no drawn door -- the unseen exit is the open
  // bottom edge (see rooms.js/DESIGN.md) -- walking into that strip leaves.
  if (overlaps(p, ROOM_EXIT_TRIGGER)) { exitRoom(); return; }

  // Walking up to an object opens its interaction -- same "walk into it"
  // pattern as every other trigger in this game, just object-scoped within
  // a room. Branches on type since terminals and the mailbox wall open
  // different UIs.
  for (const item of ROOM_INTERACTABLES[collisionKey] || []) {
    if (overlaps(p, item.zone)) {
      if (item.type === 'mailbox') openMailbox(item);
      else if (item.type === 'teller') openTeller(item);
      else if (item.type === 'bookshelf') openLibrary(item);
      else openTerminal(item);
      return;
    }
  }
}

function wireZoom() {
  document.getElementById('zoomIn').addEventListener('click', () => {
    state.zoom = Math.min(ZOOM_MAX, state.zoom + ZOOM_STEP);
  });
  document.getElementById('zoomOut').addEventListener('click', () => {
    state.zoom = Math.max(ZOOM_MIN, state.zoom - ZOOM_STEP);
  });
  document.getElementById('zoomAll').addEventListener('click', () => {
    state.zoom = state.zoom === ZOOM_SHOW_ALL ? 1.5 : ZOOM_SHOW_ALL;
  });
}

async function main() {
  await loadSceneAssets();
  const playerSprites = {
    south: await loadImage('assets/character_south.png'),
    north: await loadImage('assets/character_north.png'),
    east: await loadImage('assets/character_east.png'),
    west: await loadImage('assets/character_west.png'),
  };
  const roomImages = {};
  // Start the world as soon as the scene background and player art are
  // ready. State, agents, generated sprites, and room art arrive later;
  // none of them may hold the first frame hostage.
  let last = performance.now();
  requestAnimationFrame(frame);
  await loadDoorTriggers();
  await loadMeetingPresets();
  // Real ordering fix: a genuinely fresh start now generates each seed
  // agent's onboarding profile via a real LLM call inside initAgents(),
  // which needs MODEL_TIERS already populated with real slugs -- it used
  // to load after, which would have made that first-ever boot call fail
  // silently (or use a stale placeholder slug) every time.
  await loadModelTiers();
  if (typeof loadRoomPurposes === 'function') await loadRoomPurposes();
  await initAgents();
  // Server-side sim loop liveness -- starts the poll; the server engine ticks
  // regardless, this only lets the browser observe/verify it.
  if (typeof startSimPoll === 'function') startSimPoll();
  if (typeof pollAdversarialVillage === 'function') {
    pollAdversarialVillage();
    setInterval(pollAdversarialVillage, 3000);
  }
  // Several buildings share the same room image (see rooms.js) -- load
  // each distinct path once and key the cache by path, not by building.
  // (roomImages is declared above with the early render start.)

  // Per-agent generated sprites: load each non-admin agent's 4-direction set
  // from /api/avatar/<id>/<orientation>.png. Best-effort -- any orientation
  // that 404s (PixelLab unavailable, no credits, or generation still pending)
  // is null and drawAgentAt falls back to the shared default player sprite.
  //
  // This must never gate the first frame. It used to be awaited inline, so
  // 4 requests per agent ran in series before requestAnimationFrame() was
  // ever called: 37 agents meant ~148 round trips and a black canvas for
  // over a minute on a busy server. The world now renders from the scene
  // background and default player sprites immediately; generated art swaps
  // in as it arrives.
  agentSprites = {};
  void (async () => {
    const adminIds = new Set(AGENT_ROSTER.filter(d => d.isAdmin).map(d => d.id));
    for (const id in AGENTS) {
      if (adminIds.has(id)) continue;
      const set = {};
      for (const o of ['south', 'north', 'east', 'west']) {
        set[o] = await loadImage(`/api/avatar/${id}/${o}.png`).catch(() => null);
      }
      if (['south', 'north', 'east', 'west'].some(o => set[o])) agentSprites[id] = set;
    }
    for (const building in ROOMS) {
      const path = ROOMS[building].image;
      if (!roomImages[path]) roomImages[path] = await loadImage(path).catch(() => null);
    }
  })().catch(e => console.warn('agent art preload failed:', e));

  window.addEventListener('keydown', e => {
    // Typing in the chat box (or any future text field) shouldn't leak into
    // game controls -- without this, typing a message containing "g" would
    // toggle the debug overlay mid-sentence.
    if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;
    const k = e.key.toLowerCase();
    state.keys.add(k);
    if (k === 'g') state.showFootprints = !state.showFootprints;
    if (k === 'b' && nearbyAgentId && !state.ui && state.location.kind === 'outside') {
      openConversation(nearbyAgentId);
    }
  });
  window.addEventListener('keyup', e => state.keys.delete(e.key.toLowerCase()));
  wireZoom();

  // Click-to-follow: hit-test against each visible agent's actual drawn
  // sprite box (same geometry drawAgentAt uses), converting the click
  // through the SAME camXY() the current frame's render used so this
  // can't drift out of sync with what's on screen.
  canvas.addEventListener('click', e => {
    if (state.ui || state.location.kind !== 'outside') return;
    const rect = canvas.getBoundingClientRect();
    const zoom = state.zoom;
    const sx = (e.clientX - rect.left) * (canvas.width / rect.width);
    const sy = (e.clientY - rect.top) * (canvas.height / rect.height);
    const [camX, camY] = camXY(zoom, GROUND_W, GROUND_H, cameraFocus.x, cameraFocus.y);
    const wx = sx / zoom + camX, wy = sy / zoom + camY;

    let hit = null;
    for (const id in AGENTS) {
      const a = AGENTS[id];
      if (!agentIsDrawn(a)) continue;
      const spr = playerSprites[a.dir];
      const left = a.x - (spr.width - AGENT_W) / 2;
      const top = a.y - (spr.height - AGENT_H);
      if (wx >= left && wx <= left + spr.width && wy >= top && wy <= top + spr.height) { hit = id; break; }
    }
    if (hit) {
      followAgentId = (followAgentId === hit) ? null : hit;
      showToast(followAgentId ? `Following ${AGENTS[hit].name} -- click again to stop.` : 'Stopped following.');
    } else if (followAgentId) {
      followAgentId = null;
      showToast('Stopped following.');
    }
  });

  // Governance (hiring.js / firing.js) and tasks (tasks.js).
  // Phase 3 (server-owned task lifecycle): when the server owns the sim
  // (sim.owner == 'server'), the server's task loop consumes the workQueue,
  // runs the research/skill-review schedules, assigns, completes tasks, AND
  // (Phase 4) drives auto-hire and auto-firing reviews as cadence passes.
  // The browser must NOT also run these -- it would double-consume the queue,
  // fight the server's assignment, and run governance against a stale roster.
  // Under server ownership these timers are simply not scheduled (the browser
  // becomes a pure renderer/player), exactly like the movement flip.
  // SIM_STATUS is populated by startSimPoll (a few hundred ms above) before
  // this point in main().
  const serverDrivesTasks = typeof SIM_STATUS !== 'undefined' && SIM_STATUS
    && SIM_STATUS.owner === 'server';

  // Checks every few seconds whether the admin is free to hire -- most
  // calls no-op (cooldown, admin busy, or nobody needs help); see
  // hiring.js for the actual conditions. Server-owned under sim.owner=='server'.
  if (!serverDrivesTasks) setInterval(attemptAutoHire, 5000);

  // Same idea for firing (firing.js) -- needs both admins free at once
  // and a low-enough morale candidate, so most calls no-op same as
  // hiring's. Server-owned under sim.owner=='server'.
  if (!serverDrivesTasks) setInterval(attemptAutoFiringReview, 7000);

  // Periodic autosave (Phase 2 persistence, agents.js) rather than hooking
  // every single state-mutating call site -- catches everything (morale
  // inputs, reports, mailbox, hires, contact timestamps from chat) with
  // one mechanism instead of chasing each one individually. Still scheduled
  // under server ownership: the client pushes player intent / chat / reports
  // through, and the server's merge accepts the player-facing fields while
  // keeping server-owned positions/queue/tasks.
  setInterval(saveState, 5000);

  // Player Inbox: a slow badge poll keeps the HUD count fresh (an awaiting
  // question is a genuinely urgent thing -- an agent is frozen on it), and
  // costs a single local SQLite read every 10s. Never runs while the panel
  // is open (that path polls every 3s and the modal's own timer takes over).
  refreshInboxCount();
  setInterval(refreshInboxCount, 10000);

  // Tasks (tasks.js), a few seconds after load so everything else has
  // settled first, then on a recurring cycle -- a single one-shot demo
  // task meant only ever ONE agent ever moved; everyone else (Omar
  // included) just stood still forever with nothing to
  // do. Jev (jev.js) picks who does each one from whoever's idle.
  if (!serverDrivesTasks) {
    setTimeout(runTaskCycle, 3000);
    setInterval(runTaskCycle, 6000);
    // Deliberately its own interval call, not folded into runTaskCycle
    // itself -- runTaskCycleBody short-circuits before any call at all when
    // WORK_QUEUE has nothing due yet (the idle-think tank-spends-nothing
    // contract), but a standing research topic's cadence needs checking
    // regardless of whatever's already queued. checkResearchSchedule()
    // itself makes zero calls either way -- it only ever turns a due topic
    // INTO a real queueWork() item, which the very next runTaskCycle tick
    // picks up through the existing, already-tested path.
    setInterval(checkResearchSchedule, 6000);
    // Same reasoning as checkResearchSchedule -- its own standing job, its
    // own cadence, checked regardless of the queue's current state.
    setInterval(checkSkillReviewSchedule, 6000);
  }

  renderPresetButtons();
  wireDoorMenu();
  wireSouthOutletMenu();
  document.getElementById('startCallBtn').addEventListener('click', handleStartCall);
  document.getElementById('suggestAttendeesBtn').addEventListener('click', handleSuggestAttendees);
  document.getElementById('cancelCallBtn').addEventListener('click', handleCancelCall);
  document.getElementById('endCallBtn').addEventListener('click', handleEndCall);
  document.getElementById('chatSendBtn').addEventListener('click', handleChatSend);
  document.getElementById('chatInput').addEventListener('keydown', e => {
    if (e.key === 'Enter') handleChatSend();
  });
  document.getElementById('terminalCloseBtn').addEventListener('click', closeTerminal);
  document.getElementById('mailboxCloseBtn').addEventListener('click', closeMailbox);
  document.getElementById('tellerCloseBtn').addEventListener('click', closeTeller);
  document.getElementById('browserCloseBtn').addEventListener('click', closeBrowser);
  document.getElementById('browserGoBtn').addEventListener('click', handleBrowserGo);
  document.getElementById('browserCurlBtn').addEventListener('click', handleBrowserCurl);
  document.getElementById('browserUrlInput').addEventListener('keydown', e => { if (e.key === 'Enter') handleBrowserGo(); });
  document.getElementById('workroomCloseBtn').addEventListener('click', closeWorkroom);
  document.getElementById('workroomRunBtn').addEventListener('click', handleWorkroomRun);
  document.getElementById('researchCloseBtn').addEventListener('click', closeResearch);
  document.getElementById('researchRunBtn').addEventListener('click', handleResearchRun);
  document.getElementById('libraryCloseBtn').addEventListener('click', closeLibrary);
  document.getElementById('libraryWriteBtn').addEventListener('click', handleLibraryWrite);
  document.getElementById('libraryIngestBtn').addEventListener('click', handleLibraryIngest);
  document.getElementById('designRefUploadBtn').addEventListener('click', handleDesignRefUpload);
  document.getElementById('designRefUrlBtn').addEventListener('click', handleDesignRefUrl);
  document.getElementById('designRefTasteBtn').addEventListener('click', handleDesignRefTaste);
  document.getElementById('librarySearchBtn').addEventListener('click', handleLibrarySearch);
  document.getElementById('librarySearchInput').addEventListener('keydown', e => { if (e.key === 'Enter') handleLibrarySearch(); });
  document.getElementById('agentFilePicker').addEventListener('change', e => {
    const agentId = e.target.value;
    if (agentId) renderAgentFileList(agentId);
    else document.getElementById('agentFileList').innerHTML = '';
  });
  document.getElementById('convoCloseBtn').addEventListener('click', closeConversation);
  document.getElementById('convoSendBtn').addEventListener('click', handleConversationSend);
  document.getElementById('convoInput').addEventListener('keydown', e => {
    if (e.key === 'Enter') handleConversationSend();
  });
  document.getElementById('terminalInput').addEventListener('keydown', e => {
    if (e.key === 'Enter') handleTerminalSend();
  });
  document.getElementById('hudCallMeetingBtn').addEventListener('click', openMeetingUI);
  document.getElementById('hudPhoneBtn').addEventListener('click', openPhoneBooth);
  document.getElementById('hudCommandCenterBtn').addEventListener('click', () => enterRoom('commandcenter'));
  document.getElementById('hudLogoutBtn').addEventListener('click', () => {
    fetch('/logout', { method: 'POST' }).finally(() => { window.location.href = '/'; });
  });
  const publishBtnEl = document.getElementById('publishBtn');
  if (publishBtnEl) publishBtnEl.addEventListener('click', handlePublish);
  document.getElementById('phoneCloseBtn').addEventListener('click', closePhoneBooth);
  document.getElementById('boardCloseBtn').addEventListener('click', closeBoard);
  document.getElementById('bigTaskBtn').addEventListener('click', handleBigTaskDelegate);
  document.getElementById('rosterListCloseBtn').addEventListener('click', closeRosterList);
  document.getElementById('profileBackBtn').addEventListener('click', backToRosterFromProfile);
  document.getElementById('profileCloseBtn').addEventListener('click', closeProfile);

  function render() {
    renderHud();
    if (state.location.kind === 'room') renderRoom();
    else renderOutside();
  }

  function renderHud() {
    // This counts who's actually ACTIVE right
    // now (activeAgentCount(), tasks.js -- capped at MAX_ACTIVE_AGENTS,
    // hiring.js), not the total hired roster -- the whole point of a much
    // larger hireable inventory with only some agents present at once is
    // that this number should visibly drop when someone clocks off and
    // rise again when the think tank calls them back in. Only count VISIBLE
    // busy agents: an off-duty agent is invisible, so a stale `busy` flag
    // on a resting agent (a known ghost, e.g. theo parked at the origin)
    // must not count as someone actually working.
    document.getElementById('hudResearchers').textContent = activeAgentCount();
    document.getElementById('hudWorking').textContent = Object.values(AGENTS).filter(a => a.busy && a.visible).length;
    document.getElementById('hudMorale').textContent = thinkTankMorale();
  }

  function cameraFor(zoom, worldW, worldH, focusX, focusY) {
    const [camX, camY] = camXY(zoom, worldW, worldH, focusX, focusY);
    return (x, y) => [(x - camX) * zoom, (y - camY) * zoom];
  }

  function drawPlayer(toScreen, zoom) {
    // background is one flat baked image, so the player is the only sprite
    // -- no y-sort needed, just draw it on top every frame.
    const spr = playerSprites[state.player.dir];
    const [px, py] = toScreen(
      state.player.x - (spr.width - state.player.w) / 2,
      state.player.y - (spr.height - state.player.h)
    );
    ctx.drawImage(spr, px, py, spr.width * zoom, spr.height * zoom);
  }

  function renderOutside() {
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    const zoom = state.zoom;
    const toScreen = cameraFor(zoom, GROUND_W, GROUND_H, cameraFocus.x, cameraFocus.y);

    const [bx, by] = toScreen(0, 0);
    ctx.drawImage(SCENE.bg, bx, by, GROUND_W * zoom, GROUND_H * zoom);
    renderAgents(ctx, toScreen, zoom, playerSprites);
    // The player vanishes from the map for the duration of a call, same as
    // any other participant -- see meetings.js.
    if (state.location.kind !== 'meeting') drawPlayer(toScreen, zoom);

    if (state.showFootprints) {
      if (COLLISION_GRID) {
        ctx.fillStyle = 'rgba(255,40,40,0.4)';
        const { cols, rows, cell, grid } = COLLISION_GRID;
        const cellWorld = cell * SCALE;
        for (let gy = 0; gy < rows; gy++) {
          for (let gx = 0; gx < cols; gx++) {
            if (!grid[gy][gx]) continue;
            const [dx, dy] = toScreen(gx * cellWorld, gy * cellWorld);
            ctx.fillRect(dx, dy, cellWorld * zoom, cellWorld * zoom);
          }
        }
      }
      ctx.strokeStyle = 'rgba(255,210,60,0.9)';
      for (const building in ROOM_DOOR_TRIGGERS) {
        const d = ROOM_DOOR_TRIGGERS[building];
        const [dx, dy] = toScreen(d.x, d.y);
        ctx.strokeRect(dx, dy, d.w * zoom, d.h * zoom);
      }
      ctx.strokeStyle = 'rgba(255,120,120,0.9)';
      for (const id in AGENTS) {
        const a = AGENTS[id];
        if (!agentIsDrawn(a)) continue;
        const [ax, ay] = toScreen(a.x, a.y);
        ctx.strokeRect(ax, ay, AGENT_W * zoom, AGENT_H * zoom);
      }
      ctx.strokeStyle = 'rgba(60,140,255,0.9)';
      const [ppx, ppy] = toScreen(state.player.x, state.player.y);
      ctx.strokeRect(ppx, ppy, state.player.w * zoom, state.player.h * zoom);
    }
  }

  function renderRoom() {
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    const zoom = state.zoom;
    const toScreen = cameraFor(zoom, ROOM_NATIVE_W, ROOM_NATIVE_H, state.player.x, state.player.y);
    const room = ROOMS[state.location.building];

    const [bx, by] = toScreen(0, 0);
    ctx.drawImage(roomImages[room.image], bx, by, ROOM_NATIVE_W * zoom, ROOM_NATIVE_H * zoom);
    renderAgentsInRoom(ctx, toScreen, zoom, playerSprites, state.location.building);
    drawPlayer(toScreen, zoom);

    ctx.save();
    ctx.font = '14px monospace';
    ctx.fillStyle = 'rgba(0,0,0,0.5)';
    ctx.fillRect(8, 8, ctx.measureText(room.label).width + 16, 22);
    ctx.fillStyle = '#e8e8e8';
    ctx.fillText(room.label, 16, 24);
    ctx.restore();

    if (state.showFootprints) {
      ctx.fillStyle = 'rgba(255,40,40,0.4)';
      for (const r of ROOM_COLLISIONS[room.collision]) {
        const [rx, ry] = toScreen(r.x, r.y);
        ctx.fillRect(rx, ry, r.w * zoom, r.h * zoom);
      }
      ctx.strokeStyle = 'rgba(255,210,60,0.9)';
      const [ex, ey] = toScreen(ROOM_EXIT_TRIGGER.x, ROOM_EXIT_TRIGGER.y);
      ctx.strokeRect(ex, ey, ROOM_EXIT_TRIGGER.w * zoom, ROOM_EXIT_TRIGGER.h * zoom);
      ctx.strokeStyle = 'rgba(120,255,160,0.9)';
      for (const item of ROOM_INTERACTABLES[room.collision] || []) {
        const [ix, iy] = toScreen(item.zone.x, item.zone.y);
        ctx.strokeRect(ix, iy, item.zone.w * zoom, item.zone.h * zoom);
      }
      ctx.strokeStyle = 'rgba(60,140,255,0.9)';
      const [ppx, ppy] = toScreen(state.player.x, state.player.y);
      ctx.strokeRect(ppx, ppy, state.player.w * zoom, state.player.h * zoom);
    }
  }

  function frame(now) {
    const dt = Math.min(0.05, (now - last) / 1000);
    last = now;
    // Bug found and fixed: an uncaught exception
    // anywhere in update()/render() used to mean the requestAnimationFrame
    // call below never ran, freezing the ENTIRE game dead, silently, with
    // nothing visible anywhere but the browser console. That specific bug
    // (findPath() returning an empty-not-null path) is fixed at the
    // source, but this catch is the actual structural fix -- whatever
    // breaks next, the game survives it instead of dying outright.
    try {
      update(dt);
      render();
    } catch (e) {
      console.error('Frame error (game kept running):', e);
      const msg = (e && e.message) || String(e);
      const t = Date.now();
      // Throttled, not per-frame -- a recurring error at 60fps would
      // otherwise flood the real action log; still visible (at least
      // once per 30s) in the Activity Log, not swallowed silently.
      if (msg !== lastLoopErrorMsg || t - lastLoopErrorLoggedAt > 30000) {
        lastLoopErrorMsg = msg;
        lastLoopErrorLoggedAt = t;
        logThinkTankAction('system', 'frame_error', { message: msg, stack: ((e && e.stack) || '').slice(0, 500) });
      }
    }
    requestAnimationFrame(frame);
  }
}

main();
