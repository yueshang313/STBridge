import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import test from "node:test";

import {
    BRIDGE_URL,
    BridgeClient,
    BridgeClientError,
    DEFAULT_BRIDGE_PORT,
    bridgeUrl,
    validBridgePort,
    injectAffinity,
    injectAffinityWrites,
    isValidAffinityWrites,
    runAffinityDiagnostic,
} from "../bridge_client.mjs";
import {
    runStartButtonAction,
    statusStateForError,
    statusTextForState,
} from "../status_messages.mjs";

// Marker used to check that private details never leak into statuses or diagnostics.
const SECRET = "private-detail-0123456789-abcdefghijklmnopqrstuvwxyz";
const EVENT_TYPES = {
    APP_READY: "app_ready",
    CHAT_CHANGED: "chat_changed",
    CHAT_LOADED: "chat_loaded",
    CHARACTER_PAGE_LOADED: "character_page_loaded",
    GENERATION_STARTED: "generation_started",
    GENERATION_STOPPED: "generation_stopped",
    GENERATION_ENDED: "generation_ended",
};

function httpResponse(status, payload = null, headers = {}) {
    return {
        status,
        ok: status >= 200 && status < 300,
        headers: { get: (name) => headers[name.toLowerCase()] ?? null },
        json: async () => payload,
    };
}

function createMockBridge({
    recovered: initiallyRecovered = true,
    claimResponseLosses = 0,
    claimResponseId = null,
    resultResponseLosses = 0,
} = {}) {
    const jobs = new Map();
    const calls = [];
    let recovered = initiallyRecovered;
    let pendingId = null;
    let generationGuardId = null;
    let remainingClaimResponseLosses = claimResponseLosses;
    let remainingResultResponseLosses = resultResponseLosses;

    const injection = { payload: { world_book_name: "STbridge", entries: [], macros: { emotions: "开心" } }, backups: [], failBackup: false };

    function addRequest(requestId, content, affinityWrites = null, kind = null, macros = null) {
        if (pendingId !== null || generationGuardId !== null) {
            throw new Error("mock bridge busy");
        }
        jobs.set(requestId, {
            request_id: requestId,
            content,
            status: "PENDING",
            generation_status: "NOT_STARTED",
            claim_attempt_id: null,
            reply: null,
            affinityWrites,
            kind,
            macros,
        });
        pendingId = requestId;
    }

    async function fetchImpl(url, options = {}) {
        const parsed = new URL(url);
        const path = parsed.pathname;
        const method = options.method ?? "GET";
        const body = options.body === undefined ? null : JSON.parse(options.body);
        calls.push({ url: String(url), path, method, headers: options.headers, body });

        if (path === "/v1/session/ready" && method === "POST") {
            if (!recovered && body?.generating === false && body?.active_request === false) {
                recovered = true;
                return httpResponse(200, { state: "RECOVERED" });
            }
            return recovered
                ? httpResponse(200, { state: "RECOVERED" })
                : httpResponse(200, { state: "RECOVERY_REQUIRED", reason: "client_busy" });
        }
        if (path === "/v1/injection" && method === "GET") {
            return httpResponse(200, injection.payload);
        }
        if (path === "/v1/injection/backup" && method === "POST") {
            if (injection.failBackup) return httpResponse(500, { error: "backup_failed" });
            injection.backups.push(body);
            return httpResponse(200, { file: `${body.name}-20261106-120000.json` });
        }
        if (!recovered && path !== "/v1/health") {
            return httpResponse(503, { error: "recovery_required" });
        }

        if (path === "/v1/requests/pending" && method === "GET") {
            const pending = pendingId && jobs.get(pendingId)?.status === "PENDING"
                ? pendingId
                : null;
            return pending
                ? httpResponse(200, { request_id: pending })
                : httpResponse(204);
        }

        const match = path.match(/^\/v1\/requests\/([^/]+)(?:\/(claim|generation|result|reject|failure))?$/);
        if (!match) {
            return httpResponse(404, { error: "request_not_found" });
        }

        const requestId = decodeURIComponent(match[1]);
        const action = match[2] ?? "status";
        const job = jobs.get(requestId);
        if (!job) {
            return httpResponse(404, { error: "request_not_found" });
        }

        if (action === "claim" && method === "POST") {
            const attemptId = body?.claim_attempt_id;
            if (job.status === "PENDING") {
                job.status = "CLAIMED";
                job.generation_status = "UNKNOWN";
                job.claim_attempt_id = attemptId;
                generationGuardId = requestId;
                pendingId = null;
            } else if (job.status !== "CLAIMED" || job.claim_attempt_id !== attemptId) {
                return httpResponse(409, { error: "invalid_request_state" });
            }

            if (remainingClaimResponseLosses > 0) {
                remainingClaimResponseLosses -= 1;
                throw new TypeError("mock lost claim response");
            }
            return httpResponse(200, {
                request_id: claimResponseId ?? requestId,
                status: "CLAIMED",
                content: job.content,
                ...(job.affinityWrites === null ? {} : { affinity_writes: job.affinityWrites }),
                ...(job.kind === null ? {} : { kind: job.kind }),
                ...(job.macros === null ? {} : { macros: job.macros }),
            });
        }

        if (action === "status" && method === "GET") {
            return httpResponse(200, {
                request_id: requestId,
                status: job.status,
                generation_status: job.generation_status,
            });
        }

        if (action === "generation" && method === "POST") {
            const state = body?.state;
            if (state === "RUNNING" && job.generation_status === "UNKNOWN") {
                job.generation_status = "RUNNING";
            } else if (state === "NOT_STARTED" && job.generation_status === "UNKNOWN") {
                job.generation_status = "NOT_STARTED";
                generationGuardId = null;
            } else if (state === "FINISHED"
                && ["UNKNOWN", "RUNNING"].includes(job.generation_status)) {
                job.generation_status = "FINISHED";
                generationGuardId = null;
            } else if (state !== job.generation_status) {
                return httpResponse(409, { error: "invalid_request_state" });
            }
            return httpResponse(200, {
                request_id: requestId,
                status: job.status,
                generation_status: job.generation_status,
            });
        }

        if (action === "result" && method === "POST") {
            if (job.status !== "CLAIMED" || job.generation_status !== "FINISHED") {
                return httpResponse(409, { error: "generation_not_confirmed" });
            }
            job.reply = body?.reply;
            job.status = "SUCCEEDED";
            generationGuardId = null;
            if (remainingResultResponseLosses > 0) {
                remainingResultResponseLosses -= 1;
                throw new TypeError("mock lost result response");
            }
            return httpResponse(200, {
                request_id: requestId,
                status: job.status,
                generation_status: job.generation_status,
            });
        }

        if (["reject", "failure"].includes(action) && method === "POST") {
            job.status = action === "reject" ? "REJECTED" : "FAILED";
            generationGuardId = null;
            return httpResponse(200, {
                request_id: requestId,
                status: job.status,
                generation_status: job.generation_status,
            });
        }

        return httpResponse(404, { error: "route_not_found" });
    }

    // Simulates an AstrBot plugin reload: a fresh instance with no requests and a closed gate.
    function reload({ recoveredAfterReload = false } = {}) {
        jobs.clear();
        pendingId = null;
        generationGuardId = null;
        recovered = recoveredAfterReload;
    }

    return {
        jobs,
        calls,
        addRequest,
        reload,
        fetchImpl,
        injection,
        get generationGuardId() { return generationGuardId; },
        get recovered() { return recovered; },
        set recovered(value) { recovered = value; },
    };
}

function createHarness({
    bridge = createMockBridge(),
    requestTimeoutMs = 100,
    generationTimeoutMs = 1000,
    randomId = () => "claim-attempt-test-1",
    generate,
    getContext,
    initialGroupId = null,
    setTimeoutImpl,
    clearTimeoutImpl,
    onStatus,
    onDiagnostic,
    settingsStore = null,
    bind = true,
} = {}) {
    const eventSource = new EventEmitter();
    const diagnostics = [];
    const context = {
        characterId: "character-7",
        groupId: initialGroupId,
        chatId: "chat-2026-10-29",
        chat: [],
        eventSource,
        eventTypes: EVENT_TYPES,
    };
    let generating = false;
    const appended = [];
    const statuses = [];
    const client = new BridgeClient({
        fetchImpl: bridge.fetchImpl,
        getContext: getContext ?? (() => context),
        isGenerating: () => generating,
        sendMessageAsUser: async (text) => {
            const message = { is_user: true, is_system: false, mes: text };
            context.chat.push(message);
            appended.push(message);
            return message;
        },
        generate: generate ?? (async (type) => {
            generating = true;
            eventSource.emit(EVENT_TYPES.GENERATION_STARTED, type);
            await Promise.resolve();
            context.chat.push({ is_user: false, is_system: false, mes: "native reply" });
            generating = false;
            eventSource.emit(EVENT_TYPES.GENERATION_ENDED, context.chat.length);
            return "native reply";
        }),
        requestTimeoutMs,
        generationTimeoutMs,
        pollIntervalMs: 1150,
        randomId,
        setTimeoutImpl,
        clearTimeoutImpl,
        onDiagnostic: (event) => {
            diagnostics.push(event);
            onDiagnostic?.(event);
        },
        onStatus: (status) => {
            statuses.push(status);
            onStatus?.(status);
        },
        settingsStore,
    });
    if (bind) {
        client.bindCurrent();
    }
    return {
        client,
        context,
        eventSource,
        appended,
        statuses,
        diagnostics,
        bridge,
        setGenerating: (value) => { generating = value; },
    };
}

test("completes one claimed request through native generation and submits its reply", async () => {
    const bridge = createMockBridge();
    bridge.addRequest("req-normal", "hello from QQ");
    const harness = createHarness({ bridge });

    assert.equal(await harness.client.pollOnce(), true);
    assert.equal(harness.appended.length, 1);
    assert.equal(harness.appended[0].mes, "hello from QQ");
    assert.equal(bridge.jobs.get("req-normal").status, "SUCCEEDED");
    assert.equal(bridge.jobs.get("req-normal").reply, "native reply");
    assert.equal(bridge.generationGuardId, null);

    const resultCall = bridge.calls.find((call) => call.path.endsWith("/result"));
    assert.equal(resultCall.path, "/v1/requests/req-normal/result");
    assert.deepEqual(resultCall.body, { reply: "native reply" });
    assert.ok(bridge.calls.every((call) => !("Authorization" in call.headers)));
    assert.equal(JSON.stringify(harness.statuses).includes("hello from QQ"), false);
    assert.equal(JSON.stringify(harness.statuses).includes("native reply"), false);
    assert.equal(harness.eventSource.listenerCount(EVENT_TYPES.GENERATION_STARTED), 0);
    assert.equal(harness.eventSource.listenerCount(EVENT_TYPES.GENERATION_ENDED), 0);
    assert.equal(harness.eventSource.listenerCount(EVENT_TYPES.GENERATION_STOPPED), 0);
});

// Chromium's native fetch throws "Illegal invocation" unless `this` is the window.
function strictFetchFor(bridge) {
    return function strictFetch(url, options) {
        if (this !== globalThis) {
            throw new TypeError("Failed to execute 'fetch' on 'Window': Illegal invocation");
        }
        return bridge.fetchImpl(url, options);
    };
}

test("polls normally with an injected fetch that insists on this === globalThis", async () => {
    const bridge = createMockBridge();
    bridge.addRequest("req-strict-fetch", "hello strict");
    bridge.fetchImpl = strictFetchFor({ fetchImpl: bridge.fetchImpl });
    const harness = createHarness({ bridge });

    assert.equal(await harness.client.pollOnce(), true);
    assert.equal(bridge.jobs.get("req-strict-fetch").status, "SUCCEEDED");
});

