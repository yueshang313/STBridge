import {
  avatarLetter,
  featureChips,
  installFold,
  todayCards,
  healthBanner,
  recoveryBannerText,
  healthChain,
  healthHeadline,
  healthLevel,
  healthTitle,
  dependencyStateText,
  importDoneText,
  importItemText,
  INSTALL_PATH_FIELDS,
  installButtons,
  installOverview,
  installPathsPayload,
  installPathsProblems,
  installStateBadge,
  jobStatusText,
  LAUNCHER_HINT,
  pathStatusText,
  restartNotice,
  applyTtsPatch,
  buildEmotionPayload,
  buildPlanBody,
  buildPresetPrompt,
  coerceSection,
  emptyPlanForm,
  ENGINE_LABELS,
  formatBytes,
  formatDateTime,
  GROUP_SEND_NOTE,
  nextFireText,
  chatStatusText,
  chatCountText,
  chatPayload,
  validateChatForm,
  CHAT_LIMITS,
  planPayload,
  planSummary,
  planToForm,
  resolvePage,
  serviceButtons,
  serviceStateText,
  STICKER_ACCEPT,
  stickerGroupTitle,
  stickerSettingsPayload,
  stoppedNotice,
  coverErrorText,
  bytesText,
  CLEANUP_TARGETS,
  cleanupResultText,
  cleanupSettingsPayload,
  lastAutoText,
  validateKeepDays,
  movePreset,
  INJECTION_MAX_ENTRIES,
  INJECTION_PLACEMENTS,
  INJECTION_ROLES,
  INJECTION_TARGETS,
  injectionEntryNote,
  injectionPayload,
  newInjectionEntry,
  restoreBuiltinEntries,
  pedalboardInstallCommand,
  PITCH_PRESET_LIMIT,
  pitchPresetsPayload,
  splitKeywords,
  songRequestPayload,
  keyStatusText,
  MUSIC_SOURCES,
  parseSources,
  searchErrorLines,
  songName,
  sourceLabel,
  validateOnlineName,
  formatClock,
  indexText,
  parseClock,
  timingText,
  targetLabel,
  testResultLines,
  uploadSummary,
  validatePlanForm,
  WEEKDAY_LABELS,
  DEFAULT_NICKNAME,
  textsNoticeText,
  networkPayload,
  ownerPayload,
  ownerProblem,
  ownerCurrentText,
  ownerClaimReplaces,
  ownerClaimView,
  connectorStatusText,
  chatPlatformPayload,
  chatPlatformProblems,
  onebotFormatWarning,
  tokenDisplay,
  onebotStatusText,
  platformChoiceNote,
  logsText,
  networkSavedNotice,
  networkStatusText,
  placeholderText,
  textGroups,
  textsPayload,
  textsToRaw,
  applyNotifyText,
  emptyStateMarkup,
  tabLabel,
  menuPlacement,
  nextMenuIndex,
  playTimeText,
  fieldMarkup,
  moduleKey,
  moduleMarkup,
  applyResultText,
  presetBadge,
  importResultText,
  presetLabel,
  presetNameProblem,
  presetTime,
  unknownPlaceholders,
  wakePrefixHint,
  AFFINITY_SORTS,
  agoText,
  dirtyPages,
  dirtyTabs,
  emotionNames,
  emotionStatusText,
  FEATURE_LINKS,
  hashFor,
  layoutRows,
  LIST_PAGE_SIZE,
  matchesQuery,
  pageOfRow,
  parseCollapsed,
  resolveTab,
  sortUserOrder,
  splitTarget,
  STICKER_PAGE_SIZE,
  tabFromHash,
  diffFields,
  mergeCoverPatch,
  mergeSnapshot,
  pageFromHash,
  resolveTarget,
  onboardingSteps,
  PAGE_LABELS,
  searchEntries,
  searchPathText,
  normalizeSearchText,
  filterLogLines,
  highlightSegments,
  logCountText,
  installBackupsText,
  onboardingView,
  auditionProfile,
  auditionErrorText,
  testChatProblemText,
  TESTCHAT_NOTE,
  runSaves,
  saveFailureText,
  unsavedText,
  saveBarText,
  fieldKeyText,
  readHelpOpen,
  writeHelpOpen,
  restoreValues,
  useSegmented,
  coreStatusView,
  platformName
} from "/static/js/logic.js";

const state = {
  config: null,
  models: [],
  characters: [],
  selectedCharacter: "",
  discovered: [],
  tts: null,
  engine: null,
  switching: false,
  service: null,
  serviceBusy: false,
  serviceNote: { text: "", error: false },
  openEmotions: new Set(),
  openAdvanced: new Set(),
  features: null,
  texts: null,
  network: null,
  wakePrefix: null,
  openStickerGroups: new Set(),
  schedule: null,
  testing: new Set(),
  cover: null,
  singing: false,
  queue: null,
  stickers: null,
  affinity: null,
  injection: null,
  presets: null,
  install: null,
  deps: null,
  importPreview: null
};

// Until the settings load, behave like the default engine.
const currentEngine = () => state.engine || "irodori";

const $ = (id) => document.getElementById(id);

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>\"']/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&#39;"
  }[char]));
}

async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  if (options.body && !(options.body instanceof FormData) && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  const response = await fetch(path, { ...options, headers });
  let payload = {};
  try { payload = await response.json(); } catch { /* empty response */ }
  if (!response.ok) {
    const error = new Error(payload.error || `HTTP ${response.status}`);
    error.status = response.status;
    error.payload = payload;
    throw error;
  }
  // Any saved change can make the current preset "modified": look again shortly after.
  if (options.method && options.method !== "GET" && !path.startsWith("/api/presets")) scheduleLoadPresets();
  return payload;
}

let toastTimer = null;
function notify(text, isError = false) {
  const toast = $("console-toast");
  toast.textContent = text;
  toast.classList.toggle("console-toast-error", isError);
  toast.hidden = !text;
  clearTimeout(toastTimer);
  if (text) toastTimer = setTimeout(() => { toast.hidden = true; }, isError ? 8000 : 4000);
}

let errorTimer = null;
function showError(error) {
  console.error(error);
  $("console-error-text").textContent = error instanceof Error ? error.message : String(error);
  $("console-error").hidden = false;
  clearTimeout(errorTimer);
  errorTimer = setTimeout(() => { $("console-error").hidden = true; }, 8000);
}

// ---------- In-page dialogs (no native alert / confirm / prompt) ----------

const dialogState = { resolve: null, cancelValue: null, primaryValue: null };

/** Shows the shared dialog; resolves to {value, input} where value is the clicked button's value. */
function showDialog({ title = "", text = "", input = null, buttons, cancelValue = null }) {
  if (dialogState.resolve) finishDialog(dialogState.cancelValue);
  $("app-dialog-title").textContent = title;
  $("app-dialog-text").textContent = text;
  const field = $("app-dialog-input");
  field.hidden = !input;
  field.readOnly = Boolean(input?.readOnly);
  field.value = input?.value ?? "";
  const row = $("app-dialog-buttons");
  row.innerHTML = "";
  let primary = null;
  dialogState.primaryValue = null;
  for (const item of buttons) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `btn ${item.kind === "primary" ? "btn-primary" : item.kind === "danger" ? "btn-danger" : "btn-secondary"}`;
    button.textContent = item.label;
    button.addEventListener("click", () => finishDialog(item.value));
    row.appendChild(button);
    if (item.primary) { primary = button; dialogState.primaryValue = item.value; }
  }
  dialogState.cancelValue = cancelValue;
  $("app-dialog").hidden = false;
  if (input) { field.focus(); field.select(); } else if (primary) primary.focus();
  return new Promise((resolve) => { dialogState.resolve = resolve; });
}

function finishDialog(value) {
  const resolve = dialogState.resolve;
  if (!resolve) return;
  dialogState.resolve = null;
  const input = $("app-dialog-input").value;
  $("app-dialog").hidden = true;
  resolve({ value, input });
}

function bindDialog() {
  // Capture phase: Esc / Enter belong to the dialog while it is open.
  document.addEventListener("keydown", (event) => {
    if ($("app-dialog").hidden) return;
    if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); finishDialog(dialogState.cancelValue); }
    else if (event.key === "Enter" && !(event.target instanceof HTMLButtonElement) && dialogState.primaryValue !== null) {
      event.preventDefault(); event.stopPropagation(); finishDialog(dialogState.primaryValue);
    }
  }, true);
}

/** Resolves true when confirmed. Delete-style questions pass {danger: true}: red button labelled 删除. */
async function confirmDialog(text, { danger = false, confirmLabel = danger ? "删除" : "确定", title = "请确认" } = {}) {
  const result = await showDialog({
    title, text, cancelValue: false,
    buttons: [
      { label: "取消", value: false, kind: "secondary" },
      { label: confirmLabel, value: true, kind: danger ? "danger" : "primary", primary: true }
    ]
  });
  return result.value === true;
}

/** Resolves the typed text, or null when cancelled. */
async function promptDialog(text, defaultValue = "") {
  const result = await showDialog({
    title: "请输入", text, input: { value: defaultValue }, cancelValue: null,
    buttons: [{ label: "取消", value: null, kind: "secondary" }, { label: "确定", value: "ok", kind: "primary", primary: true }]
  });
  return result.value === "ok" ? result.input : null;
}

/** Read-only, already selected text for manual copying (when the clipboard is unavailable). */
async function copyDialog(text) {
  await showDialog({
    title: "复制以下内容", text: "浏览器不允许自动复制，请按 Ctrl+C 复制：", input: { value: text, readOnly: true },
    buttons: [{ label: "关闭", value: "ok", kind: "primary", primary: true }], cancelValue: "ok"
  });
}

async function choiceDialog(text, buttons, cancelValue = "cancel", title = "有未保存的修改") {
  return (await showDialog({ title, text, buttons, cancelValue })).value;
}

// ---------- Unsaved changes: parts, snapshots and the page save bar ----------
//
// Rule shown in the console: editing an input / checkbox / select / slider is a change that only takes
// effect after "保存"; a button runs its action at once. A "part" is a group of fields saved together;
// its snapshot is what the fields held when last filled or saved, and a part is dirty while the
// current values differ from it (typing a value back clears the mark). Only the page on screen can be
// dirty: leaving a dirty page asks first.

const parts = [];
const P = {};
let forcedFill = new Set();
let saving = false;
let saveError = "";

function definePart(definition) {
  const part = { snapshot: null, ...definition };
  parts.push(part);
  return part;
}

const partsOf = (page) => parts.filter((part) => part.page === page);
const activePage = () => document.querySelector(".page.active")?.id || "dashboard";
const fieldsById = (map) => () => Object.fromEntries(Object.entries(map).map(([key, id]) => [key, $(id)]));
const namedFields = (formId) => () => Object.fromEntries([...$(formId).elements].filter((element) => element.name).map((element) => [element.name, element]));

function valueOf(element) {
  return element.type === "checkbox" ? element.checked : element.value;
}

function readValues(part) {
  const values = {};
  for (const [key, element] of Object.entries(part.collect())) values[key] = valueOf(element);
  return values;
}

function applyValues(part, values) {
  for (const [key, element] of Object.entries(part.collect())) {
    if (!(key in values)) continue;
    if (element.type === "checkbox") element.checked = Boolean(values[key]);
    else element.value = values[key];
  }
  if (part.id === "tts-common") syncSplitFields();
}

const partOpen = (part) => !part.isOpen || part.isOpen();
const partDiff = (part) => (part.snapshot && partOpen(part) ? diffFields(part.snapshot, readValues(part)) : []);
const isDirty = (part) => partDiff(part).length > 0;
const takeSnapshot = (part) => { part.snapshot = partOpen(part) ? readValues(part) : null; };
const barParts = (page) => partsOf(page).filter((part) => !part.inline);
const pageIsDirty = (page) => partsOf(page).some(isDirty);

/**
 * Fill a part from the loaded data. A part with unsaved edits is left alone ("skip", the default),
 * re-rendered with the typed values kept ("keep", for lists that change under the user) or overwritten
 * ("force"). Parts that were just saved or discarded are forced.
 */
function fillPart(part, mode = "skip") {
  if (forcedFill.has(part.id)) mode = "force";
  if (part.snapshot && isDirty(part)) {
    if (mode === "skip") return false;
    if (mode === "keep") {
      const before = readValues(part);
      const old = part.snapshot;
      part.fill();
      const merged = mergeSnapshot(old, before, readValues(part));
      applyValues(part, merged.values);
      part.snapshot = merged.snapshot;
      refreshDirtyUi();
      return true;
    }
  }
  part.fill();
  takeSnapshot(part);
  refreshDirtyUi();
  return true;
}

function clearFieldErrors() {
  document.querySelectorAll(".field-error").forEach((element) => element.classList.remove("field-error"));
  document.querySelectorAll(".field-msg").forEach((message) => { message.hidden = true; message.textContent = ""; });
}

/** Show a problem under its field (the page save bar still lists it as well). */
function setFieldMessage(element, text) {
  const message = element.closest(".field")?.querySelector(".field-msg");
  if (!message || !text) return;
  message.textContent = text;
  message.hidden = false;
}

function setSaveError(text) {
  saveError = text;
  refreshDirtyUi();
}

function markTarget(element) {
  return element.type === "checkbox" ? element.closest("label") || element : element;
}

function refreshDirtyUi() {
  const page = activePage();
  document.querySelectorAll(".field-dirty").forEach((element) => element.classList.remove("field-dirty"));
  document.querySelectorAll(".card-dirty").forEach((element) => element.classList.remove("card-dirty"));
  document.querySelectorAll(".details-dirty").forEach((element) => element.classList.remove("details-dirty"));
  const tabItems = [];
  let count = 0;
  for (const part of partsOf(page)) {
    const diff = partDiff(part);
    if (!diff.length) continue;
    if (!part.inline) count += diff.length;
    if (part.tab) tabItems.push({ tab: part.tab, count: diff.length });
    const elements = part.collect();
    for (const key of diff) {
      const element = elements[key];
      if (!element) continue;
      markTarget(element).classList.add("field-dirty");
      element.closest(".card")?.classList.add("card-dirty");
      for (let details = element.closest("details"); details; details = details.parentElement?.closest("details")) details.classList.add("details-dirty");
    }
    // rows added or removed have no element of their own: mark the card that holds the list
    if (diff.some((key) => !elements[key])) part.anchor?.()?.closest(".card")?.classList.add("card-dirty");
  }
  const bar = $("save-bar");
  if (count === 0) saveError = saveError && saving ? saveError : "";
  bar.hidden = count === 0 && !saveError;
  $("save-bar-count").textContent = count ? saveBarText(count) : "";
  $("save-bar-error").textContent = saveError;
  $("save-bar-error").hidden = !saveError;
  const dirtyTabSet = TABBED_PAGES.includes(page) ? dirtyTabs(tabItems) : new Set();
  for (const tabbed of TABBED_PAGES) {
    document.querySelectorAll(`#${tabbed} .tab`).forEach((button) => { button.querySelector(".tab-dot").hidden = tabbed !== page || !dirtyTabSet.has(button.dataset.tab); });
  }
  refreshPagers();
  decorateRows(page);
}

function fieldLabel(element) {
  const named = element.closest(".field")?.querySelector(".field-label")?.textContent.trim();
  if (named) return named;
  const label = element.closest("label");
  const first = label ? [...label.childNodes].find((node) => node.nodeType === 3 && node.textContent.trim()) : null;
  const text = (first?.textContent || element.getAttribute("aria-label") || element.name || element.id || "字段").trim();
  return text.split(/[（(]/)[0].trim() || "字段";
}

/** Problems of the dirty parts, found before anything is sent: native field rules plus each part's own checks. */
function validateParts(dirty) {
  const problems = [];
  let first = null;
  for (const part of dirty) {
    const elements = part.collect();
    for (const [key, element] of Object.entries(elements)) {
      if (element.willValidate && !element.checkValidity()) {
        revealElement(element);
        problems.push(`${part.tab ? `${tabLabel(part.page, part.tab)} › ` : ""}${part.title}：${fieldLabel(element)}${element.validationMessage ? `（${element.validationMessage}）` : "不合法"}`);
        element.classList.add("field-error");
        setFieldMessage(element, element.validationMessage || "不合法");
        first = first || element;
      }
    }
    for (const problem of part.validate ? part.validate(readValues(part)) : []) {
      problems.push(`${part.tab ? `${tabLabel(part.page, part.tab)} › ` : ""}${part.title}：${problem.text}`);
      const element = problem.key ? elements[problem.key] : null;
      if (element) { revealElement(element); element.classList.add("field-error"); setFieldMessage(element, problem.text); first = first || element; }
    }
  }
  return { problems, first };
}

function buildSteps(dirty) {
  const steps = [];
  let merged = null;
  for (const part of dirty) {
    const values = readValues(part);
    if (part.coverPatch) {
      // service, parameters, presets and music settings share one POST
      if (!merged) { merged = { id: "cover-settings", label: "翻唱设置", partIds: [], patches: [] }; steps.push(merged); }
      merged.partIds.push(part.id);
      merged.patches.push(part.coverPatch(values));
      continue;
    }
    const changed = diffFields(part.snapshot, values);
    steps.push({ id: part.id, label: part.tab ? `${tabLabel(part.page, part.tab)} › ${part.title}` : part.title, partIds: [part.id], run: () => part.save(values, changed) });
  }
  if (merged) merged.run = () => saveCoverSettings(merged.patches);
  return steps;
}

/** Re-fetch the data of a page (used after a save and after discarding). */
async function reloadPage(page) {
  if (page === "overview") await refreshOverview(true);
  else if (page === "install") { await loadInstall(); await loadDeps(); }
  else if (page === "voice") { await loadTts(); await loadAll(false); }
  else if (page === "emotions") await loadAll(false);
  else if (page === "stickers") await loadStickers();
  else if (page === "schedule") await loadSchedule();
  else if (page === "sing") await loadCover();
  else if (page === "affinity") await loadAffinity();
  else if (page === "injection") await loadInjection();
  else if (page === "connect") { await loadChatPlatform(); await loadOwner(); await loadQueue(); }
  else if (page === "system") { await loadCleanup(); await loadTexts(); await loadNetwork(); await loadLogs(); await loadPresets(); }
}

/** Save every dirty part of the page, in page order, stopping at the first failure. True when all were saved. */
async function savePage(page = activePage()) {
  if (saving) return false;
  const dirty = barParts(page).filter(isDirty);
  if (!dirty.length) return true;
  clearFieldErrors();
  const { problems, first } = validateParts(dirty);
  if (problems.length) {
    setSaveError(problems.join("；"));
    if (first) reveal(first);
    return false;
  }
  const steps = buildSteps(dirty);
  saving = true;
  setSaveError("");
  $("save-bar-save").disabled = true; $("save-bar-discard").disabled = true; $("save-bar-save").textContent = "保存中…";
  let outcome;
  try {
    outcome = await runSaves(steps);
  } finally {
    saving = false;
    $("save-bar-save").disabled = false; $("save-bar-discard").disabled = false; $("save-bar-save").textContent = "保存";
  }
  if (outcome.done.length) {
    forcedFill = new Set(steps.filter((step) => outcome.done.includes(step.id)).flatMap((step) => step.partIds));
    try { await reloadPage(page); } catch (error) { showError(error); } finally { forcedFill = new Set(); }
  }
  if (outcome.done.length) notify(outcome.notices.length ? outcome.notices.join("；") : "已保存");
  setSaveError(outcome.failed ? saveFailureText(outcome.failed) : "");
  return !outcome.failed;
}

async function discardPage(page = activePage()) {
  for (const part of partsOf(page)) part.close?.();
  forcedFill = new Set(partsOf(page).map((part) => part.id));
  try { await reloadPage(page); } catch (error) { showError(error); } finally { forcedFill = new Set(); }
  clearFieldErrors();
  setSaveError("");
}

/** True when it is fine to leave / reload the page: nothing unsaved, or the user chose to save or drop it. */
async function guardLeave() {
  const page = activePage();
  const bar = barParts(page).filter(isDirty);
  const cards = partsOf(page).filter((part) => part.inline && isDirty(part));
  if (!bar.length && !cards.length) return true;
  const cardNote = cards.length ? `\n（${cards.map((part) => part.title).join("、")}的表单卡片还没保存，继续会丢弃它们。）` : "";
  if (!bar.length) {
    const choice = await choiceDialog(`${cards.map((part) => part.title).join("、")}的表单卡片里的修改还没保存，要丢弃吗？`, [
      { label: "取消", value: "cancel", kind: "secondary" },
      { label: "丢弃并继续", value: "discard", kind: "danger", primary: true }
    ]);
    if (choice !== "discard") return false;
    await discardPage(page);
    return true;
  }
  const total = bar.reduce((sum, part) => sum + partDiff(part).length, 0);
  const choice = await choiceDialog(`${unsavedText(total)}，要先保存吗？${cardNote}`, [
    { label: "取消", value: "cancel", kind: "secondary" },
    { label: "不保存", value: "discard", kind: "secondary" },
    { label: "保存并继续", value: "save", kind: "primary", primary: true }
  ]);
  if (choice === "save") return savePage(page);
  if (choice === "discard") { await discardPage(page); return true; }
  return false;
}

async function confirmDiscardCard(part) {
  if (!isDirty(part)) return true;
  return confirmDialog("表单卡片里的修改还没保存，要丢弃吗？", { danger: true, confirmLabel: "丢弃" });
}

async function requestPage(page, tab = "") {
  ({ page, tab } = resolveTarget(page, tab));
  const target = page;
  if (target === activePage()) {
    if (tab && TABBED_PAGES.includes(target)) selectTab(target, tab);
    return;
  }
  if (!(await guardLeave())) {
    history.replaceState(null, "", hashFor(activePage(), pageTab[activePage()] || ""));
    return;
  }
  switchPage(target, tab);
}

function bindUnsaved() {
  const onEdit = (event) => {
    event.target.classList?.remove("field-error");
    if (event.target.name === "split_messages") syncSplitFields();
    refreshDirtyUi();
  };
  document.addEventListener("input", onEdit);
  document.addEventListener("change", onEdit);
  // Settings forms have no submit button any more; Enter must not reload the page.
  document.addEventListener("submit", (event) => { if (!event.defaultPrevented) event.preventDefault(); });
  window.addEventListener("beforeunload", (event) => {
    if (!parts.some(isDirty)) return;
    event.preventDefault();
    event.returnValue = "";
  });
  window.addEventListener("hashchange", () => {
    const { page, tab } = resolveTarget(pageFromHash(location.hash), tabFromHash(location.hash));
    if (page === activePage()) { if (TABBED_PAGES.includes(page)) selectTab(page, tab); }
    else requestPage(page, tab);
  });
  $("recovery-banner-dismiss").addEventListener("click", async () => {
    try { await api("/api/recovery/dismiss", { method: "POST", body: "{}" }); } catch (error) { console.error(error); }
    renderRecoveryBanner([]);
  });
  $("save-bar-save").addEventListener("click", () => { savePage(); });
  $("save-bar-discard").addEventListener("click", async () => {
    if (await confirmDialog("放弃本页所有未保存的修改？", { danger: true, confirmLabel: "放弃" })) await discardPage();
  });
}

// ---------- Sing page tabs ----------

const TABBED_PAGES = ["sing", "system"];
const pageTab = { sing: "library", system: "general" };

/** Tabs are not pages: switching them never asks about unsaved changes (one save bar covers the whole page). */
function selectTab(page, tab, { silent = false } = {}) {
  pageTab[page] = resolveTab(page, tab);
  document.querySelectorAll(`#${page} .tab`).forEach((button) => {
    const active = button.dataset.tab === pageTab[page];
    button.classList.toggle("active", active);
    button.setAttribute("aria-selected", String(active));
  });
  document.querySelectorAll(`#${page} .tab-panel`).forEach((panel) => { panel.hidden = panel.dataset.tab !== pageTab[page]; });
  if (!silent && activePage() === page) history.replaceState(null, "", hashFor(page, pageTab[page]));
}
const selectSingTab = (tab, options) => selectTab("sing", tab, options);

// ---------- Row menus (⋯) and the shared ▶ player ----------

const ICON_DOTS = '<svg viewBox="0 0 16 16" aria-hidden="true"><circle cx="3" cy="8" r="1.4"/><circle cx="8" cy="8" r="1.4"/><circle cx="13" cy="8" r="1.4"/></svg>';
const ICON_PLAY = '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M5 3.2v9.6L12.6 8z"/></svg>';
const ICON_PAUSE = '<svg viewBox="0 0 16 16" aria-hidden="true"><rect x="4" y="3" width="3" height="10" rx="0.8"/><rect x="9" y="3" width="3" height="10" rx="0.8"/></svg>';

/**
 * A row's ⋯ menu. The real buttons stay in the row (hidden) with their data-* attributes, so the handlers bound
 * to them after rendering keep working; the popup only shows copies that click the originals.
 */
function rowMenu(buttonsHtml) {
  if (!buttonsHtml) return "";
  return `<span class="row-menu"><button type="button" class="btn btn-secondary btn-sm menu-trigger" aria-haspopup="menu" aria-expanded="false" aria-label="更多操作">${ICON_DOTS}</button><span class="menu-source" hidden>${buttonsHtml}</span></span>`;
}

let openedMenu = null;

function closeMenu({ restoreFocus = false } = {}) {
  if (!openedMenu) return;
  const { trigger, pop } = openedMenu;
  openedMenu = null;
  pop.remove();
  trigger.setAttribute("aria-expanded", "false");
  if (restoreFocus && trigger.isConnected) trigger.focus();
}

function openMenu(trigger) {
  closeMenu();
  const sources = [...(trigger.closest(".row-menu")?.querySelectorAll(".menu-source > button") || [])];
  if (!sources.length) return;
  const pop = document.createElement("div");
  pop.className = "menu-pop";
  pop.setAttribute("role", "menu");
  for (const source of sources) {
    const item = document.createElement("button");
    item.type = "button";
    item.setAttribute("role", "menuitem");
    item.className = `menu-item${source.className.includes("btn-danger") ? " danger" : ""}`;
    item.textContent = source.textContent.trim();
    item.disabled = source.disabled;
    item.addEventListener("click", () => { focusReturn = trigger; closeMenu({ restoreFocus: true }); source.click(); });
    pop.appendChild(item);
  }
  document.body.appendChild(pop);
  const place = menuPlacement(trigger.getBoundingClientRect(), { width: pop.offsetWidth, height: pop.offsetHeight }, { width: window.innerWidth, height: window.innerHeight });
  pop.style.top = `${place.top}px`;
  pop.style.left = `${place.left}px`;
  pop.classList.toggle("up", place.up);
  trigger.setAttribute("aria-expanded", "true");
  openedMenu = { trigger, pop };
  pop.addEventListener("keydown", (event) => {
    const items = [...pop.querySelectorAll(".menu-item:not(:disabled)")];
    if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); closeMenu({ restoreFocus: true }); return; }
    if (event.key === "Tab") { closeMenu(); return; }
    if (["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) {
      event.preventDefault();
      items[nextMenuIndex(items.indexOf(document.activeElement), items.length, event.key)]?.focus();
    }
  });
  pop.querySelector(".menu-item:not(:disabled)")?.focus();
}

