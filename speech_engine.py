"""Desktop-only speech; headless services publish events instead of playing audio."""
import time
from datetime import datetime, timezone
from collections import deque

from PySide6.QtCore import QObject, QLocale, QTimer, Signal
from PySide6.QtTextToSpeech import QTextToSpeech


class SpeechEngine(QObject):
    unavailable = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.queue = deque(maxlen=8)
        self.engine = QTextToSpeech('speechd', self)
        self.engine.setLocale(QLocale('zh_CN'))
        self.engine.stateChanged.connect(self._state)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._next)
        self.timer.start(250)
        self.reported_error = False
        QTimer.singleShot(0, lambda: self._state(self.engine.state()))

    def say(self, event):
        if event.get('time'):
            try:
                if (datetime.now(timezone.utc) - datetime.fromisoformat(event['time'])).total_seconds() > 30:
                    return
            except (ValueError, TypeError):
                return
        text = str(event.get('text', ''))[:600]
        if not text:
            return
        item = (time.monotonic(), text)
        if event.get('warning'):
            self.queue.clear()
            self.engine.stop()
            self.queue.appendleft(item)
        else:
            self.queue.append(item)
        self._next()

    def _state(self, state):
        if state == QTextToSpeech.State.Error and not self.reported_error:
            self.reported_error = True
            self.unavailable.emit('语音引擎不可用，请在向导中检查系统中文语音服务：' + self.engine.errorString())

    def _next(self):
        if self.engine.state() != QTextToSpeech.State.Ready:
            return
        while self.queue:
            stamp, text = self.queue.popleft()
            if time.monotonic() - stamp < 30:
                self.engine.say(text)
                break

    def clear(self):
        self.queue.clear()
        self.engine.stop()

    def close(self):
        self.timer.stop()
        self.clear()
