import vm from 'node:vm';
import fs from 'node:fs';
import assert from 'node:assert/strict';

const context = vm.createContext({console, Date, Math, JSON, Object, Array, Set, Promise});
vm.runInContext(`const REPORTS = []; let nextReportId = 1;
  const WORK_QUEUE = []; const RESEARCH_TOPICS = []; const TASKS = {};
  let AGENT_KEYS = {};`, context);
vm.runInContext(fs.readFileSync(new URL('../agents.js', import.meta.url), 'utf8'), context);
vm.runInContext(fs.readFileSync(new URL('../sim_bridge.js', import.meta.url), 'utf8'), context);
context.saved = {sim: {owner: 'server'}, agentRoster: [{id: 'ada'}], agents: {
  ada: {id: 'ada', x: 20, y: 40, busy: true, task: 't1', inRoom: 'observatory',
        visible: false, mailbox: [], profile: {notes: ['real work']}}},
  tasks: {t1: {id: 't1', status: 'working'}}, workQueue: [], researchTopics: [], reports: []};
context.apiFetch = async () => ({ok: true, json: async () => context.saved});
await vm.runInContext('initAgents()', context);
assert.equal(vm.runInContext('AGENTS.ada.busy', context), true);
assert.equal(vm.runInContext('AGENTS.ada.task', context), 't1');
assert.equal(vm.runInContext('AGENTS.ada.visible', context), false);
assert.equal(vm.runInContext('TASKS.t1.status', context), 'working');
context.saved = {...context.saved, agentRoster: [{id: 'leo'}], agents: {
  leo: {id: 'leo', x: 10, y: 20, busy: false, profile: {notes: []}}},
  tasks: {t2: {id: 't2', status: 'done', note: 'Actual result'}},
  reports: [{id: 1}], nextReportId: 2, agentKeys: {leo: 'test-key'}};
vm.runInContext("SIM_STATUS = {owner: 'server'}", context);
await vm.runInContext('refreshServerState()', context);
assert.equal(vm.runInContext('AGENTS.ada', context), undefined);
assert.equal(vm.runInContext('AGENT_ROSTER[0].id', context), 'leo');
assert.equal(vm.runInContext('TASKS.t1', context), undefined);
assert.equal(vm.runInContext('TASKS.t2.note', context), 'Actual result');
assert.equal(vm.runInContext('REPORTS.length', context), 1);
assert.equal(vm.runInContext('AGENT_KEYS.leo', context), 'test-key');
context.apiFetch = async () => {throw new Error('offline');};
await assert.rejects(vm.runInContext('refreshServerState()', context), /offline/);
assert.equal(vm.runInContext('serverStateRefreshing', context), false);
assert.equal(vm.runInContext('TASKS.t2.note', context), 'Actual result');
console.log('Live roster, task results, reports, reload, and offline recovery checks passed.');
