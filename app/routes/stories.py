"""API para historias, chat e ilustraciones asociadas."""

from __future__ import annotations

import os
import asyncio
import json
import re
import base64
from pathlib import Path

import secrets

from fastapi import APIRouter, Body, Request
from fastapi.responses import JSONResponse

from app.enhancer import enhance as enhance_image_prompt
from app.engine import EngineError
from app.prompt_zones import POSES_SEXUALES_TAGS
from app.routes.image import api_generate

router = APIRouter()
GENRES = {"Romance", "Romance erótico", "Erótico / sexual", "Aventura", "Fantasía", "Misterio", "Drama"}
TONES = {"Cálido", "Cinematográfico", "Ligero", "Melancólico", "Intenso"}
_EXPLICIT_ACT_RE = re.compile(
    r"\b(?:sexo|sex|oral sex|sexo oral|fellatio|felaci[oó]n|cunnilingus|anilingus|"
    r"vaginal sex|anal sex|penetraci[oó]n|penetration|masturbaci[oó]n|masturbation|"
    r"deepthroat|irrumatio|paizuri|handjob|blowjob|licking penis|chup(?:ar|ando) el pene)\b",
    re.IGNORECASE,
)
_EXPOSURE_TAGS = {
    "nude", "completely nude", "naked", "topless", "bottomless", "nipples",
    "areola", "breasts out", "bare breasts", "no panties", "pussy", "vagina",
    "vulva", "penis", "genitals", "clitoris", "pubic hair",
}
_EXPOSURE_REQUESTS: dict[str, re.Pattern[str]] = {
    "nude": re.compile(r"\b(?:nude|naked|desnud[oa]|sin ropa|completamente desnuda?)\b", re.I),
    "completely nude": re.compile(r"\b(?:completamente desnuda?|fully nude|completely nude)\b", re.I),
    "naked": re.compile(r"\b(?:naked|desnud[oa]|sin ropa)\b", re.I),
    "topless": re.compile(r"\b(?:topless|sin (?:sost[eé]n|brasier|parte de arriba)|pechos al aire|tetas al aire)\b", re.I),
    "bottomless": re.compile(r"\b(?:bottomless|sin bragas|sin ropa interior)\b", re.I),
    "nipples": re.compile(r"\b(?:nipples?|pezones|pez[oó]n|tetas al aire|pechos al aire)\b", re.I),
    "areola": re.compile(r"\b(?:areola|areolas)\b", re.I),
    "breasts out": re.compile(r"\b(?:breasts? out|pechos al aire|tetas al aire)\b", re.I),
    "bare breasts": re.compile(r"\b(?:bare breasts?|pechos desnudos?|tetas desnudas?)\b", re.I),
    "no panties": re.compile(r"\b(?:no panties|sin bragas|sin ropa interior)\b", re.I),
    "pussy": re.compile(r"\b(?:pussy|co[oñ]o|vulva|vagina)\b", re.I),
    "vagina": re.compile(r"\b(?:vagina|vaginal|co[oñ]o)\b", re.I),
    "vulva": re.compile(r"\b(?:vulva|vagina|co[oñ]o)\b", re.I),
    "penis": re.compile(r"\b(?:penis|pene|miembro)\b", re.I),
    "genitals": re.compile(r"\b(?:genitals?|genitales|pene|vagina|vulva|co[oñ]o)\b", re.I),
    "clitoris": re.compile(r"\b(?:clitoris|cl[ií]toris)\b", re.I),
    "pubic hair": re.compile(r"\b(?:pubic hair|vello p[uú]bico)\b", re.I),
}


def _filter_unrequested_explicit_tags(tags: str, story: dict, prompt_context: str) -> str:
    """Keep NSFW as a rating; include sex/exposure only when the scene asks for it."""
    is_nsfw = story.get("rating") == "nsfw"
    allow_explicit_act = is_nsfw and bool(_EXPLICIT_ACT_RE.search(prompt_context))
    allow_exposure = {
        tag for tag, pattern in _EXPOSURE_REQUESTS.items()
        if is_nsfw and pattern.search(prompt_context)
    }
    act_tags = {tag.casefold() for tag in POSES_SEXUALES_TAGS}
    kept = []
    for raw_tag in tags.split(","):
        tag = raw_tag.strip()
        if not tag:
            continue
        core = tag.casefold()
        if core.startswith("(") and ":" in core:
            core = core[1:].rsplit(":", 1)[0].strip()
        if not allow_explicit_act and core in act_tags:
            continue
        if core in _EXPOSURE_TAGS and core not in allow_exposure:
            continue
        kept.append(tag)
    return ", ".join(kept)


