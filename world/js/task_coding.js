// Sandbox context, code writing, and the graded review loop.
// Split out of tasks.js to keep every script under 1,000 lines; classic
// script scope means these globals are shared exactly as before.

// Real code-writing capability, built for the finger-drumming project --
// an agent generates ACTUAL file content via a real chat call (model tier
// picked by Jev per pickModelTierForAction(), world.js -- coding is
// exactly the "reserve for real work" case that function exists for),
// then writes it into the shared sandbox through the exact same heredoc-
// via-/api/execute path validated live before any of this orchestration
// was built: a plain shell command, classified by Jev like any other
// (nothing new needed server-side -- the sandbox already lets an agent
// write/read files inside its own workspace).
// General, reusable sandbox-context reader -- reading "what real files
// already exist in this sandbox" isn't specific to any one project. Real
// bug this already fixed, worth keeping general: a naive
// concatenate-everything command can hit serve.py's own SANDBOX_MAX_OUTPUT
// cap (20,000 bytes) mid-file -- checking each file's size with `wc -c`
// before including it means the shell itself never emits enough to hit
// that ceiling, and an oversized file is skipped WHOLE with a clear note,
// never truncated mid-content.
async function getSandboxContext(agentId, sandboxId) {
  try {
    const res = await agentFetch('/api/execute', agentId, {
      method: 'POST',
      body: JSON.stringify({
        agentId,
        command: "budget=15000; total=0; for f in *.html *.js *.css *.py *.md; do [ -f \"$f\" ] || continue; sz=$(wc -c < \"$f\"); if [ $((total + sz)) -gt $budget ]; then echo \"--- $f --- (skipped, over context budget)\"; continue; fi; echo \"--- $f ---\"; cat \"$f\"; total=$((total + sz)); done",
        purpose: 'Reading current sandbox files for context before the next step.',
        sandboxId,
      }),
    });
    const data = await res.json();
    if (!data.allowed || !data.stdout) return '(nothing written yet)';
    return data.stdout;
  } catch (e) {
    return '(could not read current sandbox state)';
  }
}

function _heredocBalance(command) {
  const opens = (command.match(/<<-?\s*'?EOF'?/g) || []).length;
  const closes = command.split('\n').filter(line => line.trim() === 'EOF').length;
  return { opens, closes, balanced: opens === 0 || opens === closes };
}

// Real fix for a real, corroborated complaint -- three of five agent
// retrospectives independently named being "forced into awkward chunks"
// as a frustration (they blamed the model tier; the actual cause is this
// max_tokens ceiling, confirmed: a real generation cut
// off mid-heredoc, executed anyway, produced a genuinely broken file).
// The old fix (reject and give up) was safe but still left the agent's
// work half-finished. This is the real fix: when a generation looks
// truncated, ask the SAME model to continue exactly where it left off,
// bounded to a couple of attempts so this can't loop forever on a
// model that just can't finish the job.
const CODE_CONTINUATION_ATTEMPTS = 2;

// Real fix for the single biggest thing found wrong with this whole
// system, not by reading code but by running it for eight straight
// rounds: the core loop wasn't agentic. Every fact a fix ever needed
// (which globals exist, what a button's real parent is, what a boolean
// guard flag's actual value is) had to be pre-guessed and stuffed into
// context by a human, one missing fact at a time, because the model
// itself had no way to just go check. This lets the model ask FOR ITSELF,
// mid-generation, instead of guessing -- bounded so a confused model
// can't loop forever racking up real page-probe + chat calls.
const MAX_CODE_PROBE_ROUNDS = 3;

// A probe request looks like {"probeRequest": {"path", "actions", "probes"}}
// and nothing else -- deliberately narrow so an ordinary shell command
// (which never parses as JSON, let alone JSON shaped exactly like this)
// is never mistaken for one. Returns null for anything that isn't
// unambiguously a probe request, which just falls through to the normal
// shell-command path unchanged.
function _parseProbeRequest(reply) {
  let parsed;
  try {
    parsed = JSON.parse(reply.trim().replace(/^```(?:json)?\s*|\s*```\s*$/g, ''));
  } catch (e) {
    return null;
  }
  const req = parsed && parsed.probeRequest;
  if (!req || typeof req !== 'object') return null;
  if (!Array.isArray(req.actions) && !Array.isArray(req.probes)) return null;
  return { path: req.path || 'index.html', actions: req.actions || [], probes: req.probes || [] };
}

