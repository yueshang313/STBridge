import { BridgeClientError } from "./bridge_client.mjs";

const BRIDGE_STATUS_TEXT = {
    bound: "已绑定当前角色和聊天",
    unbound: "尚未绑定",
    polling: "轮询中",
    checking_requests: "正在检查待处理请求",
    request_claimed: "已领取请求",
    adding_user_message: "正在向当前聊天追加用户消息",
    generation_started: "ST 原生生成中",
    generation_timeout_waiting: "已超过等待时限，仍在等待 ST 确认生成结束",
    generation_state_unconfirmed: "未能确认 ST 生成状态，已停止提交回复",
    request_succeeded: "回复已提交",
    result_unconfirmed: "未能确认结果提交，等待服务端状态确认",
    request_expired: "请求已过期，未提交回复",
    request_state_conflict: "请求状态冲突，未提交回复",
    binding_mismatch: "当前角色或聊天与绑定不一致",
    chat_changed: "生成期间聊天发生变化，未提交回复",
    st_busy: "ST 正在执行其他生成，已拒绝请求",
    generation_failed: "ST 生成失败",
    generation_timeout: "ST 生成超时",
    extension_error: "扩展处理失败",
    request_too_large: "请求文本超过限制",
    binding_required: "请先绑定当前角色和聊天",
    context_unavailable: "当前 ST 上下文不可用",
    context_read_failed: "读取 ST 上下文失败",
    group_context_unsupported: "当前为群组上下文，暂不支持",
    character_id_missing: "当前角色 ID 缺失",
    chat_id_missing: "当前聊天 ID 缺失",
    chat_array_invalid: "当前聊天数据格式无效",
    binding_listener_setup_failed: "无法建立聊天切换保护，绑定或启动已拒绝",
    binding_listener_cleanup_failed: "聊天切换保护清理失败，操作已拒绝",
    unclassified_operation_error: "桥接操作发生未分类错误",
    network_timeout: "连接桥接服务超时",
    network_error: "无法连接桥接服务",
    unauthorized: "AstrBot 端的 ST Bridge 插件版本过旧（仍要求令牌），请更新插件",
    recovery_required: "AstrBot 插件刚重载，ST 空闲后会自动解除保护",
    bridge_ready: "已向 AstrBot 报告空闲，桥接已就绪",
    affinity_injected: "已写入发言人的好感度",
    affinity_inject_failed: "好感度写入失败，本次按聊天里原有的数值生成",
    poll_rate_limited: "轮询过快，稍后重试",
    request_in_progress: "请求处理中，请等待当前操作结束",
    stopping_after_request: "当前请求结束后停止轮询",
    invalid_bridge_port: "bridge 端口无效（需要 1024～65535 的整数）",
    bridge_port_saved: "bridge 端口已保存，已按新端口重新连接",
};

const UNKNOWN_STATUS_TEXT = "桥接状态已更新";

export function statusStateForError(error) {
    if (error instanceof BridgeClientError
        && typeof error.code === "string"
        && error.code.length > 0) {
        return error.code;
    }
    return "unclassified_operation_error";
}

export function statusTextForState(state) {
    return Object.prototype.hasOwnProperty.call(BRIDGE_STATUS_TEXT, state)
        ? BRIDGE_STATUS_TEXT[state]
        : UNKNOWN_STATUS_TEXT;
}

function getRunningState(getRunning) {
    try {
        return Boolean(getRunning());
    } catch {
        return false;
    }
}

function reportRenderFailure(reportDiagnostic, error) {
    try {
        reportDiagnostic("button_status_render", error);
    } catch {
        // Diagnostic reporting must not change UI control flow.
    }
}

export function runStartButtonAction({ start, getRunning, renderStatus, reportDiagnostic }) {
    let startFailed = false;
    let startError;
    try {
        start();
    } catch (error) {
        startFailed = true;
        startError = error;
    }

    if (startFailed) {
        const state = statusStateForError(startError);
        try {
            renderStatus({ state });
        } catch (error) {
            reportRenderFailure(reportDiagnostic, error);
            try {
                renderStatus({ state: getRunningState(getRunning) ? "polling" : "stopped" });
            } catch {
                // The client state remains authoritative if the UI cannot render.
            }
        }
        return { startSucceeded: false, running: getRunningState(getRunning) };
    }

    try {
        renderStatus({ state: "polling" });
    } catch (error) {
        reportRenderFailure(reportDiagnostic, error);
        try {
            renderStatus({ state: getRunningState(getRunning) ? "polling" : "stopped" });
        } catch {
            // The client state remains authoritative if the UI cannot render.
        }
    }

    return { startSucceeded: true, running: getRunningState(getRunning) };
}
