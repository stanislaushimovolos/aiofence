from constellate import Fence


def test__Fence__default_construction__not_cancelled():
    fence = Fence()
    assert fence.cancelled is False
    assert fence.reasons == ()
