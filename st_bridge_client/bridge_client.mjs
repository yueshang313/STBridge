// The bridge only listens on 127.0.0.1; its port is a setting of the extension (default 11452).
const DEFAULT_BRIDGE_PORT = 11452;
const BRIDGE_PORT_MIN = 1024;
const BRIDGE_PORT_MAX = 65535;

/** The port as an integer in 1024-65535 (text such as "12001" is accepted), else null. */
function validBridgePort(value) {
    const number = typeof value === "string" && /^\s*\d+\s*$/.test(value) ? Number(value) : value;
    return Number.isInteger(number) && number >= BRIDGE_PORT_MIN && number <= BRIDGE_PORT_MAX ? number : null;
}

function bridgeUrl(port = DEFAULT_BRIDGE_PORT) {
    const valid = validBridgePort(port);
    if (valid === null) {
        throw new BridgeClientError("invalid_bridge_port");
    }
    return `http://127.0.0.1:${valid}`;
}

const BRIDGE_URL = bridgeUrl();
const MAX_REQUEST_ID_CHARS = 128;
const MAX_REQUEST_TEXT_BYTES = 8 * 1024;
const MAX_REPLY_BYTES = 32 * 1024;
const REQUEST_ID_PATTERN = /^[A-Za-z0-9_.:-]{1,128}$/;
const STARTUP_PHASES = new Set([
    "identity_read",
    "listener_setup",
    "schedule_poll",
    "button_status_render",
    "other_startup",
]);
const REQUIRED_BINDING_EVENTS = ["CHAT_CHANGED", "CHAT_LOADED", "CHARACTER_PAGE_LOADED"];
const AUTO_START_EVENTS = ["APP_READY", "CHAT_CHANGED"];
const AFFINITY_FIELD = "好感度";
const AFFINITY_MIN = 0;
const AFFINITY_MAX = 100;

export class BridgeClientError extends Error {
    constructor(code, status = 0, retryAfterMs = 0) {
        super(code);
        this.name = "BridgeClientError";
        this.code = code;
        this.status = status;
        this.retryAfterMs = retryAfterMs;
    }
}

function valueId(value) {
    if (value === undefined || value === null || value === "") {
        return null;
    }
    return String(value);
}

function sameBinding(left, right) {
    return Boolean(left && right)
        && left.characterId === right.characterId
        && left.chatId === right.chatId;
}

function safeCode(value, fallback = "protocol_error") {
    return typeof value === "string" && /^[a-z_]{1,48}$/.test(value)
        ? value
        : fallback;
}

function createAttemptId(cryptoObject = globalThis.crypto) {
    if (typeof cryptoObject?.randomUUID === "function") {
        return cryptoObject.randomUUID();
    }
    if (typeof cryptoObject?.getRandomValues === "function") {
        const bytes = cryptoObject.getRandomValues(new Uint8Array(16));
        return Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
    }
    throw new BridgeClientError("secure_random_unavailable");
}

function attachEvent(eventSource, eventName, listener, onCleanupFailure = () => {}) {
    if (!eventName || typeof eventSource?.on !== "function"
        || typeof eventSource?.removeListener !== "function") {
        throw new BridgeClientError("binding_listener_setup_failed");
    }
    const remove = () => eventSource.removeListener(eventName, listener);
    try {
        eventSource.on(eventName, listener);
    } catch {
        try {
            remove();
        } catch {
            onCleanupFailure(remove);
            throw new BridgeClientError("binding_listener_cleanup_failed");
        }
        throw new BridgeClientError("binding_listener_setup_failed");
    }
    return remove;
}

function removeListeners(removers) {
    const failed = [];
    for (const remove of removers) {
        try {
            remove();
        } catch {
            failed.push(remove);
        }
    }
    return failed;
}

function startupErrorType(error) {
    try {
        if (error instanceof TypeError) return "TypeError";
        if (error instanceof ReferenceError) return "ReferenceError";
        if (error instanceof RangeError) return "RangeError";
    } catch {
        return "OtherError";
    }
    return "OtherError";
}

export function isValidAffinity(affinity) {
    return Boolean(affinity) && typeof affinity === "object"
        && typeof affinity.character === "string"
        && affinity.character.length > 0 && affinity.character.length <= 80
        && Number.isInteger(affinity.value)
        && affinity.value >= AFFINITY_MIN && affinity.value <= AFFINITY_MAX;
}

const WRITE_VALUE_LIMIT = 100;
const MAX_AFFINITY_WRITES = 4;
const MAX_WRITE_PATH_SEGMENTS = 3;
const WRITE_SEGMENT_PATTERN = /^[0-9A-Za-z_\u3400-\u9fff]{1,40}$/u;
const FORBIDDEN_SEGMENTS = new Set(["__proto__", "constructor", "prototype"]);

function isValidWrite(write) {
    return Boolean(write) && typeof write === "object"
        && Array.isArray(write.path)
        && write.path.length >= 1 && write.path.length <= MAX_WRITE_PATH_SEGMENTS
        && write.path.every((segment) => typeof segment === "string"
            && WRITE_SEGMENT_PATTERN.test(segment) && !FORBIDDEN_SEGMENTS.has(segment))
        && (write.value === null
            || (Number.isInteger(write.value)
                && write.value >= -WRITE_VALUE_LIMIT && write.value <= WRITE_VALUE_LIMIT));
}

/** The claim's stat_data writes: 1-4 entries, each a safe path (max 3 segments) and an integer or null. */
export function isValidAffinityWrites(writes) {
    return Array.isArray(writes) && writes.length >= 1 && writes.length <= MAX_AFFINITY_WRITES
        && writes.every(isValidWrite);
}

function isPlainObject(value) {
    return value !== null && typeof value === "object" && !Array.isArray(value);
}

/** Walk stat_data along the path without touching anything; the parents are all plain objects. */
function locateNode(statData, path) {
    let parent = statData;
    for (let index = 0; index < path.length - 1; index += 1) {
        if (!Object.hasOwn(parent, path[index])) {
            return { exists: false, blocked: false };
        }
        const next = parent[path[index]];
        if (!isPlainObject(next)) {
            return { exists: false, blocked: true };
        }
        parent = next;
    }
    const key = path[path.length - 1];
    return Object.hasOwn(parent, key)
        ? { exists: true, parent, key, node: parent[key] }
        : { exists: false, blocked: false };
}