test("the default fetch path also survives a strict globalThis.fetch", async () => {
    const bridge = createMockBridge();
    const originalFetch = globalThis.fetch;
    globalThis.fetch = strictFetchFor(bridge);
    try {
        const client = new BridgeClient({
            getContext: () => ({}),
            isGenerating: () => false,
            sendMessageAsUser: async () => ({}),
            generate: async () => "x",
        });
        // Called as a method, like the real client does at every request.
        const response = await client.fetchImpl("http://127.0.0.1:11452/v1/health", {});
        assert.ok(response.status !== undefined);
    } finally {
        globalThis.fetch = originalFetch;
    }
    assert.ok(bridge.calls.some((call) => call.path === "/v1/health"));
});

test("recovery gate does not touch the ST chat", async () => {
    const bridge = createMockBridge({ recovered: false });
    bridge.addRequest("req-recovery", "do not append");
    const harness = createHarness({ bridge });

    assert.equal(await harness.client.pollOnce(), false);
    assert.equal(harness.appended.length, 0);
    assert.equal(bridge.jobs.get("req-recovery").status, "PENDING");
    assert.equal(harness.client.needsReadyReport, true);
});

test("HTTP timeout and unavailable service are reported without ST changes", async (t) => {
    await t.test("request timeout", async () => {
        const eventSource = new EventEmitter();
        const statuses = [];
        const client = new BridgeClient({
            fetchImpl: (_url, { signal }) => new Promise((_resolve, reject) => {
                signal.addEventListener("abort", () => {
                    reject(new DOMException("aborted", "AbortError"));
                }, { once: true });
            }),
            getContext: () => ({
                characterId: "c1", groupId: null, chatId: "chat1", chat: [],
                eventSource, eventTypes: EVENT_TYPES,
            }),
            isGenerating: () => false,
            sendMessageAsUser: async () => assert.fail("must not append"),
            generate: async () => assert.fail("must not generate"),
            requestTimeoutMs: 5,
            randomId: () => "attempt-timeout",
            onStatus: (status) => statuses.push(status.state),
        });
        client.bindCurrent();

        assert.equal(await client.pollOnce(), false);
        assert.ok(statuses.includes("network_timeout"));
    });

    await t.test("service unavailable", async () => {
        const eventSource = new EventEmitter();
        const statuses = [];
        const client = new BridgeClient({
            fetchImpl: async () => { throw new TypeError("connection refused"); },
            getContext: () => ({
                characterId: "c1", groupId: null, chatId: "chat1", chat: [],
                eventSource, eventTypes: EVENT_TYPES,
            }),
            isGenerating: () => false,
            sendMessageAsUser: async () => assert.fail("must not append"),
            generate: async () => assert.fail("must not generate"),
            randomId: () => "attempt-unavailable",
            onStatus: (status) => statuses.push(status.state),
        });
        client.bindCurrent();

        assert.equal(await client.pollOnce(), false);
        assert.ok(statuses.includes("network_error"));
    });
});

test("retries an ambiguous claim with the same attempt ID", async () => {
    const bridge = createMockBridge({ claimResponseLosses: 1 });
    bridge.addRequest("req-retry-claim", "claim once");
    const harness = createHarness({ bridge });

    assert.equal(await harness.client.pollOnce(), false);
    assert.equal(harness.client.pendingClaim.requestId, "req-retry-claim");
    assert.equal(harness.client.pendingClaim.attemptId, "claim-attempt-test-1");

    assert.equal(await harness.client.pollOnce(), true);
    const claims = bridge.calls.filter((call) => call.path.endsWith("/claim"));
    assert.equal(claims.length, 2);
    assert.deepEqual(claims.map((call) => call.body.claim_attempt_id), [
        "claim-attempt-test-1",
        "claim-attempt-test-1",
    ]);
    assert.equal(harness.appended.length, 1);
    assert.equal(bridge.jobs.get("req-retry-claim").status, "SUCCEEDED");
});

test("does not duplicate a result after the server committed but its response was lost", async () => {
    const bridge = createMockBridge({ resultResponseLosses: 1 });
    bridge.addRequest("req-result-retry", "one reply");
    const harness = createHarness({ bridge });

    assert.equal(await harness.client.pollOnce(), true);
    assert.equal(bridge.jobs.get("req-result-retry").status, "SUCCEEDED");
    assert.equal(bridge.jobs.get("req-result-retry").reply, "native reply");
    assert.equal(bridge.calls.filter((call) => call.path.endsWith("/result")).length, 1);
    assert.equal(bridge.calls.some((call) => call.path === "/v1/requests/req-result-retry"), true);
});

test("does not process a mismatched request ID in the claim response", async () => {
    const bridge = createMockBridge({ claimResponseId: "req-other" });
    bridge.addRequest("req-original", "must not append");
    const harness = createHarness({ bridge });

    assert.equal(await harness.client.pollOnce(), false);
    assert.equal(harness.appended.length, 0);
    assert.equal(bridge.jobs.get("req-original").status, "CLAIMED");
    assert.equal(bridge.generationGuardId, "req-original");
});

test("a status timeout after claiming fails closed without generating or releasing the slot", async () => {
    const bridge = createMockBridge();
    bridge.addRequest("req-status-outage", "do not generate twice");
    const originalFetch = bridge.fetchImpl;
    bridge.fetchImpl = async (url, options) => {
        const path = new URL(url).pathname;
        if (path === "/v1/requests/req-status-outage"
            && (options.method ?? "GET") === "GET") {
            throw new TypeError("private status transport detail");
        }
        return originalFetch(url, options);
    };
    const harness = createHarness({ bridge });

    assert.equal(await harness.client.pollOnce(), false);
    assert.equal(harness.appended.length, 0);
    assert.equal(bridge.jobs.get("req-status-outage").status, "CLAIMED");
    assert.equal(bridge.jobs.get("req-status-outage").generation_status, "UNKNOWN");
    assert.equal(bridge.generationGuardId, "req-status-outage");
    assert.equal(bridge.calls.some((call) => call.path.endsWith("/result")), false);
    assert.equal(JSON.stringify(harness.statuses).includes("private status transport detail"), false);
    assert.throws(() => bridge.addRequest("req-next", "blocked"), /mock bridge busy/);
});

test("confirms NOT_STARTED for an expired request before any ST mutation", async () => {
    const bridge = createMockBridge();
    bridge.addRequest("req-expired", "expired content");
    const originalFetch = bridge.fetchImpl;
    bridge.fetchImpl = async (url, options) => {
        const path = new URL(url).pathname;
        const method = options.method ?? "GET";
        if (path.endsWith("/claim")) {
            const response = await originalFetch(url, options);
            bridge.jobs.get("req-expired").status = "EXPIRED";
            return response;
        }
        return originalFetch(url, options);
    };
    const harness = createHarness({ bridge });

    assert.equal(await harness.client.pollOnce(), true);
    assert.equal(harness.appended.length, 0);
    assert.equal(bridge.jobs.get("req-expired").status, "EXPIRED");
    assert.equal(bridge.jobs.get("req-expired").generation_status, "NOT_STARTED");
    assert.equal(bridge.generationGuardId, null);
    assert.equal(bridge.calls.some((call) => call.path.endsWith("/result")), false);
});

test("generation failure is submitted only after ST reports generation stopped", async () => {
    const bridge = createMockBridge();
    bridge.addRequest("req-generation-failed", "try generation");
    const harness = createHarness({
        bridge,
        generate: async (type) => {
            harness.setGenerating(true);
            harness.eventSource.emit(EVENT_TYPES.GENERATION_STARTED, type);
            await Promise.resolve();
            harness.setGenerating(false);
            harness.eventSource.emit(EVENT_TYPES.GENERATION_STOPPED);
            throw new Error("private model error must not be sent");
        },
    });

    assert.equal(await harness.client.pollOnce(), true);
    assert.equal(bridge.jobs.get("req-generation-failed").status, "FAILED");
    assert.equal(bridge.jobs.get("req-generation-failed").generation_status, "FINISHED");
    assert.equal(bridge.jobs.get("req-generation-failed").reply, null);
    assert.equal(bridge.calls.some((call) => call.path.endsWith("/result")), false);
    assert.equal(JSON.stringify(bridge.calls).includes("private model error"), false);
    assert.equal(JSON.stringify(harness.statuses).includes("private model error"), false);
});

test("generation wait timeout does not clear the generation guard before ST ends", async () => {
    const bridge = createMockBridge();
    bridge.addRequest("req-long-generation", "wait for the actual end");
    let harness;
    let releaseGeneration;
    let generationTimeout;
    const generationGate = new Promise((resolve) => { releaseGeneration = resolve; });
    harness = createHarness({
        bridge,
        generationTimeoutMs: 10,
        requestTimeoutMs: 100,
        setTimeoutImpl: (callback, delayMs) => {
            if (delayMs === 10) {
                generationTimeout = callback;
            }
            return { callback, delayMs };
        },
        clearTimeoutImpl: () => {},
        generate: async (type) => {
            harness.setGenerating(true);
            harness.eventSource.emit(EVENT_TYPES.GENERATION_STARTED, type);
            await generationGate;
            harness.context.chat.push({ is_user: false, is_system: false, mes: "native reply" });
            harness.setGenerating(false);
            harness.eventSource.emit(EVENT_TYPES.GENERATION_ENDED, harness.context.chat.length);
            return "native reply";
        },
    });

    const processing = harness.client.pollOnce();
    while (bridge.jobs.get("req-long-generation").generation_status !== "RUNNING") {
        await new Promise((resolve) => setImmediate(resolve));
    }
    assert.equal(typeof generationTimeout, "function");
    generationTimeout();
    await new Promise((resolve) => setImmediate(resolve));
    assert.ok(harness.statuses.some((status) => status.state === "generation_timeout_waiting"));
    assert.equal(bridge.jobs.get("req-long-generation").generation_status, "RUNNING");
    assert.equal(bridge.generationGuardId, "req-long-generation");
    assert.equal(bridge.calls.some((call) => call.path.endsWith("/result")), false);

    releaseGeneration();
    assert.equal(await processing, true);
    assert.equal(bridge.jobs.get("req-long-generation").status, "SUCCEEDED");
    assert.equal(bridge.generationGuardId, null);
});

test("chat switch during generation rejects the request instead of submitting a reply", async () => {
    const bridge = createMockBridge();
    bridge.addRequest("req-chat-switch", "for the original chat");
    let harness;
    let currentContext;
    harness = createHarness({
        bridge,
        generate: async (type) => {
            harness.setGenerating(true);
            harness.eventSource.emit(EVENT_TYPES.GENERATION_STARTED, type);
            currentContext = {
                characterId: "character-other",
                groupId: null,
                chatId: "chat-other",
                chat: [],
                eventSource: harness.eventSource,
                eventTypes: EVENT_TYPES,
            };
            harness.eventSource.emit(EVENT_TYPES.CHAT_CHANGED, "chat-other");
            currentContext.chat.push({ is_user: false, is_system: false, mes: "wrong chat reply" });
            harness.setGenerating(false);
            harness.eventSource.emit(EVENT_TYPES.GENERATION_ENDED, currentContext.chat.length);
            return "wrong chat reply";
        },
    });
    currentContext = harness.context;
    harness.client.getContext = () => currentContext;

    assert.equal(await harness.client.pollOnce(), true);
    assert.equal(bridge.jobs.get("req-chat-switch").status, "REJECTED");
    assert.equal(bridge.jobs.get("req-chat-switch").reply, null);
    assert.equal(bridge.calls.some((call) => call.path.endsWith("/result")), false);
    assert.equal(bridge.calls.some((call) => call.path.endsWith("/reject")
        && call.body.error_code === "chat_changed"), true);
});

test("a same-chat message appended while checking state prevents normal generation", async () => {
    const bridge = createMockBridge();
    bridge.addRequest("req-chat-mutated", "bridge user message");
    const harness = createHarness({ bridge });
    const append = harness.client.sendMessageAsUser;
    let generationCalled = false;
    harness.client.sendMessageAsUser = async (text) => {
        const message = await append(text);
        harness.context.chat.push({ is_user: true, is_system: false, mes: "manual message" });
        return message;
    };
    harness.client.generate = async () => {
        generationCalled = true;
        return "must not generate";
    };

    assert.equal(await harness.client.pollOnce(), true);
    assert.equal(generationCalled, false);
    assert.equal(bridge.jobs.get("req-chat-mutated").status, "REJECTED");
    assert.equal(bridge.jobs.get("req-chat-mutated").generation_status, "NOT_STARTED");
    assert.equal(bridge.jobs.get("req-chat-mutated").reply, null);
    assert.equal(bridge.calls.some((call) => call.path.endsWith("/result")), false);
});

