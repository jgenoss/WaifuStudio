import { api, postJson } from "../api.js";
import { $, on } from "../dom.js";

let stories = [];
let current = null;
let models = [];
let storyModelState = { state: "unknown", available: false };
let storyImageModelState = { state: "unknown", available: false };
let storyModelToolsExpanded = true;
let storyModelToolsTouched = false;
const activeImagePolls = new Set();
let readinessBusy = false;
const STORY_IMAGE_FILES = {
  "Anima-2.9B-preview-v1.safetensors": 5843204206,
  "qwen_3_06b_base.safetensors": 1192135096,
  "qwen_image_vae.safetensors": 253806246,
};

function status(text = "", error = false) {
  const creating = !$("story-create").classList.contains("hidden");
  const el = creating ? $("story-config-note") : $("story-status");
  el.textContent = text;
  el.classList.toggle("error", error);
}

function renderModelState(data) {
  storyModelState = data || { state: "error", detail: "No se pudo leer el estado del modelo." };
  const labels = {
    unknown: "Consultando…", missing: "Falta descargar", stopped: "Descargado · detenido",
    downloading: "Descargando y verificando…", starting: "Iniciando en CPU…",
    running: "Listo para conversar", error: "No se pudo iniciar", external: "Proveedor externo listo",
  };
  $("story-model-state").textContent = labels[data?.state] || data?.state || "Estado desconocido";
  const details = data?.detail || "Qwen3.5-4B · 2,71 GB · CPU · 4K contexto";
  $("story-model-detail").textContent = `${details}${data?.state === "running" ? ` · ${data.threads} hilos` : ""}`;
  const progress = $("story-model-progress");
  progress.hidden = data?.state !== "downloading";
  progress.value = Number(data?.progress) || 0;
  const button = $("btn-story-model");
  button.hidden = data?.state === "external";
  button.disabled = ["downloading", "starting"].includes(data?.state);
  button.textContent = data?.state === "running" ? "Detener modelo" : "Descargar e iniciar modelo";
  button.classList.toggle("primary", data?.state !== "running");
  button.classList.toggle("secondary", data?.state === "running");
  updateStoryModelTools();
}

function updateStoryModelTools() {
  const ready = storyModelState.available && storyImageModelState.available;
  if (!storyModelToolsTouched) storyModelToolsExpanded = !ready;
  const root = $("story-model-tools");
  root.classList.toggle("collapsed", !storyModelToolsExpanded);
  const toggle = $("btn-story-model-tools-toggle");
  toggle.textContent = storyModelToolsExpanded ? "Ocultar modelos" : "Mostrar modelos";
  toggle.setAttribute("aria-expanded", String(storyModelToolsExpanded));
}

async function refreshModelState() {
  try { renderModelState(await api("/api/stories/model")); }
  catch (error) { renderModelState({ state: "error", detail: error.message }); }
}

async function toggleStoryModel() {
  const action = storyModelState.state === "running" ? "stop" : "start";
  const button = $("btn-story-model");
  button.disabled = true;
  try {
    renderModelState(await api(`/api/stories/model/${action}`, { method: "POST" }));
    if (action === "start") {
      for (;;) {
        await new Promise((resolve) => setTimeout(resolve, 1500));
        const data = await api("/api/stories/model");
        renderModelState(data);
        if (["running", "error", "external"].includes(data.state)) break;
      }
      if (storyModelState.state === "error") status(storyModelState.detail, true);
    }
  } catch (error) {
    renderModelState({ state: "error", detail: error.message });
  } finally { button.disabled = false; }
}

