import sys
import math

from datetime import datetime

from sentry_sdk.utils import (
    AnnotatedValue,
    capture_internal_exception,
    disable_capture_event,
    format_timestamp,
    json_dumps,
    safe_repr,
    strip_string,
)

import sentry_sdk.utils

from sentry_sdk._compat import text_type, PY2, string_types, number_types, iteritems

from sentry_sdk._types import MYPY

if MYPY:
    from datetime import timedelta

    from types import TracebackType

    from typing import Any
    from typing import Callable
    from typing import ContextManager
    from typing import Dict
    from typing import List
    from typing import Optional
    from typing import Tuple
    from typing import Type
    from typing import Union

    from sentry_sdk._types import NotImplementedType, Event

    Span = Dict[str, Any]

    ReprProcessor = Callable[[Any, Dict[str, Any]], Union[NotImplementedType, str]]
    Segment = Union[str, int]


if PY2:
    # Importing ABCs from collections is deprecated, and will stop working in 3.8
    # https://github.com/python/cpython/blob/master/Lib/collections/__init__.py#L49
    from collections import Mapping, Sequence, Set

    serializable_str_types = string_types

else:
    # New in 3.3
    # https://docs.python.org/3/library/collections.abc.html
    from collections.abc import Mapping, Sequence, Set

    # Bytes are technically not strings in Python 3, but we can serialize them
    serializable_str_types = (str, bytes)


# Maximum length of JSON-serialized event payloads that can be safely sent
# before the server may reject the event due to its size. This is not intended
# to reflect actual values defined server-side, but rather only be an upper
# bound for events sent by the SDK.
#
# Can be overwritten if wanting to send more bytes, e.g. with a custom server.
# When changing this, keep in mind that events may be a little bit larger than
# this value due to attached metadata, so keep the number conservative.
MAX_EVENT_BYTES = 10**6

MAX_DATABAG_DEPTH = 5
MAX_DATABAG_BREADTH = 100
CYCLE_MARKER = "<cyclic>"


global_repr_processors = []  # type: List[ReprProcessor]


def add_global_repr_processor(processor):
    # type: (ReprProcessor) -> None
    global_repr_processors.append(processor)


class Memo(object):
    __slots__ = ("_ids", "_objs")

    def __init__(self):
        # type: () -> None
        self._ids = {}  # type: Dict[int, Any]
        self._objs = []  # type: List[Any]

    def memoize(self, obj):
        # type: (Any) -> ContextManager[bool]
        self._objs.append(obj)
        return self

    def __enter__(self):
        # type: () -> bool
        obj = self._objs[-1]
        if id(obj) in self._ids:
            return True
        else:
            self._ids[id(obj)] = obj
            return False

    def __exit__(
        self,
        ty,  # type: Optional[Type[BaseException]]
        value,  # type: Optional[BaseException]
        tb,  # type: Optional[TracebackType]
    ):
        # type: (...) -> None
        self._ids.pop(id(self._objs.pop()), None)


