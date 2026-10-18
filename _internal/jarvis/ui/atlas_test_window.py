"""Dedicated QA front-end: the production Agent, ChatView and RobotWidget.

One queued signal / GUI acknowledgement per step prevents unlimited UI events.
All widget access is in the GUI thread. Stop waits for current finite step and
its owned cleanup; no Thread.terminate and no 'undo last user action'.
"""
from __future__ import annotations
import threading
from PySide6.QtCore import QTimer, QObject, Signal, Qt
from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPushButton, QSpinBox
from jarvis.testing.atlas_session import atlas_cases
from jarvis.ui.chat_view import ChatView
from jarvis.ui.robot_widget import RobotWidget


class _Signals(QObject):
    step = Signal(object)
    finished = Signal(object)


class AtlasTestWindow(QWidget):
    def __init__(self, session, locales, selection, limit, live):
        super().__init__()
        self.session, self.locales = session, locales
        self.selection, self.limit, self.live = selection, limit, live
        self._done = False
        self._ack = threading.Event()
        self._delay = 0
        self._signals = _Signals(self)
        self._signals.step.connect(self._step, Qt.QueuedConnection)
        self._signals.finished.connect(self._finished, Qt.QueuedConnection)
        self.setWindowTitle('NEO 4.0.20.146 — проверка фразатласа / Atlas QA')
        self.resize(850,740)
        layout = QVBoxLayout(self)
        notice = QLabel('Отдельный тестовый профиль. Все фразы проходят настоящий Agent и шлюз.\n'
                        'Обычные действия заблокированы; LIVE — только собственные тестовые таймеры/файлы/кубик.\n'
                        'Микрофон, ваши программы и музыкальные аккаунты этот прогон не проверяет.')
        notice.setWordWrap(True); layout.addWidget(notice)
        self.robot = RobotWidget(parent=self); self.robot.setFixedHeight(140); layout.addWidget(self.robot)
        self.progress = QLabel('Подготовка…'); layout.addWidget(self.progress)
        controls = QHBoxLayout(); layout.addLayout(controls)
        self.stop_button = QPushButton('Остановить после текущей команды')
        self.stop_button.clicked.connect(self._stop); controls.addWidget(self.stop_button)
        controls.addWidget(QLabel('Пауза между фразами, мс:'))
        delay = QSpinBox(); delay.setRange(0,2000); delay.setValue(0)
        delay.valueChanged.connect(lambda value: setattr(self,'_delay',value)); controls.addWidget(delay)
        self.chat = ChatView(); layout.addWidget(self.chat,1)
        self.path = QLabel(str(session.output)); self.path.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.path.setWordWrap(True); layout.addWidget(self.path)
        QTimer.singleShot(100,self._start)

    def _start(self):
        self._thread = threading.Thread(target=self._run,name='NEO-Atlas-QA',daemon=False)
        self._thread.start()

    def _show(self, row):
        self._ack.clear()
        self._signals.step.emit(row)
        # Do not enqueue the next phrase until the GUI consumed this event.
        while not self._ack.wait(.1):
            if self.session.stop.is_set(): return

    def _run(self):
        try:
            for index, case in enumerate(atlas_cases(self.locales,self.selection)):
                if self.session.stop.is_set() or (self.limit and index >= self.limit): break
                self._show({'stage':'input','locale':case.locale,'input':case.phrase,'index':index+1})
                if self.session.stop.is_set(): break
                self._show(self.session.check(case))
                if self.session.stop.wait(self._delay/1000): break
            if self.live and not self.session.stop.is_set():
                for row in self.session.live_fixtures(self.locales): self._show(row)
                for row in self.session.file_fixtures(self.locales): self._show(row)
        except Exception as exc:
            self.session.record({'status':'ERROR','response':f'{type(exc).__name__}: {exc}'})
        finally:
            self._signals.finished.emit(self.session.finish())

    def _step(self,row):
        try:
            if len(self.chat.items)>160: self.chat.clear_messages()
            self.chat.set_language(row.get('locale','ru'))
            if row.get('stage')=='input':
                self.chat.add_user(row['input']); self.robot.set_state('thinking')
                self.progress.setText(f"{row['index']} · {row['locale']} · {self.selection}")
            else:
                if row.get('execution')=='real-owned-fixture' and row.get('input'):
                    self.chat.add_user(row['input'])
                self.chat.add_assistant(f"[{row['status']}] {row.get('response','')}")
                self.robot.set_state('idle')
        finally:
            self._ack.set()

    def _stop(self):
        self.session.stop.set(); self._ack.set()
        self.stop_button.setEnabled(False)
        self.progress.setText('Остановка: завершаю текущую проверку и восстановление…')

    def _finished(self,summary):
        self._done=True; self.stop_button.setEnabled(False)
        self.robot.set_state('idle')
        self.progress.setText('Готово. '+', '.join(f'{key}: {value}' for key,value in summary['counts'].items()))
        self.chat.add_assistant('Журналы сохранены в папке ниже. NO_ROUTE/NEEDS_INPUT не означают выполненное действие.')

    def closeEvent(self,event):
        if not self._done:
            self._stop(); event.ignore()
        else:
            event.accept()
