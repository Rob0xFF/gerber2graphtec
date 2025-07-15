#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import sys
import traceback
from pathlib import Path

import usb.core, usb.util

from PyQt5.QtWidgets import (
    QApplication, QWidget, QLabel, QLineEdit, QPushButton, QFileDialog,
    QVBoxLayout, QHBoxLayout, QGridLayout, QGroupBox, QSizePolicy, QSpacerItem,
    QGraphicsScene, QMessageBox, QGraphicsPathItem, QGraphicsView,
)
from PyQt5.QtCore import Qt, QObject, pyqtSignal, QPointF
from PyQt5.QtGui import QPainterPath, QPen, QPainter

import graphtec
import optimize
import mergepads
from gerber_parser import extract_strokes_from_gerber

# ---------------------------------------------------------------------------
# Patch for deprecated "rU" mode (still used by some legacy libraries)
# ---------------------------------------------------------------------------
import builtins
_open = builtins.open

def _open_patch(filename, mode="r", *args, **kwargs):
    if "U" in mode:
        mode = mode.replace("U", "")
    return _open(filename, mode, *args, **kwargs)

builtins.open = _open_patch

# ---------------------------------------------------------------------------
# Helper utilities
# ---------------------------------------------------------------------------

def floats(s: str):
    """Wandelt kommaseparierte Zahlen in Float-Liste um."""
    return list(map(float, s.strip().split(",")))

# ---------------------------------------------------------------------------
# Zoom‑capable QGraphicsView
# ---------------------------------------------------------------------------
class ZoomView(QGraphicsView):
    """QGraphicsView mit Mausrad-Zoom (¼× … 16×) — Anker unter Maus."""

    _ZOOM_STEP = 1.15
    _ZOOM_MIN  = -10  # • 1 / 1.15^10  ≈ 0.25×
    _ZOOM_MAX  =  20  # • 1.15^20      ≈ 16×

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # Render-Qualität
        self.setRenderHint(QPainter.Antialiasing)

        # Zoomen unter Maus / zentriert
        self.setTransformationAnchor(QGraphicsView.AnchorUnderMouse)
        self.setResizeAnchor(QGraphicsView.AnchorViewCenter)

        self._zoom = 0

    # ------------------------------------------------------------------
    # Mouse‑wheel override
    # ------------------------------------------------------------------
    def wheelEvent(self, event):
        delta_y = event.angleDelta().y()
        if delta_y == 0:                       # horizontaler Scroll → ignorieren
            return super().wheelEvent(event)

        direction = 1 if delta_y > 0 else -1
        if not (self._ZOOM_MIN <= self._zoom + direction <= self._ZOOM_MAX):
            return  # Zoom-Grenzen erreicht

        factor = self._ZOOM_STEP if direction > 0 else 1 / self._ZOOM_STEP
        self.scale(factor, factor)
        self._zoom += direction