def _story_identity_prompt(story: dict, character: dict) -> str:
    """Shared novel/character instructions used by both chat and image prompting."""
    tags = ", ".join(character.get("tags") or [])
    notes = character.get("notes") or ""
    base = (
        f"Ficción interactiva de {story['genre']} en español; tono {story['tone']}. "
        f"Personaje central: {character['name']}. Rasgos: {tags}. Perfil: {notes}. "
        "Todos los personajes románticos o íntimos son adultos ficticios de 21 años o más; "
        "no incluyas menores. Toda intimidad debe ser consensuada y deseada."
    )
    if story["rating"] == "sfw":
        base += " Clasificación SFW: sin sexualidad explícita; personajes vestidos."
    else:
        base += (
            " Clasificación NSFW: se permite erotismo adulto consensuado cuando la escena lo pide. "
            "El género orienta los temas, pero no convierte cada turno en una escena sexual; "
            "sigue la acción concreta del usuario y no escales la intimidad por iniciativa propia."
        )
    return base


def _story_chat_system_prompt(story: dict, character: dict) -> str:
    return (
        _story_identity_prompt(story, character) + " "
        f"Eres {character['name']}. Mantén en cada respuesta el género {story.get('genre', 'de la historia')} "
        f"y el estilo {story.get('tone', 'establecido')}: adapta vocabulario, ritmo y atmósfera a ambos, sin cambiar de género ni tono por iniciativa propia. "
        "Estructura cada respuesta en cuatro bloques separados, con estos rótulos exactos y en este orden: "
        "Diálogo: lo que el personaje dice en voz alta; Acción: solo movimientos y acciones observables del personaje; "
        "Pensamiento: su estado interno, claramente privado; Escenario: lugar, ambiente y elementos visibles del entorno. "
        "Escribe 1 o 2 frases breves por bloque; no mezcles pensamientos con diálogo o acción ni pongas acciones del usuario como hechos consumados. "
        "Mantén continuidad con la conversación y responde directamente a la última indicación del usuario, que tiene prioridad sobre cualquier resumen o dirección anterior. "
        "Cuando el usuario proponga una acción o un cambio de escena, haz que la respuesta avance esa acción y sitúe al personaje en esa escena; no la sustituyas por una escena genérica. "
        "No decidas acciones, pensamientos ni diálogos por el usuario. "
        "La escena es ficción y respeta el perfil establecido del personaje."
    )


_STORY_RESPONSE_SECTION_RE = re.compile(
    r"(?im)^\s*(?:\*\*)?(diálogo|dialogo|acción|accion|pensamiento|escenario)(?:\*\*)?\s*:\s*"
)


def _story_response_sections(content: str) -> dict[str, str]:
    """Parse labeled response blocks; older unstructured messages remain supported."""
    sections: dict[str, str] = {}
    matches = list(_STORY_RESPONSE_SECTION_RE.finditer(str(content or "")))
    for index, match in enumerate(matches):
        key = match.group(1).casefold()
        key = {"dialogo": "diálogo", "accion": "acción"}.get(key, key)
        end = matches[index + 1].start() if index + 1 < len(matches) else len(content)
        sections[key] = str(content)[match.end():end].strip(" \t\r\n*_")
    return sections


