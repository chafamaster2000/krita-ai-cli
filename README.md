# kri — talk to your AI assistant, it drives Krita for you

> Started as a fork of [nanayax3/krita-mcp](https://github.com/nanayax3/krita-mcp);
> now a standalone project: a live bridge to
> [Acly/krita-ai-diffusion](https://github.com/Acly/krita-ai-diffusion) driven by a
> zero-dependency CLI (`kri`) instead of an MCP server.
> You describe what you want in plain language; an AI assistant with shell access
> (Claude Code, or any coding agent) sets the prompt, switches style, paints rough
> region masks, generates, shows you the result, and applies it — **all inside your
> real, open Krita document.** Your layers, your canvas, your undo history.

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
AI assistant  ──Bash──►  kri CLI (cli/kri)  ──HTTP :5678──►  Krita plugin (kritamcp)
   (Claude Code, etc.)    translates a subcommand      runs the command on Krita's
                          into one HTTP request         main UI thread
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

1. **The AI assistant** runs shell commands. Each `kri` subcommand is one Krita
   action: `kri canvas`, `kri ai generate`, etc. — and `kri batch` packs many
   actions into a single invocation (one assistant turn instead of ten).

2. **The `kri` CLI** (`cli/kri`) is a single-file, stdlib-only Python script. It
   validates arguments (argparse choices/types) and turns each subcommand into
   a single HTTP request to `localhost:5678`. No venv, no dependencies, ~50 ms
   startup.

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

### 1. Install Krita AI Diffusion (required for the `kri ai …` commands)

Follow the official guide: <https://github.com/Acly/krita-ai-diffusion>. The
plugin must be installed and its ComfyUI backend reachable. Without it, the
painting commands still work, but every `kri ai …` command returns an error.

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

### 3. Install the `kri` CLI (on your machine)

No dependencies, no venv — it's one stdlib-only Python file:

```bash
ln -sf "$PWD/cli/kri" ~/.local/bin/kri     # make sure ~/.local/bin is on PATH
kri health                                  # → {"status": "ok", "plugin": "kritamcp"}
```

Everyday flow with an assistant (or by hand):

```bash
kri status                 # document + AI Diffusion state, one call
kri look                   # canvas → /tmp/kri-canvas.jpg (the assistant Reads it)
kri batch <<'EOF'          # many steps, one round-trip
[{"action": "set_color", "params": {"color": "#ffffff"}},
 {"action": "fill", "params": {"x": 100, "y": 100, "radius": 80}}]
EOF
kri ai generate --wait     # generate and block until the queue drains
```

`kri --help` / `kri <cmd> --help` list everything. For Claude Code, allowlist it
in `settings.json` so every call runs unprompted except `exec`:

```json
"permissions": {
  "allow": ["Bash(kri:*)"],
  "ask": ["Bash(kri exec:*)"]
}
```

### 4. (Optional) `kri exec` — arbitrary Python inside Krita

Escape hatch for anything without a subcommand: launch Krita with
`KRITAMCP_ALLOW_EXEC=1` in its environment, then pipe a script to `kri exec`.
It runs on Krita's main thread with `app`/`doc`/`view`/`layer` and the Qt paint
classes in scope; `print()` output and tracebacks come back in the response.
Off by default.

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

Three skills carry the knowledge (`kri-prompt-format`,
`kri-prompt-unknown-entities`, `kri-prompt-sanity-check`), and a
reminder-only `PreToolUse` hook injects the active model's family before
`kri ai set-prompt` / `kri ai generate` (it matches on the Bash command text and
stays silent for anything else). Wire the hook in your `settings.json`:

```json
"PreToolUse": [
  {
    "matcher": "Bash",
    "hooks": [
      { "type": "command", "command": "python3 \"$HOME/.claude/hooks/krita-prompt-reminder.py\"", "timeout": 8 }
    ]
  }
]
```

On Windows use `python` instead of `python3`. The hook only *reminds* — it never
blocks a call. Restart Claude Code (or run `/hooks`) to load it.

---

## The commands

All commands run against the document currently open in Krita. Every command
prints JSON and exits non-zero on error, so `kri a && kri b` chains stop at the
first failure.

### Painting & canvas

| Command | What it does |
|------|--------------|
| `kri health` | Is Krita up with the plugin active? |
| `kri canvas` | New canvas (width, height, name, background) — fills a visible paint layer and makes it active |
| `kri open` | Open an existing `.kra`, `.png`, `.jpg`, … |
| `kri color` | Set foreground paint color (hex) |
| `kri brush` | Pick a brush preset (partial name match), size, opacity |
| `kri stroke` | Paint an antialiased stroke through points: `kri stroke 100,100 150,120`, optional `--feather` |
| `kri fill` | Antialiased filled circle at a point (great for mask blobs) |
| `kri shape` | Rectangle / ellipse / line, fill or outline, optional `feather` |
| `kri clear` | Flood the active layer with one solid color |
| `kri color-at` | Eyedropper — sample the pixel at (x, y) |
| `kri brushes` | List brush presets (filter, limit) |
| `kri look` | **Look at the canvas.** Writes the image to a file (default `/tmp/kri-canvas.jpg`) and prints the path — the assistant Reads it. Default = small JPEG for the working loop; `--full` = full-res PNG for final review |
| `kri save` | Export the document to a file path |
| `kri undo` / `kri redo` | Step the undo history |
| `kri batch` | **Run many actions in ONE round-trip** (JSON via stdin or file), stopping at the first error; `--look fast` also returns the final canvas. Use whenever you'd otherwise chain calls (e.g. several fills to build a mask) |
| `kri exec` | Arbitrary Python inside Krita (requires `KRITAMCP_ALLOW_EXEC=1` at Krita launch) |

### AI Diffusion bridge

| Command | What it does |
|------|--------------|
| `kri status` | **The whole AI state in one call:** connection, workspace, style, strength/seed, queue, all regions, all control layers, recent jobs, available styles. Prefer this over chaining the read-only tools below |
| `kri ai status` | Lighter snapshot: connection, document, workspace, style, strength/seed, queue counts, current prompt, and the resolved **model architecture** |
| `kri ai styles` | List AI Diffusion style presets (filter, limit) |
| `kri ai set-params` | Set style, strength, seed, fixed_seed, batch_count |
| `kri ai set-prompt` | Set positive (on active region or root) and/or negative (always root) prompt |
| `kri ai workspace` | Switch workspace: `generation` / `upscaling` / `live` / `animation` / `custom` |
| `kri ai generate` | Trigger generation in the current workspace; `--wait` blocks until the queue drains (no manual polling) |
| `kri ai jobs` | List jobs newest-first (id, state, prompt, result count) |
| `kri ai apply` | Apply a finished job's result to the canvas (no `job_id` → latest) |
| `kri ai preview` | Save a result to disk **without** applying it, for review |
| `kri ai cancel` | Cancel the active job and/or all queued jobs |
| **Regions** | |
| `kri ai region create` | Make a region + a **fresh transparent paint layer**, set its prompt, activate the layer so painting tools draw its mask |
| `kri ai region list` | Root prompt + every child region with layer ids, prompts, active flag |
| `kri ai region select` | Select a region by index or layer_id (no args = root) |
| `kri ai region remove` | Remove a region by index |
| **Control Layers** (ControlNet / IP-Adapter) | |
| `kri ai control add` | Add a control to root or a region: `scribble`, `line_art`, `canny_edge`, `depth`, `pose`, `segmentation`, … (ControlNet) or `reference`, `style`, `composition`, `face` (IP-Adapter) |
| `kri ai control list` | List control layers across root + all regions (or one scope) |
| `kri ai control set` | Change an existing control's mode / strength |
| `kri ai control remove` | Remove a control by index |

---

## Workflows

### A — One image from a prompt

```bash
kri canvas 1024 1024 --bg "#ffffff"
kri ai set-params --style "cinematic-photo-xl"
kri ai set-prompt -p "a lighthouse on a cliff at dusk, stormy sea"
kri ai generate --wait          # blocks until the job finishes
kri ai apply && kri look --full # apply the latest result, review full-res
```

### B — Two subjects, placed by region

Regions let you say "*this* prompt belongs in *this* area." You paint a rough
silhouette on each region's mask layer.

```bash
# scene + both regions with their masks, in ONE round-trip
kri batch <<'EOF'
[{"action": "new_canvas", "params": {"width": 1024, "height": 1024, "background": "#ffffff"}},
 {"action": "ai_set_params", "params": {"style": "juggernaut-xl"}},
 {"action": "ai_set_prompt", "params": {"positive": "snowy mountain valley, golden hour"}},
 {"action": "ai_create_region", "params": {"positive": "grizzly bear standing on snow"}},
 {"action": "set_color", "params": {"color": "#6b4226"}},
 {"action": "draw_shape", "params": {"shape": "ellipse", "x": 70, "y": 520, "width": 420, "height": 320}},
 {"action": "ai_create_region", "params": {"positive": "timber wolf, alert, side profile"}},
 {"action": "draw_shape", "params": {"shape": "ellipse", "x": 560, "y": 560, "width": 380, "height": 300}},
 {"action": "ai_select_region", "params": {}}]
EOF
kri ai generate --wait && kri ai apply && kri look
```

Each `ai_create_region` activates a fresh transparent layer, so the shapes that
follow it paint *that* region's silhouette mask.

### C — Restyle an existing image (img2img / refine)

Set `strength` below `1.0` and AI Diffusion switches to **refine**: your current
canvas becomes the img2img source instead of starting from noise.

```bash
kri open /path/to/photo.png
kri ai set-params --style "digital-artwork-xl" --strength 0.6
kri ai set-prompt -p "oil painting, thick impasto brushwork"
kri ai generate --wait && kri ai apply
```

### D — Force structure with a Control Layer

Regions constrain *where a prompt applies*; they don't force a subject to appear
in a precise shape. For strict pose/outline/composition, add a Control Layer that
reads from a layer you've drawn on:

```bash
# … draw a scribble / line-art / pose on a layer, then:
kri ai control add scribble --strength 0.7   # sources the active layer
kri ai set-prompt -p "a knight in plate armor"
kri ai generate --wait
```

---

## Prompt formatting (the secret sauce)

**Every diffusion model family wants a different prompt *language*.** Feeding
danbooru tags to a natural-language model — or full sentences to a booru anime
model — visibly degrades the result. The rule: **before every
`kri ai set-prompt`, check the active style's model and write in its dialect.**

| Family (by resolved architecture) | Prompt convention | Negative prompt |
|---|---|---|
| Flux, Flux Kontext, Z-Image, Qwen, SD3, realistic SDXL (Cinematic Photo / Digital Artwork) | **Natural language** — descriptive sentences | Flux / Z-Image Turbo ignore it (leave empty); others optional |
| Pony, Illustrious, NoobAI, Animagine, most anime SDXL / SD1.5 | **Danbooru tags** — comma-separated keywords (Pony also wants `score_9, score_8_up, score_7_up`) | Quality tags: `worst quality, low quality, …` |

Two more rules the assistant follows:

- **Classify by architecture, not by the style's name.** A style called
  "Realistic" can run on an anime checkpoint. `kri ai status` /
  `kri status` return a `model` block with the resolved `architecture`
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
- **Generation is asynchronous.** `kri ai generate` returns the moment the job
  is *queued*, not when the image is done. Use `--wait` to block until the queue
  drains (it polls `ai_status` internally), then apply.

---

## Performance: why it feels snappy

Most of the speed-up in this project wasn't faster computation — it was
**removing round-trip tax.** For an AI assistant the expensive hop is its own
turn (a full model inference per command), so the wins come from doing more per
invocation and never blocking on a poll.

- **CLI over MCP.** No tool schemas inflating the assistant's context, no server
  process; a stdlib-only script that starts in ~50 ms. (The MCP server this repo
  used to ship was retired — see tag `v-mcp-final`.)
- **`kri status`.** Document + the whole AI state in one invocation instead of
  chaining `health` + `status` + `region list` + `control list` + `styles`.
- **`kri batch`.** Bundle "set color + three fills + draw shape" into one
  request; the projection refreshes **once at the end**, not per command.
- **`kri ai generate --wait`.** One invocation that blocks until the job is done
  — the assistant doesn't burn turns polling `kri ai jobs`.
- **`kri look`.** A small JPEG for the iteration loop costs a fraction of the
  vision tokens of a full-res PNG; `--full` only for the final look.
- **Event-driven command queue (plugin side).** The HTTP worker thread and
  Krita's main thread hand off through a per-command `threading.Event`, and a Qt
  signal wakes the main thread the instant a command lands — no 250 ms polling
  on the hot path (a slow fallback timer remains only as a safety net).

The export/save timeout is bumped to **120 s** on both sides (CLI transport and
the plugin's command-queue wait), because full-resolution render/export of a
large canvas can easily exceed the default 30 s.

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
| CLI endpoint | `http://localhost:5678` | `KRITA_URL` env var |
| `kri look` output path | `/tmp/kri-canvas.jpg` | `KRI_LOOK_PATH` env var or `-o` |
| Canvas/preview output dir | `~/krita-mcp-output` | `CANVAS_OUTPUT_DIR` in the plugin |
| `kri exec` gate | disabled | `KRITAMCP_ALLOW_EXEC=1` in Krita's environment |
| Shared auth token | _(none)_ | Set `KRITAMCP_TOKEN` to the **same** value for both the plugin (env at Krita launch) and your shell |

### Security model

The plugin's HTTP server binds to **localhost only**, so remote machines can't
reach it. Two further guards block the classic threat of a malicious local web
page driving Krita (CSRF / DNS-rebinding against a localhost server):

- **Origin guard (always on).** Any request carrying an `Origin` or `Referer`
  header is rejected with `403`. The `kri` CLI speaks plain HTTP and sends
  neither; a browser always sends one.
- **Shared token (optional).** Set `KRITAMCP_TOKEN` to require an
  `X-Kritamcp-Token` header on every request — same value in the env Krita
  launches from and in the shell running `kri`.
- **`kri exec` is double-gated.** Disabled in the plugin unless Krita starts
  with `KRITAMCP_ALLOW_EXEC=1`, and (with Claude Code) listed under `ask`
  permissions so it always prompts.

Also note: action dispatch is an **allow-list by construction** — an incoming
`action: "foo"` can only ever reach a method literally named `cmd_foo`, never
arbitrary plugin internals.

File paths passed to `kri save` / `kri open` are **not** sandboxed — whoever
runs the CLI is trusted to choose them, like any agent with disk access.
Previews from `kri ai preview` land in `CANVAS_OUTPUT_DIR`; `kri look` images
land at `/tmp/kri-canvas.jpg` (or `-o`).

---

## Architecture notes

- **Threading.** Every command — painting *and* `cmd_ai_*` — runs on Krita's Qt
  main thread, driven by the queue's wake signal. AI Diffusion's `Model` API
  expects main-thread calls for document mutation, so this falls out naturally
  with no extra locking.
- **Job lifecycle.** `model.generate()` returns right after enqueuing; the actual
  diffusion runs on AI Diffusion's asyncio loop and reports back via signals.
  There is no built-in "wait until done" in the plugin — `kri ai generate --wait`
  does the polling client-side so the assistant doesn't have to.
- **Graceful degradation.** Model/architecture resolution is best-effort: if
  AI Diffusion's API has drifted, or you're offline, `kri ai status` still
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
