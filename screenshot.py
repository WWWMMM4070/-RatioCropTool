# -*- coding: utf-8 -*-
"""比例锁定截图 / 裁剪工具（单文件，PyQt5 + Pillow）

截图裁剪与导入图片裁剪共用同一套比例锁定交互与像素级裁剪输出：
抓屏取物理像素、图片读原始像素、裁剪为像素级切片、默认输出无损 PNG。
界面取配图 d:\\Wallpaper\\png\\人行道-城市-夜晚.png 的夜空做横幅并派生整套配色，缺图自动降级。

依赖：pip install PyQt5 Pillow        运行：python screenshot.py
打包：pyinstaller -F -w -n RatioCropTool --add-data "d:\\Wallpaper\\png\\人行道-城市-夜晚.png;." screenshot.py
验证：conda picture（Py 3.11.17 + PyQt5 5.15.10 + Pillow 9.5.0）、base（Py 3.13.13 + 5.15.11 + 12.3.0）
"""
import ctypes
import os
import sys


# ---- Windows 高DPI感知：必须早于 QApplication 创建 ----
def dpi_aware():
    """否则 125%/150% 下拿到的是被系统放大的逻辑分辨率，画面发虚、坐标偏移。"""
    if sys.platform != "win32":
        return
    try:                    # Win10 1703+：Per-Monitor V2
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:       # 回退：Per-Monitor → System
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()


dpi_aware()

try:
    from PIL import Image
    from PyQt5.QtCore import Qt, QEvent, QRect, QRectF, QPointF, QTimer
    from PyQt5.QtGui import QImage, QPainter, QPixmap, QColor, QFont, QPen, QCursor
    from PyQt5.QtWidgets import (QApplication, QWidget, QDialog, QComboBox, QSpinBox, QLabel,
                                 QPushButton, QHBoxLayout, QVBoxLayout, QGridLayout,
                                 QFileDialog, QMessageBox)
except ImportError as ex:
    sys.exit("缺少依赖 %s\n当前解释器: %s\n请在该环境执行: python -m pip install PyQt5 Pillow"
             % (ex.name, sys.executable))

# 让 125%/150% 用真实小数缩放比，保证「逻辑坐标 × 缩放比 = 物理像素」的映射精确
if hasattr(QApplication, "setHighDpiScaleFactorRoundingPolicy"):
    QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)

MIN_PX = 10                                     # 选区最小边长（像素）
ART = r"d:\Wallpaper\png\人行道-城市-夜晚.png"    # 界面配图
BASE_AVG = (31, 61, 96)                         # 配图主色（缺图时的兜底）


# ---- Pillow <-> Qt：只做原始像素拷贝 ----
def pil_to_qimage(im):
    im = im if im.mode == "RGBA" else im.convert("RGBA")
    return QImage(im.tobytes("raw", "RGBA"), im.width, im.height,
                  QImage.Format_RGBA8888).copy()


def qimage_to_pil(img):
    img = img.convertToFormat(QImage.Format_RGBA8888)
    p = img.constBits()
    p.setsize(img.sizeInBytes())
    return Image.frombytes("RGBA", (img.width(), img.height()), bytes(p))


# ---- 屏幕捕获：完整虚拟桌面的物理像素原始画面 ----
def grab_desktop():
    """返回 (逻辑虚拟桌面矩形, 物理像素整屏图)。
    Windows 优先 GDI 抓整个虚拟桌面（物理像素、多屏坐标统一），取不到时回退 Qt 逐屏拼接。
    """
    vs = QRect()
    for s in QApplication.screens():
        vs = QRect(s.geometry()) if vs.isNull() else vs.united(s.geometry())
    if sys.platform == "win32":
        try:
            from PIL import ImageGrab
            return vs, ImageGrab.grab(all_screens=True).convert("RGB")
        except Exception:
            pass
    dpr = QApplication.primaryScreen().devicePixelRatio()
    shot = Image.new("RGB", (round(vs.width() * dpr), round(vs.height() * dpr)))
    for s in QApplication.screens():
        g = s.geometry()
        q = s.grabWindow(0).toImage()
        q.setDevicePixelRatio(1.0)              # 去掉缩放标记 -> 物理像素
        shot.paste(qimage_to_pil(q).convert("RGB"),
                   (round((g.x() - vs.x()) * dpr), round((g.y() - vs.y()) * dpr)))
    return vs, shot


