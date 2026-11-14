import { getContext } from "../../../extensions.js";
import { isGenerating, sendMessageAsUser } from "../../../../script.js";
import { BridgeClient, BridgeClientError, runAffinityDiagnostic } from "./bridge_client.mjs";
import { createInjectionSection, registerMacros, setMacros } from "./injection_panel.js";
import {
    runStartButtonAction,
    statusStateForError,
    statusTextForState,
} from "./status_messages.mjs";

const PANEL_ID = "st-bridge-client-panel";
const RESULT_ID = "st-bridge-client-result";
const BUTTON_ID = "st-bridge-client-diagnose";
const TEST_INPUT_ID = "st-bridge-client-test-input";
const TEST_BUTTON_ID = "st-bridge-client-test-send";
const TEST_RESULT_ID = "st-bridge-client-test-result";
const BRIDGE_BIND_ID = "st-bridge-client-bind";
const BRIDGE_CLEAR_BINDING_ID = "st-bridge-client-clear-binding";
const BRIDGE_START_ID = "st-bridge-client-start";
const BRIDGE_STOP_ID = "st-bridge-client-stop";
const BRIDGE_BINDING_RESULT_ID = "st-bridge-client-binding";
const BRIDGE_STATUS_ID = "st-bridge-client-status";
const BRIDGE_PORT_ID = "st-bridge-client-bridge-port";
const BRIDGE_PORT_SAVE_ID = "st-bridge-client-bridge-port-save";
const AFFINITY_CHARACTER_ID = "st-bridge-client-affinity-character";
const AFFINITY_VALUE_ID = "st-bridge-client-affinity-value";
const AFFINITY_RESULT_ID = "st-bridge-client-affinity-result";
const AFFINITY_REASON_TEXT = {
    api_missing: "未找到酒馆助手（TavernHelper）的变量接口",
    variable_missing: "最新楼层的 stat_data 里没有这个角色的好感度（MVU 是否已初始化？角色名是否正确？）",
    invalid_affinity: "角色名或数值无效（数值须为 0～100 的整数）",
    read_failed: "读取楼层变量失败",
    write_failed: "写入楼层变量失败",
    readback_failed: "写入后读回失败",
    readback_mismatch: "写入后读回的数值不一致",
    template_missing: "未找到提示词模板（EjsTemplate），无法按世界书方式读回",
    template_failed: "按世界书方式读回时出错",
    chat_empty: "当前聊天没有消息",
    path_blocked: "stat_data 路径上有不是对象的节点",
    node_type_invalid: "目标变量的类型不是数组或数字",
};
const SUMMARY_LIMIT = 160;
const TEST_REPLY_LIMIT = 2000;
const TEST_TIMEOUT_MS = 120000;

const SETTINGS_KEY = "st_bridge_client";
const SUBTITLE_ID = "st-bridge-client-subtitle";
const TABS = [
    { id: "bridge", label: "桥接", icon: "fa-link" },
    { id: "injection", label: "注入", icon: "fa-syringe" },
    { id: "diagnose", label: "诊断", icon: "fa-stethoscope" },
];

let testBusy = false;
let bridgeClient = null;

// Binding and the enable switch persist through ST's own extension settings.
const settingsStore = {
    load() {
        const saved = getContext().extensionSettings?.[SETTINGS_KEY];
        return saved && typeof saved === "object" ? saved : null;
    },
    save(data) {
        const context = getContext();
        if (!context.extensionSettings) {
            return;
        }
        context.extensionSettings[SETTINGS_KEY] = {
            ...(context.extensionSettings[SETTINGS_KEY] || {}),
            ...data,
        };
        context.saveSettingsDebounced?.();
    },
};

function summarize(value, limit = SUMMARY_LIMIT) {
    const text = String(value ?? "")
        .replace(/\s+/g, " ")
        .trim();

    if (!text) {
        return "（空）";
    }

    return text.length > limit
        ? `${text.slice(0, limit)}...`
        : text;
}

function safeErrorMessage(error) {
    const message = String(error?.message ?? error ?? "未知错误")
        .replace(/\s+/g, " ")
        .trim();

    const localError = /^(ST 当前已有|当前聊天上下文|当前版本缺少|追加消息期间|生成流程已结束|生成被停止|生成超过)/.test(message);
    if (!localError) {
        return "ST 生成失败；底层错误详情未在扩展中展示。";
    }

    return summarize(message || "未知错误", 300);
}

