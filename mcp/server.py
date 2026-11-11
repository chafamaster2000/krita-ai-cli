"""
Krita MCP Server
Bridge between Claude (or any MCP client) and Krita painting application.

Uses FastMCP to expose Krita painting tools over the Model Context Protocol,
communicating with a Krita plugin via HTTP.

Versioned again in krita-ai-cli (mcp/server.py): the `v-mcp-final` server plus
the layer/mask/SAM wrappers over `kri`, plus the tools that kri extensions
declare in their kri-extension.toml (`kri ext mcp-tools`, loaded at start-up;
each call runs `kri <ext> <command…> …`). No extension code is imported here,
so the server venv never needs the extensions' dependencies.

Env: KRITA_URL, KRITAMCP_TOKEN, KRI_BIN (the kri executable, default `kri`).
"""

from fastmcp import FastMCP
try:
    from fastmcp.utilities.types import Image
except ImportError:  # newer FastMCP re-exports it at the top level
    from fastmcp import Image
try:
    from fastmcp.exceptions import ToolError
except ImportError:  # very old FastMCP — degrade to a plain runtime error
    ToolError = RuntimeError
import atexit
import base64
import json
import httpx
import os
from typing import Annotated, Literal, Optional
from pydantic import Field

# Shared enum domains — expressed as Literal so they become JSON-schema `enum`s.
# The model can't pass an out-of-domain value; validation happens before the
# command ever reaches Krita.
Workspace = Literal["generation", "upscaling", "live", "animation", "custom"]
JobStateFilter = Literal["all", "queued", "executing", "finished", "cancelled"]
ControlModeName = Literal[
    "scribble", "line_art", "soft_edge", "canny_edge", "depth", "normal",
    "pose", "segmentation", "blur", "stencil",
    "reference", "style", "composition", "face",
]
ShapeName = Literal["rectangle", "ellipse", "line"]
CanvasMode = Literal["fast", "full"]

# Configuration
# 127.0.0.1, not localhost: avoids the ~2 s IPv6 fallback on Windows.
KRITA_URL = os.environ.get("KRITA_URL", "http://127.0.0.1:5678")
# Optional shared-token auth. Set the same value here (via env) and in the
# plugin (KRITAMCP_TOKEN) to require it; empty = no token check.
KRITA_TOKEN = os.environ.get("KRITAMCP_TOKEN", "")

mcp = FastMCP(
    "krita-mcp",
    instructions=(
        "Bridge to a running Krita instance for painting and AI Diffusion image "
        "generation.\n\n"
        "Efficiency: prefer krita_ai_overview to read all AI state in one call "
        "instead of chaining status/list_* tools, and krita_batch to run several "
        "paint/AI commands in a single round-trip. While iterating, call "
        "krita_get_canvas with mode='fast'; use mode='full' only for a final "
        "detailed review.\n\n"
        "Region masks: krita_ai_create_region activates a fresh transparent layer "
        "— paint the silhouette with krita_fill/krita_draw_shape/krita_stroke. "
        "Keep regions sizable (>15% of the canvas) and prefer 2 regions over 3+ "
        "for reliable per-region adherence."
    ),
)

# Persistent HTTP client — reuses the connection across commands instead of
# paying a fresh TCP handshake on every call. Closed on interpreter exit so we
# don't leak the socket when the MCP host shuts the server down.
_client = httpx.Client(timeout=30.0)
atexit.register(_client.close)


def _headers() -> dict:
    return {"X-Kritamcp-Token": KRITA_TOKEN} if KRITA_TOKEN else {}


def send_command(action: str, params: Optional[dict] = None, timeout: float = 30.0) -> dict:
    """Send command to Krita plugin and return result.

    The intended timeout is forwarded in the body so the plugin can stop waiting
    on its side just before our transport gives up, returning a clean error
    instead of a dropped connection.
    """
    if params is None:
        params = {}

    try:
        response = _client.post(
            KRITA_URL,
            json={"action": action, "params": params, "timeout": timeout},
            timeout=timeout,
            headers=_headers(),
        )
        return response.json()
    except httpx.ConnectError:
        return {"error": "Cannot connect to Krita. Is Krita running with the MCP plugin enabled?"}
    except Exception as e:
        return {"error": str(e)}


def _decode_image(result: dict) -> Image:
    """Turn a plugin canvas payload (base64 + format) into an inline MCP Image."""
    raw = base64.b64decode(result["data_b64"])
    return Image(data=raw, format=result.get("format", "png"))


def _raise_on_error(result: dict) -> dict:
    """Raise ToolError if the plugin reported a failure; otherwise return result.

    Surfacing errors as ToolError (instead of a normal "Error: ..." string) lets
    the MCP client mark the call as failed rather than mistaking the message for a
    successful result. Truthiness, not key presence: the AI status payload carries
    a nullable `error` field that is null on success.
    """
    if result.get("error"):
        raise ToolError(str(result["error"]))
    return result


@mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
def krita_health() -> str:
    """Check if Krita is running and the MCP plugin is active."""
    try:
        response = _client.get(f"{KRITA_URL}/health", timeout=5.0, headers=_headers())
        data = response.json()
        return f"Krita is running. Plugin: {data.get('plugin', 'unknown')}"
    except Exception:
        return "Cannot connect to Krita. Make sure Krita is running with the MCP plugin enabled."


@mcp.tool()
def krita_new_canvas(
    width: Annotated[int, Field(ge=1, le=16384)] = 800,
    height: Annotated[int, Field(ge=1, le=16384)] = 600,
    name: str = "New Canvas",
    background: str = "#1a1a2e"
) -> str:
    """
    Create a new canvas in Krita.

    Args:
        width: Canvas width in pixels (default 800)
        height: Canvas height in pixels (default 600)
        name: Document name
        background: Background color as hex (default dark blue)
    """
    _raise_on_error(send_command("new_canvas", {
        "width": width,
        "height": height,
        "name": name,
        "background": background
    }))
    return f"Created canvas: {width}x{height}, background: {background}"


@mcp.tool(annotations={"idempotentHint": True})
def krita_set_color(color: str) -> str:
    """
    Set the foreground (paint) color.

    Args:
        color: Hex color code (e.g., "#ff6b6b", "#b8a9c9")
    """
    _raise_on_error(send_command("set_color", {"color": color}))
    return f"Color set to {color}"


@mcp.tool(annotations={"idempotentHint": True})
def krita_set_brush(
    preset: Optional[str] = None,
    size: Annotated[Optional[int], Field(ge=1, le=10000)] = None,
    opacity: Annotated[Optional[float], Field(ge=0.0, le=1.0)] = None
) -> str:
    """
    Set brush preset and properties.

    Args:
        preset: Brush preset name (partial match, e.g., "Basic", "Soft", "Airbrush")
        size: Brush size in pixels
        opacity: Brush opacity (0.0 to 1.0)
    """
    params = {}
    if preset:
        params["preset"] = preset
    if size:
        params["size"] = size
    if opacity is not None:
        params["opacity"] = opacity

    _raise_on_error(send_command("set_brush", params))
    return f"Brush set: preset={preset}, size={size}, opacity={opacity}"


@mcp.tool()
def krita_stroke(
    points: list[list[int]],
    size: Annotated[Optional[int], Field(ge=1, le=10000)] = None,
    feather: Annotated[float, Field(ge=0.0)] = 0.0,
    opacity: Annotated[float, Field(ge=0.0, le=1.0)] = 1.0,
) -> str:
    """
    Paint a stroke through a series of points (antialiased, round cap/join).

    Args:
        points: List of [x, y] coordinate pairs, e.g., [[100, 100], [150, 120], [200, 150]]
        size: Stroke width in pixels. Omit to use the current brush size.
        feather: Soft-edge amount (0 = crisp). A few px gives a soft mask boundary.
        opacity: Stroke opacity 0.0-1.0.
    """
    if len(points) < 2:
        return "Error: Need at least 2 points for a stroke"

    params = {"points": points, "feather": feather, "opacity": opacity}
    if size is not None:
        params["size"] = size

    _raise_on_error(send_command("stroke", params))
    return f"Stroke painted with {len(points)} points"


@mcp.tool()
def krita_fill(
    x: int,
    y: int,
    radius: Annotated[int, Field(ge=1, le=10000)] = 50,
    feather: Annotated[float, Field(ge=0.0)] = 0.0,
) -> str:
    """
    Fill a circular area with the current color (antialiased).

    Args:
        x: X coordinate
        y: Y coordinate
        radius: Fill radius in pixels
        feather: Soft-edge amount (0 = crisp). Useful for soft region-mask edges.
    """
    _raise_on_error(send_command("fill", {"x": x, "y": y, "radius": radius, "feather": feather}))
    return f"Filled at ({x}, {y}) with radius {radius}"


@mcp.tool()
def krita_draw_shape(
    shape: ShapeName,
    x: int,
    y: int,
    width: int = 100,
    height: int = 100,
    fill: bool = True,
    stroke: bool = False,
    feather: Annotated[float, Field(ge=0.0)] = 0.0,
    x2: Optional[int] = None,
    y2: Optional[int] = None
) -> str:
    """
    Draw a shape on the canvas (antialiased). Great for region masks: a filled
    rectangle or ellipse defines WHERE a region applies in one call.

    Args:
        shape: Type of shape - "rectangle", "ellipse", or "line"
        x: X coordinate (top-left for shapes, start point for lines)
        y: Y coordinate (top-left for shapes, start point for lines)
        width: Width of shape (ignored for lines if x2/y2 provided)
        height: Height of shape (ignored for lines if x2/y2 provided)
        fill: Whether to fill the shape
        stroke: Whether to draw outline
        feather: Soft-edge amount (0 = crisp). Useful for soft region-mask edges.
        x2: End X for lines (optional)
        y2: End Y for lines (optional)
    """
    params = {
        "shape": shape,
        "x": x,
        "y": y,
        "width": width,
        "height": height,
        "fill": fill,
        "stroke": stroke,
        "feather": feather
    }
    if x2 is not None:
        params["x2"] = x2
    if y2 is not None:
        params["y2"] = y2

    _raise_on_error(send_command("draw_shape", params))
    return f"Drew {shape} at ({x}, {y})"


@mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
def krita_get_canvas(
    mode: CanvasMode = "fast",
    max_dim: Annotated[int, Field(ge=64, le=8192)] = 1024,
) -> Image:
    """
    Look at the current canvas. Returns the image inline so you can see it directly.

    Pick the mode by intent:
      - mode="fast" (DEFAULT): a downscaled JPEG (<= max_dim px). Cheap and quick.
        Use this WHILE drawing/iterating — checking placement, progress, masks.
      - mode="full": the full-resolution PNG. Slower / more tokens. Use this ONLY
        to review the FINAL result once you're done, when detail matters.

    Args:
        mode: "fast" for the drawing loop (default), "full" for final review.
        max_dim: Max longest-side pixels for fast mode (default 1024).
    """
    # Extended timeout — full-res render can take a while on large canvases
    result = _raise_on_error(
        send_command("get_canvas", {"mode": mode, "max_dim": max_dim}, timeout=120.0)
    )
    return _decode_image(result)


@mcp.tool()
def krita_undo() -> str:
    """Undo the last action."""
    _raise_on_error(send_command("undo", {}))
    return "Undone"


@mcp.tool()
def krita_redo() -> str:
    """Redo the last undone action."""
    _raise_on_error(send_command("redo", {}))
    return "Redone"


@mcp.tool(annotations={"destructiveHint": True, "idempotentHint": True})
def krita_clear(color: str = "#1a1a2e") -> str:
    """
    Clear the canvas to a solid color.

    Args:
        color: Color to fill canvas with (default dark blue)
    """
    _raise_on_error(send_command("clear", {"color": color}))
    return f"Canvas cleared to {color}"


@mcp.tool(annotations={"destructiveHint": True, "idempotentHint": True})
def krita_save(path: str) -> str:
    """
    Save the current canvas to a specific file path.

    Args:
        path: Full file path to save to (e.g., "C:/art/my_painting.png")
    """
    # Extended timeout — saving large files can take a while
    _raise_on_error(send_command("save", {"path": path}, timeout=120.0))
    return f"Saved to {path}"


@mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
def krita_get_color_at(x: int, y: int) -> str:
    """
    Sample the color at a specific pixel (eyedropper).

    Args:
        x: X coordinate
        y: Y coordinate
    """
    result = _raise_on_error(send_command("get_color_at", {"x": x, "y": y}))
    return f"Color at ({x}, {y}): {result.get('color', 'unknown')} (R:{result.get('r')}, G:{result.get('g')}, B:{result.get('b')})"


@mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
def krita_list_brushes(
    filter: str = "",
    limit: Annotated[int, Field(ge=1, le=500)] = 20,
) -> str:
    """
    List available brush presets.

    Args:
        filter: Filter brushes by name (partial match)
        limit: Maximum number to return
    """
    result = _raise_on_error(send_command("list_brushes", {"filter": filter, "limit": limit}))

    brushes = result.get("brushes", [])
    if not brushes:
        return "No brushes found matching filter"

    return f"Available brushes ({len(brushes)}):\n" + "\n".join(f"  - {b}" for b in brushes)


@mcp.tool()
def krita_open_file(path: str) -> str:
    """
    Open an existing file in Krita (.kra, .png, .jpg, etc).

    Args:
        path: Full file path to open (e.g., "C:/art/my_painting.kra")
    """
    result = _raise_on_error(send_command("open_file", {"path": path}, timeout=30.0))
    return f"Opened: {result.get('name', 'unknown')} ({result.get('width')}x{result.get('height')})"


@mcp.tool()
def krita_batch(commands: list[dict], review: Optional[str] = None):
    """
    Run several commands in ONE round-trip instead of one call each. Use this
    whenever you'd otherwise chain multiple tool calls — e.g. building a region
    mask with several fills, or "set prompt + switch workspace + generate".

    The projection refreshes once at the end (not per command), and you can
    fetch the resulting canvas in the same call via `review`.

    Args:
        commands: List of {"action": str, "params": {...}} items. `action` is the
            underlying command name (without the "krita_"/"krita_ai_" prefix),
            e.g. "fill", "draw_shape", "stroke", "set_color", "ai_set_prompt",
            "ai_set_workspace", "ai_generate". Example:
              [{"action": "set_color", "params": {"color": "#ffffff"}},
               {"action": "fill", "params": {"x": 100, "y": 100, "radius": 80}},
               {"action": "draw_shape", "params": {"shape": "ellipse", "x": 200, "y": 50, "width": 150, "height": 200}}]
        review: Optional. "fast" or "full" → also returns the final canvas image
            inline. Omit to skip the screenshot.
    """
    result = _raise_on_error(
        send_command("batch", {"commands": commands, "review": review}, timeout=120.0)
    )

    summary = json.dumps(
        {"count": result.get("count"), "results": result.get("results")},
        indent=2, default=str,
    )
    canvas = result.get("canvas")
    if review and isinstance(canvas, dict) and canvas.get("data_b64"):
        return [summary, _decode_image(canvas)]
    return summary


