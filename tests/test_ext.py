"""Tests del sistema de extensiones de kri con una extensión de juguete.

Cada clase arma su propio ext-venv en un XDG_DATA_HOME temporal e instala
wheels de juguete construidas acá mismo (zip a mano: no hace falta red ni un
build backend). Los comandos corren el kri real por subprocess, contra el
plugin falso de test_kri."""
import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import threading
import unittest
import zipfile
from http.server import HTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_kri import FakePlugin  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KRI = os.path.join(REPO, "cli", "kri")
SERVER = os.path.join(REPO, "mcp", "server.py")

TOY_EXT = '''
def register(ctx):
    sp = ctx.parser.add_subparsers(dest="cmd", metavar="command")
    p = sp.add_parser("echo", help="repite TEXT")
    p.add_argument("text")
    p.add_argument("--upper", action="store_true")
    p.add_argument("--times", type=int, default=1)
    p.set_defaults(fn=echo)
    sp.add_parser("info").set_defaults(fn=info)
    sp.add_parser("ping").set_defaults(fn=ping)
    sp.add_parser("boom").set_defaults(fn=boom)
    sp.add_parser("ret").set_defaults(fn=lambda args, ctx: {"status": "ok", "ret": 1})


def echo(args, ctx):
    text = args.text.upper() if args.upper else args.text
    ctx.out({"status": "ok", "echo": " ".join([text] * args.times)})


def info(args, ctx):
    ctx.out({"status": "ok", "version": ctx.version, "json": ctx.json,
             "krita_url": ctx.krita_url, "autoselect_url": ctx.autoselect_url,
             "data_dir": ctx.data_dir, "config_dir": ctx.config_dir,
             "name": ctx.name})


def ping(args, ctx):
    ctx.out({"status": "ok", "plugin": ctx.send("toy_ping", {"x": 1})})


def boom(args, ctx):
    raise ValueError("kaboom")
'''

TOY_MANIFEST = '''
[extension]
name = "{name}"
version = "{version}"
description = "Extensión de juguete para los tests"
min_kri = "{min_kri}"
entry_point = "{pkg}.kri_ext:register"

[dependencies]
python = ">=3.8"
services = ["claude-cli", "flux-capacitor"]

[[commands]]
name = "echo"
help = "repite TEXT"

[[mcp_tools]]
name = "{name}_echo"
command = ["echo"]
description = "Repite un texto"
params = {{ text = {{type = "string", required = true}}, upper = {{type = "boolean"}}, times = {{type = "integer"}} }}
positional = ["text"]
timeout_s = 30
annotations = {{ readOnlyHint = true }}
'''


def make_wheel(outdir, dist, version, pkg, files, entry_points):
    """Wheel py3-none-any mínima: {pkg}/… + dist-info con entry_points."""
    info = f"{dist.replace('-', '_')}-{version}.dist-info"
    entries = {f"{pkg}/{name}": body for name, body in files.items()}
    entries[f"{info}/METADATA"] = (f"Metadata-Version: 2.1\nName: {dist}\n"
                                   f"Version: {version}\n")
    entries[f"{info}/WHEEL"] = ("Wheel-Version: 1.0\nGenerator: kri-tests\n"
                                "Root-Is-Purelib: true\nTag: py3-none-any\n")
    if entry_points:
        entries[f"{info}/entry_points.txt"] = "[kri.extensions]\n" + "".join(
            f"{k} = {v}\n" for k, v in entry_points.items())
    record = []
    for name, body in entries.items():
        data = body.encode()
        digest = base64.urlsafe_b64encode(
            hashlib.sha256(data).digest()).rstrip(b"=").decode()
        record.append(f"{name},sha256={digest},{len(data)}")
    record.append(f"{info}/RECORD,,")
    entries[f"{info}/RECORD"] = "\n".join(record) + "\n"
    path = os.path.join(outdir, f"{dist.replace('-', '_')}-{version}-py3-none-any.whl")
    with zipfile.ZipFile(path, "w") as zf:
        for name, body in entries.items():
            zf.writestr(name, body)
    return path