function displayValue(value) {
    return value === undefined || value === null || value === ""
        ? "不可用"
        : String(value);
}

function getMessageRole(message) {
    if (!message) {
        return "无消息";
    }

    if (message.is_system) {
        return "system";
    }

    if (message.is_user) {
        return "user";
    }

    return "assistant";
}

function makeRow(label, value) {
    const row = document.createElement("div");
    row.className = "st-bridge-client-row";

    const key = document.createElement("strong");
    key.textContent = `${label}: `;
    row.appendChild(key);

    const content = document.createElement("span");
    content.textContent = value;
    row.appendChild(content);

    return row;
}

function renderResult(result, error = null) {
    const output = document.getElementById(RESULT_ID);
    if (!output) {
        return;
    }

    output.replaceChildren();

    if (error) {
        output.appendChild(makeRow("诊断状态", `失败：${error.message || error}`));
        return;
    }

    output.appendChild(makeRow("诊断状态", "完成"));
    output.appendChild(makeRow("角色名称", result.characterName));
    output.appendChild(makeRow("角色 ID", result.characterId));
    output.appendChild(makeRow("群聊 ID", result.groupId));
    output.appendChild(makeRow("当前聊天 ID", result.chatId));
    output.appendChild(makeRow("聊天消息数量", String(result.messageCount)));
    output.appendChild(makeRow("最近消息角色", result.lastRole));
    output.appendChild(makeRow("最近消息摘要", result.lastSummary));
    output.appendChild(makeRow("正常生成函数", result.hasGenerate ? "可用" : "不可用"));
    output.appendChild(makeRow("事件总线", result.hasEventBus ? "可用" : "不可用"));
    output.appendChild(makeRow("生成相关事件", result.hasGenerationEvents ? "可用" : "不可用"));
    output.appendChild(makeRow("消息追加接口", result.hasMessageAppendInterface ? "可用" : "不可用"));
    output.appendChild(makeRow("聊天保存接口", result.hasSaveChat ? "可用" : "不可用"));
    output.appendChild(makeRow(
        "后续生成上下文",
        result.readyForNormalGeneration ? "接口齐全（本次未调用）" : "接口不完整",
    ));
}

function renderTestResult(status, detail = "", reply = "", eventObserved = null) {
    const output = document.getElementById(TEST_RESULT_ID);
    if (!output) {
        return;
    }

    output.replaceChildren();
    output.appendChild(makeRow("生成测试状态", status));

    if (detail) {
        output.appendChild(makeRow("详情", detail));
    }

    if (eventObserved !== null) {
        output.appendChild(makeRow(
            "生成完成事件",
            eventObserved ? "已收到" : "未在等待窗口收到（以 Generate 完成为准）",
        ));
    }

    if (reply !== "") {
        output.appendChild(makeRow("实际回复", summarize(reply, TEST_REPLY_LIMIT)));
    }
}

function renderBridgeStatus({ state, requestId = null } = {}) {
    const output = document.getElementById(BRIDGE_STATUS_ID);
    if (output) {
        const label = statusTextForState(state);
        const suffix = bridgeClient?.enabled === false ? "（自动桥接已停用）" : "";
        output.textContent = requestId
            ? `${label}（request_id: ${requestId}）${suffix}`
            : `${label}${suffix}`;
        // 101e: make "another page is on duty" impossible to miss.
        output.classList?.toggle?.(
            "st-bridge-other-page",
            state === "other_client_active" || state === "not_active_client",
        );
    }

    const bindButton = document.getElementById(BRIDGE_BIND_ID);
    const clearButton = document.getElementById(BRIDGE_CLEAR_BINDING_ID);
    const startButton = document.getElementById(BRIDGE_START_ID);
    const stopButton = document.getElementById(BRIDGE_STOP_ID);
    if (!bridgeClient || !bindButton) {
        return;
    }

    bindButton.disabled = bridgeClient.running || bridgeClient.hasWork;
    clearButton.disabled = bridgeClient.running || bridgeClient.hasWork;
    startButton.disabled = bridgeClient.running
        || bridgeClient.activeRequestId !== null
        || bridgeClient.busy;
    stopButton.disabled = !bridgeClient.running;
}

