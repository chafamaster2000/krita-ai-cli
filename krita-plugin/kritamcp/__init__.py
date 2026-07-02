"""
Krita MCP Bridge - HTTP server for external paint commands in Krita
Allows Claude (or any MCP client) to paint by sending commands to this plugin.
"""

from krita import *
from PyQt5.QtCore import (
    QTimer, QThread, pyqtSignal, QObject, Qt, QPointF, QRectF, QUuid,
    QByteArray, QBuffer, QIODevice,
)
from PyQt5.QtGui import (
    QColor, QImage, QPainter, QPen, QBrush, QPainterPath,
)
from PyQt5.QtWidgets import QMessageBox, QApplication
import base64
import json
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
import os

# Configuration - customize these as needed
SERVER_PORT = 5678
CANVAS_OUTPUT_DIR = os.path.expanduser("~/krita-mcp-output")
# Optional shared-token auth: if set (same value as the MCP server's
# KRITAMCP_TOKEN), every request must carry it in the X-Kritamcp-Token header.
# Empty = no token check (still safe for local use thanks to the Origin guard).
AUTH_TOKEN = os.environ.get("KRITAMCP_TOKEN", "")

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
            ev = self.events.get(command_id)
            if ev is None:
                # The waiter already timed out and cleaned up — drop the result
                # instead of leaving an orphan in self.results forever.
                return
            self.results[command_id] = result
        ev.set()

    def get_result(self, command_id, ev, timeout=120):
        """Block on this command's Event until the main thread sets its result.

        The default timeout of 120s matters — canvas export and save operations
        can take a long time on large canvases. Removing the event under the lock
        before set_result can store its result is what lets set_result discard
        orphans (see above), so the two halves never leak entries.
        """
        signalled = ev.wait(timeout)
        with self.lock:
            self.events.pop(command_id, None)
            result = self.results.pop(command_id, None)
        if result is not None:
            # Result landed (possibly right at the deadline) — prefer it.
            return result
        if not signalled:
            return {"error": "Timeout waiting for command execution"}
        return {"error": "Command produced no result"}

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

    def _auth_ok(self):
        """Gate every request. A genuine MCP client speaks straight HTTP with no
        Origin/Referer; a web page (the CSRF / DNS-rebinding vector against a
        localhost server) always sends one — so reject those outright. An
        optional shared token adds a second factor when configured."""
        if self.headers.get('Origin') or self.headers.get('Referer'):
            self.send_json_response({"error": "Forbidden: browser-origin request rejected"}, 403)
            return False
        if AUTH_TOKEN and self.headers.get('X-Kritamcp-Token') != AUTH_TOKEN:
            self.send_json_response({"error": "Unauthorized"}, 401)
            return False
        return True

    def do_GET(self):
        """Handle GET requests - mainly for health check."""
        if not self._auth_ok():
            return
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

        if not self._auth_ok():
            return

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

        # Wait just under the client's own timeout so we return a clean timeout
        # error before its socket gives up. Falls back to 120s if unspecified.
        try:
            client_timeout = float(command.get("timeout", 120))
        except (TypeError, ValueError):
            client_timeout = 120.0
        wait_timeout = max(5.0, client_timeout - 2.0)

        # Block on this command's own event until the main thread sets a result.
        result = command_queue.get_result(command_id, ev, timeout=wait_timeout)

        # Use a truthy check, not key-presence: several successful results
        # carry an "error": None field (e.g. ai_status), which must NOT be
        # treated as an HTTP 500.
        if result.get("error"):
            self.send_json_response(result, 500)
        else:
            self.send_json_response(result)


