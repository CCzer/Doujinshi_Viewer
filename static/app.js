"use strict";

const byId = id => document.getElementById(id);
const library = byId("library");
const reader = byId("reader");
const pages = byId("pages");
const toggle = byId("scroll-toggle");
const speed = byId("speed");
const groups = {works: "作品", actors: "登场人物", tags: "分类标签", authors: "作者"};
const statuses = {pending: "等待标签", unmatched: "未找到匹配", ambiguous: "存在多个匹配", error: "索引失败"};
const selected = new Set();
let books = [];
let initialized = false;
let indexing = false;
let viewMode = "list";
let refreshTimer;
let refreshSequence = 0;
try {
  if (localStorage.getItem("library-view") === "cover") viewMode = "cover";
} catch {
  // Storage can be unavailable in some browser sessions.
}
let activeBook = null;
let loadSequence = 0;
let frame = null;
let lastTime = null;
let remainder = 0;

const tagKey = tag => JSON.stringify([tag.kind, tag.name]);

function tagButton(tag, count) {
  const key = tagKey(tag);
  const button = document.createElement("button");
  button.type = "button";
  button.dataset.tag = key;
  button.textContent = count === undefined ? tag.name : tag.name + " · " + count;
  button.setAttribute("aria-pressed", selected.has(key));
  button.addEventListener("click", () => {
    if (selected.has(key)) selected.delete(key);
    else selected.add(key);
    renderLibrary();
    const matching = [...document.querySelectorAll("#facets button")].find(item => item.dataset.tag === key);
    matching?.focus({preventScroll: true});
  });
  return button;
}

function renderLibrary() {
  const facets = byId("facets");
  const results = byId("books");
  const known = new Map();
  for (const book of books) {
    for (const tag of book.tags) {
      const key = tagKey(tag);
      if (!known.has(key)) known.set(key, {tag, count: 0});
      known.get(key).count++;
    }
  }
  for (const key of selected) {
    if (!known.has(key)) selected.delete(key);
  }
  const shown = books.filter(book => [...selected].every(key => book.tags.some(tag => tagKey(tag) === key)));
  const pending = books.filter(book => book.status === "pending").length;
  const unresolved = books.filter(book => !["pending", "indexed"].includes(book.status)).length;
  byId("summary").textContent = [
    books.length + " 本同人志",
    indexing ? "正在更新标签" : "",
    pending ? pending + " 本待索引" : "",
    unresolved ? unresolved + " 本标签待确认" : "",
  ].filter(Boolean).join(" · ");
  byId("clear").hidden = !selected.size;
  byId("results-title").textContent = selected.size ? "筛选结果" : "全部同人志";
  byId("count").textContent = shown.length + " 本";

  facets.replaceChildren();
  for (const [kind, label] of Object.entries(groups)) {
    const entries = [...known.values()].filter(entry => entry.tag.kind === kind)
      .sort((a, b) => b.count - a.count || a.tag.name.localeCompare(b.tag.name, "zh-CN"));
    if (!entries.length) continue;
    const section = document.createElement("section");
    section.className = "facet";
    const title = document.createElement("h3");
    title.textContent = label;
    const chips = document.createElement("div");
    chips.className = "chips";
    for (const {tag, count} of entries) chips.append(tagButton(tag, count));
    section.append(title, chips);
    facets.append(section);
  }
  if (!known.size) showMessage(facets, "暂无可用标签");

  results.replaceChildren();
  results.classList.toggle("cover-grid", viewMode === "cover");
  byId("list-view").setAttribute("aria-pressed", viewMode === "list");
  byId("cover-view").setAttribute("aria-pressed", viewMode === "cover");
  for (const book of shown) {
    const entry = document.createElement("article");
    entry.className = "book";
    const link = document.createElement("a");
    link.href = "#book/" + book.id;
    if (viewMode === "cover") {
      const cover = document.createElement("img");
      cover.className = "cover-image";
      cover.loading = "lazy";
      cover.alt = "";
      cover.src = "/api/books/" + book.id + "/cover";
      link.append(cover);
    }
    link.append(document.createTextNode(book.name));
    entry.append(link);
    if (statuses[book.status]) {
      const status = document.createElement("p");
      status.className = "status";
      status.textContent = statuses[book.status];
      entry.append(status);
    }
    if (book.tags.length) {
      const chips = document.createElement("div");
      chips.className = "chips";
      for (const tag of book.tags) chips.append(tagButton(tag));
      entry.append(chips);
    }
    results.append(entry);
  }
  if (!shown.length) showMessage(results, selected.size ? "没有同时包含所选标签的同人志" : "PDF 文件夹为空");
}

