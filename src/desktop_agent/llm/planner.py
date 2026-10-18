# -*- coding: utf-8 -*-
"""Planner: the agent-facing wrapper around LlmClient.

Keeps usage counters and nothing else, so a caller that only wants to talk to
the model can use LlmClient directly without pulling in the agent.
"""
from __future__ import annotations

import json

from .. import config
from .client import LlmClient, LlmError

__all__ = ['Planner', 'LlmClient', 'LlmError', 'parse_json']


def parse_json(text: str):
    """Best-effort extraction of the first JSON object in a reply."""
    try:
        return json.loads(text)
    except Exception:
        pass
    i, j = text.find('{'), text.rfind('}')
    if i >= 0 and j > i:
        try:
            return json.loads(text[i:j + 1])
        except Exception:
            return None
    return None


class Planner:
    def __init__(self, model: str = None, timeout: float = 60.0,
                 api_key: str = None, base_url: str = None):
        settings = config.load_settings()
        self.client = LlmClient(
            api_key or config.load_api_key(),
            base_url=base_url or settings.get('base_url'),
            model=model or settings.get('model'),
            timeout=timeout)
        self.calls = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0

    def chat(self, messages, json_mode=True, max_tokens=1400,
             temperature=0.0, image_png=None) -> dict:
        out = self.client.chat(messages, json_mode=json_mode, max_tokens=max_tokens,
                               temperature=temperature, image_png=image_png)
        self.calls += 1
        self.prompt_tokens += out.get('prompt_tokens', 0)
        self.completion_tokens += out.get('completion_tokens', 0)
        return out
