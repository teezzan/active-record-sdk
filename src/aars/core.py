import asyncio
import warnings
from abc import ABC
from collections import OrderedDict
from operator import attrgetter
from typing import Type, TypeVar, Dict, ClassVar, List, Set, Any, Union, Tuple, Optional, Generic

import aiohttp
from aleph_client.vm.cache import VmCache
from pydantic import BaseModel

import aleph_client.asynchronous as client
from aleph_client.types import Account
from aleph_client.chains.ethereum import get_fallback_account
from aleph_client.conf import settings

from .utils import subslices
from .exceptions import AlreadyForgottenError

T = TypeVar('T', bound='Record')


class Record(BaseModel, ABC):
    """
    A basic record which is persisted on Aleph decentralized storage.

    Records can be updated: revision numbers begin at 0 (original upload) and increment for each `upsert()` call.

    Previous revisions can be restored by calling `fetch_revision(rev_no=<number>)` or `fetch_revision(
    rev_hash=<item_hash of inserted update>)`.

    They can also be forgotten: Aleph will ask the network to forget given item, in order to allow for GDPR-compliant
    applications.

    Records have an `indices` class attribute, which allows one to select an index and query it with a key.
    """
    forgotten: bool = False
    _item_hash: Optional[str] = None
    _current_revision: Optional[int] = None
    revision_hashes: List[str] = []
    __indices: ClassVar[Dict[str, 'Index']] = {}

    @property
    def item_hash(self) -> str:
        assert self._item_hash is not None, 'Item hash not set, upsert() first'
        return self._item_hash

    @item_hash.setter
    def item_hash(self, value: str):
        self._item_hash = value

    @property
    def current_revision(self) -> int:
        assert self._current_revision is not None, 'Current revision not set, upsert() first'
        return self._current_revision

    @current_revision.setter
    def current_revision(self, value: int):
        self._current_revision = value

    def __repr__(self):
        return f'{type(self).__name__}({self.item_hash})'

    @property
    def content(self) -> Dict[str, Any]:
        """
        :return: content dictionary of the object, as it is to be stored on Aleph.
        """
        return self.dict(exclude={'item_hash', 'current_revision', 'revision_hashes', 'indices', 'forgotten'})

    async def update_revision_hashes(self: T):
        """
        Updates the list of available revision hashes, in order to fetch these.
        """
        self.revision_hashes = [self.item_hash] + await AARS.fetch_revisions(type(self), ref=self.item_hash)

    async def fetch_revision(self: T, rev_no: Optional[int] = None, rev_hash: Optional[str] = None) -> T:
        """
        Fetches a revision of the object by revision number (0 => original) or revision hash.
        :param rev_no: the revision number of the revision to fetch.
        :param rev_hash: the hash of the revision to fetch.
        """
        if rev_no is not None:
            if rev_no < 0:
                rev_no = len(self.revision_hashes) + rev_no
            if self.current_revision == rev_no:
                return self
            elif rev_no > len(self.revision_hashes):
                raise IndexError(f'No revision no. {rev_no} found for {self}')
            else:
                self.current_revision = rev_no
        elif rev_hash is not None:
            if rev_hash == self.revision_hashes[self.current_revision]:
                return self
            try:
                self.current_revision = self.revision_hashes.index(rev_hash)
            except ValueError:
                raise IndexError(f'{rev_hash} is not a revision of {self}')
        else:
            raise ValueError('Either rev or hash must be provided')

        self.__dict__.update((await AARS.fetch_records(
            type(self),
            item_hashes=[self.revision_hashes[self.current_revision]]
        ))[0].content)

        return self

    async def upsert(self):
        """
        Posts a new item to Aleph or amends it, if it was already posted. Will add new items to local indices.
        For indices to be persisted on Aleph, you need to call `upsert()` on the index itself or `cls.update_indices()`.
        """
        await AARS.post_or_amend_object(self)
        if self.current_revision == 0:
            [index.add(self) for index in self.get_indices()]
        return self

    async def forget(self):
        """
        Orders Aleph to forget a specific object with all its revisions.
        The forgotten object should be deleted afterward, as it is useless now.
        """
        if not self.forgotten:
            await AARS.forget_objects([self])
            self.forgotten = True
        else:
            raise AlreadyForgottenError(self)

    @classmethod
    async def create(cls: Type[T], **kwargs) -> T:
        """
        Initializes and uploads a new item with given properties.
        """
        obj = cls(**kwargs)
        return await obj.upsert()

    @classmethod
    async def from_post(cls: Type[T], post: Dict[str, Any]) -> T:
        """
        Initializes a record object from its raw Aleph data.
        :post: Raw Aleph data.
        """
        obj = cls(**post['content'])
        if post.get('ref') is None:
            obj._item_hash = post['item_hash']
        else:
            obj._item_hash = post['ref']
        await obj.update_revision_hashes()
        obj._current_revision = obj.revision_hashes.index(post['item_hash'])
        return obj

    @classmethod
    async def get(cls: Type[T], hashes: Union[str, List[str]]) -> List[T]:
        """
        Fetches one or more objects of given type by its/their item_hash[es].
        """
        if not isinstance(hashes, List):
            hashes = [hashes]
        return await AARS.fetch_records(cls, list(hashes))

    @classmethod
    async def fetch_all(cls: Type[T]) -> List[T]:
        """
        Fetches all objects of given type.
        """
        return await AARS.fetch_records(cls)

    @classmethod
    async def query(cls: Type[T], **kwargs) -> List[T]:
        """
        Queries an object by given properties through an index, in order to fetch applicable records.
        An index name is defined as '<object_class>.[<object_properties>.]' and is initialized by creating
        an Index instance, targeting a BaseRecord class with a list of properties.

        >>> Index(MyRecord, ['property1', 'property2'])

        This will create an index named 'MyRecord.property1.property2' which can be queried with:

        >>> MyRecord.query(property1='value1', property2='value2')

        If no index is defined for the given properties, an IndexError is raised.

        If only a part of the keys is indexed for the given query, a fallback index is used and locally filtered.
        """
        sorted_items: OrderedDict[str, Any] = OrderedDict(sorted(kwargs.items()))
        sorted_keys = sorted_items.keys()
        full_index_name = cls.__name__ + '.' + '.'.join(sorted_keys)
        index = cls.get_index(full_index_name)
        keys = repr(index).split('.')[1:]
        items = await index.fetch(
            OrderedDict({key: str(sorted_items.get(key)) for key in keys})
        )
        if full_index_name != repr(index):
            filtered_items = list()
            for item in items:
                # eliminate the item which does not fulfill this properties
                class_properties = vars(item)
                required_class_properties = {key: class_properties.get(key) for key in sorted_keys}
                if required_class_properties == dict(sorted_items):
                    filtered_items.append(item)
            return filtered_items
        else:
            return items

    @classmethod
    def add_index(cls: Type[T], index: 'Index') -> None:
        cls.__indices[repr(index)] = index

    @classmethod
    def get_index(cls: Type[T], index_name: str) -> 'Index'[T]:
        """
        Returns an index or any of its subindices by its name. The name is defined as
        '<object_class>.[<object_properties>.]' with the properties being sorted alphabetically. For example,
        Book.author.title is a valid index name, while Book.title.author is not.
        :param index_name: The name of the index to fetch.
        :return: The index instance or a subindex.
        """
        index = cls.__indices.get(index_name)
        if index is None:
            key_subslices = subslices(list(index_name.split('.')[1:]))
            # returns all plausible combinations of keys
            key_subslices = sorted(key_subslices, key=lambda x: len(x), reverse=True)
            for keys in key_subslices:
                name = cls.__name__ + '.' + '.'.join(keys)
                if cls.__indices.get(name):
                    warnings.warn(f'No index {index_name} found. Using {name} instead.')
                    return cls.__indices[name]
            raise IndexError(f'No index or subindex for {index_name} found.')
        return index

    @classmethod
    def get_indices(cls: Type[T]) -> List['Index']:
        return [index for index in cls.__indices.values() if index.datatype == cls]

    @classmethod
    async def update_indices(cls: Type[T]) -> None:
        """Updates all indices of given type."""
        tasks = [index.upsert() for index in cls.get_indices()]
        await asyncio.gather(*tasks)

    @classmethod
    async def regenerate_indices(cls: Type[T]) -> None:
        """Regenerates all indices of given type."""
        items = await cls.fetch_all()
        for index in cls.get_indices():
            index.regenerate(items)


