"""Tests for the kri CLI against a fake kritamcp plugin server.

The fake server records every request body in `FakePlugin.requests_log` and
answers from `FakePlugin.responses` (action -> dict, or list of dicts served
in order for polling tests). Tests invoke the real CLI via subprocess with
KRITA_URL pointed at the fake server."""
import base64
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KRI = os.path.join(REPO, "cli", "kri")


class FakePlugin(BaseHTTPRequestHandler):
    requests_log = []
    responses = {}
    hooks = {}  # action -> callable(cmd), run before answering

    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        self._reply({"status": "ok", "plugin": "kritamcp"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        cmd = json.loads(self.rfile.read(n))
        FakePlugin.requests_log.append(cmd)
        hook = FakePlugin.hooks.get(cmd["action"])
        if hook:
            hook(cmd)
        r = FakePlugin.responses.get(cmd["action"], {"status": "ok"})
        if isinstance(r, list):
            r = r.pop(0) if len(r) > 1 else r[0]
        self._reply(r, 500 if r.get("error") else 200)

    def _reply(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)


class FakeDaemon(BaseHTTPRequestHandler):
    """Fake krita-autoselect daemon: records /segment requests, answers from
    FakeDaemon.response."""
    requests_log = []
    response = {}

    def log_message(self, fmt, *args):
        pass

    def do_GET(self):  # /health
        r = FakeDaemon.response or {"status": "ok"}
        body = json.dumps(r).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        FakeDaemon.requests_log.append(
            (self.path, json.loads(self.rfile.read(n))))
        r = FakeDaemon.response or {"status": "ok"}
        body = json.dumps(r).encode()
        self.send_response(500 if r.get("error") else 200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)


class KriTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = HTTPServer(("localhost", 0), FakePlugin)
        cls.port = cls.server.server_address[1]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.daemon = HTTPServer(("localhost", 0), FakeDaemon)
        cls.daemon_port = cls.daemon.server_address[1]
        threading.Thread(target=cls.daemon.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.daemon.shutdown()
        cls.daemon.server_close()

    def setUp(self):
        FakePlugin.requests_log.clear()
        FakePlugin.responses = {}
        FakePlugin.hooks = {}
        FakeDaemon.requests_log.clear()
        FakeDaemon.response = {}

    def kri(self, *args, stdin=None):
        env = dict(os.environ,
                   KRITA_URL=f"http://localhost:{self.port}",
                   AUTOSELECT_URL=f"http://localhost:{self.daemon_port}")
        return subprocess.run(
            [sys.executable, KRI, *args],
            capture_output=True, text=True, env=env, input=stdin, timeout=30,
        )

    def last_request(self):
        return FakePlugin.requests_log[-1]

    # ----- Task 1 -----

    def test_health_ok(self):
        r = self.kri("health")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("kritamcp", r.stdout)

    def test_connection_error_exits_1(self):
        env = dict(os.environ, KRITA_URL="http://localhost:1")  # nothing there
        r = subprocess.run([sys.executable, KRI, "undo"],
                           capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(r.returncode, 1)
        self.assertIn("Cannot connect", r.stderr)

    # ----- Task 3 -----

    def test_status_merges_doc_and_ai_in_one_roundtrip(self):
        FakePlugin.responses["batch"] = {
            "status": "ok", "count": 2,
            "results": [
                {"status": "ok", "document": {"name": "x.kra", "width": 800,
                                              "height": 600, "layers": []}},
                {"status": "ok", "ai": {"workspace": "generation"},
                 "regions": [], "styles": []},
            ],
        }
        r = self.kri("status")
        self.assertEqual(r.returncode, 0, r.stderr)
        merged = json.loads(r.stdout)
        self.assertEqual(merged["doc"]["name"], "x.kra")
        self.assertEqual(merged["ai"]["workspace"], "generation")
        # exactly ONE round-trip
        self.assertEqual(len(FakePlugin.requests_log), 1)
        req = self.last_request()
        self.assertEqual(req["action"], "batch")
        self.assertEqual([c["action"] for c in req["params"]["commands"]],
                         ["doc_info", "ai_overview"])

    def test_status_tolerates_missing_ai_plugin(self):
        FakePlugin.responses["batch"] = {
            "status": "ok", "count": 2,
            "results": [
                {"status": "ok", "document": None},
                {"error": "AI Diffusion plugin not loaded: ..."},
            ],
        }
        r = self.kri("status")
        self.assertEqual(r.returncode, 0, r.stderr)
        merged = json.loads(r.stdout)
        self.assertIn("error", merged["ai"])

    # ----- Task 4 -----

    def test_paint_command_mappings(self):
        """Each CLI invocation maps to the right action + params."""
        cases = [
            (["canvas", "800", "600", "--bg", "#ffffff"],
             "new_canvas", {"width": 800, "height": 600,
                            "name": "New Canvas", "background": "#ffffff"}),
            (["color", "#ff0000"], "set_color", {"color": "#ff0000"}),
            (["brush", "--size", "12"], "set_brush", {"size": 12}),
            (["stroke", "10,10", "50,60", "--size", "8"],
             "stroke", {"points": [[10, 10], [50, 60]], "size": 8,
                        "feather": 0.0, "opacity": 1.0}),
            (["fill", "100", "120", "30"],
             "fill", {"x": 100, "y": 120, "radius": 30, "feather": 0.0}),
            (["shape", "ellipse", "10", "20", "100", "50"],
             "draw_shape", {"shape": "ellipse", "x": 10, "y": 20,
                            "width": 100, "height": 50, "fill": True,
                            "stroke": False, "feather": 0.0}),
            (["redo"], "redo", {}),
            (["clear", "#000000"], "clear", {"color": "#000000"}),
            (["color-at", "5", "6"], "get_color_at", {"x": 5, "y": 6}),
            (["brushes", "soft", "--limit", "5"],
             "list_brushes", {"filter": "soft", "limit": 5}),
        ]
        for argv, action, params in cases:
            with self.subTest(argv=argv):
                FakePlugin.requests_log.clear()
                r = self.kri(*argv)
                self.assertEqual(r.returncode, 0, f"{argv}: {r.stderr}")
                req = self.last_request()
                self.assertEqual(req["action"], action)
                self.assertEqual(req["params"], params)

    def test_stroke_rejects_bad_point(self):
        r = self.kri("stroke", "10,10", "banana")
        self.assertEqual(r.returncode, 2)
        self.assertIn("invalid point", r.stderr)

    def test_save_uses_absolute_path(self):
        r = self.kri("save", "out.png")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(os.path.isabs(self.last_request()["params"]["path"]))

    # ----- Task 5 -----

    def _canvas_payload(self, fmt="jpeg"):
        return {"status": "ok", "mode": "fast", "format": fmt,
                "data_b64": base64.b64encode(b"fakeimagebytes").decode(),
                "width": 640, "height": 480}

    def test_look_writes_image_and_prints_path(self):
        FakePlugin.responses["get_canvas"] = self._canvas_payload()
        with tempfile.TemporaryDirectory() as d:
            dest = os.path.join(d, "c.jpg")
            r = self.kri("look", "-o", dest)
            self.assertEqual(r.returncode, 0, r.stderr)
            info = json.loads(r.stdout)
            self.assertEqual(info["path"], dest)
            with open(dest, "rb") as f:
                self.assertEqual(f.read(), b"fakeimagebytes")
        self.assertEqual(self.last_request()["params"]["mode"], "fast")

    def test_status_compacts_styles_to_one_line_each(self):
        """Los estilos salen como strings 'Nombre — filename' (menos tokens),
        no como lista de objetos."""
        FakePlugin.responses["batch"] = {
            "status": "ok", "count": 2,
            "results": [
                {"status": "ok", "document": {"name": "x.kra"}},
                {"status": "ok", "ai": {"workspace": "generation"},
                 "styles": [{"filename": "built-in/flux.json", "name": "Flux"},
                            {"filename": "built-in/qwen.json", "name": "Qwen"}]},
            ],
        }
        r = self.kri("status")
        self.assertEqual(r.returncode, 0, r.stderr)
        merged = json.loads(r.stdout)
        self.assertEqual(merged["styles"],
                         ["Flux - built-in/flux.json",
                          "Qwen - built-in/qwen.json"])

    def test_look_max_dim_default_defers_to_plugin(self):
        """Sin --max-dim no se manda max_dim (manda el default del plugin);
        con --max-dim viaja explícito."""
        FakePlugin.responses["get_canvas"] = self._canvas_payload()
        with tempfile.TemporaryDirectory() as d:
            r = self.kri("look", "-o", os.path.join(d, "a.jpg"))
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertNotIn("max_dim", self.last_request()["params"])
            r = self.kri("look", "--max-dim", "256", "-o", os.path.join(d, "b.jpg"))
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertEqual(self.last_request()["params"]["max_dim"], 256)

    def test_look_default_path_uses_system_tempdir(self):
        """Sin -o ni KRI_LOOK_PATH, el default cae en tempfile.gettempdir()
        (no en /tmp hardcodeado, que no existe en Windows)."""
        FakePlugin.responses["get_canvas"] = self._canvas_payload()
        env = dict(os.environ, KRITA_URL=f"http://localhost:{self.port}")
        env.pop("KRI_LOOK_PATH", None)
        r = subprocess.run([sys.executable, KRI, "look"],
                           capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        path = json.loads(r.stdout)["path"]
        self.assertEqual(os.path.dirname(path), tempfile.gettempdir())
        self.assertTrue(os.path.isfile(path))
        os.remove(path)

    def test_look_full_mode(self):
        FakePlugin.responses["get_canvas"] = self._canvas_payload(fmt="png")
        with tempfile.TemporaryDirectory() as d:
            dest = os.path.join(d, "c.png")
            r = self.kri("look", "--full", "-o", dest)
            self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.last_request()["params"]["mode"], "full")

    def test_batch_from_stdin_sets_stop_on_error(self):
        FakePlugin.responses["batch"] = {"status": "ok", "count": 2,
                                         "results": [{"status": "ok"}] * 2}
        script = json.dumps([
            {"action": "set_color", "params": {"color": "#fff"}},
            {"action": "fill", "params": {"x": 1, "y": 2, "radius": 3}},
        ])
        r = self.kri("batch", stdin=script)
        self.assertEqual(r.returncode, 0, r.stderr)
        req = self.last_request()
        self.assertEqual(req["action"], "batch")
        self.assertTrue(req["params"]["stop_on_error"])
        self.assertEqual(len(req["params"]["commands"]), 2)

    def test_batch_stopped_at_exits_1(self):
        FakePlugin.responses["batch"] = {
            "status": "ok", "count": 2, "stopped_at": 1,
            "results": [{"status": "ok"}, {"error": "boom"}],
        }
        r = self.kri("batch", stdin='[{"action":"undo"},{"action":"undo"}]')
        self.assertEqual(r.returncode, 1)
        self.assertIn("stopped at command 1", r.stderr)

    def test_batch_look_writes_canvas(self):
        FakePlugin.responses["batch"] = {
            "status": "ok", "count": 1, "results": [{"status": "ok"}],
            "canvas": self._canvas_payload(),
        }
        with tempfile.TemporaryDirectory() as d:
            dest = os.path.join(d, "after.jpg")
            r = self.kri("batch", "--look", "fast", "-o", dest,
                         stdin='[{"action":"undo"}]')
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertTrue(os.path.exists(dest))
            res = json.loads(r.stdout)
            self.assertEqual(res["canvas"]["path"], dest)
        self.assertEqual(self.last_request()["params"]["review"], "fast")

    def test_batch_invalid_json_exits_2(self):
        r = self.kri("batch", stdin="not json")
        self.assertEqual(r.returncode, 2)

    # ----- Task 6 -----

    def test_ai_command_mappings(self):
        cases = [
            (["ai", "status"], "ai_status", {}),
            (["ai", "overview"], "ai_overview", {}),
            (["ai", "set-prompt", "-p", "a fox", "-n", "blurry"],
             "ai_set_prompt", {"positive": "a fox", "negative": "blurry"}),
            (["ai", "set-params", "--style", "flux", "--strength", "0.7"],
             "ai_set_params", {"style": "flux", "strength": 0.7}),
            (["ai", "workspace", "generation"],
             "ai_set_workspace", {"name": "generation"}),
            (["ai", "jobs", "--state", "finished", "--limit", "3"],
             "ai_list_jobs", {"state": "finished", "limit": 3}),
            (["ai", "apply", "--index", "1"], "ai_apply", {"index": 1}),
            (["ai", "cancel", "--queued"],
             "ai_cancel", {"active": True, "queued": True}),
            (["ai", "preview", "job42", "--index", "2"],
             "ai_save_preview", {"job_id": "job42", "index": 2, "filename": ""}),
            (["ai", "styles", "anime"],
             "ai_list_styles", {"filter": "anime", "limit": 30}),
            (["ai", "region", "create", "-p", "a dog"],
             "ai_create_region", {"positive": "a dog", "group": False}),
            (["ai", "region", "list"], "ai_list_regions", {}),
            (["ai", "region", "select", "0"], "ai_select_region", {"index": 0}),
            (["ai", "region", "remove", "1"], "ai_remove_region", {"index": 1}),
            (["ai", "control", "add", "depth", "--strength", "0.9"],
             "ai_add_control", {"mode": "depth", "strength": 0.9}),
            (["ai", "control", "list"], "ai_list_controls", {}),
            (["ai", "control", "set", "0", "--mode", "pose"],
             "ai_set_control", {"index": 0, "mode": "pose"}),
            (["ai", "control", "remove", "0", "--region", "1"],
             "ai_remove_control", {"index": 0, "region_index": 1}),
        ]
        for argv, action, params in cases:
            with self.subTest(argv=argv):
                FakePlugin.requests_log.clear()
                r = self.kri(*argv)
                self.assertEqual(r.returncode, 0, f"{argv}: {r.stderr}")
                req = self.last_request()
                self.assertEqual(req["action"], action)
                self.assertEqual(req["params"], params)

    # ----- Selecciones -----

    def test_select_command_mappings(self):
        cases = [
            (["select", "rect", "10", "20", "100", "50"],
             "select_shape", {"shape": "rectangle", "x": 10, "y": 20,
                              "width": 100, "height": 50, "mode": "replace"}),
            (["select", "ellipse", "0", "0", "300", "200", "--mode", "add"],
             "select_shape", {"shape": "ellipse", "x": 0, "y": 0,
                              "width": 300, "height": 200, "mode": "add"}),
            (["select", "poly", "10,10", "200,10", "100,150", "--mode", "subtract"],
             "select_shape", {"shape": "polygon",
                              "points": [[10, 10], [200, 10], [100, 150]],
                              "mode": "subtract"}),
            (["select", "all"], "select_all", {}),
            (["select", "none"], "select_none", {}),
            (["select", "invert"], "select_invert", {}),
            (["select", "feather", "12"],
             "select_modify", {"op": "feather", "radius": 12}),
            (["select", "grow", "5"],
             "select_modify", {"op": "grow", "radius": 5}),
            (["select", "shrink", "3"],
             "select_modify", {"op": "shrink", "radius": 3}),
            (["select", "border", "2"],
             "select_modify", {"op": "border", "radius": 2}),
            (["select", "info"], "select_info", {}),
        ]
        for argv, action, params in cases:
            with self.subTest(argv=argv):
                FakePlugin.requests_log.clear()
                r = self.kri(*argv)
                self.assertEqual(r.returncode, 0, f"{argv}: {r.stderr}")
                req = self.last_request()
                self.assertEqual(req["action"], action)
                self.assertEqual(req["params"], params)

    def test_select_poly_rejects_bad_point(self):
        r = self.kri("select", "poly", "10,10", "banana")
        self.assertEqual(r.returncode, 2)
        self.assertIn("invalid point", r.stderr)

    def test_select_poly_needs_three_points(self):
        r = self.kri("select", "poly", "10,10", "20,20")
        self.assertEqual(r.returncode, 2)
        self.assertIn("at least 3 points", r.stderr)

    def test_select_info_error_exits_1(self):
        FakePlugin.responses["select_info"] = {"error": "No active document"}
        r = self.kri("select", "info")
        self.assertEqual(r.returncode, 1)
        self.assertIn("No active document", r.stderr)

    # ----- select sam (daemon krita-autoselect) -----

    def _sam_ready(self, mask=b"fakemaskpng", count=1):
        FakePlugin.responses["get_canvas"] = {
            "status": "ok", "mode": "full", "format": "png",
            "data_b64": base64.b64encode(b"fakecanvas").decode(),
            "width": 800, "height": 600}
        FakePlugin.responses["select_from_mask"] = {
            "status": "ok", "mode": "replace",
            "selection": {"x": 10, "y": 10, "width": 100, "height": 80}}
        FakeDaemon.response = {
            "status": "ok", "width": 800, "height": 600, "count": count,
            "instances": [{"index": 0, "score": 0.97, "box": [10, 10, 110, 90]}],
        }
        if mask is not None:
            FakeDaemon.response["mask_b64"] = base64.b64encode(mask).decode()

    def test_select_sam_full_flow(self):
        """canvas (plugin) → /segment (daemon) → select_from_mask (plugin)."""
        self._sam_ready()
        r = self.kri("select", "sam", "the red car",
                     "--point", "400,300", "--neg-point", "50,60",
                     "--threshold", "0.6", "--mode", "add")
        self.assertEqual(r.returncode, 0, r.stderr)
        # 1) canvas pedido al plugin en full
        self.assertEqual(FakePlugin.requests_log[0]["action"], "get_canvas")
        self.assertEqual(FakePlugin.requests_log[0]["params"]["mode"], "full")
        # 2) daemon recibió imagen + prompts
        path, seg = FakeDaemon.requests_log[-1]
        self.assertEqual(path, "/segment")
        self.assertEqual(seg["text"], "the red car")
        self.assertEqual(seg["points"], [[400, 300], [50, 60]])
        self.assertEqual(seg["point_labels"], [1, 0])
        self.assertEqual(seg["threshold"], 0.6)
        self.assertEqual(seg["combine"], "union")
        self.assertEqual(seg["image_b64"],
                         base64.b64encode(b"fakecanvas").decode())
        # 3) la máscara volvió al plugin con el mode pedido
        apply_req = FakePlugin.requests_log[-1]
        self.assertEqual(apply_req["action"], "select_from_mask")
        self.assertEqual(apply_req["params"]["mask_b64"],
                         base64.b64encode(b"fakemaskpng").decode())
        self.assertEqual(apply_req["params"]["mode"], "add")
        # 4) el resultado mergea instancias + selección
        res = json.loads(r.stdout)
        self.assertEqual(res["count"], 1)
        self.assertEqual(res["selection"]["width"], 100)

    def test_select_sam_instance_flag(self):
        self._sam_ready()
        r = self.kri("select", "sam", "car", "--instance", "1")
        self.assertEqual(r.returncode, 0, r.stderr)
        _, seg = FakeDaemon.requests_log[-1]
        self.assertEqual(seg["combine"], 1)

    def test_select_sam_within(self):
        self._sam_ready()
        r = self.kri("select", "sam", "hand", "--within", "280", "0",
                     "150", "150")
        self.assertEqual(r.returncode, 0, r.stderr)
        _, seg = FakeDaemon.requests_log[-1]
        self.assertEqual(seg["within"], [280, 0, 150, 150])
        self.assertEqual(seg["text"], "hand")

    def test_select_sam_list_does_not_touch_selection(self):
        self._sam_ready(mask=None)
        r = self.kri("select", "sam", "car", "--list")
        self.assertEqual(r.returncode, 0, r.stderr)
        _, seg = FakeDaemon.requests_log[-1]
        self.assertTrue(seg["list_only"])
        actions = [q["action"] for q in FakePlugin.requests_log]
        self.assertNotIn("select_from_mask", actions)
        self.assertIn("instances", json.loads(r.stdout))

    def test_select_sam_zero_matches_exits_1(self):
        self._sam_ready(mask=None, count=0)
        FakeDaemon.response["instances"] = []
        r = self.kri("select", "sam", "unicorn")
        self.assertEqual(r.returncode, 1)
        self.assertIn("no instances", r.stderr)

    def test_select_sam_requires_some_prompt(self):
        r = self.kri("select", "sam")
        self.assertEqual(r.returncode, 2)
        self.assertIn("prompt", r.stderr)

    def test_select_sam_daemon_down_mentions_daemon(self):
        """AI Select ausente → el error conserva el hint de siempre."""
        self._sam_ready()
        FakePlugin.responses["sam_start"] = {
            "error": "AI Select (SAM) plugin not loaded", "ai_select": False}
        env = dict(os.environ,
                   KRITA_URL=f"http://localhost:{self.port}",
                   AUTOSELECT_URL="http://localhost:1")
        r = subprocess.run([sys.executable, KRI, "select", "sam", "car"],
                           capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(r.returncode, 1)
        self.assertIn("krita-autoselect", r.stderr)
        self.assertIn("AI Select", r.stderr)
        # le pidió al plugin que arranque el daemon, con la URL explícita
        start = [q for q in FakePlugin.requests_log if q["action"] == "sam_start"]
        self.assertEqual(start[0]["params"], {"url": "http://localhost:1"})
        self.assertNotIn("select_from_mask",
                         [q["action"] for q in FakePlugin.requests_log])

    def test_select_sam_autostarts_daemon_via_ai_select(self):
        """Daemon caído → sam_start en el plugin → espera start.done → reintenta."""
        self._sam_ready()
        with socket.socket() as probe:
            probe.bind(("localhost", 0))
            port = probe.getsockname()[1]
        url = f"http://localhost:{port}"
        started = []

        def start_daemon(cmd):  # lo que haría AI Select: levantar el daemon
            srv = HTTPServer(("localhost", port), FakeDaemon)
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            started.append(srv)

        FakePlugin.hooks["sam_start"] = start_daemon
        FakePlugin.responses["sam_start"] = {"status": "ok", "seq": 3, "url": url}
        FakePlugin.responses["sam_status"] = [
            {"status": "ok", "state": "starting", "start": {"seq": 3, "done": False}},
            {"status": "ok", "state": "ready",
             "start": {"seq": 3, "done": True, "ok": True, "message": ""}},
        ]
        env = dict(os.environ, KRITA_URL=f"http://localhost:{self.port}",
                   AUTOSELECT_URL=url)
        try:
            r = subprocess.run([sys.executable, KRI, "select", "sam", "car"],
                               capture_output=True, text=True, env=env,
                               timeout=30)
        finally:
            for srv in started:
                srv.shutdown()
                srv.server_close()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(FakeDaemon.requests_log), 1)
        actions = [q["action"] for q in FakePlugin.requests_log]
        self.assertEqual(actions[:2], ["get_canvas", "sam_start"])
        self.assertEqual(actions.count("sam_status"), 2)
        self.assertEqual(actions[-1], "select_from_mask")

    def test_select_sam_autostart_failure_reports_reason(self):
        self._sam_ready()
        FakePlugin.responses["sam_start"] = {"status": "ok", "seq": 1}
        FakePlugin.responses["sam_status"] = {
            "status": "ok", "state": "error",
            "start": {"seq": 1, "done": True, "ok": False,
                      "message": "backend did not start (exit 3)"}}
        env = dict(os.environ, KRITA_URL=f"http://localhost:{self.port}",
                   AUTOSELECT_URL="http://localhost:1")
        r = subprocess.run([sys.executable, KRI, "select", "sam", "car"],
                           capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(r.returncode, 1)
        self.assertIn("backend did not start (exit 3)", r.stderr)

    def test_select_sam_to_layer_creates_filled_layer_in_one_batch(self):
        self._sam_ready()
        FakePlugin.responses["batch"] = {
            "status": "ok", "count": 2, "results": [
                {"status": "ok", "selection": {"x": 1, "y": 2,
                                               "width": 3, "height": 4}},
                {"status": "ok", "layer": {"name": "beard", "id": "abc-1"}}]}
        r = self.kri("select", "sam", "the beard", "--to-layer", "beard",
                     "--color", "#eeeeee", "--blend", "multiply",
                     "--below", "Line art")
        self.assertEqual(r.returncode, 0, r.stderr)
        req = self.last_request()
        self.assertEqual(req["action"], "batch")
        self.assertTrue(req["params"]["stop_on_error"])
        sel, layer = req["params"]["commands"]
        self.assertEqual(sel["action"], "select_from_mask")
        self.assertEqual(sel["params"]["mode"], "replace")
        self.assertEqual(layer, {"action": "layer_new", "params": {
            "name": "beard", "color": "#eeeeee", "selection": True,
            "blend": "multiply", "below": "Line art"}})
        res = json.loads(r.stdout)
        self.assertEqual(res["layer"]["id"], "abc-1")
        self.assertEqual(res["selection"]["width"], 3)

    def test_select_sam_to_layer_defaults_white_and_saves_mask(self):
        self._sam_ready()
        FakePlugin.responses["batch"] = {
            "status": "ok", "count": 2, "results": [
                {"status": "ok", "selection": None},
                {"status": "ok", "layer": {"name": "coat", "id": "id-9"}}]}
        FakePlugin.responses["select_save"] = {
            "status": "ok", "mask": {"name": "coat mask"}}
        r = self.kri("select", "sam", "coat", "--to-layer", "coat",
                     "--save-mask", "coat mask")
        self.assertEqual(r.returncode, 0, r.stderr)
        batch = [q for q in FakePlugin.requests_log if q["action"] == "batch"][0]
        self.assertEqual(batch["params"]["commands"][1]["params"]["color"],
                         "#ffffff")
        self.assertEqual(self.last_request(), {
            "action": "select_save", "timeout": 30.0,
            "params": {"name": "coat mask", "on": "id-9"}})
        self.assertEqual(json.loads(r.stdout)["mask"]["name"], "coat mask")

    def test_select_sam_to_layer_error_names_failing_step(self):
        self._sam_ready()
        FakePlugin.responses["batch"] = {
            "status": "ok", "count": 2, "stopped_at": 1, "results": [
                {"status": "ok"}, {"error": "Layer not found: 'Lineart'"}]}
        r = self.kri("select", "sam", "coat", "--to-layer", "coat",
                     "--above", "Lineart")
        self.assertEqual(r.returncode, 1)
        self.assertIn("Layer not found", r.stderr)
        self.assertIn("layer_new", r.stderr)

    def test_select_sam_to_layer_rejects_list(self):
        r = self.kri("select", "sam", "coat", "--to-layer", "coat", "--list")
        self.assertEqual(r.returncode, 2)

    # ----- capas -----

    def test_layer_command_mappings(self):
        cases = [
            (["layer", "list"], "layer_list", {}),
            (["layer", "blends"], "layer_blends", {}),
            (["layer", "new", "Flats", "--type", "group", "--top"],
             "layer_new", {"name": "Flats", "type": "group", "top": True}),
            (["layer", "new", "beard", "--below", "Line art", "--blend",
              "soft-light", "--opacity", "80", "--hidden", "--color", "#fff"],
             "layer_new", {"name": "beard", "below": "Line art",
                           "blend": "soft-light", "opacity": 80.0,
                           "hidden": True, "color": "#fff"}),
            (["layer", "new", "beard", "--color", "#fff", "--selection",
              "--into", "Flats"],
             "layer_new", {"name": "beard", "color": "#fff",
                           "selection": True, "into": "Flats"}),
            (["layer", "set", "Line art", "--blend", "multiply",
              "--opacity", "90", "--hidden", "--lock", "--alpha-lock",
              "--no-inherit-alpha", "--name", "Ink"],
             "layer_set", {"ref": "Line art", "name": "Ink",
                           "blend": "multiply", "opacity": 90.0,
                           "visible": False, "locked": True,
                           "alpha_locked": True, "inherit_alpha": False}),
            (["layer", "set", "Ink", "--visible", "--unlock",
              "--no-alpha-lock"],
             "layer_set", {"ref": "Ink", "visible": True, "locked": False,
                           "alpha_locked": False}),
            (["layer", "move", "Line art", "--top"],
             "layer_move", {"ref": "Line art", "top": True}),
            (["layer", "move", "Flats/beard", "--above", "coat"],
             "layer_move", {"ref": "Flats/beard", "above": "coat"}),
            (["layer", "move", "beard", "--bottom"],
             "layer_move", {"ref": "beard", "bottom": True}),
            (["layer", "select", "beard"], "layer_select", {"ref": "beard"}),
            (["layer", "duplicate", "beard", "--name", "beard 2", "--into",
              "Flats"],
             "layer_duplicate", {"ref": "beard", "name": "beard 2",
                                 "into": "Flats"}),
            (["layer", "delete", "a", "Flats/b"],
             "layer_delete", {"refs": ["a", "Flats/b"]}),
            (["layer", "group", "Flats", "beard", "coat", "--blend",
              "normal"],
             "layer_group", {"name": "Flats", "refs": ["beard", "coat"],
                             "blend": "normal"}),
            (["layer", "fill", "beard", "#112233"],
             "layer_fill", {"ref": "beard", "color": "#112233"}),
            (["layer", "fill", "beard", "#112233", "--selection"],
             "layer_fill", {"ref": "beard", "color": "#112233",
                            "selection": True}),
            (["colorspace"], "doc_colorspace", {}),
            (["colorspace", "RGBA", "U16"],
             "doc_colorspace", {"model": "RGBA", "depth": "U16"}),
        ]
        for argv, action, params in cases:
            with self.subTest(argv=argv):
                FakePlugin.requests_log.clear()
                r = self.kri(*argv)
                self.assertEqual(r.returncode, 0, f"{argv}: {r.stderr}")
                req = self.last_request()
                self.assertEqual(req["action"], action)
                self.assertEqual(req["params"], params)

    def test_layer_import_sends_absolute_path_not_pixels(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "beard.png")
            with open(path, "wb") as f:
                f.write(b"png")
            r = subprocess.run(
                [sys.executable, KRI, "layer", "import", "beard.png",
                 "--name", "Beard", "--x", "10", "--y", "-5", "--above",
                 "coat", "--blend", "multiply", "--opacity", "50"],
                capture_output=True, text=True, cwd=d, timeout=30,
                env=dict(os.environ, KRITA_URL=f"http://localhost:{self.port}"))
        self.assertEqual(r.returncode, 0, r.stderr)
        req = self.last_request()
        self.assertEqual(req["action"], "layer_import")
        self.assertEqual(req["params"], {
            "path": path, "name": "Beard", "x": 10, "y": -5,
            "above": "coat", "blend": "multiply", "opacity": 50.0})
        self.assertGreaterEqual(req["timeout"], 120.0)

    def test_layer_import_missing_file_exits_2(self):
        r = self.kri("layer", "import", "/nonexistent/x.png")
        self.assertEqual(r.returncode, 2)
        self.assertIn("not found", r.stderr)
        self.assertEqual(FakePlugin.requests_log, [])

    def test_layer_export_absolute_path(self):
        r = self.kri("layer", "export", "beard", "out/beard.png", "--crop")
        self.assertEqual(r.returncode, 0, r.stderr)
        params = self.last_request()["params"]
        self.assertTrue(os.path.isabs(params["path"]))
        self.assertTrue(params["path"].endswith("out/beard.png"))
        self.assertEqual(params["ref"], "beard")
        self.assertTrue(params["crop"])

    def test_layer_placement_flags_are_exclusive(self):
        r = self.kri("layer", "new", "x", "--top", "--below", "y")
        self.assertEqual(r.returncode, 2)
        self.assertEqual(FakePlugin.requests_log, [])

    def test_layer_usage_errors_exit_2(self):
        for argv in (["layer", "set", "beard"],
                     ["layer", "move", "beard"],
                     ["layer", "new", "x", "--selection"],
                     ["layer", "set", "x", "--visible", "--hidden"],
                     ["layer", "group", "G"]):
            with self.subTest(argv=argv):
                r = self.kri(*argv)
                self.assertEqual(r.returncode, 2, r.stderr)
        self.assertEqual(FakePlugin.requests_log, [])

    def test_layer_ambiguous_ref_lists_candidates(self):
        FakePlugin.responses["layer_select"] = {
            "error": "Ambiguous layer 'beard': 2 match (beard, Flats/beard)",
            "candidates": [{"path": "beard", "id": "a"},
                           {"path": "Flats/beard", "id": "b"}]}
        r = self.kri("layer", "select", "beard")
        self.assertEqual(r.returncode, 1)
        self.assertIn("Flats/beard", r.stderr)
        self.assertIn("candidates", r.stderr)

    def test_select_mask_mappings(self):
        cases = [
            (["select", "from-layer", "beard"],
             "select_from_layer", {"ref": "beard", "mode": "replace"}),
            (["select", "from-layer", "Flats/coat", "--mode", "add"],
             "select_from_layer", {"ref": "Flats/coat", "mode": "add"}),
            (["select", "save", "beard mask"],
             "select_save", {"name": "beard mask"}),
            (["select", "save", "beard mask", "--on", "beard"],
             "select_save", {"name": "beard mask", "on": "beard"}),
            (["select", "load", "beard mask", "--mode", "subtract"],
             "select_load", {"name": "beard mask", "mode": "subtract"}),
        ]
        for argv, action, params in cases:
            with self.subTest(argv=argv):
                FakePlugin.requests_log.clear()
                r = self.kri(*argv)
                self.assertEqual(r.returncode, 0, f"{argv}: {r.stderr}")
                req = self.last_request()
                self.assertEqual(req["action"], action)
                self.assertEqual(req["params"], params)

    # ----- sam (daemon vía AI Select) -----

    def test_sam_status_merges_plugin_and_daemon_health(self):
        FakePlugin.responses["sam_status"] = {
            "status": "ok", "ai_select": True, "state": "ready",
            "url": "http://127.0.0.1:5679"}
        FakeDaemon.response = {"status": "ok", "service": "krita-autoselect"}
        r = self.kri("sam", "status")
        self.assertEqual(r.returncode, 0, r.stderr)
        res = json.loads(r.stdout)
        self.assertEqual(res["ai_select"]["state"], "ready")
        self.assertTrue(res["daemon"]["reachable"])
        self.assertEqual(res["daemon"]["service"], "krita-autoselect")
        self.assertEqual(self.last_request()["params"]["url"],
                         f"http://localhost:{self.daemon_port}")

    def test_sam_status_warns_when_daemon_lacks_segment_many(self):
        FakePlugin.responses["sam_status"] = {"status": "ok", "ai_select": True}
        FakeDaemon.response = {"status": "ok", "service": "krita-autoselect"}
        res = json.loads(self.kri("sam", "status").stdout)
        self.assertIn("no tiene segment_many", res["warning"])
        FakeDaemon.response = {"status": "ok", "api_version": 2,
                               "capabilities": ["segment", "segment_many"]}
        res = json.loads(self.kri("sam", "status").stdout)
        self.assertNotIn("warning", res)

    def test_human_output_and_errors(self):
        r = self.kri("--human", "health")
        self.assertEqual(r.returncode, 0)
        self.assertIn("status: ok", r.stdout)
        FakePlugin.responses["select_info"] = {"error": "No selection"}
        r = self.kri("select", "info", "--human")
        self.assertEqual(r.returncode, 1)
        self.assertIn("error: No selection", r.stderr)

    def test_version(self):
        r = self.kri("--version")
        self.assertEqual(r.stdout.strip(), "kri 0.5.1")

    def test_sam_status_reports_down_daemon_without_failing(self):
        FakePlugin.responses["sam_status"] = {
            "status": "ok", "ai_select": False, "detail": "not loaded"}
        env = dict(os.environ, KRITA_URL=f"http://localhost:{self.port}",
                   AUTOSELECT_URL="http://localhost:1")
        r = subprocess.run([sys.executable, KRI, "sam", "status"],
                           capture_output=True, text=True, env=env, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        res = json.loads(r.stdout)
        self.assertFalse(res["daemon"]["reachable"])
        self.assertFalse(res["ai_select"]["ai_select"])

    def test_sam_start_waits_for_ready(self):
        FakePlugin.responses["sam_start"] = {
            "status": "ok", "seq": 7,
            "url": f"http://localhost:{self.daemon_port}"}
        FakePlugin.responses["sam_status"] = [
            {"status": "ok", "start": {"seq": 6, "done": True, "ok": False}},
            {"status": "ok", "start": {"seq": 7, "done": True, "ok": True}}]
        r = self.kri("sam", "start")
        self.assertEqual(r.returncode, 0, r.stderr)
        res = json.loads(r.stdout)
        self.assertTrue(res["daemon"]["reachable"])
        statuses = [q for q in FakePlugin.requests_log
                    if q["action"] == "sam_status"]
        self.assertEqual(len(statuses), 2)  # ignoró el resultado viejo (seq 6)

    def test_sam_start_no_wait_and_stop(self):
        FakePlugin.responses["sam_start"] = {"status": "ok", "seq": 1}
        r = self.kri("sam", "start", "--no-wait")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.last_request()["action"], "sam_start")
        self.assertEqual(len(FakePlugin.requests_log), 1)
        r = self.kri("sam", "stop")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.last_request()["action"], "sam_stop")

    def test_sam_start_url_mismatch_error_surfaces(self):
        FakePlugin.responses["sam_start"] = {
            "error": "AI Select manages the daemon at http://127.0.0.1:5679",
            "url": "http://127.0.0.1:5679"}
        r = self.kri("sam", "start")
        self.assertEqual(r.returncode, 1)
        self.assertIn("AI Select manages", r.stderr)

    def test_select_from_mask_file(self):
        FakePlugin.responses["select_from_mask"] = {
            "status": "ok", "selection": {"x": 0, "y": 0,
                                          "width": 10, "height": 10}}
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "mask.png")
            with open(path, "wb") as f:
                f.write(b"maskbytes")
            r = self.kri("select", "from-mask", path, "--mode", "intersect")
        self.assertEqual(r.returncode, 0, r.stderr)
        req = self.last_request()
        self.assertEqual(req["action"], "select_from_mask")
        self.assertEqual(req["params"]["mask_b64"],
                         base64.b64encode(b"maskbytes").decode())
        self.assertEqual(req["params"]["mode"], "intersect")

    def test_set_prompt_requires_p_or_n(self):
        r = self.kri("ai", "set-prompt")
        self.assertEqual(r.returncode, 2)

    def test_generate_no_wait_returns_immediately(self):
        FakePlugin.responses["ai_generate"] = {"status": "ok",
                                               "workspace": "generation"}
        r = self.kri("ai", "generate")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(FakePlugin.requests_log), 1)

    def test_generate_wait_polls_until_idle(self):
        FakePlugin.responses["ai_generate"] = {"status": "ok"}
        FakePlugin.responses["ai_status"] = [
            {"status": "ok", "queue": {"queued": 1, "executing": 0}},
            {"status": "ok", "queue": {"queued": 0, "executing": 1}},
            {"status": "ok", "queue": {"queued": 0, "executing": 0}},
        ]
        r = self.kri("ai", "generate", "--wait", "--poll", "0.05")
        self.assertEqual(r.returncode, 0, r.stderr)
        final = json.loads(r.stdout)
        self.assertEqual(final["final"]["queue"]["executing"], 0)
        status_calls = [q for q in FakePlugin.requests_log
                        if q["action"] == "ai_status"]
        self.assertEqual(len(status_calls), 3)

    def test_generate_wait_timeout_exits_1(self):
        FakePlugin.responses["ai_generate"] = {"status": "ok"}
        FakePlugin.responses["ai_status"] = {
            "status": "ok", "queue": {"queued": 0, "executing": 1}}
        r = self.kri("ai", "generate", "--wait", "--poll", "0.05",
                     "--timeout", "0.2")
        self.assertEqual(r.returncode, 1)
        self.assertIn("still generating", r.stderr)

    def test_exec_sends_code_and_timeout(self):
        FakePlugin.responses["exec"] = {"status": "ok", "stdout": "hi\n"}
        r = self.kri("exec", "--timeout", "60", stdin='print("hi")')
        self.assertEqual(r.returncode, 0, r.stderr)
        req = self.last_request()
        self.assertEqual(req["action"], "exec")
        self.assertEqual(req["params"]["code"], 'print("hi")')
        self.assertEqual(req["timeout"], 60.0)

    def test_exec_disabled_error_surfaces(self):
        FakePlugin.responses["exec"] = {
            "error": ("exec is disabled. Start Krita with "
                      "KRITAMCP_ALLOW_EXEC=1 to enable it.")}
        r = self.kri("exec", stdin="print(1)")
        self.assertEqual(r.returncode, 1)
        self.assertIn("KRITAMCP_ALLOW_EXEC", r.stderr)

    # ----- diálogo modal (fix/krita-live) -----

    def test_dialog_maps_to_modal_dialog(self):
        FakePlugin.responses["modal_dialog"] = {
            "status": "ok", "modal": {"class": "QMessageBox", "title": "Krita",
                                      "text": "Ha ocurrido un problema"},
            "dismissed": False}
        r = self.kri("dialog")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.last_request()["action"], "modal_dialog")
        self.assertEqual(self.last_request()["params"], {"dismiss": False})
        self.assertIn("QMessageBox", r.stdout)

    def test_dialog_dismiss(self):
        r = self.kri("dialog", "--dismiss")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.last_request()["params"], {"dismiss": True})

    def test_blocked_by_modal_error_is_passed_through(self):
        FakePlugin.responses["open_file"] = {
            "error": "Krita abrió un diálogo modal durante 'open_file'",
            "modal_dialog": {"title": "Krita"},
            "hint": "cerralo en Krita o con `kri dialog --dismiss`"}
        r = self.kri("open", "/tmp/x.kra")
        self.assertEqual(r.returncode, 1)
        self.assertIn("diálogo modal", json.loads(r.stderr)["error"])

    def test_read_timeout_is_not_reported_as_cannot_connect(self):
        import time
        FakePlugin.hooks["exec"] = lambda cmd: time.sleep(2.5)
        r = self.kri("exec", "--timeout", "1", stdin="result = 1")
        self.assertEqual(r.returncode, 1)
        err = json.loads(r.stderr)
        self.assertNotIn("Cannot connect", err["error"])
        self.assertTrue(err.get("timeout"))
        self.assertIn("kri dialog", err["hint"])


if __name__ == "__main__":
    unittest.main()