function showCurrentBinding() {
    const output = document.getElementById(BRIDGE_BINDING_RESULT_ID);
    if (!output) {
        return;
    }

    const binding = bridgeClient?.binding;
    let currentCharacterId = "不可用";
    let currentChatId = "不可用";
    try {
        const context = getContext();
        currentCharacterId = displayValue(context?.characterId);
        currentChatId = displayValue(
            context ? getCurrentChatId(context) : undefined,
        );
    } catch {
        // Keep the panel usable while ST context is unavailable.
    }

    const rows = [
        makeRow("绑定状态", binding ? "已绑定" : "尚未绑定"),
        makeRow("当前角色 ID", currentCharacterId),
        makeRow("当前聊天 ID", currentChatId),
    ];
    if (binding) {
        rows.push(
            makeRow("绑定角色 ID", binding.characterId),
            makeRow("绑定聊天 ID", binding.chatId),
        );
    }
    for (const row of rows) {
        row.style.display = "flex";
        row.style.flexWrap = "wrap";
        row.style.gap = "0.25em";
        row.style.margin = "0";
    }
    output.replaceChildren(...rows);
}

async function runAffinityCheck() {
    const output = document.getElementById(AFFINITY_RESULT_ID);
    if (!output) {
        return;
    }
    if (bridgeClient?.hasWork) {
        output.replaceChildren(makeRow("诊断状态", "桥接正在处理请求，请稍后再试"));
        return;
    }
    const character = document.getElementById(AFFINITY_CHARACTER_ID).value.trim();
    const value = Number(document.getElementById(AFFINITY_VALUE_ID).value);
    // The name is remembered for the next time (it is the character's name inside stat_data).
    settingsStore.save({ affinityCharacter: character });
    let report;
    try {
        report = await runAffinityDiagnostic({
            helper: globalThis.TavernHelper,
            ejs: globalThis.EjsTemplate,
            chat: getContext().chat,
            character,
            value,
        });
    } catch {
        report = { ok: false, reason: "write_failed" };
    }
    const rows = [
        makeRow("诊断结果", report.ok ? "成功：世界书会读到写入的数值" : "失败"),
    ];
    if (report.reason) {
        rows.push(makeRow("原因", AFFINITY_REASON_TEXT[report.reason] ?? report.reason));
    }
    if (report.messageIndex !== undefined) {
        rows.push(makeRow("写入楼层", `#${report.messageIndex}（最新楼层）`));
    }
    if (report.before !== undefined) {
        rows.push(makeRow("原有数值", displayValue(report.before)));
    }
    rows.push(makeRow("写入数值", displayValue(report.written)));
    if (report.helperReadback !== undefined) {
        rows.push(makeRow("酒馆助手读回", displayValue(report.helperReadback)));
    }
    if (report.worldbookReadback !== undefined) {
        rows.push(makeRow("世界书 getvar 读回", displayValue(report.worldbookReadback)));
    }
    if (report.restored !== undefined) {
        rows.push(makeRow("已恢复原值", report.restored ? "是" : "否"));
    }
    const last = bridgeClient?.lastAffinityResult;
    rows.push(makeRow(
        "最近一次桥接注入",
        last
            ? (last.ok
                ? `成功（${last.writes.map((entry) => entry.skipped ? `${entry.path} 跳过` : entry.path).join("、")}）`
                : `失败：${AFFINITY_REASON_TEXT[last.reason] ?? last.reason}`)
            : "本次页面加载后还没有",
    ));
    output.replaceChildren(...rows);
}

function diagnoseCurrentChat() {
    const context = getContext();
    const chat = Array.isArray(context.chat) ? context.chat : [];
    const lastMessage = chat.at(-1);
    const character = context.characters?.[context.characterId];
    let chatId = context.chatId;

    if (chatId === undefined && typeof context.getCurrentChatId === "function") {
        chatId = context.getCurrentChatId();
    }

    const generationEvents = context.eventTypes ?? {};
    const hasGenerate = typeof context.generate === "function";
    const hasEventBus = typeof context.eventSource?.on === "function";
    const hasGenerationEvents = [
        "MESSAGE_RECEIVED",
        "GENERATION_STARTED",
        "GENERATION_STOPPED",
        "GENERATION_ENDED",
    ].every((name) => typeof generationEvents[name] === "string");
    const hasMessageAppendInterface =
        typeof context.executeSlashCommandsWithOptions === "function" ||
        typeof context.addOneMessage === "function";
    const hasSaveChat = typeof context.saveChat === "function";

    return {
        characterName: displayValue(character?.name ?? context.name2),
        characterId: displayValue(context.characterId),
        groupId: displayValue(context.groupId),
        chatId: displayValue(chatId),
        messageCount: chat.length,
        lastRole: getMessageRole(lastMessage),
        lastSummary: summarize(lastMessage?.extra?.display_text ?? lastMessage?.mes),
        hasGenerate,
        hasEventBus,
        hasGenerationEvents,
        hasMessageAppendInterface,
        hasSaveChat,
        readyForNormalGeneration:
            hasGenerate &&
            hasEventBus &&
            hasGenerationEvents &&
            hasMessageAppendInterface &&
            hasSaveChat,
    };
}

