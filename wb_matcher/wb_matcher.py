#!/usr/bin/env python3
"""
White Balance Matcher
=====================
Make a whole folder of photos (e.g. an apartment) share one colour balance.

Workflow
--------
1. "Open folder..." and pick the folder with your photos.
2. Select the photo whose colours you like and press "Use selected as reference".
   Every other photo is analysed and an automatic correction is suggested.
3. Review each photo (Before | After, or Reference | After). Fix it by hand if needed:
     - Match strength   : how much of the automatic correction to apply
     - Temperature      : blue <-> yellow
     - Tint             : green <-> magenta
     - Pick neutral spot: click a wall/ceiling/door that should look like the
                          walls in the reference photo
   Untick "Include in export" for photos you don't want to save.
4. "Save results..." writes the corrected photos to <folder>/corrected.
   Your originals are never touched.

Quality
-------
* Resolution is never changed; EXIF, ICC profile and DPI are kept.
* JPEGs are re-saved with the SAME quantisation tables and chroma subsampling as
  the original, so there is no extra visible compression loss.
* Photos that need no change are copied byte-for-byte.
* Colour is corrected in linear light with a per-channel 8-bit lookup table.
* Optional "PNG (lossless)" output avoids JPEG re-compression completely.

Requirements:  Python 3.9+, Pillow, numpy  (Tkinter ships with Python; on
Debian/Ubuntu install it with:  sudo apt install python3-tk)
    pip install pillow numpy
Run:
    python wb_matcher.py
"""
from __future__ import annotations

import os
import queue
import shutil
import threading
from collections import OrderedDict

try:
    import numpy as np
    from PIL import Image, ImageOps, ImageTk, JpegImagePlugin
except ImportError as exc:  # pragma: no cover
    raise SystemExit(f"Missing dependency ({exc}). Install with:  pip install pillow numpy")

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

# --------------------------------------------------------------------------- #
# Core colour maths (no GUI)
# --------------------------------------------------------------------------- #
EXTS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".webp"}
PREVIEW_MAX = 1000          # long side of the preview shown in the GUI
STAT_MAX = 600              # long side used to analyse an image
K_SLIDER = 0.003            # log-gain per slider unit (+-100 -> about x1.35)
MAX_LOG_GAIN = 0.8          # clamp on automatic corrections (about x2.2)
LUM = np.array([0.2126, 0.7152, 0.0722])

_x = np.arange(256) / 255.0
SRGB_TO_LIN = np.where(_x <= 0.04045, _x / 12.92, ((_x + 0.055) / 1.055) ** 2.4)


def lin_to_srgb(v):
    v = np.clip(v, 0.0, 1.0)
    return np.where(v <= 0.0031308, v * 12.92, 1.055 * np.power(v, 1 / 2.4) - 0.055)


def load_rgb(path, maxside):
    """Open an image for preview/analysis: RGB, EXIF-rotated, shrunk."""
    with Image.open(path) as im:
        im.draft("RGB", (maxside, maxside))          # fast JPEG downscale (no-op otherwise)
        im.load()
        im = ImageOps.exif_transpose(im)
        if im.mode == "RGBA":
            bg = Image.new("RGB", im.size, (255, 255, 255))
            bg.paste(im, mask=im.getchannel("A"))
            im = bg
        else:
            im = im.convert("RGB")
    im.thumbnail((maxside, maxside), Image.LANCZOS)
    return im