function renderStoryImageModel(data) {
  storyImageModelState = data || { state: "error", detail: "No se pudo consultar el modelo de imagen." };
  const labels = { unknown: "Comprobando…", missing: "No instalado", downloading: "Descargando y verificando…", ready: "Listo para ilustrar", error: "Descarga incompleta" };
  $("story-image-model-state").textContent = labels[data?.state] || data?.state || "Estado desconocido";
  const total = Number(data?.total) || 0;
  const downloaded = Number(data?.downloaded) || 0;
  const size = total ? ` · ${(downloaded / 1e9).toFixed(2)} / ${(total / 1e9).toFixed(2)} GB` : "";
  $("story-image-model-detail").textContent = `${data?.detail || "Anima 2.9B · licencia no comercial"}${size}`;
  const progress = $("story-image-model-progress");
  progress.hidden = !["downloading", "error"].includes(data?.state) || !total;
  progress.value = Math.max(0, Math.min(100, Number(data?.progress) || 0));
  const button = $("btn-story-image-model");
  button.hidden = data?.state === "ready";
  button.disabled = data?.state === "downloading";
  button.textContent = data?.state === "error" ? "Reanudar descarga" : "Descargar modelo de imagen · 7,29 GB";
  updateStoryModelTools();
}

async function refreshStoryImageModel() {
  try {
    const data = await api("/api/stories/image-model");
    if (["downloading", "error", "missing"].includes(data.state)) {
      const transfers = await api("/api/model-downloads");
      const items = Array.isArray(transfers?.items) ? transfers.items : [];
      data.downloaded = Object.entries(STORY_IMAGE_FILES).reduce((sum, [file, size]) => {
        const item = items.find((entry) => entry.file === file);
        return sum + Math.min(size, Number(item?.downloaded) || 0);
      }, 0);
      data.progress = data.total ? data.downloaded * 100 / data.total : 0;
    }
    renderStoryImageModel(data);
  }
  catch (error) { renderStoryImageModel({ state: "error", detail: error.message }); }
}

async function downloadStoryImageModel() {
  const button = $("btn-story-image-model");
  button.disabled = true;
  try {
    renderStoryImageModel(await api("/api/stories/image-model/start", { method: "POST" }));
    while (storyImageModelState.state === "downloading") {
      await new Promise((resolve) => setTimeout(resolve, 1500));
      renderStoryImageModel(await api("/api/stories/image-model"));
    }
    if (storyImageModelState.state === "ready") {
      models = await api("/api/models?scan=1");
      status("Modelo de imagen listo. Ya puedes ilustrar escenas.");
    } else if (storyImageModelState.state === "error") {
      status(storyImageModelState.detail, true);
    }
  } catch (error) {
    renderStoryImageModel({ state: "error", detail: error.message });
    status(error.message, true);
  } finally { button.disabled = storyImageModelState.state === "downloading"; }
}

function showCreate(show = true) {
  $("story-welcome").classList.add("hidden");
  $("story-active").classList.add("hidden");
  $("story-create").classList.toggle("hidden", !show);
  if (!show && !current) $("story-welcome").classList.remove("hidden");
}

function renderList() {
  const root = $("story-list");
  root.replaceChildren();
  $("story-list-empty").classList.toggle("hidden", stories.length > 0);
  for (const story of stories) {
    const row = document.createElement("div");
    row.className = "story-list-row";
    const button = document.createElement("button");
    button.type = "button";
    button.className = `story-list-item${current?.id === story.id ? " active" : ""}`;
    const title = document.createElement("strong");
    title.textContent = story.title;
    const detail = document.createElement("small");
    detail.textContent = `${story.character_name || "Personaje"} · ${story.genre}`;
    button.append(title, detail);
    button.addEventListener("click", () => openStory(story.id));
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "story-list-delete";
    remove.textContent = "×";
    remove.title = `Eliminar «${story.title}»`;
    remove.setAttribute("aria-label", `Eliminar historia ${story.title}`);
    remove.addEventListener("click", () => deleteStory(story.id));
    row.append(button, remove);
    root.append(row);
  }
}