function createEventWaiter(eventSource, eventName) {
    let settled = false;
    let resolvePromise;

    const listener = (...args) => {
        if (settled) {
            return;
        }

        settled = true;
        eventSource.removeListener(eventName, listener);
        resolvePromise(args);
    };

    const promise = new Promise((resolve) => {
        resolvePromise = resolve;
        eventSource.on(eventName, listener);
    });

    return {
        promise,
        cancel() {
            if (settled) {
                return;
            }

            settled = true;
            eventSource.removeListener(eventName, listener);
        },
    };
}

function delay(milliseconds) {
    return new Promise((resolve) => window.setTimeout(resolve, milliseconds));
}

function suspendMainDraft() {
    const textarea = document.getElementById("send_textarea");
    if (!textarea) {
        return null;
    }

    const previousValue = textarea.value;
    textarea.value = "";
    textarea.dispatchEvent(new Event("input", { bubbles: true }));

    return { textarea, previousValue };
}

function restoreMainDraft(state) {
    if (!state || state.textarea.value !== "") {
        return;
    }

    state.textarea.value = state.previousValue;
    state.textarea.dispatchEvent(new Event("input", { bubbles: true }));
}

function setTestControls(input, button, busy) {
    input.disabled = busy;
    button.disabled = busy || !input.value.trim();
}

// Scheduled turns: ST's own /sys implementation (the narrator message, role "system" in the prompt).
// Imported on demand so a different ST layout only fails scheduled turns, not the whole extension.
async function sendNarratorMessageNative(text) {
    const module = await import("../../../slash-commands.js");
    if (typeof module.sendNarratorMessage !== "function") {
        throw new Error("sendNarratorMessage is unavailable");
    }
    await module.sendNarratorMessage({}, text);
}

function getCurrentChatId(context) {
    if (context.chatId !== undefined) {
        return context.chatId;
    }

    return typeof context.getCurrentChatId === "function"
        ? context.getCurrentChatId()
        : undefined;
}

