import assert from "node:assert/strict";
import test from "node:test";

import {
    BOOK_PREFIX,
    GLOBAL_ORDER_ID,
    MACRO_KEYS,
    applyToPreset,
    applyToWorldBook,
    bookAction,
    bookStatus,
    buildPrompt,
    cleanEntries,
    cleanMacros,
    isOurPrompt,
    macroHelpText,
    macroValue,
    newMacroKeys,
    presetStatus,
    removeFromPreset,
    removeFromWorldBook,
} from "../injection.mjs";

const entry = (id, over = {}) => ({
    id, title: `条目${id}`, enabled: true, content: `内容-${id}`, target: "preset",
    placement: "after_chat_history", depth: 0, role: "system", order: 10, ...over,
});

// a typical Chat Completion preset: the user's own prompts, a Chat History marker, the global order
const userPreset = () => ({
    prompts: [
        { identifier: "main", name: "Main", system_prompt: true, role: "system", content: "主提示" },
        { identifier: "chatHistory", name: "Chat History", system_prompt: true, marker: true },
        { identifier: "jailbreak", name: "Post-History", system_prompt: true, role: "system", content: "后置指令" },
        { identifier: "mine", name: "我的", role: "system", content: "x" },
    ],
    prompt_order: [
        { character_id: GLOBAL_ORDER_ID, order: [
            { identifier: "main", enabled: true },
            { identifier: "chatHistory", enabled: true },
            { identifier: "jailbreak", enabled: true },
            { identifier: "mine", enabled: false },
        ] },
    ],
});
const orderOf = (preset, index = 0) => preset.prompt_order[index].order.map((slot) => slot.identifier);
const userPart = (preset) => ({
    prompts: preset.prompts.filter((prompt) => !isOurPrompt(prompt.identifier)),
    order: preset.prompt_order.map((item) => item.order.filter((slot) => !isOurPrompt(slot.identifier))),
});

const entries = () => [
    entry("style", { order: 10 }),
    entry("emotion", { order: 20 }),
    entry("hint", { order: 30, target: "worldbook" }),
    entry("rule", { order: 40, target: "worldbook", role: "user", depth: 2 }),
];

test("cleanEntries keeps good entries in order and drops malformed ones", () => {
    const good = cleanEntries([
        entry("a"), { ...entry("b"), role: "tool", depth: -3, target: "x", placement: "y", title: "  " },
        null, entry("a"), { ...entry("Bad Id") }, { ...entry("c"), content: "  " }, { ...entry("d"), enabled: false },
    ]);
    assert.deepEqual(good.map((item) => item.id), ["a", "b", "d"]);
    assert.deepEqual([good[1].role, good[1].depth, good[1].target, good[1].placement, good[1].title], ["system", 0, "preset", "after_chat_history", "b"]);
    assert.equal(good[2].enabled, false);
    assert.deepEqual(cleanEntries("nope"), []);
    assert.deepEqual(cleanEntries(undefined), []);
});

test("first sync: preset entries go right after Chat History, in order, as relative prompts", () => {
    const preset = userPreset();
    const result = applyToPreset(preset, entries());
    assert.deepEqual(result.added, ["stbridge_style", "stbridge_emotion"]);
    assert.deepEqual(result.updated, []);
    assert.deepEqual(result.removed, []);
    assert.equal(result.noChatHistory, false);
    assert.deepEqual(orderOf(result), ["main", "chatHistory", "stbridge_style", "stbridge_emotion", "jailbreak", "mine"]);
    const prompt = result.prompts.find((item) => item.identifier === "stbridge_style");
    assert.deepEqual(
        [prompt.system_prompt, prompt.marker, prompt.role, prompt.content, prompt.enabled, prompt.injection_position],
        [false, false, "system", "内容-style", true, 0],
    );
    assert.deepEqual(result.prompts.slice(0, 4), preset.prompts, "the user's prompts keep their place");
    assert.deepEqual(result.prompts.slice(4).map((item) => item.identifier), ["stbridge_style", "stbridge_emotion"]);
    assert.ok(result.prompt_order[0].order.filter((slot) => isOurPrompt(slot.identifier)).every((slot) => slot.enabled === true));
    assert.deepEqual(preset, userPreset(), "the input is not changed");
});