test("late reply from an expired request cannot complete or contaminate the next request", async () => {
    const bridge = createMockBridge();
    bridge.addRequest("req-old", "old content");
    let harness;
    let releaseGeneration;
    let generationCount = 0;
    const generationGate = new Promise((resolve) => { releaseGeneration = resolve; });
    harness = createHarness({
        bridge,
        generate: async (type) => {
            generationCount += 1;
            harness.setGenerating(true);
            harness.eventSource.emit(EVENT_TYPES.GENERATION_STARTED, type);
            if (generationCount === 1) {
                await generationGate;
                harness.context.chat.push({ is_user: false, is_system: false, mes: "late old reply" });
                harness.setGenerating(false);
                harness.eventSource.emit(EVENT_TYPES.GENERATION_ENDED, harness.context.chat.length);
                return "late old reply";
            }
            await Promise.resolve();
            harness.context.chat.push({ is_user: false, is_system: false, mes: "native reply" });
            harness.setGenerating(false);
            harness.eventSource.emit(EVENT_TYPES.GENERATION_ENDED, harness.context.chat.length);
            return "native reply";
        },
    });

    const oldRequest = harness.client.pollOnce();
    while (!bridge.jobs.get("req-old").generation_status
        || bridge.jobs.get("req-old").generation_status !== "RUNNING") {
        await new Promise((resolve) => setImmediate(resolve));
    }
    bridge.jobs.get("req-old").status = "EXPIRED";
    assert.throws(() => bridge.addRequest("req-new", "new content"), /mock bridge busy/);

    releaseGeneration();
    assert.equal(await oldRequest, true);
    assert.equal(bridge.jobs.get("req-old").status, "EXPIRED");
    assert.equal(bridge.jobs.get("req-old").reply, null);
    assert.equal(bridge.generationGuardId, null);
    assert.equal(bridge.calls.some((call) => call.path === "/v1/requests/req-old/result"), false);

    bridge.addRequest("req-new", "new content");
    assert.equal(await harness.client.pollOnce(), true);
    assert.equal(bridge.jobs.get("req-new").status, "SUCCEEDED");
    assert.equal(bridge.jobs.get("req-new").reply, "native reply");
    assert.equal(bridge.jobs.get("req-old").reply, null);
    assert.deepEqual(bridge.calls
        .filter((call) => call.path.endsWith("/result"))
        .map((call) => call.path), ["/v1/requests/req-new/result"]);
});

test("reports specific ST context failures in a stable priority order", async (t) => {
    const cases = [
        ["getContext throws", (harness) => {
            harness.client.getContext = () => { throw new Error("private context detail"); };
        }, "context_read_failed"],
        ["group context takes priority", (harness) => {
            harness.context.groupId = "group-1";
            harness.context.characterId = null;
            harness.context.chatId = null;
            harness.context.chat = null;
        }, "group_context_unsupported"],
        ["missing character ID takes priority", (harness) => {
            harness.context.characterId = null;
            harness.context.chatId = null;
            harness.context.chat = null;
        }, "character_id_missing"],
        ["missing chat ID takes priority", (harness) => {
            harness.context.chatId = null;
            harness.context.chat = null;
        }, "chat_id_missing"],
        ["non-array chat is reported", (harness) => {
            harness.context.chat = {};
        }, "chat_array_invalid"],
    ];

    for (const [name, invalidate, expectedCode] of cases) {
        await t.test(name, () => {
            const harness = createHarness();
            invalidate(harness);

            assert.throws(() => harness.client.bindCurrent(), (error) => {
                assert.equal(error.code, expectedCode);
                assert.equal(error.message.includes("private context detail"), false);
                return true;
            });
            assert.equal(harness.bridge.calls.length, 0);
            assert.equal(harness.appended.length, 0);
        });
    }
});

test("polling reports the specific context failure without making a request", async () => {
    const bridge = createMockBridge();
    const harness = createHarness({
        bridge,
        setTimeoutImpl: () => 1,
        clearTimeoutImpl: () => {},
    });
    harness.client.start();
    harness.context.groupId = "group-1";

    assert.equal(await harness.client.pollOnce(), false);
    assert.equal(harness.statuses.at(-1).state, "group_context_unsupported");
    assert.equal(bridge.calls.length, 0);
    assert.equal(harness.appended.length, 0);
    harness.client.stop();
});

test("maps each context diagnostic state to its specific UI text", () => {
    const cases = [
        ["context_read_failed", "读取 ST 上下文失败"],
        ["group_context_unsupported", "当前为群组上下文，暂不支持"],
        ["character_id_missing", "当前角色 ID 缺失"],
        ["chat_id_missing", "当前聊天 ID 缺失"],
        ["chat_array_invalid", "当前聊天数据格式无效"],
    ];

    for (const [state, text] of cases) {
        assert.equal(statusTextForState(state), text);
    }
});

test("uses generic text for unknown states and unclassified exceptions", () => {
    assert.equal(statusTextForState("unmapped_state"), "桥接状态已更新");

    const errorState = statusStateForError(new Error("private internal detail"));
    assert.equal(errorState, "unclassified_operation_error");
    assert.notEqual(errorState, "context_unavailable");
    assert.equal(statusTextForState(errorState), "桥接操作发生未分类错误");

    const knownError = new BridgeClientError("group_context_unsupported");
    assert.equal(statusStateForError(knownError), "group_context_unsupported");
    assert.equal(statusTextForState(statusStateForError(knownError)), "当前为群组上下文，暂不支持");
});

test("cleans up partial binding listener registration and reports a safe phase", () => {
    const diagnostics = [];
    const harness = createHarness({ onDiagnostic: (event) => diagnostics.push(event) });
    const originalSource = harness.eventSource;
    const registered = new Map();
    const failingSource = {
        on(name, listener) {
            const listeners = registered.get(name) ?? new Set();
            listeners.add(listener);
            registered.set(name, listeners);
            if (name === EVENT_TYPES.CHAT_LOADED) {
                throw new TypeError(`private failure ${SECRET}`);
            }
        },
        removeListener(name, listener) {
            registered.get(name)?.delete(listener);
        },
    };
    harness.context.eventSource = failingSource;

    assert.throws(() => harness.client.start(), (error) =>
        error.code === "binding_listener_setup_failed");
    assert.equal(harness.client.running, false);
    assert.equal(harness.client.pollTimer, null);
    assert.equal([...registered.values()].reduce((sum, listeners) => sum + listeners.size, 0), 0);
    assert.equal(originalSource.listenerCount(EVENT_TYPES.CHAT_CHANGED), 1);
    assert.deepEqual(diagnostics, [{ phase: "listener_setup", errorType: "OtherError" }]);
    assert.equal(JSON.stringify(diagnostics).includes(SECRET), false);
    assert.equal(JSON.stringify(diagnostics).includes("private failure"), false);
});

test("rolls back running state when poll timer scheduling fails", () => {
    const diagnostics = [];
    const harness = createHarness({
        onDiagnostic: (event) => diagnostics.push(event),
        setTimeoutImpl: () => { throw new RangeError(`timer failure ${SECRET}`); },
    });
    const boundListenerCount = harness.eventSource.listenerCount(EVENT_TYPES.CHAT_CHANGED);

    assert.throws(() => harness.client.start(), RangeError);
    assert.equal(harness.client.running, false);
    assert.equal(harness.client.pollTimer, null);
    assert.equal(harness.eventSource.listenerCount(EVENT_TYPES.CHAT_CHANGED), boundListenerCount);
    assert.deepEqual(diagnostics, [{ phase: "schedule_poll", errorType: "RangeError" }]);
    assert.equal(JSON.stringify(diagnostics).includes(SECRET), false);
    assert.equal(JSON.stringify(diagnostics).includes("timer failure"), false);
});

test("invokes browser-style timer functions with the global receiver", () => {
    const timerId = { id: "poll-timer" };
    const scheduled = [];
    const cleared = [];
    function browserSetTimeout(callback, delay) {
        if (this !== globalThis) {
            throw new TypeError("Illegal invocation");
        }
        scheduled.push({ callback, delay });
        return timerId;
    }
    function browserClearTimeout(id) {
        if (this !== globalThis) {
            throw new TypeError("Illegal invocation");
        }
        cleared.push(id);
    }
    const harness = createHarness({
        setTimeoutImpl: browserSetTimeout,
        clearTimeoutImpl: browserClearTimeout,
    });

    assert.doesNotThrow(() => harness.client.start());
    assert.equal(harness.client.running, true);
    assert.equal(harness.client.pollTimer, timerId);
    assert.equal(scheduled.length, 1);
    assert.equal(scheduled[0].delay, 0);

    harness.client.stop();
    assert.equal(harness.client.running, false);
    assert.equal(harness.client.pollTimer, null);
    assert.deepEqual(cleared, [timerId]);
});

test("clears an already scheduled timer if startup then fails synchronously", () => {
    const diagnostics = [];
    const clearedTimers = [];
    let scheduledCallback;
    const bridge = createMockBridge();
    const harness = createHarness({
        bridge,
        onDiagnostic: (event) => diagnostics.push(event),
        setTimeoutImpl: (callback) => {
            scheduledCallback = callback;
            return 91;
        },
        clearTimeoutImpl: (timer) => clearedTimers.push(timer),
    });
    const schedulePoll = harness.client._schedulePoll.bind(harness.client);
    harness.client._schedulePoll = (delayMs) => {
        schedulePoll(delayMs);
        throw new Error(`post-schedule failure ${SECRET}`);
    };

    assert.throws(() => harness.client.start(), Error);
    assert.equal(harness.client.running, false);
    assert.equal(harness.client.pollTimer, null);
    assert.deepEqual(clearedTimers, [91]);
    assert.deepEqual(diagnostics, [{ phase: "schedule_poll", errorType: "OtherError" }]);
    assert.equal(JSON.stringify(diagnostics).includes(SECRET), false);
    assert.equal(JSON.stringify(diagnostics).includes("post-schedule failure"), false);
    return scheduledCallback().then(() => {
        assert.equal(bridge.calls.length, 0);
    });
});

test("ordinary context read failure keeps its specific status without leaking details", () => {
    const harness = createHarness();
    const rendered = [];
    harness.client.getContext = () => { throw new Error(`private context failure ${SECRET}`); };

    const result = runStartButtonAction({
        start: () => harness.client.start(),
        getRunning: () => harness.client.running,
        renderStatus: (status) => rendered.push(status),
        reportDiagnostic: (phase, error) => harness.client.reportStartupDiagnostic(phase, error),
    });

    assert.equal(result.startSucceeded, false);
    assert.equal(result.running, false);
    assert.equal(rendered[0].state, "context_read_failed");
    assert.equal(harness.client.running, false);
    assert.equal(harness.diagnostics.length, 0);
});

test("unexpected identity property errors report identity_read without sensitive details", () => {
    const harness = createHarness();
    harness.client.getContext = () => new Proxy({}, {
        get(_target, property) {
            if (property === "groupId") {
                throw new TypeError(`private identity failure ${SECRET}`);
            }
            return undefined;
        },
    });
    const rendered = [];

    const result = runStartButtonAction({
        start: () => harness.client.start(),
        getRunning: () => harness.client.running,
        renderStatus: (status) => rendered.push(status),
        reportDiagnostic: (phase, error) => harness.client.reportStartupDiagnostic(phase, error),
    });

    assert.equal(result.startSucceeded, false);
    assert.equal(result.running, false);
    assert.equal(rendered[0].state, "unclassified_operation_error");
    assert.deepEqual(harness.diagnostics, [{ phase: "identity_read", errorType: "TypeError" }]);
    assert.equal(JSON.stringify(harness.diagnostics).includes(SECRET), false);
    assert.equal(JSON.stringify(harness.diagnostics).includes("private identity failure"), false);
});