# ---------------------------------------------------------------------------
# Haupt-GUI
# ---------------------------------------------------------------------------
class G2GGui(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Gerber to Graphtec (Silhouette Cutter)")
        self.setMinimumWidth(720)
        self.setup_ui()

    # --------------------------------------------------------------
    # Vorschau aktualisieren
    # --------------------------------------------------------------
    def update_preview(self, strokes):
        self.scene.clear()
        pen = QPen(Qt.black)
        pen.setWidthF(0.001)

        for path in strokes:
            if not path:
                continue
            painter_path = QPainterPath()
            painter_path.moveTo(QPointF(*path[0]))
            for pt in path[1:]:
                painter_path.lineTo(QPointF(*pt))
            item = QGraphicsPathItem(painter_path)
            item.setPen(pen)
            self.scene.addItem(item)

        # Szene-Rect setzen und gesamtes Board einpassen
        self.scene.setSceneRect(self.scene.itemsBoundingRect())
        self.view.fitInView(self.scene.sceneRect(), Qt.KeepAspectRatio)

    # --------------------------------------------------------------
    # UI aufbauen
    # --------------------------------------------------------------
    def setup_ui(self):
        main_layout  = QHBoxLayout()
        left_layout  = QVBoxLayout()
        self.inputs  = {}

        # -------------------------------------------------- Helper ---
        def add_row(grid, label_text, key, default="", row=None):
            label = QLabel(label_text)
            label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            edit = QLineEdit(default)
            edit.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
            self.inputs[key] = edit
            row_index = row if row is not None else grid.rowCount()
            grid.addWidget(label, row_index, 0)
            grid.addWidget(edit, row_index, 1)

        # ---------------------------------------------- Dateien-Box ---
        file_box   = QGroupBox("Files")
        file_layout = QGridLayout()

        # Gerber
        gerber_edit = QLineEdit(
            "in.gbr"
        )
        self.inputs["gerber"] = gerber_edit
        file_layout.addWidget(QLabel("Gerber File:"), 0, 0)
        file_layout.addWidget(gerber_edit, 0, 1)
        btn_gerber = QPushButton("Browse")
        btn_gerber.clicked.connect(lambda: self.browse_file("gerber"))
        file_layout.addWidget(btn_gerber, 0, 2)

        # Output
        output_edit = QLineEdit(
            "out.graphtec"
        )
        self.inputs["output"] = output_edit
        file_layout.addWidget(QLabel("Output File:"), 1, 0)
        file_layout.addWidget(output_edit, 1, 1)
        btn_output = QPushButton("Browse")
        btn_output.clicked.connect(lambda: self.browse_file("output"))
        file_layout.addWidget(btn_output, 1, 2)

        file_box.setLayout(file_layout)
        left_layout.addWidget(file_box)

        # ---------------------------------------------- Parameter ---
        param_box    = QGroupBox("Parameters")
        param_layout = QGridLayout()
        add_row(param_layout, "Offset:",           "offset",        "1.0,4.5",      0)
        add_row(param_layout, "Border:",           "border",        "0,0",          1)
        add_row(param_layout, "Matrix:",           "matrix",        "1,0,0,1",      2)
        add_row(param_layout, "Speed:",            "speed",         "2,2",          3)
        add_row(param_layout, "Force:",            "force",         "8,30",         4)
        add_row(param_layout, "Merge:",            "merge",         "0",            5)
        add_row(param_layout, "Merge Threshold:",  "merge_thresh",  "0.014,0.009",  6)
        add_row(param_layout, "Cut Mode:",         "cut_mode",      "0",            7)
        param_box.setLayout(param_layout)
        left_layout.addWidget(param_box)

        # ---------------------------------------------- Buttons ----
        button_layout = QHBoxLayout()
        convert_btn   = QPushButton("1. Create Graphtec / Silhouette File")
        convert_btn.clicked.connect(self.main_program)
        send_btn      = QPushButton("2. Send File to Cutter")
        send_btn.clicked.connect(self.send_to_cutter)
        button_layout.addWidget(convert_btn)
        button_layout.addWidget(send_btn)
        left_layout.addLayout(button_layout)

        # Spacer am Ende
        left_layout.addSpacerItem(QSpacerItem(0, 0, QSizePolicy.Minimum, QSizePolicy.Expanding))
        main_layout.addLayout(left_layout)

        # ---------------------------------------------- Vorschau ----
        self.scene = QGraphicsScene()
        self.view  = ZoomView(self.scene)
        self.view.setMinimumSize(800, 600)  # größere Start-Canvas
        main_layout.addWidget(self.view, stretch=1)

        self.setLayout(main_layout)

    # --------------------------------------------------------------
    # Datei-Dialoge
    # --------------------------------------------------------------
    def browse_file(self, key):
        if key == "gerber":
            file, _ = QFileDialog.getOpenFileName(
                self, "Select Gerber File", "", "Gerber Files (*.gbr);;All Files (*)"
            )
        else:
            file, _ = QFileDialog.getSaveFileName(
                self, "Select Output File", "", "Output Files (*.graphtec);;All Files (*)"
            )
        if file:
            self.inputs[key].setText(file)

    # --------------------------------------------------------------
    # Haupt-Workflow
    # --------------------------------------------------------------
    def main_program(self):
        try:
            gerber_path   = Path(self.inputs["gerber"].text())
            output_path   = Path(self.inputs["output"].text())
            offset        = floats(self.inputs["offset"].text())
            border        = floats(self.inputs["border"].text())
            matrix        = floats(self.inputs["matrix"].text())
            speeds        = floats(self.inputs["speed"].text())
            forces        = floats(self.inputs["force"].text())
            merge         = int(float(self.inputs["merge"].text()))
            merge_thresh  = floats(self.inputs["merge_thresh"].text())
            cut_mode      = int(self.inputs["cut_mode"].text())

            if not gerber_path.is_file():
                QMessageBox.critical(self, "Error", "Gerber file not found.")
                return

            strokes = extract_strokes_from_gerber(str(gerber_path))

            # Skalierung inch → mm
            scale    = 25.4
            strokes  = [[(x / scale, y / scale) for x, y in poly] for poly in strokes]

            if merge:
                strokes = mergepads.fix_small_geometry(strokes, merge_thresh[0], merge_thresh[1])

            max_x, max_y = optimize.max_extent(strokes)
            border_path = [
                (-border[0],            -border[1]),
                ( max_x + border[0],    -border[1]),
                ( max_x + border[0],     max_y + border[1]),
                (-border[0],             max_y + border[1]),
            ]

            # Vorschau
            self.update_preview(strokes)

            # Output-Datei schreiben
            with open(output_path, "w") as fout:
                g = graphtec.graphtec(out_file=fout)
                g.start()
                g.set(offset=(offset[0] + border[0] + 0.5, offset[1] + border[1] + 0.5), matrix=matrix)

                def apply_speed_force(s: float, f: float):
                    g.set(speed=s, force=f)

                if cut_mode == 0:
                    lines = optimize.optimize(strokes, border)
                    for s, f in zip(speeds, forces):
                        apply_speed_force(s, f)
                        for x in lines:
                            g.line(*x)
                        if any(border):
                            g.closed_path(border_path)
                else:
                    for s, f in zip(speeds, forces):
                        apply_speed_force(s, f)
                        for s_poly in strokes:
                            g.closed_path(s_poly)
                        if any(border):
                            g.closed_path(border_path)

                g.end()

            QMessageBox.information(self, "Fertig", f"Datei wurde gespeichert:\n{output_path}")

        except Exception:
            tb = traceback.format_exc()
            print(tb, file=sys.stderr)
            QMessageBox.critical(self, "Fehler", tb)

    # --------------------------------------------------------------
    # Senden an den Cutter
    # --------------------------------------------------------------
    def send_to_cutter(self):
        VID, PID = 0x0B4D, 0x1123
        dev = usb.core.find(idVendor=VID, idProduct=PID)
        if dev is None:
            raise ValueError("Plotter not found")
        
        dev.set_configuration()
        cfg = dev.get_active_configuration()
        intf = cfg[(0, 0)]                    # erstes Interface = Printer
        ep_out = usb.util.find_descriptor(
            intf,
            custom_match=lambda e:
                usb.util.endpoint_direction(e.bEndpointAddress) == usb.util.ENDPOINT_OUT
        )
        with open(self.inputs["output"].text(), "rb") as f:
            data = f.read()
        ep_out.write(data, timeout=0)

# ---------------------------------------------------------------------------
# Uncaught-Hook – zeigt Exceptions als Dialog
# ---------------------------------------------------------------------------
class UncaughtHook(QObject):
    exception_caught = pyqtSignal(object, object, object)

    def __init__(self):
        super().__init__()
        sys.excepthook = self.handle_exception
        self.exception_caught.connect(self.show_messagebox)

    def handle_exception(self, exctype, value, exc_traceback):
        if issubclass(exctype, KeyboardInterrupt):
            sys.__excepthook__(exctype, value, exc_traceback)
        else:
            self.exception_caught.emit(exctype, value, exc_traceback)

    def show_messagebox(self, exctype, value, exc_traceback):
        tb = "".join(traceback.format_exception(exctype, value, exc_traceback))
        QMessageBox.critical(None, "Uncaught Exception", tb)

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    app = QApplication(sys.argv)
    UncaughtHook()
    win = G2GGui()
    win.show()
    sys.exit(app.exec_())
