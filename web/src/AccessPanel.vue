<script setup lang="ts">
import { onMounted,ref } from 'vue'
import type { Device,Management } from './api'
import { useConsole } from './context'
const {api,run,notify}=useConsole(),devices=ref<Device[]>([]),identifier=ref(''),name=ref(''),credential=ref('')
async function load(){devices.value=(await api.request<Management>('/v1/management')).devices||[]}
async function viewer(){credential.value='查看密钥：'+(await api.request<{viewer_token:string}>('/v1/access',{})).viewer_token}
async function issue(){const result=await api.request<{token:string}>('/v1/devices',{station_id:identifier.value,name:name.value});credential.value=`工位 ${identifier.value} 的上传密钥（仅显示本次）：${result.token}`;await load()}
async function revoke(id:string){await api.request('/v1/devices/revoke',{station_id:id});notify('工位上传权限已撤销。');await load()}
onMounted(()=>void run(load))
</script>
<template><section class="card"><h2>查看端访问</h2><p class="muted">查看密钥允许查询、回放、下载和创建剪辑，不能扫码、改设置或清理原片。</p><button @click="run(viewer)">获取查看密钥</button></section><section class="card"><h2>上传工位授权</h2><p class="muted">密钥绑定工位 ID，只允许上传该工位的录像。默认本机与中心一体，后续可用于多工位保存。</p><form @submit.prevent="run(issue)"><div class="form-grid"><label>工位 ID<input v-model="identifier" required maxlength="64" placeholder="目标工位的稳定节点 ID"></label><label>工位名称<input v-model="name" required maxlength="80"></label></div><div class="form-actions"><button class="primary">签发 / 轮换上传密钥</button></div></form><div v-if="credential" class="credential"><code>{{ credential }}</code></div><div class="table-scroll"><table><thead><tr><th>工位</th><th>ID</th><th>授权状态</th><th></th></tr></thead><tbody><tr v-for="device in devices" :key="device.id"><td>{{ device.name }}</td><td class="mono">{{ device.id }}</td><td><span class="badge" :class="device.enabled?'green':''">{{ device.enabled?'已授权':'已撤销' }}</span></td><td><button v-if="device.enabled" @click="run(()=>revoke(device.id))">撤销</button></td></tr><tr v-if="!devices.length"><td colspan="4" class="empty compact">尚未授权其他工位。</td></tr></tbody></table></div></section><p class="fine">独立 Go 管理中心与 PackingProof 适配留待后续。跨网接入请使用 TLS 或安全隧道。</p></template>
