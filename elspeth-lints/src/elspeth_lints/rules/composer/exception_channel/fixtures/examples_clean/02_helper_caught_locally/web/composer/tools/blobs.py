def _inner(keys):
    if not keys:
        raise ValueError("keys must be non-empty")
    return keys[0]


def _outer(container, keys):
    head = _inner(keys)
    if head in container and not isinstance(container[head], dict):
        raise ValueError("segment is not an object")
    return head


def handler(state, container, keys):
    try:
        head = _outer(container, keys)
    except (KeyError, ValueError) as exc:
        return _failure_result(state, str(exc))
    return (state, head)


def _failure_result(state, message):
    return (state, message)
