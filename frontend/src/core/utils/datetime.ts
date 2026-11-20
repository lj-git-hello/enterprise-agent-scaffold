/**
 * datetime —— 通用时间格式化（跨业务、零业务词汇）。
 *
 * 服务端统一返回 ISO 时间串；前端只做展示层格式化，不做业务语义判断。
 */

/** 时间缺字段时的占位符（不静默显示空值） */
export const EMPTY_TIME_PLACEHOLDER = '—'

/**
 * 格式化服务端 ISO 时间串为「MM-DD HH:mm」。
 *
 * 缺省或非法值一律返回占位符：宁可显式留白，也不显示误导性的 1970 年。
 */
export function formatDateTime(iso?: string | null): string {
  if (!iso) return EMPTY_TIME_PLACEHOLDER
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return EMPTY_TIME_PLACEHOLDER
  const pad = (value: number): string => String(value).padStart(2, '0')
  return `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`
}
