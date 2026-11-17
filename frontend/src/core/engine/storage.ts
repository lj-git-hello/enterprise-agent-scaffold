/**
 * Storage —— 基于 localforage(IndexedDB) 的**离线兜底快照**。
 *
 * 权威源已收敛到服务端：本模块只保存"服务端快照的本地副本"，
 * 服务端不可达时用于兜底展示，随时可丢。
 *
 * v2 相对 v1 的变化：
 * - 键位按用户分区（`session:<userId>:<sessionId>`），换身份/登出可整区清理；
 * - 信封携带 schema 版本、写入时间与来源，供 TTL / LRU 淘汰使用；
 * - 容量上限 + TTL，避免无界增长；
 * - 写入前剔除敏感字段（签名/令牌/口令等），本地不留存凭据材料；
 * - 配额溢出时淘汰最旧快照重试，失败则显式置为不可写（不再假装健康）。
 */

import localforage from 'localforage'
import { isReactive, reactive, toRaw } from 'vue'

/** 当前快照 schema 版本 */
export const SCHEMA_VERSION = 2

/** 快照来源：服务端权威副本 / 本地缓存 */
export type SnapshotSource = 'server' | 'cache'

/** 快照信封 */
export interface StoredEnvelope<T> {
  /** schema 版本 */
  v: number
  /** 写入时间戳（毫秒） */
  updatedAt: number
  /** 来源标记 */
  source: SnapshotSource
  /** 负载 */
  data: T
}

/** 防抖持久化延迟（毫秒） */
const SAVE_DEBOUNCE_MS = 1000

/** 快照保留时长（毫秒）：7 天，超期视为无缓存 */
export const SNAPSHOT_TTL_MS = 7 * 24 * 60 * 60 * 1000
/** 每用户保留的会话快照上限 */
export const MAX_SESSIONS_PER_USER = 30
/** 单会话保留的消息条数上限 */
export const MAX_MESSAGES_PER_SESSION = 300
/** 会话集合条目上限 */
export const MAX_CONVERSATIONS = 200

/** 敏感键名匹配（核心级通用规则，业务特有字段由插件脱敏器补充） */
export const SENSITIVE_KEY_PATTERN =
  /(signat|signature|token|secret|passwo?rd|credential|api[_-]?key|private[_-]?key|nonce|salt|certificate)/i

/** 专用 localforage 实例，与其它本地存储隔离 */
const messageStore = localforage.createInstance({
  name: 'agent-scaffold',
  storeName: 'messages',
  description: '会话消息离线兜底快照',
})

/**
 * 存储健康态（响应式）。
 *
 * 不再"无条件置健康"：写入失败（配额/驱动异常）必须显式暴露，
 * 否则"刷新后数据消失"会变成无头案。
 */
export const storageHealth = reactive({
  /** 是否可写 */
  writable: true,
  /** 最近一次错误描述 */
  lastError: null as string | null,
  /** 最近一次错误发生时间戳 */
  lastErrorAt: 0,
})

function markHealthy(): void {
  storageHealth.writable = true
  storageHealth.lastError = null
}

function markUnhealthy(error: unknown): void {
  storageHealth.writable = false
  storageHealth.lastError = error instanceof Error ? error.message : String(error)
  storageHealth.lastErrorAt = Date.now()
}

/** 判断是否命中浏览器存储配额上限。 */
export function isQuotaExceeded(error: unknown): boolean {
  if (!error || typeof error !== 'object') return false
  const detail = error as { name?: string; code?: number }
  return (
    detail.name === 'QuotaExceededError' ||
    detail.code === 22 ||
    detail.code === 1014
  )
}

/**
 * 深度净化：剥离 Vue 响应式代理 + 剔除敏感字段。
 *
 * IndexedDB 使用结构化克隆（structured clone），遇到 Proxy 会抛
 * DataCloneError，因此落盘前必须剥离响应式代理。
 */
export function sanitizeValue<T>(value: T): T {
  if (Array.isArray(value)) {
    return value.map((item) => sanitizeValue(item)) as unknown as T
  }
  if (value !== null && typeof value === 'object') {
    const raw = isReactive(value) ? toRaw(value) : value
    const result: Record<string, unknown> = {}
    for (const [key, item] of Object.entries(raw as Record<string, unknown>)) {
      if (SENSITIVE_KEY_PATTERN.test(key)) continue
      result[key] = sanitizeValue(item)
    }
    return result as T
  }
  return value
}