# ---- 图像源：同一份原始像素既用于显示，也用于裁剪 ----
class Source:
    """area=图在控件中的逻辑矩形，pil=原始像素图，scale=原始像素/逻辑尺寸（缩放比）。
    交互用逻辑坐标，只在绘制与输出时换算物理像素；裁剪是 PIL 直接切片，无缩放无插值。
    """

    def __init__(self, area, pil):
        self.area, self.pil, self.qimg = QRectF(area), pil, pil_to_qimage(pil)

    def scale(self):
        return (self.pil.width / self.area.width(), self.pil.height / self.area.height())

    def draw(self, p, rect=None, opacity=1.0):
        """rect=None 画整幅；否则只画 rect 对应的那部分原始像素"""
        p.save()
        p.setOpacity(opacity)
        if rect is None:
            p.drawImage(self.area, self.qimg)
        else:
            sx, sy = self.scale()
            p.drawImage(rect, self.qimg, QRectF((rect.x() - self.area.x()) * sx,
                                                (rect.y() - self.area.y()) * sy,
                                                rect.width() * sx, rect.height() * sy))
        p.restore()

    def _box(self, r, wh=None):
        """逻辑选区 → 原图整型切片框（坐标映射唯一入口）：
        ① 逻辑坐标 × 缩放比 = 物理像素  ② 夹进原图且至少 1 像素
        ③ 取整会有零点几像素误差，这里按整数比例 rw:rh 对齐：在「内收 / 外扩」两档里
           取与原选区偏差最小的一档，使输出比例严格等于设定值。
        """
        sx, sy = self.scale()
        w, h = self.pil.size
        b = [round((r.left() - self.area.left()) * sx), round((r.top() - self.area.top()) * sy),
             round((r.right() - self.area.left()) * sx), round((r.bottom() - self.area.top()) * sy)]
        b[0], b[1] = max(0, min(b[0], w - 1)), max(0, min(b[1], h - 1))
        b[2], b[3] = max(b[0] + 1, min(b[2], w)), max(b[1] + 1, min(b[3], h))
        if wh:
            rw, rh = wh
            cw, ch = b[2] - b[0], b[3] - b[1]
            n, best = int(min(cw / rw, ch / rh)), None
            for k in (n, n + 1):
                nw, nh = k * rw, k * rh
                if k < 1 or nw < MIN_PX or nh < MIN_PX or b[0] + nw > w or b[1] + nh > h:
                    continue
                dev = max(abs(nw - cw), abs(nh - ch))
                if best is None or dev < best[0]:
                    best = (dev, nw, nh)
            if best:
                b[2], b[3] = b[0] + best[1], b[1] + best[2]
        return b

    def size_of(self, r, wh=None):
        """选区的物理像素尺寸（界面读数 = 实际输出）"""
        b = self._box(r, wh)
        return b[2] - b[0], b[3] - b[1]

    def crop(self, r, wh=None):
        """像素级切片输出（PIL 直接取原始像素，无损）"""
        return self.pil.crop(tuple(self._box(r, wh)))


