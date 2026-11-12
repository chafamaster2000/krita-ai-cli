"""Tests de cli/install.py (contrato de componente §8).

Rutas por plataforma con environ falso; merges de kritarc y settings.json;
y el ciclo install → reinstall (0 cambios) → uninstall (árbol vacío) en un
HOME temporal, sin el server MCP (eso pide red y el CLI claude)."""
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location(
    "kri_install", os.path.join(REPO, "cli", "install.py"))
install = importlib.util.module_from_spec(spec)
spec.loader.exec_module(install)

KINDS = {"copy", "symlink", "write", "kritarc", "venv", "pip", "mcp", "skill"}


class PathsTest(unittest.TestCase):
    def test_linux(self):
        p = install.paths({"HOME": "/h"}, "linux")
        self.assertEqual(p["shim"], "/h/.local/bin/kri")
        self.assertEqual(p["pykrita"], "/h/.local/share/krita/pykrita")
        self.assertEqual(p["kritarc"], "/h/.config/kritarc")
        self.assertEqual(p["ext_venv"], "/h/.local/share/kri/ext-venv")
        self.assertEqual(p["mcp_server"], "/h/.local/share/krita-mcp/server.py")
        self.assertEqual(p["skills"], "/h/.claude/skills")
        p = install.paths({"HOME": "/h", "XDG_DATA_HOME": "/d",
                           "XDG_CONFIG_HOME": "/c"}, "linux")
        self.assertEqual(p["ext_venv"], "/d/kri/ext-venv")
        self.assertEqual(p["kritarc"], "/c/kritarc")

    def test_macos(self):
        p = install.paths({"HOME": "/Users/u"}, "darwin")
        self.assertEqual(p["pykrita"],
                         "/Users/u/Library/Application Support/krita/pykrita")
        self.assertEqual(p["kritarc"], "/Users/u/Library/Preferences/kritarc")
        self.assertEqual(p["ext_venv"],
                         "/Users/u/Library/Application Support/kri/ext-venv")

    def test_windows(self):
        env = {"USERPROFILE": r"C:\Users\u", "APPDATA": r"C:\Users\u\AppData\Roaming",
               "LOCALAPPDATA": r"C:\Users\u\AppData\Local"}
        p = install.paths(env, "win32")
        self.assertEqual(p["shim"], r"C:\Users\u\AppData\Local\Programs\kri\kri.cmd")
        self.assertEqual(p["pykrita"], r"C:\Users\u\AppData\Roaming\krita\pykrita")
        self.assertEqual(p["kritarc"], r"C:\Users\u\AppData\Local\kritarc")
        self.assertEqual(p["ext_venv_python"],
                         r"C:\Users\u\AppData\Local\kri\ext-venv\Scripts\python.exe")
        self.assertEqual(p["skills"], r"C:\Users\u\.claude\skills")

    def test_claude_config_dir(self):
        for plat, home in (("linux", "/h"), ("darwin", "/Users/u")):
            p = install.paths({"HOME": home, "CLAUDE_CONFIG_DIR": "/cc"}, plat)
            self.assertEqual(p["claude"], "/cc")
            self.assertEqual(p["skills"], "/cc/skills")
            self.assertEqual(p["hooks"], "/cc/hooks")
            self.assertEqual(p["settings"], "/cc/settings.json")
            self.assertEqual(p["claude_json"], "/cc/.claude.json")
        p = install.paths({"HOME": "/h", "CLAUDE_CONFIG_DIR": ""}, "linux")
        self.assertEqual(p["claude"], "/h/.claude")
        self.assertEqual(p["claude_json"], "/h/.claude.json")

    def test_windows_plan_uses_copies_and_cmd_shim(self):
        env = {"platform": "win32", "USERPROFILE": r"C:\Users\u",
               "APPDATA": r"C:\Users\u\AppData\Roaming",
               "LOCALAPPDATA": r"C:\Users\u\AppData\Local",
               "python": r"C:\Python312\python.exe",
               "KRI_INSTALL_SKIP": "mcp"}
        acts = install.install_component(plan_only=True, env=env)
        self.assertTrue(all(a["kind"] in KINDS for a in acts))
        shim = next(a for a in acts if a["component"] == "shim")
        self.assertEqual(shim["kind"], "write")
        self.assertNotIn("symlink", {a["kind"] for a in acts})