function sessionKeyPrefix(userId: string): string {
  return `session:${userId}:`
}

function sessionKey(userId: string, sessionId: string): string {
  return `${sessionKeyPrefix(userId)}${sessionId}`
}

function conversationsKey(userId: string): string {
  return `conversations:${userId}`
}

/** v1 信封形状（{updatedAt, messages}）判定。 */
function isLegacyEnvelope(
  value: unknown,
): value is { updatedAt: number; messages: unknown } {
  if (!value || typeof value !== 'object') return false
  const legacy = value as { v?: unknown; updatedAt?: unknown; messages?: unknown }
  if (typeof legacy.v === 'number') return false
  return typeof legacy.updatedAt === 'number' && 'messages' in legacy
}

/**
 * v1 → v2 迁移（模块级记忆化，公开 API 前置等待）。
 *
 * - v1 会话键 `session:<sessionId>` 无用户分区，无法安全归属 → 直接丢弃
 *   （服务端已成权威源，旧会话快照无回退价值）；
 * - v1 会话集合 `conversations:<userId>` 的信封补齐为 v2 结构。
 */
const migrationPromise: Promise<void> = migrateToV2()

async function migrateToV2(): Promise<void> {
  try {
    const allKeys = await messageStore.keys()
    for (const key of allKeys) {
      if (typeof key !== 'string') continue
      if (key.startsWith('session:')) {
        const rest = key.slice('session:'.length)
        // v2 键含用户分区（形如 `session:<userId>:<sessionId>`），保留；
        // v1 键无用户分区，丢弃。
        if (!rest.includes(':')) {
          await messageStore.removeItem(key)
        }
        continue
      }
      if (key.startsWith('conversations:')) {
        const legacy = await messageStore.getItem<unknown>(key)
        if (isLegacyEnvelope(legacy)) {
          await messageStore.setItem(key, {
            v: SCHEMA_VERSION,
            updatedAt: legacy.updatedAt,
            source: 'cache',
            data: legacy.messages,
          })
        }
      }
    }
  } catch (error) {
    markUnhealthy(error)
  }
}

/** 会话快照元信息（用于 TTL / LRU 淘汰） */
interface SessionMeta {
  key: string
  updatedAt: number
}

/** 收集某用户的会话快照元信息（按写入时间倒序）。 */
async function collectUserSessionMetas(userId: string): Promise<SessionMeta[]> {
  const prefix = sessionKeyPrefix(userId)
  const allKeys = await messageStore.keys()
  const metas: SessionMeta[] = []
  for (const key of allKeys) {
    if (typeof key !== 'string' || !key.startsWith(prefix)) continue
    const envelope = await messageStore.getItem<StoredEnvelope<unknown>>(key)
    metas.push({ key, updatedAt: envelope?.updatedAt ?? 0 })
  }
  metas.sort((a, b) => b.updatedAt - a.updatedAt)
  return metas
}

/** 淘汰全局最旧的会话快照（配额兜底时调用）。 */
async function evictOldestSession(): Promise<boolean> {
  const allKeys = await messageStore.keys()
  const sessionKeys = allKeys.filter(
    (key): key is string => typeof key === 'string' && key.startsWith('session:'),
  )
  if (sessionKeys.length === 0) return false
  let oldestKey = sessionKeys[0]
  let oldestAt = Number.POSITIVE_INFINITY
  for (const key of sessionKeys) {
    const envelope = await messageStore.getItem<StoredEnvelope<unknown>>(key)
    const at = envelope?.updatedAt ?? 0
    if (at < oldestAt) {
      oldestAt = at
      oldestKey = key
    }
  }
  await messageStore.removeItem(oldestKey)
  return true
}

/** 清理超出 TTL 的过期快照（写入前调用：过期数据不应继续占用空间）。 */
async function purgeExpiredSessions(userId: string): Promise<void> {
  const metas = await collectUserSessionMetas(userId)
  const now = Date.now()
  for (const meta of metas) {
    if (now - meta.updatedAt > SNAPSHOT_TTL_MS) {
      await messageStore.removeItem(meta.key)
    }
  }
}

