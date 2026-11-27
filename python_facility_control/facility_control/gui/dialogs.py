"""Qt implementation of the controller's DialogProvider.

`ask()` is called from the controller thread; it hands a `DialogHandle` back immediately and
emits a queued signal so the GUI thread shows a QMessageBox (no nested event loop).
The controller decides what to do with the handle: in blocking mode (the VI's behaviour, default)
it waits for the answer – the control loop stands still, the GUI keeps repainting; in non-blocking
mode it carries on and applies the answer when it arrives.

Two sorts of box:

  * QUESTIONS ('two', 'three') and, in blocking mode, the 'info' answer to something the operator
    just pressed: modal, like the LabVIEW dialog – the operator has to answer before using the panel.
    The controller only ever has one of these open.
  * NOTICES ('notice'; and 'info' in non-blocking mode): the facility is telling the operator
    something while it carries on ("No RS-485 reply from Turbo 2").  These are NEVER modal: the
    panel underneath – STOP, Clear Error, the mode buttons – stays usable however many are open
    (2026-11-27: they used to be modal, and a fault that raised hundreds of them locked the
    operators out of the panel).  The controller limits how many it raises (Controller._notify);
    `max_notices` here is a second limit in case anything ever gets past that.
"""
from __future__ import annotations

from typing import List, Optional

from PySide6.QtCore import QObject, Qt, Signal, Slot
from PySide6.QtWidgets import QMessageBox, QWidget

from ..controller import DialogHandle


class QtDialogProvider(QObject):
    _ask = Signal(object)   # payload dict

    def __init__(self, parent: Optional[QWidget] = None, modal_info: bool = True, max_notices: int = 8):
        """modal_info: one-button 'info' boxes are modal – right when the controller waits for them
        (blocking mode); non-blocking mode passes False so they never get in the way.
        max_notices: never more than this many non-modal boxes open at once; a notice beyond that
        is dropped here (the controller has written it to the event log)."""
        super().__init__(parent)
        self._parent = parent
        self.modal_info = modal_info
        self.max_notices = int(max_notices)
        self.dropped = 0
        self._ask.connect(self._show, Qt.QueuedConnection)
        self._open_boxes: List[QMessageBox] = []
        self._open_notices: List[QMessageBox] = []

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
        one_button = kind in ("info", "notice")
        modal = not one_button or (kind == "info" and self.modal_info)
        if not modal and len(self._open_notices) >= self.max_notices:
            self.dropped += 1                      # (the controller's own limit is lower - a backstop)
            handle.resolve(None)
            return
        box = QMessageBox(self._parent)
        box.setWindowTitle("Facility Control")
        box.setText(payload["message"])
        box.setIcon(QMessageBox.Information if one_button else QMessageBox.Question)
        buttons = payload.get("buttons") or ["OK"]
        qbtns = [box.addButton(b.strip() or "OK", QMessageBox.ActionRole) for b in buttons]
        box.setAttribute(Qt.WA_DeleteOnClose, True)

        def finished(_code=None):
            clicked = box.clickedButton()
            if clicked in qbtns:
                result = qbtns.index(clicked)
            else:                                  # window closed with [x]
                result = 3 if kind == "three" else 1
            handle.resolve(None if one_button else result)
            if box in self._open_boxes:
                self._open_boxes.remove(box)
            if box in self._open_notices:
                self._open_notices.remove(box)

        box.finished.connect(finished)
        self._open_boxes.append(box)
        if modal:
            box.setWindowModality(Qt.ApplicationModal)
            box.open()
        else:
            # show(), not open(): QDialog.open() makes a box window-modal whatever was asked for,
            # and a modal notice blocks the panel it sits on
            box.setModal(False)
            box.setWindowModality(Qt.NonModal)
            self._open_notices.append(box)
            box.show()
            self._cascade(box)
            box.raise_()

    def _cascade(self, box: QMessageBox) -> None:
        """Several notices: each a little down and to the right of the one before, so that none
        hides behind another."""
        n = len(self._open_notices) - 1
        if n <= 0:
            return
        try:
            first = self._open_notices[0]
            box.move(first.x() + 28 * n, first.y() + 28 * n)
        except Exception:
            pass
