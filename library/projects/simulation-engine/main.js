// Entry point: wires the engines to the page when a canvas is present.
import { BoidSystem, DEFAULT_PARAMS } from './boids.js';
import { WeatherStation } from './weatherstation.js';

function startBoids(canvas) {
  const ctx = canvas.getContext('2d');
  const system = new BoidSystem({ ...DEFAULT_PARAMS });
  function frame() {
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    for (const b of system.step()) {
      ctx.beginPath();
      ctx.arc(b.pos.x, b.pos.y, 2, 0, Math.PI * 2);
      ctx.fill();
    }
    requestAnimationFrame(frame);
  }
  frame();
}

const canvas = document.getElementById('boids-canvas');
if (canvas) startBoids(canvas);

// Expose engines for the Node smoke test / debugging.
if (typeof globalThis !== 'undefined') {
  globalThis.BuildLab = { BoidSystem, WeatherStation };
}