function renderMessages() {
  const root = $("story-messages");
  root.replaceChildren();
  for (const message of current?.messages || []) {
    const bubble = document.createElement("article");
    bubble.className = `story-bubble ${message.role}`;
    const speaker = document.createElement("span");
    speaker.className = "story-speaker";
    speaker.textContent = message.role === "user" ? "Tú" : current.character_name;
    const text = document.createElement("div");
    text.textContent = message.content;
    bubble.append(speaker, text);
    if (message.image_url) {
      const img = document.createElement("img");
      img.className = "story-illustration";
      img.src = message.image_url;
      img.alt = `Ilustración de ${current.title}`;
      img.loading = "lazy";
      bubble.append(img);
    }
    const illustrationBusy = message.role === "assistant" && ["queued", "running"].includes(message.illustration_status);
    if (illustrationBusy) {
      const pending = document.createElement("div");
      pending.className = "story-illustration-progress";
      const label = document.createElement("small");
      label.className = "story-image-progress-label";
      label.textContent = message.illustration_status === "queued" ? "Ilustración en cola…" : "Generando ilustración…";
      const progress = document.createElement("progress");
      progress.max = 100;
      progress.className = "story-image-progress-bar";
      progress.dataset.messageId = message.id;
      if (Number.isFinite(message.illustration_progress)) progress.value = message.illustration_progress;
      pending.append(label, progress);
      bubble.append(pending);
    }
    if (message.role === "assistant" && !illustrationBusy) {
      const actions = document.createElement("div");
      actions.className = "story-message-actions";
      if (!message.image_url) {
        const illustrate = document.createElement("button");
        illustrate.type = "button";
        illustrate.textContent = message.illustration_status === "error" ? "↻ Reintentar ilustración" : "✦ Ilustrar escena";
        illustrate.addEventListener("click", () => illustrateMessage(message.id, illustrate));
        actions.append(illustrate);
      }
      const regenerate = document.createElement("button");
      regenerate.type = "button";
      regenerate.className = "secondary";
      regenerate.textContent = "↻ Regenerar respuesta";
      regenerate.title = "Volver a responder directamente a tu última intervención, conservando el contexto de la historia";
      regenerate.addEventListener("click", () => regenerateMessage(message.id, regenerate));
      actions.append(regenerate);
      const custom = document.createElement("button");
      custom.type = "button";
      custom.className = "secondary";
      custom.textContent = "＋ Otra imagen a solicitud";
      const customForm = document.createElement("form");
      customForm.className = "story-image-request hidden";
      const customInput = document.createElement("textarea");
      customInput.maxLength = 700;
      customInput.rows = 2;
      customInput.required = true;
      customInput.placeholder = "Describe la imagen que quieres ver en esta escena…";
      const customSubmit = document.createElement("button");
      customSubmit.type = "submit";
      customSubmit.className = "primary";
      customSubmit.textContent = "Generar imagen";
      customForm.append(customInput, customSubmit);
      custom.addEventListener("click", () => {
        customForm.classList.toggle("hidden");
        if (!customForm.classList.contains("hidden")) customInput.focus();
      });
      customForm.addEventListener("submit", (event) => {
        event.preventDefault();
        requestStoryImage(message.id, customInput.value, customSubmit);
      });
      actions.append(custom);
      bubble.append(actions);
      bubble.append(customForm);
    }
    if (message.role === "user" || message.role === "assistant") {
      let actions = bubble.querySelector(".story-message-actions");
      if (!actions) {
        actions = document.createElement("div");
        actions.className = "story-message-actions";
        bubble.append(actions);
      }
      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "story-message-delete";
      remove.textContent = "Eliminar mensaje";
      remove.addEventListener("click", () => deleteStoryMessage(message.id, message.role));
      actions.append(remove);
    }
    root.append(bubble);
  }
  root.scrollTop = root.scrollHeight;
}

async function refreshList() {
  stories = await api("/api/stories");
  renderList();
}

