#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Gerber-to-Graphtec GUI — USB-only (synchronized multi-pass).

UI refinements
--------------
* Short English labels + tooltips.
* Pass grid (Speed/Force per pass, 1–3).
* Merge small shapes + tolerance.
* Device auto-detect.
* Non-blocking USB upload.
* **NEW:** Centered “Preview” placeholder when no layout is loaded.
"""

from __future__ import annotations

import sys
import traceback
from pathlib import Path
from typing import List, Optional, Tuple

import usb.core
import usb.util
from PyQt5.QtCore import Qt, QObject, QThread, pyqtSignal, QPointF
from PyQt5.QtGui import QPainterPath, QPen, QFont
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
                ) == usb.util.ENDPOINT_OUT,
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
        vbox.setContentsMargins(0, 0, 0, 0)

        # Pass count row
        top = QHBoxLayout()
        vbox.addLayout(top)
        lbl_passes = QLabel("Passes:")
        lbl_passes.setToolTip("Number of cut passes (1–3).")
        top.addWidget(lbl_passes)
        self.pass_spin = QSpinBox()
        self.pass_spin.setRange(1, 3)
        self.pass_spin.setValue(2)
        self.pass_spin.setToolTip("Number of cut passes (1–3).")
        top.addWidget(self.pass_spin)
        top.addStretch()

        # Grid header + pass rows
        grid = QGridLayout()
        vbox.addLayout(grid)

        header_blank = QLabel("")
        header_speed = QLabel("Speed")
        header_force = QLabel("Force")
        header_speed.setAlignment(Qt.AlignCenter)
        header_force.setAlignment(Qt.AlignCenter)
        header_speed.setToolTip("Cut speed (1=slow, 10=fast).")
        header_force.setToolTip("Blade force (1=light, 33=heavy).")
        grid.addWidget(header_blank, 0, 0)
        grid.addWidget(header_speed, 0, 1)
        grid.addWidget(header_force, 0, 2)

        self.speed_spins: List[QSpinBox] = []
        self.force_spins: List[QSpinBox] = []

        for i in range(3):
            row = i + 1
            lbl = QLabel(f"Pass {i+1}:")
            lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            lbl.setToolTip(f"Settings for pass {i+1}.")
            grid.addWidget(lbl, row, 0)

            s = QSpinBox()
            s.setRange(1, 10)
            s.setToolTip("Cut speed (1=slow, 10=fast).")
            grid.addWidget(s, row, 1)

            f = QSpinBox()
            f.setRange(1, 33)
            f.setToolTip("Blade force (1=light, 33=heavy).")
            grid.addWidget(f, row, 2)

            enabled = i == 0
            s.setEnabled(enabled)
            f.setEnabled(enabled)

            self.speed_spins.append(s)
            self.force_spins.append(f)

        # Enable/disable pass rows on change
        self.pass_spin.valueChanged.connect(self._update_enabled)

    def _update_enabled(self, val: int):
        for i in range(3):
            en = i < val
            self.speed_spins[i].setEnabled(en)
            self.force_spins[i].setEnabled(en)

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

        # Cutter box ---------------------------------------------------------
        cutter_box = QGroupBox("Cutter")
        cutter_box.setToolTip("Connected Silhouette cutter (auto-detected).")
        dl = QHBoxLayout(cutter_box)
        self.ind = QLabel()
        self.ind.setFixedSize(10, 10)
        self.ind.setStyleSheet("border-radius:5px;background:#a00")
        self.ind.setToolTip("Green = detected, red = not detected.")
        self.dev_lbl = QLabel("not detected")
        self.dev_lbl.setToolTip("Detected cutter model, if any.")
        dl.addWidget(self.ind)
        dl.addWidget(self.dev_lbl)
        dl.addStretch()
        btn_refresh = QPushButton("Refresh")
        btn_refresh.setToolTip("Re-scan USB for supported cutters.")
        btn_refresh.clicked.connect(self._update_device)
        dl.addWidget(btn_refresh)
        left.addWidget(cutter_box)

        # Job Files box ------------------------------------------------------
        files_box = QGroupBox("Job Files")
        files_box.setToolTip("Select input Gerber and output Graphtec job file.")
        fg = QGridLayout(files_box)
        left.addWidget(files_box)
        self.inp: dict[str, QLineEdit] = {}

        def add_path(row: int, label: str, key: str, default: str, is_open: bool, tip: str):
            lbl = QLabel(label + ":")
            lbl.setToolTip(tip)
            fg.addWidget(lbl, row, 0)
            le = QLineEdit(default)
            le.setToolTip(tip)
            fg.addWidget(le, row, 1)
            self.inp[key] = le
            btn = QPushButton("…")
            btn.setToolTip("Browse…")
            fg.addWidget(btn, row, 2)
            btn.clicked.connect(lambda _, op=is_open: self._browse(op))

        add_path(0, "Gerber",  "gerber",  "in.gbr",       True,  "Input Gerber layer to cut.")
        add_path(1, "Job file","output",  "out.graphtec", False, "Where to write the Graphtec/Silhouette cut job.")

        # Settings box -------------------------------------------------------
        settings_box = QGroupBox("Settings")
        settings_box.setToolTip("Cut parameters.")
        pg = QGridLayout(settings_box)
        left.addWidget(settings_box)
        cur = 0

        def add_row(label: str, widget: QWidget, tip: str):
            nonlocal cur
            lbl = QLabel(label)
            lbl.setToolTip(tip)
            widget.setToolTip(tip)
            pg.addWidget(lbl, cur, 0)
            pg.addWidget(widget, cur, 1)
            cur += 1

        self.offset_edit = QLineEdit("1.0,4.5")
        add_row("Offset (in):", self.offset_edit,
                "Shift all coordinates by X,Y before cutting (inches).")

        self.border_edit = QLineEdit("0,0")
        add_row("Margin (in):", self.border_edit,
                "Extra margin added to design bounding box (inches).")

        self.matrix_edit = QLineEdit("1,0,0,1")
        add_row("Transform:", self.matrix_edit,
                "Affine transform [a,b,c,d] applied before output (advanced).")

        # Multi-pass widget spans both columns
        self.multi_pass = MultiPassWidget()
        self.multi_pass.setToolTip("Configure up to 3 passes with individual speed/force values.")
        pg.addWidget(self.multi_pass, cur, 0, 1, 2)
        cur += 1

        # Merge checkbox
        self.merge_chk = QCheckBox("Merge small shapes")
        self.merge_chk.setToolTip("Collapse / simplify very small or overlapping pad shapes before cutting.")
        pg.addWidget(self.merge_chk, cur, 0, 1, 2)
        cur += 1

        # Merge tolerance
        self.merge_thresh_edit = QLineEdit("0.014,0.01")
        add_row("Merge tol.:", self.merge_thresh_edit,
                "Min size and distance of shapes to merge (inches).")

        # Mode
        self.mode_cmb = QComboBox()
        self.mode_cmb.addItem("Enhanced", 0)
        self.mode_cmb.addItem("Standard", 1)
        add_row("Mode:", self.mode_cmb,
                "Enhanced = line-optimized toolpaths; Standard = closed polygons.")

        # Action buttons -----------------------------------------------------
        actions = QHBoxLayout()
        btn_prep = QPushButton("1. Prepare")
        btn_prep.setToolTip("Parse Gerber and generate Graphtec job file.")
        btn_prep.clicked.connect(self._prepare)
        actions.addWidget(btn_prep)
        btn_cut = QPushButton("2. Cut")
        btn_cut.setToolTip("Send the prepared job file to the cutter via USB.")
        btn_cut.clicked.connect(self._cut)
        actions.addWidget(btn_cut)
        left.addLayout(actions)

        # Spacer
        left.addSpacerItem(QSpacerItem(0, 0, QSizePolicy.Minimum, QSizePolicy.Expanding))

        # --- right pane (preview) ------------------------------------------
        self.scene = QGraphicsScene()
        self.view = ZoomView(self.scene)
        self.view.setMinimumSize(800, 600)
        main.addWidget(self.view)

        # initial device scan
        self._update_device()

        # initial placeholder
        self._show_preview()

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

    # ------------------------- placeholder preview -------------------------
    def _show_empty_preview(self):
        """Show centered 'Preview' text when no strokes are loaded."""
        self.scene.clear()
        item = self.scene.addText("Preview")
        font = QFont(item.font())
        font.setPointSize(48)
        item.setFont(font)
        item.setDefaultTextColor(Qt.lightGray)
        br = item.boundingRect()
        # center around 0,0
        item.setPos(-br.width() / 2, -br.height() / 2)
        self.scene.setSceneRect(-br.width() / 2, -br.height() / 2, br.width(), br.height())
        self.view.fitInView(self.scene.sceneRect(), Qt.KeepAspectRatio)

    # ------------------------- preview helper ------------------------------
    def _show_preview(self):
        if not self._strokes:
            self._show_empty_preview()
            return

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
            # library emits inches; convert to mm
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

                if cm == 0:  # Enhanced / optimized
                    lines = optimize.optimize(strokes, br)
                    for s, f in zip(speeds, forces):
                        apply(s, f)
                        for x1, y1, x2, y2 in lines:
                            g.line(x1, y1, x2, y2)
                        if any(br):
                            g.closed_path(bpath)
                else:  # Standard / closed polys
                    for s, f in zip(speeds, forces):
                        apply(s, f)
                        for poly in strokes:
                            g.closed_path(poly)
                        if any(br):
                            g.closed_path(bpath)
                g.end()

            QMessageBox.information(self, "Done", f"Job file created:\n{out}")
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