# ----- AI Diffusion (Acly/krita-ai-diffusion) tools -----
# These talk to the AI Diffusion plugin running in the same Krita process via
# the kritamcp bridge. Require the plugin to be installed and enabled in Krita.


def _fmt(result: dict) -> str:
    """Format a JSON-ish result as compact pretty text for the MCP client.

    Raises ToolError on a truthy `error` — the AI status payload includes a
    nullable `error` field for the server connection state, so null is not a
    failure.
    """
    _raise_on_error(result)
    return json.dumps(result, indent=2, default=str)


@mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
def krita_ai_status() -> str:
    """
    Get AI Diffusion plugin status: server connection, active document, workspace,
    current style/strength/seed, queued/executing/finished job counts, current prompt.

    The `model` field reports the active style's resolved architecture for prompt
    formatting: model.architecture is the family (e.g. "sdxl", "flux", "zimage",
    "illu", "qwen", "sd3"), plus checkpoint and loras. Use it to format prompts in
    the right convention (natural language vs danbooru tags) before ai_set_prompt.
    """
    return _fmt(send_command("ai_status"))


@mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
def krita_ai_overview() -> str:
    """
    Full AI Diffusion state in ONE call: status (connection, workspace, style,
    strength, seed, queue) plus all regions, control layers, recent jobs, and
    available styles. Prefer this over chaining ai_status + ai_list_regions +
    ai_list_controls + ai_list_jobs + ai_list_styles when you need the lay of
    the land before or after making changes. The `ai.model` field carries the
    resolved architecture (family/checkpoint/loras) for prompt formatting.
    """
    return _fmt(send_command("ai_overview"))


@mcp.tool(annotations={"idempotentHint": True})
def krita_ai_set_prompt(
    positive: Optional[str] = None,
    negative: Optional[str] = None,
) -> str:
    """
    Set positive and/or negative prompt for the active document.
    Positive is written to the active region (or root if no region selected).
    Negative is always written to the root region.

    Args:
        positive: Positive prompt text. Omit to leave unchanged.
        negative: Negative prompt text. Omit to leave unchanged.
    """
    params = {}
    if positive is not None:
        params["positive"] = positive
    if negative is not None:
        params["negative"] = negative
    if not params:
        return "Error: provide at least one of positive/negative"
    return _fmt(send_command("ai_set_prompt", params))


@mcp.tool(annotations={"idempotentHint": True})
def krita_ai_set_params(
    style: Optional[str] = None,
    strength: Annotated[Optional[float], Field(ge=0.0, le=1.0)] = None,
    seed: Optional[int] = None,
    fixed_seed: Optional[bool] = None,
    batch_count: Annotated[Optional[int], Field(ge=1, le=100)] = None,
) -> str:
    """
    Set generation parameters.

    Args:
        style: Style filename or partial name match (see krita_ai_list_styles).
        strength: 0.0-1.0, denoising strength for img2img / refine workflows.
        seed: Seed value.
        fixed_seed: If True, reuse `seed` for every generation instead of randomizing.
        batch_count: Number of images per generation call.
    """
    params = {}
    for k, v in {
        "style": style,
        "strength": strength,
        "seed": seed,
        "fixed_seed": fixed_seed,
        "batch_count": batch_count,
    }.items():
        if v is not None:
            params[k] = v
    if not params:
        return "Error: provide at least one parameter"
    return _fmt(send_command("ai_set_params", params))


@mcp.tool(annotations={"idempotentHint": True})
def krita_ai_set_workspace(name: Workspace) -> str:
    """
    Switch AI Diffusion workspace.

    Args:
        name: One of "generation", "upscaling", "live", "animation", "custom".
    """
    return _fmt(send_command("ai_set_workspace", {"name": name}))


@mcp.tool(annotations={"openWorldHint": True})
def krita_ai_generate() -> str:
    """
    Trigger image generation in the current workspace. Returns immediately;
    use krita_ai_status or krita_ai_list_jobs to monitor progress.
    """
    return _fmt(send_command("ai_generate"))


@mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
def krita_ai_list_jobs(
    state: JobStateFilter = "all",
    limit: Annotated[int, Field(ge=1, le=200)] = 20,
) -> str:
    """
    List jobs from the AI Diffusion queue, newest first.

    Args:
        state: Filter by state - "all", "queued", "executing", "finished", "cancelled".
        limit: Max number of jobs to return.
    """
    return _fmt(send_command("ai_list_jobs", {"state": state, "limit": limit}))


