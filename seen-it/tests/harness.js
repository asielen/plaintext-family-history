// harness.js - run background.js under node with a pretend Chrome.
//
// background.js is the add-on's service worker: it talks only to chrome.*
// APIs. This gives it in-memory storage (values copied in and out, as Chrome
// serializes them), alarms that fire only when a test says so, the message
// listeners, and a native helper that answers from a test's own function.
// Nothing here touches a real browser, the disk or the registry.
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const clone = (x) => (x === undefined ? undefined : JSON.parse(JSON.stringify(x)));

function makeArea(name, fire) {
  const data = {};
  return {
    data,
    async get(keys) {
      if (keys == null) return clone(data);
      const list = typeof keys === 'string' ? [keys] : Array.isArray(keys) ? keys : Object.keys(keys);
      const out = {};
      for (const k of list) if (k in data) out[k] = clone(data[k]);
      return out;
    },
    async set(o) {
      const changes = {};
      for (const [k, v] of Object.entries(o)) {
        if (JSON.stringify(data[k]) === JSON.stringify(v)) continue; // Chrome reports real changes only
        changes[k] = { oldValue: clone(data[k]), newValue: clone(v) };
        data[k] = clone(v);
      }
      if (Object.keys(changes).length) fire(changes, name);
    },
    async remove(keys) {
      for (const k of [].concat(keys)) delete data[k];
    },
  };
}

function load(opts = {}) {
  const listeners = { changed: [], alarm: [], installed: [], startup: [], message: [], external: [] };
  const on = (list) => ({ addListener: (f) => list.push(f) });
  const fire = (changes, area) => { for (const f of listeners.changed) f(changes, area); };
  const local = makeArea('local', fire);
  const sync = makeArea('sync', fire);
  const alarms = {};
  const alarmLog = [];
  const nativeCalls = [];
  const native = opts.native || (() => ({ ok: false, error: 'no helper in this test' }));

  const chrome = {
    storage: { local, sync, onChanged: on(listeners.changed) },
    alarms: {
      create(name, info) { alarms[name] = Object.assign({ name }, info); alarmLog.push(name); },
      async clear(name) { const had = name in alarms; delete alarms[name]; return had; },
      async get(name) { return alarms[name] ? clone(alarms[name]) : undefined; },
      onAlarm: on(listeners.alarm),
    },
    runtime: {
      onInstalled: on(listeners.installed),
      onStartup: on(listeners.startup),
      onMessage: on(listeners.message),
      onMessageExternal: on(listeners.external),
      lastError: undefined,
      sendNativeMessage(host, msg, cb) {
        nativeCalls.push(clone(msg));
        Promise.resolve().then(() => native(clone(msg))).then((r) => setImmediate(() => cb(clone(r))));
      },
    },
    history: { search: async () => [], getVisits: async () => [] },
    permissions: { request: async () => true },
  };
  // Timers the worker sets itself must not keep a test process alive.
  const unref = (t) => (t && t.unref ? t.unref() : t);
  const context = vm.createContext({
    chrome,
    console: { log() {}, warn() {}, error() {} },
    setTimeout: (...a) => unref(setTimeout(...a)),
    clearTimeout,
    setInterval: (...a) => unref(setInterval(...a)),
    clearInterval,
    crypto: require('crypto').webcrypto,
    TextEncoder,
    URL,
  });
  const file = path.join(__dirname, '..', 'background.js');
  vm.runInContext(fs.readFileSync(file, 'utf8'), context, { filename: file });

  const settle = async (rounds = 20) => {
    for (let i = 0; i < rounds; i++) await new Promise((r) => setImmediate(r));
  };
  const ask = (list, msg, sender) => new Promise((resolve) => {
    let answered = false;
    for (const f of list) {
      const keep = f(msg, sender || {}, (r) => { answered = true; resolve(clone(r)); });
      if (keep !== true && !answered) continue;
    }
    setTimeout(() => { if (!answered) resolve(undefined); }, 2000).unref();
  });
  return {
    chrome, context, local, sync, alarms, alarmLog, nativeCalls, settle,
    send: (msg) => ask(listeners.message, msg),
    sendExternal: (msg, sender) => ask(listeners.external, msg, sender || { id: 'some-other-extension' }),
    fireAlarm: async (name) => {
      delete alarms[name];
      await Promise.all(listeners.alarm.map((f) => f({ name })));
      await settle();
    },
    startup: async () => { await Promise.all(listeners.startup.map((f) => f())); await settle(); },
  };
}

module.exports = { load };
