from elspeth.contracts.trust_boundary import trust_boundary


@trust_boundary(tier=3, source='x', source_param='response', suppresses=())
def admit(response):
    return getattr(response, 'a', None)