function bindMenus() {
  document.addEventListener("click", (event) => {
    const trigger = event.target.closest(".menu-trigger");
    if (!trigger) return;
    event.preventDefault();
    if (openedMenu?.trigger === trigger) closeMenu({ restoreFocus: true });
    else openMenu(trigger);
  });
  document.addEventListener("mousedown", (event) => {
    if (openedMenu && !openedMenu.pop.contains(event.target) && !event.target.closest(".menu-trigger")) closeMenu();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && openedMenu) { event.preventDefault(); closeMenu({ restoreFocus: true }); }
  });
  window.addEventListener("scroll", () => closeMenu(), true);
  window.addEventListener("resize", () => closeMenu());
}

/** ▶ button for one audio file; the page has one shared <audio>, started by the button's data-play-src. */
function playButton(src) {
  return `<span class="play-wrap"><button type="button" class="btn btn-secondary btn-sm play-btn" data-play-src="${escapeHtml(src)}" aria-label="播放">${ICON_PLAY}</button><small class="play-time" aria-live="off"></small></span>`;
}

const player = { button: null };

function playerReset({ failed = false } = {}) {
  const button = player.button;
  player.button = null;
  if (!button) return;
  button.innerHTML = ICON_PLAY;
  button.setAttribute("aria-label", "播放");
  button.classList.remove("playing");
  const time = button.closest(".play-wrap")?.querySelector(".play-time");
  if (time) { time.textContent = failed ? "无法播放" : ""; time.classList.toggle("play-failed", failed); }
}

function startPlayback(button) {
  const audio = $("shared-audio");
  if (player.button === button) {
    // the same button: pause (the second click on a playing row)
    audio.pause();
    playerReset();
    return;
  }
  audio.pause();
  playerReset();
  player.button = button;
  button.innerHTML = ICON_PAUSE;
  button.setAttribute("aria-label", "暂停");
  button.classList.add("playing");
  const time = button.closest(".play-wrap")?.querySelector(".play-time");
  if (time) { time.textContent = ""; time.classList.remove("play-failed"); }
  audio.src = button.dataset.playSrc;
  const started = audio.play();
  if (started && typeof started.catch === "function") started.catch(() => { if (player.button === button) playerReset({ failed: true }); });
}

function bindPlayer() {
  const audio = $("shared-audio");
  document.addEventListener("click", (event) => {
    const button = event.target.closest("[data-play-src]");
    if (button) { event.preventDefault(); startPlayback(button); }
  });
  audio.addEventListener("timeupdate", () => {
    const button = player.button;
    if (!button) return;
    if (!button.isConnected) { audio.pause(); playerReset(); return; }
    const time = button.closest(".play-wrap")?.querySelector(".play-time");
    if (time) time.textContent = playTimeText(audio.currentTime, audio.duration);
  });
  audio.addEventListener("ended", () => playerReset());
  audio.addEventListener("error", () => { if (player.button) playerReset({ failed: true }); });
}

// ---------- Right-hand drawer (edit model / character / plan) ----------
//
// The three edit forms are the same hidden cards as before; "hidden" is still what the open / save / cancel code toggles.
// A MutationObserver turns that into a drawer: overlay, focus in and trapped, Esc / overlay / ✕ ask before dropping edits.

const DRAWERS = [
  { id: "model-form-card", part: () => P.modelCard },
  { id: "character-form-card", part: () => P.characterCard },
  { id: "plan-form-card", part: () => P.planCard }
];
let drawerOpen = null;
let focusReturn = null;

const FOCUSABLE = 'a[href], button, input:not([type="hidden"]), select, textarea, [tabindex]:not([tabindex="-1"])';

function drawerFocusables(card) {
  return [...card.querySelectorAll(FOCUSABLE)].filter((element) => !element.disabled && !element.closest("[hidden]"));
}

/** Close a drawer; with edits that were not saved, ask first. Resolves whether it closed. */
async function requestCloseDrawer(id) {
  const def = DRAWERS.find((item) => item.id === id);
  const card = $(id);
  if (!def || card.hidden) return true;
  if (isDirty(def.part())) {
    const choice = await choiceDialog("这里的修改还没保存，放弃修改并关闭吗？", [
      { label: "继续编辑", value: "stay", kind: "secondary", primary: true },
      { label: "放弃修改", value: "discard", kind: "danger" }
    ], "stay", "放弃修改？");
    if (choice !== "discard") return false;
  }
  card.hidden = true;
  return true;
}

function onDrawerToggle(def) {
  const card = $(def.id);
  if (!card.hidden) {
    // only one drawer at a time
    for (const other of DRAWERS) if (other.id !== def.id && !$(other.id).hidden) $(other.id).hidden = true;
    drawerOpen = def.id;
    $("drawer-overlay").hidden = false;
    if (!card.contains(document.activeElement)) drawerFocusables(card)[0]?.focus();
    return;
  }
  if (drawerOpen === def.id) {
    drawerOpen = null;
    $("drawer-overlay").hidden = true;
    if (focusReturn?.isConnected) focusReturn.focus();
  }
}

function bindDrawers() {
  for (const def of DRAWERS) {
    const card = $(def.id);
    card.classList.add("drawer-card");
    card.setAttribute("role", "dialog");
    card.setAttribute("aria-modal", "true");
    const title = card.querySelector(":scope > h3");
    card.setAttribute("aria-labelledby", title.id);
    const head = document.createElement("div");
    head.className = "drawer-head";
    title.replaceWith(head);
    head.appendChild(title);
    const close = document.createElement("button");
    close.type = "button";
    close.className = "close-btn drawer-close";
    close.setAttribute("aria-label", "关闭");
    close.textContent = "✕";
    close.addEventListener("click", () => requestCloseDrawer(def.id));
    head.appendChild(close);
    new MutationObserver(() => onDrawerToggle(def)).observe(card, { attributes: true, attributeFilter: ["hidden"] });
  }
  $("drawer-overlay").addEventListener("click", () => { if (drawerOpen) requestCloseDrawer(drawerOpen); });
  // where the focus goes back to: the button that opened the drawer (for a menu item, the ⋯ that opened the menu)
  document.addEventListener("click", (event) => {
    if (event.target.closest(".menu-source, .menu-pop, .drawer-card, .dialog-overlay, .drawer-overlay")) return;
    focusReturn = event.target.closest("button, a[href]") || focusReturn;
  }, true);
  document.addEventListener("keydown", (event) => {
    if (!drawerOpen || event.defaultPrevented) return;
    if (!$("app-dialog").hidden || !$("path-picker").hidden) return;
    if (event.key === "Escape") { event.preventDefault(); requestCloseDrawer(drawerOpen); return; }
    if (event.key !== "Tab") return;
    const card = $(drawerOpen);
    const items = drawerFocusables(card);
    if (!items.length) return;
    const first = items[0];
    const last = items[items.length - 1];
    if (!card.contains(document.activeElement)) { event.preventDefault(); first.focus(); }
    else if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
  });
}

function bindEmptyStates() {
  document.addEventListener("click", (event) => {
    const action = event.target.closest("[data-empty-action]");
    if (action) { $(action.dataset.emptyAction)?.click(); return; }
    const upload = event.target.closest("[data-empty-upload]");
    if (upload) [...document.querySelectorAll("[data-upload-sticker]")].find((input) => input.dataset.uploadSticker === upload.dataset.emptyUpload)?.click();
  });
}

// ---------- Collapsible cards ----------

const COLLAPSE_STORAGE_KEY = "stbridge.console.collapsed";
let collapsedCards = {};

function loadCollapsed() {
  try { collapsedCards = parseCollapsed(localStorage.getItem(COLLAPSE_STORAGE_KEY)); } catch { collapsedCards = {}; }
}

function saveCollapsed() {
  try { localStorage.setItem(COLLAPSE_STORAGE_KEY, JSON.stringify(collapsedCards)); } catch { /* storage unavailable: the choice is simply not remembered */ }
}

function setCollapsed(card, collapsed, remember = true) {
  card.classList.toggle("collapsed", collapsed);
  card.querySelector(":scope > .card-title")?.setAttribute("aria-expanded", String(!collapsed));
  if (remember) {
    collapsedCards[card.dataset.cardKey] = collapsed;
    saveCollapsed();
  }
}

/** Every titled card outside the overview folds by clicking its title bar; the state is remembered per card. */
function enhanceCards() {
  loadCollapsed();
  const used = new Set();
  document.querySelectorAll(".page:not(#overview) .card").forEach((card) => {
    if (card.dataset.noCollapse !== undefined || card.classList.contains("console-note-card")) return;
    const title = card.querySelector(":scope > .card-header, :scope > h3");
    if (!title) return;
    const heading = title.matches("h3") ? title : title.querySelector("h3");
    let key = `${card.closest(".page").id}:${(heading?.childNodes[0]?.textContent || "").trim()}`;
    while (used.has(key)) key += "#";
    used.add(key);
    card.dataset.cardKey = key;
    card.classList.add("collapsible");
    title.classList.add("card-title");
    title.setAttribute("role", "button");
    title.tabIndex = 0;
    const toggle = () => setCollapsed(card, !card.classList.contains("collapsed"));
    title.addEventListener("click", (event) => { if (!event.target.closest("button, a, input, select, label")) toggle(); });
    title.addEventListener("keydown", (event) => {
      if ((event.key === "Enter" || event.key === " ") && event.target === title) { event.preventDefault(); toggle(); }
    });
    setCollapsed(card, collapsedCards[key] === true, false);
  });
}

/** Form modules are <details>: open by default, the choice is remembered; revealElement opens them like any fold. */
function enhanceModules(root = document) {
  root.querySelectorAll("details.module").forEach((module) => {
    if (module.dataset.moduleBound) return;
    module.dataset.moduleBound = "1";
    const key = moduleKey(module.dataset.module);
    module.open = collapsedCards[key] !== true;
    module.addEventListener("toggle", () => {
      collapsedCards[key] = !module.open;
      saveCollapsed();
    });
  });
}

/** Make an element visible: right tab, open folds and cards, and the list page that holds its row. */
function revealElement(element) {
  const panel = element.closest(".tab-panel");
  const panelPage = panel?.closest(".page")?.id;
  if (panel && panelPage && panel.dataset.tab !== pageTab[panelPage]) selectTab(panelPage, panel.dataset.tab);
  for (let details = element.closest("details"); details; details = details.parentElement?.closest("details")) details.open = true;
  const card = element.closest(".card.collapsed");
  if (card) setCollapsed(card, false);
  const row = element.closest("[data-list-row]");
  if (row) showListRow(row);
}

/** Show an element the user is about to work in: open every fold / card / tab around it, scroll to it and focus the first editable field. */
function reveal(element) {
  revealElement(element);
  element.scrollIntoView({ block: "nearest" });
  const field = element.matches("input, select, textarea") ? element : element.querySelector("input:not([readonly]):not([type=hidden]):not([type=file]):not([type=checkbox]), select, textarea");
  field?.focus();
}

// ---------- Paged lists (browser side only: every row stays in the page, the other pages are hidden) ----------

const pagedLists = new Map();

/** The list `key`; its page, search words and sort survive re-renders. `config` says where its rows are. */
function pagedList(key, config) {
  let list = pagedLists.get(key);
  if (!list) {
    list = { key, page: 1, query: "", sort: config.defaultSort || "", rows: [], matched: [] };
    pagedLists.set(key, list);
  }
  Object.assign(list, config);
  return list;
}

function applyList(list) {
  const container = list.container;
  if (!container) return;
  let rows = [...container.querySelectorAll(list.rowSelector)];
  if (list.sortOrder && rows.length) {
    const order = sortUserOrder(rows.map((row) => ({ updated_at: row.dataset.updated, value: Number(row.dataset.value), nickname: row.dataset.nickname, id: row.dataset.id })), list.sort);
    rows = order.map((index) => rows[index]);
    rows.forEach((row) => container.appendChild(row));
  }
  rows.forEach((row) => row.setAttribute("data-list-row", ""));
  const matches = rows.map((row) => matchesQuery(row.dataset.search || "", list.query));
  const layout = layoutRows(matches, list.page, list.size);
  list.page = layout.slice.page;
  rows.forEach((row, index) => { row.hidden = !layout.shown[index]; });
  list.rows = rows;
  list.matched = layout.matched;
  list.pages = layout.slice.pages;
  renderPager(list);
}

function pagerFor(list) {
  let pager = list.container.nextElementSibling;
  if (!pager || !pager.classList.contains("pager") || pager.dataset.pagerFor !== list.key) {
    pager = document.createElement("div");
    pager.className = "pager";
    pager.dataset.pagerFor = list.key;
    pager.addEventListener("click", (event) => {
      const button = event.target.closest("[data-page-to]");
      if (!button) return;
      list.page = Number(button.dataset.pageTo);
      applyList(list);
    });
    list.container.insertAdjacentElement("afterend", pager);
  }
  return pager;
}

function renderPager(list) {
  const pager = pagerFor(list);
  if (list.pages <= 1) { pager.hidden = true; pager.innerHTML = ""; return; }
  const dirtyRows = list.rows.map((row, index) => (row.querySelector(".field-dirty") ? index : -1)).filter((index) => index >= 0);
  const dirty = new Set(dirtyPages(list.matched, dirtyRows, list.size));
  const dot = (page) => (dirty.has(page) ? '<span class="pager-dot" title="这一页有未保存的修改">●</span>' : "");
  const button = (label, page, disabled, current = false) => `<button type="button" class="btn btn-secondary btn-sm ${current ? "pager-current" : ""}" data-page-to="${page}" ${disabled ? "disabled" : ""}>${label}</button>`;
  const numbers = list.pages <= 12
    ? Array.from({ length: list.pages }, (_, index) => button(`${index + 1}${dot(index + 1)}`, index + 1, false, index + 1 === list.page)).join("")
    : `<span>第 ${list.page} / ${list.pages} 页${dirty.size ? '<span class="pager-dot" title="有的页有未保存的修改">●</span>' : ""}</span>`;
  pager.hidden = false;
  pager.innerHTML = `${button("上一页", list.page - 1, list.page <= 1)}${numbers}${button("下一页", list.page + 1, list.page >= list.pages)}<span>共 ${list.matched.length} 条</span>`;
}

function refreshPagers() {
  for (const list of pagedLists.values()) if (list.container?.isConnected && list.pages > 1) renderPager(list);
}

/** Search words and sort mode come from boxes next to the list; neither counts as an edit. */
function bindListControls(list, searchId, sortId) {
  const search = searchId ? $(searchId) : null;
  const sort = sortId ? $(sortId) : null;
  search?.addEventListener("input", () => { list.query = search.value; list.page = 1; applyList(list); });
  sort?.addEventListener("change", () => { list.sort = sort.value; list.page = 1; applyList(list); });
}

function showListRow(row) {
  for (const list of pagedLists.values()) {
    if (!list.container?.contains(row)) continue;
    let page = pageOfRow(list.matched, list.rows.indexOf(row), list.size);
    if (!page) {
      list.query = "";
      const search = list.searchId ? $(list.searchId) : null;
      if (search) search.value = "";
      applyList(list);
      page = pageOfRow(list.matched, list.rows.indexOf(row), list.size);
    }
    list.page = page || 1;
    applyList(list);
    return;
  }
}

const LIST_DEFS = {
  "affinity-users": { containerId: "affinity-users", rowSelector: ".console-list-item", size: LIST_PAGE_SIZE, searchId: "affinity-search", sortId: "affinity-sort", sortOrder: true, defaultSort: "updated" },
  songs: { containerId: "song-list", rowSelector: ".song-row", size: LIST_PAGE_SIZE, searchId: "song-search" },
  covers: { containerId: "cover-cache", rowSelector: ".cover-row", size: LIST_PAGE_SIZE, searchId: "cover-search" },
  aliases: { containerId: "schedule-targets", rowSelector: ".target-row", size: LIST_PAGE_SIZE }
};

function refreshList(key) {
  const def = LIST_DEFS[key];
  const container = $(def.containerId);
  if (!container) return;
  applyList(pagedList(key, { container, rowSelector: def.rowSelector, size: def.size, searchId: def.searchId, sortOrder: def.sortOrder, defaultSort: def.defaultSort }));
}

function setupLists() {
  for (const [key, def] of Object.entries(LIST_DEFS)) {
    const list = pagedList(key, { container: $(def.containerId), rowSelector: def.rowSelector, size: def.size, searchId: def.searchId, sortOrder: def.sortOrder, defaultSort: def.defaultSort });
    bindListControls(list, def.searchId, def.sortId);
  }
  $("affinity-sort").innerHTML = AFFINITY_SORTS.map(([value, label]) => `<option value="${value}">${escapeHtml(label)}</option>`).join("");
}

/** A list that exists only while its group is rendered (stickers, reference audio of one emotion). */
function refreshGroupList(key, container, rowSelector, size) {
  applyList(pagedList(key, { container, rowSelector, size }));
}

// ---------- Overview ----------

function setBadge(id, text, className = "") {
  const badge = $(id);
  badge.textContent = text;
  badge.className = `status-badge ${className}`.trim();
}

const CHIP_STATE_TEXT = { on: "已启用", off: "未启用", unknown: "未知" };

function renderFeatures() {
  $("ov-features").innerHTML = featureChips(state.features).map((chip) => {
    const target = splitTarget(chip.goto);
    return `<a class="feature-chip ${chip.state}" href="${hashFor(target.page, target.tab)}" data-goto="${escapeHtml(chip.goto)}" title="${escapeHtml(`${chip.label}：${CHIP_STATE_TEXT[chip.state]}`)}"><span class="feature-dot" aria-hidden="true"></span>${escapeHtml(chip.label)}</a>`;
  }).join("");
}

async function loadFeatureFlags() {
  const [stickers, schedule, cover, cleanup] = await Promise.allSettled([api("/api/stickers"), api("/api/schedule"), api("/api/cover"), api("/api/cleanup")]);
  const flag = (result, read) => { try { return result.status === "fulfilled" ? Boolean(read(result.value)) : undefined; } catch { return undefined; } };
  state.features = {
    stickers: flag(stickers, (data) => data.settings.enabled),
    schedule: flag(schedule, (data) => data.settings.enabled),
    cover: flag(cover, (data) => data.settings.enabled),
    song_request: flag(cover, (data) => data.song_request.enabled),
    cleanup: flag(cleanup, (data) => data.settings.enabled)
  };
  renderFeatures();
}

// ---------- 101e: an unusable config/affinity file was replaced ----------
function renderRecoveryBanner(notices) {
  const text = recoveryBannerText(notices);
  const box = $("recovery-banner");
  if (!box) return;
  box.hidden = !text;
  if (text) $("recovery-banner-text").textContent = text;
}

// ---------- H1: health chain (banner on every page, chain on the overview) ----------

const BASE_TITLE = document.title;
const health = { status: null, failures: 0, rvc: null, chain: [] };

/** Look at /api/status once: the answer is kept for the chain; two failures in a row mean the core is gone. */
async function fetchStatus() {
  try {
    const payload = await api("/api/status");
    health.status = payload;
    health.failures = 0;
    return payload;
  } catch (error) {
    health.failures += 1;
    throw error;
  } finally {
    renderHealth();
  }
}

function pollStatus() {
  return fetchStatus().catch(() => null);
}

/** The voice and RVC states the chain needs (the overview and the voice page fetch the same ones on their own). */
async function pollServices() {
  const [voice, rvc] = await Promise.allSettled([api("/api/tts/service"), api("/api/cover/service")]);
  if (voice.status === "fulfilled") state.service = voice.value;
  health.rvc = rvc.status === "fulfilled" ? rvc.value : null;
  renderHealth();
}

function renderHealth() {
  const chain = healthChain({
    status: health.status,
    failures: health.failures,
    voice: { settings: state.tts, service: state.service },
    cover: { enabled: state.features ? state.features.cover : undefined, service: health.rvc }
  });
  health.chain = chain;
  const name = currentNickname();
  renderRecoveryBanner(health.status && health.status.data_recovery);
  const banner = healthBanner(chain, name);
  const box = $("health-banner");
  // The overview says the same in its own headline; the tab title still gets the sign.
  box.hidden = !banner || activePage() === "overview";
  box.className = `health-banner${banner ? ` level-${banner.level}` : ""}`;
  if (banner) {
    $("health-banner-text").textContent = banner.text;
    const link = $("health-banner-link");
    link.textContent = banner.link;
    link.dataset.goto = banner.goto;
    link.href = hashFor(splitTarget(banner.goto).page, splitTarget(banner.goto).tab);
  }
  document.title = healthTitle(BASE_TITLE, chain);
  const level = healthLevel(chain);
  $("ov-health").className = `card health-chain level-${level}`;
  $("ov-name").textContent = name;
  $("ov-avatar").textContent = avatarLetter(name);
  $("side-name").textContent = name;
  $("side-avatar").textContent = avatarLetter(name);
  renderSideStatus();
  $("ov-health-headline").textContent = healthHeadline(chain, name);
  renderToday();
  $("ov-health-nodes").innerHTML = chain.map((node) => {
    const target = splitTarget(node.goto);
    return `<li><a class="health-node level-${node.level}" href="${hashFor(target.page, target.tab)}" data-goto="${escapeHtml(node.goto)}" title="${escapeHtml(node.detail || node.text)}"><span class="health-dot" aria-hidden="true"></span><strong>${escapeHtml(node.label)}</strong><small>${escapeHtml(node.text)}</small></a></li>`;
  }).join("");
}

/** The five "today" cards, from the status the chain already fetched (one request feeds both). */
function renderToday() {
  const cards = todayCards(health.status ? health.status.today : null);
  for (const card of cards) {
    const box = $("ov-today").querySelector(`[data-today="${card.id}"]`);
    if (!box) continue;
    box.querySelector(".today-number").textContent = card.value;
    box.querySelector(".today-sub").textContent = card.sub;
  }
}

/** Overview data: the status (chain + today's numbers), the voice and RVC services, the install state; switches only when `full`. */
async function refreshOverview(full = false) {
  const [, rvc, , install, ownerClaim, onboarding] = await Promise.allSettled([
    fetchStatus(),
    api("/api/cover/service"),
    refreshService(),
    api("/api/install"),
    api("/api/owner/claim"),
    api("/api/onboarding")
  ]);
  renderOverviewInstall(install.status === "fulfilled" ? install.value : null);
  if (onboarding.status === "fulfilled") state.onboarding = onboarding.value.onboarding;
  state.checklist = {
    install: install.status === "fulfilled" ? install.value : null,
    owner: ownerClaim.status === "fulfilled" ? ownerClaim.value : null
  };
  renderOnboarding();
  health.rvc = rvc.status === "fulfilled" ? rvc.value : null;
  if (full || !state.features) await loadFeatureFlags();
  renderHealth();
}

// ---------- U5-2: settings search (Ctrl+K) ----------

const SEARCH_KIND_TEXT = { page: "页面", tab: "标签", card: "卡片", setting: "设置", item: "条目" };
let searchIndex = [];
let searchShown = [];
let searchActive = 0;

const cleanText = (text) => String(text ?? "").replace(/\s+/g, " ").trim();
const inDrawerOrDialog = (element) => Boolean(element.closest(".drawer-card, .dialog-overlay, #search-modal"));

/** The index of everything the console shows, built from the page itself each time the search opens (so it never goes stale). */
function buildSearchIndex() {
  const entries = [];
  const keyHints = new Map();
  for (const part of parts) {
    let fields = {};
    try { fields = part.collect(); } catch { fields = {}; }
    for (const [key, element] of Object.entries(fields)) {
      if (element) keyHints.set(element, `${key} ${part.id}`);
    }
  }
  const pageTitle = (page) => PAGE_LABELS[page] || page;
  for (const page of PAGE_IDS) {
    entries.push({ kind: "page", label: pageTitle(page), keys: page, help: "", path: [pageTitle(page)], page, tab: "", element: null });
    const section = $(page);
    if (!section) continue;
    section.querySelectorAll(":scope > .tabs .tab").forEach((button) => {
      const label = cleanText(button.firstChild?.textContent || button.textContent);
      entries.push({ kind: "tab", label, keys: `${page} ${button.dataset.tab}`, help: "", path: [pageTitle(page), label], page, tab: button.dataset.tab, element: null });
    });
    const where = (element) => {
      const panel = element.closest(".tab-panel");
      const tab = panel ? panel.dataset.tab : "";
      return { tab, tabLabel: tab ? tabLabel(page, tab) : "" };
    };
    const cardTitle = (element) => {
      const card = element.closest(".card");
      return card ? cleanText((card.querySelector("h3") || {}).textContent) : "";
    };
    section.querySelectorAll(".card").forEach((card) => {
      if (inDrawerOrDialog(card)) return;
      const title = cleanText((card.querySelector("h3") || {}).textContent);
      if (!title) return;
      const { tab, tabLabel: tl } = where(card);
      entries.push({ kind: "card", label: title, keys: "", help: "", path: [pageTitle(page), tl, title].filter(Boolean), page, tab, element: card });
    });
    section.querySelectorAll("details.module > summary").forEach((summary) => {
      if (inDrawerOrDialog(summary)) return;
      const label = cleanText(summary.textContent);
      const { tab, tabLabel: tl } = where(summary);
      entries.push({ kind: "card", label, keys: "", help: "", path: [pageTitle(page), tl, cardTitle(summary), label].filter(Boolean), page, tab, element: summary.parentElement });
    });
    section.querySelectorAll(".field").forEach((field) => {
      if (inDrawerOrDialog(field)) return;
      const labelElement = field.querySelector(".field-label");
      const label = cleanText(labelElement ? labelElement.textContent : field.getAttribute("aria-label"));
      if (!label) return;
      const controls = [...field.querySelectorAll("input, select, textarea")];
      const keys = controls.flatMap((control) => [control.name, control.id, keyHints.get(control)]).filter(Boolean).join(" ");
      const help = cleanText(((field.querySelector(".field-help") || {}).textContent) || "");
      const { tab, tabLabel: tl } = where(field);
      entries.push({ kind: "setting", label, keys, help, path: [pageTitle(page), tl, cardTitle(field), label].filter(Boolean), page, tab, element: field });
    });
    // lists made while the console runs: emotions, plans, rows of the paged lists
    const items = [
      ...section.querySelectorAll("[data-emotion-card] > summary strong"),
      ...section.querySelectorAll(".plan-card > header strong"),
      ...section.querySelectorAll("[data-list-row] .row-main > strong, [data-list-row] > strong")
    ];
    for (const strong of items) {
      if (inDrawerOrDialog(strong)) continue;
      const label = cleanText(strong.textContent);
      if (!label) continue;
      const row = strong.closest("[data-emotion-card], .plan-card, [data-list-row]");
      const { tab, tabLabel: tl } = where(strong);
      entries.push({ kind: "item", label, keys: "", help: "", path: [pageTitle(page), tl, cardTitle(strong), label].filter(Boolean), page, tab, element: row });
    }
  }
  return entries;
}

function renderSearchResults() {
  const list = $("search-results");
  const query = $("search-input").value;
  searchShown = searchEntries(searchIndex, query);
  searchActive = 0;
  if (!normalizeSearchText(query)) {
    list.innerHTML = "";
    $("search-note").textContent = "↑↓ 选择 · 回车跳转 · Esc 关闭";
    return;
  }
  if (!searchShown.length) {
    list.innerHTML = '<li class="search-empty">没有找到匹配的设置。</li>';
    $("search-note").textContent = "试试别的字，或者英文参数名。";
    return;
  }
  list.innerHTML = searchShown.map((entry, index) => `<li class="search-result${index === 0 ? " active" : ""}" role="option" id="search-option-${index}" data-search-index="${index}" aria-selected="${index === 0}"><span><strong>${escapeHtml(entry.label)}</strong><span class="search-kind">${SEARCH_KIND_TEXT[entry.kind] || ""}</span></span><small>${escapeHtml(searchPathText(entry))}</small></li>`).join("");
  $("search-note").textContent = `${searchShown.length} 条 · ↑↓ 选择 · 回车跳转 · Esc 关闭`;
}