function readWriteValue(statData, path) {
    if (!isPlainObject(statData)) {
        return undefined;
    }
    const located = locateNode(statData, path);
    if (!located.exists) {
        return undefined;
    }
    return Array.isArray(located.node) ? located.node[0] : located.node;
}

function applyWrite(statData, write) {
    let parent = statData;
    for (const segment of write.path.slice(0, -1)) {
        if (!Object.hasOwn(parent, segment)) {
            parent[segment] = {};
        }
        parent = parent[segment];
    }
    const key = write.path[write.path.length - 1];
    if (Object.hasOwn(parent, key) && Array.isArray(parent[key])) {
        parent[key][0] = write.value;
    } else if (Object.hasOwn(parent, key)) {
        parent[key] = write.value;
    } else {
        // MVU variables are [value, description]; a node we have to create gets an empty description.
        parent[key] = [write.value, ""];
    }
}

/**
 * Write the claim's affinity values into stat_data on one chat message through TavernHelper.
 * Only existing structure is modified in place (MVU's merge is shallow, so stat_data is never
 * replaced): an array node gets [0] set, a missing node becomes [value, ""] with any missing
 * parent objects created, and a null value is written only where the node already exists.
 * Every entry is read back. Returns { ok, reason, results: [{ path, ok, reason, skipped }] }.
 */
export function injectAffinityWrites(helper, messageIndex, writes) {
    if (!isValidAffinityWrites(writes)) {
        return { ok: false, reason: "invalid_affinity", results: [] };
    }
    if (typeof helper?.getVariables !== "function" || typeof helper?.updateVariablesWith !== "function") {
        return { ok: false, reason: "api_missing", results: [] };
    }
    const option = { type: "message", message_id: messageIndex };
    let statData;
    try {
        statData = helper.getVariables(option)?.stat_data;
    } catch {
        return { ok: false, reason: "read_failed", results: [] };
    }
    if (!isPlainObject(statData)) {
        // MVU has not initialised stat_data on this message; never invent the root.
        return { ok: false, reason: "variable_missing", results: [] };
    }

    const results = writes.map((write) => ({ path: write.path.join("."), ok: true, reason: null, skipped: false }));
    const pending = [];
    writes.forEach((write, index) => {
        const located = locateNode(statData, write.path);
        if (!located.exists && located.blocked) {
            results[index].ok = false;
            results[index].reason = "path_blocked";
        } else if (!located.exists && write.value === null) {
            results[index].skipped = true;
        } else if (located.exists && !Array.isArray(located.node)
            && typeof located.node !== "number" && located.node !== null) {
            results[index].ok = false;
            results[index].reason = "node_type_invalid";
        } else {
            pending.push(index);
        }
    });

    if (pending.length > 0) {
        try {
            helper.updateVariablesWith((variables) => {
                for (const index of pending) {
                    applyWrite(variables.stat_data, writes[index]);
                }
                return variables;
            }, option);
        } catch {
            for (const index of pending) {
                results[index].ok = false;
                results[index].reason = "write_failed";
            }
            return summariseWrites(results);
        }
        let after;
        try {
            after = helper.getVariables(option)?.stat_data;
        } catch {
            for (const index of pending) {
                results[index].ok = false;
                results[index].reason = "readback_failed";
            }
            return summariseWrites(results);
        }
        for (const index of pending) {
            if (readWriteValue(after, writes[index].path) !== writes[index].value) {
                results[index].ok = false;
                results[index].reason = "readback_mismatch";
            }
        }
    }
    return summariseWrites(results);
}

function summariseWrites(results) {
    const failed = results.find((entry) => !entry.ok);
    return { ok: !failed, reason: failed ? failed.reason : null, results };
}

function readAffinityValue(variables, character) {
    const current = variables?.stat_data?.[character]?.[AFFINITY_FIELD];
    return Array.isArray(current) ? current[0] : current;
}

/**
 * Write stat_data.<character>.好感度[0] on one chat message through TavernHelper
 * (JS-Slash-Runner). Only the number is replaced; MVU's description in [1] stays.
 * Returns { ok: true, before } or { ok: false, reason }.
 */
export function injectAffinity(helper, messageIndex, affinity) {
    if (!isValidAffinity(affinity)) {
        return { ok: false, reason: "invalid_affinity" };
    }
    if (typeof helper?.getVariables !== "function" || typeof helper?.updateVariablesWith !== "function") {
        return { ok: false, reason: "api_missing" };
    }
    const option = { type: "message", message_id: messageIndex };
    let current;
    try {
        current = helper.getVariables(option)?.stat_data?.[affinity.character]?.[AFFINITY_FIELD];
    } catch {
        return { ok: false, reason: "read_failed" };
    }
    if (!Array.isArray(current) && typeof current !== "number") {
        // MVU has not initialised this character on this message; never invent the structure.
        return { ok: false, reason: "variable_missing" };
    }
    const before = Array.isArray(current) ? current[0] : current;
    try {
        helper.updateVariablesWith((variables) => {
            const holder = variables.stat_data[affinity.character];
            if (Array.isArray(holder[AFFINITY_FIELD])) {
                holder[AFFINITY_FIELD][0] = affinity.value;
            } else {
                holder[AFFINITY_FIELD] = affinity.value;
            }
            return variables;
        }, option);
    } catch {
        return { ok: false, reason: "write_failed" };
    }
    let after;
    try {
        after = readAffinityValue(helper.getVariables(option), affinity.character);
    } catch {
        return { ok: false, reason: "readback_failed" };
    }
    return after === affinity.value ? { ok: true, before } : { ok: false, reason: "readback_mismatch" };
}

/**
 * Host-side acceptance check: write a test value to the latest message, read it back
 * exactly as the world book does (Prompt Template getvar), then restore the old value.
 */
