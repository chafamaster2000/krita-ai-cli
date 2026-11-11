"""Sistema de extensiones de kri (contrato: README, sección "Extensions").

Lo importa cli/kri sólo para `kri ext …`, para `kri --help` y para los
subcomandos que no son nativos: los comandos nativos nunca cargan este
módulo. Una extensión es un paquete pip con un entry point en el grupo
`kri.extensions` (nombre = subcomando) y un `kri-extension.toml` en la raíz
del paquete. Se instala en el venv propio de kri, `<data>/kri/ext-venv`
(el python del sistema es PEP 668: nada de --break-system-packages).

`K` es el módulo kri que nos importó (send, out, http_request, …); lo setea
kri.extensions() antes de usar nada de acá.
"""
import argparse
import json
import os
import sys

K = None  # el módulo kri (cli/kri), lo inyecta kri.extensions()

EXT_GROUP = "kri.extensions"
EXT_MANIFEST = "kri-extension.toml"
RESERVED_NAMES = {"ext", "help"}


def kri_dirs(environ=None, platform=None):
    """(<data>/kri, <config>/kri) según la plataforma (§4.3 del plan):
    Linux XDG_DATA_HOME|~/.local/share y XDG_CONFIG_HOME|~/.config; macOS
    ~/Library/Application Support; Windows %LOCALAPPDATA%."""
    environ = os.environ if environ is None else environ
    platform = sys.platform if platform is None else platform
    home = (environ.get("HOME") or environ.get("USERPROFILE")
            or os.path.expanduser("~"))
    if platform.startswith("win"):
        base = environ.get("LOCALAPPDATA") or os.path.join(
            home, "AppData", "Local")
        return os.path.join(base, "kri"), os.path.join(base, "kri")
    if platform == "darwin":
        base = os.path.join(home, "Library", "Application Support", "kri")
        return base, base
    data = environ.get("XDG_DATA_HOME") or os.path.join(home, ".local", "share")
    conf = environ.get("XDG_CONFIG_HOME") or os.path.join(home, ".config")
    return os.path.join(data, "kri"), os.path.join(conf, "kri")


def ext_venv_dir():
    return os.path.join(kri_dirs()[0], "ext-venv")


def venv_python(venv, platform=None):
    platform = sys.platform if platform is None else platform
    if platform.startswith("win"):
        return os.path.join(venv, "Scripts", "python.exe")
    return os.path.join(venv, "bin", "python")


def venv_site(venv, platform=None):
    """site-packages del venv para ESTE intérprete (kri y el venv comparten
    python: el venv se crea con sys.executable)."""
    platform = sys.platform if platform is None else platform
    if platform.startswith("win"):
        return os.path.join(venv, "Lib", "site-packages")
    v = sys.version_info
    return os.path.join(venv, "lib", f"python{v.major}.{v.minor}",
                        "site-packages")


def _venv_version(venv):
    """version_info del pyvenv.cfg ('3.14.4'), o None."""
    try:
        with open(os.path.join(venv, "pyvenv.cfg")) as f:
            for line in f:
                k, _, v = line.partition("=")
                if k.strip() in ("version_info", "version"):
                    return v.strip()
    except OSError:
        pass
    return None


def activate_ext_venv():
    """Suma el site-packages del ext-venv al sys.path (al final, con sus .pth:
    así andan los `pip install -e`). Devuelve la ruta o None si no existe."""
    site_dir = venv_site(ext_venv_dir())
    if not os.path.isdir(site_dir):
        return None
    import site
    site.addsitedir(site_dir)
    return site_dir


def ext_config_path():
    return os.path.join(kri_dirs()[1], "extensions.toml")


