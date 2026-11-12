#!/usr/bin/env python3
"""Instalación de kri: contrato de componente del instalador del ecosistema.

    install_component(plan_only: bool, uninstall: bool, env) -> list[Action]
    Action = {"kind": "copy|symlink|write|kritarc|venv|pip|mcp|skill",
              "src", "dst", "detail",
              "status": "create|update|ok|remove|absent|skip",
              "component": "shim|plugin|kritarc|skills|hook|settings|extvenv|mcp",
              # sólo si plan_only=False:
              "done": bool, "error": str|None}

`status` dice qué haría la acción: create/update/remove cambian algo; ok
(ya está), absent (no hay nada que desinstalar) y skip (no se toca, con el
motivo en detail) no. Con plan_only=False se ejecutan las que cambian algo,
así que una segunda corrida da todo en ok (idempotente).

Respaldos (mismo esquema que el instalador de autopainter, bajo <data>/kri):
lo que ya existe y no instaló kri se MUEVE a
<data>/kri/backups/<fecha>/<ruta absoluta> antes de reemplazarlo; el server
MCP `krita` previo y el valor previo de kritarc se guardan en
<data>/kri/install-manifest.json. En el plan, `backup` dice adónde va cada
cosa (al instalar) y `restore` qué vuelve (al desinstalar). El uninstall sólo
deshace lo que dice el manifiesto, al revés, y devuelve los respaldos.

`env` es un mapping tipo os.environ (HOME, USERPROFILE, APPDATA,
LOCALAPPDATA, XDG_*, PATH) más claves opcionales:
  platform      "linux" | "darwin" | "win32" (default sys.platform)
  python        intérprete para venvs y el shim de Windows (default sys.executable)
  KRI_INSTALL_SKIP  componentes a saltear, separados por coma (p. ej. "mcp,settings")

Qué instala (contrato del instalador del ecosistema):
  shim      kri en el PATH: lanzador sh ~/.local/bin/kri (Unix) o
            %LOCALAPPDATA%\\Programs\\kri\\kri.cmd (Windows), los dos con
            el intérprete del install escrito con ruta absoluta (no
            depende del PATH: desde Finder el python3 es otro y kri no
            vería el ext-venv). Un symlink viejo a cli/kri se migra.
  plugin    krita-plugin/kritamcp + .desktop copiados a pykrita
  kritarc   [python] enable_kritamcp=true (con Krita cerrado: Krita reescribe
            kritarc al salir)
  skills    claude/skills/* a ~/.claude/skills (symlink; copia en Windows)
  hook      claude/hooks/krita-prompt-reminder.py a ~/.claude/hooks
  settings  ~/.claude/settings.json: allow Bash(kri:*), ask Bash(kri exec:*) y
            el hook PreToolUse (en Unix, con el intérprete absoluto)
  (~/.claude es $CLAUDE_CONFIG_DIR si está seteada; ~/.claude.json pasa a
  $CLAUDE_CONFIG_DIR/.claude.json, como hace Claude Code)
  extvenv   <data>/kri/ext-venv (las extensiones: kri ext install)
  mcp       <data>/krita-mcp: venv + requirements + server.py, y
            `claude mcp add --scope user krita …`

CLI: python3 cli/install.py [--plan-json] [--uninstall] [--skip a,b]
"""
import hashlib
import json
import ntpath
import os
import posixpath
import shlex
import shutil
import subprocess
import sys
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COMPONENTS = ("shim", "plugin", "kritarc", "skills", "hook", "settings",
              "extvenv", "mcp")
HOOK_FILE = "krita-prompt-reminder.py"
ALLOW = ["Bash(kri:*)"]
ASK = ["Bash(kri exec:*)"]
MCP_NAME = "krita"


# ----- rutas -----

