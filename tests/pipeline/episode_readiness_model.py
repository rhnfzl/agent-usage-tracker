from collections.abc import Iterator
from enum import Enum, auto
from itertools import combinations


class Step(Enum):
    START_SCAN = auto()
    FINISH_SCAN = auto()
    START_REBUILD = auto()
    PUBLISH_REBUILD = auto()
    ABORT_REBUILD = auto()


def schedules() -> Iterator[tuple[Step, ...]]:
    rebuilds = (
        Step.START_REBUILD, Step.PUBLISH_REBUILD,
        Step.START_REBUILD, Step.ABORT_REBUILD,
        Step.START_REBUILD, Step.PUBLISH_REBUILD,
    )
    for start, finish in combinations(range(len(rebuilds) + 2), 2):
        steps = list(rebuilds)
        steps.insert(start, Step.START_SCAN)
        steps.insert(finish, Step.FINISH_SCAN)
        yield tuple(steps)


def expected_watermarks(steps: tuple[Step, ...]) -> Iterator[int | None]:
    scan_started = completed = captured = published = None
    for tick, step in enumerate(steps, start=1):
        match step:
            case Step.START_SCAN:
                scan_started = tick
            case Step.FINISH_SCAN:
                completed = scan_started
            case Step.START_REBUILD:
                captured = completed
            case Step.PUBLISH_REBUILD:
                published = captured
            case Step.ABORT_REBUILD:
                captured = None
        yield published
