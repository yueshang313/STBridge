import { test } from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { pathToFileURL } from "node:url";

// logic.js is a browser module without a package.json: load a copy as .mjs.
const source = fs.readFileSync(new URL("../js/logic.js", import.meta.url), "utf8");
const directory = fs.mkdtempSync(path.join(os.tmpdir(), "console-logic-"));
const file = path.join(directory, "logic.mjs");
fs.writeFileSync(file, source);
const L = await import(pathToFileURL(file).href);

test("form values are coerced per field type", () => {
  const common = L.coerceSection("common", {
    auto_start: true, stop_on_exit: false, start_timeout: "180", tts_timeout_minutes: "5",
    prob: "99.5", text_limit: "0", cooldown: "3", send_text_with_audio: true, ignored: "x"
  });
  assert.deepEqual(common, {
    auto_start: true, stop_on_exit: false, start_timeout: 180, tts_timeout_minutes: 5,
    prob: 99.5, text_limit: 0, cooldown: 3, send_text_with_audio: true
  });
  const iro = L.coerceSection("irodori", { host: "127.0.0.1", port: "8088", num_steps: "0", cfg_scale_caption: "4", lora_adapter: "" });
  assert.deepEqual(iro, { host: "127.0.0.1", port: 8088, num_steps: 0, cfg_scale_caption: 4, lora_adapter: "" });
  assert.ok(Number.isNaN(L.coerceSection("irodori", { port: "" }).port));
});

test("the split-message fields are coerced to bool / float / int", () => {
  const common = L.coerceSection("common", { split_messages: true, split_interval: "2.5", split_max: "4", split_extra: "x" });
  assert.deepEqual(common, { split_messages: true, split_interval: 2.5, split_max: 4 });
  const empty = L.coerceSection("common", { split_messages: false, split_interval: "", split_max: "" });
  assert.equal(empty.split_messages, false);
  assert.ok(Number.isNaN(empty.split_interval) && Number.isNaN(empty.split_max));
  assert.ok(L.TTS_FIELDS.common.some(([name, type]) => name === "split_interval" && type === "float"));
});

test("a settings patch changes only the named parts and does not mutate the input", () => {
  const settings = {
    engine: "irodori",
    common: { prob: 100, cooldown: 0 },
    irodori: { host: "127.0.0.1", port: 8088 },
    gpt_sovits: { host: "127.0.0.1", port: 9880 }
  };
  const before = JSON.stringify(settings);
  const next = L.applyTtsPatch(settings, { engine: "gpt_sovits", common: { prob: 50 } });
  assert.equal(next.engine, "gpt_sovits");
  assert.deepEqual(next.common, { prob: 50, cooldown: 0 });
  assert.deepEqual(next.irodori, settings.irodori);
  assert.deepEqual(next.gpt_sovits, settings.gpt_sovits);
  assert.equal(JSON.stringify(settings), before);
  assert.equal(L.applyTtsPatch(settings, { irodori: { port: 9000 } }).irodori.host, "127.0.0.1");
});

test("the stopped-service notice names the engines", () => {
  assert.equal(L.stoppedNotice([]), "");
  assert.equal(L.stoppedNotice(undefined), "");
  assert.equal(L.stoppedNotice(["irodori"]), "已关闭插件启动的 Irodori 服务");
  assert.equal(L.stoppedNotice(["gpt_sovits"]), "已关闭插件启动的 GPT-SoVITS 服务");
});

test("service buttons follow the state and stay disabled while busy", () => {
  assert.deepEqual(L.serviceButtons("stopped", false), { startDisabled: false, stopDisabled: true });
  assert.deepEqual(L.serviceButtons("failed", false), { startDisabled: false, stopDisabled: true });
  assert.deepEqual(L.serviceButtons("running", false), { startDisabled: true, stopDisabled: false });
  assert.deepEqual(L.serviceButtons("starting", false), { startDisabled: true, stopDisabled: false });
  assert.deepEqual(L.serviceButtons("external", false), { startDisabled: true, stopDisabled: true });
  assert.deepEqual(L.serviceButtons("stopped", true), { startDisabled: true, stopDisabled: true });
  assert.equal(L.serviceStateText("external"), "运行中（外部启动，插件不管理）");
});

const FULL_PROFILE = {
  ref_audio_path: "D:\\ref\\a.wav", prompt_text: "テキスト", prompt_text_lang: "日文", text_lang: "日文",
  top_k: 15, top_p: 0.9, temperature: 0.8, speed_factor: 1.25, split_sentence: false,
  caption: "既存の caption", emoji: "😊"
};

test("saving in Irodori mode keeps every GPT-SoVITS field", () => {
  const payload = L.buildEmotionPayload(FULL_PROFILE, "irodori", { caption: "新しい", emoji: "😠" });
  assert.equal(payload.caption, "新しい");
  assert.equal(payload.emoji, "😠");
  for (const key of ["ref_audio_path", "prompt_text", "prompt_text_lang", "text_lang", "top_k", "top_p", "temperature", "speed_factor", "split_sentence"]) {
    assert.deepEqual(payload[key], FULL_PROFILE[key], key);
  }
  // clearing the visible fields clears them, nothing else
  const cleared = L.buildEmotionPayload(FULL_PROFILE, "irodori", { caption: "", emoji: "" });
  assert.equal(cleared.caption, "");
  assert.equal(cleared.ref_audio_path, FULL_PROFILE.ref_audio_path);
});

test("saving in GPT-SoVITS mode keeps caption and emoji", () => {
  const payload = L.buildEmotionPayload(FULL_PROFILE, "gpt_sovits", {
    ref_audio_path: "D:\\ref\\b.wav", prompt_text: "別", prompt_text_lang: "日文", text_lang: "日文",
    top_k: "20", top_p: "1", temperature: "1", speed_factor: "1.1", split_sentence: true
  });
  assert.equal(payload.caption, "既存の caption");
  assert.equal(payload.emoji, "😊");
  assert.equal(payload.ref_audio_path, "D:\\ref\\b.wav");
  assert.equal(payload.top_k, 20);
  assert.equal(payload.speed_factor, 1.1);
  assert.equal(payload.split_sentence, true);
});

test("saving an emotion never reads fields the form did not show", () => {
  // A hidden field value that leaked into the raw values must be ignored.
  const payload = L.buildEmotionPayload(FULL_PROFILE, "irodori", { caption: "x", emoji: "y", ref_audio_path: "HACK", top_k: "1" });
  assert.equal(payload.ref_audio_path, FULL_PROFILE.ref_audio_path);
  assert.equal(payload.top_k, 15);
  const missing = L.buildEmotionPayload(undefined, "irodori", { caption: "c", emoji: "" });
  assert.deepEqual(missing, { caption: "c", emoji: "" });
});

test("Irodori presets list emotions with a caption or an emoji, in config order", () => {
  const manual = {
    无感情: { caption: "普段", emoji: "" },
    认真: { caption: "", emoji: "" },
    开心: { caption: "", emoji: "😊" },
    伤心: { caption: "  ", emoji: "🥺", ref_audio_path: "" }
  };
  const preset = L.buildPresetPrompt("irodori", [{ name: "目录情绪", available: 3 }], manual);
  assert.deepEqual(preset.names, ["无感情", "开心", "伤心"]);
  assert.equal(preset.text, "每次回复开头输出 [情绪:xxx]，xxx 只能从以下选择：无感情、开心、伤心");
  assert.equal(L.buildPresetPrompt("irodori", [], {}).text, "");
});

test("GPT-SoVITS presets list usable folder emotions then manual reference audios", () => {
  const discovered = [{ name: "伤心", available: 0 }, { name: "开心", available: 3 }, { name: "认真", available: 6 }];
  const manual = { 开心: { ref_audio_path: "D:\\a.wav", caption: "x" }, 害羞: { ref_audio_path: "D:\\b.wav" }, 生气: { ref_audio_path: "", caption: "只有风格" } };
  const preset = L.buildPresetPrompt("gpt_sovits", discovered, manual);
  assert.deepEqual(preset.names, ["开心", "认真", "害羞"]);
  assert.ok(preset.text.endsWith("开心、认真、害羞"));
  assert.equal(L.buildPresetPrompt("gpt_sovits", [], {}).text, "");
});

test("the backend usable flag decides what counts as usable", () => {
  assert.equal(L.audioIsUsable({ usable: false, out_of_range: false, readable: true }), false);
  assert.equal(L.audioIsUsable({ usable: true, out_of_range: true }), true);
  assert.equal(L.audioIsUsable({ out_of_range: true }), false);
  assert.equal(L.audioIsUsable({ readable: false }), false);
  assert.equal(L.audioIsUsable({}), true);
  const groups = L.groupAudiosByEmotion([
    { emotion: "开心", usable: true }, { emotion: "开心", usable: false }, { emotion: "开心", usable: false }
  ]);
  assert.equal(L.groupTitle(groups[0]), "开心 · 可用 1 / 共 3 段");
});

test("reference audio is grouped by emotion with usable and total counts", () => {
  const audios = [
    { emotion: "开心", prompt_text: "a", out_of_range: false },
    { emotion: "开心", prompt_text: "b", out_of_range: true },
    { emotion: "认真", prompt_text: "c", readable: false },
    { emotion: "认真", prompt_text: "d" },
    { emotion: "认真", prompt_text: "e" },
    { prompt_text: "f" }
  ];
  const groups = L.groupAudiosByEmotion(audios);
  assert.deepEqual(groups.map((group) => [group.emotion, group.usable, group.total]), [
    ["default", 1, 1], ["开心", 1, 2], ["认真", 2, 3]
  ]);
  assert.equal(L.groupTitle(groups[1]), "开心 · 可用 1 / 共 2 段");
  assert.equal(groups[2].audios.length, 3);
  assert.deepEqual(L.groupAudiosByEmotion([]), []);
  // deleting one entry and regrouping updates the counts
  const after = L.groupAudiosByEmotion(audios.filter((audio) => audio.prompt_text !== "b"));
  assert.equal(L.groupTitle(after.find((group) => group.emotion === "开心")), "开心 · 可用 1 / 共 1 段");
});

test("sticker settings payload keeps blanks invalid", () => {
  assert.deepEqual(L.stickerSettingsPayload({ enabled: true, prob: "30", cooldown: "60" }), { enabled: true, prob: 30, cooldown: 60 });
  const blank = L.stickerSettingsPayload({ enabled: false, prob: "", cooldown: null });
  assert.equal(blank.enabled, false);
  assert.ok(Number.isNaN(blank.prob) && Number.isNaN(blank.cooldown));
});

test("sticker group title, sizes and upload summary", () => {
  assert.equal(L.stickerGroupTitle({ name: "开心", count: 3 }), "开心 · 3 张");
  assert.equal(L.formatBytes(10), "1 KB");
  assert.equal(L.formatBytes(2048), "2 KB");
  assert.equal(L.formatBytes(3 * 1024 * 1024), "3.0 MB");
  assert.equal(L.uploadSummary({ saved: ["a.png", "b.gif"], rejected: [] }), "已上传 2 张");
  assert.equal(
    L.uploadSummary({ saved: ["a.png"], rejected: [{ filename: "x.bin", error: "unsupported image format" }] }),
    "已上传 1 张；1 张未收：x.bin（unsupported image format）"
  );
});

// ---- scheduled messages ----

test("weekday text: none, all, runs and lists", () => {
  assert.equal(L.weekdaysText([]), "");
  assert.equal(L.weekdaysText([0, 1, 2, 3, 4, 5, 6]), "");
  assert.equal(L.weekdaysText([0, 1, 2, 3, 4]), "周一至周五");
  assert.equal(L.weekdaysText([4, 0, 3, 1, 2]), "周一至周五");
  assert.equal(L.weekdaysText([5, 6]), "周六、周日");
  assert.equal(L.weekdaysText([0, 2, 4]), "周一、周三、周五");
  assert.equal(L.weekdaysText([1, 2, 3]), "周二至周四");
  assert.deepEqual(L.WEEKDAY_LABELS, ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]);
});

test("plan summaries", () => {
  const daily = { kind: "daily", time: "07:30", jitter_minutes: 10, weekdays: [0, 1, 2, 3, 4] };
  assert.equal(L.planSummary(daily), "每天 07:30 ±10 分钟 · 周一至周五");
  assert.equal(L.planSummary({ ...daily, jitter_minutes: 0, weekdays: [] }), "每天 07:30");
  assert.equal(L.planSummary({ kind: "interval", start: "10:00", end: "20:00", every_minutes: 120 }), "10:00–20:00 每 120 分钟");
});

test("target labels and times", () => {
  const groups = [{ key: "abcd1234", alias: "制作人的群" }];
  assert.equal(L.targetLabel({ type: "private" }, groups), "私聊（主人）");
  assert.equal(L.targetLabel({ type: "group", key: "abcd1234" }, groups), "制作人的群");
  assert.equal(L.targetLabel({ type: "group", key: "ffff0000" }, groups), "群 ffff0000（未记录）");
  assert.equal(L.formatDateTime("2026-11-03T07:30:12"), "2026-11-03 07:30");
  assert.equal(L.formatDateTime(""), "");
  assert.equal(L.nextFireText({ enabled: false, next_fire: "2026-11-03T07:30" }), "未启用");
  assert.equal(L.nextFireText({ enabled: true, next_fire: null }), "暂无");
  assert.equal(L.nextFireText({ enabled: true, next_fire: "2026-11-03T07:30" }), "2026-11-03 07:30");
});

test("a saved plan goes back without the server-computed fields", () => {
  const plan = { id: "0a1b2c3d", name: "早安", enabled: false, next_fire: "x", running: true, kind: "daily" };
  assert.deepEqual(L.planPayload(plan, { enabled: true }), { id: "0a1b2c3d", name: "早安", enabled: true, kind: "daily" });
});

test("plan to form and back keeps the plan", () => {
  const plan = {
    id: "0a1b2c3d", name: "早安", enabled: true, kind: "daily", time: "07:30", weekdays: [4, 0], jitter_minutes: 10,
    target: { type: "group", key: "abcd1234" }, prompt: "道早安", fallback_texts: ["一", "二"]
  };
  const form = L.planToForm(plan);
  assert.equal(form.target, "group:abcd1234");
  assert.equal(form.fallbackText, "一\n二");
  assert.deepEqual(L.buildPlanBody(form), { ...plan, weekdays: [0, 4] });
  const interval = {
    id: "1a2b3c4d", name: "喝水", enabled: false, kind: "interval", start: "10:00", end: "20:00", every_minutes: 120,
    target: { type: "private" }, prompt: "喝水", fallback_texts: []
  };
  assert.deepEqual(L.buildPlanBody(L.planToForm(interval)), interval);
});