export async function runAffinityDiagnostic({ helper, ejs, chat, character, value }) {
    const report = { ok: false, character, written: value };
    if (!Array.isArray(chat) || chat.length === 0) {
        return { ...report, reason: "chat_empty" };
    }
    const messageIndex = chat.length - 1;
    report.messageIndex = messageIndex;
    const written = injectAffinity(helper, messageIndex, { character, value });
    if (!written.ok) {
        return { ...report, reason: written.reason };
    }
    report.before = written.before;
    try {
        report.helperReadback = readAffinityValue(
            helper.getVariables({ type: "message", message_id: messageIndex }),
            character,
        );
    } catch {
        report.helperReadback = null;
    }
    if (typeof ejs?.evalTemplate === "function") {
        const variablePath = JSON.stringify(`stat_data.${character}.${AFFINITY_FIELD}[0]`);
        try {
            const output = await ejs.evalTemplate(`<%= getvar(${variablePath}) %>`);
            report.worldbookReadback = String(output).trim();
        } catch {
            report.worldbookReadback = null;
            report.reason = "template_failed";
        }
    } else {
        report.worldbookReadback = null;
        report.reason = "template_missing";
    }
    if (typeof written.before === "number") {
        report.restored = injectAffinity(helper, messageIndex, { character, value: written.before }).ok;
    }
    report.ok = report.worldbookReadback === String(value) && report.helperReadback === value;
    return report;
}

export class BridgeClient {
    constructor({
        fetchImpl = globalThis.fetch,
        getContext,
        isGenerating,
        isLocallyBusy = () => false,
        sendMessageAsUser,
        generate,
        // Optional: scheduled (proactive) turns insert a narrator line instead of a user message.
        sendNarratorMessage = null,
        requestTimeoutMs = 5000,
        generationTimeoutMs = 120000,
        pollIntervalMs = 1150,
        setTimeoutImpl = globalThis.setTimeout,
        clearTimeoutImpl = globalThis.clearTimeout,
        randomId = () => createAttemptId(),
        onStatus = () => {},
        onDiagnostic = () => {},
        settingsStore = null,
        getVariableApi = () => globalThis.TavernHelper,
        onAffinityResult = () => {},
        onMacros = () => {},
    }) {
        if (typeof fetchImpl !== "function"
            || typeof getContext !== "function" || typeof isGenerating !== "function"
            || typeof sendMessageAsUser !== "function" || typeof generate !== "function") {
            throw new TypeError("BridgeClient requires its browser and ST adapters");
        }

        this.fetchImpl = fetchImpl;
        this.getContext = getContext;
        this.isGenerating = isGenerating;
        this.isLocallyBusy = isLocallyBusy;
        this.sendMessageAsUser = sendMessageAsUser;
        this.sendNarratorMessage = typeof sendNarratorMessage === "function" ? sendNarratorMessage : null;
        this.generate = generate;
        this.requestTimeoutMs = requestTimeoutMs;
        this.generationTimeoutMs = generationTimeoutMs;
        this.pollIntervalMs = pollIntervalMs;
        this.setTimeoutImpl = setTimeoutImpl.bind(globalThis);
        this.clearTimeoutImpl = clearTimeoutImpl.bind(globalThis);
        this.randomId = randomId;
        this.onStatus = onStatus;
        this.onDiagnostic = onDiagnostic;
        this.getVariableApi = getVariableApi;
        this.onAffinityResult = onAffinityResult;
        // R5: this turn's values of the {{stbridge_*}} macros, handed over before anything is generated.
        this.onMacros = onMacros;
        this.lastAffinityResult = null;
        this.bridgePort = DEFAULT_BRIDGE_PORT;

        this.binding = null;
        this.bindingEpoch = 0;
        this.running = false;
        this.busy = false;
        this.pendingClaim = null;
        this.activeRequestId = null;
        this.pollTimer = null;
        this.bindingListeners = [];
        this.orphanedBindingListeners = [];
        this.bindingEventSource = null;
        this.bindingListenersReady = false;
        this.lastStatus = null;
        this.retryAfterMs = 0;
        // Set on start and on 503 recovery_required; cleared once AstrBot reports RECOVERED.
        this.needsReadyReport = false;
        this.enabled = true;
        this.autoStartListeners = [];
        this.settingsStore = settingsStore;
        this._restoreSettings();
    }

    _restoreSettings() {
        let saved = null;
        try {
            saved = this.settingsStore?.load?.() ?? null;
        } catch {
            saved = null;
        }
        const binding = saved?.binding;
        if (binding
            && typeof binding.characterId === "string" && binding.characterId
            && typeof binding.chatId === "string" && binding.chatId) {
            this.binding = { characterId: binding.characterId, chatId: binding.chatId };
        }
        this.enabled = saved?.enabled !== false;
        this.bridgePort = validBridgePort(saved?.bridgePort) ?? DEFAULT_BRIDGE_PORT;
    }

    get bridgeUrl() {
        return bridgeUrl(this.bridgePort);
    }

    /**
     * Use another bridge port at once: polling stops, the new address is saved and polling starts again
     * (a new bridge needs the idle report again). Refused while a request is being handled.
     */
    setBridgePort(value) {
        const port = validBridgePort(value);
        if (port === null) {
            throw new BridgeClientError("invalid_bridge_port");
        }
        if (this.hasWork) {
            throw new BridgeClientError("request_in_progress");
        }
        if (port === this.bridgePort) {
            return port;
        }
        const wasRunning = this.running;
        if (wasRunning) {
            this._halt();
        }
        this.bridgePort = port;
        try {
            this.settingsStore?.save?.({ bridgePort: port });
        } catch {
            // Persistence is best effort; the in-memory state stays authoritative.
        }
        this.needsReadyReport = true;
        this._status("bridge_port_saved");
        if (wasRunning) {
            this.start();
        }
        return port;
    }

    _persistSettings() {
        try {
            this.settingsStore?.save?.({
                binding: this.binding ? { ...this.binding } : null,
                enabled: this.enabled,
            });
        } catch {
            // Persistence is best effort; the in-memory state stays authoritative.
        }
    }

    get hasWork() {
        return this.busy || this.pendingClaim !== null || this.activeRequestId !== null;
    }

    bindCurrent() {
        if (this.running || this.hasWork) {
            throw new BridgeClientError("request_in_progress");
        }
        const identity = this._readIdentity();
        this._ensureBindingListeners();
        this.binding = identity;
        this.bindingEpoch += 1;
        this._persistSettings();
        this._status("bound");
        return { ...identity };
    }

