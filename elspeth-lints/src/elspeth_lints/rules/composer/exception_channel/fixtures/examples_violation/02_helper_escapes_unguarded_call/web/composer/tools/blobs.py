def _parse(value):
    if not isinstance(value, str):
        raise ValueError("must be str")
    return value


def guarded_handler(state, value):
    try:
        return _parse(value)
    except ValueError as exc:
        return _failure_result(state, str(exc))


def unguarded_handler(state, value):
    return _parse(value)


def _failure_result(state, message):
    return (state, message)