test("a new plan has no id and trims text", () => {
  const form = { ...L.emptyPlanForm(), name: "  新计划 ", prompt: " 说点什么 ", fallbackText: " 一 \n\n 二 \r\n" };
  const body = L.buildPlanBody(form);
  assert.equal("id" in body, false);
  assert.equal(body.name, "新计划");
  assert.equal(body.prompt, "说点什么");
  assert.deepEqual(body.fallback_texts, ["一", "二"]);
  assert.deepEqual(body.target, { type: "private" });
  assert.equal(body.jitter_minutes, 0);
});

test("form validation catches the usual mistakes", () => {
  const ok = { ...L.emptyPlanForm(), name: "x", prompt: "y" };
  assert.deepEqual(L.validatePlanForm(ok), []);
  const cases = [
    [{ name: " " }, "名称"],
    [{ name: "x".repeat(41) }, "名称"],
    [{ prompt: "" }, "要求"],
    [{ prompt: "y".repeat(301) }, "要求"],
    [{ time: "7:30" }, "时刻"],
    [{ jitter: "61" }, "浮动"],
    [{ jitter: "1.5" }, "浮动"],
    [{ fallbackText: "a\nb\nc\nd\ne\nf" }, "最多 5 句"],
    [{ fallbackText: "x".repeat(201) }, "备用文字"],
    [{ kind: "interval", start: "20:00", end: "10:00" }, "早于结束"],
    [{ kind: "interval", start: "10:00", end: "10:00" }, "早于结束"],
    [{ kind: "interval", every: "14" }, "间隔"],
    [{ kind: "interval", every: "721" }, "间隔"],
    [{ kind: "interval", start: "", end: "20:00" }, "HH:MM"]
  ];
  for (const [patch, word] of cases) {
    const errors = L.validatePlanForm({ ...ok, ...patch });
    assert.ok(errors.some((error) => error.includes(word)), `${JSON.stringify(patch)} -> ${errors}`);
  }
  assert.deepEqual(L.validatePlanForm({ ...ok, kind: "interval" }), []);
  assert.deepEqual(L.validatePlanForm({ ...ok, jitter: "" }), []);
});

test("result texts are Chinese", () => {
  assert.equal(L.resultText("sent"), "已发送");
  assert.equal(L.resultText("fallback"), "已发备用文字");
  assert.equal(L.resultText("skip:NoOwnerSession"), "跳过（还没记录主人私聊，请先用主人账号私聊琴音一次）");
  assert.equal(L.resultText("skip:NoOwnerSession", "小夜"), "跳过（还没记录主人私聊，请先用主人账号私聊小夜一次）");
  assert.equal(L.resultText("skip:Probability"), "跳过（概率未命中）");
  assert.equal(L.resultText("skip:Whatever"), "跳过（Whatever）");
  assert.equal(L.resultText("failed:RuntimeError"), "失败（RuntimeError）");
  assert.equal(L.resultText(undefined), "-");
});

test("test result lines include timings and the group reminder", () => {
  const result = { result: "sent", voice: "skip:Cooldown", sticker: "failed:OtherError", st_ms: 12345, total_ms: 20500 };
  assert.deepEqual(L.testResultLines(result, false), [
    "结果：已发送", "语音：跳过（冷却中）", "表情包：失败（OtherError）", "ST 耗时：12.3 秒 · 总耗时：20.5 秒"
  ]);
  const group = L.testResultLines(result, true);
  assert.equal(group.length, 5);
  assert.equal(group[4], L.GROUP_SEND_NOTE);
  assert.match(L.GROUP_SEND_NOTE, /QQ 拦截/);
  assert.match(L.GROUP_SEND_NOTE, /以群里实际收到为准/);
});

// ---- song covers ----

test("clock formatting and parsing", () => {
  assert.equal(L.formatClock(0), "0:00");
  assert.equal(L.formatClock(65), "1:05");
  assert.equal(L.formatClock(185.4), "3:05");
  assert.equal(L.formatClock(3723), "1:02:03");
  assert.equal(L.formatClock(-1), "-");
  assert.equal(L.formatClock(undefined), "-");
  assert.equal(L.parseClock("1:05"), 65);
  assert.equal(L.parseClock("0:30"), 30);
  assert.equal(L.parseClock("75"), 75);
  assert.equal(L.parseClock("1:02:03"), 3723);
  assert.equal(L.parseClock("1:30.5"), 90.5);
  assert.equal(L.parseClock("  "), null);
  assert.equal(L.parseClock(""), null);
  for (const bad of ["abc", "1:xx", "1:2:3:4", "-5", ":30"]) assert.ok(Number.isNaN(L.parseClock(bad)), bad);
});

test("song label and index text", () => {
  assert.equal(L.songLabel({ title: "歌", duration: 200 }), "歌 · 3:20");
  assert.equal(L.indexText({ index_path: "E:/x.index" }, "added.index"), "手动指定");
  assert.equal(L.indexText({ index_path: "" }, "added.index"), "自动匹配：added.index");
  assert.match(L.indexText({ index_path: "" }, ""), /没有找到匹配/);
});

test("cover errors are Chinese and unknown kinds stay visible", () => {
  assert.equal(L.coverErrorText("QueueFull"), "队列已满（最多 1 首在处理 + 3 首排队），请稍后再试");
  assert.match(L.coverErrorText("Timeout"), /超时/);
  assert.match(L.coverErrorText("SeparationFailed"), /人声分离/);
  assert.equal(L.coverErrorText("Weird"), "失败（Weird）");
});

test("timing text and cache hits", () => {
  assert.equal(
    L.timingText({ cached: false, timings: { sep_ms: 12000, vc_ms: 30500, mix_ms: 800, total_ms: 43300 } }),
    "去伴奏 12.0 秒 · 声音转换 30.5 秒 · 混音 0.8 秒 · 合计 43.3 秒"
  );
  assert.equal(L.timingText({ cached: true, timings: {} }), "命中缓存，没有重新生成");
  assert.match(L.timingText({ cached: false }), /0.0 秒/);
  assert.match(
    L.timingText({ cached: false, timings: { sep_ms: 1000, dereverb_ms: 2500, vc_ms: 0, mix_ms: 0, total_ms: 0 } }),
    /^去伴奏 1.0 秒 · 去混响 2.5 秒 · 声音转换/
  );
});

test("cover settings payload sends the output sample rate as a number", () => {
  assert.equal(L.coverSettingsPayload({ output_sample_rate: 24000 }, { output_sample_rate: "44100" }).output_sample_rate, 44100);
  assert.equal(L.coverSettingsPayload({}, { output_sample_rate: "24000" }).output_sample_rate, 24000);
});

test("cover settings payload converts numbers and trims text", () => {
  const base = { rvc_root: "D:/RVC", model: "a.pth", enabled: true, port: 8099 };
  const body = L.coverSettingsPayload(base, {
    enabled: false, rvc_root: " E:/RVC ", port: "8100", model: "b.pth", f0_up_key: "-3", index_rate: "0.5",
    clip_seconds: "", index_path: "", f0_method: "rmvpe"
  });
  assert.equal(body.enabled, false);
  assert.equal(body.rvc_root, "E:/RVC");
  assert.equal(body.port, 8100);
  assert.equal(body.f0_up_key, -3);
  assert.equal(body.index_rate, 0.5);
  assert.ok(Number.isNaN(body.clip_seconds));
  assert.equal(body.index_path, "");
  assert.equal(body.model, "b.pth");
  assert.deepEqual(base, { rvc_root: "D:/RVC", model: "a.pth", enabled: true, port: 8099 });
});

test("online source helpers: sources parsing, labels and key status", () => {
  assert.deepEqual(L.parseSources(" wy, kg，qq "), { sources: ["wy", "kg", "qq"], error: "" });
  assert.match(L.parseSources("").error, /至少/);
  assert.match(L.parseSources("wy,xx").error, /xx/);
  assert.match(L.parseSources("wy kg wy").error, /重复/);
  assert.equal(L.songName({ title: "春日影", artist: "MyGO" }), "春日影 - MyGO");
  assert.equal(L.songName({ title: "春日影" }), "春日影");
  assert.equal(L.sourceLabel("wy"), "网易云");
  assert.equal(L.keyStatusText({ key_set: false }), "未设置");
  assert.equal(L.keyStatusText({ key_set: true, key_tail: "ab12" }), "已设置 …ab12");
  assert.equal(L.keyStatusText({ key_set: true, key_tail: "" }), "已设置");
  assert.equal(L.validateOnlineName("  "), "请输入歌名");
  assert.equal(L.validateOnlineName("x".repeat(201)), "歌名太长");
  assert.equal(L.validateOnlineName("春日影"), "");
});

test("online error texts and per-source error lines", () => {
  assert.match(L.coverErrorText("not_found"), /没有找到/);
  assert.match(L.coverErrorText("preview_only"), /试听/);
  assert.match(L.coverErrorText("MusicKeyNotSet"), /密钥/);
  assert.equal(L.coverErrorText("HTTP404"), "来源返回 HTTP404");
  assert.match(L.coverErrorText("network:ClientError"), /网络/);
  assert.deepEqual(L.searchErrorLines([{ source: "kg", error: "api_failed" }]), ["酷狗：来源返回失败（没有结果或需要权限）"]);
  assert.deepEqual(L.searchErrorLines(undefined), []);
});

test("cover settings payload carries sources and the key fields, never the display-only ones", () => {
  const base = { sources: ["wy"], key_set: true, key_tail: "ab12", port: 8099 };
  const body = L.coverSettingsPayload(base, {
    music_api_key: " newkey ", clear_music_api_key: false, sources: ["kg", "qq"], min_full_seconds: "60", download_timeout_seconds: ""
  });
  assert.equal(body.music_api_key, "newkey");
  assert.equal(body.clear_music_api_key, false);
  assert.deepEqual(body.sources, ["kg", "qq"]);
  assert.equal(body.min_full_seconds, 60);
  assert.ok(Number.isNaN(body.download_timeout_seconds));
  assert.ok(!("key_set" in body) && !("key_tail" in body));
  assert.deepEqual(base.sources, ["wy"]);
  assert.equal(L.coverSettingsPayload({}, { clear_music_api_key: true }).clear_music_api_key, true);
});

test("cleanup helpers: byte text, keep-days validation, settings payload", () => {
  assert.equal(L.bytesText(0), "0 B");
  assert.equal(L.bytesText(undefined), "0 B");
  assert.equal(L.bytesText(900), "900 B");
  assert.equal(L.bytesText(2048), "2 KB");
  assert.equal(L.bytesText(72 * 1024 * 1024), "72.0 MB");
  assert.equal(L.bytesText(3 * 1024 * 1024 * 1024), "3.00 GB");
  for (const good of ["1", "3", " 365 ", 30]) assert.equal(L.validateKeepDays(good), "");
  for (const bad of ["", "0", "366", "1.5", "-2", "abc", null]) assert.match(L.validateKeepDays(bad), /1～365/);
  assert.deepEqual(L.cleanupSettingsPayload(true, " 7 "), { enabled: true, keep_days: 7 });
  assert.deepEqual(L.cleanupSettingsPayload(0, 3), { enabled: false, keep_days: 3 });
});

test("cleanup result and last-run texts", () => {
  assert.equal(L.cleanupResultText({ removed: 12, bytes: 5 * 1024 * 1024, skipped: 0 }), "已清理 12 个文件，释放 5.0 MB");
  assert.match(L.cleanupResultText({ removed: 1, bytes: 10, skipped: 2 }), /2 个文件被占用，已跳过/);
  assert.equal(L.lastAutoText({}), "还没有自动清理过");
  assert.equal(L.lastAutoText(undefined), "还没有自动清理过");
  const text = L.lastAutoText({ time: 1700000000, removed: 3, bytes: 2048, skipped: 0 });
  assert.match(text, /^\d{4}-\d\d-\d\d \d\d:\d\d · 已清理 3 个文件，释放 2 KB$/);
  assert.equal(Object.keys(L.CLEANUP_TARGETS).join(","), "astrbot_temp,bridge_temp,cover_temp,cover_stems");
});

test("song request helpers: text lines and the settings payload", () => {
  assert.deepEqual(L.textLines("  好 \r\n\n来了《{歌名}》\n   \n"), ["好", "来了《{歌名}》"]);
  assert.deepEqual(L.textLines(""), []);
  assert.deepEqual(L.textLines(null), []);
  const body = L.songRequestPayload({
    enabled: 1, delivery_mode: "proactive", daily_limit: " 3 ", limit_owner: 1, permission_variable: "  点歌权限 ", owner_min_affinity: "0", group_min_affinity: " -10 ", refuse_percent: "25",
    list_timeout_seconds: "180", per_source: "5",
    accept_texts: "好\n来", limit_text: "ignored"
  });
  assert.deepEqual(body, {
    enabled: true, delivery_mode: "proactive", daily_limit: 3, limit_owner: true, permission_variable: "点歌权限", owner_min_affinity: 0, group_min_affinity: -10, refuse_percent: 25,
    list_timeout_seconds: 180, per_source: 5
  });
});

test("song request payload leaves blank numbers invalid for the server", () => {
  const body = L.songRequestPayload({ enabled: false, owner_min_affinity: "", group_min_affinity: null });
  assert.equal(body.enabled, false);
  assert.equal(body.delivery_mode, "passive");
  assert.equal(body.limit_owner, false);
  assert.ok(Number.isNaN(body.daily_limit));
  assert.ok(Number.isNaN(body.owner_min_affinity));
  assert.ok(Number.isNaN(body.group_min_affinity));
  assert.ok(Number.isNaN(body.per_source));
  assert.equal(body.permission_variable, "");
  assert.ok(!("accept_texts" in body) && !("limit_text" in body));
});