function moveSearchSelection(step) {
  if (!searchShown.length) return;
  searchActive = (searchActive + step + searchShown.length) % searchShown.length;
  $("search-results").querySelectorAll(".search-result").forEach((item, index) => {
    item.classList.toggle("active", index === searchActive);
    item.setAttribute("aria-selected", String(index === searchActive));
    if (index === searchActive) item.scrollIntoView?.({ block: "nearest" });
  });
  $("search-input").setAttribute("aria-activedescendant", `search-option-${searchActive}`);
}

function openSearch() {
  if (!$("app-dialog").hidden) return;
  searchIndex = buildSearchIndex();
  $("search-modal").hidden = false;
  $("search-input").value = "";
  renderSearchResults();
  $("search-input").focus();
}

function closeSearch() {
  $("search-modal").hidden = true;
}

/** Go to a result: the page and tab, every fold around it opened, scrolled to and lit for about two seconds. */
async function jumpToSearchEntry(entry) {
  closeSearch();
  await requestPage(entry.page, entry.tab || "");
  if (activePage() !== entry.page) return;
  const element = entry.element;
  if (!element || !element.isConnected) return;
  revealElement(element);
  element.scrollIntoView({ block: "center", behavior: "smooth" });
  element.classList.add("search-highlight");
  setTimeout(() => element.classList.remove("search-highlight"), 2000);
}

function bindSearch() {
  const isMac = /Mac|iPhone|iPad/.test(navigator.platform || "");
  $("search-hint").textContent = isMac ? "⌘ K" : "Ctrl K";
  $("search-open").addEventListener("click", openSearch);
  document.addEventListener("keydown", (event) => {
    if ((event.ctrlKey || event.metaKey) && !event.altKey && String(event.key).toLowerCase() === "k") {
      event.preventDefault();
      if ($("search-modal").hidden) openSearch(); else closeSearch();
    }
  });
  $("search-modal").addEventListener("click", (event) => { if (event.target === $("search-modal")) closeSearch(); });
  $("search-input").addEventListener("input", renderSearchResults);
  $("search-input").addEventListener("keydown", (event) => {
    if (event.key === "ArrowDown") { event.preventDefault(); moveSearchSelection(1); }
    else if (event.key === "ArrowUp") { event.preventDefault(); moveSearchSelection(-1); }
    else if (event.key === "Enter") { event.preventDefault(); const entry = searchShown[searchActive]; if (entry) jumpToSearchEntry(entry).catch(showError); }
    else if (event.key === "Escape") { event.preventDefault(); closeSearch(); }
  });
  $("search-results").addEventListener("click", (event) => {
    const item = event.target.closest("[data-search-index]");
    const entry = item ? searchShown[Number(item.dataset.searchIndex)] : null;
    if (entry) jumpToSearchEntry(entry).catch(showError);
  });
}

// ---------- U5-1: the 开始使用 checklist ----------

async function saveOnboarding(changes) {
  const answer = await api("/api/onboarding", { method: "POST", body: JSON.stringify(changes) });
  state.onboarding = answer.onboarding;
  renderOnboarding();
}

function renderOnboarding() {
  const flags = state.onboarding;
  const card = $("ov-onboarding");
  if (!flags) { card.hidden = true; $("onboarding-restore").hidden = true; return; }
  const steps = onboardingSteps({
    install: state.checklist?.install,
    status: health.status,
    owner: state.checklist?.owner,
    voice: { settings: state.tts, service: state.service },
    flags
  });
  const view = onboardingView(steps, flags, state.onboardingExpanded === true);
  card.hidden = !view.visible;
  $("onboarding-restore").hidden = view.visible;
  $("onboarding-line").textContent = view.line;
  const progress = $("onboarding-progress");
  progress.setAttribute("aria-valuemax", String(view.total));
  progress.setAttribute("aria-valuenow", String(view.done));
  progress.dataset.done = String(view.total > 0 && view.done === view.total);
  $("onboarding-progress-bar").style.width = view.total ? `${Math.round((view.done * 100) / view.total)}%` : "0%";
  card.classList.toggle("collapsed", view.collapsed);
  card.classList.toggle("needs-attention", view.collapsed && view.needs > 0);
  $("onboarding-steps").hidden = view.collapsed;
  $("onboarding-toggle").textContent = view.collapsed ? "展开" : "收起";
  $("onboarding-toggle").hidden = !view.collapsed && !(flags.completed || view.needs === 0);
  $("onboarding-steps").innerHTML = steps.map((step, index) => {
    const mark = step.done ? '<span class="onboarding-mark done" aria-label="已完成">✓</span>' : `<span class="onboarding-mark" aria-label="未完成">${index + 1}</span>`;
    const go = step.action === "test-chat"
      ? '<button type="button" class="btn btn-secondary btn-sm" data-onboarding-action="test-chat">发一句测试</button>'
      : `<button type="button" class="btn btn-secondary btn-sm" data-onboarding-go="${escapeHtml(step.id)}">去设置</button>`;
    const skip = step.optional
      ? (step.skipped
        ? '<button type="button" class="btn btn-secondary btn-sm" data-onboarding-skip="0">撤销跳过</button>'
        : step.done ? "" : '<button type="button" class="btn btn-secondary btn-sm" data-onboarding-skip="1">跳过</button>')
      : "";
    return `<li class="onboarding-step${step.done ? " done" : ""}" data-onboarding-step="${escapeHtml(step.id)}">${mark}<span class="onboarding-text"><strong>${escapeHtml(step.title)}${step.optional ? "（可选）" : ""}</strong><small>${escapeHtml(step.hint)}</small></span><span class="onboarding-actions">${go}${skip}</span></li>`;
  }).join("");
  state.onboardingSteps = steps;
  if (view.markCompleted) saveOnboarding({ completed: true }).catch(console.error);
}

/** Open the page of a checklist step and flash the card the step is about. */
async function goToOnboardingStep(step) {
  const target = splitTarget(step.goto);
  await requestPage(target.page, target.tab);
  if (activePage() !== resolveTarget(target.page, target.tab).page) return;
  const element = $(step.anchor);
  const card = element?.closest(".card") || element;
  if (!card) return;
  revealElement(card);
  card.scrollIntoView({ block: "center", behavior: "smooth" });
  card.classList.add("onboarding-highlight");
  setTimeout(() => card.classList.remove("onboarding-highlight"), 2600);
}

function bindOnboarding() {
  $("onboarding-steps").addEventListener("click", (event) => {
    const go = event.target.closest("[data-onboarding-go]");
    if (go) { const step = state.onboardingSteps?.find((item) => item.id === go.dataset.onboardingGo); if (step) goToOnboardingStep(step).catch(showError); return; }
    if (event.target.closest('[data-onboarding-action="test-chat"]')) { openTestChat(); return; }
    const skip = event.target.closest("[data-onboarding-skip]");
    if (skip) saveOnboarding({ voice_skipped: skip.dataset.onboardingSkip === "1" }).catch(showError);
  });
  $("onboarding-toggle").addEventListener("click", () => { state.onboardingExpanded = !state.onboardingExpanded; renderOnboarding(); });
  $("onboarding-hide").addEventListener("click", () => { $("onboarding-hide").closest("details").open = false; saveOnboarding({ hidden: true }).catch(showError); });
  $("onboarding-show").addEventListener("click", () => { state.onboardingExpanded = false; saveOnboarding({ hidden: false }).catch(showError); });
}

// ---------- U5-1: the test chat dialog and the 试听 buttons ----------

function openTestChat() {
  $("testchat-note").textContent = TESTCHAT_NOTE;
  $("testchat-modal").hidden = false;
  $("testchat-text").focus();
}

function closeTestChat() {
  $("testchat-modal").hidden = true;
}

let testChatBusy = false;

async function sendTestChat() {
  if (testChatBusy) return;
  const text = $("testchat-text").value.trim();
  if (!text) { $("testchat-text").focus(); return; }
  testChatBusy = true;
  $("testchat-send").disabled = true;
  $("testchat-send").textContent = "等 ST 回复中…";
  $("testchat-result").hidden = false;
  $("testchat-status").textContent = "已发出，ST 生成中（可能要几十秒）…";
  $("testchat-reply").textContent = "";
  let problem = "";
  let failed = false;
  try {
    const result = await api("/api/test/chat", { method: "POST", body: JSON.stringify({ chat: $("testchat-kind").value, text }) });
    problem = testChatProblemText(result, null);
    failed = Boolean(problem);
    $("testchat-reply").textContent = (result.replies || []).join("\n——\n");
    $("testchat-status").textContent = problem ? problem : `成功，用了 ${(Number(result.elapsed_ms) / 1000).toFixed(1)} 秒`;
    if (!problem) {
      state.onboarding = { ...(state.onboarding || {}), tested: true };
      renderOnboarding();
    }
  } catch (error) {
    failed = true;
    $("testchat-status").textContent = testChatProblemText(null, error);
  } finally {
    testChatBusy = false;
    $("testchat-send").disabled = false;
    $("testchat-send").textContent = "发送";
  }
  $("testchat-status").classList.toggle("console-note-error", failed);
}

let auditionAudio = null;
let auditionBusy = false;

/** Synthesize one sentence with the current engine and play it. `report(text, isError)` shows the outcome. */
async function runAudition(button, body, report) {
  if (auditionBusy) return;
  auditionBusy = true;
  const label = button.textContent;
  button.disabled = true;
  button.classList.add("busy");
  button.textContent = "合成中…";
  report("", false);
  try {
    const answer = await api("/api/tts/test", { method: "POST", body: JSON.stringify({ ...body, return_audio: true }) });
    auditionAudio?.pause();
    auditionAudio = new Audio(`data:${answer.content_type || "audio/wav"};base64,${answer.audio_base64}`);
    try {
      await auditionAudio.play();
      report(`正在播放（${answer.duration_seconds ?? "?"} 秒，合成用了 ${(answer.elapsed_ms / 1000).toFixed(1)} 秒）`, false);
    } catch {
      report("合成好了，但浏览器没让自动播放：点一下页面再试一次", true);
    }
  } catch (error) {
    report(auditionErrorText(error.status, error.payload), true);
  } finally {
    auditionBusy = false;
    button.disabled = false;
    button.classList.remove("busy");
    button.textContent = label;
  }
}

function bindAudition() {
  $("voice-audition").addEventListener("click", (event) => {
    const note = $("voice-audition-note");
    runAudition(event.currentTarget, {}, (text, isError) => {
      note.hidden = !text;
      note.textContent = text;
      note.classList.toggle("console-note-error", isError);
    });
  });
  $("emotions-list").addEventListener("click", (event) => {
    const button = event.target.closest("[data-audition-emotion]");
    if (!button) return;
    event.preventDefault();
    event.stopPropagation();
    const name = button.dataset.auditionEmotion;
    const body = { character: state.selectedCharacter || undefined, emotion: name };
    // What is typed in the emotion's form, saved or not, is what gets heard.
    const form = [...$("emotions-list").querySelectorAll("[data-emotion-form]")].find((item) => item.dataset.emotionForm === name);
    if (form) {
      const raw = Object.fromEntries(new FormData(form).entries());
      if (form.elements.split_sentence) raw.split_sentence = form.elements.split_sentence.checked;
      body.emotion_profile = auditionProfile(currentEngine(), raw);
    }
    runAudition(button, body, (text, isError) => { if (text) notify(text, isError); });
  });
}

let installProblem = null;

/** The install card: one line when all is well; opens by itself (once per change) when something needs doing. */
function renderOverviewInstall(overview) {
  const info = installOverview(overview);
  const fold = installFold(overview);
  setBadge("ov-install-state", info.badge, info.badgeClass);
  $("ov-install-summary").textContent = fold.summary;
  $("ov-install-ext").textContent = info.extension;
  $("ov-install-plugin").textContent = info.plugin;
  $("ov-install-paths").textContent = info.paths;
  const card = $("ov-install-card");
  card.classList.toggle("level-warn", fold.level === "warn");
  card.classList.toggle("level-error", fold.level === "error");
  card.classList.toggle("level-ok", fold.level === "ok");
  const problem = fold.level === "warn" || fold.level === "error";
  if (problem !== installProblem) {
    installProblem = problem;
    card.open = problem;
  }
}

let overviewTicks = 0;
let healthTicks = 0;

function startServicePolling() {
  setInterval(() => {
    if (document.hidden) return;
    const page = activePage();
    healthTicks += 1;
    if (page === "overview") { overviewTicks += 1; refreshOverview(overviewTicks % 5 === 0).catch(console.error); }
    else {
      // Every other page: the banner still needs the status (every tick) and, less often, the services and switches.
      pollStatus();
      if (healthTicks % 5 === 0) pollServices();
      if (healthTicks % 20 === 0) loadFeatureFlags().then(renderHealth).catch(console.error);
      if (page === "voice") refreshService();
      else if (page === "connect") refreshChatPlatformStatus().catch(console.error);
    }
  }, 3000);
  // Coming back to the tab: look right away instead of waiting for the next tick.
  document.addEventListener("visibilitychange", () => { if (!document.hidden && activePage() !== "overview") { pollStatus(); pollServices(); } });
  pollStatus();
  pollServices();
  if (!state.features) loadFeatureFlags().then(renderHealth).catch(console.error);
}

// ---------- Voice page: the engine in use first, the other one folded away ----------

function arrangeVoiceEngines() {
  const engine = currentEngine();
  const other = engine === "irodori" ? "gpt_sovits" : "irodori";
  const nodes = { irodori: $("voice-irodori"), gpt_sovits: $("voice-gpt") };
  $("voice-current").appendChild(nodes[engine]);
  $("voice-unused-body").appendChild(nodes[other]);
  $("voice-unused").hidden = false;
  $("voice-unused-title").innerHTML = `<strong>未使用的引擎：${escapeHtml(ENGINE_LABELS[other])}</strong>`;
}

// ---------- Emotions page ----------

const selectedCharacter = () => state.characters.find((item) => item.name === state.selectedCharacter);

function bindCharacterActions(root) {
  root.querySelectorAll("[data-edit-character]").forEach((button) => button.addEventListener("click", () => requestOpenCharacter(button.dataset.editCharacter)));
  root.querySelectorAll("[data-default-character]").forEach((button) => button.addEventListener("click", async () => {
    const character = state.characters.find((item) => item.name === button.dataset.defaultCharacter);
    try { await api("/api/config/defaults", { method: "POST", body: JSON.stringify({ character: character.name, emotion: character.default_emotion || "neutral" }) }); await loadAll(false, "keep"); } catch (error) { showError(error); }
  }));
  root.querySelectorAll("[data-delete-character]").forEach((button) => button.addEventListener("click", async () => {
    if (!(await confirmDialog(`删除角色“${button.dataset.deleteCharacter}”？`, { danger: true }))) return;
    try { await api(`/api/characters/${encodeURIComponent(button.dataset.deleteCharacter)}`, { method: "DELETE" }); state.selectedCharacter = ""; await loadAll(); } catch (error) { showError(error); }
  }));
}

/** Switch the character whose emotions are shown (asks first when the emotion cards hold unsaved edits). */
async function chooseCharacter(name) {
  if (name === state.selectedCharacter) return;
  if (!(await guardLeave())) return;
  state.selectedCharacter = name;
  state.discovered = [];
  renderCharacters();
  refreshEmotions("force");
}

/** The ⋯ menu of a character: 设为默认 (not for the default one) and 删除. */
function characterMenuButtons(rawName, isDefault) {
  const name = escapeHtml(rawName);
  return `${isDefault ? "" : `<button type="button" class="btn btn-secondary btn-sm" data-default-character="${name}">设为默认</button>`}<button type="button" class="btn btn-danger-ghost btn-sm" data-delete-character="${name}">删除</button>`;
}

function renderCharacterSummary() {
  const box = $("character-summary");
  $("wake-hint").hidden = true;
  const characters = state.characters;
  if (!characters.length) { box.innerHTML = emptyStateMarkup("characters"); return; }
  const current = selectedCharacter() || characters[0];
  const name = escapeHtml(current.name);
  const facts = `<div class="info-item"><span class="label">昵称（唤醒词）</span><span class="value">${escapeHtml(current.nickname || "-")}</span></div><div class="info-item"><span class="label">ST 角色名</span><span class="value">${escapeHtml(current.st_name || "-")}</span></div><div class="info-item"><span class="label">绑定模型</span><span class="value">${escapeHtml(current.model_name || "未绑定")}</span></div><div class="info-item"><span class="label">默认情绪</span><span class="value">${escapeHtml(current.default_emotion || "neutral")}</span></div>`;
  if (characters.length === 1) {
    box.innerHTML = `<div class="info-item"><span class="label">角色</span><span class="value">${name}</span></div>${facts}<div class="console-actions"><button class="btn btn-secondary btn-sm" data-edit-character="${name}">编辑</button></div>`;
  } else {
    const options = characters.map((item) => `<option value="${escapeHtml(item.name)}" ${item.name === current.name ? "selected" : ""}>${escapeHtml(item.name)}</option>`).join("");
    box.innerHTML = `<label class="console-inline-label">角色 <select id="character-select" class="select">${options}</select></label>${facts}<div class="console-actions"><button class="btn btn-secondary btn-sm" data-edit-character="${name}">编辑</button>${rowMenu(characterMenuButtons(name, current.name === state.config?.default_character))}</div>`;
    $("character-select").addEventListener("change", async (event) => {
      const wanted = event.target.value;
      event.target.value = state.selectedCharacter;
      await chooseCharacter(wanted);
    });
  }
  bindCharacterActions(box);
  const hint = wakePrefixHint(state.wakePrefix, current.nickname || "");
  const hintBox = $("wake-hint");
  hintBox.hidden = hint.status === "unknown";
  hintBox.textContent = hint.status === "ok" ? `唤醒前缀：${hint.text}` : hint.text;
  hintBox.classList.toggle("console-note-error", hint.status === "mismatch");
}

function audioRow(audio, model) {
  const problem = audioProblem(audio);
  const pick = audio.pick ? `<span class="status-badge status-success" title="与日志中的 pick 序号对应">#${audio.pick}</span> ` : "";
  return `<div class="audio-card list-row ${problem ? "audio-card-problem" : ""}"><div class="row-main audio-meta"><strong>${pick}${escapeHtml(audio.prompt_text)}</strong><small>${formatDuration(audio.duration)}</small>${problem ? `<span class="status-badge status-error">${escapeHtml(problem)}</span>` : ""}</div><div class="row-actions">${playButton(audioStreamUrl(model, audio.relative_path))}${rowMenu(`<button type="button" class="btn btn-danger-ghost btn-sm" data-delete-audio="${escapeHtml(audio.relative_path)}">删除</button>`)}</div></div>`;
}

function emotionCard(name, engine, character, info, profile) {
  const status = emotionStatusText(engine, { caption: profile?.caption, usable: info?.available, total: info?.audios?.length || 0, override: Boolean(profile) });
  const badges = `${name === character.default_emotion ? ' <span class="status-badge status-success">默认</span>' : ""}${profile && engine === "gpt_sovits" ? ' <span class="status-badge">有手动参数</span>' : ""}`;
  const escaped = escapeHtml(name);
  const form = (deleteLabel) => `<form class="emotion-form" data-emotion-form="${escaped}"><div class="fields">${emotionFormFields(engine, name, profile)}</div><div class="console-actions"><button type="button" class="btn btn-danger-ghost btn-sm" data-delete-emotion="${escaped}">${deleteLabel}</button></div></form>`;
  let body;
  if (engine === "irodori") {
    body = form("删除该情绪的 caption 和表情设置");
  } else {
    const audios = info?.audios || [];
    const advanced = profile
      ? form("删除手动参数（不会删除参考音频文件）")
      : `<p class="console-note">没有手动参数：合成时用这个情绪文件夹里的音频和服务设置里的默认参数。</p><div class="console-actions"><button type="button" class="btn btn-secondary btn-sm" data-create-override="${escaped}">创建手动参数</button></div>`;
    body = `<div class="emotion-audios"><div data-audio-list="${escaped}">${audios.length ? audios.map((audio) => audioRow(audio, character.model_name)).join("") : '<p class="placeholder">这个情绪还没有参考音频。</p>'}</div><div class="console-actions"><label class="btn btn-secondary btn-sm file-button">上传到“${escaped}”<input type="file" accept=".wav,.mp3,.ogg,.flac" data-upload-emotion="${escaped}"></label></div></div><details class="emotion-advanced" data-emotion-advanced="${escaped}" ${state.openAdvanced.has(name) ? "open" : ""}><summary>高级：手动参数</summary>${advanced}</details>`;
  }
  return `<details class="emotion-editor emotion-card" data-emotion-card="${escaped}" ${state.openEmotions.has(name) ? "open" : ""}><summary><strong>${escaped}</strong>${badges} <small>${escapeHtml(status)}</small> <button type="button" class="btn btn-secondary btn-sm audition-btn" data-audition-emotion="${escaped}" title="用这一栏现在填的内容合成一句话并播放（没保存也可以）">试听</button></summary>${body}</details>`;
}

/** The emotion cards of the selected character (the part's fill; reference audio comes from state.discovered). */
function renderEmotionCards() {
  const character = selectedCharacter();
  const engine = currentEngine();
  const list = $("emotions-list");
  $("new-emotion").hidden = engine !== "irodori";
  $("new-emotion").disabled = !character;
  $("emotion-upload-card").hidden = !(character && engine === "gpt_sovits");
  $("emotion-title").textContent = character ? `${character.name} · 情绪` : "选择角色后查看情绪";
  if (!character) { list.innerHTML = emptyStateMarkup("characters"); return; }
  const overrides = character.emotions || {};
  const discovered = engine === "gpt_sovits" ? state.discovered : [];
  const byName = Object.fromEntries(discovered.map((emotion) => [emotion.name, emotion]));
  const names = emotionNames(discovered.map((emotion) => emotion.name), Object.keys(overrides));
  const note = engine === "gpt_sovits" && !character.model_name ? '<p class="placeholder">角色未绑定模型，无法识别参考音频目录。</p>' : "";
  const empty = engine === "irodori"
    ? '<p class="placeholder">还没有情绪。点“新建覆盖”添加情绪，并填写 caption 和表情。</p>'
    : '<p class="placeholder">参考音频目录下还没有情绪文件夹：在上面“新情绪”里上传第一段音频。</p>';
  list.innerHTML = note + (names.length ? names.map((name) => emotionCard(name, engine, character, byName[name], overrides[name])).join("") : empty);
  list.querySelectorAll("[data-emotion-card]").forEach((details) => details.addEventListener("toggle", () => {
    if (details.open) state.openEmotions.add(details.dataset.emotionCard); else state.openEmotions.delete(details.dataset.emotionCard);
  }));
  list.querySelectorAll("[data-emotion-advanced]").forEach((details) => details.addEventListener("toggle", () => {
    if (details.open) state.openAdvanced.add(details.dataset.emotionAdvanced); else state.openAdvanced.delete(details.dataset.emotionAdvanced);
  }));
  list.querySelectorAll("[data-delete-emotion]").forEach((button) => button.addEventListener("click", async () => {
    const name = button.dataset.deleteEmotion;
    const text = engine === "irodori" ? `删除情绪“${name}”的 caption 和表情设置？` : `删除情绪“${name}”的手动参数？只删除覆盖，不会删除参考音频文件。`;
    if (!(await confirmDialog(text, { danger: true }))) return;
    try { await api(`/api/characters/${encodeURIComponent(character.name)}/emotions/${encodeURIComponent(name)}`, { method: "DELETE" }); await loadAll(false, "keep"); } catch (error) { showError(error); }
  }));
  list.querySelectorAll("[data-create-override]").forEach((button) => button.addEventListener("click", async () => {
    try { await api(`/api/characters/${encodeURIComponent(character.name)}/emotions/${encodeURIComponent(button.dataset.createOverride)}`, { method: "PUT", body: JSON.stringify({}) }); state.openAdvanced.add(button.dataset.createOverride); await loadAll(false, "keep"); } catch (error) { showError(error); }
  }));
  list.querySelectorAll("[data-upload-emotion]").forEach((input) => input.addEventListener("change", () => {
    const file = input.files[0];
    if (file) uploadAudioTo(input.dataset.uploadEmotion, file);
  }));
  list.querySelectorAll("[data-delete-audio]").forEach((button) => button.addEventListener("click", async () => {
    if (!(await confirmDialog("删除这条参考音频？", { danger: true }))) return;
    try { await api(`/api/audio?model=${encodeURIComponent(character.model_name)}&path=${encodeURIComponent(button.dataset.deleteAudio)}`, { method: "DELETE" }); await loadDiscovered(); } catch (error) { showError(error); }
  }));
  list.querySelectorAll("[data-audio-list]").forEach((container) => refreshGroupList(`audio:${container.dataset.audioList}`, container, ".audio-card", LIST_PAGE_SIZE));
  enhanceModules(list);
}

/** Render the cards now, then fetch the reference-audio folders and render again keeping what was typed. */
function refreshEmotions(mode = "skip") {
  renderCharacterSummary();
  fillPart(P.emotions, mode);
  loadDiscovered().catch(showError);
}

async function loadDiscovered() {
  const character = selectedCharacter();
  if (!character || currentEngine() !== "gpt_sovits" || !character.model_name) {
    state.discovered = [];
    renderPresetBox(character);
    renderEmotionOptions();
    return;
  }
  const requested = character.name;
  const data = await api(`/api/emotions/discovered?character=${encodeURIComponent(requested)}`);
  if (state.selectedCharacter !== requested) return;
  state.discovered = data.emotions || [];
  fillPart(P.emotions, "keep");
  renderPresetBox(character);
  renderEmotionOptions();
}

// ---------- Nickname of the character in use ----------

/** The default character (else the first one): its nickname is the wake word and goes into the reply texts. */
function activeCharacter() {
  return state.characters.find((item) => item.name === state.config?.default_character) || state.characters[0];
}

const currentNickname = () => activeCharacter()?.nickname || DEFAULT_NICKNAME;

/** Every place that names the character in prose shows the current nickname. */
function applyNickname() {
  document.querySelectorAll(".nick").forEach((element) => { element.textContent = currentNickname(); });
}

// ---------- Ports (系统 › 端口) ----------

async function loadNetwork() {
  state.network = await api("/api/network");
  fillPart(P.network);
  $("network-note").textContent = networkStatusText(state.network);
}

// ---------- Owner (系统 › 常规) and the core log (系统 › 日志) ----------

async function loadOwner() {
  state.owner = await api("/api/owner");
  fillPart(P.owner);
  await refreshOwnerClaim();
}

// The owner claim: the console shows a 6-digit code, the user sends it to the bot in a private chat.
let ownerClaimTimer = null;

function renderOwnerClaim(claim, { announce = false } = {}) {
  state.ownerClaim = claim;
  $("owner-current").textContent = ownerCurrentText(claim);
  const view = ownerClaimView(claim);
  $("owner-claim-box").hidden = !view.pending;
  $("owner-claim-code").textContent = view.code;
  const note = $("owner-claim-note");
  // An old result ("已认领" from an hour ago) is not shown again when the page is opened later.
  note.textContent = view.pending || announce ? view.text : "";
  note.classList.toggle("console-note-error", view.tone === "error" && (view.pending || announce));
  return view;
}

