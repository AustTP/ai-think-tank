// WeatherStation: a deterministic, fixed-timestep weather system that
// registers with the World's systems registry like any other system.
// Self-contained seeded RNG so it works headless and in the browser.

function mulberry32(seed) {
  let a = seed >>> 0;
  return function next() {
    a |= 0;
    a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

export class WeatherStation {
  constructor(seed = 1) {
    this.seed = seed >>> 0;
    this.rng = mulberry32(this.seed);
    this.time = 0; // seconds since start
    this.temperature = 15 + this.rng() * 10; // degrees C
    this.humidity = 40 + this.rng() * 30; // percent
    this.pressure = 1000 + this.rng() * 20; // hPa
    this.windDirection = this.rng() * 2 * Math.PI; // radians
    this.windSpeed = this.rng() * 8; // m/s
    this.rain = 0; // 0..1 intensity
    this.frontStrength = this.rng(); // slow-moving pressure front
  }

  step(dt) {
    if (dt <= 0) return this.snapshot();
    this.time += dt;
    const day = 86400;
    const diurnal = Math.sin((2 * Math.PI * this.time) / day - Math.PI / 2);
    // Diurnal temperature swing modulated by cloud cover (rain).
    const swing = 6 * (1 - 0.6 * this.rain);
    this.temperature += (diurnal * swing - (this.temperature - 18) * 0.01 * dt) * (dt / day) * 24;
    this.temperature = Math.max(-30, Math.min(45, this.temperature));
    // Humidity rises with rain, falls in clear/warm conditions.
    const humidityTarget = 55 - this.temperature * 0.5 + this.rain * 35;
    this.humidity += (humidityTarget - this.humidity) * 0.01 * dt;
    this.humidity = Math.max(0, Math.min(100, this.humidity));
    // Fronts drift the pressure; rain releases pressure.
    this.frontStrength += (this.rng() - 0.5) * 0.002 * dt;
    this.frontStrength = Math.max(0, Math.min(1, this.frontStrength));
    this.pressure += (this.frontStrength - 0.5) * 0.05 * dt - this.rain * 0.01 * dt;
    this.pressure = Math.max(950, Math.min(1050, this.pressure));
    // Wind veers slowly; gusts scale with front strength.
    this.windDirection += (this.rng() - 0.5) * 0.01 * dt;
    this.windDirection = (this.windDirection + 2 * Math.PI) % (2 * Math.PI);
    const windTarget = 2 + this.frontStrength * 10;
    this.windSpeed += (windTarget - this.windSpeed) * 0.005 * dt;
    this.windSpeed = Math.max(0, Math.min(40, this.windSpeed));
    // Rain forms when humidity is high and pressure is low; clears otherwise.
    const rainTarget = (this.humidity > 75 && this.pressure < 1000)
      ? (this.humidity - 75) / 25 * (1000 - this.pressure) / 50
      : 0;
    this.rain += (rainTarget - this.rain) * 0.02 * dt;
    this.rain = Math.max(0, Math.min(1, this.rain));
    return this.snapshot();
  }

  snapshot() {
    return {
      time: this.time,
      temperature: this.temperature,
      humidity: this.humidity,
      pressure: this.pressure,
      windDirection: this.windDirection,
      windSpeed: this.windSpeed,
      rain: this.rain,
    };
  }

  reset() {
    const fresh = new WeatherStation(this.seed);
    for (const key of Object.keys(fresh)) {
      this[key] = fresh[key];
    }
  }
}

// System adapter: works whether the World calls systems as
// system.update(world, dt) or system(world, dt).
export function createWeatherSystem(seed = 1) {
  const station = new WeatherStation(seed);
  function weatherSystem(world, dt) {
    return weatherSystem.update(world, dt);
  }
  Object.defineProperty(weatherSystem, 'name', { value: 'weather' });
  weatherSystem.station = station;
  weatherSystem.update = function update(world, dt) {
    world.weather = station.step(dt);
    return world.weather;
  };
  return weatherSystem;
}
