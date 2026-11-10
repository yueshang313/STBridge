// R5b: the "注入到预设/世界书" block of the extension panel, and the {{stbridge_*}} macros.
//
// The pure logic lives in injection.mjs (tested with node). This file is the ST glue and is checked by hand
// in a real SillyTavern. ST internals are imported lazily: if a future ST
// moves one of them, only this block says "not supported" and the rest of the extension keeps working.

import {
    MACRO_KEYS,
    macroHelpText,
    newMacroKeys,
    applyToPreset,
    applyToWorldBook,
    bookAction,
    bookStatus,
    cleanEntries,
    cleanMacros,
    macroValue,
    presetEntries,
    presetStatus,
    removeFromPreset,
    removeFromWorldBook,
} from "./injection.mjs";

const SECTION_ID = "st-bridge-client-injection";
const STATUS_ID = "st-bridge-client-injection-status";
const RESULT_ID = "st-bridge-client-injection-result";
const SYNC_ID = "st-bridge-client-injection-sync";
const REMOVE_ID = "st-bridge-client-injection-remove";
const REFRESH_ID = "st-bridge-client-injection-refresh";
const DEFAULT_MACRO_REFRESH_MS = 60000;

const MACRO_HELP = {
    emotions: "当前角色的情绪列表",
    group_tag: "群友发言前缀",
    speaker: "本轮发言人",
    affinity: "好感度数值",
    affinity_tier: "好感度档位名",
    affinity_hint: "档位态度描述",
    affinity_var: "更新好感度的变量路径",
    affinity_range: "好感度范围",
    affinity_step: "单次最大变化",
    song_allowed: "本轮是否允许唱歌",
    mood: "琴音最近的心情走向",
    owner_mood: "琴音对主人的心情走向",
};

// ---------- macros ----------

const macroState = { values: null, fromClaim: false, help: {}, getContext: null, registered: new Set() };

/** Called with every claim's macros (and with the core's default values when nothing was claimed yet). */
export function setMacros(macros, { fromClaim = false, help = null } = {}) {
    const clean = cleanMacros(macros);
    if (!clean) return;
    if (help && typeof help === "object") macroState.help = { ...macroState.help, ...help };
    // A key the core added after this extension was written: register it now (once).
    if (macroState.getContext) {
        for (const key of newMacroKeys(clean, macroState.registered)) registerOneMacro(macroState.getContext, key);
    }
    if (!fromClaim && macroState.fromClaim) return;
    macroState.values = clean;
    if (fromClaim) macroState.fromClaim = true;
}

/** Registers {{stbridge_<key>}} once; the values are read at every generation, so they always follow the latest turn. */
export function registerMacros(getContext) {
    macroState.getContext = getContext;
    for (const key of MACRO_KEYS) {
        if (macroState.registered.has(key)) continue;
        if (registerOneMacro(getContext, key) === false) return false;
    }
    return true;
}

/** Register one macro (never twice). false: ST offers no macro API at all. */
function registerOneMacro(getContext, key) {
    if (macroState.registered.has(key)) return true;
    const context = getContext();
    const name = `stbridge_${key}`;
    const help = macroHelpText(macroState.help, key, MACRO_HELP[key] ?? undefined);
    const read = () => macroValue(macroState.values, key, getContext().name1);
    try {
        if (typeof context.registerMacro === "function") {
            context.registerMacro(name, read, help);
        } else if (typeof context.macros?.register === "function") {
            context.macros.register(name, { handler: read, description: help });
        } else {
            globalThis.console?.warn?.("ST Bridge: no macro API found, {{stbridge_*}} macros are not available");
            return false;
        }
        macroState.registered.add(key);
    } catch (error) {
        globalThis.console?.warn?.("ST Bridge: macro registration failed", name, error);
        macroState.registered.add(key);
    }
    return true;
}

// ---------- ST modules ----------

let stModules = null;

