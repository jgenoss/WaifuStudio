import { api } from "./api.js";
import { $, on } from "./dom.js";

let refreshTimer = null;

function formatBytes(value) {
  if (!Number.isFinite(Number(value)) || Number(value) < 0) return "—";
  const bytes = Number(value);
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let size = bytes / 1024;
  let index = 0;
  while (size >= 1024 && index < units.length - 1) { size /= 1024; index += 1; }
  return `${size.toFixed(size >= 10 ? 1 : 2)} ${units[index]}`;
}

function stateLabel(state) {
  return ({
    downloading: "Descargando",
    retrying: "Conexión detenida · reintentando",
    starting: "Preparando modelo",
    downloaded: "Descarga completa · verificando",
    verified: "Listo",
    fallback: "Descarga estándar",
    interrupted: "Interrumpida",
    error: "Error",
  })[state] || state || "Desconocido";
}

function renderDownloads(data) {
  const list = $("model-download-list");
  list.replaceChildren();
  const items = Array.isArray(data?.items) ? data.items : [];
  const active = Number(data?.active) || 0;
  const badge = $("download-active-badge");
  badge.textContent = String(active);
  badge.classList.toggle("hidden", active === 0);
  $("download-modal-summary").textContent = active
    ? `${active} descarga${active === 1 ? "" : "s"} activa${active === 1 ? "" : "s"}`
    : "Estado de transferencias recientes";
  if (!items.length) {
    const empty = document.createElement("p");
    empty.className = "downloads-empty";
    empty.textContent = "No hay descargas recientes. Inicia una desde el instalador o el módulo de Historias.";
    list.append(empty);
    return;
  }
  for (const item of items) {
    const card = document.createElement("article");
    card.className = `download-item state-${item.state || "unknown"}`;
    const heading = document.createElement("div");
    heading.className = "download-item-heading";
    const filename = document.createElement("strong");
    filename.textContent = item.file || "Modelo";
    const state = document.createElement("span");
    state.textContent = stateLabel(item.state);
    heading.append(filename, state);
    card.append(heading);

    if (Number.isFinite(Number(item.total)) && Number(item.total) > 0) {
      const progress = document.createElement("progress");
      progress.max = 100;
      progress.value = Math.max(0, Math.min(100, Number(item.percent) || 0));
      card.append(progress);
      const stats = document.createElement("div");
      stats.className = "download-stats";
      const received = item.downloaded == null ? "Preparando…" : formatBytes(item.downloaded);
      stats.textContent = `${received} / ${formatBytes(item.total)} · ${progress.value.toFixed(1)}%`;
      card.append(stats);
    }

    const details = document.createElement("div");
    details.className = "download-details";
    const parts = [];
    if (Number(item.speed_mbps) > 0) parts.push(`${Number(item.speed_mbps).toFixed(1)} MB/s`);
    if (Number(item.workers) > 1) parts.push(`${item.workers} conexiones`);
    if (item.detail) parts.push(item.detail);
    details.textContent = parts.join(" · ");
    card.append(details);
    list.append(card);
  }
}

async function refreshDownloads() {
  try { renderDownloads(await api("/api/model-downloads")); }
  catch (error) {
    const list = $("model-download-list");
    list.replaceChildren();
    const note = document.createElement("p");
    note.className = "downloads-empty error";
    note.textContent = `No se pudo consultar el estado: ${error.message}`;
    list.append(note);
  }
}

function closeDownloads() {
  $("model-download-modal").classList.add("hidden");
  if (refreshTimer) clearInterval(refreshTimer);
  refreshTimer = setInterval(refreshDownloads, 5000);
}

function openDownloads() {
  $("model-download-modal").classList.remove("hidden");
  refreshDownloads();
  if (refreshTimer) clearInterval(refreshTimer);
  refreshTimer = setInterval(refreshDownloads, 2000);
}

function initDownloads() {
  on("btn-downloads", "click", openDownloads);
  on("btn-downloads-close", "click", closeDownloads);
  on("btn-downloads-refresh", "click", refreshDownloads);
  on("model-download-modal", "click", (event) => {
    if (event.target === $("model-download-modal")) closeDownloads();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !$("model-download-modal").classList.contains("hidden")) closeDownloads();
  });
  refreshDownloads();
  if (refreshTimer) clearInterval(refreshTimer);
  refreshTimer = setInterval(refreshDownloads, 5000);
}

export { initDownloads, refreshDownloads };