test("the entries are sorted by order, then by their place in the list", () => {
    const result = applyToPreset(userPreset(), [entry("b", { order: 20 }), entry("a", { order: 10 }), entry("c", { order: 20 })]);
    assert.deepEqual(orderOf(result).filter(isOurPrompt), ["stbridge_a", "stbridge_b", "stbridge_c"]);
});

test("syncing again changes nothing (idempotent) and is reported as in sync", () => {
    const first = applyToPreset(userPreset(), entries());
    const preset = { prompts: first.prompts, prompt_order: first.prompt_order };
    const second = applyToPreset(preset, entries());
    assert.deepEqual(second.prompts, first.prompts);
    assert.deepEqual(second.prompt_order, first.prompt_order);
    assert.deepEqual([second.added, second.updated, second.removed], [[], [], []]);
    assert.deepEqual(presetStatus(preset, entries()), { total: 2, present: 2, matching: 2, stale: 0, ordered: true, inSync: true });
});

test("a fresh preset is reported as not injected", () => {
    assert.deepEqual(presetStatus(userPreset(), entries()), { total: 2, present: 0, matching: 0, stale: 0, ordered: false, inSync: false });
    // nothing wanted and nothing there is in sync
    assert.equal(presetStatus(userPreset(), [entry("w", { target: "worldbook" })]).inSync, true);
});

test("a changed text is an update; the new text is written and the status notices the old one", () => {
    const first = applyToPreset(userPreset(), entries());
    const preset = { prompts: first.prompts, prompt_order: first.prompt_order };
    const changed = entries();
    changed[0].content = "新的文风";
    assert.equal(presetStatus(preset, changed).inSync, false);
    assert.equal(presetStatus(preset, changed).matching, 1);
    const result = applyToPreset(preset, changed);
    assert.deepEqual([result.added, result.updated, result.removed], [[], ["stbridge_style"], []]);
    assert.equal(result.prompts.find((item) => item.identifier === "stbridge_style").content, "新的文风");
    assert.deepEqual(result.prompts.map((item) => item.identifier), first.prompts.map((item) => item.identifier), "same places");
    assert.equal(orderOf(result).filter(isOurPrompt).length, 2, "no duplicates in the order");
});

test("switching an entry off removes it from the prompts and from the order", () => {
    const first = applyToPreset(userPreset(), entries());
    const preset = { prompts: first.prompts, prompt_order: first.prompt_order };
    const off = entries();
    off[1].enabled = false;
    assert.equal(presetStatus(preset, off).stale, 1);
    const result = applyToPreset(preset, off);
    assert.deepEqual(result.removed, ["stbridge_emotion"]);
    assert.ok(!result.prompts.some((item) => item.identifier === "stbridge_emotion"));
    assert.deepEqual(orderOf(result), ["main", "chatHistory", "stbridge_style", "jailbreak", "mine"]);
});

test("moving an entry from the preset to the world book removes it from the preset", () => {
    const first = applyToPreset(userPreset(), entries());
    const preset = { prompts: first.prompts, prompt_order: first.prompt_order };
    const moved = entries();
    moved[1].target = "worldbook";
    const result = applyToPreset(preset, moved);
    assert.deepEqual(result.removed, ["stbridge_emotion"]);
    assert.deepEqual(orderOf(result).filter(isOurPrompt), ["stbridge_style"]);
    assert.deepEqual(bookEntriesOf(moved), ["emotion", "hint", "rule"]);
});

const bookEntriesOf = (list) => list.filter((item) => item.enabled && item.target === "worldbook").map((item) => item.id);

test("an entry deleted from the list is removed from the preset as well", () => {
    const first = applyToPreset(userPreset(), entries());
    const preset = { prompts: first.prompts, prompt_order: first.prompt_order };
    const result = applyToPreset(preset, [entry("style")]);
    assert.deepEqual(result.removed, ["stbridge_emotion"]);
    assert.equal(result.prompts.filter((item) => isOurPrompt(item.identifier)).length, 1);
});