async def _generate_story_opening(
    request: Request, story: dict, character: dict, opening_prompt: str = ""
) -> str | JSONResponse:
    """Create and persist the first in-character message without a synthetic user turn."""
    llm = request.app.state.story_llm or request.app.state.llm
    if not callable(llm):
        return JSONResponse(status_code=503, content={"error": "Configura el modelo conversacional para iniciar la historia"})
    external = bool(os.environ.get("WAIFU_STORY_LLM_URL", "").strip())
    try:
        model_available = external or bool(request.app.state.story_model.status().get("available"))
    except Exception:
        model_available = external
    if not model_available:
        return JSONResponse(status_code=503, content={"error": "Inicia el modelo conversacional para generar la escena de bienvenida"})
    system = _story_chat_system_prompt(story, character)
    premise = opening_prompt.strip() or (
        "Inventa una apertura breve y coherente a partir del perfil del personaje, el género y el tono. "
        "Sitúa inmediatamente una escena concreta y deja al usuario espacio para intervenir."
    )
    transcript = (
        "INICIO DE LA NOVELA. Escribe el primer mensaje del personaje: presenta una situación "
        "inmersiva con acciones observables y detalles del escenario, habla con la voz de su perfil "
        "y termina con una invitación o pregunta abierta. No escribas un mensaje del usuario ni "
        "decidas sus acciones.\nPremisa o escena inicial solicitada:\n"
        f"{premise}\n\n{character['name']}:"
    )
    try:
        try:
            greeting = await asyncio.to_thread(
                llm, system, transcript, temperature=0.85, max_tokens=500
            )
        except TypeError:
            greeting = await asyncio.to_thread(llm, system, transcript)
    except Exception as exc:
        return JSONResponse(status_code=503, content={"error": f"No se pudo generar la escena de bienvenida: {exc}"})
    if not isinstance(greeting, str) or not greeting.strip():
        return JSONResponse(status_code=502, content={"error": "El modelo devolvió una apertura vacía"})
    request.app.state.stories.add_message(
        story["id"], "assistant", greeting.strip()[:6000]
    )
    return greeting.strip()[:6000]


def _story_illustration_prompt(story: dict, character: dict, message: dict, image_request: str = "") -> str:
    """Compose an image prompt from the selected scene and its story continuity."""
    messages = story.get("messages") or []
    try:
        target_id = int(message.get("id", 0))
    except (TypeError, ValueError):
        target_id = 0
    before_scene = []
    for item in messages:
        try:
            item_id = int(item.get("id", 0))
        except (TypeError, ValueError):
            continue
        if not target_id or item_id <= target_id:
            before_scene.append(item)
    previous_user = next(
        (item for item in reversed(before_scene) if item.get("role") == "user"),
        None,
    )
    traits = ", ".join(str(tag).strip() for tag in character.get("tags", []) if str(tag).strip())
    extras = ", ".join(str(tag).strip() for tag in character.get("extras", []) if str(tag).strip())
    notes = str(character.get("notes") or "").strip()[:500]
    scene_direction = str(previous_user.get("content") or "").strip()[:1200] if previous_user else ""
    response_sections = _story_response_sections(str(message.get("content") or ""))
    action_description = response_sections.get("acción", "")
    setting_description = response_sections.get("escenario", "")
    if not response_sections:
        # Backward compatibility for assistant messages created before labeled blocks.
        action_description = str(message.get("content") or "").strip()[:900]
    parts = [
        "Ilustración anime de una sola escena coherente para novela visual.",
        f"Acción y encuadre solicitados por el usuario: {scene_direction}" if scene_direction else "",
        f"Petición visual adicional del usuario: {image_request[:700]}" if image_request else "",
        f"Momento actual narrado por el personaje — acciones visibles: {action_description[:700]}" if action_description else "",
        f"Escenario y entorno visible: {setting_description[:500]}" if setting_description else "",
        f"Personaje principal adulto: {character['name']}; conservar diseño y rasgos: {traits}" if traits else f"Personaje principal adulto: {character['name']}; conservar su diseño.",
        f"Ropa y accesorios establecidos del personaje: {extras}" if extras else "",
        f"Perfil del personaje para mantener identidad: {notes}" if notes else "",
        f"Género y tono generales de la novela: {story.get('genre', '')}, {story.get('tone', '')}; úsalos solo como contexto.",
        "Todos los personajes íntimos son adultos ficticios de 21 años o más y toda interacción sexual es consensuada.",
        "Ilustra únicamente acciones observables y elementos visibles del escenario. No dibujes pensamientos, monólogo interno ni diálogo; no añadas bocadillos, texto o narración escrita.",
        "Usa tags concretos, si están presentes o se desprenden directamente de la escena, para rasgos, ropa, accesorios, acción, poses, expresión, cámara, fondo y escenario; poses sexuales o sexys solo si la conversación las pide.",
        "La clasificación NSFW solo clasifica la historia: no significa que esta escena deba ser sexual o desnuda. Si no se pide desnudez, conserva la ropa descrita; si no se describe, muestra al personaje vestido. No infieras sexo por el género erótico. Cuando sí se pida una acción adulta consensuada, represéntala directamente con participantes, pose, contacto, orientación corporal y encuadre visibles; no la sustituyas por una pose sensual genérica, un beso o un primer plano del rostro. La petición visual adicional manda si existe. No añadir gente ajena a la escena ni texto.",
    ]
    return ", ".join(part for part in parts if part)