async function runGenerationTest(input, button) {
    if (testBusy) {
        return;
    }

    const messageText = input.value.trim();
    if (!messageText) {
        renderTestResult("未执行", "测试消息不能为空。");
        setTestControls(input, button, false);
        return;
    }

    if (bridgeClient?.running || bridgeClient?.hasWork) {
        renderTestResult("未执行", "请先停止桥接轮询并等待当前请求结束。");
        return;
    }

    let context;
    let eventWaiter;
    let stoppedWaiter;
    let generationPromise;
    let timeoutId;
    let draftState;
    let timedOut = false;
    let eventObserved = false;

    testBusy = true;
    setTestControls(input, button, true);
    renderTestResult("准备中", "测试会向当前聊天追加用户消息并生成回复。请勿在生成期间重复操作。 ");

    try {
        context = getContext();

        if (typeof isGenerating === "function" && isGenerating()) {
            throw new Error("ST 当前已有生成任务，请等待其完成后再测试。");
        }

        if (!Array.isArray(context.chat)) {
            throw new Error("当前聊天上下文不可用。");
        }

        if (typeof sendMessageAsUser !== "function" || typeof context.generate !== "function") {
            throw new Error("当前版本缺少原生消息追加或生成接口。");
        }

        if (typeof context.eventSource?.on !== "function" || typeof context.eventSource?.removeListener !== "function") {
            throw new Error("当前版本缺少可用的事件监听接口。");
        }

        const generationEnded = context.eventTypes?.GENERATION_ENDED;
        const generationStopped = context.eventTypes?.GENERATION_STOPPED;
        if (typeof generationEnded !== "string" || typeof generationStopped !== "string") {
            throw new Error("当前版本缺少生成完成或停止事件定义。");
        }

        const initialChatId = getCurrentChatId(context);
        draftState = suspendMainDraft();

        renderTestResult("追加消息中", "正在使用 ST 原生用户消息流程。 ");
        await sendMessageAsUser(messageText);

        const afterMessageContext = getContext();
        if (String(getCurrentChatId(afterMessageContext)) !== String(initialChatId)) {
            throw new Error("追加消息期间当前聊天发生变化，已停止本次测试。");
        }

        if (typeof isGenerating === "function" && isGenerating()) {
            throw new Error("追加消息期间检测到其他生成任务，未启动本次测试生成。");
        }

        const userMessageIndex = afterMessageContext.chat.length - 1;
        eventWaiter = createEventWaiter(context.eventSource, generationEnded);
        stoppedWaiter = createEventWaiter(context.eventSource, generationStopped);
        renderTestResult("生成中", "正在调用 ST 的 normal 生成流程。 ");

        generationPromise = Promise.resolve().then(() => context.generate("normal", { automatic_trigger: true }));
        const timeoutPromise = new Promise((resolve) => {
            timeoutId = window.setTimeout(() => resolve({ timedOut: true }), TEST_TIMEOUT_MS);
        });
        const generationOutcome = await Promise.race([
            generationPromise.then((result) => ({ timedOut: false, result })),
            stoppedWaiter.promise.then(() => ({ stopped: true })),
            timeoutPromise,
        ]);

        if (generationOutcome.stopped) {
            generationPromise.catch(() => { });
            await Promise.race([generationPromise.catch(() => { }), delay(2000)]);
            throw new Error("生成被停止，未将其作为成功结果。");
        }

        if (generationOutcome.timedOut) {
            timedOut = true;
            eventWaiter.cancel();
            generationPromise.catch(() => { });
            try {
                context.stopGeneration?.();
            } catch {
                // The timeout result remains authoritative if stopping also fails.
            }
            await Promise.race([generationPromise.catch(() => { }), delay(2000)]);
            throw new Error(`生成超过 ${TEST_TIMEOUT_MS / 1000} 秒未完成。`);
        }

        await Promise.race([
            eventWaiter.promise.then(() => {
                eventObserved = true;
            }),
            delay(1000),
        ]);

        const finalContext = getContext();
        const chat = Array.isArray(finalContext.chat) ? finalContext.chat : [];
        const reply = chat.length > userMessageIndex + 1 ? chat.at(-1) : null;

        if (!reply || reply.is_user || reply.is_system) {
            throw new Error("生成流程已结束，但未找到本次测试对应的新 assistant 回复。");
        }

        renderTestResult(
            "成功",
            "消息已追加，ST 已完成 normal 生成并按原生流程保存聊天。",
            reply.mes,
            eventObserved,
        );
    } catch (error) {
        renderTestResult(
            timedOut ? "超时" : "失败",
            safeErrorMessage(error),
            "",
            eventObserved,
        );
    } finally {
        if (timeoutId !== undefined) {
            window.clearTimeout(timeoutId);
        }

        eventWaiter?.cancel();
        stoppedWaiter?.cancel();
        restoreMainDraft(draftState);
        testBusy = false;
        setTestControls(input, button, false);
    }
}

function makeCardTitle(iconClass, text) {
    const title = document.createElement("h4");
    title.className = "st-bridge-card-title";
    const icon = document.createElement("i");
    icon.className = `fa-solid ${iconClass}`;
    title.appendChild(icon);
    const label = document.createElement("span");
    label.textContent = text;
    title.appendChild(label);
    return title;
}

// "v<manifest version> | STbridge" under the title; stays empty when the manifest can't be read.
async function showVersion(element) {
    try {
        const response = await fetch(new URL("./manifest.json", import.meta.url));
        const manifest = await response.json();
        if (manifest && typeof manifest.version === "string" && manifest.version) {
            element.textContent = `v${manifest.version} | STbridge`;
        }
    } catch (error) {
        globalThis.console?.warn?.("ST Bridge manifest read failed", error);
    }
}