test("diagnoses button status rendering separately without restarting the client", () => {
    const diagnostics = [];
    const harness = createHarness({
        setTimeoutImpl: () => 77,
        clearTimeoutImpl: () => {},
        onDiagnostic: (event) => diagnostics.push(event),
    });
    let starts = 0;
    let renders = 0;
    const rendered = [];

    const result = runStartButtonAction({
        start: () => {
            starts += 1;
            harness.client.start();
        },
        getRunning: () => harness.client.running,
        renderStatus: (status) => {
            renders += 1;
            if (renders === 1) {
                throw new Error(`render failure ${SECRET}`);
            }
            rendered.push(status);
        },
        reportDiagnostic: (phase, error) => harness.client.reportStartupDiagnostic(phase, error),
    });

    assert.equal(result.startSucceeded, true);
    assert.equal(result.running, true);
    assert.equal(starts, 1);
    assert.deepEqual(rendered, [{ state: "polling" }]);
    assert.deepEqual(diagnostics, [{ phase: "button_status_render", errorType: "OtherError" }]);
    assert.equal(JSON.stringify(diagnostics).includes(SECRET), false);
    assert.equal(JSON.stringify(diagnostics).includes("render failure"), false);
    harness.client.stop();
});

test("reports unexpected failures before identity inspection as other_startup", () => {
    const diagnostics = [];
    const harness = createHarness({ onDiagnostic: (event) => diagnostics.push(event) });
    Object.defineProperty(harness.client, "binding", {
        configurable: true,
        get() { throw new ReferenceError(`unexpected ${SECRET}`); },
    });

    assert.throws(() => harness.client.start(), ReferenceError);
    assert.equal(harness.client.running, false);
    assert.equal(harness.client.pollTimer, null);
    assert.deepEqual(diagnostics, [{ phase: "other_startup", errorType: "ReferenceError" }]);
    assert.equal(JSON.stringify(diagnostics).includes(SECRET), false);
});

test("first asynchronous HTTP failure is not reported as a startup diagnostic", async () => {
    const diagnostics = [];
    const scheduled = [];
    const bridge = createMockBridge();
    bridge.fetchImpl = async () => { throw new TypeError(`private network failure ${SECRET}`); };
    const harness = createHarness({
        bridge,
        onDiagnostic: (event) => diagnostics.push(event),
        setTimeoutImpl: (callback, delay) => {
            const timer = scheduled.length + 12;
            scheduled.push({ callback, delay, timer });
            return timer;
        },
        clearTimeoutImpl: () => {},
    });

    harness.client.start();
    assert.equal(harness.client.running, true);
    assert.deepEqual(diagnostics, []);
    assert.equal(scheduled[0].delay, 0);
    await scheduled[0].callback();
    assert.equal(diagnostics.length, 0);
    assert.equal(harness.statuses.some((status) => status.state === "network_error"), true);
    assert.equal(JSON.stringify(diagnostics).includes(SECRET), false);
    harness.client.stop();
});

test("a falsy thrown value is still treated as a startup failure", () => {
    const rendered = [];
    const result = runStartButtonAction({
        start: () => { throw null; },
        getRunning: () => false,
        renderStatus: (status) => rendered.push(status),
    });

    assert.equal(result.startSucceeded, false);
    assert.deepEqual(rendered, [{ state: "unclassified_operation_error" }]);
});

test("listener setup errors have stable UI text", () => {
    assert.equal(
        statusTextForState("binding_listener_setup_failed"),
        "无法建立聊天切换保护，绑定或启动已拒绝",
    );
    assert.equal(
        statusTextForState("binding_listener_cleanup_failed"),
        "聊天切换保护清理失败，操作已拒绝",
    );
});

test("getContext failure during listener setup rejects binding and startup", async (t) => {
    for (const operation of ["bind", "start"]) {
        await t.test(operation, () => {
            const harness = createHarness();
            const originalBinding = { ...harness.client.binding };
            const statusesBefore = harness.statuses.length;
            if (operation === "bind") {
                harness.client.clearBinding();
            }

            let calls = 0;
            harness.client.getContext = () => {
                calls += 1;
                if (calls === 2) {
                    throw new Error(`private context failure ${SECRET}`);
                }
                return harness.context;
            };

            assert.throws(() => {
                if (operation === "bind") {
                    harness.client.bindCurrent();
                } else {
                    harness.client.start();
                }
            }, (error) => error.code === "binding_listener_setup_failed"
                && error.message === "binding_listener_setup_failed");
            assert.equal(calls, 2);
            assert.deepEqual(harness.client.binding, operation === "bind" ? null : originalBinding);
            assert.equal(harness.client.running, false);
            assert.equal(harness.client.pollTimer, null);
            assert.equal(harness.eventSource.listenerCount(EVENT_TYPES.CHAT_CHANGED), 1);
            assert.equal(harness.eventSource.listenerCount(EVENT_TYPES.CHAT_LOADED), 1);
            assert.equal(harness.eventSource.listenerCount(EVENT_TYPES.CHARACTER_PAGE_LOADED), 1);
            assert.equal(harness.statuses.length, statusesBefore + (operation === "bind" ? 1 : 0));
        });
    }
});

test("missing listener prerequisites reject binding and startup", async (t) => {
    const cases = [
        ["eventSource", (context) => { context.eventSource = null; }],
        ["CHAT_CHANGED", (context) => {
            context.eventTypes = { ...EVENT_TYPES, CHAT_CHANGED: undefined };
        }],
        ["CHAT_LOADED", (context) => {
            context.eventTypes = { ...EVENT_TYPES, CHAT_LOADED: "" };
        }],
        ["CHARACTER_PAGE_LOADED", (context) => {
            context.eventTypes = { ...EVENT_TYPES, CHARACTER_PAGE_LOADED: null };
        }],
        ["on", (context) => {
            context.eventSource = { removeListener() {} };
        }],
        ["removeListener", (context) => {
            context.eventSource = { on() {} };
        }],
    ];

    for (const [name, invalidate] of cases) {
        for (const operation of ["bind", "start"]) {
            await t.test(`${name} during ${operation}`, () => {
                const harness = createHarness();
                const originalBinding = { ...harness.client.binding };
                if (operation === "bind") {
                    harness.client.clearBinding();
                }
                invalidate(harness.context);

                assert.throws(() => {
                    if (operation === "bind") {
                        harness.client.bindCurrent();
                    } else {
                        harness.client.start();
                    }
                }, (error) => error.code === "binding_listener_setup_failed");
                assert.deepEqual(harness.client.binding, operation === "bind" ? null : originalBinding);
                assert.equal(harness.client.running, false);
                assert.equal(harness.client.pollTimer, null);
                assert.equal(harness.eventSource.listenerCount(EVENT_TYPES.CHAT_CHANGED), 1);
                assert.equal(harness.eventSource.listenerCount(EVENT_TYPES.CHAT_LOADED), 1);
                assert.equal(harness.eventSource.listenerCount(EVENT_TYPES.CHARACTER_PAGE_LOADED), 1);
            });
        }
    }
});

test("failed later listener registration cleans temporary listeners and preserves the old binding", () => {
    const harness = createHarness();
    const oldBinding = { ...harness.client.binding };
    const oldSource = harness.eventSource;
    const registered = new Map();
    const failingSource = {
        on(name, listener) {
            const listeners = registered.get(name) ?? new Set();
            listeners.add(listener);
            registered.set(name, listeners);
            if (name === EVENT_TYPES.CHAT_LOADED) {
                throw new TypeError(`private registration detail ${SECRET}`);
            }
        },
        removeListener(name, listener) {
            registered.get(name)?.delete(listener);
        },
    };
    harness.context.characterId = "character-next";
    harness.context.chatId = "chat-next";
    harness.context.eventSource = failingSource;

    assert.throws(() => harness.client.bindCurrent(), (error) =>
        error.code === "binding_listener_setup_failed");
    assert.deepEqual(harness.client.binding, oldBinding);
    assert.equal(harness.client.running, false);
    assert.equal(harness.client.pollTimer, null);
    assert.equal([...registered.values()].reduce((total, listeners) => total + listeners.size, 0), 0);
    assert.equal(oldSource.listenerCount(EVENT_TYPES.CHAT_CHANGED), 1);
    assert.equal(oldSource.listenerCount(EVENT_TYPES.CHAT_LOADED), 1);
    assert.equal(oldSource.listenerCount(EVENT_TYPES.CHARACTER_PAGE_LOADED), 1);
    assert.equal(harness.statuses.at(-1).state, "bound");
});

test("failed temporary listener cleanup is reported and retained for retry", () => {
    const harness = createHarness();
    const oldBinding = { ...harness.client.binding };
    const registered = new Map();
    let failRegistration = true;
    const failingSource = {
        on(name, listener) {
            const listeners = registered.get(name) ?? new Set();
            listeners.add(listener);
            registered.set(name, listeners);
            if (failRegistration && name === EVENT_TYPES.CHAT_LOADED) {
                throw new TypeError("registration rejected");
            }
        },
        removeListener(name, listener) {
            if (name === EVENT_TYPES.CHAT_LOADED) {
                throw new Error(`cleanup detail ${SECRET}`);
            }
            registered.get(name)?.delete(listener);
        },
    };
    harness.context.characterId = "character-next";
    harness.context.chatId = "chat-next";
    harness.context.eventSource = failingSource;

    assert.throws(() => harness.client.bindCurrent(), (error) =>
        error.code === "binding_listener_cleanup_failed"
        && error.message === "binding_listener_cleanup_failed");
    assert.deepEqual(harness.client.binding, oldBinding);
    assert.equal(harness.client.orphanedBindingListeners.length, 1);
    assert.equal(registered.get(EVENT_TYPES.CHAT_CHANGED)?.size ?? 0, 0);
    assert.equal(registered.get(EVENT_TYPES.CHAT_LOADED)?.size ?? 0, 1);
    assert.equal(harness.eventSource.listenerCount(EVENT_TYPES.CHAT_CHANGED), 1);
    assert.equal(harness.eventSource.listenerCount(EVENT_TYPES.CHAT_LOADED), 1);

    failRegistration = false;
    failingSource.removeListener = (name, listener) => {
        registered.get(name)?.delete(listener);
    };
    assert.deepEqual(harness.client.bindCurrent(), {
        characterId: "character-next",
        chatId: "chat-next",
    });
    assert.equal(harness.client.orphanedBindingListeners.length, 0);
    assert.equal(registered.get(EVENT_TYPES.CHAT_LOADED)?.size ?? 0, 1);
    assert.equal(harness.client.binding.characterId, "character-next");
});

test("old listener replacement failure rejects binding and keeps the new protection active", () => {
    const harness = createHarness();
    const originalBinding = { ...harness.client.binding };
    const oldSource = harness.eventSource;
    const originalRemove = oldSource.removeListener.bind(oldSource);
    oldSource.removeListener = (name, listener) => {
        if (name === EVENT_TYPES.CHAT_LOADED) {
            throw new Error(`old listener cleanup detail ${SECRET}`);
        }
        return originalRemove(name, listener);
    };
    const nextSource = new EventEmitter();
    const statusesBefore = harness.statuses.length;
    harness.context.characterId = "character-next";
    harness.context.chatId = "chat-next";
    harness.context.eventSource = nextSource;

    assert.throws(() => harness.client.bindCurrent(), (error) =>
        error.code === "binding_listener_cleanup_failed");
    assert.deepEqual(harness.client.binding, originalBinding);
    assert.equal(harness.statuses.length, statusesBefore);
    assert.equal(harness.client.bindingEventSource, nextSource);
    assert.equal(harness.client.bindingListenersReady, true);
    assert.equal(harness.client.bindingListeners.length, 3);
    assert.equal(harness.client.orphanedBindingListeners.length, 1);
    for (const eventName of [
        EVENT_TYPES.CHAT_CHANGED,
        EVENT_TYPES.CHAT_LOADED,
        EVENT_TYPES.CHARACTER_PAGE_LOADED,
    ]) {
        assert.equal(nextSource.listenerCount(eventName), 1);
    }
    assert.equal(oldSource.listenerCount(EVENT_TYPES.CHAT_LOADED), 1);

    oldSource.removeListener = originalRemove;
    assert.deepEqual(harness.client.bindCurrent(), {
        characterId: "character-next",
        chatId: "chat-next",
    });
    assert.equal(harness.client.orphanedBindingListeners.length, 0);
    assert.equal(oldSource.listenerCount(EVENT_TYPES.CHAT_LOADED), 0);
    assert.equal(harness.client.binding.characterId, "character-next");
});

