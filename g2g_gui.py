#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Gerber-to-Graphtec GUI — USB only.

* Unterstützt alle bekannten Silhouette VID/PID-Paare.
* Zeigt beim Start einen **grünen Punkt + Gerätenamen** oder einen **roten Punkt**
  für "not detected".
* "Refresh"-Button aktualisiert die Anzeige manuell.
* Nicht-blockierender USB-Upload (8 KiB-Chunks, timeout = 0) mit Fortschrittsbalken.
"""
from __future__ import annotations

import sys
import traceback
from pathlib import Path
from typing import List, Optional, Tuple

import usb.core
import usb.util
from PyQt5.QtCore import Qt, QObject, QThread, pyqtSignal, QPointF
from PyQt5.QtGui import QPainterPath, QPen, QPainter
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
    QSpacerItem,
    QVBoxLayout,
    QWidget,
    QProgressDialog,
)

import graphtec
import mergepads
import optimize
from gerber_parser import extract_strokes_from_gerber

# -------------------------------- patch deprecated "rU" --------------------
import builtins as _bi
_open_orig = _bi.open
_bi.open = lambda f, m="r", *a, **kw: _open_orig(f, m.replace("U", ""), *a, **kw)

# -------------------------------- device table ------------------------------
_DEVICE_LIST: List[Tuple[str, int, int]] = [
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
_SUPPORTED = [(vid, pid) for _, vid, pid in _DEVICE_LIST]
_NAME = {(vid, pid): name for name, vid, pid in _DEVICE_LIST}

CHUNK = 8192  # USB bulk packet size

# -------------------------------- helpers -----------------------------------

def floats(s: str):
    return list(map(float, s.split(","))) if s.strip() else []


def detect_device() -> Optional[str]:
    """Return product name of first connected supported cutter or *None*."""
    for vid, pid in _SUPPORTED:
        if usb.core.find(idVendor=vid, idProduct=pid):
            return _NAME[(vid, pid)]
    return None

# -------------------------------- view with zoom ----------------------------
class ZoomView(QGraphicsView):
    _STEP, _MIN, _MAX = 1.15, -10, 20
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.setRenderHint(QPainter.Antialiasing)
        self.setTransformationAnchor(self.AnchorUnderMouse)
        self.setResizeAnchor(self.AnchorViewCenter)
        self._z = 0
    def wheelEvent(self, ev):
        dy = ev.angleDelta().y(); d = 1 if dy > 0 else -1
        if dy == 0 or not (self._MIN <= self._z + d <= self._MAX):
            return super().wheelEvent(ev)
        f = self._STEP if d > 0 else 1 / self._STEP; self.scale(f, f); self._z += d

# -------------------------------- usb sender thread -------------------------
class UsbSender(QThread):
    progress = pyqtSignal(int); finished = pyqtSignal(); error = pyqtSignal(str)
    def __init__(self, fn: Path, parent=None):
        super().__init__(parent); self.fn = fn
    @staticmethod
    def _ep_out():
        for vid, pid in _SUPPORTED:
            dev = usb.core.find(idVendor=vid, idProduct=pid)
            if not dev: continue
            try: dev.set_configuration()
            except usb.core.USBError: pass
            ep = usb.util.find_descriptor(dev.get_active_configuration()[(0,0)],
                custom_match=lambda e: usb.util.endpoint_direction(e.bEndpointAddress)==usb.util.ENDPOINT_OUT)
            if ep: return ep
        raise RuntimeError("No supported cutter found")
    def run(self):
        try:
            ep = self._ep_out(); size = max(1, self.fn.stat().st_size); sent = 0
            with self.fn.open("rb") as fh:
                for chunk in iter(lambda: fh.read(CHUNK), b""):
                    ep.write(chunk, timeout=0); sent += len(chunk); self.progress.emit(int(sent/size*100))
            self.finished.emit()
        except Exception as e:
            self.error.emit(str(e))

# -------------------------------- main gui ----------------------------------
class Gui(QWidget):
    def __init__(self):
        super().__init__(); self._strokes = [] ; self._init_ui()
        
    def _init_ui(self):
        self.setWindowTitle("Gerber → Graphtec (USB)"); self.setMinimumWidth(720)
        main=QHBoxLayout(self); left=QVBoxLayout(); main.addLayout(left)
        self.inp:dict[str,QLineEdit]={}
        def row(g,l,k,d="",r=None):
            lab=QLabel(l); lab.setAlignment(Qt.AlignRight|Qt.AlignVCenter)
            ed=QLineEdit(d); ed.setSizePolicy(QSizePolicy.Expanding,QSizePolicy.Preferred)
            self.inp[k]=ed; rr=r if r is not None else g.rowCount(); g.addWidget(lab,rr,0); g.addWidget(ed,rr,1)
        # device
        dbox=QGroupBox("Device"); dh=QHBoxLayout(dbox)
        self.dot=QLabel(); self.dot.setFixedSize(10,10); self.dot.setStyleSheet("border-radius:5px;background:#a00")
        self.dev_lbl=QLabel("not detected"); btn_ref=QPushButton("Refresh",clicked=self._refresh_dev)
        dh.addWidget(self.dot); dh.addWidget(self.dev_lbl); dh.addStretch(); dh.addWidget(btn_ref)
        left.addWidget(dbox)
        # files
        fbox=QGroupBox("Files"); fg=QGridLayout(fbox)
        row(fg,"Gerber File:","gerber","in.gbr",0); row(fg,"Output File:","output","out.graphtec",1)
        fg.addWidget(QPushButton("Browse",clicked=lambda:self._browse(True)),0,2)
        fg.addWidget(QPushButton("Browse",clicked=lambda:self._browse(False)),1,2)
        left.addWidget(fbox)
        # params (short)
        pbox=QGroupBox("Parameters"); pg=QGridLayout(pbox)
        row(pg,"Offset x,y [in]:","offset","1.0,4.5",0); row(pg,"Border x,y [in]:","border","0,0",1)
        row(pg,"Matrix:","matrix","1,0,0,1",2); row(pg,"Speed (1-10):","speed","2,2",3)
        row(pg,"Force (1-33):","force","8,30",4); row(pg,"Merge:","merge","0",5)
        row(pg,"Merge Thres:","merge_thresh","0.014,0.009",6); row(pg,"Cut Mode:","cut_mode","0",7)
        left.addWidget(pbox)
        # actions
        act=QHBoxLayout(); act.addWidget(QPushButton("1. Prepare",clicked=self._prepare)); act.addWidget(QPushButton("2. Cut",clicked=self._cut))
        left.addLayout(act); left.addSpacerItem(QSpacerItem(0,0,QSizePolicy.Minimum,QSizePolicy.Expanding))
        # preview
        self.scene=QGraphicsScene(); self.view=ZoomView(self.scene); self.view.setMinimumSize(800,600); main.addWidget(self.view)
        self._refresh_dev()

    # ---------------- device refresh -----------------
    def _refresh_dev(self):
        name = detect_device()
        if name:
            self.dot.setStyleSheet("border-radius:5px;background:#0a0"); self.dev_lbl.setText(f"{name}")
        else:
            self.dot.setStyleSheet("border-radius:5px;background:#a00"); self.dev_lbl.setText("not detected")

    # ---------------- helper dialogs -----------------
    def _browse(self, opn: bool):
        path,_ = (QFileDialog.getOpenFileName if opn else QFileDialog.getSaveFileName)(self,"Select file","","All Files (*)")
        if path: self.inp["gerber" if opn else "output"].setText(path)

    def _show_preview(self):
        self.scene.clear(); pen = QPen(Qt.black); pen.setWidthF(0.001)
        for poly in self._strokes:
            if not poly: continue
            p=QPainterPath(QPointF(*poly[0])); [p.lineTo(QPointF(x,y)) for x,y in poly[1:]]
            item=QGraphicsPathItem(p); item.setPen(pen); self.scene.addItem(item)
        self.scene.setSceneRect(self.scene.itemsBoundingRect()); self.view.fitInView(self.scene.sceneRect(),Qt.KeepAspectRatio)

    # ---------------- file generation ----------------
    def _prepare(self):
        try:
            gbr = Path(self.inp["gerber"].text()); out = Path(self.inp["output"].text())
            if not gbr.is_file(): raise RuntimeError("Gerber not found")
            off=floats(self.inp["offset"].text()) or [0,0]; br=floats(self.inp["border"].text()) or [0,0]
            mat=floats(self.inp["matrix"].text()) or [1,0,0,1]; sp=floats(self.inp["speed"].text()) or [2,2]
            fo=floats(self.inp["force"].text()) or [8,30]; mg=bool(int(float(self.inp["merge"].text() or 0)))
            mth=floats(self.inp["merge_thresh"].text()) or [0.014,0.009]; cm=int(self.inp["cut_mode"].text() or 0)
            strokes=extract_strokes_from_gerber(str(gbr)); strokes=[[(x/25.4,y/25.4) for x,y in p] for p in strokes]
            if mg: strokes=mergepads.fix_small_geometry(strokes,*mth)
            self._strokes=strokes; self._show_preview()
            max_x,max_y=optimize.max_extent(strokes); bpath=[(-br[0],-br[1]),(max_x+br[0],-br[1]),(max_x+br[0],max_y+br[1]),(-br[0],max_y+br[1])]
            with out.open("w") as fout:
                g=graphtec.graphtec(out_file=fout); g.start(); g.set(offset=(off[0]+br[0]+0.5,off[1]+br[1]+0.5),matrix=mat)
                def apply(s,f): g.set(speed=s,force=f)
                if cm==0:
                    lines=optimize.optimize(strokes,br)
                    for s,f in zip(sp,fo): apply(s,f); [g.line(x1,y1,x2,y2) for x1,y1,x2,y2 in lines];
                    if any(br): g.closed_path(bpath)
                else:
                    for s,f in zip(sp,fo): apply(s,f); [g.closed_path(poly) for poly in strokes];
                    if any(br): g.closed_path(bpath)
                g.end()
            QMessageBox.information(self,"Done",f"File saved:\n{out}")
        except Exception:
            QMessageBox.critical(self,"Error",traceback.format_exc())

    # ---------------- usb send -----------------------
    def _cut(self):
        out=Path(self.inp["output"].text())
        if not out.exists(): QMessageBox.warning(self,"Missing","Prepare file first"); return
        dlg=QProgressDialog("Cutting …","Cancel",0,100,self); dlg.setWindowModality(Qt.WindowModal)
        sender=UsbSender(out,self); sender.progress.connect(dlg.setValue)
        sender.finished.connect(lambda: (dlg.close(),QMessageBox.information(self,"Done","Job finished")))
        sender.error.connect(lambda m: (dlg.close(),QMessageBox.critical(self,"Error",m)))
        dlg.canceled.connect(sender.terminate); sender.start(); dlg.show()

# -------------------------------- exception hook ----------------------------
class Hook(QObject):
    exc = pyqtSignal(object,object,object)
    def __init__(self): super().__init__(); sys.excepthook=self._h; self.exc.connect(self._s)
    def _h(self,e,v,t): self.exc.emit(e,v,t)
    def _s(self,e,v,t): QMessageBox.critical(None,"Uncaught","".join(traceback.format_exception(e,v,t)))

# -------------------------------- main --------------------------------------
if __name__ == "__main__":
    app = QApplication(sys.argv); Hook(); w = Gui(); w.show(); sys.exit(app.exec())