<template>
  <div class="ignore-list-view">
    <v-card color="surface" variant="flat" class="iris-hero-card mb-3">
      <v-card-title class="d-flex align-center iris-section-title">
        <v-icon icon="mdi-account-cancel-outline" color="primary" class="mr-2" />
        忽略名单
        <v-chip size="small" variant="tonal" color="primary" class="ml-3">
          手写 {{ ownCount }} 条
        </v-chip>
        <v-chip
          v-if="externalCount"
          size="small"
          variant="tonal"
          color="info"
          class="ml-2"
        >
          外部同步 {{ externalCount }}
        </v-chip>
        <v-chip
          size="small"
          variant="tonal"
          :color="effective.length ? 'warning' : 'success'"
          class="ml-2"
        >
          生效 {{ effective.length }}
        </v-chip>
        <v-spacer />
        <v-btn
          color="primary"
          variant="tonal"
          size="small"
          prepend-icon="mdi-refresh"
          :loading="loading"
          @click="loadData"
        >
          刷新
        </v-btn>
      </v-card-title>
      <v-card-text class="pt-0">
        <div class="text-body-2 text-medium-emphasis mb-2">
          这些账号的发言<strong>不会</strong>触发 Iris 主动回复（插话/跟进/发起话题），
          也不会进入滑动窗口和关注锚点。直接 <code>@</code> 机器人或被唤醒前缀点名时，
          仍会正常应答。
        </div>
        <v-alert
          v-if="!syncUserOnly && syncExternal"
          type="warning"
          variant="tonal"
          density="compact"
          class="mt-2"
        >
          <div class="text-body-2">
            当前「外部黑名单同步」为兼容模式（裸数字也同步）。源插件的名单是
            <strong>群号 + 好友号</strong>混合体，可能把同号的真人误加入。
            建议在「隐藏参数」中开启 <code>reply_ignore_sync_user_only</code>。
          </div>
        </v-alert>
      </v-card-text>
    </v-card>

    <v-progress-linear v-if="loading" indeterminate color="primary" class="mb-3" />

    <!-- 批量添加 -->
    <v-card color="surface" variant="flat" class="mb-3">
      <v-card-text>
        <div class="d-flex align-center ga-2 mb-2">
          <v-icon icon="mdi-plus-circle-outline" color="primary" size="20" />
          <span class="text-subtitle-2 font-weight-medium">批量添加</span>
          <v-spacer />
          <v-btn
            size="small"
            variant="text"
            prepend-icon="mdi-playlist-plus"
            @click="showBulk = !showBulk"
          >
            {{ showBulk ? '收起' : '粘贴多条' }}
          </v-btn>
        </div>

        <div class="d-flex ga-2 align-start">
          <v-text-field
            v-model="newEntry"
            density="compact"
            variant="outlined"
            placeholder="输入 ID 后回车，如 100000005 或 @昵称(ID)"
            prepend-inner-icon="mdi-pound"
            hide-details
            class="flex-grow-1"
            @keyup.enter="addEntry"
          />
          <v-btn color="primary" variant="tonal" size="small" height="40" @click="addEntry">
            添加
          </v-btn>
        </div>

        <v-expand-transition>
          <div v-if="showBulk" class="mt-3">
            <v-textarea
              v-model="bulkText"
              density="compact"
              variant="outlined"
              rows="5"
              placeholder="每行一个，或用逗号/空格分隔。支持 @昵称(ID)、qq:FriendMessage:ID"
              hide-details
            />
            <div class="d-flex ga-2 mt-2 align-center">
              <v-btn
                color="primary"
                variant="flat"
                size="small"
                prepend-icon="mdi-import"
                :loading="importing"
                @click="importBulk"
              >
                解析并导入
              </v-btn>
              <span v-if="bulkPreview.length" class="text-caption text-medium-emphasis">
                识别 {{ bulkPreview.filter((r) => r.valid).length }} / {{ bulkPreview.length }} 条
                <span v-if="bulkPreview.some((r) => !r.valid)" class="text-warning ml-1">
                  （{{ bulkPreview.filter((r) => !r.valid).length }} 条无法解析，将被跳过）
                </span>
              </span>
            </div>
          </div>
        </v-expand-transition>
      </v-card-text>
    </v-card>

    <!-- 未保存提醒：改动只在内存里，不点保存不会写入隐藏参数 -->
    <v-alert
      v-if="dirty"
      type="warning"
      variant="tonal"
      density="compact"
      class="mb-3"
      icon="mdi-content-save-alert"
    >
      <div class="d-flex align-center">
        <div class="text-body-2">
          有改动<strong>尚未保存</strong>。点「保存」前，这些账号不会被忽略。
        </div>
        <v-spacer />
        <v-btn
          color="warning"
          variant="flat"
          size="small"
          prepend-icon="mdi-content-save"
          :loading="saving"
          @click="saveAll"
        >
          立即保存
        </v-btn>
      </div>
    </v-alert>

    <!-- 一人一行 -->
    <v-card color="surface" variant="flat">
      <v-card-title class="d-flex align-center iris-section-title">
        <v-icon icon="mdi-format-list-bulleted" color="primary" class="mr-2" />
        名单明细
        <v-spacer />
        <v-btn
          v-if="dirty"
          size="small"
          variant="text"
          color="warning"
          prepend-icon="mdi-undo-variant"
          @click="loadData"
        >
          放弃修改
        </v-btn>
        <v-btn
          color="primary"
          variant="flat"
          size="small"
          prepend-icon="mdi-content-save"
          :loading="saving"
          :disabled="!dirty"
          @click="saveAll"
        >
          保存
        </v-btn>
      </v-card-title>

      <v-card-text class="pt-0">
        <v-alert
          v-if="!rows.length"
          type="info"
          variant="tonal"
          density="compact"
          class="mt-2"
        >
          名单为空。当前 Iris 不会忽略任何账号的主动回复。
        </v-alert>

        <v-alert
          v-else-if="!ownCount && externalCount"
          type="info"
          variant="tonal"
          density="compact"
          class="mt-2 mb-2"
        >
          <div class="text-body-2">
            手写名单为空，但已有 <strong>{{ externalCount }} 个</strong>账号
            通过「完全禁用」插件的黑名单自动同步过来（下方标记为「外部同步」）。
            要增删这类账号，请去该插件的配置里改。
          </div>
        </v-alert>

        <div v-else class="ignore-rows mt-2">
          <!-- 表头 -->
          <div class="ignore-row ignore-row--head text-caption text-medium-emphasis">
            <span>#</span>
            <span>原始写法</span>
            <span>解析出的 ID</span>
            <span>来源 / 状态</span>
            <span class="text-right">操作</span>
          </div>

          <div
            v-for="(row, i) in rows"
            :key="`${row.source}-${row.index}-${i}`"
            class="ignore-row"
            :class="{
              'ignore-row--invalid': row.status === 'invalid',
              'ignore-row--external': row.source === 'external',
            }"
          >
            <span class="text-caption text-disabled">{{ i + 1 }}</span>

            <div v-if="row.source === 'external'" class="ignore-row__readonly">
              <code>{{ row.raw }}</code>
            </div>
            <v-text-field
              v-else
              v-model="row.raw"
              density="compact"
              variant="plain"
              hide-details
              class="ignore-row__input"
              @update:model-value="onRowEdit(row)"
            />

            <div class="d-flex flex-wrap ga-1">
              <v-chip
                v-for="id in row.ids"
                :key="id"
                size="x-small"
                variant="tonal"
                color="primary"
                label
              >
                {{ id }}
              </v-chip>
              <span v-if="!row.ids.length" class="text-caption text-warning">
                无法解析
              </span>
            </div>

            <div class="d-flex flex-wrap ga-1 align-center">
              <v-chip
                v-if="row.source === 'external'"
                size="x-small"
                variant="tonal"
                color="info"
                prepend-icon="mdi-link-variant"
              >
                外部同步
              </v-chip>
              <!-- 手写条目在保存前一律提示「未保存」，避免用户以为已生效 -->
              <v-chip
                v-if="row.source === 'own' && dirty"
                size="x-small"
                variant="flat"
                color="warning"
                prepend-icon="mdi-content-save-alert"
              >
                未保存
              </v-chip>
              <v-chip
                v-else-if="row.status === 'ok'"
                size="x-small"
                variant="tonal"
                color="success"
              >
                生效中
              </v-chip>
              <v-chip
                v-else-if="row.status === 'invalid'"
                size="x-small"
                variant="tonal"
                color="warning"
              >
                格式无效
              </v-chip>
              <v-chip
                v-else
                size="x-small"
                variant="tonal"
                color="default"
              >
                未生效
              </v-chip>
            </div>

            <div class="text-right">
              <v-tooltip
                v-if="row.source === 'external'"
                text="来自外部黑名单，请去该插件修改"
                location="top"
              >
                <template #activator="{ props }">
                  <v-icon
                    icon="mdi-lock-outline"
                    size="16"
                    class="text-disabled"
                    v-bind="props"
                  />
                </template>
              </v-tooltip>
              <v-tooltip v-else text="移除此行" location="top">
                <template #activator="{ props }">
                  <v-btn
                    icon="mdi-delete-outline"
                    variant="text"
                    size="x-small"
                    color="error"
                    v-bind="props"
                    @click="removeRow(i)"
                  />
                </template>
              </v-tooltip>
            </div>
          </div>
        </div>

        <v-divider v-if="rows.length" class="my-3" />
        <div v-if="rows.length" class="d-flex align-center text-caption text-medium-emphasis">
          <v-icon icon="mdi-information-outline" size="14" class="mr-1" />
          修改后点击「保存」才会写入隐藏参数并生效。生效集合共
          {{ effective.length }} 个（可能包含外部同步来的条目）。
        </div>
      </v-card-text>
    </v-card>

    <v-snackbar v-model="snackbar" :color="snackbarColor" timeout="2600">
      {{ snackbarText }}
    </v-snackbar>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import {
  checkIgnoreIds,
  getIgnoreList,
  saveIgnoreList,
  type CheckRow,
  type IgnoreRow,
} from '@/api/ignoreList'

