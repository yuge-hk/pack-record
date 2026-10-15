"""Qt recording library, playback, export queue, retention and local order entry."""
from concurrent.futures import ThreadPoolExecutor
import shutil
from pathlib import Path
from urllib.request import urlopen

from PySide6.QtCore import Qt, Signal, QTimer, QUrl
from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QTableWidget, QTableWidgetItem, QTabWidget, QWidget, QMessageBox, QDoubleSpinBox, QFileDialog,
    QFormLayout, QSpinBox, QComboBox, QHeaderView, QTextEdit)

STATE = dict(local='仅本地', queued='待处理', uploading='上传中', verified='已校验接收', error='失败', ready='可回放 / 下载', running='处理中')


class AsyncDialog(QDialog):
    finished_work = Signal(object, object, object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix='desktop-management')
        self.disposed = False
        self.finished_work.connect(self._done, Qt.QueuedConnection)

    def run(self, function, callback=lambda _: None):
        if self.disposed:
            return
        def work():
            try:
                result, error = function(), None
            except Exception as exc:
                result, error = None, str(exc)
            if not self.disposed:
                try:
                    self.finished_work.emit(callback, result, error)
                except RuntimeError:
                    pass  # The dialog may have been destroyed during the task.
        self.pool.submit(work)

    def _done(self, callback, result, error):
        if self.disposed:
            return
        if error:
            QMessageBox.warning(self, '操作未完成', error)
        else:
            callback(result)

    def closeEvent(self, event):
        self.disposed = True
        self.pool.shutdown(wait=False, cancel_futures=True)
        super().closeEvent(event)

    def reject(self):
        self.disposed = True
        if hasattr(self, 'timer'):
            self.timer.stop()
        self.pool.shutdown(wait=False, cancel_futures=True)
        super().reject()


class PlaybackDialog(QDialog):
    def __init__(self, ticket, parent=None):
        super().__init__(parent)
        self.setWindowTitle('录像回放 · 原始文件保留')
        self.resize(900, 620)
        layout = QVBoxLayout(self)
        video = QVideoWidget()
        layout.addWidget(video, 1)
        self.audio = QAudioOutput(self)
        self.player = QMediaPlayer(self)
        self.player.setVideoOutput(video)
        self.player.setAudioOutput(self.audio)
        self.message = QLabel()
        layout.addWidget(self.message)
        self.player.errorOccurred.connect(lambda *_: self.message.setText('回放失败：' + self.player.errorString() + '。可在管理页生成兼容 MP4 后重试。'))
        controls = QHBoxLayout()
        for name, action in (('播放', self.player.play), ('暂停', self.player.pause), ('回到开头', lambda: self.player.setPosition(0))):
            button = QPushButton(name)
            button.clicked.connect(action)
            controls.addWidget(button)
        self.position = QLabel()
        controls.addWidget(self.position)
        layout.addLayout(controls)
        self.player.positionChanged.connect(lambda value: self.position.setText(f'{value/1000:.1f} / {self.player.duration()/1000:.1f} 秒'))
        self.player.setSource(QUrl(ticket['url']) if ticket.get('url') else QUrl.fromLocalFile(ticket['path']))
        self.player.play()

    def closeEvent(self, event):
        self.player.stop()
        self.player.setSource(QUrl())
        super().closeEvent(event)


