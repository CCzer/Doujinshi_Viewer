"use strict";

const byId = id => document.getElementById(id);
const library = byId("library");
const reader = byId("reader");
const pages = byId("pages");
const toggle = byId("scroll-toggle");
const speed = byId("speed");
const pageSize = byId("page-size");
const bookSearch = byId("book-search");
const BOOKS_PER_PAGE = 20;
const COLLAPSED_TAG_COUNT = 15;
const BOOK_TAG_PREVIEW_COUNT = 5;
const groups = {works: "作品", actors: "登场人物", tags: "分类标签", authors: "作者"};
const statuses = {pending: "等待标签", unmatched: "未找到匹配", ambiguous: "存在多个匹配", error: "索引失败"};
const selected = new Set();
let books = [];
let bookPage = 1;
const expandedTagGroups = new Set();
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
    bookPage = 1;
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
  const query = bookSearch.value.trim().toLowerCase();
  const shown = books.filter(book => book.name.toLowerCase().includes(query)
    && [...selected].every(key => book.tags.some(tag => tagKey(tag) === key)));
  const pageCount = Math.max(1, Math.ceil(shown.length / BOOKS_PER_PAGE));
  bookPage = Math.min(bookPage, pageCount);
  const offset = (bookPage - 1) * BOOKS_PER_PAGE;
  const pending = books.filter(book => book.status === "pending").length;
  const unresolved = books.filter(book => !["pending", "indexed", "manual"].includes(book.status)).length;
  byId("summary").textContent = [
    books.length + " 本同人志",
    indexing ? "正在更新标签" : "",
    pending ? pending + " 本待索引" : "",
    unresolved ? unresolved + " 本标签待确认" : "",
  ].filter(Boolean).join(" · ");
  byId("clear").hidden = !selected.size;
  byId("results-title").textContent = selected.size || query ? "筛选结果" : "全部同人志";
  byId("count").textContent = shown.length + " 本";

  facets.replaceChildren();
  for (const [kind, label] of Object.entries(groups)) {
    const entries = [...known.values()].filter(entry => entry.tag.kind === kind)
      .sort((a, b) => b.count - a.count || a.tag.name.localeCompare(b.tag.name, "zh-CN"));
    if (!entries.length) continue;

    const expanded = expandedTagGroups.has(kind);
    const section = document.createElement("section");
    section.className = "facet";
    const title = document.createElement("h3");
    title.textContent = label;
    section.setAttribute("aria-label", label);
    const chips = document.createElement("div");
    chips.className = "chips";
    chips.id = "facet-tags-" + kind;
    const visible = expanded ? entries : entries.slice(0, COLLAPSED_TAG_COUNT);
    for (const {tag, count} of visible) chips.append(tagButton(tag, count));
    section.append(title, chips);

    if (entries.length > COLLAPSED_TAG_COUNT) {
      const button = document.createElement("button");
      button.type = "button";
      button.id = "facet-expand-" + kind;
      button.className = "facet-expand";
      button.textContent = expanded ? "收起" : "展开";
      button.setAttribute("aria-label", button.textContent + label);
      button.setAttribute("aria-controls", chips.id);
      button.setAttribute("aria-expanded", expanded);
      button.addEventListener("click", () => {
        if (expanded) expandedTagGroups.delete(kind);
        else expandedTagGroups.add(kind);
        renderLibrary();
        byId(button.id)?.focus({preventScroll: true});
      });
      section.append(button);
    }

    facets.append(section);
  }
  if (!known.size) showMessage(facets, "暂无可用标签");

  results.replaceChildren();
  results.classList.toggle("cover-grid", viewMode === "cover");
  byId("list-view").setAttribute("aria-pressed", viewMode === "list");
  byId("cover-view").setAttribute("aria-pressed", viewMode === "cover");
  for (const book of shown.slice(offset, offset + BOOKS_PER_PAGE)) {
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
    const chips = document.createElement("div");
    chips.className = "chips";
    for (const tag of book.tags.slice(0, BOOK_TAG_PREVIEW_COUNT)) chips.append(tagButton(tag));
    const edit = document.createElement("button");
    edit.type = "button";
    edit.className = "book-edit";
    edit.textContent = "编辑标签";
    edit.addEventListener("click", () => openTagEditor(book));
    if (viewMode === "list") {
      chips.append(edit);
      entry.append(chips);
    } else {
      if (book.tags.length) entry.append(chips);
      entry.append(edit);
    }
    results.append(entry);
  }
  if (!shown.length) {
    const emptyMessage = query ? "没有符合搜索条件的同人志"
      : selected.size ? "没有同时包含所选标签的同人志" : "书库为空";
    showMessage(results, emptyMessage);
  }
  byId("pagination").hidden = pageCount === 1;
  byId("page-number").textContent = "第 " + bookPage + " 页，共 " + pageCount + " 页";
  byId("previous-page").disabled = bookPage === 1;
  byId("next-page").disabled = bookPage === pageCount;
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