def paths(env, platform=None):
    """Rutas de destino para la plataforma (sin tocar el disco)."""
    platform = platform or env.get("platform") or sys.platform
    win = platform.startswith("win")
    P = ntpath if win else posixpath
    home = (env.get("USERPROFILE") if win else None) or env.get("HOME") \
        or os.path.expanduser("~")
    if win:
        local = env.get("LOCALAPPDATA") or P.join(home, "AppData", "Local")
        roaming = env.get("APPDATA") or P.join(home, "AppData", "Roaming")
        data = local
        bin_dir = P.join(local, "Programs", "kri")
        pykrita = P.join(roaming, "krita", "pykrita")
        kritarc = P.join(local, "kritarc")
        kri_data = kri_conf = P.join(local, "kri")
    elif platform == "darwin":
        data = P.join(home, "Library", "Application Support")
        bin_dir = P.join(home, ".local", "bin")
        pykrita = P.join(data, "krita", "pykrita")
        kritarc = P.join(home, "Library", "Preferences", "kritarc")
        kri_data = kri_conf = P.join(data, "kri")
    else:
        data = env.get("XDG_DATA_HOME") or P.join(home, ".local", "share")
        conf = env.get("XDG_CONFIG_HOME") or P.join(home, ".config")
        bin_dir = P.join(home, ".local", "bin")
        pykrita = P.join(data, "krita", "pykrita")
        kritarc = P.join(conf, "kritarc")
        kri_data, kri_conf = P.join(data, "kri"), P.join(conf, "kri")
    claude_base = env.get("CLAUDE_CONFIG_DIR")
    claude = claude_base or P.join(home, ".claude")
    mcp_dir = P.join(data, "krita-mcp")
    venv_bin = P.join("Scripts", "python.exe") if win else P.join("bin", "python")
    return {
        "platform": platform, "win": win, "home": home,
        "bin_dir": bin_dir,
        "shim": P.join(bin_dir, "kri.cmd" if win else "kri"),
        "pykrita": pykrita, "kritarc": kritarc,
        "claude": claude,
        "skills": P.join(claude, "skills"),
        "hooks": P.join(claude, "hooks"),
        "settings": P.join(claude, "settings.json"),
        "kri_data": kri_data, "kri_config": kri_conf,
        "ext_venv": P.join(kri_data, "ext-venv"),
        "ext_venv_python": P.join(kri_data, "ext-venv", venv_bin),
        "mcp_dir": mcp_dir,
        "mcp_venv": P.join(mcp_dir, ".venv"),
        "mcp_python": P.join(mcp_dir, ".venv", venv_bin),
        "mcp_server": P.join(mcp_dir, "server.py"),
        "claude_json": P.join(claude_base or home, ".claude.json"),
        "manifest": P.join(kri_data, "install-manifest.json"),
        "backups": P.join(kri_data, "backups"),
        "join": P.join,
    }


# ----- utilidades -----

def _hash_tree(path):
    """Hash de un archivo o de un árbol (sin __pycache__), o None si no existe."""
    if not os.path.exists(path):
        return None
    h = hashlib.sha256()
    if os.path.isfile(path):
        with open(path, "rb") as f:
            h.update(f.read())
        return h.hexdigest()
    for root, dirs, files in os.walk(path):
        dirs[:] = sorted(d for d in dirs if d != "__pycache__")
        for name in sorted(files):
            if name.endswith(".pyc"):
                continue
            full = os.path.join(root, name)
            h.update(os.path.relpath(full, path).replace(os.sep, "/").encode())
            with open(full, "rb") as f:
                h.update(f.read())
    return h.hexdigest()


def _links_to(dst, src):
    return os.path.islink(dst) and os.path.realpath(dst) == os.path.realpath(src)


def _remove(path):
    if os.path.islink(path) or os.path.isfile(path):
        os.remove(path)
    elif os.path.isdir(path):
        shutil.rmtree(path)


def _prune(path, home):
    """Borra directorios vacíos desde el padre de `path` hacia arriba, sin
    salir de HOME (para que un uninstall deje el árbol como estaba)."""
    home = os.path.realpath(home)
    d = os.path.dirname(path)
    while d and os.path.realpath(d) != home and \
            os.path.realpath(d).startswith(home + os.sep):
        try:
            os.rmdir(d)
        except OSError:
            break
        d = os.path.dirname(d)


def _ignore_pyc(_dir, names):
    return [n for n in names if n == "__pycache__" or n.endswith(".pyc")]


def _read(path):
    try:
        with open(path, newline="") as f:
            return f.read()
    except OSError:
        return None


def _load_json(path):
    text = _read(path)
    if text is None or not text.strip():
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


# ----- manifiesto y respaldos -----
# Mismo esquema que el instalador de autopainter (<data>/autopainter/...),
# bajo <data>/kri: lo ajeno que se reemplaza se MUEVE a
# backups/<fecha>/<ruta absoluta> (fuera de skills/ y pykrita, para que no se
# cargue dos veces) y queda anotado en install-manifest.json. El uninstall
# recorre el manifiesto al revés: saca lo nuestro y devuelve los respaldos.

