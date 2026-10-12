/* WeatherStation: browser-side recorder for the simulation lab harness.
   Loaded as a classic script (no ESM exports) so it can never trip the
   "Unexpected token 'export'" page error that plain-<script> ESM files cause. */
(function () {
  'use strict';

  var MAX_HISTORY = 600;
  var series = {};   // name -> array of stat snapshots
  var listeners = [];

  function record(name, stats) {
    if (typeof name !== 'string' || name.length === 0) return;
    if (!stats || typeof stats !== 'object') return;
    if (!series[name]) series[name] = [];
    var snap = Object.assign({ t: Date.now() }, stats);
    series[name].push(snap);
    if (series[name].length > MAX_HISTORY) series[name].shift();
    for (var i = 0; i < listeners.length; i++) {
      try { listeners[i](name, snap); } catch (e) { /* listener errors ignored */ }
    }
  }

  function history(name) {
    return (series[name] || []).slice();
  }

  function latest(name) {
    var h = series[name];
    return h && h.length ? h[h.length - 1] : null;
  }

  function names() {
    return Object.keys(series);
  }

  function onChange(fn) {
    if (typeof fn === 'function') listeners.push(fn);
  }

  function reset() {
    series = {};
    listeners = [];
  }

  var WeatherStation = {
    record: record,
    history: history,
    latest: latest,
    names: names,
    onChange: onChange,
    reset: reset
  };

  globalThis.WeatherStation = WeatherStation;
})();