const loading = ref(false)
const saving = ref(false)
const importing = ref(false)

const rows = ref<IgnoreRow[]>([])
const effective = ref<string[]>([])
const rawText = ref('')
const syncExternal = ref(false)
const syncUserOnly = ref(false)

const newEntry = ref('')
const bulkText = ref('')
const bulkPreview = ref<CheckRow[]>([])
const showBulk = ref(false)

const edited = ref(false)
const dirty = computed(() => edited.value)

const snackbar = ref(false)
const snackbarText = ref('')
const snackbarColor = ref<'success' | 'error' | 'warning'>('success')

function notify(text: string, color: 'success' | 'error' | 'warning' = 'success') {
  snackbarText.value = text
  snackbarColor.value = color
  snackbar.value = true
}

/** 从行列表重建手写名单（外部同步条目不可写回，必须排除） */
function rowsToEntries(list: IgnoreRow[]): string[] {
  return list
    .filter((r) => r.source !== 'external')
    .map((r) => (r.raw || '').trim())
    .filter(Boolean)
}

/** 手写条目数（顶部「N 条」应只算这些，避免和外部同步混淆） */
const ownCount = computed(
  () => rows.value.filter((r) => r.source !== 'external').length,
)
const externalCount = computed(
  () => rows.value.filter((r) => r.source === 'external').length,
)

