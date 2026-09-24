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


class TestManualEdit(unittest.TestCase):
    """Ручная правка: тело меняется, ссылка на источник и безопасность сохраняются."""

    def test_split_keeps_source_line(self):
        from bot.moderate import split_body
        text = 'Тело поста.\n\n<a href="https://e.com">Источник: S</a>'
        body, tail = split_body(text)
        self.assertEqual(body, "Тело поста.")
        self.assertIn("Источник: S", tail)

    def test_split_without_source(self):
        from bot.moderate import split_body
        body, tail = split_body("Только тело")
        self.assertEqual((body, tail), ("Только тело", ""))

    def test_sanitize_keeps_simple_tags_only(self):
        from bot.moderate import sanitize
        out = sanitize("<b>жирный</b> и <script>alert(1)</script> и 5 < 7")
        self.assertIn("<b>жирный</b>", out)
        self.assertNotIn("<script>", out)
        self.assertIn("&lt;script&gt;", out)
        self.assertIn("5 &lt; 7", out)


class TestUtil(unittest.TestCase):
    def test_similarity(self):
        self.assertGreater(util.similarity("Huawei unveils 5nm chip",
                                           "Huawei presents 5nm chip"), 0.55)
        self.assertLess(util.similarity("BYD battery plant",
                                        "Alibaba cloud earnings"), 0.3)

    def test_chinese_titles_compare(self):
        self.assertGreater(util.similarity("华为发布新款芯片", "华为发布新芯片"), 0.5)


# ── апгрейд 2026-09: разметка, голос, дедуп по событиям ──────────────────────

class TestHtml(unittest.TestCase):
    def test_unclosed_and_foreign_tags_from_model(self):
        out = util.sanitize_html("<b>Заголовок\n<p>абзац</p> <strong>x</strong>", drop_unknown=True)
        self.assertEqual(out, "<b>Заголовок\nабзац <b>x</b></b>")

    def test_crossed_tags_are_balanced(self):
        self.assertEqual(util.sanitize_html("<b>a<i>b</b>c</i>"), "<b>a<i>b</i></b>c")

    def test_expandable_quote_and_br(self):
        out = util.sanitize_html("<blockquote expandable>▫️ a<br/>▫️ b</blockquote>", True)
        self.assertEqual(out, "<blockquote expandable>▫️ a\n▫️ b</blockquote>")

    def test_nested_quote_flattened(self):
        out = util.sanitize_html("<blockquote>a <blockquote>b</blockquote> c</blockquote>")
        self.assertEqual(out, "<blockquote>a b c</blockquote>")

    def test_entities_not_double_escaped(self):
        self.assertEqual(util.sanitize_html("AT&amp;T и 5 < 7"), "AT&amp;T и 5 &lt; 7")

    def test_visible_len_ignores_tags_counts_emoji_as_two(self):
        self.assertEqual(util.visible_len("<b>🔋 ab</b>"), 5)

    def test_fit_html_never_cuts_a_tag(self):
        out = util.fit_html("<b>" + "слово " * 400 + "</b>", 300)
        self.assertLessEqual(util.visible_len(out), 300)
        self.assertNotIn("<b", out)


class TestVoice(unittest.TestCase):
    def test_prompt_formats_and_asks_for_author_take(self):
        from bot.llm import _system_prompt
        prompt = _system_prompt()
        self.assertIn("<blockquote>💬", prompt)
        self.assertIn("expandable", prompt)
        self.assertNotIn("Сухо, по делу", prompt)

    def test_render_cleans_model_markup(self):
        data = {"publish": True, "score": 8, "title": "t", "tags": ["#ИИ"],
                "text": "🧠 <b>Заголовок\n\n<p>Текст</p><blockquote>💬 мнение"}
        out = render(data, "https://e.com", "S")
        self.assertIn("<b>Заголовок\n\nТекст<blockquote>💬 мнение</blockquote></b>", out)
        self.assertNotIn("<p>", out)


class TestPrefilterWords(unittest.TestCase):
    def test_short_terms_match_whole_words_only(self):
        self.assertFalse(relevance._has("ev", "every review of the union"))
        self.assertFalse(relevance._has("nio", "senior union"))
        self.assertTrue(relevance._has("ev", "china ev sales"))
        self.assertTrue(relevance._has("chip", "chinese chipmaker"))
        self.assertTrue(relevance._has("宇树", "宇树科技发布"))


