import json
import shutil

import pytest
from PIL import Image, ImageDraw, ImageFont

from scan_screen.config import Config, load_config
from scan_screen.ocr import extract_blocks, group_words
from scan_screen.translate import DeepLTranslator, GoogleFreeTranslator, TranslationError


def _data(words):
    """Build a fake pytesseract DICT from (text, conf, block, par, line, left, top, w, h)."""
    keys = ["text", "conf", "block_num", "par_num", "line_num", "left", "top", "width", "height"]
    return {k: [w[i] for w in words] for i, k in enumerate(keys)}


def test_group_words_paragraphs_and_scale():
    data = _data([
        ("Hello", 95, 1, 1, 1, 10, 10, 50, 20),
        ("world", 90, 1, 1, 1, 70, 10, 50, 20),
        ("again", 92, 1, 1, 2, 10, 40, 50, 20),
        ("junk", 10, 1, 1, 2, 70, 40, 50, 20),   # low confidence, dropped
        ("", -1, 1, 1, 2, 0, 0, 0, 0),           # empty, dropped
        ("Second", 88, 2, 1, 1, 10, 200, 80, 20),
    ])
    blocks = group_words(data, min_conf=40, scale=2.0)
    assert [b.text for b in blocks] == ["Hello world again", "Second"]
    first = blocks[0]
    assert (first.x, first.y, first.w, first.h) == (5, 5, 55, 25)
    assert first.line_height == 10


def test_group_words_joins_hyphenation_and_cjk():
    data = _data([
        ("transla-", 90, 1, 1, 1, 0, 0, 80, 20),
        ("tion", 90, 1, 1, 2, 0, 30, 40, 20),
    ])
    assert group_words(data)[0].text == "translation"
    cjk = _data([("日本", 90, 1, 1, 1, 0, 0, 40, 20), ("語", 90, 1, 1, 1, 45, 0, 20, 20)])
    assert group_words(cjk, joiner="")[0].text == "日本語"


class FakeResp:
    def __init__(self, payload, status=200):
        self.payload, self.status_code, self.ok, self.text = payload, status, status < 400, json.dumps(payload)

    def json(self):
        return self.payload

    def raise_for_status(self):
        if not self.ok:
            import requests
            raise requests.HTTPError(str(self.status_code))


class FakeSession:
    def __init__(self, handler):
        self.handler, self.calls = handler, []

    def post(self, url, **kw):
        self.calls.append((url, kw))
        return self.handler(url, kw)


def test_google_batches_into_one_request_and_keeps_order():
    def handler(url, kw):
        q = kw["data"]["q"]
        # Google returns one segment per sentence/line; newlines are kept in the text.
        return FakeResp([[[line.upper() + ("\n" if i < q.count("\n") else ""), line, None]
                          for i, line in enumerate(q.split("\n"))], None, "es"])

    s = FakeSession(handler)
    t = GoogleFreeTranslator(session=s)
    assert t.translate_batch(["uno", "dos", "tres"], "en") == ["UNO", "DOS", "TRES"]
    assert len(s.calls) == 1


def test_google_falls_back_when_lines_merge():
    def handler(url, kw):
        q = kw["data"]["q"]
        return FakeResp([[[q.replace("\n", " ").upper(), q, None]]])  # lost the line breaks

    s = FakeSession(handler)
    assert GoogleFreeTranslator(session=s).translate_batch(["uno", "dos"], "en") == ["UNO", "DOS"]
    assert len(s.calls) == 3  # one batch attempt, then one per paragraph


def test_google_retries_once_on_rate_limit(monkeypatch):
    monkeypatch.setattr("scan_screen.translate.time.sleep", lambda s: None)
    responses = iter([FakeResp({}, 429), FakeResp([[["Hi", "Hola", None]]])])
    s = FakeSession(lambda u, kw: next(responses))
    assert GoogleFreeTranslator(session=s).translate_batch(["Hola"], "en") == ["Hi"]


def test_google_bad_response_raises():
    t = GoogleFreeTranslator(session=FakeSession(lambda u, kw: FakeResp({"oops": 1})))
    with pytest.raises(TranslationError):
        t.translate_batch(["x"], "en")


def test_deepl_request_shape_and_errors():
    s = FakeSession(lambda u, kw: FakeResp({"translations": [{"text": "Hallo"}, {"text": "Welt"}]}))
    t = DeepLTranslator("abc:fx", session=s)
    assert t.translate_batch(["Hello", "World"], "de", "en") == ["Hallo", "Welt"]
    url, kw = s.calls[0]
    assert url == "https://api-free.deepl.com/v2/translate"
    assert kw["json"] == {"text": ["Hello", "World"], "target_lang": "DE", "source_lang": "EN"}
    assert DeepLTranslator._target_code("en") == "EN-US"

    bad = DeepLTranslator("key", session=FakeSession(lambda u, kw: FakeResp({}, 403)))
    assert bad.url.startswith("https://api.deepl.com")
    with pytest.raises(TranslationError, match="API key"):
        bad.translate_batch(["x"], "de")
    with pytest.raises(TranslationError):
        DeepLTranslator("")


def test_config_created_and_unknown_keys_ignored(tmp_path):
    path = tmp_path / "config.json"
    assert load_config(path) == Config()
    assert path.exists()
    path.write_text(json.dumps({"target_language": "de", "removed_option": 1}))
    assert load_config(path).target_language == "de"


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="tesseract not installed")
@pytest.mark.parametrize("dark", [False, True])
def test_ocr_reads_rendered_text(dark):
    img = Image.new("RGB", (700, 160), (25, 25, 30) if dark else (255, 255, 255))
    draw = ImageDraw.Draw(img)
    font = ImageFont.load_default(size=28)
    draw.text((20, 20), "The quick brown fox", fill=(230, 230, 230) if dark else (0, 0, 0), font=font)
    draw.text((20, 100), "jumps over the dog", fill=(230, 230, 230) if dark else (0, 0, 0), font=font)
    blocks = extract_blocks(img)
    text = " ".join(b.text for b in blocks).lower()
    assert "quick brown fox" in text and "jumps over" in text
    for b in blocks:
        assert 0 <= b.x < 700 and 0 <= b.y < 160 and b.w > 0 and b.h > 0