    clearBinding() {
        this.binding = null;
        this.bindingEpoch += 1;
        this._persistSettings();
        if (!this.hasWork) {
            this._status("unbound");
        }
    }

    start() {
        let phase = "other_startup";
        let startupStarted = false;
        try {
            if (!this.binding) {
                throw new BridgeClientError("binding_required");
            }

            phase = "identity_read";
            if (!sameBinding(this.binding, this._readIdentity())) {
                throw new BridgeClientError("binding_mismatch");
            }
            if (this.running) {
                return;
            }

            phase = "listener_setup";
            this._ensureBindingListeners();
            this.running = true;
            startupStarted = true;

            phase = "other_startup";
            this.needsReadyReport = true;
            if (!this.enabled) {
                this.enabled = true;
                this._persistSettings();
            }
            this._status("polling");

            phase = "schedule_poll";
            this._schedulePoll(0);
        } catch (error) {
            if (startupStarted) {
                this._rollbackStartup();
                this._status("stopped");
            }
            if (!(error instanceof BridgeClientError)
                || ["binding_listener_setup_failed", "binding_listener_cleanup_failed"].includes(error.code)) {
                this.reportStartupDiagnostic(phase, error);
            }
            throw error;
        }
    }

    reportStartupDiagnostic(phase, error) {
        if (!STARTUP_PHASES.has(phase)) {
            return false;
        }

        const diagnostic = Object.freeze({ phase, errorType: startupErrorType(error) });
        try {
            this.onDiagnostic(diagnostic);
        } catch {
            // Diagnostics must not affect bridge control flow.
        }
        return true;
    }

    _rollbackStartup() {
        this.running = false;
        const timer = this.pollTimer;
        this.pollTimer = null;
        if (timer !== null) {
            try {
                this.clearTimeoutImpl(timer);
            } catch {
                // Keep the client stopped even if timer cleanup fails.
            }
        }
    }

    stop() {
        if (this.enabled) {
            this.enabled = false;
            this._persistSettings();
        }
        this._halt();
    }

    _halt() {
        this.running = false;
        if (this.pollTimer !== null) {
            this.clearTimeoutImpl(this.pollTimer);
            this.pollTimer = null;
        }
        this._status(this.hasWork ? "stopping_after_request" : "stopped");
    }

    dispose() {
        this._halt();
        const failed = [
            ...removeListeners(this.bindingListeners),
            ...removeListeners(this.orphanedBindingListeners),
            ...removeListeners(this.autoStartListeners),
        ];
        this.autoStartListeners = [];
        this.bindingListeners = [];
        this.orphanedBindingListeners = failed;
        this.bindingListenersReady = false;
        this.bindingEventSource = null;
        if (failed.length > 0) {
            throw new BridgeClientError("binding_listener_cleanup_failed");
        }
    }

    autoStart() {
        if (!this.enabled || !this.binding || this.running) {
            return false;
        }
        let identity;
        try {
            identity = this._readIdentity();
        } catch {
            return false;
        }
        if (!sameBinding(this.binding, identity)) {
            return false;
        }
        try {
            this.start();
            return true;
        } catch {
            return false;
        }
    }

    attachAutoStart() {
        if (this.autoStartListeners.length > 0) {
            return true;
        }
        let eventSource;
        let eventTypes;
        try {
            const context = this.getContext();
            eventSource = context?.eventSource;
            eventTypes = context?.eventTypes ?? {};
        } catch {
            return false;
        }
        if (typeof eventSource?.on !== "function" || typeof eventSource?.removeListener !== "function") {
            return false;
        }
        // Defer so ST has finished switching chats before the binding is compared.
        const handler = () => {
            try {
                this.setTimeoutImpl(() => this.autoStart(), 0);
            } catch {
                // A failed auto-start leaves the manual buttons available.
            }
        };
        for (const key of AUTO_START_EVENTS) {
            const name = eventTypes[key];
            if (typeof name !== "string" || !name) {
                continue;
            }
            try {
                eventSource.on(name, handler);
                this.autoStartListeners.push(() => eventSource.removeListener(name, handler));
            } catch {
                // Keep any listeners that did register.
            }
        }
        return this.autoStartListeners.length > 0;
    }

    _isClientIdle() {
        if (this.pendingClaim !== null || this.activeRequestId !== null) {
            return false;
        }
        try {
            return !this.isGenerating() && !this.isLocallyBusy();
        } catch {
            return false;
        }
    }

    async _reportReadyIfIdle() {
        if (!this._isClientIdle()) {
            return false;
        }
        try {
            const result = await this._request("/v1/session/ready", {
                method: "POST",
                body: { generating: false, active_request: false },
            });
            if (result?.state === "RECOVERED") {
                this.needsReadyReport = false;
                return true;
            }
        } catch {
            // The next poll retries the report.
        }
        return false;
    }

    async pollOnce() {
        if (this.busy) {
            return false;
        }
        if (!this.binding) {
            this._status("binding_required");
            return false;
        }

        let currentIdentity;
        try {
            currentIdentity = this._readIdentity();
        } catch (error) {
            this._status(error instanceof BridgeClientError ? error.code : "context_read_failed");
            return false;
        }
        if (!sameBinding(this.binding, currentIdentity)) {
            this._status("binding_mismatch");
            return false;
        }

        this.busy = true;
        try {
            if (this.pendingClaim) {
                return await this._claimAndProcess(this.pendingClaim);
            }

            if (this.needsReadyReport && await this._reportReadyIfIdle()) {
                this._status("bridge_ready");
            }

            this._status("checking_requests");
            const pending = await this._request("/v1/requests/pending");
            if (pending === null) {
                this._status(this.running ? "polling" : "idle");
                return false;
            }
            if (!pending || typeof pending.request_id !== "string"
                || pending.request_id.length > MAX_REQUEST_ID_CHARS
                || !REQUEST_ID_PATTERN.test(pending.request_id)) {
                throw new BridgeClientError("invalid_pending_response");
            }

            this.pendingClaim = {
                requestId: pending.request_id,
                attemptId: this.randomId(),
            };
            return await this._claimAndProcess(this.pendingClaim);
        } catch (error) {
            const code = safeCode(error?.code, "network_error");
            if (code === "recovery_required") {
                this.needsReadyReport = true;
            }
            this._status(code, this.pendingClaim?.requestId ?? null);
            if (error instanceof BridgeClientError && error.retryAfterMs > 0) {
                this.retryAfterMs = error.retryAfterMs;
            }
            return false;
        } finally {
            this.busy = false;
            if (!this.running && !this.hasWork) {
                this._status("stopped");
            } else if (this.lastStatus) {
                this._status(this.lastStatus.state, this.lastStatus.requestId);
            }
        }
    }

