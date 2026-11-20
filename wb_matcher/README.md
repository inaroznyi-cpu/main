# White Balance Matcher

Desktop tool that gives a whole folder of photos the same colour balance as one reference photo.

## Install & run
```bash
pip install -r requirements.txt     # Pillow + numpy  (Tkinter comes with Python;
                                    #  Debian/Ubuntu: sudo apt install python3-tk)
python wb_matcher.py
```

## Use
1. **Open folder…** – choose the folder with your photos.
2. Select the photo whose colours you like → **★ Use selected as reference**.
   All other photos get an automatic suggested correction.
3. Review with PageUp / PageDown. View modes: *Before | After*, *Reference | After*, …
   Fine-tune a photo with **Match strength**, **Temperature**, **Tint**, or **Pick neutral spot**
   (click a wall that should look like the reference walls). Double-click a slider to reset it.
   Untick **Include in export** to skip a photo.
4. **Save results…** → files go to `<folder>/corrected/`. Originals are never modified.

## Quality
- Resolution unchanged; EXIF, ICC profile, DPI and file dates kept.
- JPEGs are saved with the original's quantisation tables and chroma subsampling (no extra visible loss).
- Unchanged photos (e.g. the reference) are copied byte-for-byte.
- "PNG (lossless)" output avoids JPEG re-compression entirely.
- 8-bit images; assumes sRGB-like colour. The "highlights newly clipped" value warns when a correction pushes bright areas to pure white.
