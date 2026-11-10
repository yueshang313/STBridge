// Pure console logic (no DOM): imported by app.js and unit-tested with node.

export const ENGINES = ["irodori", "gpt_sovits"];
export const ENGINE_LABELS = { irodori: "Irodori", gpt_sovits: "GPT-SoVITS" };

// ---------- TTS settings ----------

// [name, type]: "text" | "int" | "float" | "bool"
export const TTS_FIELDS = {
  common: [
    ["auto_start", "bool"], ["stop_on_exit", "bool"], ["start_timeout", "int"],
    ["tts_timeout_minutes", "int"], ["prob", "float"], ["text_limit", "int"],
    ["cooldown", "int"], ["send_text_with_audio", "bool"],
    ["split_messages", "bool"], ["split_interval", "float"], ["split_max", "int"]
  ],
  irodori: [
    ["host", "text"], ["port", "int"], ["launch_dir", "text"], ["server_dir", "text"],
    ["hf_checkpoint", "text"], ["lora_adapter", "text"], ["num_steps", "int"],
    ["cfg_scale_text", "float"], ["cfg_scale_caption", "float"], ["default_caption", "text"], ["output_mode", "text"]
  ],
  gpt_sovits: [
    ["host", "text"], ["port", "int"], ["launch_dir", "text"], ["gpt_model_path", "text"],
    ["sovits_model_path", "text"], ["ref_audio_path", "text"], ["prompt_text", "text"],
    ["prompt_text_lang", "text"], ["text_lang", "text"], ["top_k", "int"], ["top_p", "float"],
    ["temperature", "float"], ["speed_factor", "float"], ["split_sentence", "bool"]
  ]
};

/** Convert raw form values (strings, checkbox booleans) into one settings section. */
export function coerceSection(section, raw) {
  const result = {};
  for (const [name, type] of TTS_FIELDS[section]) {
    if (!(name in raw)) continue;
    const value = raw[name];
    if (type === "bool") result[name] = Boolean(value);
    else if (type === "int" || type === "float") result[name] = value === "" || value === null ? NaN : Number(value);
    else result[name] = String(value ?? "");
  }
  return result;
}

/** Apply a partial change to a whole settings object; returns a new object. */
export function applyTtsPatch(settings, patch) {
  const next = JSON.parse(JSON.stringify(settings));
  if (patch.engine !== undefined) next.engine = patch.engine;
  for (const section of ["common", "irodori", "gpt_sovits"]) {
    if (patch[section]) next[section] = { ...next[section], ...patch[section] };
  }
  return next;
}

/** "已关闭插件启动的 Irodori 服务" for the engines whose own service was stopped. */
export function stoppedNotice(stopped) {
  const names = (stopped || []).map((engine) => ENGINE_LABELS[engine] || engine);
  return names.length ? `已关闭插件启动的 ${names.join("、")} 服务` : "";
}

const STATE_TEXT = {
  stopped: "已停止", starting: "启动中…", running: "运行中（插件启动）",
  external: "运行中（外部启动，插件不管理）", failed: "启动失败"
};

export function serviceStateText(state) {
  return STATE_TEXT[state] || state || "-";
}

/** Which service buttons are usable for a state while no request is running. */
export function serviceButtons(state, busy) {
  return {
    startDisabled: Boolean(busy) || state === "starting" || state === "running" || state === "external",
    stopDisabled: Boolean(busy) || (state !== "running" && state !== "starting")
  };
}

// ---------- Emotion forms ----------

// The fields each engine edits; the other engine's fields must survive a save untouched.
export const EMOTION_FIELDS = {
  irodori: ["caption", "emoji"],
  gpt_sovits: [
    "ref_audio_path", "prompt_text", "prompt_text_lang", "text_lang",
    "top_k", "top_p", "temperature", "speed_factor", "split_sentence"
  ]
};
const NUMERIC_EMOTION_FIELDS = new Set(["top_k", "top_p", "temperature", "speed_factor"]);

/**
 * Body for saving one manual emotion: the stored profile with only the current
 * engine's visible fields replaced by the form values.
 */
export function buildEmotionPayload(profile, engine, raw) {
  const payload = { ...(profile || {}) };
  for (const name of EMOTION_FIELDS[engine] || []) {
    if (!(name in raw)) continue;
    const value = raw[name];
    if (name === "split_sentence") payload[name] = Boolean(value);
    else if (NUMERIC_EMOTION_FIELDS.has(name)) payload[name] = Number(value);
    else payload[name] = String(value ?? "");
  }
  return payload;
}

function hasText(value) {
  return typeof value === "string" && value.trim() !== "";
}

/**
 * ST preset prompt for the engine's usable emotions.
 * Irodori: manual emotions with a caption or emoji.
 * GPT-SoVITS: emotions with usable folder audio, then manual emotions with a reference audio path.
 */
export function buildPresetPrompt(engine, discovered, manualEmotions) {
  const names = [];
  const add = (name) => { if (!names.includes(name)) names.push(name); };
  const manual = Object.entries(manualEmotions || {});
  if (engine === "irodori") {
    for (const [name, profile] of manual) if (hasText(profile?.caption) || hasText(profile?.emoji)) add(name);
  } else {
    for (const emotion of discovered || []) if (emotion.available > 0) add(emotion.name);
    for (const [name, profile] of manual) if (hasText(profile?.ref_audio_path)) add(name);
  }
  return {
    names,
    text: names.length ? `每次回复开头输出 [情绪:xxx]，xxx 只能从以下选择：${names.join("、")}` : ""
  };
}

// ---------- Reference audio ----------

/** Same rule as the backend: prefer its `usable` flag, else fall back to the older fields. */
export function audioIsUsable(audio) {
  if (typeof audio.usable === "boolean") return audio.usable;
  return !audio.out_of_range && audio.readable !== false;
}

/** Group /api/audio entries by emotion folder: [{emotion, total, usable, audios}] sorted by name. */
export function groupAudiosByEmotion(audios) {
  const groups = new Map();
  for (const audio of audios || []) {
    const emotion = audio.emotion || "default";
    if (!groups.has(emotion)) groups.set(emotion, { emotion, total: 0, usable: 0, audios: [] });
    const group = groups.get(emotion);
    group.total += 1;
    if (audioIsUsable(audio)) group.usable += 1;
    group.audios.push(audio);
  }
  // Code-point order (not locale order) so the result does not depend on the browser's ICU data.
  return [...groups.values()].sort((a, b) => (a.emotion < b.emotion ? -1 : a.emotion > b.emotion ? 1 : 0));
}

export function groupTitle(group) {
  return `${group.emotion} · 可用 ${group.usable} / 共 ${group.total} 段`;
}

// ---------- Stickers ----------

export const STICKER_ACCEPT = ".png,.jpg,.jpeg,.gif";

/** Body for /api/stickers/settings from raw form values (percent and seconds). */
export function stickerSettingsPayload(raw) {
  const number = (value) => (value === "" || value === null || value === undefined ? NaN : Number(value));
  return { enabled: Boolean(raw.enabled), prob: number(raw.prob), cooldown: number(raw.cooldown) };
}

export function stickerGroupTitle(group) {
  return `${group.name} · ${group.count} 张`;
}

export function formatBytes(size) {
  if (size >= 1024 * 1024) return `${(size / (1024 * 1024)).toFixed(1)} MB`;
  return `${Math.max(1, Math.round(size / 1024))} KB`;
}

/** Toast text after an upload response: saved count plus rejected files with the reason. */
export function uploadSummary(result) {
  const saved = (result.saved || []).length;
  const rejected = result.rejected || [];
  let text = `已上传 ${saved} 张`;
  if (rejected.length) text += `；${rejected.length} 张未收：${rejected.map((item) => `${item.filename}（${item.error}）`).join("、")}`;
  return text;
}

// ---------- Scheduled messages ----------

export const WEEKDAY_LABELS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]; // index = Python weekday
export const GROUP_SEND_NOTE = "群聊主动消息可能被 QQ 拦截；AstrBot 重启后，需要该群先有人发过消息才可能送达。试发结果显示成功也请以群里实际收到为准。";
export const PLAN_LIMITS = { name: 40, prompt: 300, fallbackCount: 5, fallbackChars: 200, jitter: [0, 60], every: [15, 720] };

/** "周一至周五" for a run of 3+ consecutive days, else "周一、周三"; empty or all seven: no restriction. */
export function weekdaysText(weekdays) {
  const days = [...new Set(weekdays || [])].filter((day) => day >= 0 && day <= 6).sort((a, b) => a - b);
  if (!days.length || days.length === 7) return "";
  const consecutive = days.every((day, index) => index === 0 || day === days[index - 1] + 1);
  if (days.length >= 3 && consecutive) return `${WEEKDAY_LABELS[days[0]]}至${WEEKDAY_LABELS[days[days.length - 1]]}`;
  return days.map((day) => WEEKDAY_LABELS[day]).join("、");
}

export function planSummary(plan) {
  if (plan.kind === "interval") return `${plan.start}–${plan.end} 每 ${plan.every_minutes} 分钟`;
  let text = `每天 ${plan.time}`;
  if (plan.jitter_minutes) text += ` ±${plan.jitter_minutes} 分钟`;
  const days = weekdaysText(plan.weekdays);
  return days ? `${text} · ${days}` : text;
}

/** "私聊（主人）" or the group's alias (a group that is no longer known keeps its key). */
export function targetLabel(target, groups) {
  if (!target || target.type !== "group") return "私聊（主人）";
  const group = (groups || []).find((item) => item.key === target.key);
  return group ? group.alias : `群 ${target.key}（未记录）`;
}

export function formatDateTime(iso) {
  return typeof iso === "string" && iso ? iso.replace("T", " ").slice(0, 16) : "";
}

export function nextFireText(plan) {
  if (!plan.enabled) return "未启用";
  return plan.next_fire ? formatDateTime(plan.next_fire) : "暂无";
}

/** The plan without the fields only the server computes, as the body for saving it again. */
export function planPayload(plan, overrides = {}) {
  const { next_fire, running, ...rest } = plan;
  return { ...rest, ...overrides };
}

export function emptyPlanForm(kind = "daily") {
  return {
    id: "", name: "", enabled: true, kind, time: "08:00", weekdays: [], jitter: 0,
    start: "10:00", end: "20:00", every: 120, target: "private", prompt: "", fallbackText: ""
  };
}

/** Form values (strings, a weekday list, "private" | "group:<key>") from a plan or template. */
export function planToForm(plan) {
  const form = emptyPlanForm(plan.kind === "interval" ? "interval" : "daily");
  form.id = plan.id || "";
  form.name = plan.name || "";
  form.enabled = plan.enabled !== false;
  if (plan.kind === "interval") {
    form.start = plan.start; form.end = plan.end; form.every = plan.every_minutes;
  } else {
    form.time = plan.time; form.weekdays = [...(plan.weekdays || [])]; form.jitter = plan.jitter_minutes || 0;
  }
  form.target = plan.target?.type === "group" ? `group:${plan.target.key}` : "private";
  form.prompt = plan.prompt || "";
  form.fallbackText = (plan.fallback_texts || []).join("\n");
  return form;
}