/**
 * 执行容量淘汰（写入后调用）。
 *
 * 新写入的快照时间戳最新，必定排在首位，因此不会被淘汰；
 * 淘汰发生在写入之后，最终条数恒定 ≤ MAX_SESSIONS_PER_USER。
 */
async function enforceSessionCapacity(userId: string): Promise<void> {
  const metas = await collectUserSessionMetas(userId)
  // metas 已按写入时间倒序 → 超出上限的即为最旧
  for (const meta of metas.slice(MAX_SESSIONS_PER_USER)) {
    await messageStore.removeItem(meta.key)
  }
}

/** 安全写入：配额溢出时淘汰最旧快照重试一次，失败则标记不可写。 */
async function safeSetItem(
  key: string,
  envelope: StoredEnvelope<unknown>,
): Promise<boolean> {
  try {
    await messageStore.setItem(key, envelope)
    markHealthy()
    return true
  } catch (error) {
    if (isQuotaExceeded(error)) {
      try {
        const evicted = await evictOldestSession()
        if (evicted) {
          await messageStore.setItem(key, envelope)
          markHealthy()
          return true
        }
      } catch (retryError) {
        markUnhealthy(retryError)
        return false
      }
    }
    markUnhealthy(error)
    return false
  }
}

/** 截断消息数组（保留最新若干条）。 */
function truncateMessages<T>(messages: T[]): T[] {
  if (messages.length <= MAX_MESSAGES_PER_SESSION) return messages
  return messages.slice(messages.length - MAX_MESSAGES_PER_SESSION)
}

// ---------------------------------------------------------------------------
// 会话消息快照
// ---------------------------------------------------------------------------

/** 立即保存某会话的消息快照。 */
export async function saveMessages<T>(
  userId: string,
  sessionId: string,
  messages: T[],
): Promise<T[]> {
  await migrationPromise
  const envelope: StoredEnvelope<T[]> = {
    v: SCHEMA_VERSION,
    updatedAt: Date.now(),
    source: 'cache',
    data: truncateMessages(sanitizeValue(messages)),
  }
  await purgeExpiredSessions(userId)
  await safeSetItem(sessionKey(userId, sessionId), envelope)
  await enforceSessionCapacity(userId)
  return messages
}

/** 读取某会话的消息快照；不存在或已超 TTL 时返回 null。 */
export async function loadMessages<T>(
  userId: string,
  sessionId: string,
): Promise<StoredEnvelope<T[]> | null> {
  await migrationPromise
  const key = sessionKey(userId, sessionId)
  const envelope = await messageStore.getItem<StoredEnvelope<T[]>>(key)
  if (!envelope) return null
  if (Date.now() - envelope.updatedAt > SNAPSHOT_TTL_MS) {
    await messageStore.removeItem(key)
    return null
  }
  return envelope
}

/** 清除某会话的本地快照。 */
export async function clearSession(
  userId: string,
  sessionId: string,
): Promise<void> {
  await migrationPromise
  await messageStore.removeItem(sessionKey(userId, sessionId))
}

/** 列出某用户的会话快照键（按写入时间倒序）。 */
export async function listUserSessionKeys(userId: string): Promise<string[]> {
  await migrationPromise
  const metas = await collectUserSessionMetas(userId)
  return metas.map((meta) => meta.key)
}

// ---------------------------------------------------------------------------
// 会话 / 工单集合（左栏【我的会话】），按用户隔离持久化
// ---------------------------------------------------------------------------

/** 保存某用户的会话集合快照。 */
export async function saveConversations<T>(
  userId: string,
  list: T[],
): Promise<T[]> {
  await migrationPromise
  const capped = list.length > MAX_CONVERSATIONS ? list.slice(0, MAX_CONVERSATIONS) : list
  const envelope: StoredEnvelope<T[]> = {
    v: SCHEMA_VERSION,
    updatedAt: Date.now(),
    source: 'cache',
    data: sanitizeValue(capped),
  }
  await safeSetItem(conversationsKey(userId), envelope)
  return list
}

/** 读取某用户的会话集合快照；不存在或已超 TTL 时返回 null。 */
export async function loadConversations<T>(
  userId: string,
): Promise<StoredEnvelope<T[]> | null> {
  await migrationPromise
  const key = conversationsKey(userId)
  const envelope = await messageStore.getItem<StoredEnvelope<T[]>>(key)
  if (!envelope) return null
  if (Date.now() - envelope.updatedAt > SNAPSHOT_TTL_MS) {
    await messageStore.removeItem(key)
    return null
  }
  return envelope
}

