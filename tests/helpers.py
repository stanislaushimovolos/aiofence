import asyncio


def deadline_in(delay: float) -> float:
    return asyncio.get_running_loop().time() + delay