def _story_character_visual_anchor(character: dict) -> str:
    """Return the saved visual identity separately so prompt refinement cannot drop it."""
    tags = [str(tag).strip() for tag in character.get("tags", []) if str(tag).strip()]
    extras = [str(tag).strip() for tag in character.get("extras", []) if str(tag).strip()]
    notes = str(character.get("notes") or "")
    match = re.search(
        r"\[DISEÑO_VISUAL_FIJO\]\s*([\s\S]*?)\s*\[/DISEÑO_VISUAL_FIJO\]",
        notes,
        re.IGNORECASE,
    )
    visual_design = match.group(1).strip() if match else ""
    if not visual_design:
        appearance = re.search(r"(?:^|\n)Apariencia:\s*([^\n]+)", notes, re.IGNORECASE)
        visual_design = appearance.group(1).strip() if appearance else ""
    identity = ", ".join(part for part in [*tags, *extras, visual_design] if part)
    if not identity:
        return ""
    return (
        f"FIXED CHARACTER DESIGN — {character.get('name', 'main character')}: {identity}. "
        "Preserve these exact defining appearance traits; do not redesign, recolor, replace, "
        "or add distinguishing features to this character."
    )


async def _refine_story_illustration_prompt(request: Request, prompt: str, story: dict, character: dict) -> str:
    """Use the image prompt planner to turn story intent into canonical visual tags."""
    external = bool(os.environ.get("WAIFU_STORY_LLM_URL", "").strip())
    try:
        available = external or bool(request.app.state.story_model.status().get("available"))
    except Exception:
        available = external
    llm = request.app.state.story_llm or request.app.state.llm
    if not available or not callable(llm):
        return prompt
    try:
        result = await asyncio.to_thread(
            enhance_image_prompt,
            prompt,
            family="anima",
            preprompt="ninguno",
            rating=story.get("rating"),
            llm=llm,
            strength="fiel",
        )
    except Exception:
        return prompt
    refined = result.get("positive") if isinstance(result, dict) else None
    if not isinstance(refined, str) or not refined.strip():
        return prompt
    refined = _filter_unrequested_explicit_tags(refined, story, prompt)
    return refined.strip()[:3000] if refined.strip() else prompt


def _message_json(message: dict, store, jobs: dict | None = None) -> dict:
    item = dict(message)
    item["image_url"] = None
    generation_id = item.get("generation_id")
    if generation_id:
        generation = store.get(generation_id)
        item["illustration_status"] = generation.get("status") if generation else "missing"
        if generation and generation.get("status") in ("queued", "running"):
            item["image_job_id"] = next(
                (job_id for job_id, job in (jobs or {}).items() if job.get("gen_id") == generation_id),
                None,
            )
        else:
            item["image_job_id"] = None
        if generation and generation.get("status") == "done" and generation.get("outputs"):
            item["image_url"] = f"/media/{generation_id}/{generation['outputs'][0]}"
    else:
        item["illustration_status"] = None
    return item


@router.get("/api/stories")
async def list_stories(request: Request) -> list[dict]:
    return request.app.state.stories.list()