// `projectLabel` makes the actual thing being built a real parameter
// rather than a baked-in assumption, so this coding pipeline (probe-before-
// writing, heredoc-continuation handling, orphaned/phantom-file detection)
// serves any project, not just one hardcoded default.
async function runCodingTask(agentId, sandboxId, backlogItem, contextSummary, projectLabel) {
  const a = AGENTS[agentId];
  if (!a) return { ok: false, note: 'agent missing' };
  const tier = await pickModelTierForAction(a, `write real code for: ${backlogItem}`);
  let systemPrompt = `You are ${a.name}, a developer on a small team building ${projectLabel || 'a real, working small web application for the team'}. `
    + `Current project state:\n${contextSummary || '(nothing written yet -- you may be starting the first file.)'}\n\n`
    + `Your task right now: ${backlogItem}\n\n`
    + `Before writing your final answer, you may check real facts about how the ACTUAL running page behaves right now -- what globals it defines and their real shape, what a button click or keypress actually does -- instead of guessing a plausible-sounding name. `
    + `To do this, respond with ONLY a JSON object, no shell command, no markdown fences, no explanation, in exactly this shape: `
    + `{"probeRequest": {"path": "index.html", "actions": [{"type":"click","selector":"text=Play Challenge"}, {"type":"keydown","key":"q"}], "probes": ["document.body.className", "typeof window.SomeGlobal"]}}\n`
    + `Action types are click ({selector}), keydown ({key}), wait ({ms}), eval ({code}). You can do this up to ${MAX_CODE_PROBE_ROUNDS} times if you genuinely need to. `
    + `When ready to write the actual fix, respond with ONLY a single shell command, no explanation, no markdown fences, that writes or updates the necessary file(s) using one or more heredocs, e.g.:\n`
    + `cat > index.html << 'EOF'\n<contents>\nEOF\n`
    + `Write real, complete, working code for this specific piece -- no placeholders, no "TODO," no stubs. Keep it focused on just this task, building on what already exists rather than starting over. `
    + `If an existing file has grown large, PREFER adding a new small file (e.g. a separate .js file, linked with its own <script src> tag) over rewriting the whole large file -- a full rewrite risks running out of room mid-file and being cut off incomplete, which a small new file avoids.`;
  systemPrompt = prependWorkingGuide(systemPrompt, await readWorkingGuide());

  const messages = [{ role: 'system', content: systemPrompt }, { role: 'user', content: 'Go ahead.' }];
  let command = '';
  let attempts = 0;
  let probeRounds = 0;
  while (attempts <= CODE_CONTINUATION_ATTEMPTS) {
    let reply;
    try {
      const res = await agentFetch('/api/chat', agentId, {
        method: 'POST',
        body: JSON.stringify({ model: tier.slug, messages, max_tokens: 3500, agentId }),
      });
      const data = await res.json();
      if (!res.ok || data.error || !data.reply) return { ok: false, note: 'model call failed or returned nothing', tier: tier.label };
      reply = data.reply;
    } catch (e) {
      return { ok: false, note: 'model call failed: ' + e.message, tier: tier.label };
    }

    // Real agentic step, not a pre-guessed fact stuffed into context up
    // front: the model decides for itself, this round, whether it needs
    // to check something real before answering.
    const probeReq = probeRounds < MAX_CODE_PROBE_ROUNDS ? _parseProbeRequest(reply) : null;
    if (probeReq) {
      probeRounds++;
      messages.push({ role: 'assistant', content: reply });
      const probeData = await requestPageProbe(agentId, sandboxId, probeReq.path, probeReq.actions, probeReq.probes);
      let feedback = formatPageProbeResult(probeData);
      if (probeRounds >= MAX_CODE_PROBE_ROUNDS) {
        feedback += `\n\nYou have used all ${MAX_CODE_PROBE_ROUNDS} probe rounds. Respond now with ONLY your final shell command.`;
      }
      messages.push({ role: 'user', content: feedback });
      continue; // does not count against CODE_CONTINUATION_ATTEMPTS -- that guards against truncation, a different failure mode entirely
    }

    // Only the FIRST real command reply can have a fenced-code wrapper
    // worth stripping -- a continuation is raw mid-file text by
    // definition, and stripping ``` from the middle of a continued
    // heredoc body would corrupt real file content that happens to
    // contain a code fence.
    command += attempts === 0 ? reply.trim().replace(/^```(?:bash|sh)?\s*|\s*```\s*$/g, '') : reply;
    if (!command) return { ok: false, note: 'model returned an empty command', tier: tier.label };

    const balance = _heredocBalance(command);
    if (balance.balanced) break;
    attempts++;
    if (attempts > CODE_CONTINUATION_ATTEMPTS) {
      return { ok: false, note: `generation still looked truncated after ${CODE_CONTINUATION_ATTEMPTS} continuation attempt(s) (${balance.opens} heredoc(s) opened, ${balance.closes} closed) -- not executed`, tier: tier.label, command };
    }
    messages.push({ role: 'assistant', content: command });
    messages.push({ role: 'user', content: 'You were cut off before finishing. Continue EXACTLY where you left off -- do not repeat anything you already wrote, do not restart the heredoc or add a new one, just output the rest of the raw file content and the closing EOF line(s).' });
  }

  let result;
  try {
    const res = await agentFetch('/api/execute', agentId, {
      method: 'POST',
      body: JSON.stringify({ agentId, command, purpose: `Coding task: ${backlogItem}`, sandboxId }),
    });
    const data = await res.json();
    if (!data.allowed) return { ok: false, note: `blocked: ${data.reason || 'no reason given'}`, command, tier: tier.label };
    result = { ok: data.exitCode === 0 && !data.timedOut, exitCode: data.exitCode, stdout: data.stdout, stderr: data.stderr, command, tier: tier.label };
  } catch (e) {
    return { ok: false, note: 'execute failed: ' + e.message, command, tier: tier.label };
  }

  // Mistake class: a fix wrote a genuinely correct new
  // .js file, using
  // real property names verified against the actual running page, and it
  // had zero effect anyway -- because it was never referenced by a
  // <script src> tag in index.html, so the browser never loaded it. Not a
  // wrong guess this time; an orphaned file. A page-probe checks what's
  // ACTUALLY loaded, so it can't catch "you forgot to link this" -- that
  // needs a direct, mechanical check of the file the model just wrote.
  if (result.ok) {
    const writtenJsFiles = _extractWrittenJsFiles(command);
    if (writtenJsFiles.length > 0) {
      const unlinked = await _findUnlinkedJsFiles(agentId, sandboxId, writtenJsFiles);
      if (unlinked.length > 0) {
        const linked = await _autoLinkJsFiles(agentId, sandboxId, unlinked, messages, tier);
        result.note = linked.ok
          ? `auto-linked previously-orphaned file(s) into index.html: ${unlinked.join(', ')}`
          : `WARNING: created ${unlinked.join(', ')} but ${unlinked.length > 1 ? 'they are' : 'it is'} not referenced by a <script src> tag in index.html, and the automatic follow-up to link ${unlinked.length > 1 ? 'them' : 'it'} failed -- ${linked.note}`;
      }
    }

    // The mirror-image mistake: a rebuild
    // task, WITH the complete real file inventory already in its own
    // context (verified directly -- every real filename was present,
    // either in full or as a budget-skip notice), still fabricated three
    // plausible-sounding sibling files (keymap.js, score.js, game.js)
    // that were never written, and referenced them in <script src> tags
    // anyway. A page-probe can't catch this either -- it only sees
    // what's actually loaded; a 404 for a nonexistent script is silent.
    // This is a direct, mechanical check of index.html itself, same
    // "don't guess, check" discipline as the orphaned-file check above.
    const phantomRefs = await _findPhantomScriptRefs(agentId, sandboxId);
    if (phantomRefs.length > 0) {
      const cleaned = await _removePhantomScriptRefs(agentId, sandboxId, phantomRefs);
      const refList = phantomRefs.map(p => `${p.file} (in ${p.html})`).join(', ');
      const phantomNote = cleaned.ok
        ? `removed <script src> reference(s) to file(s) that don't actually exist: ${refList}`
        : `WARNING: HTML file(s) reference file(s) that were never written and don't exist: ${refList} -- automatic cleanup failed (${cleaned.note})`;
      result.note = result.note ? `${result.note} | ${phantomNote}` : phantomNote;
    }

    // Advisory only -- see _findDanglingSelectorRefs for why there's no
    // safe auto-fix here, unlike the two checks above.
    const dangling = await _findDanglingSelectorRefs(agentId, sandboxId);
    if (dangling.length > 0) {
      const danglingNote = `WARNING: code queries selector(s) that don't exist anywhere in the sandbox's markup and are never created dynamically either: ${dangling.map(d => `${d.selector} (in ${d.files.join(', ')})`).join('; ')} -- likely a missing element, not real gameplay yet.`;
      result.note = result.note ? `${result.note} | ${danglingNote}` : danglingNote;
    }
  }
  return result;
}

