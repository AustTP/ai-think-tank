// 2D cellular automata engine (ES module).
// Reusable core: torus or bounded grids, configurable birth/survive rules,
// deterministic stepping via an injected RNG, and helpers for rendering.

export function createCA(options = {}) {
  const width = options.width | 0 || 64;
  const height = options.height | 0 || 64;
  const torus = options.torus !== false;
  const birth = options.birth || [3];
  const survive = options.survive || [2, 3];

  const birthSet = new Set(birth);
  const surviveSet = new Set(survive);

  let cells = new Uint8Array(width * height);
  let gen = 0;
  let population = 0;

  function idx(x, y) {
    return y * width + x;
  }

  function inBounds(x, y) {
    return x >= 0 && x < width && y >= 0 && y < height;
  }

  function getCell(x, y) {
    if (torus) {
      const wx = ((x % width) + width) % width;
      const wy = ((y % height) + height) % height;
      return cells[idx(wx, wy)];
    }
    if (!inBounds(x, y)) return 0;
    return cells[idx(x, y)];
  }

  function setCell(x, y, value) {
    if (!torus && !inBounds(x, y)) return;
    const wx = torus ? ((x % width) + width) % width : x;
    const wy = torus ? ((y % height) + height) % height : y;
    const i = idx(wx, wy);
    const next = value ? 1 : 0;
    if (cells[i] === next) return;
    population += next ? 1 : -1;
    cells[i] = next;
  }

  function countNeighbors(x, y) {
    let n = 0;
    for (let dy = -1; dy <= 1; dy++) {
      for (let dx = -1; dx <= 1; dx++) {
        if (dx === 0 && dy === 0) continue;
        n += getCell(x + dx, y + dy);
      }
    }
    return n;
  }

  function step() {
    const next = new Uint8Array(width * height);
    let nextPop = 0;
    for (let y = 0; y < height; y++) {
      for (let x = 0; x < width; x++) {
        const n = countNeighbors(x, y);
        const alive = cells[idx(x, y)];
        const nextAlive = alive ? surviveSet.has(n) : birthSet.has(n);
        if (nextAlive) {
          next[idx(x, y)] = 1;
          nextPop++;
        }
      }
    }
    cells = next;
    population = nextPop;
    gen++;
    return { generation: gen, population: nextPop };
  }

  function reset(pattern) {
    cells = new Uint8Array(width * height);
    gen = 0;
    population = 0;
    if (pattern) paint(pattern);
  }

  // pattern: array of [x, y] pairs
  function paint(pattern) {
    for (const [x, y] of pattern) setCell(x, y, 1);
  }

  // Seed with random live cells at the given density.
  function randomize(density = 0.3, rng = Math.random) {
    cells = new Uint8Array(width * height);
    gen = 0;
    population = 0;
    for (let i = 0; i < cells.length; i++) {
      if (rng() < density) {
        cells[i] = 1;
        population++;
      }
    }
  }

  function render(ctx, cellSize = 8, colors = { dead: '#111', alive: '#7fd962' }) {
    ctx.fillStyle = colors.dead;
    ctx.fillRect(0, 0, width * cellSize, height * cellSize);
    ctx.fillStyle = colors.alive;
    for (let y = 0; y < height; y++) {
      for (let x = 0; x < width; x++) {
        if (cells[idx(x, y)]) {
          ctx.fillRect(x * cellSize, y * cellSize, cellSize, cellSize);
        }
      }
    }
  }

  function snapshot() {
    return {
      width,
      height,
      generation: gen,
      population,
      cells: Array.from(cells),
    };
  }

  return {
    width,
    height,
    get generation() { return gen; },
    get population() { return population; },
    getCell,
    setCell,
    countNeighbors,
    step,
    reset,
    paint,
    randomize,
    render,
    snapshot,
  };
}

// Common named rules.
export const RULES = {
  life: { birth: [3], survive: [2, 3] },
  highlife: { birth: [3, 6], survive: [2, 3] },
  seeds: { birth: [2], survive: [] },
  daynight: { birth: [3, 6, 7, 8], survive: [2, 4, 6, 7, 8] },
};

export default createCA;
