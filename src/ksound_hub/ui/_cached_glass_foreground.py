"""Optional precomposited Glass wallpaper backing widget (IP15m8 A/B).

Only enabled by KSH_GLASS_FLAT_BACKGROUND=1, and only when the existing
StaticBlurLabel's offscreen blur is active. No audio or meter behavior changes.
"""
from __future__ import annotations

from collections.abc import Callable
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPixmap
from PySide6.QtWidgets import QWidget


class CachedGlassForeground(QWidget):
    """Opaque foreground that paints a cached wallpaper + black tint image.

    Child widgets (cards, sliders, meters) retain their original hierarchy.
    Qt need not recompose the three full-window layers under each meter repaint.
    """

    def __init__(self, source_label, darkness_getter: Callable[[], int], parent=None):
        super().__init__(parent)
        self._source_label = source_label
        self._darkness_getter = darkness_getter
        self._backdrop = QPixmap()
        self._backdrop_key = None
        self.setAttribute(Qt.WA_OpaquePaintEvent, True)
        self.setAutoFillBackground(False)

    def invalidate_background(self) -> None:
        self._backdrop_key = None
        self.update()

    def resizeEvent(self, event) -> None:
        self._backdrop_key = None
        super().resizeEvent(event)

    def _ensure_backdrop(self) -> None:
        w, h = self.width(), self.height()
        if w < 1 or h < 1:
            return
        source = self._source_label.pixmap()
        source_key = source.cacheKey() if source is not None and not source.isNull() else None
        darkness = max(0, min(255, int(self._darkness_getter())))
        dpr = max(1.0, self.devicePixelRatioF())
        key = (w, h, round(dpr, 3), source_key, darkness)
        if key == self._backdrop_key and not self._backdrop.isNull():
            return
        image = QPixmap(max(1, round(w * dpr)), max(1, round(h * dpr)))
        image.setDevicePixelRatio(dpr)
        image.fill(QColor('#02050a'))
        painter = QPainter(image)
        try:
            if source_key is not None:
                # QLabel.setScaledContents(True) scales the pixmap to its rect.
                painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
                painter.drawPixmap(QRectF(0, 0, w, h), source,
                                   QRectF(0, 0, source.width(), source.height()))
            painter.fillRect(QRectF(0, 0, w, h), QColor(0, 0, 0, darkness))
        finally:
            painter.end()
        self._backdrop = image
        self._backdrop_key = key

    def paintEvent(self, event) -> None:
        self._ensure_backdrop()
        painter = QPainter(self)
        try:
            if self._backdrop.isNull():
                painter.fillRect(event.rect(), QColor('#02050a'))
            else:
                # Only blit the dirty portion, not the whole window image.
                dirty = event.rect()
                painter.setClipRegion(event.region())
                dpr = self._backdrop.devicePixelRatioF()
                painter.drawPixmap(QRectF(dirty), self._backdrop,
                                   QRectF(dirty.x() * dpr, dirty.y() * dpr,
                                          dirty.width() * dpr, dirty.height() * dpr))
        finally:
            painter.end()