class MergeTest(unittest.TestCase):
    def test_kritarc_add_and_remove(self):
        base = "[General]\nfoo=1\n\n[python]\nenable_other=true\n"
        added = install._kritarc_set(base, "true")
        self.assertIn("[python]\nenable_other=true\nenable_kritamcp=true", added)
        self.assertEqual(install._kritarc_set(added, "true"), added)
        self.assertEqual(install._kritarc_set(added, None).strip(), base.strip())

    def test_kritarc_from_scratch_and_back_to_empty(self):
        added = install._kritarc_set("", "true")
        self.assertEqual(added, "[python]\nenable_kritamcp=true\n")
        self.assertEqual(install._kritarc_set(added, None), "")

    def test_kritarc_flips_false(self):
        added = install._kritarc_set("[python]\nenable_kritamcp=false\n[x]\n", "true")
        self.assertIn("enable_kritamcp=true", added)
        self.assertNotIn("false", added)

    def test_settings_merge_is_reversible(self):
        p = install.paths({"HOME": "/h"}, "linux")
        user = {"permissions": {"allow": ["Bash(ls:*)"]}, "model": "x",
                "hooks": {"PreToolUse": [{"matcher": "Edit", "hooks": [
                    {"type": "command", "command": "otro"}]}]}}
        merged = install._settings_merge(user, p, add=True)
        self.assertIn("Bash(kri:*)", merged["permissions"]["allow"])
        self.assertEqual(merged["permissions"]["ask"], ["Bash(kri exec:*)"])
        self.assertEqual(len(merged["hooks"]["PreToolUse"]), 2)
        self.assertEqual(install._settings_merge(merged, p, add=True), merged)
        self.assertEqual(install._settings_merge(merged, p, add=False), user)

    def test_settings_hook_uses_absolute_python_and_migrates(self):
        p = install.paths({"HOME": "/h"}, "linux")
        py = "/opt/py 3/bin/python3"
        old = install._settings_merge({}, p, add=True)  # el `python3 …` de antes
        cmd = old["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        self.assertTrue(cmd.startswith("python3 "))
        new = install._settings_merge(old, p, add=True, python=py)
        hooks = new["hooks"]["PreToolUse"]
        self.assertEqual(len(hooks), 1)
        self.assertEqual(hooks[0]["hooks"][0]["command"],
                         "'/opt/py 3/bin/python3' \"/h/.claude/hooks/"
                         "krita-prompt-reminder.py\"")
        self.assertEqual(install._settings_merge(new, p, add=True, python=py),
                         new)
        self.assertEqual(install._settings_merge(new, p, add=False), {})
        # Windows no cambia: `python`
        w = install.paths({"USERPROFILE": r"C:\Users\u"}, "win32")
        self.assertTrue(install._hook_command(w, py).startswith("python "))


class LiveCycleTest(unittest.TestCase):
    def test_install_reinstall_uninstall_in_temp_home(self):
        home = tempfile.mkdtemp(prefix="kri-install-test-")
        self.addCleanup(shutil.rmtree, home, True)
        env = {"HOME": home, "PATH": os.environ.get("PATH", ""),
               "platform": "linux", "KRI_INSTALL_SKIP": "mcp"}
        first = install.install_component(plan_only=False, env=env)
        self.assertFalse([a for a in first if a.get("error")], first)
        self.assertTrue(all(a["status"] == "create" for a in first))
        shim = os.path.join(home, ".local", "bin", "kri")
        self.assertFalse(os.path.islink(shim))
        self.assertTrue(os.access(shim, os.X_OK))
        with open(shim) as f:
            self.assertIn(sys.executable, f.read())
        self.assertTrue(os.path.isfile(os.path.join(
            home, ".local", "share", "krita", "pykrita", "kritamcp", "__init__.py")))
        self.assertTrue(os.path.exists(os.path.join(
            home, ".local", "share", "kri", "ext-venv", "bin", "python")))
        with open(os.path.join(home, ".claude", "settings.json")) as f:
            self.assertIn("Bash(kri:*)", json.load(f)["permissions"]["allow"])

        again = install.install_component(plan_only=False, env=env)
        self.assertEqual({a["status"] for a in again}, {"ok"}, again)

        gone = install.install_component(plan_only=False, uninstall=True, env=env)
        self.assertFalse([a for a in gone if a.get("error")], gone)
        self.assertEqual(os.listdir(home), [])
        # sin manifiesto no queda nada que deshacer
        self.assertEqual(install.install_component(
            plan_only=True, uninstall=True, env=env), [])

    def test_uninstall_without_manifest_touches_nothing(self):
        home = tempfile.mkdtemp(prefix="kri-install-test-")
        self.addCleanup(shutil.rmtree, home, True)
        bin_dir = os.path.join(home, ".local", "bin")
        os.makedirs(bin_dir)
        os.symlink(os.path.join(REPO, "cli", "kri"), os.path.join(bin_dir, "kri"))
        env = {"HOME": home, "platform": "linux", "KRI_INSTALL_SKIP": "mcp"}
        self.assertEqual(install.install_component(
            plan_only=False, uninstall=True, env=env), [])
        self.assertTrue(os.path.islink(os.path.join(bin_dir, "kri")))


class LauncherMigrationTest(unittest.TestCase):
    """~/.local/bin/kri pasa de symlink a lanzador sh con el python absoluto
    (desde Finder el PATH es mínimo y `python3` es otro)."""

    SKIP = "mcp,extvenv,plugin,kritarc,skills"

    def setUp(self):
        self.home = tempfile.mkdtemp(prefix="kri-launcher-test-")
        self.addCleanup(shutil.rmtree, self.home, True)
        self.p = install.paths({"HOME": self.home}, "linux")
        self.env = {"HOME": self.home, "platform": "linux",
                    "PATH": os.environ.get("PATH", ""),
                    "KRI_INSTALL_SKIP": self.SKIP}
        self.src = os.path.join(REPO, "cli", "kri")

    def test_launcher_runs_without_path(self):
        done = install.install_component(plan_only=False, env=self.env)
        self.assertFalse([a for a in done if a.get("error")], done)
        import subprocess
        r = subprocess.run([self.p["shim"], "--version"], capture_output=True,
                           text=True, env={"PATH": "/nonexistent"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("kri", r.stdout + r.stderr)
        with open(self.p["settings"]) as f:
            hook = json.load(f)["hooks"]["PreToolUse"][0]["hooks"][0]
        self.assertTrue(hook["command"].startswith(sys.executable + " ")
                        or hook["command"].startswith("'" + sys.executable))

    def test_hand_symlink_is_backed_up_and_restored(self):
        """Lo de hoy en una máquina instalada a mano: symlink a cli/kri fuera
        del manifiesto y un settings.json con el hook `python3 …`."""
        os.makedirs(self.p["bin_dir"])
        os.symlink(self.src, self.p["shim"])
        os.makedirs(self.p["claude"])
        old = install._settings_merge({"model": "x"}, self.p, add=True)
        with open(self.p["settings"], "w") as f:
            json.dump(old, f)
        before = snapshot(self.home)

        plan = install.install_component(plan_only=True, env=self.env)
        shim = next(a for a in plan if a["component"] == "shim")
        self.assertEqual((shim["kind"], shim["status"]), ("write", "update"))
        self.assertTrue(shim["backup"])
        self.assertIn("symlink", shim["detail"])

        done = install.install_component(plan_only=False, env=self.env)
        self.assertFalse([a for a in done if a.get("error")], done)
        self.assertFalse(os.path.islink(self.p["shim"]))
        self.assertTrue(os.path.islink(shim["backup"]))
        with open(self.p["settings"]) as f:
            cmd = json.load(f)["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        self.assertIn(sys.executable, cmd)
        again = install.install_component(plan_only=False, env=self.env)
        self.assertEqual({a["status"] for a in again}, {"ok"}, again)

        gone = install.install_component(plan_only=False, uninstall=True,
                                         env=self.env)
        self.assertFalse([a for a in gone if a.get("error")], gone)
        self.assertEqual(snapshot(self.home), before)

    def test_symlink_from_manifest_is_replaced_without_backup(self):
        """El symlink lo puso un install de kri anterior: se reemplaza, la
        entrada vieja se va y el uninstall saca el lanzador."""
        os.makedirs(self.p["bin_dir"])
        os.symlink(self.src, self.p["shim"])
        os.makedirs(os.path.dirname(self.p["manifest"]))
        eid = "symlink|" + self.p["shim"]
        with open(self.p["manifest"], "w") as f:
            json.dump({"version": 1, "component": "kri", "entries": [
                {"id": eid, "component": "shim", "kind": "symlink",
                 "src": self.src, "dst": self.p["shim"]}]}, f)
        env = dict(self.env, KRI_INSTALL_SKIP=self.SKIP + ",hook,settings")
        plan = install.install_component(plan_only=True, env=env)
        self.assertEqual([a["status"] for a in plan], ["update"])
        self.assertNotIn("backup", plan[0])
        done = install.install_component(plan_only=False, env=env)
        self.assertFalse([a for a in done if a.get("error")], done)
        with open(self.p["manifest"]) as f:
            ids = [e["id"] for e in json.load(f)["entries"]]
        self.assertEqual(ids, ["write|" + self.p["shim"]])
        self.assertFalse(os.path.isdir(self.p["backups"]))
        gone = install.install_component(plan_only=False, uninstall=True,
                                         env=env)
        self.assertFalse([a for a in gone if a.get("error")], gone)
        self.assertEqual(os.listdir(self.home), [])


FAKE_CLAUDE = """#!{python}
# claude falso: sólo `mcp add-json NAME JSON --scope user` y `mcp remove NAME`,
# sobre $HOME/.claude.json (como el real, scope user = mcpServers de arriba).
import json, os, sys
path = os.path.join(os.environ["HOME"], ".claude.json")
try:
    data = json.load(open(path))
except (OSError, ValueError):
    data = {{}}
args = sys.argv[1:]
servers = data.setdefault("mcpServers", {{}})
if args[:2] == ["mcp", "add-json"]:
    servers[args[2]] = json.loads(args[3])
elif args[:2] == ["mcp", "remove"]:
    if args[2] not in servers:
        sys.exit(1)
    del servers[args[2]]
else:
    sys.exit(2)
json.dump(data, open(path, "w"), indent=2)
"""


def snapshot(root):
    """{ruta relativa: contenido | ('link', destino) | json parseado}."""
    snap = {}
    for d, dirs, files in os.walk(root):
        for name in dirs + files:
            full = os.path.join(d, name)
            rel = os.path.relpath(full, root)
            if os.path.islink(full):
                snap[rel] = ("link", os.readlink(full))
                if name in dirs:
                    dirs.remove(name)
            elif os.path.isdir(full):
                snap[rel] = "dir"
            elif name.endswith(".json"):
                with open(full) as f:
                    snap[rel] = json.load(f)
            else:
                with open(full, "rb") as f:
                    snap[rel] = f.read()
    return snap


class BackupRestoreTest(unittest.TestCase):
    """Con cosas ajenas instaladas (server MCP krita, skills copiadas, otro
    shim, kritarc y settings propios), install respalda y uninstall deja
    todo como estaba."""

    def setUp(self):
        self.home = home = tempfile.mkdtemp(prefix="kri-backup-test-")
        self.addCleanup(shutil.rmtree, home, True)
        tools = os.path.join(home, "tools")  # fuera del árbol que se compara
        os.makedirs(tools)
        fake = os.path.join(tools, "claude")
        with open(fake, "w") as f:
            f.write(FAKE_CLAUDE.format(python=sys.executable))
        os.chmod(fake, 0o755)
        self.root = os.path.join(home, "u")
        os.makedirs(self.root)
        r = self.root
        p = install.paths({"HOME": r}, "linux")
        self.p = p
        # skills copiadas a mano (lo que hay hoy en el HOME real)
        for name in ("kri", "kri-prompt-format"):
            os.makedirs(os.path.join(p["skills"], name))
            with open(os.path.join(p["skills"], name, "SKILL.md"), "w") as f:
                f.write(f"copia vieja de {name}\n")
        os.makedirs(p["bin_dir"])
        os.symlink("/usr/bin/true", p["shim"])
        with open(p["claude_json"], "w") as f:
            json.dump({"numStartups": 3, "mcpServers": {
                "krita": {"type": "stdio", "command": "/opt/krita-mcp/python",
                          "args": ["/opt/krita-mcp/server.py"],
                          "env": {"PATH": "/x"}},
                "otro": {"type": "stdio", "command": "otro"}}}, f)
        os.makedirs(os.path.dirname(p["kritarc"]))
        with open(p["kritarc"], "w") as f:
            f.write("[General]\nx=1\n\n[python]\nenable_kritamcp=false\n")
        os.makedirs(p["claude"], exist_ok=True)
        with open(p["settings"], "w") as f:
            f.write('{"permissions": {"allow": ["Bash(kri:*)"]}, "model": "x"}')
        # venv del MCP ya armado: no hace falta red (queda "ok", no es nuestro)
        os.makedirs(os.path.dirname(p["mcp_python"]))
        open(p["mcp_python"], "w").close()
        with open(os.path.join(p["mcp_venv"], ".kri-requirements.sha256"), "w") as f:
            f.write(install._hash_tree(os.path.join(REPO, "mcp", "requirements.txt")))
        self.env = {"HOME": r, "platform": "linux",
                    "PATH": tools + os.pathsep + os.environ.get("PATH", "")}

    def test_plan_shows_backups(self):
        acts = install.install_component(plan_only=True, env=self.env)
        by_dst = {a["dst"]: a for a in acts}
        skill = by_dst[os.path.join(self.p["skills"], "kri")]
        self.assertEqual(skill["status"], "update")
        self.assertTrue(skill["backup"].startswith(
            os.path.join(self.p["kri_data"], "backups")))
        self.assertTrue(by_dst[self.p["shim"]]["backup"])
        mcp = by_dst["claude:user:krita"]
        self.assertEqual(mcp["status"], "update")
        self.assertEqual(mcp["backup"]["mcpServers"]["krita"]["command"],
                         "/opt/krita-mcp/python")
        # nada se tocó
        self.assertFalse(os.path.exists(self.p["manifest"]))

    def test_install_then_uninstall_restores_everything(self):
        before = snapshot(self.root)
        done = install.install_component(plan_only=False, env=self.env)
        self.assertFalse([a for a in done if a.get("error")], done)

        # instalado: lo nuestro en su lugar, lo ajeno respaldado
        self.assertTrue(os.path.islink(os.path.join(self.p["skills"], "kri")))
        with open(self.p["claude_json"]) as f:
            cj = json.load(f)
        self.assertEqual(cj["mcpServers"]["krita"]["args"], [self.p["mcp_server"]])
        self.assertIn("otro", cj["mcpServers"])
        with open(self.p["manifest"]) as f:
            manifest = json.load(f)
        entries = {e["id"]: e for e in manifest["entries"]}
        self.assertEqual(entries["mcp|user|krita"]["prev"]["command"],
                         "/opt/krita-mcp/python")
        skill = entries["skill|" + os.path.join(self.p["skills"], "kri")]
        with open(os.path.join(skill["backup"], "SKILL.md")) as f:
            self.assertEqual(f.read(), "copia vieja de kri\n")
        self.assertEqual(entries["kritarc|" + self.p["kritarc"]]["prev"], "false")

        again = install.install_component(plan_only=False, env=self.env)
        self.assertEqual({a["status"] for a in again}, {"ok"}, again)

        plan = install.install_component(plan_only=True, uninstall=True,
                                         env=self.env)
        restores = [a for a in plan if a.get("restore")]
        self.assertTrue(any(a["kind"] == "mcp" for a in restores))
        gone = install.install_component(plan_only=False, uninstall=True,
                                         env=self.env)
        self.assertFalse([a for a in gone if a.get("error")], gone)
        self.assertEqual(snapshot(self.root), before)


if __name__ == "__main__":
    unittest.main()
