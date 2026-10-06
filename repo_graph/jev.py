"""Bounded TypeSafe decisions. Source evidence is data, never an action."""
import hashlib
from http.client import HTTPException
import json
import os
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

MODEL = 'jev-1.13.0'
MAX_REQUEST_BYTES = 48 * 1024


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None  # Never forward the bearer credential to another endpoint.


OPEN = build_opener(NoRedirect()).open


def typesafe_key():
    if key := os.environ.get('TYPESAFE_API_KEY'):
        return key
    # Only this explicitly authorized entry; never source or enumerate the file.
    try:
        with (Path.home() / '.env').open(encoding='utf-8') as stream:
            for line in stream:
                for prefix in ('TYPESAFE_API_KEY=', 'export TYPESAFE_API_KEY='):
                    if line.startswith(prefix):
                        value = line[len(prefix):].strip()
                        if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
                            value = value[1:-1]
                        return value
    except OSError:
        pass
    return ''


def body(state, questions):
    encoded = json.dumps({'model': MODEL, 'state': state, 'questions': questions},
                         ensure_ascii=False, separators=(',', ':')).encode()
    if len(encoded) > MAX_REQUEST_BYTES:
        raise ValueError('Jev request exceeds the 48 KiB export budget')
    return encoded


def evaluate(encoded, key=None, *, timeout=10):
    if len(encoded) > MAX_REQUEST_BYTES:
        raise ValueError('Jev request exceeds the 48 KiB export budget')
    key = key or typesafe_key()
    if not key:
        raise RuntimeError('TYPESAFE_API_KEY is unavailable')
    request = Request('https://api.typesafe.ai/v1/systemone', data=encoded,
                      headers={'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'})
    try:
        with OPEN(request, timeout=timeout) as response:
            raw = response.read(256 * 1024 + 1)
        if len(raw) > 256 * 1024:
            raise ValueError('Jev response exceeds the response budget')
        result = json.loads(raw)
    except HTTPError as error:
        code = error.code
        error.close()
        raise RuntimeError(f'TypeSafe HTTP {code}; request was not retried') from None
    except (URLError, HTTPException):
        raise RuntimeError('TypeSafe connection failed; request was not retried') from None
    if not isinstance(result, dict) or result.get('model') != MODEL or not isinstance(result.get('answers'), dict):
        raise ValueError('Unexpected Jev model or response schema')
    usage = result.get('usage', {})
    if not isinstance(usage, dict) or any(type(usage.get(field)) is not int or usage[field] < 0 for field in ('input_tokens', 'output_tokens')):
        raise ValueError('Missing or invalid Jev token usage')
    return {'model':result['model'], 'answers':result['answers'],
            'usage':{field:usage[field] for field in ('input_tokens','output_tokens')}}


def request_hash(encoded):
    return hashlib.sha256(encoded).hexdigest()