// Pulls every .js filename a heredoc-based shell command actually wrote
// (`cat > name.js` or `cat >> name.js`) -- deliberately simple pattern
// matching over the command text itself, not a filesystem diff, since the
// command is already the single source of truth for what this step
// intended to create.
function _extractWrittenJsFiles(command) {
  const files = new Set();
  const re = /cat\s*>>?\s*([A-Za-z0-9_.\-]+\.js)\b/g;
  let m;
  while ((m = re.exec(command))) files.add(m[1]);
  return [...files];
}

// A direct, mechanical grep against every real *.html file in the
// sandbox -- not a guess, not an LLM judgment call, and (per the real
// miss this closes) not just index.html either: a project can have more
// than one real page (e.g. a settings/remap screen), and a file legitimately
// linked from one of those, not index.html, used to get wrongly flagged
// as unlinked.
async function _findUnlinkedJsFiles(agentId, sandboxId, jsFiles) {
  const checkCommand = jsFiles
    .map(f => `grep -q 'src="${f}"' *.html 2>/dev/null && echo "LINKED:${f}" || echo "UNLINKED:${f}"`)
    .join(' ; ');
  try {
    const res = await agentFetch('/api/execute', agentId, {
      method: 'POST',
      body: JSON.stringify({ agentId, command: checkCommand, purpose: 'Verifying newly written script files are actually linked into index.html.', sandboxId }),
    });
    const data = await res.json();
    const stdout = data.stdout || '';
    return jsFiles.filter(f => stdout.includes(`UNLINKED:${f}`));
  } catch (e) {
    // Can't confirm either way -- treat as unlinked so the follow-up at
    // least attempts a fix rather than silently trusting an unconfirmed file.
    return jsFiles;
  }
}

// Gap found: settings.js got auto-linked into
// index.html by blind default, even though the sandbox already had a
// real settings.html it obviously belonged to instead. Linking it into
// the wrong page doesn't make it work -- it just moves where the dead
// reference lives, and now settings.js's own DOM queries are dangling on
// a page that never has those elements either. Prefer a same-named HTML
// page (settings.js -> settings.html) when one actually exists in the
// sandbox; index.html stays the fallback for anything that isn't
// obviously page-specific.
async function _guessLinkTargetHtml(agentId, sandboxId, jsFile) {
  const base = jsFile.replace(/\.js$/, '');
  if (base === 'index') return 'index.html';
  const checkCommand = `[ -f "${base}.html" ] && echo yes || echo no`;
  try {
    const res = await agentFetch('/api/execute', agentId, {
      method: 'POST',
      body: JSON.stringify({ agentId, command: checkCommand, purpose: `Checking whether ${jsFile} has a same-named HTML page to link into.`, sandboxId }),
    });
    const data = await res.json();
    return (data.stdout || '').includes('yes') ? `${base}.html` : 'index.html';
  } catch (e) {
    return 'index.html';
  }
}

