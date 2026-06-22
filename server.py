"""
Krita MCP Server
Bridge between Claude (or any MCP client) and Krita painting application.

Uses FastMCP to expose Krita painting tools over the Model Context Protocol,
communicating with a Krita plugin via HTTP.
"""

from fastmcp import FastMCP
import httpx
import os
from typing import Optional

# Configuration
KRITA_URL = os.environ.get("KRITA_URL", "http://localhost:5678")

mcp = FastMCP("krita-mcp")

# Persistent HTTP client — reuses the connection across commands instead of
# paying a fresh TCP handshake on every call.
_client = httpx.Client(timeout=30.0)


def send_command(action: str, params: dict = None, timeout: float = 30.0) -> dict:
    """Send command to Krita plugin and return result."""
    if params is None:
        params = {}

    try:
        response = _client.post(
            KRITA_URL,
            json={"action": action, "params": params},
            timeout=timeout,
        )
        return response.json()
    except httpx.ConnectError:
        return {"error": "Cannot connect to Krita. Is Krita running with the MCP plugin enabled?"}
    except Exception as e:
        return {"error": str(e)}


@mcp.tool()
def krita_health() -> str:
    """Check if Krita is running and the MCP plugin is active."""
    try:
        response = _client.get(f"{KRITA_URL}/health", timeout=5.0)
        data = response.json()
        return f"Krita is running. Plugin: {data.get('plugin', 'unknown')}"
    except:
        return "Cannot connect to Krita. Make sure Krita is running with the MCP plugin enabled."


