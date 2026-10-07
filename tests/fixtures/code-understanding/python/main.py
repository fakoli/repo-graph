"""Synthetic source only: café, λ and 🌱 test UTF-8 offsets."""

from .helpers import finish as imported_finish
from .optional import added


def local(value):
    return value + 1


def direct():
    return local(1)


def imported():
    return imported_finish(2)


def reference():
    return local


def value_alias():
    next_step = local
    return next_step(2)


def shadow():
    def local(value):
        return value + 100
    return local(3)


def callback(fn):
    return fn(4)


class First:
    def run(self):
        return local(5)


class Second:
    def run(self):
        return imported_finish(6)


def receiver(choose_first):
    worker = First() if choose_first else Second()
    return worker.run()


def missing():
    return added(7)


def dynamic(table, name):
    return table[name](8)


def café(value):
    return value - 1


def unicode_call():
    return café(9)