async function refreshOwnerClaim({ announce = false } = {}) {
  const view = renderOwnerClaim(await api("/api/owner/claim"), { announce });
  clearTimeout(ownerClaimTimer);
  ownerClaimTimer = null;
  if (view.poll) ownerClaimTimer = setTimeout(pollOwnerClaim, 1000);
  return view;
}

async function pollOwnerClaim() {
  ownerClaimTimer = null;
  try {
    const view = await refreshOwnerClaim({ announce: true });
    if (!view.poll && state.ownerClaim?.state === "claimed") {
      // The owner changed behind this page: show it in the fields too.
      state.owner = await api("/api/owner");
      fillPart(P.owner);
      await loadChatPlatform();
      notify("主人已认领，立即生效");
    }
  } catch (error) {
    console.error(error);
  }
}

async function startOwnerClaim() {
  if (ownerClaimReplaces(state.ownerClaim)
    && !await confirmDialog("当前已有主人，认领后会替换。继续吗？", { confirmLabel: "继续认领" })) return;
  await api("/api/owner/claim", { method: "POST" });
  await refreshOwnerClaim({ announce: true });
}

async function loadChatPlatform() {
  state.chatPlatform = await api("/api/chat-platform");
  state.wakePrefix = state.chatPlatform.wake_prefix ?? state.wakePrefix;
  fillPart(P.chatPlatform);
  state.tokenShown = false;  // a fresh load of the tab starts masked; the status polling below keeps what the user chose
  showChatPlatformInfo();
}

/** Only the connection state (no form fill): called while the tab is open. */
async function refreshChatPlatformStatus() {
  const data = await api("/api/chat-platform");
  state.chatPlatform = { ...state.chatPlatform, onebot: data.onebot };
  showChatPlatformInfo();
}

function showChatPlatformInfo() {
  const data = state.chatPlatform;
  if (!data) return;
  const onebot = data.onebot || {};
  $("chatplat-status").textContent = onebotStatusText(onebot.status);
  const warning = onebotFormatWarning(onebot.status);
  $("chatplat-format-warning").hidden = !warning;
  $("chatplat-format-warning").textContent = warning;
  $("chatplat-url").textContent = onebot.url || "-";
  $("chatplat-token").dataset.value = onebot.token || "";  // the real value lives here; the text is masked
  setTokenShown(state.tokenShown === true);
  $("chatplat-choice-note").textContent = platformChoiceNote(data.settings.active);
  const hint = wakePrefixHint(data.wake_prefix, currentNickname());
  $("chatplat-wake-note").textContent = data.settings.active === "napcat" && data.wake_prefix
    ? `NapCat 里群聊的唤醒方式：@ 机器人，或消息以 ${data.wake_prefix.join("、")} 开头（跟随角色昵称，${hint.status === "ok" ? "已一致" : "无需另外设置"}）。`
    : "";
}

function setTokenShown(shown) {
  state.tokenShown = shown;
  const element = $("chatplat-token");
  const value = element.dataset.value || "";
  element.textContent = tokenDisplay(value, shown);
  const button = $("chatplat-show-token");
  button.hidden = !value;
  button.textContent = shown ? "隐藏" : "显示";
  button.dataset.shown = shown ? "1" : "";
}

async function copyOnebotValue(id, button) {
  const element = $(id);
  const value = "value" in element.dataset ? element.dataset.value : element.textContent;  // the token's text may be masked
  if (!value || value === "-") return;
  try { await navigator.clipboard.writeText(value); button.textContent = "已复制"; setTimeout(() => { button.textContent = "复制"; }, 1500); }
  catch { await copyDialog(value); }
}

let logData = null;

async function loadLogs() {
  const lines = Number($("logs-lines").value) || 300;
  logData = await api(`/api/logs?lines=${lines}`);
  renderLogs();
  $("logs-files").textContent = logData.files && logData.files.length > 1 ? `日志文件：${logData.files.join("、")}` : "";
}

const escapeLogText = (text) => escapeHtml(text);

/** Draw the log lines that pass the keyword and level filters; stay at the bottom only if the reader was there. */
function renderLogs() {
  const view = $("logs-view");
  const atBottom = view.scrollHeight - view.scrollTop - view.clientHeight < 40;
  const all = logData && Array.isArray(logData.lines) ? logData.lines : [];
  const keyword = $("logs-filter").value;
  const shown = filterLogLines(all, { keyword, level: $("logs-level").value });
  if (!all.length) view.textContent = logsText(logData);
  else if (!shown.length) view.textContent = "没有符合筛选条件的行。";
  else {
    view.innerHTML = shown.map((line) => `<span class="log-line${line.level ? ` lvl-${line.level}` : ""}">${highlightSegments(line.text, keyword).map((part) => (part.match ? `<mark>${escapeLogText(part.text)}</mark>` : escapeLogText(part.text))).join("")}</span>`).join("\n");
  }
  $("logs-count").textContent = all.length ? logCountText(shown.length, all.length) : "";
  if (atBottom) view.scrollTop = view.scrollHeight;
}

let logsTimer = null;

/** Every 5 seconds while the switch is on, the logs tab is open and the browser tab is visible. */
function syncLogsAutoRefresh() {
  clearInterval(logsTimer);
  logsTimer = null;
  if (!$("logs-auto").checked) return;
  logsTimer = setInterval(() => {
    if (document.hidden || activePage() !== "system" || pageTab.system !== "logs") return;
    loadLogs().catch(console.error);
  }, 5000);
}

// ---------- Reply texts (系统 › 回复文案) ----------

async function loadTexts() {
  state.texts = await api("/api/texts");
  fillPart(P.texts);
}

function renderTexts() {
  const data = state.texts;
  if (!data) return;
  const notice = textsNoticeText(data.notice);
  $("texts-notice").hidden = !notice;
  $("texts-notice").textContent = notice;
  const box = $("texts-list");
  box.innerHTML = textGroups(data.meta).map((group) => `<div class="texts-group"><h4>${escapeHtml(group.group)}</h4>${group.items.map((item) => {
    const field = item.kind === "lines"
      ? `<textarea data-text-key="${item.key}" rows="3" aria-label="${escapeHtml(item.label)}"></textarea>`
      : `<input data-text-key="${item.key}" maxlength="100" aria-label="${escapeHtml(item.label)}">`;
    return `<label>${escapeHtml(item.label)}<small class="console-note">${escapeHtml(placeholderText(item))}</small>${field}</label><div class="console-actions"><button type="button" class="btn btn-secondary btn-sm" data-text-reset="${item.key}">恢复默认</button></div>`;
  }).join("")}</div>`).join("");
  const raw = textsToRaw(data.texts, data.meta);
  box.querySelectorAll("[data-text-key]").forEach((field) => { field.value = raw[field.dataset.textKey]; });
  const defaults = textsToRaw(data.defaults, data.meta);
  box.querySelectorAll("[data-text-reset]").forEach((button) => button.addEventListener("click", () => {
    const field = box.querySelector(`[data-text-key="${button.dataset.textReset}"]`);
    field.value = defaults[button.dataset.textReset];
    field.dispatchEvent(new Event("input", { bubbles: true }));
  }));
}

function audioStreamUrl(model, relativePath) {
  return `/api/audio/stream?model=${encodeURIComponent(model)}&path=${encodeURIComponent(relativePath)}`;
}

function formatDuration(seconds) {
  return typeof seconds === "number" ? `${seconds.toFixed(1)} 秒` : "时长未知";
}

function audioProblem(audio) {
  if (audio.out_of_range) return `时长 ${formatDuration(audio.duration)}，超出 3～10 秒，不参与随机选择`;
  if (audio.readable === false) return "无法读取 WAV 文件头，不参与随机选择";
  return "";
}

function browseButton(mode, title) {
  return `<button type="button" class="btn btn-secondary btn-sm" data-browse="${mode}" data-browse-title="${escapeHtml(title)}">浏览…</button>`;
}

// ---------- Path picker ----------

const picker = { mode: "dir", current: "", parent: null, selected: "", resolve: null };
const PICKER_HINTS = {
  dir: "双击进入目录，点“选择”使用当前所在目录。",
  ckpt: "双击进入目录，选择一个 .ckpt 文件。",
  pth: "双击进入目录，选择一个 .pth 文件。",
  audio: "双击进入目录，选择一个音频文件（.wav / .mp3 / .ogg / .flac）。",
  launcher: LAUNCHER_HINT
};

function openPathPicker(mode, initial, title) {
  picker.mode = mode;
  picker.selected = "";
  $("path-picker-title").textContent = title || "选择路径";
  $("path-picker-hint").textContent = PICKER_HINTS[mode] || "";
  $("path-picker").hidden = false;
  pickerNavigate((initial || "").trim(), true);
  return new Promise((resolve) => { picker.resolve = resolve; });
}

function closePathPicker(result) {
  $("path-picker").hidden = true;
  const resolve = picker.resolve;
  picker.resolve = null;
  if (resolve) resolve(result);
}

async function pickerNavigate(path, fallbackToStart = false) {
  try {
    const data = await api(`/api/fs/list?mode=${encodeURIComponent(picker.mode)}&path=${encodeURIComponent(path)}`);
    renderPicker(data);
  } catch (error) {
    if (fallbackToStart && path) { await pickerNavigate("", false); $("path-picker-hint").textContent = `原路径不可用（${error.message}），已打开起始目录。`; return; }
    $("path-picker-hint").textContent = `无法打开：${error.message}`;
  }
}

function renderPicker(data) {
  picker.current = data.path || "";
  picker.parent = data.parent || null;
  picker.selected = data.selected || "";
  $("path-picker-current").value = picker.current;
  $("path-picker-up").disabled = !picker.parent;
  const drives = data.drives || [];
  $("path-picker-drives").innerHTML = '<option value="">盘符…</option>' + drives.map((drive) => `<option value="${escapeHtml(drive)}">${escapeHtml(drive)}</option>`).join("");
  const list = $("path-picker-list");
  if (!picker.current) {
    list.innerHTML = drives.map((drive) => `<li class="path-picker-item" data-path="${escapeHtml(drive)}" data-dir="1">💽 ${escapeHtml(drive)}</li>`).join("") || '<li class="placeholder">没有可用的盘符。</li>';
  } else {
    list.innerHTML = (data.entries || []).map((entry) => `<li class="path-picker-item ${entry.path === picker.selected ? "selected" : ""}" data-path="${escapeHtml(entry.path)}" data-dir="${entry.is_dir ? 1 : 0}">${entry.is_dir ? "📁" : (picker.mode === "audio" ? "🎵" : "📄")} ${escapeHtml(entry.name)}</li>`).join("") || '<li class="placeholder">此目录下没有子目录或匹配的文件。</li>';
  }
  list.querySelectorAll(".path-picker-item").forEach((item) => {
    item.addEventListener("click", () => {
      list.querySelectorAll(".path-picker-item.selected").forEach((other) => other.classList.remove("selected"));
      item.classList.add("selected");
      picker.selected = item.dataset.dir === "1" ? "" : item.dataset.path;
    });
    item.addEventListener("dblclick", () => {
      if (item.dataset.dir === "1") pickerNavigate(item.dataset.path);
      else closePathPicker(item.dataset.path);
    });
  });
}

function choosePickerPath() {
  if (picker.mode === "dir") {
    if (!picker.current) { $("path-picker-hint").textContent = "请先进入一个目录。"; return; }
    closePathPicker(picker.current);
    return;
  }
  if (!picker.selected) { $("path-picker-hint").textContent = "请选择一个文件。"; return; }
  closePathPicker(picker.selected);
}

function bindPathPicker() {
  $("path-picker-close").addEventListener("click", () => closePathPicker(null));
  $("path-picker-cancel").addEventListener("click", () => closePathPicker(null));
  $("path-picker-choose").addEventListener("click", choosePickerPath);
  $("path-picker-up").addEventListener("click", () => { if (picker.parent) pickerNavigate(picker.parent); });
  $("path-picker-drives").addEventListener("change", (event) => { if (event.target.value) pickerNavigate(event.target.value); });
  $("path-picker-current").addEventListener("keydown", (event) => { if (event.key === "Enter") { event.preventDefault(); pickerNavigate(event.target.value.trim()); } });
  document.addEventListener("keydown", (event) => { if (event.key === "Escape" && !$("path-picker").hidden) closePathPicker(null); });
  document.addEventListener("click", async (event) => {
    const button = event.target.closest("[data-browse]");
    if (!button) return;
    event.preventDefault();
    const input = button.closest(".path-input-row")?.querySelector("input");
    if (!input) return;
    const chosen = await openPathPicker(button.dataset.browse, input.value, button.dataset.browseTitle);
    if (chosen) { input.value = chosen; input.dispatchEvent(new Event("input", { bubbles: true })); }
  });
}

// ---------- Pages ----------

function switchPage(page, tab = "") {
  page = resolvePage(page);
  document.querySelectorAll(".page").forEach((item) => item.classList.toggle("active", item.id === page));
  document.querySelectorAll(".nav-item").forEach((item) => item.classList.toggle("active", item.dataset.page === page));
  if (TABBED_PAGES.includes(page)) selectTab(page, tab || pageTab[page], { silent: true });
  const wanted = hashFor(page, pageTab[page] || "");
  if (location.hash !== wanted) history.replaceState(null, "", wanted);
  refreshDirtyUi();
  renderHealth();
  if (page === "overview") refreshOverview(true).catch(showError);
  if (page === "install") { loadInstall().catch(showError); loadDeps().catch(showError); }
  if (page === "voice") { refreshService(); loadTts().catch(showError); loadAll(false).catch(showError); }
  if (page === "emotions") loadAll(false).catch(showError);
  if (page === "stickers") loadStickers().catch(showError);
  if (page === "schedule") loadSchedule().catch(showError);
  if (page === "sing") loadCover().catch(showError);
  if (page === "affinity") loadAffinity().catch(showError);
  if (page === "injection") loadInjection().catch(showError);
  if (page === "connect") { loadChatPlatform().catch(showError); loadOwner().catch(showError); loadQueue().catch(showError); }
  if (page === "system") { loadCleanup().catch(showError); loadTexts().catch(showError); loadNetwork().catch(showError); loadLogs().catch(showError); }
  loadPresets().catch(console.error);
}

// ---------- 总预设 (whole-config presets) ----------
//
// Not a settings form: nothing here goes through the page save bar. Every button acts at once on what is
// saved on the server; the sidebar shows which preset is current and whether the settings drifted from it.

const PAGE_IDS = ["overview", "install", "connect", "voice", "emotions", "stickers", "schedule", "sing", "affinity", "injection", "system"];
let presetTimer = null;
let presetBusy = false;

function scheduleLoadPresets() {
  clearTimeout(presetTimer);
  presetTimer = setTimeout(() => loadPresets().catch(console.error), 400);
}

async function loadPresets() {
  state.presets = await api("/api/presets");
  renderPresets();
}

function presetRow(item, { auto = false } = {}) {
  const id = escapeHtml(item.id);
  const time = presetTime(item.updated_at);
  const badges = (item.current ? `<span class="preset-badge">当前${state.presets?.modified ? "·已修改" : ""}</span>` : "")
    + (item.broken ? `<span class="preset-badge warn">文件损坏</span>` : "");
  const action = (name, label, kind = "btn-secondary") => `<button type="button" class="btn ${kind} btn-sm" data-preset-action="${name}" data-preset-id="${id}">${label}</button>`;
  const apply = item.broken ? "" : action("apply", item.current && !state.presets?.modified ? "重新载入" : "切换", "btn-primary");
  const menu = [
    auto || item.broken ? "" : action("overwrite", "用当前设置覆盖"),
    auto || item.broken ? "" : action("rename", "重命名"),
    item.broken ? "" : action("export", "导出"),
    action("delete", "删除", "btn-danger-ghost")
  ].join("");
  return `<div class="console-list-item list-row preset-row" data-preset-row="${id}"><div class="row-main preset-name"><strong>${escapeHtml(item.name)}</strong>${badges}<small>${time ? `更新于 ${escapeHtml(time)}` : ""}</small></div><div class="row-actions">${apply}${rowMenu(menu)}</div></div>`;
}

function renderPresets() {
  const listing = state.presets;
  $("preset-current").textContent = presetLabel(listing);
  $("preset-modal-current").textContent = presetLabel(listing);
  const badge = presetBadge(listing);
  setBadge("preset-switcher-badge", badge.text, badge.kind);
  $("preset-switcher-badge").hidden = !badge.text;
  const select = $("preset-select");
  const options = ['<option value="">切换预设…</option>'];
  for (const item of listing?.presets || []) if (!item.broken) options.push(`<option value="${escapeHtml(item.id)}">${escapeHtml(item.name)}</option>`);
  const autos = (listing?.autos || []).filter((item) => !item.broken);
  if (autos.length) options.push(`<optgroup label="切换前的自动存档">${autos.map((item) => `<option value="${escapeHtml(item.id)}">${escapeHtml(item.name)}</option>`).join("")}</optgroup>`);
  select.innerHTML = options.join("");
  select.value = "";
  const list = $("preset-list");
  list.innerHTML = (listing?.presets || []).length
    ? listing.presets.map((item) => presetRow(item)).join("")
    : emptyStateMarkup("presets");
  const autoList = $("preset-auto-list");
  autoList.innerHTML = (listing?.autos || []).length
    ? listing.autos.map((item) => presetRow(item, { auto: true })).join("")
    : emptyStateMarkup("autos");
}

function showPresetResult(text, isError = false) {
  const box = $("preset-result");
  box.textContent = text;
  box.hidden = !text;
  box.classList.toggle("console-note-error", isError);
}

/** Unsaved edits anywhere (a page, or an open form card) would be lost by a switch: ask first. */
async function confirmDropUnsaved(actionText) {
  if (!parts.some(isDirty)) return true;
  const choice = await choiceDialog(`页面上还有没保存的修改，${actionText}会丢弃它们。`, [
    { label: "取消", value: "cancel", kind: "secondary" },
    { label: "放弃修改并继续", value: "discard", kind: "danger", primary: true }
  ]);
  return choice === "discard";
}

/** After the settings changed under the page: refill every part from the server, dropping what was typed. */
async function refreshAllPages() {
  state.selectedCharacter = "";
  for (const page of PAGE_IDS) await discardPage(page);
  try { await loadAll(true); } catch (error) { showError(error); }
}

async function applyPreset(id) {
  if (presetBusy) return;
  const item = [...(state.presets?.presets || []), ...(state.presets?.autos || [])].find((entry) => entry.id === id);
  if (!item || !(await confirmDropUnsaved("切换预设"))) return;
  presetBusy = true;
  try {
    const result = await api(`/api/presets/${id}/apply`, { method: "POST", body: "{}" });
    state.presets = result.list;
    renderPresets();
    await refreshAllPages();
    const text = applyResultText(result, item.name);
    showPresetResult(text, result.warnings?.length > 0);
    notify(applyNotifyText(result, item.name), result.warnings?.length > 0);
  } catch (error) { showError(error); } finally { presetBusy = false; }
  loadPresets().catch(console.error);
}

async function saveAsPreset() {
  const name = $("preset-new-name").value.trim();
  const problem = presetNameProblem(name);
  if (problem) { notify(problem, true); $("preset-new-name").focus(); return; }
  if (parts.some(isDirty) && !(await confirmDialog("页面上还有没保存的修改，预设只会保存已经保存的设置。继续吗？", { confirmLabel: "继续" }))) return;
  try {
    await api("/api/presets", { method: "POST", body: JSON.stringify({ name }) });
    $("preset-new-name").value = "";
    notify(`已把当前设置保存为预设「${name}」`);
    showPresetResult("");
  } catch (error) { showError(error); }
  await loadPresets().catch(console.error);
}

async function overwritePreset(item) {
  if (parts.some(isDirty) && !(await confirmDialog("页面上还有没保存的修改，预设只会保存已经保存的设置。继续吗？", { confirmLabel: "继续" }))) return;
  if (!(await confirmDialog(`用当前设置覆盖预设「${item.name}」？它原来的内容会被替换。`, { confirmLabel: "覆盖" }))) return;
  try {
    await api(`/api/presets/${item.id}`, { method: "PUT", body: JSON.stringify({ use_current: true }) });
    notify(`预设「${item.name}」已更新`);
  } catch (error) { showError(error); }
  await loadPresets().catch(console.error);
}

async function renamePreset(item) {
  const name = await promptDialog("新的预设名称：", item.name);
  if (name === null) return;
  const problem = presetNameProblem(name);
  if (problem) { notify(problem, true); return; }
  try {
    await api(`/api/presets/${item.id}`, { method: "PUT", body: JSON.stringify({ name: name.trim() }) });
    notify("已重命名");
  } catch (error) { showError(error); }
  await loadPresets().catch(console.error);
}

async function deletePreset(item) {
  if (!(await confirmDialog(`删除预设「${item.name}」？${item.current ? "它是当前预设，删除后当前设置不变，只是不再属于任何预设。" : ""}`, { danger: true }))) return;
  try {
    await api(`/api/presets/${item.id}`, { method: "DELETE" });
    notify("已删除");
  } catch (error) { showError(error); }
  await loadPresets().catch(console.error);
}

function exportPreset(item) {
  const link = document.createElement("a");
  link.href = `/api/presets/${item.id}/export`;
  link.download = `${item.name}.json`;
  document.body.appendChild(link);
  link.click();
  link.remove();
}

async function importPresetFile(file) {
  if (!file) return;
  if (file.size > 1024 * 1024) { notify("预设文件太大（最多 1 MB）", true); return; }
  try {
    const result = await api("/api/presets/import", { method: "POST", body: await file.text() });
    showPresetResult(importResultText(result), (result.missing_paths || []).length > 0);
    notify(`已导入「${result.preset.name}」`);
  } catch (error) { showError(error); }
  await loadPresets().catch(console.error);
}

function openPresetModal() {
  $("preset-modal").hidden = false;
  loadPresets().catch(console.error);
  $("preset-modal-close").focus();
}

function closePresetModal() {
  $("preset-modal").hidden = true;
  $("preset-switcher").focus();
}

function bindPresets() {
  $("preset-switcher").addEventListener("click", openPresetModal);
  $("preset-modal-close").addEventListener("click", closePresetModal);
  $("preset-modal").addEventListener("click", (event) => { if (event.target === $("preset-modal")) closePresetModal(); });
  document.addEventListener("keydown", (event) => {
    // The shared question dialog (z-index above) owns Esc while it is open.
    if (event.key === "Escape" && !$("preset-modal").hidden && $("app-dialog").hidden) { event.preventDefault(); closePresetModal(); }
  });
  $("preset-select").addEventListener("change", () => {
    const id = $("preset-select").value;
    $("preset-select").value = "";
    if (id) applyPreset(id);
  });
  $("preset-save-new").addEventListener("click", saveAsPreset);
  $("preset-new-name").addEventListener("keydown", (event) => { if (event.key === "Enter") { event.preventDefault(); saveAsPreset(); } });
  $("preset-import").addEventListener("click", () => $("preset-import-file").click());
  $("preset-import-file").addEventListener("change", () => { const file = $("preset-import-file").files[0]; $("preset-import-file").value = ""; importPresetFile(file); });
  $("preset-card").addEventListener("click", (event) => {
    const button = event.target.closest("[data-preset-action]");
    if (!button) return;
    const id = button.dataset.presetId;
    const item = [...(state.presets?.presets || []), ...(state.presets?.autos || [])].find((entry) => entry.id === id);
    if (!item) return;
    const action = button.dataset.presetAction;
    if (action === "apply") applyPreset(id);
    else if (action === "overwrite") overwritePreset(item);
    else if (action === "rename") renamePreset(item);
    else if (action === "export") exportPreset(item);
    else if (action === "delete") deletePreset(item);
  });
}

// ---------- Affinity ----------

function tierRow(tier = { min: 0, max: 100, name: "", description: "" }) {
  return `<div class="console-list-item affinity-tier list-row"><input class="input" data-tier="min" type="number" min="-100" max="100" step="1" value="${tier.min}" aria-label="最小值"><span>～</span><input class="input" data-tier="max" type="number" min="-100" max="100" step="1" value="${tier.max}" aria-label="最大值"><input class="input" data-tier="name" maxlength="20" value="${escapeHtml(tier.name)}" placeholder="档位名称" aria-label="档位名称">${rowMenu('<button type="button" class="btn btn-danger-ghost btn-sm" data-remove-tier>删除</button>')}<textarea class="input" data-tier="description" rows="2" maxlength="500" placeholder="态度描述：这一档的角色对对方是什么态度（可用 {{char}} 代表角色名）" aria-label="态度描述">${escapeHtml(tier.description || "")}</textarea></div>`;
}

function bindTierButtons() {
  for (const id of ["affinity-tiers", "group-affinity-tiers"]) {
    $(id).querySelectorAll("[data-remove-tier]").forEach((button) => button.addEventListener("click", () => { button.closest(".affinity-tier").remove(); refreshDirtyUi(); }));
  }
}

function readTiers(containerId) {
  return [...$(containerId).querySelectorAll(".affinity-tier")].map((row) => ({
    min: Number(row.querySelector('[data-tier="min"]').value),
    max: Number(row.querySelector('[data-tier="max"]').value),
    name: row.querySelector('[data-tier="name"]').value,
    description: row.querySelector('[data-tier="description"]').value
  }));
}

function fillAffinitySettings() {
  const settings = state.affinity.settings;
  $("affinity-character-name").textContent = settings.character;
  $("affinity-nickname").textContent = settings.display_name;
  $("affinity-initial").value = settings.initial_value;
  $("affinity-max-delta").value = settings.max_delta;
  $("affinity-tiers").innerHTML = settings.tiers.map(tierRow).join("");
  const group = settings.group;
  $("group-affinity-variable").value = group.variable;
  $("group-affinity-role-tag").value = group.role_tag;
  $("group-affinity-min").value = group.min;
  $("group-affinity-max").value = group.max;
  $("group-affinity-initial").value = group.initial_value;
  $("group-affinity-tiers").innerHTML = group.tiers.map(tierRow).join("");
  bindTierButtons();
}

function fillAffinityUsers() {
  const group = state.affinity.settings.group;
  const users = state.affinity.users || [];
  $("affinity-users").innerHTML = users.length ? users.map((user) => `
    <div class="console-list-item list-row" data-search="${escapeHtml(`${user.nickname || ""} ${user.id}`)}" data-id="${escapeHtml(user.id)}" data-nickname="${escapeHtml(user.nickname || "")}" data-value="${escapeHtml(user.value)}" data-updated="${escapeHtml(user.updated_at || "")}"><div class="row-main"><strong>${escapeHtml(user.nickname || "（未记录昵称）")}</strong><small><span class="badge-count">${user.track === "owner" ? "所有者" : "群友"}</span> ID ${escapeHtml(user.id)} · ${escapeHtml(user.platform)} · ${escapeHtml(user.tier)} · 更新于 ${escapeHtml(user.updated_at ? new Date(user.updated_at).toLocaleString() : "-")}</small></div><div class="row-actions"><input class="input affinity-value-input" type="number" min="${user.track === "owner" ? 0 : group.min}" max="${user.track === "owner" ? 100 : group.max}" step="1" value="${user.value}" data-affinity-user="${escapeHtml(user.id)}" aria-label="好感度"></div></div>`).join("") : emptyStateMarkup("users");
  refreshList("affinity-users");
}

async function loadAffinity() {
  try {
    state.affinity = await api("/api/affinity");
    $("affinity-user-count").textContent = `${(state.affinity.users || []).length} 位用户`;
    fillPart(P.affinitySettings);
    fillPart(P.affinityUsers);
  } catch (error) {
    $("affinity-users").innerHTML = `<p class="placeholder">好感度数据不可用：${escapeHtml(error.message)}</p>`;
    throw error;
  }
}