export function fallbackLines(text) {
  return String(text ?? "").split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
}

const HHMM = /^([01]\d|2[0-3]):[0-5]\d$/;

/** Front-end checks only; the server validates again and its message is shown as well. */
export function validatePlanForm(form) {
  const errors = [];
  const name = String(form.name ?? "").trim();
  if (!name) errors.push("请填写名称");
  else if (name.length > PLAN_LIMITS.name) errors.push(`名称不能超过 ${PLAN_LIMITS.name} 个字`);
  if (form.kind === "interval") {
    if (!HHMM.test(form.start) || !HHMM.test(form.end)) errors.push("开始和结束时间需要是 HH:MM");
    else if (form.start >= form.end) errors.push("开始时间必须早于结束时间（不能跨午夜）");
    const every = Number(form.every);
    if (!Number.isInteger(every) || every < PLAN_LIMITS.every[0] || every > PLAN_LIMITS.every[1]) {
      errors.push(`间隔需要是 ${PLAN_LIMITS.every[0]}～${PLAN_LIMITS.every[1]} 分钟的整数`);
    }
  } else {
    if (!HHMM.test(form.time)) errors.push("时刻需要是 HH:MM");
    const jitter = Number(form.jitter === "" ? 0 : form.jitter);
    if (!Number.isInteger(jitter) || jitter < PLAN_LIMITS.jitter[0] || jitter > PLAN_LIMITS.jitter[1]) {
      errors.push(`浮动需要是 ${PLAN_LIMITS.jitter[0]}～${PLAN_LIMITS.jitter[1]} 分钟的整数`);
    }
  }
  const prompt = String(form.prompt ?? "").trim();
  if (!prompt) errors.push("请填写要求");
  else if (prompt.length > PLAN_LIMITS.prompt) errors.push(`要求不能超过 ${PLAN_LIMITS.prompt} 个字`);
  const lines = fallbackLines(form.fallbackText);
  if (lines.length > PLAN_LIMITS.fallbackCount) errors.push(`备用文字最多 ${PLAN_LIMITS.fallbackCount} 句`);
  if (lines.some((line) => line.length > PLAN_LIMITS.fallbackChars)) {
    errors.push(`每句备用文字不能超过 ${PLAN_LIMITS.fallbackChars} 个字`);
  }
  return errors;
}

// ---------- Proactive chat (主动搭话) ----------

export const CHAT_LIMITS = { silence: [30, 1440], factor: [1, 4], unanswered: [0, 10], daily: [0, 20], prompt: 300, windows: 6, gap: [0, 240], dedupe: [0.5, 1] };
export const CHAT_STATUS_TEXT = {
  Disabled: "已关闭",
  NoOwner: "没有主人私聊会话（请先用主人账号私聊一次）",
  Running: "正在生成并发送…",
  UnansweredLimit: "等待中：未回已达上限，等你开口后恢复",
  DailyLimit: "等待中：今天已发满",
  Waiting: "等待中：还没到时间",
  Retry: "等待中：上次没发成，稍后重试",
  QuietHours: "等待中：免打扰时间",
  OutsideWindow: "等待中：不在作息时段",
  Busy: "等待中：ST 正在忙或消息正在发送",
  Conversing: "等待中：正在对话",
  Gap: "等待中：离上一条主动消息太近",
  T1Priority: "等待中：定时计划优先",
  Ready: "马上发送"
};

/** The state line: reason text plus the expected time when there is one. */
export function chatStatusText(status) {
  if (!status) return "不可用";
  const base = CHAT_STATUS_TEXT[status.reason] || status.reason;
  return status.next_at ? `${base}（预计 ${formatDateTime(status.next_at)}）` : base;
}

export function chatCountText(status, key) {
  if (!status) return "-";
  if (key === "today") return `${status.today} / ${status.daily_limit}`;
  return `${status.unanswered}（超过 ${status.max_unanswered} 次就先不发）`;
}

/** Fields of the form → the body for POST /api/schedule/chat. */
export function chatPayload(values, windows) {
  return {
    enabled: Boolean(values.enabled),
    silence_min: Number(values.silence_min),
    silence_max: Number(values.silence_max),
    followup_factor: Number(values.factor),
    max_unanswered: Number(values.max_unanswered),
    daily_limit: Number(values.daily_limit),
    quiet_hours: { start: values.quiet_start, end: values.quiet_end },
    active_windows: { weekday: windows.weekday, weekend: windows.weekend },
    prompt: String(values.prompt ?? "").trim()
  };
}

/** Problems with the chat form as a list of {key, text}. */
export function validateChatForm(values, windows) {
  const problems = [];
  const inRange = (value, [low, high]) => Number.isFinite(value) && value >= low && value <= high;
  const min = Number(values.silence_min);
  const max = Number(values.silence_max);
  if (!Number.isInteger(min) || !inRange(min, CHAT_LIMITS.silence)) problems.push({ key: "silence_min", text: `最短沉默需要是 ${CHAT_LIMITS.silence[0]}～${CHAT_LIMITS.silence[1]} 分钟的整数` });
  if (!Number.isInteger(max) || !inRange(max, CHAT_LIMITS.silence)) problems.push({ key: "silence_max", text: `最长沉默需要是 ${CHAT_LIMITS.silence[0]}～${CHAT_LIMITS.silence[1]} 分钟的整数` });
  else if (Number.isInteger(min) && min > max) problems.push({ key: "silence_max", text: "最长沉默不能小于最短沉默" });
  if (!inRange(Number(values.factor), CHAT_LIMITS.factor)) problems.push({ key: "factor", text: `追问倍数需要是 ${CHAT_LIMITS.factor[0]}～${CHAT_LIMITS.factor[1]}` });
  if (!Number.isInteger(Number(values.max_unanswered)) || !inRange(Number(values.max_unanswered), CHAT_LIMITS.unanswered)) problems.push({ key: "max_unanswered", text: `最多追问需要是 ${CHAT_LIMITS.unanswered[0]}～${CHAT_LIMITS.unanswered[1]} 的整数` });
  if (!Number.isInteger(Number(values.daily_limit)) || !inRange(Number(values.daily_limit), CHAT_LIMITS.daily)) problems.push({ key: "daily_limit", text: `每天最多需要是 ${CHAT_LIMITS.daily[0]}～${CHAT_LIMITS.daily[1]} 的整数` });
  if (!HHMM.test(values.quiet_start)) problems.push({ key: "quiet_start", text: "免打扰开始需要是 HH:MM" });
  if (!HHMM.test(values.quiet_end)) problems.push({ key: "quiet_end", text: "免打扰结束需要是 HH:MM" });
  for (const [name, label] of [["weekday", "工作日"], ["weekend", "周末"]]) {
    const list = windows[name] || [];
    if (list.length > CHAT_LIMITS.windows) problems.push({ text: `${label}作息最多 ${CHAT_LIMITS.windows} 段` });
    list.forEach((item, index) => {
      if (!HHMM.test(item.start) || !HHMM.test(item.end)) problems.push({ key: `win:${name}:${index}:start`, text: `${label}第 ${index + 1} 段的时间需要是 HH:MM` });
      else if (item.start >= item.end) problems.push({ key: `win:${name}:${index}:end`, text: `${label}第 ${index + 1} 段的开始必须早于结束（不能跨午夜）` });
    });
  }
  const prompt = String(values.prompt ?? "").trim();
  if (!prompt) problems.push({ key: "prompt", text: "请填写提示" });
  else if (prompt.length > CHAT_LIMITS.prompt) problems.push({ key: "prompt", text: `提示不能超过 ${CHAT_LIMITS.prompt} 个字` });
  return problems;
}

/** Body for POST /api/schedule/plans (no id: create). */
export function buildPlanBody(form) {
  const body = {
    name: String(form.name ?? "").trim(),
    enabled: Boolean(form.enabled),
    kind: form.kind === "interval" ? "interval" : "daily",
    target: String(form.target || "").startsWith("group:")
      ? { type: "group", key: String(form.target).slice("group:".length) }
      : { type: "private" },
    prompt: String(form.prompt ?? "").trim(),
    fallback_texts: fallbackLines(form.fallbackText)
  };
  if (form.id) body.id = form.id;
  if (body.kind === "interval") {
    body.start = form.start; body.end = form.end; body.every_minutes = Number(form.every);
  } else {
    body.time = form.time;
    body.weekdays = [...new Set(form.weekdays || [])].sort((a, b) => a - b);
    body.jitter_minutes = Number(form.jitter === "" ? 0 : form.jitter);
  }
  return body;
}

const RESULT_REASONS = {
  NoOwnerSession: "还没记录主人私聊，请先用主人账号私聊{昵称}一次",
  UnknownGroup: "这个群还没有被记录",
  NoFallbackText: "ST 没有回复，且没有备用文字",
  Missed: "错过了时间",
  DeferTimeout: "对方一直在聊天，推迟超时",
  Probability: "概率未命中",
  Cooldown: "冷却中",
  NoTag: "回复没有情绪标签",
  InvalidTag: "情绪标签无效",
  NoImages: "该情绪没有表情包",
  Disabled: "已关闭",
  TtsUnavailable: "语音服务不可用",
  NoText: "没有可朗读的文字",
  Fallback: "已改发备用文字",
  NotAttempted: "未执行",
  Unavailable: "不可用",
  PrepareError: "准备失败"
};

const FAILED_REASONS = {
  Duplicate: "ST 写的和上一条太像，已丢弃",
  StUnavailable: "ST 不可用",
  StFailed: "ST 没有回复",
  InvalidReply: "回复格式无效",
  QueueFull: "队列已满",
  QueueTimeout: "排队超时",
  QueueUnavailable: "队列不可用"
};

// What the console shows for the nickname until the character list is loaded (the default character's nickname).
export const DEFAULT_NICKNAME = "琴音";

/** "sent" / "fallback" / "skip:X" / "failed:T" (and missing values) as Chinese text. */
export function resultText(value, nickname = DEFAULT_NICKNAME) {
  if (value === "sent") return "已发送";
  if (value === "fallback") return "已发备用文字";
  if (value === "sent:fallback(duplicate)") return "已发备用文字（ST 写的和上一条太像）";
  if (value === "deferred:Requeued") return "已让路（刚有人开口，等安静了再发）";
  if (typeof value !== "string" || !value) return "-";
  if (value.startsWith("skip:")) {
    const reason = value.slice(5);
    return `跳过（${(RESULT_REASONS[reason] || reason).replace("{昵称}", nickname)}）`;
  }
  if (value.startsWith("failed:")) return `失败（${FAILED_REASONS[value.slice(7)] || value.slice(7)}）`;
  return value;
}

