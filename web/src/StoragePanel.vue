<script setup lang="ts">
import { onMounted,onUnmounted,ref } from 'vue'
import type { Management } from './api'
import { stateName } from './api'
import { useConsole } from './context'
const {api,run,notify}=useConsole(),status=ref<Management>({exports:[]})
async function load(){status.value=await api.request<Management>('/v1/management')}
async function action(path:string){await api.request(path,{});await load();notify('操作已提交，后台继续处理。')}
async function cleanup(){if(!confirm('按当前保留期限和容量策略清理符合条件的录像？'))return;const result=await api.request<{deleted:number}>('/v1/cleanup',{});notify(`已清理 ${result.deleted} 条录像。未确认上传和使用中的原片受保护。`);await load()}
onMounted(()=>void run(load));const timer=setInterval(()=>void run(load),10000);onUnmounted(()=>clearInterval(timer))
</script>
<template><div class="volume-grid"><section v-for="volume in status.volumes" :key="volume.path" class="card volume"><span class="eyebrow">录像存储目录</span><h2>{{ volume.free_gb===null?'目录不可用':volume.free_gb.toFixed(1)+' GB' }}</h2><p class="muted">{{ volume.free_gb===null?volume.error:'当前可用空间' }}</p><code>{{ volume.path }}</code></section></div><section class="card"><div class="section-title"><div><h2>备份任务</h2><p class="muted">断线时继续本地录像，重连后自动重试。收到校验确认后才允许按策略清理。</p></div></div><div class="toolbar"><button class="primary" @click="run(()=>action('/v1/uploads/queue'))">上传未备份录像</button><button @click="run(()=>action('/v1/uploads/retry'))">重试失败上传</button></div><p v-if="status.error" class="notice error">{{ status.error }}</p><div class="table-scroll"><table><thead><tr><th>单号</th><th>状态</th><th>重试次数</th><th>提示</th></tr></thead><tbody><tr v-for="job in status.transfers" :key="job.id"><td>{{ job.barcode||job.record_id }}</td><td><span class="badge">{{ stateName[job.state]||job.state }}</span></td><td>{{ job.attempts }}</td><td class="wrap">{{ job.error||'—' }}</td></tr><tr v-if="!status.transfers?.length"><td colspan="4" class="empty compact">暂无备份任务，请先在工位设置中配置目标。</td></tr></tbody></table></div></section><section class="card"><h2>录像保留与清理</h2><p class="muted">默认关闭自动清理。保留天数、水位和清理策略在“工位设置”中调整；清理只作用于受管理且符合条件的录像。</p><button @click="run(cleanup)">执行当前清理策略</button></section></template>
