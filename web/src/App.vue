<script setup lang="ts">
import { computed, onUnmounted, provide, ref } from 'vue'
import { ApiClient } from './api'
import type { Settings, Status } from './api'
import { contextKey } from './context'
import LibraryPanel from './LibraryPanel.vue'
import StoragePanel from './StoragePanel.vue'
import OrdersPanel from './OrdersPanel.vue'
import SettingsPanel from './SettingsPanel.vue'
import AccessPanel from './AccessPanel.vue'
const api = new ApiClient()
const key = ref(''), settings = ref<Settings | null>(null), active = ref('library'), message = ref(''), error = ref(false), busy = ref(0)
const state = ref(''), current = ref<string | null>(null), access = ref<'admin' | 'viewer'>('viewer')
const admin = computed(() => access.value === 'admin')
const pages = computed(() => [{id:'library',title:'录像查询',mark:'▤',note:'查找、回放与导出'}, ...(admin.value ? [
  {id:'storage',title:'存储与备份',mark:'▥',note:'磁盘、补传与清理'}, {id:'orders',title:'订单提醒',mark:'◉',note:'件数、备注与退款'},
  {id:'settings',title:'工位设置',mark:'⚙',note:'设备、语音与首次配置'}, {id:'access',title:'接入管理',mark:'◇',note:'查看端与上传工位'}] : [])])
const pageTitle = computed(() => pages.value.find(p => p.id === active.value)?.title || '录像查询')
function notify(value: string) { error.value = false; message.value = value }
async function run(job: () => Promise<void>) { busy.value++; try { await job() } catch (e) { error.value = true; message.value = e instanceof Error ? e.message : String(e) } finally { busy.value-- } }
provide(contextKey, { api, run, notify })
async function connect() { await run(async () => { api.token = key.value; const result = await api.request<Status>('/v1/status'); access.value = result.access; settings.value = result.settings; state.value = result.snapshot.state; current.value = result.snapshot.current; key.value = ''; message.value = ''; active.value = 'library' }) }
function logout() { settings.value = null; api.token = ''; key.value = ''; message.value = '' }
const poll = setInterval(() => { if (settings.value && !busy.value) void run(async () => { const result = await api.request<Status>('/v1/status'); state.value = result.snapshot.state; current.value = result.snapshot.current }) }, 10000)
onUnmounted(() => clearInterval(poll))
</script>
<template>
  <div v-if="!settings" class="login-page"><div class="login-brand"><span class="brand-icon">P</span><span>PACKRECORD</span></div><form class="login-card" @submit.prevent="connect"><span class="eyebrow">本地优先 · 按单号留证</span><h1>连接录像中心</h1><p class="muted">管理工位设置，查找每一份打包记录。</p><label>访问密钥<input id="key" v-model="key" type="password" required autocomplete="off" placeholder="管理员或查看密钥"></label><p v-if="message" class="notice error" role="alert">{{ message }}</p><button id="connect" class="primary wide" :disabled="busy > 0">{{ busy ? '连接中…' : '连接中心' }}</button><p class="fine">密钥只在当前页面内存中使用。管理员密钥位于本机数据目录的 service.token。</p></form><span class="login-foot">PackRecord · 工位录像与本地保存</span></div>
  <div v-else class="shell" id="workspace"><aside class="sidebar"><div class="brand"><span class="brand-icon">P</span><div>PACKRECORD<small>录像中心</small></div></div><div class="node"><span class="online-dot"></span><div>{{ settings.station_name }}<small>{{ admin ? '管理控制台' : '只读查看' }}</small></div></div><nav><button v-for="p in pages" :key="p.id" :class="{active:active===p.id}" @click="active=p.id;message=''" :data-page="p.id"><span class="nav-mark">{{ p.mark }}</span><span>{{ p.title }}<small>{{ p.note }}</small></span></button></nav><div class="sidebar-foot">本地录像独立运行<br>中心离线不影响打包</div></aside><div class="main-area"><header class="topbar"><span>工作空间 <span class="slash">/</span> {{ pageTitle }}</span><div class="row"><span class="connection"><i class="online-dot"></i>已连接</span><button class="quiet" @click="logout">退出</button></div></header><main><div class="page-heading"><div><span class="eyebrow">PACKRECORD CONSOLE</span><h1>{{ pageTitle }}</h1></div><span class="status-pill" v-if="state==='recording'">● 正在录制 {{ current }}</span><span class="status-pill" v-else>{{ state==='idle' ? '工位待机' : state==='testing' ? '设备试录中' : state==='fault' ? '工位故障' : state }}</span></div><p v-if="message" class="notice" :class="{error}" role="status">{{ message }}<button @click="message=''" aria-label="关闭提示">×</button></p><LibraryPanel v-if="active==='library'"/><StoragePanel v-else-if="active==='storage'"/><OrdersPanel v-else-if="active==='orders'"/><SettingsPanel v-else-if="active==='settings'" :settings="settings" @saved="settings=$event"/><AccessPanel v-else-if="active==='access'"/></main><footer>PackRecord 2.1.0 <span>本机采集 · 可靠保存</span></footer></div></div>
</template>
