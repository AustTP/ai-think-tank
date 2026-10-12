// WeatherStation boids configuration -- browser-safe (no ESM exports).
// Attaches the boids sim config to the global WeatherStation registry so the
// plain <script> in index.html works without module loading.
(function () {
  'use strict';

  var DEFAULT_PARAMS = {
    count: 60,
    width: 400,
    height: 300,
    maxSpeed: 3,
    visualRange: 40,
    separationDistance: 20,
    separationWeight: 0.05,
    alignmentWeight: 0.05,
    cohesionWeight: 0.005
  };

  function createBoidsSim(params) {
    var p = Object.assign({}, DEFAULT_PARAMS, params || {});
    var boids = [];
    for (var i = 0; i < p.count; i++) {
      boids.push({
        x: Math.random() * p.width,
        y: Math.random() * p.height,
        vx: (Math.random() - 0.5) * p.maxSpeed,
        vy: (Math.random() - 0.5) * p.maxSpeed
      });
    }

    function step() {
      for (var i = 0; i < boids.length; i++) {
        var b = boids[i];
        var cx = 0, cy = 0, ax = 0, ay = 0, sx = 0, sy = 0, n = 0;
        for (var j = 0; j < boids.length; j++) {
          if (i === j) continue;
          var o = boids[j];
          var dx = o.x - b.x, dy = o.y - b.y;
          var d2 = dx * dx + dy * dy;
          if (d2 > p.visualRange * p.visualRange) continue;
          n++;
          cx += o.x; cy += o.y;
          ax += o.vx; ay += o.vy;
          if (d2 < p.separationDistance * p.separationDistance && d2 > 0) {
            sx -= dx; sy -= dy;
          }
        }
        if (n > 0) {
          b.vx += (cx / n - b.x) * p.cohesionWeight + (ax / n - b.vx) * p.alignmentWeight + sx * p.separationWeight;
          b.vy += (cy / n - b.y) * p.cohesionWeight + (ay / n - b.vy) * p.alignmentWeight + sy * p.separationWeight;
        }
        var sp = Math.sqrt(b.vx * b.vx + b.vy * b.vy) || 1;
        if (sp > p.maxSpeed) {
          b.vx = (b.vx / sp) * p.maxSpeed;
          b.vy = (b.vy / sp) * p.maxSpeed;
        }
        b.x = (b.x + b.vx + p.width) % p.width;
        b.y = (b.y + b.vy + p.height) % p.height;
      }
      return boids;
    }

    return {
      name: 'boids',
      params: p,
      getBoids: function () { return boids; },
      step: step
    };
  }

  var config = {
    name: 'boids',
    defaultParams: DEFAULT_PARAMS,
    createBoidsSim: createBoidsSim
  };

  globalThis.WeatherStationBoidsConfig = config;

  // Best-effort registration into the live WeatherStation hub.
  try {
    if (typeof window !== 'undefined' &&
        window.WeatherStation &&
        typeof window.WeatherStation.record === 'function') {
      var sim = createBoidsSim();
      window.WeatherStation.record(sim.name, sim);
    }
  } catch (err) {
    // Registration is optional; the config global is always available.
  }
})();
