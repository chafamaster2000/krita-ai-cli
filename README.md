# Krita MCP Server — AI Diffusion Bridge Fork

> Fork of [nanayax3/krita-mcp](https://github.com/nanayax3/krita-mcp) that adds a live
> bridge to [Acly/krita-ai-diffusion](https://github.com/Acly/krita-ai-diffusion).
> An MCP client can now drive AI image generation end-to-end from inside Krita:
> set prompts, switch styles, create regions, draw silhouette masks, generate,
> review results, and apply them — all without touching Krita's UI.

This bridge allows Claude (or any MCP client) to create canvases, paint strokes,
draw shapes, export images, **and now drive Krita AI Diffusion's full generation
pipeline** — all inside a running Krita instance.

## What This Fork Adds

The upstream `nanayax3/krita-mcp` exposes Krita's painting primitives (canvas,
brush, stroke, fill, shape, export). This fork keeps every existing tool intact
and adds a second surface that bridges to the AI Diffusion plugin running in the
same Krita process. The plugin imports `ai_diffusion.model.root` directly and
calls its API on Krita's main thread.

**Why this works without patching AI Diffusion:** the `kritamcp` plugin and the
`ai_diffusion` plugin both run inside Krita's embedded Python interpreter. Once
both are loaded, `kritamcp` can `from ai_diffusion.model.root import root` and
manipulate the live `Model` object that AI Diffusion uses internally. No fork
of the AI Diffusion plugin is needed.

13 new MCP tools are exposed (`krita_ai_*`). The original painting tools
are unchanged.

## How It Works

Two components:

1. **Krita Plugin** (`krita-plugin/`) — A Python plugin that runs inside Krita,
   exposing an HTTP server on `localhost:5678`. It receives commands and executes
   them on Krita's main thread via a command queue. New handlers (`cmd_ai_*`)
   bridge to the AI Diffusion plugin's internal API.

2. **MCP Server** (`server.py`) — A [FastMCP](https://github.com/jlowin/fastmcp)
   server that exposes tools to any MCP client. Translates MCP tool calls into
   HTTP requests to the Krita plugin.

```
MCP Client (Claude, etc.)  ←→  MCP Server (server.py)  ←→  Krita Plugin (HTTP :5678)
                                                              │
                                                              ↓ (in-process import)
                                                          AI Diffusion plugin
                                                              │
                                                              ↓
                                                          ComfyUI backend
```

## Setup

### 1. Install Krita AI Diffusion (required for the AI tools)

Follow the install guide at https://github.com/Acly/krita-ai-diffusion. The
plugin must be installed and the ComfyUI backend reachable. Without it, the
`krita_ai_*` tools will return an error but the painting tools still work.

### 2. Install the Krita MCP Plugin

Copy the plugin files to your Krita plugins directory:

| OS | Path |
|----|------|
| Windows | `%APPDATA%\krita\pykrita\` |
| Linux | `~/.local/share/krita/pykrita/` |
| macOS | `~/Library/Application Support/krita/pykrita/` |

Copy both:
- `krita-plugin/kritamcp/` (the folder with `__init__.py`)
- `krita-plugin/kritamcp.desktop`

Then in Krita: **Settings → Configure Krita → Python Plugin Manager → Enable
"Krita MCP Bridge"** and restart Krita.

### 3. Install the MCP Server

```bash
pip install fastmcp httpx
```

Or with a virtual environment:
```bash
python -m venv .venv
source .venv/bin/activate  # or .venv\Scripts\activate on Windows
pip install -r requirements.txt
```

### 4. Configure Your MCP Client

Add to your MCP client config (e.g., Claude Desktop's `claude_desktop_config.json`):

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

If using a virtual environment, point `command` at the venv's Python.

### 5. (Optional) Prompt-formatting skills + hook

The prompt-formatting workflow (see [Prompt Formatting](#prompt-formatting--match-the-model-family))
is implemented as personal Claude Code skills plus a reminder hook, kept in your
own Claude Code config (`~/.claude/` on macOS/Linux, `%USERPROFILE%\.claude\` on
Windows) — it is **not** shipped in this repo. Three skills
(`krita-ai-prompt-format`, `image-prompt-unknown-entities`,
`image-prompt-sanity-check`) carry the model→convention knowledge; a
reminder-only `PreToolUse` hook (`~/.claude/hooks/krita-prompt-reminder.py`)
calls `ai_status` and injects the active model's family before `set_prompt` /
`generate`. Example hook wiring in `<config>/settings.json` (merge into any
existing `"hooks"`):

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

On Windows use `python` (or `py`) instead of `python3`; `$HOME` resolves in both
PowerShell and Git Bash. Restart Claude Code (or open `/hooks`) to load it. The
hook is reminder-only and never blocks.

## Available Tools

### Original Painting Tools (from upstream)

| Tool | Description |
|------|-------------|
| `krita_health` | Check if Krita is running with the plugin active |
| `krita_new_canvas` | Create a new canvas (width, height, background color) |
| `krita_set_color` | Set foreground paint color (hex) |
| `krita_set_brush` | Set brush preset, size, and opacity |
| `krita_stroke` | Paint a stroke through a list of [x, y] points |
| `krita_fill` | Fill a circular area at a point |
| `krita_draw_shape` | Draw rectangle, ellipse, or line |
| `krita_get_canvas` | Export canvas to PNG (for the AI to see progress) |
| `krita_save` | Save canvas to a specific file path |
| `krita_undo` / `krita_redo` | Undo/redo actions |
| `krita_clear` | Clear canvas to a solid color |
| `krita_get_color_at` | Eyedropper — sample color at a pixel |
| `krita_list_brushes` | List available brush presets |
| `krita_open_file` | Open an existing `.kra`, `.png`, `.jpg`, etc. |

### AI Diffusion Bridge (new in this fork)

| Tool | Description |
|------|-------------|
| `krita_ai_status` | Server connection, active document, workspace, current style/strength/seed, queue counts, current prompt |
| `krita_ai_list_styles` | List available AI Diffusion style presets (filter, limit) |
| `krita_ai_set_prompt` | Set positive/negative prompt on the active region (or root) |
| `krita_ai_set_params` | Set style, strength, seed, fixed_seed, batch_count |
| `krita_ai_set_workspace` | Switch workspace: `generation` / `upscaling` / `live` / `animation` / `custom` |
| `krita_ai_generate` | Trigger generation in the current workspace |
| `krita_ai_list_jobs` | List jobs newest-first with id, state, prompt, result count |
| `krita_ai_apply` | Apply a finished job's result to the canvas (job_id optional → latest) |
| `krita_ai_cancel` | Cancel active and/or queued jobs |
| `krita_ai_save_preview` | Save a generated image to disk without applying (for review) |
| `krita_ai_create_region` | Create a new region linked to a fresh paint layer; activates the layer for mask painting |
| `krita_ai_list_regions` | List root prompt + all child regions with their layer ids and prompts |
| `krita_ai_select_region` | Select a region by index or layer_id (no args = root) |
| `krita_ai_remove_region` | Remove a region by index |

## Region Workflow Example

The canonical loop for region-based generation:

```text
1. krita_new_canvas(1024, 1024, "#ffffff")
2. krita_ai_set_params(style="cinematic-photo-xl")
3. krita_ai_set_prompt(positive="snowy mountain landscape, golden hour")
4. krita_ai_create_region(positive="grizzly bear standing on snow")
   → activates a fresh transparent paint layer
5. krita_set_color("#6b4226")
6. krita_draw_shape(shape="ellipse", x=70, y=520, width=420, height=320)
   ... draw rest of bear silhouette on the region's layer
7. krita_ai_create_region(positive="timber wolf, alert, side profile")
   → activates a second fresh layer
8. ... draw wolf silhouette
9. krita_ai_select_region()      # deselect → root active
10. krita_ai_generate()
11. krita_ai_list_jobs()         # wait until state == "finished"
12. krita_ai_save_preview(job_id="...") + Read the PNG to review
13. krita_ai_apply(job_id="...")
```

### Tips Learned the Hard Way

- **`create_region` forces a NEW empty paint layer** in this fork. Upstream
  AI Diffusion's `create_region` heuristic links the active layer if it happens
  to be a paint layer — which means linking the fully opaque canvas layer and
  making the region cover everything. The bridge bypasses that and always
  creates a fresh transparent layer for the silhouette mask.

- **Region size matters.** The doc says "ensure adequate size." In practice
  small silhouettes (under ~15% of the canvas) get ignored by the model. Aim
  for clearly distinct, sizable zones.

- **Two regions is reliable, three+ is fragile.** SDXL with two well-separated
  regions hit 2/2 consistently in testing. Three regions dropped to 1–2/3 hits.
  Per the AI Diffusion handbook, regions "are NOT a tool for composition" — they
  constrain prompts to areas but don't force subjects to appear. For strict
  multi-subject composition you need Control Layers (scribble / line-art /
  depth), which this bridge does not currently expose.

- **Root prompt is appended to every region prompt.** Keep root short and
  scene-level (style, mood, lighting) rather than describing the background in
  full. Strong scene description in root competes with region prompts and can
  make Flux-class models ignore the regions.

- **Model choice matters more than expected.** In multi-region tests,
  SDXL-class models (Juggernaut, Cinematic Photo XL, ZavyChroma) gave the
  most reliable per-region adherence. Turbo / 1-step models (Z-Image Turbo)
  effectively skip regional conditioning. Flux variants are too prompt-literal
  with a strong root prompt and tend to render only the global scene.

- **Img2img refine via strength < 1.0.** When `strength` is below 1.0 the
  workflow switches to refine — the existing canvas is used as the img2img
  source. Useful for style-transferring an existing image (set
  `style="digital-artwork-xl"`, `strength=0.6`, set a new prompt, generate).

## Prompt Formatting — Match the Model Family

**Every diffusion model family wants a different prompt *language*.** Sending
danbooru tags to a natural-language model (or full sentences to a booru anime
model) noticeably degrades results. The rule of thumb: **before every
`krita_ai_set_prompt`, check the active style with `krita_ai_status` and format
the prompt for that style's family.**

| Family (keyword in style name) | Prompt convention | Negative |
|---|---|---|
| Flux, Flux Kontext, Z-Image, Qwen, SD3, SDXL realistic (Cinematic Photo / Digital Artwork) | Natural language — descriptive sentences | Flux / Z-Image Turbo ignore it (leave empty); others optional |
| Pony, Illustrious, NoobAI, Animagine, most anime SDXL/SD1.5 | Danbooru tags — comma-separated keywords (Pony also needs `score_9, score_8_up, score_7_up`) | Quality tags: `worst quality, low quality, ...` |

Two more rules:

- **Confirm by architecture, not just the name.** A style named "Realistic" can
  run on an anime checkpoint. `krita_ai_status` returns a `model` block with the
  resolved `architecture` (e.g. `flux`, `zimage`, `illu`, `qwen`, `sd3`, `sdxl`),
  `checkpoint`, and `loras` — classify by that. Note `sdxl`/`sd15` are ambiguous
  (they run both booru and natural-language checkpoints), so for those fall back
  to the checkpoint/style name; if still unclear, don't guess — ask.
- **Unknown proper nouns → look them up first.** If a prompt names a character,
  creature, or fictional place that isn't a globally known real person/landmark
  (e.g. "Deku Tree", not "Messi"), web-search it before writing the prompt so you
  describe what it actually looks like instead of inventing something generic.

This workflow is encoded as three editable Claude Code skills (source of truth,
meant to change as model families do — not hardcoded into the plugin):

| Skill | Role |
|-------|------|
| `krita-ai-prompt-format` | Model→convention table, tag↔sentence conversion, per-family negatives. Classifies by `model.architecture`. |
| `image-prompt-unknown-entities` | Web-search an unknown proper noun before describing it (reusable beyond Krita). |
| `image-prompt-sanity-check` | Last gate before `krita_ai_generate`: verify the final prompt is coherent with the active model and the user's intent. |

A reminder-only `PreToolUse` hook (`krita-prompt-reminder.py`) fires before
`krita_ai_set_prompt` / `krita_ai_generate`: it calls `ai_status` itself and
injects the active model's family + convention, so the right format is enforced
even if the skills aren't loaded. It never blocks.

These skills and the hook live in your personal Claude Code config, not in this
repo; see [Setup step 5](#5-optional-prompt-formatting-skills--hook) for the hook
wiring.

## The Export Timeout Fix (from upstream)

By default HTTP requests and command queue operations time out after ~30 seconds.
Canvas export (`get_canvas`) and file save (`save`) operations can easily exceed
this on larger canvases. Both sides — MCP server timeout and plugin command queue
timeout — are bumped to 120s.

If you build new commands that can take a long time, remember to pass an
extended `timeout` to `send_command(...)` on both sides.

## Configuration

| Setting | Default | How to Change |
|---------|---------|---------------|
| Plugin HTTP port | `5678` | Edit `SERVER_PORT` in plugin `__init__.py` |
| MCP server URL | `http://localhost:5678` | Set `KRITA_URL` env var |
| Canvas output dir | `~/krita-mcp-output` | Edit `CANVAS_OUTPUT_DIR` in plugin `__init__.py` |
| Shared auth token | _(none)_ | Set `KRITAMCP_TOKEN` to the **same** value for both the plugin (env at Krita launch) and the MCP server |

### Security model

The plugin's HTTP server binds to `localhost` only, so remote machines can't
reach it. Two further guards protect against a malicious local web page driving
Krita (the classic CSRF / DNS-rebinding vector against a localhost server):

- **Origin guard (always on):** any request carrying an `Origin` or `Referer`
  header is rejected with `403`. A real MCP client speaks plain HTTP and sends
  neither; a browser always sends one.
- **Shared token (optional):** set `KRITAMCP_TOKEN` to require an
  `X-Kritamcp-Token` header on every request. Set the same value in the
  environment Krita launches from and for the MCP server process.

File paths passed to `krita_save` / `krita_open_file` are **not** sandboxed —
your MCP client is trusted to choose them, the same as any agent with disk
access.

Generated previews from `krita_ai_save_preview` and exports from
`krita_get_canvas` land in `CANVAS_OUTPUT_DIR`.

## Architecture Notes

### Why the bridge is a separate plugin

It would be cleaner to add MCP support directly into `ai_diffusion` as a
contributed feature. We chose a separate plugin so that:

- Upstream `ai_diffusion` updates don't require re-applying patches.
- The bridge can be installed and removed without touching the AI Diffusion
  installation.
- Failures on the bridge side never affect normal AI Diffusion UI use.

The trade-off: the bridge imports private-ish internals of `ai_diffusion`
(`model.root`, `model.regions._regions`, `model.regions._add`). If upstream
refactors those, the bridge breaks. The imports are localized to
`kritamcp/__init__.py` so the surface area to update is small.

### Threading

All `cmd_ai_*` handlers run on Krita's Qt main thread via the existing
`QTimer`-driven command queue. AI Diffusion's `Model` API expects to be called
on the main thread for document mutation, so this works out naturally without
extra locking.

### Job lifecycle

`model.generate()` returns synchronously after enqueuing the job. The actual
generation runs on AI Diffusion's asyncio loop and reports back via signals.
`krita_ai_list_jobs` polls `model.jobs` to surface state. There is no built-in
"wait until done" — the MCP client should poll or sleep + re-check, exactly as
the test suite does.

## Painting Approach (from upstream)

The plugin paints using **direct pixel manipulation** (not Krita's native brush
engine for strokes). Strokes use a custom soft-circle renderer with configurable
hardness. Alpha blending is done manually in BGRA pixel format. Shapes
(rectangle, ellipse, line) are rasterized directly. This approach is reliable
and doesn't depend on Krita's internal brush state.

## Credits

- Original Krita MCP bridge: [nanayax3/krita-mcp](https://github.com/nanayax3/krita-mcp)
- AI Diffusion plugin (not modified, only bridged):
  [Acly/krita-ai-diffusion](https://github.com/Acly/krita-ai-diffusion)
- This fork adds the AI Diffusion bridge layer.

## License

MIT
