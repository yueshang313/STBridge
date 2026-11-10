// R5b: pure helpers that put STbridge's rules into an ST Chat Completion preset and a world book.
//
// Nothing here touches ST: every function takes the data structures ST uses (oai_settings, a world book
// file) and returns new ones, so node can test them. index.js does the ST side (read, back up, save).
//
// Only what carries a STbridge mark is ever touched:
//   preset prompts      identifier "stbridge_<id>"
//   world book entries  comment    "STbridge:<id>"

export const PROMPT_PREFIX = "stbridge_";
export const BOOK_PREFIX = "STbridge:";
export const GLOBAL_ORDER_ID = 100001; // ST's dummy character id of the global prompt order
export const CHAT_HISTORY = "chatHistory";
export const WORLD_INFO_AT_DEPTH = 4;
export const INJECTION_POSITION = { relative: 0, absolute: 1 };
const ROLE_CODE = { system: 0, user: 1, assistant: 2 };

export const MACRO_KEYS = [
    "emotions", "group_tag", "speaker", "affinity", "affinity_tier", "affinity_hint",
    "affinity_var", "affinity_range", "affinity_step", "song_allowed", "mood", "owner_mood",
];
// The fallback list above is always registered; any other key the core hands over is registered on first sight.
export const MACRO_KEY_RE = /^[a-z][a-z0-9_]{0,31}$/;
export const GENERIC_MACRO_HELP = "STbridge 提供的宏";

const isObject = (value) => value !== null && typeof value === "object" && !Array.isArray(value);
const clone = (value) => JSON.parse(JSON.stringify(value));

// ---------- the entry list from the core ----------

/** Entries as the core serves them (/v1/injection); anything malformed is dropped, the order is kept. */
export function cleanEntries(list) {
    if (!Array.isArray(list)) return [];
    const seen = new Set();
    const result = [];
    for (const entry of list) {
        if (!isObject(entry) || typeof entry.id !== "string" || !/^[a-z0-9][a-z0-9_]{0,29}$/.test(entry.id) || seen.has(entry.id)) continue;
        if (typeof entry.content !== "string" || !entry.content.trim()) continue;
        seen.add(entry.id);
        result.push({
            id: entry.id,
            title: typeof entry.title === "string" && entry.title.trim() ? entry.title.trim() : entry.id,
            enabled: entry.enabled !== false,
            content: entry.content,
            target: entry.target === "worldbook" ? "worldbook" : "preset",
            placement: entry.placement === "depth" ? "depth" : "after_chat_history",
            depth: Number.isInteger(entry.depth) && entry.depth >= 0 ? entry.depth : 0,
            role: Object.hasOwn(ROLE_CODE, entry.role) ? entry.role : "system",
            order: Number.isFinite(entry.order) ? entry.order : 0,
        });
    }
    return result;
}

const byOrder = (entries) => entries
    .map((entry, index) => [entry, index])
    .sort((a, b) => a[0].order - b[0].order || a[1] - b[1])
    .map(([entry]) => entry);

export const presetEntries = (entries) => byOrder(entries.filter((entry) => entry.enabled && entry.target === "preset"));
export const bookEntries = (entries) => byOrder(entries.filter((entry) => entry.enabled && entry.target === "worldbook"));

// ---------- preset ----------

export const promptIdentifier = (id) => `${PROMPT_PREFIX}${id}`;
export const isOurPrompt = (identifier) => typeof identifier === "string" && identifier.startsWith(PROMPT_PREFIX);

/** The prompt object ST's Prompt Manager stores for one entry. */
export function buildPrompt(entry) {
    return {
        identifier: promptIdentifier(entry.id),
        name: `STbridge：${entry.title}`,
        system_prompt: false,
        marker: false,
        role: entry.role,
        content: entry.content,
        enabled: true,
        injection_position: entry.placement === "depth" ? INJECTION_POSITION.absolute : INJECTION_POSITION.relative,
        injection_depth: entry.depth,
        injection_order: 100,
        injection_trigger: [],
        forbid_overrides: false,
    };
}

function samePrompt(current, wanted) {
    return isObject(current)
        && ["name", "role", "content", "injection_position", "injection_depth"].every((key) => current[key] === wanted[key])
        && current.enabled !== false
        && current.marker !== true;
}

/** Which prompt_order lists get our entries: the global one, else every list. */
function targetOrderIndexes(promptOrder) {
    const global = [];
    promptOrder.forEach((item, index) => {
        if (isObject(item) && item.character_id === GLOBAL_ORDER_ID) global.push(index);
    });
    return global.length ? global : promptOrder.map((_, index) => index);
}

function strippedOrder(promptOrder) {
    const list = Array.isArray(promptOrder) ? clone(promptOrder) : [];
    for (const item of list) {
        if (isObject(item) && Array.isArray(item.order)) item.order = item.order.filter((slot) => !isOurPrompt(slot?.identifier));
    }
    return list;
}