class Index(Record, Generic[T]):
    """
    Class to define Indices.
    """
    datatype: Type[T]
    index_on: List[str]
    hashmap: Dict[Union[str, Tuple], str] = {}

    def __init__(self, datatype: Type[T], on: Union[str, List[str], Tuple[str]]):
        """
        Creates a new index given a datatype and a single or multiple properties to index on.

        >>> Index(MyRecord, 'foo')

        This will create an index named 'MyRecord.foo', which is stored in the `MyRecord` class.
        It is not recommended using the index directly, but rather through the `query` method of the `Record` class like
        so:

        >>> MyRecord.query(foo='bar')

        This returns all records of type MyRecord where foo is equal to 'bar'.

        :param datatype: The datatype to index.
        :param on: The properties to index on.
        """
        if isinstance(on, str):
            on = [on]
        super(Index, self).__init__(datatype=datatype, index_on=sorted(on))
        datatype.add_index(self)

    def __str__(self):
        return f"Index({self.datatype.__name__}.{'.'.join(self.index_on)})"

    def __repr__(self):
        return f"{self.datatype.__name__}.{'.'.join(self.index_on)}"

    async def fetch(self, keys: Optional[Union[OrderedDict[str, str], List[OrderedDict[str, str]]]] = None) -> List[T]:
        """
        Fetches records with given hash(es) from the index.

        :param keys: The hash(es) to fetch.
        """
        hashes: Set[Optional[str]]
        if keys is None:
            hashes = set(self.hashmap.values())
        elif isinstance(keys, OrderedDict):
            if len(keys.values()) == 1:
                hashes = {self.hashmap.get(list(keys.values())[0])}
            else:
                # noinspection PySetFunctionToLiteral
                hashes = set([self.hashmap.get(tuple(keys.values())), ])
        elif isinstance(keys, List):
            hashes = set([self.hashmap.get(tuple(key.values())) for key in keys])
        else:
            hashes = set()

        return await AARS.fetch_records(self.datatype, list([h for h in hashes if h is not None]))

    def add(self, obj: T):
        """Adds a record to the index."""
        assert issubclass(type(obj), Record)
        self.hashmap[attrgetter(*self.index_on)(obj)] = obj.item_hash

    def regenerate(self, items: List[T]):
        """Regenerates the index with given items."""
        self.hashmap = {}
        for item in items:
            self.add(item)


