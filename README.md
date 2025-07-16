# Gerber‑to‑Graphtec (USB) – Cut Your Own Solder Paste Stencils

**Fork + modern GUI rewrite** of the classic *gerber2graphtec* toolchain for producing **accurate SMT solder‑paste stencils** on low‑cost Graphtec‑engine craft cutters (Silhouette Cameo / Portrait family).

This fork replaces the original Gerber→(gerbv→pstoedit→pic) conversion chain with a **direct Python workflow**:

* Parse Gerber using **[pcb-tools](https://github.com/curtacircuitos/pcb-tools)**.
* Convert Gerber primitives (lines, arcs, flashes, polygons) into optimized cutter strokes.
* Generate native Graphtec/Silhouette job commands (existing `graphtec` backend).
* Stream the job **directly over USB** using **[pyusb](https://github.com/pyusb/pyusb)** – no CUPS, no `/dev/usb/lp0`, no driver install required on macOS.
* Query cutter status (ready / moving / unloaded / paused) using control bytes inspired by **[py_silhouette](https://github.com/mossblaser/py_silhouette)**.

⚙️ **Primary purpose:** Quickly turn your PCB solder‑paste Gerber layer into a **mylar / film stencil** suitable for fine‑pitch hand soldering & reflow. Field‑tested on thin transparency film ~3–5 mil; usable down to ~0.5 mm pitch QFP/QFN & 0201 passives (with tuning).

---

## Screenshot

> *Replace the image path below with your actual screenshot before publishing.*

![Gerber‑to‑Graphtec GUI screenshot](Screenshot.png)

---

## Why this fork?

The original `gerber2graphtec` command‑line tool remains a clever, proven path for producing high‑quality stencils on hobby cutters. Over time, however:

* Legacy utilities (gerbv, pstoedit, Ghostscript, pic) became cumbersome to install on macOS and Windows.
* USB device nodes (e.g., `/dev/usb/lp0`) aren’t universally available.
* Users increasingly expect an interactive GUI workflow.

This fork modernizes the pipeline while preserving the original project’s proven cutting strategies (short segments, anti‑backlash planning, multi‑pass force profiles, etc.).

---

## Key Features (Fork Enhancements)

### Modern Gerber Ingest
- Uses **pcb-tools** to read RS‑274X Gerber directly – no gerbv/pstoedit chain.
- Handles common primitives: lines, rectangles, circles, obrounds, polygons, arcs (with angle normalization & segmentation).
- Optional geometry cleanup: **merge small shapes** below user thresholds (size, spacing).

### USB‑Native Cutter Control
- Auto‑detects connected Silhouette devices by VID/PID (Portrait / Cameo families).
- Direct **bulk USB streaming** (chunked packets; configurable size).
- Device **status polling** (Ready, Moving, No Media, Paused, Unknown) using commands derived from *py_silhouette*.
- Color status indicator (red = none, yellow = not ready, green = ready, blue = cutting).

### Interactive PyQt GUI
- **Prepare → Cut** 2‑step workflow.
- Live preview canvas (zoom wheel; anti‑aliased view).
- Centered "Preview" placeholder when no data loaded.
- **Multi‑Pass control**: choose 1–3 passes; per‑pass speed & force kept in sync.
- **Enhanced vs Standard** cutting modes:
  - *Enhanced*: line‑optimized strokes (fast, minimal lift; good for stencils).
  - *Standard*: closed polygon cutting (original outline fidelity).
- Offset & Margin (inches) to position stencil on sheet.
- Transform matrix (advanced affine tweak).
- Merge tolerance (min_size, min_dist in inches) for cleaning pad clusters.
- Cancelable cut job with safe interruption and USB release.
- Automatic settings persistence via **QSettings** (saved on Prepare).

### Reliability / UX
- Job streaming occurs in a background thread; GUI remains responsive.
- After cancel or job completion, USB interface is cleanly released.
- Device state auto‑polls every second when idle; UI updates background color & text.
- Pre‑flight readiness check warns if cutter has no media loaded.

---

## Quick Start

### 1. Install (recommended: virtualenv)

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install --upgrade pip
pip install pcb-tools pyusb PyQt5
```

> The repo includes small helper modules (`graphtec`, `optimize`, `mergepads`, `gerber_parser`) which are imported locally; no extra install step needed if you run from the source tree.

### 2. Run the GUI

```bash
python g2g_gui.py
```

### 3. Prepare a Cut Job
1. Load your solder‑paste **Gerber** (*.gbr*).
2. Choose an **output job file** (*.graphtec*).
3. Adjust Offset / Margin as needed to locate your stencil on film.
4. Select number of **Passes** (1–3) and set per‑pass Speed (1–10) & Force (1–33).
5. (Optional) Enable **Merge small shapes** and set tolerance.
6. Pick **Mode** → *Enhanced* (optimized) or *Standard* (polygons).
7. Click **1. Prepare**. This parses the file, generates the job, shows a preview, and saves settings.

### 4. Cut
1. Load film / mylar in cutter.
2. Confirm the cutter shows *Ready* (green).
3. Click **2. Cut**.
4. Watch progress; cancel if needed.

---

## Parameter Reference

| Parameter | Units | GUI Field | Description |
|---|---|---|---|
| Offset | inches | `Offset (in)` | X,Y shift applied before output. Use to locate stencil on loaded film. |
| Margin | inches | `Margin (in)` | Extra border added around design extents; cutter can frame the area. |
| Transform | raw coeffs | `Transform` | Affine matrix `[a,b,c,d]` (scales/shears). Advanced calibration. |
| Passes | count | `Passes` | 1–3 multi‑pass cuts. Additional passes can increase cut quality in thicker film. |
| Speed | steps | `Speed` columns | 1 = slowest, 10 = fastest. Lower speed improves detail in thin film. |
| Force | steps | `Force` columns | 1 = light kiss cut, 33 = heavy. Dial in for your film & blade. |
| Merge tol. | inches | `Merge tol.` | Two comma values: `min_size,min_dist`. Features smaller than `min_size` or closer than `min_dist` may be merged/simplified. Uses `mergepads.fix_small_geometry()`. |
| Mode | select | `Mode` | **Enhanced:** optimized line sequencing (fast); **Standard:** closed polygons. |

---

## Supported Devices (auto‑detected by VID/PID)

| Model | VID | PID |
|---|---|---|
| Silhouette Portrait | 0x0B4D | 0x1123 |
| Silhouette Portrait 2 | 0x0B4D | 0x1132 |
| Silhouette Portrait 3 | 0x0B4D | 0x113A |
| Silhouette Cameo | 0x0B4D | 0x1121 |
| Silhouette Cameo 2 | 0x0B4D | 0x112B |
| Silhouette Cameo 3 | 0x0B4D | 0x112F |
| Silhouette Cameo 4 | 0x0B4D | 0x1137 |
| Silhouette Cameo 4 Plus | 0x0B4D | 0x1138 |
| Silhouette Cameo 4 Pro | 0x0B4D | 0x1139 |

> Detection is first‑match. If multiple supported cutters are connected, the first one returned by USB enumeration will be used.

---

## Materials & Cut Tips

* Use **mylar / polyester film ~3–5 mil** thick (common laser transparency stock works well).
* Many users **shrink paste apertures ~2 mils** in CAM before export; hobby blades can flare cuts slightly.
* Start conservative: Speed ~2, Force ~5 first pass; add deeper force on 2nd/3rd passes.
* Inspect with backlight; fully weeded apertures should be clean rectangles.

(These guidelines build on real‑world results from the original project; see links below.)

---

## Command‑Line Legacy (Original Project)

The classic CLI version piped Gerber→Graphtec commands straight to a USB printer node:

```bash
# basic usage
gerber2graphtec paste.gbr > /dev/usb/lp0

# with calibration & multi‑pass
gerber2graphtec \
  --offset 3,4 \
  --matrix 1.001,0,-0.0005,0.9985 \
  --speed 2,1 \
  --force 5,25 \
  paste.gbr > /dev/usb/lp0
```

That still works if you build and run the original CLI. This fork’s GUI wraps and extends the same underlying concepts.

---

## Upstream Usage Notes (Historical)

The original README recommended:

* Shrinking paste features by ~2 mils pre‑Gerber.
* Cutting thin mylar / transparency stock (3–5 mil).
* Experimenting with speeds / forces for best quality.
* Using helper script **file2graphtec** on macOS / Windows when `/dev/usb/lp0` not available.
* Installing via macports: `gerbv`, `pstoedit`, `libusb`, etc. (replaced in this fork; retained here for legacy reference.)
* Watching out for old gerbv (<2.6.0) aperture omission bugs.

Those notes are preserved for historical context; see *Links* below for deep dive discussions.

---

## Original Resources & Further Reading

These pages document the techniques, calibration ideas, and background that inspired this tool and its fork. (All links from the original project are retained.)

- http://pmonta.com/blog/2012/12/25/smt-stencil-cutting/
- http://dangerousprototypes.com/forum/viewtopic.php?f=68&t=5341
- http://hackeda.com/blog/start-printing-pcb-stencils-for-about-200/
- http://hackaday.com/2012/12/27/diy-smd-stencils-made-with-a-craft-cutter/

### GUI origin
An early optional GUI was contributed by **jesuscf** in the Dangerous Prototypes thread. This fork’s PyQt GUI is a modern re‑implementation.

### Protocol Documentation Credits (Original Project)
Thanks to the authors of **robocut** and **graphtecprint** for Graphtec protocol information:

- http://gitorious.org/robocut
- http://vidar.botfu.org/graphtecprint
- https://github.com/jnweiger/graphtecprint

Additional inspiration: **Cathy Sexton** – http://www.idleloop.com/robotics/cutter/index.php

---

## Additional Open-Source Projects Referenced in This Fork

### pcb-tools
Used for native Gerber parsing (layers, primitives, flashes, apertures). We walk the PCB layer primitives and emit cutter strokes directly, bypassing gerbv/pstoedit.

> GitHub: https://github.com/curtacircuitos/pcb-tools

### py_silhouette
Used as a reference for device VID/PID tables and low‑level USB control codes, especially querying device state (`ESC 0x05`) and general endpoint behavior.

> GitHub: https://github.com/mossblaser/py_silhouette

---

## Development Notes

* Tested on **macOS** and **Linux**; Windows should work if PyUSB can claim the interface.
* Python **3.9+** recommended (developed & tested on newer versions; PyQt5 compatible).
* USB access may require udev rules (Linux) or administrator approval (macOS first connect).
* If you see *permission denied* errors, try running once with `sudo` or update udev rules for your VID/PID.
* Adjust `CHUNK` in `g2g_gui.py` to tune streaming granularity vs overhead. Smaller chunks = more progress updates, slightly more USB calls.

---

## License

This fork retains the **original project’s license** (see `LICENSE` in this repository). Modifications © their respective contributors.

If the upstream license file is missing in your fork, please copy it in full from the original repository before release.

---

## Contributing

Pull requests welcome! Ideas:
- Native Windows device claim helpers.
- Batch / CLI wrapper around the new Python pipeline.
- Optional auto material length detection.
- SVG overlay preview or aperture tagging.

Open an issue if you hit trouble cutting ultra‑fine stencil apertures; include Gerber + your material, pass settings, and cutter model.

---

## Acknowledgements

Huge thanks to the original *gerber2graphtec* author and community, including contributors in the Dangerous Prototypes forums, **jesuscf** (first GUI), the maintainers of **robocut**, **graphtecprint**, **pcb-tools**, and **py_silhouette**, and everyone who shared calibration tips for cutting reliable SMT stencils on hobby hardware.

Happy cutting & good solder joints! 🔧🧪🧲

