from PySide6.QtCore import QObject, QThread, Signal


class BackendWorker(QObject):
    """Runs one backend callable off the GUI thread and reports the outcome back."""

    finished = Signal(object)
    failed = Signal(str)

    def __init__(self, func, args=(), kwargs=None):
        super().__init__()
        self._func = func
        self._args = args
        self._kwargs = kwargs or {}

    def run(self):
        try:
            result = self._func(*self._args, **self._kwargs)
        except Exception as exc:  # noqa: BLE001 - any backend failure must reach the GUI as a dialog, never a crash
            self.failed.emit(str(exc))
            return
        self.finished.emit(result)


def run_in_background(owner, func, on_success, on_error, args=(), kwargs=None):
    """
    Runs func(*args, **kwargs) on a background QThread.
    on_success(result) / on_error(message) are invoked back on the GUI thread.

    A strong reference to (thread, worker) is kept on `owner._background_ops`
    until `thread.finished` actually fires (i.e. the OS thread has fully
    stopped, not just when the worker's own finished/failed signal fires).
    Dropping the Python reference any earlier races the thread's own teardown
    and can crash the interpreter, so both cleanup and deleteLater are chained
    off `thread.finished` rather than off the worker's result signals.
    """
    thread = QThread(owner)
    worker = BackendWorker(func, args=args, kwargs=kwargs)
    worker.moveToThread(thread)

    if not hasattr(owner, "_background_ops"):
        owner._background_ops = []
    holder = (thread, worker)
    owner._background_ops.append(holder)

    def cleanup():
        if holder in owner._background_ops:
            owner._background_ops.remove(holder)

    thread.started.connect(worker.run)
    worker.finished.connect(on_success)
    worker.failed.connect(on_error)
    worker.finished.connect(thread.quit)
    worker.failed.connect(thread.quit)
    thread.finished.connect(worker.deleteLater)
    thread.finished.connect(thread.deleteLater)
    thread.finished.connect(cleanup)

    thread.start()
    return thread, worker