function seconds(ms) {
  return `${(Number(ms || 0) / 1000).toFixed(1)} 秒`;
}

/** Lines shown after "立即试发"; a group target gets the delivery reminder. */
export function testResultLines(result, isGroup, nickname = DEFAULT_NICKNAME) {
  const lines = [
    `结果：${resultText(result.result, nickname)}`,
    `语音：${resultText(result.voice, nickname)}`,
    `表情包：${resultText(result.sticker, nickname)}`,
    `ST 耗时：${seconds(result.st_ms)} · 总耗时：${seconds(result.total_ms)}`
  ];
  if (isGroup) lines.push(GROUP_SEND_NOTE);
  return lines;
}

// ---------- Song covers ----------

/** "3:05" / "1:02:03" for a number of seconds; "-" when unknown. */
export function formatClock(seconds) {
  const total = Math.round(Number(seconds));
  if (!Number.isFinite(total) || total < 0) return "-";
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const rest = String(total % 60).padStart(2, "0");
  return hours ? `${hours}:${String(minutes).padStart(2, "0")}:${rest}` : `${minutes}:${rest}`;
}

/** Seconds from "mm:ss", "h:mm:ss" or a plain number; null for an empty text (clear), NaN when invalid. */
export function parseClock(text) {
  const value = String(text ?? "").trim();
  if (!value) return null;
  const parts = value.split(":");
  if (parts.length > 3 || parts.some((part) => !/^\d+(\.\d+)?$/.test(part.trim()))) return NaN;
  return parts.reduce((total, part) => total * 60 + Number(part), 0);
}

export function songLabel(song) {
  return `${song.title} · ${formatClock(song.duration)}`;
}

const COVER_ERRORS = {
  Disabled: "翻唱功能已关闭",
  NoModel: "还没有选择声音模型",
  RvcRootNotSet: "还没有设置 RVC 目录",
  SongNotFound: "找不到这首歌",
  SongFileMissing: "歌曲文件丢失，请重新上传",
  QueueFull: "队列已满（最多 1 首在处理 + 3 首排队），请稍后再试",
  ServiceStartFailed: "RVC 工作进程启动失败，请看上方服务状态",
  Timeout: "处理超时，工作进程已停止",
  WorkerUnreachable: "连不上 RVC 工作进程",
  SeparationFailed: "人声分离失败",
  SeparationOutputMissing: "人声分离没有产出文件",
  ConversionFailed: "声音转换失败",
  ConversionEmpty: "声音转换没有产出",
  ModelNotFound: "RVC 里找不到这个声音模型",
  PitchShiftUnavailable: "伴奏变调需要先在 RVC 里安装 pedalboard（见控制台说明）",
  PitchShiftFailed: "伴奏变调失败",
  SepModelNotFound: "RVC 里找不到这个去伴奏 / 去混响模型（assets/pymss_weights）",
  InputNotFound: "工作进程读不到歌曲文件",
  RvcImportFailed: "RVC 环境加载失败（看 cover_service.log）",
  OutputMissing: "工作进程没有产出文件",
  ClipFailed: "截取片段失败（ffmpeg）",
  MixFailed: "混音失败（ffmpeg）",
  Busy: "工作进程正忙",
  MusicKeyNotSet: "还没有设置音乐 API 密钥（在上方“在线歌源”里填写并保存）",
  "music api key not set": "还没有设置音乐 API 密钥（在上方“在线歌源”里填写并保存）",
  not_found: "所有来源都没有找到完整的歌",
  preview_only: "这个来源只给了试听片段（可能是 VIP 歌曲），没有保存",
  api_failed: "来源返回失败（没有结果或需要权限）",
  no_link: "来源没有给出下载链接",
  timeout: "连接来源超时",
  too_large: "文件超过 50 MB 上限，已中止",
  empty: "下载到的文件是空的",
  invalid_url: "下载链接不是 http / https",
  "invalid source": "不认识的来源"
};

export function coverErrorText(kind) {
  const text = String(kind ?? "");
  if (COVER_ERRORS[text]) return COVER_ERRORS[text];
  if (/^HTTP\d+$/.test(text)) return `来源返回 ${text}`;
  if (text.startsWith("network:")) return "连不上来源（网络错误）";
  return `失败（${text}）`;
}

export const MUSIC_SOURCES = { wy: "网易云", qq: "QQ 音乐", kw: "酷我", kg: "酷狗", mg: "咪咕", qi: "千千" };

export function sourceLabel(code) {
  return MUSIC_SOURCES[code] || code || "未知";
}

/** "歌名 - 歌手" (manual uploads have no artist). */
export function songName(song) {
  return song.artist ? `${song.title} - ${song.artist}` : song.title;
}

/** "wy, kg，qq" -> { sources, error }: known, unique, at least one. */
export function parseSources(text) {
  const sources = String(text ?? "").split(/[\s,，、]+/).filter(Boolean);
  if (!sources.length) return { sources, error: "至少要有一个来源" };
  const unknown = sources.find((item) => !MUSIC_SOURCES[item]);
  if (unknown) return { sources, error: `不认识的来源：${unknown}（可用：${Object.keys(MUSIC_SOURCES).join("、")}）` };
  if (new Set(sources).size !== sources.length) return { sources, error: "来源不能重复" };
  return { sources, error: "" };
}

/** Label under the API key box; the key itself never reaches the page. */
export function keyStatusText(settings) {
  if (!settings.key_set) return "未设置";
  return settings.key_tail ? `已设置 …${settings.key_tail}` : "已设置";
}

/** Error text for an empty search box, else "". */
export function validateOnlineName(name) {
  const text = String(name ?? "").trim();
  if (!text) return "请输入歌名";
  if (text.length > 200) return "歌名太长";
  return "";
}

/** One line per source that failed in an "auto" search. */
export function searchErrorLines(errors) {
  return (errors || []).map((item) => `${sourceLabel(item.source)}：${coverErrorText(item.error)}`);
}

function secondsText(ms) {
  return `${(Number(ms || 0) / 1000).toFixed(1)} 秒`;
}

/** Timing line under a finished try-sing; a cache hit has nothing to time. */
export function timingText(result) {
  if (result.cached) return "命中缓存，没有重新生成";
  const t = result.timings || {};
  const dereverb = (t.dereverb_ms === undefined ? "" : ` · 去混响 ${secondsText(t.dereverb_ms)}`)
    + (t.pitch_ms === undefined ? "" : ` · 伴奏变调 ${secondsText(t.pitch_ms)}`);
  return `去伴奏 ${secondsText(t.sep_ms)}${dereverb} · 声音转换 ${secondsText(t.vc_ms)} · 混音 ${secondsText(t.mix_ms)} · 合计 ${secondsText(t.total_ms)}`;
}

/** "自动匹配：xxx.index" or the manual/none cases. */
export function indexText(settings, autoIndex) {
  if (settings.index_path) return "手动指定";
  return autoIndex ? `自动匹配：${autoIndex}` : "没有找到匹配的索引文件（将不使用索引）";
}

// ---------- Storage cleanup ----------

export const CLEANUP_TARGETS = {
  astrbot_temp: "AstrBot 临时目录（语音、图片发送残留）",
  bridge_temp: "bridge 临时语音（temp_audio）",
  cover_temp: "翻唱工作进程临时目录（stbridge_cover_*）",
  cover_stems: "翻唱中间文件（cover_stems）"
};

/** "0 B" / "12 KB" / "3.4 MB" / "1.20 GB" (formatBytes shows 1 KB for an empty folder). */
export function bytesText(size) {
  const n = Number(size) || 0;
  if (n <= 0) return "0 B";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${Math.round(n / 1024)} KB`;
  if (n < 1024 * 1024 * 1024) return `${(n / (1024 * 1024)).toFixed(1)} MB`;
  return `${(n / (1024 * 1024 * 1024)).toFixed(2)} GB`;
}

/** Error text for the keep-days box, else "". Whole days, 1～365. */
export function validateKeepDays(value) {
  const text = String(value ?? "").trim();
  if (!/^\d+$/.test(text)) return "保留天数需要是 1～365 的整数";
  const days = Number(text);
  return days >= 1 && days <= 365 ? "" : "保留天数需要是 1～365 的整数";
}

export function cleanupSettingsPayload(enabled, keepDays) {
  return { enabled: Boolean(enabled), keep_days: Number(String(keepDays).trim()) };
}

/** "已清理 N 个文件，释放 X；N 个被占用已跳过". */
export function cleanupResultText(result) {
  const skipped = result.skipped ? `；${result.skipped} 个文件被占用，已跳过` : "";
  return `已清理 ${result.removed} 个文件，释放 ${bytesText(result.bytes)}${skipped}`;
}

/** Line for the last automatic run; `last` is {} before the first one. `time` is epoch seconds. */
export function lastAutoText(last) {
  if (!last || !last.time) return "还没有自动清理过";
  const when = new Date(Number(last.time) * 1000);
  const pad = (n) => String(n).padStart(2, "0");
  const stamp = `${when.getFullYear()}-${pad(when.getMonth() + 1)}-${pad(when.getDate())} ${pad(when.getHours())}:${pad(when.getMinutes())}`;
  return `${stamp} · ${cleanupResultText(last)}`;
}

// ---------- Chat song requests ----------

const SONG_REQUEST_NUMBERS = ["owner_min_affinity", "group_min_affinity", "refuse_percent", "list_timeout_seconds", "per_source", "daily_limit"];

/** One non-empty trimmed line per entry. */
export function textLines(text) {
  return String(text ?? "").split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
}

/** Body for the song-request settings from raw form values; blank numbers become NaN so the server answers 400. */
export function songRequestPayload(raw) {
  const body = {
    enabled: Boolean(raw.enabled), delivery_mode: String(raw.delivery_mode ?? "passive"),
    limit_owner: Boolean(raw.limit_owner), permission_variable: String(raw.permission_variable ?? "").trim()
  };
  for (const name of SONG_REQUEST_NUMBERS) {
    const value = raw[name];
    body[name] = value === "" || value === null || value === undefined ? NaN : Number(value);
  }
  return body;
}

const COVER_NUMBERS = [
  "port", "f0_up_key", "inst_pitch", "index_rate", "vocal_reverb", "rms_mix_rate", "protect", "clip_seconds",
  "default_start_percent", "vocal_gain_db", "inst_gain_db", "idle_stop_minutes", "job_timeout_seconds",
  "min_full_seconds", "download_timeout_seconds", "output_sample_rate"
];

/** Settings body from raw form values (strings): numbers converted, blanks left invalid for the server. */
export function coverSettingsPayload(base, raw) {
  const body = { ...base };
  for (const [name, value] of Object.entries(raw)) {
    if (COVER_NUMBERS.includes(name)) body[name] = value === "" || value === null ? NaN : Number(value);
    else if (name === "enabled" || name === "keep_stems") body[name] = Boolean(value);
    else if (name === "sources") body[name] = Array.isArray(value) ? [...value] : [];
    else if (name === "pitch_presets") body[name] = Array.isArray(value) ? value.map((item) => ({ ...item, keywords: [...item.keywords] })) : [];
    else if (name === "clear_music_api_key") body[name] = value === true;
    else body[name] = String(value ?? "").trim();
  }
  // key_set / key_tail are display-only; an empty key means "keep the saved one".
  delete body.key_set;
  delete body.key_tail;
  return body;
}

// ---------- Pitch presets ----------

export const PITCH_PRESET_LIMIT = 10;

/** Keywords from one text box: split on commas, 顿号, semicolons and whitespace; blanks dropped. */
export function splitKeywords(text) {
  return String(text ?? "").split(/[,，、;；\s]+/).map((word) => word.trim()).filter(Boolean);
}

/** Preset list for the settings body from raw rows ({name, vocal, inst, keywords} as typed); numbers blank → NaN. */
export function pitchPresetsPayload(rows) {
  const number = (value) => (value === "" || value === null || value === undefined ? NaN : Number(value));
  return (rows || []).map((row) => ({
    name: String(row.name ?? "").trim(), vocal: number(row.vocal), inst: number(row.inst),
    keywords: Array.isArray(row.keywords) ? row.keywords.map((word) => String(word).trim()).filter(Boolean) : splitKeywords(row.keywords)
  }));
}

/** Moves rows[index] by delta (-1 up, +1 down) in a copy; out-of-range moves return the list unchanged. */
export function movePreset(rows, index, delta) {
  const target = index + delta;
  if (index < 0 || index >= rows.length || target < 0 || target >= rows.length) return [...rows];
  const copy = [...rows];
  [copy[index], copy[target]] = [copy[target], copy[index]];
  return copy;
}

export const PEDALBOARD_NEEDS_ROOT = "请先设置 RVC 目录";

/** The pip command that installs pedalboard into the RVC folder's own Python (the folder comes from the saved settings). */
export function pedalboardInstallCommand(rvcRoot) {
  const root = String(rvcRoot ?? "").trim().replace(/[\\/]+$/, "");
  if (!root) return PEDALBOARD_NEEDS_ROOT;
  return `"${root}\\runtime\\python.exe" -m pip install --no-deps pedalboard -i https://mirrors.pku.edu.cn/pypi/simple`;
}

