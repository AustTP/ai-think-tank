// Boids flocking simulation with configurable parameters.
export function createFlock(config = {}) {
  const width = config.width ?? 100;
  const height = config.height ?? 100;
  const count = Math.max(0, config.count ?? 20);
  const separation = config.separation ?? 1.0;
  const alignment = config.alignment ?? 1.0;
  const cohesion = config.cohesion ?? 1.0;
  const vision = config.vision ?? 10;
  const maxSpeed = config.maxSpeed ?? 2;
  const rng = config.rng ?? Math.random;

  const boids = [];
  for (let i = 0; i < count; i++) {
    boids.push({
      x: rng() * width,
      y: rng() * height,
      vx: (rng() * 2 - 1) * maxSpeed,
      vy: (rng() * 2 - 1) * maxSpeed,
    });
  }

  function step() {
    for (const b of boids) {
      let sepX = 0, sepY = 0, aliX = 0, aliY = 0, cohX = 0, cohY = 0, neighbors = 0;
      for (const o of boids) {
        if (o === b) continue;
        const dx = o.x - b.x;
        const dy = o.y - b.y;
        const dist = Math.hypot(dx, dy);
        if (dist > vision || dist === 0) continue;
        neighbors++;
        sepX -= dx / dist;
        sepY -= dy / dist;
        aliX += o.vx;
        aliY += o.vy;
        cohX += dx;
        cohY += dy;
      }
      if (neighbors > 0) {
        b.vx += separation * sepX + alignment * (aliX / neighbors - b.vx)
          + cohesion * (cohX / neighbors);
        b.vy += separation * sepY + alignment * (aliY / neighbors - b.vy)
          + cohesion * (cohY / neighbors);
      }
      const speed = Math.hypot(b.vx, b.vy);
      if (speed > maxSpeed) {
        b.vx = (b.vx / speed) * maxSpeed;
        b.vy = (b.vy / speed) * maxSpeed;
      }
      b.x = (b.x + b.vx + width) % width;
      b.y = (b.y + b.vy + height) % height;
    }
  }

  return {
    step,
    boids: () => boids.map((b) => ({ ...b })),
    params: { width, height, count, separation, alignment, cohesion, vision, maxSpeed },
  };
}

export default createFlock;
