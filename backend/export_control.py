"""Cancellation and progress scoped to the export worker thread."""
import threading
from contextlib import contextmanager


class Cancelled(Exception):
    pass


class Control:
    def __init__(self, update):
        self.event = threading.Event()
        self.update = update

    def check(self):
        if self.event.is_set():
            raise Cancelled()


_local = threading.local()


def current():
    return getattr(_local, 'control', None)


def check():
    if current():
        current().check()


@contextmanager
def use(control):
    previous = current()
    _local.control = control
    try:
        control.check()
        yield
        control.check()
    finally:
        _local.control = previous
