"""K-Sounds: cache a static blurred BACKGROUND pixmap, not a live QWidget effect.

Only instantiated for Glass background_label objects. Normal QLabel behavior is
restored by launching with KSH_STATIC_BG_BLUR=0. No audio/meter model changes.
"""
from __future__ import annotations
import os
from PySide6.QtCore import QEvent, QRectF, Qt, QTimer
from PySide6.QtGui import QPainter, QPixmap
from PySide6.QtWidgets import QLabel, QGraphicsBlurEffect, QGraphicsScene


class StaticBlurLabel(QLabel):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._ksh_blur_effect = None
        self._ksh_original_pixmap = None
        self._ksh_static_blur_active = os.environ.get(
            "KSH_STATIC_BG_BLUR", "1"
        ).strip().lower() not in {"0", "no", "false", "off"}
        self._ksh_blur_render_key = None
        self._ksh_deferred = QTimer(self)
        self._ksh_deferred.setSingleShot(True)
        self._ksh_deferred.setInterval(120)
        self._ksh_deferred.timeout.connect(self._ksh_render_source)

    def setGraphicsEffect(self, effect):
        if self._ksh_static_blur_active and isinstance(effect, QGraphicsBlurEffect):
            # Keep the original effect object for its user-controlled radius,
            # but never install it on the QWidget's live repaint chain.
            self._ksh_blur_effect = effect
            existing = self.pixmap()
            if existing is not None and not existing.isNull():
                self._ksh_original_pixmap = QPixmap(existing)
            effect.blurRadiusChanged.connect(self._ksh_radius_changed)
            self._ksh_render_source()
            return
        super().setGraphicsEffect(effect)

    def setPixmap(self, pixmap):
        if not self._ksh_static_blur_active:
            return super().setPixmap(pixmap)
        if pixmap is None or pixmap.isNull():
            self._ksh_original_pixmap = None
            self._ksh_blur_render_key = None
            return super().clear()
        # Always cache the UNBLURRED input, preventing double-blur on updates.
        self._ksh_original_pixmap = QPixmap(pixmap)
        self._ksh_blur_render_key = None
        self._ksh_render_source()

    def clear(self):
        self._ksh_original_pixmap = None
        self._ksh_blur_render_key = None
        self._ksh_deferred.stop()
        return super().clear()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._ksh_static_blur_active and self._ksh_original_pixmap is not None:
            if self.hasScaledContents():
                self._ksh_deferred.start()

    def showEvent(self, event):
        super().showEvent(event)
        if self._ksh_static_blur_active and self._ksh_original_pixmap is not None:
            self._ksh_deferred.start()

    def _ksh_radius_changed(self, *_args):
        self._ksh_blur_render_key = None
        self._ksh_deferred.start()

    def _ksh_render_source(self):
        source = self._ksh_original_pixmap
        if source is None or source.isNull():
            return
        effect = self._ksh_blur_effect
        radius = float(effect.blurRadius()) if effect is not None else 0.0
        # Qt's live blur is applied AFTER QLabel's scaled-contents drawing.
        # Reproduce this ordering for background labels with scaledContents.
        scaled = bool(self.hasScaledContents() and self.width() > 1 and self.height() > 1)
        dpr = max(0.1, source.devicePixelRatioF())
        requested_size = (max(1, round(self.width() * dpr)),
                          max(1, round(self.height() * dpr))) if scaled else None
        key = (source.cacheKey(), round(radius, 3), requested_size)
        if key == self._ksh_blur_render_key:
            return
        src = source
        if requested_size is not None and (src.width(), src.height()) != requested_size:
            src = source.scaled(requested_size[0], requested_size[1],
                                Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
            src.setDevicePixelRatio(dpr)
        if radius <= 0.0:
            out = src
        else:
            # QGraphicsBlurEffect is still used, but rendered OFFSCREEN ONCE
            # per changed source/radius/size; never by the 40 Hz meter paints.
            out = QPixmap(src.size())
            out.setDevicePixelRatio(src.devicePixelRatioF())
            out.fill(Qt.transparent)
            logical_w = src.width() / max(0.1, src.devicePixelRatioF())
            logical_h = src.height() / max(0.1, src.devicePixelRatioF())
            scene = QGraphicsScene()
            scene.setSceneRect(QRectF(0, 0, logical_w, logical_h))
            item = scene.addPixmap(src)
            blur = QGraphicsBlurEffect()
            blur.setBlurRadius(radius)
            item.setGraphicsEffect(blur)
            painter = QPainter(out)
            try:
                scene.render(painter, QRectF(0, 0, logical_w, logical_h),
                             QRectF(0, 0, logical_w, logical_h))
            finally:
                painter.end()
        # QLabel receives an ordinary pixmap; no dynamic QGraphicsBlurEffect.
        super().setPixmap(out)
        self._ksh_blur_render_key = key