def disabled_extensions():
    try:
        import tomllib
        with open(ext_config_path(), "rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        return []
    except Exception as e:  # config rota: avisar pero no frenar a kri
        print(f"kri: ignoring broken {ext_config_path()}: {e}", file=sys.stderr)
        return []
    names = (data.get("disabled") or {}).get("names") or []
    return [n for n in names if isinstance(n, str)]


def write_disabled(names):
    path = ext_config_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    items = ", ".join(json.dumps(n) for n in sorted(set(names)))
    with open(path, "w") as f:
        f.write("# Lo administra kri: `kri ext enable|disable NOMBRE`.\n"
                f"[disabled]\nnames = [{items}]\n")


def vtuple(v):
    """'0.5.0rc1' -> (0, 5, 0). Sólo la parte numérica inicial de cada pieza."""
    parts = []
    for piece in str(v).strip().split("."):
        digits = ""
        for ch in piece:
            if not ch.isdigit():
                break
            digits += ch
        if not digits:
            break
        parts.append(int(digits))
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts)


def spec_ok(version, spec):
    """¿`version` (tupla) cumple `spec` ('>=3.11,<4')? (ok, motivo)."""
    ops = (">=", "<=", "==", "!=", "~=", ">", "<")
    for clause in str(spec).split(","):
        clause = clause.strip()
        if not clause:
            continue
        op = next((o for o in ops if clause.startswith(o)), None)
        if op is None:
            return False, f"no entiendo la condición {clause!r}"
        want = vtuple(clause[len(op):])
        have = tuple(version)[:3]
        if op == "~=":
            op, ok = ">=", have >= want
        else:
            ok = {">=": have >= want, "<=": have <= want, "==": have == want,
                  "!=": have != want, ">": have > want, "<": have < want}[op]
        if not ok:
            return False, f"necesita {clause} y hay {'.'.join(map(str, have))}"
    return True, None


class ExtError(Exception):
    pass


def discover_extensions():
    """{nombre: entry point}, sin importar el código de ninguna extensión.
    Mira el ext-venv y lo que ya esté en sys.path."""
    activate_ext_venv()
    from importlib.metadata import entry_points
    found = {}
    for ep in entry_points(group=EXT_GROUP):
        found.setdefault(ep.name, ep)
    return found


def read_manifest(ep):
    """(manifiesto dict, ruta). Busca kri-extension.toml en la raíz del
    paquete del entry point (find_spec no ejecuta el paquete) o entre los
    archivos de la distribución. Lanza ExtError."""
    import importlib.util
    import tomllib
    top = ep.value.split(":")[0].strip().split(".")[0]
    candidates = []
    try:
        spec = importlib.util.find_spec(top)
    except Exception:
        spec = None
    for loc in (spec.submodule_search_locations or []) if spec else []:
        candidates.append(os.path.join(loc, EXT_MANIFEST))
    dist = getattr(ep, "dist", None)
    for f in (dist.files or []) if dist else []:
        if f.name == EXT_MANIFEST:
            candidates.append(str(dist.locate_file(f)))
    for path in candidates:
        if os.path.isfile(path):
            try:
                with open(path, "rb") as fh:
                    return tomllib.load(fh), path
            except Exception as e:
                raise ExtError(f"{EXT_MANIFEST} inválido ({path}): {e}")
    raise ExtError(f"falta {EXT_MANIFEST} en el paquete {top!r} "
                   f"(tiene que ir como package data)")


PARAM_TYPES = {"string", "integer", "number", "boolean", "array"}


def manifest_problems(name, ep, manifest):
    """Lista de problemas del manifiesto (vacía = sirve)."""
    problems = []
    ext = manifest.get("extension")
    if not isinstance(ext, dict):
        return ["falta la tabla [extension]"]
    if ext.get("name") != name:
        problems.append(f"[extension] name = {ext.get('name')!r} pero el entry "
                        f"point se llama {name!r}")
    if not ext.get("version"):
        problems.append("falta [extension] version")
    declared = str(ext.get("entry_point") or "").replace(" ", "")
    if declared and declared != ep.value.replace(" ", ""):
        problems.append(f"entry_point {declared!r} no coincide con el "
                        f"instalado {ep.value!r}")
    if name in K.NATIVE_COMMANDS or name in RESERVED_NAMES:
        problems.append(f"{name!r} choca con un comando nativo de kri")
    min_kri = ext.get("min_kri")
    if min_kri and vtuple(min_kri) > vtuple(K.__version__):
        problems.append(f"pide kri >= {min_kri} y este kri es {K.__version__}")
    py = (manifest.get("dependencies") or {}).get("python")
    if py:
        ok, why = spec_ok(sys.version_info[:3], py)
        if not ok:
            problems.append(f"python: {why}")
    for i, cmd in enumerate(manifest.get("commands") or []):
        if not isinstance(cmd, dict) or not cmd.get("name"):
            problems.append(f"[[commands]] #{i} sin name")
    for tool in manifest.get("mcp_tools") or []:
        problems += [f"mcp_tools {tool.get('name', '?')}: {p}"
                     for p in mcp_tool_problems(tool)]
    return problems