test("cover settings payload: separation models, vocal reverb and keep-stems", () => {
  const body = L.coverSettingsPayload({}, {
    sep_model: " vocals-bs-roformer-368 ", dereverb_model: "", vocal_reverb: "35", keep_stems: true, f0_method: "fcpe"
  });
  assert.equal(body.sep_model, "vocals-bs-roformer-368");
  assert.equal(body.dereverb_model, "");
  assert.equal(body.vocal_reverb, 35);
  assert.equal(body.keep_stems, true);
  assert.equal(L.coverSettingsPayload({}, { keep_stems: false }).keep_stems, false);
  assert.ok(Number.isNaN(L.coverSettingsPayload({}, { vocal_reverb: "" }).vocal_reverb));
  assert.ok(!("filter_radius" in L.coverSettingsPayload({}, { f0_method: "rmvpe" })));
  assert.match(L.coverErrorText("SepModelNotFound"), /pymss_weights/);
});

test("pitch presets: keyword splitting, payload rows, moving and the new error text", () => {
  assert.deepEqual(L.splitKeywords("男声, 男生、男歌手；  高一点 "), ["男声", "男生", "男歌手", "高一点"]);
  assert.deepEqual(L.splitKeywords(""), []);
  assert.deepEqual(L.splitKeywords(undefined), []);
  const payload = L.pitchPresetsPayload([
    { name: " 男声歌 ", vocal: "12", inst: "0", keywords: "男声、男生" },
    { name: "x", vocal: "", inst: "-2", keywords: ["a", " b ", ""] }
  ]);
  assert.deepEqual(payload[0], { name: "男声歌", vocal: 12, inst: 0, keywords: ["男声", "男生"] });
  assert.ok(Number.isNaN(payload[1].vocal));
  assert.deepEqual(payload[1].keywords, ["a", "b"]);
  assert.deepEqual(L.pitchPresetsPayload(undefined), []);
  const rows = [{ name: "a" }, { name: "b" }, { name: "c" }];
  assert.deepEqual(L.movePreset(rows, 1, -1).map((r) => r.name), ["b", "a", "c"]);
  assert.deepEqual(L.movePreset(rows, 1, 1).map((r) => r.name), ["a", "c", "b"]);
  assert.deepEqual(L.movePreset(rows, 0, -1).map((r) => r.name), ["a", "b", "c"]);
  assert.deepEqual(L.movePreset(rows, 2, 1).map((r) => r.name), ["a", "b", "c"]);
  assert.equal(rows[0].name, "a");
  assert.match(L.coverErrorText("PitchShiftUnavailable"), /pedalboard/);
  assert.equal(L.pedalboardInstallCommand(""), "请先设置 RVC 目录");
  assert.equal(L.pedalboardInstallCommand(undefined), "请先设置 RVC 目录");
  assert.equal(L.pedalboardInstallCommand("  "), "请先设置 RVC 目录");
  assert.equal(
    L.pedalboardInstallCommand("E:\\Tools\\My RVC\\"),
    '"E:\\Tools\\My RVC\\runtime\\python.exe" -m pip install --no-deps pedalboard -i https://mirrors.pku.edu.cn/pypi/simple',
  );
  assert.ok(!/D:\\RVC/.test(L.pedalboardInstallCommand("")));
  assert.equal(L.PITCH_PRESET_LIMIT, 10);
});

test("cover settings payload carries the overall pitch and the presets", () => {
  const presets = [{ name: "a", vocal: 1, inst: 2, keywords: ["k"] }];
  const body = L.coverSettingsPayload({}, { inst_pitch: "-3", pitch_presets: presets });
  assert.equal(body.inst_pitch, -3);
  assert.deepEqual(body.pitch_presets, presets);
  assert.notEqual(body.pitch_presets[0].keywords, presets[0].keywords);
  assert.ok(Number.isNaN(L.coverSettingsPayload({}, { inst_pitch: "" }).inst_pitch));
  assert.match(L.timingText({ cached: false, timings: { sep_ms: 1000, pitch_ms: 1500 } }), /伴奏变调 1.5 秒/);
});

test("diffFields lists changed, added and removed fields; changing back clears them", () => {
  const snapshot = { a: "1", b: true, c: "x" };
  assert.deepEqual(L.diffFields(snapshot, { a: "1", b: true, c: "x" }), []);
  assert.deepEqual(L.diffFields(snapshot, { a: "2", b: true, c: "x" }), ["a"]);
  assert.deepEqual(L.diffFields(snapshot, { a: "2", b: false, c: "x" }).sort(), ["a", "b"]);
  assert.deepEqual(L.diffFields(snapshot, { a: "1", b: true }), ["c"]);
  assert.deepEqual(L.diffFields(snapshot, { a: "1", b: true, c: "x", d: "new" }), ["d"]);
  assert.deepEqual(L.diffFields({}, {}), []);
  assert.deepEqual(L.diffFields(null, { a: "1" }), ["a"]);
  // typed "2" then back to "1": no longer a change
  const edited = { ...snapshot, a: "2" };
  edited.a = "1";
  assert.deepEqual(L.diffFields(snapshot, edited), []);
  assert.equal(L.unsavedText(3), "有 3 处未保存的修改");
});

test("mergeSnapshot keeps typed values of existing rows and starts new rows clean", () => {
  const oldSnapshot = { "song:1:title": "A", "song:2:title": "B" };
  const before = { "song:1:title": "A edited", "song:2:title": "B" };
  const fresh = { "song:1:title": "A", "song:2:title": "B", "song:3:title": "C" };
  const merged = L.mergeSnapshot(oldSnapshot, before, fresh);
  assert.deepEqual(merged.values, { "song:1:title": "A edited", "song:2:title": "B", "song:3:title": "C" });
  assert.deepEqual(merged.snapshot, fresh);
  assert.deepEqual(L.diffFields(merged.snapshot, merged.values), ["song:1:title"]);
  // a removed row is dropped from both
  const gone = L.mergeSnapshot(oldSnapshot, before, { "song:2:title": "B" });
  assert.deepEqual(gone.values, { "song:2:title": "B" });
  assert.deepEqual(gone.snapshot, { "song:2:title": "B" });
});

test("mergeCoverPatch lays only the changed parts over the server settings", () => {
  const base = {
    enabled: true, rvc_root: "D:/RVC", port: 8099, model: "a.pth", f0_up_key: 0, index_rate: 0.5,
    sources: ["wy"], pitch_presets: [{ name: "旧", vocal: 0, inst: 0, keywords: ["旧"] }], key_set: true, key_tail: "ab12"
  };
  // only the presets changed: every other field stays what the server has
  const presets = [{ name: "男声", vocal: -12, inst: 0, keywords: ["男声"] }];
  const onlyPresets = L.mergeCoverPatch(base, [{ pitch_presets: presets }]);
  assert.deepEqual(onlyPresets.pitch_presets, presets);
  assert.equal(onlyPresets.rvc_root, "D:/RVC");
  assert.equal(onlyPresets.port, 8099);
  assert.deepEqual(onlyPresets.sources, ["wy"]);
  assert.ok(!("music_api_key" in onlyPresets) && !("key_set" in onlyPresets) && !("key_tail" in onlyPresets));
  assert.deepEqual(base.pitch_presets, [{ name: "旧", vocal: 0, inst: 0, keywords: ["旧"] }]);
  // only the service part
  const onlyService = L.mergeCoverPatch(base, [{ enabled: false, rvc_root: " E:/RVC ", port: "8100" }]);
  assert.equal(onlyService.enabled, false);
  assert.equal(onlyService.rvc_root, "E:/RVC");
  assert.equal(onlyService.port, 8100);
  assert.equal(onlyService.model, "a.pth");
  assert.deepEqual(onlyService.pitch_presets, base.pitch_presets);
  // key left blank keeps the saved one; the clear box clears it
  const keep = L.mergeCoverPatch(base, [{ music_api_key: "", clear_music_api_key: false, sources: ["kg"] }]);
  assert.equal(keep.music_api_key, "");
  assert.equal(keep.clear_music_api_key, false);
  assert.deepEqual(keep.sources, ["kg"]);
  const clear = L.mergeCoverPatch(base, [{ music_api_key: "", clear_music_api_key: true }]);
  assert.equal(clear.clear_music_api_key, true);
  // several parts at once
  const all = L.mergeCoverPatch(base, [{ port: "8101" }, { f0_up_key: "-3" }, { pitch_presets: presets }]);
  assert.equal(all.port, 8101);
  assert.equal(all.f0_up_key, -3);
  assert.deepEqual(all.pitch_presets, presets);
  assert.deepEqual(L.mergeCoverPatch(base, []).pitch_presets, base.pitch_presets);
});

test("runSaves runs in order, stops at the first failure and reports what was done", async () => {
  const calls = [];
  const ok = (id, notice) => ({ id, label: id, run: async () => { calls.push(id); return notice; } });
  const bad = (id) => ({ id, label: `部分${id}`, run: async () => { calls.push(id); throw new Error("boom"); } });

  const all = await L.runSaves([ok("a", "已存 a"), ok("b"), ok("c", "已存 c")]);
  assert.deepEqual(all.done, ["a", "b", "c"]);
  assert.equal(all.failed, null);
  assert.deepEqual(all.pending, []);
  assert.deepEqual(all.notices, ["已存 a", "已存 c"]);

  calls.length = 0;
  const stopped = await L.runSaves([ok("a"), bad("b"), ok("c"), ok("d")]);
  assert.deepEqual(calls, ["a", "b"]);
  assert.deepEqual(stopped.done, ["a"]);
  assert.equal(stopped.failed.id, "b");
  assert.equal(stopped.failed.label, "部分b");
  assert.equal(stopped.failed.error.message, "boom");
  assert.deepEqual(stopped.pending, ["c", "d"]);
  assert.equal(L.saveFailureText(stopped.failed), "部分b保存失败：boom");
  assert.equal(L.saveFailureText({ label: "x", error: "text" }), "x保存失败：text");

  const first = await L.runSaves([bad("a"), ok("b")]);
  assert.deepEqual(first.done, []);
  assert.deepEqual(first.pending, ["b"]);

  const empty = await L.runSaves([]);
  assert.deepEqual(empty, { done: [], notices: [], failed: null, pending: [] });
});

// ---- U2: pages, tabs, status, paging ----

test("the sidebar always shows the same eight pages, in order, whatever the engine", () => {
  assert.deepEqual(L.visiblePages(), ["overview", "voice", "emotions", "stickers", "schedule", "sing", "affinity", "injection", "install", "system"]);
  assert.deepEqual(L.visiblePages("irodori"), L.visiblePages("gpt_sovits"));
  for (const page of L.visiblePages()) assert.equal(L.resolvePage(page), page);
  assert.equal(L.resolvePage("nope"), "overview");
  assert.equal(L.resolvePage(""), "overview");
});

test("old hashes map to the new pages", () => {
  const expected = {
    dashboard: "overview", irodori: "voice", models: "voice", characters: "emotions", audios: "emotions",
    cover: "sing", cleanup: "system", stickers: "stickers", schedule: "schedule", affinity: "affinity"
  };
  for (const [old, page] of Object.entries(expected)) assert.equal(L.resolvePage(L.pageFromHash(`#${old}`)), page, old);
  assert.equal(L.resolvePage(L.pageFromHash("#cleanup")), "system");
  assert.equal(L.pageFromHash("#cover/songs"), "cover");
  assert.equal(L.pageFromHash(""), "");
  assert.equal(L.pageFromHash(undefined), "");
});

test("tab names come from the hash and only the sing page has tabs", () => {
  assert.equal(L.tabFromHash("#sing/params"), "params");
  assert.equal(L.tabFromHash("#sing"), "");
  assert.equal(L.tabFromHash(""), "");
  assert.equal(L.resolveTab("sing", "chat"), "chat");
  assert.equal(L.resolveTab("sing", "nope"), "library");
  assert.equal(L.resolveTab("sing", ""), "library");
  assert.equal(L.resolveTab("voice", "chat"), "");
  assert.deepEqual(L.SING_TABS, ["library", "params", "chat", "service"]);
  assert.equal(L.hashFor("sing", "chat"), "#sing/chat");
  assert.equal(L.hashFor("voice", ""), "#voice");
});

test("ST status: generating, online, offline, never connected, unknown", () => {
  assert.deepEqual(L.stStatus({ http_running: true, active: true, last_poll_age: 300 }), { code: "generating", text: "正在生成" });
  assert.deepEqual(L.stStatus({ http_running: true, active: false, last_poll_age: 1.2 }), { code: "online", text: "在线" });
  assert.equal(L.stStatus({ http_running: true, active: false, last_poll_age: 10 }).code, "online");
  assert.deepEqual(L.stStatus({ http_running: true, active: false, last_poll_age: 10.1 }), { code: "offline", text: "离线（最近一次 10 秒前）" });
  assert.equal(L.stStatus({ http_running: true, active: false, last_poll_age: 125 }).text, "离线（最近一次 2 分钟前）");
  assert.equal(L.stStatus({ http_running: true, active: false, last_poll_age: 7300 }).text, "离线（最近一次 2 小时前）");
  assert.deepEqual(L.stStatus({ http_running: true, active: false, last_poll_age: null }), { code: "never", text: "从未连接" });
  assert.deepEqual(L.stStatus(null), { code: "unknown", text: "未知" });
  assert.deepEqual(L.stStatus(undefined), { code: "unknown", text: "未知" });
  assert.equal(L.stStatus({ http_running: false, active: false, last_poll_age: null }).code, "stopped");
  assert.equal(L.agoText(59.6), "1 分钟前");
  assert.equal(L.agoText(0), "0 秒前");
});

test("bridge gate text", () => {
  assert.equal(L.bridgeGateText("RECOVERED"), "已就绪");
  assert.equal(L.bridgeGateText("RECOVERY_REQUIRED"), "等待 ST 就绪");
  assert.equal(L.bridgeGateText("STOPPING"), "正在停止");
  assert.equal(L.bridgeGateText("STOPPED"), "已停止");
  assert.equal(L.bridgeGateText(null), "未运行");
  assert.equal(L.bridgeGateText("weird"), "未知");
});