def _items(*specs):
    out = []
    for i, (source, title) in enumerate(specs):
        out.append(Item(id=f"id{i}", title=title, summary="", url=f"https://e.com/{i}",
                        source_id=source.lower(), source_name=source))
    return out


class TestEvents(unittest.TestCase):
    def _ask(self, rows):
        return lambda system, user: {"items": rows}

    def test_same_event_in_batch_keeps_first(self):
        from bot import events
        items = _items(("TechNode", "Alibaba unveils Zhenwu V900 AI chip"),
                       ("cnBeta", "阿里发布新一代自研AI芯片"),
                       ("CnEVPost", "BYD opens new plant"))
        rows = [{"n": 0, "group": "g1", "seen": None}, {"n": 1, "group": "g1", "seen": None},
                {"n": 2, "group": "g2", "seen": None}]
        kept = events.pick(items, {"items": []}, self._ask(rows), log=lambda *a: None)
        self.assertEqual([i.source_name for i in kept], ["TechNode", "CnEVPost"])

    def test_already_shown_event_is_dropped(self):
        from bot import events
        items = _items(("Pandaily", "Alibaba opens Qwen-Image-2.1"))
        memory = {"items": [{"title": "Alibaba открыла Qwen-Image-2.1", "at": util.iso(NOW)}]}
        rows = [{"n": "N0", "group": "g1", "seen": "M0", "new_facts": False}]
        self.assertEqual(events.pick(items, memory, self._ask(rows), log=lambda *a: None), [])

    def test_new_facts_become_followup(self):
        from bot import events
        items = _items(("CnEVPost", "Xiaomi YU7 deliveries begin"))
        memory = {"items": [{"title": "Xiaomi открыла предзаказ YU7", "at": util.iso(NOW)}]}
        rows = [{"n": 0, "group": "g1", "seen": "M0", "new_facts": True}]
        kept = events.pick(items, memory, self._ask(rows), log=lambda *a: None)
        self.assertEqual(kept[0].followup_of, "Xiaomi открыла предзаказ YU7")
        kept = events.pick(_items(("CnEVPost", "x")), memory, self._ask(rows),
                           followups=False, log=lambda *a: None)
        self.assertEqual(kept, [])

    def test_model_failure_keeps_everything(self):
        from bot import events

        def boom(*a):
            raise RuntimeError("timeout")
        items = _items(("A", "one"), ("B", "two"))
        self.assertEqual(events.pick(items, {"items": []}, boom, log=lambda *a: None), items)

    def test_garbage_answer_keeps_everything(self):
        from bot import events
        items = _items(("A", "one"), ("B", "two"))
        ask = self._ask([{"n": 99, "group": "g"}, "мусор", {"n": 0, "seen": "M7"}])
        self.assertEqual(len(events.pick(items, {"items": []}, ask, log=lambda *a: None)), 2)


class _TmpState:
    """Подменяет папку state/ на временную."""

    def __enter__(self):
        import tempfile
        from bot import store
        self._dir = tempfile.TemporaryDirectory()
        self._patch = mock.patch.object(store, "STATE_DIR", Path(self._dir.name))
        self._patch.start()
        return Path(self._dir.name)

    def __exit__(self, *exc):
        self._patch.stop()
        self._dir.cleanup()


class TestEventMemory(unittest.TestCase):
    def test_backfill_from_existing_state(self):
        from bot import events, store
        with _TmpState():
            store.save("published.json", {"items": [
                {"id": "p1", "title": "Alibaba показала ИИ-чип", "at": util.iso(NOW)},
                {"id": "p2", "title": "Старьё", "at": util.iso(NOW - timedelta(days=40))},
            ], "last_at": None})
            store.save("queue.json", {"items": [
                {"id": "q1", "title": "Xiaomi 18 Pro", "text": "<b>Текст</b>",
                 "created_at": util.iso(NOW)}]})
            memory = events.load(days=10)
        titles = [e["title"] for e in memory["items"]]
        self.assertEqual(titles, ["Alibaba показала ИИ-чип", "Xiaomi 18 Pro"])
        self.assertTrue(memory["backfilled"])