test("the user's own prompts and their order are never touched", () => {
    const preset = userPreset();
    const before = userPart(preset);
    const result = applyToPreset(preset, entries());
    assert.deepEqual(userPart(result), before);
    const removed = removeFromPreset(result);
    assert.deepEqual(userPart(removed), before);
    assert.deepEqual(removed.prompts, preset.prompts);
    assert.deepEqual(removed.prompt_order, preset.prompt_order);
    assert.deepEqual(removed.removed, ["stbridge_style", "stbridge_emotion"]);
});

test("a depth entry becomes an absolute prompt with its depth, still listed in the order", () => {
    const result = applyToPreset(userPreset(), [entry("deep", { placement: "depth", depth: 3, role: "user" })]);
    const prompt = result.prompts.find((item) => item.identifier === "stbridge_deep");
    assert.deepEqual([prompt.injection_position, prompt.injection_depth, prompt.role], [1, 3, "user"]);
    assert.ok(orderOf(result).includes("stbridge_deep"));
});

test("with a global order and per-character orders only the global one is written", () => {
    const preset = userPreset();
    preset.prompt_order.push({ character_id: 5, order: [{ identifier: "main", enabled: true }, { identifier: "chatHistory", enabled: true }] });
    const result = applyToPreset(preset, entries());
    assert.deepEqual(orderOf(result, 0).filter(isOurPrompt), ["stbridge_style", "stbridge_emotion"]);
    assert.deepEqual(orderOf(result, 1), ["main", "chatHistory"]);
});

test("with only per-character orders every list gets the entries", () => {
    const preset = userPreset();
    preset.prompt_order = [
        { character_id: 5, order: [{ identifier: "main", enabled: true }, { identifier: "chatHistory", enabled: true }] },
        { character_id: 6, order: [{ identifier: "chatHistory", enabled: true }, { identifier: "main", enabled: true }] },
    ];
    const result = applyToPreset(preset, entries());
    assert.deepEqual(orderOf(result, 0), ["main", "chatHistory", "stbridge_style", "stbridge_emotion"]);
    assert.deepEqual(orderOf(result, 1), ["chatHistory", "stbridge_style", "stbridge_emotion", "main"]);
    // removing cleans every list, whichever it was written to
    const removed = removeFromPreset(result);
    assert.deepEqual(removed.prompt_order, preset.prompt_order);
});

test("without a Chat History marker the entries go to the end of the order", () => {
    const preset = userPreset();
    preset.prompt_order[0].order = preset.prompt_order[0].order.filter((slot) => slot.identifier !== "chatHistory");
    const result = applyToPreset(preset, entries());
    assert.equal(result.noChatHistory, true);
    assert.deepEqual(orderOf(result), ["main", "jailbreak", "mine", "stbridge_style", "stbridge_emotion"]);
});

test("a preset without any order gets a global one holding just our entries", () => {
    const result = applyToPreset({ prompts: [] }, entries());
    assert.equal(result.prompt_order[0].character_id, GLOBAL_ORDER_ID);
    assert.deepEqual(orderOf(result), ["stbridge_style", "stbridge_emotion"]);
    assert.deepEqual(applyToPreset(undefined, []), { prompts: [], prompt_order: [], added: [], updated: [], removed: [], noChatHistory: false, count: 0 });
});

test("an entry the user switched off in the Prompt Manager makes the preset out of sync", () => {
    const first = applyToPreset(userPreset(), entries());
    const preset = { prompts: first.prompts, prompt_order: first.prompt_order };
    preset.prompt_order[0].order.find((slot) => slot.identifier === "stbridge_style").enabled = false;
    assert.equal(presetStatus(preset, entries()).inSync, false);
    assert.equal(presetStatus(preset, entries()).ordered, false);
});

// ---------- world book ----------

const template = { uid: 0, key: ["x"], comment: "", content: "", constant: false, disable: true, order: 100, position: 0, depth: 4, role: 0, selective: true, probability: 50, extra: { a: 1 } };
const mine = (uid, comment = "我的条目") => ({ ...template, uid, comment, content: "用户内容", key: ["k"] });

