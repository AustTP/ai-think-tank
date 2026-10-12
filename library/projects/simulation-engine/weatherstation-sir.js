// Weather Station integration: runs SIR ensembles and reports summaries
// like a station bulletin. Pure ES module, no DOM dependency.

import { createSIR } from './sir.js';

export function runEnsemble(config, runs) {
  const count = runs === undefined ? 10 : runs;
  const summaries = [];
  for (let seed = 1; seed <= count; seed++) {
    const sim = createSIR(Object.assign({}, config, { seed }));
    summaries.push(sim.totals());
  }

  const total = summaries.length;
  const mean = (key) =>
    summaries.reduce((acc, s) => acc + s[key], 0) / total;

  return {
    runs: total,
    meanSusceptible: mean('susceptible'),
    meanInfected: mean('infected'),
    meanRecovered: mean('recovered'),
    attackRate: mean('recovered') / config.population,
  };
}

export function bulletin(config, runs) {
  const ens = runEnsemble(config, runs);
  return (
    'WEATHER STATION / EPIDEMIC BULLETIN: ' +
    ens.runs + ' runs, R0~' +
    (config.beta / config.gamma).toFixed(2) +
    ', attack rate ' +
    (ens.attackRate * 100).toFixed(1) + '%'
  );
}
