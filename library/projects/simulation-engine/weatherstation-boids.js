// Adapts the boids flocking simulation to the weather-station interface.
import { createFlock } from './boids.js';

export function createBoidsStation(options = {}) {
  const flock = createFlock(options);
  let steps = 0;
  return {
    name: 'boids',
    step() {
      flock.step();
      steps++;
    },
    state() {
      const boids = flock.boids();
      if (boids.length === 0) return { steps, count: 0, meanSpeed: 0 };
      const meanSpeed = boids.reduce((s, b) => s + Math.hypot(b.vx, b.vy), 0) / boids.length;
      return { steps, count: boids.length, meanSpeed };
    },
    params: flock.params,
  };
}

export default createBoidsStation;