class ManagementDialog(AsyncDialog):
    def __init__(self, controller, parent=None):
        super().__init__(parent)
        self.controller, self.manager = controller, controller.manager
        self.admin = controller.settings['role'] != 'viewer'
        self.page, self.items, self.export_items = 1, [], []
        self.setWindowTitle('录像、存储与工位管理')
        self.resize(1100, 760)
        root = QVBoxLayout(self)
        self.tabs = QTabWidget()
        root.addWidget(self.tabs)
        library = QWidget()
        layout = QVBoxLayout(library)
        self.tabs.addTab(library, '录像库')
        search = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText('快递单号')
        self.date_from, self.date_to, self.station = QLineEdit(), QLineEdit(), QLineEdit()
        self.date_from.setPlaceholderText('开始日期 YYYY-MM-DD')
        self.date_to.setPlaceholderText('结束日期 YYYY-MM-DD')
        self.station.setPlaceholderText('工位 ID（可选）')
        for edit in (self.search, self.date_from, self.date_to, self.station):
            search.addWidget(edit)
        self._button(search, '查询', lambda: self.refresh_library(reset=True))
        layout.addLayout(search)
        self.records = self._table(['单号', '工位', '开始时间', '时长秒', '录像状态', '上传状态'])
        layout.addWidget(self.records)
        actions = QHBoxLayout()
        self._button(actions, '上一页', self.previous)
        self.page_label = QLabel()
        actions.addWidget(self.page_label)
        self._button(actions, '下一页', self.next_page)
        self._button(actions, '回放原片', self.play_record)
        self._button(actions, '导出原片', self.save_original)
        layout.addLayout(actions)
        clip = QHBoxLayout()
        clip.addWidget(QLabel('剪辑范围（秒）：'))
        self.start, self.end = QDoubleSpinBox(), QDoubleSpinBox()
        for spin in (self.start, self.end):
            spin.setRange(0, 100000)
            spin.setDecimals(2)
            clip.addWidget(spin)
        self._button(clip, '生成 H.264 MP4 / 剪辑', self.create_export)
        layout.addLayout(clip)
        self.records.itemSelectionChanged.connect(self._selected)
        exports = QWidget()
        exp_layout = QVBoxLayout(exports)
        self.tabs.addTab(exports, '导出任务')
        self.exports = self._table(['状态', '开始秒', '结束秒', '错误'])
        exp_layout.addWidget(self.exports)
        buttons = QHBoxLayout()
        self._button(buttons, '刷新', self.refresh_status)
        self._button(buttons, '回放导出文件', self.play_export)
        self._button(buttons, '另存导出文件', self.save_export)
        exp_layout.addLayout(buttons)
        self.status_text = QTextEdit()
        self.status_text.setReadOnly(True)
        storage = QWidget()
        s_layout = QVBoxLayout(storage)
        self.tabs.addTab(storage, '存储与上传')
        s_layout.addWidget(self.status_text)
        buttons = QHBoxLayout()
        self._button(buttons, '刷新状态', self.refresh_status)
        if self.admin:
            self._button(buttons, '执行当前清理策略', self.cleanup)
            self._button(buttons, '上传未备份录像', lambda: self.run(self.manager.queue_uploads, lambda _: self.refresh_status()))
            self._button(buttons, '重试失败上传', lambda: self.run(self.manager.retry_uploads, lambda _: self.refresh_status()))
        s_layout.addLayout(buttons)
        if self.admin:
            self._order_tab()
            self._device_tab()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh_status)
        self.timer.start(5000)
        self.refresh_library()
        self.refresh_status()

    @staticmethod
    def _table(headers):
        table = QTableWidget(0, len(headers))
        table.setHorizontalHeaderLabels(headers)
        table.setSelectionBehavior(QTableWidget.SelectRows)
        table.setSelectionMode(QTableWidget.SingleSelection)
        table.setEditTriggers(QTableWidget.NoEditTriggers)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        return table

    @staticmethod
    def _button(layout, name, action):
        button = QPushButton(name)
        button.clicked.connect(action)
        layout.addWidget(button)
        return button

    def _fill(self, table, rows):
        table.setRowCount(len(rows))
        for i, row in enumerate(rows):
            for j, value in enumerate(row):
                table.setItem(i, j, QTableWidgetItem(str(value or '—')))

    def refresh_library(self, reset=False):
        if reset:
            self.page = 1
        query = dict(search=self.search.text().strip(), date_from=self.date_from.text().strip(),
                     date_to=self.date_to.text().strip(), station=self.station.text().strip(), page=self.page)
        self.run(lambda: self.manager.search(**query), self._library_ready)

    def _library_ready(self, result):
        self.items, self.total = result['items'], result['total']
        self._fill(self.records, [[r['barcode'], r['station_name'], r['start_time'], round(r['duration'], 1),
                   '故障片段' if r['capture'].get('interrupted') else '已归档', STATE.get(r['upload_state'], r['upload_state'])] for r in self.items])
        self.page_label.setText(f'第 {self.page} 页 · 共 {self.total} 条')

    def previous(self):
        if self.page > 1:
            self.page -= 1
            self.refresh_library()

    def next_page(self):
        if self.page * 50 < getattr(self, 'total', 0):
            self.page += 1
            self.refresh_library()

    def _record(self):
        index = self.records.currentRow()
        if not 0 <= index < len(self.items):
            QMessageBox.information(self, '请选择录像', '请先在列表中选择一条录像。')
            return None
        return self.items[index]

    def _selected(self):
        index = self.records.currentRow()
        if 0 <= index < len(self.items):
            self.start.setValue(0)
            self.end.setValue(self.items[index]['duration'])

    def _play(self, ticket):
        dialog = PlaybackDialog(ticket, self)
        try:
            dialog.exec()
        finally:
            dialog.player.stop()
            dialog.player.setSource(QUrl())
            dialog.deleteLater()
            self.run(lambda: self.manager.release_media(ticket))

    def play_record(self):
        record = self._record()
        if record:
            self.run(lambda: self.manager.media_ticket(record_id=record['id']), self._play)

    def create_export(self):
        record = self._record()
        if record:
            start, end = self.start.value(), self.end.value()
            self.run(lambda: self.manager.create_export(record['id'], start, end), lambda _: (self.tabs.setCurrentIndex(1), self.refresh_status()))

    def _export(self):
        index = self.exports.currentRow()
        if not 0 <= index < len(self.export_items) or self.export_items[index]['state'] != 'ready':
            QMessageBox.information(self, '导出未就绪', '请选择已经完成的导出任务。')
            return None
        return self.export_items[index]

    def play_export(self):
        job = self._export()
        if job:
            self.run(lambda: self.manager.media_ticket(export_id=job['id']), self._play)

    def _save(self, ticket_function, suggested):
        destination, _ = QFileDialog.getSaveFileName(self, '另存录像', suggested)
        if not destination:
            return
        def copy():
            ticket = ticket_function()
            target = Path(destination)
            roots = [self.controller.settings.get('record_path'), *self.controller.settings.get('record_paths', [])]
            if any(root and target.resolve().is_relative_to(Path(root).resolve()) for root in roots):
                raise ValueError('请另存到录像管理目录以外，避免覆盖原始录像')
            if ticket.get('path') and target.resolve() == Path(ticket['path']).resolve():
                raise ValueError('不能覆盖源录像')
            partial = target.with_name(target.name + '.packrecord.part')
            try:
                if ticket.get('url'):
                    with urlopen(ticket['url'], timeout=30) as source, partial.open('xb') as output:
                        shutil.copyfileobj(source, output)
                else:
                    with open(ticket['path'], 'rb') as source, partial.open('xb') as output:
                        shutil.copyfileobj(source, output)
                # QFileDialog asks for overwrite confirmation for existing destinations.
                partial.replace(target)
            finally:
                partial.unlink(missing_ok=True)
                self.manager.release_media(ticket)
            return str(target)
        self.run(copy, lambda path: QMessageBox.information(self, '已导出', path))

    def save_original(self):
        record = self._record()
        if record:
            self._save(lambda: self.manager.media_ticket(record_id=record['id']), record['barcode'] + '.mkv')

    def save_export(self):
        job = self._export()
        if job:
            self._save(lambda: self.manager.media_ticket(export_id=job['id']), '录像剪辑.mp4')

    def refresh_status(self):
        if not self.disposed:
            self.run(self.manager.status, self._status_ready)

    def _status_ready(self, status):
        current = self.exports.currentRow()
        self.export_items = status.get('exports', [])
        self._fill(self.exports, [[STATE.get(j['state'], j['state']), j['start'], j['end'], j['error']] for j in self.export_items])
        if 0 <= current < self.exports.rowCount():
            self.exports.selectRow(current)
        lines = ['存储目录：']
        for row in status.get('volumes', []):
            space = f"{row['free_gb']:.1f} GB 可用" if row['free_gb'] is not None else row['error']
            lines.append(row['path'] + ' · ' + space)
        lines.append('\n上传任务：')
        for job in status.get('transfers', []):
            lines.append(f"{job['barcode']} · {STATE.get(job['state'], job['state'])} · 重试 {job['attempts']} 次 · {job['error']}")
        lines.append('\n清理与保存记录：')
        for event in status.get('audit', [])[:30]:
            lines.append(f"{event['time']} · {event['action']} · {event['detail']}")
        if status.get('error'):
            lines.append('\n后台提示：' + status['error'])
        self.status_text.setPlainText('\n'.join(lines))
        if hasattr(self, 'device_status'):
            self.device_status.setPlainText('\n'.join(f"{d['name']} · {d['id']} · {'已授权' if d['enabled'] else '已撤销'}" for d in status.get('devices', [])))

    def cleanup(self):
        if QMessageBox.question(self, '执行清理策略', '将按当前保留期限和容量水位清理符合条件的录像。确认执行？',
                                QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel) == QMessageBox.Yes:
            self.run(self.manager.cleanup, lambda result: (QMessageBox.information(self, '清理完成', f"已清理 {result['deleted']} 条录像"), self.refresh_library(), self.refresh_status()))

    def _order_tab(self):
        page = QWidget()
        form = QFormLayout(page)
        self.tabs.addTab(page, '订单提醒')
        self.order_fields = {}
        for key, label in (('tracking_number', '快递单号'), ('buyer_remark', '买家留言'), ('seller_remark', '卖家备注'), ('product_summary', '商品摘要')):
            edit = QLineEdit()
            self.order_fields[key] = edit
            form.addRow(label, edit)
        quantity = QSpinBox()
        quantity.setRange(1, 10000)
        self.order_fields['quantity'] = quantity
        form.addRow('商品总件数', quantity)
        refund = QComboBox()
        for text, value in (('未知', 'unknown'), ('无退款', 'none'), ('退款中', 'pending'), ('已退款', 'refunded')):
            refund.addItem(text, value)
        self.order_fields['refund_status'] = refund
        form.addRow('退款状态', refund)
        button = QPushButton('保存订单提醒')
        button.clicked.connect(self.save_order)
        form.addRow(button)
        form.addRow(QLabel('扫码开始录制后提醒多件商品、留言和退款状态。\n平台 / XLSX / 油猴适配预留统一入口。退款播报不代替平台出库控制。'))

    def save_order(self):
        values = {key: widget.text().strip() for key, widget in self.order_fields.items() if isinstance(widget, QLineEdit)}
        values.update(quantity=self.order_fields['quantity'].value(), refund_status=self.order_fields['refund_status'].currentData(), source='manual')
        self.run(lambda: self.manager.save_orders([values]), lambda _: QMessageBox.information(self, '已保存', '订单信息已保存，匹配单号开始录制时播报。'))

    def _device_tab(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        self.tabs.addTab(page, '工位接入')
        layout.addWidget(QLabel('当前默认本机与中心一体。上传密钥绑定工位 ID，未来多工位使用；Go 管理中心另行开发。'))
        self.device_id, self.device_name = QLineEdit(), QLineEdit()
        self.device_id.setPlaceholderText('目标工位 ID')
        self.device_name.setPlaceholderText('目标工位名称')
        layout.addWidget(self.device_id)
        layout.addWidget(self.device_name)
        row = QHBoxLayout()
        self._button(row, '签发 / 轮换上传密钥', self.issue_device)
        self._button(row, '撤销工位', self.revoke_device)
        if hasattr(self.manager, 'viewer_access'):
            self._button(row, '获取查看密钥', lambda: self.run(self.manager.viewer_access,
                lambda value: QMessageBox.information(self, '查看密钥', value['viewer_token'])))
        layout.addLayout(row)
        self.device_status = QTextEdit()
        self.device_status.setReadOnly(True)
        layout.addWidget(self.device_status)

    def issue_device(self):
        identifier, name = self.device_id.text().strip(), self.device_name.text().strip()
        self.run(lambda: self.manager.issue_device(identifier, name),
                 lambda result: QMessageBox.information(self, '上传密钥（请保存）', result['token']))

    def revoke_device(self):
        identifier = self.device_id.text().strip()
        self.run(lambda: self.manager.revoke_device(identifier), lambda _: self.refresh_status())

    def closeEvent(self, event):
        self.timer.stop()
        super().closeEvent(event)
