"""
Krita MCP Bridge - HTTP server for external paint commands in Krita
Allows Claude (or any MCP client) to paint by sending commands to this plugin.
"""

from krita import *

# Krita 6 ships PyQt6 (Qt6), Krita 5 ships PyQt5 (Qt5). Krita's own `krita`
# module blocks importing the *other* binding (ModuleNotFoundError), so trying
# PyQt6 first and falling back to PyQt5 always picks the right one — the same
# pattern Krita 6's bundled plugins use. Everything below sticks to APIs that
# exist in both: fully-scoped enums (Qt.PenStyle.NoPen, QImage.Format.Format_X,
# ...), which PyQt5 5.11+ also accepts, and no exec_().
try:
    from PyQt6.QtCore import (
        QTimer, QThread, pyqtSignal, QObject, Qt, QPointF, QRectF, QUuid,
        QByteArray, QBuffer, QIODevice,
    )
    from PyQt6.QtGui import (
        QColor, QImage, QPainter, QPen, QBrush, QPainterPath,
    )
    from PyQt6.QtWidgets import QMessageBox, QApplication
except ImportError:
    from PyQt5.QtCore import (
        QTimer, QThread, pyqtSignal, QObject, Qt, QPointF, QRectF, QUuid,
        QByteArray, QBuffer, QIODevice,
    )
    from PyQt5.QtGui import (
        QColor, QImage, QPainter, QPen, QBrush, QPainterPath,
    )
    from PyQt5.QtWidgets import QMessageBox, QApplication
import base64
import difflib
import json
import sys
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
import os

try:
    from .modal import ModalGuard
except ImportError:  # cargado como módulo suelto
    from modal import ModalGuard

# Configuration - customize these as needed
# KRITAMCP_PORT: a second Krita (tests, another profile) can serve on its
# own port; point kri at it with KRITA_URL.
SERVER_PORT = int(os.environ.get("KRITAMCP_PORT", "5678"))
CANVAS_OUTPUT_DIR = os.path.expanduser("~/krita-mcp-output")
# Optional shared-token auth: if set (same value as the MCP server's
# KRITAMCP_TOKEN), every request must carry it in the X-Kritamcp-Token header.
# Empty = no token check (still safe for local use thanks to the Origin guard).
AUTH_TOKEN = os.environ.get("KRITAMCP_TOKEN", "")

# Blending modes (composite op ids) by category, as registered by Krita's
# KoCompositeOpRegistry (5.3 / 6.0). libkis' Node.setBlendingMode does NOT
# validate: an unknown id silently renders as "normal", so we check here.
BLEND_MODES = {
    "mix": ["normal", "behind", "greater", "overlay", "lambert_lighting",
            "lambert_lighting_gamma2.2", "erase", "alphadarken", "marker",
            "hard mix", "hard_mix_hdr", "hard_mix_photoshop",
            "hard_mix_softer_photoshop", "grain_merge", "grain_extract",
            "parallel", "allanon", "geometric_mean", "destination-atop",
            "destination-in", "hard overlay", "hard_overlay_hdr",
            "interpolation", "interpolation 2x", "penumbra a", "penumbra b",
            "penumbra c", "penumbra d"],
    "arithmetic": ["add", "subtract", "multiply", "divide", "inverse_subtract"],
    "dark": ["burn", "linear_burn", "darken", "gamma_dark", "darker color",
             "shade_ifs_illusions", "fog_darken_ifs_illusions", "easy burn"],
    "light": ["dodge", "dodge_hdr", "linear_dodge", "lighten", "linear light",
              "screen", "pin_light", "vivid_light", "vivid_light_hdr",
              "flat_light", "hard_light", "soft_light_ifs_illusions",
              "soft_light_pegtop_delphi", "soft_light", "soft_light_svg",
              "gamma_light", "gamma_illumination", "lighter color", "pnorm_a",
              "pnorm_b", "super_light", "tint_ifs_illusions",
              "fog_lighten_ifs_illusions", "easy dodge", "luminosity_sai"],
    "negative": ["diff", "equivalence", "additive_subtractive", "exclusion",
                 "arc_tangent", "negation"],
    "binary": ["xor", "or", "and", "nand", "nor", "xnor", "implication",
               "not_implication", "converse", "not_converse"],
    "modulo": ["modulo", "modulo_continuous", "divisive_modulo",
               "divisive_modulo_continuous", "modulo_shift",
               "modulo_shift_continuous"],
    "hsy": ["color", "hue", "tint", "saturation", "luminize", "dec_saturation",
            "inc_saturation", "dec_luminosity", "inc_luminosity"],
    "hsi": ["color_hsi", "hue_hsi", "saturation_hsi", "intensity",
            "dec_saturation_hsi", "inc_saturation_hsi", "dec_intensity",
            "inc_intensity"],
    "hsl": ["color_hsl", "hue_hsl", "saturation_hsl", "lightness",
            "dec_saturation_hsl", "inc_saturation_hsl", "dec_lightness",
            "inc_lightness"],
    "hsv": ["color_hsv", "hue_hsv", "saturation_hsv", "value",
            "dec_saturation_hsv", "inc_saturation_hsv", "dec_value",
            "inc_value"],
    "quadratic": ["reflect", "glow", "freeze", "heat", "glow_heat",
                  "heat_glow", "reflect_freeze", "freeze_reflect",
                  "heat_glow_freeze_reflect_hybrid"],
    "misc": ["bumpmap", "combine_normal", "dissolve", "copy_red",
             "copy_green", "copy_blue", "copy", "tangent_normalmap"],
}
BLEND_IDS = {m for modes in BLEND_MODES.values() for m in modes}
# Friendly names (Photoshop / CSS style) -> Krita id. Keys are normalized:
# lowercase, with "-" for any run of spaces/underscores/hyphens.
BLEND_ALIASES = {
    "over": "normal",
    "soft-light": "soft_light",
    "hard-light": "hard_light",
    "color-burn": "burn",
    "color-dodge": "dodge",
    "linear-burn": "linear_burn",
    "linear-dodge": "linear_dodge",
    "linear-light": "linear light",
    "vivid-light": "vivid_light",
    "pin-light": "pin_light",
    "hard-mix": "hard mix",
    "difference": "diff",
    "addition": "add",
    "plus": "add",
    "luminosity": "luminize",
    "darker-color": "darker color",
    "lighter-color": "lighter color",
    "grain-merge": "grain_merge",
    "grain-extract": "grain_extract",
}
PLACEMENT_KEYS = ("above", "below", "into", "top", "bottom")
# Perfil de la capa RGBA8 intermedia: PNG/JPG y colores hex son sRGB.
SRGB_PROFILE = "sRGB-elle-V2-srgbtrc.icc"

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


