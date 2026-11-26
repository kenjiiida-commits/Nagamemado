#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ながめ窓 (Nagamemado) - Ubuntu用 写真閲覧ソフト
  ・フォルダツリー＋サムネイル一覧
  ・大きく表示（拡大縮小・回転・全画面）
  ・撮影情報（EXIF）の表示
  ・スライドショー
  ・ファイル操作（ゴミ箱へ・名前変更・コピー・移動）

Copyright (c) 2026 飯田顕治
License: MIT
"""

import os
import re
import sys
import shutil
import subprocess
import datetime
from collections import OrderedDict

from PySide6.QtCore import (
    Qt, QObject, QRunnable, QThreadPool, Signal, QSize, QSettings, QTimer,
    QDir, QFile, QStandardPaths, QUrl, QRectF, QRect, QPoint,
    QPropertyAnimation, QEasingCurve, QEvent,
)
from PySide6.QtGui import (
    QImageReader, QImage, QPixmap, QIcon, QStandardItemModel, QStandardItem,
    QAction, QKeySequence, QPainter, QColor, QDesktopServices, QImageIOHandler,
)
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QSplitter, QTreeView, QListView,
    QFileSystemModel, QToolBar, QLineEdit, QComboBox, QSlider, QLabel,
    QDockWidget, QTextBrowser, QVBoxLayout, QHBoxLayout, QGraphicsView,
    QGraphicsScene, QGraphicsPixmapItem, QMessageBox, QInputDialog,
    QFileDialog, QAbstractItemView, QStyle, QToolButton, QFrame, QSizePolicy,
)

try:
    from PIL import Image
    HAS_PIL = True
except Exception:  # Pillow が無くても動くようにする
    HAS_PIL = False

APP_NAME = "ながめ窓"
APP_ID = "nagamemado"
VERSION = "0.2.0"

PATH_ROLE = Qt.UserRole + 1
SIZE_ROLE = Qt.UserRole + 2
MTIME_ROLE = Qt.UserRole + 3
THUMB_ROLE = Qt.UserRole + 4   # 読み込み済みサムネイルの大きさ

# 大きな写真（1億画素など）も開けるように、読み込み上限を広げる（MB）
QImageReader.setAllocationLimit(2048)


# ----------------------------------------------------------------------
# 小さな便利関数
# ----------------------------------------------------------------------
def supported_exts():
    """このパソコンのQtで読める画像の拡張子"""
    fmts = {bytes(f).decode().lower() for f in QImageReader.supportedImageFormats()}
    wanted = {"jpg", "jpeg", "jpe", "png", "gif", "bmp", "webp", "tif", "tiff",
              "heic", "heif", "avif", "jxl", "ico", "tga", "pbm", "pgm", "ppm",
              "xbm", "xpm", "svg", "jfif"}
    exts = fmts & wanted
    if "jpeg" in exts or "jpg" in exts:
        exts |= {"jpg", "jpeg", "jpe", "jfif"}
    if "tiff" in exts or "tif" in exts:
        exts |= {"tif", "tiff"}
    return {"." + e for e in exts}


IMAGE_EXTS = set()


def is_image(name):
    return os.path.splitext(name)[1].lower() in IMAGE_EXTS


def human_size(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} GB"


def natural_key(s):
    """ IMG_2.jpg が IMG_10.jpg より前に来る並べ方 """
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


def unique_path(folder, name):
    """同じ名前があれば「名前 (1).jpg」のようにずらす"""
    base, ext = os.path.splitext(name)
    cand = os.path.join(folder, name)
    i = 1
    while os.path.exists(cand):
        cand = os.path.join(folder, f"{base} ({i}){ext}")
        i += 1
    return cand


def move_to_trash(path):
    """ゴミ箱へ移動（失敗したら gio trash を試す）"""
    try:
        res = QFile.moveToTrash(path)
        ok = res[0] if isinstance(res, tuple) else bool(res)
        if ok:
            return True
    except Exception:
        pass
    try:
        r = subprocess.run(["gio", "trash", path], capture_output=True)
        return r.returncode == 0
    except Exception:
        return False


def theme_icon(names, fallback):
    for n in names:
        ic = QIcon.fromTheme(n)
        if not ic.isNull():
            return ic
    return QApplication.style().standardIcon(fallback)


def load_image(path):
    reader = QImageReader(path)
    reader.setAutoTransform(True)   # EXIFの向きに合わせて自動回転
    img = reader.read()
    return img, reader.errorString()


# ----------------------------------------------------------------------
# 撮影情報（EXIF）
# ----------------------------------------------------------------------
FLASH_TEXT = {0: "発光なし", 1: "発光", 5: "発光（反射光なし）", 7: "発光（反射光あり）",
              9: "強制発光", 16: "発光禁止", 24: "自動・発光なし", 25: "自動・発光"}
WB_TEXT = {0: "自動", 1: "マニュアル"}


def _num(v):
    try:
        return float(v)
    except Exception:
        try:
            return v[0] / v[1]
        except Exception:
            return None


def _clean(v):
    if isinstance(v, bytes):
        try:
            v = v.decode("utf-8", "ignore")
        except Exception:
            return ""
    return str(v).replace("\x00", "").strip()


def _dms_to_deg(dms, ref):
    try:
        d, m, s = (_num(x) for x in dms)
        deg = d + m / 60.0 + s / 3600.0
        if _clean(ref) in ("S", "W"):
            deg = -deg
        return deg
    except Exception:
        return None


def read_exif(path):
    """(見出し, 値) のリストと、GPS座標（あれば）を返す"""
    rows, gps_pos = [], None
    if not HAS_PIL:
        return rows, gps_pos
    try:
        with Image.open(path) as im:
            ex = im.getexif()
            if not ex:
                return rows, gps_pos
            base = dict(ex)
            try:
                sub = dict(ex.get_ifd(0x8769))
            except Exception:
                sub = {}
            try:
                gps = dict(ex.get_ifd(0x8825))
            except Exception:
                gps = {}
    except Exception:
        return rows, gps_pos

    def g(tag):
        return sub.get(tag, base.get(tag))

    dt = g(36867) or g(306)
    if dt:
        s = _clean(dt)
        try:
            d = datetime.datetime.strptime(s, "%Y:%m:%d %H:%M:%S")
            s = d.strftime("%Y年%m月%d日 %H:%M:%S")
        except Exception:
            pass
        rows.append(("撮影日時", s))

    make, model = _clean(g(271) or ""), _clean(g(272) or "")
    if model:
        cam = model if (not make or model.lower().startswith(make.lower().split()[0])) \
            else f"{make} {model}"
        rows.append(("カメラ", cam))
    elif make:
        rows.append(("カメラ", make))

    lens = _clean(g(42036) or "")
    if lens:
        rows.append(("レンズ", lens))

    exp = _num(g(33434)) if g(33434) is not None else None
    if exp:
        if exp < 1:
            rows.append(("シャッター速度", f"1/{round(1 / exp)} 秒"))
        else:
            rows.append(("シャッター速度", f"{exp:g} 秒"))

    fn = _num(g(33437)) if g(33437) is not None else None
    if fn:
        rows.append(("絞り", f"F{fn:.1f}"))

    iso = g(34855)
    if iso:
        if isinstance(iso, (tuple, list)):
            iso = iso[0]
        rows.append(("ISO感度", f"ISO {iso}"))

    fl = _num(g(37386)) if g(37386) is not None else None
    if fl:
        f35 = g(41989)
        txt = f"{fl:g} mm"
        if f35:
            txt += f"（35mm換算 {f35} mm）"
        rows.append(("焦点距離", txt))

    eb = _num(g(37380)) if g(37380) is not None else None
    if eb is not None and abs(eb) > 0.001:
        rows.append(("露出補正", f"{eb:+.1f} EV"))

    fl_v = g(37385)
    if fl_v is not None:
        try:
            rows.append(("フラッシュ", FLASH_TEXT.get(int(fl_v), "発光" if int(fl_v) & 1 else "発光なし")))
        except Exception:
            pass

    wb = g(41987)
    if wb is not None:
        try:
            rows.append(("ホワイトバランス", WB_TEXT.get(int(wb), str(wb))))
        except Exception:
            pass

    sw = _clean(g(305) or "")
    if sw:
        rows.append(("ソフトウェア", sw))

    if gps and 2 in gps and 4 in gps:
        lat = _dms_to_deg(gps[2], gps.get(1, "N"))
        lon = _dms_to_deg(gps[4], gps.get(3, "E"))
        if lat is not None and lon is not None:
            gps_pos = (lat, lon)
            rows.append(("撮影場所", f"{lat:.6f}, {lon:.6f}"))

    return rows, gps_pos


def basic_info(path):
    rows = []
    try:
        st = os.stat(path)
    except OSError:
        return rows
    rows.append(("ファイル名", os.path.basename(path)))
    rows.append(("場所", os.path.dirname(path)))
    rows.append(("ファイルサイズ", f"{human_size(st.st_size)}（{st.st_size:,} バイト）"))
    reader = QImageReader(path)
    sz = reader.size()
    if sz.isValid():
        w, h = sz.width(), sz.height()
        try:
            if reader.transformation() & QImageIOHandler.Transformation.TransformationRotate90:
                w, h = h, w
        except Exception:
            pass
        mp = w * h / 1_000_000
        rows.append(("画素数", f"{w} × {h}（{mp:.1f} メガピクセル）"))
    fmt = bytes(reader.format()).decode().upper()
    if fmt:
        rows.append(("形式", fmt))
    mt = datetime.datetime.fromtimestamp(st.st_mtime)
    rows.append(("更新日時", mt.strftime("%Y年%m月%d日 %H:%M:%S")))
    return rows


def info_html(path):
    if not path:
        return "<p style='color:gray'>写真を選ぶと、ここに情報が出ます。</p>"
    esc = lambda s: (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
    out = ["<style>td{padding:2px 6px;vertical-align:top} td.k{color:#777;white-space:nowrap}"
           " h4{margin:6px 0 2px 0}</style>"]
    out.append("<h4>ファイル</h4><table>")
    for k, v in basic_info(path):
        out.append(f"<tr><td class='k'>{esc(k)}</td><td>{esc(v)}</td></tr>")
    out.append("</table>")
    rows, pos = read_exif(path)
    out.append("<h4>撮影情報</h4>")
    if rows:
        out.append("<table>")
        for k, v in rows:
            out.append(f"<tr><td class='k'>{esc(k)}</td><td>{esc(v)}</td></tr>")
        out.append("</table>")
        if pos:
            lat, lon = pos
            url = f"https://maps.gsi.go.jp/#16/{lat:.6f}/{lon:.6f}/"
            out.append(f"<p><a href='{url}'>地理院地図で撮影場所を見る</a></p>")
    elif not HAS_PIL:
        out.append("<p style='color:gray'>撮影情報を読むには python3-pil が必要です。</p>")
    else:
        out.append("<p style='color:gray'>撮影情報はありません。</p>")
    return "".join(out)


# ----------------------------------------------------------------------
# 裏で画像を読み込む仕組み（画面が固まらないように）
# ----------------------------------------------------------------------
class LoaderSignals(QObject):
    thumb_done = Signal(int, str, QImage)
    full_done = Signal(str, QImage)


class ThumbTask(QRunnable):
    def __init__(self, gen, path, size, signals):
        super().__init__()
        self.gen, self.path, self.size, self.signals = gen, path, size, signals

    def run(self):
        img = QImage()
        try:
            reader = QImageReader(self.path)
            reader.setAutoTransform(True)
            s = reader.size()
            if s.isValid() and (s.width() > self.size or s.height() > self.size):
                reader.setScaledSize(s.scaled(self.size, self.size, Qt.KeepAspectRatio))
            img = reader.read()
            if not img.isNull() and (img.width() > self.size or img.height() > self.size):
                img = img.scaled(self.size, self.size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        except Exception:
            pass
        try:
            self.signals.thumb_done.emit(self.gen, self.path, img)
        except RuntimeError:
            pass  # アプリ終了中


class FullTask(QRunnable):
    def __init__(self, path, signals):
        super().__init__()
        self.path, self.signals = path, signals

    def run(self):
        img, _ = load_image(self.path)
        try:
            self.signals.full_done.emit(self.path, img)
        except RuntimeError:
            pass


# ----------------------------------------------------------------------
# 写真を大きく表示する部品
# ----------------------------------------------------------------------
class ImageView(QGraphicsView):
    zoomChanged = Signal(float)
    doubleClicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._scene = QGraphicsScene(self)
        self.setScene(self._scene)
        self._item = QGraphicsPixmapItem()
        self._item.setTransformationMode(Qt.SmoothTransformation)
        self._scene.addItem(self._item)
        self.setBackgroundBrush(QColor(24, 24, 24))
        self.setFrameShape(QFrame.NoFrame)
        self.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.AnchorViewCenter)
        self.setDragMode(QGraphicsView.ScrollHandDrag)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setFocusPolicy(Qt.NoFocus)
        self._rot = 0
        self._scale = 1.0
        self._fit = True
        self.upscale_small = False

    def has_image(self):
        return not self._item.pixmap().isNull()

    def set_image(self, img):
        pm = QPixmap.fromImage(img) if img is not None and not img.isNull() else QPixmap()
        self._item.setPixmap(pm)
        self._item.setTransformOriginPoint(0, 0)
        self._scene.setSceneRect(QRectF(pm.rect()))
        self._rot = 0
        self._fit = True
        self.fit()

    def _apply(self):
        self.resetTransform()
        self.scale(self._scale, self._scale)
        self.rotate(self._rot)
        self.zoomChanged.emit(self._scale)

    def fit(self):
        self._fit = True
        pm = self._item.pixmap()
        if pm.isNull():
            self.resetTransform()
            return
        w, h = pm.width(), pm.height()
        if self._rot % 180:
            w, h = h, w
        vw, vh = max(1, self.viewport().width() - 4), max(1, self.viewport().height() - 4)
        s = min(vw / w, vh / h)
        if not self.upscale_small:
            s = min(s, 1.0)
        self._scale = s
        self._apply()
        self.centerOn(self._item)

    def actual_size(self):
        self._fit = False
        self._scale = 1.0
        self._apply()
        self.centerOn(self._item)

    def zoom(self, factor):
        if not self.has_image():
            return
        new = max(0.02, min(32.0, self._scale * factor))
        factor = new / self._scale
        self._fit = False
        self._scale = new
        self.scale(factor, factor)
        self.zoomChanged.emit(self._scale)

    def rotate_by(self, deg):
        self._rot = (self._rot + deg) % 360
        if self._fit:
            self.fit()
        else:
            self._apply()

    def zoom_percent(self):
        return self._scale * 100

    def wheelEvent(self, e):
        d = e.angleDelta().y()
        if d:
            self.zoom(1.15 if d > 0 else 1 / 1.15)
        e.accept()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if self._fit:
            self.fit()

    def mouseDoubleClickEvent(self, e):
        self.doubleClicked.emit()


# ----------------------------------------------------------------------
# ビューア窓（大きく表示・スライドショー）
# ----------------------------------------------------------------------
class ViewerWindow(QMainWindow):
    currentChanged = Signal(str)
    fileTrashed = Signal(str)

    CACHE_MAX = 6

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.paths = []
        self.index = -1
        self._cache = OrderedDict()
        self._pending = set()
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(2)
        self._signals = LoaderSignals()
        self._signals.full_done.connect(self._on_prefetched)
        self.setWindowTitle(APP_NAME)
        self.resize(1100, 760)

        central = QWidget()
        lay = QVBoxLayout(central)
        lay.setContentsMargins(0, 0, 0, 0)
        self.view = ImageView()
        self.view.upscale_small = settings.value("upscale_small", False, type=bool)
        lay.addWidget(self.view)
        self.setCentralWidget(central)

        # 写真の上に重ねる情報表示
        self.overlay = QLabel(self.view)
        self.overlay.setStyleSheet(
            "QLabel{background:rgba(0,0,0,170);color:#eee;padding:8px 12px;"
            "border-radius:6px;font-size:10.5pt}")
        self.overlay.setTextFormat(Qt.RichText)
        self.overlay.hide()
        self.overlay.move(12, 12)

        # 大きな文字の一時メッセージ（スライドショー開始など）
        self.toast = QLabel(self.view)
        self.toast.setStyleSheet(
            "QLabel{background:rgba(0,0,0,180);color:white;padding:10px 18px;"
            "border-radius:8px;font-size:13pt}")
        self.toast.hide()
        self._toast_timer = QTimer(self, singleShot=True, timeout=self.toast.hide)

        self.slide_timer = QTimer(self, timeout=self._slide_next)

        self._build_actions()
        self.view.zoomChanged.connect(lambda *_: self._update_status())
        self.view.doubleClicked.connect(self.toggle_fullscreen)

        self.status = QLabel()
        self.statusBar().addWidget(self.status, 1)
        self.zoom_label = QLabel()
        self.statusBar().addPermanentWidget(self.zoom_label)

        geo = settings.value("viewer_geometry")
        if geo is not None:
            self.restoreGeometry(geo)

    # --- 操作ボタン・キー ---
    def _act(self, text, keys, slot, icon=None, checkable=False):
        a = QAction(text, self)
        if keys:
            a.setShortcuts([QKeySequence(k) for k in keys])
        if icon is not None:
            a.setIcon(icon)
        a.setCheckable(checkable)
        a.triggered.connect(slot)
        self.addAction(a)
        return a

    def _build_actions(self):
        S = QStyle.StandardPixmap
        tb = QToolBar("操作")
        tb.setMovable(False)
        tb.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
        tb.setIconSize(QSize(22, 22))
        self.addToolBar(Qt.BottomToolBarArea, tb)
        self.toolbar = tb

        self.a_prev = self._act("前へ", ["Left", "PgUp", "Backspace"], self.prev,
                                theme_icon(["go-previous"], S.SP_ArrowLeft))
        self.a_next = self._act("次へ", ["Right", "PgDown", "Space"], self.next,
                                theme_icon(["go-next"], S.SP_ArrowRight))
        self.a_first = self._act("最初", ["Home"], lambda: self.show_index(0))
        self.a_last = self._act("最後", ["End"], lambda: self.show_index(len(self.paths) - 1))
        self.a_zin = self._act("拡大", ["+", "=", "Ctrl++"], lambda: self.view.zoom(1.25),
                               theme_icon(["zoom-in"], S.SP_ArrowUp))
        self.a_zout = self._act("縮小", ["-", "Ctrl+-"], lambda: self.view.zoom(0.8),
                                theme_icon(["zoom-out"], S.SP_ArrowDown))
        self.a_fit = self._act("全体", ["0", "Ctrl+0"], self.view.fit,
                               theme_icon(["zoom-fit-best"], S.SP_TitleBarMaxButton))
        self.a_1x = self._act("等倍", ["1"], self.view.actual_size,
                              theme_icon(["zoom-original"], S.SP_TitleBarNormalButton))
        self.a_rl = self._act("左回転", ["L", "Ctrl+L"], lambda: self.view.rotate_by(-90),
                              theme_icon(["object-rotate-left"], S.SP_BrowserReload))
        self.a_rr = self._act("右回転", ["R", "Ctrl+R"], lambda: self.view.rotate_by(90),
                              theme_icon(["object-rotate-right"], S.SP_BrowserReload))
        self.a_slide = self._act("スライドショー", ["S", "F5"], self.toggle_slideshow,
                                 theme_icon(["media-playback-start"], S.SP_MediaPlay), True)
        self.a_full = self._act("全画面", ["F", "F11"], self.toggle_fullscreen,
                                theme_icon(["view-fullscreen"], S.SP_TitleBarMaxButton))
        self.a_info = self._act("撮影情報", ["I"], self.toggle_overlay,
                                theme_icon(["dialog-information"], S.SP_MessageBoxInformation), True)
        self.a_trash = self._act("ゴミ箱へ", ["Delete"], self.trash_current,
                                 theme_icon(["user-trash"], S.SP_TrashIcon))
        self.a_close = self._act("閉じる", ["Esc"], self.on_escape)

        for a in (self.a_prev, self.a_next, None, self.a_zout, self.a_zin, self.a_fit,
                  self.a_1x, None, self.a_rl, self.a_rr, None, self.a_slide, self.a_full,
                  self.a_info, None, self.a_trash):
            if a is None:
                tb.addSeparator()
            else:
                tb.addAction(a)

    # --- 表示 ---
    def open_list(self, paths, index, slideshow=False):
        self.paths = list(paths)
        self._cache.clear()
        self.show_index(index)
        if not self.isVisible():
            self.show()
        self.raise_()
        self.activateWindow()
        if slideshow:
            if not self.isFullScreen():
                self.toggle_fullscreen()
            self.start_slideshow()

    def _get_image(self, path):
        if path in self._cache:
            self._cache.move_to_end(path)
            return self._cache[path]
        img, err = load_image(path)
        self._put_cache(path, img)
        return img

    def _put_cache(self, path, img):
        self._cache[path] = img
        self._cache.move_to_end(path)
        while len(self._cache) > self.CACHE_MAX:
            self._cache.popitem(last=False)

    def _prefetch(self):
        for d in (1, -1, 2):
            i = self.index + d
            if 0 <= i < len(self.paths):
                p = self.paths[i]
                if p not in self._cache and p not in self._pending:
                    self._pending.add(p)
                    self._pool.start(FullTask(p, self._signals))

    def _on_prefetched(self, path, img):
        self._pending.discard(path)
        if path in self.paths and path not in self._cache:
            self._put_cache(path, img)

    def show_index(self, i):
        if not self.paths:
            self.view.set_image(None)
            self.status.setText("表示する写真がありません")
            return
        i = max(0, min(len(self.paths) - 1, i))
        self.index = i
        path = self.paths[i]
        img = self._get_image(path)
        self.view.set_image(img)
        if img.isNull():
            self.show_toast("この画像は開けませんでした")
        self.setWindowTitle(f"{os.path.basename(path)} - {APP_NAME}")
        self._update_status()
        if self.overlay.isVisible():
            self._update_overlay()
        self.currentChanged.emit(path)
        self._prefetch()

    def _update_status(self):
        if 0 <= self.index < len(self.paths):
            p = self.paths[self.index]
            pm = self.view._item.pixmap()
            dims = f"{pm.width()} × {pm.height()}" if not pm.isNull() else "—"
            self.status.setText(f"{self.index + 1} / {len(self.paths)}　{os.path.basename(p)}　{dims}")
        self.zoom_label.setText(f"{self.view.zoom_percent():.0f}%")

    def next(self):
        if self.index < len(self.paths) - 1:
            self.show_index(self.index + 1)
        else:
            self.show_toast("最後の写真です")

    def prev(self):
        if self.index > 0:
            self.show_index(self.index - 1)
        else:
            self.show_toast("最初の写真です")

    def show_toast(self, text, ms=1500):
        self.toast.setText(text)
        self.toast.adjustSize()
        self.toast.move((self.view.width() - self.toast.width()) // 2,
                        self.view.height() - self.toast.height() - 30)
        self.toast.show()
        self.toast.raise_()
        self._toast_timer.start(ms)

    # --- 撮影情報の重ね表示 ---
    def toggle_overlay(self):
        if self.overlay.isVisible():
            self.overlay.hide()
            self.a_info.setChecked(False)
        else:
            self._update_overlay()
            self.overlay.show()
            self.a_info.setChecked(True)

    def _update_overlay(self):
        if not (0 <= self.index < len(self.paths)):
            return
        p = self.paths[self.index]
        rows = [r for r in basic_info(p) if r[0] in ("ファイル名", "画素数", "ファイルサイズ")]
        ex, _ = read_exif(p)
        rows += ex
        html = "<table>" + "".join(
            f"<tr><td style='color:#aaa;padding-right:10px'>{k}</td><td>{v}</td></tr>"
            for k, v in rows) + "</table>"
        self.overlay.setText(html)
        self.overlay.adjustSize()

    # --- スライドショー ---
    def interval_ms(self):
        return int(float(self.settings.value("slide_interval", 4)) * 1000)

    def start_slideshow(self):
        self.slide_timer.start(self.interval_ms())
        self.a_slide.setChecked(True)
        self.a_slide.setText("停止")
        self.show_toast(f"▶ スライドショー（{self.interval_ms() / 1000:g}秒ごと）　S / Esc で止めます", 2500)

    def stop_slideshow(self, quiet=False):
        if self.slide_timer.isActive():
            self.slide_timer.stop()
            if not quiet:
                self.show_toast("■ スライドショーを止めました")
        self.a_slide.setChecked(False)
        self.a_slide.setText("スライドショー")

    def toggle_slideshow(self):
        if self.slide_timer.isActive():
            self.stop_slideshow()
        else:
            self.start_slideshow()

    def _slide_next(self):
        if not self.paths:
            self.stop_slideshow(True)
            return
        loop = self.settings.value("slide_loop", True, type=bool)
        if self.index < len(self.paths) - 1:
            self.show_index(self.index + 1)
        elif loop:
            self.show_index(0)
        else:
            self.stop_slideshow()

    # --- 全画面 ---
    def toggle_fullscreen(self):
        if self.isFullScreen():
            self.showNormal()
            self.toolbar.show()
            self.statusBar().show()
        else:
            self.toolbar.hide()
            self.statusBar().hide()
            self.showFullScreen()

    def on_escape(self):
        if self.slide_timer.isActive():
            self.stop_slideshow()
        elif self.isFullScreen():
            self.toggle_fullscreen()
        else:
            self.close()

    # --- ゴミ箱へ ---
    def trash_current(self):
        if not (0 <= self.index < len(self.paths)):
            return
        was_running = self.slide_timer.isActive()
        self.stop_slideshow(True)
        p = self.paths[self.index]
        r = QMessageBox.question(self, "ゴミ箱へ移動",
                                 f"「{os.path.basename(p)}」をゴミ箱へ移動しますか？",
                                 QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes)
        if r != QMessageBox.Yes:
            if was_running:
                self.start_slideshow()
            return
        if not move_to_trash(p):
            QMessageBox.warning(self, "失敗", "ゴミ箱へ移動できませんでした。")
            return
        self._cache.pop(p, None)
        del self.paths[self.index]
        self.fileTrashed.emit(p)
        if not self.paths:
            self.view.set_image(None)
            self.close()
            return
        self.show_index(min(self.index, len(self.paths) - 1))
        self.show_toast("ゴミ箱へ移動しました")

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if self.toast.isVisible():
            self.toast.move((self.view.width() - self.toast.width()) // 2,
                            self.view.height() - self.toast.height() - 30)

    def closeEvent(self, e):
        self.stop_slideshow(True)
        if self.isFullScreen():
            self.showNormal()
            self.toolbar.show()
            self.statusBar().show()
        self.settings.setValue("viewer_geometry", self.saveGeometry())
        super().closeEvent(e)


# ----------------------------------------------------------------------
# マウスを乗せたサムネイルが「ポン」と大きくなる部品
# ----------------------------------------------------------------------
class HoverPopup(QLabel):
    """メイン窓の上に重ねて表示する拡大サムネイル（クリックは下に素通り）"""

    def __init__(self, parent):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setScaledContents(True)
        self.setStyleSheet("QLabel{background:#222;border:3px solid #3a3a3a;border-radius:3px}")
        self.anim = QPropertyAnimation(self, b"geometry", self)
        self.anim.setDuration(160)
        self.anim.setEasingCurve(QEasingCurve.OutBack)   # 少し行き過ぎて戻る「ポン」の動き
        self.path = None
        self.hide()

    def pop(self, path, pixmap, start_rect, target_rect):
        self.path = path
        self.setPixmap(pixmap)
        self.anim.stop()
        self.setGeometry(start_rect)
        self.show()
        self.raise_()
        self.anim.setStartValue(start_rect)
        self.anim.setEndValue(target_rect)
        self.anim.start()

    def update_pixmap(self, path, pixmap):
        if self.isVisible() and path == self.path:
            self.setPixmap(pixmap)

    def dismiss(self):
        self.anim.stop()
        self.path = None
        self.hide()


# ----------------------------------------------------------------------
# メイン窓（フォルダ＋サムネイル一覧）
# ----------------------------------------------------------------------
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.settings = QSettings(APP_ID, APP_ID)
        self.setWindowTitle(f"{APP_NAME} {VERSION}")
        self.resize(1200, 800)
        self.folder = ""
        self.gen = 0
        self.items = {}          # path -> QStandardItem
        self.requested = set()   # サムネイルを頼んだ path
        self.thumb_size = int(self.settings.value("thumb_size", 160))
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(max(2, min(6, (os.cpu_count() or 4) - 1)))
        self.signals = LoaderSignals()
        self.signals.thumb_done.connect(self._on_thumb)
        self.viewer = None
        self._placeholder = None
        # マウスを乗せたときの拡大表示
        self.hover_signals = LoaderSignals()
        self.hover_signals.thumb_done.connect(self._on_hover_image)
        self.hover_cache = OrderedDict()
        self.hover_pending = set()
        self._hover_path = None
        self.popup = HoverPopup(self)
        self._hover_timer = QTimer(self, singleShot=True, interval=220, timeout=self._show_hover)

        self._build_ui()
        self._build_actions()
        self.setAcceptDrops(True)

        geo = self.settings.value("geometry")
        if geo is not None:
            self.restoreGeometry(geo)
        st = self.settings.value("splitter")
        if st is not None:
            self.splitter.restoreState(st)

    # --- 画面づくり ---
    def _build_ui(self):
        self.fs_model = QFileSystemModel(self)
        self.fs_model.setFilter(QDir.AllDirs | QDir.NoDotAndDotDot | QDir.Drives)
        self.fs_model.setRootPath("/")
        self.tree = QTreeView()
        self.tree.setModel(self.fs_model)
        for c in (1, 2, 3):
            self.tree.hideColumn(c)
        self.tree.setHeaderHidden(True)
        self.tree.setAnimated(True)
        self.tree.selectionModel().currentChanged.connect(self._on_tree_current)

        self.model = QStandardItemModel(self)
        self.list = QListView()
        self.list.setModel(self.model)
        self.list.setViewMode(QListView.IconMode)
        self.list.setResizeMode(QListView.Adjust)
        self.list.setMovement(QListView.Static)
        self.list.setUniformItemSizes(True)
        self.list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.list.setTextElideMode(Qt.ElideMiddle)
        self.list.setWordWrap(False)
        self.list.setSpacing(4)
        self.list.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.list.setDragEnabled(False)
        self.list.doubleClicked.connect(lambda idx: self.open_viewer(idx.row()))
        self.list.selectionModel().selectionChanged.connect(lambda *_: self._sel_timer.start())
        self.list.verticalScrollBar().valueChanged.connect(lambda *_: self._vis_timer.start())
        self.list.installEventFilter(self)
        self.list.setMouseTracking(True)
        self.list.viewport().installEventFilter(self)
        self.list.verticalScrollBar().valueChanged.connect(lambda *_: self._hover_cancel())
        self.empty_label = QLabel("このフォルダには写真がありません", self.list.viewport())
        self.empty_label.setStyleSheet("color:gray;font-size:12pt")
        self.empty_label.hide()

        self.splitter = QSplitter()
        self.splitter.addWidget(self.tree)
        self.splitter.addWidget(self.list)
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([260, 940])
        self.setCentralWidget(self.splitter)

        self.info = QTextBrowser()
        self.info.setOpenExternalLinks(True)
        self.info.setHtml(info_html(None))
        self.dock = QDockWidget("写真の情報", self)
        self.dock.setObjectName("info_dock")
        self.dock.setWidget(self.info)
        self.dock.setFeatures(QDockWidget.DockWidgetClosable | QDockWidget.DockWidgetMovable)
        self.addDockWidget(Qt.RightDockWidgetArea, self.dock)
        self.resizeDocks([self.dock], [300], Qt.Horizontal)

        self.status = QLabel()
        self.statusBar().addWidget(self.status, 1)

        self._sel_timer = QTimer(self, singleShot=True, interval=120, timeout=self._on_selection)
        self._vis_timer = QTimer(self, singleShot=True, interval=60, timeout=self._request_visible)
        self._size_timer = QTimer(self, singleShot=True, interval=250, timeout=self._apply_thumb_size)

    def _build_actions(self):
        S = QStyle.StandardPixmap
        tb = QToolBar("メイン")
        tb.setObjectName("main_toolbar")
        tb.setMovable(False)
        tb.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
        tb.setIconSize(QSize(22, 22))
        self.addToolBar(tb)

        def act(text, keys, slot, icon=None, on_list=False, checkable=False):
            a = QAction(text, self)
            if keys:
                a.setShortcuts([QKeySequence(k) for k in keys])
            if icon is not None:
                a.setIcon(icon)
            a.setCheckable(checkable)
            a.triggered.connect(slot)
            if on_list:   # 一覧にいるときだけ効くキー（入力欄の Delete などとぶつからないように）
                a.setShortcutContext(Qt.WidgetWithChildrenShortcut)
                self.list.addAction(a)
            else:
                self.addAction(a)
            return a

        self.a_open = act("フォルダを開く", ["Ctrl+O"], self.choose_folder,
                          theme_icon(["folder-open"], S.SP_DirOpenIcon))
        self.a_up = act("上へ", ["Alt+Up"], self.go_up, theme_icon(["go-up"], S.SP_FileDialogToParent))
        self.a_reload = act("再読込", ["Ctrl+R"], lambda: self.load_folder(self.folder, keep_sel=True),
                            theme_icon(["view-refresh"], S.SP_BrowserReload))
        self.a_view = act("大きく表示", [], lambda: self.open_viewer(),
                          theme_icon(["zoom-in", "image-x-generic"], S.SP_FileDialogContentsView))
        self.a_slide = act("スライドショー", ["F5"], lambda: self.open_viewer(slideshow=True),
                           theme_icon(["media-playback-start"], S.SP_MediaPlay))
        self.a_rename = act("名前変更", ["F2"], self.rename_selected,
                            theme_icon(["edit-rename", "document-edit"], S.SP_FileDialogDetailedView), True)
        self.a_copy = act("コピー", ["Ctrl+Shift+C"], lambda: self.copy_move(False),
                          theme_icon(["edit-copy"], S.SP_FileDialogNewFolder), True)
        self.a_move = act("移動", ["Ctrl+Shift+M"], lambda: self.copy_move(True),
                          theme_icon(["go-jump", "folder-move"], S.SP_ArrowForward), True)
        self.a_trash = act("ゴミ箱へ", ["Delete"], self.trash_selected,
                           theme_icon(["user-trash"], S.SP_TrashIcon), True)
        self.a_selall = act("すべて選択", ["Ctrl+A"], self.list.selectAll, None, True)
        self.a_files = act("ファイルマネージャで開く", [], self.open_in_file_manager)
        self.a_info = self.dock.toggleViewAction()
        self.a_info.setText("情報パネル")
        self.a_info.setShortcut(QKeySequence("Ctrl+I"))
        self.a_info.setIcon(theme_icon(["dialog-information"], S.SP_MessageBoxInformation))

        for a in (self.a_open, self.a_up, self.a_reload, None, self.a_view, self.a_slide, None,
                  self.a_rename, self.a_copy, self.a_move, self.a_trash, None, self.a_info):
            if a is None:
                tb.addSeparator()
            else:
                tb.addAction(a)

        # パス欄・並び順・サムネイルの大きさ
        bar = QToolBar("場所")
        bar.setObjectName("path_toolbar")
        bar.setMovable(False)
        self.addToolBarBreak()
        self.addToolBar(bar)
        self.path_edit = QLineEdit()
        self.path_edit.setPlaceholderText("フォルダの場所（入力して Enter）")
        self.path_edit.returnPressed.connect(lambda: self.load_folder(os.path.expanduser(self.path_edit.text().strip())))
        bar.addWidget(self.path_edit)
        bar.addWidget(QLabel("　並び順 "))
        self.sort_combo = QComboBox()
        self.sort_combo.addItems(["名前", "更新日時", "サイズ"])
        self.sort_combo.setCurrentIndex(int(self.settings.value("sort_key", 0)))
        self.sort_combo.currentIndexChanged.connect(self._on_sort_changed)
        bar.addWidget(self.sort_combo)
        self.order_btn = QToolButton()
        self.order_btn.setCheckable(True)
        self.order_btn.setChecked(self.settings.value("sort_desc", False, type=bool))
        self.order_btn.toggled.connect(self._on_sort_changed)
        self._update_order_text()
        bar.addWidget(self.order_btn)
        bar.addWidget(QLabel("　大きさ "))
        self.size_slider = QSlider(Qt.Horizontal)
        self.size_slider.setRange(64, 320)
        self.size_slider.setSingleStep(16)
        self.size_slider.setPageStep(32)
        self.size_slider.setValue(self.thumb_size)
        self.size_slider.setFixedWidth(140)
        self.size_slider.valueChanged.connect(lambda *_: self._size_timer.start())
        bar.addWidget(self.size_slider)

        # メニュー
        mb = self.menuBar()
        m = mb.addMenu("ファイル(&F)")
        m.addAction(self.a_open)
        m.addAction(self.a_up)
        m.addAction(self.a_reload)
        m.addAction(self.a_files)
        m.addSeparator()
        m.addAction(act("終了", ["Ctrl+Q"], self.close))
        m = mb.addMenu("編集(&E)")
        for a in (self.a_rename, self.a_copy, self.a_move, self.a_trash, None, self.a_selall):
            m.addSeparator() if a is None else m.addAction(a)
        m = mb.addMenu("表示(&V)")
        m.addAction(self.a_view)
        m.addAction(self.a_slide)
        m.addAction(self.a_info)
        m.addSeparator()
        m.addAction(act("スライドショーの間隔…", [], self.set_interval))
        self.a_loop = act("スライドショーを繰り返す", [], self._toggle_loop, checkable=True)
        self.a_loop.setChecked(self.settings.value("slide_loop", True, type=bool))
        m.addAction(self.a_loop)
        self.a_hover = act("マウスを乗せるとサムネイルを拡大", [], self._toggle_hover, checkable=True)
        self.a_hover.setChecked(self.settings.value("hover_zoom", True, type=bool))
        m.addAction(self.a_hover)
        self.a_upscale = act("小さい写真も画面いっぱいに拡大", [], self._toggle_upscale, checkable=True)
        self.a_upscale.setChecked(self.settings.value("upscale_small", False, type=bool))
        m.addAction(self.a_upscale)
        m = mb.addMenu("ヘルプ(&H)")
        m.addAction(act("キー操作の一覧", ["F1"], self.show_keys))
        m.addAction(act(f"{APP_NAME}について", [], self.show_about))

    # --- フォルダ読み込み ---
    def start_location(self):
        last = self.settings.value("last_folder", "")
        if last and os.path.isdir(last):
            return last
        pics = QStandardPaths.writableLocation(QStandardPaths.PicturesLocation)
        if pics and os.path.isdir(pics):
            return pics
        return QDir.homePath()

    def choose_folder(self):
        d = QFileDialog.getExistingDirectory(self, "フォルダを選ぶ", self.folder or QDir.homePath())
        if d:
            self.load_folder(d)

    def go_up(self):
        if self.folder:
            parent = os.path.dirname(self.folder.rstrip("/")) or "/"
            if parent != self.folder:
                self.load_folder(parent)

    def _on_tree_current(self, cur, _prev):
        p = self.fs_model.filePath(cur)
        if p and os.path.normpath(p) != os.path.normpath(self.folder or ""):
            self.load_folder(p, sync_tree=False)

    def _sort_entries(self, entries):
        key = self.sort_combo.currentIndex()
        desc = self.order_btn.isChecked()
        if key == 1:
            entries.sort(key=lambda e: (e[2], natural_key(e[0])), reverse=desc)
        elif key == 2:
            entries.sort(key=lambda e: (e[1], natural_key(e[0])), reverse=desc)
        else:
            entries.sort(key=lambda e: natural_key(e[0]), reverse=desc)
        return entries

    def load_folder(self, path, keep_sel=False, sync_tree=True, select=None):
        if not path or not os.path.isdir(path):
            if path:
                QMessageBox.warning(self, "開けません", f"フォルダが見つかりません：\n{path}")
            return
        path = os.path.abspath(path)
        prev_sel = self.selected_paths() if keep_sel else []
        self.folder = path
        self.settings.setValue("last_folder", path)
        self.path_edit.setText(path)
        self.setWindowTitle(f"{os.path.basename(path) or path} - {APP_NAME}")

        if sync_tree:
            idx = self.fs_model.index(path)
            if idx.isValid():
                self.tree.selectionModel().blockSignals(True)
                self.tree.setCurrentIndex(idx)
                self.tree.selectionModel().blockSignals(False)
                self.tree.scrollTo(idx)
                self.tree.expand(idx)

        self._hover_cancel()
        self.gen += 1
        self.pool.clear()
        self.requested.clear()
        self.items.clear()
        self.model.clear()

        entries = []
        try:
            with os.scandir(path) as it:
                for e in it:
                    try:
                        if e.is_file() and not e.name.startswith(".") and is_image(e.name):
                            st = e.stat()
                            entries.append((e.name, st.st_size, st.st_mtime))
                    except OSError:
                        continue
        except PermissionError:
            QMessageBox.warning(self, "開けません", "このフォルダを読む権限がありません。")
        self._sort_entries(entries)

        ph = self._placeholder_icon()
        self.model.blockSignals(True)
        for name, size, mtime in entries:
            full = os.path.join(path, name)
            it = QStandardItem(ph, name)
            it.setData(full, PATH_ROLE)
            it.setData(size, SIZE_ROLE)
            it.setData(mtime, MTIME_ROLE)
            it.setToolTip(name)
            it.setEditable(False)
            self.items[full] = it
            self.model.appendRow(it)
        self.model.blockSignals(False)
        self._apply_grid()
        self.list.reset()

        self.empty_label.setVisible(not entries)
        self._place_empty_label()
        self.status.setText(f"{len(entries)} 枚の写真")
        if select:
            self._select_path(select)
        elif prev_sel:
            for p in prev_sel:
                self._select_path(p, add=True)
        self._on_selection()
        self._vis_timer.start()

    def _place_empty_label(self):
        self.empty_label.adjustSize()
        vp = self.list.viewport()
        self.empty_label.move((vp.width() - self.empty_label.width()) // 2, 60)

    def _placeholder_icon(self):
        s = self.thumb_size
        if self._placeholder is None or self._placeholder[0] != s:
            pm = QPixmap(s, s)
            pm.fill(Qt.transparent)
            p = QPainter(pm)
            p.setRenderHint(QPainter.Antialiasing)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(128, 128, 128, 50))
            m = s // 10
            p.drawRoundedRect(m, m + s // 8, s - 2 * m, s - 2 * m - s // 4, 6, 6)
            p.end()
            self._placeholder = (s, QIcon(pm))
        return self._placeholder[1]

    def _apply_grid(self):
        s = self.thumb_size
        self.list.setIconSize(QSize(s, s))
        fm = self.list.fontMetrics()
        self.list.setGridSize(QSize(s + 20, s + fm.height() + 16))

    def _apply_thumb_size(self):
        s = self.size_slider.value()
        if s == self.thumb_size:
            return
        self.thumb_size = s
        self.settings.setValue("thumb_size", s)
        self.hover_cache.clear()
        self._hover_cancel()
        self.gen += 1
        self.pool.clear()
        self.requested.clear()
        self._apply_grid()
        self._vis_timer.start()

    # --- サムネイルは見えている所から順に作る ---
    def _request_visible(self):
        n = self.model.rowCount()
        if not n:
            return
        vp = self.list.viewport().rect()
        h = vp.height()
        area = vp.adjusted(0, -h // 2, 0, h)   # 少し先まで用意しておく
        for row in range(n):
            idx = self.model.index(row, 0)
            r = self.list.visualRect(idx)
            if r.top() > area.bottom():
                break
            if not r.intersects(area):
                continue
            it = self.model.itemFromIndex(idx)
            path = it.data(PATH_ROLE)
            if it.data(THUMB_ROLE) == self.thumb_size or path in self.requested:
                continue
            self.requested.add(path)
            self.pool.start(ThumbTask(self.gen, path, self.thumb_size, self.signals))

    def _on_thumb(self, gen, path, img):
        if gen != self.gen:
            return
        it = self.items.get(path)
        if it is None:
            return
        if img.isNull():
            it.setIcon(QApplication.style().standardIcon(QStyle.SP_MessageBoxWarning))
            it.setToolTip(f"{it.text()}\n（読み込めませんでした）")
        else:
            it.setIcon(QIcon(QPixmap.fromImage(img)))
        it.setData(self.thumb_size, THUMB_ROLE)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._vis_timer.start()
        self._place_empty_label()

    def eventFilter(self, obj, ev):
        if obj is self.list and ev.type() == ev.Type.KeyPress:
            if ev.key() in (Qt.Key_Return, Qt.Key_Enter):
                self.open_viewer()
                return True
            if ev.key() == Qt.Key_Backspace:
                self.go_up()
                return True
        if obj is self.list and ev.type() == ev.Type.Resize:
            self._vis_timer.start()
        if obj is self.list.viewport():
            t = ev.type()
            if t == QEvent.MouseMove:
                self._hover_move(ev.position().toPoint())
            elif t in (QEvent.Leave, QEvent.MouseButtonPress, QEvent.MouseButtonDblClick,
                       QEvent.Wheel, QEvent.Hide):
                self._hover_cancel()
        return super().eventFilter(obj, ev)

    # --- マウスを乗せたサムネイルの拡大 ---
    def _hover_size(self):
        return max(280, min(520, int(self.thumb_size * 2.2)))

    def _hover_move(self, pos):
        idx = self.list.indexAt(pos)
        path = self.model.itemFromIndex(idx).data(PATH_ROLE) if idx.isValid() else None
        if path == self._hover_path:
            return
        self._hover_path = path
        self.popup.dismiss()
        if path and self.a_hover.isChecked():
            self._hover_timer.start()
        else:
            self._hover_timer.stop()

    def _hover_cancel(self):
        self._hover_timer.stop()
        self._hover_path = None
        self.popup.dismiss()

    def _show_hover(self):
        path = self._hover_path
        it = self.items.get(path)
        if it is None or not self.isActiveWindow() and QApplication.activeWindow() is not None:
            return
        if it.data(THUMB_ROLE) is None:
            return   # まだサムネイルができていない
        s = self.thumb_size
        cell = self.list.visualRect(it.index())
        center_vp = QPoint(cell.center().x(), cell.top() + 4 + s // 2)
        center = self.list.viewport().mapTo(self, center_vp)

        pm = self.hover_cache.get(path)
        if pm is None:
            pm = it.icon().pixmap(QSize(s, s))
            if path not in self.hover_pending:
                self.hover_pending.add(path)
                QThreadPool.globalInstance().start(
                    ThumbTask(0, path, self._hover_size(), self.hover_signals))
        else:
            self.hover_cache.move_to_end(path)
        if pm.isNull():
            return
        w, h = pm.width(), pm.height()
        k = s / max(w, h)
        start = QRect(0, 0, max(1, int(w * k)), max(1, int(h * k)))
        start.moveCenter(center)
        big = self._hover_size()
        k = big / max(w, h)
        target = QRect(0, 0, int(w * k), int(h * k))
        target.moveCenter(center)
        # 窓からはみ出さないように寄せる
        area = self.centralWidget().geometry().adjusted(6, 6, -6, -6)
        if target.left() < area.left():
            target.moveLeft(area.left())
        if target.right() > area.right():
            target.moveRight(area.right())
        if target.top() < area.top():
            target.moveTop(area.top())
        if target.bottom() > area.bottom():
            target.moveBottom(area.bottom())
        self.popup.pop(path, pm, start, target)

    def _on_hover_image(self, _gen, path, img):
        self.hover_pending.discard(path)
        if img.isNull():
            return
        pm = QPixmap.fromImage(img)
        self.hover_cache[path] = pm
        while len(self.hover_cache) > 40:
            self.hover_cache.popitem(last=False)
        self.popup.update_pixmap(path, pm)

    def _toggle_hover(self, on):
        self.settings.setValue("hover_zoom", on)
        if not on:
            self._hover_cancel()

    def changeEvent(self, e):
        if e.type() == QEvent.ActivationChange and not self.isActiveWindow():
            self._hover_cancel()
        super().changeEvent(e)

    # --- 選択と情報 ---
    def selected_rows(self):
        return sorted(i.row() for i in self.list.selectionModel().selectedIndexes())

    def selected_paths(self):
        return [self.model.item(r).data(PATH_ROLE) for r in self.selected_rows()]

    def all_paths(self):
        return [self.model.item(r).data(PATH_ROLE) for r in range(self.model.rowCount())]

    def _select_path(self, path, add=False):
        if path is None:
            return
        it = self.items.get(path)
        if it is None:
            return
        idx = it.index()
        sm = self.list.selectionModel()
        flag = sm.SelectionFlag.Select if add else sm.SelectionFlag.ClearAndSelect
        sm.setCurrentIndex(idx, flag)
        self.list.scrollTo(idx)

    def _on_selection(self):
        paths = self.selected_paths()
        total = self.model.rowCount()
        if not paths:
            self.info.setHtml(info_html(None))
            self.status.setText(f"{total} 枚の写真")
            return
        if len(paths) == 1:
            p = paths[0]
            self.info.setHtml(info_html(p))
            it = self.items.get(p)
            size = it.data(SIZE_ROLE) if it else 0
            self.status.setText(f"{total} 枚の写真　｜　{os.path.basename(p)}（{human_size(size)}）")
        else:
            size = sum(self.items[p].data(SIZE_ROLE) or 0 for p in paths if p in self.items)
            self.info.setHtml(f"<p><b>{len(paths)} 枚</b>を選んでいます<br>合計 {human_size(size)}</p>")
            self.status.setText(f"{total} 枚の写真　｜　{len(paths)} 枚選択（{human_size(size)}）")

    def _on_sort_changed(self, *_):
        self._update_order_text()
        self.settings.setValue("sort_key", self.sort_combo.currentIndex())
        self.settings.setValue("sort_desc", self.order_btn.isChecked())
        if self.folder:
            self.load_folder(self.folder, keep_sel=True)

    def _update_order_text(self):
        self.order_btn.setText("▼ 降順" if self.order_btn.isChecked() else "▲ 昇順")

    # --- ビューア ---
    def open_viewer(self, row=None, slideshow=False):
        self._hover_cancel()
        paths = self.all_paths()
        if not paths:
            return
        if row is None:
            rows = self.selected_rows()
            row = rows[0] if rows else 0
        if self.viewer is None:
            self.viewer = ViewerWindow(self.settings)
            self.viewer.setWindowIcon(self.windowIcon())
            self.viewer.currentChanged.connect(lambda p: self._select_path(p))
            self.viewer.fileTrashed.connect(self._remove_path)
        self.viewer.view.upscale_small = self.a_upscale.isChecked()
        self.viewer.open_list(paths, row, slideshow=slideshow)

    def _remove_path(self, path):
        self.hover_cache.pop(path, None)
        if self.popup.path == path:
            self._hover_cancel()
        it = self.items.pop(path, None)
        if it is not None:
            self.model.removeRow(it.row())
        self.requested.discard(path)
        self.empty_label.setVisible(self.model.rowCount() == 0)
        self._on_selection()
        self._vis_timer.start()

    # --- ファイル操作 ---
    def trash_selected(self):
        paths = self.selected_paths()
        if not paths:
            return
        msg = (f"「{os.path.basename(paths[0])}」をゴミ箱へ移動しますか？" if len(paths) == 1
               else f"{len(paths)} 枚の写真をゴミ箱へ移動しますか？")
        if QMessageBox.question(self, "ゴミ箱へ移動", msg, QMessageBox.Yes | QMessageBox.No,
                                QMessageBox.Yes) != QMessageBox.Yes:
            return
        next_row = self.selected_rows()[0]
        failed = []
        for p in paths:
            if move_to_trash(p):
                self._remove_path(p)
            else:
                failed.append(os.path.basename(p))
        if failed:
            QMessageBox.warning(self, "一部失敗", "ゴミ箱へ移動できなかったもの：\n" + "\n".join(failed))
        n = self.model.rowCount()
        if n:
            idx = self.model.index(min(next_row, n - 1), 0)
            self.list.selectionModel().setCurrentIndex(idx, self.list.selectionModel().SelectionFlag.ClearAndSelect)

    def rename_selected(self):
        paths = self.selected_paths()
        if len(paths) != 1:
            if paths:
                QMessageBox.information(self, "名前変更", "名前変更は1枚ずつ行います。1枚だけ選んでください。")
            return
        old = paths[0]
        name = os.path.basename(old)
        new, ok = QInputDialog.getText(self, "名前変更", "新しい名前：", QLineEdit.Normal, name)
        new = new.strip()
        if not ok or not new or new == name:
            return
        if "/" in new or new in (".", ".."):
            QMessageBox.warning(self, "名前変更", "その名前は使えません（「/」は入れられません）。")
            return
        if not os.path.splitext(new)[1]:
            new += os.path.splitext(name)[1]   # 拡張子を忘れたら元のを付ける
        dst = os.path.join(os.path.dirname(old), new)
        if os.path.exists(dst):
            QMessageBox.warning(self, "名前変更", "同じ名前のファイルがすでにあります。")
            return
        try:
            os.rename(old, dst)
        except OSError as e:
            QMessageBox.warning(self, "名前変更", f"変更できませんでした：\n{e}")
            return
        self.load_folder(self.folder, select=dst)

    def copy_move(self, move):
        paths = self.selected_paths()
        if not paths:
            return
        title = "移動先のフォルダ" if move else "コピー先のフォルダ"
        start = self.settings.value("last_dest", "") or self.folder
        dest = QFileDialog.getExistingDirectory(self, title, start)
        if not dest:
            return
        if os.path.normpath(dest) == os.path.normpath(self.folder):
            QMessageBox.information(self, title, "今のフォルダと同じ場所です。別のフォルダを選んでください。")
            return
        self.settings.setValue("last_dest", dest)
        done, renamed, failed = 0, 0, []
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            for p in paths:
                name = os.path.basename(p)
                target = unique_path(dest, name)
                if os.path.basename(target) != name:
                    renamed += 1
                try:
                    if move:
                        shutil.move(p, target)
                        self._remove_path(p)
                    else:
                        shutil.copy2(p, target)
                    done += 1
                except OSError as e:
                    failed.append(f"{name}（{e.strerror or e}）")
                QApplication.processEvents()
        finally:
            QApplication.restoreOverrideCursor()
        word = "移動" if move else "コピー"
        msg = f"{done} 枚を{word}しました。"
        if renamed:
            msg += f"\n（同じ名前があったので {renamed} 枚は「名前 (1)」のように名前をずらしました）"
        if failed:
            msg += "\n\nできなかったもの：\n" + "\n".join(failed)
            QMessageBox.warning(self, word, msg)
        else:
            self.statusBar().showMessage(msg.replace("\n", " "), 6000)

    def open_in_file_manager(self):
        if self.folder:
            QDesktopServices.openUrl(QUrl.fromLocalFile(self.folder))

    # --- 設定 ---
    def set_interval(self):
        cur = float(self.settings.value("slide_interval", 4))
        v, ok = QInputDialog.getDouble(self, "スライドショーの間隔", "何秒ごとに次の写真へ進みますか？",
                                       cur, 1, 600, 1)
        if ok:
            self.settings.setValue("slide_interval", v)
            if self.viewer and self.viewer.slide_timer.isActive():
                self.viewer.slide_timer.setInterval(int(v * 1000))

    def _toggle_loop(self, on):
        self.settings.setValue("slide_loop", on)

    def _toggle_upscale(self, on):
        self.settings.setValue("upscale_small", on)
        if self.viewer:
            self.viewer.view.upscale_small = on
            if self.viewer.view._fit:
                self.viewer.view.fit()

    # --- ヘルプ ---
    def show_keys(self):
        QMessageBox.information(self, "キー操作の一覧", (
            "【一覧の画面】\n"
            "  Enter / ダブルクリック … 大きく表示\n"
            "  F5 … スライドショー\n"
            "  F2 … 名前変更　　Delete … ゴミ箱へ\n"
            "  Ctrl+Shift+C … コピー　　Ctrl+Shift+M … 移動\n"
            "  Backspace / Alt+↑ … 上のフォルダへ\n"
            "  Ctrl+I … 情報パネル　　Ctrl+R … 再読込\n"
            "  マウスを乗せる … サムネイルが大きくなる（表示メニューで切替）\n\n"
            "【大きく表示の画面】\n"
            "  → / Space / PageDown … 次へ\n"
            "  ← / Backspace / PageUp … 前へ\n"
            "  Home / End … 最初 / 最後\n"
            "  マウスホイール / + / - … 拡大・縮小\n"
            "  0 … 画面に合わせる　　1 … 等倍（100%）\n"
            "  ドラッグ … 写真を動かす\n"
            "  R / L … 右回転 / 左回転（表示だけ。ファイルは変わりません）\n"
            "  S … スライドショー開始・停止\n"
            "  F / F11 / ダブルクリック … 全画面\n"
            "  I … 撮影情報を重ねて表示\n"
            "  Delete … ゴミ箱へ\n"
            "  Esc … スライドショー停止 → 全画面解除 → 閉じる"))

    def show_about(self):
        QMessageBox.about(self, f"{APP_NAME}について", (
            f"<h3>{APP_NAME}（Nagamemado） {VERSION}</h3>"
            "<p>Ubuntu用の写真閲覧ソフトです。</p>"
            "<p>作者：飯田顕治<br>ライセンス：MIT</p>"
            f"<p>読める形式：{', '.join(sorted(e[1:] for e in IMAGE_EXTS))}</p>"))

    # --- ドラッグ＆ドロップ ---
    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        urls = [u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()]
        if urls:
            self.open_path(urls[0])

    def open_path(self, p):
        p = os.path.abspath(os.path.expanduser(p))
        if os.path.isdir(p):
            self.load_folder(p)
        elif os.path.isfile(p):
            self.load_folder(os.path.dirname(p), select=p)
            if p in self.items:
                self.open_viewer(self.items[p].row())
        else:
            self.load_folder(self.start_location())

    def closeEvent(self, e):
        self.settings.setValue("geometry", self.saveGeometry())
        self.settings.setValue("splitter", self.splitter.saveState())
        self.pool.clear()
        if self.viewer:
            self.viewer.close()
        super().closeEvent(e)


def find_icon():
    here = os.path.dirname(os.path.abspath(__file__))
    for p in (os.path.join(here, "nagamemado.svg"),
              "/usr/share/icons/hicolor/scalable/apps/nagamemado.svg"):
        if os.path.exists(p):
            return QIcon(p)
    return QIcon.fromTheme("image-viewer")


def main():
    global IMAGE_EXTS
    app = QApplication(sys.argv)
    app.setApplicationName(APP_ID)
    app.setApplicationDisplayName(APP_NAME)
    app.setDesktopFileName(APP_ID)
    IMAGE_EXTS = supported_exts()
    win = MainWindow()
    win.setWindowIcon(find_icon())
    app.setWindowIcon(win.windowIcon())
    win.show()
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    if args:
        win.open_path(args[0])
    else:
        win.load_folder(win.start_location())
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