// ---------- Unsaved-change tracking and the page save bar ----------

/** The page named by a location hash ("#cover/x" -> "cover"), or "" when there is none. */
export function pageFromHash(hash) {
  return String(hash ?? "").replace(/^#/, "").split("/")[0];
}

/** Field names whose value differs between a snapshot and the current values (a missing field counts as changed). */
export function diffFields(snapshot, current) {
  const before = snapshot || {};
  const after = current || {};
  const names = new Set([...Object.keys(before), ...Object.keys(after)]);
  return [...names].filter((name) => !(name in before) || !(name in after) || before[name] !== after[name]);
}

export function unsavedText(count) {
  return `有 ${count} 处未保存的修改`;
}

/**
 * Re-render a list that holds unsaved edits: `before` are the values typed so far, `oldSnapshot` the
 * baseline they are compared with, `fresh` the values of the re-rendered rows. Rows that existed keep
 * the typed values and the old baseline; new rows start clean; removed rows are dropped.
 */
export function mergeSnapshot(oldSnapshot, before, fresh) {
  const values = {};
  const snapshot = {};
  for (const name of Object.keys(fresh)) {
    const known = name in oldSnapshot && name in before;
    values[name] = known ? before[name] : fresh[name];
    snapshot[name] = name in oldSnapshot ? oldSnapshot[name] : fresh[name];
  }
  return { values, snapshot };
}

/**
 * One body for POST /api/cover/settings: the server's settings with every changed part laid over them.
 * `parts` are raw patches (as typed); a blank key means "keep the saved one", clear_music_api_key clears it.
 */
export function mergeCoverPatch(base, parts) {
  const raw = {};
  for (const part of parts) Object.assign(raw, part);
  return coverSettingsPayload(base, raw);
}

/**
 * Run save steps in order and stop at the first failure.
 * Each step is {id, label, run}; `run` is awaited and may return a notice.
 * Returns {done: [ids], failed: {id, label, error} | null, pending: [ids never started], notices: [text]}.
 */
export async function runSaves(steps) {
  const done = [];
  const notices = [];
  for (let index = 0; index < steps.length; index += 1) {
    const step = steps[index];
    try {
      const notice = await step.run();
      done.push(step.id);
      if (notice) notices.push(notice);
    } catch (error) {
      return {
        done, notices,
        failed: { id: step.id, label: step.label, error },
        pending: steps.slice(index + 1).map((item) => item.id)
      };
    }
  }
  return { done, notices, failed: null, pending: [] };
}

/** Red text for the save bar: which part failed and why. */
export function saveFailureText(failed) {
  const message = failed.error instanceof Error ? failed.error.message : String(failed.error);
  return `${failed.label}保存失败：${message}`;
}

// ---------- U2: pages, tabs, ST status, paging, collapsed cards ----------

export const PAGE_LABELS = {
  overview: "总览", voice: "语音", emotions: "角色和情绪", stickers: "表情包",
  schedule: "定时计划", sing: "唱歌", affinity: "好感度", injection: "ST 注入", install: "安装与路径", system: "系统"
};
export const SING_TABS = ["library", "params", "chat", "service"];
export const SING_TAB_LABELS = { library: "歌库", params: "翻唱参数", chat: "聊天点歌", service: "服务与缓存" };
export const SYSTEM_TABS = ["general", "platform", "texts", "logs", "cleanup", "about"];
export const SYSTEM_TAB_LABELS = { general: "常规", platform: "聊天平台", texts: "回复文案", logs: "日志", cleanup: "存储清理", about: "关于" };
/** The pages that have tabs, and which tabs. */
export const PAGE_TABS = { sing: SING_TABS, system: SYSTEM_TABS };

/** The label of a tab of a page ("" for an unknown one). */
export function tabLabel(page, tab) {
  return ({ sing: SING_TAB_LABELS, system: SYSTEM_TAB_LABELS }[page] || {})[tab] || "";
}

// Page names of earlier console versions (old bookmarks and hashes).
export const LEGACY_PAGES = {
  dashboard: "overview", irodori: "voice", models: "voice", characters: "emotions", audios: "emotions",
  cover: "sing", cleanup: "system"
};

/** The sidebar pages: always all of them, whatever the engine. */
export function visiblePages() {
  return Object.keys(PAGE_LABELS);
}

/** The page to show for a name from a hash: old names map to their new page, unknown ones to the overview. */
export function resolvePage(page) {
  const mapped = LEGACY_PAGES[page] || page;
  return visiblePages().includes(mapped) ? mapped : "overview";
}

/** The tab named by "#sing/params" ("params"), or "" when there is none. */
export function tabFromHash(hash) {
  return String(hash ?? "").replace(/^#/, "").split("/")[1] || "";
}

/** A valid tab for the page: only the sing and system pages have tabs; an unknown tab falls back to the first. */
export function resolveTab(page, tab) {
  const tabs = PAGE_TABS[page];
  if (!tabs) return "";
  return tabs.includes(tab) ? tab : tabs[0];
}

export function hashFor(page, tab) {
  return tab ? `#${page}/${tab}` : `#${page}`;
}

// ST connection, from GET /api/status (ST polls about once a second, but not while it generates)
export const ST_ONLINE_SECONDS = 10;

export function agoText(seconds) {
  const total = Math.max(0, Math.round(Number(seconds) || 0));
  if (total < 60) return `${total} 秒前`;
  const minutes = Math.floor(total / 60);
  return minutes < 60 ? `${minutes} 分钟前` : `${Math.floor(minutes / 60)} 小时前`;
}

/** {code, text} for the ST badge: online / generating / offline / never / stopped / unknown. */
export function stStatus(bridge) {
  if (!bridge || typeof bridge !== "object") return { code: "unknown", text: "未知" };
  if (bridge.http_running === false) return { code: "stopped", text: "桥接未运行" };
  if (bridge.active) return { code: "generating", text: "正在生成" };
  const age = bridge.last_poll_age;
  if (age === null || age === undefined) return { code: "never", text: "从未连接" };
  if (age <= ST_ONLINE_SECONDS) return { code: "online", text: "在线" };
  return { code: "offline", text: `离线（最近一次 ${agoText(age)}）` };
}

export function bridgeGateText(gate) {
  if (gate === "RECOVERED") return "已就绪";
  if (gate === "RECOVERY_REQUIRED") return "等待 ST 就绪";
  if (gate === "STOPPING") return "正在停止";
  if (gate === "STOPPED") return "已停止";
  if (gate === null || gate === undefined) return "未运行";
  return "未知";
}

// Long lists are paged in the browser only: every row stays in the page and the other pages are hidden.
export const LIST_PAGE_SIZE = 20;
export const STICKER_PAGE_SIZE = 24;

/** The slice of a list for a page; an out-of-range page is pulled back to the nearest valid one. */
export function pageSlice(total, page, size) {
  const pages = Math.max(1, Math.ceil(total / size));
  const wanted = Math.trunc(Number(page)) || 1;
  const current = Math.min(Math.max(1, wanted), pages);
  const start = (current - 1) * size;
  return { start, end: Math.min(total, start + size), pages, page: current };
}

/** Every whitespace-separated word of the query appears in the text (case-insensitive); an empty query matches. */
export function matchesQuery(text, query) {
  const words = String(query ?? "").toLowerCase().split(/\s+/).filter(Boolean);
  const haystack = String(text ?? "").toLowerCase();
  return words.every((word) => haystack.includes(word));
}

/**
 * Which rows are on screen: `matches[i]` says whether row i passes the search; the matching rows are
 * paged. Returns {shown: [bool per row], slice, matched: [row indexes in list order]}.
 */
export function layoutRows(matches, page, size) {
  const matched = [];
  matches.forEach((ok, index) => { if (ok) matched.push(index); });
  const slice = pageSlice(matched.length, page, size);
  const shown = matches.map(() => false);
  for (const index of matched.slice(slice.start, slice.end)) shown[index] = true;
  return { shown, slice, matched };
}

/** Pages (1-based) that hold at least one of the dirty rows; `matched` lists the row indexes in page order. */
export function dirtyPages(matched, dirtyRows, size) {
  const dirty = new Set(dirtyRows);
  const pages = new Set();
  matched.forEach((row, position) => { if (dirty.has(row)) pages.add(Math.floor(position / size) + 1); });
  return [...pages].sort((a, b) => a - b);
}

/** The page (1-based) that shows a row, or 0 when it is filtered out. */
export function pageOfRow(matched, row, size) {
  const position = matched.indexOf(row);
  return position < 0 ? 0 : Math.floor(position / size) + 1;
}

export const AFFINITY_SORTS = [
  ["updated", "最近更新"], ["high", "好感度从高到低"], ["low", "好感度从低到高"], ["name", "昵称"]
];

/** Row indexes of the users in display order. Users: {updated_at, value, nickname, id}. Ties keep the server order. */
export function sortUserOrder(users, mode) {
  const order = users.map((_, index) => index);
  const time = (user) => (user.updated_at ? Date.parse(user.updated_at) || 0 : 0);
  const name = (user) => String(user.nickname || user.id || "");
  const compare = {
    high: (a, b) => b.value - a.value,
    low: (a, b) => a.value - b.value,
    name: (a, b) => name(a).localeCompare(name(b), "zh-Hans-CN"),
    updated: (a, b) => time(b) - time(a)
  }[mode] || (() => 0);
  return order.sort((a, b) => compare(users[a], users[b]) || a - b);
}

/** Tabs that hold unsaved changes. Items: {tab, count}. */
export function dirtyTabs(items) {
  return new Set(items.filter((item) => item.tab && item.count > 0).map((item) => item.tab));
}

/**
 * Where a row menu goes: right-aligned under its trigger, flipped above it when there is no room below,
 * always kept inside the window. `rect` is the trigger's box, `size` the menu's, `view` the window's.
 */
export function menuPlacement(rect, size, view, gap = 4, margin = 8) {
  const left = Math.max(margin, Math.min(rect.right - size.width, view.width - size.width - margin));
  const below = rect.bottom + gap;
  const fitsBelow = below + size.height <= view.height - margin;
  const above = rect.top - gap - size.height;
  if (!fitsBelow && above >= margin) return { top: above, left, up: true };
  return { top: Math.max(margin, Math.min(below, view.height - size.height - margin)), left, up: false };
}

/** The menu item that gets the focus after a key press (ArrowUp / ArrowDown wrap around, Home / End jump). */
export function nextMenuIndex(index, count, key) {
  if (!count) return -1;
  if (key === "ArrowDown") return index < 0 ? 0 : (index + 1) % count;
  if (key === "ArrowUp") return index < 0 ? count - 1 : (index - 1 + count) % count;
  if (key === "Home") return 0;
  if (key === "End") return count - 1;
  return index;
}

/** "已播放 / 总长" under a ▶ button; the total is left out while the browser does not know it yet. */
export function playTimeText(current, total) {
  const clock = (value) => {
    const seconds = Math.max(0, Math.floor(Number(value) || 0));
    return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
  };
  return Number.isFinite(total) && total > 0 ? `${clock(current)} / ${clock(total)}` : clock(current);
}

/** Empty-list states: one sentence, and the main action when there is one (it clicks the original button). */
export const EMPTY_STATES = {
  models: { text: "还没有模型。", action: "new-model", label: "＋ 添加模型" },
  characters: { text: "还没有角色。", action: "new-character", label: "＋ 添加角色" },
  plans: { text: "还没有计划。", action: "new-plan", label: "新建计划" },
  songs: { text: "歌库是空的。", action: "song-file", label: "上传歌曲" },
  stickers: { text: "这个情绪还没有表情包。", upload: true, label: "上传" },
  users: { text: "还没有用户。QQ 用户通过桥接收到第一条带好感度变化的回复后会出现在这里。" },
  covers: { text: "还没有翻唱。" },
  autos: { text: "还没有自动存档。" },
  presets: { text: "当前设置还没有保存为预设。" }
};

/** HTML of an empty state; `options.upload` (an already escaped name) makes the button open that sticker group's file chooser. */
export function emptyStateMarkup(kind, options = {}) {
  const state = EMPTY_STATES[kind];
  if (!state) return "";
  let button = "";
  if (state.action) button = `<button type="button" class="btn btn-primary btn-sm" data-empty-action="${state.action}">${state.label}</button>`;
  else if (state.upload && options.upload !== undefined) button = `<button type="button" class="btn btn-primary btn-sm" data-empty-upload="${options.upload}">${state.label}</button>`;
  return `<div class="empty-state"><p>${state.text}</p>${button}</div>`;
}

/** Key under which a form module's collapsed state is remembered (shares the card map in localStorage). */
export function moduleKey(slug) {
  return `module:${slug}`;
}

/**
 * One form field: short label, control, optional unit suffix and help text, and a slot for the field's error message.
 * `control` is ready-made HTML (the caller escapes its values). A switch puts the name left and the toggle right.
 */
export function fieldMarkup({ control, label, unit = "", help = "", mono = false, wide = false, switchField = false }) {
  const labelClass = mono ? "field-label mono" : "field-label";
  const helpHtml = help ? `<small class="field-help">${help}</small>` : "";
  if (switchField) {
    return `<label class="field field-switch field-wide"><span class="field-text"><span class="${labelClass}">${label}</span>${helpHtml}</span>${control}</label>`;
  }
  const body = unit ? `<span class="field-input">${control}<span class="field-unit">${unit}</span></span>` : control;
  return `<label class="${wide ? "field field-wide" : "field"}"><span class="${labelClass}">${label}</span>${body}${helpHtml}<small class="field-msg" hidden></small></label>`;
}

/** A collapsible group of related fields; open by default, the open state is remembered per slug. */
export function moduleMarkup(slug, title, body) {
  return `<details class="module" data-module="${slug}" open><summary>${title}</summary><div class="module-body">${body}</div></details>`;
}

/** Saved collapsed-card map from localStorage text; anything unreadable gives {}. */
export function parseCollapsed(text) {
  let data;
  try { data = JSON.parse(text); } catch { return {}; }
  if (!data || typeof data !== "object" || Array.isArray(data)) return {};
  return Object.fromEntries(Object.entries(data).filter(([, value]) => typeof value === "boolean"));
}

/** Overview switches: where each feature is configured. */
export const FEATURE_LINKS = [
  { key: "stickers", label: "表情包", target: "stickers" },
  { key: "schedule", label: "定时计划", target: "schedule" },
  { key: "cover", label: "翻唱", target: "sing/service" },
  { key: "song_request", label: "聊天点歌", target: "sing/chat" },
  { key: "cleanup", label: "自动清理", target: "system" }
];

/** "sing/chat" -> {page: "sing", tab: "chat"}. */
export function splitTarget(target) {
  const [page, tab = ""] = String(target).split("/");
  return { page, tab };
}

/** Summary line of an emotion card. GPT-SoVITS counts usable / all audios; Irodori shows whether a caption is set. */
export function emotionStatusText(engine, info) {
  if (engine === "irodori") return info.caption && String(info.caption).trim() ? "已设置 caption" : "未设置";
  if (!info.total) return info.override ? "无参考音频（有手动参数）" : "无参考音频";
  return `${info.usable} 段可用 / 共 ${info.total} 段`;
}

/** Emotion names for the cards: auto-detected folders first, then manual overrides that have no folder. */
export function emotionNames(discoveredNames, overrideNames) {
  const names = [];
  for (const name of [...discoveredNames, ...overrideNames]) if (!names.includes(name)) names.push(name);
  return names;
}


// ---------- G1: nickname, reply texts, wake prefix ----------

/** Hint on the character card: does AstrBot's wake prefix match the nickname? `prefixes` null = unknown (no hint). */
export function wakePrefixHint(prefixes, nickname) {
  if (!Array.isArray(prefixes) || !prefixes.length) return { status: "unknown", text: "" };
  if (prefixes.some((prefix) => prefix === nickname || prefix === `/${nickname}`)) return { status: "ok", text: "已一致" };
  const shown = prefixes.join("、");
  return {
    status: "mismatch",
    text: `AstrBot 的唤醒前缀是 ${shown}，和昵称不一致，命令要用 ${shown} 开头；要用昵称唤醒，请去 AstrBot 设置里改成 /${nickname}`
  };
}

/** The reply texts grouped for the card, in the server's order: [{group, items}]. */
export function textGroups(meta) {
  const groups = [];
  for (const item of meta) {
    let entry = groups.find((candidate) => candidate.group === item.group);
    if (!entry) { entry = { group: item.group, items: [] }; groups.push(entry); }
    entry.items.push(item);
  }
  return groups;
}

/** Form values for the texts: one line per sentence for the "lines" kind. */
export function textsToRaw(texts, meta) {
  const raw = {};
  for (const item of meta) raw[item.key] = item.kind === "lines" ? (texts[item.key] || []).join("\n") : String(texts[item.key] ?? "");
  return raw;
}

/** Body for POST /api/texts from the form values (trimmed; blank lines dropped). */
export function textsPayload(raw, meta) {
  const body = {};
  for (const item of meta) body[item.key] = item.kind === "lines" ? textLines(raw[item.key]) : String(raw[item.key] ?? "").trim();
  return body;
}

/** "可用占位符：{昵称}、{歌名}" for one text. */
export function placeholderText(item) {
  return `可用占位符：${item.placeholders.join("、")}`;
}

/** Placeholders in `text` that are not in `allowed` (each as written, braces included). */
export function unknownPlaceholders(text, allowed) {
  return [...String(text ?? "").matchAll(/\{[^{}]*\}/g)].map((match) => match[0]).filter((token) => !allowed.includes(token));
}

// ---------- G1b: ports ----------

export const NETWORK_FIELDS = { st_port: "ST 端口", bridge_port: "bridge 端口", console_port: "控制台端口", connector_port: "连接器端口", onebot_port: "NapCat 端口" };

/** Body for POST /api/network from the form values (blank or non-numeric become NaN so the server answers 400). */
export function networkPayload(raw) {
  const body = {};
  for (const name of Object.keys(NETWORK_FIELDS)) {
    const value = raw[name];
    body[name] = value === "" || value === null || value === undefined ? NaN : Number(value);
  }
  return body;
}

/** The notice after saving: the ports are read when the core starts, so the change waits for a restart (a plugin reload does it). */
export function networkSavedNotice(data) {
  return `端口已保存，重启核心（关掉 start.bat 的窗口再运行一次）后生效；控制台的新地址是 ${data.console_url}`;
}

/** Note under the port form: what runs now, and whether a saved change is still waiting for a reload. */
export function networkStatusText(data) {
  const use = data.in_use;
  const running = use ? `当前运行中：ST ${use.st_port} · bridge ${use.bridge_port} · 控制台 ${use.console_port} · 连接器 ${use.connector_port}${use.onebot_port ? ` · NapCat ${use.onebot_port}` : ""}。` : "";
  const waiting = data.reload_required ? `已保存的端口还没生效：重启核心（关掉 start.bat 的窗口再运行一次）后生效，控制台的新地址是 ${data.console_url}。` : "";
  return `${running}${waiting}`.trim();
}

/** Line at the top of the reply-texts card after an upgrade that had to replace texts ("" when nothing was replaced). */
export function textsNoticeText(notice) {
  const count = notice && Array.isArray(notice.replaced) ? notice.replaced.length : 0;
  if (!count) return "";
  return `升级时有 ${count} 条回复文案含有不支持的写法（未知的占位符或长度不对），已换成默认值，原文在 ${notice.backup || "config.json.bak-g1"}；在这里保存一次后此提示消失。`;
}


// ---------- R2b: owner, connector, log ----------

/** Body for POST /api/owner from the two form values (surrounding blanks do not count). */
export function ownerPayload(raw) {
  return { platform_id: String(raw.platform_id ?? "").trim(), sender_id: String(raw.sender_id ?? "").trim() };
}

/** What is wrong with the owner fields before they are sent ("" when fine): both set, or both empty. */
export function ownerProblem(raw) {
  const body = ownerPayload(raw);
  if (Boolean(body.platform_id) !== Boolean(body.sender_id)) return "平台 ID 和发送者 ID 要么都填，要么都留空";
  return "";
}

/** The overview line for the connector: who is connected, or that nobody is. */
export function connectorStatusText(status) {
  if (!status || !status.connected) return "插件未连接（未安装或 AstrBot 没开）";
  return `${status.name || "连接器"}${status.version ? ` ${status.version}` : ""} 已连接`;
}

// ---------- R3: chat platform (AstrBot or NapCat) ----------

export const PLATFORM_NAMES = { astrbot: "AstrBot", napcat: "NapCat" };

/** Group numbers typed into the whitelist box: separated by commas, spaces or new lines; duplicates dropped. */
export function parseGroupList(text) {
  const found = [];
  for (const item of String(text ?? "").split(/[\s,，、;；]+/)) {
    if (item && !found.includes(item)) found.push(item);
  }
  return found;
}

/** What is wrong with the platform fields before they are sent ("" when fine); keyed like the form fields. */
export function chatPlatformProblems(raw) {
  const problems = [];
  if (parseGroupList(raw.group_whitelist).some((item) => !/^[1-9][0-9]{0,19}$/.test(item))) {
    problems.push({ key: "group_whitelist", text: "群号只能是数字（用逗号、空格或换行分隔）" });
  } else if (parseGroupList(raw.group_whitelist).length > 200) {
    problems.push({ key: "group_whitelist", text: "最多 200 个群" });
  }
  const owner = String(raw.napcat_owner ?? "").trim();
  if (owner && !/^[1-9][0-9]{0,19}$/.test(owner)) problems.push({ key: "napcat_owner", text: "QQ 号只能是数字" });
  return problems;
}

/** Body for POST /api/chat-platform. */
export function chatPlatformPayload(raw) {
  return {
    active: raw.active === "napcat" ? "napcat" : "astrbot",
    group_whitelist: parseGroupList(raw.group_whitelist),
    napcat_owner: String(raw.napcat_owner ?? "").trim()
  };
}

/** NapCat's connection for the platform tab and the overview (the status object of GET /api/status). */
export function onebotStatusText(status) {
  if (status && status.port_unavailable) return `端口 ${status.port} 被占用，NapCat 不可用`;
  if (!status || !status.connected) return "未连接";
  const who = status.self_id ? `小号 ${status.self_id} ` : "";
  const beat = status.heartbeat_age === null || status.heartbeat_age === undefined ? "" : `，最近心跳 ${agoText(status.heartbeat_age)}`;
  return `${who}已连接${beat}`;
}

/** The overview line: the active platform and whether it is connected. */
export function platformOverviewText(platform, connector, onebot) {
  if (platform === "napcat") return `NapCat：${onebotStatusText(onebot)}`;
  const note = onebot && onebot.port_unavailable ? `（${onebotStatusText(onebot)}）` : "";
  return `AstrBot：${connectorStatusText(connector)}${note}`;
}

/** What the console shows for the NapCat token: "-" without one, a fixed-length mask unless shown. */
export const TOKEN_MASK = "••••••••";
export function tokenDisplay(token, shown) {
  if (!token) return "-";
  return shown ? String(token) : TOKEN_MASK;
}

/** Warning under the NapCat status when messages arrived in the wrong format ("" when fine). */
export function onebotFormatWarning(status) {
  return status && status.format_warning ? "收到的消息不是 array 格式，已忽略：请在 NapCat 的网络配置里把“消息格式”改成 array。" : "";
}

/** Note under the platform choice: what the choice means right now. */
export function platformChoiceNote(active) {
  return active === "napcat"
    ? "当前是 NapCat：只处理 NapCat 收到的消息（私聊只有主人，群聊只有白名单里的群）；AstrBot 来的消息会被静默忽略。"
    : "当前是 AstrBot：行为和以前一样；NapCat 即使连着，它的消息也会被忽略。";
}

export const LOG_LINE_CHOICES = [100, 300, 1000];

/** Text of the log view: the lines as they are, or a hint when there is nothing yet. */
export function logsText(data) {
  const lines = data && Array.isArray(data.lines) ? data.lines : [];
  return lines.length ? lines.join("\n") : "还没有日志（核心启动后才会有）。";
}

// ---------- 总预设 (whole-config presets) ----------

export const PRESET_NAME_MAX = 40;

/** Why a typed preset name is refused ("" when it is fine); the server checks the same rules. */
export function presetNameProblem(name) {
  const text = typeof name === "string" ? name.trim() : "";
  if (!text) return "请输入预设名称";
  if (text.length > PRESET_NAME_MAX) return `名称最长 ${PRESET_NAME_MAX} 个字`;
  if (/[\u0000-\u001f\u007f/\\]/.test(text) || text.includes("..")) return "名称不能含有 / \\ .. 或控制字符";
  return "";
}

/** The sidebar line: which preset is current, and whether the settings were changed since. */
export function presetLabel(listing) {
  if (!listing || !listing.current) return "未保存为预设";
  const name = listing.current_name || "（未命名）";
  return listing.modified ? `${name}（已修改）` : name;
}

/** The overview card's badge: saved / modified (warning colour) / not a preset. */
export function presetBadge(listing) {
  if (!listing || !listing.current) return { text: "未保存为预设", kind: "" };
  return listing.modified ? { text: "已修改", kind: "status-warning" } : { text: "已保存", kind: "status-success" };
}

/** One-line result for the toast and the overview card (the full text is on the system page). */
export function applyNotifyText(result, name) {
  const parts = [`已切换到「${name}」。`];
  if (result.archive?.name) parts.push(`切换前的设置已存档为「${result.archive.name}」。`);
  for (const note of result.notes || []) parts.push(`${note}。`.replace(/。。$/, "。"));
  for (const warning of result.warnings || []) parts.push(`⚠ ${warning}。`.replace(/。。$/, "。"));
  const missing = Array.isArray(result.missing_paths) ? result.missing_paths.length : 0;
  if (missing) parts.push(`有 ${missing} 个路径本机找不到，详见“系统”页的“总预设”卡片。`);
  return parts.join("");
}

/** "2026-11-04T12:00:01.123456" -> "2026-11-04 12:00" ("" for anything else). */
export function presetTime(iso) {
  const match = /^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2})/.exec(typeof iso === "string" ? iso : "");
  return match ? `${match[1]} ${match[2]}` : "";
}

