def inspect_value(obj):
    first = getattr(obj, 'status', None)
    second = getattr(obj, dynamic_name, None)
    return first, second