async function openStory(id, openingPrompt = "") {
  try {
    current = await api(`/api/stories/${id}`);
    $("story-create").classList.add("hidden");
    $("story-welcome").classList.add("hidden");
    $("story-active").classList.remove("hidden");
    $("story-title").textContent = current.title;
    $("story-subtitle").textContent = `${current.character_name} · ${current.genre} · ${current.tone}`;
    let openingError = "";
    if (!(current.messages || []).length) {
      status("Escribiendo la escena de bienvenida…");
      try {
        await postJson(`/api/stories/${id}/opening`, { opening_prompt: openingPrompt });
        current = await api(`/api/stories/${id}`);
      } catch (error) {
        openingError = error.message;
      }
    }
    $("story-title").textContent = current.title;
    $("story-subtitle").textContent = `${current.character_name} · ${current.genre} · ${current.tone}`;
    $("story-create").classList.add("hidden");
    $("story-welcome").classList.add("hidden");
    $("story-active").classList.remove("hidden");
    renderList();
    renderMessages();
    status(openingError, Boolean(openingError));
    $("story-input").focus();
    for (const message of current.messages || []) {
      if (message.image_job_id && ["queued", "running"].includes(message.illustration_status)) {
        pollIllustration(message.image_job_id, current.id, message.id);
      }
    }
  } catch (error) { status(error.message, true); }
}

async function beginCreate() {
  current = null;
  renderList();
  const characters = await api("/api/characters");
  models = [];
  const select = $("story-character");
  select.replaceChildren();
  for (const char of characters || []) {
    const option = document.createElement("option");
    option.value = char.id;
    option.textContent = char.name;
    select.append(option);
  }
  $("story-config-note").textContent = "Puedes crear un personaje aquí. El chat necesita iniciar el modelo conversacional; las ilustraciones usan los modelos de imagen instalados en WaifuStudio.";
  $("story-create").reset();
  $("story-create").classList.remove("hidden");
  $("story-character-create").classList.add("hidden");
  $("story-welcome").classList.add("hidden");
  $("story-active").classList.add("hidden");
  $("story-adult-consent-wrap").classList.add("hidden");
}

function beginCharacterCreate() {
  $("story-create").classList.add("hidden");
  $("story-character-form").reset();
  $("story-character-age").value = "21";
  $("story-character-create-status").textContent = "La edad mínima del personaje es 21 años para mantenerlo adulto en historias e ilustraciones.";
  $("story-character-create-status").classList.remove("error");
  $("story-character-create").classList.remove("hidden");
  $("story-character-name").focus();
}

function randomStoryCharacter() {
  const pick = (items) => items[Math.floor(Math.random() * items.length)];
  const designs = [
    { identity: "Mujer adulta", appearance: "long black hair, violet eyes, pale skin, soft curvy build, black gothic dress, silver moon hairpin" },
    { identity: "Hombre adulto", appearance: "short silver hair, amber eyes, warm brown skin, athletic build, navy coat, leather gloves" },
    { identity: "Persona adulta no binaria", appearance: "wavy auburn hair, green eyes, olive skin, slender build, layered traveler outfit, star earrings" },
    { identity: "Mujer adulta", appearance: "long lavender hair, ice blue eyes, freckled fair skin, tall elegant build, burgundy suit, round glasses" },
    { identity: "Hombre adulto", appearance: "curly dark brown hair, gray eyes, deep brown skin, compact muscular build, modern streetwear, eyebrow scar" },
  ];
  const design = pick(designs);
  const names = ["Luna", "Mika", "Akira", "Vera", "Nadia", "Kaori", "Dante", "Ren"];
  $("story-character-name").value = pick(names);
  $("story-character-age").value = String(21 + Math.floor(Math.random() * 15));
  $("story-character-identity").value = design.identity;
  $("story-character-appearance").value = design.appearance;
  $("story-character-personality").value = pick([
    "Curiosa, ingeniosa y de voluntad firme; observa antes de confiar.",
    "Cálido, bromista y leal; expresa con claridad lo que siente.",
    "Reservada, observadora y valiente; habla con calma y precisión.",
    "Directa, aventurera y compasiva; usa un humor juguetón.",
  ]);
  $("story-character-background").value = pick([
    "Viajera que busca un lugar al que pertenecer. Habla de forma natural y cercana.",
    "Artista independiente con un objetivo propio. Su voz es reflexiva y expresiva.",
    "Investigador que sigue una pista misteriosa. Habla con curiosidad y cautela.",
    "Heredera de una pequeña posada. Su meta es proteger a quienes considera familia.",
  ]);
  $("story-character-create-status").textContent = "Diseño aleatorio completo. Revísalo y edítalo antes de guardarlo.";
  $("story-character-create-status").classList.remove("error");
}

