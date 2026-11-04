#!/usr/bin/env node
// kimi-quota MCP server — 供「Kimi 额度」侧栏面板与模型工具使用
// 纯 Node 无依赖：stdio JSON-RPC + Kimi 用量接口轮询 + ui:// 资源与订阅推送。
// stdout 只输出协议消息；诊断日志一律走 stderr，不记录 API Key。
import { readFile, writeFile, mkdir } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import path from "node:path";
import os from "node:os";

const SERVER_NAME = "kimi-quota";
const SERVER_VERSION = "0.2.0";
const SUPPORTED_PROTOCOL_VERSIONS = ["2025-03-26", "2025-06-18", "2025-11-25"];
const DEFAULT_PROTOCOL_VERSION = "2025-06-18";

const PLUGIN_ROOT = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const PANEL_PATH = path.join(PLUGIN_ROOT, "server", "panel.html");
const ZCODE_DIR = path.join(os.homedir(), ".zcode");
const PROVIDER_CONFIG = path.join(ZCODE_DIR, "v2", "provider_config.json");
const STATE_DIR = process.env.ZCODE_PLUGIN_DATA || path.join(os.homedir(), ".cache", "kimi-quota");
const STATE_FILE = path.join(STATE_DIR, "panel-state.json");
const USAGE_URL_DEFAULT = "https://api.kimi.com/coding/v1/usages";
const FETCH_TIMEOUT_MS = 8000;
const MIN_FETCH_INTERVAL_MS = 60_000; // 与 hook 的重试节流一致
const SUBSCRIBER_POLL_MS = 20_000;

const subscribers = new Set(); // uri -> Set(requestId?)，只关心 uri 集合
let panelHtmlCache = null;
let snapshot = null; // { ok, fetchedAt, fiveHour, monthly, stale, error }
let lastFetchAt = 0;
let fetching = null; // 进行中的 Promise，避免并发重复请求

const log = (...args) => process.stderr.write(`[kimi-quota] ${args.join(" ")}\n`);

// ---------- 配置 ----------

function usageUrl() {
  const url = process.env.KIMI_QUOTA_URL || USAGE_URL_DEFAULT;
  // 覆盖地址仅允许官方域名的 HTTPS，防止密钥被发往第三方（与 hook 白名单一致）
  return /^https:\/\/api\.kimi\.com\/.+|^https:\/\/api\.kimi\.ai\/.+/.test(url)
    ? url
    : USAGE_URL_DEFAULT;
}

async function apiKey() {
  if (process.env.KIMI_API_KEY) return process.env.KIMI_API_KEY;
  try {
    const raw = await readFile(PROVIDER_CONFIG, "utf8");
    const config = JSON.parse(raw);
    const rules = config?.config?.providerConfigRules?.providerRules;
    if (!Array.isArray(rules)) return null;
    const rule = rules.find((item) => item?.providerId === "moonshot-kimi");
    const key = rule?.config?.access?.apiKey;
    return typeof key === "string" && key ? key : null;
  } catch {
    return null;
  }
}

// ---------- Kimi 用量解析（移植自 hooks/kimi_quota.sh 的 build_line） ----------

function finiteNumber(value) {
  const num = typeof value === "number" ? value : typeof value === "string" ? Number(value) : NaN;
  return Number.isFinite(num) ? num : null;
}

function parseFiveHour(payload) {
  const detail = Array.isArray(payload?.limits)
    ? payload.limits.find((item) => item?.window?.duration === 300)?.detail
    : undefined;
  if (detail && typeof detail === "object") {
    const limit = finiteNumber(detail.limit ?? null);
    const used = finiteNumber(detail.used ?? null);
    const remaining = finiteNumber(detail.remaining ?? null);
    const usedValue = used ?? (limit !== null && remaining !== null ? limit - remaining : null);
    const resetTime = detail.resetTime ?? detail.reset_time ?? null;
    if (limit !== null && limit > 0 && usedValue !== null) {
      return { usedRatio: usedValue / limit, used: usedValue, limit, resetTime };
    }
  }
  const pool = payload?.usages?.limit_5h;
  const ratio = finiteNumber(pool?.used_ratio ?? null);
  if (ratio !== null) {
    return { usedRatio: ratio, used: null, limit: null, resetTime: pool?.reset_time ?? pool?.resetTime ?? null };
  }
  return null;
}

function parseMonthly(payload) {
  const pool = payload?.usages?.limit_month_total ?? payload?.usages?.limit_month_code ?? null;
  if (!pool || typeof pool !== "object") return null;
  const ratio = finiteNumber(pool.used_ratio ?? null);
  if (ratio === null) return null;
  return { usedRatio: ratio, used: null, limit: null, resetTime: pool.reset_time ?? pool.resetTime ?? null };
}

