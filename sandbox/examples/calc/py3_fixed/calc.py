# Correct Python 3 migration: preserves the legacy behaviour.
def average(xs):
    return sum(xs) // len(xs)


def shout(s):
    return s.upper() + "!"


def evens(xs):
    return list(filter(lambda x: x % 2 == 0, xs))


def append_twice(xs, x):
    xs.append(x)
    xs.append(x)


class Counter:
    def __init__(self, start=0):
        self.n = start

    def bump(self, by=1):
        self.n += by
        return self.n


def spin():
    while True:
        pass
