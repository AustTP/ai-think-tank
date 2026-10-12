// Weatherstation adapter: exposes the predator-prey ecosystem as a station.

import { createEcosystem } from './predatorprey.js';

export const predatorPreyStation = {
  id: 'predatorprey',
  label: 'Predator-Prey Ecosystem',
  create(seed) {
    const sim = createEcosystem({ seed });
    // Map singular count keys to the plural field names the station contract uses.
    const origStep = sim.step.bind(sim);
    sim.step = function step() {
      const s = origStep();
      return { ...s, predators: s.predator, prey: s.prey };
    };
    sim.history = sim.history.map((s) => ({ ...s, predators: s.predator, prey: s.prey }));
    return sim;
  },
  tick(sim) {
    return sim.step();
  },
  series(sim) {
    return sim.history;
  },
  fields: ['prey', 'predators'],
};

export default predatorPreyStation;
