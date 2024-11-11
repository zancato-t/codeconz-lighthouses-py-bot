from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from typing import ClassVar as _ClassVar, Iterable as _Iterable, Mapping as _Mapping, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class Action(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    PASS: _ClassVar[Action]
    MOVE: _ClassVar[Action]
    ATTACK: _ClassVar[Action]
    CONNECT: _ClassVar[Action]
PASS: Action
MOVE: Action
ATTACK: Action
CONNECT: Action

class NewPlayer(_message.Message):
    __slots__ = ("name", "serverAddress")
    NAME_FIELD_NUMBER: _ClassVar[int]
    SERVERADDRESS_FIELD_NUMBER: _ClassVar[int]
    name: str
    serverAddress: str
    def __init__(self, name: _Optional[str] = ..., serverAddress: _Optional[str] = ...) -> None: ...

class MapRow(_message.Message):
    __slots__ = ("Row",)
    ROW_FIELD_NUMBER: _ClassVar[int]
    Row: _containers.RepeatedScalarFieldContainer[int]
    def __init__(self, Row: _Optional[_Iterable[int]] = ...) -> None: ...

class Position(_message.Message):
    __slots__ = ("X", "Y")
    X_FIELD_NUMBER: _ClassVar[int]
    Y_FIELD_NUMBER: _ClassVar[int]
    X: int
    Y: int
    def __init__(self, X: _Optional[int] = ..., Y: _Optional[int] = ...) -> None: ...

class Lighthouse(_message.Message):
    __slots__ = ("Position", "Owner", "Energy", "Connections", "HaveKey")
    POSITION_FIELD_NUMBER: _ClassVar[int]
    OWNER_FIELD_NUMBER: _ClassVar[int]
    ENERGY_FIELD_NUMBER: _ClassVar[int]
    CONNECTIONS_FIELD_NUMBER: _ClassVar[int]
    HAVEKEY_FIELD_NUMBER: _ClassVar[int]
    Position: Position
    Owner: int
    Energy: int
    Connections: _containers.RepeatedCompositeFieldContainer[Position]
    HaveKey: bool
    def __init__(self, Position: _Optional[_Union[Position, _Mapping]] = ..., Owner: _Optional[int] = ..., Energy: _Optional[int] = ..., Connections: _Optional[_Iterable[_Union[Position, _Mapping]]] = ..., HaveKey: bool = ...) -> None: ...

class PlayerID(_message.Message):
    __slots__ = ("PlayerID",)
    PLAYERID_FIELD_NUMBER: _ClassVar[int]
    PlayerID: int
    def __init__(self, PlayerID: _Optional[int] = ...) -> None: ...

class NewPlayerInitialState(_message.Message):
    __slots__ = ("PlayerID", "PlayerCount", "Position", "Map", "Lighthouses")
    PLAYERID_FIELD_NUMBER: _ClassVar[int]
    PLAYERCOUNT_FIELD_NUMBER: _ClassVar[int]
    POSITION_FIELD_NUMBER: _ClassVar[int]
    MAP_FIELD_NUMBER: _ClassVar[int]
    LIGHTHOUSES_FIELD_NUMBER: _ClassVar[int]
    PlayerID: int
    PlayerCount: int
    Position: Position
    Map: _containers.RepeatedCompositeFieldContainer[MapRow]
    Lighthouses: _containers.RepeatedCompositeFieldContainer[Lighthouse]
    def __init__(self, PlayerID: _Optional[int] = ..., PlayerCount: _Optional[int] = ..., Position: _Optional[_Union[Position, _Mapping]] = ..., Map: _Optional[_Iterable[_Union[MapRow, _Mapping]]] = ..., Lighthouses: _Optional[_Iterable[_Union[Lighthouse, _Mapping]]] = ...) -> None: ...

class NewTurn(_message.Message):
    __slots__ = ("Position", "Score", "Energy", "View", "Lighthouses")
    POSITION_FIELD_NUMBER: _ClassVar[int]
    SCORE_FIELD_NUMBER: _ClassVar[int]
    ENERGY_FIELD_NUMBER: _ClassVar[int]
    VIEW_FIELD_NUMBER: _ClassVar[int]
    LIGHTHOUSES_FIELD_NUMBER: _ClassVar[int]
    Position: Position
    Score: int
    Energy: int
    View: _containers.RepeatedCompositeFieldContainer[MapRow]
    Lighthouses: _containers.RepeatedCompositeFieldContainer[Lighthouse]
    def __init__(self, Position: _Optional[_Union[Position, _Mapping]] = ..., Score: _Optional[int] = ..., Energy: _Optional[int] = ..., View: _Optional[_Iterable[_Union[MapRow, _Mapping]]] = ..., Lighthouses: _Optional[_Iterable[_Union[Lighthouse, _Mapping]]] = ...) -> None: ...

class NewAction(_message.Message):
    __slots__ = ("Action", "Destination", "Energy")
    ACTION_FIELD_NUMBER: _ClassVar[int]
    DESTINATION_FIELD_NUMBER: _ClassVar[int]
    ENERGY_FIELD_NUMBER: _ClassVar[int]
    Action: Action
    Destination: Position
    Energy: int
    def __init__(self, Action: _Optional[_Union[Action, str]] = ..., Destination: _Optional[_Union[Position, _Mapping]] = ..., Energy: _Optional[int] = ...) -> None: ...

class PlayerReady(_message.Message):
    __slots__ = ("Ready",)
    READY_FIELD_NUMBER: _ClassVar[int]
    Ready: bool
    def __init__(self, Ready: bool = ...) -> None: ...