async function saveStoryCharacter(event) {
  event.preventDefault();
  const age = Number($("story-character-age").value);
  if (!Number.isInteger(age) || age < 21 || age > 120) {
    $("story-character-create-status").textContent = "La edad debe ser un número entre 21 y 120.";
    $("story-character-create-status").classList.add("error");
    return;
  }
  const name = $("story-character-name").value.trim();
  const identity = $("story-character-identity").value.trim();
  const appearance = $("story-character-appearance").value.trim();
  const personality = $("story-character-personality").value.trim();
  const background = $("story-character-background").value.trim();
  if (!name || !personality) {
    $("story-character-create-status").textContent = "Escribe un nombre y una personalidad para guardar el personaje.";
    $("story-character-create-status").classList.add("error");
    return;
  }
  const notes = [
    `Edad: ${age} años (adulto, 21+).`,
    identity && `Identidad: ${identity}.`,
    appearance && `Apariencia: ${appearance}`,
    `Personalidad: ${personality}`,
    background && `Historia y forma de hablar: ${background}`,
  ].filter(Boolean).join("\n");
  const tags = ["adult", ...appearance.split(",").map((tag) => tag.trim()), identity].filter(Boolean);
  const submit = $("story-character-form").querySelector('[type="submit"]');
  submit.disabled = true;
  try {
    const created = await postJson("/api/characters", { name, tags, notes, rating: "sfw" });
    const characters = await api("/api/characters");
    const select = $("story-character");
    select.replaceChildren();
    for (const char of characters || []) {
      const option = document.createElement("option");
      option.value = char.id;
      option.textContent = char.name;
      select.append(option);
    }
    select.value = String(created.id);
    $("story-character-create").classList.add("hidden");
    $("story-create").classList.remove("hidden");
    $("story-config-note").textContent = `${name} se guardó y quedó seleccionado para esta novela.`;
  } catch (error) {
    $("story-character-create-status").textContent = error.message;
    $("story-character-create-status").classList.add("error");
  } finally { submit.disabled = false; }
}

async function createStory(event) {
  event.preventDefault();
  if (!$("story-character").value) { status("Primero crea un personaje en OC Maker.", true); return; }
  const rating = $("story-rating").value;
  if (rating === "nsfw" && !$("story-adult-consent").checked) {
    status("Confirma la opción de contenido para adultos.", true); return;
  }
  const submit = $("story-create").querySelector('[type="submit"]');
  submit.disabled = true;
  submit.textContent = "Creando escena inicial…";
  $("story-config-note").textContent = "Creando la novela y escribiendo el primer mensaje del personaje…";
  try {
    const story = await postJson("/api/stories", {
      character_id: Number($("story-character").value),
      title: $("story-title-input").value.trim(),
      opening_prompt: $("story-opening-input").value.trim(),
      genre: $("story-genre").value,
      tone: $("story-tone").value,
      rating,
      adult_confirmed: rating === "nsfw" && $("story-adult-consent").checked,
    });
    await refreshList();
    await openStory(story.id, $("story-opening-input").value.trim());
    if (story.opening_generated || current?.messages?.length) {
      status("La historia comenzó. Ya puedes responder a la escena de bienvenida.");
    } else if (story.opening_error) {
      status(`${story.opening_error} La novela quedó guardada y puedes continuarla cuando el modelo esté listo.`, true);
    }
  } catch (error) { status(error.message, true); }
  finally {
    submit.disabled = false;
    submit.textContent = "Crear novela";
  }
}