@mcp.tool()
def krita_new_canvas(
    width: int = 800,
    height: int = 600,
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
    result = send_command("new_canvas", {
        "width": width,
        "height": height,
        "name": name,
        "background": background
    })

    if "error" in result:
        return f"Error: {result['error']}"
    return f"Created canvas: {width}x{height}, background: {background}"


@mcp.tool()
def krita_set_color(color: str) -> str:
    """
    Set the foreground (paint) color.

    Args:
        color: Hex color code (e.g., "#ff6b6b", "#b8a9c9")
    """
    result = send_command("set_color", {"color": color})

    if "error" in result:
        return f"Error: {result['error']}"
    return f"Color set to {color}"


@mcp.tool()
def krita_set_brush(
    preset: Optional[str] = None,
    size: Optional[int] = None,
    opacity: Optional[float] = None
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

    result = send_command("set_brush", params)

    if "error" in result:
        return f"Error: {result['error']}"
    return f"Brush set: preset={preset}, size={size}, opacity={opacity}"


@mcp.tool()
def krita_stroke(points: list[list[int]], pressure: float = 1.0) -> str:
    """
    Paint a stroke through a series of points.

    Args:
        points: List of [x, y] coordinate pairs, e.g., [[100, 100], [150, 120], [200, 150]]
        pressure: Brush pressure (0.0 to 1.0, affects stroke thickness/opacity)
    """
    if len(points) < 2:
        return "Error: Need at least 2 points for a stroke"

    result = send_command("stroke", {
        "points": points,
        "pressure": pressure
    })

    if "error" in result:
        return f"Error: {result['error']}"
    return f"Stroke painted with {len(points)} points"


@mcp.tool()
def krita_fill(x: int, y: int, radius: int = 50) -> str:
    """
    Fill an area with current color (paints a filled circle at the point).

    Args:
        x: X coordinate
        y: Y coordinate
        radius: Fill radius in pixels
    """
    result = send_command("fill", {"x": x, "y": y, "radius": radius})

    if "error" in result:
        return f"Error: {result['error']}"
    return f"Filled at ({x}, {y}) with radius {radius}"


@mcp.tool()
def krita_draw_shape(
    shape: str,
    x: int,
    y: int,
    width: int = 100,
    height: int = 100,
    fill: bool = True,
    stroke: bool = False,
    x2: Optional[int] = None,
    y2: Optional[int] = None
) -> str:
    """
    Draw a shape on the canvas.

    Args:
        shape: Type of shape - "rectangle", "ellipse", or "line"
        x: X coordinate (top-left for shapes, start point for lines)
        y: Y coordinate (top-left for shapes, start point for lines)
        width: Width of shape (ignored for lines if x2/y2 provided)
        height: Height of shape (ignored for lines if x2/y2 provided)
        fill: Whether to fill the shape
        stroke: Whether to draw outline
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
        "stroke": stroke
    }
    if x2 is not None:
        params["x2"] = x2
    if y2 is not None:
        params["y2"] = y2

    result = send_command("draw_shape", params)

    if "error" in result:
        return f"Error: {result['error']}"
    return f"Drew {shape} at ({x}, {y})"


@mcp.tool()
def krita_get_canvas(filename: str = "canvas.png") -> str:
    """
    Export current canvas to a PNG file and return the path.
    Use this to see your painting progress.

    Args:
        filename: Output filename (saved to configured output directory)
    """
    # Extended timeout — canvas export can take a while on large canvases
    result = send_command("get_canvas", {"filename": filename}, timeout=120.0)

    if "error" in result:
        return f"Error: {result['error']}"

    path = result.get("path", "")
    return f"Canvas saved to: {path}"


@mcp.tool()
def krita_undo() -> str:
    """Undo the last action."""
    result = send_command("undo", {})

    if "error" in result:
        return f"Error: {result['error']}"
    return "Undone"


@mcp.tool()
def krita_redo() -> str:
    """Redo the last undone action."""
    result = send_command("redo", {})

    if "error" in result:
        return f"Error: {result['error']}"
    return "Redone"


@mcp.tool()
def krita_clear(color: str = "#1a1a2e") -> str:
    """
    Clear the canvas to a solid color.

    Args:
        color: Color to fill canvas with (default dark blue)
    """
    result = send_command("clear", {"color": color})

    if "error" in result:
        return f"Error: {result['error']}"
    return f"Canvas cleared to {color}"


@mcp.tool()
def krita_save(path: str) -> str:
    """
    Save the current canvas to a specific file path.

    Args:
        path: Full file path to save to (e.g., "C:/art/my_painting.png")
    """
    # Extended timeout — saving large files can take a while
    result = send_command("save", {"path": path}, timeout=120.0)

    if "error" in result:
        return f"Error: {result['error']}"
    return f"Saved to {path}"


@mcp.tool()
def krita_get_color_at(x: int, y: int) -> str:
    """
    Sample the color at a specific pixel (eyedropper).

    Args:
        x: X coordinate
        y: Y coordinate
    """
    result = send_command("get_color_at", {"x": x, "y": y})

    if "error" in result:
        return f"Error: {result['error']}"
    return f"Color at ({x}, {y}): {result.get('color', 'unknown')} (R:{result.get('r')}, G:{result.get('g')}, B:{result.get('b')})"


@mcp.tool()
def krita_list_brushes(filter: str = "", limit: int = 20) -> str:
    """
    List available brush presets.

    Args:
        filter: Filter brushes by name (partial match)
        limit: Maximum number to return
    """
    result = send_command("list_brushes", {"filter": filter, "limit": limit})

    if "error" in result:
        return f"Error: {result['error']}"

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
    result = send_command("open_file", {"path": path}, timeout=30.0)

    if "error" in result:
        return f"Error: {result['error']}"

    return f"Opened: {result.get('name', 'unknown')} ({result.get('width')}x{result.get('height')})"


# ----- AI Diffusion (Acly/krita-ai-diffusion) tools -----
# These talk to the AI Diffusion plugin running in the same Krita process via
# the kritamcp bridge. Require the plugin to be installed and enabled in Krita.

import json as _json


def _fmt(result: dict) -> str:
    """Format a JSON-ish result as compact pretty text for the MCP client.

    Only treats `error` as a failure when it has a truthy value — the AI status
    payload includes a nullable `error` field for the server connection state.
    """
    err = result.get("error")
    if err:
        return f"Error: {err}"
    return _json.dumps(result, indent=2, default=str)


@mcp.tool()
def krita_ai_status() -> str:
    """
    Get AI Diffusion plugin status: server connection, active document, workspace,
    current style/strength/seed, queued/executing/finished job counts, current prompt.
    """
    return _fmt(send_command("ai_status"))


@mcp.tool()
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


@mcp.tool()
def krita_ai_set_params(
    style: Optional[str] = None,
    strength: Optional[float] = None,
    seed: Optional[int] = None,
    fixed_seed: Optional[bool] = None,
    batch_count: Optional[int] = None,
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


@mcp.tool()
def krita_ai_set_workspace(name: str) -> str:
    """
    Switch AI Diffusion workspace.

    Args:
        name: One of "generation", "upscaling", "live", "animation", "custom".
    """
    return _fmt(send_command("ai_set_workspace", {"name": name}))


@mcp.tool()
def krita_ai_generate() -> str:
    """
    Trigger image generation in the current workspace. Returns immediately;
    use krita_ai_status or krita_ai_list_jobs to monitor progress.
    """
    return _fmt(send_command("ai_generate"))


@mcp.tool()
def krita_ai_list_jobs(state: str = "all", limit: int = 20) -> str:
    """
    List jobs from the AI Diffusion queue, newest first.

    Args:
        state: Filter by state - "all", "queued", "executing", "finished", "cancelled".
        limit: Max number of jobs to return.
    """
    return _fmt(send_command("ai_list_jobs", {"state": state, "limit": limit}))


@mcp.tool()
def krita_ai_apply(job_id: Optional[str] = None, index: int = 0) -> str:
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


@mcp.tool()
def krita_ai_cancel(active: bool = True, queued: bool = False) -> str:
    """
    Cancel running or queued generation jobs.

    Args:
        active: Cancel the currently executing job.
        queued: Cancel all queued (not yet started) jobs.
    """
    return _fmt(send_command("ai_cancel", {"active": active, "queued": queued}))


@mcp.tool()
def krita_ai_save_preview(job_id: str, index: int = 0, filename: str = "") -> str:
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


@mcp.tool()
def krita_ai_list_styles(filter: str = "", limit: int = 30) -> str:
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


@mcp.tool()
def krita_ai_list_regions() -> str:
    """
    List all regions for the active document: root prompt (positive/negative) plus
    each child region with its index, prompt, linked layers, and active flag.
    """
    return _fmt(send_command("ai_list_regions"))


@mcp.tool()
def krita_ai_select_region(
    index: Optional[int] = None,
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


@mcp.tool()
def krita_ai_remove_region(index: int) -> str:
    """
    Remove a region by index.

    Args:
        index: Region index from krita_ai_list_regions.
    """
    return _fmt(send_command("ai_remove_region", {"index": index}))


@mcp.tool()
def krita_ai_add_control(
    mode: str = "scribble",
    layer_id: Optional[str] = None,
    strength: Optional[float] = None,
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


@mcp.tool()
def krita_ai_list_controls(region_index: Optional[int] = None) -> str:
    """
    List Control Layers. Without region_index, lists controls across root
    plus all regions; with region_index (use -1 for root) lists that scope only.
    """
    params = {}
    if region_index is not None:
        params["region_index"] = region_index
    return _fmt(send_command("ai_list_controls", params))


@mcp.tool()
def krita_ai_remove_control(index: int, region_index: Optional[int] = None) -> str:
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


@mcp.tool()
def krita_ai_set_control(
    index: int,
    mode: Optional[str] = None,
    strength: Optional[float] = None,
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


if __name__ == "__main__":
    mcp.run()