# ---- 比例锁定裁剪控件（截图遮罩 / 图片裁剪窗口共用）----
class CropView(QWidget):
    """拖拽建区 · 整体移动 · 拖四边 · 拖四角，全程锁比例；交互用逻辑坐标，输出时换算物理像素。"""
    CURSORS = dict(l=Qt.SizeHorCursor, r=Qt.SizeHorCursor, t=Qt.SizeVerCursor, b=Qt.SizeVerCursor,
                   tl=Qt.SizeFDiagCursor, br=Qt.SizeFDiagCursor, tr=Qt.SizeBDiagCursor,
                   bl=Qt.SizeBDiagCursor, move=Qt.SizeAllCursor)

    def __init__(self, src, ratio, wh, done, cancel):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.src, self.ratio, self.wh, self.done, self.cancel = src, ratio, wh, done, cancel
        self.sel, self.act = QRectF(), None         # 选区（逻辑坐标）/ 当前操作
        self.start, self.base = QPointF(), QRectF()
        self.finished = False                       # 是否已确认/取消，防止重复回调
        self.setMouseTracking(True)
        self.setCursor(Qt.CrossCursor)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setAttribute(Qt.WA_DeleteOnClose)

    def _place(self, aw, ah, w, h, ax, ay, dx, dy):
        """几何核心：① 按比例由宽推高 ② 越界等比缩小 ③ 最小尺寸兜底。
        aw/ah=锚点两方向的可用空间；dx/dy：1=向右下展开，-1=向左上展开，0=以锚点居中。
        """
        if self.ratio:
            w = max(w, max(MIN_PX, MIN_PX * self.ratio))
            h = w / self.ratio
            k = min(1.0, aw / w, ah / h)
            w, h = w * k, h * k
        else:
            w, h = min(max(w, MIN_PX), aw), min(max(h, MIN_PX), ah)
        return QRectF(ax if dx > 0 else (ax - w if dx < 0 else ax - w / 2),
                      ay if dy > 0 else (ay - h if dy < 0 else ay - h / 2), w, h)

    def _drag(self, pos):
        """新建 / 平移 / 四边 / 四角，全部汇到 _place 统一锁比例与边界"""
        b = QRectF(self.rect())
        if self.act == "new":                       # 以按下点为锚点向拖动方向展开
            a = self.start
            dx, dy = pos.x() - a.x(), pos.y() - a.y()
            sx, sy = (1 if dx >= 0 else -1), (1 if dy >= 0 else -1)
            w, h = abs(dx), abs(dy)
            if self.ratio and h * self.ratio > w:   # 跟随伸展更大的方向
                w = h * self.ratio
            self.sel = self._place((b.right() - a.x()) if sx > 0 else (a.x() - b.left()),
                                   (b.bottom() - a.y()) if sy > 0 else (a.y() - b.top()),
                                   w, h, a.x(), a.y(), sx, sy)
            return
        r, c = self.base, self.base.center()
        if self.act == "move":                      # 尺寸不变（比例天然保持），仅贴边收敛
            d = pos - self.start
            x = min(max(r.left() + d.x(), b.left()), max(b.left(), b.right() - r.width()))
            y = min(max(r.top() + d.y(), b.top()), max(b.top(), b.bottom() - r.height()))
            self.sel = QRectF(x, y, r.width(), r.height())
        elif len(self.act) == 2:                    # 四角：对角固定
            dx, dy = (-1 if "l" in self.act else 1), (-1 if "t" in self.act else 1)
            ax, ay = (r.right() if dx < 0 else r.left()), (r.bottom() if dy < 0 else r.top())
            w, h = (pos.x() - ax) * dx, (pos.y() - ay) * dy
            if self.ratio and w / self.ratio < h:
                w = h * self.ratio
            self.sel = self._place((b.right() - ax) if dx > 0 else (ax - b.left()),
                                   (b.bottom() - ay) if dy > 0 else (ay - b.top()),
                                   w, h, ax, ay, dx, dy)
        elif self.act in "lr":                      # 左右边：对边固定，垂直居中
            dx = 1 if self.act == "r" else -1
            ax = r.left() if dx > 0 else r.right()
            self.sel = self._place((b.right() - ax) if dx > 0 else (ax - b.left()),
                                   2 * min(c.y() - b.top(), b.bottom() - c.y()),
                                   abs(pos.x() - ax), r.height() if not self.ratio else 0,
                                   ax, c.y(), dx, 0)
        else:                                       # 上下边：对边固定，水平居中
            dy = 1 if self.act == "b" else -1
            ay = r.top() if dy > 0 else r.bottom()
            h = abs(pos.y() - ay)
            self.sel = self._place(2 * min(c.x() - b.left(), b.right() - c.x()),
                                   (b.bottom() - ay) if dy > 0 else (ay - b.top()),
                                   h * self.ratio if self.ratio else r.width(), h, c.x(), ay, 0, dy)

    def _hit(self, pos):
        """命中检测：四角 > 四边 > 选区内部 > 空白"""
        r = self.sel
        if r.width() < 1 or r.height() < 1:
            return None
        m = 6.0
        nl, nr = abs(pos.x() - r.left()) <= m, abs(pos.x() - r.right()) <= m
        nt, nb = abs(pos.y() - r.top()) <= m, abs(pos.y() - r.bottom()) <= m
        ix = r.left() - m <= pos.x() <= r.right() + m
        iy = r.top() - m <= pos.y() <= r.bottom() + m
        for ok, name in (((nl and nt), "tl"), ((nr and nt), "tr"), ((nl and nb), "bl"),
                         ((nr and nb), "br"), ((nl and iy), "l"), ((nr and iy), "r"),
                         ((nt and ix), "t"), ((nb and ix), "b")):
            if ok:
                return name
        return "move" if r.contains(pos) else None

    def mousePressEvent(self, e):
        if e.button() != Qt.LeftButton:
            return
        self.start, self.base = QPointF(e.pos()), QRectF(self.sel)
        self.act = self._hit(QPointF(e.pos())) or "new"
        if self.act == "new":
            self.sel = QRectF()                     # 空白处按下 = 重新框选
        self.update()

    def mouseMoveEvent(self, e):
        pos = QPointF(e.pos())
        if self.act is None:
            self.setCursor(QCursor(self.CURSORS.get(self._hit(pos), Qt.CrossCursor)))
        else:
            self._drag(pos)
            self.update()

    def mouseReleaseEvent(self, e):
        if self.act == "new" and (e.pos() - self.start.toPoint()).manhattanLength() < 4:
            self.sel = QRectF()                     # 只点一下，不留干扰的小选区
        self.act = None
        self.update()

    def mouseDoubleClickEvent(self, e):
        if self.ok():
            self._finish(True)                      # 双击确认

    def keyPressEvent(self, e):
        if e.key() in (Qt.Key_Return, Qt.Key_Enter) and self.ok():
            self._finish(True)                      # 回车确认
        elif e.key() == Qt.Key_Escape:
            self._finish(False)                     # Esc 取消

    def showEvent(self, e):
        super().showEvent(e)
        self.setFocus()
        QTimer.singleShot(0, self.setFocus)
        QApplication.instance().installEventFilter(self)   # 兜底：没拿到焦点也能响应回车/Esc

    def eventFilter(self, obj, ev):
        if ev.type() == QEvent.KeyPress:
            self.keyPressEvent(ev)
            return True
        return False

    def closeEvent(self, e):
        QApplication.instance().removeEventFilter(self)
        if not self.finished:                       # 被外部关闭（如 Alt+F4）按取消处理
            self.finished = True
            self.cancel()
        super().closeEvent(e)

    def ok(self):
        """边界校验：选区物理像素宽高都不小于 MIN_PX"""
        if self.sel.width() < 1 or self.sel.height() < 1:
            return False
        w, h = self.src.size_of(self.sel, self.wh)
        return w >= MIN_PX and h >= MIN_PX

    def _finish(self, confirm):
        """统一出口：先关窗（卸载事件过滤器）再回调，避免结果窗按键被吞，也不会重复回调。"""
        if self.finished:
            return
        self.finished = True
        result = self.src.crop(self.sel, self.wh) if confirm else None
        cb = self.done if confirm else self.cancel
        self.close()
        if confirm:
            cb(result)
        else:
            cb()

    def paintEvent(self, e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing, True)
        p.setRenderHint(QPainter.SmoothPixmapTransform, True)  # 仅显示平滑，不影响输出像素
        p.fillRect(self.rect(), QColor(26, 26, 28))
        self.src.draw(p, None, 0.45)                           # 全屏半透明遮罩
        if self.sel.width() > 0.5 and self.sel.height() > 0.5:
            self.src.draw(p, self.sel)                         # 选区内还原原始亮度（1:1 物理像素）
            self._frame(p)
        else:
            p.setFont(QFont(self.font().family(), 11))
            p.setPen(QColor(208, 212, 208))
            p.drawText(self.rect(), Qt.AlignCenter,
                       "拖拽创建选区 · 可整体移动 / 拉伸四边四角（比例锁定）\n回车或双击确认 · Esc 取消")

    def _frame(self, p):
        """柔和配色的边框、四角把手与物理像素尺寸提示"""
        r, tint = self.sel, QColor(150, 178, 156)
        pen = QPen(tint)
        pen.setWidthF(1.4)
        p.setBrush(Qt.NoBrush)
        p.setPen(pen)
        p.drawRect(r)
        p.setPen(Qt.NoPen)
        p.setBrush(tint)
        for pt in (r.topLeft(), r.topRight(), r.bottomLeft(), r.bottomRight()):
            p.drawRoundedRect(QRectF(pt.x() - 3.5, pt.y() - 3.5, 7, 7), 2, 2)
        pw, ph = self.src.size_of(r, self.wh)
        text = "%d × %d px" % (pw, ph)
        p.setFont(QFont(p.font().family(), 9))
        fm = p.fontMetrics()
        tw, th = fm.horizontalAdvance(text) + 14, fm.height() + 6
        x = min(max(r.center().x() - tw / 2, 4), max(4, self.width() - tw - 4))
        y = r.bottom() + 7 if r.bottom() + 7 + th <= self.height() else r.bottom() - th - 7
        p.setBrush(QColor(46, 50, 47, 210))
        p.drawRoundedRect(QRectF(x, y, tw, th), 5, 5)
        p.setPen(QColor(232, 238, 232))
        p.drawText(QRectF(x, y, tw, th), Qt.AlignCenter, text)


