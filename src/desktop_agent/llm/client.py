# -*- coding: utf-8 -*-
"""A standalone chat-completions client.

Deliberately knows nothing about agents, windows or screenshots: you can import
it on its own to talk to any OpenAI-compatible endpoint.
"""
from __future__ import annotations

import base64
import http.client
import json
import ssl
import time


class LlmError(RuntimeError):
    pass


class LlmClient:
    def __init__(self, api_key: str, base_url: str = 'https://api.deepseek.com',
                 model: str = 'deepseek-flash', timeout: float = 60.0):
        if not api_key:
            raise LlmError('missing API key')
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        base = base_url.rstrip('/')
        if base.startswith('https://'):
            self.host, self.tls, self.prefix = base[8:], True, ''
        elif base.startswith('http://'):
            self.host, self.tls, self.prefix = base[7:], False, ''
        else:
            self.host, self.tls, self.prefix = base, True, ''
        self._conn = None
        self._ssl = ssl.create_default_context()

    # ---- transport -----------------------------------------------------
    def _connect(self):
        if self.tls:
            return http.client.HTTPSConnection(self.host, timeout=self.timeout,
                                               context=self._ssl)
        return http.client.HTTPConnection(self.host, timeout=self.timeout)

    def _post(self, path: str, body: bytes) -> dict:
        last = None
        for attempt in range(3):
            try:
                if self._conn is None:
                    self._conn = self._connect()
                self._conn.request('POST', path, body=body, headers={
                    'Content-Type': 'application/json',
                    'Authorization': 'Bearer ' + self.api_key,
                    'Content-Length': str(len(body))})
                r = self._conn.getresponse()
                raw = r.read()
                if r.status != 200:
                    raise LlmError('HTTP %s: %s' % (r.status, raw[:300]))
                return json.loads(raw.decode('utf-8'))
            except Exception as e:
                last = e
                try:
                    self._conn.close()
                except Exception:
                    pass
                self._conn = None
                time.sleep(0.5 * (attempt + 1))
        raise LlmError('request failed after retries: %r' % (last,))

    # ---- public API ----------------------------------------------------
    def chat(self, messages, json_mode: bool = True, max_tokens: int = 1400,
             temperature: float = 0.0, image_png: bytes = None) -> dict:
        if image_png:
            last = messages[-1]
            content = [{'type': 'text', 'text': last['content']}]
            content.append({'type': 'image_url', 'image_url': {
                'url': 'data:image/png;base64,' + base64.b64encode(image_png).decode()}})
            messages = list(messages[:-1]) + [{'role': 'user', 'content': content}]

        body = {'model': self.model, 'messages': messages,
                'max_tokens': max_tokens, 'temperature': temperature, 'stream': False}
        if json_mode:
            body['response_format'] = {'type': 'json_object'}

        t0 = time.perf_counter()
        j = self._post('/chat/completions', json.dumps(body).encode('utf-8'))
        dt = (time.perf_counter() - t0) * 1000
        choice = j['choices'][0]
        usage = j.get('usage', {})
        return {
            'text': choice['message'].get('content') or '',
            'finish_reason': choice.get('finish_reason', ''),
            'ms': round(dt, 1),
            'prompt_tokens': usage.get('prompt_tokens', 0),
            'completion_tokens': usage.get('completion_tokens', 0),
        }