/** The paths a preset mentions that this machine does not have, one per line. */
export function missingPathsText(list) {
  if (!Array.isArray(list) || !list.length) return "";
  return list.map((item) => `${item.field}：${item.path}`).join("\n");
}

/** Message after switching: what happened, the automatic archive, side-effect notes, warnings and missing paths. */
export function applyResultText(result, name) {
  const lines = [`已切换到「${name}」。`];
  if (result.archive?.name) lines.push(`切换前的设置已自动存档为「${result.archive.name}」。`);
  for (const note of result.notes || []) lines.push(note);
  for (const warning of result.warnings || []) lines.push(`⚠ ${warning}`);
  const missing = missingPathsText(result.missing_paths);
  if (missing) lines.push("本机找不到下面这些路径，相关功能可能不能用：", missing);
  return lines.join("\n");
}

/** Message after importing a preset file. */
export function importResultText(result) {
  const lines = [`已把「${result.preset?.name || ""}」加入预设列表（还没有应用，需要时在列表里点“切换”）。`];
  const missing = missingPathsText(result.missing_paths);
  lines.push(missing ? "本机找不到下面这些路径，切换后相关功能可能不能用：" : "预设里的路径在本机都能找到。");
  if (missing) lines.push(missing);
  return lines.join("\n");
}

