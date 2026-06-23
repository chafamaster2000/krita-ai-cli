# Krita MCP — talk to your AI assistant, it drives Krita for you

> A fork of [nanayax3/krita-mcp](https://github.com/nanayax3/krita-mcp) that adds a
> live bridge to [Acly/krita-ai-diffusion](https://github.com/Acly/krita-ai-diffusion).
> You describe what you want in plain language; an AI assistant (Claude, or any
> MCP client) sets the prompt, switches style, paints rough region masks,
> generates, shows you the result, and applies it — **all inside your real,
> open Krita document.** Your layers, your canvas, your undo history.

## What this is, for an artist

If you already use **Krita + AI Diffusion**, you know the loop: type a prompt,
pick a style, maybe scribble a region, hit *Generate*, look at the result, apply
or retry. This project lets you hand that loop to an AI assistant and steer it in
conversation instead of clicking the panel yourself.

You say:

> *"Snowy mountain at golden hour. Put a grizzly bear on the left and a wolf on
> the right, keep them well separated. Use a realistic SDXL style."*

…and the assistant does the mechanical work for you: it checks which model your
style runs on, **writes the prompt in the language that model actually wants**
(plain sentences for realistic/Flux models, danbooru tags for anime models),
creates two regions, paints a rough silhouette for each, generates, and shows you
the image right in the chat so you can say *"the bear's too small, redo"*.

**Nothing is faked or done in some external app.** The assistant is operating the
same AI Diffusion plugin you already have, on the document you have open. When
it's done, you're left with a normal Krita file — layers, regions, generated
result — that you keep editing by hand.

### Why this is useful

- **Stay in Krita.** No new canvas, no separate web UI. Real layers, real undo.
- **Describe instead of configure.** "Make it warmer and add fog" beats hunting
  for the right slider. The assistant translates intent into prompt + params.
- **Right prompt language, automatically.** The single biggest quality killer in
  AI Diffusion is feeding tag-style prompts to a sentence model (or vice-versa).
  The assistant detects the model family from your active style and formats
  accordingly — see [Prompt formatting](#prompt-formatting-the-secret-sauce).
- **Rough composition by silhouette.** Want the bear *here* and the wolf *there*?
  The assistant paints a quick filled shape on a region's mask layer to say
  "this prompt applies to this area."
- **Review in the loop.** It can show you a downscaled preview while iterating and
  a full-resolution one for the final look, without you exporting anything.

> **What it is not:** it's not a different image generator, and it doesn't replace
> your hands. It's a remote control for the AI Diffusion panel you already use.

---

## How it works (the mental model)

There are three pieces in a chain. The key trick is in the last hop.

```
You (in a chat)
   │  "put a bear on the left…"
   ▼
AI assistant  ──MCP──►  MCP Server (server.py)  ──HTTP :5678──►  Krita plugin (kritamcp)
   (Claude, etc.)        translates a tool call          runs the command on Krita's
                         into one HTTP request            main UI thread
                                                                  │
                                                                  │  in-process import
                                                                  ▼
                                                          AI Diffusion plugin
                                                          (the panel you already use)
                                                                  │
                                                                  ▼
                                                             ComfyUI backend
                                                          (does the actual diffusion)
```

1. **The AI assistant** speaks **MCP** (Model Context Protocol) — a standard way
   for an assistant to call "tools." Each tool here is one Krita action:
   `krita_new_canvas`, `krita_ai_generate`, etc.

2. **The MCP Server** (`server.py`) is a small Python program. It advertises those
   tools to the assistant, validates the arguments, and turns each tool call into
   a single HTTP request to `localhost:5678`. It runs on your machine, alongside
   the assistant.

3. **The Krita plugin** (`krita-plugin/kritamcp/`) runs *inside* Krita. It listens
   on `localhost:5678`, and when a command arrives it executes it **on Krita's
   main UI thread** — the same thread Krita uses when *you* click things, which is
   the only thread that's safe to mutate the document from.

### The trick: no fork of AI Diffusion needed

Krita runs every Python plugin in **one shared interpreter**. So once both the
`kritamcp` plugin and the `ai_diffusion` plugin are loaded, `kritamcp` can simply:

```python
from ai_diffusion.model.root import root
model = root.model_for_active_document()
model.generate()
```

That `model` object is the **exact same one** the AI Diffusion panel uses. The
bridge isn't re-implementing generation or talking to ComfyUI directly — it's
reaching over and pressing AI Diffusion's own buttons from code. That's why a
generation triggered by the assistant shows up in your AI Diffusion history just
like one you started yourself, and why **no patched/forked AI Diffusion is
required.**

> **Trade-off (honest version):** the bridge imports a few internals of
> AI Diffusion (`model.root`, `model.regions`, `model.layers`). If a future
> AI Diffusion release renames those, the bridge breaks until it's updated. All
> such imports are localized to `kritamcp/__init__.py`, so the surface to fix is
> small, and the code degrades gracefully (e.g. status still works if the model
> can't be resolved).

### Why a separate plugin instead of a patch

- AI Diffusion updates don't require re-applying patches.
- You can install/remove the bridge without touching your AI Diffusion install.
- A bug in the bridge can never break normal AI Diffusion panel use.

---

## Setup

### 1. Install Krita AI Diffusion (required for the `krita_ai_*` tools)

Follow the official guide: <https://github.com/Acly/krita-ai-diffusion>. The
plugin must be installed and its ComfyUI backend reachable. Without it, the
painting tools still work, but every `krita_ai_*` tool returns an error.

### 2. Install this bridge plugin into Krita

Copy these into your Krita `pykrita` folder:

- `krita-plugin/kritamcp/` (the whole folder, with `__init__.py`)
- `krita-plugin/kritamcp.desktop`

| OS | `pykrita` path |
|----|----------------|
| Windows | `%APPDATA%\krita\pykrita\` |
| Linux | `~/.local/share/krita/pykrita/` |
| macOS | `~/Library/Application Support/krita/pykrita/` |

Then in Krita: **Settings → Configure Krita → Python Plugin Manager → enable
"Krita MCP Bridge"**, and restart Krita. On launch the plugin starts an HTTP
server on port `5678`; if that port is taken it pops up a warning instead of
failing silently.

### 3. Install the MCP server (on your machine)

```bash
pip install fastmcp httpx
```

Or with a virtual environment:

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 4. Point your MCP client at the server

For example, Claude Desktop's `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "krita": {
      "command": "python",
      "args": ["/path/to/server.py"]
    }
  }
}
```

If you used a virtualenv, set `command` to that venv's Python so `fastmcp` and
`httpx` are importable.

### 5. (Optional) Prompt-formatting skills + reminder hook

The prompt-formatting brain (see [below](#prompt-formatting-the-secret-sauce))
is shipped in this repo under [`claude/`](claude/) as **Claude Code skills + a
reminder hook** — the source of truth, kept editable on purpose because the right
model→convention knowledge changes as new model families ship. To use it, copy
the contents into your own Claude Code config (`~/.claude/` on macOS/Linux,
`%USERPROFILE%\.claude\` on Windows):

```bash
cp -r claude/skills/* ~/.claude/skills/
cp claude/hooks/krita-prompt-reminder.py ~/.claude/hooks/
```

Three skills carry the knowledge (`krita-ai-prompt-format`,
`image-prompt-unknown-entities`, `image-prompt-sanity-check`), and a
reminder-only `PreToolUse` hook injects the active model's family before
`set_prompt`/`generate`. Wire the hook in your `settings.json`:

```json
"PreToolUse": [
  {
    "matcher": "mcp__krita__krita_ai_set_prompt|mcp__krita__krita_ai_generate",
    "hooks": [
      { "type": "command", "command": "python3 \"$HOME/.claude/hooks/krita-prompt-reminder.py\"", "timeout": 8 }
    ]
  }
]
```

On Windows use `python` instead of `python3`. The hook only *reminds* — it never
blocks a call. Restart Claude Code (or run `/hooks`) to load it.

---

## The tools

All tools run against the document currently open in Krita.

### Painting & canvas

| Tool | What it does |
|------|--------------|
| `krita_health` | Is Krita up with the plugin active? |
| `krita_new_canvas` | New canvas (width, height, name, background) — fills a visible paint layer and makes it active |
| `krita_open_file` | Open an existing `.kra`, `.png`, `.jpg`, … |
| `krita_set_color` | Set foreground paint color (hex) |
| `krita_set_brush` | Pick a brush preset (partial name match), size, opacity |
| `krita_stroke` | Paint an antialiased stroke through `[[x,y], …]` points, with optional `feather` |
| `krita_fill` | Antialiased filled circle at a point (great for mask blobs) |
| `krita_draw_shape` | Rectangle / ellipse / line, fill or outline, optional `feather` |
| `krita_clear` | Flood the active layer with one solid color |
| `krita_get_color_at` | Eyedropper — sample the pixel at (x, y) |
| `krita_list_brushes` | List brush presets (filter, limit) |
| `krita_get_canvas` | **Look at the canvas inline.** `mode="fast"` (default) = small JPEG for the working loop; `mode="full"` = full-res PNG for final review |
| `krita_save` | Export the document to a file path |
| `krita_undo` / `krita_redo` | Step the undo history |
| `krita_batch` | **Run many commands in ONE round-trip** + optionally return the canvas. Use whenever you'd otherwise chain calls (e.g. several fills to build a mask) |

### AI Diffusion bridge

| Tool | What it does |
|------|--------------|
| `krita_ai_overview` | **The whole AI state in one call:** connection, workspace, style, strength/seed, queue, all regions, all control layers, recent jobs, available styles. Prefer this over chaining the read-only tools below |
| `krita_ai_status` | Lighter snapshot: connection, document, workspace, style, strength/seed, queue counts, current prompt, and the resolved **model architecture** |
| `krita_ai_list_styles` | List AI Diffusion style presets (filter, limit) |
| `krita_ai_set_params` | Set style, strength, seed, fixed_seed, batch_count |
| `krita_ai_set_prompt` | Set positive (on active region or root) and/or negative (always root) prompt |
| `krita_ai_set_workspace` | Switch workspace: `generation` / `upscaling` / `live` / `animation` / `custom` |
| `krita_ai_generate` | Trigger generation in the current workspace (returns immediately) |
| `krita_ai_list_jobs` | List jobs newest-first (id, state, prompt, result count) |
| `krita_ai_apply` | Apply a finished job's result to the canvas (no `job_id` → latest) |
| `krita_ai_save_preview` | Save a result to disk **without** applying it, for review |
| `krita_ai_cancel` | Cancel the active job and/or all queued jobs |
| **Regions** | |
| `krita_ai_create_region` | Make a region + a **fresh transparent paint layer**, set its prompt, activate the layer so painting tools draw its mask |
| `krita_ai_list_regions` | Root prompt + every child region with layer ids, prompts, active flag |
| `krita_ai_select_region` | Select a region by index or layer_id (no args = root) |
| `krita_ai_remove_region` | Remove a region by index |
| **Control Layers** (ControlNet / IP-Adapter) | |
| `krita_ai_add_control` | Add a control to root or a region: `scribble`, `line_art`, `canny_edge`, `depth`, `pose`, `segmentation`, … (ControlNet) or `reference`, `style`, `composition`, `face` (IP-Adapter) |
| `krita_ai_list_controls` | List control layers across root + all regions (or one scope) |
| `krita_ai_set_control` | Change an existing control's mode / strength |
| `krita_ai_remove_control` | Remove a control by index |

---

## Workflows

### A — One image from a prompt

```text
1. krita_new_canvas(1024, 1024, background="#ffffff")
2. krita_ai_set_params(style="cinematic-photo-xl")
3. krita_ai_set_prompt(positive="a lighthouse on a cliff at dusk, stormy sea")
4. krita_ai_generate()
5. krita_ai_list_jobs()            # poll until state == "finished"
6. krita_get_canvas(mode="full")   # or save_preview to review off-canvas
7. krita_ai_apply()                # apply the latest finished result
```

### B — Two subjects, placed by region

Regions let you say "*this* prompt belongs in *this* area." You paint a rough
silhouette on each region's mask layer.

```text
1. krita_new_canvas(1024, 1024, background="#ffffff")
2. krita_ai_set_params(style="juggernaut-xl")
3. krita_ai_set_prompt(positive="snowy mountain valley, golden hour")  # root = scene
4. krita_ai_create_region(positive="grizzly bear standing on snow")
   → activates a fresh transparent layer for its mask
5. krita_set_color("#6b4226")
6. krita_draw_shape(shape="ellipse", x=70, y=520, width=420, height=320)
   … (or use krita_batch to paint the whole silhouette in one round-trip)
7. krita_ai_create_region(positive="timber wolf, alert, side profile")
8. … paint the wolf silhouette on the new layer
9. krita_ai_select_region()        # deselect → root active
10. krita_ai_generate()
11. krita_ai_list_jobs()           # poll until finished
12. krita_ai_apply()
```

### C — Restyle an existing image (img2img / refine)

Set `strength` below `1.0` and AI Diffusion switches to **refine**: your current
canvas becomes the img2img source instead of starting from noise.

```text
1. krita_open_file("/path/to/photo.png")
2. krita_ai_set_params(style="digital-artwork-xl", strength=0.6)
3. krita_ai_set_prompt(positive="oil painting, thick impasto brushwork")
4. krita_ai_generate()  →  list_jobs  →  apply
```

### D — Force structure with a Control Layer

Regions constrain *where a prompt applies*; they don't force a subject to appear
in a precise shape. For strict pose/outline/composition, add a Control Layer that
reads from a layer you've drawn on:

```text
1. … draw a scribble / line-art / pose on a layer
2. krita_ai_add_control(mode="scribble", strength=0.7)   # sources the active layer
3. krita_ai_set_prompt(positive="a knight in plate armor")
4. krita_ai_generate()
```

---

## Prompt formatting (the secret sauce)

**Every diffusion model family wants a different prompt *language*.** Feeding
danbooru tags to a natural-language model — or full sentences to a booru anime
model — visibly degrades the result. The rule: **before every
`krita_ai_set_prompt`, check the active style's model and write in its dialect.**

| Family (by resolved architecture) | Prompt convention | Negative prompt |
|---|---|---|
| Flux, Flux Kontext, Z-Image, Qwen, SD3, realistic SDXL (Cinematic Photo / Digital Artwork) | **Natural language** — descriptive sentences | Flux / Z-Image Turbo ignore it (leave empty); others optional |
| Pony, Illustrious, NoobAI, Animagine, most anime SDXL / SD1.5 | **Danbooru tags** — comma-separated keywords (Pony also wants `score_9, score_8_up, score_7_up`) | Quality tags: `worst quality, low quality, …` |

Two more rules the assistant follows:

- **Classify by architecture, not by the style's name.** A style called
  "Realistic" can run on an anime checkpoint. `krita_ai_status` /
  `krita_ai_overview` return a `model` block with the resolved `architecture`
  (`flux`, `zimage`, `illu`, `qwen`, `sd3`, `sdxl`, …), `checkpoint` and `loras`
  — decide from that. `sdxl`/`sd15` are ambiguous (used by both booru and
  natural-language checkpoints), so fall back to the checkpoint/style name; if
  still unclear, ask rather than guess.
- **Unknown proper nouns → look them up first.** If a prompt names a character,
  creature, or fictional place that isn't a globally famous real person/landmark
  (e.g. "Deku Tree", not "Messi"), web-search it before writing the prompt, so it
  describes what the thing actually looks like instead of inventing a generic
  stand-in.

This ships in the repo as three editable Claude Code skills + a reminder hook
under [`claude/`](claude/) (see
[Setup step 5](#5-optional-prompt-formatting-skills--reminder-hook)). They're the
source of truth and meant to evolve as model families do — not hardcoded into the
plugin.

---

## Tips learned the hard way

- **`create_region` always makes a *fresh, empty* layer in this fork.** Upstream
  AI Diffusion's heuristic links the active layer if it happens to be a paint
  layer — which, on a freshly-filled opaque canvas, links the *whole background*
  and makes the region cover everything. The bridge bypasses that and always
  creates a transparent layer for the silhouette mask.
- **Make regions sizable.** Silhouettes under ~15% of the canvas tend to get
  ignored. Aim for clearly distinct, generous zones.
- **Two regions is reliable; three or more is fragile.** Per the AI Diffusion
  handbook, regions "are NOT a tool for composition" — they constrain prompts to
  areas but don't *force* subjects to appear. For strict multi-subject layout,
  reach for **Control Layers** instead.
- **Keep the root prompt short and scene-level** (style, mood, lighting). The
  root is appended to every region prompt; a heavy root competes with the regions
  and can make Flux-class models render only the global scene.
- **Model choice matters more than you'd expect.** In multi-region tests,
  SDXL-class checkpoints (Juggernaut, Cinematic Photo XL, ZavyChroma) gave the
  most reliable per-region adherence. Turbo / 1-step models (Z-Image Turbo)
  effectively skip regional conditioning; Flux variants are prompt-literal and
  tend to render only the global scene when the root is strong.
- **Generation is asynchronous.** `krita_ai_generate` returns the moment the job
  is *queued*, not when the image is done. Poll `krita_ai_list_jobs` (or
  `krita_ai_status`) until the job is `finished` before applying.

---

## Performance: why it feels snappy

Most of the speed-up in this fork wasn't faster computation — it was **removing
round-trip tax.** Every assistant→server→plugin→server→assistant hop has latency;
the wins come from making fewer, fatter hops and never blocking on a poll.

- **Event-driven command queue.** The HTTP worker thread and Krita's main thread
  hand off through a per-command `threading.Event`, and a Qt signal wakes the main
  thread the instant a command lands — no 250 ms polling on the hot path (a slow
  fallback timer remains only as a safety net).
- **Persistent HTTP connection.** The MCP server keeps one `httpx.Client` open and
  reuses the socket instead of paying a TCP handshake per call.
- **`krita_batch`.** Bundle "set color + three fills + draw shape" into one
  request; the projection refreshes **once at the end**, not per command.
- **`krita_ai_overview`.** Read all AI state in a single call instead of chaining
  `status` + `list_regions` + `list_controls` + `list_jobs` + `list_styles`.
- **`krita_get_canvas(mode="fast")`.** A small JPEG for the iteration loop costs a
  fraction of the vision tokens of a full-res PNG; switch to `mode="full"` only
  for the final look.

The export/save timeout is bumped to **120 s** on both sides (server transport and
the plugin's command-queue wait), because full-resolution render/export of a large
canvas can easily exceed the default 30 s. If you add a long-running command,
pass an extended `timeout` to `send_command(...)` on both sides.

---

## How the painting actually works

Strokes, fills and shapes don't go through Krita's brush engine. The plugin draws
with **Qt's `QPainter`** onto a small transparent overlay sized to the shape's
bounding box, optionally feathers the alpha (a cheap downscale→upscale blur), then
source-over composites it back into the layer with a single `setPixelData`. All
the pixel work happens in Qt's C++ side — there's no per-pixel Python loop.

This works without color conversion because Krita's U8 "RGBA" layer is laid out
**BGRA in memory**, which is exactly `QImage.Format_ARGB32` on a little-endian
machine — so the byte buffers map directly. The same property lets background
fills happen via one `QImage.fill()` instead of building a multi-hundred-megabyte
Python byte list for a 4K canvas. The result is antialiased edges, reliable
output, and no dependence on Krita's internal brush state.

---

## Configuration

| Setting | Default | How to change |
|---------|---------|---------------|
| Plugin HTTP port | `5678` | `SERVER_PORT` in the plugin `__init__.py` |
| MCP server URL | `http://localhost:5678` | `KRITA_URL` env var |
| Canvas/preview output dir | `~/krita-mcp-output` | `CANVAS_OUTPUT_DIR` in the plugin |
| Shared auth token | _(none)_ | Set `KRITAMCP_TOKEN` to the **same** value for both the plugin (env at Krita launch) and the MCP server |

### Security model

The plugin's HTTP server binds to **localhost only**, so remote machines can't
reach it. Two further guards block the classic threat of a malicious local web
page driving Krita (CSRF / DNS-rebinding against a localhost server):

- **Origin guard (always on).** Any request carrying an `Origin` or `Referer`
  header is rejected with `403`. A real MCP client speaks plain HTTP and sends
  neither; a browser always sends one.
- **Shared token (optional).** Set `KRITAMCP_TOKEN` to require an
  `X-Kritamcp-Token` header on every request — same value in the env Krita
  launches from and for the MCP server.

Also note: action dispatch is an **allow-list by construction** — an incoming
`action: "foo"` can only ever reach a method literally named `cmd_foo`, never
arbitrary plugin internals.

File paths passed to `krita_save` / `krita_open_file` are **not** sandboxed — your
MCP client is trusted to choose them, like any agent with disk access. Previews
from `krita_ai_save_preview` and exports from `krita_get_canvas` land in
`CANVAS_OUTPUT_DIR`.

---

## Architecture notes

- **Threading.** Every command — painting *and* `cmd_ai_*` — runs on Krita's Qt
  main thread, driven by the queue's wake signal. AI Diffusion's `Model` API
  expects main-thread calls for document mutation, so this falls out naturally
  with no extra locking.
- **Job lifecycle.** `model.generate()` returns right after enqueuing; the actual
  diffusion runs on AI Diffusion's asyncio loop and reports back via signals.
  There is no built-in "wait until done" — the client polls `model.jobs` via
  `krita_ai_list_jobs`.
- **Graceful degradation.** Model/architecture resolution is best-effort: if
  AI Diffusion's API has drifted, or you're offline, `krita_ai_status` still
  returns a usable payload with `model.available = false` rather than erroring.

---

## Credits

- Original Krita MCP bridge: [nanayax3/krita-mcp](https://github.com/nanayax3/krita-mcp)
- AI Diffusion plugin (bridged, **not** modified):
  [Acly/krita-ai-diffusion](https://github.com/Acly/krita-ai-diffusion)
- This fork adds the AI Diffusion bridge layer, Control Layers, batching, and the
  prompt-formatting workflow.

## License

MIT
</content>
</invoke>
