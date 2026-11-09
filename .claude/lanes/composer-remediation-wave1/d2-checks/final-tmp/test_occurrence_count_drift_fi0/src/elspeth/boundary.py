def admit(x):
    a = getattr(x, 'a', None)
    b = getattr(x, 'b', None)
    c = getattr(x, 'c', None)
    return a, b, c