// ---------- Queue ----------

async function loadQueue() {
  const data = await api("/api/queue");
  state.queue = data;
  fillPart(P.queue);
}

// ---------- Mood ----------

async function loadMood() {
  const data = await api("/api/mood");
  state.mood = data;
  fillPart(P.mood);
  $("mood-global").textContent = data.available ? data.global.text : "不可用";
  $("mood-owner").textContent = data.available ? data.owner.text : "不可用";
  $("mood-reset-global").disabled = !data.available || data.global.count === 0;
  $("mood-reset-owner").disabled = !data.available || data.owner.count === 0;
}

async function resetMood(track) {
  const label = track === "owner" ? "对主人的心情" : "整体心情";
  if (!(await confirmDialog(`清空“${label}”的记录？清空后这一项回到“还没有记录”。`, { danger: true, confirmLabel: "清空" }))) return;
  try {
    state.mood = await api("/api/mood/reset", { method: "POST", body: JSON.stringify({ track }) });
    notify(`已清空${label}`);
    await loadMood();
  } catch (error) { showError(error); }
}

function renderModels() {
  const keyword = ($("model-filter").value || "").trim().toLowerCase();
  const models = state.models.filter((model) => !keyword || model.name.toLowerCase().includes(keyword));
  $("model-total").textContent = `${models.length} 个模型`;
  $("models-list").innerHTML = models.length ? models.map((model) => `
    <article class="model-card ${model.valid ? "" : "invalid"}">
      <div class="model-card-header"><div class="model-card-title-wrap"><span class="model-card-title">${escapeHtml(model.name)}</span></div><span class="status-badge ${model.valid ? "status-success" : "status-error"}">${model.valid ? "● 已配置" : "● 路径不完整"}</span></div>
      <div class="model-files"><div class="file-status ${model.files.gpt_weights ? "valid" : "invalid"}"><span>GPT 权重 (.ckpt)</span><span class="file-status-pill">${model.files.gpt_weights ? "存在" : "缺失"}</span></div><div class="file-status ${model.files.sovits_weights ? "valid" : "invalid"}"><span>SoVITS 权重 (.pth)</span><span class="file-status-pill">${model.files.sovits_weights ? "存在" : "缺失"}</span></div><div class="file-status ${model.files.reference_audios ? "valid" : "invalid"}"><span>参考音频目录</span><span class="file-status-pill">${model.files.reference_audios ? "存在" : "默认目录"}</span></div></div>
      <div class="model-actions"><button class="btn btn-secondary btn-sm" data-edit-model="${escapeHtml(model.name)}">编辑</button><button class="btn btn-secondary btn-sm" data-open-audio-model="${escapeHtml(model.name)}">参考音频</button>${rowMenu(`<button type="button" class="btn btn-danger-ghost btn-sm" data-delete-model="${escapeHtml(model.name)}">删除配置</button>`)}</div>
    </article>`).join("") : emptyStateMarkup("models");
  $("models-list").querySelectorAll("[data-edit-model]").forEach((button) => button.addEventListener("click", async () => { if (await confirmDiscardCard(P.modelCard)) openModel(button.dataset.editModel); }));
  $("models-list").querySelectorAll("[data-open-audio-model]").forEach((button) => button.addEventListener("click", () => {
    const character = state.characters.find((item) => item.model_name === button.dataset.openAudioModel);
    if (character) {
      requestPage("emotions").then(() => {
        if (activePage() !== "emotions") return;
        state.selectedCharacter = character.name; state.discovered = [];
        renderCharacters(); refreshEmotions("force");
      });
    }
    else notify("请先创建绑定该模型的角色。", true);
  }));
  $("models-list").querySelectorAll("[data-delete-model]").forEach((button) => button.addEventListener("click", async () => {
    if (!(await confirmDialog(`删除模型配置“${button.dataset.deleteModel}”？不会删除权重文件。`, { danger: true }))) return;
    try { await api(`/api/models/${encodeURIComponent(button.dataset.deleteModel)}`, { method: "DELETE" }); await loadAll(); } catch (error) { showError(error); }
  }));
}

function openModel(name = "") {
  const model = state.models.find((item) => item.name === name);
  $("model-form-card").hidden = false;
  $("model-form-title").textContent = model ? "编辑模型路径" : "添加模型";
  $("model-name").value = model?.name || "";
  $("model-name").readOnly = Boolean(model);
  $("model-gpt").value = model?.gpt_model_path || "";
  $("model-sovits").value = model?.sovits_model_path || "";
  $("model-audio-root").value = model?.reference_audio_root || "";
  takeSnapshot(P.modelCard); refreshDirtyUi();
  reveal($("model-form-card"));
}

function renderCharacterOptions() {
  const chosenModel = $("character-model").value;
  $("character-model").innerHTML = '<option value="">未绑定</option>' + state.models.map((item) => `<option value="${escapeHtml(item.name)}">${escapeHtml(item.name)}</option>`).join("");
  $("character-model").value = chosenModel;
}

function renderEmotionOptions() {
  const character = state.characters.find((item) => item.name === state.selectedCharacter);
  const names = new Set([
    ...state.discovered.map((emotion) => emotion.name),
    ...Object.keys(character?.emotions || {})
  ]);
  $("emotion-options").innerHTML = [...names].map((name) => `<option value="${escapeHtml(name)}"></option>`).join("");
}

function renderCharacters() {
  $("characters-list").innerHTML = state.characters.length ? state.characters.map((character) => `
    <div class="console-list-item list-row ${state.selectedCharacter === character.name ? "active" : ""}" data-character="${escapeHtml(character.name)}"><div class="row-main"><strong>${escapeHtml(character.name)}</strong>${character.name === state.config?.default_character ? '<span class="status-badge status-success">默认</span>' : ""}<small>昵称 ${escapeHtml(character.nickname || "-")} · ${escapeHtml(character.model_name || "未绑定模型")} · 默认情绪 ${escapeHtml(character.default_emotion || "neutral")}</small></div><div class="row-actions"><button class="btn btn-secondary btn-sm" data-edit-character="${escapeHtml(character.name)}">编辑</button>${rowMenu(characterMenuButtons(character.name, character.name === state.config?.default_character))}</div></div>`).join("") : emptyStateMarkup("characters");
  $("characters-list").querySelectorAll("[data-character]").forEach((item) => item.addEventListener("click", (event) => {
    if (event.target.closest("button")) return;
    chooseCharacter(item.dataset.character);
  }));
  bindCharacterActions($("characters-list"));
}

/** Opening the card for another character also switches the emotion list, so that asks about unsaved emotions. */
async function requestOpenCharacter(name = "") {
  if (name && name !== state.selectedCharacter) { if (!(await guardLeave())) return; }
  else if (!(await confirmDiscardCard(P.characterCard))) return;
  openCharacter(name);
}

function openCharacter(name = "") {
  const character = state.characters.find((item) => item.name === name);
  if (character && state.selectedCharacter !== character.name) {
    state.selectedCharacter = character.name; state.discovered = [];
    renderCharacters(); refreshEmotions("force");
  }
  $("character-form-card").hidden = false;
  $("character-form-title").textContent = character ? `编辑角色：${character.name}` : "添加角色";
  $("character-name").value = character?.name || "";
  $("character-name").readOnly = Boolean(character);
  $("character-nickname").value = character?.nickname || "";
  $("character-st-name").value = character?.st_name || "";
  $("character-model").value = character?.model_name || "";
  $("character-default-emotion").value = character?.default_emotion || "neutral";
  takeSnapshot(P.characterCard); refreshDirtyUi();
  reveal($("character-form-card"));
}

function renderPresetBox(character) {
  const box = $("preset-prompt-box");
  if (!character) { box.innerHTML = ""; return; }
  const engine = currentEngine();
  const { text } = buildPresetPrompt(engine, state.discovered, character.emotions);
  const how = engine === "irodori"
    ? "Irodori 用手动覆盖里的 <strong>caption（风格描述）和表情</strong>控制语气，<strong>不需要参考音频</strong>；配了其中任一项的情绪才会出现在下面的预设提示里。"
    : "预设提示里的情绪包括：参考音频目录里有可用音频的情绪，以及手动配置了参考音频的情绪。";
  box.innerHTML = `<div class="card console-note-card"><p>${how}</p>${text ? `<p>ST 预设提示词：</p><div class="preset-row"><code id="preset-prompt">${escapeHtml(text)}</code><button type="button" class="btn btn-secondary btn-sm" id="copy-preset">复制</button></div>` : '<p class="placeholder">还没有可用的情绪。</p>'}</div>`;
  $("copy-preset")?.addEventListener("click", async () => {
    try { await navigator.clipboard.writeText(text); $("copy-preset").textContent = "已复制"; }
    catch { await copyDialog(text); }
  });
}

function emotionFormFields(engine, name, profile) {
  const mono = (label, control, help, extra = {}) => fieldMarkup({ label, control, help, mono: true, ...extra });
  if (engine === "irodori") {
    return `<div class="module-body">${mono("caption", `<textarea name="caption" rows="2" maxlength="300" placeholder="例：嬉しそうに、明るく弾んだ声で楽しげに話している。">${escapeHtml(profile.caption || "")}</textarea>`, "Irodori 风格描述（日文），可空", { wide: true })}${mono("emoji", `<input name="emoji" maxlength="16" value="${escapeHtml(profile.emoji || "")}" placeholder="例：😊">`, "加在句首的表情，可空")}</div>`;
  }
  const reference = [
    mono("ref_audio_path", `<div class="path-input-row"><input name="ref_audio_path" value="${escapeHtml(profile.ref_audio_path)}" placeholder="留空则使用目录识别的音频">${browseButton("audio", "选择参考音频")}</div>`, "参考音频路径", { wide: true }),
    mono("prompt_text", `<textarea name="prompt_text" rows="2">${escapeHtml(profile.prompt_text)}</textarea>`, "参考文本", { wide: true }),
    mono("prompt_lang", `<input name="prompt_text_lang" value="${escapeHtml(profile.prompt_text_lang)}">`, "参考文本语言"),
    mono("text_lang", `<input name="text_lang" value="${escapeHtml(profile.text_lang)}">`, "目标合成语言")
  ].join("");
  const generation = [
    mono("top_k", `<input name="top_k" type="number" min="1" max="100" value="${profile.top_k}">`, "1～100"),
    mono("top_p", `<input name="top_p" type="number" min="0" max="1" step="0.01" value="${profile.top_p}">`, "0～1"),
    mono("temperature", `<input name="temperature" type="number" min="0" max="2" step="0.05" value="${profile.temperature}">`, "0～2"),
    mono("speed_factor", `<input name="speed_factor" type="number" min="0.25" max="2" step="0.05" value="${profile.speed_factor}">`, "0.25～2"),
    fieldMarkup({ label: "text_split", mono: true, help: "启用句子切分", switchField: true, control: `<input name="split_sentence" type="checkbox" ${profile.split_sentence ? "checked" : ""}>` })
  ].join("");
  return moduleMarkup("emotion-gpt-ref", "参考音频", reference) + moduleMarkup("emotion-gpt-gen", "生成参数", generation);
}

// ---------- Stickers ----------

function stickerUrl(emotion, name) {
  return `/api/stickers/file?emotion=${encodeURIComponent(emotion)}&name=${encodeURIComponent(name)}`;
}

function stickerItem(emotion, image) {
  return `<figure class="sticker-item"><span class="sticker-menu">${rowMenu(`<button type="button" class="btn btn-danger-ghost btn-sm" data-delete-sticker="${escapeHtml(image.name)}" data-sticker-emotion="${escapeHtml(emotion)}">删除</button>`)}</span><img loading="lazy" alt="${escapeHtml(image.name)}" src="${stickerUrl(emotion, image.name)}"><small title="${escapeHtml(image.name)}">${escapeHtml(image.name)} · ${formatBytes(image.size)}</small></figure>`;
}

async function loadStickers() {
  const data = await api("/api/stickers");
  state.stickers = data;
  fillPart(P.stickerSettings);
  const list = $("sticker-list");
  list.innerHTML = data.groups.length ? data.groups.map((group) => `<details class="emotion-editor audio-group" data-sticker-group="${escapeHtml(group.name)}" ${state.openStickerGroups.has(group.name) ? "open" : ""}><summary><strong>${escapeHtml(stickerGroupTitle(group))}</strong></summary>${group.images.length ? `<div class="sticker-grid">${group.images.map((image) => stickerItem(group.name, image)).join("")}</div>` : emptyStateMarkup("stickers", { upload: escapeHtml(group.name) })}<div class="console-actions"><label class="btn btn-secondary btn-sm file-button">上传到“${escapeHtml(group.name)}”（可多选）<input type="file" multiple accept="${STICKER_ACCEPT}" data-upload-sticker="${escapeHtml(group.name)}"></label></div></details>`).join("") : emptyStateMarkup("stickerGroups");
  list.querySelectorAll(".sticker-grid").forEach((grid) => refreshGroupList(`sticker:${grid.closest("[data-sticker-group]").dataset.stickerGroup}`, grid, ".sticker-item", STICKER_PAGE_SIZE));
  list.querySelectorAll("[data-sticker-group]").forEach((details) => details.addEventListener("toggle", () => {
    if (details.open) state.openStickerGroups.add(details.dataset.stickerGroup); else state.openStickerGroups.delete(details.dataset.stickerGroup);
  }));
  list.querySelectorAll("[data-upload-sticker]").forEach((input) => input.addEventListener("change", () => uploadStickers(input.dataset.uploadSticker, [...input.files]).finally(() => { input.value = ""; })));
  list.querySelectorAll("[data-delete-sticker]").forEach((button) => button.addEventListener("click", async () => {
    if (!(await confirmDialog("删除这张表情包？", { danger: true }))) return;
    try { await api(`/api/stickers?emotion=${encodeURIComponent(button.dataset.stickerEmotion)}&name=${encodeURIComponent(button.dataset.deleteSticker)}`, { method: "DELETE" }); await loadStickers(); } catch (error) { showError(error); }
  }));
}

async function uploadStickers(emotion, files) {
  if (!files.length) return;
  const form = new FormData(); form.append("emotion", emotion);
  for (const file of files) form.append("file", file);
  state.openStickerGroups.add(emotion);
  try {
    notify(uploadSummary(await api("/api/stickers/upload", { method: "POST", body: form })));
  } catch (error) { showError(error); }
  await loadStickers().catch(() => {});
}

// ---------- Scheduled messages ----------

function scheduleGroups() {
  return state.schedule?.targets?.groups || [];
}

function renderScheduleTargets() {
  const targets = state.schedule.targets;
  const groups = targets.groups;
  $("schedule-targets").innerHTML = `<div class="info-item"><span class="label">主人私聊</span><span class="value">${targets.owner_known ? "已记录" : `未记录：请先用主人账号私聊${escapeHtml(currentNickname())}一次`}</span></div>${groups.length ? groups.map((group) => `<div class="target-row"><input class="input console-inline-input" data-alias-input="${escapeHtml(group.key)}" value="${escapeHtml(group.custom_alias)}" placeholder="${escapeHtml(group.alias)}" maxlength="40"><small>最后一次见到：${escapeHtml(formatDateTime(group.last_seen) || "-")}</small></div>`).join("") : '<p class="placeholder">还没有见过任何群：群里有人发过消息后会出现在这里。</p>'}`;
  $("schedule-group-note").textContent = GROUP_SEND_NOTE;
  refreshList("aliases");
}

function planCard(plan) {
  const testing = state.testing.has(plan.id);
  return `<article class="emotion-editor plan-card" data-plan="${escapeHtml(plan.id)}"><header><strong>${escapeHtml(plan.name)}</strong><label class="console-check"><input type="checkbox" data-plan-toggle="${escapeHtml(plan.id)}" ${plan.enabled ? "checked" : ""}> 启用</label></header><div class="plan-meta"><span>${escapeHtml(planSummary(plan))}</span><span>发送到：${escapeHtml(targetLabel(plan.target, scheduleGroups()))}</span><span>下次触发：${escapeHtml(nextFireText(plan))}</span>${plan.running ? '<span class="status-badge status-success">运行中</span>' : ""}</div><div class="plan-actions"><button class="btn btn-secondary btn-sm" data-plan-edit="${escapeHtml(plan.id)}">编辑</button><button class="btn btn-secondary btn-sm" data-plan-test="${escapeHtml(plan.id)}" ${testing ? "disabled" : ""}>${testing ? "生成中…" : "立即试发"}</button>${rowMenu(`<button type="button" class="btn btn-danger-ghost btn-sm" data-plan-delete="${escapeHtml(plan.id)}">删除</button>`)}</div><p class="console-note plan-result" data-plan-result="${escapeHtml(plan.id)}" hidden></p></article>`;
}

function renderPlans() {
  const plans = state.schedule.plans;
  const list = $("plan-list");
  list.innerHTML = plans.length ? plans.map(planCard).join("") : emptyStateMarkup("plans");
  const find = (id) => plans.find((plan) => plan.id === id);
  list.querySelectorAll("[data-plan-edit]").forEach((button) => button.addEventListener("click", async () => { if (await confirmDiscardCard(P.planCard)) openPlanForm(planToForm(find(button.dataset.planEdit)), "编辑计划"); }));
  list.querySelectorAll("[data-plan-delete]").forEach((button) => button.addEventListener("click", async () => {
    const plan = find(button.dataset.planDelete);
    if (!(await confirmDialog(`删除计划“${plan.name}”？`, { danger: true }))) return;
    try { await api(`/api/schedule/plans/${encodeURIComponent(plan.id)}`, { method: "DELETE" }); await loadSchedule(); } catch (error) { showError(error); }
  }));
  list.querySelectorAll("[data-plan-test]").forEach((button) => button.addEventListener("click", () => testPlan(find(button.dataset.planTest))));
}

async function testPlan(plan) {
  const id = plan.id;
  state.testing.add(id);
  const card = $("plan-list").querySelector(`[data-plan="${CSS.escape(id)}"]`);
  const button = card?.querySelector("[data-plan-test]");
  if (button) { button.disabled = true; button.textContent = "生成中…"; }
  let lines;
  try {
    const data = await api(`/api/schedule/plans/${encodeURIComponent(id)}/test`, { method: "POST" });
    lines = testResultLines(data.result, plan.target?.type === "group", currentNickname());
  } catch (error) {
    lines = [`试发失败：${error.message}`];
  }
  state.testing.delete(id);
  // Re-render for the fresh next-fire time, then show the result on the same card.
  try { await loadSchedule(); } catch (error) { console.error(error); }
  const target = $("plan-list").querySelector(`[data-plan-result="${CSS.escape(id)}"]`);
  if (target) { target.textContent = lines.join("\n"); target.hidden = false; }
}

function renderTemplateOptions() {
  const select = $("plan-template");
  select.innerHTML = '<option value="">从模板添加…</option>' + state.schedule.templates.map((template, index) => `<option value="${index}">${escapeHtml(template.name)}</option>`).join("");
}

function renderTargetOptions(selected) {
  const options = [['private', '私聊（主人）'], ...scheduleGroups().map((group) => [`group:${group.key}`, group.alias])];
  if (selected && !options.some(([value]) => value === selected)) options.push([selected, targetLabel({ type: "group", key: selected.slice(6) }, [])]);
  $("plan-target").innerHTML = options.map(([value, label]) => `<option value="${escapeHtml(value)}" ${value === selected ? "selected" : ""}>${escapeHtml(label)}</option>`).join("");
}

function togglePlanKind() {
  const interval = $("plan-kind").value === "interval";
  $("plan-daily-fields").hidden = interval;
  $("plan-interval-fields").hidden = !interval;
}

function openPlanForm(form, title) {
  $("plan-form-title").textContent = title;
  $("plan-form").dataset.planId = form.id;
  $("plan-name").value = form.name; $("plan-enabled").checked = form.enabled; $("plan-kind").value = form.kind;
  $("plan-time").value = form.time; $("plan-jitter").value = form.jitter;
  $("plan-weekdays").innerHTML = WEEKDAY_LABELS.map((label, day) => `<label><input type="checkbox" data-weekday="${day}" ${form.weekdays.includes(day) ? "checked" : ""}> ${label}</label>`).join("");
  $("plan-start").value = form.start; $("plan-end").value = form.end; $("plan-every").value = form.every;
  renderTargetOptions(form.target);
  $("plan-prompt").value = form.prompt; $("plan-fallbacks").value = form.fallbackText;
  $("plan-error").hidden = true;
  togglePlanKind();
  $("plan-form-card").hidden = false;
  takeSnapshot(P.planCard); refreshDirtyUi();
  reveal($("plan-form-card"));
}

function readPlanForm() {
  return {
    id: $("plan-form").dataset.planId || "",
    name: $("plan-name").value, enabled: $("plan-enabled").checked, kind: $("plan-kind").value,
    time: $("plan-time").value,
    weekdays: [...$("plan-weekdays").querySelectorAll("[data-weekday]:checked")].map((box) => Number(box.dataset.weekday)),
    jitter: $("plan-jitter").value, start: $("plan-start").value, end: $("plan-end").value, every: $("plan-every").value,
    target: $("plan-target").value, prompt: $("plan-prompt").value, fallbackText: $("plan-fallbacks").value
  };
}

function showPlanError(text) {
  $("plan-error").textContent = text;
  $("plan-error").hidden = !text;
}

async function submitPlan(event) {
  event.preventDefault();
  const form = readPlanForm();
  const errors = validatePlanForm(form);
  if (errors.length) { showPlanError(errors.join("；")); return; }
  try {
    await api("/api/schedule/plans", { method: "POST", body: JSON.stringify(buildPlanBody(form)) });
    $("plan-form-card").hidden = true;
    await loadSchedule();
  } catch (error) { showPlanError(`保存失败：${error.message}`); }
}

// ---------- Proactive chat (主动搭话) ----------

function windowRowMarkup(name, index, item) {
  return `<div class="chat-window-row" data-window-row="${name}"><input type="time" data-win="${name}:${index}:start" value="${escapeHtml(item.start)}"> – <input type="time" data-win="${name}:${index}:end" value="${escapeHtml(item.end)}"><button type="button" class="btn btn-danger-ghost btn-sm" data-window-remove="${name}:${index}">删除</button></div>`;
}

function renderChatWindows(name, list) {
  const box = $(`chat-windows-${name}`);
  box.innerHTML = list.length ? list.map((item, index) => windowRowMarkup(name, index, item)).join("") : '<p class="placeholder">不限（任何时间都可以搭话）</p>';
  $(`chat-window-add-${name}`).disabled = list.length >= CHAT_LIMITS.windows;
  box.querySelectorAll("[data-window-remove]").forEach((button) => button.addEventListener("click", () => {
    const [group, index] = button.dataset.windowRemove.split(":");
    const current = readChatWindows()[group];
    current.splice(Number(index), 1);
    renderChatWindows(group, current);
    refreshDirtyUi();
  }));
}

function readChatWindows() {
  const result = { weekday: [], weekend: [] };
  for (const name of Object.keys(result)) {
    $(`chat-windows-${name}`).querySelectorAll("[data-window-row]").forEach((row) => {
      const inputs = row.querySelectorAll("[data-win]");
      result[name].push({ start: inputs[0].value, end: inputs[1].value });
    });
  }
  return result;
}

function addChatWindow(name) {
  const current = readChatWindows()[name];
  if (current.length >= CHAT_LIMITS.windows) return;
  current.push({ start: "09:00", end: "18:00" });
  renderChatWindows(name, current);
  refreshDirtyUi();
}

function renderChatStatus() {
  const chat = state.schedule?.chat;
  const status = chat?.status;
  $("chat-status-text").textContent = chat?.available ? chatStatusText(status) : "不可用";
  $("chat-status-today").textContent = chatCountText(status, "today");
  $("chat-status-unanswered").textContent = chatCountText(status, "unanswered");
  $("chat-test").disabled = !chat?.available || state.chatTesting === true;
}

async function refreshChatStatus() {
  const data = await api("/api/schedule/chat");
  if (state.schedule) state.schedule.chat = { ...state.schedule.chat, ...data, settings: state.schedule.chat?.settings || data.settings };
  renderChatStatus();
}

async function testChat() {
  state.chatTesting = true;
  $("chat-test").disabled = true;
  $("chat-test").textContent = "生成中…";
  let lines;
  try {
    const data = await api("/api/schedule/chat/test", { method: "POST" });
    lines = testResultLines(data.result, false, currentNickname());
  } catch (error) {
    lines = [`试发失败：${error.message}`];
  }
  state.chatTesting = false;
  $("chat-test").textContent = "立即试发";
  try { await refreshChatStatus(); } catch (error) { console.error(error); }
  $("chat-test-result").textContent = lines.join("\n");
  $("chat-test-result").hidden = false;
}

async function loadSchedule() {
  const data = await api("/api/schedule");
  state.schedule = data;
  fillPart(P.scheduleSettings);
  fillPart(P.proactiveChat);
  renderChatStatus();
  renderTemplateOptions();
  fillPart(P.scheduleAliases, "keep");
  fillPart(P.schedulePlans, "keep");
}

// ---------- Storage cleanup ----------

let cleanupData = null;

function renderCleanup() {
  const { stats, settings, last_auto: last, cover_cache: covers } = cleanupData;
  $("cleanup-stats").innerHTML = Object.entries(CLEANUP_TARGETS)
    .map(([name, label]) => `<div class="cover-row"><strong>${escapeHtml(label)}</strong><span>${escapeHtml(String(stats[name]?.files ?? 0))} 个文件 · ${escapeHtml(bytesText(stats[name]?.bytes))}</span></div>`)
    .join("");
  fillPart(P.cleanupSettings);
  $("cleanup-last").textContent = `最近一次自动清理：${lastAutoText(last)}`;
  $("cleanup-cover-cache").textContent = `当前有 ${covers.entries} 条翻唱，占用 ${bytesText(covers.bytes)}`;
  const backups = cleanupData.install_backups;
  $("cleanup-install-backups").textContent = installBackupsText(backups);
  $("cleanup-clear-backups").disabled = !backups || (backups.labels
    && Object.values(backups.labels).every((label) => label.count <= 1));
}

async function clearInstallBackups() {
  if (!(await confirmDialog("把插件和 ST 扩展的安装备份各只留最新一份？更旧的会被删除。预设备份等其他文件不受影响。", { danger: true, confirmLabel: "清理" }))) return;
  try {
    const result = await api("/api/cleanup/install-backups/clear", { method: "POST", body: "{}" });
    notify(result.failed ? `已删除 ${result.removed} 份，${result.failed} 份删不掉（文件被占用，下次安装时会再试）` : `已删除 ${result.removed} 份旧备份，释放 ${bytesText(result.bytes)}`, result.failed > 0);
    await loadCleanup();
  } catch (error) { showError(error); }
}

async function loadCleanup() {
  cleanupData = await api("/api/cleanup");
  renderCleanup();
}

async function runCleanup() {
  $("cleanup-run").disabled = true;
  try {
    const result = await api("/api/cleanup/run", { method: "POST" });
    $("cleanup-result").textContent = cleanupResultText(result);
    await loadCleanup();
  } catch (error) { showError(error); }
  $("cleanup-run").disabled = false;
}

async function clearCoverCache() {
  if (!(await confirmDialog("清空所有已生成的翻唱缓存？歌库里的歌不受影响，之后再试唱需要重新生成。", { danger: true, confirmLabel: "清空" }))) return;
  try {
    const result = await api("/api/cleanup/covers/clear", { method: "POST" });
    notify(`已清空翻唱缓存：${result.removed} 个文件，释放 ${bytesText(result.bytes)}`);
    await loadCleanup();
  } catch (error) { showError(error); }
}