def _fake_grouping(system, user):
    """Модель-заглушка: всё про Alibaba — одно событие, и оно «уже было», если есть в M."""
    rows, memory_hit = [], None
    for line in user.splitlines():
        if line[:2].startswith("M") and line[1:2].isdigit() and "Alibaba" in line:
            memory_hit = line.split(" ", 1)[0]
    for line in user.splitlines():
        if not (line.startswith("N") and line[1:2].isdigit()):
            continue
        n = int(line.split(" ", 1)[0][1:])
        about_alibaba = "alibaba" in line.lower() or "阿里" in line
        rows.append({"n": n, "group": "ali" if about_alibaba else f"g{n}",
                     "seen": memory_hit if about_alibaba else None, "new_facts": False})
    return {"items": rows}


class TestCollectRemembersBatches(unittest.TestCase):
    """Сценарий из жизни: Alibaba Zhenwu V900 пришла пятью карточками из разных изданий."""

    def _run(self, raw):
        from bot import collect

        def fake_post(item, variant_hint="", **kw):
            title = "Alibaba показала ИИ-чип" if "alibaba" in item.title.lower() or \
                "阿里" in item.title else "Unitree открыла веса модели"
            return {"publish": True, "score": 8, "title": title,
                    "text": f"🧠 <b>{title}</b>\n\nТекст.", "tags": ["#ИИ"]}

        env = {"TELEGRAM_BOT_TOKEN": "x", "MODERATOR_CHAT_ID": "1", "DEEPSEEK_API_KEY": "k"}
        with mock.patch.dict("os.environ", env), \
             mock.patch("bot.collect.collect_all", return_value=raw), \
             mock.patch("bot.collect.write_post", side_effect=fake_post), \
             mock.patch("bot.collect.ask_json", side_effect=_fake_grouping), \
             mock.patch("bot.collect.media.pick_image_url", return_value=""), \
             mock.patch("bot.collect.media.resolve", return_value=(None, "")), \
             mock.patch("bot.collect.send_message", return_value={"message_id": 1}) as sm:
            sent = collect.main(limit=5, force=True)
        return sent, sm

    def _item(self, i, source, title, lang="en"):
        return Item(id=f"r{i}", title=title, summary="Chinese tech.",
                    url=f"https://e.com/{source}/{i}", source_id=source, source_name=source,
                    lang=lang, china_native=True, published=NOW)

    def test_one_card_per_event_and_memory_between_batches(self):
        from bot import store
        with _TmpState():
            store.save("queue.json", {"items": []})
            store.save("published.json", {"items": [], "last_at": None})
            first = [
                self._item(1, "TechNode", "Alibaba unveils Zhenwu V900 AI chip with Huawei-class specs"),
                self._item(2, "cnBeta", "阿里拟推超大模型 并发布新一代自研AI芯片", "zh"),
                self._item(3, "Pandaily", "Unitree opens weights of robot model UnifoLM"),
            ]
            sent, sm = self._run(first)
            self.assertEqual(sent, 2, "Alibaba — одна карточка, Unitree — вторая")

            # следующая подборка: то же событие у третьего издания
            second = [self._item(4, "SCMP Tech", "Alibaba chip Zhenwu V900 targets data centres")]
            sent, sm = self._run(second)
            self.assertEqual(sent, 0, "событие уже показывали — второй раз не присылаем")
            sm.assert_not_called()

            memory = store.load("events.json")
            self.assertEqual(len(memory["items"]), 2)


class TestPhotoCard(unittest.TestCase):
    def test_long_head_does_not_cost_the_photo(self):
        from bot import collect
        item = Item(id="x1", title="t", summary="", url="https://e.com", source_id="s",
                    source_name="TechNode")
        post = "<b>Заголовок</b>\n\n" + "слово " * 160          # ~970 видимых символов
        data = {"title": "Очень длинный заголовок для очереди модерации " * 2, "score": 8}
        with mock.patch("bot.collect.send_photo", return_value={"message_id": 5}) as sp:
            msg, is_photo, kind = collect._send_card(item, data, post, b"\xff\xd8", "source")
        self.assertTrue(is_photo)
        self.assertEqual(kind, "source")
        caption = sp.call_args[0][2]
        self.assertLessEqual(util.visible_len(caption), 1024)
        self.assertTrue(caption.endswith(post))


if __name__ == "__main__":
    unittest.main(verbosity=2)