function buildSnapshot(payload, fetchedAt) {
  const fiveHour = parseFiveHour(payload);
  const monthly = parseMonthly(payload);
  if (!fiveHour && !monthly) return null;
  return {
    ok: true,
    fetchedAt: new Date(fetchedAt).toISOString(),
    fiveHour,
    monthly,
    stale: false,
    error: null,
  };
}

function errorSnapshot(message, fetchedAt = Date.now()) {
  return { ok: false, fetchedAt: new Date(fetchedAt).toISOString(), fiveHour: null, monthly: null, stale: true, error: message };
}

// ---------- 拉取与缓存 ----------

async function fetchUsage() {
  const key = await apiKey();
  if (!key) throw new Error("无法获取 Kimi API Key（检查环境变量 KIMI_API_KEY 或 ZCode 供应商配置）");
  const response = await fetch(usageUrl(), {
    headers: { Authorization: `Bearer ${key}`, "User-Agent": "KimiCLI/1.6" },
    signal: AbortSignal.timeout(FETCH_TIMEOUT_MS),
  });
  if (!response.ok) throw new Error(`Kimi 用量接口返回 ${response.status}`);
  const text = await response.text();
  try {
    return JSON.parse(text);
  } catch {
    throw new Error("Kimi 用量接口返回了无法解析的内容");
  }
}

async function persistSnapshot(next) {
  try {
    await mkdir(STATE_DIR, { recursive: true });
    await writeFile(STATE_FILE, JSON.stringify(next), { mode: 0o600 });
  } catch (error) {
    log(`写入状态缓存失败: ${error?.message ?? error}`);
  }
}

async function refresh({ force = false } = {}) {
  const now = Date.now();
  // 成功数据 60 秒内复用；失败快照不节流，面板可立即重试
  if (!force && snapshot?.ok && now - lastFetchAt < MIN_FETCH_INTERVAL_MS) return snapshot;
  if (fetching) return fetching;
  lastFetchAt = now;
  fetching = (async () => {
    try {
      const payload = await fetchUsage();
      const next = buildSnapshot(payload, Date.now());
      if (!next) {
        // 与 hook 一致：解析失败保留原数据并记录诊断，不覆盖可用快照
        log("用量响应缺少可解析的额度字段");
        const degraded = { ...(snapshot ?? errorSnapshot("用量响应缺少可解析的额度字段")), stale: true };
        return degraded;
      }
      snapshot = next;
      await persistSnapshot(next);
      notifyResourceChanged("ui://kimi-quota/quota.json");
      return snapshot;
    } catch (error) {
      log(`刷新失败: ${error?.message ?? error}`);
      if (snapshot) {
        snapshot = { ...snapshot, stale: true, error: error?.message ?? String(error) };
      } else {
        snapshot = errorSnapshot(error?.message ?? String(error));
      }
      return snapshot;
    } finally {
      fetching = null;
    }
  })();
  return fetching;
}

async function loadPersistedSnapshot() {
  try {
    const raw = await readFile(STATE_FILE, "utf8");
    const saved = JSON.parse(raw);
    if (saved && typeof saved === "object" && "ok" in saved) {
      snapshot = { ...saved, stale: true };
      lastFetchAt = Date.parse(saved.fetchedAt) || 0;
    }
  } catch {
    // 无缓存属正常首次启动
  }
}

// 面板打开期间保持后台刷新：有订阅者且缓存到期时拉取
setInterval(() => {
  if (subscribers.size === 0) return;
  if (Date.now() - lastFetchAt >= MIN_FETCH_INTERVAL_MS) void refresh();
}, SUBSCRIBER_POLL_MS);

// ---------- MCP stdio ----------

function sendMessage(message) {
  process.stdout.write(`${JSON.stringify(message)}\n`);
}

function reply(id, result) {
  sendMessage({ jsonrpc: "2.0", id, result });
}

function replyError(id, code, message) {
  sendMessage({ jsonrpc: "2.0", id, error: { code, message } });
}

function notifyResourceChanged(uri) {
  if (subscribers.has(uri)) {
    sendMessage({ jsonrpc: "2.0", method: "notifications/resources/updated", params: { uri } });
  }
}

function quotaText(snap) {
  if (!snap?.ok) return `Kimi 额度查询失败：${snap?.error ?? "未知错误"}`;
  const pct = (ratio) => (ratio === null || ratio === undefined ? "—" : `${Math.round(ratio * 1000) / 10}%`);
  const local = (iso) => {
    if (!iso) return "—";
    const date = new Date(iso);
    return Number.isNaN(date.getTime()) ? "—" : date.toLocaleString("zh-CN", { hour12: false });
  };
  const five = snap.fiveHour ? `5h ${pct(snap.fiveHour.usedRatio)}` : "5h —";
  const month = snap.monthly ? `月 ${pct(snap.monthly.usedRatio)}` : "月 —";
  const fiveReset = snap.fiveHour?.resetTime ? local(snap.fiveHour.resetTime) : "—";
  const monthReset = snap.monthly?.resetTime ? local(snap.monthly.resetTime) : "—";
  return `Kimi 已用 · ${five} · ${month} · 重置 5h ${fiveReset} · 月 ${monthReset}`;
}