# ---- 界面配色：由配图主色派生低饱和柔和色 ----
def mix(c, target, k):
    """颜色 c 按比例 k 混向 target"""
    return tuple(round(a * (1 - k) + b * k) for a, b in zip(c, target))


def load_art():
    """读界面配图：打包后从捆绑资源读，源码运行读原路径；失败返回 None（自动降级纯色主题）。"""
    path = os.path.join(getattr(sys, "_MEIPASS", os.path.dirname(ART)), os.path.basename(ART))
    try:
        im = Image.open(path)
        im.load()
        return im.convert("RGB")
    except Exception:
        return None


def theme(avg):
    """由配图主色派生背景 / 面板 / 边框 / 按钮整套颜色"""
    def h(target, k):
        return "#%02x%02x%02x" % mix(avg, target, k)
    return dict(bg=h((246, 245, 242), .88), panel=h((253, 252, 250), .94),
                line=h((255, 255, 255), .80), btn=h((248, 248, 246), .76),
                hot=h((250, 250, 248), .88), press=h((236, 238, 240), .70),
                accent=h((255, 255, 255), .38), accent_hi=h((255, 255, 255), .46))


QSS = """
* { font-family: "Microsoft YaHei UI", "PingFang SC", "Segoe UI", sans-serif; font-size: 13px; }
QWidget { background: %(bg)s; color: #2f3641; }
QLabel#hint { color: #6f7885; font-size: 12px; }
QLabel#banner { background: %(accent)s; }
QLabel#art { background: transparent; color: #f2f6fa; font-size: 15px; font-weight: 600; }
QLabel#artsub { background: transparent; color: #dbe6f0; font-size: 11px; }
QLabel#preview { background: %(panel)s; border: 1px solid %(line)s; border-radius: 10px; }
QComboBox, QSpinBox { background: %(panel)s; border: 1px solid %(line)s; border-radius: 6px;
                      padding: 4px 8px; min-height: 22px; }
QComboBox::drop-down { border: none; width: 16px; }
QComboBox QAbstractItemView { background: %(panel)s; border: 1px solid %(line)s;
                              selection-background-color: %(btn)s; outline: none; }
QSpinBox:disabled { color: #a9a59e; background: %(bg)s; }
QPushButton { background: %(btn)s; border: 1px solid %(line)s; border-radius: 8px;
              padding: 9px 13px; color: #2f3641; }
QPushButton:hover { background: %(hot)s; }
QPushButton:pressed { background: %(press)s; }
QPushButton#primary { background: %(accent)s; border-color: %(accent)s; color: #ffffff; }
QPushButton#primary:hover { background: %(accent_hi)s; }
"""