// ---------- Song covers ----------

function fillSelect(id, names, selected) {
  const options = names.includes(selected) || !selected ? names : [selected, ...names];
  $(id).innerHTML = options.map((name) => `<option value="${escapeHtml(name)}" ${name === selected ? "selected" : ""}>${escapeHtml(name)}${names.includes(name) ? "" : "（文件不存在）"}</option>`).join("");
}

/** Model dropdown: items are {id, label, available}; missing checkpoints stay visible but disabled. */
function fillModelSelect(id, items, selected, noneLabel) {
  const options = noneLabel ? [{ id: "", label: noneLabel, available: true }, ...items] : [...items];
  if (selected && !options.some((item) => item.id === selected)) options.push({ id: selected, label: selected, available: false });
  $(id).innerHTML = options.map((item) => `<option value="${escapeHtml(item.id)}" ${item.id === selected ? "selected" : ""} ${item.available ? "" : "disabled"}>${escapeHtml(item.label)}${item.available ? "" : "（未找到）"}</option>`).join("");
}

function renderCoverService() {
  const service = state.cover.service;
  $("cover-state").textContent = serviceStateText(service.state);
  $("cover-state").className = `status-badge ${service.state === "running" || service.state === "external" ? "status-success" : service.state === "failed" ? "status-error" : ""}`;
  $("cover-state-note").textContent = [service.message, service.busy ? `正在处理（排队 ${service.waiting} 首）` : ""].filter(Boolean).join(" · ");
  const buttons = serviceButtons(service.state, state.singing);
  $("cover-start").disabled = buttons.startDisabled;
  $("cover-stop").disabled = buttons.stopDisabled;
}

/** Rows of the pitch-preset editor as typed (keywords still one text). */
function readPresetRows() {
  return [...$("pitch-presets-list").querySelectorAll("[data-preset-row]")].map((row) => {
    const value = (field) => row.querySelector(`[data-field="${field}"]`).value;
    return { name: value("name"), vocal: value("vocal"), inst: value("inst"), keywords: value("keywords") };
  });
}

function renderPitchPresets() {
  const rows = state.presetRows || [];
  $("pitch-presets-list").innerHTML = rows.length ? rows.map((row, index) => `<div class="song-row list-row pitch-preset-row" data-preset-row="${index}">
    <input class="input" data-field="name" value="${escapeHtml(row.name)}" maxlength="20" placeholder="预设名" aria-label="预设名">
    <label>人声 <input class="input console-inline-input" data-field="vocal" type="number" min="-12" max="12" step="1" value="${escapeHtml(row.vocal)}"></label>
    <label>整体 <input class="input console-inline-input" data-field="inst" type="number" min="-12" max="12" step="1" value="${escapeHtml(row.inst)}"></label>
    <input class="input" data-field="keywords" value="${escapeHtml(row.keywords)}" placeholder="关键词，用逗号或顿号分隔" aria-label="关键词">
    ${rowMenu(`<button class="btn btn-secondary btn-sm" type="button" data-preset-up="${index}">上移</button><button class="btn btn-secondary btn-sm" type="button" data-preset-down="${index}">下移</button><button class="btn btn-danger-ghost btn-sm" type="button" data-preset-delete="${index}">删除</button>`)}
  </div>`).join("") : '<p class="placeholder">没有预设：点歌时只用全局升降调。</p>';
  const list = $("pitch-presets-list");
  const apply = (change) => { state.presetRows = change(readPresetRows()); renderPitchPresets(); refreshDirtyUi(); };
  list.querySelectorAll("[data-preset-up]").forEach((button) => button.addEventListener("click", () => apply((rows) => movePreset(rows, Number(button.dataset.presetUp), -1))));
  list.querySelectorAll("[data-preset-down]").forEach((button) => button.addEventListener("click", () => apply((rows) => movePreset(rows, Number(button.dataset.presetDown), 1))));
  list.querySelectorAll("[data-preset-delete]").forEach((button) => button.addEventListener("click", () => apply((rows) => rows.filter((_, index) => index !== Number(button.dataset.presetDelete)))));
}

function addPitchPreset() {
  const rows = readPresetRows();
  if (rows.length >= PITCH_PRESET_LIMIT) { notify(`最多 ${PITCH_PRESET_LIMIT} 个预设`, true); return; }
  state.presetRows = [...rows, { name: "", vocal: 0, inst: 0, keywords: "" }];
  renderPitchPresets();
  refreshDirtyUi();
}

/** The parts of the cover page that are not forms: install hint and the online-search source list. */
function renderCoverMisc() {
  $("pedalboard-install").textContent = pedalboardInstallCommand(state.cover?.settings?.rvc_root);
  const sourceSelect = $("online-source"), chosen = sourceSelect.value || "auto";
  sourceSelect.innerHTML = [["auto", "自动（按顺序全部搜）"], ...Object.entries(MUSIC_SOURCES)]
    .map(([code, label]) => `<option value="${escapeHtml(code)}">${escapeHtml(label)}</option>`).join("");
  sourceSelect.value = chosen;
}

function songRow(song) {
  const id = escapeHtml(song.id);
  const who = song.artist ? `${escapeHtml(songName(song))}${song.source ? ` · ${escapeHtml(sourceLabel(song.source))}` : ""} · ` : "";
  return `<div class="song-row list-row" data-song="${id}" data-search="${escapeHtml(`${song.title} ${song.artist || ""}`)}"><div class="row-main"><input class="input" data-song-title="${id}" value="${escapeHtml(song.title)}" maxlength="80" aria-label="标题"><small>${who}${escapeHtml(formatClock(song.duration))} · ${escapeHtml(formatBytes(song.size))}</small></div><label class="row-start">起点 <input class="input console-inline-input" data-song-start="${id}" value="${song.clip_start === null ? "" : escapeHtml(formatClock(song.clip_start))}" placeholder="默认" aria-label="起点 mm:ss"></label><div class="row-actions">${playButton(`/api/cover/songs/${encodeURIComponent(song.id)}/audio`)}${rowMenu(`<button type="button" class="btn btn-danger-ghost btn-sm" data-song-delete="${id}">删除</button>`)}</div></div>`;
}

function renderSongs() {
  const songs = state.cover.songs;
  $("song-list").innerHTML = songs.length ? songs.map(songRow).join("") : emptyStateMarkup("songs");
  const list = $("song-list");
  refreshList("songs");
  list.querySelectorAll("[data-song-delete]").forEach((button) => button.addEventListener("click", async () => {
    if (!(await confirmDialog("从歌库删除这首歌？（已生成的翻唱缓存会保留）", { danger: true }))) return;
    try { await api(`/api/cover/songs/${encodeURIComponent(button.dataset.songDelete)}`, { method: "DELETE" }); await loadCover(); } catch (error) { showError(error); }
  }));
}

function renderSingSelect() {
  const songs = state.cover.songs;
  const picked = $("sing-song").value;
  $("sing-song").innerHTML = songs.length ? songs.map((song) => `<option value="${escapeHtml(song.id)}" ${song.id === picked ? "selected" : ""}>${escapeHtml(songName(song))}（${escapeHtml(formatClock(song.duration))}）</option>`).join("") : '<option value="">（歌库是空的）</option>';
}

function coverRow(cover) {
  const key = escapeHtml(cover.key);
  return `<div class="cover-row list-row" data-search="${escapeHtml(cover.title)}"><div class="row-main"><strong>${escapeHtml(cover.title)}</strong><small>${escapeHtml(formatClock(cover.duration))} · ${escapeHtml(formatBytes(cover.size))} · ${escapeHtml(formatDateTime(cover.created))}</small><small>${escapeHtml(cover.summary)}</small></div><div class="row-actions">${playButton(`/api/cover/covers/${encodeURIComponent(cover.key)}/audio`)}${rowMenu(`<button type="button" class="btn btn-danger-ghost btn-sm" data-cover-delete="${key}">删除</button>`)}</div></div>`;
}

function renderCovers() {
  const covers = state.cover.covers;
  $("cover-cache").innerHTML = covers.length ? covers.map(coverRow).join("") : emptyStateMarkup("covers");
  $("cover-cache").querySelectorAll("[data-cover-delete]").forEach((button) => button.addEventListener("click", async () => {
    if (!(await confirmDialog("删除这条翻唱缓存？", { danger: true }))) return;
    try { await api(`/api/cover/covers/${encodeURIComponent(button.dataset.coverDelete)}`, { method: "DELETE" }); await loadCover(); } catch (error) { showError(error); }
  }));
  refreshList("covers");
}

function renderSongRequest() {
  const settings = state.cover.song_request;
  $("songreq-enabled").checked = settings.enabled;
  $("songreq-owner-min").value = settings.owner_min_affinity; $("songreq-group-min").value = settings.group_min_affinity;
  $("songreq-refuse").value = settings.refuse_percent; $("songreq-timeout").value = settings.list_timeout_seconds;
  $("songreq-per-source").value = settings.per_source;
  $("songreq-delivery").value = settings.delivery_mode;
  $("songreq-daily").value = settings.daily_limit; $("songreq-limit-owner").checked = settings.limit_owner;
  $("songreq-permission").value = settings.permission_variable;
}

async function loadCover() {
  state.cover = await api("/api/cover");
  renderCoverService();
  fillPart(P.coverService); fillPart(P.coverParams); fillPart(P.coverPresets); fillPart(P.coverMusic);
  renderCoverMisc();
  fillPart(P.coverSongs, "keep");
  renderSingSelect();
  renderCovers();
  fillPart(P.songRequest);
}

function onlineRow(item, index) {
  return `<div class="song-row list-row"><div class="row-main"><strong>${escapeHtml(item.name)}</strong><small>${escapeHtml(item.artist || "未知歌手")} · ${escapeHtml(item.album || "-")} · ${escapeHtml(sourceLabel(item.source))}</small></div><div class="row-actions"><button class="btn btn-primary btn-sm" data-online-download="${index}">下载到歌库</button></div></div>`;
}

function onlineFailure(error) {
  return coverErrorText(error.payload?.error || error.message);
}

async function searchOnline() {
  const name = $("online-name").value;
  const problem = validateOnlineName(name);
  if (problem) { $("online-status").textContent = problem; return; }
  $("online-status").textContent = "搜索中…";
  $("online-results").innerHTML = "";
  try {
    const data = await api("/api/cover/online/search", { method: "POST", body: JSON.stringify({ name: name.trim(), source: $("online-source").value }) });
    const lines = searchErrorLines(data.errors);
    $("online-status").textContent = [data.results.length ? `找到 ${data.results.length} 条` : "没有结果", ...lines].join(" · ");
    $("online-results").innerHTML = data.results.map(onlineRow).join("");
    $("online-results").querySelectorAll("[data-online-download]").forEach((button) => button.addEventListener("click", async () => {
      const item = data.results[Number(button.dataset.onlineDownload)];
      button.disabled = true;
      $("online-status").textContent = `正在下载《${item.name}》…`;
      try {
        await api("/api/cover/online/download", { method: "POST", body: JSON.stringify({ source: item.source, id: item.id, name: item.name, artist: item.artist }) });
        $("online-status").textContent = `已加入歌库：${item.name}`;
        await loadCover();
      } catch (error) { $("online-status").textContent = `下载失败：${onlineFailure(error)}`; button.disabled = false; }
    }));
  } catch (error) { $("online-status").textContent = `搜索失败：${onlineFailure(error)}`; }
}

async function fetchOnline() {
  const name = $("online-name").value;
  const problem = validateOnlineName(name);
  if (problem) { $("online-status").textContent = problem; return; }
  $("online-status").textContent = "正在按来源顺序查找并下载…";
  try {
    const data = await api("/api/cover/online/fetch", { method: "POST", body: JSON.stringify({ name: name.trim() }) });
    $("online-status").textContent = `${data.source === "library" ? "歌库里已有" : `已从${sourceLabel(data.source)}下载`}：${songName(data.song)}`;
    await loadCover();
  } catch (error) { $("online-status").textContent = `获取失败：${onlineFailure(error)}`; }
}

async function runCoverService(action) {
  try { await api(`/api/cover/service/${action}`, { method: "POST" }); } catch (error) { showError(error); }
  await loadCover().catch(() => {});
}

async function uploadSongs(files) {
  if (!files.length) return;
  const form = new FormData();
  for (const file of files) form.append("file", file);
  try {
    const data = await api("/api/cover/songs/upload", { method: "POST", body: form });
    const rejected = data.rejected.map((item) => `${item.filename}（${item.error}）`);
    notify(`已上传 ${data.saved.length} 首${rejected.length ? `；未收：${rejected.join("、")}` : ""}`, rejected.length > 0);
  } catch (error) { showError(error); }
  await loadCover().catch(() => {});
}

async function singTest() {
  const songId = $("sing-song").value;
  if (!songId || state.singing) return;
  const pitchText = $("sing-pitch").value.trim();
  const body = { song_id: songId };
  if (pitchText !== "") body.f0_up_key = Number(pitchText);
  const instText = $("sing-inst-pitch").value.trim();
  if (instText !== "") body.inst_pitch = Number(instText);
  state.singing = true;
  $("sing-button").disabled = true; $("sing-button").textContent = "处理中…";
  $("sing-status").textContent = "处理中…（第一次需要加载模型，可能要几分钟）"; $("sing-result").innerHTML = "";
  renderCoverService();
  try {
    const result = await api("/api/cover/test", { method: "POST", body: JSON.stringify(body) });
    $("sing-status").textContent = timingText(result);
    $("sing-result").innerHTML = `<div class="list-row"><div class="row-main"><strong>试唱结果</strong><small>${escapeHtml(result.cover.summary)}</small></div><div class="row-actions">${playButton(`/api/cover/covers/${encodeURIComponent(result.cover.key)}/audio`)}</div></div>`;
    const resultButton = $("sing-result").querySelector("[data-play-src]");
    if (resultButton) startPlayback(resultButton);
  } catch (error) {
    $("sing-status").textContent = `试唱失败：${coverErrorText(error.payload?.error || error.message)}`;
  }
  state.singing = false;
  $("sing-button").disabled = false; $("sing-button").textContent = "试唱";
  await loadCover().catch(() => {});
}

async function loadAll(selectCurrent = true, emotionMode = "skip") {
  const [config, models] = await Promise.all([api("/api/config"), api("/api/models")]);
  const characters = await api("/api/characters");
  state.config = config; state.models = models.models || []; state.characters = characters.characters || [];
  if (selectCurrent && !state.selectedCharacter) state.selectedCharacter = config.default_character || state.characters[0]?.name || "";
  fillPart(P.gptRoot);
  try { state.wakePrefix = (await api("/api/status")).wake_prefix ?? null; } catch { state.wakePrefix = null; }
  applyNickname();
  renderModels(); renderCharacterOptions(); renderCharacters(); refreshEmotions(emotionMode);
  loadQueue().catch(console.error);
  // Awaited: a save reloads the page and the mood part must be refilled before the forced-fill window closes.
  await loadMood().catch((error) => { $("mood-global").textContent = "不可用"; console.error(error); });
}

async function submitModel(event) {
  event.preventDefault();
  try { await api("/api/models", { method: "POST", body: JSON.stringify({ name: $("model-name").value, gpt_model_path: $("model-gpt").value, sovits_model_path: $("model-sovits").value, reference_audio_root: $("model-audio-root").value }) }); $("model-form-card").hidden = true; await loadAll(); } catch (error) { showError(error); }
}

async function submitCharacter(event) {
  event.preventDefault();
  const current = state.characters.find((item) => item.name === $("character-name").value);
  try { await api("/api/characters", { method: "POST", body: JSON.stringify({ name: $("character-name").value, model_name: $("character-model").value, default_emotion: $("character-default-emotion").value, nickname: $("character-nickname").value, st_name: $("character-st-name").value, emotions: current?.emotions || {} }) }); $("character-form-card").hidden = true; state.selectedCharacter = $("character-name").value; await loadAll(); } catch (error) { showError(error); }
}

async function uploadAudioTo(emotion, file) {
  const character = state.selectedCharacter;
  if (!file || !character) { notify("请选择角色和音频文件。", true); return; }
  if (!emotion) { notify("请输入新情绪名。", true); return; }
  const form = new FormData(); form.append("character", character); form.append("emotion", emotion); form.append("prompt_text", $("audio-prompt").value); form.append("file", file);
  try {
    await api("/api/audio/upload", { method: "POST", body: form });
    $("audio-file").value = ""; $("audio-prompt").value = "";
    state.openEmotions.add(emotion);
    await loadDiscovered();
  } catch (error) { showError(error); await loadDiscovered().catch(() => {}); }
}

async function uploadNewEmotionAudio() {
  await uploadAudioTo($("audio-emotion").value.trim(), $("audio-file").files[0]);
  $("audio-file").value = "";
}

// ---------- Voice engine (tts) ----------

function fillSectionForm(form, values) {
  for (const element of form.elements) {
    if (!element.name || !(element.name in values)) continue;
    if (element.type === "checkbox") element.checked = Boolean(values[element.name]);
    else element.value = values[element.name] ?? "";
  }
}

/** The split interval and count only matter while 分条发 is on: grey them out otherwise. */
function syncSplitFields() {
  const form = $("tts-common-form");
  if (!form?.elements.split_messages) return;
  const on = form.elements.split_messages.checked;
  for (const name of ["split_interval", "split_max"]) form.elements[name].disabled = !on;
}

function renderSwitch() {
  document.querySelectorAll("[data-engine]").forEach((button) => {
    const active = button.dataset.engine === state.engine;
    button.classList.toggle("btn-primary", active);
    button.classList.toggle("btn-secondary", !active);
    button.setAttribute("aria-pressed", String(active));
    button.disabled = state.switching;
  });
}

function renderEngineUi() {
  const engine = currentEngine();
  renderSwitch();
  arrangeVoiceEngines();
  if (state.tts) {
    fillPart(P.ttsCommon); fillPart(P.irodori); fillPart(P.gptService);
  }
  if (state.characters.length || state.selectedCharacter) refreshEmotions();
}

async function loadTts() {
  const data = await api("/api/tts");
  state.tts = data.settings;
  state.engine = data.engine;
  renderEngineUi();
}

/** GET the current settings, change part of them, POST the whole section back. */
async function saveTts(patch) {
  const current = await api("/api/tts");
  const next = applyTtsPatch(current.settings, patch);
  const saved = await api("/api/tts/settings", { method: "POST", body: JSON.stringify(next) });
  state.tts = saved.settings;
  state.engine = saved.engine;
  renderEngineUi();
  return stoppedNotice(saved.stopped);
}

async function switchEngine(engine) {
  if (state.switching || engine === state.engine) return;
  if (!(await guardLeave())) return;
  state.switching = true;
  renderSwitch();
  try {
    const notice = await saveTts({ engine });
    setServiceNote("");
    notify([`已切换到 ${ENGINE_LABELS[engine]}`, notice].filter(Boolean).join("；"));
    await refreshService();
  } catch (error) {
    notify(error.message, true);
  } finally {
    state.switching = false;
    renderSwitch();
  }
}

function setServiceNote(text, error = false) {
  state.serviceNote = { text, error };
}

function renderService(data) {
  state.service = data;
  const status = data.state || "stopped";
  const badge = $("service-state");
  badge.textContent = serviceStateText(status);
  badge.className = `status-badge ${status === "running" || status === "external" ? "status-success" : status === "failed" ? "status-error" : ""}`;
  $("service-name").textContent = data.service_name || "-";
  $("service-pid").textContent = data.pid == null ? "-" : data.pid;
  $("service-address").textContent = data.host ? `${data.host}:${data.port}` : "-";
  $("service-dir").textContent = data.launch_dir || "-";
  const log = $("service-log");
  const lines = data.log_tail || [];
  const atBottom = log.scrollTop + log.clientHeight >= log.scrollHeight - 8;
  log.textContent = lines.length ? lines.join("\n") : "（暂无日志）";
  if (atBottom) log.scrollTop = log.scrollHeight;
  const buttons = serviceButtons(status, state.serviceBusy);
  $("service-start").disabled = buttons.startDisabled;
  $("service-stop").disabled = buttons.stopDisabled;
  const note = state.serviceNote;
  const message = $("service-message");
  if (note.text) { message.textContent = note.text; message.classList.toggle("console-note-error", note.error); }
  else if (status === "external") { message.textContent = "外部启动的服务，插件不管理（不能从这里关闭）"; message.classList.remove("console-note-error"); }
  else if (status === "failed") { message.textContent = data.message || "启动失败"; message.classList.add("console-note-error"); }
  else if (status === "stopped" && !data.launch_dir) { message.textContent = data.message || "未设置目录"; message.classList.remove("console-note-error"); }
  else { message.textContent = ""; message.classList.remove("console-note-error"); }
}

async function refreshService() {
  try {
    renderService(await api("/api/tts/service"));
  } catch (error) {
    $("service-state").textContent = "不可用";
    $("service-state").className = "status-badge status-error";
    console.error(error);
  }
}

async function runServiceAction(path, doing) {
  state.serviceBusy = true;
  setServiceNote(doing);
  renderService(state.service || { state: "stopped" });
  try {
    const data = await api(path, { method: "POST" });
    setServiceNote("");
    renderService(data);
  } catch (error) {
    // 502 (start failed) and 409 (nothing of ours to stop) still carry the current service state.
    setServiceNote(error.message, true);
    if (error.payload?.service) renderService(error.payload.service);
  } finally {
    state.serviceBusy = false;
    await refreshService();
  }
}

async function createEmotion() {
  const character = state.characters.find((item) => item.name === state.selectedCharacter); if (!character) return;
  const name = await promptDialog("要手动覆盖的情绪名称（通常与目录中的情绪文件夹同名）", state.discovered[0]?.name || "neutral"); if (!name || !name.trim()) return;
  try { await api(`/api/characters/${encodeURIComponent(character.name)}/emotions/${encodeURIComponent(name.trim())}`, { method: "PUT", body: JSON.stringify({}) });
    state.openEmotions.add(name.trim());
    await loadAll(false, "keep");
    const card = [...$("emotions-list").querySelectorAll("[data-emotion-card]")].find((item) => item.dataset.emotionCard === name.trim());
    if (card) reveal(card);
  } catch (error) { showError(error); }
}

// ---------- Saveable parts, in page order ----------

P.network = definePart({
  id: "network", page: "system", tab: "general", title: "端口",
  collect: fieldsById({ st_port: "network-st-port", bridge_port: "network-bridge-port", console_port: "network-console-port", connector_port: "network-connector-port", onebot_port: "network-onebot-port" }),
  fill() {
    const settings = state.network?.settings;
    if (!settings) return;
    $("network-st-port").value = settings.st_port;
    $("network-bridge-port").value = settings.bridge_port;
    $("network-console-port").value = settings.console_port;
    $("network-connector-port").value = settings.connector_port;
    $("network-onebot-port").value = settings.onebot_port ?? 11455;
  },
  async save(values) {
    state.network = await api("/api/network", { method: "POST", body: JSON.stringify(networkPayload(values)) });
    return networkSavedNotice(state.network);
  }
});

P.owner = definePart({
  id: "owner", page: "connect", title: "主人",
  collect: fieldsById({ platform_id: "owner-platform-id", sender_id: "owner-sender-id" }),
  fill() {
    const owner = state.owner?.owner;
    if (!owner) return;
    $("owner-platform-id").value = owner.platform_id;
    $("owner-sender-id").value = owner.sender_id;
  },
  validate(values) {
    const problem = ownerProblem(values);
    return problem ? [{ key: "sender_id", text: problem }] : [];
  },
  async save(values) {
    state.owner = await api("/api/owner", { method: "POST", body: JSON.stringify(ownerPayload(values)) });
    return "主人已保存，立即生效";
  }
});

P.chatPlatform = definePart({
  id: "chat-platform", page: "connect", title: "聊天平台",
  collect: fieldsById({ active: "chatplat-active", napcat_owner: "chatplat-owner", group_whitelist: "chatplat-groups" }),
  fill() {
    const settings = state.chatPlatform?.settings;
    if (!settings) return;
    $("chatplat-active").value = settings.active;
    $("chatplat-owner").value = settings.napcat_owner;
    $("chatplat-groups").value = settings.group_whitelist.join("\n");
  },
  validate: (values) => chatPlatformProblems(values),
  async save(values) {
    state.chatPlatform = { ...state.chatPlatform, ...(await api("/api/chat-platform", { method: "POST", body: JSON.stringify(chatPlatformPayload(values)) })) };
    const wake = await api("/api/chat-platform");
    state.chatPlatform.wake_prefix = wake.wake_prefix;
    state.wakePrefix = wake.wake_prefix ?? state.wakePrefix;
    showChatPlatformInfo();
    return state.chatPlatform.settings.active === "napcat" ? "已切换到 NapCat，立即生效" : "已使用 AstrBot，立即生效";
  }
});

P.texts = definePart({
  id: "texts", page: "system", tab: "texts", title: "回复文案",
  collect() {
    const fields = {};
    $("texts-list").querySelectorAll("[data-text-key]").forEach((field) => { fields[field.dataset.textKey] = field; });
    return fields;
  },
  anchor: () => $("texts-list"),
  fill: renderTexts,
  validate(values) {
    const problems = [];
    for (const item of state.texts?.meta || []) {
      const bad = unknownPlaceholders(values[item.key], item.placeholders);
      if (bad.length) problems.push({ key: item.key, text: `“${item.label}”里有未知的占位符 ${bad[0]}（${placeholderText(item)}）` });
    }
    return problems;
  },
  async save(values) {
    await api("/api/texts", { method: "POST", body: JSON.stringify(textsPayload(values, state.texts.meta)) });
    return "回复文案已保存";
  }
});

const afterTtsSave = async (notice, text) => {
  await refreshService();
  return [text, notice].filter(Boolean).join("，");
};

P.queue = definePart({
  id: "queue", page: "connect", title: "消息排队",
  collect: fieldsById({ max_waiting: "queue-max-waiting", max_wait_seconds: "queue-max-wait", reply_timeout_seconds: "queue-reply-timeout" }),
  fill() {
    const settings = state.queue?.settings;
    if (!settings) return;
    $("queue-max-waiting").value = settings.max_waiting;
    $("queue-max-wait").value = settings.max_wait_seconds;
    $("queue-reply-timeout").value = settings.reply_timeout_seconds;
  },
  async save(values) {
    await api("/api/queue/settings", { method: "POST", body: JSON.stringify({ max_waiting: Number(values.max_waiting), max_wait_seconds: Number(values.max_wait_seconds), reply_timeout_seconds: Number(values.reply_timeout_seconds) }) });
    return "消息排队设置已保存";
  }
});

P.mood = definePart({
  id: "mood", page: "emotions", title: "心情",
  collect: fieldsById({ decay_minutes: "mood-decay", recent: "mood-recent" }),
  fill() {
    const settings = state.mood?.settings;
    if (!settings) return;
    $("mood-decay").value = settings.decay_minutes;
    $("mood-recent").value = settings.recent;
  },
  async save(values) {
    await api("/api/mood/settings", { method: "POST", body: JSON.stringify({ decay_minutes: Number(values.decay_minutes), recent: Number(values.recent) }) });
    return "心情设置已保存";
  }
});

P.ttsCommon = definePart({
  id: "tts-common", page: "voice", title: "通用语音设置",
  collect: namedFields("tts-common-form"),
  fill() { if (state.tts) { fillSectionForm($("tts-common-form"), state.tts.common); syncSplitFields(); } },
  async save(values) { return afterTtsSave(await saveTts({ common: coerceSection("common", values) }), "通用语音设置已保存"); }
});