async function loadData() {
  loading.value = true
  try {
    const data = await getIgnoreList()
    rows.value = data.rows
    effective.value = data.effective
    rawText.value = data.raw
    syncExternal.value = data.sync_external
    syncUserOnly.value = data.sync_user_only
    edited.value = false
  } catch (e: any) {
    notify(e?.message || '读取失败', 'error')
  } finally {
    loading.value = false
  }
}

/**
 * 前端本地解析，规则与后端 sender_gate._parse_token 对齐。
 *
 * 只用于新增/编辑时的即时反馈，最终以服务端返回为准。
 * 必须与后端同规则，否则会出现「本地显示无法解析、保存后却生效」
 * 这类自相矛盾的提示。
 */
function parseEntry(raw: string): string[] {
  const text = (raw || '').trim()
  if (!text) return []

  // 1. 整条就是纯 ID（允许 @ 前缀）
  const bare = text.replace(/^@+/, '').trim()
  if (/^\d{5,}$/.test(bare)) return [bare]

  // 2. 括号内的 ID：@昵称(100000005) / 昵称（100000005）
  const paren = text.match(/[（(]\s*(\d{5,})\s*[)）]/)
  if (paren) return [paren[1]]

  // 3. 冒号结尾的 ID：qq:FriendMessage:100000000
  const colon = text.match(/[:：]\s*(\d{5,})\s*$/)
  if (colon) return [colon[1]]

  return []
}