@router.get("/api/stories/model")
async def story_model_status(request: Request) -> dict:
    external = os.environ.get("WAIFU_STORY_LLM_URL", "").strip()
    if external:
        return {"state": "external", "available": True, "files_ready": True,
                "progress": 100, "detail": f"Proveedor configurado: {external}",
                "model": os.environ.get("WAIFU_STORY_LLM_MODEL", "modelo externo"),
                "size": "", "threads": 0, "context": 0}
    return request.app.state.story_model.status()


@router.post("/api/stories/model/start")
async def start_story_model(request: Request) -> dict:
    if os.environ.get("WAIFU_STORY_LLM_URL", "").strip():
        return await story_model_status(request)
    return request.app.state.story_model.start()


@router.post("/api/stories/model/stop")
async def stop_story_model(request: Request) -> dict:
    if not os.environ.get("WAIFU_STORY_LLM_URL", "").strip():
        request.app.state.story_model.stop()
    return await story_model_status(request)


@router.get("/api/stories/image-model")
async def story_image_model_status(request: Request) -> dict:
    return request.app.state.story_image_model.status()


@router.post("/api/stories/image-model/start")
async def start_story_image_model(request: Request) -> dict:
    return request.app.state.story_image_model.start()


@router.post("/api/stories")
async def create_story(request: Request, payload: dict = Body(...)) -> dict:
    chars = request.app.state.characters
    raw_char_id = payload.get("character_id")
    try:
        if isinstance(raw_char_id, bool):
            raise ValueError
        char_id = int(raw_char_id)
    except (TypeError, ValueError):
        raise EngineError("Selecciona un personaje")
    character = chars.get(char_id)
    if character is None:
        raise EngineError("Personaje desconocido")
    genre = payload.get("genre", "Romance")
    tone = payload.get("tone", "Cálido")
    rating = payload.get("rating", "sfw")
    if not isinstance(genre, str) or genre not in GENRES or not isinstance(tone, str) or tone not in TONES or not isinstance(rating, str) or rating not in ("sfw", "nsfw"):
        raise EngineError("Género, tono o clasificación inválidos")
    if rating == "nsfw" and payload.get("adult_confirmed") is not True:
        raise EngineError("Confirma que eres mayor de edad y que los personajes tienen 21 años o más")
    raw_title = payload.get("title", "")
    if not isinstance(raw_title, str):
        raise EngineError("El título debe ser texto")
    title = (raw_title.strip() or f"Historia con {character['name']}")[:100]
    opening_prompt = payload.get("opening_prompt", "")
    if not isinstance(opening_prompt, str) or len(opening_prompt.strip()) > 1200:
        raise EngineError("La escena inicial debe ser texto de hasta 1200 caracteres")
    story_id = request.app.state.stories.create(title, char_id, genre, tone, rating)
    opening_generated = False
    opening_error = None
    llm = request.app.state.story_llm or request.app.state.llm
    external = bool(os.environ.get("WAIFU_STORY_LLM_URL", "").strip())
    try:
        model_available = external or bool(request.app.state.story_model.status().get("available"))
    except Exception:
        model_available = external
    if callable(llm) and model_available:
        opening = await _generate_story_opening(
            request,
            {"id": story_id, "genre": genre, "tone": tone, "rating": rating},
            character,
            opening_prompt.strip(),
        )
        if isinstance(opening, JSONResponse):
            try:
                opening_error = json.loads(opening.body.decode("utf-8")).get("error")
            except Exception:
                opening_error = "No se pudo generar la escena inicial."
        else:
            opening_generated = True
    elif not callable(llm) or not model_available:
        opening_error = "Inicia el modelo conversacional para generar el saludo inicial."
    result = request.app.state.stories.get(story_id)
    result["opening_generated"] = opening_generated
    result["opening_error"] = opening_error
    return result