test("first sync creates the entries: constant, at depth, with role, order and the mark", () => {
    const result = applyToWorldBook({ entries: {} }, entries(), template);
    assert.deepEqual([result.added, result.updated, result.removed, result.foreign, result.count], [["hint", "rule"], [], [], 0, 2]);
    const list = Object.values(result.data.entries);
    assert.deepEqual(list.map((item) => item.comment), [`${BOOK_PREFIX}hint`, `${BOOK_PREFIX}rule`]);
    const rule = list[1];
    assert.deepEqual(
        [rule.constant, rule.position, rule.depth, rule.role, rule.order, rule.disable, rule.content, rule.probability],
        [true, 4, 2, 1, 40, false, "内容-rule", 100],
    );
    assert.deepEqual(rule.key, []);
    assert.deepEqual(rule.extra, { a: 1 }, "the template's other fields are kept");
    assert.deepEqual(Object.keys(result.data.entries).map(Number), list.map((item) => item.uid));
    assert.notEqual(rule.extra, template.extra, "template objects are copies");
});

test("syncing the world book again is idempotent and keeps the uids", () => {
    const first = applyToWorldBook({ entries: {} }, entries(), template);
    const second = applyToWorldBook(first.data, entries(), template);
    assert.deepEqual(second.data, first.data);
    assert.deepEqual([second.added, second.updated, second.removed], [[], [], []]);
    assert.equal(bookStatus(first.data, entries(), true).inSync, true);
    assert.equal(bookStatus(first.data, entries(), false).inSync, false, "not bound globally");
});

test("a changed text, a switched-off entry and an entry moved to the preset are all handled", () => {
    const first = applyToWorldBook({ entries: {} }, entries(), template);
    const changed = entries();
    changed[2].content = "新内容";
    assert.deepEqual(applyToWorldBook(first.data, changed, template).updated, ["hint"]);
    assert.equal(bookStatus(first.data, changed, true).matching, 1);
    const off = entries();
    off[3].enabled = false;
    assert.deepEqual(applyToWorldBook(first.data, off, template).removed, ["rule"]);
    assert.equal(bookStatus(first.data, off, true).stale, 1);
    const moved = entries();
    moved[2].target = "preset";
    const result = applyToWorldBook(first.data, moved, template);
    assert.deepEqual(result.removed, ["hint"]);
    assert.deepEqual(Object.values(result.data.entries).map((item) => item.comment), [`${BOOK_PREFIX}rule`]);
});

test("entries of the user in the same book are never touched, and uids never collide", () => {
    const data = { entries: { 0: mine(0), 1: mine(1, `${BOOK_PREFIX}hint`), 2: mine(2) }, other: "kept" };
    const result = applyToWorldBook(data, entries(), template);
    assert.equal(result.foreign, 2);
    assert.deepEqual(result.data.entries[0], data.entries[0]);
    assert.deepEqual(result.data.entries[2], data.entries[2]);
    assert.equal(result.data.other, "kept");
    const ours = Object.values(result.data.entries).filter((item) => item.comment.startsWith(BOOK_PREFIX));
    assert.deepEqual(ours.map((item) => item.comment).sort(), [`${BOOK_PREFIX}hint`, `${BOOK_PREFIX}rule`]);
    assert.equal(new Set(Object.values(result.data.entries).map((item) => item.uid)).size, 4);
    assert.equal(result.data.entries[1].comment, `${BOOK_PREFIX}hint`, "its own uid is reused");
    // a user entry that now sits on the uid an old mark had is not overwritten
    const clash = applyToWorldBook({ entries: { 5: { ...mine(5, `${BOOK_PREFIX}hint`), uid: 9 }, 9: mine(9) } }, entries(), template);
    assert.equal(clash.data.entries[9].comment, "我的条目");
    assert.equal(Object.values(clash.data.entries).length, 3);
});

test("removing takes only the marked entries; the book is deleted only when nothing else is in it", () => {
    const both = applyToWorldBook({ entries: { 0: mine(0) } }, entries(), template);
    const removed = removeFromWorldBook(both.data);
    assert.deepEqual(removed.data.entries, { 0: mine(0) });
    assert.equal(removed.foreign, 1);
    assert.equal(bookAction(both.data, []), "strip");
    const onlyOurs = applyToWorldBook({ entries: {} }, entries(), template);
    assert.equal(bookAction(onlyOurs.data, []), "delete");
    assert.equal(bookAction(onlyOurs.data, entries()), "write");
    assert.equal(bookAction(null, []), "none");
    assert.equal(bookAction({ entries: {} }, []), "delete");
    assert.equal(bookAction(null, entries()), "write");
    assert.deepEqual(removeFromWorldBook(onlyOurs.data).data.entries, {});
});

