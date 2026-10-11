"""pendant-web against a made-up archive, over real HTTP."""

import base64
import importlib.util
import json
import threading
import urllib.error
import urllib.request
import wave
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent.parent


@pytest.fixture
def web(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    spec = importlib.util.spec_from_file_location("pendant_web", HERE / "pendant-web.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    root = tmp_path / "recordings"
    day = root / "2026-10-10"
    day.mkdir(parents=True)
    base = day / "pendant_20261010_140139"
    with wave.open(str(base.with_suffix(".wav")), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
        w.writeframes(b"\x00\x01" * 16000)
    base.with_suffix(".opus").write_bytes(b"OPUS")
    (day / "pendant_20261010_140139.words.json").write_text(json.dumps({"segments": [
        {"start": 1.0, "end": 2.0, "at": "2026-10-10T14:01:40", "text": "Good morning <script>", "starred": False},
        {"start": 300.0, "end": 302.0, "at": "2026-10-10T14:16:28", "text": "I pressed the button right there.", "starred": True},
    ]}))
    (day / "pendant_20261010_140139.txt").write_text("x\n")
    # an older recording whose WAV was tidied away
    old = day / "pendant_20261010_090000"
    old.with_suffix(".opus").write_bytes(b"OPUS")
    (day / "pendant_20261010_090000.words.json").write_text(json.dumps({"segments": [
        {"start": 0.5, "end": 1.0, "at": "2026-10-10T09:00:00", "text": "Coffee first.", "starred": False}]}))
    (day / "pendant_20261010_090000.txt").write_text("x\n")
    # not transcribed yet
    (day / "pendant_20261010_200000.opus").write_bytes(b"OPUS")
    (tmp_path / "secret.txt").write_text("outside the archive")
    server = mod.make_server(root, "pw123", host="127.0.0.1", port=0)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    yield mod, f"http://127.0.0.1:{server.server_address[1]}", root
    server.shutdown()


def get(url, password="pw123", headers=None, method="GET", data=None):
    req = urllib.request.Request(url, method=method, data=data, headers=dict(headers or {}))
    if password is not None:
        req.add_header("Authorization", "Basic " + base64.b64encode(f"me:{password}".encode()).decode())
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def test_password_required(web):
    _, url, _ = web
    assert get(url + "/", password=None)[0] == 401
    assert get(url + "/", password="wrong")[0] == 401
    assert get(url + "/audio/2026-10-10/pendant_20261010_140139.wav", password=None)[0] == 401


def test_days_list(web):
    _, url, _ = web
    status, _, body = get(url + "/")
    assert status == 200
    assert b"Saturday, October 10, 2026" in body and b"3 lines" in body and "★ 1".encode() in body


def test_day_page_is_ordered_split_escaped_and_playable(web):
    _, url, _ = web
    _, _, body = get(url + "/day/2026-10-10")
    text = body.decode()
    assert text.index("Coffee first.") < text.index("Good morning") < text.index("I pressed the button")
    assert "&lt;script&gt;" in text and "Good morning <script>" not in text
    assert 'data-src="/audio/2026-10-10/pendant_20261010_140139.wav" data-start="300.0"' in text
    assert text.count("<h2>") == 3  # 09:00, 14:01, 14:16 are separate conversations
    assert "Rebuild audio for 09:00" in text
    assert "1 recording(s) not transcribed yet" in text


def test_audio_supports_seeking(web):
    _, url, _ = web
    status, headers, body = get(url + "/audio/2026-10-10/pendant_20261010_140139.wav", headers={"Range": "bytes=100-199"})
    assert status == 206 and len(body) == 100
    assert headers["Content-Range"].startswith("bytes 100-199/")
    status, headers, body = get(url + "/audio/2026-10-10/pendant_20261010_140139.wav")
    assert status == 200 and headers["Accept-Ranges"] == "bytes" and body[:4] == b"RIFF"


@pytest.mark.parametrize("path", [
    "/audio/2026-10-10/../../secret.txt", "/audio/2026-10-10/%2e%2e%2fsecret.wav",
    "/audio/..%2f..%2f/secret.wav", "/day/../", "/day/2026-10-11", "/audio/2026-10-10/notes.wav",
])
def test_nothing_outside_the_archive(web, path):
    _, url, _ = web
    status, _, body = get(url + path)
    assert status == 404 and b"outside the archive" not in body


def test_search_and_stars(web):
    _, url, _ = web
    _, _, body = get(url + "/search?q=BUTTON")
    assert b"1 line with" in body and b"I pressed the button" in body and b"/day/2026-10-10#" in body
    _, _, body = get(url + "/stars")
    assert b"I pressed the button" in body and b"Coffee" not in body


def test_rebuild_runs_pendant_decode(web, tmp_path, monkeypatch):
    _, url, root = web
    fake = tmp_path / "pendant"
    fake.write_text('#!/bin/sh\n[ "$1" = decode ] && cp "$2" "$3"\n')
    fake.chmod(0o755)
    monkeypatch.setenv("PENDANT_BIN", str(fake))
    status, _, _ = get(url + "/rebuild/2026-10-10/pendant_20261010_090000", method="POST", data=b"")
    assert status == 200  # followed the redirect back to the day
    assert (root / "2026-10-10" / "pendant_20261010_090000.wav").exists()


def test_range_parsing(web):
    mod, _, _ = web
    assert mod.parse_range("bytes=0-", 10) == (0, 9)
    assert mod.parse_range("bytes=-4", 10) == (6, 9)
    assert mod.parse_range("bytes=5-100", 10) == (5, 9)
    assert mod.parse_range("bytes=9-3", 10) is None
    assert mod.parse_range("nonsense", 10) is None


def test_only_home_network_addresses(web):
    mod, _, _ = web
    for ok in ("127.0.0.1", "192.168.1.20", "10.0.0.5", "172.16.3.4", "::1", "fe80::1%wlan0",
               "::ffff:192.168.1.20", "169.254.10.10"):
        assert mod.from_home_network(ok), ok
    for bad in ("8.8.8.8", "52.1.2.3", "2606:4700::1111", "::ffff:8.8.8.8", "nonsense"):
        assert not mod.from_home_network(bad), bad


def test_turns_away_outside_addresses_even_with_password(web, monkeypatch):
    mod, url, _ = web
    monkeypatch.setattr(mod, "from_home_network", lambda addr: False)
    status, _, _ = get(url + "/")
    assert status == 403


def test_settings_file_made_private(web, tmp_path):
    mod, _, _ = web
    env = tmp_path / "env"
    env.write_text("PENDANT_WEB_PASSWORD=x\n")
    env.chmod(0o644)
    assert mod.tighten_settings(env) is True
    assert env.stat().st_mode & 0o777 == 0o600
    assert mod.tighten_settings(env) is False