def describe_modal(w):
    """Lo que se puede leer de un diálogo modal, para el error de kri."""
    info = {"class": w.metaObject().className(), "title": w.windowTitle()}
    if isinstance(w, QMessageBox):
        info["text"] = w.text()
        if w.informativeText():
            info["informative"] = w.informativeText()
        if w.detailedText():
            info["detailed"] = w.detailedText()
        info["buttons"] = [b.text().replace("&", "") for b in w.buttons()]
    else:
        try:
            from PyQt6.QtWidgets import QLabel, QAbstractButton
        except ImportError:
            from PyQt5.QtWidgets import QLabel, QAbstractButton
        info["text"] = "\n".join(l.text() for l in w.findChildren(QLabel)
                                 if l.text())[:2000]
        info["buttons"] = [b.text().replace("&", "")
                           for b in w.findChildren(QAbstractButton)
                           if b.isVisible() and b.text()][:12]
    return info


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
        # `kri sam start`: resultado del último ensure del daemon de AI Select
        # (lo escribe un hilo del manager; kri lo lee con sam_status).
        self._sam_start = None
        self._sam_seq = 0

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
            dispatcher.pushed.connect(self.process_commands, Qt.ConnectionType.QueuedConnection)

        # Low-frequency fallback timer in case a signal is ever missed (e.g.
        # a command queued before the dispatcher was wired up). Not the hot path.
        if self.timer is None:
            self.timer = QTimer()
            self.timer.timeout.connect(self.process_commands)
            self.timer.start(250)

        # Vigía de diálogos modales, en un timer PROPIO: Qt no vuelve a
        # disparar un timer mientras su slot sigue corriendo, y si el comando
        # trabado lo arrancó el timer de respaldo, ese timer queda mudo
        # durante todo el diálogo.
        if getattr(self, "modal_timer", None) is None:
            self.modal_timer = QTimer()
            self.modal_timer.timeout.connect(self._watch_modal)
            self.modal_timer.start(300)

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
        """Drain and execute every queued command on the main thread.

        Guarded against re-entrancy: if a command in ejecución procesa eventos
        Qt (waitForDone, acciones nativas), este slot puede volver a dispararse
        — ejecutar OTRO comando en ese punto muta el documento en medio del
        primero y crashea libkis. Los comandos nuevos esperan al drain actual.
        """
        guard = self._modal_guard()
        if getattr(self, "_processing_commands", False):
            # Estamos dentro de un comando que procesa eventos. Si ese
            # comando abrió un diálogo modal, se le contesta al cliente y a
            # los que esperan detrás, en vez de dejarlos colgados.
            if guard.check() is not None:
                guard.answer_queued(self.execute_command)
            return
        self._processing_commands = True
        try:
            while True:
                item = command_queue.pop()
                if item is None:
                    return
                command_id, command = item
                guard.begin(command_id, (command or {}).get("action"))
                try:
                    result = self.execute_command(command)
                finally:
                    guard.end()
                command_queue.set_result(command_id, result)
        finally:
            self._processing_commands = False

    def _watch_modal(self):
        if getattr(self, "_processing_commands", False):
            guard = self._modal_guard()
            if guard.check() is not None:
                guard.answer_queued(self.execute_command)

    def _modal_guard(self):
        g = getattr(self, "_modal", None)
        if g is None:
            g = self._modal = ModalGuard(
                command_queue, QApplication.activeModalWidget,
                describe_modal)
        return g

    def cmd_modal_dialog(self, params):
        """El diálogo modal abierto en Krita (o None). `dismiss`: cerrarlo
        con reject(), lo mismo que Esc o la X. Se ejecuta aun con Krita
        bloqueado por ese diálogo."""
        m = QApplication.activeModalWidget()
        if m is None:
            return {"status": "ok", "modal": None, "dismissed": False}
        info = describe_modal(m)
        dismissed = False
        if params.get("dismiss"):
            if hasattr(m, "reject"):
                m.reject()
            else:
                m.close()
            dismissed = True
        return {"status": "ok", "modal": info, "dismissed": dismissed}

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
        order is BGRA in memory, which is exactly QImage.Format.Format_ARGB32 on
        little-endian, so the byte buffers map without conversion.
        """
        x, y, w, h = bbox
        if w <= 0 or h <= 0:
            return False

        existing = bytes(layer.pixelData(x, y, w, h))
        base = QImage(existing, w, h, QImage.Format.Format_ARGB32).copy()

        overlay = QImage(w, h, QImage.Format.Format_ARGB32)
        overlay.fill(0)  # fully transparent
        painter = QPainter(overlay)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        draw_fn(painter)
        painter.end()

        if feather and feather > 0:
            # Cheap, dependency-free blur: downscale then upscale with bilinear
            # smoothing softens the alpha edge. Scaling is C++ fast.
            f = max(1, int(feather))
            sw = max(1, w // (f + 1))
            sh = max(1, h // (f + 1))
            overlay = (overlay
                       .scaled(sw, sh, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
                       .scaled(w, h, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation))

        comp = QPainter(base)
        comp.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
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
        img = QImage(w, h, QImage.Format.Format_ARGB32)
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
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
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
            painter.setPen(Qt.PenStyle.NoPen)
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
                pen.setCapStyle(Qt.PenCapStyle.RoundCap)
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
                    painter.setBrush(Qt.BrushStyle.NoBrush)
                if stroke or not fill:
                    painter.setPen(QPen(qcolor, int(params.get("line_width", 2))))
                else:
                    painter.setPen(Qt.PenStyle.NoPen)
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
        buf.open(QIODevice.OpenModeFlag.WriteOnly)
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

        # Document.projection() hands back an sRGB QImage whatever the image
        # colour space (GRAYA / 16-bit / float), unlike raw pixel bytes.
        img = doc.projection(int(x), int(y), 1, 1)
        if img is None or img.isNull():
            return {"error": "Could not read pixel"}
        c = img.pixelColor(0, 0)
        r, g, b, a = c.red(), c.green(), c.blue(), c.alpha()
        hex_color = "#{:02x}{:02x}{:02x}".format(r, g, b)
        return {"status": "ok", "color": hex_color, "r": r, "g": g, "b": b, "a": a}

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

        # Batchmode mientras se abre: si la carga trae avisos (un perfil de
        # color que falta, capas que no se pudieron leer…), Krita los muestra
        # en un QMessageBox modal que deja al comando colgado hasta que
        # alguien lo cierre. En batchmode los anota en el log y sigue.
        old_batch = app.batchmode()
        app.setBatchmode(True)
        try:
            doc = app.openDocument(filepath)
        finally:
            app.setBatchmode(old_batch)
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

    # ----- Selections -----
    # AI Diffusion generates only inside the active selection (inpaint), so
    # these are the precision tools for targeted generation and edits.

    # Cada modo mapea a la variante nativa de "Select Opaque".
    SELECT_OPAQUE_ACTIONS = {
        "replace": "selectopaque",
        "add": "selectopaque_add",
        "subtract": "selectopaque_subtract",
        "intersect": "selectopaque_intersect",
    }

    def _selection_bounds(self, doc):
        """Bounds of the current selection as a result fragment."""
        sel = doc.selection()
        if sel is None:
            return {"selection": None}
        return {"selection": {"x": sel.x(), "y": sel.y(),
                              "width": sel.width(), "height": sel.height()}}

    def _mask_image(self, doc, draw_fn):
        """Canvas-sized Grayscale8 mask painted by draw_fn (antialiased)."""
        w, h = doc.width(), doc.height()
        img = QImage(w, h, QImage.Format.Format_Grayscale8)
        img.fill(0)
        painter = QPainter(img)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(QColor(255, 255, 255)))
        draw_fn(painter)
        painter.end()
        return img

    def _select_via_layer(self, doc, gray_img, mode):
        """Mask -> selection through Krita's own Select Opaque on a temp layer.

        This is the ONLY path that yields visible marching ants: selections
        built with Selection.setPixelData never get an outline cache, so the
        canvas draws no dotted line for them. Going through the native action
        also gives us add/subtract/intersect for free. Returns an error dict
        or None."""
        action_name = self.SELECT_OPAQUE_ACTIONS.get(mode)
        if action_name is None:
            return {"error": (f"Unknown mode '{mode}'. Valid: "
                              f"{list(self.SELECT_OPAQUE_ACTIONS)}")}
        if mode in ("subtract", "intersect") and doc.selection() is None:
            return {"error": f"No existing selection to {mode} from"}
        w, h = doc.width(), doc.height()
        bpl = gray_img.bytesPerLine()
        raw = gray_img.constBits().asstring(bpl * h)
        tight = raw if bpl == w else b"".join(
            raw[i * bpl:i * bpl + w] for i in range(h))
        # Capa temporal cuyo alpha ES la máscara (ARGB gris = alpha directo).
        argb = bytearray(w * h * 4)
        argb[0::4] = tight
        argb[1::4] = tight
        argb[2::4] = tight
        argb[3::4] = tight
        prev = doc.activeNode()
        tmp = doc.createNode("__kri_select_tmp", "paintlayer")
        doc.rootNode().addChildNode(tmp, None)
        try:
            # The bytes are RGBA8; on a GRAYA / 16-bit / float document the
            # temp layer must be RGBA8 too or they land as garbage.
            if not self._is_rgba8(tmp):
                tmp.setColorSpace("RGBA", "U8", self._profile_for("RGBA", "U8"))
            tmp.setPixelData(bytes(argb), 0, 0, w, h)
            doc.setActiveNode(tmp)
            doc.waitForDone()
            action = Krita.instance().action(action_name)
            if action is None:
                return {"error": f"Krita action not found: {action_name}"}
            action.trigger()
            # NO processEvents acá: reentraría el dispatcher de comandos con
            # la capa temporal viva (mutación concurrente → crash en libkis).
            doc.waitForDone()
        finally:
            if prev is not None:
                doc.setActiveNode(prev)
            tmp.remove()
        self._maybe_refresh(doc)
        return None

    def _rebuild_outline(self, doc):
        """Re-materialize the current selection through the native path so
        its outline (marching ants) is regenerated after API-side ops like
        invert/feather/grow that invalidate it."""
        sel = doc.selection()
        if sel is None:
            return
        w, h = doc.width(), doc.height()
        data = bytes(sel.pixelData(0, 0, w, h))
        img = QImage(data, w, h, w, QImage.Format.Format_Grayscale8).copy()
        self._select_via_layer(doc, img, "replace")

    def cmd_select_shape(self, params):
        """Select a rectangle, ellipse or polygon, combined per `mode`."""
        doc = self.get_active_document()
        if not doc:
            return {"error": "No active document"}
        shape = params.get("shape", "rectangle")
        mode = params.get("mode", "replace")

        if shape == "polygon":
            points = params.get("points") or []
            if len(points) < 3:
                return {"error": "polygon needs at least 3 points"}
            path = QPainterPath()
            path.moveTo(points[0][0], points[0][1])
            for p in points[1:]:
                path.lineTo(p[0], p[1])
            path.closeSubpath()

            def draw(painter):
                painter.drawPath(path)
        elif shape in ("rectangle", "ellipse"):
            rect = QRectF(float(params.get("x", 0)), float(params.get("y", 0)),
                          float(params.get("width", 100)),
                          float(params.get("height", 100)))

            def draw(painter):
                if shape == "rectangle":
                    painter.drawRect(rect)
                else:
                    painter.drawEllipse(rect)
        else:
            return {"error": f"Unknown shape '{shape}'"}

        err = self._select_via_layer(doc, self._mask_image(doc, draw), mode)
        if err:
            return err
        return {"status": "ok", "shape": shape, "mode": mode,
                **self._selection_bounds(doc)}

    def cmd_select_all(self, params):
        """Select the whole canvas."""
        doc = self.get_active_document()
        if not doc:
            return {"error": "No active document"}
        sel = Selection()
        sel.select(0, 0, doc.width(), doc.height(), 255)
        doc.setSelection(sel)
        return {"status": "ok", **self._selection_bounds(doc)}

    def cmd_select_none(self, params):
        """Deselect (clear the global selection)."""
        doc = self.get_active_document()
        if not doc:
            return {"error": "No active document"}
        doc.setSelection(None)
        return {"status": "ok", "selection": None}

    def cmd_select_invert(self, params):
        """Invert the current selection."""
        doc = self.get_active_document()
        if not doc:
            return {"error": "No active document"}
        sel = doc.selection()
        if sel is None:
            return {"error": "No active selection to invert (use select_all first)"}
        sel.invert()
        doc.setSelection(sel)
        self._rebuild_outline(doc)
        return {"status": "ok", **self._selection_bounds(doc)}

    def cmd_select_modify(self, params):
        """Refine the current selection: feather, grow, shrink or border."""
        doc = self.get_active_document()
        if not doc:
            return {"error": "No active document"}
        sel = doc.selection()
        if sel is None:
            return {"error": "No active selection"}
        op = params.get("op", "")
        radius = int(params.get("radius", 0))
        if radius <= 0:
            return {"error": "radius must be > 0"}
        if op == "feather":
            sel.feather(radius)
        elif op == "grow":
            sel.grow(radius, radius)
        elif op == "shrink":
            sel.shrink(radius, radius, False)
        elif op == "border":
            sel.border(radius, radius)
        else:
            return {"error": f"Unknown op '{op}'. Valid: feather, grow, shrink, border"}
        doc.setSelection(sel)
        self._rebuild_outline(doc)
        return {"status": "ok", "op": op, "radius": radius,
                **self._selection_bounds(doc)}

    def cmd_select_info(self, params):
        """Bounds of the current selection (null if none)."""
        doc = self.get_active_document()
        if not doc:
            return {"error": "No active document"}
        return {"status": "ok", **self._selection_bounds(doc)}

    def cmd_select_from_mask(self, params):
        """Set the selection from a grayscale mask image (base64 PNG/JPEG).

        White = selected, black = not, greys = partial. This is how external
        segmenters (krita-autoselect / SAM) hand their result to Krita. The
        mask is scaled to canvas size if it doesn't match.
        """
        doc = self.get_active_document()
        if not doc:
            return {"error": "No active document"}
        mask_b64 = params.get("mask_b64")
        if not mask_b64:
            return {"error": "mask_b64 required"}
        mode = params.get("mode", "replace")
        try:
            raw = base64.b64decode(mask_b64)
        except Exception as e:
            return {"error": f"Invalid base64 mask: {e}"}
        img = QImage()
        if not img.loadFromData(raw) or img.isNull():
            return {"error": "Could not decode mask image"}
        w, h = doc.width(), doc.height()
        if img.width() != w or img.height() != h:
            img = img.scaled(w, h, Qt.AspectRatioMode.IgnoreAspectRatio, Qt.TransformationMode.SmoothTransformation)
        img = img.convertToFormat(QImage.Format.Format_Grayscale8)
        err = self._select_via_layer(doc, img, mode)
        if err:
            return err
        return {"status": "ok", "mode": mode, **self._selection_bounds(doc)}

    # ----- Layers -----
    # A layer reference ("ref") is a uniqueId (braces optional), a
    # "Group/Sub/Layer" path from the top level, or a unique layer name.
    # Opacity travels as a percentage (0-100) in both directions.

    @staticmethod
    def _node_id(node):
        return node.uniqueId().toString().strip("{}").lower()

    def _same(self, a, b):
        return (a is not None and b is not None
                and self._node_id(a) == self._node_id(b))

    @staticmethod
    def _children(parent):
        """childNodes() minus Krita's global selection mask: while a global
        selection exists, the root holds a hidden "Selection Mask" node that
        the Layers docker does not show and nothing here should touch."""
        kids = parent.childNodes()
        if parent.parentNode() is None:
            kids = [k for k in kids if k.type() != "selectionmask"]
        return kids

    def _walk(self, parent, prefix="", depth=0):
        """(node, path, depth) for every node under parent, top-to-bottom
        (the Layers docker order; childNodes() is bottom-to-top)."""
        for child in reversed(self._children(parent)):
            path = f"{prefix}/{child.name()}" if prefix else child.name()
            yield child, path, depth
            yield from self._walk(child, path, depth + 1)

    def _path(self, node):
        names = []
        while node is not None:
            parent = node.parentNode()
            if parent is None:  # root
                break
            names.append(node.name())
            node = parent
        return "/".join(reversed(names))

    def _resolve(self, doc, ref, types=None, what="layer"):
        """(node, None) or (None, error_dict). `types` restricts node.type()."""
        if ref is None or str(ref).strip() == "":
            return None, {"error": f"A {what} reference is required"}
        ref = str(ref)
        entries = [e for e in self._walk(doc.rootNode())
                   if types is None or e[0].type() in types]
        key = ref.strip().strip("{}").lower()
        for node, path, _ in entries:
            if self._node_id(node) == key:
                return node, None
        # A path only when it has a "/" (a leading "/" pins a top-level
        # layer): a bare name must report ambiguity across all groups.
        matchers = [lambda p, n: p == ref.strip("/")] if "/" in ref else []
        matchers += [lambda p, n: n.name() == ref,
                     lambda p, n: n.name().lower() == ref.lower()]
        for matcher in matchers:
            hits = [(n, p) for n, p, _ in entries if matcher(p, n)]
            if len(hits) == 1:
                return hits[0][0], None
            if len(hits) > 1:
                cands = [{"path": p, "id": self._node_id(n), "type": n.type()}
                         for n, p in hits]
                return None, {
                    "error": (f"Ambiguous {what} '{ref}': {len(hits)} match "
                              f"({', '.join(c['path'] for c in cands)}). "
                              f"Use a Group/Layer path or the id."),
                    "candidates": cands}
        names = sorted({p for _, p, _ in entries} | {n.name() for n, _, _ in entries})
        close = difflib.get_close_matches(ref, names, n=5, cutoff=0.5)
        err = {"error": f"{what.capitalize()} not found: '{ref}'"
               + (f". Did you mean: {', '.join(close)}?" if close else "")}
        return None, err

    def _layer_info(self, doc, node, path=None, active_id=None, bounds=True):
        """bounds: True = exact content bounds (scans freshly written pixels:
        ~0.4 s on a full-size layer), a known (x, y, w, h), or False to omit."""
        if active_id is None:
            active = doc.activeNode()
            active_id = self._node_id(active) if active is not None else ""
        r = self._bounds(node) if bounds is True else bounds
        info = {
            "name": node.name(),
            "path": path if path is not None else self._path(node),
            "type": node.type(),
            "id": self._node_id(node),
            "blend": node.blendingMode(),
            "opacity": round(node.opacity() * 100 / 255),
            "visible": node.visible(),
            "locked": node.locked(),
            "alpha_locked": node.alphaLocked(),
            "inherit_alpha": node.inheritAlpha(),
            "active": self._node_id(node) == active_id,
        }
        if bounds is not False:
            info["bounds"] = (None if r is None else
                              {"x": r[0], "y": r[1], "width": r[2], "height": r[3]})
        return info

    @staticmethod
    def _bounds(node):
        """(x, y, w, h) of the node's content, None if empty. A selection
        mask's own bounds are its tile-aligned extent: use the selection's."""
        if node.type() == "selectionmask":
            sel = node.selection()
            if sel is None or sel.width() <= 0 or sel.height() <= 0:
                return None
            return sel.x(), sel.y(), sel.width(), sel.height()
        r = node.bounds()
        if r.isEmpty():
            return None
        return r.x(), r.y(), r.width(), r.height()

    def _blend_id(self, mode):
        """(krita_id, None) or (None, error_dict). Accepts Krita ids, ids
        written with -/_/space variations and friendly aliases."""
        s = str(mode).strip().lower()
        variants = [s, s.replace("-", "_"), s.replace("-", " "),
                    s.replace("_", " "), s.replace(" ", "_")]
        for v in variants:
            if v in BLEND_IDS:
                return v, None
        norm = "-".join(t for t in s.replace("_", " ").replace("-", " ").split())
        if norm in BLEND_ALIASES:
            return BLEND_ALIASES[norm], None
        close = difflib.get_close_matches(
            s, sorted(BLEND_IDS | set(BLEND_ALIASES)), n=5, cutoff=0.5)
        return None, {
            "error": (f"Unknown blending mode '{mode}'"
                      + (f". Did you mean: {', '.join(close)}?" if close else "")
                      + " (`kri layer blends` lists them all)"),
            "suggestions": close}

    def _placement(self, doc, params):
        """Where to put a node: (spec, None) / (None, error) / (None, None)
        when no placement was given. spec = (parent, where, ref) with where
        in top/bottom/above/below."""
        given = [k for k in PLACEMENT_KEYS if params.get(k) not in (None, False, "")]
        if not given:
            return None, None
        if len(given) > 1:
            return None, {"error": f"Use only one placement, got {given}"}
        key = given[0]
        root = doc.rootNode()
        if key in ("top", "bottom"):
            return (root, key, None), None
        ref_node, err = self._resolve(doc, params[key])
        if err:
            return None, err
        if key == "into":
            if ref_node.type() != "grouplayer":
                return None, {"error": f"--into needs a group layer, "
                                       f"'{ref_node.name()}' is {ref_node.type()}"}
            return (ref_node, "top", None), None
        parent = ref_node.parentNode()
        if parent is None or not (parent.parentNode() is None
                                  or parent.type() == "grouplayer"):
            return None, {"error": f"Cannot place a layer next to the mask "
                                   f"'{ref_node.name()}'"}
        return (parent, key, ref_node), None

    def _default_placement(self, doc):
        """Above the active layer (above a mask's layer), else top of root."""
        node = doc.activeNode()
        while node is not None and node.parentNode() is not None \
                and node.type().endswith("mask"):
            node = node.parentNode()
        if node is None or node.parentNode() is None:
            return doc.rootNode(), "top", None
        return node.parentNode(), "above", node

    def _insert(self, node, spec):
        """Add an unattached node at spec = (parent, where, ref)."""
        parent, where, ref = spec
        siblings = [c for c in self._children(parent) if not self._same(c, node)]
        if where == "above":
            parent.addChildNode(node, ref)
            return
        if where == "below":
            idx = next(i for i, c in enumerate(siblings) if self._same(c, ref))
            if idx > 0:
                parent.addChildNode(node, siblings[idx - 1])
                return
            where = "bottom"
        if not siblings:
            parent.addChildNode(node, None)  # libkis: None = top of parent
            return
        if where == "top":  # above the topmost layer (not above Krita's
            parent.addChildNode(node, siblings[-1])  # global selection mask)
            return
        # bottom: libkis can only insert above a node, so go above the current
        # bottom one and then lift that one above us.
        low = siblings[0]
        parent.addChildNode(node, low)
        self._move(low, (parent, "above", node))

    def _subtree(self, node):
        return [(c, self._subtree(c)) for c in node.childNodes()]

    def _restore(self, node, tree):
        for child, sub in tree:  # bottom-to-top, each added on top
            node.addChildNode(child, None)
            self._restore(child, sub)

    def _move(self, node, spec):
        """Move an attached node. libkis has no move: Node.remove() runs
        Krita's remove command, which detaches the children one by one, so
        snapshot the subtree and put it back (same nodes, same ids)."""
        tree = self._subtree(node)
        node.remove()
        self._insert(node, spec)
        self._restore(node, tree)

    def _check_target(self, node, spec):
        """Refuse moving a node next to/into itself or its own descendants."""
        parent, _, ref = spec
        for probe in (parent, ref):
            cur = probe
            while cur is not None:
                if self._same(cur, node):
                    return {"error": f"Cannot place '{node.name()}' relative "
                                     f"to itself or its own children"}
                cur = cur.parentNode()
        return None

    def _apply_props(self, node, params, blend_id=None):
        if blend_id:
            node.setBlendingMode(blend_id)
        if params.get("opacity") is not None:
            pct = max(0.0, min(100.0, float(params["opacity"])))
            node.setOpacity(int(round(pct * 255 / 100)))
        if params.get("hidden"):
            node.setVisible(False)

    def _activate(self, doc, node):
        doc.setActiveNode(node)
        doc.waitForDone()

    def _profile_for(self, model, depth):
        names = list(Krita.instance().profiles(model, depth) or [])
        prefer = {"RGBA": SRGB_PROFILE, "GRAYA": "Gray-D50-elle-V2-srgbtrc.icc"}
        if prefer.get(model) in names:
            return prefer[model]
        srgb = [n for n in names if "srgb" in n.lower()]
        return (srgb or names or [""])[0]

    @staticmethod
    def _is_rgba8(node):
        return node.colorModel() == "RGBA" and node.colorDepth() == "U8"

    def _gray_warning(self, node):
        if node.colorModel().startswith("GRAY"):
            return ("the layer is grayscale (the document is "
                    f"{node.colorModel()}/{node.colorDepth()}): colours were "
                    "converted to gray. Run `kri colorspace RGBA U8` first "
                    "to keep them.")
        return None

    def _write_rgba8(self, doc, node, data, x, y, w, h, fresh=False):
        """Write BGRA8 bytes (QImage ARGB32 layout) into node, whatever its
        colour space. fresh=True: node is new/empty, convert it in place
        (RGBA8 -> write -> back); otherwise go through a temporary RGBA8
        layer so the node's other pixels keep their precision."""
        if self._is_rgba8(node):
            node.setPixelData(data, x, y, w, h)
            return None
        target = (node.colorModel(), node.colorDepth(), node.colorProfile())
        if fresh:
            if not node.setColorSpace("RGBA", "U8", self._profile_for("RGBA", "U8")):
                return {"error": "Could not convert the layer to RGBA 8-bit"}
            node.setPixelData(data, x, y, w, h)
            node.setColorSpace(*target)
            return None
        tmp = doc.createNode("__kri_tmp", "paintlayer")
        doc.rootNode().addChildNode(tmp, None)
        try:
            tmp.setColorSpace("RGBA", "U8", self._profile_for("RGBA", "U8"))
            tmp.setPixelData(data, x, y, w, h)
            tmp.setColorSpace(*target)
            node.setPixelData(bytes(tmp.pixelData(x, y, w, h)), x, y, w, h)
        finally:
            tmp.remove()
        return None

    def _read_rgba8(self, doc, node, x, y, w, h, projection=True):
        """Node pixels (projection by default) as BGRA8 bytes."""
        raw = bytes(node.projectionPixelData(x, y, w, h) if projection
                    else node.pixelData(x, y, w, h))
        model, depth = node.colorModel(), node.colorDepth()
        if model == "RGBA" and depth == "U8":
            return raw
        if model == "GRAYA" and depth == "U8":
            gray, alpha = raw[0::2], raw[1::2]
            out = bytearray(w * h * 4)
            out[0::4] = gray
            out[1::4] = gray
            out[2::4] = gray
            out[3::4] = alpha
            return bytes(out)
        tmp = doc.createNode("__kri_tmp", "paintlayer")
        doc.rootNode().addChildNode(tmp, None)
        try:
            if (tmp.colorModel(), tmp.colorDepth()) != (model, depth):
                tmp.setColorSpace(model, depth, node.colorProfile())
            tmp.setPixelData(raw, x, y, w, h)
            tmp.setColorSpace("RGBA", "U8", self._profile_for("RGBA", "U8"))
            return bytes(tmp.pixelData(x, y, w, h))
        finally:
            tmp.remove()

    def _selection_rect_and_mask(self, doc):
        """(x, y, w, h), mask bytes of the global selection clipped to the
        canvas — or an error dict."""
        sel = doc.selection()
        if sel is None:
            return None, None, {"error": "No active selection"}
        x0, y0 = max(0, sel.x()), max(0, sel.y())
        x1 = min(doc.width(), sel.x() + sel.width())
        y1 = min(doc.height(), sel.y() + sel.height())
        if x1 <= x0 or y1 <= y0:
            return None, None, {"error": "The selection is empty"}
        rect = (x0, y0, x1 - x0, y1 - y0)
        return rect, bytes(sel.pixelData(*rect)), None

    def _paint_color(self, doc, node, qcolor, rect, mask=None, fresh=False):
        """Fill rect of node with qcolor; with mask (selection bytes) only
        where selected, weighted by its value. Alpha-locked layers keep
        their alpha (SourceAtop), like painting with alpha lock in Krita."""
        x, y, w, h = rect
        alpha_locked = node.alphaLocked()
        if mask is None and not alpha_locked:
            img = QImage(w, h, QImage.Format.Format_ARGB32)
            img.fill(qcolor)
            return self._write_rgba8(doc, node, img.constBits().asstring(w * h * 4),
                                     x, y, w, h, fresh=fresh)
        if fresh:
            base = QImage(w, h, QImage.Format.Format_ARGB32)
            base.fill(0)
        else:
            raw = self._read_rgba8(doc, node, x, y, w, h, projection=False)
            base = QImage(raw, w, h, w * 4, QImage.Format.Format_ARGB32).copy()
        overlay = QImage(w, h, QImage.Format.Format_ARGB32_Premultiplied)
        overlay.fill(qcolor)
        if mask is not None:
            alpha = QImage(mask, w, h, w, QImage.Format.Format_Alpha8)
            p = QPainter(overlay)
            p.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationIn)
            p.drawImage(0, 0, alpha)
            p.end()
        p = QPainter(base)
        p.setCompositionMode(
            QPainter.CompositionMode.CompositionMode_SourceAtop if alpha_locked
            else QPainter.CompositionMode.CompositionMode_SourceOver)
        p.drawImage(0, 0, overlay)
        p.end()
        return self._write_rgba8(doc, node, base.constBits().asstring(w * h * 4),
                                 x, y, w, h, fresh=fresh)

    def _color_param(self, params, key="color"):
        value = params.get(key)
        if value is None:
            return None, None
        c = QColor(str(value))
        if not c.isValid():
            return None, {"error": f"Invalid colour '{value}' (use #RRGGBB)"}
        c.setAlpha(255)
        return c, None

    def cmd_layer_list(self, params):
        """The layer tree, top-to-bottom, with every property kri can set."""
        doc = self.get_active_document()
        if not doc:
            return {"error": "No active document"}
        active = doc.activeNode()
        active_id = self._node_id(active) if active is not None else ""

        def tree(parent, prefix):
            out = []
            for child in reversed(self._children(parent)):
                path = f"{prefix}/{child.name()}" if prefix else child.name()
                info = self._layer_info(doc, child, path, active_id)
                kids = tree(child, path)
                if kids:
                    info["children"] = kids
                out.append(info)
            return out

        return {
            "status": "ok",
            "document": {"name": doc.name() or "(unsaved)",
                         "width": doc.width(), "height": doc.height(),
                         "color_model": doc.colorModel(),
                         "color_depth": doc.colorDepth()},
            "active": self._path(active) if active is not None else None,
            "layers": tree(doc.rootNode(), ""),
        }

    def cmd_layer_blends(self, params):
        """Valid blending-mode ids by category + accepted aliases."""
        return {"status": "ok", "modes": BLEND_MODES, "aliases": BLEND_ALIASES}

    def _add_new_node(self, doc, params, node, paint=None):
        """Validate blend/placement, apply props, paint (optional callable
        returning an error dict or None), insert and activate the new node.

        On an RGBA8 node the pixels go in BEFORE inserting it: the add
        command dirties the node itself, so no full refreshProjection is
        needed (the fast path for importing a dozen full-size layers).
        Other colour spaces convert the node, which needs it attached."""
        blend_id = None
        if params.get("blend"):
            blend_id, err = self._blend_id(params["blend"])
            if err:
                return err
        spec, err = self._placement(doc, params)
        if err:
            return err
        self._apply_props(node, params, blend_id)
        early = paint is not None and self._is_rgba8(node)
        if early:
            err = paint()
            if err:
                return err
        self._insert(node, spec or self._default_placement(doc))
        if paint is not None and not early:
            err = paint()
            if err:
                node.remove()
                return err
        self._activate(doc, node)
        if paint is not None and not early:
            self._maybe_refresh(doc)
        return None

    def cmd_layer_new(self, params):
        """New paint or group layer, placed and made active. `color` fills
        it (inside the current selection only when `selection` is true)."""
        doc = self.get_active_document()
        if not doc:
            return {"error": "No active document"}
        name = params.get("name")
        if not name:
            return {"error": "name required"}
        ltype = params.get("type", "paint")
        if ltype not in ("paint", "group"):
            return {"error": f"Unknown layer type '{ltype}' (paint, group)"}
        color, err = self._color_param(params)
        if err:
            return err
        if color is not None and ltype == "group":
            return {"error": "--color only applies to paint layers"}
        rect = mask = None
        if color is not None:
            if params.get("selection"):
                rect, mask, err = self._selection_rect_and_mask(doc)
                if err:
                    return err
            else:
                rect = (0, 0, doc.width(), doc.height())
        node = doc.createNode(name, "paintlayer" if ltype == "paint" else "grouplayer")
        paint = None
        if color is not None:
            def paint():
                return self._paint_color(doc, node, color, rect, mask, fresh=True)
        err = self._add_new_node(doc, params, node, paint)
        if err:
            return err
        warning = self._gray_warning(node) if color is not None else None
        out = {"status": "ok", "layer": self._layer_info(
            doc, node, bounds=rect if color is not None else None)}
        if warning:
            out["warning"] = warning
        return out

    def cmd_layer_import(self, params):
        """Load a PNG/JPG (alpha kept) into a new paint layer at x, y. The
        file is read here, inside Krita, so no pixels travel over HTTP."""
        doc = self.get_active_document()
        if not doc:
            return {"error": "No active document"}
        path = os.path.expanduser(str(params.get("path") or ""))
        if not os.path.isabs(path):
            return {"error": f"path must be absolute: '{path}'"}
        if not os.path.isfile(path):
            return {"error": f"File not found: {path}"}
        img = QImage(path)
        if img.isNull():
            return {"error": f"Could not read image: {path}"}
        img = img.convertToFormat(QImage.Format.Format_ARGB32)
        w, h = img.width(), img.height()
        if img.bytesPerLine() != w * 4:
            return {"error": "Unexpected image stride"}
        data = img.constBits().asstring(w * h * 4)
        x, y = int(params.get("x", 0)), int(params.get("y", 0))
        name = params.get("name") or os.path.splitext(os.path.basename(path))[0]
        node = doc.createNode(name, "paintlayer")
        err = self._add_new_node(
            doc, params, node,
            lambda: self._write_rgba8(doc, node, data, x, y, w, h, fresh=True))
        if err:
            return err
        out = {"status": "ok",
               "layer": self._layer_info(doc, node, bounds=False),
               "image": {"width": w, "height": h, "x": x, "y": y}}
        warning = self._gray_warning(node)
        if warning:
            out["warning"] = warning
        return out

    def cmd_layer_export(self, params):
        """Write a layer's pixels (its projection: masks and children
        included) to a PNG with alpha — canvas-sized, or cropped to the
        layer bounds with `crop`. Selection masks export as grayscale."""
        doc = self.get_active_document()
        if not doc:
            return {"error": "No active document"}
        node, err = self._resolve(doc, params.get("ref"))
        if err:
            return err
        path = os.path.expanduser(str(params.get("path") or ""))
        if not os.path.isabs(path):
            return {"error": f"path must be absolute: '{path}'"}
        if not os.path.isdir(os.path.dirname(path)):
            return {"error": f"Directory does not exist: {os.path.dirname(path)}"}
        if params.get("crop"):
            r = self._bounds(node)
            if r is None:
                return {"error": f"Layer '{node.name()}' is empty"}
            x, y, w, h = r
        else:
            x, y, w, h = 0, 0, doc.width(), doc.height()
        if node.type() == "selectionmask":
            data = bytes(node.selection().pixelData(x, y, w, h))
            img = QImage(data, w, h, w, QImage.Format.Format_Grayscale8)
        else:
            data = self._read_rgba8(doc, node, x, y, w, h)
            img = QImage(data, w, h, w * 4, QImage.Format.Format_ARGB32)
        if not img.save(path, "PNG"):
            return {"error": f"Could not write {path}"}
        return {"status": "ok", "path": path, "layer": self._path(node),
                "x": x, "y": y, "width": w, "height": h}

    def cmd_layer_set(self, params):
        """Change name / blend / opacity / visibility / locks of a layer."""
        doc = self.get_active_document()
        if not doc:
            return {"error": "No active document"}
        node, err = self._resolve(doc, params.get("ref"))
        if err:
            return err
        blend_id = None
        if params.get("blend"):
            blend_id, err = self._blend_id(params["blend"])
            if err:
                return err
        if params.get("alpha_locked") is not None and node.type() != "paintlayer":
            return {"error": "alpha lock only applies to paint layers"}
        changed = []
        if params.get("name"):
            node.setName(str(params["name"]))
            changed.append("name")
        if blend_id:
            node.setBlendingMode(blend_id)
            changed.append("blend")
        if params.get("opacity") is not None:
            self._apply_props(node, {"opacity": params["opacity"]})
            changed.append("opacity")
        for key, setter in (("visible", node.setVisible),
                            ("locked", node.setLocked),
                            ("alpha_locked", node.setAlphaLocked),
                            ("inherit_alpha", node.setInheritAlpha)):
            if params.get(key) is not None:
                setter(bool(params[key]))
                changed.append(key)
        if not changed:
            return {"error": "Nothing to change"}
        self._maybe_refresh(doc)
        return {"status": "ok", "changed": changed,
                "layer": self._layer_info(doc, node)}

    def cmd_layer_move(self, params):
        """Move a layer: above/below another, into a group, top or bottom."""
        doc = self.get_active_document()
        if not doc:
            return {"error": "No active document"}
        node, err = self._resolve(doc, params.get("ref"))
        if err:
            return err
        spec, err = self._placement(doc, params)
        if err:
            return err
        if spec is None:
            return {"error": "Where to? Use above, below, into, top or bottom"}
        err = self._check_target(node, spec)
        if err:
            return err
        prev = doc.activeNode()
        self._move(node, spec)
        if prev is not None:
            self._activate(doc, prev)
        self._maybe_refresh(doc)
        return {"status": "ok", "layer": self._layer_info(doc, node)}

    def cmd_layer_select(self, params):
        """Make a layer the active one."""
        doc = self.get_active_document()
        if not doc:
            return {"error": "No active document"}
        node, err = self._resolve(doc, params.get("ref"))
        if err:
            return err
        self._activate(doc, node)
        return {"status": "ok", "layer": self._layer_info(doc, node)}

    def cmd_layer_duplicate(self, params):
        """Copy a layer (above the original unless placed) and activate it."""
        doc = self.get_active_document()
        if not doc:
            return {"error": "No active document"}
        node, err = self._resolve(doc, params.get("ref"))
        if err:
            return err
        spec, err = self._placement(doc, params)
        if err:
            return err
        if spec is None:
            spec = (node.parentNode(), "above", node)
        dup = node.duplicate()
        dup.setName(params.get("name") or f"{node.name()} copy")
        self._insert(dup, spec)
        self._activate(doc, dup)
        self._maybe_refresh(doc)
        return {"status": "ok", "layer": self._layer_info(doc, dup)}

    def cmd_layer_delete(self, params):
        """Delete one or more layers (`refs` list, or a single `ref`)."""
        doc = self.get_active_document()
        if not doc:
            return {"error": "No active document"}
        refs = params.get("refs") or ([params["ref"]] if params.get("ref") else [])
        if not refs:
            return {"error": "refs required"}
        nodes = {}
        for ref in refs:
            node, err = self._resolve(doc, ref)
            if err:
                return err
            nodes[self._node_id(node)] = node
        removed = []
        for node in nodes.values():
            cur, inside = node.parentNode(), False
            while cur is not None and not inside:  # goes with its group
                inside = self._node_id(cur) in nodes
                cur = cur.parentNode()
            if inside:
                continue
            removed.append(self._path(node))
            node.remove()
        self._maybe_refresh(doc)
        return {"status": "ok", "removed": removed}

    def cmd_layer_group(self, params):
        """Wrap layers in a new group placed where the topmost of them was,
        keeping their stacking order."""
        doc = self.get_active_document()
        if not doc:
            return {"error": "No active document"}
        name = params.get("name")
        refs = params.get("refs") or []
        if not name or not refs:
            return {"error": "name and refs required"}
        order = {self._node_id(n): i for i, (n, _, _) in
                 enumerate(self._walk(doc.rootNode()))}
        nodes = {}
        for ref in refs:
            node, err = self._resolve(doc, ref)
            if err:
                return err
            if node.type().endswith("mask"):
                return {"error": f"'{node.name()}' is a mask, not a layer"}
            nodes[self._node_id(node)] = node
        for node in nodes.values():
            cur = node.parentNode()
            while cur is not None:
                if self._node_id(cur) in nodes:
                    return {"error": f"'{node.name()}' is inside "
                                     f"'{cur.name()}', group only one of them"}
                cur = cur.parentNode()
        ordered = sorted(nodes.values(), key=lambda n: order[self._node_id(n)])
        group = doc.createNode(name, "grouplayer")
        blend_id = None
        if params.get("blend"):
            blend_id, err = self._blend_id(params["blend"])
            if err:
                return err
        self._apply_props(group, params, blend_id)
        top = ordered[0]
        top.parentNode().addChildNode(group, top)
        for node in reversed(ordered):  # bottom-most first, each onto the top
            self._move(node, (group, "top", None))
        self._activate(doc, group)
        self._maybe_refresh(doc)
        return {"status": "ok", "layer": self._layer_info(doc, group),
                "children": [self._path(n) for n in ordered]}

    def cmd_layer_fill(self, params):
        """Fill a paint layer with a colour: the whole canvas area, or only
        inside the current selection (`selection`). Honors alpha lock."""
        doc = self.get_active_document()
        if not doc:
            return {"error": "No active document"}
        node, err = self._resolve(doc, params.get("ref"))
        if err:
            return err
        if node.type() != "paintlayer":
            return {"error": f"Can only fill paint layers ('{node.name()}' "
                             f"is {node.type()})"}
        if node.locked():
            return {"error": f"Layer '{node.name()}' is locked "
                             f"(kri layer set REF --unlock)"}
        color, err = self._color_param(params)
        if err:
            return err
        if color is None:
            return {"error": "color required"}
        mask = None
        if params.get("selection"):
            rect, mask, err = self._selection_rect_and_mask(doc)
            if err:
                return err
        else:
            rect = (0, 0, doc.width(), doc.height())
        err = self._paint_color(doc, node, color, rect, mask)
        if err:
            return err
        self._maybe_refresh(doc)
        out = {"status": "ok", "layer": self._layer_info(doc, node, bounds=False),
               "filled": {"x": rect[0], "y": rect[1],
                          "width": rect[2], "height": rect[3]}}
        warning = self._gray_warning(node)
        if warning:
            out["warning"] = warning
        return out

    def cmd_doc_colorspace(self, params):
        """Report the image colour space, or convert it (model/depth)."""
        doc = self.get_active_document()
        if not doc:
            return {"error": "No active document"}
        before = {"color_model": doc.colorModel(), "color_depth": doc.colorDepth(),
                  "profile": doc.colorProfile()}
        model = params.get("model")
        if not model:
            return {"status": "ok", **before}
        model = str(model).upper()
        depth = str(params.get("depth") or "U8").upper()
        profile = params.get("profile") or self._profile_for(model, depth)
        if not profile or not doc.setColorSpace(model, depth, profile):
            return {"error": (f"Could not convert to {model}/{depth} "
                              f"(profile '{profile}'). Models: RGBA, GRAYA, "
                              f"CMYKA, LABA, XYZA, YCbCrA; depths: U8, U16, "
                              f"F16, F32")}
        doc.waitForDone()
        self._maybe_refresh(doc)
        return {"status": "ok", "before": before,
                "color_model": doc.colorModel(), "color_depth": doc.colorDepth(),
                "profile": doc.colorProfile()}

    # ----- Masks: selection <-> layers -----

    def cmd_select_from_layer(self, params):
        """Selection from a layer's alpha (Krita's native Select Opaque), so
        the marching ants show; a selection mask restores its selection."""
        doc = self.get_active_document()
        if not doc:
            return {"error": "No active document"}
        node, err = self._resolve(doc, params.get("ref"))
        if err:
            return err
        mode = params.get("mode", "replace")
        if node.type() == "selectionmask":
            return self._load_selection_mask(doc, node, mode)
        action_name = self.SELECT_OPAQUE_ACTIONS.get(mode)
        if action_name is None:
            return {"error": (f"Unknown mode '{mode}'. Valid: "
                              f"{list(self.SELECT_OPAQUE_ACTIONS)}")}
        if mode in ("subtract", "intersect") and doc.selection() is None:
            return {"error": f"No existing selection to {mode} from"}
        action = Krita.instance().action(action_name)
        if action is None:
            return {"error": f"Krita action not found: {action_name}"}
        prev = doc.activeNode()
        try:
            self._activate(doc, node)
            action.trigger()
            doc.waitForDone()
        finally:
            if prev is not None:
                doc.setActiveNode(prev)
        self._maybe_refresh(doc)
        return {"status": "ok", "layer": self._path(node), "mode": mode,
                **self._selection_bounds(doc)}

    def _load_selection_mask(self, doc, mask, mode):
        sel = mask.selection()
        w, h = doc.width(), doc.height()
        data = bytes(sel.pixelData(0, 0, w, h))
        img = QImage(data, w, h, w, QImage.Format.Format_Grayscale8).copy()
        err = self._select_via_layer(doc, img, mode)
        if err:
            return err
        return {"status": "ok", "mask": self._path(mask), "mode": mode,
                **self._selection_bounds(doc)}

    def cmd_select_save(self, params):
        """Store the current selection as a named selection mask on a layer
        (`on`, default the active layer). Same name on the same layer is
        overwritten."""
        doc = self.get_active_document()
        if not doc:
            return {"error": "No active document"}
        name = params.get("name")
        if not name:
            return {"error": "name required"}
        sel = doc.selection()
        if sel is None:
            return {"error": "No active selection to save"}
        if params.get("on"):
            target, err = self._resolve(doc, params["on"])
            if err:
                return err
        else:
            target = doc.activeNode()
            while target is not None and target.type().endswith("mask"):
                target = target.parentNode()
            if target is None or target.parentNode() is None:
                return {"error": "No active layer to attach the mask to (use on)"}
        if target.type().endswith("mask"):
            return {"error": f"'{target.name()}' is a mask; save onto a layer"}
        prev = doc.activeNode()
        existing = [c for c in target.childNodes()
                    if c.type() == "selectionmask" and c.name() == name]
        if existing:
            mask, replaced = existing[0], True
        else:
            mask, replaced = doc.createSelectionMask(name), False
        mask.setSelection(sel.duplicate())
        if not replaced:
            target.addChildNode(mask, None)
        if prev is not None:
            self._activate(doc, prev)
        self._maybe_refresh(doc)
        return {"status": "ok", "replaced": replaced,
                "mask": self._layer_info(doc, mask)}

    def cmd_select_load(self, params):
        """Restore a selection saved with select_save (a selection mask)."""
        doc = self.get_active_document()
        if not doc:
            return {"error": "No active document"}
        node, err = self._resolve(doc, params.get("name"),
                                  types=("selectionmask",), what="selection mask")
        if err:
            return err
        return self._load_selection_mask(doc, node, params.get("mode", "replace"))

    # ----- SAM daemon via the "AI Select (SAM)" plugin -----
    # Same trick as AI Diffusion: both plugins live in Krita's one Python
    # interpreter. We never import it ourselves (that would register a
    # disabled plugin's docker); only use it if Krita already loaded it.

    def _autoselect_manager(self):
        mod = sys.modules.get("kritaautoselect")
        # get_manager is the public name (krita-autoselect api_version 2);
        # _get_manager is what older AI Select plugins expose.
        getter = None
        for attr in ("get_manager", "_get_manager"):
            getter = getattr(mod, attr, None) if mod else None
            if getter is not None:
                break
        if getter is None:
            raise RuntimeError(
                "AI Select (SAM) plugin not loaded — enable it in Settings > "
                "Configure Krita > Python Plugin Manager")
        return getter()

    @staticmethod
    def _url_key(url):
        rest = str(url).split("://", 1)[-1].rstrip("/")
        hostport = rest.split("/", 1)[0]
        host, _, port = hostport.partition(":")
        if host in ("localhost", "::1", "[::1]"):
            host = "127.0.0.1"
        return host, port or "80"

    def cmd_sam_status(self, params):
        """State of the SAM daemon AI Select manages (does not start it)."""
        try:
            mgr = self._autoselect_manager()
        except RuntimeError as e:
            return {"status": "ok", "ai_select": False, "detail": str(e)}
        snap = mgr.snapshot()
        out = {"status": "ok", "ai_select": True, "url": mgr.config.url,
               "state": snap.get("state"), "detail": snap.get("detail") or "",
               "owned": mgr.owned_alive, "loaded": snap.get("loaded"),
               "pid": snap.get("pid"), "device": snap.get("device")}
        if params.get("url"):
            out["url_matches"] = (self._url_key(params["url"])
                                  == self._url_key(mgr.config.url))
        if self._sam_start is not None:
            out["start"] = dict(self._sam_start)
        return out

    def cmd_sam_start(self, params):
        """Ask AI Select to reuse or start its daemon. Returns at once; the
        outcome lands in sam_status["start"] (kri polls it)."""
        try:
            mgr = self._autoselect_manager()
        except RuntimeError as e:
            return {"error": str(e), "ai_select": False}
        url = params.get("url")
        if url and self._url_key(url) != self._url_key(mgr.config.url):
            return {"error": (f"AI Select manages the daemon at "
                              f"{mgr.config.url}, but kri uses {url} "
                              f"(AUTOSELECT_URL). Point AUTOSELECT_URL there "
                              f"or change the URL in AI Select's settings."),
                    "url": mgr.config.url}
        self._sam_seq += 1
        state = {"seq": self._sam_seq, "done": False}
        self._sam_start = state

        def done(ok, message):  # manager thread
            state.update(done=True, ok=bool(ok), message=message or "")

        mgr.start(done)
        return {"status": "ok", "seq": state["seq"], "url": mgr.config.url,
                "state": mgr.snapshot().get("state")}

    def cmd_sam_stop(self, params):
        """Stop the daemon AI Select started (a foreign one is never touched)."""
        try:
            mgr = self._autoselect_manager()
        except RuntimeError as e:
            return {"error": str(e), "ai_select": False}
        stopped = mgr.stop()
        return {"status": "ok", "stopped": stopped, "url": mgr.config.url,
                "detail": "" if stopped else
                "no daemon started by AI Select is running"}

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