def mcp_tool_problems(tool):
    problems = []
    if not isinstance(tool, dict):
        return ["no es una tabla"]
    if not tool.get("name"):
        problems.append("falta name")
    if not tool.get("description"):
        problems.append("falta description")
    cmd = tool.get("command")
    if not (isinstance(cmd, list) and cmd and all(isinstance(c, str) for c in cmd)):
        problems.append("command tiene que ser una lista de strings no vacía")
    params = tool.get("params") or {}
    if not isinstance(params, dict):
        return problems + ["params tiene que ser una tabla"]
    for pname, spec in params.items():
        if not isinstance(spec, dict) or spec.get("type") not in PARAM_TYPES:
            problems.append(f"param {pname}: type tiene que ser uno de "
                            f"{sorted(PARAM_TYPES)}")
    for pname in tool.get("positional") or []:
        if pname not in params:
            problems.append(f"positional {pname!r} no está en params")
    return problems


def tool_schema(tool):
    """JSON schema de entrada de una mcp_tool del manifiesto."""
    props, required = {}, []
    for pname, spec in (tool.get("params") or {}).items():
        prop = {k: v for k, v in spec.items()
                if k in ("type", "enum", "description", "default", "items",
                         "minimum", "maximum")}
        if prop.get("type") == "array" and "items" not in prop:
            prop["items"] = {"type": "string"}
        props[pname] = prop
        if spec.get("required"):
            required.append(pname)
    return {"type": "object", "properties": props, "required": required,
            "additionalProperties": False}


def tool_argv(ext_name, tool, arguments):
    """argv de `kri` para llamar a una mcp_tool: `EXT CMD… POSICIONALES
    --flag valor…`. boolean true = `--flag` solo (false se omite), array =
    `--flag v1 v2`, `_` del nombre pasa a `-`. El server MCP aplica la misma
    regla (mcp/server.py, ext_tool_argv)."""
    argv = [ext_name, *tool["command"]]
    params = tool.get("params") or {}
    positional = tool.get("positional") or []
    for pname in positional:
        value = arguments.get(pname)
        if value is None:
            continue
        argv += [str(v) for v in value] if isinstance(value, list) else [str(value)]
    for pname in list(params) + [k for k in arguments if k not in params]:
        value = arguments.get(pname)
        if pname in positional or value is None or value is False:
            continue
        flag = "--" + pname.replace("_", "-")
        if value is True:
            argv.append(flag)
        elif isinstance(value, list):
            argv += [flag, *map(str, value)]
        else:
            argv += [flag, str(value)]
    return argv