test("pageSlice pulls an out-of-range page back", () => {
  assert.deepEqual(L.pageSlice(45, 1, 20), { start: 0, end: 20, pages: 3, page: 1 });
  assert.deepEqual(L.pageSlice(45, 3, 20), { start: 40, end: 45, pages: 3, page: 3 });
  assert.deepEqual(L.pageSlice(45, 9, 20), { start: 40, end: 45, pages: 3, page: 3 });
  assert.deepEqual(L.pageSlice(45, 0, 20), { start: 0, end: 20, pages: 3, page: 1 });
  assert.deepEqual(L.pageSlice(45, -4, 20), { start: 0, end: 20, pages: 3, page: 1 });
  assert.deepEqual(L.pageSlice(45, "2", 20), { start: 20, end: 40, pages: 3, page: 2 });
  assert.deepEqual(L.pageSlice(0, 1, 20), { start: 0, end: 0, pages: 1, page: 1 });
  assert.deepEqual(L.pageSlice(20, 2, 20), { start: 0, end: 20, pages: 1, page: 1 });
  assert.deepEqual(L.pageSlice(24, 1, 24), { start: 0, end: 24, pages: 1, page: 1 });
  assert.deepEqual(L.pageSlice(25, 2, 24), { start: 24, end: 25, pages: 2, page: 2 });
});

test("search matches every word, case-insensitively", () => {
  assert.equal(L.matchesQuery("春日影 MyGO", ""), true);
  assert.equal(L.matchesQuery("春日影 MyGO", "  "), true);
  assert.equal(L.matchesQuery("春日影 MyGO", "mygo"), true);
  assert.equal(L.matchesQuery("春日影 MyGO", "春日 go"), true);
  assert.equal(L.matchesQuery("春日影 MyGO", "春日 ave"), false);
  assert.equal(L.matchesQuery(undefined, "x"), false);
});

test("layoutRows pages only the rows that match the search", () => {
  const matches = Array.from({ length: 45 }, (_, index) => index % 2 === 0); // 23 rows match
  const first = L.layoutRows(matches, 1, 20);
  assert.equal(first.slice.pages, 2);
  assert.equal(first.shown.filter(Boolean).length, 20);
  assert.equal(first.shown[0], true);
  assert.equal(first.shown[1], false);
  const second = L.layoutRows(matches, 2, 20);
  assert.equal(second.shown.filter(Boolean).length, 3);
  assert.deepEqual(second.matched.slice(20), [40, 42, 44]);
  // the current page is gone after rows disappear: pulled back
  const fewer = L.layoutRows(matches.slice(0, 10), 2, 20);
  assert.equal(fewer.slice.page, 1);
  assert.equal(L.layoutRows([], 3, 20).slice.page, 1);
});

test("dirtyPages and pageOfRow tell which pages hold edited rows", () => {
  const matched = Array.from({ length: 45 }, (_, index) => index);
  assert.deepEqual(L.dirtyPages(matched, [0, 25, 44], 20), [1, 2, 3]);
  assert.deepEqual(L.dirtyPages(matched, [], 20), []);
  assert.deepEqual(L.dirtyPages([5, 9, 30], [30, 99], 2), [2]); // row 30 is the third match -> page 2; 99 is filtered out
  assert.equal(L.pageOfRow(matched, 44, 20), 3);
  assert.equal(L.pageOfRow([3, 4], 9, 20), 0);
});

test("affinity users sort by recent update, score or name", () => {
  const users = [
    { id: "a", nickname: "乙", value: 10, updated_at: "2026-11-01T10:00:00" },
    { id: "b", nickname: "甲", value: 50, updated_at: "2026-11-03T10:00:00" },
    { id: "c", nickname: "", value: 50, updated_at: null }
  ];
  assert.deepEqual(L.sortUserOrder(users, "updated"), [1, 0, 2]);
  assert.deepEqual(L.sortUserOrder(users, "high"), [1, 2, 0]);
  assert.deepEqual(L.sortUserOrder(users, "low"), [0, 1, 2]);
  assert.deepEqual(L.sortUserOrder(users, "name").length, 3);
  assert.deepEqual(L.sortUserOrder(users, "unknown"), [0, 1, 2]);
  assert.deepEqual(L.sortUserOrder([], "high"), []);
  assert.equal(L.AFFINITY_SORTS[0][0], "updated");
});

test("dirty tabs and collapsed-card storage parsing", () => {
  assert.deepEqual([...L.dirtyTabs([{ tab: "library", count: 2 }, { tab: "chat", count: 0 }, { tab: "", count: 3 }, { count: 1 }])], ["library"]);
  assert.deepEqual(L.parseCollapsed('{"a":true,"b":false,"c":"x"}'), { a: true, b: false });
  assert.deepEqual(L.parseCollapsed("not json"), {});
  assert.deepEqual(L.parseCollapsed(null), {});
  assert.deepEqual(L.parseCollapsed("[1]"), {});
});

test("feature links point at a page and a tab", () => {
  assert.deepEqual(L.splitTarget("sing/chat"), { page: "sing", tab: "chat" });
  assert.deepEqual(L.splitTarget("stickers"), { page: "stickers", tab: "" });
  for (const link of L.FEATURE_LINKS) assert.ok(L.visiblePages().includes(L.splitTarget(link.target).page), link.key);
});

test("emotion cards: names merged, status text per engine", () => {
  assert.deepEqual(L.emotionNames(["happy", "sad"], ["sad", "angry"]), ["happy", "sad", "angry"]);
  assert.deepEqual(L.emotionNames([], []), []);
  assert.equal(L.emotionStatusText("irodori", { caption: "x" }), "已设置 caption");
  assert.equal(L.emotionStatusText("irodori", { caption: "  " }), "未设置");
  assert.equal(L.emotionStatusText("irodori", {}), "未设置");
  assert.equal(L.emotionStatusText("gpt_sovits", { usable: 3, total: 4 }), "3 段可用 / 共 4 段");
  assert.equal(L.emotionStatusText("gpt_sovits", { total: 0, override: true }), "无参考音频（有手动参数）");
  assert.equal(L.emotionStatusText("gpt_sovits", { total: 0 }), "无参考音频");
});

// ---- G1: nickname, reply texts, wake prefix ----

test("wake prefix hint: consistent, mismatching and unknown", () => {
  assert.deepEqual(L.wakePrefixHint(["/琴音"], "琴音"), { status: "ok", text: "已一致" });
  assert.equal(L.wakePrefixHint(["!", "琴音"], "琴音").status, "ok");
  const mismatch = L.wakePrefixHint(["/"], "琴音");
  assert.equal(mismatch.status, "mismatch");
  assert.match(mismatch.text, /唤醒前缀是 \/，和昵称不一致，命令要用 \/ 开头/);
  assert.match(mismatch.text, /改成 \/琴音/);
  assert.match(L.wakePrefixHint(["/", "!"], "小夜").text, /是 \/、!，/);
  assert.deepEqual(L.wakePrefixHint(null, "琴音"), { status: "unknown", text: "" });
  assert.deepEqual(L.wakePrefixHint([], "琴音"), { status: "unknown", text: "" });
  assert.equal(L.wakePrefixHint(["/"], "琴音").status, "mismatch");
});

test("reply texts: grouping, form values and the request body", () => {
  const meta = [
    { key: "queue_full", kind: "text", group: "排队与出错", label: "满了", placeholders: ["{昵称}"] },
    { key: "song_usage", kind: "text", group: "点歌", label: "用法", placeholders: ["{昵称}"] },
    { key: "accept_texts", kind: "lines", group: "点歌", label: "答应", placeholders: ["{昵称}", "{歌名}"] }
  ];
  assert.deepEqual(L.textGroups(meta).map((entry) => [entry.group, entry.items.map((item) => item.key)]), [["排队与出错", ["queue_full"]], ["点歌", ["song_usage", "accept_texts"]]]);
  const texts = { queue_full: "找{昵称}的人太多", song_usage: "用法", accept_texts: ["好", "来《{歌名}》"] };
  const raw = L.textsToRaw(texts, meta);
  assert.deepEqual(raw, { queue_full: "找{昵称}的人太多", song_usage: "用法", accept_texts: "好\n来《{歌名}》" });
  assert.deepEqual(L.textsPayload(raw, meta), texts);
  assert.deepEqual(L.textsPayload({ queue_full: "  x  ", song_usage: "", accept_texts: " a \n\n b \n" }, meta), { queue_full: "x", song_usage: "", accept_texts: ["a", "b"] });
  assert.equal(L.placeholderText(meta[2]), "可用占位符：{昵称}、{歌名}");
});

test("unknown placeholders are found in a text", () => {
  assert.deepEqual(L.unknownPlaceholders("找{昵称}唱《{歌名}》", ["{昵称}", "{歌名}"]), []);
  assert.deepEqual(L.unknownPlaceholders("找{昵名}，{}，{次数}", ["{昵称}"]), ["{昵名}", "{}", "{次数}"]);
  assert.deepEqual(L.unknownPlaceholders("没有占位符", ["{昵称}"]), []);
  assert.deepEqual(L.unknownPlaceholders(null, []), []);
});

test("network form: payload, saved notice and status text", () => {
  assert.deepEqual(L.networkPayload({ st_port: "11451", bridge_port: " 12001 ", console_port: 11453, connector_port: "11454", onebot_port: "11455" }), { st_port: 11451, bridge_port: 12001, console_port: 11453, connector_port: 11454, onebot_port: 11455 });
  const blank = L.networkPayload({ st_port: "", bridge_port: null });
  assert.ok(Number.isNaN(blank.st_port) && Number.isNaN(blank.bridge_port) && Number.isNaN(blank.console_port) && Number.isNaN(blank.connector_port) && Number.isNaN(blank.onebot_port));
  assert.equal(L.networkSavedNotice({ console_url: "http://127.0.0.1:12002" }), "端口已保存，重启核心（关掉 start.bat 的窗口再运行一次）后生效；控制台的新地址是 http://127.0.0.1:12002");
  const same = { in_use: { st_port: 11451, bridge_port: 11452, console_port: 11453, connector_port: 11454 }, reload_required: false, console_url: "http://127.0.0.1:11453" };
  assert.equal(L.networkStatusText(same), "当前运行中：ST 11451 · bridge 11452 · 控制台 11453 · 连接器 11454。");
  assert.match(L.networkStatusText({ ...same, reload_required: true, console_url: "http://127.0.0.1:12002" }), /重启核心（关掉 start\.bat 的窗口再运行一次）后生效，控制台的新地址是 http:\/\/127\.0\.0\.1:12002/);
  assert.equal(L.networkStatusText({ in_use: null, reload_required: false, console_url: "x" }), "");
});

test("upgrade notice for replaced reply texts", () => {
  assert.equal(L.textsNoticeText(null), "");
  assert.equal(L.textsNoticeText({ replaced: [] }), "");
  assert.equal(L.textsNoticeText(undefined), "");
  const text = L.textsNoticeText({ replaced: ["refuse_texts", "limit_text"], backup: "config.json.bak-g1" });
  assert.match(text, /升级时有 2 条回复文案含有不支持的写法/);
  assert.match(text, /已换成默认值，原文在 config\.json\.bak-g1/);
  assert.match(L.textsNoticeText({ replaced: ["x"] }), /config\.json\.bak-g1/);
});

test("preset names are checked like the server does", () => {
  assert.equal(L.presetNameProblem("日常"), "");
  assert.equal(L.presetNameProblem("  日常  "), "");
  assert.equal(L.presetNameProblem("x".repeat(40)), "");
  for (const bad of ["", "   ", null, undefined, "x".repeat(41), "a/b", "a\\b", "../x", "a..b", "tab\there"]) {
    assert.notEqual(L.presetNameProblem(bad), "", String(bad));
  }
});

test("the sidebar label names the current preset and marks a modified one", () => {
  assert.equal(L.presetLabel(null), "未保存为预设");
  assert.equal(L.presetLabel({ current: null }), "未保存为预设");
  assert.equal(L.presetLabel({ current: "p1", current_name: "日常", modified: false }), "日常");
  assert.equal(L.presetLabel({ current: "p1", current_name: "日常", modified: true }), "日常（已修改）");
  assert.equal(L.presetLabel({ current: "p1", current_name: null, modified: null }), "（未命名）");
});

test("the overview badge shows saved, modified or not a preset", () => {
  assert.deepEqual(L.presetBadge(null), { text: "未保存为预设", kind: "" });
  assert.deepEqual(L.presetBadge({ current: null }), { text: "未保存为预设", kind: "" });
  assert.deepEqual(L.presetBadge({ current: "p1", modified: false }), { text: "已保存", kind: "status-success" });
  assert.deepEqual(L.presetBadge({ current: "p1", modified: true }), { text: "已修改", kind: "status-warning" });
});

test("the one-line switch result carries notes, warnings and the number of missing paths", () => {
  assert.equal(L.applyNotifyText({}, "A"), "已切换到「A」。");
  const text = L.applyNotifyText({
    archive: { name: "切换前-2026-11-04 12:00:00" }, notes: ["语音服务已停止，下次使用时按新设置启动"],
    warnings: ["翻唱设置未能生效（RuntimeError），请在唱歌页检查"], missing_paths: [{}, {}, {}]
  }, "夜间");
  assert.match(text, /^已切换到「夜间」。切换前的设置已存档为「切换前-2026-11-04 12:00:00」。/);
  assert.match(text, /语音服务已停止，下次使用时按新设置启动。/);
  assert.match(text, /⚠ 翻唱设置未能生效（RuntimeError），请在唱歌页检查。/);
  assert.match(text, /有 3 个路径本机找不到/);
  assert.ok(!text.includes("\n"));
  assert.ok(!text.includes("。。"));
});

test("preset times are shown to the minute", () => {
  assert.equal(L.presetTime("2026-11-04T12:00:01.123456"), "2026-11-04 12:00");
  assert.equal(L.presetTime("2026-11-04 08:05:00"), "2026-11-04 08:05");
  assert.equal(L.presetTime(""), "");
  assert.equal(L.presetTime(5), "");
});

