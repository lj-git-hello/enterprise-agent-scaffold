/**
 * PluginRegistry —— 前端业务插件注册中枢（单例，零业务词汇）。
 *
 * 各业务插件在 src/plugins/<biz>/plugin.ts 中声明自身清单，
 * 由 main.ts 通过 import.meta.glob 自动装载并注册；通用层据此解析
 * 端点、卡片与表单组件，实现"新增业务 = 新增一个插件目录"，底座零改动。
 *
 * 端点以**后端 manifest 为唯一真相源**：启动时拉取 /api/v1/plugin/manifest，
 * 按插件名匹配后覆盖本地默认值（applyManifestEndpoints）。
 */

import { computed, ref } from 'vue'
import type { Component } from 'vue'

import { CardRegistry } from '../a2ui/CardRegistry'
import type { WorkflowEndpoints } from '../a2ui/types'

/** 插件声明的演示身份（通用外壳身份切换器的数据源） */
export interface PluginIdentity {
  /** 登录账号 ID */
  id: string
  /** 展示名 */
  label: string
  /** 角色标识（语义由插件解释） */
  role: string
}

/** 右栏表单分区标题的默认文案（插件未声明时的中性回落） */
export const DEFAULT_FORM_SECTION_TITLE = '业务表单'

/** 左栏关联计数单位文案的默认文案（插件未声明时的中性回落） */
export const DEFAULT_SESSION_COUNT_LABEL = '关联'

/**
 * 卡片载荷脱敏器：剔除不应落入本地存储的敏感字段。
 *
 * 业务特有字段（签名摘要、许可编号等）的判定规则归属插件，
 * 核心层只负责在落盘前调用插件提供的脱敏器。
 */
export type CardPayloadSanitizer = (
  payload: Record<string, unknown>,
) => Record<string, unknown>

/** 插件清单（业务插件自描述） */
export interface PluginDefinition {
  /** 插件唯一名（须与后端 manifest 的 name 一致） */
  name: string
  /** 插件语义化版本（与后端 manifest 比对，检测版本偏斜） */
  version: string
  /** 工作流端点（相对 /api） */
  endpoints: WorkflowEndpoints
  /** 业务卡片：card_type → 组件（注册进通用卡片中枢） */
  cards?: Record<string, Component>
  /**
   * 只读检视面板：card_type → 组件。
   *
   * 与 cards 同构的另一套映射，但语义不同：cards 是"可交互卡片"，
   * inspectors 是"只读详情视图"（无审批 / 提交动作，仅呈现服务端权威数据）。
   * core 只负责按 card_type 解析并渲染，不理解面板内的业务字段。
   */
  inspectors?: Record<string, Component>
  /**
   * 卡片脱敏器：card_type → 脱敏函数。
   *
   * 仅作用于本地快照（离线兜底），实时视图与服务端权威数据保持原样。
   */
  cardSanitizers?: Record<string, CardPayloadSanitizer>
  /** 业务表单组件（宿主经动态组件渲染） */
  formComponent?: Component
  /** 右栏表单区标题（core 不写死任何业务分区名，未声明时回落中性默认名） */
  formSectionTitle?: string
  /** 左栏关联计数的单位文案（未声明时回落中性默认名） */
  sessionCountLabel?: string
  /** 演示身份（可缺省：生产环境接入企业 IAM 时无需） */
  identities?: PluginIdentity[]
}

class PluginRegistryClass {
  private readonly plugins = new Map<string, PluginDefinition>()
  /** 生效插件名（响应式：setActive 后视图自动刷新） */
  private readonly activeName = ref('')
  /** 后端 manifest 下发的端点覆盖（响应式，覆盖后视图自动刷新） */
  private readonly endpointOverrides = ref<
    Record<string, Partial<WorkflowEndpoints>>
  >({})
  /** 后端 manifest 下发的演示身份覆盖（响应式） */
  private readonly identityOverrides = ref<
    Record<string, PluginIdentity[]>
  >({})

  /** 注册插件清单；其卡片同步注册进通用卡片中枢。 */
  register(plugin: PluginDefinition): void {
    this.plugins.set(plugin.name, plugin)
    for (const [cardType, component] of Object.entries(plugin.cards ?? {})) {
      CardRegistry.register(cardType, component)
    }
    if (!this.activeName.value) {
      this.activeName.value = plugin.name
    }
  }