test("normal bind, repeated start, and stop preserve exactly one listener set", () => {
    const timers = [];
    const cleared = [];
    const harness = createHarness({
        setTimeoutImpl: (callback, delay) => {
            const id = timers.length + 1;
            timers.push({ callback, delay, id });
            return id;
        },
        clearTimeoutImpl: (id) => cleared.push(id),
    });

    assert.equal(harness.client.bindingListeners.length, 3);
    assert.deepEqual(harness.client.bindCurrent(), {
        characterId: "character-7",
        chatId: "chat-2026-10-29",
    });
    assert.equal(harness.client.bindingListeners.length, 3);
    harness.client.start();
    harness.client.start();
    assert.equal(harness.client.running, true);
    assert.equal(timers.length, 1);
    for (const eventName of [
        EVENT_TYPES.CHAT_CHANGED,
        EVENT_TYPES.CHAT_LOADED,
        EVENT_TYPES.CHARACTER_PAGE_LOADED,
    ]) {
        assert.equal(harness.eventSource.listenerCount(eventName), 1);
    }
    harness.client.stop();
    harness.client.stop();
    assert.equal(harness.client.running, false);
    assert.equal(harness.client.pollTimer, null);
    assert.deepEqual(cleared, [1]);
    assert.equal(harness.client.bindingListeners.length, 3);
});

test("listener setup failure does not clear an active request or generation guard", async () => {
    const bridge = createMockBridge();
    bridge.addRequest("req-listener-setup", "active request");
    let harness;
    let releaseGeneration;
    const generationGate = new Promise((resolve) => { releaseGeneration = resolve; });
    harness = createHarness({
        bridge,
        generate: async (type) => {
            harness.setGenerating(true);
            harness.eventSource.emit(EVENT_TYPES.GENERATION_STARTED, type);
            await generationGate;
            harness.context.chat.push({ is_user: false, is_system: false, mes: "native reply" });
            harness.setGenerating(false);
            harness.eventSource.emit(EVENT_TYPES.GENERATION_ENDED, harness.context.chat.length);
            return "native reply";
        },
    });

    const processing = harness.client.pollOnce();
    while (bridge.jobs.get("req-listener-setup").generation_status !== "RUNNING") {
        await new Promise((resolve) => setImmediate(resolve));
    }
    const activeId = harness.client.activeRequestId;
    const failingSource = {
        on(name, listener) {
            if (name === EVENT_TYPES.CHAT_LOADED) {
                throw new Error(`registration detail ${SECRET}`);
            }
            return harness.eventSource.on(name, listener);
        },
        removeListener: harness.eventSource.removeListener.bind(harness.eventSource),
    };
    harness.context.eventSource = failingSource;

    assert.throws(() => harness.client.start(), (error) =>
        error.code === "binding_listener_setup_failed");
    assert.equal(harness.client.activeRequestId, activeId);
    assert.equal(harness.client.hasWork, true);
    assert.equal(bridge.jobs.get(activeId).generation_status, "RUNNING");
    assert.equal(bridge.generationGuardId, activeId);
    assert.equal(bridge.jobs.get(activeId).status, "CLAIMED");

    releaseGeneration();
    assert.equal(await processing, true);
    assert.equal(bridge.jobs.get(activeId).status, "SUCCEEDED");
    assert.equal(bridge.jobs.get(activeId).reply, "native reply");
    assert.equal(bridge.generationGuardId, null);
});

// ---------- Seamless bridge: idle reporting, reload recovery, persistence, auto-start ----------

function readyCalls(bridge) {
    return bridge.calls.filter((call) => call.path === "/v1/session/ready");
}

function createSettingsStore(initial = null) {
    const store = {
        data: initial,
        saves: [],
        load: () => store.data,
        save: (value) => {
            store.data = { ...(store.data || {}), ...value };
            store.saves.push(value);
        },
    };
    return store;
}

function manualTimers() {
    const scheduled = [];
    let nextId = 1;
    return {
        scheduled,
        setTimeoutImpl: (callback, delay) => {
            const id = nextId;
            nextId += 1;
            scheduled.push({ id, callback, delay });
            return id;
        },
        // Request and generation timeouts are cleared, so only live timers stay queued.
        clearTimeoutImpl: (id) => {
            const index = scheduled.findIndex((entry) => entry.id === id);
            if (index >= 0) {
                scheduled.splice(index, 1);
            }
        },
        async runNext() {
            const next = scheduled.shift();
            await next.callback();
            return next;
        },
    };
}

test("requests carry no Authorization header", async () => {
    const bridge = createMockBridge();
    bridge.addRequest("req-no-auth", "hello");
    const harness = createHarness({ bridge });
    assert.equal(await harness.client.pollOnce(), true);
    assert.ok(bridge.calls.length > 0);
    assert.ok(bridge.calls.every((call) => Object.keys(call.headers).every((name) => name.toLowerCase() !== "authorization")));
});

test("a 503 recovery_required leads to an idle report and then normal processing", async () => {
    const bridge = createMockBridge({ recovered: false });
    bridge.addRequest("req-after-reload", "hello after reload");
    const harness = createHarness({ bridge });

    assert.equal(await harness.client.pollOnce(), false);
    assert.ok(harness.statuses.some((status) => status.state === "recovery_required"));
    assert.equal(readyCalls(bridge).length, 0);

    assert.equal(await harness.client.pollOnce(), true);
    const ready = readyCalls(bridge);
    assert.equal(ready.length, 1);
    assert.deepEqual(ready[0].body, { generating: false, active_request: false });
    assert.equal(ready[0].method, "POST");
    assert.equal(bridge.recovered, true);
    assert.equal(harness.client.needsReadyReport, false);
    assert.equal(bridge.jobs.get("req-after-reload").status, "SUCCEEDED");
    const order = bridge.calls.map((call) => call.path);
    assert.ok(order.indexOf("/v1/session/ready") < order.lastIndexOf("/v1/requests/pending"));
});

test("a busy client does not report idle until ST finishes", async () => {
    const bridge = createMockBridge({ recovered: false });
    const harness = createHarness({ bridge });
    harness.setGenerating(true);

    await harness.client.pollOnce();
    await harness.client.pollOnce();
    await harness.client.pollOnce();
    assert.equal(readyCalls(bridge).length, 0);
    assert.equal(bridge.recovered, false);

    harness.setGenerating(false);
    await harness.client.pollOnce();
    assert.equal(readyCalls(bridge).length, 1);
    assert.equal(bridge.recovered, true);
});

test("starting the poll loop reports idle once even before any 503", async () => {
    const bridge = createMockBridge({ recovered: false });
    const timers = manualTimers();
    const harness = createHarness({ bridge, ...timers });

    harness.client.start();
    await timers.runNext();
    assert.equal(readyCalls(bridge).length, 1);
    assert.equal(bridge.calls[0].path, "/v1/session/ready");
    assert.equal(bridge.recovered, true);

    await timers.runNext();
    assert.equal(readyCalls(bridge).length, 1);
    harness.client.stop();
});

test("a claim lost across a plugin reload is abandoned instead of retried forever", async () => {
    const bridge = createMockBridge({ claimResponseLosses: 1 });
    bridge.addRequest("req-lost-claim", "claim response lost");
    const harness = createHarness({ bridge });

    assert.equal(await harness.client.pollOnce(), false);
    assert.notEqual(harness.client.pendingClaim, null);

    bridge.reload();
    assert.equal(await harness.client.pollOnce(), false);
    assert.equal(harness.client.pendingClaim, null);
    assert.equal(harness.client.hasWork, false);

    assert.equal(await harness.client.pollOnce(), false);
    assert.equal(readyCalls(bridge).length, 1);
    assert.equal(bridge.recovered, true);
    assert.equal(harness.appended.length, 0);
});

for (const recoveredAfterReload of [false, true]) {
    const label = recoveredAfterReload ? "404 for the old request" : "503 while the new gate is closed";
    test(`a reload during ST generation ends with the client idle (${label})`, async () => {
        const bridge = createMockBridge();
        bridge.addRequest("req-reload-mid-generation", "reload while generating");
        let harness;
        let reloadPending = true;
        harness = createHarness({
            bridge,
            generate: async (type) => {
                harness.setGenerating(true);
                harness.eventSource.emit(EVENT_TYPES.GENERATION_STARTED, type);
                await Promise.resolve();
                if (reloadPending) {
                    reloadPending = false;
                    bridge.reload({ recoveredAfterReload });
                }
                harness.context.chat.push({ is_user: false, is_system: false, mes: "late reply" });
                harness.setGenerating(false);
                harness.eventSource.emit(EVENT_TYPES.GENERATION_ENDED, harness.context.chat.length);
                return "late reply";
            },
        });

        assert.equal(await harness.client.pollOnce(), true);
        assert.equal(harness.client.hasWork, false);
        assert.equal(harness.client.activeRequestId, null);
        assert.ok(harness.statuses.some((status) => status.state === "generation_state_unconfirmed"));
        assert.equal(bridge.calls.some((call) => call.path.endsWith("/result")), false);

        for (let attempt = 0; attempt < 3 && !bridge.recovered; attempt += 1) {
            await harness.client.pollOnce();
        }
        assert.equal(bridge.recovered, true);
        assert.equal(harness.client.hasWork, false);

        bridge.addRequest("req-next", "next message");
        assert.equal(await harness.client.pollOnce(), true);
        assert.equal(bridge.jobs.get("req-next").status, "SUCCEEDED");
    });
}

test("binding and the enable switch persist across page loads", () => {
    const settings = createSettingsStore();
    const first = createHarness({ settingsStore: settings });
    assert.deepEqual(settings.data, {
        binding: { characterId: "character-7", chatId: "chat-2026-10-29" },
        enabled: true,
    });

    const reloaded = createHarness({ settingsStore: settings, bind: false });
    assert.deepEqual(reloaded.client.binding, { characterId: "character-7", chatId: "chat-2026-10-29" });
    assert.equal(reloaded.client.enabled, true);

    reloaded.client.stop();
    assert.equal(settings.data.enabled, false);
    const disabled = createHarness({ settingsStore: settings, bind: false });
    assert.equal(disabled.client.enabled, false);
    assert.equal(disabled.client.autoStart(), false);
    assert.equal(disabled.client.running, false);

    disabled.client.start();
    assert.equal(settings.data.enabled, true);
    disabled.client.stop();

    first.client.clearBinding();
    assert.equal(settings.data.binding, null);
    const unbound = createHarness({ settingsStore: settings, bind: false });
    assert.equal(unbound.client.binding, null);
});

test("invalid or unreadable saved settings fall back to an unbound, enabled client", () => {
    for (const data of [
        { binding: { characterId: 7, chatId: "chat" }, enabled: "yes" },
        { binding: "character-7" },
        null,
    ]) {
        const harness = createHarness({ settingsStore: createSettingsStore(data), bind: false });
        assert.equal(harness.client.binding, null);
        assert.equal(harness.client.enabled, true);
    }
    const throwing = createHarness({
        settingsStore: { load: () => { throw new Error(SECRET); }, save: () => { throw new Error(SECRET); } },
    });
    assert.deepEqual(throwing.client.binding, { characterId: "character-7", chatId: "chat-2026-10-29" });
});