    _schedulePoll(delayMs = this.pollIntervalMs) {
        if (!this.running || this.pollTimer !== null) {
            return;
        }
        this.pollTimer = this.setTimeoutImpl(async () => {
            this.pollTimer = null;
            if (!this.running) {
                return;
            }
            await this.pollOnce();
            const delay = this.retryAfterMs || this.pollIntervalMs;
            this.retryAfterMs = 0;
            this._schedulePoll(delay);
        }, delayMs);
    }

    async _claimAndProcess(claimAttempt) {
        let claimed;
        try {
            claimed = await this._request(
                `/v1/requests/${encodeURIComponent(claimAttempt.requestId)}/claim`,
                {
                    method: "POST",
                    body: { claim_attempt_id: claimAttempt.attemptId },
                },
            );
        } catch (error) {
            // 503 recovery_required means AstrBot reloaded; the old request no longer exists.
            if (error instanceof BridgeClientError
                && ([404, 409, 410].includes(error.status) || error.code === "recovery_required")) {
                this.pendingClaim = null;
            }
            throw error;
        }

        if (claimed?.request_id !== claimAttempt.requestId
            || claimed.status !== "CLAIMED"
            || typeof claimed.content !== "string") {
            throw new BridgeClientError("invalid_claim_response");
        }
        if (new TextEncoder().encode(claimed.content).length > MAX_REQUEST_TEXT_BYTES) {
            this.pendingClaim = null;
            await this._failBeforeGeneration(
                claimAttempt.requestId,
                "request_too_large",
                this.bindingEpoch,
            );
            return false;
        }

        this.pendingClaim = null;
        this.activeRequestId = claimed.request_id;
        const epoch = this.bindingEpoch;
        this._status("request_claimed", claimed.request_id);
        try {
            await this._processClaimed(claimed, epoch);
        } finally {
            this.activeRequestId = null;
        }
        return true;
    }

    async _processClaimed(claimed, epoch) {
        const requestId = claimed.request_id;
        let status = await this._getStatus(requestId);
        if (status.status === "EXPIRED") {
            await this._confirmNotStarted(requestId);
            this._status("request_expired", requestId);
            return;
        }
        if (status.status !== "CLAIMED" || status.generation_status !== "UNKNOWN") {
            this._status("request_state_conflict", requestId);
            return;
        }

        if (!this._bindingIsCurrent(epoch)) {
            await this._rejectBeforeGeneration(requestId, "binding_mismatch", epoch);
            return;
        }
        if (this.isGenerating() || this.isLocallyBusy()) {
            await this._rejectBeforeGeneration(requestId, "st_busy", epoch);
            return;
        }

        const context = this.getContext();
        if (!Array.isArray(context.chat)) {
            await this._failBeforeGeneration(requestId, "extension_error", epoch);
            return;
        }

        if (claimed.macros !== undefined && claimed.macros !== null) {
            try {
                this.onMacros(claimed.macros);
            } catch {
                // The macros are a convenience for the prompt; they never change request processing.
            }
        }

        // A scheduled turn has no user message: the request text goes in as a narrator line.
        const proactive = claimed.kind === "proactive";
        if (proactive && !this.sendNarratorMessage) {
            await this._failBeforeGeneration(requestId, "extension_error", epoch);
            return;
        }

        this._status("adding_user_message", requestId);
        let appendedMessage;
        const lengthBefore = context.chat.length;
        try {
            if (proactive) {
                await this.sendNarratorMessage(claimed.content);
                const chatNow = this.getContext().chat;
                // The narrator command pushes one message at the end; it returns no message object.
                appendedMessage = Array.isArray(chatNow) && chatNow.length === lengthBefore + 1
                    ? chatNow[lengthBefore]
                    : undefined;
            } else {
                appendedMessage = await this.sendMessageAsUser(claimed.content);
            }
        } catch {
            await this._failBeforeGeneration(requestId, "extension_error", epoch);
            return;
        }

        if (!this._bindingIsCurrent(epoch)) {
            await this._rejectBeforeGeneration(requestId, "chat_changed", epoch);
            return;
        }

        const afterAppend = this.getContext();
        if (!Array.isArray(afterAppend.chat) || !afterAppend.chat.length
            || !appendedMessage || typeof appendedMessage !== "object") {
            await this._failBeforeGeneration(requestId, "extension_error", epoch);
            return;
        }
        const userMessageIndex = afterAppend.chat.indexOf(appendedMessage);
        const userMessage = afterAppend.chat[userMessageIndex];
        if (userMessageIndex < 0 || !userMessage
            || (proactive
                ? userMessage.is_user !== false || userMessage.extra?.type !== "narrator"
                : userMessage.is_user !== true)) {
            await this._failBeforeGeneration(requestId, "extension_error", epoch);
            return;
        }

        // sendMessageAsUser has already run MESSAGE_SENT, so Prompt Template copied the previous
        // message's variables onto this one; overwrite the speaker's value before generating.
        if (claimed.affinity_writes !== undefined && claimed.affinity_writes !== null) {
            this._injectAffinity(requestId, claimed.affinity_writes, userMessageIndex);
        }

        if (this.isGenerating() || this.isLocallyBusy()) {
            await this._rejectBeforeGeneration(requestId, "st_busy", epoch);
            return;
        }

        status = await this._getStatus(requestId);
        if (status.status === "EXPIRED") {
            await this._confirmNotStarted(requestId);
            this._status("request_expired", requestId);
            return;
        }
        if (status.status !== "CLAIMED" || status.generation_status !== "UNKNOWN") {
            await this._confirmNotStarted(requestId);
            this._status("request_state_conflict", requestId);
            return;
        }

        await this._runNativeGeneration(requestId, userMessageIndex, appendedMessage, epoch, proactive);
    }

