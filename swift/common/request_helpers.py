# Copyright (c) 2010-2013 OpenStack Foundation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
Miscellaneous utility functions for use in generating responses.

Why not swift.common.utils, you ask? Because this way we can import things
from swob in here without creating circular imports.
"""

import sys
import time
from contextlib import contextmanager
from urllib import unquote
from swift.common.constraints import FORMAT2CONTENT_TYPE
from swift.common.exceptions import ListingIterError, SegmentError
from swift.common.http import is_success, HTTP_SERVICE_UNAVAILABLE
from swift.common.swob import HTTPBadRequest, HTTPNotAcceptable
from swift.common.utils import split_path, validate_device_partition
from swift.common.wsgi import make_subrequest


def get_param(req, name, default=None):
    """
    Get parameters from an HTTP request ensuring proper handling UTF-8
    encoding.

    :param req: request object
    :param name: parameter name
    :param default: result to return if the parameter is not found
    :returns: HTTP request parameter value
              (as UTF-8 encoded str, not unicode object)
    :raises: HTTPBadRequest if param not valid UTF-8 byte sequence
    """
    value = req.params.get(name, default)
    if value and not isinstance(value, unicode):
        try:
            value.decode('utf8')    # Ensure UTF8ness
        except UnicodeDecodeError:
            raise HTTPBadRequest(
                request=req, content_type='text/plain',
                body='"%s" parameter not valid UTF-8' % name)
    return value


def get_listing_content_type(req):
    """
    Determine the content type to use for an account or container listing
    response.

    :param req: request object
    :returns: content type as a string (e.g. text/plain, application/json)
    :raises: HTTPNotAcceptable if the requested content type is not acceptable
    :raises: HTTPBadRequest if the 'format' query param is provided and
             not valid UTF-8
    """
    query_format = get_param(req, 'format')
    if query_format:
        req.accept = FORMAT2CONTENT_TYPE.get(
            query_format.lower(), FORMAT2CONTENT_TYPE['plain'])
    out_content_type = req.accept.best_match(
        ['text/plain', 'application/json', 'application/xml', 'text/xml'])
    if not out_content_type:
        raise HTTPNotAcceptable(request=req)
    return out_content_type


def split_and_validate_path(request, minsegs=1, maxsegs=None,
                            rest_with_last=False):
    """
    Utility function to split and validate the request path.

    :returns: result of split_path if everything's okay
    :raises: HTTPBadRequest if something's not okay
    """
    try:
        segs = split_path(unquote(request.path),
                          minsegs, maxsegs, rest_with_last)
        validate_device_partition(segs[0], segs[1])
        return segs
    except ValueError as err:
        raise HTTPBadRequest(body=str(err), request=request,
                             content_type='text/plain')


def is_user_meta(server_type, key):
    """
    Tests if a header key starts with and is longer than the user
    metadata prefix for given server type.

    :param server_type: type of backend server i.e. [account|container|object]
    :param key: header key
    :returns: True if the key satisfies the test, False otherwise
    """
    if len(key) <= 8 + len(server_type):
        return False
    return key.lower().startswith(get_user_meta_prefix(server_type))


def is_sys_meta(server_type, key):
    """
    Tests if a header key starts with and is longer than the system
    metadata prefix for given server type.

    :param server_type: type of backend server i.e. [account|container|object]
    :param key: header key
    :returns: True if the key satisfies the test, False otherwise
    """
    if len(key) <= 11 + len(server_type):
        return False
    return key.lower().startswith(get_sys_meta_prefix(server_type))


def is_sys_or_user_meta(server_type, key):
    """
    Tests if a header key starts with and is longer than the user or system
    metadata prefix for given server type.

    :param server_type: type of backend server i.e. [account|container|object]
    :param key: header key
    :returns: True if the key satisfies the test, False otherwise
    """
    return is_user_meta(server_type, key) or is_sys_meta(server_type, key)


def strip_user_meta_prefix(server_type, key):
    """
    Removes the user metadata prefix for a given server type from the start
    of a header key.

    :param server_type: type of backend server i.e. [account|container|object]
    :param key: header key
    :returns: stripped header key
    """
    return key[len(get_user_meta_prefix(server_type)):]


def strip_sys_meta_prefix(server_type, key):
    """
    Removes the system metadata prefix for a given server type from the start
    of a header key.

    :param server_type: type of backend server i.e. [account|container|object]
    :param key: header key
    :returns: stripped header key
    """
    return key[len(get_sys_meta_prefix(server_type)):]


def get_user_meta_prefix(server_type):
    """
    Returns the prefix for user metadata headers for given server type.

    This prefix defines the namespace for headers that will be persisted
    by backend servers.

    :param server_type: type of backend server i.e. [account|container|object]
    :returns: prefix string for server type's user metadata headers
    """
    return 'x-%s-%s-' % (server_type.lower(), 'meta')


def get_sys_meta_prefix(server_type):
    """
    Returns the prefix for system metadata headers for given server type.

    This prefix defines the namespace for headers that will be persisted
    by backend servers.

    :param server_type: type of backend server i.e. [account|container|object]
    :returns: prefix string for server type's system metadata headers
    """
    return 'x-%s-%s-' % (server_type.lower(), 'sysmeta')


def remove_items(headers, condition):
    """
    Removes items from a dict whose keys satisfy
    the given condition.

    :param headers: a dict of headers
    :param condition: a function that will be passed the header key as a
                      single argument and should return True if the header
                      is to be removed.
    :returns: a dict, possibly empty, of headers that have been removed
    """
    removed = {}
    keys = filter(condition, headers)
    removed.update((key, headers.pop(key)) for key in keys)
    return removed


def close_if_possible(maybe_closable):
    close_method = getattr(maybe_closable, 'close', None)
    if callable(close_method):
        return close_method()


@contextmanager
def closing_if_possible(maybe_closable):
    """
    Like contextlib.closing(), but doesn't crash if the object lacks a close()
    method.

    PEP 333 (WSGI) says: "If the iterable returned by the application has a
    close() method, the server or gateway must call that method upon
    completion of the current request[.]" This function makes that easier.
    """
    yield maybe_closable
    close_if_possible(maybe_closable)


class SegmentedIterable(object):
    """
    Iterable that returns the object contents for a large object.

    :param req: original request object
    :param app: WSGI application from which segments will come
    :param listing_iter: iterable yielding the object segments to fetch,
                         along with the byte subranges to fetch, in the
                         form of a tuple (object-path, first-byte, last-byte)
                         or (object-path, None, None) to fetch the whole thing.
    :param max_get_time: maximum permitted duration of a GET request (seconds)
    :param logger: logger object
    :param swift_source: value of swift.source in subrequest environ
                         (just for logging)
    :param ua_suffix: string to append to user-agent.
    :param name: name of manifest (used in logging only)
    :param response: optional response object for the response being sent
                     to the client.
    """
    def __init__(self, req, app, listing_iter, max_get_time,
                 logger, ua_suffix, swift_source,
                 name='<not specified>', response=None):
        self.req = req
        self.app = app
        self.listing_iter = listing_iter
        self.max_get_time = max_get_time
        self.logger = logger
        self.ua_suffix = " " + ua_suffix
        self.swift_source = swift_source
        self.name = name
        self.response = response

    def app_iter_range(self, *a, **kw):
        """
        swob.Response will only respond with a 206 status in certain cases; one
        of those is if the body iterator responds to .app_iter_range().

        However, this object (or really, its listing iter) is smart enough to
        handle the range stuff internally, so we just no-op this out for swob.
        """
        return self

    def __iter__(self):
        start_time = time.time()
        have_yielded_data = False

        if self.response and self.response.content_length:
            bytes_left = int(self.response.content_length)
        else:
            bytes_left = None

        try:
            for seg_path, seg_etag, seg_size, first_byte, last_byte \
                    in self.listing_iter:
                if time.time() - start_time > self.max_get_time:
                    raise SegmentError(
                        'ERROR: While processing manifest %s, '
                        'max LO GET time of %ds exceeded' %
                        (self.name, self.max_get_time))
                seg_req = make_subrequest(
                    self.req.environ, path=seg_path, method='GET',
                    headers={'x-auth-token': self.req.headers.get(
                        'x-auth-token')},
                    agent=('%(orig)s ' + self.ua_suffix),
                    swift_source=self.swift_source)
                if first_byte is not None or last_byte is not None:
                    seg_req.headers['Range'] = "bytes=%s-%s" % (
                        # The 0 is to avoid having a range like "bytes=-10",
                        # which actually means the *last* 10 bytes.
                        '0' if first_byte is None else first_byte,
                        '' if last_byte is None else last_byte)

                seg_resp = seg_req.get_response(self.app)
                if not is_success(seg_resp.status_int):
                    close_if_possible(seg_resp.app_iter)
                    raise SegmentError(
                        'ERROR: While processing manifest %s, '
                        'got %d while retrieving %s' %
                        (self.name, seg_resp.status_int, seg_path))

                elif ((seg_etag and (seg_resp.etag != seg_etag)) or
                        (seg_size and (seg_resp.content_length != seg_size) and
                         not seg_req.range)):
                    # The content-length check is for security reasons. Seems
                    # possible that an attacker could upload a >1mb object and
                    # then replace it with a much smaller object with same
                    # etag. Then create a big nested SLO that calls that
                    # object many times which would hammer our obj servers. If
                    # this is a range request, don't check content-length
                    # because it won't match.
                    close_if_possible(seg_resp.app_iter)
                    raise SegmentError(
                        'Object segment no longer valid: '
                        '%(path)s etag: %(r_etag)s != %(s_etag)s or '
                        '%(r_size)s != %(s_size)s.' %
                        {'path': seg_req.path, 'r_etag': seg_resp.etag,
                         'r_size': seg_resp.content_length,
                         's_etag': seg_etag,
                         's_size': seg_size})

                for chunk in seg_resp.app_iter:
                    have_yielded_data = True
                    if bytes_left is None:
                        yield chunk
                    elif bytes_left >= len(chunk):
                        yield chunk
                        bytes_left -= len(chunk)
                    else:
                        yield chunk[:bytes_left]
                        bytes_left -= len(chunk)
                        close_if_possible(seg_resp.app_iter)
                        raise SegmentError(
                            'Too many bytes for %(name)s; truncating in '
                            '%(seg)s with %(left)d bytes left' %
                            {'name': self.name, 'seg': seg_req.path,
                             'left': bytes_left})
                close_if_possible(seg_resp.app_iter)

            if bytes_left:
                raise SegmentError(
                    'Not enough bytes for %s; closing connection' %
                    self.name)

        except ListingIterError as err:
            # I have to save this error because yielding the ' ' below clears
            # the exception from the current stack frame.
            excinfo = sys.exc_info()
            self.logger.exception('ERROR: While processing manifest %s, %s',
                                  self.name, err)
            # Normally, exceptions before any data has been yielded will
            # cause Eventlet to send a 5xx response. In this particular
            # case of ListingIterError we don't want that and we'd rather
            # just send the normal 2xx response and then hang up early
            # since 5xx codes are often used to judge Service Level
            # Agreements and this ListingIterError indicates the user has
            # created an invalid condition.
            if not have_yielded_data:
                yield ' '
            raise excinfo
        except SegmentError as err:
            self.logger.exception(err)
            # This doesn't actually change the response status (we're too
            # late for that), but this does make it to the logs.
            if self.response:
                self.response.status = HTTP_SERVICE_UNAVAILABLE
            raise