test("auto-start follows APP_READY and CHAT_CHANGED for the saved chat only", async () => {
    const settings = createSettingsStore({
        binding: { characterId: "character-7", chatId: "chat-2026-10-29" },
        enabled: true,
    });
    const timers = manualTimers();
    const harness = createHarness({ settingsStore: settings, bind: false, ...timers });
    assert.equal(harness.client.attachAutoStart(), true);
    assert.equal(harness.client.attachAutoStart(), true);

    harness.context.chatId = "some-other-chat";
    harness.eventSource.emit(EVENT_TYPES.APP_READY);
    assert.equal(timers.scheduled.length, 1);
    await timers.runNext();
    assert.equal(harness.client.running, false);

    harness.context.chatId = "chat-2026-10-29";
    harness.eventSource.emit(EVENT_TYPES.CHAT_CHANGED);
    await timers.runNext();
    assert.equal(harness.client.running, true);

    harness.context.chatId = "some-other-chat";
    harness.eventSource.emit(EVENT_TYPES.CHAT_CHANGED);
    await timers.runNext();
    assert.equal(harness.client.running, true);
    assert.equal(await harness.client.pollOnce(), false);
    assert.equal(harness.statuses.at(-1).state, "binding_mismatch");

    harness.context.chatId = "chat-2026-10-29";
    harness.bridge.addRequest("req-back-in-chat", "after switching back");
    assert.equal(await harness.client.pollOnce(), true);
    assert.equal(harness.bridge.jobs.get("req-back-in-chat").status, "SUCCEEDED");

    harness.client.dispose();
    assert.equal(harness.eventSource.listenerCount(EVENT_TYPES.APP_READY), 0);
});

test("auto-start does nothing when disabled or unbound", async () => {
    for (const data of [
        { binding: { characterId: "character-7", chatId: "chat-2026-10-29" }, enabled: false },
        { binding: null, enabled: true },
    ]) {
        const timers = manualTimers();
        const harness = createHarness({ settingsStore: createSettingsStore(data), bind: false, ...timers });
        harness.client.attachAutoStart();
        harness.eventSource.emit(EVENT_TYPES.APP_READY);
        await timers.runNext();
        assert.equal(harness.client.running, false);
    }
});

test("retry delay starts at zero and the loop keeps retrying through outages", async () => {
    const bridge = createMockBridge();
    const timers = manualTimers();
    const harness = createHarness({ bridge, ...timers });
    assert.equal(harness.client.retryAfterMs, 0);

    const workingFetch = bridge.fetchImpl;
    bridge.fetchImpl = async () => { throw new TypeError("connection refused"); };
    harness.client.fetchImpl = bridge.fetchImpl;
    harness.client.start();
    await timers.runNext();
    await timers.runNext();
    assert.equal(timers.scheduled.at(-1).delay, 1150);
    assert.equal(harness.statuses.at(-1).state, "network_error");

    harness.client.fetchImpl = async (url, options) => {
        if (new URL(url).pathname === "/v1/requests/pending") {
            return { status: 429, ok: false, headers: { get: () => "3" }, json: async () => ({ error: "poll_rate_limited" }) };
        }
        return workingFetch(url, options);
    };
    await timers.runNext();
    assert.equal(timers.scheduled.at(-1).delay, 3000);
    assert.equal(harness.client.retryAfterMs, 0);

    harness.client.fetchImpl = workingFetch;
    bridge.addRequest("req-after-outage", "service is back");
    await timers.runNext();
    assert.equal(bridge.jobs.get("req-after-outage").status, "SUCCEEDED");
    assert.equal(timers.scheduled.at(-1).delay, 1150);
    harness.client.stop();
});

test("status texts cover the seamless states and drop the token prompt", () => {
    assert.equal(statusTextForState("token_required"), "桥接状态已更新");
    assert.match(statusTextForState("recovery_required"), /自动解除/);
    assert.match(statusTextForState("bridge_ready"), /就绪/);
    assert.match(statusTextForState("unauthorized"), /版本过旧/);
});

// ---------- P6: per-speaker affinity injection ----------

const KOTONE = "藤田琴音";
const DESCRIPTION = "[0, 100] 对网友的好感度。";

function mvuVariables(value) {
    return {
        stat_data: {
            [KOTONE]: { 好感度: [value, DESCRIPTION], 状态: "开心" },
            月村手毬: { 好感度: [30, "手毬的描述"] },
        },
        display_data: {},
    };
}

// Mirrors TavernHelper getVariables/updateVariablesWith for type 'message' on chat[message_id].variables[swipe].
function createFakeTavernHelper(chat, { failWrite = false, ignoreWrites = false } = {}) {
    const calls = [];
    const pick = (messageId) => chat.at(messageId);
    return {
        calls,
        getVariables(option) {
            calls.push(["get", option.type, option.message_id]);
            const message = pick(option.message_id);
            return structuredClone(message?.variables?.[message?.swipe_id ?? 0] ?? {});
        },
        updateVariablesWith(updater, option) {
            calls.push(["update", option.type, option.message_id]);
            if (failWrite) {
                throw new Error(`write failed ${SECRET}`);
            }
            const message = pick(option.message_id);
            const next = updater(structuredClone(message?.variables?.[message?.swipe_id ?? 0] ?? {}));
            if (!ignoreWrites) {
                message.variables = [next];
            }
            return next;
        },
    };
}

// Prompt Template copies the previous message's variables onto the new user message during MESSAGE_SENT.
function sendWithCopiedVariables(harness) {
    return async (text) => {
        const previous = harness.context.chat.at(-1);
        const message = {
            is_user: true,
            is_system: false,
            mes: text,
            variables: [structuredClone(previous?.variables?.[0] ?? {})],
        };
        harness.context.chat.push(message);
        harness.appended.push(message);
        return message;
    };
}

const OWNER_PATH = [KOTONE, "好感度"];
const GROUP_PATH = ["群友好感度"];

function ownerAndGroupWrites(ownerValue = 73, groupValue = 12) {
    return [{ path: OWNER_PATH, value: ownerValue }, { path: GROUP_PATH, value: groupValue }];
}

function affinityHarness({ helperOptions, affinityWrites = ownerAndGroupWrites(), previousValue = 12, previous } = {}) {
    const bridge = createMockBridge();
    bridge.addRequest("req-affinity", "【群友】小明（1）[好感度:12]：你好", affinityWrites);
    const seenAtGeneration = [];
    let harness;
    harness = createHarness({
        bridge,
        generate: async (type) => {
            const userMessage = harness.context.chat.at(-1);
            seenAtGeneration.push(structuredClone(userMessage.variables?.[0] ?? null));
            harness.eventSource.emit(EVENT_TYPES.GENERATION_STARTED, type);
            await Promise.resolve();
            harness.context.chat.push({ is_user: false, is_system: false, mes: "reply" });
            harness.eventSource.emit(EVENT_TYPES.GENERATION_ENDED, harness.context.chat.length);
            return "reply";
        },
    });
    harness.context.chat.push({
        is_user: false, is_system: false, mes: "earlier", variables: [previous ?? mvuVariables(previousValue)],
    });
    const helper = createFakeTavernHelper(harness.context.chat, helperOptions);
    const results = [];
    harness.client.getVariableApi = () => helper;
    harness.client.onAffinityResult = (result) => results.push(result);
    harness.client.sendMessageAsUser = sendWithCopiedVariables(harness);
    return { harness, bridge, helper, results, seenAtGeneration };
}

function withGroupNode(value) {
    const variables = mvuVariables(12);
    variables.stat_data.群友好感度 = [value, "群友的描述"];
    return variables;
}

test("the speaker's affinity writes go to the new user message before generation", async () => {
    const { harness, bridge, helper, results, seenAtGeneration } = affinityHarness({ previous: withGroupNode(0) });

    assert.equal(await harness.client.pollOnce(), true);
    assert.equal(bridge.jobs.get("req-affinity").status, "SUCCEEDED");
    assert.equal(seenAtGeneration.length, 1);
    assert.deepEqual(seenAtGeneration[0].stat_data[KOTONE].好感度, [73, DESCRIPTION]);
    assert.deepEqual(seenAtGeneration[0].stat_data.群友好感度, [12, "群友的描述"]);
    assert.equal(seenAtGeneration[0].stat_data[KOTONE].状态, "开心");
    assert.deepEqual(seenAtGeneration[0].stat_data.月村手毬.好感度, [30, "手毬的描述"]);
    // The earlier message keeps its own values; only the new user message (index 1) was written.
    assert.deepEqual(harness.context.chat[0].variables[0].stat_data[KOTONE].好感度, [12, DESCRIPTION]);
    assert.deepEqual(harness.context.chat[0].variables[0].stat_data.群友好感度, [0, "群友的描述"]);
    assert.ok(helper.calls.some(([kind, type, index]) => kind === "update" && type === "message" && index === 1));
    assert.deepEqual(results, [{
        ok: true,
        reason: null,
        writes: [
            { path: "藤田琴音.好感度", ok: true, reason: null, skipped: false },
            { path: "群友好感度", ok: true, reason: null, skipped: false },
        ],
    }]);
    assert.ok(harness.statuses.some((status) => status.state === "affinity_injected"));
});

test("a private turn writes null into an existing group node", async () => {
    const { harness, bridge, seenAtGeneration } = affinityHarness({
        previous: withGroupNode(30), affinityWrites: ownerAndGroupWrites(61, null),
    });
    assert.equal(await harness.client.pollOnce(), true);
    assert.equal(bridge.jobs.get("req-affinity").status, "SUCCEEDED");
    assert.deepEqual(seenAtGeneration[0].stat_data[KOTONE].好感度, [61, DESCRIPTION]);
    assert.deepEqual(seenAtGeneration[0].stat_data.群友好感度, [null, "群友的描述"]);
});

test("a private turn skips null when the group node does not exist", async () => {
    const { harness, bridge, seenAtGeneration, results } = affinityHarness({ affinityWrites: ownerAndGroupWrites(61, null) });
    assert.equal(await harness.client.pollOnce(), true);
    assert.equal(bridge.jobs.get("req-affinity").status, "SUCCEEDED");
    assert.equal(Object.hasOwn(seenAtGeneration[0].stat_data, "群友好感度"), false);
    assert.equal(results[0].ok, true);
    assert.deepEqual(results[0].writes[1], { path: "群友好感度", ok: true, reason: null, skipped: true });
});

test("a missing group node is created as [value, description] without touching other data", async () => {
    const { harness, bridge, seenAtGeneration } = affinityHarness();
    assert.equal(await harness.client.pollOnce(), true);
    assert.equal(bridge.jobs.get("req-affinity").status, "SUCCEEDED");
    assert.deepEqual(seenAtGeneration[0].stat_data.群友好感度, [12, ""]);
    assert.deepEqual(seenAtGeneration[0].stat_data[KOTONE].好感度, [73, DESCRIPTION]);
    assert.deepEqual(seenAtGeneration[0].stat_data.月村手毬.好感度, [30, "手毬的描述"]);
    assert.deepEqual(seenAtGeneration[0].display_data, {});
});

test("affinity injection failures never stop generation", async (t) => {
    for (const [label, options, reason] of [
        ["write throws", { helperOptions: { failWrite: true } }, "write_failed"],
        ["write ignored", { helperOptions: { ignoreWrites: true } }, "readback_mismatch"],
        ["invalid value", { affinityWrites: [{ path: OWNER_PATH, value: 150 }] }, "invalid_affinity"],
        ["invalid path", { affinityWrites: [{ path: ["a.b"], value: 1 }] }, "invalid_affinity"],
    ]) {
        await t.test(label, async () => {
            const { harness, bridge, results } = affinityHarness(options);
            assert.equal(await harness.client.pollOnce(), true);
            assert.equal(bridge.jobs.get("req-affinity").status, "SUCCEEDED");
            assert.equal(results.length, 1);
            assert.equal(results[0].ok, false);
            assert.equal(results[0].reason, reason);
            assert.ok(harness.statuses.some((status) => status.state === "affinity_inject_failed"));
            assert.equal(JSON.stringify(harness.statuses).includes(SECRET), false);
        });
    }

    await t.test("TavernHelper missing", async () => {
        const { harness, bridge, results } = affinityHarness();
        harness.client.getVariableApi = () => undefined;
        assert.equal(await harness.client.pollOnce(), true);
        assert.equal(bridge.jobs.get("req-affinity").status, "SUCCEEDED");
        assert.equal(results[0].reason, "api_missing");
    });

    await t.test("getVariableApi throws", async () => {
        const { harness, bridge, results } = affinityHarness();
        harness.client.getVariableApi = () => { throw new Error(SECRET); };
        assert.equal(await harness.client.pollOnce(), true);
        assert.equal(bridge.jobs.get("req-affinity").status, "SUCCEEDED");
        assert.equal(results[0].reason, "write_failed");
    });
});

