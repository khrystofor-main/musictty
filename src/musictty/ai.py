"""AI radio: a language model turns a mood into a few songs to start from.

Any OpenAI-compatible chat API works. DeepSeek and Nous Portal are preset: the provider is
the one whose key is set (DEEPSEEK_API_KEY, NOUS_API_KEY), or MUSICTTY_AI_PROVIDER picks one.
MUSICTTY_AI_KEY, MUSICTTY_AI_URL and MUSICTTY_AI_MODEL override the preset. The key stays in
the environment, never in musictty's files.

Blocking: async code runs it in a thread.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass

# name -> (API base URL, model, the environment variable with its key)
PROVIDERS = {
    "deepseek": ("https://api.deepseek.com", "deepseek-chat", "DEEPSEEK_API_KEY"),
    "nous": (
        "https://inference-api.nousresearch.com/v1",
        "google/gemini-3.8-flash",
        "NOUS_API_KEY",
    ),
}

SONGS = 8

PROMPT = f"""You pick music for an endless radio. The listener describes a mood, a moment or a
style, in any language. Suggest {SONGS} real, existing songs that fit it and can be found on
YouTube Music: different artists, in a good listening order. Reply with the list only, one song
per line as "Artist - Title", with no numbering and no comments."""


class AIError(Exception):
    """The AI couldn't be asked: no key, a network or API error. The message is for the user."""


@dataclass(frozen=True)
class Config:
    provider: str
    url: str
    model: str
    key: str


def config() -> Config:
    env = os.environ
    name = env.get("MUSICTTY_AI_PROVIDER", "").strip().lower()
    if name and name not in PROVIDERS:
        raise AIError(f"unknown ai provider {name}: {', '.join(PROVIDERS)}")
    if not name:
        # the provider whose key is set; DeepSeek when only MUSICTTY_AI_KEY is
        name = next((p for p, (*_, var) in PROVIDERS.items() if env.get(var)), "deepseek")
    url, model, var = PROVIDERS[name]
    key = env.get("MUSICTTY_AI_KEY") or env.get(var)
    if not key:
        keys = " or ".join(v for *_, v in PROVIDERS.values())
        raise AIError(f"no ai key: set {keys} (after setx, open a new terminal)")
    return Config(
        provider=name,
        url=(env.get("MUSICTTY_AI_URL") or url).rstrip("/"),
        model=env.get("MUSICTTY_AI_MODEL") or model,
        key=key,
    )


_LIST_MARK = re.compile(r"^\s*(?:\d+[.)]|[-*•])\s*")


def parse(reply: str) -> list[str]:
    """Song lines of the reply as search queries, without numbering, bullets and quotes."""
    songs: list[str] = []
    for line in reply.splitlines():
        line = _LIST_MARK.sub("", line)
        line = " ".join(re.sub(r"[\"“”*]", "", line).split())
        # "Artist - Title" (or —, –): anything else is chatter around the list
        if re.search(r"\S [-—–] \S", line) and line.lower() not in {s.lower() for s in songs}:
            songs.append(line)
    return songs[:SONGS]


def suggest(mood: str, cfg: Config | None = None) -> list[str]:
    """Songs for a mood as "Artist - Title" search queries."""
    cfg = cfg or config()
    body = {
        "model": cfg.model,
        "messages": [
            {"role": "system", "content": PROMPT},
            {"role": "user", "content": mood},
        ],
        "temperature": 1.0,
    }
    request = urllib.request.Request(
        f"{cfg.url}/chat/completions",
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {cfg.key}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            data = json.load(response)
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:200]
        hint = {
            401: "check the key",
            402: "out of credits",
            404: "check MUSICTTY_AI_URL / MUSICTTY_AI_MODEL",
        }.get(e.code, detail)
        raise AIError(f"{cfg.provider} answered {e.code}: {hint}") from None
    except (OSError, ValueError) as e:
        raise AIError(f"{cfg.provider} is unreachable: {e}") from None
    try:
        reply = data["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError):
        raise AIError(f"{cfg.provider} sent an unexpected answer") from None
    songs = parse(reply)
    if not songs:
        raise AIError("the ai suggested no songs")
    return songs