def serialize(event, smart_transaction_trimming=False, **kwargs):
    # type: (Event, bool, **Any) -> Event
    memo = Memo()
    path = []  # type: List[Segment]
    meta_stack = []  # type: List[Dict[str, Any]]
    span_description_bytes = []  # type: List[int]

    def _annotate(**meta):
        # type: (**Any) -> None
        while len(meta_stack) <= len(path):
            try:
                segment = path[len(meta_stack) - 1]
                node = meta_stack[-1].setdefault(text_type(segment), {})
            except IndexError:
                node = {}

            meta_stack.append(node)

        meta_stack[-1].setdefault("", {}).update(meta)

    def _should_repr_strings():
        # type: () -> Optional[bool]
        """
        By default non-serializable objects are going through
        safe_repr(). For certain places in the event (local vars) we
        want to repr() even things that are JSON-serializable to
        make their type more apparent. For example, it's useful to
        see the difference between a unicode-string and a bytestring
        when viewing a stacktrace.

        For container-types we still don't do anything different.
        Generally we just try to make the Sentry UI present exactly
        what a pretty-printed repr would look like.

        :returns: `True` if we are somewhere in frame variables, and `False` if
            we are in a position where we will never encounter frame variables
            when recursing (for example, we're in `event.extra`). `None` if we
            are not (yet) in frame variables, but might encounter them when
            recursing (e.g.  we're in `event.exception`)
        """
        try:
            p0 = path[0]
            if p0 == "stacktrace" and path[1] == "frames" and path[3] == "vars":
                return True

            if (
                p0 in ("threads", "exception")
                and path[1] == "values"
                and path[3] == "stacktrace"
                and path[4] == "frames"
                and path[6] == "vars"
            ):
                return True
        except IndexError:
            return None

        return False

    def _is_databag():
        # type: () -> Optional[bool]
        """
        A databag is any value that we need to trim.

        :returns: Works like `_should_repr_strings()`. `True` for "yes",
            `False` for :"no", `None` for "maybe soon".
        """
        try:
            rv = _should_repr_strings()
            if rv in (True, None):
                return rv

            p0 = path[0]
            if p0 == "request" and path[1] == "data":
                return True

            if p0 == "breadcrumbs" and path[1] == "values":
                path[2]
                return True

            if p0 == "extra":
                return True

        except IndexError:
            return None

        return False

    def _serialize_node(
        obj,  # type: Any
        is_databag=None,  # type: Optional[bool]
        should_repr_strings=None,  # type: Optional[bool]
        segment=None,  # type: Optional[Segment]
        remaining_breadth=None,  # type: Optional[int]
        remaining_depth=None,  # type: Optional[int]
    ):
        # type: (...) -> Any
        if segment is not None:
            path.append(segment)

        try:
            with memo.memoize(obj) as result:
                if result:
                    return CYCLE_MARKER

                return _serialize_node_impl(
                    obj,
                    is_databag=is_databag,
                    should_repr_strings=should_repr_strings,
                    remaining_depth=remaining_depth,
                    remaining_breadth=remaining_breadth,
                )
        except BaseException:
            capture_internal_exception(sys.exc_info())

            if is_databag:
                return "<failed to serialize, use init(debug=True) to see error logs>"

            return None
        finally:
            if segment is not None:
                path.pop()
                del meta_stack[len(path) + 1 :]

    def _flatten_annotated(obj):
        # type: (Any) -> Any
        if isinstance(obj, AnnotatedValue):
            _annotate(**obj.metadata)
            obj = obj.value
        return obj

    def _serialize_node_impl(
        obj, is_databag, should_repr_strings, remaining_depth, remaining_breadth
    ):
        # type: (Any, Optional[bool], Optional[bool], Optional[int], Optional[int]) -> Any
        if should_repr_strings is None:
            should_repr_strings = _should_repr_strings()

        if is_databag is None:
            is_databag = _is_databag()

        if is_databag and remaining_depth is None:
            remaining_depth = MAX_DATABAG_DEPTH
        if is_databag and remaining_breadth is None:
            remaining_breadth = MAX_DATABAG_BREADTH

        obj = _flatten_annotated(obj)

        if remaining_depth is not None and remaining_depth <= 0:
            _annotate(rem=[["!limit", "x"]])
            if is_databag:
                return _flatten_annotated(strip_string(safe_repr(obj)))
            return None

        if is_databag and global_repr_processors:
            hints = {"memo": memo, "remaining_depth": remaining_depth}
            for processor in global_repr_processors:
                result = processor(obj, hints)
                if result is not NotImplemented:
                    return _flatten_annotated(result)

        sentry_repr = getattr(type(obj), "__sentry_repr__", None)

        if obj is None or isinstance(obj, (bool, number_types)):
            if should_repr_strings or (
                isinstance(obj, float) and (math.isinf(obj) or math.isnan(obj))
            ):
                return safe_repr(obj)
            else:
                return obj

        elif callable(sentry_repr):
            return sentry_repr(obj)

        elif isinstance(obj, datetime):
            return (
                text_type(format_timestamp(obj))
                if not should_repr_strings
                else safe_repr(obj)
            )

        elif isinstance(obj, Mapping):
            # Create temporary copy here to avoid calling too much code that
            # might mutate our dictionary while we're still iterating over it.
            obj = dict(iteritems(obj))

            rv_dict = {}  # type: Dict[str, Any]
            i = 0

            for k, v in iteritems(obj):
                if remaining_breadth is not None and i >= remaining_breadth:
                    _annotate(len=len(obj))
                    break

                str_k = text_type(k)
                v = _serialize_node(
                    v,
                    segment=str_k,
                    should_repr_strings=should_repr_strings,
                    is_databag=is_databag,
                    remaining_depth=remaining_depth - 1
                    if remaining_depth is not None
                    else None,
                    remaining_breadth=remaining_breadth,
                )
                rv_dict[str_k] = v
                i += 1

            return rv_dict

        elif not isinstance(obj, serializable_str_types) and isinstance(
            obj, (Set, Sequence)
        ):
            rv_list = []

            for i, v in enumerate(obj):
                if remaining_breadth is not None and i >= remaining_breadth:
                    _annotate(len=len(obj))
                    break

                rv_list.append(
                    _serialize_node(
                        v,
                        segment=i,
                        should_repr_strings=should_repr_strings,
                        is_databag=is_databag,
                        remaining_depth=remaining_depth - 1
                        if remaining_depth is not None
                        else None,
                        remaining_breadth=remaining_breadth,
                    )
                )

            return rv_list

        if should_repr_strings:
            obj = safe_repr(obj)
        else:
            if isinstance(obj, bytes):
                obj = obj.decode("utf-8", "replace")

            if not isinstance(obj, string_types):
                obj = safe_repr(obj)

        # Allow span descriptions to be longer than other strings.
        #
        # For database auto-instrumented spans, the description contains
        # potentially long SQL queries that are most useful when not truncated.
        # Because arbitrarily large events may be discarded by the server as a
        # protection mechanism, we dynamically limit the description length
        # later in _truncate_span_descriptions.
        if (
            smart_transaction_trimming
            and len(path) == 3
            and path[0] == "spans"
            and path[-1] == "description"
        ):
            span_description_bytes.append(len(obj))
            return obj
        return _flatten_annotated(strip_string(obj))

    def _truncate_span_descriptions(serialized_event, event, excess_bytes):
        # type: (Event, Event, int) -> None
        """
        Modifies serialized_event in-place trying to remove excess_bytes from
        span descriptions. The original event is used read-only to access the
        span timestamps (represented as RFC3399-formatted strings in
        serialized_event).

        It uses heuristics to prioritize preserving the description of spans
        that might be the most interesting ones in terms of understanding and
        optimizing performance.
        """
        # When truncating a description, preserve a small prefix.
        min_length = 10

        def shortest_duration_longest_description_first(args):
            # type: (Tuple[int, Span]) -> Tuple[timedelta, int]
            i, serialized_span = args
            span = event["spans"][i]
            now = datetime.utcnow()
            start = span.get("start_timestamp") or now
            end = span.get("timestamp") or now
            duration = end - start
            description = serialized_span.get("description") or ""
            return (duration, -len(description))

        # Note: for simplicity we sort spans by exact duration and description
        # length. If ever needed, we could have a more involved heuristic, e.g.
        # replacing exact durations with "buckets" and/or looking at other span
        # properties.
        path.append("spans")
        for i, span in sorted(
            enumerate(serialized_event.get("spans") or []),
            key=shortest_duration_longest_description_first,
        ):
            description = span.get("description") or ""
            if len(description) <= min_length:
                continue
            excess_bytes -= len(description) - min_length
            path.extend([i, "description"])
            # Note: the last time we call strip_string we could preserve a few
            # more bytes up to a total length of MAX_EVENT_BYTES. Since that's
            # not strictly required, we leave it out for now for simplicity.
            span["description"] = _flatten_annotated(
                strip_string(description, max_length=min_length)
            )
            del path[-2:]
            del meta_stack[len(path) + 1 :]

            if excess_bytes <= 0:
                break
        path.pop()
        del meta_stack[len(path) + 1 :]

    disable_capture_event.set(True)
    try:
        rv = _serialize_node(event, **kwargs)
        if meta_stack and isinstance(rv, dict):
            rv["_meta"] = meta_stack[0]

        sum_span_description_bytes = sum(span_description_bytes)
        if smart_transaction_trimming and sum_span_description_bytes > 0:
            span_count = len(event.get("spans") or [])
            # This is an upper bound of how many bytes all descriptions would
            # consume if the usual string truncation in _serialize_node_impl
            # would have taken place, not accounting for the metadata attached
            # as event["_meta"].
            descriptions_budget_bytes = span_count * sentry_sdk.utils.MAX_STRING_LENGTH

            # If by not truncating descriptions we ended up with more bytes than
            # per the usual string truncation, check if the event is too large
            # and we need to truncate some descriptions.
            #
            # This is guarded with an if statement to avoid JSON-encoding the
            # event unnecessarily.
            if sum_span_description_bytes > descriptions_budget_bytes:
                original_bytes = len(json_dumps(rv))
                excess_bytes = original_bytes - MAX_EVENT_BYTES
                if excess_bytes > 0:
                    # Event is too large, will likely be discarded by the
                    # server. Trim it down before sending.
                    _truncate_span_descriptions(rv, event, excess_bytes)

                    # Span descriptions truncated, set or reset _meta.
                    #
                    # We run the same code earlier because we want to account
                    # for _meta when calculating original_bytes, the number of
                    # bytes in the JSON-encoded event.
                    if meta_stack and isinstance(rv, dict):
                        rv["_meta"] = meta_stack[0]
        return rv
    finally:
        disable_capture_event.set(False)