  /** 按名取插件清单。 */
  get(name: string): PluginDefinition | undefined {
    return this.plugins.get(name)
  }

  /** 取生效插件：显式指定优先，否则取首个已注册插件。 */
  getActive(): PluginDefinition | undefined {
    if (this.activeName.value) {
      const named = this.plugins.get(this.activeName.value)
      if (named) return named
    }
    const first = this.plugins.values().next()
    return first.done ? undefined : first.value
  }

  /** 指定生效插件；未注册返回 false（调用方应给出明确提示，不静默失败）。 */
  setActive(name: string): boolean {
    if (!this.plugins.has(name)) {
      return false
    }
    this.activeName.value = name
    return true
  }

  /** 列出全部已注册插件。 */
  list(): PluginDefinition[] {
    return [...this.plugins.values()]
  }

  /** 清空注册表（主要用于测试隔离）。 */
  clear(): void {
    this.plugins.clear()
    this.activeName.value = ''
    this.endpointOverrides.value = {}
    this.identityOverrides.value = {}
  }

  /**
   * 取生效插件为某卡片类型注册的脱敏器；未注册返回 undefined。
   *
   * core 只调用插件提供的函数，不解释任何业务字段名。
   */
  getCardSanitizer(
    cardType: string,
  ): CardPayloadSanitizer | undefined {
    const plugin = this.getActive()
    if (!plugin) return undefined
    return plugin.cardSanitizers?.[cardType]
  }

  /**
   * 取生效插件为某卡片类型注册的只读检视面板；未注册返回 undefined。
   *
   * cardType 为空（无卡片类型）时同样返回 undefined —— 没有类型就没有
   * 可检视的业务对象，core 不得凭空造一个视图。
   */
  getInspector(cardType: string | null): Component | undefined {
    if (!cardType) return undefined
    const plugin = this.getActive()
    if (!plugin) return undefined
    return plugin.inspectors?.[cardType]
  }

  /** 右栏表单分区标题（响应式；插件未声明时回落中性默认名）。 */
  readonly activeFormSectionTitle = computed<string>(() => {
    const plugin = this.getActive()
    return plugin?.formSectionTitle ?? DEFAULT_FORM_SECTION_TITLE
  })

  /** 左栏关联计数的单位文案（响应式；插件未声明时回落中性默认名）。 */
  readonly activeSessionCountLabel = computed<string>(() => {
    const plugin = this.getActive()
    return plugin?.sessionCountLabel ?? DEFAULT_SESSION_COUNT_LABEL
  })

  /**
   * 生效插件的端点（响应式）。
   * 本地默认值叠加后端 manifest 覆盖值；插件未注册时为 undefined。
   */
  readonly activeEndpoints = computed<WorkflowEndpoints | undefined>(() => {
    const plugin = this.getActive()
    if (!plugin) return undefined
    const override = this.endpointOverrides.value[plugin.name]
    return override ? { ...plugin.endpoints, ...override } : plugin.endpoints
  })

  /** 用后端 manifest 下发的端点覆盖本地默认值；插件未注册返回 false。 */
  applyManifestEndpoints(
    name: string,
    endpoints: Partial<WorkflowEndpoints>,
  ): boolean {
    if (!this.plugins.has(name)) {
      return false
    }
    this.endpointOverrides.value = {
      ...this.endpointOverrides.value,
      [name]: { ...(this.endpointOverrides.value[name] ?? {}), ...endpoints },
    }
    return true
  }

  /**
   * 生效插件的演示身份（响应式）。
   * 本地默认值叠加后端 manifest 覆盖值；插件未声明身份时为空数组。
   */
  readonly activeIdentities = computed<PluginIdentity[]>(() => {
    const plugin = this.getActive()
    if (!plugin) return []
    const override = this.identityOverrides.value[plugin.name]
    return override ?? plugin.identities ?? []
  })

  /** 用后端 manifest 下发的演示身份覆盖本地默认值；插件未注册返回 false。 */
  applyManifestIdentities(
    name: string,
    identities: PluginIdentity[],
  ): boolean {
    if (!this.plugins.has(name)) {
      return false
    }
    this.identityOverrides.value = {
      ...this.identityOverrides.value,
      [name]: identities,
    }
    return true
  }
}

export const PluginRegistry = new PluginRegistryClass()
