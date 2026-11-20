/**
 * threads —— 通用线程 / 会话查询客户端（跨业务、零业务词汇）。
 *
 * 服务端是会话列表与消息的唯一权威源：本客户端提供左栏列表与会话
 * 消息的读取能力，本地 IndexedDB 仅作离线兜底快照。
 *
 * 路径风格与既有 '/v1/agent/chat-stream'、'/v1/plugin/manifest' 一致：
 * 通用能力端点由核心层固定，不进插件 manifest（manifest 描述的是插件私有端点）。
 */

import { http } from './http'
import type {
  CardSnapshot,
  ChatMessageRecord,
  SessionSummary,
  TaskSummary,
  ThreadDetail,
} from '../a2ui/types'

/** 线程列表端点（相对 /api） */
export const THREADS_PATH = '/v1/threads'
/** 会话消息端点前缀（相对 /api） */
export const SESSIONS_PATH = '/v1/sessions'

/** 单线程消息端点 */
export function threadMessagesPath(threadId: string): string {
  return `${THREADS_PATH}/${encodeURIComponent(threadId)}/messages`
}

/** 整个会话的消息端点 */
export function sessionMessagesPath(sessionId: string): string {
  return `${SESSIONS_PATH}/${encodeURIComponent(sessionId)}/messages`
}

/** 线程列表查询参数 */
export interface ThreadListParams {
  /** 线程类别：all=全部；task=排除对话线程 */
  kind?: 'all' | 'task'
  /** 限定某一会话 */
  session_id?: string
  /** 是否包含已归档线程 */
  include_archived?: boolean
  /** 标题关键字（大小写不敏感的子串匹配） */
  q?: string
  limit?: number
  offset?: number
}

/** 线程列表响应 */
export interface ThreadListResponse {
  items: TaskSummary[]
  has_more: boolean
  /** 过滤后的可见条目总数 */
  total?: number
  server_time: string
}

/** 消息列表响应 */
export interface MessageListResponse {
  items: ChatMessageRecord[]
  has_more: boolean
  server_time?: string
  total?: number
}

/** 会话聚合列表响应（左栏「我的会话」） */
export interface SessionListResponse {
  items: SessionSummary[]
  has_more: boolean
  /** 过滤后的可见会话总数 */
  total?: number
  server_time: string
}

/** 线程只读详情响应 */
export interface ThreadDetailResponse {
  /** 线程元信息 */
  thread: ThreadDetail
  /** 线程内最新一张卡片（不限类型） */
  card: CardSnapshot | null
  /** 线程内每种卡片类型的最新一张 */
  cards: CardSnapshot[]
  server_time: string
}

/** 拉取按 session_id 聚合的可见会话列表。 */
export async function fetchSessions(
  params: { q?: string; limit?: number; offset?: number } = {},
): Promise<SessionListResponse> {
  const { data } = await http.get<SessionListResponse>(SESSIONS_PATH, { params })
  return data
}

/** 拉取线程只读详情（线程元信息 + 卡片快照）。 */
export async function fetchThreadDetail(
  threadId: string,
): Promise<ThreadDetailResponse> {
  const { data } = await http.get<ThreadDetailResponse>(
    `${THREADS_PATH}/${encodeURIComponent(threadId)}`,
  )
  return data
}

/** 拉取当前用户可见的线程列表。 */
export async function fetchThreads(
  params: ThreadListParams = {},
): Promise<ThreadListResponse> {
  const { data } = await http.get<ThreadListResponse>(THREADS_PATH, { params })
  return data
}

/** 拉取单线程的消息与卡片快照。 */
export async function fetchThreadMessages(
  threadId: string,
  params: { limit?: number; offset?: number; order?: 'asc' | 'desc' } = {},
): Promise<MessageListResponse> {
  const { data } = await http.get<MessageListResponse>(
    threadMessagesPath(threadId),
    { params },
  )
  return data
}

/** 拉取整个会话的消息（跨线程全序，按 seq 排序）。 */
export async function fetchSessionMessages(
  sessionId: string,
  params: { limit?: number; offset?: number } = {},
): Promise<MessageListResponse> {
  const { data } = await http.get<MessageListResponse>(
    sessionMessagesPath(sessionId),
    { params },
  )
  return data
}