function resizePages() {
  const scale = Number(pageSize.value);
  const relativeTop = pages.scrollTop / Math.max(1, pages.scrollHeight);
  byId("page-size-value").value = scale + "%";
  pages.style.setProperty("--page-width", scale + "%");
  pages.style.setProperty("--page-max-width", (scale * 10) + "px");
  pages.scrollTop = relativeTop * pages.scrollHeight;
}

pageSize.addEventListener("input", resizePages);
resizePages();
toggle.addEventListener("click", () => {
  if (frame !== null) {
    stopScroll();
    return;
  }
  toggle.setAttribute("aria-pressed", "true");
  toggle.textContent = "暂停滚动";
  frame = requestAnimationFrame(scrollFrame);
});
bookSearch.addEventListener("input", () => {
  bookPage = 1;
  renderLibrary();
});

byId("clear").addEventListener("click", () => {
  selected.clear();
  bookPage = 1;
  renderLibrary();
});
function changePage(step) {
  bookPage += step;
  renderLibrary();
  byId("results-title").scrollIntoView({block: "start"});
}

byId("previous-page").addEventListener("click", () => changePage(-1));
byId("next-page").addEventListener("click", () => changePage(1));
window.addEventListener("hashchange", () => {
  if (byId("setup").hidden && initialized) route();
});

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

const tagEditor = byId("tag-editor");
const editableTags = byId("editable-tags");
const tagForm = byId("tag-form");
const tagName = byId("tag-name");
const tagStatus = byId("tag-status");
let editingBookId = null;

function showEditableTags(tags) {
  editableTags.replaceChildren();
  for (const tag of tags) {
    const remove = document.createElement("button");
    remove.type = "button";
    remove.textContent = groups[tag.kind] + " · " + tag.name + " ×";
    remove.setAttribute("aria-label", "删除" + groups[tag.kind] + " " + tag.name);
    remove.addEventListener("click", () => editTag(tag, false));
    editableTags.append(remove);
  }
  if (!tags.length) showMessage(editableTags, "暂无标签");
}

function openTagEditor(book) {
  editingBookId = book.id;
  tagForm.reset();
  tagStatus.textContent = "";
  byId("tag-book-name").textContent = book.name;
  showEditableTags(book.tags);
  tagEditor.showModal();
  tagName.focus();
}

async function editTag(tag, present) {
  const bookId = editingBookId;
  const controls = [...tagEditor.querySelectorAll("#editable-tags button, #tag-form button")];
  controls.forEach(control => { control.disabled = true; });
  tagStatus.textContent = "";
  try {
    const response = await fetch("/api/books/" + bookId + "/tags", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({...tag, present}),
    });
    if (!response.ok) throw new Error("保存标签失败，请刷新书库后重试。");
    const tags = await response.json();
    if (tagEditor.open && editingBookId === bookId) {
      showEditableTags(tags);
      if (present) tagName.value = "";
      tagName.focus();
      tagStatus.textContent = present ? "标签已添加" : "标签已删除";
    }
    refresh();
  } catch (error) {
    if (tagEditor.open && editingBookId === bookId) tagStatus.textContent = error.message;
  } finally {
    controls.forEach(control => { control.disabled = false; });
  }
}

byId("tag-editor-close").addEventListener("click", () => tagEditor.close());
tagForm.addEventListener("submit", event => {
  event.preventDefault();
  const name = tagName.value.trim();
  if (name) editTag({kind: byId("tag-kind").value, name}, true);
});

const settings = byId("settings");
const settingsAuth = byId("settings-auth");
const settingsForm = byId("settings-form");
const settingsStatus = byId("settings-status");
let adminPassword = "";