# ---- 结果窗口：DPI 无损预览 + 保存 PNG + 复制 ----
class ResultDialog(QDialog):
    def __init__(self, img, parent=None):
        super().__init__(parent)                    # 配色随父窗口继承，无需重复设置样式表
        self.img = img
        self.setWindowTitle("裁剪结果")
        self.view = QLabel()
        self.view.setObjectName("preview")
        self.view.setAlignment(Qt.AlignCenter)
        self._dpr = 0.0
        self._preview()
        info = QLabel("%d × %d px · %s" % (img.width, img.height, img.mode))
        info.setObjectName("hint")
        self.status = QLabel("")
        self.status.setObjectName("hint")
        row = QHBoxLayout()
        row.setSpacing(8)
        for text, slot, name in (("保存为 PNG", self._save, "primary"),
                                 ("复制到剪贴板", self._copy, ""), ("关闭", self.accept, "")):
            b = QPushButton(text)
            b.clicked.connect(slot)
            if name:
                b.setObjectName(name)
            row.addWidget(b)
        row.addWidget(self.status, 1)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(14, 14, 14, 12)
        lay.setSpacing(9)
        lay.addWidget(self.view, 0, Qt.AlignHCenter)
        lay.addWidget(info)
        lay.addLayout(row)

    def _preview(self):
        """预览位图按物理像素准备：k≤1 时逐像素 1:1，125%/150% 下不会被拉伸发虚。"""
        s = self.screen() or QApplication.primaryScreen()
        dpr = s.devicePixelRatio() or 1.0
        av, img = s.availableGeometry(), self.img
        k = min(1.0, av.width() * 0.6 * dpr / img.width, av.height() * 0.6 * dpr / img.height)
        pm = QPixmap.fromImage(pil_to_qimage(img)).scaled(
            max(1, round(img.width * k)), max(1, round(img.height * k)),
            Qt.KeepAspectRatio, Qt.SmoothTransformation)
        pm.setDevicePixelRatio(dpr)
        self.view.setFixedSize(round(pm.width() / dpr) + 8, round(pm.height() / dpr) + 8)
        self.view.setPixmap(pm)
        self._dpr = dpr

    def showEvent(self, e):
        super().showEvent(e)
        if abs((self.screen() or QApplication.primaryScreen()).devicePixelRatio()
               - self._dpr) > 0.01:                 # 落到另一块缩放比不同的屏幕时重算
            self._preview()

    def _save(self):
        path, _ = QFileDialog.getSaveFileName(self, "保存裁剪结果", "crop.png", "PNG 图片 (*.png)")
        if not path:
            return
        if not path.lower().endswith(".png"):
            path += ".png"
        out = self.img if self.img.mode in ("RGB", "RGBA") else self.img.convert("RGBA")
        if out.mode == "RGBA" and out.getextrema()[3][1] == 255:
            out = out.convert("RGB")                # 全不透明时省掉 Alpha 通道
        try:
            out.save(path, "PNG")                   # PNG 无损压缩：输出与原始像素一致
            self.status.setText("已保存")
        except Exception as ex:
            QMessageBox.warning(self, "保存失败", str(ex))

    def _copy(self):
        QApplication.clipboard().setPixmap(QPixmap.fromImage(pil_to_qimage(self.img)))
        self.status.setText("已复制到剪贴板")