class ExtContext:
    """Lo único que una extensión ve de kri (las extensiones no importan kri).
    Contrato en el README, sección "Extensiones"."""

    def __init__(self, name, parser, manifest):
        self.name = name
        self.version = K.__version__
        self.parser = parser
        self.manifest = manifest
        self.json = not K.HUMAN
        self.krita_url = K.KRITA_URL
        self.autoselect_url = K.AUTOSELECT_URL
        data, config = kri_dirs()
        self.data_dir, self.config_dir = data, config

    def send(self, action, params=None, timeout=30.0):
        return K.send(action, params, timeout)

    def http_request(self, method, path, body=None, timeout=30.0, url=None,
                     hint=None):
        return K.http_request(method, path, body, timeout, url, hint)

    def sam_ensure(self, timeout=None):
        url, err = K.sam_ensure(timeout or K.SAM_START_TIMEOUT)
        if url:
            K.AUTOSELECT_URL = self.autoselect_url = url
        return url, err

    def autoselect_require(self, capability="segment_many", start=True):
        """URL de un daemon de autoselect con `capability`; si no hay, termina
        con un error que explica cómo arreglarlo (mensaje de degradación)."""
        health = K.daemon_health(self.autoselect_url)
        if not health.get("reachable") and start:
            url, err = self.sam_ensure()
            if not url:
                self.out({"error": f"el daemon de autoselect no responde en "
                                   f"{self.autoselect_url} y AI Select no lo "
                                   f"pudo arrancar: {err}",
                          "hint": "kri sam start, o AUTOSELECT_URL=…"})
            health = K.daemon_health(url)
        if not health.get("reachable"):
            self.out({"error": f"el daemon de autoselect no responde en "
                               f"{self.autoselect_url}", "hint": "kri sam start"})
        gap = K.capability_gap(health, capability)
        if gap:
            self.out({"error": gap, "daemon": health})
        return health["url"]

    def out(self, result):
        K.out(result)


def _broken(name, exc, what="rota"):
    res = {"error": f"extensión {name} {what}: {type(exc).__name__}: {exc}",
           "hint": "kri ext list"}
    if os.environ.get("KRI_DEBUG"):
        import traceback
        res["traceback"] = traceback.format_exc()
    return res


def load_extension(name, ep):
    """Importa y registra una extensión en un parser propio. (parser, ctx).
    Lanza ExtError con un motivo legible."""
    manifest, _ = read_manifest(ep)
    problems = manifest_problems(name, ep, manifest)
    if problems:
        raise ExtError("; ".join(problems))
    ext = manifest["extension"]
    parser = argparse.ArgumentParser(
        prog=f"kri {name}",
        description=f"{ext.get('description', '')} (extensión {name} "
                    f"{ext.get('version')})")
    ctx = ExtContext(name, parser, manifest)
    try:
        register = ep.load()
        register(ctx)
    except Exception as e:
        raise ExtError(f"{type(e).__name__}: {e}") from e
    return parser, ctx


def run_extension(name, rest):
    exts = discover_extensions()
    ep = exts.get(name)
    if ep is None:
        print(f"kri: comando desconocido {name!r}: no es nativo ni una "
              f"extensión instalada (kri --help, kri ext list)", file=sys.stderr)
        sys.exit(2)
    if name in disabled_extensions():
        K.out({"error": f"la extensión {name} está deshabilitada",
             "hint": f"kri ext enable {name}"})
    try:
        parser, ctx = load_extension(name, ep)
    except ExtError as e:
        K.out(_broken(name, e))
    args = parser.parse_args(rest)
    fn = getattr(args, "fn", None)
    if fn is None:
        parser.print_usage(sys.stderr)
        print(f"kri {name}: falta el subcomando", file=sys.stderr)
        sys.exit(2)
    try:
        result = fn(args, ctx)
    except (SystemExit, KeyboardInterrupt):
        raise
    except Exception as e:
        K.out(_broken(name, e, "falló"))
    if isinstance(result, dict):
        K.out(result)
    sys.exit(0)


def ext_row(name, ep, enabled, try_load=True):
    """Estado de una extensión para `kri ext list`."""
    dist = getattr(ep, "dist", None)
    row = {"name": name, "version": None, "description": None,
           "enabled": enabled, "ok": False, "reason": None,
           "entry_point": ep.value,
           "dist": dist.metadata["Name"] if dist else None,
           "source": _dist_source(dist)}
    try:
        manifest, path = read_manifest(ep)
    except ExtError as e:
        row["reason"] = str(e)
        return row
    ext = manifest.get("extension") or {}
    row.update(version=ext.get("version"), description=ext.get("description"),
               manifest=path,
               commands=[c.get("name") for c in manifest.get("commands") or []
                         if isinstance(c, dict)],
               mcp_tools=[t.get("name") for t in manifest.get("mcp_tools") or []
                          if isinstance(t, dict)])
    try:
        if enabled and try_load:
            load_extension(name, ep)
        else:
            problems = manifest_problems(name, ep, manifest)
            if problems:
                raise ExtError("; ".join(problems))
        row["ok"] = True
    except ExtError as e:
        row["reason"] = str(e)
    return row