@router.post("/api/stories/{story_id}/opening")
async def generate_story_opening(story_id: int, request: Request, payload: dict = Body(default={})) -> Any:
    story = request.app.state.stories.get(story_id)
    if story is None:
        return JSONResponse(status_code=404, content={"error": "Historia desconocida"})
    if story["messages"]:
        return JSONResponse(status_code=409, content={"error": "La historia ya tiene mensajes"})
    opening_prompt = payload.get("opening_prompt", "")
    if not isinstance(opening_prompt, str) or len(opening_prompt.strip()) > 1200:
        return JSONResponse(status_code=400, content={"error": "La escena inicial debe ser texto de hasta 1200 caracteres"})
    character = request.app.state.characters.get(story["character_id"])
    if character is None:
        return JSONResponse(status_code=409, content={"error": "El personaje de esta historia ya no existe"})
    greeting = await _generate_story_opening(request, story, character, opening_prompt)
    if isinstance(greeting, JSONResponse):
        return greeting
    refreshed = request.app.state.stories.get(story_id)
    message = refreshed["messages"][-1]
    return {"message": _message_json(message, request.app.state.store, request.app.state.jobs)}


@router.get("/api/stories/{story_id}")
async def get_story(story_id: int, request: Request) -> dict:
    story = request.app.state.stories.get(story_id)
    if story is None:
        return JSONResponse(status_code=404, content={"error": "Historia desconocida"})
    story["messages"] = [
        _message_json(m, request.app.state.store, request.app.state.jobs)
        for m in story["messages"]
    ]
    return story


@router.delete("/api/stories/{story_id}")
async def delete_story(story_id: int, request: Request) -> dict:
    if not request.app.state.stories.delete(story_id):
        return JSONResponse(status_code=404, content={"error": "Historia desconocida"})
    return {"deleted": True}


@router.delete("/api/stories/{story_id}/messages/{message_id}")
async def delete_story_message(story_id: int, message_id: int, request: Request) -> Any:
    story = request.app.state.stories.get(story_id)
    if story is None:
        return JSONResponse(status_code=404, content={"error": "Historia desconocida"})
    if not any(message["id"] == message_id for message in story["messages"]):
        return JSONResponse(status_code=404, content={"error": "Mensaje desconocido"})
    if not request.app.state.stories.delete_message(story_id, message_id):
        return JSONResponse(status_code=404, content={"error": "Mensaje desconocido"})
    return {"deleted": True, "message_id": message_id}


@router.post("/api/stories/{story_id}/messages")
async def send_message(story_id: int, request: Request, payload: dict = Body(...)) -> dict:
    story = request.app.state.stories.get(story_id)
    if story is None:
        return JSONResponse(status_code=404, content={"error": "Historia desconocida"})
    content = payload.get("content")
    if not isinstance(content, str) or not content.strip():
        raise EngineError("Escribe un mensaje")
    content = content.strip()[:4000]
    character = request.app.state.characters.get(story["character_id"])
    if character is None:
        return JSONResponse(status_code=409, content={"error": "El personaje de esta historia ya no existe"})
    llm = request.app.state.story_llm or request.app.state.llm
    if not callable(llm):
        return JSONResponse(status_code=503, content={"error": "Configura un modelo conversacional compatible para usar Historias"})
    if not os.environ.get("WAIFU_STORY_LLM_URL", "").strip() and not request.app.state.story_model.status()["available"]:
        return JSONResponse(status_code=503, content={"error": "Descarga e inicia el modelo desde la tarjeta de Historias"})
    system = _story_chat_system_prompt(story, character)
    history = story["messages"]
    summary = story.get("summary") or ""
    summarized_through = int(story.get("summarized_through") or 0)
    pending = [item for item in history if item["id"] > summarized_through]
    if len(pending) > 20:
        to_summarize = pending[:-12]
        old_text = "\n".join(
            f"{'Usuario' if item['role'] == 'user' else character['name']}: {item['content']}"
            for item in to_summarize
        )
        try:
            previous = f"Resumen anterior: {summary}\n" if summary else ""
            new_summary = llm(
                "Resume la continuidad de una novela interactiva en español. Conserva hechos, decisiones del usuario, relación entre personajes, lugares, promesas y conflictos pendientes. Sé conciso y no inventes detalles.",
                previous + old_text,
                temperature=0.2,
                max_tokens=350,
            )
            if isinstance(new_summary, str) and new_summary.strip():
                summary = new_summary.strip()[:5000]
                summarized_through = to_summarize[-1]["id"]
                request.app.state.stories.update_summary(story_id, summary, summarized_through)
        except Exception:
            pass
    history = [item for item in history if item["id"] > summarized_through][-16:]
    transcript = "\n".join(
        f"{'Usuario' if item['role'] == 'user' else character['name']}: {item['content']}"
        for item in history
    )
    if summary:
        transcript = f"Resumen de continuidad anterior: {summary}\n\n{transcript}"
    transcript += f"\nUsuario: {content}\n{character['name']}:"
    try:
        try:
            answer = llm(system, transcript, temperature=0.85, max_tokens=500)
        except TypeError:
            answer = llm(system, transcript)
    except Exception as exc:
        return JSONResponse(status_code=503, content={"error": f"El modelo no pudo responder: {exc}"})
    if not isinstance(answer, str) or not answer.strip():
        raise EngineError("El modelo devolvió una respuesta vacía")
    user_id = request.app.state.stories.add_message(story_id, "user", content)
    assistant_id = request.app.state.stories.add_message(story_id, "assistant", answer.strip()[:6000])
    return {
        "user": {"id": user_id, "role": "user", "content": content},
        "assistant": {"id": assistant_id, "role": "assistant", "content": answer.strip()[:6000]},
    }