/**
 * The preset's prompts / prompt_order with our entries written in. Our prompts keep their place in `prompts`
 * (new ones go to the end); in the order list they form one block right after Chat History, or at the end
 * when the list has no Chat History. Entries that are off, moved to the world book or gone are removed.
 */
export function applyToPreset(oai, entries) {
    const wanted = presetEntries(entries).map(buildPrompt);
    const current = Array.isArray(oai?.prompts) ? oai.prompts : [];
    const ours = new Map(current.filter((prompt) => isOurPrompt(prompt?.identifier)).map((prompt) => [prompt.identifier, prompt]));
    const wantedIds = new Set(wanted.map((prompt) => prompt.identifier));

    const added = wanted.filter((prompt) => !ours.has(prompt.identifier)).map((prompt) => prompt.identifier);
    const updated = wanted.filter((prompt) => ours.has(prompt.identifier) && !samePrompt(ours.get(prompt.identifier), prompt)).map((prompt) => prompt.identifier);
    const removed = [...ours.keys()].filter((identifier) => !wantedIds.has(identifier));

    const placed = new Set();
    const prompts = [];
    for (const prompt of current) {
        if (!isOurPrompt(prompt?.identifier)) { prompts.push(prompt); continue; }
        const replacement = wanted.find((item) => item.identifier === prompt.identifier);
        if (replacement && !placed.has(replacement.identifier)) { prompts.push(replacement); placed.add(replacement.identifier); }
    }
    for (const prompt of wanted) if (!placed.has(prompt.identifier)) prompts.push(prompt);

    const promptOrder = strippedOrder(oai?.prompt_order);
    let noChatHistory = false;
    if (wanted.length) {
        if (!promptOrder.length) promptOrder.push({ character_id: GLOBAL_ORDER_ID, order: [] });
        for (const index of targetOrderIndexes(promptOrder)) {
            const item = promptOrder[index];
            if (!isObject(item)) continue;
            if (!Array.isArray(item.order)) item.order = [];
            const at = item.order.findIndex((slot) => slot?.identifier === CHAT_HISTORY);
            if (at < 0) noChatHistory = true;
            item.order.splice(at < 0 ? item.order.length : at + 1, 0, ...wanted.map((prompt) => ({ identifier: prompt.identifier, enabled: true })));
        }
    }
    return { prompts, prompt_order: promptOrder, added, updated, removed, noChatHistory, count: wanted.length };
}

/** The preset without any of our prompts. */
export function removeFromPreset(oai) {
    const current = Array.isArray(oai?.prompts) ? oai.prompts : [];
    return {
        prompts: current.filter((prompt) => !isOurPrompt(prompt?.identifier)),
        prompt_order: strippedOrder(oai?.prompt_order),
        removed: current.filter((prompt) => isOurPrompt(prompt?.identifier)).map((prompt) => prompt.identifier),
    };
}

const orderSignature = (promptOrder) => (Array.isArray(promptOrder) ? promptOrder : []).map((item) => (
    isObject(item) && Array.isArray(item.order)
        ? item.order.map((slot) => (isOurPrompt(slot?.identifier) ? `${slot.identifier}${slot.enabled === false ? "-" : "+"}` : String(slot?.identifier))).join("|")
        : ""
));

/** How the preset compares with the entry list: counts and whether it is in sync. */
export function presetStatus(oai, entries) {
    const wanted = presetEntries(entries).map(buildPrompt);
    const current = Array.isArray(oai?.prompts) ? oai.prompts : [];
    const ours = current.filter((prompt) => isOurPrompt(prompt?.identifier));
    const byId = new Map(ours.map((prompt) => [prompt.identifier, prompt]));
    const matching = wanted.filter((prompt) => samePrompt(byId.get(prompt.identifier), prompt)).length;
    const stale = ours.filter((prompt) => !wanted.some((item) => item.identifier === prompt.identifier)).length;
    const after = applyToPreset(oai, entries);
    const ordered = JSON.stringify(orderSignature(after.prompt_order)) === JSON.stringify(orderSignature(oai?.prompt_order));
    return {
        total: wanted.length,
        present: ours.length - stale,
        matching,
        stale,
        ordered,
        inSync: matching === wanted.length && stale === 0 && ordered,
    };
}

// ---------- world book ----------

const isOurBookEntry = (entry) => isObject(entry) && typeof entry.comment === "string" && entry.comment.startsWith(BOOK_PREFIX);

/** A free uid, never one of `used`. */
function freeUid(used, start = 0) {
    let uid = start;
    while (used.has(uid)) uid += 1;
    return uid;
}

/**
 * The world book data with our entries rewritten: every entry marked "STbridge:<id>" is replaced by the
 * wanted ones (same uid when it already existed), everything else stays. `template` is ST's default entry.
 */
