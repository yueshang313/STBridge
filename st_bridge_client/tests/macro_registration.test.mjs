// L1c: the {{stbridge_*}} macros are registered from the fallback list, and keys the core adds later get registered too.
import assert from "node:assert/strict";
import test from "node:test";

import { MACRO_KEYS } from "../injection.mjs";
import { registerMacros, setMacros } from "../injection_panel.js";

function makeContext() {
    const registered = [];
    const context = { name1: "制作人", registerMacro: (name, handler, help) => registered.push({ name, handler, help }) };
    return { context, registered };
}

// The module keeps its state for the page's lifetime, so these steps run in order on one module instance.
test("the fallback list is registered even when the core is offline, once each", () => {
    const { context, registered } = makeContext();
    assert.equal(registerMacros(() => context), true);
    assert.deepEqual(registered.map((item) => item.name), MACRO_KEYS.map((key) => `stbridge_${key}`));
    assert.ok(registered.some((item) => item.name === "stbridge_mood") && registered.some((item) => item.name === "stbridge_owner_mood"));
    registerMacros(() => context);
    assert.equal(registered.length, MACRO_KEYS.length, "registering again adds nothing");
    globalThis.__ctx = { context, registered };
});

test("a key the core adds later is registered on first sight, with the core's help text, and read live", () => {
    const { context, registered } = globalThis.__ctx;
    const before = registered.length;
    setMacros({ emotions: "开心", mood: "m", fresh_key: "v1" }, { help: { fresh_key: "新宏的说明" } });
    assert.equal(registered.length, before + 1);
    const added = registered.at(-1);
    assert.equal(added.name, "stbridge_fresh_key");
    assert.equal(added.help, "新宏的说明");
    assert.equal(added.handler(), "v1");
    setMacros({ fresh_key: "v2" }, { fromClaim: true });
    assert.equal(registered.length, before + 1, "not registered twice");
    assert.equal(added.handler(), "v2", "the handler follows the latest values");
});

test("a key without help text gets the generic line; invalid key names are ignored", () => {
    const { registered } = globalThis.__ctx;
    const before = registered.length;
    setMacros({ plain_key: "x", "Bad-Key": "y", "9bad": "z", valid2: 5 }, { fromClaim: true });
    const names = registered.slice(before).map((item) => item.name);
    assert.deepEqual(names, ["stbridge_plain_key"]);
    assert.equal(registered.at(-1).help, "STbridge 提供的宏");
});

test("with the newer macro API only, new keys register through it as well", () => {
    const registered = [];
    const context = { name1: "x", macros: { register: (name, options) => registered.push({ name, options }) } };
    // a fresh registration pass on the same module: nothing new, the fallback keys are already known
    registerMacros(() => context);
    setMacros({ api_two: "v" }, { fromClaim: true, help: { api_two: "二号" } });
    assert.deepEqual(registered.map((item) => item.name), ["stbridge_api_two"]);
    assert.equal(registered[0].options.description, "二号");
    assert.equal(registered[0].options.handler(), "v");
});