def _dist_source(dist):
    """{'url': 'file:///…', 'editable': bool} de direct_url.json, si hay."""
    if dist is None:
        return None
    try:
        raw = dist.read_text("direct_url.json")
    except Exception:
        raw = None
    if not raw:
        return None
    try:
        info = json.loads(raw)
    except ValueError:
        return None
    return {"url": info.get("url"),
            "editable": bool((info.get("dir_info") or {}).get("editable"))}


def _norm_dist(name):
    import re
    return re.sub(r"[-_.]+", "-", name).lower()


def _invalidate_metadata():
    import importlib
    importlib.invalidate_caches()


def pip_backend(venv):
    """('venv'|'uv'|'pip', argv base). Orden: el pip del propio ext-venv, uv,
    o el pip del sistema apuntando al venv con --python (nunca instala en el
    python del sistema: PEP 668). KRI_PIP fuerza uno."""
    import importlib.util
    import shutil
    py = venv_python(venv)
    forced = os.environ.get("KRI_PIP")
    has_venv_pip = os.path.isdir(os.path.join(venv_site(venv), "pip"))
    if forced == "venv" or (not forced and has_venv_pip):
        return "venv", [py, "-m", "pip"]
    uv = shutil.which("uv")
    if forced == "uv" or (not forced and uv):
        return "uv", [uv or "uv", "pip"]
    if forced == "pip" or importlib.util.find_spec("pip"):
        return "pip", [sys.executable, "-m", "pip", "--python", py]
    raise ExtError("no hay con qué instalar: ni pip en el ext-venv, ni uv, "
                   "ni pip en el python del sistema")


def run_pip(venv, op, args, force=False):
    import subprocess
    kind, base = pip_backend(venv)
    argv = list(base) + [op]
    if kind == "uv":
        argv += ["--python", venv_python(venv)]
    elif op == "uninstall":
        argv.append("-y")
    if kind != "uv":
        argv.append("--disable-pip-version-check")
    if force:
        argv.append("--reinstall" if kind == "uv" else "--force-reinstall")
    argv += list(args)
    r = subprocess.run(argv, capture_output=True, text=True)
    return kind, r.returncode, (r.stdout + r.stderr).strip()


def ensure_ext_venv():
    """Crea el ext-venv con ESTE intérprete si falta. (venv, creado?)."""
    import shutil
    import subprocess
    venv = ext_venv_dir()
    if os.path.exists(venv_python(venv)):
        have = _venv_version(venv)
        mine = "%d.%d" % sys.version_info[:2]
        if have and not have.startswith(mine + ".") and have != mine:
            raise ExtError(f"el ext-venv ({venv}) es de python {have} y kri "
                           f"corre con {mine}: borralo y reinstalá las "
                           f"extensiones")
        return venv, False
    os.makedirs(os.path.dirname(venv), exist_ok=True)
    r = subprocess.run([sys.executable, "-m", "venv", venv],
                       capture_output=True, text=True)
    if r.returncode != 0:  # p. ej. Debian sin python3-venv: sin ensurepip
        shutil.rmtree(venv, ignore_errors=True)
        r = subprocess.run([sys.executable, "-m", "venv", "--without-pip", venv],
                           capture_output=True, text=True)
    if r.returncode != 0:
        shutil.rmtree(venv, ignore_errors=True)
        raise ExtError(f"no pude crear el ext-venv en {venv}: "
                       f"{(r.stdout + r.stderr).strip()[-500:]}")
    return venv, True