test("switch and import results list notes, warnings and missing paths", () => {
  const missing = [{ field: "cover.rvc_root", path: "D:/RVC" }, { field: "tts.irodori.launch_dir", path: "D:/Iro" }];
  assert.equal(L.missingPathsText([]), "");
  assert.equal(L.missingPathsText(missing), "cover.rvc_root：D:/RVC\ntts.irodori.launch_dir：D:/Iro");
  const text = L.applyResultText({ archive: { name: "切换前-2026-11-04 12:00:00" }, notes: ["语音服务已停止"], warnings: ["翻唱设置未能生效"], missing_paths: missing }, "夜间");
  assert.match(text, /^已切换到「夜间」。/);
  assert.match(text, /自动存档为「切换前-2026-11-04 12:00:00」/);
  assert.match(text, /语音服务已停止/);
  assert.match(text, /⚠ 翻唱设置未能生效/);
  assert.match(text, /cover\.rvc_root：D:\/RVC/);
  assert.equal(L.applyResultText({}, "A"), "已切换到「A」。");
  const imported = L.importResultText({ preset: { name: "朋友" }, missing_paths: [] });
  assert.match(imported, /「朋友」/);
  assert.match(imported, /都能找到/);
  assert.match(L.importResultText({ preset: { name: "朋友" }, missing_paths: missing }), /D:\/Iro/);
});

test("a module's collapsed state has its own key in the shared card map", () => {
  assert.equal(L.moduleKey("voice-common-auto"), "module:voice-common-auto");
  assert.notEqual(L.moduleKey("a"), L.moduleKey("b"));
  assert.deepEqual(L.parseCollapsed(JSON.stringify({ [L.moduleKey("x")]: true, "sing:转换参数": false })), { "module:x": true, "sing:转换参数": false });
});

test("a field is label + control + optional unit and help + a slot for its error message", () => {
  const plain = L.fieldMarkup({ label: "服务启动超时", control: '<input name="start_timeout">', unit: "秒", help: "30～600" });
  assert.match(plain, /^<label class="field">/);
  assert.match(plain, /<span class="field-label">服务启动超时<\/span>/);
  assert.match(plain, /<span class="field-input"><input name="start_timeout"><span class="field-unit">秒<\/span><\/span>/);
  assert.match(plain, /<small class="field-help">30～600<\/small>/);
  assert.match(plain, /<small class="field-msg" hidden><\/small><\/label>$/);
  const mono = L.fieldMarkup({ label: "top_k", control: '<input name="top_k">', mono: true, wide: true });
  assert.match(mono, /class="field field-wide"/);
  assert.match(mono, /class="field-label mono"/);
  assert.ok(!mono.includes("field-help") && !mono.includes("field-unit"));
});

test("a switch puts the name (and help) left and the checkbox right inside one label", () => {
  const html = L.fieldMarkup({ label: "text_split", mono: true, help: "启用句子切分", switchField: true, control: '<input name="split_sentence" type="checkbox">' });
  assert.match(html, /^<label class="field field-switch field-wide"><span class="field-text">/);
  assert.ok(html.indexOf("field-text") < html.indexOf("<input"));
  assert.match(html, /<span class="field-label mono">text_split<\/span><small class="field-help">启用句子切分<\/small><\/span><input name="split_sentence" type="checkbox"><\/label>$/);
});

test("a module is an open <details> with its slug, title and body", () => {
  const html = L.moduleMarkup("emotion-gpt-ref", "参考音频", "<p>x</p>");
  assert.equal(html, '<details class="module" data-module="emotion-gpt-ref" open><summary>参考音频</summary><div class="module-body"><p>x</p></div></details>');
});

test("a row menu sits right-aligned under its trigger and flips above near the window's bottom", () => {
  const view = { width: 1000, height: 700 };
  const size = { width: 160, height: 120 };
  assert.deepEqual(L.menuPlacement({ top: 100, bottom: 130, left: 800, right: 840 }, size, view), { top: 134, left: 680, up: false });
  assert.deepEqual(L.menuPlacement({ top: 600, bottom: 630, left: 800, right: 840 }, size, view), { top: 476, left: 680, up: true });
  // never leaves the window sideways
  assert.equal(L.menuPlacement({ top: 100, bottom: 130, left: 0, right: 30 }, size, view).left, 8);
  assert.equal(L.menuPlacement({ top: 100, bottom: 130, left: 990, right: 1010 }, size, view).left, 832);
  // no room above either: stay below, clamped into the window
  const cramped = L.menuPlacement({ top: 20, bottom: 50, left: 300, right: 340 }, { width: 160, height: 650 }, view);
  assert.equal(cramped.up, false);
  assert.ok(cramped.top >= 8 && cramped.top + 650 <= 700 - 8 + 1);
});

test("menu keys move the focus and wrap around", () => {
  assert.equal(L.nextMenuIndex(-1, 3, "ArrowDown"), 0);
  assert.equal(L.nextMenuIndex(0, 3, "ArrowDown"), 1);
  assert.equal(L.nextMenuIndex(2, 3, "ArrowDown"), 0);
  assert.equal(L.nextMenuIndex(-1, 3, "ArrowUp"), 2);
  assert.equal(L.nextMenuIndex(0, 3, "ArrowUp"), 2);
  assert.equal(L.nextMenuIndex(2, 3, "ArrowUp"), 1);
  assert.equal(L.nextMenuIndex(1, 3, "Home"), 0);
  assert.equal(L.nextMenuIndex(0, 3, "End"), 2);
  assert.equal(L.nextMenuIndex(1, 3, "x"), 1);
  assert.equal(L.nextMenuIndex(0, 0, "ArrowDown"), -1);
});

test("playback time is elapsed / total, or just elapsed while the length is unknown", () => {
  assert.equal(L.playTimeText(75, 200), "1:15 / 3:20");
  assert.equal(L.playTimeText(0, 59.9), "0:00 / 0:59");
  assert.equal(L.playTimeText(5.7, NaN), "0:05");
  assert.equal(L.playTimeText(5, Infinity), "0:05");
  assert.equal(L.playTimeText(undefined, 10), "0:00 / 0:10");
});

test("the system page has five tabs; an unknown tab falls back to the first, other pages have none", () => {
  assert.deepEqual(L.SYSTEM_TABS, ["general", "platform", "texts", "logs", "cleanup", "about"]);
  assert.equal(L.resolveTab("system", "cleanup"), "cleanup");
  assert.equal(L.resolveTab("system", "nope"), "general");
  assert.equal(L.resolveTab("system", ""), "general");
  assert.equal(L.resolveTab("affinity", "x"), "");
  assert.equal(L.tabLabel("system", "texts"), "回复文案");
  assert.equal(L.tabLabel("sing", "params"), "翻唱参数");
  assert.equal(L.tabLabel("voice", "x"), "");
  assert.equal(L.hashFor("system", "cleanup"), "#system/cleanup");
  assert.equal(L.tabFromHash("#system/about"), "about");
});

test("empty states say one sentence and name the main action when there is one", () => {
  const models = L.emptyStateMarkup("models");
  assert.match(models, /^<div class="empty-state"><p>还没有模型。<\/p><button type="button" class="btn btn-primary btn-sm" data-empty-action="new-model">＋ 添加模型<\/button><\/div>$/);
  assert.match(L.emptyStateMarkup("characters"), /data-empty-action="new-character"/);
  assert.match(L.emptyStateMarkup("plans"), /data-empty-action="new-plan">新建计划</);
  assert.match(L.emptyStateMarkup("songs"), /歌库是空的。.*data-empty-action="song-file">上传歌曲</);
  assert.match(L.emptyStateMarkup("stickers", { upload: "happy" }), /这个情绪还没有表情包。.*data-empty-upload="happy">上传</);
  for (const kind of ["users", "covers", "autos", "presets"]) {
    assert.ok(!L.emptyStateMarkup(kind).includes("<button"), kind);
    assert.match(L.emptyStateMarkup(kind), /^<div class="empty-state"><p>.+<\/p><\/div>$/);
  }
  assert.equal(L.emptyStateMarkup("unknown"), "");
});

test("owner form: payload trims, one empty field is a problem, both empty or both set is fine", () => {
  assert.deepEqual(L.ownerPayload({ platform_id: " qq_official ", sender_id: "  abc " }), { platform_id: "qq_official", sender_id: "abc" });
  assert.deepEqual(L.ownerPayload({}), { platform_id: "", sender_id: "" });
  assert.equal(L.ownerProblem({ platform_id: "", sender_id: "" }), "");
  assert.equal(L.ownerProblem({ platform_id: "a", sender_id: "b" }), "");
  assert.match(L.ownerProblem({ platform_id: "a", sender_id: "  " }), /要么都填/);
  assert.match(L.ownerProblem({ platform_id: "", sender_id: "b" }), /要么都填/);
});

test("overview connector line: the connected connector, or not connected", () => {
  assert.equal(L.connectorStatusText({ connected: true, name: "astrbot", version: "2.0.0" }), "astrbot 2.0.0 已连接");
  assert.equal(L.connectorStatusText({ connected: true, name: "astrbot" }), "astrbot 已连接");
  assert.equal(L.connectorStatusText({ connected: false, name: null }), "插件未连接（未安装或 AstrBot 没开）");
  assert.equal(L.connectorStatusText(null), "插件未连接（未安装或 AstrBot 没开）");
  assert.equal(L.connectorStatusText(undefined), "插件未连接（未安装或 AstrBot 没开）");
});

test("log view: the lines as they are, a hint when there are none", () => {
  assert.equal(L.logsText({ lines: ["a", "b c"] }), "a" + String.fromCharCode(10) + "b c");
  assert.match(L.logsText({ lines: [] }), /还没有日志/);
  assert.match(L.logsText(null), /还没有日志/);
  assert.deepEqual(L.LOG_LINE_CHOICES, [100, 300, 1000]);
});

test("R3: the NapCat port shows in the port note only when the core reports it", () => {
  const use = { st_port: 11451, bridge_port: 11452, console_port: 11453, connector_port: 11454, onebot_port: 11455 };
  assert.equal(L.networkStatusText({ in_use: use, reload_required: false, console_url: "x" }), "当前运行中：ST 11451 · bridge 11452 · 控制台 11453 · 连接器 11454 · NapCat 11455。");
  assert.deepEqual(Object.keys(L.NETWORK_FIELDS), ["st_port", "bridge_port", "console_port", "connector_port", "onebot_port"]);
});

test("R3: group lists are split on any separator and de-duplicated", () => {
  assert.deepEqual(L.parseGroupList("123\n456, 789 ，123；101"), ["123", "456", "789", "101"]);
  assert.deepEqual(L.parseGroupList(""), []);
  assert.deepEqual(L.parseGroupList(null), []);
});

test("R3: platform fields are checked before they are sent", () => {
  assert.deepEqual(L.chatPlatformProblems({ active: "napcat", group_whitelist: "123\n456", napcat_owner: "789" }), []);
  assert.deepEqual(L.chatPlatformProblems({ active: "astrbot", group_whitelist: "", napcat_owner: "" }), []);
  assert.equal(L.chatPlatformProblems({ group_whitelist: "12a", napcat_owner: "" })[0].key, "group_whitelist");
  assert.equal(L.chatPlatformProblems({ group_whitelist: "0123", napcat_owner: "" })[0].key, "group_whitelist");
  assert.equal(L.chatPlatformProblems({ group_whitelist: "", napcat_owner: "abc" })[0].key, "napcat_owner");
  assert.equal(L.chatPlatformProblems({ group_whitelist: Array.from({ length: 201 }, (_, i) => String(i + 1)).join(" "), napcat_owner: "" })[0].key, "group_whitelist");
});

test("R3: the platform payload", () => {
  assert.deepEqual(L.chatPlatformPayload({ active: "napcat", group_whitelist: "1 2\n2", napcat_owner: " 99 " }), { active: "napcat", group_whitelist: ["1", "2"], napcat_owner: "99" });
  assert.deepEqual(L.chatPlatformPayload({ active: "weird", group_whitelist: "", napcat_owner: null }), { active: "astrbot", group_whitelist: [], napcat_owner: "" });
});

test("R3: NapCat status and overview lines", () => {
  assert.equal(L.onebotStatusText(null), "未连接");
  assert.equal(L.onebotStatusText({ connected: false }), "未连接");
  assert.equal(L.onebotStatusText({ connected: true, self_id: "10000", heartbeat_age: null }), "小号 10000 已连接");
  assert.equal(L.onebotStatusText({ connected: true, self_id: "10000", heartbeat_age: 12 }), `小号 10000 已连接，最近心跳 ${L.agoText(12)}`);
  assert.equal(L.onebotStatusText({ connected: true, self_id: null }), "已连接");
  assert.equal(L.platformOverviewText("astrbot", { connected: true, name: "astrbot", version: "2.0.0" }, null), "AstrBot：astrbot 2.0.0 已连接");
  assert.equal(L.platformOverviewText(undefined, null, null), "AstrBot：插件未连接（未安装或 AstrBot 没开）");
  assert.equal(L.platformOverviewText("napcat", null, { connected: true, self_id: "1" }), "NapCat：小号 1 已连接");
  assert.equal(L.platformOverviewText("napcat", { connected: true, name: "x" }, { connected: false }), "NapCat：未连接");
  assert.equal(L.tokenDisplay("", true), "-");
  assert.equal(L.tokenDisplay(undefined, false), "-");
  assert.equal(L.tokenDisplay("abc123-secret-token", false), L.TOKEN_MASK);
  assert.equal(L.TOKEN_MASK.length, 8);  // fixed length: the real length is not exposed
  assert.equal(L.tokenDisplay("abc123-secret-token", true), "abc123-secret-token");
  assert.equal(L.onebotFormatWarning({ format_warning: false }), "");
  assert.equal(L.onebotFormatWarning(null), "");
  assert.match(L.onebotFormatWarning({ format_warning: true }), /array/);
  assert.match(L.platformChoiceNote("napcat"), /当前是 NapCat/);
  assert.match(L.platformChoiceNote("astrbot"), /当前是 AstrBot/);
  assert.equal(L.tabLabel("system", "platform"), "聊天平台");
});

test("R3: the wake prefix hint is satisfied by the NapCat prefixes (slash nickname and nickname)", () => {
  assert.deepEqual(L.wakePrefixHint(["/琴音", "琴音"], "琴音"), { status: "ok", text: "已一致" });
});

