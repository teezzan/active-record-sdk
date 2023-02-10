import operator
from itertools import *
from typing import AsyncIterator, List, TypeVar, OrderedDict, Generic, Type

T = TypeVar('T')


class IndexQuery(OrderedDict, Generic[T]):
    record_type: Type[T]

    def __init__(self, record_type: Type[T], **kwargs):
        super().__init__({item[0]: item[1] for item in sorted(kwargs.items())})
        self.record_type = record_type

    def get_index_name(self) -> str:
        return self.record_type.__name__ + '.' + '.'.join(self.keys())

    def get_subquery(self, keys: List[str]) -> 'IndexQuery':
        return IndexQuery(self.record_type, **{key: arg for key, arg in self.items() if key in keys})


def subslices(seq):
    """
    Return all contiguous non-empty subslices of a sequence.
    Taken from more_itertools.

    Example:
        list(subslices([1, 2, 3])) == [[1], [1, 2], [1, 2, 3], [2], [2, 3], [3]]
    """
    #
    slices = starmap(slice, combinations(range(len(seq) + 1), 2))
    return map(operator.getitem, repeat(seq), slices)


def possible_index_names(seq):
    """
    Return all possible index names for a sequence of properties.

    Example:
        list(possible_index_names(['A', 'B', 'C'])) == [['A'], ['A.B'], ['A.B.C'], ['B'], ['B.C'], ['C']]
    """
    return map('.'.join, subslices(seq))


async def async_iterator_to_list(iterator: AsyncIterator[T]) -> List[T]:
    """
    Return a list from an async iterator.
    """
    return [item async for item in iterator]