async function loadStModules() {
    if (stModules) return stModules;
    const [openai, presets, worldInfo] = await Promise.all([
        import("../../../openai.js"),
        import("../../../preset-manager.js"),
        import("../../../world-info.js"),
    ]);
    for (const [module, names] of [
        [openai, ["oai_settings", "getChatCompletionPreset"]],
        [presets, ["getPresetManager"]],
        [worldInfo, ["loadWorldInfo", "saveWorldInfo", "deleteWorldInfo", "updateWorldInfoList", "newWorldInfoEntryTemplate"]],
    ]) {
        for (const name of names) {
            if (!(name in module)) throw new Error(`missing_${name}`);
        }
    }
    stModules = { openai, presets, worldInfo };
    return stModules;
}

const errorText = (error) => String(error?.code || error?.message || error || "unknown").slice(0, 80);

// ---------- ST operations ----------

function isChatCompletion(getContext) {
    return getContext().mainApi === "openai";
}

async function readWorldBook(modules, name) {
    const { worldInfo } = modules;
    const exists = Array.isArray(worldInfo.world_names) && worldInfo.world_names.includes(name);
    const data = exists ? await worldInfo.loadWorldInfo(name) : null;
    const bound = Array.isArray(worldInfo.selected_world_info) && worldInfo.selected_world_info.includes(name);
    return { data: data ?? null, bound };
}

function setBookBound(name, bound) {
    const $ = globalThis.jQuery || globalThis.$;
    const select = $("#world_info");
    const option = select.children().filter(function matches() { return $(this).text() === name; });
    if (!option.length) throw new Error("world_info_option_missing");
    option.prop("selected", bound);
    select.trigger("change");
}

async function savePreset(modules, preset) {
    const { oai_settings: settings, getChatCompletionPreset } = modules.openai;
    settings.prompts = preset.prompts;
    settings.prompt_order = preset.prompt_order;
    await modules.presets.getPresetManager("openai").savePreset(settings.preset_settings_openai, getChatCompletionPreset(settings));
    try {
        modules.openai.promptManager?.render?.(false);
    } catch {
        // The list redraws itself the next time the Prompt Manager opens.
    }
}

async function backupPreset(modules, client) {
    const { oai_settings: settings, getChatCompletionPreset } = modules.openai;
    const saved = await client.backupPreset(settings.preset_settings_openai, getChatCompletionPreset(settings));
    return saved?.file ?? "";
}

async function syncBook(modules, name, entries) {
    const { worldInfo } = modules;
    const current = await readWorldBook(modules, name);
    const action = bookAction(current.data, entries);
    if (action === "none") return "无需处理";
    if (action === "delete") {
        await worldInfo.deleteWorldInfo(name);
        return "已删除世界书";
    }
    if (action === "strip") {
        const stripped = removeFromWorldBook(current.data);
        await worldInfo.saveWorldInfo(name, stripped.data, true);
        worldInfo.reloadEditor?.(name);
        return `已移除 STbridge 条目（世界书里还有 ${stripped.foreign} 条你自己的，保留）`;
    }
    let data = current.data;
    if (!data) {
        await worldInfo.saveWorldInfo(name, { entries: {} }, true);
        await worldInfo.updateWorldInfoList();
        data = { entries: {} };
    }
    const result = applyToWorldBook(data, entries, worldInfo.newWorldInfoEntryTemplate);
    await worldInfo.saveWorldInfo(name, result.data, true);
    worldInfo.reloadEditor?.(name);
    if (!current.bound) setBookBound(name, true);
    return `世界书「${name}」写入 ${result.count} 条（新增 ${result.added.length}，更新 ${result.updated.length}，移除 ${result.removed.length}），已设为全局`;
}

// ---------- the panel block ----------

/**
 * Builds the block. `getClient()` returns the BridgeClient (created after the panel), `getContext` is ST's.
 * Returns {element, start}: append the element to the panel, call start() once the client exists.
 */
