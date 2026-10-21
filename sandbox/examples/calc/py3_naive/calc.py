# "Migrated" version: the Py2 file run unchanged under Python 3 (a naive migration).
def average(xs):
    return sum(xs) / len(xs)


def shout(s):
    return s.upper() + "!"


def evens(xs):
    return filter(lambda x: x % 2 == 0, xs)


def append_twice(xs, x):
    xs.append(x)
    xs.append(x)


class Counter(object):
    def __init__(self, start=0):
        self.n = start

    def bump(self, by=1):
        self.n += by
        return self.n


def spin():
    while True:
        pass
