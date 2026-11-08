import json
import os
import socket
import zipfile
from pathlib import Path

import pytest
import requests

from scan_screen import privacy
from scan_screen.config import load_config
from scan_screen.translate import CachingTranslator, Translator, TranslationError
from scan_screen.translate_local import LocalTranslator, ModelMissing, route, safe_extract, source_from_ocr

# Set SCAN_SCREEN_TEST_MODELS to a folder with an installed es_en model to run real offline translation.
MODELS = os.environ.get("SCAN_SCREEN_TEST_MODELS")


@pytest.fixture
def offline():
    privacy.enforce_offline()
    yield
    privacy.disable()


def test_offline_blocks_internet_but_not_local_sockets(offline):
    with pytest.raises(privacy.NetworkBlocked):
        socket.create_connection(("example.com", 443), timeout=2)
    with pytest.raises(privacy.NetworkBlocked):
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            s.connect(("93.184.216.34", 443))
        finally:
            s.close()
    with pytest.raises(requests.exceptions.ConnectionError):
        requests.get("https://translate.googleapis.com/", timeout=2)
    # Unix sockets (e.g. the X11 display) are not internet traffic and keep working.
    a, b = socket.socketpair()
    a.sendall(b"ok")
    assert b.recv(2) == b"ok"
    a.close()
    b.close()


def test_allow_network_is_per_thread_and_scoped(offline):
    import threading

    seen = {}

    def other_thread():
        seen["allowed"] = privacy._allowed()

    with privacy.allow_network():
        assert privacy._allowed()
        t = threading.Thread(target=other_thread)
        t.start()
        t.join()
    assert seen["allowed"] is False
    assert not privacy._allowed()


def test_disable_restores_networking():
    privacy.enforce_offline()
    privacy.disable()
    assert socket.socket.connect is privacy._original_connect
    assert not privacy.is_offline()


def test_source_language_and_pivot_routes():
    assert source_from_ocr("spa", "en") == "es"
    assert source_from_ocr("eng+jpn", "en") == "ja"
    assert source_from_ocr("eng", "en") is None
    have = {("es", "en"), ("en", "de")}
    assert route("es", "en", have) == [("es", "en")]
    assert route("es", "de", have) == [("es", "en"), ("en", "de")]
    assert route("fr", "en", have) is None


def test_missing_model_raises_model_missing(tmp_path):
    t = LocalTranslator("spa", root=tmp_path)
    with pytest.raises(ModelMissing) as e:
        t.translate_batch(["Hola"], "en")
    assert (e.value.src, e.value.dst) == ("es", "en")
    with pytest.raises(TranslationError, match="auto-detect"):
        LocalTranslator("eng", root=tmp_path).translate_batch(["Hello"], "en")


def _zip(path: Path, members: dict[str, str]):
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)


def test_safe_extract_rejects_path_traversal(tmp_path):
    bad = tmp_path / "bad.zip"
    _zip(bad, {"pkg/metadata.json": "{}", "../../evil.txt": "x"})
    with pytest.raises(TranslationError, match="unsafe"):
        safe_extract(bad, tmp_path / "out")
    assert not (tmp_path / "evil.txt").exists()

    good = tmp_path / "good.zip"
    _zip(good, {"translate-es_en/metadata.json": json.dumps({"from_code": "es", "to_code": "en"})})
    assert safe_extract(good, tmp_path / "ok").name == "translate-es_en"


def test_cache_translates_each_text_once():
    class Counting(Translator):
        name = "count"
        calls: list = []

        def translate_batch(self, texts, target, source="auto"):
            self.calls.append(list(texts))
            return [t.upper() for t in texts]

    inner = Counting()
    t = CachingTranslator(inner)
    assert t.translate_batch(["a", "b", "a"], "en") == ["A", "B", "A"]
    assert t.translate_batch(["b", "c"], "en") == ["B", "C"]
    assert inner.calls == [["a", "b"], ["c"]]


def test_old_config_migrates_to_offline(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"engine": "google", "target_language": "de"}))
    cfg = load_config(path)
    assert cfg.engine == "local" and cfg.target_language == "de"
    assert json.loads(path.read_text())["config_version"] == 3
    # A deliberate online choice in a current config is respected.
    path.write_text(json.dumps({"engine": "deepl", "config_version": 2}))
    assert load_config(path).engine == "deepl"


@pytest.mark.skipif(not MODELS, reason="set SCAN_SCREEN_TEST_MODELS to a models folder with es_en")
def test_real_offline_translation_with_network_blocked(offline):
    t = LocalTranslator("spa", root=Path(MODELS))
    out = t.translate_batch(["Comprar ahora", "Todos los productos tienen un descuento. El envío es gratis."], "en")
    assert out[0].lower() == "buy now"
    assert "free" in out[1].lower() and "." in out[1]


def test_old_live_defaults_migrate_to_faster_ones(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"engine": "local", "config_version": 2,
                                "live_interval_ms": 600, "live_peek_ms": 3000}))
    cfg = load_config(path)
    assert (cfg.live_interval_ms, cfg.live_peek_ms) == (300, 10000)
    path.write_text(json.dumps({"config_version": 2, "live_interval_ms": 900}))
    assert load_config(path).live_interval_ms == 900  # a deliberate choice is kept