def toy_wheel(outdir, name="toy", version="0.1.0", min_kri="0.5.0",
              ext_code=TOY_EXT, manifest=True, entry_point=True, pkg=None):
    pkg = pkg or f"kri_{name}"
    files = {"__init__.py": "", "kri_ext.py": ext_code}
    if manifest:
        files["kri-extension.toml"] = TOY_MANIFEST.format(
            name=name, version=version, min_kri=min_kri, pkg=pkg)
    eps = {name: f"{pkg}.kri_ext:register"} if entry_point else {}
    return make_wheel(outdir, f"kri-{name}", version, pkg, files, eps)


def mcp_python():
    """Un python con fastmcp para probar el server (o None)."""
    candidates = [os.environ.get("KRI_MCP_PYTHON"),
                  os.path.expanduser("~/.local/share/krita-mcp/.venv/bin/python"),
                  os.path.expanduser("~/.local/opt/krita-mcp/.venv/bin/python")]
    for py in filter(None, candidates):
        if os.path.exists(py) and subprocess.run(
                [py, "-c", "import fastmcp"], capture_output=True).returncode == 0:
            return py
    return None


class ExtBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="kri-ext-test-")
        cls.wheels = os.path.join(cls.tmp, "wheels")
        os.makedirs(cls.wheels)
        cls.server = HTTPServer(("127.0.0.1", 0), FakePlugin)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.env = dict(os.environ)
        cls.env.update(
            KRITA_URL=f"http://127.0.0.1:{cls.server.server_address[1]}",
            XDG_DATA_HOME=os.path.join(cls.tmp, "data"),
            XDG_CONFIG_HOME=os.path.join(cls.tmp, "config"),
            AUTOSELECT_URL="http://127.0.0.1:9",  # nada escucha ahí
        )
        cls.env.pop("KRI_DEBUG", None)
        cls.venv = os.path.join(cls.tmp, "data", "kri", "ext-venv")

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        FakePlugin.requests_log = []
        FakePlugin.responses = {}
        FakePlugin.hooks = {}

    @classmethod
    def run_kri(cls, *args, env=None):
        return subprocess.run([sys.executable, KRI, *args], capture_output=True,
                              text=True, env=env or cls.env, timeout=300)

    def kri(self, *args, code=0):
        r = self.run_kri(*args)
        self.assertEqual(r.returncode, code, f"kri {args}: {r.stdout}{r.stderr}")
        return r

    def kri_json(self, *args, code=0):
        r = self.kri(*args, code=code)
        return json.loads(r.stdout if code == 0 else r.stderr)