function showMessage(container, message) {
  const text = document.createElement("p");
  text.className = "empty";
  text.textContent = message;
  container.replaceChildren(text);
}

function stopScroll() {
  if (frame !== null) cancelAnimationFrame(frame);
  frame = null;
  lastTime = null;
  remainder = 0;
  toggle.setAttribute("aria-pressed", "false");
  toggle.textContent = "自动滚动";
}

function scrollFrame(now) {
  if (lastTime !== null) {
    remainder += Math.min(now - lastTime, 100) * Number(speed.value) / 1000;
    const pixels = Math.floor(remainder);
    remainder -= pixels;
    pages.scrollTop += pixels;
  }
  if (pages.scrollTop + pages.clientHeight >= pages.scrollHeight - 1) {
    stopScroll();
    return;
  }
  lastTime = now;
  frame = requestAnimationFrame(scrollFrame);
}

async function route() {
  const id = Number(location.hash.match(/^#book\/(\d+)$/)?.[1]);
  const book = books.find(item => item.id === id);
  if (!book) {
    loadSequence++;
    activeBook = null;
    stopScroll();
    reader.hidden = true;
    library.hidden = false;
    pages.replaceChildren();
    return;
  }
  if (activeBook === book.id) return;
  activeBook = book.id;
  const sequence = ++loadSequence;
  stopScroll();
  toggle.disabled = true;
  library.hidden = true;
  reader.hidden = false;
  byId("reading-title").textContent = book.name;
  showMessage(pages, "正在打开书籍…");
  pages.scrollTop = 0;
  pages.focus({preventScroll: true});

  const endpoint = "/api/books/" + book.id + "/pages";
  try {
    const response = await fetch(endpoint);
    if (!response.ok) throw new Error("PDF");
    const dimensions = await response.json();
    if (sequence !== loadSequence) return;
    const fragment = document.createDocumentFragment();
    dimensions.forEach(([width, height], index) => {
      const page = document.createElement("img");
      page.alt = "第 " + (index + 1) + " 页";
      page.style.aspectRatio = width + " / " + height;
      page.loading = index === 0 ? "eager" : "lazy";
      page.decoding = "async";
      page.src = endpoint + "/" + (index + 1);
      fragment.append(page);
    });
    pages.replaceChildren(fragment);
    toggle.disabled = false;
  } catch {
    if (sequence === loadSequence) showMessage(pages, "无法打开此书籍");
  }
}

speed.addEventListener("input", () => { byId("speed-value").value = speed.value; });
toggle.addEventListener("click", () => {
  if (frame !== null) {
    stopScroll();
    return;
  }
  toggle.setAttribute("aria-pressed", "true");
  toggle.textContent = "暂停滚动";
  frame = requestAnimationFrame(scrollFrame);
});
byId("clear").addEventListener("click", () => {
  selected.clear();
  renderLibrary();
});
window.addEventListener("hashchange", route);

async function refresh() {
  clearTimeout(refreshTimer);
  const sequence = ++refreshSequence;
  try {
    const response = await fetch("/api/library", {cache: "no-store"});
    if (!response.ok) throw new Error("library");
    const updated = await response.json();
    if (sequence !== refreshSequence) return;
    const running = response.headers.get("X-Indexing") === "1";
    if (!initialized || indexing !== running || JSON.stringify(books) !== JSON.stringify(updated)) {
      initialized = true;
      indexing = running;
      books = updated;
      renderLibrary();
    }
    route();
    if (running) refreshTimer = setTimeout(refresh, 2500);
  } catch {
    if (sequence !== refreshSequence) return;
    initialized = false;
    byId("summary").textContent = "书库加载失败，正在重试";
    refreshTimer = setTimeout(refresh, 5000);
  }
}
for (const mode of ["list", "cover"]) {
  byId(mode + "-view").addEventListener("click", () => {
    viewMode = mode;
    try {
      localStorage.setItem("library-view", mode);
    } catch {
      // The view can still change when storage is unavailable.
    }
    renderLibrary();
  });
}

const settings = byId("settings");
const settingsAuth = byId("settings-auth");
const settingsForm = byId("settings-form");
const settingsStatus = byId("settings-status");
let adminToken = "";

function settingsRequest(values) {
  const headers = {};
  if (adminToken) headers["X-Admin-Token"] = adminToken;
  if (values) headers["Content-Type"] = "application/json";
  return fetch("/api/settings", {
    method: values ? "POST" : "GET",
    headers,
    body: values ? JSON.stringify(values) : undefined,
    cache: "no-store",
  });
}

function showSettings(config) {
  settingsAuth.hidden = true;
  settingsForm.hidden = false;
  byId("pdf-dir").value = config.pdf_dir;
  byId("images-dir").value = config.images_dir;
  byId("jm-username").value = config.jm_username;
  byId("jm-password").value = "";
  byId("jm-password").placeholder = config.has_password ? "已保存，留空保留" : "";
}

async function loadSettings() {
  try {
    const response = await settingsRequest();
    if (response.status === 403) {
      settingsAuth.hidden = false;
      settingsForm.hidden = true;
      settingsStatus.textContent = "请从主机本地访问设置，或输入管理员令牌。";
      return;
    }
    if (!response.ok) throw new Error("无法读取设置");
    settingsStatus.textContent = "";
    showSettings(await response.json());
  } catch (error) {
    settingsStatus.textContent = error.message;
  }
}

byId("settings-open").addEventListener("click", () => {
  settings.showModal();
  loadSettings();
});
byId("settings-close").addEventListener("click", () => settings.close());
settings.addEventListener("close", () => {
  byId("jm-password").value = "";
  byId("admin-token").value = "";
  adminToken = "";
  settingsForm.hidden = true;
  settingsAuth.hidden = true;
  settingsStatus.textContent = "";
});
settingsAuth.addEventListener("submit", event => {
  event.preventDefault();
  adminToken = byId("admin-token").value;
  byId("admin-token").value = "";
  loadSettings();
});
settingsForm.addEventListener("submit", async event => {
  event.preventDefault();
  const buttons = [...settingsForm.querySelectorAll("button")];
  buttons.forEach(button => { button.disabled = true; });
  try {
    const response = await settingsRequest({
      pdf_dir: byId("pdf-dir").value,
      images_dir: byId("images-dir").value,
      jm_username: byId("jm-username").value,
      jm_password: byId("jm-password").value,
      refresh: event.submitter?.value === "refresh",
    });
    if (!response.ok) {
      const result = await response.json().catch(() => ({}));
      throw new Error(result.error || "保存设置失败");
    }
    const config = await response.json();
    if (settings.open) {
      showSettings(config);
      settingsStatus.textContent = "配置已保存。标签更新进度可在书库查看。";
    }
    refresh();
  } catch (error) {
    if (settings.open) settingsStatus.textContent = error.message;
  } finally {
    buttons.forEach(button => { button.disabled = false; });
  }
});

refresh();