    _injectAffinity(requestId, writes, messageIndex) {
        let result;
        try {
            result = injectAffinityWrites(this.getVariableApi(), messageIndex, writes);
        } catch {
            result = { ok: false, reason: "write_failed", results: [] };
        }
        this.lastAffinityResult = Object.freeze({
            ok: result.ok,
            reason: result.reason ?? null,
            writes: Object.freeze(result.results.map((entry) => Object.freeze({ ...entry }))),
        });
        this._status(result.ok ? "affinity_injected" : "affinity_inject_failed", requestId);
        try {
            this.onAffinityResult(this.lastAffinityResult);
        } catch {
            // Reporting must not change request processing.
        }
        return result.ok;
    }

    /** R5: the rule entries (and default macro values) the console holds. */
    async fetchInjection() {
        return this._request("/v1/injection");
    }

    /** R5: have the core keep a copy of the ST preset before the extension changes it; resolves to {file}. */
    async backupPreset(name, preset) {
        return this._request("/v1/injection/backup", { method: "POST", body: { name, preset } });
    }

    async _runNativeGeneration(requestId, userMessageIndex, appendedMessage, epoch, narrator = false) {
        const context = this.getContext();
        const eventTypes = context.eventTypes ?? {};
        const eventSource = context.eventSource;
        const neededEvents = [
            eventTypes.GENERATION_STARTED,
            eventTypes.GENERATION_STOPPED,
            eventTypes.GENERATION_ENDED,
        ];
        if (neededEvents.some((name) => typeof name !== "string")
            || typeof eventSource?.on !== "function"
            || typeof eventSource?.removeListener !== "function") {
            await this._failBeforeGeneration(requestId, "extension_error", epoch);
            return;
        }

        let started = false;
        let ended = null;
        let resolveStarted;
        let resolveEnded;
        const startedPromise = new Promise((resolve) => { resolveStarted = resolve; });
        const endedPromise = new Promise((resolve) => { resolveEnded = resolve; });
        let runningConfirmation = null;
        const removeStarted = attachEvent(eventSource, eventTypes.GENERATION_STARTED, (type) => {
            if (type === "normal" && !started) {
                started = true;
                runningConfirmation = this._confirmGenerationState(requestId, "RUNNING");
                resolveStarted(true);
                this._status("generation_started", requestId);
            }
        });
        const removeEnded = attachEvent(eventSource, eventTypes.GENERATION_ENDED, (chatLength) => {
            if (!started || ended) {
                return;
            }
            ended = { stopped: false, chatLength };
            resolveEnded(ended);
        });
        const removeStopped = attachEvent(eventSource, eventTypes.GENERATION_STOPPED, () => {
            if (!started || ended) {
                return;
            }
            ended = { stopped: true, chatLength: null };
            resolveEnded(ended);
        });

        let timedOut = false;
        const timeoutId = this.setTimeoutImpl(() => {
            timedOut = true;
            this._status("generation_timeout_waiting", requestId);
        }, this.generationTimeoutMs);

        let generationPromise;
        let generationWasInvoked = false;
        try {
            const beforeGeneration = this.getContext();
            const currentChat = Array.isArray(beforeGeneration.chat) ? beforeGeneration.chat : [];
            if (!this._bindingIsCurrent(epoch)
                || currentChat.length !== userMessageIndex + 1
                || currentChat[userMessageIndex] !== appendedMessage
                || currentChat[userMessageIndex]?.is_user !== !narrator) {
                await this._rejectBeforeGeneration(requestId, "chat_changed", epoch);
                return;
            }
            if (this.isGenerating() || this.isLocallyBusy()) {
                await this._rejectBeforeGeneration(requestId, "st_busy", epoch);
                return;
            }

            generationWasInvoked = true;
            generationPromise = Promise.resolve().then(() =>
                this.generate("normal", { automatic_trigger: true }),
            ).then(
                (value) => ({ ok: true, value }),
                () => ({ ok: false }),
            );

            const first = await Promise.race([
                startedPromise.then(() => ({ kind: "started" })),
                endedPromise.then((event) => ({ kind: "ended", event })),
                generationPromise.then((outcome) => ({ kind: "settled", outcome })),
            ]);

            if (first.kind === "settled" && !started && !this.isGenerating()) {
                await this._finishWithoutGeneration(requestId, first.outcome, epoch);
                return;
            }

            if (first.kind !== "ended" && first.kind !== "settled") {
                ended = await endedPromise;
            } else if (first.kind === "ended") {
                ended = first.event;
            } else {
                ended = await endedPromise;
            }

            const outcome = first.kind === "settled"
                ? first.outcome
                : await generationPromise;

            if (!started || !ended || this.isGenerating()) {
                this._status("generation_state_unconfirmed", requestId);
                return;
            }
            if (runningConfirmation) {
                await runningConfirmation;
            }

            const finished = await this._confirmGenerationFinished(requestId);
            if (!finished) {
                this._status("generation_state_unconfirmed", requestId);
                return;
            }

            const currentStatus = await this._getStatus(requestId);
            if (currentStatus.status === "EXPIRED") {
                this._status("request_expired", requestId);
                return;
            }
            if (currentStatus.status !== "CLAIMED"
                || currentStatus.generation_status !== "FINISHED") {
                this._status("request_state_conflict", requestId);
                return;
            }

            if (!this._bindingIsCurrent(epoch)) {
                await this._postTerminal(requestId, "reject", { error_code: "chat_changed" }, "REJECTED");
                this._status("chat_changed", requestId);
                return;
            }

            if (!outcome.ok || ended.stopped) {
                await this._postTerminal(
                    requestId,
                    "failure",
                    { error_code: timedOut ? "generation_timeout" : "generation_failed" },
                    "FAILED",
                );
                this._status(timedOut ? "generation_timeout" : "generation_failed", requestId);
                return;
            }

            const finalContext = this.getContext();
            const chat = Array.isArray(finalContext.chat) ? finalContext.chat : [];
            if (!this._bindingIsCurrent(epoch)
                || !ended
                || ended.chatLength !== chat.length
                || chat.length !== userMessageIndex + 2
                || chat[userMessageIndex] !== appendedMessage) {
                await this._postTerminal(requestId, "reject", { error_code: "chat_changed" }, "REJECTED");
                this._status("chat_changed", requestId);
                return;
            }

            const replyMessage = chat[userMessageIndex + 1];
            const reply = typeof replyMessage?.mes === "string" ? replyMessage.mes : "";
            if (!reply.trim() || replyMessage.is_user || replyMessage.is_system) {
                await this._postTerminal(
                    requestId,
                    "failure",
                    { error_code: "generation_failed" },
                    "FAILED",
                );
                this._status("generation_failed", requestId);
                return;
            }

            if (new TextEncoder().encode(reply).length > MAX_REPLY_BYTES) {
                await this._postTerminal(
                    requestId,
                    "failure",
                    { error_code: "extension_error" },
                    "FAILED",
                );
                this._status("reply_too_large", requestId);
                return;
            }

            const result = await this._postTerminal(
                requestId,
                "result",
                { reply },
                "SUCCEEDED",
            );
            this._status(result ? "request_succeeded" : "result_unconfirmed", requestId);
        } catch (error) {
            if (!generationWasInvoked) {
                await this._failBeforeGeneration(requestId, "extension_error", epoch);
            } else {
                this._status(safeCode(error?.code, "generation_state_unconfirmed"), requestId);
            }
        } finally {
            this.clearTimeoutImpl(timeoutId);
            removeStarted();
            removeEnded();
            removeStopped();
        }
    }