def _installed_by(target, is_path, exts):
    """Nombres de extensión que vienen de `target` (ruta o requisito pip)."""
    names = []
    if is_path:
        want = os.path.realpath(target)
    else:
        import re
        want = _norm_dist(re.split(r"[\s\[<>=!~;@]", target.strip(), 1)[0])
    for name, ep in exts.items():
        dist = getattr(ep, "dist", None)
        if dist is None:
            continue
        if is_path:
            src = _dist_source(dist) or {}
            url = src.get("url") or ""
            if url.startswith("file://"):
                from urllib.parse import unquote
                if os.path.realpath(unquote(url[len("file://"):])) == want:
                    names.append(name)
        elif _norm_dist(dist.metadata["Name"]) == want:
            names.append(name)
    return names


def _ext_fingerprint(exts):
    fp = {}
    for name, ep in exts.items():
        dist = getattr(ep, "dist", None)
        fp[name] = (dist.metadata["Name"], dist.version,
                    str(dist._path) if hasattr(dist, "_path") else None) \
            if dist else None
    return fp


def cmd_ext_list(a):
    exts = discover_extensions()
    disabled = set(disabled_extensions())
    venv = ext_venv_dir()
    K.out({"status": "ok", "kri": K.__version__,
         "ext_venv": {"path": venv, "exists": os.path.exists(venv_python(venv))},
         "config": ext_config_path(),
         "extensions": [ext_row(n, exts[n], n not in disabled)
                        for n in sorted(exts)]})


def cmd_ext_install(a):
    target = a.target
    is_path = os.path.exists(os.path.expanduser(target))
    if is_path:
        target = os.path.abspath(os.path.expanduser(target))
    try:
        venv, created = ensure_ext_venv()
        before = _ext_fingerprint(discover_extensions())
        args = (["-e"] if a.editable else []) + [target]
        kind, rc, log = run_pip(venv, "install", args, force=a.force)
    except ExtError as e:
        K.out({"error": str(e)})
    if rc != 0:
        K.out({"error": f"falló la instalación de {target}", "pip": kind,
             "log": log[-2000:]})
    _invalidate_metadata()
    exts = discover_extensions()
    after = _ext_fingerprint(exts)
    names = set(_installed_by(target, is_path, exts))
    names |= {n for n in after if before.get(n) != after[n]}
    if not names:
        K.out({"error": f"{target} se instaló pero no declara ningún entry point "
                      f"en el grupo {EXT_GROUP!r}",
             "hint": "en pyproject.toml: [project.entry-points.\"kri.extensions\"] "
                     "NOMBRE = \"paquete.kri_ext:register\"; para sacarlo: "
                     "kri ext remove --dist DIST",
             "pip": kind})
    rows, failed = [], []
    for name in sorted(names):
        row = ext_row(name, exts[name], enabled=True)
        rows.append(row)
        if not row["ok"]:
            failed.append(row)
    if failed:
        removed = []
        for row in failed:  # rollback: no dejar instalada una extensión rota
            if row.get("dist"):
                run_pip(venv, "uninstall", [row["dist"]])
                removed.append(row["dist"])
        K.out({"error": "extensión inválida, no se instaló: " + "; ".join(
                f"{r['name']}: {r['reason']}" for r in failed),
             "rolled_back": removed, "extensions": rows})
    disabled = set(disabled_extensions())
    if disabled & names:
        write_disabled(disabled - names)
    K.out({"status": "ok", "installed": rows, "ext_venv": venv,
         "created_venv": created, "pip": kind})


def _require_ext(name):
    exts = discover_extensions()
    if name not in exts:
        K.out({"error": f"no hay ninguna extensión {name!r} instalada",
             "hint": "kri ext list"})
    return exts[name]


def cmd_ext_enable(a):
    _require_ext(a.name)
    write_disabled(set(disabled_extensions()) - {a.name})
    K.out({"status": "ok", "name": a.name, "enabled": True})


def cmd_ext_disable(a):
    _require_ext(a.name)
    write_disabled(set(disabled_extensions()) | {a.name})
    K.out({"status": "ok", "name": a.name, "enabled": False})