class Ctx:
    def __init__(self, env):
        self.env = env
        self.p = paths(env)
        self.python = env.get("python") or sys.executable
        self.stamp = time.strftime("%Y%m%d-%H%M%S")
        data = _load_json(self.p["manifest"]) or {}
        self.entries = data.get("entries") or []

    def entry(self, eid):
        return next((e for e in self.entries if e["id"] == eid), None)

    def backup_path(self, dst):
        rel = dst.replace(":", "").lstrip("\\/")
        return self.p["join"](self.p["backups"], self.stamp, rel)

    def record(self, eid, fields):
        old = self.entry(eid)
        if old is None:
            self.entries.append(dict(fields, id=eid))
            return
        for k, v in fields.items():  # el primer respaldo / valor previo manda
            if k in ("backup", "prev", "prev_text", "created", "created_file",
                     "added") and k in old:
                continue
            old[k] = v

    def forget(self, eid):
        self.entries = [e for e in self.entries if e["id"] != eid]

    def save(self):
        path = self.p["manifest"]
        if not self.entries:
            if os.path.exists(path):
                os.remove(path)
            _prune_tree(self.p["backups"])
            _prune(path, self.p["home"])
            return
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            json.dump({"version": 1, "component": "kri", "repo": REPO,
                       "entries": self.entries}, f, indent=1)
            f.write("\n")


def _prune_tree(root):
    """Borra las carpetas vacías de un árbol (los respaldos ya devueltos)."""
    if not os.path.isdir(root):
        return
    for d, _subdirs, _files in os.walk(root, topdown=False):
        try:
            os.rmdir(d)
        except OSError:
            pass


class Act:
    """Una acción: dict para el plan + cómo aplicarla. apply() devuelve los
    campos a anotar en el manifiesto (o None = borrar la entrada)."""

    def __init__(self, component, kind, src, dst, detail, status, apply=None,
                 eid=None, **extra):
        self.d = {"component": component, "kind": kind, "src": src,
                  "dst": dst, "detail": detail, "status": status, **extra}
        self.apply = apply
        self.eid = eid or f"{kind}|{dst}"


# ----- acciones de instalación -----

def _placement(ctx, component, kind, src, dst, detail, same, place):
    """Deja algo en dst. `same`: ya está lo nuestro. Si hay algo que no
    instalamos nosotros (no está en el manifiesto), se respalda."""
    eid = f"{kind}|{dst}"
    exists = os.path.lexists(dst)
    status = "ok" if same else ("update" if exists else "create")
    extra = {}
    backup = None
    if status == "update" and ctx.entry(eid) is None:
        backup = extra["backup"] = ctx.backup_path(dst)
        detail += f"; lo que hay se respalda en {backup}"

    def apply():
        rec = {"component": component, "kind": kind, "src": src, "dst": dst}
        if os.path.lexists(dst):
            if backup:
                os.makedirs(os.path.dirname(backup), exist_ok=True)
                shutil.move(dst, backup)
                rec["backup"] = backup
            else:
                _remove(dst)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        place()
        return rec
    return Act(component, kind, src, dst, detail, status, apply, eid, **extra)


def _link_or_copy(ctx, component, kind, src, dst, detail):
    """symlink en Unix, copia en Windows (archivo o directorio)."""
    if ctx.p["win"]:
        same = _hash_tree(dst) is not None and _hash_tree(dst) == _hash_tree(src)
        return _placement(ctx, component, "copy" if kind == "symlink" else kind,
                          src, dst, detail, same, lambda: _copy(src, dst))
    return _placement(ctx, component, kind, src, dst, detail,
                      _links_to(dst, src), lambda: os.symlink(src, dst))


def _copy(src, dst):
    if os.path.isdir(src):
        shutil.copytree(src, dst, ignore=_ignore_pyc)
    else:
        shutil.copy2(src, dst)


LAUNCHER_MARK = "# kri launcher (krita-ai-cli cli/install.py)"


def _launcher_text(python, script):
    """Lanzador sh de kri: el intérprete va con ruta absoluta para no
    depender del PATH (Claude Code o el server MCP lanzados desde Finder o
    el escritorio ven un PATH mínimo, y `python3` puede ser otro python cuyo
    site-packages no es el del ext-venv)."""
    return (f"#!/bin/sh\n{LAUNCHER_MARK}\n"
            f"exec {shlex.quote(python)} {shlex.quote(script)} \"$@\"\n")


def _venv_version(venv):
    """'3.12' del pyvenv.cfg, o None."""
    for line in (_read(os.path.join(venv, "pyvenv.cfg")) or "").splitlines():
        k, _, v = line.partition("=")
        if k.strip() in ("version_info", "version") and v.strip():
            return ".".join(v.strip().split(".")[:2])
    return None


