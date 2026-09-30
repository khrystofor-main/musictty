import asyncio
import http.server
import json
import threading

import pytest
from fakes import tid

from musictty import ai, control, music
from musictty.control import Failure
from musictty.models import Track

S, A = tid(0), tid(1)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    keys = ("DEEPSEEK_API_KEY", "NOUS_API_KEY", "MUSICTTY_AI_KEY")
    settings = ("MUSICTTY_AI_PROVIDER", "MUSICTTY_AI_URL", "MUSICTTY_AI_MODEL")
    for var in keys + settings:
        monkeypatch.delenv(var, raising=False)
    for proxy in ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY"):
        monkeypatch.delenv(proxy, raising=False)


def test_the_provider_is_the_one_with_a_key(monkeypatch):
    with pytest.raises(ai.AIError, match="DEEPSEEK_API_KEY or NOUS_API_KEY"):
        ai.config()
    monkeypatch.setenv("NOUS_API_KEY", "nous-key")
    cfg = ai.config()
    assert (cfg.provider, cfg.key) == ("nous", "nous-key")
    assert cfg.url.startswith("https://inference-api.nousresearch.com")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-key")
    assert ai.config().provider == "deepseek"  # both keys: the first preset
    monkeypatch.setenv("MUSICTTY_AI_PROVIDER", "nous")
    assert ai.config().key == "nous-key"
    monkeypatch.setenv("MUSICTTY_AI_PROVIDER", "other")
    with pytest.raises(ai.AIError, match="unknown ai provider"):
        ai.config()


def test_overrides(monkeypatch):
    monkeypatch.setenv("MUSICTTY_AI_KEY", "k")
    monkeypatch.setenv("MUSICTTY_AI_URL", "http://localhost:1/v1/")
    monkeypatch.setenv("MUSICTTY_AI_MODEL", "some-model")
    assert ai.config() == ai.Config("deepseek", "http://localhost:1/v1", "some-model", "k")


def test_parse_takes_only_the_songs():
    reply = """Here are some songs for your evening:

1. Nujabes - Aruarian Dance
2) "Bonobo — Kerala"
- **Tycho – Awake**
* nujabes - aruarian dance
Enjoy!"""
    assert ai.parse(reply) == ["Nujabes - Aruarian Dance", "Bonobo — Kerala", "Tycho – Awake"]


@pytest.fixture
def api():
    """A fake OpenAI-compatible chat API: answers what `reply` holds, records the requests."""
    state = {"status": 200, "content": "Nujabes - Aruarian Dance\nBonobo - Kerala", "seen": []}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state["seen"].append((self.path, self.headers["Authorization"], body))
            if state["status"] == 200:
                data = {"choices": [{"message": {"content": state["content"]}}]}
            else:
                data = {"error": {"message": "nope"}}
            payload = json.dumps(data).encode()
            self.send_response(state["status"])
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    httpd = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    state["cfg"] = ai.Config("test", f"http://127.0.0.1:{httpd.server_port}/v1", "m", "secret")
    yield state
    httpd.shutdown()


def test_suggest(api):
    assert ai.suggest("calm evening", api["cfg"]) == ["Nujabes - Aruarian Dance", "Bonobo - Kerala"]
    path, auth, body = api["seen"][0]
    assert (path, auth) == ("/v1/chat/completions", "Bearer secret")
    assert body["model"] == "m"
    assert body["messages"][-1] == {"role": "user", "content": "calm evening"}


@pytest.mark.parametrize(
    ("status", "message"), [(401, "check the key"), (402, "out of credits"), (500, "nope")]
)
def test_suggest_errors(api, status, message):
    api["status"] = status
    with pytest.raises(ai.AIError, match=message):
        ai.suggest("calm", api["cfg"])


def test_suggest_without_songs(api):
    api["content"] = "Sorry, I can't help with that."
    with pytest.raises(ai.AIError, match="no songs"):
        ai.suggest("calm", api["cfg"])


def test_ai_radio(monkeypatch):
    monkeypatch.setattr(
        ai, "suggest", lambda mood: ["Nujabes - Aruarian Dance", "Nobody - Nothing"]
    )
    found = {"Nujabes - Aruarian Dance": Track(S, "Nujabes — Aruarian Dance")}
    monkeypatch.setattr(music, "find_songs", lambda qs: [found[q] for q in qs if q in found])
    started = []

    async def fake_start(seed=None, **kwargs):
        started.append((seed, kwargs))

    monkeypatch.setattr(control, "start", fake_start)
    tracks = asyncio.run(control.ai_radio("calm"))
    assert tracks == [found["Nujabes - Aruarian Dance"]]
    assert started == [(S, {"queue": tracks, "then_radio": True, "source": "ai radio"})]

    monkeypatch.setattr(music, "find_songs", lambda qs: [])
    with pytest.raises(Failure, match="none of the ai's songs"):
        asyncio.run(control.ai_radio("calm"))


def test_ai_radio_without_a_key():
    with pytest.raises(Failure, match="no ai key"):
        asyncio.run(control.ai_radio("calm"))
