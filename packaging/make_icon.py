#!/usr/bin/env python3
"""Render the 256x256 app icon (USB stick + download arrow) with Qt."""
import os
import sys

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QGuiApplication, QImage, QLinearGradient, QPainter, QPen, QPolygonF

app = QGuiApplication(sys.argv[:1])
S = 256
img = QImage(S, S, QImage.Format_ARGB32)
img.fill(Qt.transparent)
p = QPainter(img)
p.setRenderHint(QPainter.Antialiasing)

g = QLinearGradient(0, 0, S, S)
g.setColorAt(0, QColor("#2b8cff"))
g.setColorAt(1, QColor("#1747b8"))
p.setBrush(QBrush(g))
p.setPen(Qt.NoPen)
p.drawRoundedRect(QRectF(8, 8, S - 16, S - 16), 48, 48)

# four tiles (generic "windows" hint, not the logo)
p.setBrush(QColor(255, 255, 255, 60))
for x, y in ((40, 40), (84, 40), (40, 84), (84, 84)):
    p.drawRoundedRect(QRectF(x, y, 38, 38), 6, 6)

# USB stick body
p.setBrush(QColor("#ffffff"))
p.drawRoundedRect(QRectF(118, 96, 82, 120), 16, 16)
p.setBrush(QColor("#dfe7f5"))
p.drawRect(QRectF(134, 62, 50, 38))
p.setBrush(QColor("#1747b8"))
p.drawRect(QRectF(144, 72, 10, 10))
p.drawRect(QRectF(164, 72, 10, 10))

# arrow
p.setBrush(QColor("#1f6fe8"))
p.drawRect(QRectF(151, 120, 16, 44))
p.drawPolygon(QPolygonF([QPointF(135, 160), QPointF(183, 160), QPointF(159, 192)]))
p.end()

out = sys.argv[1] if len(sys.argv) > 1 else "winusb-steamos.png"
img.save(out)
print("icon written:", out)