function addEntry() {
  const text = newEntry.value.trim()
  if (!text) return
  const ids = parseEntry(text)
  rows.value.push({
    index: rows.value.length,
    raw: text,
    ids,
    active: false,
    status: ids.length ? 'not_active' : 'invalid',
    source: 'own',
  })
  newEntry.value = ''
  edited.value = true
  if (!ids.length) {
    notify('该写法无法解析出 ID，保存后不会生效', 'warning')
  }
}

function removeRow(i: number) {
  rows.value.splice(i, 1)
  edited.value = true
}

function onRowEdit(row: IgnoreRow) {
  edited.value = true
  const ids = parseEntry(row.raw)
  row.ids = ids
  row.status = ids.length ? 'not_active' : 'invalid'
}

async function importBulk() {
  const text = bulkText.value.trim()
  if (!text) return
  importing.value = true
  try {
    bulkPreview.value = await checkIgnoreIds(text)
    const valid = bulkPreview.value.filter((r) => r.valid)
    if (!valid.length) {
      notify('没有可导入的有效 ID', 'warning')
      return
    }
    const existing = new Set(rowsToEntries(rows.value))
    // 外部同步条目也要参与去重：同一个 ID 不必再手写一遍
    const existingIds = new Set<string>()
    for (const r of rows.value) {
      for (const id of r.ids) existingIds.add(id)
    }
    let added = 0
    for (const item of valid) {
      const raw = item.raw.trim()
      if (!raw || existing.has(raw)) continue
      if (item.ids.some((id) => existingIds.has(id))) continue
      existing.add(raw)
      item.ids.forEach((id) => existingIds.add(id))
      rows.value.push({
        index: rows.value.length,
        raw,
        ids: item.ids,
        active: false,
        status: 'not_active',
        source: 'own',
      })
      added += 1
    }
    bulkText.value = ''
    edited.value = true
    notify(`已导入 ${added} 条${valid.length - added ? `，跳过 ${valid.length - added} 条重复` : ''}`)
  } catch (e: any) {
    notify(e?.message || '导入失败', 'error')
  } finally {
    importing.value = false
  }
}

async function saveAll() {
  saving.value = true
  try {
    const entries = rowsToEntries(rows.value)
    const data = await saveIgnoreList(entries)
    rows.value = data.rows
    effective.value = data.effective
    rawText.value = data.raw
    edited.value = false
    notify(`已保存，当前生效 ${data.effective.length} 个`)
  } catch (e: any) {
    notify(e?.message || '保存失败', 'error')
  } finally {
    saving.value = false
  }
}

onMounted(loadData)
</script>

<style scoped>
.iris-hero-card {
  border: 1px solid rgba(var(--v-theme-primary), 0.16);
}

.iris-section-title {
  font-size: 0.95rem;
  font-weight: 600;
}

.ignore-rows {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.ignore-row {
  display: grid;
  grid-template-columns: 32px minmax(160px, 1.4fr) minmax(150px, 1.2fr) 110px 44px;
  align-items: center;
  gap: 10px;
  padding: 4px 8px;
  border-radius: 6px;
  border-bottom: 1px solid rgba(var(--v-border-color), 0.08);
}

.ignore-row:not(.ignore-row--head):hover {
  background: rgba(var(--v-theme-primary), 0.04);
}

.ignore-row--head {
  border-bottom: 1px solid rgba(var(--v-border-color), 0.24);
  padding-bottom: 6px;
}

.ignore-row--invalid {
  background: rgba(var(--v-theme-warning), 0.06);
}

.ignore-row--external {
  background: rgba(var(--v-theme-info), 0.05);
}

.ignore-row__readonly code {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 0.85rem;
  opacity: 0.75;
}

.ignore-row__input :deep(input) {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 0.85rem;
}

@media (max-width: 720px) {
  .ignore-row {
    grid-template-columns: 24px 1fr;
    row-gap: 4px;
  }
  .ignore-row--head {
    display: none;
  }
}
</style>