test("no affinity writes in the claim means no variable access", async () => {
    const { harness, bridge, helper, results } = affinityHarness({ affinityWrites: null });
    assert.equal(await harness.client.pollOnce(), true);
    assert.equal(bridge.jobs.get("req-affinity").status, "SUCCEEDED");
    assert.deepEqual(helper.calls, []);
    assert.deepEqual(results, []);
});

test("the old single-value affinity field is ignored", async () => {
    const { harness, bridge, helper, results } = affinityHarness({ affinityWrites: null });
    bridge.jobs.get("req-affinity").affinity = { character: KOTONE, value: 40 };
    assert.equal(await harness.client.pollOnce(), true);
    assert.equal(bridge.jobs.get("req-affinity").status, "SUCCEEDED");
    assert.deepEqual(helper.calls, []);
    assert.deepEqual(results, []);
});

test("affinity writes are validated on the client too", () => {
    const ok = { path: ["群友好感度"], value: -100 };
    assert.equal(isValidAffinityWrites([ok, { path: ["a", "b", "c"], value: null }, { path: ["x"], value: 100 }]), true);
    for (const bad of [
        null, [], "text", [ok, ok, ok, ok, ok],
        [{ path: [], value: 1 }],
        [{ path: ["a", "b", "c", "d"], value: 1 }],
        [{ path: "群友好感度", value: 1 }],
        [{ path: ["a.b"], value: 1 }],
        [{ path: ["a[0]"], value: 1 }],
        [{ path: [""], value: 1 }],
        [{ path: ["__proto__"], value: 1 }],
        [{ path: ["constructor"], value: 1 }],
        [{ path: ["prototype"], value: 1 }],
        [{ path: [1], value: 1 }],
        [{ path: ["a"], value: 101 }],
        [{ path: ["a"], value: -101 }],
        [{ path: ["a"], value: 1.5 }],
        [{ path: ["a"], value: "5" }],
        [{ path: ["a"], value: undefined }],
        [{ path: ["a"] }],
    ]) {
        assert.equal(isValidAffinityWrites(bad), false, JSON.stringify(bad));
    }
});

test("injectAffinityWrites: arrays, plain numbers, intermediate objects, blocked paths and bad nodes", () => {
    const chat = [{
        variables: [{
            stat_data: {
                数组: { 好感度: [5, "d"] },
                纯数字: 7,
                空: null,
                字符串: "text",
                列表: [1, 2],
            },
        }],
    }];
    const helper = createFakeTavernHelper(chat);
    const good = injectAffinityWrites(helper, 0, [
        { path: ["数组", "好感度"], value: -20 },
        { path: ["纯数字"], value: 8 },
        { path: ["空"], value: 9 },
        { path: ["新", "层", "好感度"], value: 3 },
    ]);
    const statData = chat[0].variables[0].stat_data;
    assert.equal(good.ok, true);
    assert.deepEqual(statData.数组.好感度, [-20, "d"]);
    assert.equal(statData.纯数字, 8);
    assert.equal(statData.空, 9);
    assert.deepEqual(statData.新.层.好感度, [3, ""]);
    assert.equal(Object.getPrototypeOf(statData.新) === Object.prototype, true);

    const result = injectAffinityWrites(helper, 0, [
        { path: ["字符串"], value: 1 },
        { path: ["列表", "x"], value: 1 },
        { path: ["数组", "好感度"], value: 2 },
    ]);
    assert.equal(result.ok, false);
    assert.equal(result.reason, "node_type_invalid");
    assert.deepEqual(result.results.map((entry) => entry.reason), ["node_type_invalid", "path_blocked", null]);
    assert.equal(statData.字符串, "text");
    assert.deepEqual(statData.列表, [1, 2]);
    // a failing entry does not stop the others
    assert.deepEqual(chat[0].variables[0].stat_data.数组.好感度, [2, "d"]);
});

test("injectAffinityWrites never invents the stat_data root and ignores a missing null target", () => {
    const chat = [{ variables: [{ other: {} }] }];
    const helper = createFakeTavernHelper(chat);
    assert.deepEqual(injectAffinityWrites(helper, 0, [{ path: ["a"], value: 1 }]), {
        ok: false, reason: "variable_missing", results: [],
    });
    assert.equal(helper.calls.some(([kind]) => kind === "update"), false);

    chat[0].variables = [{ stat_data: { x: {} } }];
    const onlyNull = injectAffinityWrites(helper, 0, [{ path: ["a", "b"], value: null }]);
    assert.equal(onlyNull.ok, true);
    assert.equal(helper.calls.some(([kind]) => kind === "update"), false);
    assert.deepEqual(chat[0].variables[0].stat_data, { x: {} });
    assert.equal(injectAffinityWrites({}, 0, [{ path: ["a"], value: 1 }]).reason, "api_missing");
});

test("injectAffinity never invents MVU structure and supports plain numbers", () => {
    const chat = [{ variables: [{ stat_data: { other: {} } }] }];
    const helper = createFakeTavernHelper(chat);
    assert.deepEqual(injectAffinity(helper, 0, { character: KOTONE, value: 10 }), { ok: false, reason: "variable_missing" });
    assert.equal(helper.calls.some(([kind]) => kind === "update"), false);

    chat[0].variables = [{ stat_data: { [KOTONE]: { 好感度: 40 } } }];
    assert.deepEqual(injectAffinity(helper, 0, { character: KOTONE, value: 55 }), { ok: true, before: 40 });
    assert.equal(chat[0].variables[0].stat_data[KOTONE].好感度, 55);

    assert.equal(injectAffinity(helper, 0, { character: "", value: 5 }).reason, "invalid_affinity");
    assert.equal(injectAffinity(helper, 0, { character: KOTONE, value: 5.5 }).reason, "invalid_affinity");
    assert.equal(injectAffinity({}, 0, { character: KOTONE, value: 5 }).reason, "api_missing");
});

test("the affinity diagnostic writes, reads back like the world book, and restores", async () => {
    const chat = [
        { variables: [mvuVariables(5)] },
        { variables: [mvuVariables(12)] },
    ];
    const helper = createFakeTavernHelper(chat);
    const templates = [];
    const ejs = {
        evalTemplate: async (code) => {
            templates.push(code);
            return String(chat.at(-1).variables[0].stat_data[KOTONE].好感度[0]);
        },
    };

    const report = await runAffinityDiagnostic({ helper, ejs, chat, character: KOTONE, value: 73 });
    assert.equal(report.ok, true);
    assert.equal(report.messageIndex, 1);
    assert.equal(report.before, 12);
    assert.equal(report.helperReadback, 73);
    assert.equal(report.worldbookReadback, "73");
    assert.equal(report.restored, true);
    assert.deepEqual(templates, ['<%= getvar("stat_data.藤田琴音.好感度[0]") %>']);
    assert.deepEqual(chat[1].variables[0].stat_data[KOTONE].好感度, [12, DESCRIPTION]);
    assert.deepEqual(chat[0].variables[0].stat_data[KOTONE].好感度, [5, DESCRIPTION]);

    const noTemplate = await runAffinityDiagnostic({ helper, ejs: undefined, chat, character: KOTONE, value: 40 });
    assert.equal(noTemplate.ok, false);
    assert.equal(noTemplate.reason, "template_missing");
    assert.equal(noTemplate.helperReadback, 40);
    assert.deepEqual(chat[1].variables[0].stat_data[KOTONE].好感度, [12, DESCRIPTION]);

    const broken = await runAffinityDiagnostic({
        helper, ejs: { evalTemplate: async () => { throw new Error(SECRET); } }, chat, character: KOTONE, value: 40,
    });
    assert.equal(broken.reason, "template_failed");
    assert.equal(broken.ok, false);

    assert.equal((await runAffinityDiagnostic({ helper, ejs, chat: [], character: KOTONE, value: 1 })).reason, "chat_empty");
    assert.equal((await runAffinityDiagnostic({ helper: undefined, ejs, chat, character: KOTONE, value: 1 })).reason, "api_missing");
});

test("affinity status texts exist", () => {
    assert.match(statusTextForState("affinity_injected"), /好感度/);
    assert.match(statusTextForState("affinity_inject_failed"), /好感度/);
});

// ---- scheduled (proactive) turns: the request text goes in as a narrator line ----

function narratorSender(harness, calls = []) {
    return async (text) => {
        calls.push(text);
        harness.context.chat.push({
            is_user: false, is_system: false, mes: text, extra: { type: "narrator" },
        });
    };
}

test("a proactive request inserts a narrator line instead of a user message", async () => {
    const bridge = createMockBridge();
    bridge.addRequest("req-proactive", "（系统提示：现在是 2026-11-02 星期一 07:30。道早安。）", null, "proactive");
    const seenAtGeneration = [];
    const harness = createHarness({
        bridge,
        generate: async (type) => {
            seenAtGeneration.push(structuredClone(harness.context.chat.at(-1)));
            harness.eventSource.emit(EVENT_TYPES.GENERATION_STARTED, type);
            await Promise.resolve();
            harness.context.chat.push({ is_user: false, is_system: false, mes: "早安，制作人。" });
            harness.eventSource.emit(EVENT_TYPES.GENERATION_ENDED, harness.context.chat.length);
            return "reply";
        },
    });
    const narratorCalls = [];
    harness.client.sendNarratorMessage = narratorSender(harness, narratorCalls);

    assert.equal(await harness.client.pollOnce(), true);
    assert.deepEqual(narratorCalls, ["（系统提示：现在是 2026-11-02 星期一 07:30。道早安。）"]);
    assert.equal(harness.appended.length, 0, "sendMessageAsUser must not run");
    assert.equal(seenAtGeneration[0].extra.type, "narrator");
    assert.equal(bridge.jobs.get("req-proactive").status, "SUCCEEDED");
    assert.equal(bridge.jobs.get("req-proactive").reply, "早安，制作人。");
    assert.equal(harness.context.chat.length, 2);
});

test("a normal request never uses the narrator path", async () => {
    const bridge = createMockBridge();
    bridge.addRequest("req-chat", "hello");
    const harness = createHarness({ bridge });
    const narratorCalls = [];
    harness.client.sendNarratorMessage = narratorSender(harness, narratorCalls);

    assert.equal(await harness.client.pollOnce(), true);
    assert.deepEqual(narratorCalls, []);
    assert.equal(harness.appended.length, 1);
    assert.equal(bridge.jobs.get("req-chat").status, "SUCCEEDED");
});