async function readPanelHtml() {
  if (panelHtmlCache) return panelHtmlCache;
  panelHtmlCache = await readFile(PANEL_PATH, "utf8");
  return panelHtmlCache;
}

const methods = {
  async initialize(id, params) {
    const requested = params?.protocolVersion;
    reply(id, {
      protocolVersion: SUPPORTED_PROTOCOL_VERSIONS.includes(requested) ? requested : DEFAULT_PROTOCOL_VERSION,
      capabilities: {
        tools: { listChanged: false },
        resources: { subscribe: true, listChanged: false },
      },
      serverInfo: { name: SERVER_NAME, version: SERVER_VERSION },
    });
  },

  async "tools/list"(id) {
    reply(id, {
      tools: [
        {
          name: "get_quota",
          description: "查询 Kimi 5 小时窗口与月度已用额度比例及重置时间，返回文本摘要和结构化数据",
          inputSchema: { type: "object", properties: {}, additionalProperties: false },
        },
      ],
    });
  },

  async "tools/call"(id, params) {
    if (params?.name !== "get_quota") {
      replyError(id, -32602, `未知工具: ${params?.name}`);
      return;
    }
    const snap = await refresh();
    reply(id, {
      content: [{ type: "text", text: quotaText(snap) }],
      structuredContent: snap,
    });
  },

  async "resources/list"(id) {
    reply(id, {
      resources: [
        {
          uri: "ui://kimi-quota/panel.html",
          name: "Kimi 额度面板",
          description: "Kimi 5 小时/月度额度状态面板",
          mimeType: "text/html;profile=mcp-app",
        },
        {
          uri: "ui://kimi-quota/quota.json",
          name: "Kimi 额度快照",
          description: "当前额度快照 JSON，支持订阅更新",
          mimeType: "application/json",
        },
      ],
    });
  },

  async "resources/read"(id, params) {
    const uri = params?.uri;
    if (uri === "ui://kimi-quota/panel.html") {
      reply(id, {
        contents: [{ uri, mimeType: "text/html;profile=mcp-app", text: await readPanelHtml() }],
      });
      return;
    }
    if (uri === "ui://kimi-quota/quota.json") {
      const snap = await refresh();
      reply(id, {
        contents: [{ uri, mimeType: "application/json", text: JSON.stringify(snap) }],
      });
      return;
    }
    replyError(id, -32602, `未知资源: ${uri}`);
  },

  async "resources/subscribe"(id, params) {
    const uri = params?.uri;
    if (uri !== "ui://kimi-quota/quota.json" && uri !== "ui://kimi-quota/panel.html") {
      replyError(id, -32602, `未知资源: ${uri}`);
      return;
    }
    subscribers.add(uri);
    reply(id, {});
  },

  async "resources/unsubscribe"(id, params) {
    subscribers.delete(params?.uri);
    reply(id, {});
  },

  async ping(id) {
    reply(id, {});
  },
};

let buffer = "";
process.stdin.setEncoding("utf8");
process.stdin.on("data", (chunk) => {
  buffer += chunk;
  let index;
  while ((index = buffer.indexOf("\n")) >= 0) {
    const line = buffer.slice(0, index).trim();
    buffer = buffer.slice(index + 1);
    if (!line) continue;
    handleLine(line).catch((error) => log(`处理消息异常: ${error?.message ?? error}`));
  }
});

async function handleLine(line) {
  let message;
  try {
    message = JSON.parse(line);
  } catch {
    log("收到无法解析的消息");
    return;
  }
  if (message?.id === undefined || message?.id === null) return; // 通知（含 initialized）无需应答
  const handler = methods[message.method];
  if (!handler) {
    replyError(message.id, -32601, `未知方法: ${message.method}`);
    return;
  }
  inFlight++;
  try {
    await handler(message.id, message.params);
  } finally {
    inFlight--;
    maybeExit();
  }
}

process.on("uncaughtException", (error) => log(`未捕获异常: ${error?.message ?? error}`));
process.on("unhandledRejection", (reason) => log(`未处理的拒绝: ${reason}`));
// MCP stdio:客户端关闭 stdin 即会话结束。等在途请求应答落地再退出;
// 网络挂起等异常情况下 5 秒硬退出,避免遗留孤儿进程。
let inFlight = 0;
let stdinClosed = false;
function maybeExit() {
  if (stdinClosed && inFlight <= 0) process.exit(0);
}
process.stdin.on("end", () => {
  stdinClosed = true;
  maybeExit();
  setTimeout(() => process.exit(0), 5000).unref();
});
process.stdin.on("close", () => {
  stdinClosed = true;
  maybeExit();
});
process.stdin.on("error", () => process.exit(0));

await loadPersistedSnapshot();
log(`server 就绪 root=${PLUGIN_ROOT} state=${STATE_FILE}`);