// One-shot, narrowly-scoped follow-up: ask the SAME model, in the SAME
// conversation, for just the missing <script src> line(s) -- not a whole
// new coding task, since the actual file content was already correct.
// Grouped by target page (see _guessLinkTargetHtml) since not every
// unlinked file necessarily belongs on the same page.
async function _autoLinkJsFiles(agentId, sandboxId, unlinkedFiles, messages, tier) {
  const byTarget = new Map(); // html -> [files]
  for (const f of unlinkedFiles) {
    const html = await _guessLinkTargetHtml(agentId, sandboxId, f);
    if (!byTarget.has(html)) byTarget.set(html, []);
    byTarget.get(html).push(f);
  }

  let allOk = true;
  const notes = [];
  for (const [html, files] of byTarget) {
    const prompt = `You just created ${files.join(', ')} but never added a <script src="..."> tag for ${files.length > 1 ? 'them' : 'it'} in ${html}, so ${files.length > 1 ? 'they are' : 'it is'} never actually loaded by the page. `
      + `Respond with ONLY a single shell command, no explanation, that appends the missing <script src> tag(s) to ${html} -- e.g.:\ncat >> ${html} << 'EOF'\n<script src="${files[0]}"></script>\nEOF`;
    try {
      const res = await agentFetch('/api/chat', agentId, {
        method: 'POST',
        body: JSON.stringify({ model: tier.slug, messages: [...messages, { role: 'user', content: prompt }], max_tokens: 400, agentId }),
      });
      const data = await res.json();
      if (!res.ok || data.error || !data.reply) { allOk = false; notes.push(`${html}: follow-up model call failed or returned nothing`); continue; }
      const linkCommand = data.reply.trim().replace(/^```(?:bash|sh)?\s*|\s*```\s*$/g, '');
      const execRes = await agentFetch('/api/execute', agentId, {
        method: 'POST',
        body: JSON.stringify({ agentId, command: linkCommand, purpose: `Linking previously-unlinked file(s) into ${html}: ${files.join(', ')}`, sandboxId }),
      });
      const execData = await execRes.json();
      if (!execData.allowed) { allOk = false; notes.push(`${html}: blocked (${execData.reason || 'no reason given'})`); continue; }
      if (!(execData.exitCode === 0 && !execData.timedOut)) { allOk = false; notes.push(`${html}: exit code ${execData.exitCode}`); }
    } catch (e) {
      allOk = false; notes.push(`${html}: follow-up execute failed: ${e.message}`);
    }
  }
  return { ok: allOk, note: allOk ? [...byTarget.keys()].join(', ') : notes.join('; ') };
}

// The mirror check to _findUnlinkedJsFiles: which real <script src="...">
// references, in ANY *.html file in the sandbox, point at a .js file that
// doesn't actually exist. A direct, mechanical grep+existence-check
// inside the real sandbox -- not a guess, not an LLM judgment call about
// whether a name "sounds real."
//
// Real miss this closes: originally checked index.html only. A rewrite
// task wrote settings.html with <script src="settings.js">, but
// settings.js itself was never actually created -- caught only by
// directly reading the code afterward, not by this check, because it
// never looked past index.html. Every *.html file in the sandbox gets
// the same check now, not just the entry page.
async function _findPhantomScriptRefs(agentId, sandboxId) {
  const checkCommand = `for html in *.html; do [ -f "$html" ] || continue; for f in $(grep -oE 'src="[^"]+\\.js"' "$html" | sed -E 's/src="//;s/"$//' | sort -u); do [ -f "$f" ] && echo "EXISTS:$html:$f" || echo "PHANTOM:$html:$f"; done; done`;
  try {
    const res = await agentFetch('/api/execute', agentId, {
      method: 'POST',
      body: JSON.stringify({ agentId, command: checkCommand, purpose: 'Verifying every <script src> reference in every HTML file actually corresponds to a real file.', sandboxId }),
    });
    const data = await res.json();
    const stdout = data.stdout || '';
    return [...stdout.matchAll(/PHANTOM:([^:\n]+):(\S+)/g)].map(m => ({ html: m[1], file: m[2] }));
  } catch (e) {
    return []; // can't confirm -- don't guess-remove something that might be real
  }
}

// Removes phantom <script src> lines outright rather than asking the
// model to reconcile them -- the safest mechanical action available.
// Writing the referenced file for real isn't the right call either: the
// filename was fabricated, not a real, deferred piece of work, so
// "finishing" it would just be inventing content to match an invented
// name. A dangling reference to a file that will never exist is strictly
// worse (a silent 404 in a real browser) than no reference at all.
// Grouped by which HTML file each phantom reference actually came from --
// a project can have more than one real page, and cleaning the wrong one
// (or only ever index.html) would silently leave the others broken.
async function _removePhantomScriptRefs(agentId, sandboxId, phantomRefs) {
  const byHtml = new Map();
  for (const { html, file } of phantomRefs) {
    if (!byHtml.has(html)) byHtml.set(html, []);
    byHtml.get(html).push(file);
  }
  let allOk = true;
  const notes = [];
  for (const [html, files] of byHtml) {
    const excludes = files.map(f => `-e 'src="${f.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}"'`).join(' ');
    const cleanupCommand = `grep -v ${excludes} "${html}" > "${html}.__cleanup_tmp" && mv "${html}.__cleanup_tmp" "${html}"`;
    try {
      const res = await agentFetch('/api/execute', agentId, {
        method: 'POST',
        body: JSON.stringify({ agentId, command: cleanupCommand, purpose: `Removing phantom <script src> reference(s) in ${html} to nonexistent file(s): ${files.join(', ')}`, sandboxId }),
      });
      const data = await res.json();
      if (!data.allowed) { allOk = false; notes.push(`${html}: blocked (${data.reason || 'no reason given'})`); continue; }
      if (!(data.exitCode === 0 && !data.timedOut)) { allOk = false; notes.push(`${html}: exit code ${data.exitCode}`); }
    } catch (e) {
      allOk = false; notes.push(`${html}: cleanup execute failed: ${e.message}`);
    }
  }
  return { ok: allOk, note: notes.length ? notes.join('; ') : 'removed' };
}