function settingsRequest(values) {
  const headers = {};
  if (adminPassword) headers["X-Admin-Password"] = encodeURIComponent(adminPassword);
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
  byId("zip-dir").value = config.zip_dir;
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
      settingsStatus.textContent = adminPassword ? "管理员密码不正确。" : "请输入管理员密码。";
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
  byId("admin-password").value = "";
  adminPassword = "";
  settingsForm.hidden = true;
  settingsAuth.hidden = true;
  settingsStatus.textContent = "";
});
settingsAuth.addEventListener("submit", event => {
  event.preventDefault();
  adminPassword = byId("admin-password").value;
  byId("admin-password").value = "";
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
      zip_dir: byId("zip-dir").value,
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

const setupForm = byId("setup-form");
const setupPanels = [...setupForm.querySelectorAll(".setup-panel")];
const setupNext = byId("setup-next");
const setupError = byId("setup-error");
let setupStep = 0;

function showSetupStep(step) {
  setupStep = step;
  setupPanels.forEach((panel, index) => { panel.hidden = index !== step; });
  byId("setup-step").textContent = "第 " + (step + 1) + " 步，共 " + setupPanels.length + " 步";
  byId("setup-back").hidden = step === 0;
  setupNext.textContent = step === setupPanels.length - 1 ? "保存配置" : "下一步";
  setupPanels[step].querySelector("input").focus();
}

function showRestartMessage() {
  setupForm.reset();
  setupForm.hidden = true;
  byId("setup-step").textContent = "";
  byId("setup-complete").hidden = false;
}

byId("setup-back").addEventListener("click", () => {
  setupError.textContent = "";
  showSetupStep(setupStep - 1);
});

setupForm.addEventListener("submit", async event => {
  event.preventDefault();
  setupError.textContent = "";
  const admin = byId("setup-admin").value;
  const username = byId("setup-jm-username").value.trim();
  const password = byId("setup-jm-password").value;
  if (setupStep === 0 && (!admin.trim() || admin !== byId("setup-confirm").value)) {
    setupError.textContent = "请输入管理员密码，并确认两次输入一致。";
    return;
  }
  if (setupStep === 1 && Boolean(username) !== Boolean(password)) {
    setupError.textContent = "JM 账号和密码需要同时填写，或同时留空。";
    return;
  }
  if (setupStep < setupPanels.length - 1) {
    showSetupStep(setupStep + 1);
    return;
  }

  setupNext.disabled = true;
  byId("setup-back").disabled = true;
  try {
    const response = await fetch("/api/setup", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({
        admin_password: admin,
        jm_username: username,
        jm_password: password,
        pdf_dir: byId("setup-pdf-dir").value,
        images_dir: byId("setup-images-dir").value,
        zip_dir: byId("setup-zip-dir").value,
      }),
    });
    if (response.status === 409) {
      await initialize();
      return;
    }
    const result = await response.json();
    if (!response.ok) {
      const fields = ["pdf_dir", "images_dir", "zip_dir"];
      const field = fields.findIndex(name => result.error?.endsWith(name));
      if (field >= 0) showSetupStep(field + 2);
      throw new Error(result.error || "保存配置失败");
    }
    showRestartMessage();
  } catch (error) {
    setupError.textContent = error.message;
  } finally {
    setupNext.disabled = false;
    byId("setup-back").disabled = false;
  }
});

async function initialize() {
  try {
    const response = await fetch("/api/setup", {cache: "no-store"});
    if (!response.ok) throw new Error("setup status");
    const state = await response.json();
    if (state.setup_required || state.restart_required) {
      library.hidden = true;
      byId("setup").hidden = false;
      if (state.restart_required) showRestartMessage();
      else {
        setupForm.hidden = false;
        for (const name of ["pdf", "images", "zip"]) {
          const input = byId("setup-" + name + "-dir");
          input.value = state.default_paths[name + "_dir"] ?? "";
        }
        showSetupStep(0);
      }
      return;
    }
    byId("setup").hidden = true;
    library.hidden = false;
    refresh();
  } catch {
    byId("setup").hidden = false;
    byId("setup-step").textContent = "无法读取配置状态，正在重试…";
    setupForm.hidden = true;
    setTimeout(initialize, 3000);
  }
}

initialize();
