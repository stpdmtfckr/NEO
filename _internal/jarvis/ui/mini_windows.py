"""On-demand browser and lightweight image viewer. No WebEngine startup import."""
from __future__ import annotations

from pathlib import Path
import uuid
from urllib.parse import quote_plus

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices, QImageReader, QPixmap
from PySide6.QtWidgets import QCheckBox, QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMessageBox, QPushButton, QScrollArea, QSlider, QVBoxLayout, QWidget

from jarvis.ui.bookshelf import restore_visible_geometry
from jarvis.ui.desktop_strings import dt
from jarvis.language import language_code


def safe_browser_url(value: str) -> QUrl:
    text = str(value or "").strip()
    if "://" not in text and "." in text and " " not in text:
        text = "https://" + text
    url = QUrl(text)
    if url.scheme() in {"http", "https"} and url.host():
        return url
    if url.scheme():
        raise ValueError("Only HTTP and HTTPS are allowed in the mini browser")
    return QUrl("https://www.google.com/search?q=" + quote_plus(text))


class FloatingWindow(QMainWindow):
    def __init__(self, config, key):
        super().__init__(None, Qt.Tool)
        self.config, self.key, self.language = config, key, language_code(config)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.setMinimumSize(320, 220)
        self.setWindowTitle(dt(key, self.language))
        root = QWidget()
        self.layout_box = QVBoxLayout(root)
        self.setCentralWidget(root)
        self.bar = QHBoxLayout()
        self.pin = QCheckBox(dt("pin", self.language))
        self.pin.toggled.connect(self.set_pinned)
        self.bar.addWidget(self.pin)
        self.opacity = QSlider(Qt.Horizontal)
        self.opacity.setRange(25, 100)
        self.opacity.setValue(100)
        self.opacity.setToolTip(dt("opacity", self.language))
        self.opacity.setMaximumWidth(100)
        self.opacity.valueChanged.connect(lambda value: self.setWindowOpacity(value / 100))
        self.bar.addWidget(self.opacity)
        self.layout_box.addLayout(self.bar)
        state = config.get("desktop.mini_windows." + key, {}) or {}
        restore_visible_geometry(self, {"size": [700, 520], **state})
        self.pin.setChecked(bool(state.get("pinned", False)))
        self.opacity.setValue(int(state.get("opacity", 100)))

    def set_pinned(self, value):
        visible = self.isVisible()
        self.setWindowFlag(Qt.WindowStaysOnTopHint, bool(value))
        if visible:
            self.show()

    def closeEvent(self, event):
        desktop = self.config.data.setdefault("desktop", {}).setdefault("mini_windows", {})
        desktop[self.key] = {"size": [self.width(), self.height()], "position": [self.x(), self.y()], "screen": self.screen().name() if self.screen() else "", "pinned": self.pin.isChecked(), "opacity": self.opacity.value()}
        self.config.save(self.config.data)
        super().closeEvent(event)


class ImageViewer(FloatingWindow):
    def __init__(self, config, path, viewer_id=""):
        super().__init__(config, "image")
        self.viewer_id = str(viewer_id or uuid.uuid4().hex)
        self._zoom_factor = 1.0
        reader = QImageReader(str(path))
        size = reader.size()
        if not size.isValid() or size.width() * size.height() > 40_000_000:
            raise ValueError("Image dimensions exceed viewer limit")
        image = reader.read()
        if image.isNull():
            raise ValueError("Image could not be decoded")
        self.source = QPixmap.fromImage(image)
        self.label = QLabel()
        self.label.setAlignment(Qt.AlignCenter)
        self.label.setMinimumSize(1, 1)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(False)
        self.scroll.setAlignment(Qt.AlignCenter)
        self.scroll.setWidget(self.label)
        self.layout_box.addWidget(self.scroll, 1)
        self.setWindowTitle(Path(path).name)

    def set_zoom_factor(self, value):
        self._zoom_factor = max(0.25, min(3.0, float(value)))
        self._render_image()

    def zoom(self, delta):
        self.set_zoom_factor(self._zoom_factor + float(delta))

    def _render_image(self):
        if not hasattr(self, "scroll"):
            return
        target = self.source.size().scaled(self.scroll.viewport().size(), Qt.KeepAspectRatio)
        target *= self._zoom_factor
        pixmap = self.source.scaled(target, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self.label.setPixmap(pixmap)
        self.label.resize(pixmap.size())

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._render_image()


class MiniBrowser(FloatingWindow):
    def __init__(self, config, url="https://www.google.com"):
        # Import only when explicitly requested; ordinary listening never starts Chromium.
        from PySide6.QtWebEngineWidgets import QWebEngineView
        from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile, QWebEngineSettings
        class WebPage(QWebEnginePage):
            def acceptNavigationRequest(self, target, navigation_type, is_main_frame):
                return target.scheme() in {"https", "http", "about"}
        super().__init__(config, "browser")
        self.address = QLineEdit()
        self.address.setPlaceholderText(dt("address", self.language))
        self.bar.insertWidget(0, self.address, 1)
        self.view = QWebEngineView()
        self.profile = QWebEngineProfile(self)  # private, per-window off-the-record profile
        self.page = WebPage(self.profile, self.view)
        self.view.setPage(self.page)
        self.view.settings().setAttribute(QWebEngineSettings.PlaybackRequiresUserGesture, True)
        # New browser capabilities are never granted silently.
        self.page.featurePermissionRequested.connect(lambda origin, feature: self.page.setFeaturePermission(origin, feature, QWebEnginePage.PermissionDeniedByUser))
        self.profile.downloadRequested.connect(lambda item: item.cancel())
        self.address.returnPressed.connect(lambda: self.navigate(self.address.text()))
        self.view.urlChanged.connect(lambda value: self.address.setText(value.toString()))
        for symbol, callback in (("←", self.view.back), ("→", self.view.forward), ("↻", self.view.reload), ("−", lambda: self.zoom(-0.1)), ("+", lambda: self.zoom(0.1))):
            button = QPushButton(symbol)
            button.setMaximumWidth(35)
            button.clicked.connect(callback)
            self.bar.addWidget(button)
        self.layout_box.addWidget(self.view, 1)
        self.navigate(url)

    def navigate(self, url):
        self.view.setUrl(safe_browser_url(url))

    def zoom(self, delta):
        self.view.setZoomFactor(max(0.25, min(3.0, self.view.zoomFactor() + float(delta))))

    def closeEvent(self, event):
        self.view.stop()
        self.view.setUrl(QUrl("about:blank"))
        super().closeEvent(event)