// A different class of dangling reference than the phantom-script check
// above: not "this file doesn't exist" but "this JS queries a class/id
// that nothing in the sandbox's markup ever creates, and nothing in the
// JS creates dynamically either." Real miss this closes: the finger-
// drums rewrite's app.js built and positioned falling-note elements
// against `.highway`/`.hit-line`, and styles.css had real CSS rules for
// both -- but no HTML file ever actually placed those elements in the
// page. Three individually-plausible files, never cross-checked against
// each other. Deliberately advisory, not auto-fixed like the phantom-
// script check: there's no single safe mechanical fix here (the right
// answer might be "add the missing element" or "this JS is dead, remove
// it," which needs real judgment), so this only surfaces the finding.
// Static and heuristic -- only simple `.class`/`#id`/getElementById
// forms are checked, and only flagged when the selector appears NOWHERE
// at all, neither in markup nor created dynamically, which is
// unambiguous regardless of when or how the page actually runs.
async function _findDanglingSelectorRefs(agentId, sandboxId) {
  const readCommand = `for f in *.html *.js; do [ -f "$f" ] || continue; echo "--- $f ---"; cat "$f"; done`;
  let blob;
  try {
    const res = await agentFetch('/api/execute', agentId, {
      method: 'POST',
      body: JSON.stringify({ agentId, command: readCommand, purpose: 'Checking for JS selectors with no matching markup or dynamic creation anywhere in the sandbox.', sandboxId }),
    });
    const data = await res.json();
    if (!data.allowed || !data.stdout) return [];
    blob = data.stdout;
  } catch (e) {
    return []; // can't confirm either way -- don't flag on a read failure
  }

  const files = {};
  let current = null;
  for (const line of blob.split('\n')) {
    const m = line.match(/^--- (.+) ---$/);
    if (m) { current = m[1]; files[current] = []; continue; }
    if (current) files[current].push(line);
  }

  const htmlText = Object.entries(files).filter(([f]) => f.endsWith('.html')).map(([, lines]) => lines.join('\n')).join('\n');
  const jsEntries = Object.entries(files).filter(([f]) => f.endsWith('.js'));
  const jsText = jsEntries.map(([, lines]) => lines.join('\n')).join('\n');

  const queried = new Map(); // "kind:name" -> {name, kind, files: Set}
  const noteQuery = (name, kind, file) => {
    const key = `${kind}:${name}`;
    if (!queried.has(key)) queried.set(key, { name, kind, files: new Set() });
    queried.get(key).files.add(file);
  };
  for (const [file, lines] of jsEntries) {
    const text = lines.join('\n');
    for (const m of text.matchAll(/\.querySelector(?:All)?\(\s*['"]([.#][A-Za-z0-9_-]+)['"]/g)) {
      const raw = m[1];
      noteQuery(raw.slice(1), raw[0] === '.' ? 'class' : 'id', file);
    }
    for (const m of text.matchAll(/getElementById\(\s*['"]([A-Za-z0-9_-]+)['"]/g)) {
      noteQuery(m[1], 'id', file);
    }
  }

  const dangling = [];
  for (const { name, kind, files: fileSet } of queried.values()) {
    const attr = kind === 'class' ? 'class' : 'id';
    const inMarkup = new RegExp(`${attr}="[^"]*\\b${name}\\b[^"]*"`).test(htmlText);
    if (inMarkup) continue;
    const createdDynamically = kind === 'class'
      ? new RegExp(`classList\\.add\\([^)]*['"]${name}['"]|className\\s*=\\s*['"][^'"]*\\b${name}\\b`).test(jsText)
      : new RegExp(`\\.id\\s*=\\s*['"]${name}['"]|setAttribute\\(\\s*['"]id['"]\\s*,\\s*['"]${name}['"]`).test(jsText);
    if (createdDynamically) continue;
    dangling.push({ selector: (kind === 'class' ? '.' : '#') + name, files: [...fileSet] });
  }
  return dangling;
}

// Real, generally-delegatable review/QA pass: gatherUnifiedContext
// (sandbox-agnostic), a skeptical critique call, a real screenshot visual
// pass, and a Jev actionable/clean verdict that queues a real follow-up
// fix. Escalation queues a plain WORK_QUEUE item with the same
// goal/projectLabel -- there's no fixed "the developers" role list for an
// arbitrary delegated project, so whoever's next free under that goal
// picks it up, the same mechanism every other generalized subtask uses.
// runCodingTask (above) lets the model request a runtime probe mid-task via
// a probeRequest JSON reply, parsed by _parseProbeRequest and run through
// requestPageProbe/formatPageProbeResult -- project-agnostic. Reviews reuse
// that same mechanism so a reviewer checks what the page actually DOES, not
// just what the source says. Capped a bit tighter than
// coding's MAX_CODE_PROBE_ROUNDS (3): reviewing is diagnostic, not
// iterative building, so there's less reason for a long back-and-forth.
const MAX_REVIEW_PROBE_ROUNDS = 2;

async function runReviewTask(agentId, task, projectLabel) {
  const a = AGENTS[agentId];
  const isQA = task.taskType === 'qa';
  const backlogItem = task.instructions ? `${task.title} -- ${task.instructions}` : task.title;

  // Produce the first review once here; the graded revision loop reuses it
  // for round 1 and only re-produces (via _produceReviewText) after an
  // inline revision.
  const fullReview = await _produceReviewText(agentId, task, projectLabel, a, isQA, backlogItem, null);
  if (!fullReview) return { ok: false, note: `Tried to ${isQA ? 'QA-test' : 'review'} "${task.title}", but the model call didn't produce anything usable.` };

  await writeLibraryFile(agentId, `archive/${Date.now()}-${task.taskType}-${task.id || 'adhoc'}.md`, `# ${task.title}\n\nProject: ${projectLabel}\nBy: ${a.name} (${isQA ? 'QA' : 'review'})\n\n${fullReview}\n`, 'firsthand');

  // Phase G -- the self-improving revision loop. When this review's task
  // carries a checklist (a big-task deliverable produced under one),
  // grade the deliverable requirement-by-requirement and let a failed
  // check drive an inline revision of the SAME project, re-grading after
  // each round (the article's create -> evaluate -> revise -> evaluate
  // again), bounded by MAX_REVISION_ROUNDS. An uncertain grade, a
  // human-type requirement, or an exhausted revision budget escalates to
  // the player instead of guessing or looping forever. Reviews with NO
  // checklist (ambient, ad-hoc, or pre-checklist big tasks) keep the old
  // single actionable/clean verdict and queue a fix -- unchanged.
  const checklist = task.checklist || [];
  if (checklist.length === 0) {
    const verdict = (await requestJevChoice(
      `A ${isQA ? 'QA/playtester' : 'reviewer'} just wrote this about "${projectLabel}": "${fullReview.slice(0, 1500)}" Does this identify at least one concrete, real, fixable problem (a bug, missing feature, broken integration)? Or does it say things genuinely look solid with nothing actionable?`,
      [
        { id: 'actionable', description: 'Yes -- it names at least one real, specific problem that should be fixed.' },
        { id: 'clean', description: 'No -- it says things look solid, or only raises vague/subjective suggestions, nothing a developer needs to act on.' },
      ]
    ))?.choice;
    if (verdict === 'actionable') {
      queueWork([{
        title: `Fix issues found in ${isQA ? 'QA testing' : 'review'} of "${task.title}"`,
        room: 'pressoffice',
        instructions: `A ${isQA ? 'QA pass' : 'review'} of "${projectLabel}" found real problems -- see the Library entry just filed for "${task.title}" -- fix them. Build on the existing files.`,
        goal: projectLabel,
      }]);
    }
    return { ok: true, note: `Filed a ${isQA ? 'QA pass' : 'review'} on "${task.title}" (text + visual)${verdict === 'actionable' ? ', queued a fix' : ', nothing actionable found'}.` };
  }

  // Checklist present: run the graded revision loop over this task's own
  // revision budget. Each round grades a review, and if any requirement
  // still fails with budget left, the shared sandbox is revised and the
  // review re-run. The first review (above) seeds round 1; only later
  // rounds pay another model call. Returns {ok, note}.
  task.revisions = task.revisions || 0;
  return await runGradedReviewLoop(agentId, task, projectLabel, isQA, {
    produce: async (reviseBrief) => {
      // Fresh review AFTER an inline revision re-checks exactly the
      // requirements that previously failed. Context is re-gathered inside
      // _produceReviewText so the reviewer sees the revised state.
      const brief = reviseBrief
        ? `${backlogItem}\n\nRevision round: these requirements previously failed -- confirm they are actually fixed, and flag anything still wrong:\n${reviseBrief}`
        : backlogItem;
      return await _produceReviewText(agentId, task, projectLabel, a, isQA, brief, null);
    },
    firstReview: fullReview,
  });
}

// Produce the review/QA text for the current state of the shared sandbox
// -- the probe + screenshot + critique pass that used to live inline in
// runReviewTask. Refactored out so the graded revision loop (G3) can call
// it once per round against the freshly-revised files.
async function _produceReviewText(agentId, task, projectLabel, a, isQA, brief, context) {
  const tier = await pickModelTierForAction(a, `${isQA ? 'QA/playtest' : 'review'} real code for: ${task.title}`);
  // Context is gathered fresh on every round so the reviewer sees the
  // CURRENT (possibly just-revised) state, not a snapshot from round 1.
  if (context === null) context = await gatherUnifiedContext(agentId, WORKROOM_SANDBOX_ID, task.title);
  let systemPrompt = (isQA
    ? `You are ${a.name}, QA-testing a teammate's real work for a project: ${projectLabel}. Current code/state:\n${context}\n\nTask: ${brief}\n\n`
      + `Assess, as a real playtester/QA would, whether this is actually usable end to end for its intended purpose -- not just "a file that sounds related exists." Call out anything broken, missing, or incomplete, as specifically as you can. Be honest either way.`
    : `You are ${a.name}, reviewing a teammate's real code for a project: ${projectLabel}. Be a genuine, skeptical reviewer, not a rubber stamp. Current code:\n${context}\n\nTask: ${brief}\n\n`
      + `List real, specific problems you actually see (bugs, broken logic, missing pieces), or say plainly if it genuinely looks solid. Be honest either way.`)
    + ` Before answering, you may check real facts about how the ACTUAL running page behaves right now -- what a button click or keypress actually does -- instead of guessing from the source alone. `
    + `To do this, respond with ONLY a JSON object, no markdown fences, no explanation, in exactly this shape: `
    + `{"probeRequest": {"path": "index.html", "actions": [{"type":"click","selector":"text=Some Button"}, {"type":"keydown","key":"a"}], "probes": ["document.body.className", "typeof window.SomeGlobal"]}}\n`
    + `Action types are click ({selector}), keydown ({key}), wait ({ms}), eval ({code}). You can do this up to ${MAX_REVIEW_PROBE_ROUNDS} times if you genuinely need to. `
    + `When ready, respond with your final assessment as plain text, not JSON.`;

  const messages = [{ role: 'system', content: systemPrompt }, { role: 'user', content: 'Give your assessment.' }];
  let review = null;
  let probeRounds = 0;
  while (true) {
    let reply;
    try {
      const res = await agentFetch('/api/chat', agentId, {
        method: 'POST',
        body: JSON.stringify({ model: tier.slug, messages, max_tokens: 900, agentId }),
      });
      const data = await res.json();
      if (!res.ok || data.error || !data.reply) break;
      reply = data.reply.trim();
    } catch (e) { break; }

    const probeReq = probeRounds < MAX_REVIEW_PROBE_ROUNDS ? _parseProbeRequest(reply) : null;
    if (probeReq) {
      probeRounds++;
      messages.push({ role: 'assistant', content: reply });
      const probeData = await requestPageProbe(agentId, WORKROOM_SANDBOX_ID, probeReq.path, probeReq.actions, probeReq.probes);
      let feedback = formatPageProbeResult(probeData);
      if (probeRounds >= MAX_REVIEW_PROBE_ROUNDS) {
        feedback += `\n\nYou have used all ${MAX_REVIEW_PROBE_ROUNDS} probe rounds. Respond now with your final assessment as plain text.`;
      }
      messages.push({ role: 'user', content: feedback });
      continue;
    }
    review = reply;
    break;
  }
  if (!review) return null;

  const visual = await reviewScreenshot(
    agentId, WORKROOM_SANDBOX_ID, 'index.html',
    `You're reviewing a real screenshot of this project's main page. Task: ${brief}. Describe what you actually see, and call out anything that looks visually broken -- elements in the wrong place, overlapping, cut off, or missing.`
  );
  return visual.ok
    ? `${review}\n\n## Visual check (real screenshot)\n\n${visual.review}`
    : `${review}\n\n## Visual check\n\n(could not complete: ${visual.note})`;
}

// Post a review requirement the loop can't resolve back to the player.
// Reuses serve.py's escalation email + resolve-link channel (the same one
// every sandbox/execution gate uses to surface a risky decision), so an
// uncertain or budget-exhausted requirement reaches the human as an
// actionable "here's what needs you" rather than being silently dropped
// or auto-decided.
async function escalateReviewRequirement(agentId, kind, question) {
  try {
    await agentFetch('/api/review/escalate', agentId, {
      method: 'POST',
      body: JSON.stringify({ agentId, kind, question }),
    });
  } catch (e) { /* email channel is best-effort -- never let escalation failure crash the review */ }
}

// Phase G -- the "working guide": the think tank's durable lessons file that
// every task reads at start so each correction is useful to the NEXT piece
// (the article's accumulated-lessons memory). Read by all agents; written
// only by directors/admin -- serve.py enforces that ACL server-side for the
// working-guide.md path specifically (see write_library_file), so a lay
// agent can never accidentally rewrite shared work-guiding knowledge.
// A missing or unreadable guide is not an error -- it simply contributes
// nothing to the prompt.
async function readWorkingGuide() {
  try {
    const content = await readLibraryFile('working-guide.md');
    return content ? content.trim() : null;
  } catch (e) { return null; }
}

// Prepend the working guide (when one exists) into a task-start system
// prompt. Callers build their prompt first, then wrap: the guide is placed
// ABOVE the task so the agent reads it as durable standing guidance.
function prependWorkingGuide(prompt, guide) {
  if (!guide) return prompt;
  return `You carry the think tank's working guide -- hard-won lessons from past work. Follow them in ADDITION to your current task:\n\n${guide}\n\n----\n\n${prompt}`;
}

// Director/admin-led helper to append a single, GENERAL lesson to the
// working guide (e.g. one distilled from a player rejection or a revision
// loop that kept failing). "General" is a discipline, enforced by whoever
// calls this -- this is the article's "separate general rules from changes
// specific to this piece." The write is fire-and-forget like every library
// write; serve.py rejects it silently if the caller doesn't hold director
// tier. Appending (never clobbering) keeps prior lessons intact.
async function appendWorkingGuide(agentId, lesson) {
  const guide = await readWorkingGuide();
  const block = guide
    ? `${guide}\n\n## Lesson\n\n${lesson}`
    : `# Working guide\n\nEvery task reads this before it starts. Each lesson is a GENERAL rule distilled from real past work -- not a per-piece fix.\n\n## Lesson\n\n${lesson}`;
  await writeLibraryFile(agentId, 'working-guide.md', block);
}

// The bounded graded revision loop (Phase G). Grades a task's checklist
// against the produced review, and when a requirement fails with revision
// budget left, revises the shared sandbox and re-reviews. Surfaces
// human-type and uncertain requirements to the player; stops auto-revising
// once MAX_REVISION_ROUNDS is exhausted. `produce` is injected so tests
// can drive the loop without a live browser -- grading.js keeps the pure
// per-requirement logic, this keeps the orchestration.
async function runGradedReviewLoop(agentId, task, projectLabel, isQA, hooks) {
  const checklist = task.checklist || [];
  let revisionBrief = null;
  let round = 0;
  let escalated = [];

  while (true) {
    round++;
    // Round 1 reuses the review runReviewTask already produced (no extra
    // model call). Only later rounds -- after an inline revision -- pay a
    // fresh review of the revised state.
    const review = round === 1 ? hooks.firstReview : await hooks.produce(revisionBrief);
    if (!review) return { ok: false, note: `Tried to ${isQA ? 'QA-test' : 'review'} "${task.title}", but the model call didn't produce anything usable.` };

    // Grade every checklist requirement in one pass.
    const jevGrades = await gradeJevRequirements(checklist, review, agentId);
    const codeGrades = [];
    for (const req of checklist) {
      if (req.type === 'code') codeGrades.push(gradeCodeRequirement(req, review));
    }
    const grades = jevGrades.concat(codeGrades);

    // Partition: what must reach the player vs. what triggers a revision.
    const failing = grades.filter(g => g.verdict === GRADE_FAILS);
    const unsure = grades.filter(g => g.verdict === GRADE_UNSURE);
    const humanReqs = checklist.filter(r => r.type === 'human');

    if (failing.length === 0 && unsure.length === 0 && humanReqs.length === 0) {
      return { ok: true, note: `${isQA ? 'QA pass' : 'review'} on "${task.title}" -- all ${checklist.length} checklist requirements met in round ${round}.`, grades, escalated };
    }

    // If we can still revise AND there are concrete failures to fix, revise.
    if (failing.length > 0 && task.revisions < MAX_REVISION_ROUNDS) {
      const reqText = failing.map(f => `- ${f.section}: ${checklist.find(r => r.id === f.requirementId)?.question || f.requirementId}`).join('\n');
      await writeLibraryFile(agentId, `archive/${Date.now()}-revision-${task.id || 'adhoc'}.md`,
        `# Revision round ${task.revisions + 1} -- ${task.title}\n\nProject: ${projectLabel}\n\nFailed requirements:\n${reqText}\n`, 'firsthand');
      let ctxSummary = '';
      try {
        const lsRes = await agentFetch('/api/execute', agentId, {
          method: 'POST',
          body: JSON.stringify({ agentId, sandboxId: WORKROOM_SANDBOX_ID, command: 'ls -la' }),
        });
        const lsData = await lsRes.json();
        if (lsData.allowed && lsData.stdout) ctxSummary = `Current files in the shared Work Room sandbox:\n${lsData.stdout}`;
      } catch (e) { /* fallbacks below cover an empty summary */ }
      const reviseBrief = `A ${isQA ? 'QA pass' : 'review'} of "${projectLabel}" found these requirements failing. Fix EACH of them, building on the existing files (do not start over or invent capabilities to pass the check):\n${reqText}`;
      await runCodingTask(agentId, WORKROOM_SANDBOX_ID, reviseBrief, ctxSummary, projectLabel);
      task.revisions++;
      revisionBrief = `Requirements that still need attention (re-review the fixed work):\n${reqText}`;
      // Loop back to produce() a fresh review against the revised files and re-grade.
      continue;
    }

    // Not revisable (no concrete failure, or budget exhausted): surface the
    // still-open requirements to the player and stop looping.
    await writeLibraryFile(agentId, `archive/${Date.now()}-${task.taskType}-${task.id || 'adhoc'}.md`,
      `# ${isQA ? 'QA pass' : 'review'} on "${task.title}" (final)\n\nProject: ${projectLabel}\n\n${review}\n`, 'firsthand');
    for (const g of unsure) {
      escalated.push(g.requirementId);
      await escalateReviewRequirement(agentId, 'uncertain review requirement',
        `Could not confidently judge requirement "${checklist.find(r => r.id === g.requirementId)?.question || g.requirementId}" (confidence ${g.confidence !== null ? g.confidence.toFixed(2) : 'n/a'}). Resolve it: ${g.section}`);
    }
    for (const r of humanReqs) {
      escalated.push(r.id);
      await escalateReviewRequirement(agentId, 'review decision for you', `${r.question} (${r.section})`);
    }
    const budgetNote = task.revisions >= MAX_REVISION_ROUNDS && failing.length > 0
      ? ` -- reached the ${MAX_REVISION_ROUNDS}-round revision cap, ${failing.length} requirement(s) still failing`
      : '';
    return { ok: true, note: `${isQA ? 'QA pass' : 'review'} on "${task.title}" finished after ${task.revisions} revision(s)${budgetNote}${escalated.length ? '; escalated to the player: ' + escalated.join(', ') : ''}.`, grades, escalated };
  }
}