export function applyToWorldBook(data, entries, template = {}) {
    const wanted = bookEntries(entries);
    const source = isObject(data) ? data : {};
    const old = isObject(source.entries) ? source.entries : {};
    const kept = {};
    const marked = new Map();
    let foreign = 0;
    for (const [key, entry] of Object.entries(old)) {
        if (isOurBookEntry(entry)) marked.set(entry.comment.slice(BOOK_PREFIX.length), { key, entry });
        else { kept[key] = entry; foreign += 1; }
    }
    const used = new Set(Object.keys(kept).map(Number));
    const rewritten = {};
    const added = [];
    const updated = [];
    for (const entry of wanted) {
        const before = marked.get(entry.id);
        const previous = before ? Number(before.entry.uid ?? before.key) : NaN;
        const uid = Number.isInteger(previous) && previous >= 0 && !used.has(previous) ? previous : freeUid(used);
        used.add(uid);
        const built = {
            ...clone(template),
            uid,
            key: [],
            keysecondary: [],
            comment: `${BOOK_PREFIX}${entry.id}`,
            content: entry.content,
            constant: true,
            disable: false,
            order: entry.order,
            position: WORLD_INFO_AT_DEPTH,
            depth: entry.depth,
            role: ROLE_CODE[entry.role],
            probability: 100,
            useProbability: true,
        };
        rewritten[uid] = built;
        if (!before) added.push(entry.id);
        else if (!sameBookEntry(before.entry, built)) updated.push(entry.id);
    }
    const removed = [...marked.keys()].filter((id) => !wanted.some((entry) => entry.id === id));
    return { data: { ...source, entries: { ...kept, ...rewritten } }, count: wanted.length, foreign, added, updated, removed };
}

/** The world book data without our entries; `foreign` counts what is left. */
export function removeFromWorldBook(data) {
    const result = applyToWorldBook(data, [], {});
    return { data: result.data, foreign: result.foreign, removed: result.removed };
}

function sameBookEntry(current, wanted) {
    return ["content", "constant", "disable", "order", "position", "depth", "role"].every((key) => current[key] === wanted[key]);
}

/** How the world book compares with the entry list. `data` is null when the book does not exist. */
export function bookStatus(data, entries, bound) {
    const wanted = bookEntries(entries);
    if (!isObject(data)) {
        return { exists: false, bound: Boolean(bound), total: wanted.length, matching: 0, stale: 0, foreign: 0, inSync: wanted.length === 0 };
    }
    const all = isObject(data.entries) ? Object.values(data.entries) : [];
    const ours = all.filter(isOurBookEntry);
    const byId = new Map(ours.map((entry) => [entry.comment.slice(BOOK_PREFIX.length), entry]));
    const rebuilt = applyToWorldBook(data, entries, {});
    const matching = wanted.filter((entry) => {
        const found = byId.get(entry.id);
        return found && sameBookEntry(found, Object.values(rebuilt.data.entries).find((item) => item.comment === `${BOOK_PREFIX}${entry.id}`));
    }).length;
    const stale = ours.filter((entry) => !wanted.some((item) => `${BOOK_PREFIX}${item.id}` === entry.comment)).length;
    return {
        exists: true,
        bound: Boolean(bound),
        total: wanted.length,
        matching,
        stale,
        foreign: all.length - ours.length,
        inSync: matching === wanted.length && stale === 0 && (wanted.length === 0 || Boolean(bound)),
    };
}

/** What the sync does with the book: write it, delete it (nothing wanted and nothing of the user's in it) or leave it. */
export function bookAction(data, entries) {
    const wanted = bookEntries(entries).length;
    if (wanted > 0) return "write";
    if (!isObject(data)) return "none";
    const all = isObject(data.entries) ? Object.values(data.entries) : [];
    return all.every(isOurBookEntry) ? "delete" : "strip";
}

// ---------- macros ----------

/** The value of one `{{stbridge_<key>}}` macro: the latest claim's value; the owner's name fills "speaker" when empty. */
export function macroValue(macros, key, userName = "") {
    const value = isObject(macros) && typeof macros[key] === "string" ? macros[key] : "";
    if (key === "speaker" && !value) return typeof userName === "string" ? userName : "";
    return value;
}

/** Keep the entries with a valid key name and a string value (any such key, not only the fallback list). */
export function cleanMacros(macros) {
    if (!isObject(macros)) return null;
    const result = {};
    for (const [key, value] of Object.entries(macros)) {
        if (MACRO_KEY_RE.test(key) && typeof value === "string") result[key] = value;
    }
    return result;
}

/** The keys of cleaned macros that are not registered yet, in the core's order. */
export function newMacroKeys(macros, registered) {
    return isObject(macros) ? Object.keys(macros).filter((key) => MACRO_KEY_RE.test(key) && !registered.has(key)) : [];
}

/** The help text for a macro: the core's own description when it gave one, else a generic line. */
export function macroHelpText(help, key, fallback = GENERIC_MACRO_HELP) {
    return isObject(help) && typeof help[key] === "string" && help[key].trim() ? help[key].trim() : fallback;
}