@router.post("/api/stories/{story_id}/messages/{message_id}/regenerate")
async def regenerate_story_message(story_id: int, message_id: int, request: Request) -> Any:
    story = request.app.state.stories.get(story_id)
    if story is None:
        return JSONResponse(status_code=404, content={"error": "Historia desconocida"})
    messages = story["messages"]
    message_index = next(
        (index for index, item in enumerate(messages) if item["id"] == message_id),
        None,
    )
    if message_index is None or messages[message_index]["role"] != "assistant":
        return JSONResponse(status_code=404, content={"error": "Respuesta del personaje desconocida"})
    message = messages[message_index]
    if message.get("generation_id"):
        generation = request.app.state.store.get(message["generation_id"])
        if generation and generation.get("status") in ("queued", "running"):
            return JSONResponse(status_code=409, content={"error": "Espera a que termine la ilustración antes de regenerar la respuesta"})
    history = messages[:message_index]
    if not any(item["role"] == "user" for item in history):
        return JSONResponse(status_code=409, content={"error": "Esta respuesta no tiene una intervención previa para regenerar"})
    character = request.app.state.characters.get(story["character_id"])
    if character is None:
        return JSONResponse(status_code=409, content={"error": "El personaje de esta historia ya no existe"})
    llm = request.app.state.story_llm or request.app.state.llm
    if not callable(llm):
        return JSONResponse(status_code=503, content={"error": "Configura un modelo conversacional compatible para regenerar respuestas"})
    if not os.environ.get("WAIFU_STORY_LLM_URL", "").strip() and not request.app.state.story_model.status()["available"]:
        return JSONResponse(status_code=503, content={"error": "Inicia el modelo conversacional desde Historias para regenerar respuestas"})

    summarized_through = int(story.get("summarized_through") or 0)
    clear_summary = message_id <= summarized_through
    summary = "" if clear_summary else str(story.get("summary") or "")
    latest_user = next((item for item in reversed(history) if item["role"] == "user"), None)
    if latest_user is None:
        return JSONResponse(status_code=409, content={"error": "No se encontró la indicación del usuario que originó esta respuesta"})
    previous_context = [
        item for item in history
        if item["id"] != latest_user["id"] and (clear_summary or item["id"] > summarized_through)
    ][-16:]
    transcript = "\n".join(
        f"{'Usuario' if item['role'] == 'user' else character['name']}: {item['content']}"
        for item in previous_context
    )
    if summary:
        transcript = f"Resumen de continuidad anterior: {summary}\n\n{transcript}"
    transcript += (
        f"\n\nINDICACIÓN ACTUAL DEL USUARIO — responde a esto de forma directa y representa "
        f"el cambio de escena o acción que solicita, manteniendo el perfil del personaje:\n"
        f"{latest_user['content']}\n\n{character['name']}:"
    )
    try:
        try:
            answer = llm(_story_chat_system_prompt(story, character), transcript, temperature=0.85, max_tokens=500)
        except TypeError:
            answer = llm(_story_chat_system_prompt(story, character), transcript)
    except Exception as exc:
        return JSONResponse(status_code=503, content={"error": f"El modelo no pudo regenerar la respuesta: {exc}"})
    if not isinstance(answer, str) or not answer.strip():
        raise EngineError("El modelo devolvió una respuesta vacía")
    content = answer.strip()[:6000]
    if not request.app.state.stories.replace_message_content(
        story_id, message_id, content, clear_summary=clear_summary
    ):
        return JSONResponse(status_code=404, content={"error": "La respuesta ya no existe"})
    return {"message": {"id": message_id, "role": "assistant", "content": content}, "regenerated": True}