@mcp.tool()
def krita_ai_apply(
    job_id: Optional[str] = None,
    index: Annotated[int, Field(ge=0)] = 0,
) -> str:
    """
    Apply a generated result to the canvas.

    Args:
        job_id: Job ID to apply. If omitted, applies the latest finished job.
        index: Which result image from the batch (default 0).
    """
    params = {"index": index}
    if job_id is not None:
        params["job_id"] = job_id
    return _fmt(send_command("ai_apply", params))


@mcp.tool(annotations={"idempotentHint": True})
def krita_ai_cancel(active: bool = True, queued: bool = False) -> str:
    """
    Cancel running or queued generation jobs.

    Args:
        active: Cancel the currently executing job.
        queued: Cancel all queued (not yet started) jobs.
    """
    return _fmt(send_command("ai_cancel", {"active": active, "queued": queued}))


@mcp.tool(annotations={"idempotentHint": True})
def krita_ai_save_preview(
    job_id: str,
    index: Annotated[int, Field(ge=0)] = 0,
    filename: str = "",
) -> str:
    """
    Save a generated result image (without applying it) so it can be reviewed.

    Args:
        job_id: Job ID from krita_ai_list_jobs.
        index: Which result image from the batch (default 0).
        filename: Output filename. Defaults to preview_<jobid>_<index>.png in the
                  kritamcp output dir (~/krita-mcp-output).
    """
    params = {"job_id": job_id, "index": index}
    if filename:
        params["filename"] = filename
    return _fmt(send_command("ai_save_preview", params))


@mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
def krita_ai_list_styles(
    filter: str = "",
    limit: Annotated[int, Field(ge=1, le=200)] = 30,
) -> str:
    """
    List available AI Diffusion style presets.

    Args:
        filter: Substring filter on style name or filename.
        limit: Max styles to return.
    """
    return _fmt(send_command("ai_list_styles", {"filter": filter, "limit": limit}))


@mcp.tool()
def krita_ai_create_region(positive: str = "", group: bool = False) -> str:
    """
    Create a new AI Diffusion region linked to a fresh paint layer, set its prompt,
    and activate it so subsequent painting tools (krita_fill, krita_draw_shape,
    krita_stroke) draw the region's mask.

    The region's content prompt is `positive`. Paint a silhouette / mask on the
    activated layer to define WHERE this region applies on the canvas.

    Args:
        positive: Positive prompt for this region (e.g., "a golden retriever, sitting").
        group: If True, create a layer GROUP for the region. Default False = paint layer only.
    """
    return _fmt(send_command("ai_create_region", {"positive": positive, "group": group}))


@mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
def krita_ai_list_regions() -> str:
    """
    List all regions for the active document: root prompt (positive/negative) plus
    each child region with its index, prompt, linked layers, and active flag.
    """
    return _fmt(send_command("ai_list_regions"))


@mcp.tool(annotations={"idempotentHint": True})
def krita_ai_select_region(
    index: Annotated[Optional[int], Field(ge=0)] = None,
    layer_id: Optional[str] = None,
) -> str:
    """
    Select an existing region as the active one. Pass nothing to select root region.

    Args:
        index: Region index from krita_ai_list_regions.
        layer_id: Layer id linked to the region (alternative to index).
    """
    params = {}
    if index is not None:
        params["index"] = index
    if layer_id is not None:
        params["layer_id"] = layer_id
    return _fmt(send_command("ai_select_region", params))


@mcp.tool(annotations={"destructiveHint": True})
def krita_ai_remove_region(index: Annotated[int, Field(ge=0)]) -> str:
    """
    Remove a region by index.

    Args:
        index: Region index from krita_ai_list_regions.
    """
    return _fmt(send_command("ai_remove_region", {"index": index}))


@mcp.tool()
def krita_ai_add_control(
    mode: ControlModeName = "scribble",
    layer_id: Optional[str] = None,
    strength: Annotated[Optional[float], Field(ge=0.0, le=2.0)] = None,
    region_index: Optional[int] = None,
) -> str:
    """
    Add a ControlNet / IP-Adapter Control Layer to root (default) or a region.
    The control sources its image from `layer_id` if provided, otherwise from
    the currently active Krita layer. ControlNet modes enforce silhouette /
    structure; IP-Adapter modes extract reference style or composition.

    Modes:
      Structural (ControlNet — enforces silhouette / structure):
        scribble, line_art, soft_edge, canny_edge, depth, normal,
        pose, segmentation, blur, stencil
      IP-Adapter (extracts from image):
        reference, style, composition, face

    Args:
        mode: control mode name (see above).
        layer_id: optional UUID of source layer. Omit to use active layer.
        strength: optional 0.0-2.0 control influence. Omit to use preset default.
        region_index: optional region index. Omit / -1 = root (applies to whole image).
    """
    params = {"mode": mode}
    if layer_id is not None:
        params["layer_id"] = layer_id
    if strength is not None:
        params["strength"] = strength
    if region_index is not None:
        params["region_index"] = region_index
    return _fmt(send_command("ai_add_control", params))


@mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
def krita_ai_list_controls(region_index: Optional[int] = None) -> str:
    """
    List Control Layers. Without region_index, lists controls across root
    plus all regions; with region_index (use -1 for root) lists that scope only.
    """
    params = {}
    if region_index is not None:
        params["region_index"] = region_index
    return _fmt(send_command("ai_list_controls", params))


@mcp.tool(annotations={"destructiveHint": True})
def krita_ai_remove_control(
    index: Annotated[int, Field(ge=0)],
    region_index: Optional[int] = None,
) -> str:
    """
    Remove a Control Layer by index from root (default) or a region.

    Args:
        index: control layer index from krita_ai_list_controls.
        region_index: optional region index. Omit / -1 = root.
    """
    params = {"index": index}
    if region_index is not None:
        params["region_index"] = region_index
    return _fmt(send_command("ai_remove_control", params))


@mcp.tool(annotations={"idempotentHint": True})
def krita_ai_set_control(
    index: Annotated[int, Field(ge=0)],
    mode: Optional[ControlModeName] = None,
    strength: Annotated[Optional[float], Field(ge=0.0, le=2.0)] = None,
    region_index: Optional[int] = None,
) -> str:
    """
    Update mode and/or strength of an existing Control Layer.

    Args:
        index: control layer index from krita_ai_list_controls.
        mode: optional new mode (see krita_ai_add_control for valid modes).
        strength: optional new strength 0.0-2.0.
        region_index: optional region index. Omit / -1 = root.
    """
    params = {"index": index}
    if mode is not None:
        params["mode"] = mode
    if strength is not None:
        params["strength"] = strength
    if region_index is not None:
        params["region_index"] = region_index
    return _fmt(send_command("ai_set_control", params))


# ----- Layers, masks and SAM: thin wrappers over the `kri` CLI -----

SelectMode = Literal["replace", "add", "subtract", "intersect"]
PlaceSpec = Annotated[Optional[str], Field(
    description="Where the layer goes: 'top', 'bottom', 'above:REF', "
                "'below:REF' or 'into:GROUP'. Omit = above the active layer.")]
LayerRef = Annotated[str, Field(
    description="Layer: unique name, 'Group/Sub/Layer' path or id from "
                "krita_layer_list")]


KRI_BIN = os.environ.get("KRI_BIN", "kri")


def _kri(*args: str, timeout: float = 180.0) -> str:
    """Run `kri ARGS`; its JSON stdout is the result, failures raise."""
    import subprocess
    try:
        r = subprocess.run([KRI_BIN, *args], capture_output=True, text=True,
                           timeout=timeout)
    except subprocess.TimeoutExpired:
        raise ToolError(f"kri {' '.join(args[:2])} timed out after {timeout:.0f}s")
    except OSError as e:
        raise ToolError(f"cannot run {KRI_BIN!r}: {e}")
    if r.returncode != 0:
        raise ToolError((r.stdout + r.stderr).strip() or f"kri {args[0]} failed")
    return r.stdout.strip()


def _opts(**flags) -> list[str]:
    """--flag value for set values; True -> bare --flag; False/None skipped."""
    out = []
    for k, v in flags.items():
        flag = "--" + k.replace("_", "-")
        if v is True:
            out.append(flag)
        elif v is not None and v is not False:
            out += [flag, str(v)]
    return out


def _place(place: Optional[str]) -> list[str]:
    if not place:
        return []
    key, _, ref = place.partition(":")
    key = key.strip().lower()
    if key in ("top", "bottom") and not ref:
        return [f"--{key}"]
    if key in ("above", "below", "into") and ref.strip():
        return [f"--{key}", ref.strip()]
    raise ToolError(f"Bad place '{place}': use top, bottom, above:REF, "
                    f"below:REF or into:GROUP")


@mcp.tool()
def krita_select_sam(
    text: Optional[str] = None,
    points: Optional[list[list[int]]] = None,
    neg_points: Optional[list[list[int]]] = None,
    box: Optional[list[int]] = None,
    instance: Optional[int] = None,
    threshold: Optional[float] = None,
    list_only: bool = False,
    mode: SelectMode = "replace",
    to_layer: Optional[str] = None,
    color: Optional[str] = None,
    blend: Optional[str] = None,
    place: PlaceSpec = None,
    save_mask: Optional[str] = None,
) -> str:
    """AI selection with SAM 3 (krita-autoselect daemon; if it is down, the
    AI Select Krita plugin is asked to start it).

    text: concept prompt ("the red car") -> every matching instance.
    points: [[x,y],...] clicks on ONE object; neg_points exclude parts.
    box: [x, y, w, h] around the object. instance: pick one (0 = best),
    default union. list_only: just return instances (score + bbox).
    to_layer: also create a paint layer with that name filled with `color`
    (default white) inside the selection, with `blend` and `place`;
    save_mask stores the selection as a selection mask on it.
    The first call loads the model (can take ~30-60 s).
    """
    args = ["select", "sam"] + ([text] if text else [])
    for x, y in points or []:
        args += ["--point", f"{x},{y}"]
    for x, y in neg_points or []:
        args += ["--neg-point", f"{x},{y}"]
    if box:
        args += ["--box", *map(str, box)]
    args += _opts(instance=instance, threshold=threshold, list=list_only,
                  mode=mode, to_layer=to_layer, color=color, blend=blend,
                  save_mask=save_mask) + _place(place)
    return _kri(*args, timeout=420)


@mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
def krita_layer_list() -> str:
    """Layer tree, top to bottom: name, path, type, id, blend, opacity (%),
    visible, locked, alpha_locked, inherit_alpha, active, bounds; plus the
    document size and colour space."""
    return _kri("layer", "list")


@mcp.tool()
def krita_layer_new(
    name: str,
    type: Literal["paint", "group"] = "paint",
    color: Optional[str] = None,
    in_selection: bool = False,
    blend: Optional[str] = None,
    opacity: Annotated[Optional[float], Field(ge=0, le=100)] = None,
    hidden: bool = False,
    place: PlaceSpec = None,
) -> str:
    """New paint/group layer, made active. color (#RRGGBB) fills it;
    in_selection fills only inside the current selection. blend: Krita id or
    alias (multiply, screen, soft-light...)."""
    return _kri("layer", "new", name, *_opts(
        type=type, color=color, selection=in_selection, blend=blend,
        opacity=opacity, hidden=hidden), *_place(place))


@mcp.tool()
def krita_layer_import(
    path: str,
    name: Optional[str] = None,
    x: Optional[int] = None,
    y: Optional[int] = None,
    blend: Optional[str] = None,
    opacity: Annotated[Optional[float], Field(ge=0, le=100)] = None,
    hidden: bool = False,
    place: PlaceSpec = None,
) -> str:
    """PNG/JPG (alpha kept) into a new paint layer at x, y. Krita reads the
    file from disk. On a grayscale document colours turn gray: call
    krita_colorspace("RGBA", "U8") first."""
    return _kri("layer", "import", path, *_opts(
        name=name, x=x, y=y, blend=blend, opacity=opacity, hidden=hidden),
        *_place(place))


@mcp.tool()
def krita_layer_export(ref: LayerRef, path: str, crop: bool = False) -> str:
    """Write a layer's pixels to a PNG with alpha (canvas-sized; crop = to
    its bounds). Selection masks export as grayscale."""
    return _kri("layer", "export", ref, path, *_opts(crop=crop))


@mcp.tool()
def krita_layer_set(
    ref: LayerRef,
    name: Optional[str] = None,
    blend: Optional[str] = None,
    opacity: Annotated[Optional[float], Field(ge=0, le=100)] = None,
    visible: Optional[bool] = None,
    locked: Optional[bool] = None,
    alpha_locked: Optional[bool] = None,
    inherit_alpha: Optional[bool] = None,
) -> str:
    """Change layer properties; only the given ones change. inherit_alpha
    clips the layer to the layers below it."""
    args = ["layer", "set", ref] + _opts(name=name, blend=blend, opacity=opacity)
    for flag, value, on, off in (
            ("visible", visible, "--visible", "--hidden"),
            ("locked", locked, "--lock", "--unlock"),
            ("alpha_locked", alpha_locked, "--alpha-lock", "--no-alpha-lock"),
            ("inherit_alpha", inherit_alpha, "--inherit-alpha", "--no-inherit-alpha")):
        if value is not None:
            args.append(on if value else off)
    return _kri(*args)


@mcp.tool()
def krita_layer_move(ref: LayerRef, place: PlaceSpec) -> str:
    """Move a layer (children and masks travel along)."""
    if not place:
        raise ToolError("place is required")
    return _kri("layer", "move", ref, *_place(place))


@mcp.tool()
def krita_layer_op(
    op: Literal["select", "duplicate", "delete"],
    refs: list[str],
    name: Optional[str] = None,
    place: PlaceSpec = None,
) -> str:
    """select: make refs[0] the active layer. duplicate: copy refs[0]
    (optional new name / place). delete: remove every ref."""
    if op == "delete":
        return _kri("layer", "delete", *refs)
    if op == "select":
        return _kri("layer", "select", refs[0])
    return _kri("layer", "duplicate", refs[0], *_opts(name=name), *_place(place))


@mcp.tool()
def krita_layer_group(name: str, refs: list[str]) -> str:
    """Wrap layers in a new group where the topmost of them was."""
    return _kri("layer", "group", name, *refs)


@mcp.tool()
def krita_layer_fill(ref: LayerRef, color: str, in_selection: bool = False) -> str:
    """Fill a paint layer with a colour (whole canvas, or only inside the
    selection). Honors alpha lock."""
    return _kri("layer", "fill", ref, color, *_opts(selection=in_selection))


@mcp.tool(annotations={"readOnlyHint": True, "idempotentHint": True})
def krita_layer_blends() -> str:
    """Valid blending-mode ids by category, plus accepted aliases."""
    return _kri("layer", "blends")


