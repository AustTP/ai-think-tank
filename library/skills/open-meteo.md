# Open-Meteo

## Purpose
Reach for this before any live-weather work -- the `weather_now` agent tool's
backing service. Free, no API key, and already wired end-to-end (geocode a
place name, fetch current conditions).

## Key facts
- **Geocoding** (place name -> lat/lon):
  `GET https://geocoding-api.open-meteo.com/v1/search?name=<place>&count=1&
  language=en`. Returns `results[]` with `latitude`, `longitude`, `timezone`,
  `country_code`, `elevation`, `population`.
- **Forecast / current weather**:
  `GET https://api.open-meteo.com/v1/forecast?latitude=<lat>&longitude=<lon>
  &current=apparent_temperature,temperature_2m,weather_code&temperature_unit=
  celsius`. Any hourly variable can be requested via `current=`; the response
  has a `current` object with `time` + the requested values. The legacy
  `current_weather=true` also works. Current conditions are based on 15-minutely
  model data.
- Other forecast params: `hourly`/`daily`, `forecast_days` (0-16),
  `past_days` (0-92), `timezone`, `models`, `cell_selection`.
- No API key for free non-commercial use. Fair-use limits: **10,000 calls/day,
  300,000 calls/month, 5,000/hour, 600/min**; no uptime guarantee. Paid
  commercial plans use `customer-api.open-meteo.com` with an `apikey` param.
- Data is CC BY 4.0 (attribution required for redistribution).
- Other endpoints (not wired): `/v1/archive` (historical), `/v1/air-quality`,
  `/v1/marine`, `/v1/elevation`.

## Policy for this think tank
1. Use `weather_now` for any live-conditions question; it is free and
   keyless, so there is no reason to ever pay a Treg/other provider for weather
   (Treg's catalog does carry weather endpoints -- skip them).
2. Stay well under the per-day call limits (a single task should touch this a
   handful of times at most). If the village ever needs bulk historical
   weather, use the `/v1/archive` endpoint rather than hammering forecast.

## Sources
- https://open-meteo.com/en/docs (forecast + current params, `current=`)
- https://open-meteo.com/en/docs/geocoding-api (geocoding)
- https://open-meteo.com/en/pricing (free tier limits, commercial upgrade)

## Lessons learned
- The whole service is deliberately keyless and generous, so the cost discipline
  is about not abusing the shared 10k/day cap, not about spend. It is the
  reference "free and reliable" endpoint in this codebase -- the shape the other
  free integrations (Open-Meteo, Tavily's free tier) are compared against.
