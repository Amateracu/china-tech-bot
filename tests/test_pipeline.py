"""Оффлайн-проверка пайплайна: парсинг фида, фильтр, дедуп, сборка поста.

Запуск: python -m tests.test_pipeline
Сеть и API не используются — всё на фикстурах.
"""
import io
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot import relevance, util                     # noqa: E402
from bot.llm import render                          # noqa: E402
from bot.sources import Item                        # noqa: E402
from bot.sources.rss import fetch_rss               # noqa: E402

NOW = datetime.now(timezone.utc)
RSS = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/"><channel><title>Fixture</title>
<item>
  <title>Huawei unveils new 5nm chip made by SMIC</title>
  <link>https://example.com/a?utm_source=rss&amp;id=1</link>
  <description>&lt;p&gt;&lt;img src="https://cdn.example.com/chip.jpg"/&gt;The Chinese company said the semiconductor is produced domestically.&lt;/p&gt;</description>
  <enclosure url="https://cdn.example.com/hero.jpg" type="image/jpeg" length="120000"/>
  <pubDate>{NOW.strftime('%a, %d %b %Y %H:%M:%S +0000')}</pubDate>
</item>
<item>
  <title>Huawei presents a 5nm chip manufactured by SMIC</title>
  <link>https://example.com/b</link>
  <description>Same story, different outlet.</description>
  <pubDate>{NOW.strftime('%a, %d %b %Y %H:%M:%S +0000')}</pubDate>
</item>
<item>
  <title>China lottery results for September</title>
  <link>https://example.com/c</link>
  <description>Nothing technological here.</description>
  <pubDate>{NOW.strftime('%a, %d %b %Y %H:%M:%S +0000')}</pubDate>
</item>
<item>
  <title>Local bakery opens in Lyon</title>
  <link>https://example.com/d</link>
  <description>Croissants.</description>
  <pubDate>{(NOW - timedelta(days=5)).strftime('%a, %d %b %Y %H:%M:%S +0000')}</pubDate>
</item>
</channel></rss>"""

ATOM = """<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"><title>AtomFix</title>
<entry>
  <title>Unitree humanoid robot enters mass production in Hangzhou</title>
  <link rel="alternate" href="https://example.org/robot"/>
  <summary>Chinese robotics maker starts shipping.</summary>
  <published>2026-09-21T06:00:00Z</published>