@mcp.tool()
def krita_mask(
    op: Literal["from_layer", "save", "load"],
    name: str,
    on: Optional[str] = None,
    mode: SelectMode = "replace",
) -> str:
    """Selections <-> layers. from_layer: select the alpha of layer `name`.
    save: store the current selection as selection mask `name` on layer `on`
    (default active). load: restore saved mask `name` as the selection."""
    if op == "from_layer":
        return _kri("select", "from-layer", name, "--mode", mode)
    if op == "save":
        return _kri("select", "save", name, *_opts(on=on))
    return _kri("select", "load", name, "--mode", mode)


@mcp.tool()
def krita_sam(action: Literal["status", "start", "stop"] = "status") -> str:
    """SAM daemon behind krita_select_sam, via the AI Select Krita plugin:
    status, start (waits until it answers) or stop (only one AI Select
    started)."""
    return _kri("sam", action, timeout=240)


@mcp.tool()
def krita_colorspace(model: Optional[str] = None, depth: Optional[str] = None) -> str:
    """Show the image colour space, or convert it (e.g. "RGBA", "U8")."""
    return _kri("colorspace", *([model] if model else []),
                *([depth] if depth else []), timeout=120)


# ----- Tools declared by kri extensions -----

import logging
import subprocess

from fastmcp.tools import Tool
try:
    from fastmcp.tools.tool import ToolResult
except ImportError:
    from fastmcp.tools import ToolResult
from mcp.types import TextContent, ToolAnnotations

log = logging.getLogger("krita-mcp")


def ext_tool_argv(ext: str, tool: dict, arguments: dict) -> list[str]:
    """Same rule as kri's tool_argv (cli/kri_extensions.py): EXT CMD…
    POSITIONALS --flag value…; boolean true = bare --flag (false omitted),
    array = --flag v1 v2, '_' in the name becomes '-'."""
    argv = [ext, *tool["command"]]
    params = tool.get("params") or {}
    positional = tool.get("positional") or []
    for name in positional:
        value = arguments.get(name)
        if value is None:
            continue
        argv += [str(v) for v in value] if isinstance(value, list) else [str(value)]
    for name in list(params) + [k for k in arguments if k not in params]:
        value = arguments.get(name)
        if name in positional or value is None or value is False:
            continue
        flag = "--" + name.replace("_", "-")
        if value is True:
            argv.append(flag)
        elif isinstance(value, list):
            argv += [flag, *map(str, value)]
        else:
            argv += [flag, str(value)]
    return argv


class ExtensionTool(Tool):
    """An MCP tool backed by `kri <ext> <command…>` (schema from the manifest)."""
    ext: str
    spec: dict

    async def run(self, arguments: dict) -> ToolResult:
        import anyio
        argv = ext_tool_argv(self.ext, self.spec, arguments)
        timeout = float(self.spec.get("timeout_s") or 600)
        text = await anyio.to_thread.run_sync(
            lambda: _kri(*argv, timeout=timeout))
        return ToolResult(content=[TextContent(type="text", text=text)])


def _tool_names(server: FastMCP) -> set:
    import asyncio
    try:  # list_tools is async; at import time there is no running loop
        return {t.name for t in asyncio.run(server.list_tools())}
    except Exception:
        return set()


def load_extension_tools(server: FastMCP = mcp) -> list[str]:
    """Register every tool from `kri ext mcp-tools`. A kri without `ext`
    (older than 0.5.0), a missing kri or a name clash only log a warning."""
    try:
        r = subprocess.run([KRI_BIN, "ext", "mcp-tools"], capture_output=True,
                           text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as e:
        log.warning("kri ext mcp-tools failed (%s): no extension tools", e)
        return []
    if r.returncode != 0:
        log.warning("kri ext mcp-tools exited %s (kri too old for "
                    "extensions?): %s", r.returncode, r.stderr.strip()[:300])
        return []
    try:
        listing = json.loads(r.stdout)
    except ValueError:
        log.warning("kri ext mcp-tools: not JSON, no extension tools")
        return []
    for skipped in listing.get("skipped") or []:
        log.warning("extension tool skipped: %s", skipped)
    registered = []
    existing = _tool_names(server)
    for spec in listing.get("tools") or []:
        name = spec.get("name")
        if not name or name in registered or name in existing:
            log.warning("extension tool %r clashes with an existing tool: "
                        "skipped", name)
            continue
        annotations = spec.get("annotations") or {}
        server.add_tool(ExtensionTool(
            name=name,
            description=f"{spec.get('description', '')} (kri extension "
                        f"{spec['extension']})",
            parameters=spec.get("input_schema") or {"type": "object",
                                                     "properties": {}},
            annotations=ToolAnnotations(**annotations) if annotations else None,
            ext=spec["extension"], spec=spec))
        registered.append(name)
    return registered


EXTENSION_TOOLS = load_extension_tools()


if __name__ == "__main__":
    mcp.run()
