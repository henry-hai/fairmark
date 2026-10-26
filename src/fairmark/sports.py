"""Sport icon for any Kalshi market, read from the sport tag Kalshi puts on its series."""
from __future__ import annotations

import json
import urllib.request
from functools import lru_cache

SERIES_URL = 'https://api.elections.kalshi.com/trade-api/v2/series/'
TAG_ICONS = {
    'Football': '🏈', 'CFB': '🏈', 'Basketball': '🏀', 'Baseball': '⚾', 'Hockey': '🏒', 'Soccer': '⚽',
    'MMA': '🥊', 'UFC': '🥊', 'Boxing': '🥊', 'Tennis': '🎾', 'Squash': '🎾', 'Golf': '⛳',
    'Motorsport': '🏎️', 'Esports': '🎮', 'Video games': '🎮', 'Cricket': '🏏', 'Rugby': '🏉',
    'Aussie Rules': '🏉', 'Lacrosse': '🥍', 'Table Tennis': '🏓', 'Volleyball': '🏐', 'Darts': '🎯',
    'Chess': '♟️', 'Cycling': '🚴', 'Rowing': '🚣', 'Olympics': '🏅',
}
DEFAULT_ICON = '🏆'


def series_of(ticker: str) -> str:
    return ticker.split('-')[0]


@lru_cache(maxsize=None)
def series_tags(series: str, fetch=None) -> tuple[str, ...]:
    fetch = fetch or (lambda url: json.load(urllib.request.urlopen(url, timeout=20)))
    try:
        return tuple(fetch(SERIES_URL + series)['series'].get('tags') or ())
    except Exception:
        return ()


def sport_icon(ticker: str, fetch=None) -> str:
    """The first tag Kalshi gives the market's series that has an icon, else a trophy."""
    return next((TAG_ICONS[tag] for tag in series_tags(series_of(ticker), fetch) if tag in TAG_ICONS), DEFAULT_ICON)
