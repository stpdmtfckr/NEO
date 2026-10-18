"""A separate Voice section. Worker lifetime never depends on Qt widget lifetime."""
from __future__ import annotations
from PySide6.QtCore import QTimer, QEvent
from PySide6.QtWidgets import QWidget, QVBoxLayout, QFormLayout, QGridLayout, QLabel, QCheckBox, QComboBox, QSpinBox, QPushButton, QProgressBar, QMessageBox, QSizePolicy
from jarvis.response_voice.service import VoiceService, options
from jarvis.response_voice.text import text, diagnostic_text


class ResponseVoicePage(QWidget):
    def __init__(self, config, parent=None, *, service=None):
        super().__init__(parent)
        self.config = config
        self.language = str(config.get('app.language','en'))
        from jarvis.response_voice.text import install_catalogue
        install_catalogue()
        self.service = service or VoiceService(config)
        self._owns_service = service is None
        self._closed = False
        self._action_columns = 0
        self._viewport = None
        self._labels = {}
        values = options(config)
        layout = QVBoxLayout(self)
        self.enabled = QCheckBox(text('enabled',self.language))
        self.enabled.setChecked(values['enabled']); layout.addWidget(self.enabled)
        form = QFormLayout(); layout.addLayout(form)
        form.setRowWrapPolicy(QFormLayout.WrapLongRows)
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        # NEO 185: light / medium / heavy voice model.
        self.tier = QComboBox()
        self.tier.setProperty('i18n_skip_combo', True)
        for key in ('light', 'standard', 'heavy'): self.tier.addItem(text('tier_' + key, self.language), key)
        self.tier.setCurrentIndex(max(0, self.tier.findData(values['tier'])))
        self._labels['tier'] = QLabel(); self._labels['tier'].setWordWrap(True)
        form.addRow(self._labels['tier'], self.tier)
        self.tier.currentIndexChanged.connect(self._tier_changed)
        self.speaker = QComboBox()
        self.speaker.setProperty('i18n_skip_combo', True)
        for key, speaker in [('male','Aiden'),('female','Serena')]: self.speaker.addItem(text(key,self.language),speaker)
        self.speaker.setCurrentIndex(max(0,self.speaker.findData(values['speaker'])))
        self._labels['speaker'] = QLabel()
        self._labels['speaker'].setWordWrap(True)
        form.addRow(self._labels['speaker'],self.speaker)
        self.fields = {}
        for key,label,low,high in [('robot_effect','robot',0,100),('volume','volume',0,100),('cpu_threads','threads',1,4),('idle_unload_seconds','idle',0,120),('max_characters','limit',100,2000)]:
            field = QSpinBox(); field.setRange(low,high); field.setValue(values[key])
            field.setAccessibleName(text(label,self.language))
            field.setToolTip(text(label,self.language))
            row_label = QLabel(); row_label.setWordWrap(True)
            self._labels[label] = row_label
            form.addRow(row_label,field); self.fields[key]=field
        self.status = QLabel(); self.status.setWordWrap(True); layout.addWidget(self.status)
        self.progress = QProgressBar(); self.progress.setRange(0,100); layout.addWidget(self.progress)
        self.actions = QWidget()
        self.action_grid = QGridLayout(self.actions)
        self.action_grid.setContentsMargins(0,0,0,0)
        layout.addWidget(self.actions)
        self.buttons = {}
        for index,key in enumerate(('download','cancel','check','delete','preview','stop')):
            button = QPushButton(text(key,self.language)); button.setAutoDefault(False)
            button.setSizePolicy(QSizePolicy.Expanding,QSizePolicy.Fixed)
            self.action_grid.addWidget(button,index,0); self.buttons[key]=button
        self.buttons['download'].clicked.connect(lambda: self._operation('download'))
        self.buttons['check'].clicked.connect(lambda: self._operation('check'))
        self.buttons['delete'].clicked.connect(self._delete)
        self.buttons['cancel'].clicked.connect(self.service.cancel_operation)
        self.buttons['stop'].clicked.connect(self.service.cancel)
        self.buttons['preview'].clicked.connect(self._preview)
        self.help_label=QLabel(); self.help_label.setWordWrap(True); layout.addWidget(self.help_label)
        layout.addStretch(1)
        self._retranslate()
        self.timer=QTimer(self); self.timer.setInterval(200); self.timer.timeout.connect(self.refresh); self.timer.start()
        self.refresh()

    def _set_text(self, widget, key):
        # Keep a stable Russian source for the dialog-wide translation pass too.
        widget.setProperty('i18n_text_source', text(key,'ru'))
        widget.setText(text(key,self.language))

    def _retranslate(self):
        self.language = str(self.config.get('app.language','en'))
        self._set_text(self.enabled,'enabled')
        self.enabled.setAccessibleName(text('enabled',self.language))
        for key,label in self._labels.items():self._set_text(label,key)
        for index,key in enumerate(('male','female')):
            self.speaker.setItemText(index,text(key,self.language))
        self.speaker.setAccessibleName(text('speaker',self.language))
        for index,key in enumerate(('light','standard','heavy')):
            self.tier.setItemText(index,text('tier_'+key,self.language))
        self.tier.setAccessibleName(text('tier',self.language))
        for key,label in [('robot_effect','robot'),('volume','volume'),('cpu_threads','threads'),('idle_unload_seconds','idle'),('max_characters','limit')]:
            field=self.fields[key]
            field.setProperty('i18n_a11y_name_source',text(label,'ru'))
            field.setProperty('i18n_tooltip_source',text(label,'ru'))
            field.setAccessibleName(text(label,self.language))
            field.setToolTip(text(label,self.language))
        for key,button in self.buttons.items():
            self._set_text(button,key)
            button.setAccessibleName(text(key,self.language))
        self._set_help185()
        self._reflow_actions()

    def _set_help185(self):
        key={'light':'help_light','heavy':'help_heavy'}.get(self.tier.currentData(),'help')
        self._set_text(self.help_label,key)
        light=self.tier.currentData()=='light'
        # Windows voices are not files NEO owns: nothing to download, check or delete.
        for name in ('download','check','delete','cancel'):
            if hasattr(self,'buttons'):
                # UI185-VOICE-TIER-CANCEL-01: a running operation of the previous tier keeps its Cancel.
                self.buttons[name].setVisible(not light or (name=='cancel' and getattr(self,'_busy186',False)))
        self.fields['cpu_threads'].setEnabled(not light)

    def _tier_changed(self, *_):
        if self._closed:return
        self.service.select_tier(self.tier.currentData())
        self._set_help185()
        self.refresh()

    def _reflow_actions(self):
        if not hasattr(self,'buttons') or self._closed:return
        viewport=self._viewport or self.parentWidget()
        width=viewport.contentsRect().width() if viewport is not None else self.width()
        margins=self.layout().contentsMargins()
        available=max(0,width-margins.left()-margins.right())
        buttons=list(self.buttons.values())
        required=sum(max(button.sizeHint().width() for button in buttons[column::2]) for column in (0,1))
        required+=max(6,self.action_grid.horizontalSpacing())
        columns=2 if available>=required else 1
        if columns==self._action_columns:return
        self._action_columns=columns
        for button in buttons:self.action_grid.removeWidget(button)
        for index,button in enumerate(buttons):self.action_grid.addWidget(button,index//columns,index%columns)
        self.action_grid.setColumnStretch(0,1)
        self.action_grid.setColumnStretch(1,1 if columns==2 else 0)
        self.action_grid.setColumnMinimumWidth(1,0)
        self.updateGeometry()

    def showEvent(self,event):
        super().showEvent(event)
        viewport=self.parentWidget()
        if viewport is not self._viewport:
            if self._viewport is not None:self._viewport.removeEventFilter(self)
            self._viewport=viewport
            if viewport is not None:viewport.installEventFilter(self)
        self._reflow_actions()

    def resizeEvent(self,event):
        super().resizeEvent(event)
        if hasattr(self,'buttons'):self._reflow_actions()

    def eventFilter(self,watched,event):
        if watched is self._viewport and event.type()==QEvent.Resize:self._reflow_actions()
        return super().eventFilter(watched,event)

    def values(self):
        return dict(options(self.config), enabled=self.enabled.isChecked(), speaker=self.speaker.currentData(), tier=self.tier.currentData(),
                    **{key:field.value() for key,field in self.fields.items()})

    def _operation(self, action):
        if not self._closed: self.service.model_operation(action); self.refresh()

    def _delete(self):
        if QMessageBox.question(self,text('title',self.language),text('delete_confirm',self.language),
                                QMessageBox.Yes | QMessageBox.No,QMessageBox.No) == QMessageBox.Yes:
            self._operation('delete')

    def _preview(self):
        self.service.submit(text('sample',self.language),self.language,preview=True,overrides=self.values())
        self.refresh()

    def refresh(self):
        if self._closed: return
        if str(self.config.get('app.language','en')) != self.language:
            self._retranslate()
        state = self.service.snapshot()
        voice = state['voice_state']
        key = voice if voice not in ('idle','queued') else state['state']
        if key == 'download': key = 'downloading'
        if key == 'delete': key = 'checking'
        if key == 'synthesis_timeout': key = 'synthesis_failed'
        caption = text(key,self.language)
        if key == 'verified' and not state['runtime_installed']: caption += '\n' + text('runtime_missing',self.language)
        total=max(1,state['total']); done=min(total,max(0,state['done']))
        source=text(key,'ru')
        if key=='verified' and not state['runtime_installed']:source+='\n'+text('runtime_missing','ru')
        detail=diagnostic_text(state.get('failure'),self.language)
        if detail:
            caption+='\n'+detail
            source+='\n'+diagnostic_text(state.get('failure'),'ru')
        self.status.setProperty('i18n_text_source',source)
        self.status.setText(caption)
        self.progress.setRange(0, 0 if key == "runtime" else 100)
        self.progress.setValue(int(done * 100 / total))
        self.progress.setFormat(text("runtime", self.language) if key == "runtime" else f'{done / 1_000_000:.1f} / {total / 1_000_000:.1f} MB')
        busy=state['running']
        self._busy186=bool(busy)
        for key in ('download','check','delete','preview'): self.buttons[key].setEnabled(not busy)
        self.buttons['cancel'].setEnabled(busy)
        draft=self.tier.currentData()
        running_tier=getattr(self.service,'tier',None)
        running_tier=running_tier() if callable(running_tier) else running_tier
        self.buttons['cancel'].setVisible(bool(busy) or draft!='light')
        if busy and running_tier and running_tier!=draft:
            # The progress belongs to the tier whose operation is still running, not to the draft.
            owner=text('tier_'+str(running_tier),self.language)
            self.status.setText(owner+': '+self.status.text())
        # Controls stay visible while hashing is blocked; every action reports the real reason.
        self.buttons['stop'].setEnabled(state['active'] or state['playing'])

    def close_operations(self, *_):
        if self._closed: return
        self._closed=True; self.timer.stop(); self.service.cancel_operation(); self.service.cancel()
        try: self.service.select_tier(None)
        except Exception: pass
        if self._owns_service: self.service.close()
