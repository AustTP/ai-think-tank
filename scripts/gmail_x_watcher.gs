/**
 * Gmail watcher: relays X mention-notification emails to the think tank
 * webhook (/api/webhook/x-mention) and trashes the email only after the
 * server acknowledges it. Deletion is deliberately client-side -- the
 * server never deletes mail (prompt-injection safety, content.py).
 *
 * Setup (one time, in the Gmail account the think tank reads):
 *   1. script.new (Google Apps Script), paste this file.
 *   2. Fill SERVER_URL and WEBHOOK_KEY below. WEBHOOK_KEY is the
 *      WEBHOOK_API_KEY value in ~/ai-village-template/.env; the base URL
 *      is the SELF_BASE_URL value there.
 *   3. Run setupTrigger once, approve the Gmail scope.
 *   4. In X: Settings > Notifications > mentions = individual (not digest),
 *      so one email per mention arrives in this inbox.
 *
 * Retry semantics: a mention email stays in the inbox until the server
 * returns 200, so transient failures (server down, no free authority)
 * self-heal on the next 5-minute run. A permanent rejection (401/404/400)
 * is labeled xwatcher-skipped instead, so it is not retried forever.
 */

var SERVER_URL = 'http://localhost:8000'; // must be reachable from Google's cloud (e.g. a tunnel)
var WEBHOOK_KEY = 'PASTE_WEBHOOK_API_KEY_HERE';

var SEARCH_QUERY = 'from:x.com OR from:twitter.com subject:"mentioned you" -in:trash -in:spam';

function setupTrigger() {
  ScriptApp.getProjectTriggers().forEach(function (t) {
    if (t.getHandlerFunction() === 'checkMentionEmails') ScriptApp.deleteTrigger(t);
  });
  ScriptApp.newTrigger('checkMentionEmails').timeBased().everyMinutes(5).create();
}

function checkMentionEmails() {
  var threads = GmailApp.search(SEARCH_QUERY, 0, 10);
  for (var i = 0; i < threads.length; i++) {
    processThread(threads[i]);
  }
}

function processThread(thread) {
  var msgs = thread.getMessages();
  var last = msgs[msgs.length - 1]; // newest mention in the thread is the trigger
  var body = last.getBody();
  var match = body.match(/https:\/\/(?:x|twitter)\.com\/([A-Za-z0-9_]{1,20})\/status(?:es)?\/(\d+)/);
  if (!match) {
    markSkipped(thread);
    return;
  }
  var payload = {
    id: match[2],
    url: 'https://x.com/' + match[1] + '/status/' + match[2],
    author: match[1],
    subject: last.getSubject(),
    from: last.getFrom(),
    threadId: thread.getId(),
    emailDate: last.getDate().toISOString()
  };
  var res;
  try {
    res = UrlFetchApp.fetch(SERVER_URL + '/api/webhook/x-mention', {
      method: 'post',
      contentType: 'application/json',
      headers: { 'X-Webhook-Key': WEBHOOK_KEY },
      payload: JSON.stringify(payload),
      muteHttpExceptions: true
    });
  } catch (err) {
    console.error('relay failed, leaving email for retry: ' + err);
    return;
  }
  var code = res.getResponseCode();
  if (code === 200) {
    thread.moveToTrash();
  } else if (code >= 400 && code < 500) {
    console.error('server rejected mention ' + payload.id + ' with ' + code + ': ' + res.getContentText());
    markSkipped(thread);
  } else {
    console.error('server not ready (' + code + '), leaving email for retry');
  }
}

function markSkipped(thread) {
  var label = GmailApp.getUserLabelByName('xwatcher-skipped') || GmailApp.createLabel('xwatcher-skipped');
  thread.addLabel(label);
}
