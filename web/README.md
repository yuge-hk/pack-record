# PackRecord WebUI

Vue 3 + TypeScript 单文件组件，Vite 构建。运行时无需 Node.js 或外部 CDN，静态资源由 PackRecord 自带 HTTP 服务提供。

```bash
npm ci
npm run build       # 先执行 vue-tsc，再构建 dist/
# 后端运行后可在 http://127.0.0.1:18765 使用
npm run dev         # 开发服务器，/v1 和 /media 转发至本机后端
```

构建需要 Node.js >=22.12。开发依赖版本由 package-lock.json 锁定，Debian 构建脚本自动构建前端并将 dist 打入 PyInstaller。服务返回 DTO 类型集中在 src/api.ts，页面按录像、存储、订单、设置和接入拆分。令牌仅保存在内存，不使用 localStorage、Cookie 或 URL 参数。
