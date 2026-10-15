"""Open-Meteo forecast API (https://open-meteo.com). Free, no key, up to 16 days ahead."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from pitwall.sources.http import CachedClient, RateLimiter

BASE_URL = "https://api.open-meteo.com/v1"
FORECAST_DAYS = 16
HOURLY = "temperature_2m,precipitation_probability,precipitation,weather_code,wind_speed_10m"

_limiter = RateLimiter(min_interval=1.0, per_hour=500)


def make_client(**kwargs: Any) -> OpenMeteoClient:
    kwargs.setdefault("limiter", _limiter)
    return OpenMeteoClient(BASE_URL, **kwargs)


class OpenMeteoClient(CachedClient):
    def __init__(self, base_url: str = BASE_URL, **kwargs: Any) -> None:
        super().__init__("open-meteo", base_url, **kwargs)

    def hourly(self, lat: float, lng: float) -> dict[str, Any]:
        """Hourly forecast in UTC: {"time": [...], "temperature_2m": [...], ...}."""
        data = self.get_json(
            "forecast",
            {
                "latitude": round(lat, 3),
                "longitude": round(lng, 3),
                "hourly": HOURLY,
                "timezone": "UTC",
                "forecast_days": FORECAST_DAYS,
                "wind_speed_unit": "kmh",
            },
            max_age=timedelta(hours=1),
        )
        return (data or {}).get("hourly") or {}


# WMO weather interpretation codes, as used by Open-Meteo.
WMO = {
    0: ("☀️", "Clear"),
    1: ("🌤️", "Mainly clear"),
    2: ("⛅", "Partly cloudy"),
    3: ("☁️", "Overcast"),
    45: ("🌫️", "Fog"),
    48: ("🌫️", "Freezing fog"),
    51: ("🌦️", "Light drizzle"),
    53: ("🌦️", "Drizzle"),
    55: ("🌧️", "Heavy drizzle"),
    56: ("🌧️", "Freezing drizzle"),
    57: ("🌧️", "Freezing drizzle"),
    61: ("🌦️", "Light rain"),
    63: ("🌧️", "Rain"),
    65: ("🌧️", "Heavy rain"),
    66: ("🌧️", "Freezing rain"),
    67: ("🌧️", "Freezing rain"),
    71: ("🌨️", "Light snow"),
    73: ("🌨️", "Snow"),
    75: ("❄️", "Heavy snow"),
    77: ("🌨️", "Snow grains"),
    80: ("🌦️", "Rain showers"),
    81: ("🌧️", "Rain showers"),
    82: ("⛈️", "Violent rain showers"),
    85: ("🌨️", "Snow showers"),
    86: ("🌨️", "Heavy snow showers"),
    95: ("⛈️", "Thunderstorm"),
    96: ("⛈️", "Thunderstorm with hail"),
    99: ("⛈️", "Thunderstorm with hail"),
}


def describe(code: int | None) -> tuple[str, str]:
    if code is None:
        return ("", "")
    return WMO.get(code, ("🌡️", f"Weather code {code}"))