def _shim(ctx):
    p = ctx.p
    src = os.path.join(REPO, "cli", "kri")
    detail = "kri en el PATH"
    path_dirs = (ctx.env.get("PATH") or "").split(os.pathsep)
    if not p["win"] and p["bin_dir"] not in path_dirs:
        detail += f" ({p['bin_dir']} no está en el PATH: agregalo)"
    if not p["win"]:
        return [_launcher(ctx, src, detail)]
    content = f'@"{ctx.python}" "{src}" %*\r\n'

    def write():
        with open(p["shim"], "w", newline="") as f:
            f.write(content)
    return [_placement(ctx, "shim", "write", src, p["shim"],
                       detail + f"; agregá {p['bin_dir']} al PATH",
                       _read(p["shim"]) == content, write)]


def _launcher(ctx, src, detail):
    """~/.local/bin/kri como script sh que corre cli/kri con ctx.python.

    Migración desde el symlink de versiones anteriores: si el symlink lo
    instaló kri (entrada `symlink|<shim>` en el manifiesto) se reemplaza sin
    respaldo y la entrada nueva hereda su respaldo, si tenía; si no está en
    el manifiesto (symlink puesto a mano) se respalda como cualquier cosa
    ajena y el uninstall lo devuelve."""
    shim = ctx.p["shim"]
    content = _launcher_text(ctx.python, src)
    eid = f"write|{shim}"
    legacy = ctx.entry(f"symlink|{shim}")
    exists = os.path.lexists(shim)
    same = not os.path.islink(shim) and _read(shim) == content \
        and os.access(shim, os.X_OK)
    status = "ok" if same else ("update" if exists else "create")
    detail += f"; lanzador con {ctx.python}"
    have = _venv_version(ctx.p["ext_venv"])
    if have and ctx.python == sys.executable and \
            have != "%d.%d" % sys.version_info[:2]:
        detail += (f" (ojo: el ext-venv es de python {have} y este de "
                   f"{'%d.%d' % sys.version_info[:2]}; kri ext install se "
                   f"va a quejar)")
    extra = {}
    move_to = None  # respaldo nuevo (lo que hay no es nuestro)
    inherited = legacy.get("backup") if legacy is not None else None
    if status == "update":
        if os.path.islink(shim):
            detail += "; reemplaza el symlink de versiones anteriores"
        if legacy is None and ctx.entry(eid) is None:
            move_to = extra["backup"] = ctx.backup_path(shim)
            detail += f"; lo que hay se respalda en {move_to}"

    def apply():
        rec = {"component": "shim", "kind": "write", "src": src, "dst": shim}
        if os.path.lexists(shim):
            if move_to:
                os.makedirs(os.path.dirname(move_to), exist_ok=True)
                shutil.move(shim, move_to)
            else:
                _remove(shim)
        os.makedirs(os.path.dirname(shim), exist_ok=True)
        with open(shim, "w") as f:
            f.write(content)
        os.chmod(shim, 0o755)
        if move_to or inherited:
            rec["backup"] = move_to or inherited
        if legacy is not None:
            ctx.forget(legacy["id"])
        return rec
    return Act("shim", "write", src, shim, detail, status, apply, eid, **extra)


def _plugin(ctx):
    acts = []
    for name in ("kritamcp", "kritamcp.desktop"):
        src = os.path.join(REPO, "krita-plugin", name)
        dst = ctx.p["join"](ctx.p["pykrita"], name)
        same = _hash_tree(dst) is not None and _hash_tree(dst) == _hash_tree(src)
        acts.append(_placement(
            ctx, "plugin", "copy", src, dst,
            "plugin kritamcp a pykrita (reiniciar Krita para cargarlo)", same,
            lambda src=src, dst=dst: _copy(src, dst)))
    return acts


KRITARC_KEY = "enable_kritamcp"


def _kritarc_get(text):
    section = None
    for line in (text or "").splitlines():
        s = line.strip()
        if s.startswith("[") and s.endswith("]"):
            section = s
        elif section == "[python]" and s.split("=", 1)[0].strip() == KRITARC_KEY:
            return s.split("=", 1)[1].strip() if "=" in s else ""
    return None


def _kritarc_set(text, value):
    """kritarc con [python] enable_kritamcp=<value>; value None = sin la
    línea (y sin un [python] que quede vacío)."""
    lines = text.splitlines() if text else []
    out, in_python, found = [], False, False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            if in_python and value is not None and not found:
                out.append(f"{KRITARC_KEY}={value}")
                found = True
            in_python = stripped == "[python]"
        elif in_python and stripped.split("=", 1)[0].strip() == KRITARC_KEY:
            if value is not None and not found:
                out.append(f"{KRITARC_KEY}={value}")
                found = True
            continue
        out.append(line)
    if value is not None and not found:
        if in_python:
            out.append(f"{KRITARC_KEY}={value}")
        else:
            if out and out[-1].strip():
                out.append("")
            out += ["[python]", f"{KRITARC_KEY}={value}"]
    if value is None:
        cleaned = []
        for i, line in enumerate(out):
            if line.strip() == "[python]":
                nxt = next((x for x in out[i + 1:] if x.strip()), "")
                if not nxt or nxt.strip().startswith("["):
                    continue
            cleaned.append(line)
        out = cleaned
        while out and not out[-1].strip():
            out.pop()
    return "\n".join(out) + ("\n" if out else "")


