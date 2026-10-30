"""Persistencia de novelas conversacionales."""

import asyncio
import sqlite3
import tempfile
import json
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.config import EngineConfig
from app.registry import ModelRegistry
from app.server import create_app
from app.store import Store
from app.stories import StoryStore
from app.routes import downloads as downloads_route
from app.routes.stories import (
    _filter_unrequested_explicit_tags,
    _refine_story_illustration_prompt,
    _story_chat_system_prompt,
    _story_illustration_prompt,
)


class StoryStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp.name) / "waifu.db"
        conn = sqlite3.connect(self.db_path)
        conn.execute("CREATE TABLE characters (id INTEGER PRIMARY KEY, name TEXT NOT NULL)")
        conn.execute("INSERT INTO characters(id, name) VALUES(1, 'Mika')")
        conn.commit()
        conn.close()
        self.store = StoryStore(self.db_path)
        self.store.init()

    def tearDown(self):
        self.temp.cleanup()

    def test_persists_story_messages_summary_and_illustration_link(self):
        story_id = self.store.create("Noche de verano", 1, "Romance erótico", "Cálido", "nsfw")
        self.store.add_message(story_id, "user", "Hola")
        reply_id = self.store.add_message(story_id, "assistant", "La escena continúa.")
        self.store.update_summary(story_id, "Se conocieron en la playa.", reply_id)
        self.store.link_generation(reply_id, 23)

        story = self.store.get(story_id)
        self.assertEqual(story["summary"], "Se conocieron en la playa.")
        self.assertEqual(story["messages"][1]["generation_id"], 23)
        self.assertEqual(self.store.list()[0]["character_name"], "Mika")

    def test_deleting_story_cascades_to_messages(self):
        story_id = self.store.create("Primera escena", 1, "Romance", "Cálido", "sfw")
        self.store.add_message(story_id, "user", "Hola")

        self.assertTrue(self.store.delete(story_id))
        self.assertIsNone(self.store.get(story_id))
        conn = sqlite3.connect(self.db_path)
        count = conn.execute(
            "SELECT COUNT(*) FROM story_messages WHERE story_id=?", (story_id,)
        ).fetchone()[0]
        conn.close()
        self.assertEqual(count, 0)

    def test_delete_message_invalidates_summary_and_is_scoped_to_story(self):
        story_id = self.store.create("Primera escena", 1, "Romance", "Cálido", "sfw")
        message_id = self.store.add_message(story_id, "user", "Borra este detalle")
        self.store.update_summary(story_id, "Borra este detalle", message_id)

        self.assertFalse(self.store.delete_message(story_id + 1, message_id))
        self.assertTrue(self.store.delete_message(story_id, message_id))
        story = self.store.get(story_id)
        self.assertEqual(story["messages"], [])
        self.assertEqual(story["summary"], "")
        self.assertEqual(story["summarized_through"], 0)

    def test_replacing_message_unlinks_old_illustration(self):
        story_id = self.store.create("Primera escena", 1, "Romance", "Cálido", "sfw")
        message_id = self.store.add_message(story_id, "assistant", "Respuesta original")
        self.store.link_generation(message_id, 15)
        self.store.update_summary(story_id, "Respuesta original", message_id)

        self.assertTrue(self.store.replace_message_content(
            story_id, message_id, "Respuesta nueva", clear_summary=True
        ))
        story = self.store.get(story_id)
        self.assertEqual(story["messages"][0]["content"], "Respuesta nueva")
        self.assertIsNone(story["messages"][0]["generation_id"])
        self.assertEqual(story["summary"], "")


class StoryRouteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.config = EngineConfig(root / "comfy", "http://127.0.0.1:1", root / "data")
        store = Store(self.config.data_dir / "waifu.db")
        store.init()
        self.app = create_app(
            config=self.config, store=store, registry=ModelRegistry(), start_worker=False
        )
        self.character_id = self.app.state.characters.add("Mika", ["adult woman"])

    def tearDown(self):
        self.temp.cleanup()

    def test_nsfw_requires_explicit_adult_confirmation(self):
        with TestClient(self.app) as client:
            missing = client.post("/api/stories", json={
                "character_id": self.character_id, "rating": "nsfw",
            })
            malformed = client.post("/api/stories", json={
                "character_id": self.character_id, "genre": {"unexpected": True},
            })
            accepted = client.post("/api/stories", json={
                "character_id": self.character_id, "rating": "nsfw", "adult_confirmed": True,
            })
        self.assertEqual(missing.status_code, 400)
        self.assertEqual(malformed.status_code, 400)
        self.assertEqual(accepted.status_code, 200)
        self.assertEqual(accepted.json()["rating"], "nsfw")

    def test_character_creator_payload_is_saved_for_story_selection(self):
        with TestClient(self.app) as client:
            created = client.post("/api/characters", json={
                "name": "Luna", "tags": ["adult", "silver hair"],
                "notes": "Edad: 24 años (adulta, 21+).\nPersonalidad: ingeniosa.", "rating": "sfw",
            })
            loaded = client.get(f"/api/characters/{created.json()['id']}")
        self.assertEqual(created.status_code, 200)
        self.assertEqual(loaded.status_code, 200)
        self.assertIn("Edad: 24", loaded.json()["notes"])
        self.assertIn("silver hair", loaded.json()["tags"])

    def test_erotic_genre_does_not_turn_every_chat_turn_into_sexual_scene(self):
        story = {"genre": "Erótico / sexual", "tone": "Cálido", "rating": "nsfw"}
        character = {"name": "Mika", "tags": ["black hair"], "notes": "Adulta, 24 años."}
        system = _story_chat_system_prompt(story, character)
        self.assertIn("no convierte cada turno en una escena sexual", system)
        self.assertIn("no escales la intimidad por iniciativa propia", system)

    def test_illustration_prompt_prioritizes_latest_user_scene_direction(self):
        story = {
            "genre": "Erótico / sexual", "tone": "Cálido", "rating": "nsfw",
            "messages": [
                {"id": 1, "role": "user", "content": "En el parque, realiza sexo oral consensuado a su pareja adulta bajo el farol, plano lateral."},
                {"id": 2, "role": "assistant", "content": "Liza abre sus alas junto al farol antiguo."},
            ],
        }
        character = {"name": "Mika", "tags": ["black hair"], "extras": [], "notes": "Adulta, 24 años."}
        message = story["messages"][1]
        prompt = _story_illustration_prompt(story, character, message)
        self.assertIn("Acción y encuadre solicitados por el usuario", prompt)
        self.assertIn("realiza sexo oral consensuado a su pareja adulta bajo el farol", prompt)
        self.assertIn("plano lateral", prompt)
        self.assertIn("Momento actual narrado por el personaje", prompt)
        self.assertIn("Género y tono generales de la novela", prompt)

    def test_nsfw_rating_alone_does_not_add_explicit_scene_tags(self):
        story = {"rating": "nsfw"}
        prompt = "nsfw, uncensored, 1girl, oral, nipples, nude, from side, sitting"
        ordinary_scene = "Liza está sentada bajo el farol y mira al usuario. Género erótico/sexual."
        filtered = _filter_unrequested_explicit_tags(prompt, story, ordinary_scene)
        self.assertIn("nsfw", filtered)
        self.assertIn("uncensored", filtered)
        self.assertIn("from side", filtered)
        self.assertIn("sitting", filtered)
        self.assertNotIn("oral", filtered)
        self.assertNotIn("nipples", filtered)
        self.assertNotIn("nude", filtered)

    def test_explicit_scene_request_keeps_requested_act_but_not_unrequested_nudity(self):
        story = {"rating": "nsfw"}
        prompt = "nsfw, uncensored, oral, nipples, nude, from side"
        explicit_scene = "El usuario pide sexo oral consensuado bajo el farol, vista lateral."
        filtered = _filter_unrequested_explicit_tags(prompt, story, explicit_scene)
        self.assertIn("oral", filtered)
        self.assertIn("from side", filtered)
        self.assertNotIn("nipples", filtered)
        self.assertNotIn("nude", filtered)

    def test_image_refiner_is_instructed_to_keep_requested_pose_and_camera(self):
        story = {"genre": "Erótico / sexual", "tone": "Cálido", "rating": "nsfw"}
        character = {"name": "Mika", "tags": [], "notes": "Adulta, 24 años."}
        captured = {}

        class AvailableModel:
            def status(self):
                return {"available": True}

        def fake_llm(system, prompt, **kwargs):
            captured["system"] = system
            captured["prompt"] = prompt
            return "1girl, 1boy, oral, from side"

        app = SimpleNamespace(state=SimpleNamespace(
            story_model=AvailableModel(), story_llm=fake_llm, llm=None,
        ))
        result = asyncio.run(_refine_story_illustration_prompt(
            SimpleNamespace(app=app),
            "PRIMARY SCENE ACTION REQUESTED BY USER: sexo oral consensuado, plano lateral",
            story,
            character,
        ))
        self.assertIn("oral", result)
        self.assertIn("from side", result)
        self.assertIn("nsfw", result)
        self.assertIn("uncensored", result)
        self.assertIn("Keep the user's intent exactly as requested", captured["system"])
        self.assertIn("when the user requests it, the explicit act and body parts", captured["system"])
        self.assertIn("sexo oral consensuado, plano lateral", captured["prompt"])

    def test_download_status_endpoint_reports_active_transfer(self):
        with tempfile.TemporaryDirectory() as tmp:
            status_dir = Path(tmp)
            (status_dir / "model.json").write_text(json.dumps({
                "id": "model", "file": "large-model.gguf", "state": "downloading",
                "downloaded": 50, "total": 100, "percent": 50,
                "workers": 8, "speed_mbps": 12.5, "detail": "Descarga paralela en curso",
                "updated_at": time.time(),
            }), encoding="utf-8")
            with patch.object(downloads_route, "STATUS_DIR", status_dir), TestClient(self.app) as client:
                response = client.get("/api/model-downloads")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["active"], 1)
        self.assertEqual(response.json()["items"][0]["percent"], 50)

    def test_message_is_not_saved_when_local_model_is_not_running(self):
        with TestClient(self.app) as client:
            story = client.post("/api/stories", json={"character_id": self.character_id}).json()
            response = client.post(f"/api/stories/{story['id']}/messages", json={"content": "Hola"})
            saved = client.get(f"/api/stories/{story['id']}").json()["messages"]
        self.assertEqual(response.status_code, 503)
        self.assertEqual(saved, [])

    def test_delete_message_route_removes_only_selected_message(self):
        story_id = self.app.state.stories.create("Escena", self.character_id, "Romance", "Cálido", "sfw")
        first_id = self.app.state.stories.add_message(story_id, "user", "Primer turno")
        second_id = self.app.state.stories.add_message(story_id, "assistant", "Respuesta")
        with TestClient(self.app) as client:
            missing = client.delete(f"/api/stories/{story_id}/messages/999")
            deleted = client.delete(f"/api/stories/{story_id}/messages/{first_id}")
            story = client.get(f"/api/stories/{story_id}").json()
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(deleted.status_code, 200)
        self.assertEqual([message["id"] for message in story["messages"]], [second_id])

    def test_regenerate_replaces_assistant_reply_using_preceding_history(self):
        story_id = self.app.state.stories.create("Escena", self.character_id, "Romance", "Cálido", "sfw")
        self.app.state.stories.add_message(story_id, "user", "Abre la puerta")
        assistant_id = self.app.state.stories.add_message(story_id, "assistant", "Respuesta anterior")
        self.app.state.stories.link_generation(assistant_id, 42)
        self.app.state.stories.update_summary(story_id, "Resumen previo", assistant_id)
        self.app.state.story_llm = lambda system, transcript, **kwargs: "Una respuesta distinta."
        with patch.object(self.app.state.story_model, "status", return_value={"available": True}):
            with TestClient(self.app) as client:
                response = client.post(f"/api/stories/{story_id}/messages/{assistant_id}/regenerate", json={})
                story = client.get(f"/api/stories/{story_id}").json()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["message"]["content"], "Una respuesta distinta.")
        self.assertEqual(len(story["messages"]), 2)
        self.assertEqual(story["messages"][1]["content"], "Una respuesta distinta.")
        self.assertIsNone(story["messages"][1]["generation_id"])
        self.assertEqual(story["summary"], "")

    def test_illustration_validates_model_and_links_the_queued_generation(self):
        story_id = self.app.state.stories.create("Escena", self.character_id, "Romance", "Cálido", "sfw")
        message_id = self.app.state.stories.add_message(story_id, "assistant", "Mika sonríe bajo la lluvia.")
        gen_id = self.app.state.store.add("test-image-model", "", status="queued")
        self.app.state.jobs["story-image-job"] = {"gen_id": gen_id}
        with TestClient(self.app) as client:
            invalid = client.post(
                f"/api/stories/{story_id}/messages/{message_id}/illustration", json={"model_id": "missing"}
            )
            self.assertEqual(invalid.status_code, 400)
            with patch("app.routes.stories.api_generate", new=AsyncMock(return_value={"job_id": "story-image-job"})) as generate:
                response = client.post(
                    f"/api/stories/{story_id}/messages/{message_id}/illustration", json={"model_id": "test-image-model"}
                )
                duplicate = client.post(
                    f"/api/stories/{story_id}/messages/{message_id}/illustration", json={"model_id": "test-image-model"}
                )
            persisted = client.get(f"/api/stories/{story_id}").json()["messages"][0]

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["generation_id"], gen_id)
        self.assertEqual(response.json()["image_url"], None)
        self.assertEqual(duplicate.status_code, 409)
        self.assertEqual(persisted["generation_id"], gen_id)
        generate.assert_awaited_once()
        image_payload = generate.await_args.args[1]
        self.assertEqual(image_payload["rating"], "sfw")
        self.assertIn("Momento actual narrado por el personaje", image_payload["prompt"])
        self.assertEqual(image_payload["params"]["width"], 832)
        self.assertEqual(image_payload["params"]["height"], 1216)

    def test_nsfw_illustration_passes_explicit_user_action_and_rating_to_generator(self):
        story_id = self.app.state.stories.create(
            "Escena adulta", self.character_id, "Erótico / sexual", "Cálido", "nsfw"
        )
        self.app.state.stories.add_message(
            story_id, "user", "En el parque, sexo oral consensuado bajo el farol, vista lateral."
        )
        message_id = self.app.state.stories.add_message(
            story_id, "assistant", "La escena adulta continúa junto al farol."
        )
        gen_id = self.app.state.store.add("test-image-model", "", status="queued")
        self.app.state.jobs["nsfw-story-image-job"] = {"gen_id": gen_id}
        captured = {}

        def refine(system, prompt, **kwargs):
            captured["system"] = system
            captured["prompt"] = prompt
            return "1girl, 1boy, oral, from side, park"

        self.app.state.story_llm = refine
        with patch.object(self.app.state.story_model, "status", return_value={"available": True}):
            with patch("app.routes.stories.api_generate", new=AsyncMock(
                return_value={"job_id": "nsfw-story-image-job"}
            )) as generate:
                with TestClient(self.app) as client:
                    response = client.post(
                        f"/api/stories/{story_id}/messages/{message_id}/illustration",
                        json={"model_id": "test-image-model"},
                    )

        self.assertEqual(response.status_code, 200)
        self.assertIn("Acción y encuadre solicitados por el usuario", captured["prompt"])
        self.assertIn("sexo oral consensuado bajo el farol, vista lateral", captured["prompt"])
        payload = generate.await_args.args[1]
        self.assertEqual(payload["rating"], "nsfw")
        self.assertIn("oral", payload["prompt"])
        self.assertIn("from side", payload["prompt"])
        self.assertIn("Keep the user's intent exactly as requested", captured["system"])


if __name__ == "__main__":
    unittest.main()