class AARS:
    account: Account
    channel: str
    api_url: str
    session: Optional[aiohttp.ClientSession]
    use_cache: bool
    cache: VmCache

    def __init__(self,
                 use_cache: Optional[bool] = None,
                 account: Optional[Account] = None,
                 channel: Optional[str] = None,
                 api_url: Optional[str] = None,
                 session: Optional[aiohttp.ClientSession] = None):
        """
        Initializes the SDK with an account and a channel.
        :param use_cache: Whether to use Aleph VM caching when running AARS code.
        :param account: Account with which to sign the messages.
        :param channel: Channel to which to send the messages.
        :param api_url: The API URL to use. Defaults to an official Aleph API host.
        :param session: An aiohttp session to use. Defaults to a new session.
        """
        AARS.use_cache = False if use_cache is None else use_cache
        AARS.account = account if account else get_fallback_account()
        AARS.channel = channel if channel else 'AARS_TEST'
        AARS.api_url = api_url if api_url else settings.API_HOST
        AARS.session = session if session else None
        AARS.cache = VmCache(session) if session and use_cache else None

    @classmethod
    async def post_or_amend_object(cls, obj: T, account=None, channel=None):
        """
        Posts or amends an object to Aleph. If the object is already posted, it's list of revision hashes is updated and
        the object receives the latest revision number.
        :param obj: The object to post or amend.
        :param account: The account to post the object with. If None, will use configured account.
        :param channel: The channel to post the object to. If None, will use the configured channel.
        :return: The object, as it is now on Aleph.
        """
        if account is None:
            account = cls.account
        if channel is None:
            channel = cls.channel
        assert isinstance(obj, Record)
        post_type = type(obj).__name__ if obj.item_hash is None else "amend"
        resp = await client.create_post(account=account,
                                        post_content=obj.content,
                                        post_type=post_type,
                                        channel=channel,
                                        ref=obj.item_hash,
                                        api_server=cls.api_url,
                                        session=cls.session)
        if obj.item_hash is None:
            obj.item_hash = resp.item_hash
        obj.revision_hashes.append(resp.item_hash)
        obj.current_revision = len(obj.revision_hashes) - 1
        if cls.use_cache:
            await cls.cache.set(obj.item_hash, obj.json())

    @classmethod
    async def forget_objects(cls, objs: List[T], account: Optional[Account] = None, channel: Optional[str] = None):
        """
        Forgets multiple objects from Aleph and local cache. All related revisions will be forgotten too.
        :param objs: The objects to forget.
        :param account: The account to delete the object with. If None, will use the fallback account.
        :param channel: The channel to delete the object from. If None, will use the TEST channel of the object.
        """
        if account is None:
            account = cls.account
        if channel is None:
            channel = cls.channel
        hashes = []
        for obj in objs:
            hashes += [obj.item_hash] + obj.revision_hashes
        forget_task = client.forget(account=account,
                                    hashes=hashes,
                                    reason=None,
                                    channel=channel,
                                    api_server=cls.api_url,
                                    session=cls.session)
        if cls.use_cache:
            await asyncio.gather(
                forget_task,
                *[cls.cache.delete(h) for h in hashes]
            )
        else:
            await forget_task

    @classmethod
    async def fetch_records(cls,
                            datatype: Type[T],
                            item_hashes: Optional[List[str]] = None,
                            channel: Optional[str] = None,
                            owner: Optional[str] = None) -> List[T]:
        """Retrieves posts as objects by its aleph item_hash.
        :param datatype: The type of the objects to retrieve.
        :param item_hashes: Aleph item_hashes of the objects to fetch.
        :param channel: Channel in which to look for it.
        :param owner: Account that owns the object."""
        assert issubclass(datatype, Record)
        channels = None if channel is None else [channel]
        owners = None if owner is None else [owner]
        if item_hashes is None and channels is None and owners is None:
            channels = [cls.channel]

        if cls.use_cache:
            return await cls._fetch_record_from_cache(channels, datatype, item_hashes, owners)

        aleph_resp = await client.get_posts(hashes=item_hashes,
                                            channels=channels,
                                            types=[datatype.__name__],
                                            addresses=owners,
                                            api_server=cls.api_url,
                                            session=cls.session)
        parse_tasks = [datatype.from_post(post) for post in aleph_resp['posts']]
        return list(await asyncio.gather(*parse_tasks))

    @classmethod
    async def _fetch_record_from_cache(cls, channels, datatype, item_hashes, owners):
        cache_tasks = [cls.cache.get(h) for h in item_hashes]
        cache_resp = list(await asyncio.gather(*cache_tasks))
        cache_miss_hashes = []
        for i, cached in reversed(list(enumerate(cache_resp))):
            if cached is None:
                cache_miss_hashes.append(item_hashes[i])
                cache_resp.pop(i)
        parse_tasks = []
        if len(cache_miss_hashes) > 0:
            aleph_resp = await client.get_posts(hashes=cache_miss_hashes,
                                                channels=channels,
                                                types=[datatype.__name__],
                                                addresses=owners,
                                                api_server=cls.api_url,
                                                session=cls.session)
            parse_tasks = [datatype.from_post(post) for post in aleph_resp['posts']]
        parse_tasks = parse_tasks + [datatype.parse_raw(raw) for raw in cache_resp]
        return list(await asyncio.gather(*parse_tasks))

    @classmethod
    async def fetch_revisions(cls,
                              datatype: Type[T],
                              ref: str,
                              channel: Optional[str] = None,
                              owner: Optional[str] = None) -> List[str]:
        """Retrieves posts of revisions of an object by its item_hash.
        :param datatype: The type of the objects to retrieve.
        :param ref: item_hash of the object, whose revisions to fetch.
        :param channel: Channel in which to look for it.
        :param owner: Account that owns the object."""
        owners = None if owner is None else [owner]
        channels = None if channel is None else [channel]
        if owners is None and channels is None:
            channels = [cls.channel]
        resp = await client.get_posts(refs=[ref],
                                      channels=channels,
                                      types=[datatype.__name__],
                                      addresses=owners,
                                      api_server=cls.api_url,
                                      session=cls.session)
        return list(reversed([post['item_hash'] for post in resp['posts']]))  # reverse to get the oldest first