def cmd_ext_remove(a):
    if a.dist:
        dist = a.name
    else:
        ep = _require_ext(a.name)
        d = getattr(ep, "dist", None)
        if d is None:
            K.out({"error": f"no sé de qué distribución viene {a.name}"})
        dist = d.metadata["Name"]
    venv = ext_venv_dir()
    if not os.path.exists(venv_python(venv)):
        K.out({"error": f"no hay ext-venv en {venv}"})
    try:
        kind, rc, log = run_pip(venv, "uninstall", [dist])
    except ExtError as e:
        K.out({"error": str(e)})
    if rc != 0:
        K.out({"error": f"no pude desinstalar {dist}", "log": log[-2000:]})
    if a.name in disabled_extensions():
        write_disabled(set(disabled_extensions()) - {a.name})
    K.out({"status": "ok", "removed": dist, "pip": kind})


def cmd_ext_mcp_tools(a):
    exts = discover_extensions()
    disabled = set(disabled_extensions())
    tools, skipped, seen = [], [], {}
    for name in sorted(exts):
        if name in disabled:
            continue
        ep = exts[name]
        try:  # sin importar la extensión: alcanza con el manifiesto
            manifest, _ = read_manifest(ep)
            problems = manifest_problems(name, ep, manifest)
            if problems:
                raise ExtError("; ".join(problems))
        except ExtError as e:
            skipped.append({"extension": name, "reason": str(e)})
            continue
        for tool in manifest.get("mcp_tools") or []:
            tname = tool["name"]
            if tname in seen:
                skipped.append({"extension": name, "tool": tname,
                                "reason": f"nombre repetido (ya lo usa {seen[tname]})"})
                continue
            seen[tname] = name
            example = {p: (True if s.get("type") == "boolean" else f"<{p}>")
                       for p, s in (tool.get("params") or {}).items()}
            tools.append({
                "extension": name, "name": tname,
                "description": tool["description"],
                "command": tool["command"],
                "params": tool.get("params") or {},
                "positional": tool.get("positional") or [],
                "timeout_s": tool.get("timeout_s", 600),
                "annotations": tool.get("annotations") or {},
                "input_schema": tool_schema(tool),
                "argv_example": ["kri"] + tool_argv(name, tool, example),
            })
    K.out({"status": "ok", "kri": K.__version__, "tools": tools, "skipped": skipped})


def check_service(spec):
    """Chequeo de un servicio declarado en [dependencies] services."""
    import re
    import shutil
    m = re.match(r"^\s*([A-Za-z0-9_.-]+)\s*(.*)$", str(spec))
    name, cond = (m.group(1), m.group(2).strip()) if m else (str(spec), "")
    row = {"service": spec, "ok": None, "detail": None}
    if name == "autoselect":
        health = K.daemon_health(K.AUTOSELECT_URL)
        if not health.get("reachable"):
            row.update(ok=False, detail=f"no responde en {K.AUTOSELECT_URL} (se "
                                        f"arranca a demanda: kri sam start)")
            return row
        version = health.get("api_version") or 1
        ok, why = spec_ok((version, 0, 0), cond) if cond else (True, None)
        row.update(ok=ok, detail=why or f"api_version {version}, capabilities "
                                       f"{health.get('capabilities') or ['segment']}")
        if ok and cond and K.capability_gap(health, "segment_many"):
            row["detail"] += "; " + K.capability_gap(health, "segment_many")
    elif name == "comfyui":
        url = os.environ.get("COMFYUI_URL", "http://127.0.0.1:8188")
        res = K.http_request("GET", "/system_stats", timeout=3.0, url=url)
        ok = isinstance(res, dict) and not res.get("error")
        row.update(ok=ok, detail=f"{url} " + ("responde" if ok else
                                              f"no responde: {res.get('error')}"))
    elif name in ("claude-cli", "claude"):
        path = shutil.which("claude")
        row.update(ok=bool(path), detail=path or "no está `claude` en el PATH")
    elif name in ("krita", "kritamcp"):
        res = K.http_request("GET", "/health", timeout=3.0)
        ok = isinstance(res, dict) and not res.get("error")
        row.update(ok=ok, detail=K.KRITA_URL + (" responde" if ok else
                                              f": {res.get('error')}"))
    else:
        row["detail"] = "servicio desconocido para kri: no se chequea"
    return row


