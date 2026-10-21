# Legacy Python 2 code containing three classic migration hazards.
def average(xs):
    return sum(xs) / len(xs)  # py2: floor division for ints


def shout(s):
    return s.upper() + "!"  # py2: native str is bytes


def evens(xs):
    return filter(lambda x: x % 2 == 0, xs)  # py2: returns a list


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
