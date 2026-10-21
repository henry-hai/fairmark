"""Read account fills with Kalshi's signed GET requests."""

from __future__ import annotations

import base64
import json
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

API = 'https://external-api.kalshi.com/trade-api/v2'


class FillError(Exception):
    """A request failure without credentials or response contents."""


class KalshiFills:
    def __init__(self, key_id: str, key_path: Path):
        self.key_id = key_id
        self.key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)

    def get(self, path: str, params: dict) -> dict:
        query = urlencode(params)
        url = f'{API}{path}?{query}' if query else f'{API}{path}'
        for attempt in range(3):
            timestamp = str(int(time.time() * 1000))
            message = f'{timestamp}GET/trade-api/v2{path}'.encode()
            if isinstance(self.key, Ed25519PrivateKey):
                signature = self.key.sign(message)
            else:
                signature = self.key.sign(message, padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                                                                salt_length=padding.PSS.DIGEST_LENGTH), hashes.SHA256())
            headers = {'Accept': 'application/json', 'KALSHI-ACCESS-KEY': self.key_id,
                       'KALSHI-ACCESS-TIMESTAMP': timestamp,
                       'KALSHI-ACCESS-SIGNATURE': base64.b64encode(signature).decode()}
            try:
                with urlopen(Request(url, headers=headers), timeout=20) as response:
                    payload = json.load(response)
                if not isinstance(payload, dict) or not isinstance(payload.get('fills'), list):
                    raise FillError('Kalshi returned invalid fills')
                return payload
            except HTTPError as error:
                if attempt == 2 or (error.code != 429 and error.code < 500):
                    raise FillError(f'Kalshi fills HTTP {error.code}') from None
            except (URLError, TimeoutError):
                if attempt == 2:
                    raise FillError('Kalshi fills network error') from None
            time.sleep(2 ** attempt)
        raise FillError('Kalshi fills request failed')

    def fills(self, *, min_ts: int) -> list[dict]:
        fills, cursor = [], ''
        while True:
            params = {'min_ts': min_ts, 'limit': 1000}
            if cursor:
                params['cursor'] = cursor
            page = self.get('/portfolio/fills', params)
            fills.extend(page['fills'])
            cursor = page.get('cursor') or ''
            if not cursor:
                return fills
