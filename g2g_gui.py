#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Gerber-to-Graphtec GUI — USB-only (synchronized multi-pass).

Key points
----------
* Device auto-detection (green/red dot, Refresh button).
* Passes selector (1–3) reveals matching pairs of Speed + Force spin-boxes.
  → Lists are always the same length.
* Merge checkbox, editable Merge-threshold.
* Cut-mode combo (“Enhanced” / “Standard”).
* Non-blocking USB upload (QThread + progress bar).
"""

from __future__ import annotations

import sys
import traceback
from pathlib import Path
from typing import List, Optional, Tuple

import usb.core
import usb.util
from PyQt5.QtCore import Qt, QObject, QThread, pyqtSignal, QPointF
from PyQt5.QtGui import QPainterPath, QPen
from PyQt5.QtWidgets import (
    QApplication,
    QFileDialog,
    QGraphicsPathItem,
    QGraphicsScene,
    QGraphicsView,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QSpacerItem,
    QVBoxLayout,
    QWidget,
    QProgressDialog,
    QComboBox,
    QCheckBox,
)

import graphtec
import mergepads
import optimize
from gerber_parser import extract_strokes_from_gerber

# --------------------------------------------------------------------------- #
# Patch legacy "rU" open mode                                                 #
# --------------------------------------------------------------------------- #
import builtins as _b

_orig_open = _b.open  # keep original
_b.open = lambda f, m="r", *a, **k: _orig_open(f, m.replace("U", ""), *a, **k)

# --------------------------------------------------------------------------- #
# Supported Silhouette devices                                               #
# --------------------------------------------------------------------------- #
_DEVICES: List[Tuple[str, int, int]] = [
    ("Silhouette Portrait",        0x0B4D, 0x1123),
    ("Silhouette Portrait 2",      0x0B4D, 0x1132),
    ("Silhouette Portrait 3",      0x0B4D, 0x113A),
    ("Silhouette Cameo",           0x0B4D, 0x1121),
    ("Silhouette Cameo 2",         0x0B4D, 0x112B),
    ("Silhouette Cameo 3",         0x0B4D, 0x112F),
    ("Silhouette Cameo 4",         0x0B4D, 0x1137),
    ("Silhouette Cameo 4 Plus",    0x0B4D, 0x1138),
    ("Silhouette Cameo 4 Pro",     0x0B4D, 0x1139),
]
_SUPPORTED = [(vid, pid) for _, vid, pid in _DEVICES]
_NAME = {(vid, pid): name for name, vid, pid in _DEVICES}

CHUNK = 8192  # 8 KiB USB bulk packet

# --------------------------------------------------------------------------- #
# Helper functions                                                            #
# --------------------------------------------------------------------------- #
def floats(s: str) -> List[float]:
    """Parse comma-separated numbers (spaces ignored)."""
    return [float(x) for x in s.replace(" ", "").split(",") if x]


def detect_dev() -> Optional[str]:
    """Return first connected supported cutter name or None."""
    for vid, pid in _SUPPORTED:
        if usb.core.find(idVendor=vid, idProduct=pid):
            return _NAME[(vid, pid)]
    return None


# --------------------------------------------------------------------------- #
# Zoomable QGraphicsView                                                      #
# --------------------------------------------------------------------------- #
class ZoomView(QGraphicsView):
    _STEP, _MIN, _MAX = 1.15, -10, 20

    def wheelEvent(self, ev):
        dy = ev.angleDelta().y()
        if dy == 0:
            return super().wheelEvent(ev)
        d = 1 if dy > 0 else -1
        if not (self._MIN <= getattr(self, "_z", 0) + d <= self._MAX):
            return
        f = self._STEP if d > 0 else 1 / self._STEP
        self.scale(f, f)
        self._z = getattr(self, "_z", 0) + d


# --------------------------------------------------------------------------- #
# USB streaming thread                                                        #
# --------------------------------------------------------------------------- #
class UsbSender(QThread):
    progress = pyqtSignal(int)
    finished = pyqtSignal()
    error = pyqtSignal(str)

    def __init__(self, fn: Path, parent=None):
        super().__init__(parent)
        self.fn = fn

    @staticmethod
    def _ep_out():
        for vid, pid in _SUPPORTED:
            dev = usb.core.find(idVendor=vid, idProduct=pid)
            if not dev:
                continue
            try:
                dev.set_configuration()
            except usb.core.USBError:
                pass
            intf = dev.get_active_configuration()[(0, 0)]
            ep = usb.util.find_descriptor(
                intf,
                custom_match=lambda e: usb.util.endpoint_direction(
                    e.bEndpointAddress
                )
                == usb.util.ENDPOINT_OUT,
            )
            if ep:
                return ep
        raise RuntimeError("No supported cutter found")

    def run(self):
        try:
            ep = self._ep_out()
            size = max(1, self.fn.stat().st_size)
            sent = 0
            with self.fn.open("rb") as fh:
                for chunk in iter(lambda: fh.read(CHUNK), b""):
                    ep.write(chunk, timeout=0)
                    sent += len(chunk)
                    self.progress.emit(int(sent / size * 100))
            self.finished.emit()
        except Exception as e:
            self.error.emit(str(e))


# --------------------------------------------------------------------------- #
# Multi-Pass widget (keeps Speed / Force in sync)                             #
# --------------------------------------------------------------------------- #
class MultiPassWidget(QWidget):
    """Pass selector (1–3) with synchronized Speed & Force spin-boxes."""

    def __init__(self, parent=None):
        super().__init__(parent)
        vbox = QVBoxLayout(self)

        # Pass counter
        top = QHBoxLayout()
        vbox.addLayout(top)
        top.addWidget(QLabel("Passes:"))
        self.pass_spin = QSpinBox()
        self.pass_spin.setRange(1, 3)
        self.pass_spin.setValue(1)
        top.addWidget(self.pass_spin)
        top.addStretch()

        # Grid of speed/force pairs
        grid = QGridLayout()
        vbox.addLayout(grid)
        self.speed_spins: List[QSpinBox] = []
        self.force_spins: List[QSpinBox] = []
        for i in range(3):
            s = QSpinBox()
            s.setRange(1, 10)
            f = QSpinBox()
            f.setRange(1, 33)

            enabled = i == 0
            s.setEnabled(enabled)
            f.setEnabled(enabled)

            self.speed_spins.append(s)
            self.force_spins.append(f)

            grid.addWidget(QLabel(f"Speed {i+1}"), i, 0)
            grid.addWidget(s, i, 1)
            grid.addWidget(QLabel(f"Force {i+1}"), i, 2)
            grid.addWidget(f, i, 3)

        # keep widgets enabled/disabled
        self.pass_spin.valueChanged.connect(self._update_enabled)

    # internal helper
    def _update_enabled(self, val: int):
        for i in range(3):
            en = i < val
            self.speed_spins[i].setEnabled(en)
            self.force_spins[i].setEnabled(en)

    # public interface
    def speeds(self) -> List[int]:
        n = self.pass_spin.value()
        return [s.value() for s in self.speed_spins[:n]]

    def forces(self) -> List[int]:
        n = self.pass_spin.value()
        return [f.value() for f in self.force_spins[:n]]

    def passes(self) -> int:
        return self.pass_spin.value()


# --------------------------------------------------------------------------- #
# Main GUI                                                                    #
# --------------------------------------------------------------------------- #
class Gui(QWidget):
    def __init__(self):
        super().__init__()
        self._strokes: List[List[Tuple[float, float]]] = []
        self._build_ui()

    # ------------------------- UI layout ------------------------------------
    def _build_ui(self):
        self.setWindowTitle("Gerber → Graphtec (USB)")
        main = QHBoxLayout(self)

        # --- left pane ------------------------------------------------------
        left = QVBoxLayout()
        main.addLayout(left)
        # Device box
        dbox = QGroupBox("Device")
        dl = QHBoxLayout(dbox)
        self.ind = QLabel()
        self.ind.setFixedSize(10, 10)
        self.ind.setStyleSheet("border-radius:5px;background:#a00")
        self.dev_lbl = QLabel("not detected")
        dl.addWidget(self.ind)
        dl.addWidget(self.dev_lbl)
        dl.addStretch()
        dl.addWidget(QPushButton("Refresh", clicked=self._update_device))
        left.addWidget(dbox)

        # Files box
        fbox = QGroupBox("Files")
        fg = QGridLayout(fbox)
        left.addWidget(fbox)
        self.inp: dict[str, QLineEdit] = {}

        def add_path(row: int, label: str, key: str, default: str, is_open: bool):
            fg.addWidget(QLabel(label + ":"), row, 0)
            le = QLineEdit(default)
            fg.addWidget(le, row, 1)
            self.inp[key] = le
            btn = QPushButton("…")
            fg.addWidget(btn, row, 2)
            btn.clicked.connect(
                lambda _, op=is_open: self._browse(op)
            )

        add_path(0, "Gerber", "gerber", "in.gbr", True)
        add_path(1, "Output", "output", "out.graphtec", False)

        # Parameters box
        pbox = QGroupBox("Parameters")
        pg = QGridLayout(pbox)
        left.addWidget(pbox)
        cur = 0

        def add_row(label: str, widget: QWidget):
            nonlocal cur
            pg.addWidget(QLabel(label), cur, 0)
            pg.addWidget(widget, cur, 1)
            cur += 1

        self.offset_edit = QLineEdit("1.0,4.5")
        add_row("Offset x,y [in]:", self.offset_edit)
        self.border_edit = QLineEdit("0,0")
        add_row("Border x,y [in]:", self.border_edit)
        self.matrix_edit = QLineEdit("1,0,0,1")
        add_row("Matrix:", self.matrix_edit)

        # Multi-pass widget
        self.multi_pass = MultiPassWidget()
        add_row("Pass settings:", self.multi_pass)

        # Merge & threshold
        self.merge_chk = QCheckBox("Enable merge")
        pg.addWidget(self.merge_chk, cur, 0, 1, 2)
        cur += 1
        self.merge_thresh_edit = QLineEdit("0.014,0.009")
        add_row("Merge threshold:", self.merge_thresh_edit)

        # Cut mode
        self.mode_cmb = QComboBox()
        self.mode_cmb.addItem("Enhanced", 0)
        self.mode_cmb.addItem("Standard", 1)
        add_row("Cut mode:", self.mode_cmb)

        # Action buttons
        actions = QHBoxLayout()
        actions.addWidget(QPushButton("1. Prepare", clicked=self._prepare))
        actions.addWidget(QPushButton("2. Cut", clicked=self._cut))
        left.addLayout(actions)
        left.addSpacerItem(
            QSpacerItem(0, 0, QSizePolicy.Minimum, QSizePolicy.Expanding)
        )

        # --- right pane (preview) ------------------------------------------
        self.scene = QGraphicsScene()
        self.view = ZoomView(self.scene)
        self.view.setMinimumSize(800, 600)
        main.addWidget(self.view)

        self._update_device()

    # ------------------------- device indicator ----------------------------
    def _update_device(self):
        n = detect_dev()
        self.ind.setStyleSheet(
            f"border-radius:5px;background:{'#0a0' if n else '#a00'}"
        )
        self.dev_lbl.setText(n or "not detected")

    # ------------------------- file dialogs --------------------------------
    def _browse(self, is_open: bool):
        path, _ = (
            QFileDialog.getOpenFileName
            if is_open
            else QFileDialog.getSaveFileName
        )(self, "Select file", "", "All Files (*)")
        if path:
            self.inp["gerber" if is_open else "output"].setText(path)

    # ------------------------- preview helper ------------------------------
    def _show_preview(self):
        self.scene.clear()
        pen = QPen(Qt.black)
        pen.setWidthF(0.001)
        for poly in self._strokes:
            path = QPainterPath(QPointF(*poly[0]))
            for x, y in poly[1:]:
                path.lineTo(QPointF(x, y))
            item = QGraphicsPathItem(path)
            item.setPen(pen)
            self.scene.addItem(item)
        self.scene.setSceneRect(self.scene.itemsBoundingRect())
        self.view.fitInView(self.scene.sceneRect(), Qt.KeepAspectRatio)

    # ------------------------- prepare Graphtec file -----------------------
    def _prepare(self):
        try:
            gbr = Path(self.inp["gerber"].text())
            out = Path(self.inp["output"].text())
            if not gbr.is_file():
                raise RuntimeError("Gerber not found")

            # basic params
            off = floats(self.offset_edit.text()) or [0, 0]
            br = floats(self.border_edit.text()) or [0, 0]
            mat = floats(self.matrix_edit.text()) or [1, 0, 0, 1]

            # multi-pass params
            speeds = self.multi_pass.speeds()
            forces = self.multi_pass.forces()
            cm = self.mode_cmb.currentData()

            # merge
            merge = self.merge_chk.isChecked()
            merge_thresh = floats(self.merge_thresh_edit.text()) or [0.014, 0.009]

            # extract strokes
            strokes = extract_strokes_from_gerber(str(gbr))
            strokes = [[(x / 25.4, y / 25.4) for x, y in poly] for poly in strokes]
            if merge:
                strokes = mergepads.fix_small_geometry(strokes, *merge_thresh)
            self._strokes = strokes
            self._show_preview()

            # boundary
            max_x, max_y = optimize.max_extent(strokes)
            bpath = [
                (-br[0], -br[1]),
                (max_x + br[0], -br[1]),
                (max_x + br[0], max_y + br[1]),
                (-br[0], max_y + br[1]),
            ]

            # write file
            with out.open("w") as fout:
                g = graphtec.graphtec(out_file=fout)
                g.start()
                g.set(offset=(off[0] + br[0] + 0.5, off[1] + br[1] + 0.5), matrix=mat)

                def apply(s, f):
                    g.set(speed=s, force=f)

                if cm == 0:  # enhanced
                    lines = optimize.optimize(strokes, br)
                    for s, f in zip(speeds, forces):
                        apply(s, f)
                        for x1, y1, x2, y2 in lines:
                            g.line(x1, y1, x2, y2)
                        if any(br):
                            g.closed_path(bpath)
                else:  # standard
                    for s, f in zip(speeds, forces):
                        apply(s, f)
                        for poly in strokes:
                            g.closed_path(poly)
                        if any(br):
                            g.closed_path(bpath)
                g.end()

            QMessageBox.information(self, "Done", f"File saved:\n{out}")
        except Exception:
            QMessageBox.critical(self, "Error", traceback.format_exc())

    # ------------------------- USB upload ----------------------------------
    def _cut(self):
        out = Path(self.inp["output"].text())
        if not out.exists():
            QMessageBox.warning(self, "Missing", "Prepare file first")
            return

        dlg = QProgressDialog("Cutting …", "Cancel", 0, 100, self)
        dlg.setWindowModality(Qt.WindowModal)

        sender = UsbSender(out, self)
        sender.progress.connect(dlg.setValue)
        sender.finished.connect(
            lambda: (dlg.close(), QMessageBox.information(self, "Done", "Job finished"))
        )
        sender.error.connect(
            lambda m: (dlg.close(), QMessageBox.critical(self, "Error", m))
        )
        dlg.canceled.connect(sender.terminate)
        sender.start()
        dlg.show()


# --------------------------------------------------------------------------- #
# Global uncaught-exception hook                                              #
# --------------------------------------------------------------------------- #
class Hook(QObject):
    exc = pyqtSignal(object, object, object)

    def __init__(self):
        super().__init__()
        sys.excepthook = self._handler
        self.exc.connect(self._show)

    def _handler(self, etype, value, tb):
        self.exc.emit(etype, value, tb)

    @staticmethod
    def _show(etype, value, tb):
        QMessageBox.critical(
            None, "Uncaught", "".join(traceback.format_exception(etype, value, tb))
        )


# --------------------------------------------------------------------------- #
# main                                                                        #
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    app = QApplication(sys.argv)
    Hook()
    Gui().show()
    sys.exit(app.exec())