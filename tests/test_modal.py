"""ModalGuard del plugin kritamcp (sin Krita ni Qt)."""
import importlib.util
import os
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_spec = importlib.util.spec_from_file_location(
    "kritamcp_modal", os.path.join(REPO, "krita-plugin", "kritamcp", "modal.py"))
modal = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(modal)


class FakeQueue:
    def __init__(self, items=()):
        self.items = list(items)
        self.results = {}

    def pop(self):
        return self.items.pop(0) if self.items else None

    def set_result(self, cid, res):
        self.results.setdefault(cid, res)  # como CommandQueue: el 1º gana


class Dlg:
    pass


def describe(w):
    return {"class": "QMessageBox", "title": "Krita",
            "text": "Ha ocurrido un problema al abrir x.kra.",
            "detailed": "No se ha podido cargar el perfil: x/layers/layer2.icc."}


class ModalGuardTest(unittest.TestCase):
    def setUp(self):
        self.modal = None
        self.q = FakeQueue()
        self.g = modal.ModalGuard(self.q, lambda: self.modal, describe)

    def test_no_command_no_answer(self):
        self.modal = Dlg()
        self.assertIsNone(self.g.check())
        self.assertEqual(self.q.results, {})

    def test_modal_opened_by_command_answers_its_client(self):
        self.g.begin(7, "open_file")
        self.assertIsNone(self.g.check())
        self.modal = Dlg()
        info = self.g.check()
        self.assertEqual(info["action"], "open_file")
        err = self.q.results[7]
        self.assertIn("diálogo modal", err["error"])
        self.assertIn("open_file", err["error"])
        self.assertIn("layer2.icc", err["error"])
        self.assertIn("kri dialog --dismiss", err["hint"])
        # una sola respuesta aunque el timer vuelva a mirar
        self.g.check()
        self.assertEqual(len(self.q.results), 1)

    def test_preexisting_modal_does_not_block(self):
        self.modal = Dlg()          # el usuario tenía Preferencias abierto
        self.g.begin(1, "doc_info")
        self.assertIsNone(self.g.check())
        self.assertEqual(self.q.results, {})

    def test_queued_commands_fail_fast_but_modal_dialog_runs(self):
        self.g.begin(1, "open_file")
        self.modal = Dlg()
        self.g.check()
        self.q.items = [(2, {"action": "layer_list"}),
                        (3, {"action": "modal_dialog", "params": {}})]
        ran = []
        self.g.answer_queued(lambda c: ran.append(c) or {"status": "ok"})
        self.assertIn("no se ejecutó", self.q.results[2]["error"])
        self.assertIn("layer_list", self.q.results[2]["error"])
        self.assertEqual(self.q.results[3], {"status": "ok"})
        self.assertEqual([c["action"] for c in ran], ["modal_dialog"])

    def test_closed_modal_unblocks(self):
        self.g.begin(1, "open_file")
        self.modal = Dlg()
        self.assertIsNotNone(self.g.check())
        self.modal = None
        self.assertIsNone(self.g.check())
        self.g.end()
        self.assertIsNone(self.g.blocking)


if __name__ == "__main__":
    unittest.main()