test("R3 review: a NapCat port that could not be opened is said so, whichever platform is active", () => {
  const blocked = { connected: false, self_id: null, port_unavailable: true, port: 11455 };
  assert.equal(L.onebotStatusText(blocked), "端口 11455 被占用，NapCat 不可用");
  assert.equal(L.platformOverviewText("napcat", null, blocked), "NapCat：端口 11455 被占用，NapCat 不可用");
  assert.equal(L.platformOverviewText("astrbot", { connected: true, name: "astrbot" }, blocked), "AstrBot：astrbot 已连接（端口 11455 被占用，NapCat 不可用）");
  assert.equal(L.platformOverviewText("astrbot", { connected: true, name: "astrbot" }, { connected: false, port_unavailable: false }), "AstrBot：astrbot 已连接");
});

// ---------- R4b: 安装与路径 ----------

test("install page: path status lines", () => {
  assert.equal(L.pathStatusText({ state: "empty" }), "未填（可以留空）");
  assert.equal(L.pathStatusText(undefined), "未填（可以留空）");
  assert.equal(L.pathStatusText({ state: "valid", message: "扩展目录：C:\\st" }), "✓ 有效：扩展目录：C:\\st");
  assert.equal(L.pathStatusText({ state: "invalid", message: "目录不存在" }), "✗ 无效：目录不存在");
  assert.equal(L.pathStatusText({ state: "invalid" }), "✗ 无效：路径不可用");
  assert.equal(L.pathStatusText({ state: "choose", message: "请选一个启动文件" }), "请选一个启动文件");
});

test("install page: the payload is trimmed and complete; problems", () => {
  const body = L.installPathsPayload({ st_dir: "  C:\\ST  ", astrbot_dir: "", napcat_path: undefined, rvc_dir: "D:\\r", pip_mirror: " https://x/simple " });
  assert.deepEqual(Object.keys(body).sort(), ["astrbot_dir", "gpt_sovits_dir", "irodori_dir", "irodori_server_dir", "napcat_path", "pip_mirror", "rvc_dir", "st_dir"]);
  assert.equal(body.st_dir, "C:\\ST");
  assert.equal(body.napcat_path, "");
  assert.equal(body.pip_mirror, "https://x/simple");
  assert.deepEqual(L.installPathsProblems({ pip_mirror: "" }), []);
  assert.deepEqual(L.installPathsProblems({ pip_mirror: "https://a/b" }), []);
  assert.equal(L.installPathsProblems({ pip_mirror: "ftp://a" })[0].key, "pip_mirror");
  assert.equal(L.installPathsProblems({ pip_mirror: "http://a b" })[0].key, "pip_mirror");
  assert.equal(L.installPathsProblems({ st_dir: "a\nb" })[0].key, "st_dir");
});

test("install page: badges and which buttons work", () => {
  assert.deepEqual(L.installStateBadge({ state: "installed" }), { text: "已安装", className: "status-success" });
  assert.equal(L.installStateBadge({ state: "outdated" }).className, "status-error");
  assert.equal(L.installStateBadge({ state: "missing" }).text, "未安装");
  assert.equal(L.installStateBadge({ state: "unknown" }).text, "未设置路径");
  assert.equal(L.installStateBadge(null).text, "-");
  const pack = { plugin: true, extension: true, available: true };
  const installed = L.installButtons({ state: "installed" }, pack, "plugin", false);
  assert.deepEqual([installed.installDisabled, installed.uninstallDisabled, installed.installLabel], [false, false, "重装"]);
  const missing = L.installButtons({ state: "missing" }, pack, "extension", false);
  assert.deepEqual([missing.installDisabled, missing.uninstallDisabled, missing.installLabel], [false, true, "安装"]);
  const unknown = L.installButtons({ state: "unknown" }, pack, "plugin", false);
  assert.equal(unknown.installDisabled, true);
  assert.match(unknown.reason, /AstrBot 路径/);
  assert.match(L.installButtons({ state: "unknown" }, pack, "extension", false).reason, /ST 路径/);
  assert.match(L.installButtons({ state: "installed" }, pack, "plugin", true).reason, /未保存/);
  assert.equal(L.installButtons({ state: "installed" }, pack, "plugin", true).installDisabled, true);
  assert.equal(L.installButtons({ state: "installed" }, { plugin: false, extension: false }, "plugin", false).reason, "开发环境，没有可安装的文件");
  assert.equal(L.installButtons({ state: "missing" }, null, "plugin", false).installDisabled, true);
});

test("install page: the overview card", () => {
  assert.equal(L.installOverview(null).badge, "读取失败");
  const none = L.installOverview({ checks: {}, extension: { state: "unknown" }, plugin: { state: "unknown" } });
  assert.deepEqual([none.badge, none.paths], ["待安装", "已填 0 / 7"]);
  const good = L.installOverview({
    checks: { st: { state: "valid" }, astrbot: { state: "valid" }, napcat: { state: "empty" } },
    extension: { state: "installed" }, plugin: { state: "installed" }
  });
  assert.deepEqual([good.badge, good.badgeClass, good.extension, good.plugin, good.paths], ["正常", "status-success", "已安装", "已安装", "已填 2 / 7"]);
  const bad = L.installOverview({ checks: { st: { state: "invalid" }, rvc: { state: "valid" } }, extension: { state: "missing" }, plugin: { state: "missing" } });
  assert.deepEqual([bad.badge, bad.badgeClass, bad.paths], ["有路径无效", "status-error", "已填 2 / 7，1 个无效"]);
  const noAstrbot = L.installOverview({ checks: { st: { state: "valid" } }, extension: { state: "installed" }, plugin: { state: "unknown" } });
  assert.equal(noAstrbot.badge, "正常");  // AstrBot is optional
});

test("install page: import texts", () => {
  assert.equal(L.importItemText({ name: "audio", kind: "dir", files: 3, size: 2048, exists: true }), "audio（文件夹，3 个文件，2 KB，会替换现有的）");
  assert.equal(L.importItemText({ name: "config.json", kind: "file", files: 1, size: 500, exists: false }), "config.json（1 KB）");
  const text = L.importDoneText({ copied: ["a", "b"], backup: "C:\\d\\backups\\pre-import-1", backed_up: ["a"], rewritten_paths: 3 });
  assert.match(text, /已导入 2 项/);
  assert.match(text, /先备份到：C:\\d\\backups\\pre-import-1/);
  assert.match(text, /3 处指向旧数据目录/);
  assert.match(text, /重启核心/);
  assert.match(L.importDoneText({ copied: ["a"], backup: null, backed_up: [], rewritten_paths: 0, restarting: true }), /导入完成，正在重启核心…/);
  assert.ok(!/请重启核心/.test(L.importDoneText({ copied: ["a"], backup: null, backed_up: [], rewritten_paths: 0, restarting: true })));
  const plain = L.importDoneText({ copied: ["a"], backup: null, backed_up: [], rewritten_paths: 0 });
  assert.ok(!/备份/.test(plain) && !/指向旧数据目录/.test(plain) && !/已保留/.test(plain));
  assert.match(L.importDoneText({ copied: ["a"], backup: null, backed_up: [], rewritten_paths: 0, kept_install: true }), /ST \/ AstrBot \/ NapCat 路径已保留/);
});

test("install page: dependency rows, job text, restart notice", () => {
  assert.deepEqual(L.dependencyStateText({ state: "installed" }), { text: "已安装", className: "status-success" });
  assert.equal(L.dependencyStateText({ state: "missing" }).text, "未安装");
  assert.equal(L.dependencyStateText({ state: "blocked", needs: "请先设置 RVC 目录" }).text, "请先设置 RVC 目录");
  assert.equal(L.dependencyStateText({ state: "unknown" }).text, "未检查");
  assert.equal(L.jobStatusText(null), "");
  assert.equal(L.jobStatusText({ state: "running" }), "正在安装…");
  assert.equal(L.jobStatusText({ state: "failed", message: "安装失败（退出码 1）" }), "安装失败（退出码 1）");
  assert.equal(L.jobStatusText({ state: "success" }), "安装完成");
  assert.match(L.restartNotice({ auto: true }), /自动重新连接/);
  assert.match(L.restartNotice({ auto: false }), /重新运行 start\.bat/);
  assert.match(L.restartNotice(null), /重新运行 start\.bat/);
  assert.ok(L.INSTALL_PATH_FIELDS.every((item) => item.key && item.field && item.label));
});

test("R5 injection: new custom entry ids, ordering payload, note line", () => {
  assert.equal(L.newInjectionEntry([]).id, "custom1");
  assert.equal(L.newInjectionEntry([{ id: "custom1" }, { id: "custom3" }]).id, "custom2");
  const blank = L.newInjectionEntry([]);
  assert.deepEqual([blank.builtin, blank.enabled, blank.target, blank.placement, blank.role, blank.depth], [false, true, "preset", "after_chat_history", "system", 0]);
  const body = L.injectionPayload("  STbridge ", [
    { id: "a", builtin: true, title: " 一 ", enabled: true, content: "x", target: "preset", placement: "depth", depth: "3", role: "user", order: 99 },
    { id: "b", builtin: false, title: "二", enabled: false, content: "y", target: "worldbook", placement: "after_chat_history", depth: 0, role: "system", order: 1 }
  ]);
  assert.equal(body.world_book_name, "STbridge");
  assert.deepEqual(body.entries.map((entry) => entry.order), [10, 20]);
  assert.deepEqual([body.entries[0].depth, body.entries[0].title, body.entries[1].enabled, body.entries[1].builtin], [3, "一", false, false]);
  assert.equal(L.injectionEntryNote({ target: "preset", placement: "after_chat_history", depth: 0 }), "ST 预设 · Chat History 之后");
  assert.equal(L.injectionEntryNote({ target: "preset", placement: "depth", depth: 4 }), "ST 预设 · 深度 4");
  assert.equal(L.injectionEntryNote({ target: "worldbook", placement: "after_chat_history", depth: 2 }), "世界书「STbridge」· 深度 2");
  assert.deepEqual(Object.keys(L.INJECTION_TARGETS), ["preset", "worldbook"]);
});

test("R5 injection: restoring built-in entries puts them back in the default order", () => {
  const defaults = ["a", "b", "c", "d"].map((id) => ({ id, builtin: true, title: id.toUpperCase() }));
  const custom = { id: "mine", builtin: false, title: "我的" };
  // nothing missing: unchanged copy
  const same = L.restoreBuiltinEntries(defaults, defaults);
  assert.deepEqual([same.entries.map((entry) => entry.id), same.restored], [["a", "b", "c", "d"], []]);
  // a middle one is back after its predecessor
  let result = L.restoreBuiltinEntries([defaults[0], custom, defaults[2], defaults[3]], defaults);
  assert.deepEqual(result.entries.map((entry) => entry.id), ["a", "b", "mine", "c", "d"]);
  assert.deepEqual(result.restored, ["B"]);
  // the first one goes to the front, the others follow their predecessor
  result = L.restoreBuiltinEntries([custom, defaults[3]], defaults);
  assert.deepEqual(result.entries.map((entry) => entry.id), ["a", "b", "c", "mine", "d"]);
  assert.deepEqual(result.restored, ["A", "B", "C"]);
  // an empty list restores everything, custom entries are never touched or duplicated
  result = L.restoreBuiltinEntries([], defaults);
  assert.deepEqual(result.entries.map((entry) => entry.id), ["a", "b", "c", "d"]);
  result = L.restoreBuiltinEntries([custom], defaults);
  assert.equal(result.entries.filter((entry) => entry.id === "mine").length, 1);
  assert.equal(result.entries.length, 5);
  // the inputs are not changed, and the restored entries are copies
  const input = [custom];
  L.restoreBuiltinEntries(input, defaults);
  assert.equal(input.length, 1);
  result.entries[0].title = "changed";
  assert.equal(defaults[0].title, "A");
  assert.deepEqual(L.restoreBuiltinEntries(null, null), { entries: [], restored: [] });
});

test("R5 injection: the page is in the sidebar and has no tabs", () => {
  assert.equal(L.PAGE_LABELS.injection, "ST 注入");
  assert.equal(L.resolvePage("injection"), "injection");
  assert.equal(L.resolveTab("injection", "x"), "");
});

test("L1d result texts: duplicates, giving way and the failure reasons", () => {
  assert.equal(L.resultText("sent:fallback(duplicate)"), "已发备用文字（ST 写的和上一条太像）");
  assert.equal(L.resultText("deferred:Requeued"), "已让路（刚有人开口，等安静了再发）");
  assert.equal(L.resultText("failed:Duplicate"), "失败（ST 写的和上一条太像，已丢弃）");
  assert.equal(L.resultText("failed:StUnavailable"), "失败（ST 不可用）");
  assert.equal(L.resultText("failed:QueueFull"), "失败（队列已满）");
  assert.equal(L.resultText("failed:RuntimeError"), "失败（RuntimeError）");
});

const chatValues = (over = {}) => ({
  enabled: true, silence_min: "120", silence_max: "240", factor: "1.5", max_unanswered: "2", daily_limit: "3",
  quiet_start: "00:00", quiet_end: "08:00", prompt: "想他了 {{elapsed}}", ...over
});
const chatWindows = (over = {}) => ({ weekday: [{ start: "12:00", end: "13:30" }], weekend: [], ...over });

test("L1d chat form: a good form has no problems and builds the body", () => {
  assert.deepEqual(L.validateChatForm(chatValues(), chatWindows()), []);
  const body = L.chatPayload(chatValues({ prompt: "  想他了  " }), chatWindows());
  assert.deepEqual(body, {
    enabled: true, silence_min: 120, silence_max: 240, followup_factor: 1.5, max_unanswered: 2, daily_limit: 3,
    quiet_hours: { start: "00:00", end: "08:00" },
    active_windows: { weekday: [{ start: "12:00", end: "13:30" }], weekend: [] },
    prompt: "想他了"
  });
});