def cmd_ext_doctor(a):
    exts = discover_extensions()
    disabled = set(disabled_extensions())
    venv = ext_venv_dir()
    try:
        backend = pip_backend(venv)[0]
    except ExtError as e:
        backend = f"ninguno: {e}"
    report = {"status": "ok", "kri": K.__version__,
              "python": "%d.%d.%d" % sys.version_info[:3],
              "ext_venv": {"path": venv,
                           "exists": os.path.exists(venv_python(venv)),
                           "python": _venv_version(venv), "pip": backend},
              "extensions": []}
    names = [a.name] if a.name else sorted(exts)
    for name in names:
        if name not in exts:
            K.out({"error": f"no hay ninguna extensión {name!r} instalada"})
        row = ext_row(name, exts[name], name not in disabled)
        try:
            manifest, _ = read_manifest(exts[name])
            deps = manifest.get("dependencies") or {}
        except ExtError:
            deps = {}
        row["services"] = [check_service(s) for s in deps.get("services") or []]
        row["python_req"] = deps.get("python")
        report["extensions"].append(row)
    K.out(report)


def extensions_epilog():
    """Lista de extensiones para `kri --help` (manifiestos, sin importarlas)."""
    try:
        exts = discover_extensions()
        disabled = set(disabled_extensions())
    except Exception as e:
        return f"extensions: unavailable ({e})"
    if not exts:
        return "extensions: none installed (kri ext install PATH)"
    lines = ["extensions (kri ext list):"]
    for name in sorted(exts):
        try:
            desc = (read_manifest(exts[name])[0].get("extension") or {}).get(
                "description", "")
        except ExtError as e:
            desc = f"ROTA: {e}"
        off = " [deshabilitada]" if name in disabled else ""
        lines.append(f"  {name:<12} {desc}{off}")
    return "\n".join(lines)


def build_ext_parser():
    p = argparse.ArgumentParser(
        prog="kri ext",
        description=f"kri extensions (installed in {ext_venv_dir()})")
    ext = p.add_subparsers(dest="ext_cmd", required=True, metavar="ext-command")
    sp = ext.add_parser("list", help="installed extensions: version, enabled, "
                                     "ok, reason when they don't load")
    sp.set_defaults(fn=cmd_ext_list)
    sp = ext.add_parser("install", help="pip install into kri's ext-venv "
                                        "(created if missing) + validate")
    sp.add_argument("target", help="path to the extension project/wheel, or "
                                   "a pip requirement")
    sp.add_argument("-e", "--editable", action="store_true")
    sp.add_argument("--force", action="store_true",
                    help="reinstall even if the same version is there")
    sp.set_defaults(fn=cmd_ext_install)
    for name, fn, hlp in [("enable", cmd_ext_enable, "enable an extension"),
                          ("disable", cmd_ext_disable,
                           "disable it (stays installed)")]:
        sp = ext.add_parser(name, help=hlp)
        sp.add_argument("name")
        sp.set_defaults(fn=fn)
    sp = ext.add_parser("remove", help="pip uninstall an extension")
    sp.add_argument("name")
    sp.add_argument("--dist", action="store_true",
                    help="NAME is a pip distribution, not an extension name")
    sp.set_defaults(fn=cmd_ext_remove)
    sp = ext.add_parser("mcp-tools", help="MCP tools declared by the enabled "
                                          "extensions (for the MCP server)")
    sp.set_defaults(fn=cmd_ext_mcp_tools)
    sp = ext.add_parser("doctor", help="declared python/services per extension")
    sp.add_argument("name", nargs="?")
    sp.set_defaults(fn=cmd_ext_doctor)
    return p


def main_ext(argv):
    args = build_ext_parser().parse_args(argv)
    args.fn(args)
