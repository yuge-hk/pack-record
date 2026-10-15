"""First-run wizard: explicit device configuration, real capture and human review."""
from concurrent.futures import ThreadPoolExecutor

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import QWizard, QWizardPage, QVBoxLayout, QLabel, QPushButton, QCheckBox, QMessageBox

from management_dialog import PlaybackDialog


class VerificationPage(QWizardPage):
    def __init__(self, wizard):
        super().__init__()
        self.wizard = wizard
        self.setTitle('实际试录与检查')
        layout = QVBoxLayout(self)
        self.message = QLabel('录像工位需要实际试录并检查回放；保存中心 / 查看角色不要求摄像头。')
        self.message.setWordWrap(True)
        layout.addWidget(self.message)
        self.test_button = QPushButton('按当前设置录制约 3 秒（不计入正式录像）')
        self.test_button.clicked.connect(wizard.test_capture)
        layout.addWidget(self.test_button)
        self.play_button = QPushButton('回放试录文件')
        self.play_button.setEnabled(False)
        self.play_button.clicked.connect(wizard.play_test)
        layout.addWidget(self.play_button)
        speech = QPushButton('测试中文语音提醒')
        speech.clicked.connect(lambda: wizard.app.speech.say(dict(text='语音测试，注意此单共三件商品，请核对数量', warning=False)))
        layout.addWidget(speech)
        self.confirm = QCheckBox('我已检查取景、文字清晰度、声音和语音提醒（未启用的项目除外）')
        self.confirm.toggled.connect(self.completeChanged)
        layout.addWidget(self.confirm)

    def initializePage(self):
        capture = self.wizard.app.settings['role'] in ('integrated', 'station')
        self.test_button.setVisible(capture)
        self.play_button.setVisible(capture)
        self.confirm.setText('我已检查取景、文字清晰度、声音和语音提醒（未启用的项目除外）' if capture else
                             '我已确认运行角色和保存目录；中心局域网访问需按部署说明启用')
        self.completeChanged.emit()

    def isComplete(self):
        if not self.confirm.isChecked():
            return False
        if self.wizard.app.settings['role'] not in ('integrated', 'station'):
            return True
        return self.wizard.test_ready


class SetupWizard(QWizard):
    done_work = Signal(object, object, object)

    def __init__(self, app):
        super().__init__(app)
        self.app, self.manager = app, app.controller.manager
        self.test_id, self.test_ready, self.busy = None, False, False
        self.disposed = False
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix='setup-wizard')
        self.done_work.connect(self._done, Qt.QueuedConnection)
        self.setWindowTitle('PackRecord 首次配置')
        self.resize(720, 450)
        self.setButtonText(QWizard.NextButton, '下一步')
        self.setButtonText(QWizard.BackButton, '上一步')
        self.setButtonText(QWizard.FinishButton, '完成配置')
        self.setButtonText(QWizard.CancelButton, '稍后配置')
        intro = QWizardPage()
        intro.setTitle('设置工位与保存中心')
        layout = QVBoxLayout(intro)
        text = QLabel('默认本机录像与中心保存一体。先设置工位名称、设备、编码方式和录像目录。\n'
                      '没有可用 Intel 显卡时请手动选择 H.264 CPU。\n'
                      '上传和自动清理默认关闭，可以完成试录后再启用。')
        text.setWordWrap(True)
        layout.addWidget(text)
        configure = QPushButton('打开工位、设备与存储设置')
        configure.clicked.connect(self.configure)
        layout.addWidget(configure)
        self.addPage(intro)
        self.verification = VerificationPage(self)
        self.addPage(self.verification)
        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self.poll_test)

    def configure(self):
        self.app.open_settings()
        self.test_ready = False
        self.verification.confirm.setChecked(False)
        self.verification.play_button.setEnabled(False)

    def run(self, function, callback):
        if self.busy or self.disposed:
            return
        self.busy = True
        def work():
            try:
                result, error = function(), None
            except Exception as exc:
                result, error = None, str(exc)
            if not self.disposed:
                self.done_work.emit(callback, result, error)
        self.pool.submit(work)

    def _done(self, callback, result, error):
        self.busy = False
        if self.disposed:
            return
        if error:
            self.verification.message.setText(error)
            self.verification.test_button.setEnabled(True)
        else:
            callback(result)

    def test_capture(self):
        self.test_ready = False
        self.verification.confirm.setChecked(False)
        self.verification.test_button.setEnabled(False)
        self.verification.message.setText('正在打开设备并试录，请稍候…')
        self.run(self.manager.diagnostic, self._started)

    def _started(self, result):
        self.test_id = result['id']
        self.timer.start()

    def poll_test(self):
        if self.test_id and not self.busy:
            self.run(lambda: self.manager.test_result(self.test_id), self._test_state)

    def _test_state(self, result):
        if result['state'] == 'running':
            return
        self.timer.stop()
        self.verification.test_button.setEnabled(True)
        self.test_ready = result['state'] == 'ready'
        self.verification.play_button.setEnabled(self.test_ready)
        self.verification.message.setText(f"试录完成，约 {result.get('duration', 0):.1f} 秒。请回放并检查声音、清晰度和同步。" if self.test_ready else result.get('error', '试录失败'))
        self.verification.completeChanged.emit()

    def play_test(self):
        def play(ticket):
            dialog = PlaybackDialog(ticket, self)
            dialog.exec()
            dialog.player.stop()
            dialog.player.setSource(__import__('PySide6.QtCore', fromlist=['QUrl']).QUrl())
            dialog.deleteLater()
        self.run(lambda: self.manager.media_ticket(test_id=self.test_id), play)

    def accept(self):
        if not self.verification.isComplete():
            return
        self.run(lambda: self.manager.complete_setup(self.test_id), lambda _: self._complete())

    def _complete(self):
        self.app.settings = dict(self.app.controller.settings, setup_complete=True)
        self.timer.stop()
        self.disposed = True
        self.pool.shutdown(wait=False, cancel_futures=True)
        super().accept()

    def reject(self):
        self.timer.stop()
        self.disposed = True
        self.pool.shutdown(wait=False, cancel_futures=True)
        super().reject()