test("L1d chat form: every field has its bounds", () => {
  const keys = (values, windows = chatWindows()) => L.validateChatForm(values, windows).map((problem) => problem.key);
  assert.deepEqual(keys(chatValues({ silence_min: "29" })), ["silence_min"]);
  assert.deepEqual(keys(chatValues({ silence_max: "1441" })), ["silence_max"]);
  assert.deepEqual(keys(chatValues({ silence_min: "300", silence_max: "200" })), ["silence_max"]);
  assert.deepEqual(keys(chatValues({ silence_min: "60.5" })), ["silence_min"]);
  assert.deepEqual(keys(chatValues({ factor: "0.9" })), ["factor"]);
  assert.deepEqual(keys(chatValues({ factor: "4.1" })), ["factor"]);
  assert.deepEqual(keys(chatValues({ max_unanswered: "11" })), ["max_unanswered"]);
  assert.deepEqual(keys(chatValues({ max_unanswered: "-1" })), ["max_unanswered"]);
  assert.deepEqual(keys(chatValues({ daily_limit: "21" })), ["daily_limit"]);
  assert.deepEqual(keys(chatValues({ daily_limit: "0" })), []);
  assert.deepEqual(keys(chatValues({ quiet_start: "" })), ["quiet_start"]);
  assert.deepEqual(keys(chatValues({ quiet_end: "25:00" })), ["quiet_end"]);
  assert.deepEqual(keys(chatValues({ quiet_start: "03:00", quiet_end: "03:00" })), []);
  assert.deepEqual(keys(chatValues({ prompt: "   " })), ["prompt"]);
  assert.deepEqual(keys(chatValues({ prompt: "字".repeat(301) })), ["prompt"]);
  assert.deepEqual(keys(chatValues({ prompt: "字".repeat(300) })), []);
});

test("L1d chat form: windows are checked one by one", () => {
  const problems = (windows) => L.validateChatForm(chatValues(), windows);
  assert.deepEqual(problems(chatWindows({ weekday: [{ start: "10:00", end: "09:00" }] })).map((item) => item.key), ["win:weekday:0:end"]);
  assert.deepEqual(problems(chatWindows({ weekend: [{ start: "10:00", end: "10:00" }] })).map((item) => item.key), ["win:weekend:0:end"]);
  assert.deepEqual(problems(chatWindows({ weekday: [{ start: "", end: "09:00" }] })).map((item) => item.key), ["win:weekday:0:start"]);
  const seven = Array.from({ length: 7 }, () => ({ start: "10:00", end: "11:00" }));
  assert.equal(problems(chatWindows({ weekday: seven })).length, 1);
  assert.equal(problems(chatWindows({ weekday: seven.slice(0, 6), weekend: [] })).length, 0);
  assert.equal(L.CHAT_LIMITS.windows, 6);
});

test("L1d chat status text", () => {
  assert.equal(L.chatStatusText(null), "不可用");
  assert.equal(L.chatStatusText({ reason: "Waiting", next_at: "2026-11-03T15:00" }), `等待中：还没到时间（预计 ${L.formatDateTime("2026-11-03T15:00")}）`);
  assert.equal(L.chatStatusText({ reason: "Disabled", next_at: null }), "已关闭");
  assert.equal(L.chatStatusText({ reason: "DailyLimit", next_at: null }), "等待中：今天已发满");
  assert.equal(L.chatStatusText({ reason: "Whatever", next_at: null }), "Whatever");
  for (const reason of ["UnansweredLimit", "QuietHours", "OutsideWindow", "Busy", "Conversing", "Gap", "T1Priority", "Retry", "NoOwner", "Running", "Ready"]) {
    assert.ok(L.CHAT_STATUS_TEXT[reason], reason);
  }
  const status = { today: 2, daily_limit: 3, unanswered: 1, max_unanswered: 2 };
  assert.equal(L.chatCountText(status, "today"), "2 / 3");
  assert.equal(L.chatCountText(status, "unanswered"), "1（超过 2 次就先不发）");
  assert.equal(L.chatCountText(null, "today"), "-");
});

// ---------- H1: health chain ----------

const replies = (recent, extra = {}) => ({
  count: recent.length, ok: recent.filter((code) => code === "ok").length, recent,
  last_fail_code: recent.filter((code) => code !== "ok").pop() ?? null,
  last_fail_age: recent.some((code) => code !== "ok") ? 120 : null,
  avg_ms: 2400, ...extra
});

test("H1 replyHealth: no records is grey", () => {
  for (const value of [null, undefined, {}, replies([]), { recent: "x" }]) {
    const health = L.replyHealth(value);
    assert.equal(health.level, "off");
    assert.equal(health.text, "还没有请求");
  }
});

test("H1 replyHealth: all fine is green with the average time", () => {
  const health = L.replyHealth(replies(["ok", "ok", "ok"]));
  assert.equal(health.level, "ok");
  assert.equal(health.text, "最近 3 次都正常 · 平均 2.4 秒");
  assert.equal(L.replyHealth(replies(["ok"], { avg_ms: null })).text, "最近 1 次都正常");
  assert.equal(L.replyHealth(replies(["ok"], { avg_ms: 15000 })).text, "最近 1 次都正常 · 平均 15 秒");
});

test("H1 replyHealth: the last three all failed is red, with the reason", () => {
  const health = L.replyHealth(replies(["ok", "ok", "StFailed", "StFailed", "StFailed"]));
  assert.equal(health.level, "error");
  assert.match(health.text, /连续 3 次没拿到回复：ST 生成失败/);
  assert.match(health.text, /最近一次 2 分钟前/);
  assert.match(health.reason, /^模型连续 3 次没回出来：ST 生成失败/);
  assert.equal(L.replyHealth(replies(["Timeout", "Timeout", "Timeout"])).level, "error");
});

test("H1 replyHealth: half or more of the last ten failed is yellow", () => {
  const five = replies(["ok", "StFailed", "ok", "StFailed", "ok", "StFailed", "ok", "StFailed", "ok", "StFailed"]);
  const health = L.replyHealth(five);
  assert.equal(health.level, "warn");
  assert.match(health.text, /最近 10 次里 5 次失败/);
  assert.equal(L.replyHealth(replies(["ok", "StFailed"])).level, "warn");
  assert.equal(L.replyHealth(replies(["StFailed", "StFailed"])).level, "warn");
  assert.equal(L.replyHealth(replies(["ok", "ok", "StFailed", "ok", "ok", "ok", "ok", "ok", "ok", "ok"])).level, "ok");
});

test("H1 replyHealth: only the last ten count, and an old streak does not", () => {
  const old = Array(10).fill("StFailed");
  assert.equal(L.replyHealth(replies([...old, ...Array(10).fill("ok")])).level, "ok");
  assert.equal(L.replyHealth(replies([...Array(10).fill("ok"), "StFailed", "StFailed"])).level, "ok");
  const withFailure = L.replyHealth(replies([...Array(9).fill("ok"), "InvalidReply"]));
  assert.equal(withFailure.level, "ok");
  assert.match(withFailure.text, /最近 10 次里 1 次失败/);
});

test("H1 every failure code has a plain reason", () => {
  for (const code of ["StFailed", "Timeout", "EmptyReply", "InvalidReply"]) assert.ok(L.REPLY_FAILURE_TEXT[code], code);
  assert.equal(L.replyFailureText("Nope"), "原因不明");
});

const goodStatus = (overrides = {}) => ({
  bridge: { gate: "RECOVERED", last_poll_age: 1, active: false, http_running: true },
  replies: replies(["ok", "ok"]),
  platform: "astrbot",
  connector: { connected: true, name: "st_bridge" },
  onebot: null,
  ...overrides
});
const goodVoice = (overrides = {}) => ({ settings: { engine: "irodori", common: { prob: 100, auto_start: true } }, service: { engine: "irodori", state: "running" }, ...overrides });
const goodCover = (overrides = {}) => ({ enabled: true, service: { state: "stopped" }, ...overrides });
const chainOf = (input) => L.healthChain({ status: goodStatus(), failures: 0, voice: goodVoice(), cover: goodCover(), ...input });
const byId = (chain, id) => chain.find((node) => node.id === id);

test("H1 healthChain: six nodes in travel order, all fine", () => {
  const chain = chainOf({});
  assert.deepEqual(chain.map((node) => node.id), ["qq", "core", "st", "reply", "voice", "cover"]);
  assert.deepEqual(chain.map((node) => node.label), ["QQ 平台", "核心", "ST 扩展", "模型回复", "语音", "翻唱"]);
  assert.deepEqual(chain.map((node) => node.level), ["ok", "ok", "ok", "ok", "ok", "ok"]);
  for (const node of chain) assert.ok(node.goto && node.text, node.id);
  assert.equal(L.healthHeadline(chain, "琴音"), "琴音在线 · 一切正常");
  assert.equal(L.healthBanner(chain, "琴音"), null);
  assert.equal(L.healthTitle("控制台", chain), "控制台");
});

test("H1 healthChain: QQ platform for AstrBot and NapCat", () => {
  assert.equal(byId(chainOf({ status: goodStatus({ connector: { connected: false } }) }), "qq").level, "error");
  assert.equal(byId(chainOf({ status: goodStatus({ connector: null }) }), "qq").level, "error");
  const napcat = (onebot) => byId(chainOf({ status: goodStatus({ platform: "napcat", connector: null, onebot }) }), "qq");
  assert.equal(napcat({ connected: true }).level, "ok");
  assert.equal(napcat({ connected: true }).text, "NapCat 已连接");
  assert.equal(napcat({ connected: false }).level, "error");
  assert.equal(napcat(null).level, "error");
  assert.equal(napcat({ connected: false, port_unavailable: true, port: 6700 }).reason, "NapCat 的端口 6700 被占用");
  assert.equal(byId(chainOf({ status: null }), "qq").level, "off");
  // The platform that is not in use does not matter.
  assert.equal(byId(chainOf({ status: goodStatus({ platform: "astrbot", onebot: { connected: false } }) }), "qq").level, "ok");
});

test("H1 healthChain: the core node", () => {
  assert.equal(byId(chainOf({}), "core").level, "ok");
  assert.equal(byId(chainOf({ status: null, failures: 0 }), "core").level, "off");
  const one = chainOf({ failures: 1 });
  assert.equal(byId(one, "core").level, "ok");
  const two = chainOf({ failures: 2 });
  assert.equal(byId(two, "core").level, "error");
  assert.equal(byId(two, "core").reason, "核心未运行：双击 start.bat");
  assert.deepEqual(two.filter((node) => node.id !== "core").map((node) => node.level), ["off", "off", "off", "off", "off"]);
  assert.equal(L.healthHeadline(two, "琴音"), "琴音现在回不了消息：核心未运行：双击 start.bat");
});

test("H1 healthChain: the ST node follows the extension status", () => {
  const st = (bridge) => byId(chainOf({ status: goodStatus({ bridge }) }), "st");
  assert.equal(st({ http_running: true, active: true, last_poll_age: 50 }).level, "ok");
  assert.equal(st({ http_running: true, last_poll_age: 3 }).level, "ok");
  assert.equal(st({ http_running: true, last_poll_age: 3 }).text, "在线");
  assert.equal(st({ http_running: true, last_poll_age: 125 }).level, "error");
  assert.match(st({ http_running: true, last_poll_age: 125 }).reason, /ST 扩展离线（最近一次 2 分钟前）/);
  assert.equal(st({ http_running: true, last_poll_age: null }).level, "error");
  assert.match(st({ http_running: true, last_poll_age: null }).reason, /还没连上过/);
  assert.equal(st({ http_running: false }).level, "error");
  assert.equal(st(null).level, "off");
});

test("H1 healthChain: the model node follows the reply record", () => {
  const reply = (list) => byId(chainOf({ status: goodStatus({ replies: replies(list) }) }), "reply");
  assert.equal(reply([]).level, "off");
  assert.equal(reply(["ok"]).level, "ok");
  assert.equal(reply(["ok", "InvalidReply", "ok", "InvalidReply"]).level, "warn");
  assert.equal(reply(["InvalidReply", "InvalidReply", "InvalidReply"]).level, "error");
  assert.equal(byId(chainOf({ status: goodStatus({ replies: undefined }) }), "reply").level, "off");
});

test("H1 healthChain: the voice node", () => {
  const voice = (value) => byId(chainOf({ voice: value }), "voice");
  assert.equal(voice(goodVoice()).level, "ok");
  assert.equal(voice(goodVoice()).text, "Irodori 运行中");
  assert.equal(voice(goodVoice({ service: { engine: "gpt_sovits", state: "external" } })).text, "GPT-SoVITS 运行中");
  assert.equal(voice(goodVoice({ service: { engine: "irodori", state: "starting" } })).level, "ok");
  assert.equal(voice(goodVoice({ service: { engine: "irodori", state: "failed" } })).level, "warn");
  const stopped = { engine: "irodori", state: "stopped" };
  assert.equal(voice(goodVoice({ service: stopped })).level, "ok");
  assert.equal(voice(goodVoice({ service: stopped })).text, "Irodori 用到时自动启动");
  const manual = goodVoice({ service: stopped, settings: { engine: "irodori", common: { prob: 100, auto_start: false } } });
  assert.equal(voice(manual).level, "warn");
  const off = goodVoice({ settings: { engine: "irodori", common: { prob: 0, auto_start: true } }, service: { engine: "irodori", state: "failed" } });
  assert.equal(voice(off).level, "off");
  assert.equal(voice(goodVoice({ service: null })).level, "off");
  assert.equal(voice(null).level, "off");
});

test("H1 healthChain: the cover node", () => {
  const cover = (value) => byId(chainOf({ cover: value }), "cover");
  assert.equal(cover(goodCover({ enabled: false })).level, "off");
  assert.equal(cover(goodCover({ enabled: false, service: { state: "failed" } })).level, "off");
  assert.equal(cover(goodCover({ enabled: undefined })).level, "off");
  assert.equal(cover(null).level, "off");
  assert.equal(cover(goodCover()).level, "ok");
  assert.match(cover(goodCover()).text, /待命/);
  assert.equal(cover(goodCover({ service: { state: "running" } })).text, "RVC 运行中");
  assert.equal(cover(goodCover({ service: { state: "failed" } })).level, "warn");
  assert.equal(cover(goodCover({ service: null })).level, "ok");
});

test("H1 healthHeadline: the first error wins, warnings are counted, the name is not fixed", () => {
  const broken = chainOf({ status: goodStatus({ connector: { connected: false }, bridge: { http_running: true, last_poll_age: 300 } }) });
  assert.equal(L.healthHeadline(broken, "小铃"), "小铃现在回不了消息：AstrBot 插件没连上");
  const warned = chainOf({ voice: goodVoice({ service: { engine: "irodori", state: "failed" } }), cover: goodCover({ service: { state: "failed" } }) });
  assert.equal(L.healthHeadline(warned, "小铃"), "小铃在线 · 有 2 项需要注意");
  assert.equal(L.healthHeadline(chainOf({}), "小铃"), "小铃在线 · 一切正常");
  assert.equal(L.healthHeadline(chainOf({})), `${L.DEFAULT_NICKNAME}在线 · 一切正常`);
  assert.equal(L.healthHeadline(null, "x"), "x在线 · 一切正常");
  // Grey nodes are not problems.
  const grey = chainOf({ status: goodStatus({ replies: replies([]) }), cover: goodCover({ enabled: false }) });
  assert.equal(L.healthHeadline(grey, "琴音"), "琴音在线 · 一切正常");
});