test("proactive affinity writes use the narrator line's index", async () => {
    const bridge = createMockBridge();
    bridge.addRequest("req-proactive-affinity", "（系统提示：现在是 12:00。）", ownerAndGroupWrites(61, null), "proactive");
    const seenAtGeneration = [];
    const harness = createHarness({
        bridge,
        generate: async (type) => {
            seenAtGeneration.push(structuredClone(harness.context.chat.at(-1).variables?.[0] ?? null));
            harness.eventSource.emit(EVENT_TYPES.GENERATION_STARTED, type);
            await Promise.resolve();
            harness.context.chat.push({ is_user: false, is_system: false, mes: "reply" });
            harness.eventSource.emit(EVENT_TYPES.GENERATION_ENDED, harness.context.chat.length);
            return "reply";
        },
    });
    harness.context.chat.push({
        is_user: false, is_system: false, mes: "earlier", variables: [withGroupNode(30)],
    });
    const helper = createFakeTavernHelper(harness.context.chat);
    harness.client.getVariableApi = () => helper;
    harness.client.sendNarratorMessage = async (text) => {
        const previous = harness.context.chat.at(-1);
        harness.context.chat.push({
            is_user: false, is_system: false, mes: text, extra: { type: "narrator" },
            variables: [structuredClone(previous.variables[0])],
        });
    };

    assert.equal(await harness.client.pollOnce(), true);
    assert.equal(bridge.jobs.get("req-proactive-affinity").status, "SUCCEEDED");
    assert.deepEqual(seenAtGeneration[0].stat_data[KOTONE].好感度, [61, DESCRIPTION]);
    assert.deepEqual(seenAtGeneration[0].stat_data.群友好感度, [null, "群友的描述"]);
    assert.ok(helper.calls.some(([kind, type, index]) => kind === "update" && type === "message" && index === 1));
    assert.deepEqual(harness.context.chat[0].variables[0].stat_data[KOTONE].好感度, [12, DESCRIPTION]);
});

test("a proactive request fails cleanly without a narrator adapter or when it throws", async (t) => {
    for (const [label, install] of [
        ["no adapter", () => null],
        ["adapter throws", (harness) => async () => { throw new Error("slash-commands unavailable"); }],
        ["adapter inserts nothing", () => async () => {}],
    ]) {
        await t.test(label, async () => {
            const bridge = createMockBridge();
            bridge.addRequest("req-proactive-fail", "（系统提示：现在是 12:00。）", null, "proactive");
            let generated = 0;
            const harness = createHarness({
                bridge,
                generate: async () => { generated += 1; return "unused"; },
            });
            harness.client.sendNarratorMessage = install(harness);

            assert.equal(await harness.client.pollOnce(), true);
            assert.equal(generated, 0);
            assert.equal(bridge.jobs.get("req-proactive-fail").status, "FAILED");
            assert.equal(harness.appended.length, 0);
            assert.equal(bridge.generationGuardId, null);
        });
    }
});

test("bridgeUrl builds a loopback address from the port and refuses bad ports", () => {
    assert.equal(DEFAULT_BRIDGE_PORT, 11452);
    assert.equal(bridgeUrl(), "http://127.0.0.1:11452");
    assert.equal(BRIDGE_URL, bridgeUrl());
    assert.equal(bridgeUrl(12001), "http://127.0.0.1:12001");
    assert.equal(bridgeUrl("12001"), "http://127.0.0.1:12001");
    for (const bad of [1023, 65536, 0, -1, 1.5, "abc", "", null, NaN, Infinity, "12001.5", "1e4", true, {}]) {
        assert.equal(validBridgePort(bad), null, String(bad));
        assert.throws(() => bridgeUrl(bad), (error) => error instanceof BridgeClientError && error.code === "invalid_bridge_port");
    }
    assert.equal(validBridgePort(" 1024 "), 1024);
    assert.equal(validBridgePort(65535), 65535);
    assert.equal(statusStateForError(new BridgeClientError("invalid_bridge_port")), "invalid_bridge_port");
    assert.match(statusTextForState("invalid_bridge_port"), /1024/);
    assert.match(statusTextForState("bridge_port_saved"), /新端口/);
});

test("requests go to the saved bridge port; the default is 11452", async () => {
    const settings = createSettingsStore({ bridgePort: 12001 });
    const bridge = createMockBridge();
    const harness = createHarness({ bridge, settingsStore: settings, bind: false });
    assert.equal(harness.client.bridgePort, 12001);
    harness.client.bindCurrent();
    await harness.client.pollOnce();
    assert.ok(bridge.calls.length > 0);
    assert.ok(bridge.calls.every((call) => call.url.startsWith("http://127.0.0.1:12001/")), JSON.stringify(bridge.calls.map((call) => call.url)));

    const defaults = createMockBridge();
    const plain = createHarness({ bridge: defaults });
    await plain.client.pollOnce();
    assert.ok(defaults.calls.every((call) => call.url.startsWith("http://127.0.0.1:11452/")));
});

test("a saved port that is not valid falls back to the default", () => {
    for (const bridgePort of [80, "x", 70000, null, 1.5]) {
        const harness = createHarness({ settingsStore: createSettingsStore({ bridgePort }), bind: false });
        assert.equal(harness.client.bridgePort, 11452, String(bridgePort));
    }
});

test("setBridgePort saves the port and keeps the other saved settings", () => {
    const settings = createSettingsStore();
    const harness = createHarness({ settingsStore: settings });
    assert.equal(harness.client.setBridgePort("12001"), 12001);
    assert.equal(settings.data.bridgePort, 12001);
    assert.deepEqual(settings.data.binding, { characterId: "character-7", chatId: "chat-2026-10-29" });
    assert.equal(harness.client.bridgeUrl, "http://127.0.0.1:12001");
    assert.equal(harness.statuses.at(-1).state, "bridge_port_saved");
    // saving the binding afterwards does not drop the port
    harness.client.clearBinding();
    assert.equal(settings.data.bridgePort, 12001);
    const reloaded = createHarness({ settingsStore: settings, bind: false });
    assert.equal(reloaded.client.bridgePort, 12001);
});

test("setBridgePort refuses a bad port and does not touch the saved one", () => {
    const settings = createSettingsStore({ bridgePort: 12001 });
    const harness = createHarness({ settingsStore: settings, bind: false });
    for (const bad of [80, "abc", "", 65536, null]) {
        assert.throws(() => harness.client.setBridgePort(bad), (error) => error.code === "invalid_bridge_port", String(bad));
    }
    assert.equal(harness.client.bridgePort, 12001);
    assert.equal(settings.saves.length, 0);
});

test("changing the port while polling stops, switches and starts polling again against the new address", async () => {
    const timers = manualTimers();
    const bridge = createMockBridge();
    const harness = createHarness({ bridge, ...timers });
    harness.client.start();
    assert.equal(harness.client.running, true);
    await timers.runNext?.();
    const before = bridge.calls.length;
    harness.client.setBridgePort(12001);
    assert.equal(harness.client.running, true, "polling restarted");
    assert.equal(harness.client.needsReadyReport, true);
    await harness.client.pollOnce();
    const after = bridge.calls.slice(before);
    assert.ok(after.length > 0);
    assert.ok(after.every((call) => call.url.startsWith("http://127.0.0.1:12001/")), JSON.stringify(after.map((call) => call.url)));
    assert.ok(after.some((call) => call.path === "/v1/session/ready"), "the new bridge gets the idle report");
});

test("setBridgePort to the same port changes nothing; it is refused while a request is being handled", async () => {
    const settings = createSettingsStore();
    const bridge = createMockBridge();
    const harness = createHarness({ bridge, settingsStore: settings });
    settings.saves.length = 0;
    assert.equal(harness.client.setBridgePort(11452), 11452);
    assert.equal(settings.saves.length, 0);
    harness.client.pendingClaim = { requestId: "req-1", attemptId: "a" };
    assert.throws(() => harness.client.setBridgePort(12001), (error) => error.code === "request_in_progress");
    assert.equal(harness.client.bridgePort, 11452);
});

test("R5: a claim's macros are handed over before generation, a claim without them hands over nothing", async () => {
    const bridge = createMockBridge();
    const macros = { emotions: "开心、害羞", speaker: "小明（1）", song_allowed: "否" };
    bridge.addRequest("req-macros", "你好", null, null, macros);
    const seen = [];
    let harness;
    harness = createHarness({
        bridge,
        generate: async (type) => {
            seen.push(structuredClone(harness.macroLog));
            harness.eventSource.emit(EVENT_TYPES.GENERATION_STARTED, type);
            await Promise.resolve();
            harness.context.chat.push({ is_user: false, is_system: false, mes: "reply" });
            harness.eventSource.emit(EVENT_TYPES.GENERATION_ENDED, harness.context.chat.length);
            return "reply";
        },
    });
    harness.macroLog = [];
    harness.client.onMacros = (value) => harness.macroLog.push(value);
    await harness.client.pollOnce();
    assert.deepEqual(harness.macroLog, [macros]);
    assert.deepEqual(seen, [[macros]], "already set when the generation starts");
});

test("R5: a failing macro callback never changes request processing", async () => {
    const bridge = createMockBridge();
    bridge.addRequest("req-macros-bad", "你好", null, null, { emotions: "开心" });
    const harness = createHarness({ bridge });
    harness.client.onMacros = () => { throw new Error("boom"); };
    await harness.client.pollOnce();
    assert.equal(bridge.jobs.get("req-macros-bad").status, "SUCCEEDED");
});

test("R5: a request without macros does not call the macro callback", async () => {
    const bridge = createMockBridge();
    bridge.addRequest("req-plain", "你好");
    const harness = createHarness({ bridge });
    let calls = 0;
    harness.client.onMacros = () => { calls += 1; };
    await harness.client.pollOnce();
    assert.equal(calls, 0);
    assert.equal(bridge.jobs.get("req-plain").status, "SUCCEEDED");
});

test("R5: fetchInjection reads the entry list and backupPreset posts the whole preset", async () => {
    const bridge = createMockBridge({ recovered: false });
    const harness = createHarness({ bridge });
    const data = await harness.client.fetchInjection();
    assert.equal(data.world_book_name, "STbridge");
    assert.deepEqual(data.macros, { emotions: "开心" });
    const preset = { prompts: [{ identifier: "main" }], prompt_order: [] };
    const saved = await harness.client.backupPreset("我的预设", preset);
    assert.equal(saved.file, "我的预设-20261106-120000.json");
    assert.deepEqual(bridge.injection.backups, [{ name: "我的预设", preset }]);
    const post = bridge.calls.find((call) => call.path === "/v1/injection/backup");
    assert.equal(post.method, "POST");
    assert.equal(post.headers["Content-Type"], "application/json");
    assert.ok(!("Authorization" in post.headers));
    bridge.injection.failBackup = true;
    await assert.rejects(() => harness.client.backupPreset("x", {}), (error) => error instanceof BridgeClientError && error.code === "backup_failed" && error.status === 500);
});

// ---- 101e: client_id and the single on-duty page ----

test("every core call carries the same random client_id, a different one per page load", async () => {
    const bridge = createMockBridge();
    bridge.addRequest("req-cid", "hello");
    const first = createHarness({ bridge });
    assert.equal(await first.client.pollOnce(), true);
    const ids = new Set(bridge.calls.map((call) => new URL(call.url).searchParams.get("client_id")));
    assert.equal(ids.size, 1);
    const [id] = [...ids];
    assert.match(id, /^[A-Za-z0-9_-]{8,64}$/);
    assert.ok(bridge.calls.length >= 4);

    const second = createHarness({ bridge: createMockBridge() });
    assert.notEqual(second.client.clientId, first.client.clientId);
});

test("a page the core says is not on duty claims nothing and shows the notice state", async () => {
    const bridge = createMockBridge();
    bridge.addRequest("req-inactive", "hello");
    const inner = bridge.fetchImpl;
    bridge.fetchImpl = async (url, options) => (
        new URL(url).pathname === "/v1/requests/pending"
            ? httpResponse(200, { active: false, reason: "other_client_active" })
            : inner(url, options)
    );
    const harness = createHarness({ bridge });
    assert.equal(await harness.client.pollOnce(), false);
    assert.ok(harness.statuses.some((status) => status.state === "other_client_active"));
    assert.equal(bridge.jobs.get("req-inactive").status, "PENDING");
    assert.ok(!bridge.calls.some((call) => call.path.endsWith("/claim")));
});

test("the notice states have readable texts", async () => {
    const { statusTextForState } = await import("../status_messages.mjs");
    for (const state of ["other_client_active", "not_active_client", "not_claim_owner", "reply_too_large"]) {
        assert.notEqual(statusTextForState(state), "桥接状态已更新", state);
    }
    assert.match(statusTextForState("other_client_active"), /另一个 ST 页面/);
});