    async _finishWithoutGeneration(requestId, outcome, epoch) {
        const confirmed = await this._confirmNotStarted(requestId);
        if (!confirmed) {
            this._status("generation_state_unconfirmed", requestId);
            return;
        }
        const status = await this._getStatus(requestId);
        if (status.status === "EXPIRED") {
            this._status("request_expired", requestId);
            return;
        }
        if (status.status !== "CLAIMED") {
            this._status("request_state_conflict", requestId);
            return;
        }
        const changed = !this._bindingIsCurrent(epoch);
        await this._postTerminal(
            requestId,
            changed ? "reject" : "failure",
            changed ? { error_code: "chat_changed" } : { error_code: "extension_error" },
            changed ? "REJECTED" : "FAILED",
        );
        this._status(changed ? "chat_changed" : (outcome.ok ? "generation_not_started" : "generation_failed"), requestId);
    }

    async _rejectBeforeGeneration(requestId, code, epoch) {
        await this._finishBeforeGeneration(requestId, "reject", code, epoch);
    }

    async _failBeforeGeneration(requestId, code, epoch) {
        await this._finishBeforeGeneration(requestId, "failure", code, epoch);
    }

    async _finishBeforeGeneration(requestId, action, code, epoch) {
        const confirmed = await this._confirmNotStarted(requestId);
        if (!confirmed) {
            this._status("generation_state_unconfirmed", requestId);
            return;
        }
        const status = await this._getStatus(requestId);
        if (status.status === "EXPIRED") {
            this._status("request_expired", requestId);
            return;
        }
        if (status.status !== "CLAIMED") {
            this._status("request_state_conflict", requestId);
            return;
        }
        if (action === "reject" && (code === "binding_mismatch" || code === "chat_changed")) {
            await this._postTerminal(requestId, "reject", { error_code: code }, "REJECTED");
        } else if (action === "reject") {
            await this._postTerminal(requestId, "reject", { error_code: "st_busy" }, "REJECTED");
        } else {
            const failureCode = code === "request_too_large" ? code : "extension_error";
            await this._postTerminal(requestId, "failure", { error_code: failureCode }, "FAILED");
        }
        this._status(this._bindingIsCurrent(epoch) ? code : "chat_changed", requestId);
    }

    async _confirmNotStarted(requestId) {
        for (let attempt = 0; attempt < 2; attempt += 1) {
            try {
                const result = await this._request(
                    `/v1/requests/${encodeURIComponent(requestId)}/generation`,
                    { method: "POST", body: { state: "NOT_STARTED" } },
                );
                if (result?.request_id === requestId
                    && result.generation_status === "NOT_STARTED") {
                    return true;
                }
            } catch {
                // A status read distinguishes a lost response from a rejected transition.
            }
            try {
                const status = await this._getStatus(requestId);
                if (status.generation_status === "NOT_STARTED") {
                    return true;
                }
                if (status.generation_status !== "UNKNOWN") {
                    return false;
                }
            } catch {
                return false;
            }
        }
        return false;
    }

    async _confirmGenerationState(requestId, state) {
        try {
            const result = await this._request(
                `/v1/requests/${encodeURIComponent(requestId)}/generation`,
                { method: "POST", body: { state } },
            );
            return result?.request_id === requestId
                && result.generation_status === state;
        } catch {
            try {
                const status = await this._getStatus(requestId);
                if (status.generation_status === state) {
                    return true;
                }
                if (state === "RUNNING" && status.generation_status === "UNKNOWN") {
                    const retry = await this._request(
                        `/v1/requests/${encodeURIComponent(requestId)}/generation`,
                        { method: "POST", body: { state } },
                    );
                    return retry?.request_id === requestId
                        && retry.generation_status === state;
                }
            } catch {
                return false;
            }
            return false;
        }
    }

    async _confirmGenerationFinished(requestId) {
        for (let attempt = 0; attempt < 2; attempt += 1) {
            try {
                const result = await this._request(
                    `/v1/requests/${encodeURIComponent(requestId)}/generation`,
                    { method: "POST", body: { state: "FINISHED" } },
                );
                if (result?.request_id === requestId
                    && result.generation_status === "FINISHED") {
                    return true;
                }
            } catch {
                // The server may have applied the confirmation before the response was lost.
            }
            try {
                const status = await this._getStatus(requestId);
                if (status.generation_status === "FINISHED") {
                    return true;
                }
                if (!["UNKNOWN", "RUNNING"].includes(status.generation_status)) {
                    return false;
                }
            } catch {
                return false;
            }
        }
        return false;
    }

