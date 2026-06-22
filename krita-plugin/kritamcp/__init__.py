"""
Krita MCP Bridge - HTTP server for external paint commands in Krita
Allows Claude (or any MCP client) to paint by sending commands to this plugin.
"""

from krita import *
from PyQt5.QtCore import (
    QTimer, QThread, pyqtSignal, QObject, Qt, QPointF, QRectF, QUuid,
)
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import QMessageBox
import json
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
import os

# Configuration - customize these as needed
SERVER_PORT = 5678
CANVAS_OUTPUT_DIR = os.path.expanduser("~/krita-mcp-output")

class CommandQueue:
    """Thread-safe command queue for passing commands from HTTP thread to main thread.

    Event-driven: each command carries its own threading.Event so the HTTP
    worker thread blocks on exactly its result and is woken the instant the
    main thread finishes — no polling, no shared-event races.
    """
    def __init__(self):
        self.queue = []
        self.results = {}
        self.events = {}  # command_id -> threading.Event
        self.lock = threading.Lock()

    def push(self, command_id, command):
        """Enqueue a command and return the Event to wait on for its result."""
        ev = threading.Event()
        with self.lock:
            self.queue.append((command_id, command))
            self.events[command_id] = ev
        return ev

    def pop(self):
        with self.lock:
            if self.queue:
                return self.queue.pop(0)
            return None

    def set_result(self, command_id, result):
        with self.lock:
            self.results[command_id] = result
            ev = self.events.get(command_id)
        if ev is not None:
            ev.set()

    def get_result(self, command_id, ev, timeout=120):
        """Block on this command's Event until the main thread sets its result.

        The default timeout of 120s is important — canvas export and save
        operations can take a long time on large canvases. The MCP server's
        send_command() timeout must match or exceed this value.
        """
        signalled = ev.wait(timeout)
        with self.lock:
            self.events.pop(command_id, None)
            result = self.results.pop(command_id, None)
        if not signalled:
            return {"error": "Timeout waiting for command execution"}
        if result is None:
            return {"error": "Command produced no result"}
        return result

# Global command queue
command_queue = CommandQueue()
command_counter = 0


class CommandDispatcher(QObject):
    """Lives on the main (GUI) thread. The HTTP worker emits `pushed` after
    enqueuing a command; Qt delivers it as a queued slot call on the main
    thread, so commands are processed the instant they arrive instead of
    waiting for the next timer tick."""
    pushed = pyqtSignal()


# Global dispatcher — assigned in createActions on the main thread.
dispatcher = None

class PaintRequestHandler(BaseHTTPRequestHandler):
    """HTTP request handler for paint commands."""

    def log_message(self, format, *args):
        # Suppress HTTP logging
        pass

    def send_json_response(self, data, status=200):
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def do_GET(self):
        """Handle GET requests - mainly for health check."""
        parsed = urlparse(self.path)

        if parsed.path == '/health':
            self.send_json_response({"status": "ok", "plugin": "kritamcp"})
        elif parsed.path == '/info':
            self.send_json_response({
                "status": "ok",
                "canvas_dir": CANVAS_OUTPUT_DIR,
                "commands": [
                    "new_canvas", "set_color", "set_brush", "stroke",
                    "fill", "draw_shape", "get_canvas", "undo", "redo",
                    "clear", "save", "get_color_at", "list_brushes"
                ]
            })
        else:
            self.send_json_response({"error": "Unknown endpoint"}, 404)

    def do_POST(self):
        """Handle POST requests - paint commands."""
        global command_counter

        content_length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_length).decode('utf-8')

        try:
            command = json.loads(body)
        except json.JSONDecodeError:
            self.send_json_response({"error": "Invalid JSON"}, 400)
            return

        # Assign command ID and queue it
        command_counter += 1
        command_id = command_counter
        ev = command_queue.push(command_id, command)

        # Wake the main thread immediately (queued signal) instead of waiting
        # for the fallback timer tick.
        if dispatcher is not None:
            dispatcher.pushed.emit()

        # Block on this command's own event until the main thread sets a result.
        result = command_queue.get_result(command_id, ev)

        if "error" in result:
            self.send_json_response(result, 500)
        else:
            self.send_json_response(result)


class ServerThread(QThread):
    """Thread to run HTTP server without blocking Krita UI."""

    def __init__(self, port):
        super().__init__()
        self.port = port
        self.server = None

    def run(self):
        self.server = HTTPServer(('localhost', self.port), PaintRequestHandler)
        self.server.serve_forever()

    def stop(self):
        if self.server:
            self.server.shutdown()


