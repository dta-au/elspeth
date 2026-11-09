def admit(x):
    a = getattr(x, 'a', None)
    b = getattr(x, 'b', None)
    return a, b