P.irodori = definePart({
  id: "irodori", page: "voice", title: "Irodori 设置",
  collect: namedFields("irodori-form"),
  fill() { if (state.tts) fillSectionForm($("irodori-form"), state.tts.irodori); },
  async save(values) { return afterTtsSave(await saveTts({ irodori: coerceSection("irodori", values) }), "Irodori 设置已保存"); }
});

P.gptService = definePart({
  id: "gpt-service", page: "voice", title: "GPT-SoVITS 服务设置",
  collect: namedFields("gpt-service-form"),
  fill() { if (state.tts) fillSectionForm($("gpt-service-form"), state.tts.gpt_sovits); },
  async save(values) { return afterTtsSave(await saveTts({ gpt_sovits: coerceSection("gpt_sovits", values) }), "GPT-SoVITS 服务设置已保存"); }
});

P.gptRoot = definePart({
  id: "gpt-root", page: "voice", title: "GPT-SoVITS 根目录",
  collect: fieldsById({ path: "gpt-sovits-root" }),
  fill() { $("gpt-sovits-root").value = state.config?.gpt_sovits_root || ""; },
  async save(values) {
    await api("/api/config/gpt_sovits_root", { method: "POST", body: JSON.stringify({ path: values.path }) });
    return "根目录已保存";
  }
});

P.modelCard = definePart({
  id: "model-card", page: "voice", title: "模型", inline: true,
  collect: fieldsById({ name: "model-name", gpt: "model-gpt", sovits: "model-sovits", audio_root: "model-audio-root" }),
  isOpen: () => !$("model-form-card").hidden,
  close() { $("model-form-card").hidden = true; }
});

P.characterCard = definePart({
  id: "character-card", page: "emotions", title: "角色", inline: true,
  collect: fieldsById({ name: "character-name", nickname: "character-nickname", st_name: "character-st-name", model: "character-model", default_emotion: "character-default-emotion" }),
  isOpen: () => !$("character-form-card").hidden,
  close() { $("character-form-card").hidden = true; }
});

P.emotions = definePart({
  id: "emotions", page: "emotions", title: "角色和情绪",
  collect() {
    const fields = {};
    $("emotions-list").querySelectorAll("[data-emotion-form]").forEach((form) => {
      for (const element of form.elements) if (element.name) fields[`${form.dataset.emotionForm}:${element.name}`] = element;
    });
    return fields;
  },
  anchor: () => $("emotions-list"),
  fill: renderEmotionCards,
  async save(values, changed) {
    const character = state.characters.find((item) => item.name === state.selectedCharacter);
    if (!character) throw new Error("没有选中的角色");
    const engine = currentEngine();
    const names = [...new Set(changed.map((key) => key.slice(0, key.lastIndexOf(":"))))];
    for (const name of names) {
      const form = [...$("emotions-list").querySelectorAll("[data-emotion-form]")].find((item) => item.dataset.emotionForm === name);
      if (!form) continue;
      const raw = Object.fromEntries(new FormData(form).entries());
      if (form.elements.split_sentence) raw.split_sentence = form.elements.split_sentence.checked;
      // Fields of the other engine are carried over from the stored profile unchanged.
      const data = buildEmotionPayload(character.emotions?.[name], engine, raw);
      await api(`/api/characters/${encodeURIComponent(character.name)}/emotions/${encodeURIComponent(name)}`, { method: "PUT", body: JSON.stringify(data) });
    }
    return `情绪覆盖已保存（${names.length} 个）`;
  }
});

P.stickerSettings = definePart({
  id: "sticker-settings", page: "stickers", title: "表情包发送设置",
  collect: fieldsById({ enabled: "sticker-enabled", prob: "sticker-prob", cooldown: "sticker-cooldown" }),
  fill() {
    const settings = state.stickers?.settings;
    if (!settings) return;
    $("sticker-enabled").checked = settings.enabled;
    $("sticker-prob").value = settings.prob;
    $("sticker-cooldown").value = settings.cooldown;
  },
  async save(values) {
    await api("/api/stickers/settings", { method: "POST", body: JSON.stringify(stickerSettingsPayload(values)) });
    return "表情包设置已保存";
  }
});

P.scheduleSettings = definePart({
  id: "schedule-settings", page: "schedule", title: "定时总设置",
  collect: fieldsById({ enabled: "schedule-enabled", quiet: "schedule-quiet", defer: "schedule-defer", gap: "schedule-gap", dedupe: "schedule-dedupe" }),
  fill() {
    const settings = state.schedule?.settings;
    if (!settings) return;
    $("schedule-enabled").checked = settings.enabled;
    $("schedule-quiet").value = settings.quiet_minutes;
    $("schedule-defer").value = settings.max_defer_minutes;
    $("schedule-gap").value = settings.min_gap_minutes;
    $("schedule-dedupe").value = settings.dedupe_ratio;
  },
  async save(values) {
    await api("/api/schedule/settings", { method: "POST", body: JSON.stringify({ enabled: Boolean(values.enabled), quiet_minutes: Number(values.quiet), max_defer_minutes: Number(values.defer), min_gap_minutes: Number(values.gap), dedupe_ratio: Number(values.dedupe) }) });
    return "定时设置已保存";
  }
});

P.proactiveChat = definePart({
  id: "proactive-chat", page: "schedule", title: "主动搭话",
  collect() {
    const fields = fieldsById({
      enabled: "chat-enabled", silence_min: "chat-silence-min", silence_max: "chat-silence-max", factor: "chat-factor",
      max_unanswered: "chat-max-unanswered", daily_limit: "chat-daily-limit", quiet_start: "chat-quiet-start",
      quiet_end: "chat-quiet-end", prompt: "chat-prompt"
    })();
    for (const name of ["weekday", "weekend"]) {
      $(`chat-windows-${name}`).querySelectorAll("[data-win]").forEach((input) => { fields[`win:${input.dataset.win}`] = input; });
    }
    return fields;
  },
  fill() {
    const settings = state.schedule?.chat?.settings;
    if (!settings) return;
    $("chat-enabled").checked = settings.enabled;
    $("chat-silence-min").value = settings.silence_min;
    $("chat-silence-max").value = settings.silence_max;
    $("chat-factor").value = settings.followup_factor;
    $("chat-max-unanswered").value = settings.max_unanswered;
    $("chat-daily-limit").value = settings.daily_limit;
    $("chat-quiet-start").value = settings.quiet_hours.start;
    $("chat-quiet-end").value = settings.quiet_hours.end;
    $("chat-prompt").value = settings.prompt;
    renderChatWindows("weekday", settings.active_windows.weekday);
    renderChatWindows("weekend", settings.active_windows.weekend);
  },
  validate(values) {
    return validateChatForm(values, readChatWindows());
  },
  async save(values) {
    await api("/api/schedule/chat", { method: "POST", body: JSON.stringify(chatPayload(values, readChatWindows())) });
    return "主动搭话设置已保存";
  }
});

P.scheduleAliases = definePart({
  id: "schedule-aliases", page: "schedule", title: "群别名",
  collect() {
    const fields = {};
    $("schedule-targets").querySelectorAll("[data-alias-input]").forEach((input) => { fields[`alias:${input.dataset.aliasInput}`] = input; });
    return fields;
  },
  anchor: () => $("schedule-targets"),
  fill: renderScheduleTargets,
  async save(values, changed) {
    for (const key of changed.filter((name) => name.startsWith("alias:"))) {
      const group = key.slice("alias:".length);
      await api(`/api/schedule/targets/${encodeURIComponent(group)}`, { method: "POST", body: JSON.stringify({ alias: values[key] }) });
    }
    return "别名已保存";
  }
});

P.schedulePlans = definePart({
  id: "schedule-plans", page: "schedule", title: "计划启用状态",
  collect() {
    const fields = {};
    $("plan-list").querySelectorAll("[data-plan-toggle]").forEach((box) => { fields[`plan:${box.dataset.planToggle}`] = box; });
    return fields;
  },
  anchor: () => $("plan-list"),
  fill: renderPlans,
  async save(values, changed) {
    for (const key of changed) {
      const plan = (state.schedule?.plans || []).find((item) => `plan:${item.id}` === key);
      if (!plan) continue;
      await api("/api/schedule/plans", { method: "POST", body: JSON.stringify(planPayload(plan, { enabled: Boolean(values[key]) })) });
    }
    return "计划启用状态已保存";
  }
});

P.planCard = definePart({
  id: "plan-card", page: "schedule", title: "计划", inline: true,
  collect() {
    const fields = fieldsById({
      name: "plan-name", enabled: "plan-enabled", kind: "plan-kind", time: "plan-time", jitter: "plan-jitter",
      start: "plan-start", end: "plan-end", every: "plan-every", target: "plan-target", prompt: "plan-prompt", fallbacks: "plan-fallbacks"
    })();
    $("plan-weekdays").querySelectorAll("[data-weekday]").forEach((box) => { fields[`weekday:${box.dataset.weekday}`] = box; });
    return fields;
  },
  isOpen: () => !$("plan-form-card").hidden,
  close() { $("plan-form-card").hidden = true; }
});

P.coverService = definePart({
  id: "cover-service", page: "sing", tab: "service", title: "RVC 服务",
  collect: fieldsById({ enabled: "cover-enabled", rvc_root: "cover-root", port: "cover-port" }),
  coverPatch: (values) => values,
  fill() {
    const settings = state.cover.settings;
    $("cover-enabled").checked = settings.enabled; $("cover-root").value = settings.rvc_root; $("cover-port").value = settings.port;
  }
});

const COVER_PARAM_FIELDS = {
  model: "cover-model", index_path: "cover-index", f0_up_key: "cover-pitch", inst_pitch: "cover-inst-pitch", f0_method: "cover-f0",
  index_rate: "cover-index-rate", rms_mix_rate: "cover-rms", protect: "cover-protect", sep_model: "cover-sep",
  dereverb_model: "cover-dereverb", vocal_reverb: "cover-vocal-reverb", keep_stems: "cover-keep-stems", clip_seconds: "cover-clip",
  default_start_percent: "cover-start-percent", vocal_gain_db: "cover-vocal-gain", inst_gain_db: "cover-inst-gain",
  output_sample_rate: "cover-sample-rate", idle_stop_minutes: "cover-idle", job_timeout_seconds: "cover-timeout"
};

P.coverParams = definePart({
  id: "cover-params", page: "sing", tab: "params", title: "转换参数",
  collect: fieldsById(COVER_PARAM_FIELDS),
  coverPatch: (values) => values,
  fill() {
    const { settings, models, separation_models: separation, dereverb_models: dereverb, auto_index: auto } = state.cover;
    fillSelect("cover-model", models, settings.model);
    $("cover-index").value = settings.index_path; $("cover-index-note").textContent = indexText(settings, auto);
    $("cover-pitch").value = settings.f0_up_key; $("cover-inst-pitch").value = settings.inst_pitch; $("cover-f0").value = settings.f0_method;
    $("cover-index-rate").value = settings.index_rate;
    $("cover-rms").value = settings.rms_mix_rate; $("cover-protect").value = settings.protect;
    fillModelSelect("cover-sep", separation, settings.sep_model);
    fillModelSelect("cover-dereverb", dereverb, settings.dereverb_model, "不去混响");
    $("cover-vocal-reverb").value = settings.vocal_reverb; $("cover-vocal-reverb-value").textContent = settings.vocal_reverb;
    $("cover-keep-stems").checked = settings.keep_stems;
    $("cover-clip").value = settings.clip_seconds; $("cover-start-percent").value = settings.default_start_percent;
    $("cover-vocal-gain").value = settings.vocal_gain_db; $("cover-inst-gain").value = settings.inst_gain_db;
    $("cover-sample-rate").value = String(settings.output_sample_rate);
    $("cover-idle").value = settings.idle_stop_minutes; $("cover-timeout").value = settings.job_timeout_seconds;
  }
});

P.coverPresets = definePart({
  id: "cover-presets", page: "sing", tab: "params", title: "升降调预设",
  collect() {
    const fields = {};
    $("pitch-presets-list").querySelectorAll("[data-preset-row]").forEach((row) => {
      row.querySelectorAll("[data-field]").forEach((input) => { fields[`preset:${row.dataset.presetRow}:${input.dataset.field}`] = input; });
    });
    return fields;
  },
  anchor: () => $("pitch-presets-list"),
  coverPatch: () => ({ pitch_presets: pitchPresetsPayload(readPresetRows()) }),
  validate() {
    const rows = readPresetRows();
    const empty = rows.findIndex((row) => !row.name.trim() || splitKeywords(row.keywords).length === 0);
    return empty >= 0 ? [{ key: `preset:${empty}:${rows[empty].name.trim() ? "keywords" : "name"}`, text: `第 ${empty + 1} 个预设需要名字和至少一个关键词` }] : [];
  },
  fill() {
    state.presetRows = state.cover.settings.pitch_presets.map((item) => ({ name: item.name, vocal: item.vocal, inst: item.inst, keywords: item.keywords.join("、") }));
    renderPitchPresets();
  }
});

P.coverMusic = definePart({
  id: "cover-music", page: "sing", tab: "library", title: "在线歌源设置",
  collect: fieldsById({
    music_api_key: "music-key", clear_music_api_key: "music-key-clear", sources: "music-sources",
    min_full_seconds: "music-min-seconds", download_timeout_seconds: "music-download-timeout"
  }),
  coverPatch(values) {
    const parsed = parseSources(values.sources);
    return { ...values, sources: parsed.sources || [] };
  },
  validate(values) {
    const parsed = parseSources(values.sources);
    return parsed.error ? [{ key: "sources", text: parsed.error }] : [];
  },
  fill() {
    const settings = state.cover.settings;
    $("music-key").value = ""; $("music-key-clear").checked = false; $("music-key-status").textContent = `密钥：${keyStatusText(settings)}`;
    $("music-sources").value = settings.sources.join(","); $("music-min-seconds").value = settings.min_full_seconds;
    $("music-download-timeout").value = settings.download_timeout_seconds;
  }
});

P.coverSongs = definePart({
  id: "cover-songs", page: "sing", tab: "library", title: "歌库",
  collect() {
    const fields = {};
    $("song-list").querySelectorAll("[data-song-title]").forEach((input) => { fields[`song:${input.dataset.songTitle}:title`] = input; });
    $("song-list").querySelectorAll("[data-song-start]").forEach((input) => { fields[`song:${input.dataset.songStart}:start`] = input; });
    return fields;
  },
  anchor: () => $("song-list"),
  validate(values) {
    return Object.keys(values).filter((key) => key.endsWith(":start") && Number.isNaN(parseClock(values[key])))
      .map((key) => ({ key, text: "起点需要是 mm:ss 或秒数" }));
  },
  fill: renderSongs,
  async save(values, changed) {
    const ids = new Set(changed.map((key) => key.split(":")[1]));
    for (const id of ids) {
      const body = {};
      if (changed.includes(`song:${id}:title`)) body.title = values[`song:${id}:title`];
      if (changed.includes(`song:${id}:start`)) body.clip_start = parseClock(values[`song:${id}:start`]);
      if (Object.keys(body).length) await api(`/api/cover/songs/${encodeURIComponent(id)}`, { method: "POST", body: JSON.stringify(body) });
    }
    return "歌库已保存";
  }
});

P.songRequest = definePart({
  id: "song-request", page: "sing", tab: "chat", title: "聊天点歌",
  collect: fieldsById({
    enabled: "songreq-enabled", owner_min_affinity: "songreq-owner-min", group_min_affinity: "songreq-group-min",
    refuse_percent: "songreq-refuse", list_timeout_seconds: "songreq-timeout", per_source: "songreq-per-source",
    delivery_mode: "songreq-delivery", daily_limit: "songreq-daily", limit_owner: "songreq-limit-owner",
    permission_variable: "songreq-permission"
  }),
  fill: renderSongRequest,
  async save(values) {
    await api("/api/cover/song-request/settings", { method: "POST", body: JSON.stringify(songRequestPayload(values)) });
    return "聊天点歌设置已保存";
  }
});

async function saveCoverSettings(patches) {
  const data = await api("/api/cover/settings", { method: "POST", body: JSON.stringify(mergeCoverPatch(state.cover.settings, patches)) });
  return data.stopped ? "已保存，旧的工作进程已停止" : "翻唱设置已保存";
}

P.affinitySettings = definePart({
  id: "affinity-settings", page: "affinity", title: "好感度设置",
  collect() {
    const fields = fieldsById({
      initial_value: "affinity-initial", max_delta: "affinity-max-delta",
      group_variable: "group-affinity-variable", group_role_tag: "group-affinity-role-tag", group_min: "group-affinity-min", group_max: "group-affinity-max", group_initial: "group-affinity-initial"
    })();
    for (const [id, tag] of [["affinity-tiers", "owner"], ["group-affinity-tiers", "group"]]) {
      $(id).querySelectorAll(".affinity-tier").forEach((row, index) => {
        for (const field of ["min", "max", "name", "description"]) fields[`tier:${tag}:${index}:${field}`] = row.querySelector(`[data-tier="${field}"]`);
      });
    }
    return fields;
  },
  anchor: () => $("affinity-tiers"),
  fill: fillAffinitySettings,
  async save() {
    const body = {
      initial_value: Number($("affinity-initial").value),
      max_delta: Number($("affinity-max-delta").value),
      tiers: readTiers("affinity-tiers"),
      group: {
        variable: $("group-affinity-variable").value,
        role_tag: $("group-affinity-role-tag").value,
        min: Number($("group-affinity-min").value),
        max: Number($("group-affinity-max").value),
        initial_value: Number($("group-affinity-initial").value),
        tiers: readTiers("group-affinity-tiers")
      }
    };
    await api("/api/affinity/settings", { method: "POST", body: JSON.stringify(body) });
    return "好感度设置已保存";
  }
});

// ---------- ST injection (R5) ----------

function injectionSelect(name, options, value) {
  return `<select data-f="${name}">${Object.entries(options).map(([key, label]) => `<option value="${key}"${key === value ? " selected" : ""}>${escapeHtml(label)}</option>`).join("")}</select>`;
}

function injectionEntryHtml(entry) {
  const id = escapeHtml(entry.id);
  const body = [
    fieldMarkup({ label: "启用", switchField: true, control: `<input data-f="enabled" type="checkbox"${entry.enabled ? " checked" : ""}>` }),
    fieldMarkup({ label: "标题", control: `<input data-f="title" required maxlength="40" value="${escapeHtml(entry.title)}">` }),
    fieldMarkup({ label: "写到", control: injectionSelect("target", INJECTION_TARGETS, entry.target), help: "预设：写进 ST 当前预设；世界书：写进全局世界书" }),
    fieldMarkup({ label: "位置（预设条目）", control: injectionSelect("placement", INJECTION_PLACEMENTS, entry.placement), help: "世界书条目总是按深度插入" }),
    fieldMarkup({ label: "深度", control: `<input data-f="depth" type="number" min="0" max="100" step="1" value="${entry.depth}">`, help: "按深度插入时用：0 = 最靠近最新消息" }),
    fieldMarkup({ label: "角色", control: injectionSelect("role", INJECTION_ROLES, entry.role) }),
    fieldMarkup({ label: "内容", wide: true, control: `<textarea data-f="content" rows="8" maxlength="4000">${escapeHtml(entry.content)}</textarea>`, help: "最多 4000 字；可以写 ST 宏和下方列出的 {{stbridge_…}} 宏" }),
    `<div class="field-wide injection-actions"><button type="button" class="btn btn-secondary btn-sm" data-entry-move="-1">上移</button><button type="button" class="btn btn-secondary btn-sm" data-entry-move="1">下移</button><button type="button" class="btn btn-danger-ghost btn-sm" data-entry-remove>删除</button></div>`,
    `<input data-f="id" type="hidden" value="${id}"><input data-f="builtin" type="hidden" value="${entry.builtin ? "1" : ""}">`
  ].join("");
  return `<details class="module injection-entry${entry.enabled ? "" : " entry-off"}" data-module="injection:${id}"><summary><span class="entry-title">${escapeHtml(entry.title)}</span><small class="entry-note">${escapeHtml(injectionEntryNote(entry))}</small>${entry.builtin ? '<span class="badge-count">内置</span>' : ""}</summary><div class="module-body">${body}</div></details>`;
}

/** The entries as they stand in the form (typed values included). */
function readInjectionEntries() {
  return [...$("injection-entries").querySelectorAll(".injection-entry")].map((card) => {
    const value = (name) => card.querySelector(`[data-f="${name}"]`);
    return {
      id: value("id").value, builtin: value("builtin").value === "1", title: value("title").value,
      enabled: value("enabled").checked, content: value("content").value, target: value("target").value,
      placement: value("placement").value, depth: value("depth").value, role: value("role").value, order: 0
    };
  });
}

function renderInjectionEntries(entries) {
  const list = $("injection-entries");
  list.innerHTML = entries.length ? entries.map(injectionEntryHtml).join("") : '<p class="placeholder">没有条目。点“恢复内置条目”找回默认的，或添加自定义条目。</p>';
  // Nine long texts: cards nobody opened before start collapsed (an opened / closed card stays as the user left it).
  const fresh = [...list.querySelectorAll("details.injection-entry")].filter((card) => collapsedCards[moduleKey(card.dataset.module)] === undefined);
  enhanceModules(list);
  fresh.forEach((card) => { card.open = false; });
  bindInjectionEntries();
}

function bindInjectionEntries() {
  const list = $("injection-entries");
  const change = (mutate) => { renderInjectionEntries(mutate(readInjectionEntries())); refreshDirtyUi(); };
  list.querySelectorAll(".injection-entry").forEach((card, index) => {
    const note = () => { const entry = readInjectionEntries()[index]; card.querySelector(".entry-note").textContent = injectionEntryNote(entry); card.classList.toggle("entry-off", !entry.enabled); };
    card.querySelector('[data-f="title"]').addEventListener("input", (event) => { card.querySelector(".entry-title").textContent = event.target.value; });
    for (const name of ["enabled", "target", "placement", "depth"]) card.querySelector(`[data-f="${name}"]`).addEventListener("change", note);
    card.querySelectorAll("[data-entry-move]").forEach((button) => button.addEventListener("click", () => change((entries) => movePreset(entries, index, Number(button.dataset.entryMove)))));
    card.querySelector("[data-entry-remove]").addEventListener("click", () => change((entries) => entries.filter((_, at) => at !== index)));
  });
}

function fillInjection() {
  const settings = state.injection.settings;
  $("injection-book-name").value = settings.world_book_name;
  renderInjectionEntries(settings.entries);
  $("injection-macros").innerHTML = state.injection.macros.map((macro) => `<div class="macro-row"><code>{{stbridge_${escapeHtml(macro.key)}}}</code><span>${escapeHtml(macro.description)}</span></div>`).join("");
}

async function loadInjection() {
  state.injection = await api("/api/st-injection");
  fillPart(P.injection);
}

P.injection = definePart({
  id: "injection", page: "injection", title: "ST 注入条目",
  collect() {
    const fields = { book: $("injection-book-name") };
    $("injection-entries").querySelectorAll(".injection-entry").forEach((card, index) => {
      for (const name of ["id", "builtin", "title", "enabled", "target", "placement", "depth", "role", "content"]) fields[`entry:${index}:${name}`] = card.querySelector(`[data-f="${name}"]`);
    });
    return fields;
  },
  anchor: () => $("injection-entries"),
  fill: fillInjection,
  async save() {
    const body = injectionPayload($("injection-book-name").value, readInjectionEntries());
    await api("/api/st-injection", { method: "POST", body: JSON.stringify(body) });
    return "ST 注入条目已保存，记得到 ST 扩展面板点「同步」";
  }
});

P.affinityUsers = definePart({
  id: "affinity-users", page: "affinity", title: "用户好感度",
  collect() {
    const fields = {};
    $("affinity-users").querySelectorAll("[data-affinity-user]").forEach((input) => { fields[`user:${input.dataset.affinityUser}`] = input; });
    return fields;
  },
  anchor: () => $("affinity-users"),
  fill: fillAffinityUsers,
  async save(values, changed) {
    for (const key of changed) {
      const id = key.slice("user:".length);
      await api(`/api/affinity/users/${encodeURIComponent(id)}`, { method: "PUT", body: JSON.stringify({ value: Number(values[key]) }) });
    }
    return `用户好感度已保存（${changed.length} 位）`;
  }
});

P.cleanupSettings = definePart({
  id: "cleanup-settings", page: "system", tab: "cleanup", title: "自动清理设置",
  collect: fieldsById({ enabled: "cleanup-enabled", keep_days: "cleanup-days" }),
  validate(values) {
    const problem = validateKeepDays(values.keep_days);
    return problem ? [{ key: "keep_days", text: problem }] : [];
  },
  fill() {
    if (!cleanupData) return;
    $("cleanup-enabled").checked = cleanupData.settings.enabled;
    $("cleanup-days").value = cleanupData.settings.keep_days;
  },
  async save(values) {
    await api("/api/cleanup/settings", { method: "POST", body: JSON.stringify(cleanupSettingsPayload(values.enabled, values.keep_days)) });
    return "清理设置已保存";
  }
});


// ---------- 安装与路径 (R4b) ----------

const INSTALL_IDS = {
  st_dir: "install-st", astrbot_dir: "install-astrbot", napcat_path: "install-napcat", gpt_sovits_dir: "install-gpt",
  irodori_dir: "install-irodori", irodori_server_dir: "install-irodori-server", rvc_dir: "install-rvc", pip_mirror: "install-mirror"
};
const delay = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
let jobTimer = null;

function renderPathStatuses(checks) {
  for (const { key } of INSTALL_PATH_FIELDS) {
    const element = $(`install-${key.replace(/_/g, "-")}-status`) || $(`install-${key === "gpt_sovits" ? "gpt" : key}-status`);
    if (!element) continue;
    const check = checks?.[key];
    element.textContent = pathStatusText(check);
    element.dataset.state = check?.state || "empty";
  }
  const napcat = checks?.napcat;
  const box = $("install-napcat-candidates");
  const candidates = napcat?.state === "choose" ? napcat.candidates || [] : [];
  box.hidden = candidates.length === 0;
  box.innerHTML = candidates.map((path) => `<button type="button" class="btn btn-secondary btn-sm" data-napcat-pick="${escapeHtml(path)}">${escapeHtml(path.split(/[\\/]/).pop())}</button>`).join("");
  box.querySelectorAll("[data-napcat-pick]").forEach((button) => button.addEventListener("click", () => {
    $("install-napcat").value = button.dataset.napcatPick;
    $("install-napcat").dispatchEvent(new Event("input", { bubbles: true }));
    checkInstallPaths().catch(showError);
  }));
}

function setNote(element, text, isError = false) {
  element.textContent = text;
  element.classList.toggle("console-note-error", Boolean(isError));
}

function updateInstallButtons() {
  const data = state.install;
  if (!data) return;
  const dirty = isDirty(P.installPaths);
  for (const [kind, prefix, status] of [["plugin", "plugin", data.plugin], ["extension", "extension", data.extension]]) {
    const buttons = installButtons(status, data.package, kind, dirty);
    $(`install-${prefix}-install`).disabled = buttons.installDisabled;
    $(`install-${prefix}-install`).textContent = buttons.installLabel;
    $(`install-${prefix}-uninstall`).disabled = buttons.uninstallDisabled;
    $(`install-${prefix}-install`).title = buttons.reason;
    $(`install-${prefix}-uninstall`).title = buttons.uninstallDisabled ? buttons.reason : "";
  }
}