class KritaMCPExtension(Extension):
    """Main Krita extension class."""

    def __init__(self, parent):
        super().__init__(parent)
        self.server_thread = None
        self.timer = None
        self.current_brush_size = 20
        self.current_opacity = 1.0
        self._suppress_refresh = False

    def setup(self):
        """Called when extension is loaded."""
        pass

    def createActions(self, window):
        """Called when a new window is created."""
        # Ensure output directory exists
        os.makedirs(CANVAS_OUTPUT_DIR, exist_ok=True)

        # Start HTTP server
        if self.server_thread is None:
            self.server_thread = ServerThread(SERVER_PORT)
            self.server_thread.start()
            print(f"[KritaMCP] HTTP server started on port {SERVER_PORT}")

        # Dispatcher: HTTP worker emits `pushed`, Qt delivers it as a queued
        # slot call on this (main) thread → commands run the instant they land.
        global dispatcher
        if dispatcher is None:
            dispatcher = CommandDispatcher()
            dispatcher.pushed.connect(self.process_commands, Qt.QueuedConnection)

        # Low-frequency fallback timer in case a signal is ever missed (e.g.
        # a command queued before the dispatcher was wired up). Not the hot path.
        if self.timer is None:
            self.timer = QTimer()
            self.timer.timeout.connect(self.process_commands)
            self.timer.start(250)

    def process_commands(self):
        """Drain and execute every queued command on the main thread."""
        while True:
            item = command_queue.pop()
            if item is None:
                return
            command_id, command = item
            result = self.execute_command(command)
            command_queue.set_result(command_id, result)

    def _maybe_refresh(self, doc):
        """Refresh the projection unless we're inside a batch (which refreshes
        once at the end)."""
        if doc is not None and not self._suppress_refresh:
            doc.refreshProjection()

    def execute_command(self, command):
        """Execute a paint command and return result."""
        try:
            action = command.get("action")
            params = command.get("params", {})

            if action == "new_canvas":
                return self.cmd_new_canvas(params)
            elif action == "set_color":
                return self.cmd_set_color(params)
            elif action == "set_brush":
                return self.cmd_set_brush(params)
            elif action == "stroke":
                return self.cmd_stroke(params)
            elif action == "fill":
                return self.cmd_fill(params)
            elif action == "draw_shape":
                return self.cmd_draw_shape(params)
            elif action == "get_canvas":
                return self.cmd_get_canvas(params)
            elif action == "undo":
                return self.cmd_undo(params)
            elif action == "redo":
                return self.cmd_redo(params)
            elif action == "clear":
                return self.cmd_clear(params)
            elif action == "save":
                return self.cmd_save(params)
            elif action == "get_color_at":
                return self.cmd_get_color_at(params)
            elif action == "list_brushes":
                return self.cmd_list_brushes(params)
            elif action == "open_file":
                return self.cmd_open_file(params)
            elif action == "ai_status":
                return self.cmd_ai_status(params)
            elif action == "ai_set_prompt":
                return self.cmd_ai_set_prompt(params)
            elif action == "ai_set_params":
                return self.cmd_ai_set_params(params)
            elif action == "ai_set_workspace":
                return self.cmd_ai_set_workspace(params)
            elif action == "ai_generate":
                return self.cmd_ai_generate(params)
            elif action == "ai_list_jobs":
                return self.cmd_ai_list_jobs(params)
            elif action == "ai_apply":
                return self.cmd_ai_apply(params)
            elif action == "ai_cancel":
                return self.cmd_ai_cancel(params)
            elif action == "ai_save_preview":
                return self.cmd_ai_save_preview(params)
            elif action == "ai_list_styles":
                return self.cmd_ai_list_styles(params)
            elif action == "ai_create_region":
                return self.cmd_ai_create_region(params)
            elif action == "ai_list_regions":
                return self.cmd_ai_list_regions(params)
            elif action == "ai_select_region":
                return self.cmd_ai_select_region(params)
            elif action == "ai_remove_region":
                return self.cmd_ai_remove_region(params)
            elif action == "ai_add_control":
                return self.cmd_ai_add_control(params)
            elif action == "ai_list_controls":
                return self.cmd_ai_list_controls(params)
            elif action == "ai_remove_control":
                return self.cmd_ai_remove_control(params)
            elif action == "ai_set_control":
                return self.cmd_ai_set_control(params)
            else:
                return {"error": f"Unknown action: {action}"}

        except Exception as e:
            return {"error": str(e)}

    def get_active_document(self):
        """Get active document or return None."""
        app = Krita.instance()
        return app.activeDocument()

    def get_active_view(self):
        """Get active view or return None."""
        app = Krita.instance()
        window = app.activeWindow()
        if window:
            return window.activeView()
        return None

    def get_active_layer(self):
        """Get active paint layer."""
        doc = self.get_active_document()
        if doc:
            return doc.activeNode()
        return None

    def cmd_new_canvas(self, params):
        """Create a new canvas."""
        width = params.get("width", 800)
        height = params.get("height", 600)
        name = params.get("name", "New Canvas")
        bg_color = params.get("background", "#1a1a2e")

        app = Krita.instance()

        # Create document with background color
        doc = app.createDocument(width, height, name, "RGBA", "U8", "", 120.0)

        window = app.activeWindow()
        if window:
            window.addView(doc)

        # Create a paint layer
        root = doc.rootNode()
        layer = doc.createNode("paint", "paintlayer")
        root.addChildNode(layer, None)

        # Fill background using pixel data
        color = QColor(bg_color)
        r, g, b = color.red(), color.green(), color.blue()

        # Create pixel data for entire canvas (BGRA format)
        pixel_data = bytes([b, g, r, 255] * (width * height))
        layer.setPixelData(pixel_data, 0, 0, width, height)

        doc.refreshProjection()

        return {"status": "ok", "width": width, "height": height, "name": name}

    def cmd_set_color(self, params):
        """Set foreground color."""
        color_hex = params.get("color", "#ffffff")

        view = self.get_active_view()
        if not view:
            return {"error": "No active view"}

        color = QColor(color_hex)
        mc = ManagedColor.fromQColor(color, view.canvas())
        view.setForeGroundColor(mc)

        return {"status": "ok", "color": color_hex}

    def cmd_set_brush(self, params):
        """Set brush preset and size."""
        preset_name = params.get("preset", None)
        size = params.get("size", None)
        opacity = params.get("opacity", None)

        view = self.get_active_view()
        if not view:
            return {"error": "No active view"}

        if preset_name:
            # Find brush preset
            presets = Krita.instance().resources("preset")
            found = None
            for name, preset in presets.items():
                if preset_name.lower() in name.lower():
                    found = preset
                    break
            if found:
                view.setCurrentBrushPreset(found)
            else:
                return {"error": f"Brush preset not found: {preset_name}"}

        if size is not None:
            self.current_brush_size = size
            view.setBrushSize(size)

        if opacity is not None:
            self.current_opacity = opacity
            # Opacity is set per-stroke, store for later

        return {"status": "ok", "preset": preset_name, "size": size, "opacity": opacity}

    def cmd_stroke(self, params):
        """Paint a stroke along points using pixel-level drawing with soft edges."""
        points = params.get("points", [])
        brush_size = params.get("size", self.current_brush_size)
        hardness = params.get("hardness", 0.5)  # 0.0 = very soft, 1.0 = hard edge
        opacity = params.get("opacity", 1.0)

        if len(points) < 2:
            return {"error": "Need at least 2 points for a stroke"}

        layer = self.get_active_layer()
        if not layer:
            return {"error": "No active layer"}

        doc = self.get_active_document()
        view = self.get_active_view()

        if not view:
            return {"error": "No active view"}

        # Get current foreground color
        fg = view.foregroundColor()
        qcolor = fg.colorForCanvas(view.canvas())
        r, g, b = qcolor.red(), qcolor.green(), qcolor.blue()

        width = doc.width()
        height = doc.height()
        radius = max(1, brush_size // 2)

        # Calculate bounding box for all points plus brush radius
        min_x = max(0, int(min(p[0] for p in points)) - radius - 2)
        min_y = max(0, int(min(p[1] for p in points)) - radius - 2)
        max_x = min(width, int(max(p[0] for p in points)) + radius + 2)
        max_y = min(height, int(max(p[1] for p in points)) + radius + 2)

        w = max_x - min_x
        h = max_y - min_y

        if w <= 0 or h <= 0:
            return {"error": "Stroke out of bounds"}

        # Get existing pixel data for the affected region
        existing = layer.pixelData(min_x, min_y, w, h)
        pixels = bytearray(existing)

        import math

        def draw_soft_circle(cx, cy, point_opacity=1.0):
            """Draw a soft circle with falloff at canvas coordinates."""
            for dy in range(-radius, radius + 1):
                for dx in range(-radius, radius + 1):
                    dist_sq = dx*dx + dy*dy
                    if dist_sq <= radius*radius:
                        px = int(cx) + dx - min_x
                        py = int(cy) + dy - min_y
                        if 0 <= px < w and 0 <= py < h:
                            # Calculate distance from center (0.0 to 1.0)
                            dist = math.sqrt(dist_sq) / radius if radius > 0 else 0

                            # Apply hardness curve
                            # hardness=1.0: sharp edge, hardness=0.0: gradual fade from center
                            if hardness >= 1.0:
                                alpha_factor = 1.0
                            else:
                                # Soft falloff: starts fading at hardness point
                                if dist < hardness:
                                    alpha_factor = 1.0
                                else:
                                    # Smooth falloff from hardness to edge
                                    falloff = (dist - hardness) / (1.0 - hardness) if hardness < 1.0 else 0
                                    alpha_factor = 1.0 - falloff

                            final_alpha = int(255 * alpha_factor * opacity * point_opacity)

                            if final_alpha > 0:
                                idx = (py * w + px) * 4
                                # Alpha blending with existing pixel
                                existing_b = pixels[idx]
                                existing_g = pixels[idx+1]
                                existing_r = pixels[idx+2]
                                existing_a = pixels[idx+3]

                                # Simple alpha blend
                                blend = final_alpha / 255.0
                                new_r = int(existing_r * (1 - blend) + r * blend)
                                new_g = int(existing_g * (1 - blend) + g * blend)
                                new_b = int(existing_b * (1 - blend) + b * blend)
                                new_a = max(existing_a, final_alpha)

                                pixels[idx] = new_b
                                pixels[idx+1] = new_g
                                pixels[idx+2] = new_r
                                pixels[idx+3] = new_a

        def draw_line(x1, y1, x2, y2):
            """Draw a line using interpolation with soft brush circles."""
            dist = math.sqrt((x2 - x1)**2 + (y2 - y1)**2)
            # More steps for smoother lines
            steps = max(1, int(dist / max(1, radius / 3)))

            for i in range(steps + 1):
                t = i / steps if steps > 0 else 0
                x = x1 + t * (x2 - x1)
                y = y1 + t * (y2 - y1)
                draw_soft_circle(x, y)

        # Draw soft circles at each point and lines between them
        for i in range(len(points)):
            draw_soft_circle(points[i][0], points[i][1])
            if i > 0:
                draw_line(points[i-1][0], points[i-1][1], points[i][0], points[i][1])

        layer.setPixelData(bytes(pixels), min_x, min_y, w, h)
        doc.refreshProjection()

        return {"status": "ok", "points_count": len(points), "hardness": hardness}

    def cmd_fill(self, params):
        """Fill a circular area with current color."""
        x = params.get("x", 0)
        y = params.get("y", 0)
        radius = params.get("radius", 50)

        layer = self.get_active_layer()
        if not layer:
            return {"error": "No active layer"}

        doc = self.get_active_document()
        view = self.get_active_view()

        if not view:
            return {"error": "No active view"}

        # Get current foreground color
        fg = view.foregroundColor()
        qcolor = fg.colorForCanvas(view.canvas())
        r, g, b = qcolor.red(), qcolor.green(), qcolor.blue()

        # Paint a filled circle using pixel data
        # Create a bounding box
        x1 = max(0, x - radius)
        y1 = max(0, y - radius)
        x2 = min(doc.width(), x + radius)
        y2 = min(doc.height(), y + radius)
        w = x2 - x1
        h = y2 - y1

        if w <= 0 or h <= 0:
            return {"error": "Fill area out of bounds"}

        # Get existing pixel data
        existing = layer.pixelData(x1, y1, w, h)
        pixels = bytearray(existing)

        # Draw circle
        for py in range(h):
            for px in range(w):
                # Check if point is in circle
                dx = (x1 + px) - x
                dy = (y1 + py) - y
                if dx*dx + dy*dy <= radius*radius:
                    idx = (py * w + px) * 4
                    pixels[idx] = b      # B
                    pixels[idx+1] = g    # G
                    pixels[idx+2] = r    # R
                    pixels[idx+3] = 255  # A

        layer.setPixelData(bytes(pixels), x1, y1, w, h)
        doc.refreshProjection()

        return {"status": "ok", "x": x, "y": y, "radius": radius}

    def cmd_draw_shape(self, params):
        """Draw a shape (rectangle, ellipse, line)."""
        shape = params.get("shape", "rectangle")
        x = params.get("x", 0)
        y = params.get("y", 0)
        width = params.get("width", 100)
        height = params.get("height", 100)
        fill = params.get("fill", True)

        layer = self.get_active_layer()
        if not layer:
            return {"error": "No active layer"}

        doc = self.get_active_document()
        view = self.get_active_view()

        if not view:
            return {"error": "No active view"}

        # Get current foreground color
        fg = view.foregroundColor()
        qcolor = fg.colorForCanvas(view.canvas())
        r, g, b = qcolor.red(), qcolor.green(), qcolor.blue()

        if shape == "line":
            # Draw line using pixel data
            x2 = params.get("x2", x + width)
            y2 = params.get("y2", y + height)
            line_width = params.get("line_width", 2)

            # Calculate bounding box
            x1_bound = max(0, int(min(x, x2)) - line_width)
            y1_bound = max(0, int(min(y, y2)) - line_width)
            x2_bound = min(doc.width(), int(max(x, x2)) + line_width)
            y2_bound = min(doc.height(), int(max(y, y2)) + line_width)
            w = x2_bound - x1_bound
            h = y2_bound - y1_bound

            if w > 0 and h > 0:
                existing = layer.pixelData(x1_bound, y1_bound, w, h)
                pixels = bytearray(existing)

                # Draw line with thickness
                dist = max(abs(x2 - x), abs(y2 - y))
                steps = max(1, int(dist))
                radius = max(1, line_width // 2)

                for i in range(steps + 1):
                    t = i / steps if steps > 0 else 0
                    cx = x + t * (x2 - x)
                    cy = y + t * (y2 - y)
                    for dy in range(-radius, radius + 1):
                        for dx in range(-radius, radius + 1):
                            if dx*dx + dy*dy <= radius*radius:
                                px = int(cx) + dx - x1_bound
                                py = int(cy) + dy - y1_bound
                                if 0 <= px < w and 0 <= py < h:
                                    idx = (py * w + px) * 4
                                    pixels[idx] = b
                                    pixels[idx+1] = g
                                    pixels[idx+2] = r
                                    pixels[idx+3] = 255

                layer.setPixelData(bytes(pixels), x1_bound, y1_bound, w, h)
        elif shape == "rectangle" and fill:
            # Draw filled rectangle using pixel data
            x1 = max(0, int(x))
            y1 = max(0, int(y))
            x2 = min(doc.width(), int(x + width))
            y2 = min(doc.height(), int(y + height))
            w = x2 - x1
            h = y2 - y1

            if w > 0 and h > 0:
                pixel_data = bytes([b, g, r, 255] * (w * h))
                layer.setPixelData(pixel_data, x1, y1, w, h)
        elif shape == "ellipse" and fill:
            # Draw filled ellipse using pixel data
            cx = x + width / 2
            cy = y + height / 2
            rx = width / 2
            ry = height / 2

            x1 = max(0, int(x))
            y1 = max(0, int(y))
            x2 = min(doc.width(), int(x + width))
            y2 = min(doc.height(), int(y + height))
            w = x2 - x1
            h = y2 - y1

            if w > 0 and h > 0:
                existing = layer.pixelData(x1, y1, w, h)
                pixels = bytearray(existing)

                for py in range(h):
                    for px in range(w):
                        # Check if point is in ellipse
                        dx = (x1 + px - cx) / rx if rx > 0 else 0
                        dy = (y1 + py - cy) / ry if ry > 0 else 0
                        if dx*dx + dy*dy <= 1:
                            idx = (py * w + px) * 4
                            pixels[idx] = b
                            pixels[idx+1] = g
                            pixels[idx+2] = r
                            pixels[idx+3] = 255

                layer.setPixelData(bytes(pixels), x1, y1, w, h)
        else:
            return {"error": f"Shape '{shape}' with current options not supported"}

        doc.refreshProjection()

        return {"status": "ok", "shape": shape}

    def cmd_get_canvas(self, params):
        """Export current canvas to file and return path."""
        filename = params.get("filename", "canvas.png")

        doc = self.get_active_document()
        if not doc:
            return {"error": "No active document"}

        # Ensure filename has extension
        if not filename.endswith('.png'):
            filename += '.png'

        filepath = os.path.join(CANVAS_OUTPUT_DIR, filename)

        # Export image (batch mode suppresses export dialog)
        doc.setBatchmode(True)
        doc.exportImage(filepath, InfoObject())
        doc.setBatchmode(False)

        return {"status": "ok", "path": filepath}

    def cmd_undo(self, params):
        """Undo last action."""
        app = Krita.instance()
        action = app.action('edit_undo')
        if action:
            action.trigger()
            return {"status": "ok"}
        return {"error": "Could not trigger undo"}

    def cmd_redo(self, params):
        """Redo last undone action."""
        app = Krita.instance()
        action = app.action('edit_redo')
        if action:
            action.trigger()
            return {"status": "ok"}
        return {"error": "Could not trigger redo"}

    def cmd_clear(self, params):
        """Clear the canvas."""
        layer = self.get_active_layer()
        if not layer:
            return {"error": "No active layer"}

        doc = self.get_active_document()

        # Get canvas dimensions
        width = doc.width()
        height = doc.height()

        # Clear by filling with background color
        bg_color = params.get("color", "#1a1a2e")
        color = QColor(bg_color)
        r, g, b = color.red(), color.green(), color.blue()

        # Fill entire layer with color
        pixel_data = bytes([b, g, r, 255] * (width * height))
        layer.setPixelData(pixel_data, 0, 0, width, height)

        doc.refreshProjection()

        return {"status": "ok", "color": bg_color}

    def cmd_save(self, params):
        """Save to specific path."""
        filepath = params.get("path")
        if not filepath:
            return {"error": "No path specified"}

        doc = self.get_active_document()
        if not doc:
            return {"error": "No active document"}

        # Batch mode suppresses export dialog
        doc.setBatchmode(True)
        doc.exportImage(filepath, InfoObject())
        doc.setBatchmode(False)

        return {"status": "ok", "path": filepath}

    def cmd_get_color_at(self, params):
        """Get color at specific pixel (eyedropper)."""
        x = params.get("x", 0)
        y = params.get("y", 0)

        doc = self.get_active_document()
        if not doc:
            return {"error": "No active document"}

        # Get projection pixel data at point
        layer = doc.rootNode()
        pixel_data = layer.projectionPixelData(x, y, 1, 1)

        if len(pixel_data) >= 4:
            # RGBA
            b, g, r, a = pixel_data[0], pixel_data[1], pixel_data[2], pixel_data[3]
            hex_color = "#{:02x}{:02x}{:02x}".format(r, g, b)
            return {"status": "ok", "color": hex_color, "r": r, "g": g, "b": b, "a": a}

        return {"error": "Could not read pixel"}

    def cmd_list_brushes(self, params):
        """List available brush presets."""
        filter_str = params.get("filter", "")
        limit = params.get("limit", 50)

        presets = Krita.instance().resources("preset")
        brush_list = []

        for name, preset in presets.items():
            if filter_str.lower() in name.lower():
                brush_list.append(name)
                if len(brush_list) >= limit:
                    break

        return {"status": "ok", "brushes": brush_list, "count": len(brush_list)}

    def cmd_open_file(self, params):
        """Open an existing file in Krita."""
        filepath = params.get("path")
        if not filepath:
            return {"error": "No path specified"}

        if not os.path.exists(filepath):
            return {"error": f"File not found: {filepath}"}

        app = Krita.instance()

        # Open the document
        doc = app.openDocument(filepath)
        if not doc:
            return {"error": f"Failed to open: {filepath}"}

        # Add view to active window
        window = app.activeWindow()
        if window:
            window.addView(doc)

        return {"status": "ok", "path": filepath, "name": doc.name(), "width": doc.width(), "height": doc.height()}

    # ----- AI Diffusion bridge -----
    # Talks to the Acly/krita-ai-diffusion plugin running in the same Krita process.
    # All calls happen on the main thread (driven by the QTimer in createActions),
    # which is what ai_diffusion expects for Document/Model mutations.

    def _ai_get(self):
        """Lazy import of ai_diffusion to keep kritamcp usable if the plugin is missing."""
        try:
            from ai_diffusion.model.root import root
            from ai_diffusion.model.model import Workspace
            from ai_diffusion.model.jobs import JobState
            from ai_diffusion.model.connection import ConnectionState
            from ai_diffusion.style import Styles
        except ImportError as e:
            raise RuntimeError(f"AI Diffusion plugin not loaded: {e}")
        return {
            "root": root,
            "Workspace": Workspace,
            "JobState": JobState,
            "ConnectionState": ConnectionState,
            "Styles": Styles,
        }

    def _ai_model(self):
        ai = self._ai_get()
        model = ai["root"].model_for_active_document()
        if model is None:
            raise RuntimeError("No active document — open a document in Krita first")
        return ai, model

    def cmd_ai_status(self, params):
        ai = self._ai_get()
        root = ai["root"]
        JobState = ai["JobState"]
        conn = root.connection
        status = {
            "connection": conn.state.name,
            "error": conn.error if getattr(conn, "error", None) else None,
        }
        model = root.model_for_active_document()
        if model is None:
            status["document"] = None
            return {"status": "ok", **status}
        jobs = model.jobs
        status["document"] = model.document.filename or "(unsaved)"
        status["workspace"] = model.workspace.name
        status["style"] = model.style.filename if model.style else None
        status["strength"] = float(model.strength)
        status["seed"] = int(model.seed)
        status["fixed_seed"] = bool(model.fixed_seed)
        status["batch_count"] = int(model.batch_count)
        status["queue"] = {
            "queued": jobs.count(JobState.queued),
            "executing": jobs.count(JobState.executing),
            "finished": jobs.count(JobState.finished),
            "total": len(jobs),
        }
        status["prompt"] = {
            "positive": model.regions.active_or_root.positive,
            "negative": model.regions.negative,
        }
        return {"status": "ok", **status}

    def cmd_ai_set_prompt(self, params):
        ai, model = self._ai_model()
        positive = params.get("positive")
        negative = params.get("negative")
        target = model.regions.active_or_root
        applied = {}
        if positive is not None:
            target.positive = positive
            applied["positive_set_on"] = "root" if target is model.regions else "active_region"
            applied["positive"] = positive
        if negative is not None:
            model.regions.negative = negative
            applied["negative"] = negative
        return {"status": "ok", "applied": applied}

    def cmd_ai_set_params(self, params):
        ai, model = self._ai_model()
        Styles = ai["Styles"]
        applied = {}
        style_query = params.get("style")
        if style_query is not None:
            styles = Styles.list()
            found = styles.find(style_query)
            if found is None:
                q = style_query.lower()
                found = next(
                    (s for s in styles.filtered() if q in s.name.lower() or q in s.filename.lower()),
                    None,
                )
            if found is None:
                return {"error": f"Style not found: {style_query}"}
            model.style = found
            applied["style"] = found.filename
        if params.get("strength") is not None:
            model.strength = float(params["strength"])
            applied["strength"] = model.strength
        if params.get("seed") is not None:
            model.seed = int(params["seed"])
            applied["seed"] = model.seed
        if params.get("fixed_seed") is not None:
            model.fixed_seed = bool(params["fixed_seed"])
            applied["fixed_seed"] = model.fixed_seed
        if params.get("batch_count") is not None:
            model.batch_count = int(params["batch_count"])
            applied["batch_count"] = model.batch_count
        return {"status": "ok", "applied": applied}

    def cmd_ai_set_workspace(self, params):
        ai, model = self._ai_model()
        Workspace = ai["Workspace"]
        name = params.get("name", "")
        try:
            ws = Workspace[name]
        except KeyError:
            valid = [w.name for w in Workspace]
            return {"error": f"Invalid workspace '{name}'. Valid: {valid}"}
        model.workspace = ws
        return {"status": "ok", "workspace": ws.name}

    def cmd_ai_generate(self, params):
        ai, model = self._ai_model()
        Workspace = ai["Workspace"]
        ws = model.workspace
        before = len(model.jobs)
        if ws is Workspace.generation:
            model.generate()
        elif ws is Workspace.upscaling:
            model.upscale_image()
        elif ws is Workspace.live:
            model.generate_live()
        elif ws is Workspace.animation:
            model.animation.generate()
        elif ws is Workspace.custom:
            model.custom.generate()
        else:
            return {"error": f"Unsupported workspace: {ws.name}"}
        return {
            "status": "ok",
            "workspace": ws.name,
            "jobs_before": before,
            "jobs_after": len(model.jobs),
        }

    def cmd_ai_list_jobs(self, params):
        ai, model = self._ai_model()
        filter_state = params.get("state", "all")
        limit = int(params.get("limit", 20))
        out = []
        for job in list(model.jobs):
            if filter_state != "all" and job.state.name != filter_state:
                continue
            out.append({
                "id": job.id,
                "kind": job.kind.name,
                "state": job.state.name,
                "name": job.params.name,
                "prompt": job.params.prompt,
                "seed": job.params.seed,
                "result_count": len(job.results),
                "timestamp": job.timestamp.isoformat(),
            })
        out.reverse()  # newest first
        return {"status": "ok", "jobs": out[:limit]}

    def cmd_ai_apply(self, params):
        ai, model = self._ai_model()
        JobState = ai["JobState"]
        job_id = params.get("job_id")
        index = int(params.get("index", 0))
        if job_id is None:
            finished = [j for j in model.jobs if j.state is JobState.finished and len(j.results) > 0]
            if not finished:
                return {"error": "No finished job to apply"}
            job = finished[-1]
            job_id = job.id
        else:
            job = model.jobs.find(job_id)
            if job is None:
                return {"error": f"Job not found: {job_id}"}
        if index >= len(job.results):
            return {"error": f"Index {index} out of range (have {len(job.results)} results)"}
        model.apply_generated_result(job_id, index)
        return {"status": "ok", "job_id": job_id, "index": index}

    def cmd_ai_cancel(self, params):
        ai, model = self._ai_model()
        active = bool(params.get("active", True))
        queued = bool(params.get("queued", False))
        model.cancel(active=active, queued=queued)
        return {"status": "ok", "active": active, "queued": queued}

    def cmd_ai_save_preview(self, params):
        ai, model = self._ai_model()
        job_id = params.get("job_id")
        if job_id is None:
            return {"error": "job_id required"}
        index = int(params.get("index", 0))
        filename = params.get("filename") or f"preview_{job_id}_{index}.png"
        if not filename.endswith(".png"):
            filename += ".png"
        job = model.jobs.find(job_id)
        if job is None:
            return {"error": f"Job not found: {job_id}"}
        if index >= len(job.results):
            return {"error": f"Index {index} out of range"}
        img = job.results[index]
        filepath = os.path.join(CANVAS_OUTPUT_DIR, filename)
        img.save(filepath)
        return {"status": "ok", "path": filepath}

    def cmd_ai_list_styles(self, params):
        ai = self._ai_get()
        Styles = ai["Styles"]
        filter_str = (params.get("filter", "") or "").lower()
        limit = int(params.get("limit", 30))
        out = []
        for s in Styles.list().filtered():
            if filter_str and filter_str not in s.name.lower() and filter_str not in s.filename.lower():
                continue
            out.append({"filename": s.filename, "name": s.name})
            if len(out) >= limit:
                break
        return {"status": "ok", "styles": out}

    def _region_info(self, region):
        layers = []
        for l in region.layers:
            try:
                layers.append({"id": str(l.id), "name": l.name, "type": l.type.name})
            except Exception:
                pass
        return {
            "positive": region.positive,
            "layers": layers,
            "primary_layer_id": layers[0]["id"] if layers else None,
        }

    def cmd_ai_create_region(self, params):
        """Create a new region linked to a freshly created (transparent) paint layer.

        Bypasses ai_diffusion's create_region heuristic — that one tries to link the
        currently active layer if it's a paint layer, which is wrong when the active
        layer is a fully opaque canvas (the region's mask would cover everything).

        After creation, the new layer is activated so subsequent painting tools
        (krita_fill, krita_draw_shape, krita_stroke) draw the region's silhouette mask.
        """
        ai, model = self._ai_model()
        positive = params.get("positive", "")
        group = bool(params.get("group", False))
        name_prefix = params.get("name") or f"Region {len(model.regions._regions)}"
        # Create a fresh, empty paint layer (transparent) — do NOT reuse active.
        if group:
            group_layer = model.layers.create_group(name_prefix)
            paint_layer = model.layers.create("Paint layer", parent=group_layer)
            link_layer = group_layer
            active_target = paint_layer
        else:
            paint_layer = model.layers.create(name_prefix)
            link_layer = paint_layer
            active_target = paint_layer
        region = model.regions._add(link_layer)
        if positive:
            region.positive = positive
        model.regions.active = region
        try:
            model.layers.active = active_target
        except Exception:
            pass
        return {"status": "ok", "region": self._region_info(region)}

    def cmd_ai_list_regions(self, params):
        ai, model = self._ai_model()
        root = model.regions
        active = root.active
        active_layer = None
        try:
            active_layer = root._model.layers.active
        except Exception:
            pass
        regions_out = []
        for idx, r in enumerate(root._regions):
            info = self._region_info(r)
            info["index"] = idx
            info["is_active"] = (r is active)
            regions_out.append(info)
        return {
            "status": "ok",
            "root": {
                "positive": root.positive,
                "negative": root.negative,
            },
            "active_layer_id": str(active_layer.id) if active_layer else None,
            "regions": regions_out,
        }

    def cmd_ai_select_region(self, params):
        """Select an existing region by index or by linked layer_id. Pass null/None to select root."""
        ai, model = self._ai_model()
        root = model.regions
        idx = params.get("index")
        layer_id = params.get("layer_id")
        if idx is None and layer_id is None:
            root.active = None  # root
            return {"status": "ok", "active": "root"}
        target_region = None
        if idx is not None:
            i = int(idx)
            if i < 0 or i >= len(root._regions):
                return {"error": f"Index {i} out of range (have {len(root._regions)} regions)"}
            target_region = root._regions[i]
        else:
            for r in root._regions:
                if any(str(l.id) == layer_id for l in r.layers):
                    target_region = r
                    break
            if target_region is None:
                return {"error": f"No region linked to layer_id {layer_id}"}
        root.active = target_region
        first = target_region.first_layer
        if first is not None:
            try:
                target = first
                if hasattr(first, "type") and first.type.name == "group" and first.child_layers:
                    target = first.child_layers[-1]
                model.layers.active = target
            except Exception:
                pass
        return {"status": "ok", "active": self._region_info(target_region)}

    def cmd_ai_remove_region(self, params):
        ai, model = self._ai_model()
        root = model.regions
        idx = params.get("index")
        if idx is None:
            return {"error": "index required"}
        i = int(idx)
        if i < 0 or i >= len(root._regions):
            return {"error": f"Index {i} out of range"}
        region = root._regions[i]
        root.remove(region)
        return {"status": "ok", "removed_index": i}

    def _control_info(self, control, index):
        return {
            "index": index,
            "mode": control.mode.name,
            "layer_id": str(control.layer_id),
            "strength": control.strength / control.strength_multiplier,
            "start": control.start,
            "end": control.end,
            "is_supported": control.is_supported,
            "error_text": control.error_text,
        }

    def _resolve_control_target(self, params):
        """Return (target, label, model). target is the ControlLayerList of root or region.
        region_index None or <0 means root."""
        ai, model = self._ai_model()
        region_index = params.get("region_index")
        if region_index is None or int(region_index) < 0:
            return model.regions, "root", model
        i = int(region_index)
        if i < 0 or i >= len(model.regions._regions):
            return None, None, model
        return model.regions._regions[i], f"region_{i}", model

    def cmd_ai_add_control(self, params):
        """Add a Control Layer (ControlNet / IP-Adapter) to root or a region.

        Params:
          mode: scribble | line_art | soft_edge | canny_edge | depth | normal |
                pose | segmentation | reference | style | composition | face |
                blur | stencil
          layer_id: optional UUID of the layer to use as control source. If omitted,
                    uses currently active layer.
          strength: optional 0.0-2.0 control influence (uses preset default if omitted).
          region_index: optional region index. Omit / -1 = root (whole image).
        """
        from ai_diffusion.backend.resources import ControlMode

        target, label, model = self._resolve_control_target(params)
        if target is None:
            return {"error": "Region index out of range"}

        mode_name = params.get("mode", "scribble")
        try:
            mode = ControlMode[mode_name]
        except KeyError:
            valid = ", ".join(m.name for m in ControlMode)
            return {"error": f"Unknown control mode '{mode_name}'. Valid: {valid}"}

        layer_id = params.get("layer_id")
        if layer_id:
            uid = QUuid(layer_id)
            layer = model.layers.updated().find(uid)
            if layer is None:
                return {"error": f"Layer not found: {layer_id}"}
            try:
                model.layers.active = layer
            except Exception as e:
                return {"error": f"Could not activate layer: {e}"}

        control = target.control.emplace()
        control.mode = mode

        strength = params.get("strength")
        if strength is not None:
            s = float(strength)
            control.use_custom_strength = True
            control.strength = int(max(0.0, min(2.0, s)) * control.strength_multiplier)

        idx = len(target.control) - 1
        return {
            "status": "ok",
            "scope": label,
            "control": self._control_info(control, idx),
        }

    def cmd_ai_list_controls(self, params):
        """List Control Layers. If region_index omitted, lists root + all regions."""
        region_index = params.get("region_index")
        if region_index is None:
            ai, model = self._ai_model()
            out = {
                "status": "ok",
                "root": [self._control_info(c, i) for i, c in enumerate(model.regions.control)],
                "regions": [],
            }
            for ri, r in enumerate(model.regions._regions):
                out["regions"].append({
                    "index": ri,
                    "controls": [self._control_info(c, i) for i, c in enumerate(r.control)],
                })
            return out
        target, label, _ = self._resolve_control_target(params)
        if target is None:
            return {"error": "Region index out of range"}
        return {
            "status": "ok",
            "scope": label,
            "controls": [self._control_info(c, i) for i, c in enumerate(target.control)],
        }

    def cmd_ai_remove_control(self, params):
        """Remove a Control Layer by index from root or specified region."""
        target, label, _ = self._resolve_control_target(params)
        if target is None:
            return {"error": "Region index out of range"}
        idx = params.get("index")
        if idx is None:
            return {"error": "index required"}
        i = int(idx)
        if i < 0 or i >= len(target.control):
            return {"error": f"Control index {i} out of range"}
        control = target.control[i]
        target.control.remove(control)
        return {"status": "ok", "removed_index": i, "scope": label}

    def cmd_ai_set_control(self, params):
        """Update mode and/or strength of an existing Control Layer."""
        from ai_diffusion.backend.resources import ControlMode

        target, label, _ = self._resolve_control_target(params)
        if target is None:
            return {"error": "Region index out of range"}
        idx = params.get("index")
        if idx is None:
            return {"error": "index required"}
        i = int(idx)
        if i < 0 or i >= len(target.control):
            return {"error": f"Control index {i} out of range"}
        control = target.control[i]

        mode_name = params.get("mode")
        if mode_name is not None:
            try:
                control.mode = ControlMode[mode_name]
            except KeyError:
                valid = ", ".join(m.name for m in ControlMode)
                return {"error": f"Unknown control mode '{mode_name}'. Valid: {valid}"}

        strength = params.get("strength")
        if strength is not None:
            s = float(strength)
            control.use_custom_strength = True
            control.strength = int(max(0.0, min(2.0, s)) * control.strength_multiplier)

        return {
            "status": "ok",
            "scope": label,
            "control": self._control_info(control, i),
        }


# Register the extension
Krita.instance().addExtension(KritaMCPExtension(Krita.instance()))