test("H1 healthBanner and healthTitle", () => {
  const broken = chainOf({ status: goodStatus({ bridge: { http_running: true, last_poll_age: 300 } }) });
  const banner = L.healthBanner(broken, "琴音");
  assert.equal(banner.level, "error");
  assert.equal(banner.goto, "install");
  assert.match(banner.text, /^琴音现在回不了消息：ST 扩展离线/);
  assert.equal(L.healthTitle("ST Bridge 控制台", broken), "⚠ ST Bridge 控制台");
  const warned = chainOf({ voice: goodVoice({ service: { engine: "irodori", state: "failed" } }) });
  const yellow = L.healthBanner(warned, "琴音");
  assert.equal(yellow.level, "warn");
  assert.equal(yellow.goto, "voice");
  assert.equal(L.healthTitle("ST Bridge 控制台", warned), "ST Bridge 控制台");
  // An error beats an earlier-in-the-chain warning for the link target.
  const both = chainOf({ status: goodStatus({ replies: replies(["StFailed", "StFailed", "StFailed"]) }), voice: goodVoice({ service: { engine: "irodori", state: "failed" } }) });
  assert.equal(L.healthBanner(both, "琴音").level, "error");
  assert.equal(L.healthBanner(both, "琴音").goto, "system/logs");
});

test("H1 every goto is a page or tab the console has", () => {
  const chain = chainOf({});
  for (const node of chain) {
    const target = L.splitTarget(node.goto);
    assert.ok(L.PAGE_LABELS[target.page], node.goto);
    if (target.tab) assert.equal(L.resolveTab(target.page, target.tab), target.tab, node.goto);
  }
});

test("H1.1 replyHealth: a streak of failures with no request for over 30 minutes is only a warning", () => {
  const stale = L.replyHealth(replies(["ok", "StFailed", "StFailed", "StFailed"], { last_fail_age: 45 * 60 }));
  assert.equal(stale.level, "warn");
  assert.match(stale.text, /^约 45 分钟前连续失败，之后还没有新请求/);
  assert.match(stale.reason, /^模型约 45 分钟前连续失败，之后还没有新请求/);
  assert.match(stale.text, /ST 生成失败/);
  assert.equal(L.REPLY_STALE_SECONDS, 1800);
});

test("H1.1 replyHealth: the stale rule only lowers the red, at exactly 30 minutes it is still red", () => {
  const edge = (age) => L.replyHealth(replies(["StFailed", "StFailed", "StFailed"], { last_fail_age: age }));
  assert.equal(edge(1800).level, "error");
  assert.equal(edge(1801).level, "warn");
  assert.equal(edge(60).level, "error");
  assert.equal(edge(null).level, "error");
  assert.equal(edge(undefined).level, "error");
});

test("H1.1 replyHealth: a stale warning in the chain makes a yellow banner, not a red one", () => {
  const status = { bridge: { gate: "RECOVERED", last_poll_age: 1, active: false, http_running: true }, replies: replies(["StFailed", "StFailed", "StFailed"], { last_fail_age: 3 * 3600 }), platform: "astrbot", connector: { connected: true } };
  const chain = L.healthChain({ status, failures: 0, voice: null, cover: null });
  assert.equal(chain.find((node) => node.id === "reply").level, "warn");
  const banner = L.healthBanner(chain, "琴音");
  assert.equal(banner.level, "warn");
  assert.equal(banner.text, "琴音在线 · 有 1 项需要注意");
  assert.equal(L.healthTitle("T", chain), "T");
});

test("H1.1 replyHealth: the yellow rules are unchanged by the stale rule", () => {
  const half = L.replyHealth(replies(["ok", "StFailed", "ok", "StFailed"], { last_fail_age: 3 * 3600 }));
  assert.equal(half.level, "warn");
  assert.match(half.text, /最近 4 次里 2 次失败/);
  assert.equal(L.replyHealth(replies(["ok", "ok", "ok"], { last_fail_age: 3 * 3600 })).level, "ok");
});

// ---------- H2: overview ----------

const fullToday = () => ({
  date: "2026-11-09",
  replies: { private: 12, group: 5, total: 17 },
  songs: { owner: 2, group: 1, total: 3 },
  chat: { reason: "Waiting", next_at: "2026-11-09T21:30", today: 1, daily_limit: 3 },
  mood: { global: { emotion: "开心", age: "刚才", degree: "还很明显", calm: false }, owner: { emotion: "害羞", age: "2 小时前", degree: "已经平复", calm: true } },
  queue: { active: true, waiting: 2 }
});
const cardOf = (cards, id) => cards.find((card) => card.id === id);

test("H2 todayCards: five cards in order, with big numbers and one-line notes", () => {
  const cards = L.todayCards(fullToday());
  assert.deepEqual(cards.map((card) => card.id), ["replies", "chat", "songs", "mood", "queue"]);
  assert.deepEqual(cards.map((card) => card.label), ["回复", "主动搭话", "唱歌", "心情", "排队"]);
  assert.deepEqual([cardOf(cards, "replies").value, cardOf(cards, "replies").sub], ["17", "私聊 12 · 群聊 5"]);
  assert.deepEqual([cardOf(cards, "chat").value, cardOf(cards, "chat").sub], ["1 / 3", "下次约 21:30"]);
  assert.deepEqual([cardOf(cards, "songs").value, cardOf(cards, "songs").sub], ["3", "主人 2 首"]);
  assert.deepEqual([cardOf(cards, "queue").value, cardOf(cards, "queue").sub], ["2", "正在处理 1 条"]);
  for (const card of cards) assert.ok(L.splitTarget(card.goto).page in L.PAGE_LABELS, card.goto);
});

test("H2 todayCards: nothing to show is dashes, never an error", () => {
  for (const value of [null, undefined, {}, "x", 5, { replies: null, chat: null, songs: null, mood: null, queue: null }]) {
    const cards = L.todayCards(value);
    assert.equal(cards.length, 5);
    for (const card of cards) {
      assert.equal(card.value, "-", `${JSON.stringify(value)} ${card.id}`);
      assert.ok(card.sub === "-" || card.id === "mood", `${card.id} ${card.sub}`);
    }
  }
  const partial = L.todayCards({ replies: { private: 1, group: 0, total: 1 } });
  assert.equal(cardOf(partial, "replies").value, "1");
  assert.equal(cardOf(partial, "songs").value, "-");
  assert.equal(cardOf(L.todayCards({ replies: { total: null } }), "replies").value, "-");
});

test("H2 todayCards: the chat card says the next time, or why it waits", () => {
  const chat = (extra) => cardOf(L.todayCards({ date: "2026-11-09", chat: { today: 2, daily_limit: 3, next_at: null, ...extra } }), "chat");
  assert.equal(chat({ reason: "Waiting", next_at: "2026-11-09T09:05" }).sub, "下次约 09:05");
  assert.equal(chat({ reason: "Waiting", next_at: "2026-11-10T09:05" }).sub, "下次约 明天 09:05");
  assert.equal(chat({ reason: "Waiting", next_at: "2026-11-12T09:05" }).sub, "下次约 11-12 09:05");
  assert.equal(chat({ reason: "Waiting" }).sub, "等待中");
  assert.equal(chat({ reason: "QuietHours", next_at: "2026-11-10T08:00" }).sub, "安静时段");
  assert.equal(chat({ reason: "DailyLimit" }).sub, "今天已发满");
  assert.equal(chat({ reason: "Disabled" }).sub, "已关闭");
  assert.equal(chat({ reason: "Whatever" }).sub, "Whatever");
  for (const reason of ["NoOwner", "Running", "UnansweredLimit", "Retry", "OutsideWindow", "Busy", "Conversing", "Gap", "T1Priority", "Ready"]) {
    assert.ok(chat({ reason }).sub && chat({ reason }).sub !== reason, reason);
  }
  assert.equal(chat({ reason: "Waiting" }).value, "2 / 3");
});

test("H2 todayCards: the mood card shows the overall mood and the mood towards the owner", () => {
  const mood = (value) => cardOf(L.todayCards({ mood: value }), "mood");
  assert.deepEqual([mood(fullToday().mood).value, mood(fullToday().mood).sub], ["开心", "还没平复 · 对主人：害羞（已平复）"]);
  const calm = { global: { emotion: "生气", calm: true }, owner: null };
  assert.deepEqual([mood(calm).value, mood(calm).sub], ["生气", "已平复 · 对主人：-"]);
  const ownerOnly = { global: null, owner: { emotion: "开心", calm: false } };
  assert.deepEqual([mood(ownerOnly).value, mood(ownerOnly).sub], ["-", "对主人：开心"]);
  assert.deepEqual([mood({ global: null, owner: null }).value, mood({ global: null, owner: null }).sub], ["-", "还没有记录"]);
});

test("H2 todayCards: the queue card", () => {
  const queue = (value) => cardOf(L.todayCards({ queue: value }), "queue");
  assert.deepEqual([queue({ active: false, waiting: 0 }).value, queue({ active: false, waiting: 0 }).sub], ["0", "空闲"]);
  assert.equal(queue({ active: true, waiting: 0 }).sub, "正在处理 1 条");
});

test("H2 nextTimeText", () => {
  assert.equal(L.nextTimeText("2026-11-09T21:30", "2026-11-09"), "21:30");
  assert.equal(L.nextTimeText("2026-11-10T00:05", "2026-11-09"), "明天 00:05");
  assert.equal(L.nextTimeText("2026-12-01T10:00", "2026-11-30"), "明天 10:00");
  assert.equal(L.nextTimeText("2026-11-20T10:00", "2026-11-09"), "11-20 10:00");
  assert.equal(L.nextTimeText("2026-11-09T21:30", null), "21:30");
  assert.equal(L.nextTimeText(null, "2026-11-09"), "");
  assert.equal(L.nextTimeText("", "2026-11-09"), "");
});

const installOverviewOf = (overrides = {}) => ({
  checks: Object.fromEntries(L.INSTALL_PATH_FIELDS.map(({ key }) => [key, { state: "valid" }])),
  extension: { state: "installed" }, plugin: { state: "installed" }, ...overrides
});

test("H2 installFold: all fine collapses to one line", () => {
  const fold = L.installFold(installOverviewOf());
  assert.deepEqual(fold, { summary: "安装正常 ✓", level: "ok", open: false });
  assert.equal(L.installFold(installOverviewOf({ plugin: { state: "unknown" } })).level, "ok");
});

test("H2 installFold: anything else opens by itself", () => {
  const notInstalled = L.installFold(installOverviewOf({ extension: { state: "missing" } }));
  assert.equal(notInstalled.level, "warn");
  assert.equal(notInstalled.open, true);
  const checks = { ...installOverviewOf().checks, st: { state: "invalid" } };
  const invalid = L.installFold(installOverviewOf({ checks }));
  assert.equal(invalid.level, "error");
  assert.equal(invalid.open, true);
  assert.match(invalid.summary, /路径无效/);
});

test("H2 installFold: an unreadable state is not a problem to open", () => {
  assert.deepEqual(L.installFold(null), { summary: "安装状态读取失败", level: "off", open: false });
  assert.equal(L.installFold(undefined).open, false);
});

test("H2 featureChips: on / off / unknown, display only", () => {
  const chips = L.featureChips({ stickers: true, schedule: false, cover: true, song_request: undefined, cleanup: false });
  assert.deepEqual(chips.map((chip) => chip.state), ["on", "off", "on", "unknown", "off"]);
  assert.deepEqual(chips.map((chip) => chip.label), L.FEATURE_LINKS.map((link) => link.label));
  for (const chip of chips) assert.ok(L.splitTarget(chip.goto).page in L.PAGE_LABELS, chip.goto);
  assert.ok(L.featureChips(null).every((chip) => chip.state === "unknown"));
  assert.ok(L.featureChips(undefined).every((chip) => chip.state === "unknown"));
});

test("H2 avatarLetter is the first character, whatever the name", () => {
  assert.equal(L.avatarLetter("琴音"), "琴");
  assert.equal(L.avatarLetter("  小铃 "), "小");
  assert.equal(L.avatarLetter("Mika"), "M");
  assert.equal(L.avatarLetter("😀好"), "😀");
  assert.equal(L.avatarLetter(""), "·");
  assert.equal(L.avatarLetter(null), "·");
});

test("H2 the chain nodes carry the facts the old cards showed as hover text", () => {
  const status = {
    bridge: { gate: "RECOVERED", last_poll_age: 5, active: false, http_running: true }, replies: { recent: ["ok"], avg_ms: 1000 },
    platform: "astrbot", connector: { connected: true, name: "astrbot", version: "2.0.0" }, onebot: null
  };
  const chain = L.healthChain({ status, failures: 0, voice: { settings: { engine: "irodori", common: { prob: 100, auto_start: true } }, service: { engine: "irodori", state: "running", service_name: "Irodori" } }, cover: { enabled: true, service: { state: "stopped" } } });
  const by = (id) => chain.find((node) => node.id === id);
  assert.equal(by("qq").detail, "AstrBot：astrbot 2.0.0 已连接");
  assert.equal(by("st").detail, "桥接：已就绪 · 最近一次轮询：5 秒前");
  assert.match(by("voice").detail, /^引擎：Irodori · 服务：Irodori · /);
  assert.equal(by("cover").detail, "RVC 服务：已停止");
  assert.equal(by("core").detail, "");
  assert.equal(L.healthChain({ status: null, failures: 2 })[0].detail, "");
});

test("H2 the page is called 角色和情绪 but keeps its id", () => {
  assert.equal(L.PAGE_LABELS.emotions, "角色和情绪");
  assert.ok(L.visiblePages().includes("emotions"));
  assert.equal(L.resolvePage("emotions"), "emotions");
  assert.equal(L.resolvePage("characters"), "emotions");
  assert.equal(L.resolvePage("audios"), "emotions");
});