async function sendMessage(event) {
  event.preventDefault();
  const input = $("story-input");
  const content = input.value.trim();
  if (!current || !content) return;
  const button = $("btn-story-send");
  button.disabled = true;
  status("La escena continúa…");
  try {
    await postJson(`/api/stories/${current.id}/messages`, { content });
    input.value = "";
    await openStory(current.id);
    await refreshList();
  } catch (error) {
    await openStory(current.id);
    status(error.message, true);
  } finally { button.disabled = false; }
}

async function deleteStoryMessage(messageId, role) {
  if (!current) return;
  const speaker = role === "user" ? "tu mensaje" : "la respuesta del personaje";
  if (!window.confirm(`¿Eliminar ${speaker} de esta historia? El resumen de continuidad se reiniciará.`)) return;
  try {
    const storyId = current.id;
    await api(`/api/stories/${storyId}/messages/${messageId}`, { method: "DELETE" });
    await openStory(storyId);
    await refreshList();
    status("Mensaje eliminado de la historia.");
  } catch (error) { status(error.message, true); }
}

async function regenerateMessage(messageId, button) {
  if (!current) return;
  button.disabled = true;
  status("Regenerando la respuesta con el mismo contexto…");
  try {
    const storyId = current.id;
    await postJson(`/api/stories/${storyId}/messages/${messageId}/regenerate`, {});
    await openStory(storyId);
    await refreshList();
    status("Respuesta regenerada. La ilustración anterior permanece en la Galería.");
  } catch (error) {
    status(error.message, true);
    button.disabled = false;
  }
}

async function illustrateMessage(messageId, button) {
  await requestStoryImage(messageId, "", button);
}

async function requestStoryImage(messageId, imageRequest, button) {
  if (!models.length) {
    try { models = await api("/api/models?scan=1"); }
    catch (error) { status(error.message, true); return; }
  }
  if (!models.length) { status("No hay modelos de imagen instalados. Descarga Anima desde el módulo de Ilustraciones de escenas.", true); return; }
  button.disabled = true;
  status(imageRequest ? "Preparando la imagen que pediste…" : "Encolando ilustración…");
  try {
    const result = await postJson(`/api/stories/${current.id}/messages/${messageId}/illustration`, {
      model_id: models[0].id, image_request: imageRequest,
    });
    if (result.job_id) {
      const storyId = current.id;
      await openStory(storyId);
      status(result.identity_reference_used
        ? "Ilustración en cola con la referencia visual del personaje."
        : "Ilustración en cola sin imagen base. Guarda un retrato en OC Maker y pulsa «Usar como imagen base» para fijar el diseño en las próximas escenas.");
      pollIllustration(result.job_id, storyId, messageId);
    }
  } catch (error) { status(error.message, true); button.disabled = false; }
}

async function pollIllustration(jobId, storyId, messageId) {
  if (activeImagePolls.has(jobId)) return;
  activeImagePolls.add(jobId);
  for (let attempt = 0; attempt < 180; attempt++) {
    await new Promise((resolve) => setTimeout(resolve, 2000));
    try {
      const job = await api(`/api/jobs/${encodeURIComponent(jobId)}`);
      const progress = document.querySelector(`.story-image-progress-bar[data-message-id="${messageId}"]`);
      const label = progress?.parentElement?.querySelector(".story-image-progress-label");
      if (progress && label && current?.id === storyId) {
        const percent = job.progress?.percent;
        if (typeof percent === "number" && Number.isFinite(percent)) {
          progress.value = percent;
          label.textContent = `Generando ilustración · ${Math.round(percent)}%`;
        } else {
          progress.removeAttribute("value");
          label.textContent = job.status === "queued" ? "Ilustración en cola…" : "Preparando ilustración…";
        }
      }
      if (["done", "error", "cancelled"].includes(job.status)) {
        if (job.status === "error") status(job.error || "Falló la ilustración.", true);
        else if (job.status === "cancelled") status("La ilustración fue cancelada.", true);
        else status("Ilustración lista.");
        if (current?.id === storyId) await openStory(storyId);
        activeImagePolls.delete(jobId);
        return;
      }
    } catch (error) { status(error.message, true); activeImagePolls.delete(jobId); return; }
  }
  status("La ilustración sigue procesándose; vuelve a abrir la historia más tarde.");
  activeImagePolls.delete(jobId);
}