function createPanel() {
    if (document.getElementById(PANEL_ID)) {
        return;
    }

    const host = document.getElementById("extensions_settings");
    if (!host) {
        window.setTimeout(createPanel, 250);
        return;
    }

    // ST's standard collapsible drawer (the click handler lives in ST's script.js).
    const panel = document.createElement("div");
    panel.id = PANEL_ID;
    panel.className = "extension_settings st-bridge-client-settings";

    const drawer = document.createElement("div");
    drawer.className = "inline-drawer";
    const drawerToggle = document.createElement("div");
    drawerToggle.className = "inline-drawer-toggle inline-drawer-header";
    const drawerTitle = document.createElement("b");
    drawerTitle.textContent = "ST Bridge";
    drawerToggle.appendChild(drawerTitle);
    const drawerIcon = document.createElement("div");
    drawerIcon.className = "inline-drawer-icon fa-solid fa-circle-chevron-down down";
    drawerToggle.appendChild(drawerIcon);
    drawer.appendChild(drawerToggle);
    const content = document.createElement("div");
    content.className = "inline-drawer-content";
    drawer.appendChild(content);
    panel.appendChild(drawer);

    const subtitle = document.createElement("div");
    subtitle.className = "st-bridge-subtitle";
    subtitle.id = SUBTITLE_ID;
    content.appendChild(subtitle);
    void showVersion(subtitle);

    const tabBar = document.createElement("div");
    tabBar.className = "st-bridge-tabs";
    tabBar.setAttribute("role", "tablist");
    content.appendChild(tabBar);
    const panes = {};
    for (const tab of TABS) {
        const tabButton = document.createElement("button");
        tabButton.type = "button";
        tabButton.className = "st-bridge-tab";
        tabButton.dataset.tab = tab.id;
        tabButton.setAttribute("role", "tab");
        const tabIcon = document.createElement("i");
        tabIcon.className = `fa-solid ${tab.icon}`;
        tabButton.appendChild(tabIcon);
        tabButton.appendChild(document.createTextNode(tab.label));
        tabButton.addEventListener("click", () => selectTab(tab.id, true));
        tabBar.appendChild(tabButton);
        const pane = document.createElement("div");
        pane.className = "st-bridge-pane";
        pane.dataset.pane = tab.id;
        pane.setAttribute("role", "tabpanel");
        content.appendChild(pane);
        panes[tab.id] = pane;
    }
    const selectTab = (id, remember) => {
        const active = panes[id] ? id : TABS[0].id;
        for (const tabButton of tabBar.children) {
            const on = tabButton.dataset.tab === active;
            tabButton.classList.toggle("active", on);
            tabButton.setAttribute("aria-selected", String(on));
        }
        for (const [paneId, pane] of Object.entries(panes)) {
            pane.hidden = paneId !== active;
        }
        if (remember) {
            try {
                settingsStore.save({ activeTab: active });
            } catch (error) {
                globalThis.console?.warn?.("ST Bridge tab save failed", error);
            }
        }
    };

    const diagnoseArea = document.createElement("section");
    diagnoseArea.className = "st-bridge-client-diagnose";
    diagnoseArea.appendChild(makeCardTitle("fa-stethoscope", "诊断当前聊天"));

    const button = document.createElement("button");
    button.id = BUTTON_ID;
    button.type = "button";
    button.className = "menu_button";
    button.textContent = "诊断当前聊天";
    button.addEventListener("click", () => {
        try {
            renderResult(diagnoseCurrentChat());
        } catch (error) {
            renderResult(null, error);
        }
    });
    diagnoseArea.appendChild(button);

    const result = document.createElement("div");
    result.id = RESULT_ID;
    result.className = "st-bridge-client-result";
    result.setAttribute("aria-live", "polite");
    diagnoseArea.appendChild(result);
    panes.diagnose.appendChild(diagnoseArea);

    const bridgeArea = document.createElement("section");
    bridgeArea.className = "st-bridge-client-bridge";

    bridgeArea.appendChild(makeCardTitle("fa-link", "桥接（AstrBot / NapCat）"));

    const bridgeWarning = document.createElement("p");
    bridgeWarning.className = "st-bridge-note";
    bridgeWarning.textContent = "桥接会把收到的用户消息追加到绑定聊天并调用 ST 原生生成。只需绑定一次：绑定和启用状态保存在 ST 扩展设置中，刷新页面或重启 STbridge 后会自动恢复。点“停止桥接轮询”可关闭自动桥接。";
    bridgeArea.appendChild(bridgeWarning);

    const portRow = document.createElement("div");
    const portLabel = document.createElement("label");
    portLabel.textContent = "bridge 端口";
    portLabel.htmlFor = BRIDGE_PORT_ID;
    portRow.className = "st-bridge-row";
    portRow.appendChild(portLabel);
    const portInput = document.createElement("input");
    portInput.id = BRIDGE_PORT_ID;
    portInput.className = "text_pole";
    portInput.type = "number";
    portInput.min = "1024";
    portInput.max = "65535";
    portInput.step = "1";
    portRow.appendChild(portInput);
    const portSave = document.createElement("button");
    portSave.id = BRIDGE_PORT_SAVE_ID;
    portSave.type = "button";
    portSave.className = "menu_button";
    portSave.textContent = "保存端口";
    portSave.addEventListener("click", () => {
        try {
            bridgeClient.setBridgePort(portInput.value);
            renderBridgeStatus({ state: "bridge_port_saved" });
        } catch (error) {
            renderBridgeStatus({ state: statusStateForError(error) });
        }
        portInput.value = String(bridgeClient.bridgePort);
    });
    portRow.appendChild(portSave);
    const portNote = document.createElement("span");
    portNote.className = "st-bridge-note";
    portNote.textContent = "要和 STbridge 控制台（11453）“系统 › 常规”的“端口”卡片里的 bridge 端口一致；改完立即生效，只连 127.0.0.1。";
    portRow.appendChild(portNote);
    bridgeArea.appendChild(portRow);

    const binding = document.createElement("div");
    binding.id = BRIDGE_BINDING_RESULT_ID;
    binding.setAttribute("aria-live", "polite");
    bridgeArea.appendChild(binding);

    const bridgeActions = document.createElement("div");
    bridgeActions.className = "st-bridge-client-actions";

    const bindButton = document.createElement("button");
    bindButton.id = BRIDGE_BIND_ID;
    bindButton.type = "button";
    bindButton.className = "menu_button";
    bindButton.textContent = "绑定当前角色和聊天";
    bindButton.addEventListener("click", () => {
        try {
            bridgeClient.bindCurrent();
            showCurrentBinding();
            renderBridgeStatus({ state: "bound" });
        } catch (error) {
            renderBridgeStatus({
                state: statusStateForError(error),
            });
        }
    });
    bridgeActions.appendChild(bindButton);

    const clearBindingButton = document.createElement("button");
    clearBindingButton.id = BRIDGE_CLEAR_BINDING_ID;
    clearBindingButton.type = "button";
    clearBindingButton.className = "menu_button";
    clearBindingButton.textContent = "清除绑定";
    clearBindingButton.addEventListener("click", () => {
        try {
            if (bridgeClient.running || bridgeClient.hasWork) {
                throw new BridgeClientError("request_in_progress");
            }
            bridgeClient.clearBinding();
            showCurrentBinding();
            renderBridgeStatus({ state: "unbound" });
        } catch (error) {
            renderBridgeStatus({
                state: statusStateForError(error),
            });
        }
    });
    bridgeActions.appendChild(clearBindingButton);

    const startButton = document.createElement("button");
    startButton.id = BRIDGE_START_ID;
    startButton.type = "button";
    startButton.className = "menu_button";
    startButton.textContent = "启动桥接轮询";
    startButton.addEventListener("click", () => {
        runStartButtonAction({
            start: () => bridgeClient.start(),
            getRunning: () => bridgeClient.running,
            renderStatus: renderBridgeStatus,
            reportDiagnostic: (phase, error) => bridgeClient.reportStartupDiagnostic(phase, error),
        });
    });
    bridgeActions.appendChild(startButton);

    const stopButton = document.createElement("button");
    stopButton.id = BRIDGE_STOP_ID;
    stopButton.type = "button";
    stopButton.className = "menu_button";
    stopButton.textContent = "停止桥接轮询";
    stopButton.addEventListener("click", () => bridgeClient.stop());
    bridgeActions.appendChild(stopButton);
    bridgeArea.appendChild(bridgeActions);

    const bridgeStatus = document.createElement("div");
    bridgeStatus.id = BRIDGE_STATUS_ID;
    bridgeStatus.setAttribute("aria-live", "polite");
    bridgeArea.appendChild(bridgeStatus);
    panes.bridge.appendChild(bridgeArea);

    const affinityArea = document.createElement("section");
    affinityArea.className = "st-bridge-client-affinity";
    affinityArea.appendChild(makeCardTitle("fa-heart-pulse", "好感度注入诊断"));
    const affinityNote = document.createElement("p");
    affinityNote.className = "st-bridge-note";
    affinityNote.textContent = "把测试数值写入最新楼层的 stat_data.<角色>.好感度[0]，再用与世界书相同的 getvar 读回，最后恢复原值。用于确认每轮切换发言人好感度是否生效。";
    affinityArea.appendChild(affinityNote);
    const affinityInputs = document.createElement("div");
    affinityInputs.className = "st-bridge-row";
    const affinityCharacter = document.createElement("input");
    affinityCharacter.id = AFFINITY_CHARACTER_ID;
    affinityCharacter.className = "text_pole";
    affinityCharacter.value = settingsStore.load()?.affinityCharacter ?? "";
    affinityCharacter.placeholder = "角色名（stat_data 里的键，和控制台里角色的“ST 角色名”一致）";
    affinityInputs.appendChild(affinityCharacter);
    const affinityValue = document.createElement("input");
    affinityValue.id = AFFINITY_VALUE_ID;
    affinityValue.className = "text_pole";
    affinityValue.type = "number";
    affinityValue.min = "0";
    affinityValue.max = "100";
    affinityValue.step = "1";
    affinityValue.value = "73";
    affinityInputs.appendChild(affinityValue);
    affinityArea.appendChild(affinityInputs);
    const affinityButton = document.createElement("button");
    affinityButton.type = "button";
    affinityButton.className = "menu_button";
    affinityButton.textContent = "运行好感度注入诊断";
    affinityButton.addEventListener("click", () => {
        void runAffinityCheck();
    });
    affinityArea.appendChild(affinityButton);
    const affinityResult = document.createElement("div");
    affinityResult.id = AFFINITY_RESULT_ID;
    affinityResult.setAttribute("aria-live", "polite");
    affinityArea.appendChild(affinityResult);
    panes.diagnose.appendChild(affinityArea);

    const injectionSection = createInjectionSection({ getClient: () => bridgeClient, getContext });
    panes.injection.appendChild(injectionSection.element);

    bridgeClient = new BridgeClient({
        getContext,
        settingsStore,
        isGenerating,
        isLocallyBusy: () => testBusy,
        sendMessageAsUser,
        sendNarratorMessage: sendNarratorMessageNative,
        generate: (type, options) => getContext().generate(type, options),
        onDiagnostic: ({ phase, errorType }) => {
            globalThis.console?.warn?.("ST Bridge startup diagnostic", phase, errorType);
        },
        onAffinityResult: ({ ok, reason }) => {
            if (!ok) {
                globalThis.console?.warn?.("ST Bridge affinity_inject_failed", reason);
            }
        },
        onMacros: (macros) => setMacros(macros, { fromClaim: true }),
        onStatus: (status) => {
            renderBridgeStatus(status);
            showCurrentBinding();
        },
    });

    portInput.value = String(bridgeClient.bridgePort);

    const testArea = document.createElement("section");
    testArea.className = "st-bridge-client-generation-test";

    testArea.appendChild(makeCardTitle("fa-flask", "正常生成测试"));

    const warning = document.createElement("p");
    warning.className = "st-bridge-note";
    warning.textContent = "警告：测试会向当前聊天追加用户消息并生成回复，可能修改聊天记录。建议先切换到专用临时聊天。";
    testArea.appendChild(warning);

    const input = document.createElement("textarea");
    input.id = TEST_INPUT_ID;
    input.rows = 3;
    input.placeholder = "输入测试消息";
    input.addEventListener("input", () => setTestControls(input, testButton, false));
    testArea.appendChild(input);

    const testButton = document.createElement("button");
    testButton.id = TEST_BUTTON_ID;
    testButton.type = "button";
    testButton.className = "menu_button";
    testButton.textContent = "发送测试消息并生成";
    testButton.addEventListener("click", () => {
        void runGenerationTest(input, testButton);
    });
    testArea.appendChild(testButton);

    const testResult = document.createElement("div");
    testResult.id = TEST_RESULT_ID;
    testResult.className = "st-bridge-client-test-result";
    testResult.setAttribute("aria-live", "polite");
    testArea.appendChild(testResult);

    panes.diagnose.appendChild(testArea);
    setTestControls(input, testButton, false);

    host.appendChild(panel);
    selectTab(settingsStore.load()?.activeTab, false);
    showCurrentBinding();
    renderBridgeStatus({ state: bridgeClient.binding ? "bound" : "unbound" });
    bridgeClient.attachAutoStart();
    injectionSection.start();
}

try {
    registerMacros(getContext);
} catch (error) {
    globalThis.console?.warn?.("ST Bridge macro registration failed", error);
}
createPanel();
