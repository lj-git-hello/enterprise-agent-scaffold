/**
 * uuid —— 通用 UUID 生成（跨业务、零业务词汇）。
 *
 * `crypto.randomUUID` 仅在「安全上下文」（HTTPS / localhost）可用：
 * 线上以纯 HTTP 明文公网 IP 访问时，浏览器不处于安全上下文，
 * 该 API 根本不存在，直接调用会抛 `crypto.randomUUID is not a function`
 * 并在模块初始化阶段导致整页白屏。
 *
 * 这里按可用性逐级降级，保证任何环境下都能拿到标准 RFC 4122 v4 字符串：
 * 1) 原生 `crypto.randomUUID`
 * 2) `crypto.getRandomValues`（**不受**安全上下文限制，非安全上下文可用）
 * 3) `Math.random()` 兜底（极老浏览器 / 无 Web Crypto 环境）
 */

/**
 * 运行时探测 Web Crypto 是否可用。
 *
 * `crypto` 在 DOM 类型定义中被声明为非可选全局变量，直接写
 * `typeof crypto !== 'undefined'` 属于「与声明类型恒等」的比较，
 * 在严格模式下有触发类型报错的风险。此处统一取值并把
 * 「可能不存在」显式建模，运行时语义与上述判断完全等价。
 */
function resolveCrypto(): Crypto | undefined {
  return (globalThis as { crypto?: Crypto }).crypto
}

/**
 * 把 16 字节随机数按 RFC 4122 v4 位掩码拼成标准 UUID 字符串。
 *
 * 位掩码保证版本位固定为 4、变体位固定为 RFC 4122 规定的 `10xx`，
 * 使降级路径产出的字符串与原生 `randomUUID` 完全同构。
 */
function formatV4(bytes: Uint8Array): string {
  // version 位：第 6 字节高 4 位固定为 0100（v4）
  bytes[6] = (bytes[6] & 0x0f) | 0x40
  // variant 位：第 9 字节高 2 位固定为 10（RFC 4122）
  bytes[8] = (bytes[8] & 0x3f) | 0x80

  let out = ''
  for (let i = 0; i < bytes.length; i += 1) {
    out += bytes[i].toString(16).padStart(2, '0')
    // 8-4-4-4-12 分段
    if (i === 3 || i === 5 || i === 7 || i === 9) out += '-'
  }
  return out
}

/**
 * 生成一个 RFC 4122 v4 版本的 UUID 字符串。
 *
 * 环境降级顺序：原生 `randomUUID` → `getRandomValues` → `Math.random`。
 * 三条路径产出的字符串格式完全一致，调用方无需感知运行环境差异。
 */
export function generateUUID(): string {
  const cryptoRef = resolveCrypto()

  // 首选原生实现：现代浏览器 + 安全上下文
  if (cryptoRef && typeof cryptoRef.randomUUID === 'function') {
    return cryptoRef.randomUUID()
  }

  const bytes = new Uint8Array(16)

  if (cryptoRef && typeof cryptoRef.getRandomValues === 'function') {
    // 次选：Web Crypto 随机源（非安全上下文下依然可用）
    cryptoRef.getRandomValues(bytes)
  } else {
    // 兜底：无 Web Crypto 时的最后手段
    for (let i = 0; i < bytes.length; i += 1) {
      bytes[i] = Math.floor(Math.random() * 256)
    }
  }

  return formatV4(bytes)
}