async function deleteStory(storyId = current?.id) {
  if (!storyId) return;
  const story = stories.find((item) => item.id === storyId);
  const title = story?.title || current?.title || "esta historia";
  if (!window.confirm(`¿Eliminar «${title}» y todos los mensajes de esta historia?`)) return;
  try {
    await api(`/api/stories/${storyId}`, { method: "DELETE" });
    if (current?.id === storyId) current = null;
    await refreshList();
    if (!current) showCreate(false);
    else status(`Se eliminó «${title}».`);
  } catch (error) { status(error.message, true); }
}

async function refreshSystemReadiness() {
  if (readinessBusy) return;
  readinessBusy = true;
  const badge = $("system-readiness");
  const label = $("system-readiness-label");
  try {
    const result = await api("/api/system/readiness");
    const parts = result.components || {};
    const names = [
      ["web", "App web"], ["engine", "ComfyUI"],
      ["conversation", "Modelo de chat"], ["illustration_model", "Modelo de imagen"],
    ];
    const details = names.map(([key, name]) => `${parts[key] ? "✓" : "○"} ${name}`).join(" · ");
    badge.classList.toggle("ready", !!result.ready);
    badge.classList.toggle("partial", !result.ready && !!parts.web);
    badge.classList.toggle("offline", !parts.web);
    label.textContent = result.ready ? "Sistema listo" : "Sistema incompleto";
    badge.title = details;
  } catch (_error) {
    badge.classList.remove("ready", "partial");
    badge.classList.add("offline");
    label.textContent = "Sin conexión con el sistema";
    badge.title = "No se pudo consultar el estado de los servicios.";
  } finally { readinessBusy = false; }
}

function initStories() {
  on("btn-story-new", "click", () => beginCreate().catch((e) => status(e.message, true)));
  on("btn-story-start", "click", () => beginCreate().catch((e) => status(e.message, true)));
  on("btn-story-create-back", "click", () => showCreate(false));
  on("btn-story-character-new", "click", beginCharacterCreate);
  on("btn-story-character-random", "click", randomStoryCharacter);
  on("btn-story-character-back", "click", () => {
    $("story-character-create").classList.add("hidden");
    $("story-create").classList.remove("hidden");
  });
  on("story-character-form", "submit", saveStoryCharacter);
  on("story-create", "submit", createStory);
  on("story-compose", "submit", sendMessage);
  on("btn-story-delete", "click", deleteStory);
  on("btn-story-model", "click", toggleStoryModel);
  on("btn-story-image-model", "click", downloadStoryImageModel);
  on("btn-story-model-tools-toggle", "click", () => {
    storyModelToolsTouched = true;
    storyModelToolsExpanded = !storyModelToolsExpanded;
    updateStoryModelTools();
  });
  on("system-readiness", "click", refreshSystemReadiness);
  on("story-rating", "change", () => $("story-adult-consent-wrap").classList.toggle("hidden", $("story-rating").value !== "nsfw"));
  on("story-input", "keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); $("story-compose").requestSubmit(); }
  });
  refreshList().catch((error) => status(error.message, true));
  refreshModelState();
  refreshStoryImageModel();
  refreshSystemReadiness();
  setInterval(refreshSystemReadiness, 10000);
}

export { initStories };