export function createInjectionSection({ getClient, getContext }) {
    const area = document.createElement("section");
    area.id = SECTION_ID;
    area.className = "st-bridge-client-injection";

    const title = document.createElement("h4");
    title.className = "st-bridge-card-title";
    const titleIcon = document.createElement("i");
    titleIcon.className = "fa-solid fa-syringe";
    title.appendChild(titleIcon);
    const titleText = document.createElement("span");
    titleText.textContent = "注入到预设/世界书";
    title.appendChild(titleText);
    area.appendChild(title);

    const intro = document.createElement("p");
    intro.className = "st-bridge-note";
    intro.textContent = "把 STbridge 需要的规则（情绪标签、game 标签、中日双语、唱歌标签、好感度更新）写进当前的 Chat Completion 预设和全局世界书「STbridge」。条目在 STbridge 控制台的“ST 注入”页编辑。只增删带 STbridge 标记的条目，你自己的预设条目和世界书不会被改动；改预设前会先备份。";
    area.appendChild(intro);

    const status = document.createElement("div");
    status.id = STATUS_ID;
    status.setAttribute("aria-live", "polite");
    area.appendChild(status);

    const row = document.createElement("div");
    row.className = "st-bridge-row";
    const makeButton = (id, text) => {
        const button = document.createElement("button");
        button.id = id;
        button.type = "button";
        button.className = "menu_button";
        button.textContent = text;
        row.appendChild(button);
        return button;
    };
    const syncButton = makeButton(SYNC_ID, "同步");
    const removeButton = makeButton(REMOVE_ID, "移除");
    const refreshButton = makeButton(REFRESH_ID, "刷新状态");
    area.appendChild(row);

    const result = document.createElement("div");
    result.id = RESULT_ID;
    result.setAttribute("aria-live", "polite");
    area.appendChild(result);

    let busy = false;
    let injection = null; // the core's last answer: {world_book_name, entries, macros}

    const line = (text, tone = "") => {
        const element = document.createElement("div");
        element.textContent = text;
        if (tone === "bad") element.style.color = "var(--SmartThemeQuoteColor, #c66)";
        return element;
    };
    const show = (target, lines) => target.replaceChildren(...lines.map((item) => (typeof item === "string" ? line(item) : line(...item))));
    const setBusy = (value) => {
        busy = value;
        for (const button of [syncButton, removeButton, refreshButton]) button.disabled = value;
    };

    async function loadInjection() {
        const client = getClient();
        if (!client) throw new Error("no_client");
        const data = await client.fetchInjection();
        injection = {
            world_book_name: typeof data?.world_book_name === "string" && data.world_book_name ? data.world_book_name : "STbridge",
            entries: cleanEntries(data?.entries),
            macros: data?.macros,
        };
        setMacros(injection.macros, { help: data?.macro_help });
        return injection;
    }

    async function refresh() {
        const lines = [];
        try {
            const modules = await loadStModules();
            const data = await loadInjection();
            const chat = isChatCompletion(getContext);
            lines.push(chat ? "API：Chat Completion" : ["API：不是 Chat Completion（文字补全不支持预设注入，请在控制台把条目的“写到”改成世界书）", "bad"]);
            const settings = modules.openai.oai_settings;
            if (chat) {
                lines.push(`当前预设：${settings.preset_settings_openai}`);
                const wanted = presetEntries(data.entries).length;
                const state = presetStatus(settings, data.entries);
                if (!wanted && !state.stale) lines.push("预设：控制台没有要写进预设的条目");
                else if (state.inSync) lines.push(`预设：已注入 ${state.total} 条，与控制台一致`);
                else if (!state.present && !state.stale) lines.push(["预设：还没有注入，点「同步」", "bad"]);
                else lines.push([`预设：已注入 ${state.present} / ${state.total} 条，和控制台不一致（${state.matching} 条内容一致${state.stale ? `，${state.stale} 条该移除` : ""}${state.ordered ? "" : "，位置或开关不对"}），点「同步」`, "bad"]);
            }
            const book = await readWorldBook(modules, data.world_book_name);
            const bookState = bookStatus(book.data, data.entries, book.bound);
            if (!bookState.total && !bookState.exists) lines.push("世界书：控制台没有要写进世界书的条目");
            else if (!bookState.exists) lines.push([`世界书「${data.world_book_name}」：还不存在，点「同步」创建`, "bad"]);
            else if (bookState.inSync) lines.push(`世界书「${data.world_book_name}」：已存在，${bookState.bound ? "已全局绑定，" : ""}${bookState.total} 条与控制台一致`);
            else lines.push([`世界书「${data.world_book_name}」：${bookState.bound ? "已绑定" : "未绑定"}，${bookState.matching} / ${bookState.total} 条一致${bookState.stale ? `，${bookState.stale} 条该移除` : ""}，点「同步」`, "bad"]);
        } catch (error) {
            const code = errorText(error);
            lines.push([`无法读取状态：${code === "network_error" || code === "network_timeout" ? "连不上 STbridge 核心（核心没在运行，或端口不对）" : code}`, "bad"]);
        }
        show(status, lines);
    }

    async function confirm(text) {
        const context = getContext();
        if (typeof context.callGenericPopup === "function" && context.POPUP_TYPE) {
            const answer = await context.callGenericPopup(text, context.POPUP_TYPE.CONFIRM);
            return answer === (context.POPUP_RESULT?.AFFIRMATIVE ?? 1);
        }
        return globalThis.confirm(text);
    }

    async function run(kind) {
        if (busy) return;
        setBusy(true);
        const messages = [];
        try {
            const modules = await loadStModules();
            const data = await loadInjection();
            const client = getClient();
            const chat = isChatCompletion(getContext);
            const name = modules.openai.oai_settings.preset_settings_openai;
            const verb = kind === "sync" ? "同步" : "移除";
            const lead = chat ? `将${verb === "同步" ? "修改" : "清理"}当前预设「${name}」（先备份）` : "当前不是 Chat Completion，预设会跳过";
            const book = kind === "sync" ? `并写入全局世界书「${data.world_book_name}」` : `并清理世界书「${data.world_book_name}」`;
            if (!(await confirm(`${lead}，${book}。只动带 STbridge 标记的条目。继续？`))) {
                show(result, ["已取消"]);
                return;
            }
            if (chat) {
                const settings = modules.openai.oai_settings;
                const plan = kind === "sync" ? applyToPreset(settings, data.entries) : removeFromPreset(settings);
                const changed = kind === "sync" ? plan.added.length + plan.updated.length + plan.removed.length > 0 || !presetStatus(settings, data.entries).ordered : plan.removed.length > 0;
                if (changed) {
                    const file = await backupPreset(modules, client);
                    await savePreset(modules, plan);
                    messages.push(kind === "sync"
                        ? `预设「${name}」：新增 ${plan.added.length}，更新 ${plan.updated.length}，移除 ${plan.removed.length}${plan.noChatHistory ? "（预设里没有 Chat History，已放在末尾）" : ""}；备份：${file}`
                        : `预设「${name}」：已移除 ${plan.removed.length} 条；备份：${file}`);
                } else {
                    messages.push(`预设「${name}」：没有变化`);
                }
            } else if (kind === "sync" && presetEntries(data.entries).length) {
                messages.push("预设：已跳过（不是 Chat Completion）");
            }
            messages.push(kind === "sync"
                ? await syncBook(modules, data.world_book_name, data.entries)
                : await syncBook(modules, data.world_book_name, []));
            if (kind === "sync") messages.push("宏 {{stbridge_*}} 已就绪");
            show(result, messages);
        } catch (error) {
            show(result, [...messages, [`${kind === "sync" ? "同步" : "移除"}失败：${errorText(error)}（已完成的步骤不会撤销；预设改动前有备份）`, "bad"]]);
        } finally {
            setBusy(false);
            await refresh();
        }
    }

    syncButton.addEventListener("click", () => { void run("sync"); });
    removeButton.addEventListener("click", () => { void run("remove"); });
    refreshButton.addEventListener("click", () => { void refresh(); });

    function start() {
        void refresh();
        const context = getContext();
        const source = context.eventSource;
        const types = context.eventTypes ?? {};
        for (const type of [types.OAI_PRESET_CHANGED_AFTER, types.CHAT_CHANGED, types.WORLDINFO_SETTINGS_UPDATED, types.WORLDINFO_UPDATED]) {
            if (typeof type === "string") source?.on?.(type, () => { if (!busy) void refresh(); });
        }
        // Until a real request has been claimed, keep the macros that direct generations use up to date.
        globalThis.setInterval(() => {
            if (macroState.fromClaim || !getClient()) return;
            getClient().fetchInjection().then((data) => setMacros(data?.macros, { help: data?.macro_help })).catch(() => {});
        }, DEFAULT_MACRO_REFRESH_MS);
    }

    return { element: area, start, refresh };
}