</entry></feed>"""

SRC = {"id": "fix", "name": "Fixture", "url": "https://example.com/feed",
       "lang": "en", "china_native": True, "weight": 1.0}


def _fake_get(body):
    resp = mock.Mock()
    resp.content = body.encode("utf-8")
    resp.raise_for_status = lambda: None
    return resp


class TestRss(unittest.TestCase):
    def test_parses_rss2(self):
        with mock.patch("bot.sources.rss.requests.get", return_value=_fake_get(RSS)):
            items = fetch_rss(SRC)
        self.assertEqual(len(items), 4)
        self.assertEqual(items[0].title, "Huawei unveils new 5nm chip made by SMIC")
        self.assertIn("produced domestically", items[0].summary)
        self.assertNotIn("<p>", items[0].summary)
        self.assertNotIn("utm_source", items[0].url)
        self.assertIsNotNone(items[0].published)

    def test_parses_atom(self):
        with mock.patch("bot.sources.rss.requests.get", return_value=_fake_get(ATOM)):
            items = fetch_rss(SRC)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].url, "https://example.org/robot")
        self.assertEqual(items[0].published.year, 2026)


class TestMedia(unittest.TestCase):
    def setUp(self):
        with mock.patch("bot.sources.rss.requests.get", return_value=_fake_get(RSS)):
            self.items = fetch_rss(SRC)

    def test_enclosure_wins(self):
        self.assertEqual(self.items[0].image, "https://cdn.example.com/hero.jpg")

    def test_img_in_description_is_fallback(self):
        # у второй новости enclosure нет — берём <img> из описания
        self.assertEqual(self.items[1].image, "")

    def test_media_content_tag(self):
        feed = """<?xml version="1.0"?>
        <rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/"><channel>
        <item><title>BYD ships new EV</title><link>https://e.com/1</link>
        <media:content url="https://cdn.e.com/pic.jpg" type="image/jpeg"/>
        </item></channel></rss>"""
        with mock.patch("bot.sources.rss.requests.get", return_value=_fake_get(feed)):
            items = fetch_rss(SRC)
        self.assertEqual(items[0].image, "https://cdn.e.com/pic.jpg")

    def test_img_from_html_body(self):
        feed = """<?xml version="1.0"?>
        <rss version="2.0"><channel><item>
        <title>Xiaomi chip news</title><link>https://e.com/2</link>
        <description>&lt;img src="/local/pic.png"&gt; текст</description>
        </item></channel></rss>"""
        with mock.patch("bot.sources.rss.requests.get", return_value=_fake_get(feed)):
            items = fetch_rss(SRC)
        self.assertEqual(items[0].image, "/local/pic.png")

    def test_og_image_parsed_and_absolutised(self):
        from bot import media
        html = (b'<html><head><meta property="og:image" content="/img/hero.jpg">'
                b'</head><body></body></html>')
        resp = mock.MagicMock()
        resp.headers = {"Content-Type": "text/html; charset=utf-8"}
        resp.raise_for_status = lambda: None
        resp.iter_content = lambda n: iter([html])
        resp.__enter__ = lambda self_: resp
        resp.__exit__ = lambda *a: False
        with mock.patch("bot.media.requests.get", return_value=resp):
            url = media.og_image("https://news.example.com/article/1")
        self.assertEqual(url, "https://news.example.com/img/hero.jpg")

    def test_logo_like_images_rejected(self):
        from bot import media
        item = Item(id="1", title="t", summary="", url="https://e.com/a",
                    source_id="s", source_name="S")
        item.image = "https://e.com/static/logo.png"
        with mock.patch("bot.media.og_image", return_value="") as og:
            self.assertEqual(media.pick_image_url(item), "")
            og.assert_called_once()

    def test_card_is_valid_jpeg(self):
        from bot import media
        blob = media.make_card("Huawei запустила производство 5-нм чипов", "TechNode")
        self.assertTrue(blob.startswith(b"\xff\xd8"), "должен быть JPEG")
        from PIL import Image
        with Image.open(io.BytesIO(blob)) as im:
            self.assertEqual(im.size, (media.CARD_W, media.CARD_H))

    def test_tiny_images_rejected(self):
        from bot import media
        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGB", (60, 60), (10, 10, 10)).save(buf, "JPEG")
        resp = mock.Mock()
        resp.headers = {"Content-Type": "image/jpeg"}
        resp.raise_for_status = lambda: None
        resp.iter_content = lambda n: iter([buf.getvalue()])
        with mock.patch("bot.media.requests.get", return_value=resp):
            self.assertIsNone(media.fetch_image("https://e.com/small.jpg"))


class TestFilter(unittest.TestCase):
    def setUp(self):
        with mock.patch("bot.sources.rss.requests.get", return_value=_fake_get(RSS)):
            self.items = fetch_rss(SRC)

    def test_prefilter_drops_junk_and_scores(self):
        kept = relevance.prefilter(self.items, log=lambda *a: None)
        titles = [i.title for i in kept]
        self.assertTrue(any("Huawei" in t for t in titles))
        self.assertFalse(any("lottery" in t.lower() for t in titles))
        self.assertFalse(any("bakery" in t.lower() for t in titles))
        self.assertGreater(kept[0].score, 5)          # huawei + smic + chip
        self.assertIn("huawei", kept[0].matched)

    def test_non_china_source_needs_marker(self):
        item = Item(id="x", title="Nvidia ships new GPU", summary="US only.",
                    url="https://e.com/x", source_id="s", source_name="S",
                    china_native=False)
        self.assertFalse(relevance.about_china(item))


class TestDedupe(unittest.TestCase):
    def test_similar_headlines_collapse(self):
        from bot.collect import _dedupe
        with mock.patch("bot.sources.rss.requests.get", return_value=_fake_get(RSS)):
            items = fetch_rss(SRC)
        kept = _dedupe(items[:2], [], 0.55)
        self.assertEqual(len(kept), 1, "две заметки об одном событии должны схлопнуться")

    def test_seen_url_is_skipped(self):
        from bot.collect import _dedupe
        with mock.patch("bot.sources.rss.requests.get", return_value=_fake_get(RSS)):
            items = fetch_rss(SRC)
        seen = [{"id": items[0].id, "title": items[0].title, "url": items[0].url}]
        kept = _dedupe([items[0]], seen, 0.55)
        self.assertEqual(kept, [])

    def test_different_stories_survive(self):
        a = Item(id="1", title="BYD launches solid-state battery pilot line",
                 summary="", url="https://e.com/1", source_id="s", source_name="S")
        b = Item(id="2", title="Alibaba open-sources a new Qwen model",
                 summary="", url="https://e.com/2", source_id="s", source_name="S")
        from bot.collect import _dedupe
        self.assertEqual(len(_dedupe([a, b], [], 0.55)), 2)


class TestRender(unittest.TestCase):
    def test_tags_and_source_link(self):
        data = {"publish": True, "score": 8, "title": "t",
                "text": "Huawei показала чип.", "tags": ["ИИ", "#чипы"]}
        out = render(data, "https://example.com/a?x=1", "TechNode")
        self.assertIn("#ИИ", out)
        self.assertIn("#чипы", out)
        self.assertIn('<a href="https://example.com/a?x=1">Источник: TechNode</a>', out)

    def test_escapes_source_name(self):
        data = {"publish": True, "score": 8, "title": "t", "text": "x", "tags": []}
        out = render(data, "https://e.com", "A & B <news>")
        self.assertIn("A &amp; B &lt;news&gt;", out)


class TestPublishWindow(unittest.TestCase):
    def test_window_and_gap(self):
        from bot import publish
        with mock.patch.dict(publish.PUBLISHING, {"timezone_offset": 3, "window_start": 9,
                                                  "window_end": 22, "min_gap_minutes": 90}):
            with mock.patch("bot.publish.local_now",
                            return_value=datetime(2026, 9, 21, 3, 0)):
                self.assertFalse(publish.in_window())
            with mock.patch("bot.publish.local_now",
                            return_value=datetime(2026, 9, 21, 14, 0)):
                self.assertTrue(publish.in_window())

            recent = {"last_at": util.iso(NOW - timedelta(minutes=10))}
            self.assertFalse(publish.gap_ok(recent))
            old = {"last_at": util.iso(NOW - timedelta(hours=5))}
            self.assertTrue(publish.gap_ok(old))
            self.assertTrue(publish.gap_ok({"last_at": None}))

    def test_daily_limit_counts_today(self):
        from bot import publish
        published = {"items": [{"at": util.iso(NOW)}, {"at": util.iso(NOW - timedelta(days=2))}]}
        with mock.patch.dict(publish.PUBLISHING, {"timezone_offset": 3}):
            self.assertEqual(publish.today_count(published), 1)


class TestTelegramParams(unittest.TestCase):
    """Регрессия: параметр timeout у Telegram не должен подменять таймаут HTTP.

    Из-за этого модерация падала с ValueError ещё до первого запроса.
    """

    def _post(self, captured):
        def fake(url, **kw):
            # urllib3 отвергает неположительный таймаут — воспроизводим проверку
            t = kw.get("timeout")
            if t is None or (isinstance(t, (int, float)) and t <= 0):
                raise ValueError(f"недопустимый таймаут HTTP: {t!r}")
            captured.append((url.rsplit("/", 1)[-1], kw))
            resp = mock.Mock()
            resp.status_code = 200
            resp.json = lambda: {"ok": True, "result": []}
            return resp
        return fake

    def test_get_updates_keeps_http_timeout_positive(self):
        from bot import tg_api
        got = []
        with mock.patch("bot.tg_api.requests.post", side_effect=self._post(got)):
            tg_api.get_updates(0)
        method, kw = got[0]
        self.assertEqual(method, "getUpdates")
        self.assertGreater(kw["timeout"], 0)
        self.assertNotIn("timeout", kw["json"], "короткий опрос не шлёт timeout в Telegram")

    def test_long_poll_sets_both_timeouts(self):
        from bot import tg_api
        got = []
        with mock.patch("bot.tg_api.requests.post", side_effect=self._post(got)):
            tg_api.get_updates(5, poll_seconds=25)
        _, kw = got[0]
        self.assertEqual(kw["json"]["timeout"], 25)
        self.assertGreaterEqual(kw["timeout"], 25)

    def test_other_methods_have_timeout(self):
        from bot import tg_api
        got = []
        with mock.patch("bot.tg_api.requests.post", side_effect=self._post(got)):
            tg_api.answer_callback("cb", "ок")
            tg_api.edit_caption(1, 2, "текст")
        for _, kw in got:
            self.assertGreater(kw["timeout"], 0)


class TestModerationRobustness(unittest.TestCase):
    """Регрессия: просроченный ответ на нажатие не должен отменять само решение.

    Telegram принимает answerCallbackQuery только ~15 минут. Воркер просыпается
    по расписанию и часто опаздывает — решение всё равно обязано примениться.
    """

    def _entry(self, item_id="abc123"):
        return {"id": item_id, "title": "Заголовок", "text": "Текст поста",
                "url": "https://e.com/1", "source_name": "TechNode",
                "mod_is_photo": False, "rewrites": 0}

    def _cb(self, action, item_id="abc123"):
        return {"id": "cb1", "data": f"{action}:{item_id}",
                "message": {"message_id": 7, "chat": {"id": 42}}}

    def test_expired_ack_still_moves_to_approved(self):
        from bot import moderate
        queue = {"items": [self._entry()]}
        approved = {"items": []}
        expired = RuntimeError(
            "Telegram answerCallbackQuery -> Bad Request: query is too old")
        with mock.patch("bot.moderate.answer_callback", side_effect=expired),              mock.patch("bot.moderate.edit_message") as edit:
            moderate.handle_callback(self._cb("q"), queue, approved)
        self.assertEqual(len(queue["items"]), 0, "новость должна уйти из очереди")
        self.assertEqual(len(approved["items"]), 1, "и попасть в одобренные")
        edit.assert_called_once()

    def test_failed_edit_does_not_abort(self):
        from bot import moderate
        queue = {"items": [self._entry()]}
        approved = {"items": []}
        with mock.patch("bot.moderate.answer_callback"),              mock.patch("bot.moderate.edit_message",
                        side_effect=RuntimeError("message can't be edited")):
            moderate.handle_callback(self._cb("q"), queue, approved)
        self.assertEqual(len(approved["items"]), 1)

    def test_delete_works_with_expired_ack(self):
        from bot import moderate
        queue = {"items": [self._entry()]}
        approved = {"items": []}
        with mock.patch("bot.moderate.answer_callback",
                        side_effect=RuntimeError("query is too old")), \
             mock.patch("bot.moderate.edit_message"):
            moderate.handle_callback(self._cb("d"), queue, approved)
        self.assertEqual(queue["items"], [])
        self.assertEqual(approved["items"], [])


class TestUtil(unittest.TestCase):
    def test_similarity(self):
        self.assertGreater(util.similarity("Huawei unveils 5nm chip",
                                           "Huawei presents 5nm chip"), 0.55)
        self.assertLess(util.similarity("BYD battery plant",
                                        "Alibaba cloud earnings"), 0.3)

    def test_chinese_titles_compare(self):
        self.assertGreater(util.similarity("华为发布新款芯片", "华为发布新芯片"), 0.5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