def neutral_stat(arr):
    """Linear-light RGB of the 'neutral-ish' part of the picture (walls, ceilings, ...).

    Uses mid-tone, low-saturation, unclipped pixels so coloured furniture does not
    dominate the estimate. Returns a length-3 float array.
    """
    px = arr.reshape(-1, 3)
    if len(px) > 200_000:
        px = px[:: len(px) // 200_000]
    f = px / 255.0
    lum = f @ LUM
    ok = (lum > 0.22) & (lum < 0.92) & (f.max(1) < 0.985)       # mid-tones, not clipped
    if ok.sum() < 200:
        ok = np.ones(len(px), bool)
    lin = np.maximum(SRGB_TO_LIN[px[ok]], 1e-4)
    w = lum[ok]                                                  # brighter surfaces count more
    # Colour of each pixel as (log R/G, log B/G): independent of brightness.
    cr, cb = np.log(lin[:, 0] / lin[:, 1]), np.log(lin[:, 2] / lin[:, 1])
    # The dominant colour (walls/ceiling are usually the biggest uniform area) is the
    # peak of the smoothed 2-D histogram, so coloured furniture/floors don't skew it.
    nb, lim = 64, 1.6
    hist, xe, ye = np.histogram2d(cr, cb, bins=nb, range=[[-lim, lim], [-lim, lim]], weights=w)
    sm = np.zeros_like(hist)
    for dx in range(-2, 3):
        for dy in range(-2, 3):
            sm += np.roll(np.roll(hist, dx, 0), dy, 1)
    ix, iy = np.unravel_index(np.argmax(sm), sm.shape)
    pr, pb = (xe[ix] + xe[ix + 1]) / 2, (ye[iy] + ye[iy + 1]) / 2
    near = np.hypot(cr - pr, cb - pb) < 0.15
    if near.sum() < 30:
        near = np.ones(len(lin), bool)
    return np.maximum(lin[near].mean(0), 1e-4)


def match_log_gains(target_stat, img_stat):
    """Log-gains that move img_stat's colour onto target_stat's colour."""
    g = np.log(np.maximum(target_stat, 1e-4) / np.maximum(img_stat, 1e-4))
    return np.clip(g, -MAX_LOG_GAIN, MAX_LOG_GAIN)


def final_gains(base, strength, temp, tint):
    """Combine automatic base + manual sliders into normalised linear RGB gains."""
    lg = (base * (strength / 100.0)
          + K_SLIDER * temp * np.array([1.0, 0.0, -1.0])
          + K_SLIDER * tint * np.array([0.5, -1.0, 0.5]))
    g = np.exp(lg)
    return g / float((g * LUM).sum())          # keep neutral grey at the same brightness


def build_lut(gains):
    """768-entry table (R, G, B) that applies per-channel gains in linear light."""
    lut = []
    for c in range(3):
        out = lin_to_srgb(SRGB_TO_LIN * gains[c])
        lut += [int(v) for v in np.rint(out * 255.0)]
    return lut


def apply_gains(img, gains):
    lut = build_lut(gains)
    if img.mode == "RGBA":
        r, g, b, a = img.split()
        rgb = Image.merge("RGB", (r, g, b)).point(lut)
        rgb.putalpha(a)
        return rgb
    if img.mode != "RGB":
        img = img.convert("RGB")
    return img.point(lut)


def clipped_percent(arr, gains):
    """% of pixels pushed into clipping by the gains (that were not clipped before)."""
    lin = SRGB_TO_LIN[arr]
    now = (lin * gains > 1.0).any(2)
    before = (lin > 0.999).any(2)
    return 100.0 * float((now & ~before).mean())


def _jpeg_params(src):
    kw = {"optimize": True}
    q = getattr(src, "quantization", None)
    try:
        sub = JpegImagePlugin.get_sampling(src)
    except Exception:
        sub = -1
    if q and sub in (0, 1, 2):
        kw.update(qtables=q, subsampling=sub)       # identical compression settings
    else:
        kw.update(quality=98, subsampling=0)
    if src.info.get("progressive") or src.info.get("progression"):
        kw["progressive"] = True
    return kw


def export_one(src_path, out_dir, gains, as_png=False, used_names=None):
    """Write one corrected image into out_dir. Returns the output path."""
    name = os.path.basename(src_path)
    stem, ext = os.path.splitext(name)
    out_name = stem + ".png" if as_png else name
    if used_names is not None:
        if out_name.lower() in used_names:
            out_name = f"{stem}_{ext.lstrip('.').lower()}" + (".png" if as_png else ext)
        used_names.add(out_name.lower())
    out_path = os.path.join(out_dir, out_name)

    if np.allclose(gains, 1.0, atol=1e-4) and not as_png:
        shutil.copy2(src_path, out_path)            # nothing to change: byte-for-byte copy
        return out_path

    with Image.open(src_path) as src:
        src.load()
        fmt = "PNG" if as_png else (src.format or "PNG")
        if fmt == "MPO":
            fmt = "JPEG"
        out = apply_gains(src, gains)
        kw = {}
        if src.info.get("icc_profile"):
            kw["icc_profile"] = src.info["icc_profile"]
        exif = src.info.get("exif")
        if not exif:
            ex = src.getexif()
            exif = ex.tobytes() if len(ex) else None
        if exif:
            kw["exif"] = exif
        if src.info.get("dpi"):
            kw["dpi"] = src.info["dpi"]
        if fmt == "JPEG":
            kw.update(_jpeg_params(src))
        elif fmt == "PNG":
            kw["compress_level"] = 6
        elif fmt == "TIFF":
            kw["compression"] = "tiff_lzw"
        elif fmt == "WEBP":
            kw.update(lossless=True, quality=100)
        attempts = [kw,
                    {k: v for k, v in kw.items() if k != "exif"},
                    {k: v for k, v in kw.items() if k not in ("exif", "icc_profile", "dpi")}]
        last = None
        for a in attempts:
            try:
                out.save(out_path, format=fmt, **a)
                last = None
                break
            except Exception as e:                  # try again with fewer extras
                last = e
        if last is not None:
            raise last
    try:
        shutil.copystat(src_path, out_path)         # keep file dates
    except OSError:
        pass
    return out_path


# --------------------------------------------------------------------------- #
# GUI
# --------------------------------------------------------------------------- #
VIEWS = ["Before | After", "Reference | After", "After only", "Before only"]
FORMATS = ["Same as original", "PNG (lossless)"]


class Item:
    def __init__(self, path):
        self.path = path
        self.name = os.path.basename(path)
        self.stat = None
        self.thumb = None
        self.error = None
        self.include = True
        self.reset(None)

    def reset(self, ref_stat):
        self.strength = 100
        self.temp = 0
        self.tint = 0
        self.edited = False
        if ref_stat is None or self.stat is None:
            self.base = np.zeros(3)
        else:
            self.base = match_log_gains(ref_stat, self.stat)

    def gains(self):
        return final_gains(self.base, self.strength, self.temp, self.tint)


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("White Balance Matcher")
        self.geometry("1420x860")
        self.minsize(1000, 620)

        self.folder = None
        self.items = []
        self.ref = None
        self.current = None
        self.cache = OrderedDict()
        self.disp_cache = {}
        self.q = queue.Queue()
        self._on_done = None
        self._job = None
        self._guard = False
        self._eyedrop = False
        self._panels = []
        self._photos = []
        self._tree_imgs = []

        self._build_ui()
        self.bind_all("<Prior>", lambda e: self.step(-1))   # Page Up
        self.bind_all("<Next>", lambda e: self.step(1))     # Page Down
        self.set_status("Step 1: open a folder with your photos.")

    # ---------------------------------------------------------------- UI --
    def _build_ui(self):
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("Treeview", rowheight=54)

        self.columnconfigure(1, weight=1)
        self.rowconfigure(1, weight=1)

        top = ttk.Frame(self, padding=6)
        top.grid(row=0, column=0, columnspan=3, sticky="ew")
        ttk.Button(top, text="Open folder…", command=self.open_folder).pack(side="left")
        self.btn_ref = ttk.Button(top, text="★ Use selected as reference",
                                  command=self.set_reference, state="disabled")
        self.btn_ref.pack(side="left", padx=6)
        ttk.Separator(top, orient="vertical").pack(side="left", fill="y", padx=8)
        ttk.Label(top, text="View:").pack(side="left")
        self.view = tk.StringVar(value=VIEWS[0])
        cb = ttk.Combobox(top, textvariable=self.view, values=VIEWS, state="readonly", width=17)
        cb.pack(side="left", padx=4)
        cb.bind("<<ComboboxSelected>>", lambda e: self.render())

        self.btn_save = ttk.Button(top, text="Save results…", command=self.save_all, state="disabled")
        self.btn_save.pack(side="right")
        self.fmt = tk.StringVar(value=FORMATS[0])
        ttk.Combobox(top, textvariable=self.fmt, values=FORMATS, state="readonly",
                     width=16).pack(side="right", padx=6)
        ttk.Label(top, text="Save as:").pack(side="right")

        # image list
        left = ttk.Frame(self)
        left.grid(row=1, column=0, sticky="ns")
        self.tree = ttk.Treeview(left, columns=("status",), selectmode="browse", height=20)
        self.tree.heading("#0", text="Photo")
        self.tree.heading("status", text="Status")
        self.tree.column("#0", width=210, stretch=True)
        self.tree.column("status", width=105, stretch=False)
        sb = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="y", expand=True)
        sb.pack(side="left", fill="y")
        self.tree.tag_configure("ref", foreground="#b8860b")
        self.tree.tag_configure("edited", foreground="#1f5fbf")
        self.tree.tag_configure("off", foreground="#999999")
        self.tree.bind("<<TreeviewSelect>>", self.on_select)

        # preview
        self.canvas = tk.Canvas(self, bg="#202020", highlightthickness=0, cursor="arrow")
        self.canvas.grid(row=1, column=1, sticky="nsew")
        self.canvas.bind("<Configure>", lambda e: self.schedule_render())
        self.canvas.bind("<Button-1>", self.on_canvas_click)

        # controls
        right = ttk.Frame(self, padding=10, width=280)
        right.grid(row=1, column=2, sticky="ns")
        right.grid_propagate(False)
        self.lbl_name = ttk.Label(right, text="—", font=("TkDefaultFont", 10, "bold"), wraplength=255)
        self.lbl_name.pack(anchor="w")
        self.lbl_note = ttk.Label(right, text="", foreground="#b8860b", wraplength=255)
        self.lbl_note.pack(anchor="w", pady=(0, 6))

        self.s_strength = self._slider(right, "Match strength (%)", 0, 100)
        self.s_temp = self._slider(right, "Temperature  (blue ◀ ▶ yellow)", -100, 100)
        self.s_tint = self._slider(right, "Tint  (green ◀ ▶ magenta)", -100, 100)

        self.btn_pick = ttk.Button(right, text="Pick neutral spot…", command=self.toggle_eyedrop)
        self.btn_pick.pack(fill="x", pady=(10, 2))
        ttk.Label(right, text="Click a wall / ceiling / white door in the\npreview that should look like the walls\nin the reference photo.",
                  foreground="#666666", justify="left").pack(anchor="w")
        self.btn_reset = ttk.Button(right, text="Reset this photo", command=self.reset_current)
        self.btn_reset.pack(fill="x", pady=(10, 2))
        self.include_var = tk.BooleanVar(value=True)
        self.chk_include = ttk.Checkbutton(right, text="Include in export", variable=self.include_var,
                                           command=self.on_include)
        self.chk_include.pack(anchor="w", pady=8)

        nav = ttk.Frame(right)
        nav.pack(fill="x", pady=4)
        ttk.Button(nav, text="◀ Prev", command=lambda: self.step(-1)).pack(side="left", expand=True, fill="x")
        ttk.Button(nav, text="Next ▶", command=lambda: self.step(1)).pack(side="left", expand=True, fill="x", padx=(4, 0))
        ttk.Label(right, text="Tip: PageUp / PageDown = previous / next photo",
                  foreground="#666666", wraplength=255).pack(anchor="w", pady=(4, 8))
        self.lbl_info = ttk.Label(right, text="", wraplength=255, justify="left")
        self.lbl_info.pack(anchor="w")

        # status bar
        bar = ttk.Frame(self, padding=(8, 3))
        bar.grid(row=2, column=0, columnspan=3, sticky="ew")
        self.status = tk.StringVar()
        ttk.Label(bar, textvariable=self.status).pack(side="left")
        self.progress = ttk.Progressbar(bar, length=220, mode="determinate")
        self.progress.pack(side="right")

        self._controls = [self.s_strength, self.s_temp, self.s_tint]

    def _slider(self, parent, text, lo, hi):
        ttk.Label(parent, text=text).pack(anchor="w", pady=(6, 0))
        s = tk.Scale(parent, from_=lo, to=hi, orient="horizontal", resolution=1, showvalue=True,
                     command=self.on_slider, highlightthickness=0, length=250)
        s.set(100 if hi == 100 and lo == 0 else 0)
        s.pack(fill="x")
        s.bind("<Double-Button-1>", lambda e, s=s, d=(100 if lo == 0 else 0): self._slider_default(s, d))
        return s

    def _slider_default(self, s, d):
        s.set(d)

    def set_status(self, text):
        self.status.set(text)

    # ------------------------------------------------------ background jobs --
    def start_bg(self, work, text, on_done):
        self.set_status(text)
        self.progress.configure(value=0, maximum=100)
        self.config(cursor="watch")
        self._on_done = on_done

        def runner():
            try:
                res = work(lambda i, n: self.q.put(("progress", i, n)))
                self.q.put(("done", res, None))
            except Exception as e:  # noqa: BLE001
                self.q.put(("error", None, e))

        threading.Thread(target=runner, daemon=True).start()
        self.after(40, self._poll)

    def _poll(self):
        try:
            while True:
                kind, a, b = self.q.get_nowait()
                if kind == "progress":
                    self.progress.configure(maximum=max(b, 1), value=a)
                elif kind == "done":
                    self.config(cursor="")
                    self.progress.configure(value=0)
                    self._on_done(a)
                    return
                else:
                    self.config(cursor="")
                    messagebox.showerror("Error", str(b))
                    return
        except queue.Empty:
            pass
        self.after(40, self._poll)

    # ---------------------------------------------------------- folder load --
    def open_folder(self):
        d = filedialog.askdirectory(title="Choose the folder with your photos")
        if not d:
            return
        paths = sorted(os.path.join(d, f) for f in os.listdir(d)
                       if os.path.splitext(f)[1].lower() in EXTS and os.path.isfile(os.path.join(d, f)))
        if not paths:
            messagebox.showinfo("No photos", "No JPG / PNG / TIFF / BMP / WEBP files found in that folder.")
            return
        self.folder = d
        self.items = [Item(p) for p in paths]
        self.ref = None
        self.current = None
        self.cache.clear()
        self.disp_cache.clear()
        self.btn_ref.configure(state="disabled")
        self.btn_save.configure(state="disabled")
        self.tree.delete(*self.tree.get_children())
        self.start_bg(self._load_work, "Analysing photos…", self._loaded)

    def _load_work(self, progress):
        n = len(self.items)
        for i, it in enumerate(self.items):
            try:
                small = load_rgb(it.path, STAT_MAX)
                it.stat = neutral_stat(np.asarray(small))
                th = small.copy()
                th.thumbnail((64, 48))
                it.thumb = th
            except Exception as e:  # noqa: BLE001
                it.error = str(e)
            progress(i + 1, n)

    def _loaded(self, _):
        bad = [it for it in self.items if it.error]
        self.items = [it for it in self.items if not it.error]
        if bad:
            messagebox.showwarning("Some files skipped",
                                   "Could not read:\n" + "\n".join(f"{b.name}: {b.error}" for b in bad[:10]))
        self._tree_imgs = []
        for i, it in enumerate(self.items):
            ph = ImageTk.PhotoImage(it.thumb)
            self._tree_imgs.append(ph)
            self.tree.insert("", "end", iid=str(i), text="  " + it.name, image=ph, values=("—",))
        if self.items:
            self.btn_ref.configure(state="normal")
            self.tree.selection_set("0")
        self.set_status(f"{len(self.items)} photos loaded. Step 2: select the photo with the colours you "
                        f"like and press “★ Use selected as reference”.")

    # --------------------------------------------------------------- state --
    def get_preview(self, item):
        if item in self.cache:
            self.cache.move_to_end(item)
            return self.cache[item]
        arr = np.asarray(load_rgb(item.path, PREVIEW_MAX))
        self.cache[item] = arr
        while len(self.cache) > 10:
            self.cache.popitem(last=False)
        return arr

    def row_update(self, item):
        iid = str(self.items.index(item))
        if item is self.ref:
            status, tag = "★ Reference", "ref"
        elif not item.include:
            status, tag = "✗ Skipped", "off"
        elif self.ref is None:
            status, tag = "—", ""
        elif item.edited:
            status, tag = "✎ Edited", "edited"
        else:
            status, tag = "✓ Auto", ""
        self.tree.item(iid, values=(status,), tags=(tag,) if tag else ())

    def set_reference(self):
        it = self.current
        if it is None:
            return
        if any(i.edited for i in self.items):
            if not messagebox.askyesno("Change reference",
                                       "Changing the reference resets all manual adjustments. Continue?"):
                return
        self.ref = it
        for i in self.items:
            i.reset(it.stat)
            self.row_update(i)
        self.btn_save.configure(state="normal")
        self.sync_controls()
        self.view.set(VIEWS[1])
        self.render()
        self.set_status("Step 3: review each photo (PageUp/PageDown), fine-tune with the sliders or the "
                        "neutral-spot picker, then press “Save results…”.")

    def on_select(self, _=None):
        sel = self.tree.selection()
        if not sel:
            return
        self.current = self.items[int(sel[0])]
        self.sync_controls()
        self.render()

    def step(self, d):
        if not self.items:
            return
        sel = self.tree.selection()
        i = int(sel[0]) if sel else -1
        j = min(max(i + d, 0), len(self.items) - 1)
        self.tree.selection_set(str(j))
        self.tree.see(str(j))

    def sync_controls(self):
        it = self.current
        if it is None:
            return
        self._guard = True
        self.lbl_name.configure(text=it.name)
        self.s_strength.set(it.strength)
        self.s_temp.set(it.temp)
        self.s_tint.set(it.tint)
        self.include_var.set(it.include)
        locked = it is self.ref or self.ref is None
        st = tk.DISABLED if locked else tk.NORMAL
        for s in self._controls:
            s.configure(state=st)
        self.btn_pick.configure(state="disabled" if locked else "normal")
        self.btn_reset.configure(state="disabled" if locked else "normal")
        self.lbl_note.configure(text="Reference photo – kept unchanged." if it is self.ref else
                                ("Select a reference photo first." if self.ref is None else ""))
        self._guard = False
        self._eyedrop = False
        self.canvas.configure(cursor="arrow")
        self.btn_pick.configure(text="Pick neutral spot…")

    def on_slider(self, _=None):
        if self._guard or self.current is None or self.current is self.ref:
            return
        it = self.current
        it.strength = self.s_strength.get()
        it.temp = self.s_temp.get()
        it.tint = self.s_tint.get()
        it.edited = True
        self.row_update(it)
        self.schedule_render()

    def on_include(self):
        if self.current is not None:
            self.current.include = bool(self.include_var.get())
            self.row_update(self.current)

    def reset_current(self):
        it = self.current
        if it is None or self.ref is None or it is self.ref:
            return
        it.reset(self.ref.stat)
        self.row_update(it)
        self.sync_controls()
        self.render()

    # ----------------------------------------------------------- eyedropper --
    def toggle_eyedrop(self):
        if self.ref is None or self.current is None or self.current is self.ref:
            return
        self._eyedrop = not self._eyedrop
        self.canvas.configure(cursor="crosshair" if self._eyedrop else "arrow")
        self.btn_pick.configure(text="Click on the photo… (cancel)" if self._eyedrop else "Pick neutral spot…")

    def on_canvas_click(self, ev):
        if not self._eyedrop or self.current is None:
            return
        for (x0, y0, x1, y1, kind, w, h) in self._panels:
            if kind in ("before", "after") and x0 <= ev.x < x1 and y0 <= ev.y < y1:
                ax = min(w - 1, int((ev.x - x0) / (x1 - x0) * w))
                ay = min(h - 1, int((ev.y - y0) / (y1 - y0) * h))
                arr = self.get_preview(self.current)
                patch = arr[max(0, ay - 4):ay + 5, max(0, ax - 4):ax + 5].reshape(-1, 3)
                sample = np.maximum(SRGB_TO_LIN[patch].mean(0), 1e-4)
                it = self.current
                it.base = match_log_gains(self.ref.stat, sample)
                it.strength, it.temp, it.tint, it.edited = 100, 0, 0, True
                self.row_update(it)
                self.sync_controls()
                self.render()
                return

    # --------------------------------------------------------------- render --
    def schedule_render(self):
        if self._job is not None:
            self.after_cancel(self._job)
        self._job = self.after(20, self.render)

    def _scaled(self, item, w, h):
        key = (item, w, h)
        if key not in self.disp_cache:
            if len(self.disp_cache) > 8:
                self.disp_cache.clear()
            self.disp_cache[key] = Image.fromarray(self.get_preview(item)).resize((w, h), Image.BILINEAR)
        return self.disp_cache[key]

    def render(self):
        self._job = None
        c = self.canvas
        c.delete("all")
        self._panels, self._photos = [], []
        cw, ch = max(c.winfo_width(), 50), max(c.winfo_height(), 50)
        it = self.current
        if it is None:
            c.create_text(cw // 2, ch // 2, fill="#888", font=("TkDefaultFont", 13),
                          text="Open a folder with your photos to begin")
            return
        ref, view = self.ref, self.view.get()
        if view == "Before | After":
            specs = [("Before", "before", it), ("After", "after", it)]
        elif view == "Reference | After" and ref is not None and ref is not it:
            specs = [("Reference ★", "ref", ref), ("After", "after", it)]
        elif view == "Before only":
            specs = [("Before", "before", it)]
        else:
            specs = [("After" if it is not ref else "Reference (unchanged)", "after", it)]

        n, pad, label_h = len(specs), 10, 26
        slot_w = (cw - pad * (n + 1)) // n
        slot_h = ch - label_h - pad * 2
        gains = it.gains() if ref is not None else np.ones(3)
        info = ""
        try:
            for k, (label, kind, item) in enumerate(specs):
                arr = self.get_preview(item)
                h, w = arr.shape[:2]
                s = min(slot_w / w, slot_h / h)
                nw, nh = max(1, int(w * s)), max(1, int(h * s))
                img = self._scaled(item, nw, nh)
                if kind == "after":
                    info = f"Highlights newly clipped: {clipped_percent(np.asarray(img), gains):.1f} %"
                    img = apply_gains(img, gains)
                x = pad + k * (slot_w + pad) + (slot_w - nw) // 2
                y = label_h + pad + (slot_h - nh) // 2
                ph = ImageTk.PhotoImage(img)
                self._photos.append(ph)
                c.create_image(x, y, anchor="nw", image=ph)
                c.create_text(x + nw // 2, 14, text=label, fill="#dddddd",
                              font=("TkDefaultFont", 10, "bold"))
                self._panels.append((x, y, x + nw, y + nh, kind, w, h))
        except Exception as e:  # noqa: BLE001
            c.create_text(cw // 2, ch // 2, fill="#e88", text=f"Cannot display this image:\n{e}")
        self.lbl_info.configure(text=info)

    # ----------------------------------------------------------------- save --
    def save_all(self):
        if self.ref is None or not self.items:
            return
        todo = [it for it in self.items if it.include]
        if not todo:
            messagebox.showinfo("Nothing to save", "No photos are ticked for export.")
            return
        out_dir = os.path.join(self.folder, "corrected")
        if not messagebox.askokcancel(
                "Save results",
                f"Save {len(todo)} photo(s) to:\n{out_dir}\n\n"
                "Your original files are not modified. Files with the same name already in that "
                "folder will be overwritten."):
            return
        os.makedirs(out_dir, exist_ok=True)
        jobs = [(it.path, it.gains() if it is not self.ref else np.ones(3)) for it in todo]
        as_png = self.fmt.get().startswith("PNG")

        def work(progress):
            used, errors = set(), []
            for i, (path, g) in enumerate(jobs):
                try:
                    export_one(path, out_dir, g, as_png, used)
                except Exception as e:  # noqa: BLE001
                    errors.append(f"{os.path.basename(path)}: {e}")
                progress(i + 1, len(jobs))
            return errors

        def done(errors):
            self.set_status(f"Saved {len(jobs) - len(errors)} photo(s) to {out_dir}")
            if errors:
                messagebox.showwarning("Finished with errors", "\n".join(errors[:15]))
            else:
                messagebox.showinfo("Done", f"Saved {len(jobs)} photo(s) to:\n{out_dir}")

        self.start_bg(work, "Saving…", done)


if __name__ == "__main__":
    App().mainloop()
