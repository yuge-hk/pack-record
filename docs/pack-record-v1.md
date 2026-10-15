# pack-record-v1 接入契约（2.1.0）

本版本描述已经提供的基础 HTTP 边界。未来 Go 中心独立建项目，通过适配器分别接入 PackRecord 与 PackingProof；不直接读写客户端数据库或私有录像目录。

## 身份与权限

`GET /v1/node` 返回 `protocol=pack-record-v1`、`protocol_version=1`、`station_id`、名称、运行角色和能力列表。`mobile_compatibility=false` 明确不支持 PackingProof-Mobile。中心唯一键为 `(接入类型, station_id, recording_id)`，快递单号只作查询字段。

请求使用 `Authorization: Bearer ...`。管理员负责设置、管理和设备授权；查看凭据只可查询、回放和剪辑；设备凭据由保存中心 `/v1/devices` 签发并绑定 `station_id`，轮换或撤销立即生效。客户端申报的工位 ID 必须与上传凭据一致。管理员兼容维护入口不代表可以把管理令牌当作设备凭据分发。

当前是局域网 HTTP 服务；TLS / 安全隧道由部署层提供。未来中心连接凭据应另行分权，不复用管理员令牌。尚无主动出站通道。

## 索引和状态

- `GET /v1/status?after=N`：运行状态和最多 256 条内存 UI 事件。sequence 会重启归零，只用于短期界面事件，不能当录像同步游标。
- `GET /v1/library?search=...&from=YYYY-MM-DD&to=...&station=...&page=1&page_size=50`：人工检索，页长最多 200。普通页码在并发新增时会移动，不应用作增量同步。
- `GET /v1/sync?after=0&limit=200`：持久修订游标，返回 `items/cursor/head/has_more`，页长最多 500。插入、更新、清理产生修订；项含录像 ID、来源、工位、单号、媒体属性、校验值、上传状态、完整性和删除标记，无内部路径。

每个修订项返回读取时录像的**最新快照**，可能多次返回同一录像；按 ID 幂等覆盖。更新不要求恰好一次。持续用返回 cursor 翻页直到 has_more=false，再保存游标。发生数据库重建、节点恢复旧备份或游标高于 head 时，中心必须从 0 全量重新对账；当前协议不提供历史修订快照或自动删除中心记录。

`deleted_at/delete_reason` 是本地保留策略产生的墓碑。中心是否删除自己的备份需独立策略，不能仅据此删除。录像索引同步成功、文件接收成功、哈希验证成功、原始采集是否中断是不同维度。`capture.interrupted=true` 必须一直保留；“已验证备份”不能改成“完整录制”。`upload_state=verified` 表示这个客户端的某次指定备份已验证，不代表中心索引已同步。

## 读取与导出

`GET/HEAD /v1/recordings/{id}/file` 支持单段 HTTP Range 和下载参数 `?download=1`。Web 通过 `POST /v1/playback` 传 `record_id`、`export_id` 或管理员 `test_id` 换取 1 小时媒体 URL；链接不含管理令牌，但持有链接可读取对应文件。最多 256 个活动媒体链接，原片在租约内受清理保护。

`POST /v1/playback/release` 的 `{ticket: "媒体路径最后一段"}` 可释放租约；过期及重启也释放。短链不作为永久回放地址保存。

`POST /v1/exports` 参数 `{record_id,start,end}`，单位秒；返回任务 ID。`GET /v1/exports/{id}` 查询 queued/running/ready/error，完成后读取 `.../{id}/file`。生成 H.264/AAC MP4，原片保留；最多 20 个排队/执行任务。

## 自有备份协议

1. `POST /v1/ingest/start`：`id,station_id,station_name,barcode,start_time,end_time,duration,size,sha256,extension,resolution,fps,capture`。extension 只接受 `.mkv/.mp4`，SHA256 为小写十六进制，最大 1 TiB。响应 `{upload_id,offset}`。
2. `POST /v1/ingest/{upload_id}/chunk`：最多 4 MiB 二进制，头 `X-Offset`、`X-Chunk-SHA256`。刷盘后返回新的 offset。相同偏移和相同内容重复提交可确认；不同内容或越界偏移拒绝。
3. `POST /v1/ingest/{upload_id}/complete`，JSON `{}`：验证总大小、SHA256、视频流及可探测时长，原子归档并持久化后返回 `{status:"verified",record_id,source_id,sha256,size}`。重复完成先重新核验已归档文件。归档文件被清理或改变则拒绝确认。

`(station_id,source_id)` 幂等；同 ID 不同大小或哈希拒绝。首次提交的元数据保留，重复 start 不修改完整性。响应丢失时重新 start 获取磁盘实际偏移；重启后继续。改名后数据库提交前中断，通过确定性目标路径恢复。未完成分片保留，不自动删除；管理员需监控接收盘容量，目前没有分片过期 Web 管理。

工位收到匹配的完整回执后才能标记 verified；成功上传不立即删除原片，另按保留策略处理。目录上传使用复制、刷盘、SHA256 校验，无此 HTTP 协议。PackingProof 的保存主机协议和扩展 API 应由后续 Go 适配器处理，不承诺与本协议兼容。

## 订单入口与控制边界

`POST /v1/orders`：`{orders:[{tracking_number,quantity,buyer_remark,seller_remark,product_summary,refund_status,source,updated_at}]}`，最多 500 条且整个 JSON 最大 64 KiB。refund_status 为 unknown/none/pending/refunded；updated_at 可省略，提供时必须含时区。迟到的旧数据不覆盖更新数据。

本机录制开始或当前订单更新时发布 announcement；桌面播放中文提醒，过期订单/过期事件不播报。未实现电商平台退款拦截、XLSX 解析或油猴脚本。

扫码、停止和恢复由本地串行控制器负责。现有 `/v1/commands/*` 为本机管理员维护接口，Go 首版只读管理及备份。未来远程控制须新增显式能力、授权、幂等键、过期时间和执行结果；不能断线后重放旧扫码或控制指令。