@router.post("/api/stories/{story_id}/messages/{message_id}/illustration")
async def illustrate_message(story_id: int, message_id: int, request: Request, payload: dict = Body(...)):
    story = request.app.state.stories.get(story_id)
    if story is None:
        return JSONResponse(status_code=404, content={"error": "Historia desconocida"})
    message = next((m for m in story["messages"] if m["id"] == message_id and m["role"] == "assistant"), None)
    if message is None:
        return JSONResponse(status_code=404, content={"error": "Escena desconocida"})
    image_request = payload.get("image_request", "")
    if not isinstance(image_request, str) or len(image_request.strip()) > 700:
        return JSONResponse(status_code=400, content={"error": "La descripción de la imagen debe ser texto de hasta 700 caracteres"})
    image_request = image_request.strip()
    if message.get("generation_id"):
        generation = request.app.state.store.get(message["generation_id"])
        if generation and generation.get("status") in ("queued", "running"):
            return JSONResponse(status_code=409, content={"error": "La ilustración de esta escena ya está en proceso"})
        if not image_request and generation and generation.get("status") == "done" and generation.get("outputs"):
            url = f"/media/{message['generation_id']}/{generation['outputs'][0]}"
            return {"generation_id": message["generation_id"], "image_url": url, "already_done": True}
    character = request.app.state.characters.get(story["character_id"])
    if character is None:
        return JSONResponse(status_code=409, content={"error": "El personaje de esta historia ya no existe"})
    prompt = _story_illustration_prompt(story, character, message, image_request)
    prompt = await _refine_story_illustration_prompt(request, prompt, story, character)
    visual_anchor = _story_character_visual_anchor(character)
    if visual_anchor:
        prompt = f"{visual_anchor}, {prompt}"
    image_payload = {
        "model_id": payload.get("model_id"), "prompt": prompt,
        "negative": "low quality, blurry, child, teen, loli, underage",
        "rating": story["rating"],
        "params": {"steps": 28, "cfg": 5.5, "seed": secrets.randbelow(2**31), "sampler_name": "euler", "scheduler": "normal", "width": 832, "height": 1216},
    }
    # If the user explicitly saved a character reference, use it as img2img
    # input so the face, hair, palette and silhouette stay anchored between scenes.
    # Never promote a generated story image automatically: a bad first image would
    # otherwise become the canonical design and reinforce the drift.
    reference = request.app.state.characters.first_non_sheet_ref(story["character_id"])
    identity_reference_used = False
    if reference:
        refs_root = Path(request.app.state.characters.refs_root).resolve()
        reference_path = (refs_root / reference["relpath"]).resolve()
        if reference_path.is_relative_to(refs_root) and reference_path.is_file():
            image_payload["ref_image_b64"] = base64.b64encode(reference_path.read_bytes()).decode("ascii")
            image_payload["strength"] = 0.55
            identity_reference_used = True
    generation = await api_generate(request, image_payload)
    if isinstance(generation, JSONResponse) and generation.status_code >= 400:
        return generation
    job_id = generation.get("job_id") if isinstance(generation, dict) else None
    if not job_id:
        return JSONResponse(status_code=502, content={"error": "WaifuStudio no devolvió el identificador del trabajo"})
    job = request.app.state.jobs.get(job_id, {})
    gen_id = job.get("gen_id")
    if not gen_id:
        return JSONResponse(status_code=502, content={"error": "El trabajo de imagen no quedó asociado a una generación"})
    request.app.state.stories.link_generation(message_id, gen_id)
    return {
        "job_id": job_id, "generation_id": gen_id, "image_url": None,
        "identity_reference_used": identity_reference_used,
    }