def _write_text(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", newline="") as f:
        f.write(text)


def _kritarc(ctx):
    path = ctx.p["kritarc"]
    text = _read(path)
    prev = _kritarc_get(text)
    status = "ok" if prev == "true" else ("update" if text is not None else "create")
    detail = "[python] enable_kritamcp=true (con Krita cerrado)"
    if status == "update" and prev is not None:
        detail += f"; el valor previo ({prev}) queda en el manifiesto"

    def apply():
        _write_text(path, _kritarc_set(_read(path) or "", "true"))
        return {"component": "kritarc", "kind": "kritarc", "src": None,
                "dst": path, "prev": prev, "created_file": text is None}
    return [Act("kritarc", "kritarc", None, path, detail, status, apply)]


def _skills(ctx):
    root = os.path.join(REPO, "claude", "skills")
    acts = []
    for name in sorted(os.listdir(root)):
        src = os.path.join(root, name)
        if os.path.isdir(src):
            acts.append(_link_or_copy(ctx, "skills", "skill", src,
                                      ctx.p["join"](ctx.p["skills"], name),
                                      f"skill {name}"))
    return acts


def _hook(ctx):
    src = os.path.join(REPO, "claude", "hooks", HOOK_FILE)
    return [_link_or_copy(ctx, "hook", "symlink", src,
                          ctx.p["join"](ctx.p["hooks"], HOOK_FILE),
                          "hook de recordatorio de prompts")]


def _hook_command(p, python=None):
    """Comando del hook. En Unix con el intérprete absoluto (desde Finder o
    el escritorio el PATH es mínimo); en Windows, `python`."""
    hook = p["join"](p["hooks"], HOOK_FILE)
    if p["win"] or not python:
        return f'{"python" if p["win"] else "python3"} "{hook}"'
    return f'{shlex.quote(python)} "{hook}"'


def _is_our_hook(h):
    return isinstance(h, dict) and HOOK_FILE in str(h.get("command", ""))


def _settings_merge(data, p, add, items=None, python=None):
    """settings.json con (add) o sin (not add) los permisos y el hook de kri.
    Al sacar, `items` limita a lo que agregó el install ({"allow": [...],
    "ask": [...], "hook": bool}); None = todo lo de kri. Al agregar, un hook
    nuestro con otro comando (el `python3 …` de antes) se actualiza."""
    data = json.loads(json.dumps(data))  # copia
    items = items or {"allow": ALLOW, "ask": ASK, "hook": True}
    perms = data.setdefault("permissions", {})
    for key, values in (("allow", ALLOW), ("ask", ASK)):
        lst = perms.setdefault(key, [])
        for v in values:
            if add and v not in lst:
                lst.append(v)
            elif not add and v in lst and v in items.get(key, []):
                lst.remove(v)
        if not lst:
            perms.pop(key)
    if not perms:
        data.pop("permissions")
    hooks = data.setdefault("hooks", {})
    pre = hooks.setdefault("PreToolUse", [])
    has_ours = any(_is_our_hook(h) for e in pre if isinstance(e, dict)
                   for h in e.get("hooks", []))
    command = _hook_command(p, python)
    if add and not has_ours:
        pre.append({"matcher": "Bash", "hooks": [
            {"type": "command", "command": command, "timeout": 8}]})
    elif add:
        for h in _our_hooks(data):
            h["command"] = command
    elif not add and items.get("hook"):
        for entry in pre:
            if isinstance(entry, dict):
                entry["hooks"] = [h for h in entry.get("hooks", [])
                                  if not _is_our_hook(h)]
        pre[:] = [e for e in pre if not isinstance(e, dict) or e.get("hooks")]
    if not pre:
        hooks.pop("PreToolUse")
    if not hooks:
        data.pop("hooks")
    return data


def _our_hooks(data):
    """Los hooks de kri en un settings.json (los dicts, para poder editarlos)."""
    return [h for e in (data.get("hooks") or {}).get("PreToolUse") or []
            if isinstance(e, dict) for h in e.get("hooks", [])
            if _is_our_hook(h)]


def _settings_added(before, p):
    perms = before.get("permissions") or {}
    hooked = any(_is_our_hook(h)
                 for e in (before.get("hooks") or {}).get("PreToolUse") or []
                 if isinstance(e, dict) for h in e.get("hooks", []))
    return {"allow": [v for v in ALLOW if v not in (perms.get("allow") or [])],
            "ask": [v for v in ASK if v not in (perms.get("ask") or [])],
            "hook": not hooked}


def _settings(ctx):
    path = ctx.p["settings"]
    text = _read(path)
    try:
        data = json.loads(text) if text and text.strip() else {}
    except ValueError:
        return [Act("settings", "write", None, path,
                    "settings.json no es JSON válido: no lo toco", "skip")]
    new = _settings_merge(data, ctx.p, add=True, python=ctx.python)
    status = "ok" if new == data and text is not None else (
        "update" if text is not None else "create")
    detail = (f"permisos allow {ALLOW}, ask {ASK} y hook PreToolUse "
              f"({HOOK_FILE}); el uninstall saca sólo lo que agregó")
    want = _hook_command(ctx.p, ctx.python)
    if any(h.get("command") != want for h in _our_hooks(data)):
        detail += f"; el hook pasa a `{want}`"

    def apply():
        _write_text(path, json.dumps(new, indent=2) + "\n")
        return {"component": "settings", "kind": "write", "src": None,
                "dst": path, "prev_text": text,
                "added": _settings_added(data, ctx.p)}
    return [Act("settings", "write", None, path, detail, status, apply)]


def _make_venv(venv, python):
    r = subprocess.run([python, "-m", "venv", venv], capture_output=True, text=True)
    if r.returncode != 0:  # Debian sin python3-venv: sin ensurepip
        shutil.rmtree(venv, ignore_errors=True)
        r = subprocess.run([python, "-m", "venv", "--without-pip", venv],
                           capture_output=True, text=True)
    if r.returncode != 0:
        shutil.rmtree(venv, ignore_errors=True)
        raise RuntimeError(f"no pude crear {venv}: {(r.stdout + r.stderr)[-500:]}")


def _venv_act(ctx, component, venv, venv_py, detail):
    status = "ok" if os.path.exists(venv_py) else "create"
    if status == "create" and os.path.lexists(venv):
        status = "skip"
        detail = f"{venv} existe y no es un venv: no lo toco"

    def apply():
        _make_venv(venv, ctx.python)
        return {"component": component, "kind": "venv", "src": ctx.python,
                "dst": venv, "created": True}
    return Act(component, "venv", ctx.python, venv, detail, status, apply)


def _pip_install_argv(venv_py, requirements, python):
    """pip del venv, o uv, o el pip del sistema con --python (nunca en el
    python del sistema)."""
    probe = subprocess.run([venv_py, "-m", "pip", "--version"],
                           capture_output=True)
    if probe.returncode == 0:
        return [venv_py, "-m", "pip", "install", "-r", requirements]
    uv = shutil.which("uv")
    if uv:
        return [uv, "pip", "install", "--python", venv_py, "-r", requirements]
    return [python, "-m", "pip", "--python", venv_py, "install", "-r",
            requirements]


def _mcp(ctx):
    p = ctx.p
    acts = [_venv_act(ctx, "mcp", p["mcp_venv"], p["mcp_python"],
                      "venv del server MCP")]
    req = os.path.join(REPO, "mcp", "requirements.txt")
    marker = p["join"](p["mcp_venv"], ".kri-requirements.sha256")
    want = _hash_tree(req)
    status = "ok" if _read(marker) == want else "create"

    def pip():
        r = subprocess.run(_pip_install_argv(p["mcp_python"], req, ctx.python),
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError((r.stdout + r.stderr)[-1500:])
        with open(marker, "w") as f:
            f.write(want)
        return None  # vive dentro del venv: se va con él
    acts.append(Act("mcp", "pip", req, p["mcp_venv"], "fastmcp y httpx",
                    status, pip))
    acts.append(_link_or_copy(ctx, "mcp", "symlink",
                              os.path.join(REPO, "mcp", "server.py"),
                              p["mcp_server"], "server MCP versionado"))
    acts.append(_mcp_register(ctx))
    return acts


def _claude_env(ctx):
    e = dict(os.environ)
    e.update({k: v for k, v in ctx.env.items() if isinstance(v, str)})
    e["HOME"] = ctx.p["home"]
    return e


def _claude_bin(ctx):
    return shutil.which("claude", path=ctx.env.get("PATH") or os.environ.get("PATH"))


def _mcp_current(ctx):
    """Config actual del server `krita` (scope user) en ~/.claude.json."""
    data = _load_json(ctx.p["claude_json"]) or {}
    return (data.get("mcpServers") or {}).get(MCP_NAME)


def _mcp_config(ctx):
    return {"type": "stdio", "command": ctx.p["mcp_python"],
            "args": [ctx.p["mcp_server"]], "env": {"KRI_BIN": ctx.p["shim"]}}


def _run_claude(ctx, claude, *args):
    r = subprocess.run([claude, *args], capture_output=True, text=True,
                       env=_claude_env(ctx), timeout=60)
    if r.returncode != 0:
        raise RuntimeError(f"claude {' '.join(args[:3])}: "
                           f"{(r.stdout + r.stderr)[-800:]}")


def _mcp_register(ctx):
    eid = f"mcp|user|{MCP_NAME}"
    cfg = _mcp_config(ctx)
    claude = _claude_bin(ctx)
    current = _mcp_current(ctx)
    add = ["mcp", "add-json", MCP_NAME, json.dumps(cfg), "--scope", "user"]
    detail = "claude " + " ".join(add[:3]) + " … --scope user"
    extra = {}
    foreign = current is not None and current != cfg and ctx.entry(eid) is None
    if claude is None:
        status = "skip"
        detail = "no está el CLI `claude`: corré a mano `claude " + \
            " ".join(add[:3]) + " '" + add[3] + "' --scope user`"
    else:
        status = "ok" if current == cfg else (
            "update" if current is not None else "create")
    if foreign and status != "ok":
        extra["backup"] = {"mcpServers": {MCP_NAME: current}}
        detail += (f"; respalda el server `{MCP_NAME}` actual "
                   f"({current.get('command')} {' '.join(current.get('args') or [])})"
                   f" en el manifiesto")

    def apply():
        if current is not None:
            _run_claude(ctx, claude, "mcp", "remove", MCP_NAME, "--scope", "user")
        _run_claude(ctx, claude, *add)
        rec = {"component": "mcp", "kind": "mcp", "src": ctx.p["mcp_server"],
               "dst": f"claude:user:{MCP_NAME}", "added": True}
        if foreign:
            rec["prev"] = current
        return rec
    return Act("mcp", "mcp", ctx.p["mcp_server"], f"claude:user:{MCP_NAME}",
               detail, status, apply, eid, **extra)


# ----- desinstalación (desde el manifiesto) -----

def _undo(ctx, e):
    kind, dst = e["kind"], e["dst"]
    p = ctx.p
    restore = e.get("backup")
    extra = {"restore": restore} if restore else {}

    if kind == "kritarc":
        text = _read(dst)
        prev = e.get("prev")

        def apply():
            new = _kritarc_set(_read(dst) or "", prev)
            if e.get("created_file") and not new.strip():
                os.remove(dst)
                _prune(dst, p["home"])
            else:
                _write_text(dst, new)
        detail = ("saca enable_kritamcp" if prev is None
                  else f"vuelve enable_kritamcp={prev}")
        return Act("kritarc", kind, None, dst, detail,
                   "remove" if text is not None else "absent", apply, e["id"])

    if e.get("component") == "settings":
        def apply():
            cur = _load_json(dst)
            if cur is None:
                return
            new = _settings_merge(cur, p, add=False, items=e.get("added"))
            prev_text = e.get("prev_text")
            # un hook de kri que ya estaba (no lo agregamos) vuelve a su
            # comando de antes (el `python3 …` previo al lanzador)
            try:
                prev = json.loads(prev_text) if prev_text else {}
            except ValueError:
                prev = {}
            old = [h.get("command") for h in _our_hooks(prev)] \
                if isinstance(prev, dict) else []
            if old:
                for h in _our_hooks(new):
                    h["command"] = old[0]
            if prev_text is not None and _same_json(prev_text, new):
                _write_text(dst, prev_text)  # tal cual estaba, con su formato
            elif prev_text is None and not new:
                os.remove(dst)
                _prune(dst, p["home"])
            else:
                _write_text(dst, json.dumps(new, indent=2) + "\n")
        return Act("settings", kind, None, dst,
                   "saca los permisos y el hook que agregó el install",
                   "remove" if os.path.exists(dst) else "absent", apply, e["id"])

    if kind == "mcp":
        claude = _claude_bin(ctx)
        current = _mcp_current(ctx)
        prev = e.get("prev")
        ours = current == _mcp_config(ctx)
        if claude is None:
            return Act("mcp", kind, None, dst, "no está el CLI `claude`",
                       "skip", None, e["id"])
        if current is not None and not ours:
            return Act("mcp", kind, None, dst,
                       f"el server `{MCP_NAME}` registrado ya no es el nuestro: "
                       f"no lo toco", "skip", None, e["id"], **extra)

        def apply():
            if current is not None:
                _run_claude(ctx, claude, "mcp", "remove", MCP_NAME,
                            "--scope", "user")
            if prev:
                _run_claude(ctx, claude, "mcp", "add-json", MCP_NAME,
                            json.dumps(prev), "--scope", "user")
        detail = "claude mcp remove " + MCP_NAME
        if prev:
            extra["restore"] = {"mcpServers": {MCP_NAME: prev}}
            detail += f" y vuelve el anterior ({prev.get('command')})"
        return Act("mcp", kind, None, dst, detail,
                   "remove" if current is not None or prev else "absent",
                   apply, e["id"], **extra)

    if kind == "venv":
        def apply():
            if e.get("created") and os.path.isdir(dst):
                shutil.rmtree(dst)
                _prune(dst, p["home"])
        return Act(e["component"], kind, e.get("src"), dst, "borra el venv",
                   "remove" if os.path.isdir(dst) and e.get("created") else "absent",
                   apply, e["id"])

    # colocaciones: symlink / skill / copy / write
    exists = os.path.lexists(dst)
    status = "remove" if exists or restore else "absent"
    detail = "saca lo instalado" + (f" y devuelve {restore}" if restore else "")
    if exists and kind in ("symlink", "skill") and not p["win"] \
            and not _links_to(dst, e.get("src") or ""):
        return Act(e["component"], kind, e.get("src"), dst,
                   f"{dst} ya no apunta a {e.get('src')}: no lo toco", "skip",
                   None, e["id"], **extra)

    def apply():
        if os.path.lexists(dst):
            _remove(dst)
        if restore and os.path.lexists(restore):
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.move(restore, dst)
            _prune(restore, p["home"])
        else:
            _prune(dst, p["home"])
    return Act(e["component"], kind, e.get("src"), dst, detail, status, apply,
               e["id"], **extra)


def _same_json(text, data):
    try:
        return json.loads(text) == data
    except ValueError:
        return False


# ----- contrato -----

def _skipped(env):
    raw = env.get("KRI_INSTALL_SKIP", "")
    return {s.strip() for s in raw.split(",") if s.strip()}


def plan(uninstall=False, env=None, ctx=None):
    env = dict(os.environ if env is None else env)
    ctx = ctx or Ctx(env)
    skip = _skipped(env)
    if uninstall:
        return [_undo(ctx, e) for e in reversed(ctx.entries)
                if e.get("component") not in skip]
    builders = [("shim", _shim), ("plugin", _plugin), ("skills", _skills),
                ("hook", _hook),
                ("extvenv", lambda c: [_venv_act(
                    c, "extvenv", c.p["ext_venv"], c.p["ext_venv_python"],
                    "venv de extensiones (kri ext install)")]),
                ("mcp", _mcp), ("kritarc", _kritarc), ("settings", _settings)]
    acts = []
    for name, build in builders:
        if name not in skip:
            acts += build(ctx)
    return acts


def install_component(plan_only=True, uninstall=False, env=None):
    """Contrato §8. Devuelve las acciones; si plan_only=False, además las
    ejecuta (sólo las que cambian algo), anota done/error en cada una y
    actualiza el manifiesto (<data>/kri/install-manifest.json)."""
    env = dict(os.environ if env is None else env)
    ctx = Ctx(env)
    acts = plan(uninstall, env, ctx)
    result = []
    for act in acts:
        d = dict(act.d)
        if not plan_only:
            d["done"], d["error"] = False, None
            if d["status"] in ("create", "update", "remove") and act.apply:
                try:
                    rec = act.apply()
                    d["done"] = True
                    if uninstall:
                        ctx.forget(act.eid)
                    elif rec:
                        ctx.record(act.eid, rec)
                except Exception as e:  # seguir con el resto, informar
                    d["error"] = f"{type(e).__name__}: {e}"
            elif uninstall and d["status"] == "absent":
                ctx.forget(act.eid)
        result.append(d)
    if not plan_only:
        ctx.save()
    return result


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="Instala/desinstala kri "
                                             "(contrato de componente)")
    ap.add_argument("--plan-json", action="store_true",
                    help="sólo mostrar el plan (JSON), sin tocar nada")
    ap.add_argument("--uninstall", action="store_true")
    ap.add_argument("--skip", default="",
                    help=f"componentes a saltear, coma: {','.join(COMPONENTS)}")
    a = ap.parse_args(argv)
    env = dict(os.environ)
    if a.skip:
        env["KRI_INSTALL_SKIP"] = ",".join(filter(None, [env.get(
            "KRI_INSTALL_SKIP", ""), a.skip]))
    actions = install_component(plan_only=a.plan_json, uninstall=a.uninstall,
                                env=env)
    print(json.dumps(actions, indent=1))
    return 1 if any(x.get("error") for x in actions) else 0


if __name__ == "__main__":
    sys.exit(main())