/**
 * 取消某用户尚未落盘的防抖写入。
 *
 * 清理分区前必须调用：否则清理完成后，排队中的定时器会把数据重新写回，
 * 造成"已登出/已切换身份，本机却仍残留该身份数据"。
 */
export function cancelPendingSaves(userId: string): void {
  const convTimer = conversationTimers.get(userId)
  if (convTimer) {
    clearTimeout(convTimer)
    conversationTimers.delete(userId)
  }
  for (const [key, timer] of debounceTimers) {
    if (key.startsWith(`${userId}:`)) {
      clearTimeout(timer)
      debounceTimers.delete(key)
    }
  }
}

/**
 * 清除某用户的整个本地分区（登出 / 切换身份时调用）。
 *
 * 共享终端场景下，前一身份的数据不得残留给下一身份。
 */
export async function clearUserPartition(userId: string): Promise<void> {
  await migrationPromise
  // 先掐掉排队中的落盘定时器，否则清理后会被写回
  cancelPendingSaves(userId)
  const metas = await collectUserSessionMetas(userId)
  for (const meta of metas) {
    await messageStore.removeItem(meta.key)
  }
  await messageStore.removeItem(conversationsKey(userId))
}

/** 清空全部本地快照。 */
export async function clearAll(): Promise<void> {
  await migrationPromise
  const allKeys = await messageStore.keys()
  for (const key of allKeys) {
    if (typeof key === 'string') {
      await messageStore.removeItem(key)
    }
  }
  markHealthy()
}

/** 探测本地存储可用性（写入 + 读回 + 清理）。 */
export async function probeHealth(): Promise<boolean> {
  await migrationPromise
  const probeKey = '__health_probe__'
  try {
    await messageStore.setItem(probeKey, {
      v: SCHEMA_VERSION,
      updatedAt: Date.now(),
      source: 'cache',
      data: true,
    })
    const echoed = await messageStore.getItem<StoredEnvelope<boolean>>(probeKey)
    await messageStore.removeItem(probeKey)
    const ok = echoed?.data === true
    if (ok) markHealthy()
    else markUnhealthy(new Error('本地存储读写校验不一致'))
    return ok
  } catch (error) {
    markUnhealthy(error)
    return false
  }
}

// ---------------------------------------------------------------------------
// 防抖持久化
// ---------------------------------------------------------------------------

/** 集合持久化防抖（按用户隔离）。 */
const conversationTimers = new Map<string, ReturnType<typeof setTimeout>>()

export function debounceSaveConversations<T>(
  userId: string,
  list: T[],
): void {
  const existingTimer = conversationTimers.get(userId)
  if (existingTimer) clearTimeout(existingTimer)

  const timer = setTimeout(() => {
    conversationTimers.delete(userId)
    // 落盘失败必须显式可见：静默失败会让"刷新后数据消失"变成无头案
    void saveConversations(userId, list).catch((error: unknown) => {
      console.error('[本地快照] 会话集合写入失败：', error)
    })
  }, SAVE_DEBOUNCE_MS)

  conversationTimers.set(userId, timer)
}

/** 已注册的防抖计时器（按会话隔离） */
const debounceTimers = new Map<string, ReturnType<typeof setTimeout>>()

/**
 * 防抖持久化：在最后一次调用后等待 1000ms 再真正写入。
 * 推流期间连续调用会不断重置计时器，done 后落盘一次。
 */
export function debounceSaveMessages<T>(
  userId: string,
  sessionId: string,
  messages: T[],
): void {
  const key = `${userId}:${sessionId}`
  const existingTimer = debounceTimers.get(key)
  if (existingTimer) clearTimeout(existingTimer)

  const timer = setTimeout(() => {
    debounceTimers.delete(key)
    // 落盘失败必须显式可见：静默失败会让"刷新后数据消失"变成无头案
    void saveMessages(userId, sessionId, messages).catch((error: unknown) => {
      console.error('[本地快照] 消息写入失败：', error)
    })
  }, SAVE_DEBOUNCE_MS)

  debounceTimers.set(key, timer)
}