// ---------- R4b: 安装与路径 ----------

/** The path fields of the page: request key, label. Order = order on the page. */
export const INSTALL_PATH_FIELDS = [
  { key: "st", field: "st_dir", label: "SillyTavern" },
  { key: "astrbot", field: "astrbot_dir", label: "AstrBot" },
  { key: "napcat", field: "napcat_path", label: "NapCat" },
  { key: "gpt_sovits", field: "gpt_sovits_dir", label: "GPT-SoVITS" },
  { key: "irodori", field: "irodori_dir", label: "Irodori" },
  { key: "irodori_server", field: "irodori_server_dir", label: "Irodori 服务端" },
  { key: "rvc", field: "rvc_dir", label: "RVC" }
];

/** One line for a path check: not set / valid / invalid and why. */
export function pathStatusText(check) {
  if (!check || check.state === "empty") return "未填（可以留空）";
  if (check.state === "valid") return `✓ 有效${check.message ? `：${check.message}` : ""}`;
  if (check.state === "choose") return check.message || "请选一个启动文件";
  return `✗ 无效：${check.message || "路径不可用"}`;
}

/** Body for POST /api/install/paths (and /check): every field as typed, trimmed. */
export function installPathsPayload(raw) {
  const body = {};
  for (const { field } of INSTALL_PATH_FIELDS) body[field] = String(raw[field] ?? "").trim();
  body.pip_mirror = String(raw.pip_mirror ?? "").trim();
  return body;
}

/** What is wrong with the typed values before they are sent ("" fields are fine): the mirror must be a web address. */
export function installPathsProblems(raw) {
  const problems = [];
  const mirror = String(raw.pip_mirror ?? "").trim();
  if (mirror && !/^https?:\/\/\S+$/i.test(mirror)) problems.push({ key: "pip_mirror", text: "镜像要写成 http(s):// 开头的地址（留空 = 官方源）" });
  for (const { field, label } of INSTALL_PATH_FIELDS) {
    if (/[\r\n\t]/.test(String(raw[field] ?? ""))) problems.push({ key: field, text: `${label} 路径里不能有换行` });
  }
  return problems;
}

/** Badge text + class for the plugin / extension status object. */
export function installStateBadge(status) {
  const table = {
    installed: ["已安装", "status-success"],
    outdated: ["版本不一致", "status-error"],
    unlinked: ["未指向当前数据目录", "status-error"],
    missing: ["未安装", ""],
    unknown: ["未设置路径", ""]
  };
  const [text, className] = table[status?.state] || ["-", ""];
  return { text, className };
}

