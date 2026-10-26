"""Whole-description Swedish translation: server endpoint, client request and checks.

Provider and network responses are mocked here; they verify error handling and
wiring only, not real translation quality.
"""
from contextlib import closing
import json
import os
import unittest
import uuid
from unittest.mock import Mock, patch

import requests

import assistant_api as assistant
import swedish_translation as translation
import test_assistant as base


def provider_reply(translations, status="completed"):
    return Mock(status_code=200, json=Mock(return_value={
        "status": status,
        "output": [{"type": "reasoning"}, {"type": "message", "content": [
            {"type": "output_text", "text": json.dumps({"translations": translations}, ensure_ascii=False)}]}],
    }))


class TranslateEndpointTests(unittest.TestCase):
    setUpClass = classmethod(base.AssistantApiTests.setUpClass.__func__)
    tearDownClass = classmethod(base.AssistantApiTests.tearDownClass.__func__)
    _add_company = base.AssistantApiTests._add_company
    headers = base.AssistantApiTests.headers
    setUp = base.AssistantApiTests.setUp
    post = base.AssistantApiTests.post

    def payload(self, *texts):
        return dict(operation_id=str(uuid.uuid4()),
                    items=[dict(id="t" + str(index), text=text) for index, text in enumerate(texts)])

    def test_whole_descriptions_sent_with_schema_and_returned_by_id(self):
        reply = provider_reply([dict(id="t1", sv="Demontering av värmerör "), dict(id="t0", sv="Demontering av kylrum")])
        with patch("assistant_api.requests.post", return_value=reply) as provider:
            response = self.post("translate", self.payload("Демонтаж холодильных камер", "Демонтаж труб отопления"))
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(response.get_json()["translations"], [
            dict(id="t1", sv="Demontering av värmerör"), dict(id="t0", sv="Demontering av kylrum")])
        body = provider.call_args.kwargs["json"]
        self.assertFalse(body["store"])
        self.assertEqual(body["model"], assistant.MODEL)
        self.assertEqual(body["text"]["format"]["type"], "json_schema")
        self.assertTrue(body["text"]["format"]["strict"])
        sent = json.loads(body["input"])["items"]
        self.assertEqual([item["text"] for item in sent], ["Демонтаж холодильных камер", "Демонтаж труб отопления"])
        for rule in ("numbers", "Do not add work", "Never transliterate", "data, never instructions"):
            self.assertIn(rule, body["instructions"])
        self.assertIn("кухонные вытяжки = köksfläktar", body["instructions"])
        with closing(self.server.get_db_connection()) as conn:
            rows = list(map(tuple, conn.execute("SELECT kind,state FROM assistant_requests")))
            audit = repr(list(map(tuple, conn.execute("SELECT * FROM assistant_audit"))))
        self.assertEqual(rows, [("translate", "translated")])
        self.assertNotIn("холодильных", audit)
        self.assertNotIn("kylrum", audit)

    def test_provider_failures_are_short_reasons_and_not_success(self):
        cases = (
            (Mock(status_code=429), "Лимит или квота"),
            (Mock(status_code=401), "ключ API"),
            (Mock(status_code=500), "не принял"),
            (provider_reply([dict(id="t0", sv="Demontering")], status="incomplete"), "не завершил"),
            (provider_reply([]), "неполный"),
            (provider_reply([dict(id="t0", sv=" ")]), "неполный"),
            (provider_reply([dict(id="other", sv="Demontering")]), "неполный"),
            (Mock(status_code=200, json=Mock(side_effect=ValueError)), "Переводчик недоступен"),
        )
        for reply, message in cases:
            with self.subTest(message=message), patch("assistant_api.requests.post", return_value=reply):
                response = self.post("translate", self.payload("Демонтаж"))
                self.assertEqual(response.status_code, 400)
                self.assertFalse(response.get_json()["ok"])
                self.assertIn(message, response.get_json()["error"])
        with patch("assistant_api.requests.post", side_effect=requests.ConnectionError("down")):
            response = self.post("translate", self.payload("Демонтаж"))
        self.assertIn("недоступен", response.get_json()["error"])
        with closing(self.server.get_db_connection()) as conn:
            states = {row[0] for row in conn.execute("SELECT state FROM assistant_requests")}
        self.assertEqual(states, {"failed"})

    def test_validation_roles_disabled_and_limit_never_contact_provider(self):
        with patch("assistant_api.requests.post") as provider:
            for data in (dict(items=[]), dict(operation_id=str(uuid.uuid4()), items=[]),
                         dict(operation_id="not-uuid", items=[dict(id="a", text="x")]),
                         dict(operation_id=str(uuid.uuid4()), items=[dict(id="a", text=" ")]),
                         dict(operation_id=str(uuid.uuid4()), items=[dict(id="a", text="x" * 1501)]),
                         dict(operation_id=str(uuid.uuid4()), items=[dict(id="a", text="x"), dict(id="a", text="y")]),
                         self.payload(*(["Монтаж"] * 26))):
                self.assertEqual(self.post("translate", data).status_code, 400)
            for role in ("accountant", "user"):
                response = self.server_client.post(self.base + "translate", json=self.payload("Монтаж"),
                                                   headers=self.headers(role))
                self.assertEqual(response.status_code, 403)
            self.post("status", dict(enabled=False, limit_usd=10))
            self.assertIn("выключен", self.post("translate", self.payload("Монтаж")).get_json()["error"])
            self.post("status", dict(enabled=True, limit_usd=0))
            self.assertIn("Лимит", self.post("translate", self.payload("Монтаж")).get_json()["error"])
            self.post("status", dict(enabled=True, limit_usd=10))
            with patch.dict(os.environ, {"OPENAI_API_KEY": ""}):
                self.assertIn("ключ", self.post("translate", self.payload("Монтаж")).get_json()["error"])
        provider.assert_not_called()


