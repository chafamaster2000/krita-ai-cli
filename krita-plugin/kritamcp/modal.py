"""Diálogos modales que abre un comando (sin Qt: se testea solo).

Los comandos corren en el hilo principal. Si uno abre un QMessageBox/QDialog
(un aviso al abrir un .kra, por ejemplo), Krita entra en el loop anidado del
diálogo y el comando no vuelve hasta que alguien lo cierra. Antes eso dejaba
a kri colgado hasta el timeout, y a todos los comandos siguientes encolados
detrás (process_commands no es reentrante).

El loop anidado sigue procesando timers, así que el timer de respaldo del
plugin llama a `process_commands` y éste a `ModalGuard.check()`:

- si el comando en curso abrió un modal (no estaba antes de empezar), se le
  contesta YA a su cliente con un error que dice qué diálogo es;
- lo que llegue mientras tanto se contesta con el mismo error, salvo
  `modal_dialog`, que sí se ejecuta (leer o cerrar el diálogo es seguro).

El diálogo NO se cierra solo: puede ser una pregunta que el usuario tiene
que contestar. Se cierra a mano en Krita o con `kri dialog --dismiss`.
"""

SAFE_WHILE_BLOCKED = ("modal_dialog",)


def blocked_error(action, info, queued=False):
    title = (info or {}).get("title") or (info or {}).get("class") or "diálogo"
    text = (info or {}).get("text") or ""
    detail = (info or {}).get("detailed") or ""
    first = text.strip().splitlines()[0] if text.strip() else ""
    if queued:
        msg = (f"Krita está bloqueado por un diálogo modal ('{title}': "
               f"{first}) que abrió '{(info or {}).get('action')}'; "
               f"'{action}' no se ejecutó")
    else:
        msg = (f"Krita abrió un diálogo modal durante '{action}' y quedó "
               f"esperando que alguien lo cierre ('{title}': {first})")
    if detail:
        msg += f". Detalle: {detail.strip()[:400]}"
    return {"error": msg,
            "modal_dialog": info,
            "hint": "cerralo en Krita o con `kri dialog --dismiss`; al "
                    "cerrarlo, el comando original termina en Krita"}


class ModalGuard:
    def __init__(self, queue, active_modal, describe):
        """queue: CommandQueue (pop / set_result). active_modal(): el widget
        modal activo o None. describe(widget) -> dict (class, title, text,
        detailed, buttons)."""
        self.queue = queue
        self.active_modal = active_modal
        self.describe = describe
        self.current = None    # (command_id, action, modal previo)
        self.blocking = None   # info del diálogo que bloquea, con "action"

    def begin(self, command_id, action):
        self.current = (command_id, action, self.active_modal())
        self.blocking = None

    def end(self):
        self.current = None
        self.blocking = None

    def check(self):
        """Devuelve la info del diálogo que bloquea al comando en curso, o
        None. La primera vez que lo ve le contesta al cliente con error."""
        if self.current is None:
            self.blocking = None
            return None
        command_id, action, before = self.current
        m = self.active_modal()
        if m is None or m is before:
            self.blocking = None
            return None
        if self.blocking is None:
            info = dict(self.describe(m) or {})
            info["action"] = action
            self.blocking = info
            self.queue.set_result(command_id, blocked_error(action, info))
        return self.blocking

    def answer_queued(self, run_safe):
        """Contesta todo lo encolado mientras el diálogo bloquea.
        run_safe(command) ejecuta las acciones de SAFE_WHILE_BLOCKED."""
        while True:
            item = self.queue.pop()
            if item is None:
                return
            command_id, command = item
            action = (command or {}).get("action")
            if action in SAFE_WHILE_BLOCKED:
                try:
                    res = run_safe(command)
                except Exception as e:  # noqa: BLE001
                    res = {"error": str(e)}
            else:
                res = blocked_error(action, self.blocking, queued=True)
            self.queue.set_result(command_id, res)
