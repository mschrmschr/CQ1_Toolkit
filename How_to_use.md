# How to use the CQ1 Toolkit

No install needed, no admin rights needed, no Python needed.

1. Unzip the whole folder anywhere (Desktop, Documents, a network share).
2. Double-click `CQ1_Toolkit_Launcher.exe`.
3. Pick a tool:
   - **Field → Stacks + MIP** — turns CQ1 field images into per-well
     OME-TIFF stacks, then makes MIP overview images.
   - **Stitching + MIP** — stitches CQ1 tiled wholemount acquisitions into
     one image, then makes MIP overview images.

Each tool opens its own window with a **Browse...** button to pick your
dataset folder (the folder containing `MeasurementResult.ome.xml` and an
`Image\` subfolder), a **Wells** checklist that fills in automatically once
you pick a folder, an **Advanced options** toggle for tuning knobs, a **Run**
button, and a log pane. When it finishes, **Open output folder** jumps
straight to the results.

Keep the folder structure intact (launcher exe + `tools\` subfolder next to
each other) — if you move the launcher out on its own, it won't be able to
find the tools.

If Windows SmartScreen or antivirus blocks the exe as "unrecognized
publisher," that's expected for an unsigned internal tool — click
**More info** → **Run anyway**, or ask IT to allow it.

## Which tool do I want?

- Your CQ1 acquisition is a set of separate **fields** (individual
  positions, not stitched into one big image) → use **Field → Stacks +
  MIP**. It turns each field into one proper multi-channel stack.
- Your CQ1 acquisition is a **wholemount / tiled scan** that needs to be
  stitched into one big image → use **Stitching + MIP**. It stitches the
  tiles together first, then does the same MIP step.

Both tools point at the same kind of dataset folder: one that contains
`MeasurementResult.ome.xml` (CQ1's own metadata export) and an `Image\`
subfolder with the raw tile TIFFs.

### Not sure which one your data is? Here's how to tell for certain

The individual tile filenames look identical either way —
`W0001F0001T0001Z001C1.tif` — so eyeballing the `Image\` folder won't tell
you anything. The real answer is in `MeasurementResult.ome.xml`: every
image there is named `W<well>(R<row>C<col>),A<gridIndex>,F<fieldIndex>` —
and it's the relationship between `A` (grid/area index) and `F` (field
index) that tells you which situation you're in:

- **Tiled / needs stitching:** several fields share the *same* `A`. For
  example `A1,F1` / `A1,F2` / `A1,F3` / `A1,F4`, then `A2,F5` / `A2,F6` ...
  — CQ1 recorded those fields as tiles of one grid. Their stage positions
  sit close enough together to overlap (e.g. tiles ~650 µm across but only
  ~580 µm apart, center to center — roughly a 10% overlap), which is what
  lets the pieces be stitched into one continuous image. Use **Stitching +
  MIP**.
- **Standalone fields, nothing to stitch:** `A` and `F` always increment
  together — `A1,F1`, `A2,F2`, `A3,F3` ... — meaning every field got its
  own grid of exactly one. Their stage positions are typically hundreds of
  micrometers apart, far past any field of view, so there's no overlap to
  stitch — they're independent regions of interest scattered across the
  sample. Use **Field → Stacks + MIP**.

This is exactly what `ome_metadata.py`/`ome_xml_assembler.py` compute
internally from the stage positions — you don't need to inspect the XML
yourself day to day, but if a dataset ever behaves unexpectedly in one
tool, checking this pattern in the XML is the fastest way to confirm which
tool it actually needs.

**MIP** = Maximum Intensity Projection: a single flattened image made by
taking the brightest pixel at each position across all your Z-slices — a
quick way to see your whole sample without scrolling through a stack. Both
tools make one of these automatically after the main step.

## Channel names — you must type these in yourself

CQ1 does not give you real channel names (DAPI, GFP, etc.) anywhere — its
own metadata only ever has generic labels like "CH1", "CH2". That's part of
why this toolkit exists. Because of that, both tools **require** you to
type your channel names into the "Channel names" field before you can run —
leaving it blank gives an error rather than silently producing meaningless
output.

Rules that matter:

- Type one name per channel, comma-separated, in the **same order** your
  channels were acquired in (channel 1 first, channel 2 second, ...).
  Example: `DAPI,GFP,RFP,FarRed`
- Get the order wrong and your images will still be produced, but every
  channel will be labeled with the wrong stain — there's no way for the
  tool to catch this for you, since it has no way to know what you actually
  stained with. If you're not sure of the order, check a couple of raw
  images or ask whoever ran the acquisition.
- This name gets written into the output file's own metadata (so it shows
  up correctly if you reopen it in FIJI later), and it's also what appears
  as the label on each panel of the MIP overview image.

## Picking which wells to process

Right after you pick a dataset folder, both tools automatically read its
wells straight from the metadata and show them as a **Wells** checklist,
every box ticked by default — untick any wells you don't want included,
then click Run and only the ticked wells are processed.

If the automatic scan fails for some reason (you'll see a red message there
instead of checkboxes — e.g. the dataset folder turned out not to have the
expected layout), open **Advanced options** and type a well number straight
into **Only well** instead; it always overrides the checklist, so a failed
scan never blocks you from running.

## Different wells stained differently? Run once per staining group

CQ1 plates can have different wells stained with completely different
things — e.g. well W1 is DAPI/GFP/RFP but well W2 is a different panel
entirely. Since "Channel names" is one setting for the whole run, you can't
mix stainings in a single click of Run.

Instead, run the tool once per staining group, using the Wells checklist:

1. In the Wells checklist, untick everything except the well(s) that share
   one staining, set "Channel names" to match that staining, click Run.
2. Untick those, tick the next group's well(s) instead, change "Channel
   names" to match their staining, click Run again — into the **same**
   output folder.

This is safe to repeat: output filenames already include the well number,
so different wells' runs never overwrite each other, and the MIP step is
automatically scoped to just the well(s) you ticked, so an earlier group's
overview image never gets relabeled by a later run's channel names.

> Field → Stacks + MIP's Wells checklist only matters together with "Split
> by well within field" turned on, under Advanced options → Assembly
> options — that's what makes the tool tell wells apart in the first place.
> It's on by default; turned off, only the first well found in each field is
> ever processed, regardless of what's ticked.

There's also an **Only grid index** field for the Stitching tool, under
Advanced options — that's for when a single well itself contains more than
one separate tiled region (e.g. two tissue pieces mounted in the same well,
scanned as two grids). The Wells checklist doesn't cover this axis — a
ticked well's grids are always processed together; leave "Only grid index"
blank to process every grid, or set it to isolate one.

## Where to find your results

**Field → Stacks + MIP** writes into your dataset folder by default:

```
Stacks\        one OME-TIFF per field (or per field+well)
MIP\           MIP overview images (panel PNGs, one per stack)
```

**Stitching + MIP** writes into your dataset folder by default:

```
stitched_output\        one stitched OME-TIFF per (well, grid)
stitched_output\mip\    MIP overview panel PNGs, one per stitched file
```

The stitched/stacked `.ome.tif` files are the real data — open those in FIJI
(File → Import → Bio-Formats) for anything quantitative. The MIP panel PNGs
are a quick-look overview only, for eyeballing that a run looks right, not
for measurements.

## Advanced options worth knowing about

Most Advanced fields are fine left at their defaults. A few are worth
understanding if you're tempted to change them:

| Option | Tool(s) | Default | Notes |
| --- | --- | --- | --- |
| Generate MIP after stitching / assembly | Both | On | Turn off if you only want the full-resolution `.ome.tif` output and don't need the quick-look overview PNG — saves a bit of time. |
| TIFF compression | Stitching | zlib | Both zlib and zstd are lossless — no pixel data changes either way, only file size. Benchmarked on a real 1.62 GB stitched file: zlib ≈ 45% of the uncompressed size, zstd ≈ 40% (smaller, still opens fine in FIJI/Bio-Formats). "none" skips compression entirely — fastest write, largest file; only worth it if disk space genuinely isn't a concern. |
| Compression level | Stitching | 6 (zlib) | Higher = smaller file, slower write. Pushing zlib past its default of 6 usually isn't worth it (level 9 only saved ~1.5% more in testing, for about 6x the write time). zstd's useful range is wider (0–22). |
| Predictor | Stitching | Off | This checkbox has no effect on the output either way, checked or not — testing on real CQ1 fluorescence data showed enabling it makes files ~2.6% **larger**, not smaller (this kind of data is dominated by per-pixel sensor noise, not the smooth gradients this setting is designed to help with), so it's intentionally kept off internally regardless. |
| Fallback overlap fraction | Stitching | 0.01 | Only used on the rare dataset where tile overlap can't be measured from the stage positions themselves — normally the real overlap is measured directly from your data, not estimated from this number. |
| Only grid index / Split by well within field | Stitching / Field → Stacks | — | See "Picking which wells to process" above. |

## Troubleshooting

**"Invalid input" popup right when you click Run** — a field didn't pass a
basic check before anything ran (most commonly: "Channel names are
required," or a dataset folder wasn't chosen). Fix that field and click Run
again — nothing was written yet.

**"Job failed - see the log for details" popup after Run** — something went
wrong partway through. Scroll the log pane for the actual error; common
causes are the dataset folder not having the expected
`MeasurementResult.ome.xml` / `Image\` layout, or the channel count you
typed not matching what's actually in the data. Note that "Open output
folder" becomes clickable once a run finishes either way, success or
failure (the output folder itself gets created up front) — it being enabled
doesn't by itself mean the run produced anything useful, always check the
log pane too.

**"Tool not found" when clicking a button** — someone moved the launcher
exe out of the `CQ1_Toolkit` folder, away from `tools\`. Put it back next to
`tools\`, or re-unzip a fresh copy.

**Window takes 10–20 seconds to open** — normal for these tools; they're
self-contained apps with no separate install step, which trades a slower
first launch for not needing Python or admin rights. Don't double-click the
button again while waiting.
