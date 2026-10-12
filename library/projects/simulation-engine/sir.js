// SIR epidemic model -- deterministic, seeded, framework-free ES module.
// State: S -> I -> R with rates beta (contact) and gamma (recovery).

function mulberry32(seed) {
  let a = seed >>> 0;
  return function () {
    a |= 0;
    a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

export function createSIR(config) {
  const population = config.population;
  const beta = config.beta;
  const gamma = config.gamma;
  const days = config.days;
  const seed = config.seed === undefined ? 1 : config.seed;
  const infected0 = config.infected === undefined ? 1 : config.infected;

  if (!Number.isFinite(population) || population <= 0) {
    throw new Error('population must be a positive finite number');
  }
  if (!Number.isFinite(beta) || beta < 0) {
    throw new Error('beta must be a non-negative finite number');
  }
  if (!Number.isFinite(gamma) || gamma < 0) {
    throw new Error('gamma must be a non-negative finite number');
  }
  if (!Number.isInteger(days) || days < 0) {
    throw new Error('days must be a non-negative integer');
  }
  if (!Number.isFinite(infected0) || infected0 < 0 || infected0 > population) {
    throw new Error('infected must be within [0, population]');
  }

  const rng = mulberry32(seed);
  const history = [];
  let susceptible = population - infected0;
  let infected = infected0;
  let recovered = 0;

  history.push({
    day: 0,
    susceptible,
    infected,
    recovered,
  });

  for (let day = 1; day <= days; day++) {
    const pInfect = 1 - Math.exp(-beta * infected / population);
    const pRecover = 1 - Math.exp(-gamma);

    let newInfections = 0;
    for (let i = 0; i < susceptible; i++) {
      if (rng() < pInfect) newInfections++;
    }
    let newRecoveries = 0;
    for (let i = 0; i < infected; i++) {
      if (rng() < pRecover) newRecoveries++;
    }

    newInfections = Math.min(newInfections, susceptible);
    newRecoveries = Math.min(newRecoveries, infected);

    susceptible -= newInfections;
    infected += newInfections - newRecoveries;
    recovered += newRecoveries;

    history.push({
      day,
      susceptible,
      infected,
      recovered,
    });
  }

  return {
    population,
    beta,
    gamma,
    days,
    seed,
    history,
    peak() {
      let best = history[0];
      for (const row of history) {
        if (row.infected > best.infected) best = row;
      }
      return best;
    },
    totals() {
      const last = history[history.length - 1];
      return {
        susceptible: last.susceptible,
        infected: last.infected,
        recovered: last.recovered,
      };
    },
  };
}