# ---- 主窗口：选比例 → 截图 / 导入图片 → 共用裁剪窗口 ----
class Main(QWidget):
    PRESETS = [("1:1", 1, 1), ("4:3", 4, 3), ("11:9", 11, 9), ("16:9", 16, 9), ("3:2", 3, 2)]

    def __init__(self):
        super().__init__()
        self.setWindowTitle("比例截图 / 裁剪")
        self.crop = None
        art = load_art()
        avg = art.resize((1, 1)).getpixel((0, 0)) if art else BASE_AVG   # 配图主色
        self.setStyleSheet(QSS % theme(avg))
        self._build(art)

    def _banner(self, art):
        """顶部横幅：把配图上部的夜空带裁成 400×104 并压暗一点，标题叠在图上。"""
        w, h = 400, 104
        dpr = QApplication.primaryScreen().devicePixelRatio() or 1.0
        pw, ph = round(w * dpr), round(h * dpr)
        lab = QLabel()
        lab.setObjectName("banner")
        lab.setFixedSize(w, h)
        if art:
            src = art.crop((0, 0, art.width, int(art.height * 0.42)))
            src = Image.blend(src, Image.new("RGB", src.size, (10, 14, 22)), 0.30)
            pm = QPixmap.fromImage(pil_to_qimage(src)).scaled(
                pw, ph, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
            pm = pm.copy((pm.width() - pw) // 2, (pm.height() - ph) // 2, pw, ph)
            pm.setDevicePixelRatio(dpr)             # 按物理像素准备，125%/150% 下不模糊
            lab.setPixmap(pm)
        for text, name, top in (("比例截图 / 裁剪", "art", 18),
                                ("比例锁定 · 物理像素无损", "artsub", 44)):
            t = QLabel(text, lab)
            t.setObjectName(name)
            t.setGeometry(16, top, w - 32, 22)      # 给定宽度，避免样式表字号生效后文字被自身矩形裁掉
        return lab

    def _build(self, art):
        self.combo = QComboBox()
        self.combo.addItems([p[0] for p in self.PRESETS] + ["自定义", "自由比例"])
        self.combo.setCurrentIndex(3)               # 默认 16:9
        self.combo.setFixedWidth(150)
        self.combo.currentIndexChanged.connect(self._sync)
        self.spin_w, self.spin_h = QSpinBox(), QSpinBox()
        for s in (self.spin_w, self.spin_h):
            s.setRange(1, 200)
            s.setFixedWidth(54)
        self.spin_w.setValue(16)
        self.spin_h.setValue(9)
        self.colon = QLabel(":")
        self._sync()
        grid = QGridLayout()
        grid.setHorizontalSpacing(9)
        grid.setVerticalSpacing(10)
        grid.addWidget(QLabel("裁剪比例"), 0, 0)
        grid.addWidget(self.combo, 0, 1, 1, 3)
        grid.addWidget(QLabel("自定义"), 1, 0)
        grid.addWidget(self.spin_w, 1, 1)
        grid.addWidget(self.colon, 1, 2, Qt.AlignHCenter)
        grid.addWidget(self.spin_h, 1, 3)
        row = QHBoxLayout()
        row.setSpacing(10)
        for text, slot, name in (("开始截图", self.start_shot, "primary"),
                                 ("导入图片裁剪", self.open_image, "")):
            b = QPushButton(text)
            b.clicked.connect(slot)
            if name:
                b.setObjectName(name)
            row.addWidget(b, 1)
        hint = QLabel("拖拽创建选区，可整体移动或拉伸四边四角（比例锁定）\n"
                      "回车 / 双击确认，Esc 取消；结果可保存 PNG 或复制到剪贴板")
        hint.setObjectName("hint")
        body = QVBoxLayout()
        body.setContentsMargins(16, 14, 16, 14)
        body.setSpacing(12)
        body.addLayout(grid)
        body.addLayout(row)
        body.addWidget(hint)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._banner(art))
        root.addLayout(body)
        self.setFixedWidth(400)

    def _sync(self):
        custom = self.combo.currentText() == "自定义"
        for w in (self.spin_w, self.spin_h, self.colon):
            w.setEnabled(custom)

    def ratio(self):
        """返回 (宽高比 float；None=自由比例, 用于精确比例对齐的整数比或 None)"""
        i = self.combo.currentIndex()
        if i < len(self.PRESETS):
            _, rw, rh = self.PRESETS[i]
            return rw / rh, (rw, rh)
        if self.combo.currentText() == "自定义":
            rw, rh = self.spin_w.value(), self.spin_h.value()
            return rw / rh, (rw, rh)
        return None, None

    def start_shot(self):
        if self.crop:
            return
        self.hide()                                 # 截图前自动隐藏主窗口
        QTimer.singleShot(220, self._capture)       # 等窗口真正消失再抓屏，避免拍到自己

    def _capture(self):
        try:
            vs, shot = grab_desktop()
        except Exception as ex:
            self._restore()
            QMessageBox.warning(self, "截图失败", str(ex))
            return
        # 物理像素画面映射到控件局部逻辑坐标：左上角对齐 + 整幅统一缩放比
        self._open_crop(Source(QRectF(0, 0, vs.width(), vs.height()), shot), QRect(vs))

    def open_image(self):
        if self.crop:
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "选择图片", "", "图片 (*.png *.jpg *.jpeg *.bmp *.tif *.tiff *.webp *.gif)")
        if not path:
            return
        try:
            im = Image.open(path)
            im.load()
            im = im.convert("RGBA")                 # 读原始像素，不预缩放、不压缩
        except Exception as ex:
            QMessageBox.warning(self, "打开失败", str(ex))
            return
        scr = QApplication.primaryScreen().availableGeometry()
        k = min(1.0, scr.width() * 0.88 / im.width, scr.height() * 0.88 / im.height)
        w, h = max(1, round(im.width * k)), max(1, round(im.height * k))
        self.hide()
        # 这里只是把窗口显示尺寸缩小（便于操作），裁剪输出仍取原图像素
        self._open_crop(Source(QRectF(0, 0, w, h), im),
                        QRect(scr.x() + (scr.width() - w) // 2,
                              scr.y() + (scr.height() - h) // 2, w, h))

    def _open_crop(self, src, geom):
        ratio, wh = self.ratio()
        self.crop = CropView(src, ratio, wh, self._finish, self._restore)
        self.crop.setGeometry(geom)
        self.crop.show()
        self.crop.raise_()
        self.crop.activateWindow()

    def _finish(self, img):
        self._restore()                             # 裁剪完成后恢复主窗口
        ResultDialog(img, self).exec_()

    def _restore(self):
        self.crop = None
        self.show()
        self.raise_()
        self.activateWindow()

    def closeEvent(self, e):
        QApplication.quit()


def main():
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    w = Main()
    w.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