/** Which buttons of the plugin / extension cards can be used. */
export function installButtons(status, packageInfo, kind, pathsDirty) {
  const available = Boolean(packageInfo && packageInfo[kind]);
  const known = Boolean(status) && status.state !== "unknown";
  const installed = known && status.state !== "missing";
  let reason = "";
  if (!available) reason = "开发环境，没有可安装的文件";
  else if (pathsDirty) reason = "路径有未保存的修改，请先保存";
  else if (!known) reason = kind === "plugin" ? "先填好 AstrBot 路径并保存" : "先填好 ST 路径并保存";
  return {
    installDisabled: Boolean(reason),
    uninstallDisabled: Boolean(reason) || !installed,
    installLabel: installed ? "重装" : "安装",
    reason
  };
}

/** The overview card: ST extension, AstrBot plugin and how many of the paths are filled in. */
export function installOverview(overview) {
  if (!overview) return { badge: "读取失败", badgeClass: "", extension: "-", plugin: "-", paths: "-" };
  const checks = overview.checks || {};
  const filled = INSTALL_PATH_FIELDS.filter(({ key }) => checks[key] && checks[key].state !== "empty").length;
  const bad = INSTALL_PATH_FIELDS.filter(({ key }) => checks[key] && checks[key].state === "invalid").length;
  const ext = installStateBadge(overview.extension).text;
  const plugin = installStateBadge(overview.plugin).text;
  const ok = overview.extension?.state === "installed" && (overview.plugin?.state === "installed" || overview.plugin?.state === "unknown");
  return {
    badge: bad ? "有路径无效" : ok ? "正常" : "待安装",
    badgeClass: bad ? "status-error" : ok ? "status-success" : "",
    extension: ext,
    plugin,
    paths: `已填 ${filled} / ${INSTALL_PATH_FIELDS.length}${bad ? `，${bad} 个无效` : ""}`
  };
}

/** Rows of the import preview: name, kind, size. */
export function importItemText(item) {
  const what = item.kind === "dir" ? `文件夹，${item.files} 个文件，${formatBytes(item.size)}` : formatBytes(item.size);
  return `${item.name}（${what}${item.exists ? "，会替换现有的" : ""}）`;
}

/** The result text of an import. */
export function importDoneText(result) {
  const lines = [`已导入 ${result.copied.length} 项。`];
  if (result.backup) lines.push(`被替换的 ${result.backed_up.length} 项已先备份到：${result.backup}`);
  if (result.rewritten_paths) lines.push(`配置里 ${result.rewritten_paths} 处指向旧数据目录的路径已改到新目录。`);
  if (result.kept_install) lines.push("这台电脑上的 ST / AstrBot / NapCat 路径已保留。");
  lines.push(result.restarting ? "导入完成，正在重启核心…" : "要让核心重新读取这些文件，请重启核心。");
  return lines.join("\n");
}

/** Status of a dependency row for the table. */
export function dependencyStateText(row) {
  if (row.state === "installed") return { text: "已安装", className: "status-success" };
  if (row.state === "missing") return { text: "未安装", className: "" };
  if (row.state === "blocked") return { text: row.needs || "不能安装", className: "" };
  return { text: "未检查", className: "" };
}

/** Text for the running / finished install job. */
export function jobStatusText(job) {
  if (!job) return "";
  if (job.state === "running") return "正在安装…";
  return job.message || (job.state === "success" ? "安装完成" : "安装失败");
}

/** Message after asking for a restart. */
export function restartNotice(result) {
  return result && result.auto ? "核心正在重启，页面会自动重新连接…" : "核心已退出。请重新运行 start.bat（这次不是由 start.bat 启动的，不会自动重来）。";
}

/** The path picker's file filter for launchers. */
export const LAUNCHER_HINT = "双击进入目录，选择 NapCat 的 .exe / .bat 启动文件。";


// ---------- R5: ST injection entries ----------

export const INJECTION_TARGETS = { preset: "ST 预设", worldbook: "世界书「STbridge」" };
export const INJECTION_PLACEMENTS = { after_chat_history: "Chat History 之后", depth: "按深度插入" };
export const INJECTION_ROLES = { system: "系统", user: "用户", assistant: "助手" };
export const INJECTION_MAX_ENTRIES = 30;
export const INJECTION_ORDER_STEP = 10;

/** A blank custom entry; its id is the first "custom<n>" nobody uses yet. */
export function newInjectionEntry(existing) {
  const used = new Set((existing || []).map((entry) => entry.id));
  let number = 1;
  while (used.has(`custom${number}`)) number += 1;
  return {
    id: `custom${number}`, builtin: false, title: "新条目", enabled: true, content: "",
    target: "preset", placement: "after_chat_history", depth: 0, role: "system", order: 0
  };
}

/**
 * The built-in entries that are missing from `entries`, put back in a copy: each one right after the closest
 * earlier built-in (in the default order) that is present, else in front. Returns {entries, restored} (titles).
 */
export function restoreBuiltinEntries(entries, defaults) {
  const result = [...(entries || [])];
  const restored = [];
  const present = new Set(result.map((entry) => entry.id));
  const wanted = (defaults || []).filter((entry) => entry.builtin);
  wanted.forEach((entry, position) => {
    if (present.has(entry.id)) return;
    let at = 0;
    for (let before = position - 1; before >= 0; before -= 1) {
      const found = result.findIndex((item) => item.id === wanted[before].id);
      if (found >= 0) { at = found + 1; break; }
    }
    result.splice(at, 0, { ...entry });
    present.add(entry.id);
    restored.push(entry.title);
  });
  return { entries: result, restored };
}

/** The request body for saving: the order of the list becomes the `order` field, numbers are numbers. */
export function injectionPayload(worldBookName, entries) {
  return {
    world_book_name: String(worldBookName ?? "").trim(),
    entries: (entries || []).map((entry, index) => ({
      id: entry.id,
      builtin: entry.builtin === true,
      title: String(entry.title ?? "").trim(),
      enabled: entry.enabled === true,
      content: String(entry.content ?? ""),
      target: entry.target,
      placement: entry.placement,
      depth: Number(entry.depth),
      role: entry.role,
      order: (index + 1) * INJECTION_ORDER_STEP
    }))
  };
}

/** One line under an entry's title: where it goes. */
export function injectionEntryNote(entry) {
  if (entry.target === "worldbook") return `世界书「STbridge」· 深度 ${entry.depth}`;
  return entry.placement === "depth" ? `ST 预设 · 深度 ${entry.depth}` : "ST 预设 · Chat History 之后";
}

// ---------- H1: health chain (QQ → core → ST → model → voice → covers) ----------

/** Why a request to ST ended without a reply, by the codes the core records (GET /api/status → replies). */
export const REPLY_FAILURE_TEXT = {
  StFailed: "ST 生成失败，可能是 API 欠费、限流或网络问题",
  Timeout: "ST 没有在时限内回复",
  EmptyReply: "ST 返回了空回复",
  InvalidReply: "回复格式不对，预设可能被改坏了"
};

/** A streak of failures older than this, with no request since, is shown as a warning instead of an error. */
export const REPLY_STALE_SECONDS = 30 * 60;

export function replyFailureText(code) {
  return REPLY_FAILURE_TEXT[code] || "原因不明";
}

function replySecondsText(milliseconds) {
  const seconds = Number(milliseconds) / 1000;
  return Number.isFinite(seconds) && seconds > 0 ? `${seconds >= 10 ? Math.round(seconds) : seconds.toFixed(1)} 秒` : "";
}

/**
 * How the model has been answering, from the `replies` object of GET /api/status:
 * {level: "off"|"ok"|"warn"|"error", text, reason}. Nothing yet: grey. The last 3 all failed: red.
 * Half or more of the last 10 failed: yellow. Otherwise green.
 */
export function replyHealth(replies) {
  const recent = replies && Array.isArray(replies.recent) ? replies.recent : [];
  if (!recent.length) return { level: "off", text: "还没有请求", reason: "" };
  const failed = (code) => code !== "ok";
  const last10 = recent.slice(-10);
  const failures = last10.filter(failed).length;
  const reason = failures ? replyFailureText(replies.last_fail_code) : "";
  const ago = failures && replies.last_fail_age !== null && replies.last_fail_age !== undefined ? `，最近一次 ${agoText(replies.last_fail_age)}` : "";
  const average = replySecondsText(replies.avg_ms);
  const last3 = recent.slice(-3);
  if (last3.length === 3 && last3.every(failed)) {
    const age = Number(replies.last_fail_age);
    if (Number.isFinite(age) && age > REPLY_STALE_SECONDS) {
      // Nothing has been asked since: the red may be out of date (the cause may be fixed), so only a warning.
      const minutes = Math.round(age / 60);
      return { level: "warn", text: `约 ${minutes} 分钟前连续失败，之后还没有新请求：${reason}`, reason: `模型约 ${minutes} 分钟前连续失败，之后还没有新请求：${reason}` };
    }
    return { level: "error", text: `连续 3 次没拿到回复：${reason}${ago}`, reason: `模型连续 3 次没回出来：${reason}` };
  }
  if (failures * 2 >= last10.length) {
    return { level: "warn", text: `最近 ${last10.length} 次里 ${failures} 次失败：${reason}${ago}`, reason: `模型回复不太稳定：${reason}` };
  }
  const base = failures ? `最近 ${last10.length} 次里 ${failures} 次失败` : `最近 ${last10.length} 次都正常`;
  return { level: "ok", text: average ? `${base} · 平均 ${average}` : base, reason: "" };
}

function chainNode(id, label, goto, level, text, reason = "") {
  return { id, label, goto, level, text, reason: reason || text, detail: "" };
}

/** The QQ side: the connector (AstrBot) or OneBot (NapCat) the active platform needs. */
function qqNode(status) {
  const make = (level, text, reason) => chainNode("qq", "QQ 平台", "system/platform", level, text, reason);
  if (!status) return make("off", "未知");
  if (status.platform === "napcat") {
    const info = status.onebot;
    if (info && info.port_unavailable) return make("error", `端口 ${info.port} 被占用`, `NapCat 的端口 ${info.port} 被占用`);
    if (info && info.connected) return make("ok", "NapCat 已连接");
    return make("error", "NapCat 未连接", "NapCat 没连上");
  }
  if (status.connector && status.connector.connected) return make("ok", "AstrBot 已连接");
  return make("error", "AstrBot 插件未连接", "AstrBot 插件没连上");
}