class ServerThread(QThread):
    """Thread to run HTTP server without blocking Krita UI."""

    def __init__(self, port):
        super().__init__()
        self.port = port
        self.server = None
        self.error = None  # set if bind/serve fails, surfaced by the extension

    def run(self):
        try:
            self.server = HTTPServer(('localhost', self.port), PaintRequestHandler)
        except OSError as e:
            # Most commonly: port already in use (a stale instance, or a reload
            # before the old server released the socket). Surface it instead of
            # dying silently — the extension polls .error after startup.
            self.error = e
            print(f"[KritaMCP] Could not bind port {self.port}: {e}")
            return
        try:
            self.server.serve_forever()
        except Exception as e:
            self.error = e
            print(f"[KritaMCP] HTTP server stopped unexpectedly: {e}")

    def stop(self):
        if self.server:
            self.server.shutdown()
            self.server.server_close()


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
            # Shut the server down cleanly when Krita quits so the socket is
            # released (otherwise the next launch can hit "address in use").
            app = QApplication.instance()
            if app is not None:
                app.aboutToQuit.connect(self.server_thread.stop)
            # run() binds asynchronously; check shortly after for a bind failure.
            QTimer.singleShot(750, self._check_server_started)

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

    def _check_server_started(self):
        """Surface a startup bind failure with a visible warning (main thread)."""
        if self.server_thread is not None and self.server_thread.error is not None:
            msg = (f"Krita MCP Bridge could not start its server on port "
                   f"{SERVER_PORT}:\n{self.server_thread.error}\n\n"
                   f"Another instance or app may be using the port. "
                   f"MCP tools will not work until this is resolved.")
            print(f"[KritaMCP] {msg}")
            try:
                QMessageBox.warning(None, "Krita MCP Bridge", msg)
            except Exception:
                pass

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
        """Execute a paint command and return result.

        Dispatch by convention: action "foo" maps to method cmd_foo. The cmd_
        prefix is the allow-list — an action can only ever reach a method that
        was written as a command handler, never arbitrary internals.
        """
        try:
            action = command.get("action")
            params = command.get("params", {})

            handler = getattr(self, f"cmd_{action}", None) if action else None
            if handler is None or not callable(handler):
                return {"error": f"Unknown action: {action}"}
            return handler(params)

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

        # Fill background with one C++-side QImage fill (no giant Python list).
        color = QColor(bg_color)
        color.setAlpha(255)
        self._solid_fill_layer(layer, width, height, color)

        # Make the filled layer the active node so subsequent painting tools
        # (draw_shape/fill/stroke) draw ON it instead of on the default layer
        # underneath, which would be hidden by this opaque background.
        doc.setActiveNode(layer)

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

    def _fg_qcolor(self, view):
        """Current foreground colour as a QColor."""
        fg = view.foregroundColor()
        return fg.colorForCanvas(view.canvas())

    def _composite(self, layer, doc, bbox, draw_fn, feather=0):
        """Composite a QPainter drawing onto a layer region in one setPixelData.

        Reads the existing region into a QImage, runs `draw_fn(painter)` to draw
        (in layer-region-local coordinates) on a transparent overlay, optionally
        feathers the overlay's alpha, then source-over composites it back. All
        the heavy lifting happens in Qt's C++ painter — no per-pixel Python.

        bbox is (x, y, w, h) already clamped to the canvas. Krita's U8 RGBA pixel
        order is BGRA in memory, which is exactly QImage.Format_ARGB32 on
        little-endian, so the byte buffers map without conversion.
        """
        x, y, w, h = bbox
        if w <= 0 or h <= 0:
            return False

        existing = bytes(layer.pixelData(x, y, w, h))
        base = QImage(existing, w, h, QImage.Format_ARGB32).copy()

        overlay = QImage(w, h, QImage.Format_ARGB32)
        overlay.fill(0)  # fully transparent
        painter = QPainter(overlay)
        painter.setRenderHint(QPainter.Antialiasing, True)
        draw_fn(painter)
        painter.end()

        if feather and feather > 0:
            # Cheap, dependency-free blur: downscale then upscale with bilinear
            # smoothing softens the alpha edge. Scaling is C++ fast.
            f = max(1, int(feather))
            sw = max(1, w // (f + 1))
            sh = max(1, h // (f + 1))
            overlay = (overlay
                       .scaled(sw, sh, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
                       .scaled(w, h, Qt.IgnoreAspectRatio, Qt.SmoothTransformation))

        comp = QPainter(base)
        comp.setCompositionMode(QPainter.CompositionMode_SourceOver)
        comp.drawImage(0, 0, overlay)
        comp.end()

        out = base.constBits().asstring(w * h * 4)
        layer.setPixelData(out, x, y, w, h)
        return True

    def _solid_fill_layer(self, layer, w, h, qcolor):
        """Fill an entire layer with a solid colour via one C++ QImage.fill.

        Avoids building a `bytes([b,g,r,a] * w*h)` Python list, whose transient
        pointer array is ~512 MB for a 4K canvas. QImage.fill writes ARGB32,
        which is BGRA in memory on little-endian — exactly Krita's U8 RGBA order.
        """
        img = QImage(w, h, QImage.Format_ARGB32)
        img.fill(qcolor)
        layer.setPixelData(img.constBits().asstring(w * h * 4), 0, 0, w, h)

    def cmd_stroke(self, params):
        """Paint a stroke through a series of points (QPainter, antialiased)."""
        points = params.get("points", [])
        brush_size = int(params.get("size", self.current_brush_size))
        feather = float(params.get("feather", 0))
        opacity = float(params.get("opacity", 1.0))

        if len(points) < 2:
            return {"error": "Need at least 2 points for a stroke"}

        layer = self.get_active_layer()
        if not layer:
            return {"error": "No active layer"}

        doc = self.get_active_document()
        view = self.get_active_view()
        if not view:
            return {"error": "No active view"}

        qcolor = self._fg_qcolor(view)
        radius = max(1, brush_size // 2)
        pad = radius + 2

        min_x = max(0, int(min(p[0] for p in points)) - pad)
        min_y = max(0, int(min(p[1] for p in points)) - pad)
        max_x = min(doc.width(), int(max(p[0] for p in points)) + pad)
        max_y = min(doc.height(), int(max(p[1] for p in points)) + pad)
        w = max_x - min_x
        h = max_y - min_y

        if w <= 0 or h <= 0:
            return {"error": "Stroke out of bounds"}

        path = QPainterPath()
        path.moveTo(points[0][0] - min_x, points[0][1] - min_y)
        for p in points[1:]:
            path.lineTo(p[0] - min_x, p[1] - min_y)

        pen_color = QColor(qcolor)
        pen_color.setAlphaF(max(0.0, min(1.0, opacity)))

        def draw(painter):
            pen = QPen(pen_color, brush_size)
            pen.setCapStyle(Qt.RoundCap)
            pen.setJoinStyle(Qt.RoundJoin)
            painter.setPen(pen)
            painter.drawPath(path)

        self._composite(layer, doc, (min_x, min_y, w, h), draw, feather=feather)
        self._maybe_refresh(doc)

        return {"status": "ok", "points_count": len(points), "feather": feather}

    def cmd_fill(self, params):
        """Fill a circular area with the current color (QPainter, antialiased)."""
        x = int(params.get("x", 0))
        y = int(params.get("y", 0))
        radius = int(params.get("radius", 50))
        feather = float(params.get("feather", 0))

        layer = self.get_active_layer()
        if not layer:
            return {"error": "No active layer"}

        doc = self.get_active_document()
        view = self.get_active_view()
        if not view:
            return {"error": "No active view"}

        qcolor = QColor(self._fg_qcolor(view))
        qcolor.setAlpha(255)

        pad = 2
        x1 = max(0, x - radius - pad)
        y1 = max(0, y - radius - pad)
        x2 = min(doc.width(), x + radius + pad)
        y2 = min(doc.height(), y + radius + pad)
        w = x2 - x1
        h = y2 - y1

        if w <= 0 or h <= 0:
            return {"error": "Fill area out of bounds"}

        def draw(painter):
            painter.setPen(Qt.NoPen)
            painter.setBrush(QBrush(qcolor))
            painter.drawEllipse(QPointF(x - x1, y - y1), radius, radius)

        self._composite(layer, doc, (x1, y1, w, h), draw, feather=feather)
        self._maybe_refresh(doc)

        return {"status": "ok", "x": x, "y": y, "radius": radius, "feather": feather}

    def cmd_draw_shape(self, params):
        """Draw a shape (rectangle, ellipse, line) with QPainter (antialiased)."""
        shape = params.get("shape", "rectangle")
        x = float(params.get("x", 0))
        y = float(params.get("y", 0))
        width = float(params.get("width", 100))
        height = float(params.get("height", 100))
        fill = bool(params.get("fill", True))
        stroke = bool(params.get("stroke", False))
        feather = float(params.get("feather", 0))

        layer = self.get_active_layer()
        if not layer:
            return {"error": "No active layer"}

        doc = self.get_active_document()
        view = self.get_active_view()
        if not view:
            return {"error": "No active view"}

        qcolor = QColor(self._fg_qcolor(view))
        qcolor.setAlpha(255)

        if shape == "line":
            x2 = float(params.get("x2", x + width))
            y2 = float(params.get("y2", y + height))
            line_width = int(params.get("line_width", 2))
            pad = line_width + 2

            x1b = max(0, int(min(x, x2)) - pad)
            y1b = max(0, int(min(y, y2)) - pad)
            x2b = min(doc.width(), int(max(x, x2)) + pad)
            y2b = min(doc.height(), int(max(y, y2)) + pad)
            w = x2b - x1b
            h = y2b - y1b
            if w <= 0 or h <= 0:
                return {"error": "Line out of bounds"}

            def draw(painter):
                pen = QPen(qcolor, line_width)
                pen.setCapStyle(Qt.RoundCap)
                painter.setPen(pen)
                painter.drawLine(QPointF(x - x1b, y - y1b), QPointF(x2 - x1b, y2 - y1b))

            self._composite(layer, doc, (x1b, y1b, w, h), draw, feather=feather)

        elif shape in ("rectangle", "ellipse"):
            pad = 2
            x1 = max(0, int(x) - pad)
            y1 = max(0, int(y) - pad)
            x2 = min(doc.width(), int(x + width) + pad)
            y2 = min(doc.height(), int(y + height) + pad)
            w = x2 - x1
            h = y2 - y1
            if w <= 0 or h <= 0:
                return {"error": "Shape out of bounds"}

            rect = QRectF(x - x1, y - y1, width, height)

            def draw(painter):
                if fill:
                    painter.setBrush(QBrush(qcolor))
                else:
                    painter.setBrush(Qt.NoBrush)
                if stroke or not fill:
                    painter.setPen(QPen(qcolor, int(params.get("line_width", 2))))
                else:
                    painter.setPen(Qt.NoPen)
                if shape == "rectangle":
                    painter.drawRect(rect)
                else:
                    painter.drawEllipse(rect)

            self._composite(layer, doc, (x1, y1, w, h), draw, feather=feather)
        else:
            return {"error": f"Unknown shape '{shape}'"}

        self._maybe_refresh(doc)
        return {"status": "ok", "shape": shape, "feather": feather}

    def cmd_get_canvas(self, params):
        """Grab the current canvas as an inline image (base64) for review.

        mode="fast" (default): downscaled JPEG (<= max_dim px) — cheap to encode
        and few vision tokens. Use this while drawing/iterating.
        mode="full": full-resolution PNG — use to inspect the final result.

        Returns the image bytes base64-encoded; the MCP server wraps them as an
        inline Image so the client sees the canvas directly (no disk round-trip).
        """
        mode = params.get("mode", "fast")
        doc = self.get_active_document()
        if not doc:
            return {"error": "No active document"}

        w, h = doc.width(), doc.height()

        if mode == "full":
            img = doc.projection(0, 0, w, h)
            fmt, mime, quality = "PNG", "image/png", -1
        else:
            # 512px: suficiente para juzgar composición/ubicación mientras se
            # itera, y ~4x menos tokens de visión que 1024 (~262 vs ~1050).
            max_dim = int(params.get("max_dim", 512))
            if w >= h:
                tw = min(w, max_dim)
                th = max(1, round(h * tw / w))
            else:
                th = min(h, max_dim)
                tw = max(1, round(w * th / h))
            img = doc.thumbnail(tw, th)
            fmt, mime, quality = "JPEG", "image/jpeg", 85

        if img is None or img.isNull():
            return {"error": "Could not render canvas projection"}

        ba = QByteArray()
        buf = QBuffer(ba)
        buf.open(QIODevice.WriteOnly)
        if quality >= 0:
            img.save(buf, fmt, quality)
        else:
            img.save(buf, fmt)
        buf.close()

        b64 = base64.b64encode(bytes(ba)).decode("ascii")
        return {
            "status": "ok",
            "mode": mode,
            "mime": mime,
            "format": fmt.lower(),
            "data_b64": b64,
            "width": img.width(),
            "height": img.height(),
        }

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

        # Clear by filling with background color (single C++ QImage fill).
        bg_color = params.get("color", "#1a1a2e")
        color = QColor(bg_color)
        color.setAlpha(255)
        self._solid_fill_layer(layer, width, height, color)

        self._maybe_refresh(doc)

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
            # Krita U8 "RGBA" is laid out BGRA in memory (little-endian ARGB32).
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

    def cmd_doc_info(self, params):
        """Lightweight document summary for `kri status` — no pixel data."""
        doc = self.get_active_document()
        if not doc:
            return {"status": "ok", "document": None}
        layers = []
        for node in doc.topLevelNodes():
            layers.append({
                "name": node.name(),
                "type": node.type(),
                "visible": node.visible(),
            })
        active = doc.activeNode()
        return {
            "status": "ok",
            "document": {
                "name": doc.name() or "(unsaved)",
                "width": doc.width(),
                "height": doc.height(),
                "active_layer": active.name() if active else None,
                "layers": layers,
            },
        }

    def cmd_exec(self, params):
        """Execute arbitrary Python inside Krita (`kri exec`). DISABLED unless
        Krita was started with KRITAMCP_ALLOW_EXEC=1 in its environment.

        Runs on the main GUI thread like every other action. Consequence: a
        long script blocks Krita's UI until it finishes, and a truly hung
        script CANNOT be preempted — the HTTP client times out and returns an
        error, but Krita keeps executing. The skill tells the model to keep
        scripts short; there is no server-side kill.
        """
        if os.environ.get("KRITAMCP_ALLOW_EXEC", "") != "1":
            return {"error": ("exec is disabled. Start Krita with "
                              "KRITAMCP_ALLOW_EXEC=1 to enable it.")}
        code = params.get("code", "")
        if not code.strip():
            return {"error": "exec: empty code"}
        import io
        import traceback
        from contextlib import redirect_stdout
        doc = self.get_active_document()
        namespace = {
            "Krita": Krita,
            "app": Krita.instance(),
            "doc": doc,
            "view": self.get_active_view(),
            "layer": self.get_active_layer(),
            "QColor": QColor, "QImage": QImage, "QPainter": QPainter,
            "QPen": QPen, "QBrush": QBrush, "QPainterPath": QPainterPath,
            "QPointF": QPointF, "QRectF": QRectF, "Qt": Qt,
        }
        buf = io.StringIO()
        try:
            with redirect_stdout(buf):
                exec(compile(code, "<kri-exec>", "exec"), namespace)
        except Exception:
            return {
                "error": "exec raised an exception",
                "traceback": traceback.format_exc(),
                "stdout": buf.getvalue(),
            }
        self._maybe_refresh(doc)
        result_out = {"status": "ok", "stdout": buf.getvalue()}
        result = namespace.get("result")
        if result is not None:
            try:
                json.dumps(result)
                result_out["result"] = result
            except (TypeError, ValueError):
                result_out["result"] = repr(result)
        return result_out

    def cmd_batch(self, params):
        """Run several commands in one round-trip.

        Each item is {"action": ..., "params": {...}} — the same shape a single
        HTTP command takes. Projection is refreshed ONCE at the end instead of
        per command. If `review` is set (a mode string like "fast"/"full", or
        true → "fast"), the final canvas image is grabbed and returned too.
        """
        commands = params.get("commands", [])
        if not isinstance(commands, list):
            return {"error": "batch 'commands' must be a list"}

        review = params.get("review")
        stop_on_error = bool(params.get("stop_on_error", False))
        results = []
        stopped_at = None
        self._suppress_refresh = True
        try:
            for i, c in enumerate(commands):
                r = self.execute_command(c)
                results.append(r)
                if stop_on_error and isinstance(r, dict) and r.get("error"):
                    stopped_at = i
                    break
        finally:
            self._suppress_refresh = False

        doc = self.get_active_document()
        if doc is not None:
            doc.refreshProjection()

        out = {"status": "ok", "count": len(results), "results": results}
        if stopped_at is not None:
            out["stopped_at"] = stopped_at
        if review:
            mode = review if isinstance(review, str) else "fast"
            out["canvas"] = self.cmd_get_canvas({"mode": mode})
        return out

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

    def _style_model_info(self, root, style):
        """Best-effort resolution of the active style's architecture/checkpoint so the
        MCP client can classify the model family (natural-language vs danbooru tags)
        by hard data instead of guessing from the style filename.

        Degrades gracefully: any failure (AI Diffusion API drift, offline, older
        version) returns a partial dict with available=False — it must NEVER break
        ai_status. `architecture` is the Arch enum member name, e.g. "sdxl", "flux",
        "zimage", "illu", "qwen", "sd3" (or "auto" if undetermined)."""
        info = {"available": False}
        if style is None:
            return info
        arch = None
        try:
            from ai_diffusion.backend.client import resolve_arch

            client = getattr(root.connection, "client_if_connected", None)
            arch = resolve_arch(style, client)
            info["resolved_from"] = "checkpoint" if client else "style"
        except Exception as e:
            # Fallback: read the declared architecture/version off the style directly
            # (covers offline + older AI Diffusion layouts that used `sd_version`).
            arch = getattr(style, "architecture", None) or getattr(style, "sd_version", None)
            info["resolved_from"] = "declared"
            if arch is None:
                info["error"] = f"arch resolve failed: {e}"
        if arch is not None:
            info["architecture"] = getattr(arch, "name", str(arch))
            info["architecture_label"] = getattr(arch, "value", None)
            info["available"] = True
        try:
            cks = getattr(style, "checkpoints", None) or []
            info["checkpoint"] = cks[0] if cks else None
        except Exception:
            pass
        try:
            loras = getattr(style, "loras", None) or []
            info["loras"] = [
                l.get("name")
                for l in loras
                if isinstance(l, dict) and l.get("enabled", True) and l.get("name")
            ]
        except Exception:
            pass
        return info

    def cmd_ai_overview(self, params):
        """Consolidated AI Diffusion state in one round-trip: status + regions +
        controls + recent jobs + styles. Lets the client know exactly where it
        stands before/after a change without chaining 5 calls."""
        status = self.cmd_ai_status(params)
        if status.get("error"):
            return status
        if status.get("document") is None:
            return {"status": "ok", "ai": status}
        regions = self.cmd_ai_list_regions({})
        controls = self.cmd_ai_list_controls({})
        jobs = self.cmd_ai_list_jobs({"limit": int(params.get("jobs_limit", 5))})
        styles = self.cmd_ai_list_styles({"limit": int(params.get("styles_limit", 30))})
        return {
            "status": "ok",
            "ai": status,
            "regions": regions.get("regions", []),
            "root_prompt": regions.get("root", {}),
            "controls": {"root": controls.get("root", []), "regions": controls.get("regions", [])},
            "recent_jobs": jobs.get("jobs", []),
            "styles": styles.get("styles", []),
        }

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
        status["model"] = self._style_model_info(root, model.style)
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
                layers.append({"id": l.id.toString(), "name": l.name, "type": l.type.name})
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
            "active_layer_id": active_layer.id.toString() if active_layer else None,
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
                if any(l.id == QUuid(layer_id) for l in r.layers):
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
            "layer_id": control.layer_id.toString() if control.layer_id is not None else None,
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
