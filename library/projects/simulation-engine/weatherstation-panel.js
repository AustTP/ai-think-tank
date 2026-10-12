/* Browser panel: renders a live WeatherStation metrics readout + sparkline
   into the harness page. No dependencies beyond weatherstation-global.js. */
(function () {
  'use strict';

  function initPanel() {
    var host = document.getElementById('ws-panel');
    if (!host) return;
    var label = document.createElement('div');
    label.style.font = '12px monospace';
    label.style.whiteSpace = 'pre';
    var canvas = document.createElement('canvas');
    canvas.id = 'ws-spark';
    canvas.width = 400;
    canvas.height = 60;
    canvas.style.border = '1px solid #999';
    host.appendChild(canvas);
    host.appendChild(label);

    var ctx = canvas.getContext('2d');

    function draw() {
      ctx.clearRect(0, 0, canvas.width, canvas.height);
      var name = 'predatorprey';
      var h = globalThis.WeatherStation
        ? globalThis.WeatherStation.history(name) : [];
      var lines = [name + ': ' + h.length + ' samples'];
      if (h.length) {
        var latest = h[h.length - 1];
        Object.keys(latest).forEach(function (k) {
          if (k !== 't' && typeof latest[k] === 'number') {
            lines.push(k + '=' + latest[k].toFixed(2));
          }
        });
        var key = Object.keys(latest).find(function (k) {
          return k !== 't' && typeof latest[k] === 'number';
        });
        if (key) {
          var vals = h.map(function (s) { return s[key]; });
          var max = Math.max.apply(null, vals) || 1;
          ctx.strokeStyle = '#2a7';
          ctx.beginPath();
          for (var i = 0; i < vals.length; i++) {
            var x = (i / Math.max(vals.length - 1, 1)) * canvas.width;
            var y = canvas.height - (vals[i] / max) * (canvas.height - 4) - 2;
            if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
          }
          ctx.stroke();
        }
      } else {
        lines.push('waiting for samples...');
      }
      label.textContent = lines.join('\n');
    }

    if (globalThis.WeatherStation) {
      globalThis.WeatherStation.onChange(function () { draw(); });
    }
    draw();
    setInterval(draw, 500);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initPanel);
  } else {
    initPanel();
  }
})();