    async _postTerminal(requestId, route, body, expectedStatus) {
        for (let attempt = 0; attempt < 2; attempt += 1) {
            try {
                const result = await this._request(
                    `/v1/requests/${encodeURIComponent(requestId)}/${route}`,
                    { method: "POST", body },
                );
                if (result?.request_id === requestId && result.status === expectedStatus) {
                    return true;
                }
            } catch {
                // Querying status avoids treating a lost response as a failed commit.
            }
            try {
                const status = await this._getStatus(requestId);
                if (status.status === expectedStatus) {
                    return true;
                }
                if (status.status !== "CLAIMED") {
                    return false;
                }
            } catch {
                return false;
            }
        }
        return false;
    }

    async _getStatus(requestId) {
        const status = await this._request(
            `/v1/requests/${encodeURIComponent(requestId)}`,
        );
        if (status?.request_id !== requestId
            || !["PENDING", "CLAIMED", "SUCCEEDED", "REJECTED", "FAILED", "EXPIRED"].includes(status.status)
            || !["UNKNOWN", "NOT_STARTED", "RUNNING", "FINISHED"].includes(status.generation_status)) {
            throw new BridgeClientError("invalid_status_response");
        }
        return status;
    }

    async _request(path, { method = "GET", body } = {}) {
        const controller = new AbortController();
        const timeoutId = this.setTimeoutImpl(
            () => controller.abort(),
            this.requestTimeoutMs,
        );
        const headers = {};
        const options = {
            method,
            mode: "cors",
            credentials: "omit",
            cache: "no-store",
            redirect: "error",
            referrerPolicy: "no-referrer",
            headers,
            signal: controller.signal,
        };
        if (body !== undefined) {
            headers["Content-Type"] = "application/json";
            options.body = JSON.stringify(body);
        }

        let response;
        try {
            response = await this.fetchImpl(`${this.bridgeUrl}${path}`, options);
        } catch (error) {
            throw new BridgeClientError(
                error?.name === "AbortError" ? "network_timeout" : "network_error",
            );
        } finally {
            this.clearTimeoutImpl(timeoutId);
        }

        if (response.status === 204) {
            return null;
        }

        let data;
        try {
            data = await response.json();
        } catch {
            throw new BridgeClientError("invalid_response", response.status);
        }
        if (!response.ok) {
            const retryAfter = Number(response.headers?.get?.("Retry-After"));
            throw new BridgeClientError(
                safeCode(data?.error, "http_error"),
                response.status,
                Number.isFinite(retryAfter) && retryAfter > 0 ? retryAfter * 1000 : 0,
            );
        }
        return data;
    }

    _readIdentity() {
        let context;
        try {
            context = this.getContext();
        } catch {
            throw new BridgeClientError("context_read_failed");
        }

        if (valueId(context?.groupId) !== null) {
            throw new BridgeClientError("group_context_unsupported");
        }

        const characterId = valueId(context?.characterId);
        if (!characterId) {
            throw new BridgeClientError("character_id_missing");
        }

        let chatId = context?.chatId;
        if (chatId === undefined && typeof context?.getCurrentChatId === "function") {
            try {
                chatId = context.getCurrentChatId();
            } catch {
                chatId = null;
            }
        }
        chatId = valueId(chatId);
        if (!chatId) {
            throw new BridgeClientError("chat_id_missing");
        }
        if (!Array.isArray(context?.chat)) {
            throw new BridgeClientError("chat_array_invalid");
        }
        return { characterId, chatId };
    }

    _bindingIsCurrent(epoch) {
        if (!this.binding || epoch !== this.bindingEpoch) {
            return false;
        }
        try {
            return sameBinding(this.binding, this._readIdentity());
        } catch {
            return false;
        }
    }

    _ensureBindingListeners() {
        let context;
        let eventSource;
        let eventNames;
        try {
            context = this.getContext();
            eventSource = context?.eventSource;
            eventNames = REQUIRED_BINDING_EVENTS.map((key) => context?.eventTypes?.[key]);
        } catch {
            throw new BridgeClientError("binding_listener_setup_failed");
        }
        let validEventSource = false;
        try {
            validEventSource = Boolean(eventSource)
                && typeof eventSource.on === "function"
                && typeof eventSource.removeListener === "function";
        } catch {
            throw new BridgeClientError("binding_listener_setup_failed");
        }
        if (!validEventSource
            || eventNames.some((name) => typeof name !== "string" || name.trim().length === 0)) {
            throw new BridgeClientError("binding_listener_setup_failed");
        }

        const failedOrphans = removeListeners(this.orphanedBindingListeners);
        this.orphanedBindingListeners = failedOrphans;
        if (failedOrphans.length > 0) {
            throw new BridgeClientError("binding_listener_cleanup_failed");
        }

        if (eventSource === this.bindingEventSource && this.bindingListenersReady) {
            return;
        }

        const nextListeners = [];
        try {
            for (const name of eventNames) {
                nextListeners.push(attachEvent(eventSource, name, () => {
                    this.bindingEpoch += 1;
                }, (remove) => nextListeners.push(remove)));
            }
        } catch (error) {
            const failedCleanup = removeListeners(nextListeners);
            this.orphanedBindingListeners.push(...failedCleanup);
            if (failedCleanup.length > 0
                || error?.code === "binding_listener_cleanup_failed") {
                throw new BridgeClientError("binding_listener_cleanup_failed");
            }
            throw error instanceof BridgeClientError
                ? error
                : new BridgeClientError("binding_listener_setup_failed");
        }

        const failedOldListeners = removeListeners(this.bindingListeners);
        if (failedOldListeners.length > 0) {
            this.bindingListeners = nextListeners;
            this.bindingEventSource = eventSource;
            this.bindingListenersReady = true;
            this.orphanedBindingListeners.push(...failedOldListeners);
            throw new BridgeClientError("binding_listener_cleanup_failed");
        }

        this.bindingListeners = nextListeners;
        this.bindingEventSource = eventSource;
        this.bindingListenersReady = true;
    }

    _status(state, requestId = null) {
        this.lastStatus = { state, requestId };
        try {
            this.onStatus(this.lastStatus);
        } catch {
            // UI reporting must not change request processing.
        }
    }
}

export { BRIDGE_URL, DEFAULT_BRIDGE_PORT, bridgeUrl, validBridgePort };