function voiceNode(voice) {
  const make = (level, text, reason) => chainNode("voice", "语音", "voice", level, text, reason);
  const settings = voice && voice.settings;
  const common = settings && settings.common;
  if (common && Number(common.prob) === 0) return make("off", "已关闭（发送概率 0）");
  const service = voice && voice.service;
  if (!service) return make("off", "未知");
  const engine = ENGINE_LABELS[service.engine] || ENGINE_LABELS[settings && settings.engine] || "语音服务";
  const code = service.state;
  if (code === "failed") return make("warn", `${engine} 启动失败`, `${engine} 语音服务启动失败`);
  if (code === "running" || code === "external") return make("ok", `${engine} 运行中`);
  if (code === "starting") return make("ok", `${engine} 启动中`);
  if (common && common.auto_start === false) return make("warn", `${engine} 没开，也不会自动启动`, `${engine} 语音服务没开，也不会自动启动`);
  return make("ok", `${engine} 用到时自动启动`);
}

function coverNode(cover) {
  const make = (level, text, reason) => chainNode("cover", "翻唱", "sing/service", level, text, reason);
  if (!cover || cover.enabled === undefined || cover.enabled === null) return make("off", "未知");
  if (!cover.enabled) return make("off", "未启用");
  const code = cover.service && cover.service.state;
  if (code === "failed") return make("warn", "RVC 服务启动失败", "翻唱用的 RVC 服务启动失败");
  if (code === "running" || code === "external") return make("ok", "RVC 运行中");
  if (code === "starting") return make("ok", "RVC 启动中");
  return make("ok", "RVC 待命（用到时自动启动）");
}

function stNode(status) {
  const make = (level, text, reason) => chainNode("st", "ST 扩展", "install", level, text, reason);
  const st = stStatus(status ? status.bridge : null);
  if (!status || st.code === "unknown") return make("off", "未知");
  if (st.code === "online" || st.code === "generating") return make("ok", st.text);
  if (st.code === "never") return make("error", st.text, "ST 扩展还没连上过，请打开装了扩展的 ST 页面");
  if (st.code === "stopped") return make("error", st.text, "桥接服务没有运行");
  return make("error", st.text, `ST 扩展${st.text}`);
}

/**
 * The six links, in the order a message travels: [{id, label, level: ok|warn|error|off, text, reason, goto}].
 * `input`: {status (GET /api/status or null), failures (polls that failed in a row), voice: {settings, service},
 * cover: {enabled, service}}. A core that cannot be reached greys out everything behind it.
 */
export function healthChain(input) {
  const { status = null, failures = 0, voice = null, cover = null } = input || {};
  if (failures >= 2) {
    const dark = (id, label, goto) => chainNode(id, label, goto, "off", "—");
    return [
      dark("qq", "QQ 平台", "system/platform"),
      chainNode("core", "核心", "system/logs", "error", "未运行", "核心未运行：双击 start.bat"),
      dark("st", "ST 扩展", "install"),
      dark("reply", "模型回复", "system/logs"),
      dark("voice", "语音", "voice"),
      dark("cover", "翻唱", "sing/service")
    ];
  }
  const core = status ? chainNode("core", "核心", "system/logs", "ok", "运行中") : chainNode("core", "核心", "system/logs", "off", "检查中…");
  const reply = status ? replyHealth(status.replies) : { level: "off", text: "未知", reason: "" };
  const nodes = [
    qqNode(status),
    core,
    stNode(status),
    chainNode("reply", "模型回复", "system/logs", reply.level, reply.text, reply.reason),
    voiceNode(voice),
    coverNode(cover)
  ];
  const details = nodeDetails(status, voice, cover);
  for (const node of nodes) node.detail = details[node.id] || "";
  return nodes;
}

/** The facts the old overview cards showed, now the hover text of the chain nodes. */
function nodeDetails(status, voice, cover) {
  const details = {};
  if (status) {
    details.qq = platformOverviewText(status.platform, status.connector, status.onebot);
    const bridge = status.bridge;
    if (bridge) {
      const poll = bridge.last_poll_age !== null && bridge.last_poll_age !== undefined ? ` · 最近一次轮询：${agoText(bridge.last_poll_age)}` : "";
      details.st = `桥接：${bridgeGateText(bridge.gate)}${poll}`;
    }
  }
  const service = voice && voice.service;
  if (service) {
    const engine = ENGINE_LABELS[service.engine] || ENGINE_LABELS[voice.settings && voice.settings.engine] || "-";
    details.voice = `引擎：${engine} · 服务：${service.service_name || "-"} · ${serviceStateText(service.state || "stopped")}`;
  }
  const rvc = cover && cover.service;
  if (rvc && rvc.state) details.cover = `RVC 服务：${serviceStateText(rvc.state)}`;
  return details;
}

/** The line at the top: what is wrong first, otherwise how many things need a look. `name` is the character's nickname. */
export function healthHeadline(chain, name = DEFAULT_NICKNAME) {
  const nodes = Array.isArray(chain) ? chain : [];
  const broken = nodes.find((node) => node.level === "error");
  if (broken) return `${name}现在回不了消息：${broken.reason || broken.text}`;
  const warnings = nodes.filter((node) => node.level === "warn").length;
  if (warnings) return `${name}在线 · 有 ${warnings} 项需要注意`;
  return `${name}在线 · 一切正常`;
}

/** The banner on every page: {level: "error"|"warn", text, goto, link} for the first problem, or null when all is well. */
export function healthBanner(chain, name = DEFAULT_NICKNAME) {
  const nodes = Array.isArray(chain) ? chain : [];
  const problem = nodes.find((node) => node.level === "error") || nodes.find((node) => node.level === "warn");
  if (!problem) return null;
  return { level: problem.level, text: healthHeadline(nodes, name), goto: problem.goto, link: `去看看（${problem.label}）` };
}

/** The tab title: a warning sign in front while something blocks replies. */
export function healthTitle(base, chain) {
  const down = Array.isArray(chain) && chain.some((node) => node.level === "error");
  return down ? `⚠ ${base}` : base;
}

// ---------- H2: overview (today's numbers, install fold, feature chips) ----------

const DASH = "-";

/** The first character of the name for the round avatar (no picture files). */
export function avatarLetter(name) {
  const first = Array.from(String(name ?? "").trim())[0];
  return first || "·";
}

const CHAT_SUB_TEXT = {
  Disabled: "已关闭",
  NoOwner: "还没有主人私聊",
  Running: "正在发送",
  UnansweredLimit: "等你回复后继续",
  DailyLimit: "今天已发满",
  Retry: "稍后重试",
  QuietHours: "安静时段",
  OutsideWindow: "不在作息时段",
  Busy: "ST 正在忙",
  Conversing: "正在聊天",
  Gap: "离上一条太近",
  T1Priority: "定时计划优先",
  Ready: "马上发送"
};

/** "HH:MM" when the time is on `today` (YYYY-MM-DD), "明天 HH:MM" the day after, otherwise "MM-DD HH:MM". */
export function nextTimeText(iso, today) {
  const text = formatDateTime(iso);
  if (!text) return "";
  const [date, clock] = text.split(" ");
  if (!clock) return "";
  if (!today || date === today) return clock;
  const next = new Date(`${today}T00:00:00`);
  if (!Number.isNaN(next.getTime())) {
    next.setDate(next.getDate() + 1);
    const tomorrow = `${next.getFullYear()}-${String(next.getMonth() + 1).padStart(2, "0")}-${String(next.getDate()).padStart(2, "0")}`;
    if (date === tomorrow) return `明天 ${clock}`;
  }
  return `${date.slice(5)} ${clock}`;
}

function chatCard(chat, date) {
  if (!chat || typeof chat !== "object") return { value: DASH, sub: DASH };
  const used = Number.isFinite(Number(chat.today)) ? chat.today : DASH;
  const limit = Number.isFinite(Number(chat.daily_limit)) ? chat.daily_limit : DASH;
  let sub = CHAT_SUB_TEXT[chat.reason];
  if (chat.reason === "Waiting") {
    const when = nextTimeText(chat.next_at, date);
    sub = when ? `下次约 ${when}` : "等待中";
  }
  return { value: `${used} / ${limit}`, sub: sub || String(chat.reason || DASH) };
}

function moodCard(mood) {
  if (!mood || typeof mood !== "object") return { value: DASH, sub: DASH };
  const overall = mood.global;
  const owner = mood.owner;
  if (!overall && !owner) return { value: DASH, sub: "还没有记录" };
  const parts = [];
  if (overall) parts.push(overall.calm ? "已平复" : "还没平复");
  parts.push(owner ? `对主人：${owner.emotion}${owner.calm ? "（已平复）" : ""}` : "对主人：-");
  return { value: overall ? overall.emotion : DASH, sub: parts.join(" · ") };
}

/**
 * The five "today" cards of the overview, from the `today` object of GET /api/status (null: all dashes):
 * [{id, label, value, sub, goto}] in the order 回复, 主动搭话, 唱歌, 心情, 排队.
 */
export function todayCards(today) {
  const data = today && typeof today === "object" ? today : {};
  const number = (value) => (Number.isFinite(Number(value)) && value !== null ? String(value) : DASH);
  const replies = data.replies;
  const songs = data.songs;
  const queue = data.queue;
  const chat = chatCard(data.chat, data.date);
  const mood = moodCard(data.mood);
  return [
    { id: "replies", label: "回复", value: replies ? number(replies.total) : DASH, sub: replies ? `私聊 ${number(replies.private)} · 群聊 ${number(replies.group)}` : DASH, goto: "system/logs" },
    { id: "chat", label: "主动搭话", value: chat.value, sub: chat.sub, goto: "schedule" },
    { id: "songs", label: "唱歌", value: songs ? number(songs.total) : DASH, sub: songs ? `主人 ${number(songs.owner)} 首` : DASH, goto: "sing/chat" },
    { id: "mood", label: "心情", value: mood.value, sub: mood.sub, goto: "emotions" },
    { id: "queue", label: "排队", value: queue ? number(queue.waiting) : DASH, sub: queue ? (queue.active ? "正在处理 1 条" : "空闲") : DASH, goto: "system/general" }
  ];
}

/**
 * The install card: everything fine collapses to one line, anything else opens by itself with a warning colour.
 * -> {summary, level: "ok"|"warn"|"error"|"off", open}
 */
export function installFold(overview) {
  const info = installOverview(overview);
  if (!overview) return { summary: "安装状态读取失败", level: "off", open: false };
  if (info.badge === "正常") return { summary: "安装正常 ✓", level: "ok", open: false };
  if (info.badge === "有路径无效") return { summary: "有路径无效，需要处理", level: "error", open: true };
  return { summary: "还没装好，需要处理", level: "warn", open: true };
}

/** The small switches row: [{key, label, goto, state: "on"|"off"|"unknown"}] (display only). */
export function featureChips(flags) {
  return FEATURE_LINKS.map((link) => {
    const on = flags ? flags[link.key] : undefined;
    return { key: link.key, label: link.label, goto: link.target, state: on === undefined ? "unknown" : on ? "on" : "off" };
  });
}