class InstalledToyTest(ExtBase):
    """La extensión toy instalada una vez para toda la clase."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        wheel = toy_wheel(cls.wheels)
        r = cls.run_kri("ext", "install", wheel)
        if r.returncode != 0:
            raise RuntimeError(f"no se pudo instalar toy: {r.stdout}{r.stderr}")
        cls.install_result = json.loads(r.stdout)

    def test_install_reports_extension_and_creates_venv(self):
        res = self.install_result
        self.assertEqual(res["status"], "ok")
        self.assertTrue(res["created_venv"])
        self.assertEqual([r["name"] for r in res["installed"]], ["toy"])
        self.assertTrue(res["installed"][0]["ok"])
        self.assertTrue(os.path.isdir(self.venv))

    def test_extension_command_runs(self):
        res = self.kri_json("toy", "echo", "hola", "--upper", "--times", "2")
        self.assertEqual(res["echo"], "HOLA HOLA")

    def test_handler_can_return_a_dict(self):
        self.assertEqual(self.kri_json("toy", "ret")["ret"], 1)

    def test_ctx_fields(self):
        res = self.kri_json("toy", "info")
        self.assertEqual(res["version"], "0.5.0")
        self.assertIs(res["json"], True)
        self.assertEqual(res["name"], "toy")
        self.assertEqual(res["krita_url"], self.env["KRITA_URL"])
        self.assertEqual(res["data_dir"], os.path.join(self.tmp, "data", "kri"))
        self.assertEqual(res["config_dir"], os.path.join(self.tmp, "config", "kri"))

    def test_human_flag_reaches_ctx_and_output(self):
        r = self.kri("toy", "info", "--human")
        self.assertIn("json: no", r.stdout)
        self.assertIn("version: 0.5.0", r.stdout)

    def test_ctx_send_talks_to_the_plugin(self):
        res = self.kri_json("toy", "ping")
        self.assertEqual(FakePlugin.requests_log[-1]["action"], "toy_ping")
        self.assertEqual(FakePlugin.requests_log[-1]["params"], {"x": 1})
        self.assertEqual(res["plugin"]["status"], "ok")

    def test_handler_exception_is_json_error(self):
        res = self.kri_json("toy", "boom", code=1)
        self.assertIn("extensión toy falló", res["error"])
        self.assertIn("kaboom", res["error"])

    def test_missing_subcommand_is_usage_error(self):
        r = self.kri("toy", code=2)
        self.assertIn("usage: kri toy", r.stderr)

    def test_ext_list(self):
        res = self.kri_json("ext", "list")
        (row,) = res["extensions"]
        self.assertEqual((row["name"], row["version"], row["enabled"], row["ok"]),
                         ("toy", "0.1.0", True, True))
        self.assertEqual(row["mcp_tools"], ["toy_echo"])
        self.assertEqual(row["dist"], "kri-toy")

    def test_help_lists_extension(self):
        r = self.kri("--help")
        self.assertIn("toy", r.stdout)
        self.assertIn("Extensión de juguete", r.stdout)

    def test_disable_enable(self):
        self.kri_json("ext", "disable", "toy")
        try:
            res = self.kri_json("toy", "echo", "x", code=1)
            self.assertIn("deshabilitada", res["error"])
            self.assertEqual(res["hint"], "kri ext enable toy")
            rows = self.kri_json("ext", "list")["extensions"]
            self.assertFalse(rows[0]["enabled"])
            self.assertEqual(self.kri_json("ext", "mcp-tools")["tools"], [])
            with open(os.path.join(self.tmp, "config", "kri",
                                   "extensions.toml")) as f:
                self.assertIn('names = ["toy"]', f.read())
        finally:
            self.kri_json("ext", "enable", "toy")
        self.assertEqual(self.kri_json("toy", "echo", "x")["echo"], "x")

    def test_enable_unknown_extension_fails(self):
        res = self.kri_json("ext", "enable", "nope", code=1)
        self.assertIn("nope", res["error"])

    def test_broken_extension_does_not_break_natives(self):
        target = os.path.join(self.venv, "lib",
                              "python%d.%d" % sys.version_info[:2],
                              "site-packages", "kri_toy", "kri_ext.py")
        with open(target) as f:
            good = f.read()
        with open(target, "w") as f:
            f.write("import paquete_que_no_existe\n")
        try:
            res = self.kri_json("toy", "echo", "x", code=1)
            self.assertIn("extensión toy rota", res["error"])
            self.assertIn("paquete_que_no_existe", res["error"])
            self.assertEqual(res["hint"], "kri ext list")
            # los nativos siguen andando
            self.assertEqual(self.kri_json("health")["status"], "ok")
            self.kri("--help")
            row = self.kri_json("ext", "list")["extensions"][0]
            self.assertFalse(row["ok"])
            self.assertIn("paquete_que_no_existe", row["reason"])
        finally:
            with open(target, "w") as f:
                f.write(good)

    def test_debug_adds_traceback(self):
        env = dict(self.env, KRI_DEBUG="1")
        r = self.run_kri("toy", "boom", env=env)
        self.assertIn("traceback", json.loads(r.stderr))

    def test_mcp_tools(self):
        res = self.kri_json("ext", "mcp-tools")
        (tool,) = res["tools"]
        self.assertEqual(tool["name"], "toy_echo")
        self.assertEqual(tool["extension"], "toy")
        self.assertEqual(tool["timeout_s"], 30)
        self.assertEqual(tool["annotations"], {"readOnlyHint": True})
        schema = tool["input_schema"]
        self.assertEqual(schema["required"], ["text"])
        self.assertEqual(schema["properties"]["upper"], {"type": "boolean"})
        self.assertEqual(tool["argv_example"],
                         ["kri", "toy", "echo", "<text>", "--upper",
                          "--times", "<times>"])
        self.assertEqual(res["skipped"], [])

    def test_doctor(self):
        res = self.kri_json("ext", "doctor")
        (row,) = res["extensions"]
        services = {s["service"]: s for s in row["services"]}
        self.assertIn(services["claude-cli"]["ok"], (True, False))
        self.assertIsNone(services["flux-capacitor"]["ok"])
        self.assertTrue(res["ext_venv"]["exists"])

    def test_native_commands_never_load_extensions(self):
        probe = textwrap.dedent(f"""
            import runpy, sys
            before = set(sys.modules)
            sys.argv = ["kri", "health"]
            try:
                runpy.run_path({KRI!r}, run_name="__main__")
            except SystemExit:
                pass
            new = set(sys.modules) - before
            sys.stderr.write(repr(sorted(m for m in new if m in (
                "kri_extensions", "importlib.metadata", "tomllib", "site"))))
        """)
        r = subprocess.run([sys.executable, "-c", probe], capture_output=True,
                           text=True, env=self.env)
        self.assertEqual(r.stderr.strip().splitlines()[-1], "[]")

    def test_mcp_server_registers_toy_tool(self):
        py = mcp_python()
        if not py:
            self.skipTest("no hay un python con fastmcp (KRI_MCP_PYTHON)")
        probe = textwrap.dedent(f"""
            import asyncio, importlib.util, json
            spec = importlib.util.spec_from_file_location("krita_mcp_server", {SERVER!r})
            server = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(server)
            from fastmcp import Client

            async def main():
                async with Client(server.mcp) as c:
                    tools = {{t.name: t for t in await c.list_tools()}}
                    t = tools["toy_echo"]
                    res = await c.call_tool("toy_echo", {{"text": "hola", "upper": True}})
                    print(json.dumps({{
                        "registered": server.EXTENSION_TOOLS,
                        "schema": t.inputSchema,
                        "readOnly": t.annotations.readOnlyHint,
                        "natives": "krita_layer_list" in tools,
                        "result": json.loads(res.content[0].text)}}))
            asyncio.run(main())
        """)
        env = dict(self.env, KRI_BIN=KRI)
        r = subprocess.run([py, "-c", probe], capture_output=True, text=True,
                           env=env, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr[-3000:])
        res = json.loads(r.stdout.strip().splitlines()[-1])
        self.assertEqual(res["registered"], ["toy_echo"])
        self.assertEqual(res["schema"]["required"], ["text"])
        self.assertTrue(res["readOnly"])
        self.assertTrue(res["natives"])
        self.assertEqual(res["result"]["echo"], "HOLA")


    def test_mcp_server_with_old_kri_registers_nothing(self):
        py = mcp_python()
        if not py:
            self.skipTest("no hay un python con fastmcp (KRI_MCP_PYTHON)")
        old = os.path.join(self.tmp, "old-kri")
        with open(old, "w") as f:
            f.write("#!/bin/sh\necho \"kri: invalid choice: 'ext'\" >&2\nexit 2\n")
        os.chmod(old, 0o755)
        probe = textwrap.dedent(f"""
            import importlib.util
            spec = importlib.util.spec_from_file_location("krita_mcp_server", {SERVER!r})
            server = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(server)
            print(server.EXTENSION_TOOLS)
        """)
        r = subprocess.run([py, "-c", probe], capture_output=True, text=True,
                           env=dict(self.env, KRI_BIN=old), timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr[-2000:])
        self.assertEqual(r.stdout.strip().splitlines()[-1], "[]")
        self.assertIn("too old for extensions", r.stderr)


class InstallValidationTest(ExtBase):
    """install valida y hace rollback; remove desinstala."""

    def installed(self):
        return [r["name"] for r in self.kri_json("ext", "list")["extensions"]]

    def test_min_kri_too_new_is_rolled_back(self):
        wheel = toy_wheel(self.wheels, name="future", min_kri="99.0")
        res = self.kri_json("ext", "install", wheel, code=1)
        self.assertIn("pide kri >= 99.0", res["error"])
        self.assertEqual(res["rolled_back"], ["kri-future"])
        self.assertNotIn("future", self.installed())

    def test_import_error_is_rolled_back(self):
        wheel = toy_wheel(self.wheels, name="rota",
                          ext_code="raise RuntimeError('rota al importar')\n")
        res = self.kri_json("ext", "install", wheel, code=1)
        self.assertIn("rota al importar", res["error"])
        self.assertNotIn("rota", self.installed())

    def test_missing_manifest_is_rolled_back(self):
        wheel = toy_wheel(self.wheels, name="sinmanifiesto", manifest=False)
        res = self.kri_json("ext", "install", wheel, code=1)
        self.assertIn("kri-extension.toml", res["error"])
        self.assertNotIn("sinmanifiesto", self.installed())

    def test_native_name_clash_is_rejected(self):
        wheel = toy_wheel(self.wheels, name="layer")
        res = self.kri_json("ext", "install", wheel, code=1)
        self.assertIn("choca con un comando nativo", res["error"])
        # y `kri layer` sigue siendo el nativo
        self.assertNotIn("layer", self.installed())

    def test_no_entry_point(self):
        wheel = toy_wheel(self.wheels, name="sinep", entry_point=False)
        res = self.kri_json("ext", "install", wheel, code=1)
        self.assertIn("kri.extensions", res["error"])
        self.kri_json("ext", "remove", "--dist", "kri-sinep")

    def test_install_then_remove(self):
        wheel = toy_wheel(self.wheels, name="efimera")
        self.kri_json("ext", "install", wheel)
        self.assertEqual(self.kri_json("efimera", "echo", "a")["echo"], "a")
        self.kri_json("ext", "disable", "efimera")
        res = self.kri_json("ext", "remove", "efimera")
        self.assertEqual(res["removed"], "kri-efimera")
        self.assertNotIn("efimera", self.installed())
        self.kri("efimera", "echo", "a", code=2)
        # remove limpia también el disabled
        with open(os.path.join(self.tmp, "config", "kri", "extensions.toml")) as f:
            self.assertNotIn("efimera", f.read())

    def test_reinstall_new_version(self):
        self.kri_json("ext", "install", toy_wheel(self.wheels, name="ver"))
        res = self.kri_json("ext", "install",
                            toy_wheel(self.wheels, name="ver", version="0.2.0"))
        self.assertEqual(res["installed"][0]["version"], "0.2.0")
        self.kri_json("ext", "remove", "ver")


class PathsTest(unittest.TestCase):
    """Rutas <data>/<config> por plataforma, sin tocar el sistema."""

    @classmethod
    def setUpClass(cls):
        import importlib.machinery
        import importlib.util
        loader = importlib.machinery.SourceFileLoader("kri_cli", KRI)
        spec = importlib.util.spec_from_loader("kri_cli", loader)
        kri = importlib.util.module_from_spec(spec)
        sys.modules["kri_cli"] = kri
        loader.exec_module(kri)
        cls.ext = kri.extensions()

    def test_linux_default_and_xdg(self):
        self.assertEqual(self.ext.kri_dirs({"HOME": "/h"}, "linux"),
                         ("/h/.local/share/kri", "/h/.config/kri"))
        self.assertEqual(self.ext.kri_dirs({"HOME": "/h", "XDG_DATA_HOME": "/d",
                                            "XDG_CONFIG_HOME": "/c"}, "linux"),
                         ("/d/kri", "/c/kri"))

    def test_macos(self):
        base = "/Users/u/Library/Application Support/kri"
        self.assertEqual(self.ext.kri_dirs({"HOME": "/Users/u"}, "darwin"),
                         (base, base))

    def test_windows(self):
        data, conf = self.ext.kri_dirs(
            {"USERPROFILE": r"C:\Users\u", "LOCALAPPDATA": r"C:\Users\u\AppData\Local"},
            "win32")
        self.assertTrue(data.startswith(r"C:\Users\u\AppData\Local"))
        self.assertTrue(data.endswith("kri"))
        self.assertEqual(data, conf)
        self.assertTrue(self.ext.venv_python("V", "win32").endswith("python.exe"))
        self.assertIn("Lib", self.ext.venv_site("V", "win32"))

    def test_spec_and_versions(self):
        self.assertTrue(self.ext.spec_ok((3, 14, 4), ">=3.11,<4")[0])
        self.assertFalse(self.ext.spec_ok((3, 10, 0), ">=3.11")[0])
        self.assertLess(self.ext.vtuple("0.5.0"), self.ext.vtuple("0.10"))

    def test_tool_argv(self):
        tool = {"command": ["run"], "positional": ["image"],
                "params": {"image": {"type": "string"},
                           "dry_run": {"type": "boolean"},
                           "krita": {"type": "boolean"},
                           "parts": {"type": "array"}}}
        self.assertEqual(
            self.ext.tool_argv("ap", tool, {"image": "a.png", "dry_run": True,
                                            "krita": False, "parts": ["x", "y"]}),
            ["ap", "run", "a.png", "--dry-run", "--parts", "x", "y"])


if __name__ == "__main__":
    unittest.main()
