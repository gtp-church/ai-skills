"""Offline workflow tests. No real Telegram credentials, session, or messages."""

import asyncio
import importlib.util
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, patch
import urllib.error
import urllib.request

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/telegram_send.py"
spec = importlib.util.spec_from_file_location("telegram_send", SCRIPT)
tg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tg)


def message(mid, text="", *, photo=None, out=False, buttons=None, group=None):
    return NS(id=mid, raw_text=text, photo=photo, document=None, media=photo,
              out=out, sender_id=1 if out else 77, buttons=buttons or [],
              grouped_id=group, entities=[], noforwards=False)


def plan():
    return {"date": "07.11", "topic": "Любовь & верность <Бога>", "target": "me",
            "songs": [{"number": "35", "title": "В тихий вечер", "new": False},
                      {"number": "149", "title": "Его милость больше", "new": True}]}


class FastReader(tg.BotReader):
    async def wait(self, floor, selector, description, settle=0):
        result = selector(await self.messages(floor))
        if not result:
            raise tg.WorkflowError(description)
        return result


class BotFixture:
    def __init__(self, missing=False, foreign=False):
        self.history = [message(1, "Старая песня (35)"), message(2, photo=NS(id=10))]
        self.counter, self.mode, self.calls = 100, None, []
        self.missing, self.foreign = missing, foreign

    def add(self, text="", **kwargs):
        self.counter += 1
        result = message(self.counter, text, **kwargs)
        self.history.append(result)
        return result

    async def send_message(self, peer, text, **kwargs):
        self.calls.append(text)
        outgoing = self.add(text, out=True)
        if text.startswith("/"):
            self.mode = text
            self.add("Введи номер гимна или список номеров через пробел")
        elif self.mode == "/info":
            self.add("В тихий вечер (35)\nТемы: доверие")
            self.add("Его милость больше (149)\nТональность: C")
            self.add(photo=NS(id=99))  # A different type of media is ignored for info.
            self.add("Звук не скачался: В тихий вечер — послушай по ссылке в названии")
            if self.foreign:
                self.add("another manual request", out=True)
        else:
            question = self.add("В каком виде прислать аккорды?")

            async def click(data):
                self.calls.append(data)
                if data == b"chords:full":
                    question.buttons = [[NS(data=b"chordmode:full:default")]]
                else:
                    question.buttons = []
                    self.add(photo=NS(id=350), group=800)
                    if self.missing:
                        self.add("Для этого гимна такого материала пока нет")
                    else:
                        self.add(photo=NS(id=1490), group=800)

            question.buttons, question.click = [[NS(data=b"chords:full")]], click
        return outgoing

    async def iter_messages(self, peer, min_id, **kwargs):
        for msg in list(self.history):
            if msg.id > min_id:
                yield msg


class InputTests(unittest.TestCase):
    def read(self, data):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "input.json"
            path.write_text(json.dumps(data))
            return tg.read_plan(path)

    def test_html_escapes_content_and_keeps_numbers_separate(self):
        body = tg.announcement(self.read(plan()))
        self.assertIn("[<code>35</code>]", body)
        self.assertIn("<code>35 149</code>", body)
        self.assertIn("Любовь &amp; верность &lt;Бога&gt;", body)
        self.assertEqual(body.count("🆕"), 1)

    def test_intentional_repeats_are_not_deduplicated(self):
        data = plan()
        data["songs"].append(data["songs"][0].copy())
        self.assertIn("<code>35 149 35</code>", tg.announcement(self.read(data)))

    def test_rejects_more_than_bot_limit_and_unknown_numbers(self):
        for value in ("035", "3 5", "３５", "#35", "", -1):
            with self.subTest(value=value):
                data = plan()
                data["songs"][0]["number"] = value
                with self.assertRaises(tg.WorkflowError):
                    self.read(data)
        data = plan()
        data["songs"] *= 6
        with self.assertRaises(tg.WorkflowError):
            self.read(data)

    def test_requires_explicit_target_and_boolean_markers(self):
        data = plan()
        data.pop("target")
        with self.assertRaises(tg.WorkflowError):
            self.read(data)
        data = plan()
        data["songs"][0]["new"] = "false"
        with self.assertRaises(tg.WorkflowError):
            self.read(data)

    def test_private_state_and_exclusive_lock(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"MCY_MUSIC_TELEGRAM_HOME": tmp}):
            root = tg.state_dir()
            path = root / "config.json"
            tg.save_json(path, {"test": True})
            self.assertEqual(json.loads(path.read_text()), {"test": True})
            if os.name != "nt":
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(root.stat().st_mode & 0o777, 0o700)
            with tg.account_lock(root):
                with self.assertRaises(tg.WorkflowError):
                    with tg.account_lock(root):
                        pass

    def test_proxy_urls_are_validated_and_credentials_decoded(self):
        self.assertEqual(tg.proxy_tuple("socks5://u%40:p%3A@127.0.0.1:10808"), ("socks5", "127.0.0.1", 10808, True, "u@", "p:"))
        with self.assertRaises(tg.WorkflowError):
            tg.proxy_tuple("socks5://127.0.0.1")

    def test_login_form_is_loopback_and_rejects_cross_origin(self):
        server = tg.LoginServer()
        url = server.start()
        try:
            with urllib.request.urlopen(url + "/state") as response:
                self.assertEqual(json.load(response)["phase"], "connecting")
            request = urllib.request.Request(url + "/action", data=b'{}', method="POST", headers={"Origin": "https://elsewhere.example"})
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(request)
            self.assertEqual(error.exception.code, 403)
            error.exception.close()
            self.assertTrue(server.actions.empty())
            with self.assertRaises(urllib.error.HTTPError) as missing:
                urllib.request.urlopen(url.replace(server.token, "/wrong-token") + "/state")
            missing.exception.close()
        finally:
            server.close()


class BotFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_both_requests_use_all_numbers_once_and_ignore_history(self):
        client = BotFixture()
        info, chords = await FastReader(client, NS(id=77)).materials(["35", "149"])
        self.assertEqual(client.calls, ["/info", "35 149", "/chords", "35 149", b"chords:full", b"chordmode:full:default"])
        self.assertEqual([n for m in info for n in tg.bot_numbers(m)], ["35", "149"])
        self.assertEqual(len(chords), 2)
        self.assertEqual({m.grouped_id for m in chords}, {800})
        self.assertTrue(all(m.id > 100 for m in info + chords))

    async def test_missing_chords_fail_instead_of_forwarding_partial_album(self):
        with self.assertRaisesRegex(tg.WorkflowError, "материала пока нет"):
            await FastReader(BotFixture(missing=True), NS(id=77)).materials(["35", "149"])

    async def test_foreign_outgoing_request_stops_bot_flow(self):
        with self.assertRaisesRegex(tg.WorkflowError, "другой запрос"):
            await FastReader(BotFixture(foreign=True), NS(id=77)).materials(["35", "149"])

    async def test_timeout_is_bounded(self):
        client = BotFixture()
        reader = tg.BotReader(client, NS(id=77), timeout=0)
        with self.assertRaisesRegex(tg.WorkflowError, "Не дождались"):
            await reader.wait(0, lambda ms: [], "test")


class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.job_id = "a" * 32
        self.messages = [message(10, "Title (35)"), message(11, photo=NS(id=1)), message(12, photo=NS(id=2))]
        self.job = {"id": self.job_id, "created_at": tg.time.time(), "account_id": 1,
                    "target": {"peer_id": 1}, "bot_id": 77, "html": "hello",
                    "batches": [[{"id": m.id, "hash": tg.fingerprint(m)} for m in self.messages[:1]],
                                [{"id": m.id, "hash": tg.fingerprint(m)} for m in self.messages[1:]]],
                    "steps": [{"kind": kind, "state": "pending", "message_ids": [], "random_ids": list(range(n))} for kind, n in (("announcement", 1), ("info", 1), ("chords", 2))]}
        self.path = self.root / (self.job_id + ".json")
        tg.save_json(self.path, self.job)
        self.client = NS(get_me=AsyncMock(return_value=NS(id=1)),
                         get_entity=AsyncMock(side_effect=lambda value: NS(id=value)),
                         get_messages=AsyncMock(side_effect=self.get_messages))
        self.peer_patch = patch.object(tg, "peer_snapshot", side_effect=lambda entity: {"peer_id": entity.id})
        self.peer_patch.start()
        self.addCleanup(self.peer_patch.stop)

    async def get_messages(self, bot, ids):
        return [next((m for m in self.messages if m.id == mid), None) for mid in ids]

    async def test_complete_delivery_and_repeat_is_noop(self):
        sender = AsyncMock(side_effect=[[NS(id=100)], [NS(id=101)], [NS(id=102), NS(id=103)]])
        with patch.object(tg, "send_personal", sender):
            result = await tg.deliver(self.client, self.root, self.job_id)
            self.assertTrue(result["sent"])
            self.assertEqual(sender.await_count, 3)
            repeat = await tg.deliver(self.client, self.root, self.job_id)
            self.assertTrue(repeat["already_sent"])
            self.assertEqual(sender.await_count, 3)
        self.assertEqual([s["state"] for s in json.loads(self.path.read_text())["steps"]], ["sent"] * 3)

    async def test_changed_source_blocks_all_target_side_effects(self):
        self.messages[0].raw_text = "edited after preview"
        with patch.object(tg, "send_personal", AsyncMock()) as sender:
            with self.assertRaisesRegex(tg.WorkflowError, "изменились"):
                await tg.deliver(self.client, self.root, self.job_id)
            sender.assert_not_awaited()

    async def test_lost_send_response_never_blindly_duplicates(self):
        sender = AsyncMock(side_effect=[[NS(id=100)], ConnectionError("response lost")])
        with patch.object(tg, "send_personal", sender):
            with self.assertRaises(ConnectionError):
                await tg.deliver(self.client, self.root, self.job_id)
            states = [s["state"] for s in json.loads(self.path.read_text())["steps"]]
            self.assertEqual(states, ["sent", "uncertain", "pending"])
            with self.assertRaisesRegex(tg.WorkflowError, "неизвестен результат"):
                await tg.deliver(self.client, self.root, self.job_id)
            self.assertEqual(sender.await_count, 2)

    async def test_partial_forward_result_is_not_reported_as_complete(self):
        sender = AsyncMock(side_effect=[[NS(id=100)], [NS(id=101)], [NS(id=102)]])
        with patch.object(tg, "send_personal", sender):
            with self.assertRaisesRegex(tg.WorkflowError, "неполный результат"):
                await tg.deliver(self.client, self.root, self.job_id)
        self.assertEqual(json.loads(self.path.read_text())["steps"][2]["state"], "uncertain")

    async def test_another_account_cannot_send_prepared_job(self):
        self.client.get_me.return_value = NS(id=2)
        with patch.object(tg, "send_personal", AsyncMock()) as sender:
            with self.assertRaisesRegex(tg.WorkflowError, "Аккаунт отличается"):
                await tg.deliver(self.client, self.root, self.job_id)
            sender.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
