"""Qt implementation of the controller's DialogProvider.

`ask()` is called from the controller thread; it hands a `DialogHandle` back immediately and
emits a queued signal so the GUI thread shows a QMessageBox with `open()` (no nested event loop).
The controller decides what to do with the handle: in blocking mode (the VI's behaviour, default)
it waits for the answer – the control loop stands still, the GUI keeps repainting; in non-blocking
mode it carries on and applies the answer when it arrives.  Yes/No boxes are application-modal
like the LabVIEW dialog – the operator has to answer before using other controls.
"""
from __future__ import annotations

from typing import List, Optional

from PySide6.QtCore import QObject, Qt, Signal, Slot
from PySide6.QtWidgets import QMessageBox, QWidget

from ..controller import DialogHandle


class QtDialogProvider(QObject):
    _ask = Signal(object)   # payload dict

    def __init__(self, parent: Optional[QWidget] = None, modal_info: bool = True):
        """modal_info: one-button (info) boxes are modal too – right when the controller waits for
        them (blocking mode); non-blocking mode passes False so they never get in the way."""
        super().__init__(parent)
        self._parent = parent
        self.modal_info = modal_info
        self._ask.connect(self._show, Qt.QueuedConnection)
        self._open_boxes: List[QMessageBox] = []

    def ask(self, kind: str, message: str, buttons: List[str]) -> DialogHandle:
        handle = DialogHandle()
        self._ask.emit({"kind": kind, "message": message, "buttons": list(buttons), "handle": handle})
        return handle

    def close_all(self) -> None:
        """Resolve every open box as 'window closed' (used when the program exits)."""
        for box in list(self._open_boxes):
            box.close()

    @Slot(object)
    def _show(self, payload: dict):
        handle: DialogHandle = payload["handle"]
        kind = payload["kind"]
        box = QMessageBox(self._parent)
        box.setWindowTitle("Facility Control")
        box.setText(payload["message"])
        box.setIcon(QMessageBox.Information if kind == "info" else QMessageBox.Question)
        buttons = payload.get("buttons") or ["OK"]
        qbtns = [box.addButton(b.strip() or "OK", QMessageBox.ActionRole) for b in buttons]
        modal = kind != "info" or self.modal_info
        box.setWindowModality(Qt.ApplicationModal if modal else Qt.NonModal)
        box.setAttribute(Qt.WA_DeleteOnClose, True)

        def finished(_code=None):
            clicked = box.clickedButton()
            if clicked in qbtns:
                result = qbtns.index(clicked)
            else:                                  # window closed with [x]
                result = 3 if kind == "three" else 1
            handle.resolve(None if kind == "info" else result)
            if box in self._open_boxes:
                self._open_boxes.remove(box)

        box.finished.connect(finished)
        self._open_boxes.append(box)
        box.open()