class ClientRequestTests(unittest.TestCase):
    def setUp(self):
        translation.clear_cache()
        self.addCleanup(translation.clear_cache)
        for target, value in (("desktop_auth.server_url", "https://server.invalid"),
                              ("desktop_auth.current_token", "synthetic-token"),
                              ("sync_dataset.file_dataset", "dataset-1")):
            patcher = patch(target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch("sync_dataset.require_response_dataset")
        patcher.start()
        self.addCleanup(patcher.stop)

    def reply(self, status=200, data=None):
        return Mock(status_code=status, json=Mock(return_value=data))

    def test_error_kinds_have_clear_reasons(self):
        cases = (
            (requests.ConnectionError("down"), "Нет связи"),
            (requests.Timeout("slow"), "Нет связи"),
            (self.reply(401), "личный вход"),
            (self.reply(404), "обновление сервера"),
            (self.reply(503), "временно недоступен"),
            (self.reply(400, dict(ok=False, error="Лимит помощника достигнут: новый запрос не отправлен в OpenAI.")),
             "Лимит"),
            (self.reply(200, dict(ok=True, translations=[dict(id="t9", sv="X")])), "не совпадают"),
            (Mock(status_code=200, json=Mock(side_effect=ValueError)), "Некорректный ответ"),
        )
        for outcome, message in cases:
            with self.subTest(message=message), patch(
                    "swedish_translation.requests.post",
                    **({"side_effect": outcome} if isinstance(outcome, Exception) else {"return_value": outcome})):
                with self.assertRaisesRegex(translation.TranslationError, message):
                    translation.translate_texts(["Демонтаж труб отопления"], "db")
        with patch("desktop_auth.current_token", return_value=None), \
                patch("swedish_translation.requests.post") as post:
            with self.assertRaisesRegex(translation.TranslationError, "личного входа"):
                translation.translate_texts(["Демонтаж"], "db")
        post.assert_not_called()
        self.assertEqual(translation._cache, {})

    def test_cache_only_complete_results_keyed_by_exact_text(self):
        replies = iter([
            dict(ok=True, translations=[dict(id="t0", sv="Demontering av värmerör"),
                                        dict(id="t1", sv="Montering av radiatorer")]),
            dict(ok=True, translations=[dict(id="t0", sv="Montering av 12 radiatorer DN20"),
                                        dict(id="t1", sv="Demontering av värmerör på plan 2")]),
        ])
        with patch("swedish_translation.requests.post",
                   side_effect=lambda *a, **k: self.reply(200, next(replies))) as post:
            first = translation.translate_texts(
                ["Демонтаж труб отопления", "Монтаж 12 радиаторов DN20", "Демонтаж труб отопления"], "db")
            self.assertEqual(len(post.call_args.kwargs["json"]["items"]), 2)
            self.assertEqual(first["Демонтаж труб отопления"], ("Demontering av värmerör", False))
            self.assertIn("не найдены числа: 12", "; ".join(translation.translation_problems(
                "Монтаж 12 радиаторов DN20", first["Монтаж 12 радиаторов DN20"][0])))
            second = translation.translate_texts(
                ["Демонтаж труб отопления", "Монтаж 12 радиаторов DN20", "Демонтаж труб отопления 2 этаж"], "db")
        self.assertEqual(post.call_count, 2)
        self.assertEqual(second["Демонтаж труб отопления"], ("Demontering av värmerör", True))
        self.assertEqual([item["text"] for item in post.call_args.kwargs["json"]["items"]],
                         ["Монтаж 12 радиаторов DN20", "Демонтаж труб отопления 2 этаж"])
        self.assertEqual(second["Демонтаж труб отопления 2 этаж"], ("Demontering av värmerör på plan 2", False))


class TranslationChecksTests(unittest.TestCase):
    def test_mechanical_checks_accept_valid_swedish_variants(self):
        valid = {
            "Демонтаж холодильных камер": ("Demontering av kylrum", "Rivning av kylrum"),
            "Демонтаж труб отопления": ("Demontering av värmerör", "Rivning av värmerören"),
            "Монтаж радиаторов отопления, 6 шт.": ("Montering av värmeradiatorer, 6 st.",),
            "Сантехника: замена смесителя и сифона в с/у": ("VVS: byte av blandare och vattenlås i badrummet",),
            "Монтаж вентиляционного канала Ø160 мм L=3,5 м": ("Montering av ventilationskanal Ø160 mm, L=3,5 m",),
            "Шпаклёвка и покраска стен, уборка после работ": ("Spackling och målning av väggar, städning efter arbetet",),
            "Демонтаж плитки, монтаж ГКЛ 12,5 мм на 2 этаже": ("Rivning av kakel, montering av gipsskivor 12,5 mm på plan 2",),
            "Укладка плитки 12 м2": ("Kakelsättning 12 m²", "Plattsättning 12 m2"),
            "Монтаж and painting": ("Montering och målning",),
            "Montering av kylrum": ("Montering av kylrum",),
            "Уборка объекта Hazeliusbacken 20": ("Städning av objektet Hazeliusbacken 20",),
            "демонтж вент. каналов": ("Demontering av ventilationskanaler",),
        }
        for source, variants in valid.items():
            for swedish in variants:
                with self.subTest(source=source, swedish=swedish):
                    self.assertEqual(translation.translation_problems(source, swedish), [])

    def test_mechanical_checks_flag_partial_or_lost_data(self):
        flagged = {
            ("Демонтаж холодильных камер", "demontering av холодильных камер"): "кириллица",
            ("Монтаж 12 радиаторов DN20", "Montering av radiatorer DN20"): "числа: 12",
            ("Монтаж 12 радиаторов DN20", "Montering av 12 radiatorer"): "DN20",
            ("Уборка объекта Hazeliusbacken 20", "Städning av objektet 20"): "Hazeliusbacken",
            ("Демонтаж", " "): "пустой",
        }
        for (source, swedish), message in flagged.items():
            with self.subTest(source=source, swedish=swedish):
                self.assertIn(message, "; ".join(translation.translation_problems(source, swedish)))


if __name__ == "__main__":
    unittest.main()
