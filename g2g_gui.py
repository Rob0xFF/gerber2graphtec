#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Gerber-to-Graphtec GUI — USB-only (synchronized multi-pass, auto device status).

Updates in this version
-----------------------
* **QSettings persistence**: remembers Gerber path, Job file path, offset, margin,
  transform, merge enable, merge tolerance, mode, and all pass speed/force values.
  Settings are **saved when you click “1. Prepare”** (not on close).
* **Preview placeholder** text size reduced by 50% (now ~36pt).
"""

from __future__ import annotations

import sys
import traceback
from pathlib import Path
from typing import List, Optional, Tuple
from enum import Enum

import usb.core
import usb.util
from PyQt5.QtCore import Qt, QObject, QThread, pyqtSignal, QPointF, QTimer, QSettings
from PyQt5.QtGui import QPainterPath, QPen, QFont, QColor, QPalette
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

CHUNK = 8192  # 8 KiB USB bulk packet (tweak for finer progress if desired)

# --------------------------------------------------------------------------- #
# Cutter state enum (mirrors py_silhouette DeviceState)                       #
# --------------------------------------------------------------------------- #
class CutterState(Enum):
    READY    = b"0"
    MOVING   = b"1"
    UNLOADED = b"2"
    PAUSED   = b"3"
    UNKNOWN  = None

_STATE_TEXT = {
    CutterState.READY:    "Ready",
    CutterState.MOVING:   "Busy / finishing job…",
    CutterState.UNLOADED: "No media. Please load material.",
    CutterState.PAUSED:   "Paused",
    CutterState.UNKNOWN:  "Unknown status",
}
_STATE_COLOR = {
    CutterState.READY:    "#0a0",  # green
    CutterState.MOVING:   "#08f",  # blue
    CutterState.UNLOADED: "#ca0",  # yellow
    CutterState.PAUSED:   "#ca0",  # yellow
    CutterState.UNKNOWN:  "#ca0",  # yellow
}
_CUTTING_COLOR = "#08f"  # blue while job streaming

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


def _open_dev_bi():
    """
    Open first supported Silhouette cutter and return (dev, intf, ep_out, ep_in).
    Caller *must* release & dispose.
    """
    for vid, pid in _SUPPORTED:
        dev = usb.core.find(idVendor=vid, idProduct=pid)
        if not dev:
            continue

        try:
            if dev.is_kernel_driver_active(0):
                try:
                    dev.detach_kernel_driver(0)
                except usb.core.USBError:
                    pass
        except (NotImplementedError, usb.core.USBError):
            pass

        try:
            dev.set_configuration()
        except usb.core.USBError:
            pass

        cfg = dev.get_active_configuration()
        intf = cfg[(0, 0)]

        try:
            usb.util.claim_interface(dev, intf.bInterfaceNumber)
        except usb.core.USBError:
            pass

        ep_out = usb.util.find_descriptor(
            intf,
            custom_match=lambda e: usb.util.endpoint_direction(e.bEndpointAddress)
            == usb.util.ENDPOINT_OUT,
        )
        ep_in = usb.util.find_descriptor(
            intf,
            custom_match=lambda e: usb.util.endpoint_direction(e.bEndpointAddress)
            == usb.util.ENDPOINT_IN,
        )

        if ep_out and ep_in:
            return dev, intf, ep_out, ep_in

        try:
            usb.util.release_interface(dev, intf.bInterfaceNumber)
        except Exception:
            pass
        usb.util.dispose_resources(dev)

    raise RuntimeError("No supported cutter found")


def query_cutter_state(timeout_ms: int = 500) -> CutterState:
    """
    Poll the cutter's current state using the ESC-0x05 command.
    Returns a CutterState enum. Raises RuntimeError if no device found.
    """
    dev = intf = ep_out = ep_in = None
    try:
        dev, intf, ep_out, ep_in = _open_dev_bi()
        ep_out.write(b"\x1b\x05", timeout=0)  # status request

        try:
            data_arr = ep_in.read(ep_in.wMaxPacketSize or 64, timeout=timeout_ms)
            try:
                data = data_arr.tobytes()
            except AttributeError:
                data = bytes(data_arr)
        except usb.core.USBError:
            data = b""

        code = data[:1] if data else b""
        if   code == CutterState.READY.value:    return CutterState.READY
        elif code == CutterState.MOVING.value:   return CutterState.MOVING
        elif code == CutterState.UNLOADED.value: return CutterState.UNLOADED
        elif code == CutterState.PAUSED.value:   return CutterState.PAUSED
        else:                                    return CutterState.UNKNOWN

    finally:
        if dev is not None and intf is not None:
            try:
                usb.util.release_interface(dev, intf.bInterfaceNumber)
            except Exception:
                pass
        if dev is not None:
            usb.util.dispose_resources(dev)


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
# USB streaming thread (opens/claims/releases each job)                       #
# --------------------------------------------------------------------------- #
class UsbSender(QThread):
    progress = pyqtSignal(int)
    finished = pyqtSignal()
    error = pyqtSignal(str)

    def __init__(self, fn: Path, parent=None):
        super().__init__(parent)
        self.fn = fn
        self.canceled = False  # debug marker

    @staticmethod
    def _open_dev():
        """Return (dev, intf, ep_out) for first supported cutter (OUT only)."""
        for vid, pid in _SUPPORTED:
            dev = usb.core.find(idVendor=vid, idProduct=pid)
            if not dev:
                continue

            try:
                if dev.is_kernel_driver_active(0):
                    try:
                        dev.detach_kernel_driver(0)
                    except usb.core.USBError:
                        pass
            except (NotImplementedError, usb.core.USBError):
                pass

            try:
                dev.set_configuration()
            except usb.core.USBError:
                pass

            cfg = dev.get_active_configuration()
            intf = cfg[(0, 0)]

            try:
                usb.util.claim_interface(dev, intf.bInterfaceNumber)
            except usb.core.USBError:
                pass

            ep_out = usb.util.find_descriptor(
                intf,
                custom_match=lambda e: usb.util.endpoint_direction(
                    e.bEndpointAddress
                ) == usb.util.ENDPOINT_OUT,
            )
            if ep_out:
                return dev, intf, ep_out

            try:
                usb.util.release_interface(dev, intf.bInterfaceNumber)
            except Exception:
                pass
            usb.util.dispose_resources(dev)

        raise RuntimeError("No supported cutter found")

    def run(self):
        dev = intf = ep = None
        try:
            dev, intf, ep = self._open_dev()
            size = max(1, self.fn.stat().st_size)
            sent = 0
            with self.fn.open("rb") as fh:
                while True:
                    if self.isInterruptionRequested():
                        self.canceled = True
                        break
                    chunk = fh.read(CHUNK)
                    if not chunk:
                        break
                    if self.isInterruptionRequested():
                        self.canceled = True
                        break
                    ep.write(chunk, timeout=0)
                    sent += len(chunk)
                    self.progress.emit(int(sent / size * 100))
            self.finished.emit()
        except Exception as e:
            self.error.emit(str(e))
        finally:
            if dev is not None and intf is not None:
                try:
                    usb.util.release_interface(dev, intf.bInterfaceNumber)
                except Exception:
                    pass
            if dev is not None:
                usb.util.dispose_resources(dev)


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
        self.pass_spin.setValue(1)
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
        self._sender: Optional[UsbSender] = None  # active USB job
        self._job_active = False
        self._cut_cancel_requested = False  # GUI-scoped cancel flag
        self._build_ui()
        self._load_settings()  # <-- load persisted values

    # ------------------------- settings helpers -----------------------------
    def _settings(self) -> QSettings:
        # org/app as requested
        return QSettings("Rob0xFF", "Gerber2Graphtec")

    def _update_pen_color(self):
        """
        Pick a contrasting preview stroke/text color based on the current Qt palette.
        Called at startup and whenever the app receives a PaletteChange event.
        """
        pal = self.view.palette() if hasattr(self, "view") else self.palette()

        # Prefer Base (viewport bg); fall back to Window.
        bg = pal.color(QPalette.Base)
        if not bg.isValid():
            bg = pal.color(QPalette.Window)

        # Perceived luminance (sRGB-ish)
        lum = 0.299 * bg.red() + 0.587 * bg.green() + 0.114 * bg.blue()

        if lum < 128:  # dark background -> light strokes
            fg = QColor("#e0e0e0")
            txt = QColor("#bdbdbd")
        else:          # light background -> dark strokes
            fg = QColor("#000000")
            txt = QColor("#808080")

        self._preview_pen = QPen(fg)
        self._preview_pen.setWidthF(0.001)
        self._preview_text_color = txt

    def changeEvent(self, ev):
        from PyQt5.QtCore import QEvent
        if ev.type() == QEvent.PaletteChange:
            self._update_pen_color()
            self._show_preview()
        super().changeEvent(ev)

        
    @staticmethod
    def _to_bool(v) -> bool:
        if isinstance(v, bool):
            return v
        if isinstance(v, str):
            return v.lower() in ("1", "true", "yes", "on")
        return bool(v)

    def _load_settings(self):
        s = self._settings()

        # paths
        gerb = s.value("paths/gerber", None, type=str)
        if gerb:
            self.inp["gerber"].setText(gerb)
        outp = s.value("paths/output", None, type=str)
        if outp:
            self.inp["output"].setText(outp)

        # scalar text params
        off = s.value("params/offset", None, type=str)
        if off: self.offset_edit.setText(off)
        mar = s.value("params/margin", None, type=str)
        if mar: self.border_edit.setText(mar)
        trn = s.value("params/transform", None, type=str)
        if trn: self.matrix_edit.setText(trn)

        # merge
        merg = s.value("params/merge_enabled", None)
        if merg is not None:
            self.merge_chk.setChecked(self._to_bool(merg))
        mtol = s.value("params/merge_tol", None, type=str)
        if mtol: self.merge_thresh_edit.setText(mtol)

        # mode
        mode_val = s.value("params/mode", None, type=int)
        if mode_val is not None:
            # find item with matching data(); fallback index 0
            idx = self.mode_cmb.findData(mode_val)
            if idx < 0: idx = 0
            self.mode_cmb.setCurrentIndex(idx)

        # passes + per-pass values
        passes = s.value("params/passes", None, type=int)
        if passes is not None:
            self.multi_pass.pass_spin.setValue(max(1, min(3, passes)))
        # set all stored values (even if disabled)
        for i in range(3):
            sp = s.value(f"params/speed_{i+1}", None, type=int)
            if sp is not None:
                self.multi_pass.speed_spins[i].setValue(max(1, min(10, sp)))
            fo = s.value(f"params/force_{i+1}", None, type=int)
            if fo is not None:
                self.multi_pass.force_spins[i].setValue(max(1, min(33, fo)))

    def _save_settings(self):
        s = self._settings()
        # paths
        s.setValue("paths/gerber",  self.inp["gerber"].text())
        s.setValue("paths/output",  self.inp["output"].text())
        # text params
        s.setValue("params/offset",    self.offset_edit.text())
        s.setValue("params/margin",    self.border_edit.text())
        s.setValue("params/transform", self.matrix_edit.text())
        # merge
        s.setValue("params/merge_enabled", self.merge_chk.isChecked())
        s.setValue("params/merge_tol",     self.merge_thresh_edit.text())
        # mode
        s.setValue("params/mode", self.mode_cmb.currentData())
        # passes + per-pass
        s.setValue("params/passes", self.multi_pass.passes())
        for i in range(3):
            s.setValue(f"params/speed_{i+1}", self.multi_pass.speed_spins[i].value())
            s.setValue(f"params/force_{i+1}", self.multi_pass.force_spins[i].value())
        s.sync()

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
        dgrid = QGridLayout(cutter_box)
        dgrid.setVerticalSpacing(2)
        dgrid.setContentsMargins(8, 8, 8, 8)
        left.addWidget(cutter_box)

        self.ind = QLabel()
        self.ind.setFixedSize(10, 10)
        self.ind.setStyleSheet("border-radius:5px;background:#a00")
        self.ind.setToolTip("Green = ready, Yellow = detected but not ready, Red = no cutter, Blue = cutting.")
        dgrid.addWidget(self.ind, 0, 0, 2, 1, Qt.AlignVCenter)

        self.dev_lbl = QLabel("No cutter found")
        self.dev_lbl.setToolTip("Detected cutter model, if any.")
        dgrid.addWidget(self.dev_lbl, 0, 1, 1, 1, Qt.AlignLeft | Qt.AlignVCenter)

        self.dev_state_lbl = QLabel("")
        self.dev_state_lbl.setToolTip("Current cutter status.")
        dgrid.addWidget(self.dev_state_lbl, 1, 1, 1, 1, Qt.AlignLeft | Qt.AlignTop)

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

        self.multi_pass = MultiPassWidget()
        self.multi_pass.setToolTip("Configure up to 3 passes with individual speed/force values.")
        pg.addWidget(self.multi_pass, cur, 0, 1, 2)
        cur += 1

        self.merge_chk = QCheckBox("Merge small shapes")
        self.merge_chk.setToolTip("Collapse / simplify very small or overlapping pad shapes before cutting.")
        pg.addWidget(self.merge_chk, cur, 0, 1, 2)
        cur += 1

        self.merge_thresh_edit = QLineEdit("0.014,0.009")
        add_row("Merge tol.:", self.merge_thresh_edit,
                "Min size and distance of shapes to merge (inches).")

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

        left.addSpacerItem(QSpacerItem(0, 0, QSizePolicy.Minimum, QSizePolicy.Expanding))

        # --- right pane (preview) ------------------------------------------
        self.scene = QGraphicsScene()
        self.view = ZoomView(self.scene)
        self.view.setMinimumSize(800, 600)
        # initialize preview colors for current theme
        self._update_pen_color()
        main.addWidget(self.view)

        self._update_device()
        self._show_preview()

        # auto-refresh timer (1s)
        self._dev_timer = QTimer(self)
        self._dev_timer.setInterval(1000)
        self._dev_timer.timeout.connect(self._poll_device)
        self._dev_timer.start()

    # ------------------------- periodic poll (skip while cutting) ----------
    def _poll_device(self):
        if self._job_active:
            return
        self._update_device()

    # ------------------------- device indicator ----------------------------
    def _update_device(self):
        """Update cutter detection + status indicator (idle-only)."""
        if self._job_active:
            return
        name = detect_dev()
        if not name:
            self.ind.setStyleSheet("border-radius:5px;background:#a00")
            self.dev_lbl.setText("No cutter found")
            self.dev_state_lbl.setText("")
            self.dev_lbl.setToolTip("No supported Silhouette cutter detected. Connect cutter.")
            return

        try:
            state = query_cutter_state(timeout_ms=200)
        except Exception as e:
            self.ind.setStyleSheet("border-radius:5px;background:#ca0")
            self.dev_lbl.setText(name)
            self.dev_state_lbl.setText("Status error")
            self.dev_state_lbl.setToolTip(str(e))
            return

        color = _STATE_COLOR[state]
        text = _STATE_TEXT[state]
        self.ind.setStyleSheet(f"border-radius:5px;background:{color}")
        self.dev_lbl.setText(name)
        self.dev_state_lbl.setText(text)
        self.dev_state_lbl.setToolTip(f"Cutter state: {text}")

    def _set_cutting_ui(self):
        """Show blue dot + 'Cutting…' in cutter box (no %)."""
        self.ind.setStyleSheet(f"border-radius:5px;background:{_CUTTING_COLOR}")
        name = detect_dev() or "Cutter"
        self.dev_lbl.setText(name)
        self.dev_state_lbl.setText("Cutting…")

    def _set_canceling_ui(self):
        self.ind.setStyleSheet(f"border-radius:5px;background:{_CUTTING_COLOR}")
        self.dev_state_lbl.setText("Canceling…")

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
        font.setPointSize(30)  # already scaled down
        item.setFont(font)
        # use theme‑contrasting text color
        item.setDefaultTextColor(getattr(self, "_preview_text_color", Qt.lightGray))
        br = item.boundingRect()
        item.setPos(-br.width() / 2, -br.height() / 2)
        self.scene.setSceneRect(-br.width() / 2, -br.height() / 2, br.width(), br.height())


    # ------------------------- preview helper ------------------------------
    def _show_preview(self):
        if not self._strokes:
            self._show_empty_preview()
            return

        self.scene.clear()
        pen = getattr(self, "_preview_pen", QPen(Qt.black))
        pen.setWidthF(0.001)  # widthF is safe; if already set, harmless
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

            off = floats(self.offset_edit.text()) or [0, 0]
            br  = floats(self.border_edit.text()) or [0, 0]
            mat = floats(self.matrix_edit.text()) or [1, 0, 0, 1]

            speeds = self.multi_pass.speeds()
            forces = self.multi_pass.forces()
            cm = self.mode_cmb.currentData()

            merge = self.merge_chk.isChecked()
            merge_thresh = floats(self.merge_thresh_edit.text()) or [0.014, 0.009]

            strokes = extract_strokes_from_gerber(str(gbr))
            strokes = [[(x / 25.4, y / 25.4) for x, y in poly] for poly in strokes]
            if merge:
                strokes = mergepads.fix_small_geometry(strokes, *merge_thresh)
            self._strokes = strokes
            self._show_preview()

            max_x, max_y = optimize.max_extent(strokes)
            bpath = [
                (-br[0], -br[1]),
                (max_x + br[0], -br[1]),
                (max_x + br[0], max_y + br[1]),
                (-br[0], max_y + br[1]),
            ]

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

            # Save settings *after* successful prepare
            self._save_settings()

            QMessageBox.information(self, "Done", f"File saved:\n{out}")
        except Exception:
            QMessageBox.critical(self, "Error", traceback.format_exc())

    # ------------------------- USB upload ----------------------------------
    def _cut(self):
        if self._sender is not None and self._sender.isRunning():
            QMessageBox.warning(self, "Busy", "A cut job is already in progress.")
            return

        # Pre-flight readiness
        while True:
            name = detect_dev()
            if not name:
                QMessageBox.critical(self, "Error", "No cutter found.")
                return
            try:
                state = query_cutter_state(timeout_ms=200)
            except Exception as e:
                btn = QMessageBox.question(
                    self,
                    "Cutter not accessible",
                    f"Could not query cutter state:\n{e}\n\nRetry?",
                    QMessageBox.Retry | QMessageBox.Cancel,
                    QMessageBox.Retry,
                )
                if btn == QMessageBox.Retry:
                    continue
                return

            if state is CutterState.READY:
                break

            msg = {
                CutterState.UNLOADED: "No material loaded. Load media and try again.",
                CutterState.MOVING:   "Cutter is busy. Wait for it to finish.",
                CutterState.PAUSED:   "Cutter is paused. Clear pause and retry.",
                CutterState.UNKNOWN:  "Cutter state unknown. Proceed anyway?",
            }[state]

            btn = QMessageBox.question(
                self,
                "Cutter not ready",
                msg + "\n\nRetry?",
                QMessageBox.Retry | QMessageBox.Cancel | QMessageBox.Ignore,
                QMessageBox.Retry,
            )
            if btn == QMessageBox.Retry:
                continue
            if btn == QMessageBox.Ignore:
                break
            return

        out = Path(self.inp["output"].text())
        if not out.exists():
            QMessageBox.warning(self, "Missing", "Prepare file first")
            return

        dlg = QProgressDialog("Cutting … 0%", "Cancel", 0, 100, self)
        dlg.setWindowModality(Qt.WindowModal)
        dlg.setAutoClose(False)   # prevent auto-close -> spurious canceled()
        dlg.setAutoReset(False)
        self._dlg_finishing = False  # guard against programmatic close

        self._sender = UsbSender(out, self)

        def _prog(p: int):
            dlg.setValue(p)
            dlg.setLabelText(f"Cutting … {p}%")

        self._sender.progress.connect(_prog)

        self._job_active = True
        self._cut_cancel_requested = False  # reset cancel flag
        self._set_cutting_ui()

        # ---- local handlers ------------------------------------------------
        def _cleanup():
            if self._sender:
                self._sender.deleteLater()
                self._sender = None
            self._job_active = False
            self._update_device()  # resume real polling

        def _done():
            self._dlg_finishing = True
            dlg.close()  # may emit canceled(); guard in _cancel()
            if self._cut_cancel_requested:
                QMessageBox.information(self, "Canceled", "Job canceled.")
            else:
                QMessageBox.information(self, "Done", "Job finished.")
            _cleanup()

        def _err(msg: str):
            self._dlg_finishing = True
            dlg.close()
            QMessageBox.critical(self, "Error", msg)
            _cleanup()

        def _cancel():
            # Guard against programmatic close at finish.
            if self._dlg_finishing:
                return
            if not (self._sender and self._sender.isRunning()):
                return
            self._cut_cancel_requested = True
            dlg.setLabelText("Canceling …")
            dlg.setCancelButton(None)
            self._set_canceling_ui()
            self._sender.canceled = True
            self._sender.requestInterruption()

        self._sender.finished.connect(_done)
        self._sender.error.connect(_err)
        dlg.canceled.connect(_cancel)

        self._sender.start()
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