function renderInstall() {
  const data = state.install;
  if (!data) return;
  $("install-data-dir").textContent = data.data_dir || "-";
  $("install-package-note").textContent = data.package.available
    ? `发布包版本 ${data.package.version}：可以安装插件和扩展。`
    : "开发环境，没有可安装的文件：安装按钮已禁用。";
  for (const [prefix, status] of [["plugin", data.plugin], ["extension", data.extension]]) {
    const badge = installStateBadge(status);
    setBadge(`install-${prefix}-state`, badge.text, badge.className);
    if (status.state === "outdated" || status.state === "unlinked") {
      const note = $(`install-${prefix}-result`);
      if (!note.textContent) setNote(note, status.message, true);
    }
  }
  $("install-napcat-card").hidden = !data.napcat_can_start;
  if (!$("install-import-source").value.trim() && data.import_source) $("install-import-source").value = data.import_source;
  fillPart(P.installPaths);
  updateInstallButtons();
}

async function loadInstall() {
  state.install = await api("/api/install");
  renderInstall();
  watchInstallJob(state.install.job);
}

async function checkInstallPaths() {
  const body = installPathsPayload(readValues(P.installPaths));
  const result = await api("/api/install/check", { method: "POST", body: JSON.stringify(body) });
  renderPathStatuses(result.checks);
}

P.installPaths = definePart({
  id: "install-paths", page: "install", title: "路径",
  collect: fieldsById(INSTALL_IDS),
  fill() {
    const settings = state.install?.settings;
    if (!settings) return;
    for (const [key, id] of Object.entries(INSTALL_IDS)) $(id).value = settings[key] ?? "";
    renderPathStatuses(state.install.checks);
  },
  validate: (values) => installPathsProblems(values),
  async save(values) {
    state.install = await api("/api/install/paths", { method: "POST", body: JSON.stringify(installPathsPayload(values)) });
    return "路径已保存";
  }
});

async function installAction(kind, action) {
  const prefix = kind === "plugin" ? "plugin" : "extension";
  const label = kind === "plugin" ? "AstrBot 插件" : "ST 扩展";
  const installed = state.install?.[prefix]?.state !== "missing";
  const question = action === "uninstall"
    ? `卸载${label}？原文件夹会先备份。`
    : installed ? `重装${label}？现有的文件夹会先备份再替换。` : `安装${label}？`;
  if (!(await confirmDialog(question, { danger: action === "uninstall", confirmLabel: action === "uninstall" ? "卸载" : "确定" }))) return;
  const result = $(`install-${prefix}-result`);
  setNote(result, action === "uninstall" ? "正在卸载…" : "正在安装…");
  try {
    const answer = await api(kind === "plugin" ? "/api/install/plugin" : "/api/install/st-extension", { method: "POST", body: JSON.stringify({ action }) });
    state.install = answer.overview;
    renderInstall();
    setNote(result, `${answer.message}${answer.backup ? `\n原文件夹已备份到：${answer.backup}` : ""}`);
    result.style.whiteSpace = "pre-line";
    notify(answer.message);
  } catch (error) {
    setNote(result, error.message, true);
  }
}

// ---- import
function renderImportPreview(preview) {
  const list = $("install-import-list");
  list.hidden = false;
  const lines = preview.items.map((item) => `<span class="import-item">${escapeHtml(importItemText(item))}</span>`);
  if (preview.skipped.length) lines.push(`<span class="import-item">不会复制：${escapeHtml(preview.skipped.join("、"))}</span>`);
  if (preview.path_references) lines.push(`<span class="import-item">配置里有 ${preview.path_references} 处指向旧数据目录的路径，导入时会改到新目录。</span>`);
  lines.push(`<span class="import-item"><strong>共 ${escapeHtml(formatBytes(preview.total_size))}</strong>${preview.replaces.length ? `，其中 ${preview.replaces.length} 项会替换这里已有的（先备份）` : ""}</span>`);
  list.innerHTML = lines.join("");
}

async function previewImport() {
  const source = $("install-import-source").value.trim();
  const result = $("install-import-result");
  $("install-import-restart").hidden = true;
  state.importPreview = null;
  $("install-import-run").disabled = true;
  try {
    const preview = await api("/api/install/import/preview", { method: "POST", body: JSON.stringify({ source }) });
    state.importPreview = preview;
    renderImportPreview(preview);
    setNote(result, "确认无误后点“确认导入”。");
    $("install-import-run").disabled = preview.items.length === 0;
  } catch (error) {
    $("install-import-list").hidden = true;
    setNote(result, error.message, true);
  }
}

async function runImport() {
  const preview = state.importPreview;
  if (!preview) return;
  if (!(await confirmDialog(`把上面 ${preview.items.length} 项复制到数据目录？这里已有的同名文件会先备份，来源不会被修改。`, { confirmLabel: "导入" }))) return;
  const result = $("install-import-result");
  $("install-import-run").disabled = true;
  try {
    const done = await api("/api/install/import", { method: "POST", body: JSON.stringify({ source: preview.source, confirm: true }) });
    setNote(result, importDoneText(done));
    $("install-import-restart").hidden = Boolean(done.restarting) || !done.restart_available;
    state.importPreview = null;
    if (done.restarting) {
      notify(restartNotice(done), !done.auto);
      if (done.auto) await waitForCore();
      else setNote(result, `${importDoneText(done)}\n${restartNotice(done)}`);
    }
  } catch (error) {
    setNote(result, error.message, true);
    $("install-import-run").disabled = false;
  }
}

// ---- extra dependencies
function renderDeps() {
  const data = state.deps;
  if (!data) return;
  const running = data.job?.state === "running";
  $("install-deps").innerHTML = data.rows.map((row) => {
    const status = dependencyStateText(row);
    const blocked = row.state === "blocked";
    return `<div class="dep-row"><div class="row-main"><strong>${escapeHtml(row.label)}</strong><small>${escapeHtml(row.note || "")}</small></div><span class="status-badge ${status.className}">${escapeHtml(status.text)}</span><button type="button" class="btn btn-secondary btn-sm" data-dep-install="${escapeHtml(row.name)}" ${blocked || running ? "disabled" : ""} ${blocked ? `title="${escapeHtml(row.needs || "")}"` : ""}>${row.state === "installed" ? "重装" : "安装"}</button></div>`;
  }).join("") || '<p class="placeholder">没有可安装的依赖</p>';
  $("install-deps").querySelectorAll("[data-dep-install]").forEach((button) => button.addEventListener("click", () => installDependency(button.dataset.depInstall)));
  $("install-manual").textContent = data.rows.map((row) => row.command).join("\n") || "-";
  renderJob(data.job);
}

function renderJob(job) {
  const box = $("install-job");
  box.hidden = !job;
  if (!job) return;
  box.textContent = [jobStatusText(job), ...job.lines].join("\n");
}

async function loadDeps() {
  state.deps = await api("/api/install/deps");
  renderDeps();
  watchInstallJob(state.deps.job);
}

/** While a dependency installs, look at it every 1.5 s (the quick overview call; the slow check runs once at the end). */
function watchInstallJob(job) {
  clearTimeout(jobTimer);
  jobTimer = null;
  if (!job || job.state !== "running") return;
  jobTimer = setTimeout(async () => {
    try {
      const overview = await api("/api/install");
      state.install = overview;
      if (state.deps) state.deps.job = overview.job;
      renderJob(overview.job);
      if (overview.job && overview.job.state === "running") watchInstallJob(overview.job);
      else await loadDeps();
    } catch (error) {
      watchInstallJob(job);
    }
  }, 1500);
}

async function installDependency(name) {
  try {
    const answer = await api(`/api/install/deps/${encodeURIComponent(name)}/install`, { method: "POST", body: "{}" });
    if (state.deps) { state.deps.job = answer.job; renderDeps(); }
    watchInstallJob(answer.job);
  } catch (error) {
    notify(error.message, true);
  }
}

async function startNapcat() {
  const result = $("install-napcat-result");
  try {
    const answer = await api("/api/install/napcat/start", { method: "POST", body: "{}" });
    setNote(result, answer.message);
  } catch (error) {
    setNote(result, error.message, true);
  }
}

// ---- restart the core and come back
async function restartCore() {
  if (!(await confirmDialog("重启核心？正在处理的消息会中断，页面会自动重新连接。", { confirmLabel: "重启" }))) return;
  let answer;
  try {
    answer = await api("/api/system/restart", { method: "POST", body: "{}" });
  } catch (error) {
    showError(error);
    return;
  }
  notify(restartNotice(answer), !answer.auto);
  if (answer.auto) await waitForCore();
}

/** Poll until the core has gone away and answers again, then reload the page (the core is a new process). */
async function waitForCore() {
  let wasDown = false;
  for (let attempt = 0; attempt < 90; attempt += 1) {
    await delay(1000);
    let up = false;
    try { up = (await fetch("/api/health", { cache: "no-store" })).ok; } catch (error) { up = false; }
    if (!up) wasDown = true;
    else if (wasDown) { location.reload(); return; }
  }
  notify("核心没有在预期时间内回来，请看 start.bat 的窗口。", true);
}

function bindInstall() {
  $("install-check").addEventListener("click", () => checkInstallPaths().catch(showError));
  $("install-paths-form").addEventListener("input", updateInstallButtons);
  $("install-mirror").addEventListener("input", updateInstallButtons);
  $("install-plugin-install").addEventListener("click", () => installAction("plugin", "install"));
  $("install-plugin-uninstall").addEventListener("click", () => installAction("plugin", "uninstall"));
  $("install-extension-install").addEventListener("click", () => installAction("extension", "install"));
  $("install-extension-uninstall").addEventListener("click", () => installAction("extension", "uninstall"));
  $("install-import-preview").addEventListener("click", previewImport);
  $("install-import-run").addEventListener("click", runImport);
  $("install-import-restart-button").addEventListener("click", restartCore);
  $("install-napcat-start").addEventListener("click", startNapcat);
  $("restart-core").addEventListener("click", restartCore);
  $("owner-claim-start").addEventListener("click", () => startOwnerClaim().catch(showError));
  $("overview-restart-core").addEventListener("click", restartCore);
}

function bind() {
  bindInstall();
  document.querySelectorAll(".nav-item").forEach((item) => item.addEventListener("click", (event) => { event.preventDefault(); requestPage(item.dataset.page); }));
  $("model-filter")?.addEventListener("input", renderModels);
  $("new-model").addEventListener("click", async () => { if (await confirmDiscardCard(P.modelCard)) openModel(); }); $("cancel-model").addEventListener("click", () => requestCloseDrawer("model-form-card")); $("model-form").addEventListener("submit", submitModel);
  $("new-character").addEventListener("click", () => requestOpenCharacter()); $("cancel-character").addEventListener("click", () => requestCloseDrawer("character-form-card")); $("character-form").addEventListener("submit", submitCharacter); $("new-emotion").addEventListener("click", createEmotion);
  $("audio-file").addEventListener("change", uploadNewEmotionAudio);
  document.querySelectorAll("[data-engine]").forEach((button) => button.addEventListener("click", () => switchEngine(button.dataset.engine)));
  $("service-start").addEventListener("click", () => runServiceAction("/api/tts/service/start", "正在启动，首次加载模型可能需要一两分钟…"));
  $("service-stop").addEventListener("click", () => runServiceAction("/api/tts/service/stop", "正在关闭…"));
  bindPathPicker();
  bindDialog();
  bindUnsaved();
  $("console-error-close").addEventListener("click", () => { $("console-error").hidden = true; });
  document.querySelectorAll("#sing .tab, #system .tab").forEach((button) => button.addEventListener("click", () => selectTab(button.closest(".page").id, button.dataset.tab)));
  document.addEventListener("click", (event) => {
    const link = event.target.closest("[data-goto]");
    if (!link) return;
    event.preventDefault();
    const target = splitTarget(link.dataset.goto);
    requestPage(target.page, target.tab);
  });
  setupLists();
  enhanceCards();
  enhanceModules();
  bindMenus();
  bindEmptyStates();
  bindPlayer();
  bindDrawers();
  bindPresets();
  bindOnboarding();
  bindAudition();
  $("overview-test-chat").addEventListener("click", openTestChat);
  $("testchat-send").addEventListener("click", sendTestChat);
  $("testchat-close").addEventListener("click", closeTestChat);
  $("testchat-cancel").addEventListener("click", closeTestChat);
  $("testchat-modal").addEventListener("click", (event) => { if (event.target === $("testchat-modal")) closeTestChat(); });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !$("testchat-modal").hidden && $("app-dialog").hidden) { event.preventDefault(); closeTestChat(); }
  });
  $("cleanup-run").addEventListener("click", runCleanup);
  $("cleanup-clear-covers").addEventListener("click", clearCoverCache);
  $("cover-vocal-reverb").addEventListener("input", () => { $("cover-vocal-reverb-value").textContent = $("cover-vocal-reverb").value; });
  $("cover-start").addEventListener("click", () => runCoverService("start"));
  $("cover-stop").addEventListener("click", () => runCoverService("stop"));
  $("online-search").addEventListener("click", searchOnline);
  $("online-fetch").addEventListener("click", fetchOnline);
  $("song-file").addEventListener("change", () => { const files = [...$("song-file").files]; $("song-file").value = ""; uploadSongs(files); });
  $("sing-button").addEventListener("click", singTest);
  $("pitch-preset-add").addEventListener("click", addPitchPreset);
  $("plan-form").addEventListener("submit", submitPlan);
  $("plan-kind").addEventListener("change", togglePlanKind);
  $("plan-cancel").addEventListener("click", () => requestCloseDrawer("plan-form-card"));
  $("new-plan").addEventListener("click", async () => { if (state.schedule && await confirmDiscardCard(P.planCard)) openPlanForm(emptyPlanForm(), "新建计划"); });
  $("plan-template").addEventListener("change", async () => {
    const template = state.schedule?.templates?.[Number($("plan-template").value)];
    $("plan-template").value = "";
    if (template && await confirmDiscardCard(P.planCard)) openPlanForm({ ...planToForm(template), id: "", enabled: true }, `从模板添加：${template.name}`);
  });
  $("injection-add").addEventListener("click", () => {
    const entries = readInjectionEntries();
    if (entries.length >= INJECTION_MAX_ENTRIES) { notify(`最多 ${INJECTION_MAX_ENTRIES} 条`, true); return; }
    renderInjectionEntries([...entries, newInjectionEntry(entries)]);
    const added = $("injection-entries").lastElementChild;
    if (added) added.open = true;
    refreshDirtyUi();
  });
  $("injection-restore").addEventListener("click", () => {
    const { entries, restored } = restoreBuiltinEntries(readInjectionEntries(), state.injection?.defaults?.entries || []);
    if (!restored.length) { notify("内置条目都在"); return; }
    renderInjectionEntries(entries);
    refreshDirtyUi();
    notify(`已恢复：${restored.join("、")}（保存后生效）`);
  });
  $("affinity-add-tier").addEventListener("click", () => { $("affinity-tiers").insertAdjacentHTML("beforeend", tierRow()); bindTierButtons(); refreshDirtyUi(); });
  $("group-affinity-add-tier").addEventListener("click", () => { $("group-affinity-tiers").insertAdjacentHTML("beforeend", tierRow()); bindTierButtons(); refreshDirtyUi(); });
  $("chatplat-copy-url").addEventListener("click", (event) => copyOnebotValue("chatplat-url", event.currentTarget));
  $("chatplat-copy-token").addEventListener("click", (event) => copyOnebotValue("chatplat-token", event.currentTarget));
  $("chatplat-show-token").addEventListener("click", (event) => setTokenShown(event.currentTarget.dataset.shown !== "1"));
  $("logs-refresh").addEventListener("click", () => loadLogs().catch(showError));
  $("chat-refresh").addEventListener("click", () => refreshChatStatus().catch(showError));
  $("chat-test").addEventListener("click", () => testChat());
  $("chat-window-add-weekday").addEventListener("click", () => addChatWindow("weekday"));
  $("chat-window-add-weekend").addEventListener("click", () => addChatWindow("weekend"));
  $("mood-refresh").addEventListener("click", () => loadMood().catch(showError));
  $("mood-reset-global").addEventListener("click", () => resetMood("global"));
  $("mood-reset-owner").addEventListener("click", () => resetMood("owner"));
  $("logs-lines").addEventListener("change", () => loadLogs().catch(showError));
  $("logs-filter").addEventListener("input", renderLogs);
  $("logs-level").addEventListener("change", renderLogs);
  $("logs-auto").addEventListener("change", syncLogsAutoRefresh);
  bindSearch();
  $("cleanup-clear-backups").addEventListener("click", clearInstallBackups);
}

// ---------- U5-3: row-style settings (config keys, segmented choices), page help, restore defaults, sidebar status ----------

const localStore = () => { try { return localStorage; } catch { return null; } };

/** The grey config key next to each setting's name, from the same collect() map the search uses. */
function decorateFieldKeys(page) {
  for (const part of partsOf(page)) {
    let elements = {};
    try { elements = part.collect(); } catch { continue; }
    for (const [key, element] of Object.entries(elements)) {
      if (!element || inDrawerOrDialog(element)) continue;
      const field = element.closest(".field");
      const label = field && field.querySelector(".field-label");
      if (!label || label.classList.contains("mono") || label.dataset.key !== undefined) continue;
      const text = fieldKeyText(key, label.textContent);
      if (text) label.dataset.key = text;
    }
  }
}

/** Up to three options: buttons side by side next to the (hidden) drop-down; rebuilt when the options change. */
function decorateSegmented(page) {
  const section = $(page);
  if (!section) return;
  section.querySelectorAll(".field > select").forEach((select) => {
    if (inDrawerOrDialog(select)) return;
    const options = [...select.options];
    const signature = options.map((option) => `${option.value}\u0001${option.textContent}`).join("\u0002");
    const totalChars = options.reduce((sum, option) => sum + option.textContent.length, 0);
    let group = select.nextElementSibling && select.nextElementSibling.classList.contains("segmented") ? select.nextElementSibling : null;
    if (group && group.dataset.signature !== signature) { group.remove(); group = null; select.removeAttribute("data-segmented-ready"); }
    if (!group && useSegmented(options.length, totalChars)) {
      group = document.createElement("div");
      group.className = "segmented";
      group.setAttribute("role", "radiogroup");
      group.setAttribute("aria-label", cleanText(select.closest(".field")?.querySelector(".field-label")?.textContent));
      group.dataset.signature = signature;
      group.innerHTML = options.map((option) => `<button type="button" role="radio" data-value="${escapeHtml(option.value)}">${escapeHtml(option.textContent)}</button>`).join("");
      select.insertAdjacentElement("afterend", group);
      select.dataset.segmentedReady = "1";
    }
    if (group) group.querySelectorAll("button").forEach((button) => {
      const on = button.dataset.value === select.value;
      button.classList.toggle("active", on);
      button.setAttribute("aria-checked", on ? "true" : "false");
    });
  });
}

function bindSegmented() {
  document.addEventListener("click", (event) => {
    const button = event.target.closest(".segmented > button");
    if (!button) return;
    const select = button.parentElement.previousElementSibling;
    if (!select || select.tagName !== "SELECT" || select.value === button.dataset.value) return;
    select.value = button.dataset.value;
    select.dispatchEvent(new Event("input", { bubbles: true }));
    select.dispatchEvent(new Event("change", { bubbles: true }));
    decorateSegmented(activePage());
  });
}

/** "还没有群" under the NapCat group whitelist while it is empty. */
function refreshGroupsEmpty() {
  const box = $("chatplat-groups-empty");
  const input = $("chatplat-groups");
  if (box && input) box.hidden = input.value.trim() !== "";
}

// -- the "?" next to a page title: opens the full explanation under the one-line lead; remembered per page --

function setHelp(id, open, remember = true) {
  const panel = $(`help-${id}`);
  if (!panel) return;
  panel.hidden = !open;
  document.querySelectorAll(`[data-help="${id}"]`).forEach((button) => {
    button.setAttribute("aria-expanded", open ? "true" : "false");
    button.title = open ? "收起说明" : "展开说明";
  });
  if (remember) writeHelpOpen(localStore(), id, open);
}

function bindHelp() {
  document.querySelectorAll(".help-panel").forEach((panel) => {
    if (panel.querySelector(".help-close")) return;
    const close = document.createElement("button");
    close.type = "button";
    close.className = "help-close";
    close.setAttribute("aria-label", "关闭说明");
    close.dataset.helpClose = panel.id.replace(/^help-/, "");
    close.textContent = "×";
    panel.appendChild(close);
  });
  document.querySelectorAll("[data-help]").forEach((button) => setHelp(button.dataset.help, readHelpOpen(localStore(), button.dataset.help), false));
  document.addEventListener("click", (event) => {
    const toggle = event.target.closest("[data-help]");
    if (toggle) { setHelp(toggle.dataset.help, $(`help-${toggle.dataset.help}`).hidden); return; }
    const close = event.target.closest("[data-help-close]");
    if (close) setHelp(close.dataset.helpClose, false);
  });
}

// -- "恢复默认": a module's own fields back to the factory values (unsaved until 保存) --

const viaState = (key, make) => ({ get: () => state[key], set: (value) => { state[key] = value; }, make });
const DEFAULT_SOURCES = {
  network: viaState("network", (cur, cfg) => ({ ...cur, settings: { ...(cur && cur.settings), ...cfg.network } })),
  queue: viaState("queue", (cur, cfg) => ({ ...cur, settings: { ...(cur && cur.settings), ...cfg.queue } })),
  mood: viaState("mood", (cur, cfg) => ({ ...cur, settings: { ...(cur && cur.settings), ...cfg.mood } })),
  "tts-common": viaState("tts", (cur, cfg) => ({ ...cur, common: { ...(cur && cur.common), ...cfg.tts.common } })),
  irodori: viaState("tts", (cur, cfg) => ({ ...cur, irodori: { ...(cur && cur.irodori), ...cfg.tts.irodori } })),
  "gpt-service": viaState("tts", (cur, cfg) => ({ ...cur, gpt_sovits: { ...(cur && cur.gpt_sovits), ...cfg.tts.gpt_sovits } })),
  "sticker-settings": viaState("stickers", (cur, cfg) => ({ ...cur, settings: { ...(cur && cur.settings), ...cfg.stickers } })),
  "schedule-settings": viaState("schedule", (cur, cfg) => ({ ...cur, settings: { ...(cur && cur.settings), ...cfg.schedule.settings } })),
  "cover-service": viaState("cover", (cur, cfg) => ({ ...cur, settings: { ...(cur && cur.settings), ...cfg.cover } })),
  "cover-params": viaState("cover", (cur, cfg) => ({ ...cur, settings: { ...(cur && cur.settings), ...cfg.cover } })),
  "cover-music": viaState("cover", (cur, cfg) => ({ ...cur, settings: { ...(cur && cur.settings), ...cfg.cover } })),
  "song-request": viaState("cover", (cur, cfg) => ({ ...cur, song_request: { ...(cur && cur.song_request), ...cfg.song_request } })),
  "cleanup-settings": { get: () => cleanupData, set: (value) => { cleanupData = value; }, make: (cur, cfg) => ({ ...cur, settings: { ...(cur && cur.settings), ...cfg.cleanup } }) }
};
// paths, folders and the music key belong to this computer, not to the factory: "恢复默认" leaves them alone
const RESTORE_SKIP = /(_root|_dir|_path|_adapter)$|^(model|key|music_api_key)$/;
let configDefaults = null;

async function loadConfigDefaults() {
  if (!configDefaults) configDefaults = await api("/api/config/defaults");
  return configDefaults;
}

const restoreContainer = (element) => element.closest("details.module") || element.closest(".card");

function decorateRestore(page) {
  for (const part of partsOf(page)) {
    if (!DEFAULT_SOURCES[part.id]) continue;
    let elements = {};
    try { elements = part.collect(); } catch { continue; }
    for (const element of Object.values(elements)) {
      if (!element || inDrawerOrDialog(element)) continue;
      const container = restoreContainer(element);
      if (!container || container.querySelector(":scope > .restore-defaults, :scope > summary > .restore-defaults, :scope > .card-header > .restore-defaults, :scope > .section-header > .restore-defaults")) continue;
      const button = document.createElement("button");
      button.type = "button";
      button.className = "restore-defaults";
      button.setAttribute("aria-label", "恢复默认");  // the words are drawn by CSS so a summary / title keeps its own text
      button.title = "把这一块的设置改回出厂值（不会马上保存）";
      const host = container.matches("details.module") ? container.querySelector(":scope > summary") : container.querySelector(":scope > .card-header, :scope > .section-header") || container;
      host.appendChild(button);
    }
  }
}

async function restoreModule(button) {
  const container = restoreContainer(button.parentElement);
  if (!container) return;
  let defaults;
  try { defaults = await loadConfigDefaults(); } catch (error) { notify(`读取默认值失败：${error.message}`, true); return; }
  let restored = 0;
  for (const part of partsOf(activePage())) {
    const source = DEFAULT_SOURCES[part.id];
    if (!source) continue;
    const elements = part.collect();
    const keys = Object.keys(elements).filter((key) => elements[key] && container.contains(elements[key]) && !RESTORE_SKIP.test(key));
    if (!keys.length || source.get() === undefined || source.get() === null) continue;
    const current = readValues(part);
    const saved = source.get();
    let factory = null;
    try {
      source.set(source.make(saved, defaults));
      part.fill();
      factory = readValues(part);
    } catch (error) {
      console.error(error);
    } finally {
      source.set(saved);
      part.fill();
      applyValues(part, current);
    }
    if (!factory) continue;
    const picked = restoreValues(factory, keys);
    applyValues(part, picked);
    restored += Object.keys(picked).length;
  }
  refreshDirtyUi();
  notify(restored ? "已改回默认值，点页面底部的“保存”才生效" : "这一块没有可以恢复的设置", !restored);
}

function bindRestore() {
  document.addEventListener("click", (event) => {
    const button = event.target.closest(".restore-defaults");
    if (!button) return;
    event.preventDefault();
    event.stopPropagation();
    restoreModule(button).catch(console.error);
  });
}

// -- the sidebar: character avatar and name on top, core state / version / chat platform at the bottom --

function renderSideStatus() {
  const view = coreStatusView(health.status, health.failures);
  $("side-dot").className = `side-dot level-${view.level}`;
  $("side-status-text").textContent = view.text;
  const version = ((state.install || (state.checklist && state.checklist.install)) || {}).package?.version;
  $("side-version").textContent = version ? `v${version}` : "";
  const platform = platformName(health.status);
  const box = $("side-platform");
  box.hidden = !platform;
  box.textContent = platform ? `聊天平台：${platform}` : "";
}

/** One pass over the page that is showing: keys, drop-down → buttons, restore buttons, empty hints. */
function decorateRows(page) {
  decorateFieldKeys(page);
  decorateSegmented(page);
  decorateRestore(page);
  refreshGroupsEmpty();
}

async function init() {
  bind();
  bindHelp();
  bindSegmented();
  bindRestore();
  $("chatplat-groups").addEventListener("input", refreshGroupsEmpty);
  try { await loadTts(); } catch (error) { console.error(error); notify(`语音设置不可用：${error.message}`, true); }
  try { await loadAll(); } catch (error) { showError(error); }
  loadPresets().catch(console.error);
  const start = resolveTarget(pageFromHash(location.hash), tabFromHash(location.hash));
  switchPage(start.page, start.tab);
  startServicePolling();
}

init();