test("a missing book is reported as missing; nothing wanted counts as in sync", () => {
    assert.deepEqual(bookStatus(null, entries(), false), { exists: false, bound: false, total: 2, matching: 0, stale: 0, foreign: 0, inSync: false });
    assert.equal(bookStatus(null, [entry("p")], false).inSync, true);
    assert.equal(bookStatus({ entries: { 0: mine(0) } }, [entry("p")], false).foreign, 1);
    assert.equal(bookStatus({ entries: {} }, entries(), true).inSync, false);
});

test("malformed data does not throw", () => {
    assert.doesNotThrow(() => applyToWorldBook(null, entries(), template));
    assert.doesNotThrow(() => applyToWorldBook({ entries: [] }, entries(), template));
    assert.doesNotThrow(() => applyToWorldBook({ entries: { 0: null, 1: 5 } }, entries(), template));
    assert.doesNotThrow(() => applyToPreset({ prompts: [null, 5, { identifier: 7 }], prompt_order: [null, { order: "x" }] }, entries()));
    assert.doesNotThrow(() => presetStatus({}, entries()));
    assert.doesNotThrow(() => removeFromPreset(null));
});

// ---------- macros ----------

test("macro values: the latest claim's strings; speaker falls back to the user's name", () => {
    const macros = { emotions: "开心、害羞", speaker: "", song_allowed: "否" };
    assert.equal(macroValue(macros, "emotions", "我"), "开心、害羞");
    assert.equal(macroValue(macros, "speaker", "制作人"), "制作人");
    assert.equal(macroValue({ speaker: "小明（1）" }, "speaker", "制作人"), "小明（1）");
    assert.equal(macroValue(macros, "affinity", "制作人"), "");
    assert.equal(macroValue(null, "emotions"), "");
    assert.equal(macroValue(null, "speaker", undefined), "");
    // any valid key name is kept (the core may add keys later); bad names and non-strings are dropped
    assert.deepEqual(cleanMacros({ emotions: "a", bogus: "b", affinity: 5 }), { emotions: "a", bogus: "b" });
    assert.deepEqual(cleanMacros({ "Bad-Key": "x", "9x": "y", _x: "z", ok_1: "fine", [`a${"b".repeat(32)}`]: "long" }), { ok_1: "fine" });
    assert.equal(cleanMacros("x"), null);
    assert.equal(cleanMacros([1]), null);
    assert.equal(MACRO_KEYS.length, 12);
    assert.ok(MACRO_KEYS.includes("mood") && MACRO_KEYS.includes("owner_mood"));
});

test("new macro keys: only valid, not yet registered ones, in the core's order; help falls back to a generic line", () => {
    const registered = new Set(["emotions", "mood"]);
    assert.deepEqual(newMacroKeys({ emotions: "a", mood: "b", extra: "c", Bad: "d", more_1: "e" }, registered), ["extra", "more_1"]);
    assert.deepEqual(newMacroKeys(null, registered), []);
    assert.equal(macroHelpText({ extra: " 说明 " }, "extra"), "说明");
    assert.equal(macroHelpText({ extra: "" }, "extra"), "STbridge 提供的宏");
    assert.equal(macroHelpText(null, "extra"), "STbridge 提供的宏");
    assert.equal(macroHelpText({}, "extra", "我自己的"), "我自己的");
});

test("buildPrompt carries the identifier, the readable name and the entry's role", () => {
    const prompt = buildPrompt(entry("x", { role: "assistant", title: "标题" }));
    assert.deepEqual([prompt.identifier, prompt.name, prompt.role], ["stbridge_x", "STbridge：标题", "assistant"]);
    assert.ok(isOurPrompt(prompt.identifier));
    assert.ok(!isOurPrompt("main"));
    assert.ok(!isOurPrompt(undefined));
});
