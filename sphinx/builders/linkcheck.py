"""The CheckExternalLinksBuilder class."""

from __future__ import annotations

import contextlib
import json
import re
import socket
import time
from enum import StrEnum
from html.parser import HTMLParser
from queue import PriorityQueue, Queue
from threading import Thread
from typing import TYPE_CHECKING, NamedTuple, cast
from urllib.parse import quote, unquote, urlparse, urlsplit, urlunparse

from docutils import nodes
from requests.exceptions import (
    ConnectionError,  # NoQA: A004
    HTTPError,
    SSLError,
    TooManyRedirects,
)
from requests.exceptions import Timeout as RequestTimeout

from sphinx._cli.util.colour import darkgray, darkgreen, purple, red, turquoise
from sphinx.builders.dummy import DummyBuilder
from sphinx.errors import ConfigError
from sphinx.locale import __
from sphinx.transforms.post_transforms import SphinxPostTransform
from sphinx.util import logging, requests
from sphinx.util._uri import encode_uri
from sphinx.util.http_date import rfc1123_to_epoch
from sphinx.util.nodes import get_node_line

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from typing import Any, Literal, TypeAlias

    from requests import Response

    from sphinx.application import Sphinx
    from sphinx.config import Config
    from sphinx.util._pathlib import _StrPath
    from sphinx.util.typing import ExtensionMetadata

    _URIProperties: TypeAlias = tuple['_Status', str, int]


class _Status(StrEnum):
    BROKEN = 'broken'
    IGNORED = 'ignored'
    RATE_LIMITED = 'rate-limited'
    REDIRECTED = 'redirected'
    TIMEOUT = 'timeout'
    UNCHECKED = 'unchecked'
    UNKNOWN = 'unknown'
    WORKING = 'working'


logger = logging.getLogger(__name__)

# matches to foo:// and // (a protocol relative URL)
uri_re = re.compile('([a-z]+:)?//')

DEFAULT_REQUEST_HEADERS = {
    'Accept': 'text/html,application/xhtml+xml;q=0.9,*/*;q=0.8',
}
CHECK_IMMEDIATELY = 0
QUEUE_POLL_SECS = 1
DEFAULT_DELAY = 60.0


class CheckExternalLinksBuilder(DummyBuilder):
    """Checks for broken external links."""

    name = 'linkcheck'
    epilog = __('Look for any errors in the above output or in %(outdir)s/output.txt')

    def init(self) -> None:
        self.broken_hyperlinks = 0
        self.timed_out_hyperlinks = 0
        self.hyperlinks: dict[str, Hyperlink] = {}
        # set a timeout for non-responding servers
        socket.setdefaulttimeout(5.0)

    def finish(self) -> None:
        checker = HyperlinkAvailabilityChecker(self.config)
        logger.info('')

        output_text = self.outdir / 'output.txt'
        output_json = self.outdir / 'output.json'
        with (
            open(output_text, 'w', encoding='utf-8') as self.txt_outfile,
            open(output_json, 'w', encoding='utf-8') as self.json_outfile,
        ):
            for result in checker.check(self.hyperlinks):
                self.process_result(result)

        if self.broken_hyperlinks or self.timed_out_hyperlinks:
            self._app.statuscode = 1

    def process_result(self, result: CheckResult) -> None:
        filename = self.env.doc2path(result.docname, False)
        res_uri = result.uri

        linkstat: dict[str, str | int | _Status] = {
            'filename': str(filename),
            'lineno': result.lineno,
            'status': result.status,
            'code': result.code,
            'uri': res_uri,
            'info': result.message,
        }
        self.write_linkstat(linkstat)

        if result.lineno and result.status != _Status.UNCHECKED:
            # unchecked links are not logged
            logger.info('(%16s: line %4d) ', result.docname, result.lineno, nonl=True)

        match result.status:
            case _Status.RATE_LIMITED | _Status.UNCHECKED:
                pass
            case _Status.IGNORED:
                if result.message:
                    msg = f'{res_uri}: {result.message}'
                else:
                    msg = res_uri
                logger.info(darkgray('-ignored- ') + msg)  # NoQA: G003
            case _Status.WORKING:
                logger.info(darkgreen('ok        ') + f'{res_uri}{result.message}')  # NoQA: G003
            case _Status.TIMEOUT:
                if self.config.verbosity < 0:
                    msg = 'timeout   ' + f'{res_uri}{result.message}'
                    logger.warning(msg, location=(result.docname, result.lineno))
                else:
                    msg = red('timeout   ') + res_uri + red(f' - {result.message}')
                    logger.info(msg)
                self.write_entry(
                    _Status.TIMEOUT,
                    result.docname,
                    filename,
                    result.lineno,
                    f'{res_uri}: {result.message}',
                )
                self.timed_out_hyperlinks += 1
            case _Status.BROKEN:
                if self.config.verbosity < 0:
                    logger.warning(
                        __('broken link: %s (%s)'),
                        res_uri,
                        result.message,
                        location=(result.docname, result.lineno),
                    )
                else:
                    msg = red('broken    ') + res_uri + red(f' - {result.message}')
                    logger.info(msg)
                self.write_entry(
                    _Status.BROKEN,
                    result.docname,
                    filename,
                    result.lineno,
                    f'{res_uri}: {result.message}',
                )
                self.broken_hyperlinks += 1
            case _Status.REDIRECTED:
                match result.code:
                    case 301:
                        text = 'permanently'
                    case 302:
                        text = 'with Found'
                    case 303:
                        text = 'with See Other'
                    case 307:
                        text = 'temporarily'
                    case 308:
                        text = 'permanently'
                    case _:
                        text = 'with unknown code'
                linkstat['text'] = text
                redirection = f'{text} to {result.message}'
                if self.config.linkcheck_allowed_redirects is not None:
                    msg = f'redirect  {res_uri} - {redirection}'
                    logger.warning(msg, location=(result.docname, result.lineno))
                else:
                    colour = turquoise if result.code == 307 else purple
                    msg = colour('redirect  ') + res_uri + colour(f' - {redirection}')
                    logger.info(msg)
                self.write_entry(
                    f'redirected {text}',
                    result.docname,
                    filename,
                    result.lineno,
                    f'{res_uri} to {result.message}',
                )
            case _Status.UNKNOWN:
                msg = 'Unknown status.'
                raise ValueError(msg)

    def write_linkstat(self, data: dict[str, str | int | _Status]) -> None:
        self.json_outfile.write(json.dumps(data))
        self.json_outfile.write('\n')

    def write_entry(
        self, what: _Status | str, docname: str, filename: _StrPath, line: int, uri: str
    ) -> None:
        self.txt_outfile.write(f'{filename}:{line}: [{what}] {uri}\n')


class HyperlinkCollector(SphinxPostTransform):
    builders = ('linkcheck',)
    default_priority = 800

    def run(self, **kwargs: Any) -> None:
        for node in self.document.findall():
            if uri := self.find_uri(node):
                self._add_uri(uri, node)

    def find_uri(self, node: nodes.Element) -> str | None:
        """Find a URI for a given node.

        This call can be used to retrieve a URI from a provided node. If no
        URI exists for a provided node, this call will return ``None``.

        This method can be useful for extension developers who wish to
        easily inject hyperlinks into a builder by only needing to override
        this method.

        :param node: A node class
        :returns: URI of the node
        """
        # reference nodes
        if isinstance(node, nodes.reference):
            if 'refuri' in node:
                return node['refuri']

        # image nodes
        if isinstance(node, nodes.image):
            uri = node['candidates'].get('?')
            if uri and '://' in uri:
                return uri

        # raw nodes
        if isinstance(node, nodes.raw):
            uri = node.get('source')
            if uri and '://' in uri:
                return uri

        return None

    def _add_uri(self, uri: str, node: nodes.Element) -> None:
        """Registers a node's URI into a builder's collection of hyperlinks.

        Provides the ability to register a URI value determined from a node
        into the linkcheck's builder. URI's processed through this call can
        be manipulated through a ``linkcheck-process-uri`` event before the
        builder attempts to validate.

        :param uri: URI to add
        :param node: A node class where the URI was found
        """
        builder = cast('CheckExternalLinksBuilder', self.env._app.builder)
        hyperlinks = builder.hyperlinks
        docname = self.env.current_document.docname

        if newuri := self.env.events.emit_firstresult('linkcheck-process-uri', uri):
            uri = newuri

        try:
            lineno = get_node_line(node)
        except ValueError:
            lineno = -1

        if uri not in hyperlinks:
            hyperlinks[uri] = Hyperlink(
                uri, docname, self.env.doc2path(docname), lineno
            )


class Hyperlink(NamedTuple):
    uri: str
    docname: str
    docpath: _StrPath
    lineno: int


class HyperlinkAvailabilityChecker:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.rate_limits: dict[str, RateLimit] = {}
        self.rqueue: Queue[CheckResult] = Queue()
        self.workers: list[Thread] = []
        self.wqueue: PriorityQueue[CheckRequest] = PriorityQueue()
        self.num_workers: int = config.linkcheck_workers

        self.to_ignore: list[re.Pattern[str]] = list(
            map(re.compile, self.config.linkcheck_ignore)
        )

    def check(self, hyperlinks: dict[str, Hyperlink]) -> Iterator[CheckResult]:
        self.invoke_threads()

        total_links = 0
        for hyperlink in hyperlinks.values():
            if self.is_ignored_uri(hyperlink.uri):
                yield CheckResult(
                    uri=hyperlink.uri,
                    docname=hyperlink.docname,
                    lineno=hyperlink.lineno,
                    status=_Status.IGNORED,
                    message='',
                    code=0,
                )
            else:
                self.wqueue.put(CheckRequest(CHECK_IMMEDIATELY, hyperlink), False)
                total_links += 1

        done = 0
        while done < total_links:
            yield self.rqueue.get()
            done += 1

        self.shutdown_threads()

    def invoke_threads(self) -> None:
        for _i in range(self.num_workers):
            thread = HyperlinkAvailabilityCheckWorker(
                self.config, self.rqueue, self.wqueue, self.rate_limits
            )
            thread.start()
            self.workers.append(thread)

    def shutdown_threads(self) -> None:
        self.wqueue.join()
        for _worker in self.workers:
            self.wqueue.put(CheckRequest(CHECK_IMMEDIATELY, None), False)

    def is_ignored_uri(self, uri: str) -> bool:
        return any(pat.match(uri) for pat in self.to_ignore)


class CheckRequest(NamedTuple):
    next_check: float
    hyperlink: Hyperlink | None


class CheckResult(NamedTuple):
    uri: str
    docname: str
    lineno: int
    status: _Status
    message: str
    code: int


class HyperlinkAvailabilityCheckWorker(Thread):
    """A worker class for checking the availability of hyperlinks."""

    def __init__(
        self,
        config: Config,
        rqueue: Queue[CheckResult],
        wqueue: Queue[CheckRequest],
        rate_limits: dict[str, RateLimit],
    ) -> None:
        self.rate_limits = rate_limits
        self.rqueue = rqueue
        self.wqueue = wqueue

        self.anchors_ignore: list[re.Pattern[str]] = list(
            map(re.compile, config.linkcheck_anchors_ignore)
        )
        self.anchors_ignore_for_url: list[re.Pattern[str]] = list(
            map(re.compile, config.linkcheck_anchors_ignore_for_url)
        )
        self.documents_exclude: list[re.Pattern[str]] = list(
            map(re.compile, config.linkcheck_exclude_documents)
        )
        self.auth = [
            (re.compile(pattern), auth_info)
            for pattern, auth_info in config.linkcheck_auth
        ]

        self.timeout: int | float | None = config.linkcheck_timeout
        self.request_headers: dict[str, dict[str, str]] = (
            config.linkcheck_request_headers
        )
        self.check_anchors: bool = config.linkcheck_anchors
        self.allowed_redirects: dict[re.Pattern[str], re.Pattern[str]]
        self.allowed_redirects = config.linkcheck_allowed_redirects or {}
        self.retries: int = config.linkcheck_retries
        self.rate_limit_timeout = config.linkcheck_rate_limit_timeout
        self._allow_unauthorized = config.linkcheck_allow_unauthorized
        self._timeout_status: Literal[_Status.BROKEN, _Status.TIMEOUT]
        if config.linkcheck_report_timeouts_as_broken:
            self._timeout_status = _Status.BROKEN
        else:
            self._timeout_status = _Status.TIMEOUT

        self.user_agent = config.user_agent
        self.tls_verify = config.tls_verify
        self.tls_cacerts = config.tls_cacerts

        self._session = requests._Session(
            _ignored_redirects=tuple(map(re.compile, config.linkcheck_ignore))
        )

        super().__init__(daemon=True)

    def run(self) -> None:
        while True:
            next_check, hyperlink = self.wqueue.get()
            if hyperlink is None:
                # An empty hyperlink is a signal to shutdown the worker; cleanup resources here
                self._session.close()
                break

            uri, docname, _docpath, lineno = hyperlink
            if uri is None:
                break

            netloc = urlsplit(uri).netloc
            with contextlib.suppress(KeyError):
                # Refresh rate limit.
                # When there are many links in the queue, workers are all stuck waiting
                # for responses, but the builder keeps queuing. Links in the queue may
                # have been queued before rate limits were discovered.
                next_check = self.rate_limits[netloc].next_check
            if next_check > time.time():
                # Sleep before putting message back in the queue to avoid
                # waking up other threads.
                time.sleep(QUEUE_POLL_SECS)
                self.wqueue.put(CheckRequest(next_check, hyperlink), False)
                self.wqueue.task_done()
                continue
            status, info, code = self._check(docname, uri, hyperlink)
            if status == _Status.RATE_LIMITED:
                logger.info(
                    darkgray('-rate limited-   ') + uri + darkgray(' | sleeping...')  # NoQA: G003
                )
            else:
                self.rqueue.put(CheckResult(uri, docname, lineno, status, info, code))
            self.wqueue.task_done()

    def _check(self, docname: str, uri: str, hyperlink: Hyperlink) -> _URIProperties:
        # check for various conditions without bothering the network

        for doc_matcher in self.documents_exclude:
            if doc_matcher.match(docname):
                info = (
                    f'{docname} matched {doc_matcher.pattern} from '
                    'linkcheck_exclude_documents'
                )
                return _Status.IGNORED, info, 0

        if len(uri) == 0 or uri.startswith(('#', 'mailto:', 'tel:')):
            return _Status.UNCHECKED, '', 0
        if not uri.startswith(('http:', 'https:')):
            if uri_re.match(uri):
                # Non-supported URI schemes (ex. ftp)
                return _Status.UNCHECKED, '', 0

            if (hyperlink.docpath.parent / uri).exists():
                return _Status.WORKING, '', 0
            return _Status.BROKEN, '', 0

        # need to actually check the URI
        status: _Status
        status, info, code = _Status.UNKNOWN, '', 0
        for _ in range(self.retries):
            status, info, code = self._check_uri(uri, hyperlink)
            if status != _Status.BROKEN:
                break

        return status, info, code

    def _retrieval_methods(
        self,
        check_anchors: bool,
        anchor: str,
    ) -> Iterator[tuple[Callable[..., Response], dict[str, bool]]]:
        if not check_anchors or not anchor:
            yield self._session.head, {'allow_redirects': True}
        yield self._session.get, {'stream': True}

    def _check_uri(self, uri: str, hyperlink: Hyperlink) -> _URIProperties:
        req_url, delimiter, anchor = uri.partition('#')
        if delimiter and anchor:
            for rex in self.anchors_ignore:
                if rex.match(anchor):
                    anchor = ''
                    break
            else:
                for rex in self.anchors_ignore_for_url:
                    if rex.match(req_url):
                        anchor = ''
                        break
            anchor = unquote(anchor)

        # handle non-ASCII URIs
        try:
            req_url.encode('ascii')
        except UnicodeError:
            req_url = encode_uri(req_url)

        # Get auth info, if any
        for pattern, auth_info in self.auth:  # NoQA: B007 (false positive)
            if pattern.match(uri):
                break
        else:
            auth_info = None

        # update request headers for the URL
        headers = _get_request_headers(uri, self.request_headers)

        # Linkcheck HTTP request logic:
        #
        # - Attempt HTTP HEAD before HTTP GET unless page content is required.
        # - Follow server-issued HTTP redirects.
        # - Respect server-issued HTTP 429 back-offs.
        error_message = ''
        status_code = -1
        response_url = retry_after = ''
        for retrieval_method, kwargs in self._retrieval_methods(
            self.check_anchors, anchor
        ):
            try:
                with retrieval_method(
                    url=req_url,
                    auth=auth_info,
                    headers=headers,
                    timeout=self.timeout,
                    **kwargs,
                    _user_agent=self.user_agent,
                    _tls_info=(self.tls_verify, self.tls_cacerts),
                ) as response:
                    if anchor and self.check_anchors and response.ok:
                        try:
                            found = contains_anchor(response, anchor)
                        except UnicodeDecodeError:
                            return (
                                _Status.IGNORED,
                                'unable to decode response content',
                                0,
                            )
                        if not found:
                            return (
                                _Status.BROKEN,
                                __("Anchor '%s' not found") % quote(anchor),
                                0,
                            )

                # Copy data we need from the (closed) response
                status_code = response.status_code
                redirect_status_code = (
                    response.history[-1].status_code if response.history else None
                )
                retry_after = response.headers.get('Retry-After', '')
                response_url = f'{response.url}'
                response.raise_for_status()
                del response
                break

            except RequestTimeout as err:
                return self._timeout_status, str(err), 0

            except SSLError as err:
                # SSL failure; report that the link is broken.
                return _Status.BROKEN, str(err), 0

            except (ConnectionError, TooManyRedirects) as err:
                # Servers drop the connection on HEAD requests, causing
                # ConnectionError.
                error_message = str(err)
                continue

            except requests._IgnoredRedirection as err:
                # A redirection to an ignored URI was attempted; report it appropriately
                return (
                    _Status.IGNORED,
                    f'ignored redirect: {err.destination}',
                    err.status_code,
                )

            except HTTPError as err:
                error_message = str(err)

                # Unauthorized: the client did not provide required credentials
                if status_code == 401:
                    if self._allow_unauthorized:
                        return _Status.WORKING, 'unauthorized', 0
                    else:
                        return _Status.BROKEN, 'unauthorized', 0

                # Rate limiting; back-off if allowed, or report failure otherwise
                if status_code == 429:
                    if next_check := self.limit_rate(response_url, retry_after):
                        self.wqueue.put(CheckRequest(next_check, hyperlink), False)
                        return _Status.RATE_LIMITED, '', 0
                    return _Status.BROKEN, error_message, 0

                # Don't claim success/failure during server-side outages
                if status_code == 503:
                    return _Status.IGNORED, 'service unavailable', 0

                # For most HTTP failures, continue attempting alternate retrieval methods
                continue

            except Exception as err:
                # Unhandled exception (intermittent or permanent); report that
                # the link is broken.
                return _Status.BROKEN, str(err), 0

        else:
            # All available retrieval methods have been exhausted; report
            # that the link is broken.
            return _Status.BROKEN, error_message, 0

        # Success; clear rate limits for the origin
        netloc = urlsplit(req_url).netloc
        self.rate_limits.pop(netloc, None)

        if (
            (response_url.rstrip('/') == req_url.rstrip('/'))
            or _allowed_redirect(req_url, response_url, self.allowed_redirects)
        ):  # fmt: skip
            return _Status.WORKING, '', 0
        elif redirect_status_code is not None:
            return _Status.REDIRECTED, response_url, redirect_status_code
        else:
            return _Status.REDIRECTED, response_url, 0

    def limit_rate(self, response_url: str, retry_after: str | None) -> float | None:
        delay = DEFAULT_DELAY
        next_check = None
        if retry_after:
            try:
                # Integer: time to wait before next attempt.
                delay = float(retry_after)
            except ValueError:
                try:
                    # An HTTP-date: time of next attempt.
                    next_check = rfc1123_to_epoch(retry_after)
                except (ValueError, TypeError):
                    # TypeError: Invalid date format.
                    # ValueError: Invalid date, e.g. Oct 52th.
                    pass
                else:
                    delay = next_check - time.time()
            else:
                next_check = time.time() + delay
        netloc = urlsplit(response_url).netloc
        if next_check is None:
            max_delay = self.rate_limit_timeout
            try:
                rate_limit = self.rate_limits[netloc]
            except KeyError:
                delay = DEFAULT_DELAY
            else:
                last_wait_time = rate_limit.delay
                delay = 2.0 * last_wait_time
                if delay > max_delay > last_wait_time:
                    delay = max_delay
            if delay > max_delay:
                return None
            next_check = time.time() + delay
        self.rate_limits[netloc] = RateLimit(delay, next_check)
        return next_check


def _get_request_headers(
    uri: str,
    request_headers: dict[str, dict[str, str]],
) -> dict[str, str]:
    url = urlsplit(uri)
    candidates = (
        f'{url.scheme}://{url.netloc}',
        f'{url.scheme}://{url.netloc}/',
        uri,
        '*',
    )

    for u in candidates:
        if u in request_headers:
            return {**DEFAULT_REQUEST_HEADERS, **request_headers[u]}
    return {}


def contains_anchor(response: Response, anchor: str) -> bool:
    """Determine if an anchor is contained within an HTTP response."""
    parser = AnchorCheckParser(anchor)
    # Read file in chunks. If we find a matching anchor, we break
    # the loop early in hopes not to have to download the whole thing.
    for chunk in response.iter_content(chunk_size=4096, decode_unicode=True):
        if isinstance(chunk, bytes):
            # requests failed to decode, manually try to decode it
            chunk = chunk.decode()

        parser.feed(chunk)
        if parser.found:
            break
    parser.close()
    return parser.found


class AnchorCheckParser(HTMLParser):
    """Specialised HTML parser that looks for a specific anchor."""

    def __init__(self, search_anchor: str) -> None:
        super().__init__()

        self.search_anchor = search_anchor
        self.found = False

    def handle_starttag(self, tag: Any, attrs: Any) -> None:
        for key, value in attrs:
            if key in {'id', 'name'} and value == self.search_anchor:
                self.found = True
                break


def _allowed_redirect(
    url: str, new_url: str, allowed_redirects: dict[re.Pattern[str], re.Pattern[str]]
) -> bool:
    return any(
        from_url.match(url) and to_url.match(new_url)
        for from_url, to_url in allowed_redirects.items()
    )


class RateLimit(NamedTuple):
    delay: float
    next_check: float


def rewrite_github_anchor(app: Sphinx, uri: str) -> str | None:
    """Rewrite anchor name of the hyperlink to github.com

    The hyperlink anchors in github.com are dynamically generated.  This rewrites
    them before checking and makes them comparable.
    """
    parsed = urlparse(uri)
    if parsed.hostname == 'github.com' and parsed.fragment:
        prefixed = parsed.fragment.startswith('user-content-')
        if not prefixed:
            fragment = f'user-content-{parsed.fragment}'
            return urlunparse(parsed._replace(fragment=fragment))
    return None


def compile_linkcheck_allowed_redirects(app: Sphinx, config: Config) -> None:
    """Compile patterns to the regexp objects."""
    if config.linkcheck_allowed_redirects is _sentinel_lar:
        config.linkcheck_allowed_redirects = None
        return
    if not isinstance(config.linkcheck_allowed_redirects, dict):
        msg = __(
            f'Invalid value `{config.linkcheck_allowed_redirects!r}` in '
            'linkcheck_allowed_redirects. Expected a dictionary.'
        )
        raise ConfigError(msg)
    allowed_redirects = {}
    for url, pattern in config.linkcheck_allowed_redirects.items():
        try:
            allowed_redirects[re.compile(url)] = re.compile(pattern)
        except re.error as exc:
            logger.warning(
                __('Failed to compile regex in linkcheck_allowed_redirects: %r %s'),
                exc.pattern,
                exc.msg,
            )
    config.linkcheck_allowed_redirects = allowed_redirects


_sentinel_lar = object()


def setup(app: Sphinx) -> ExtensionMetadata:
    app.add_builder(CheckExternalLinksBuilder)
    app.add_post_transform(HyperlinkCollector)

    app.add_config_value('linkcheck_ignore', [], '', types=frozenset({list, tuple}))
    app.add_config_value(
        'linkcheck_exclude_documents', [], '', types=frozenset({list, tuple})
    )
    app.add_config_value(
        'linkcheck_allowed_redirects', _sentinel_lar, '', types=frozenset({dict})
    )
    app.add_config_value('linkcheck_auth', [], '', types=frozenset({list, tuple}))
    app.add_config_value('linkcheck_request_headers', {}, '', types=frozenset({dict}))
    app.add_config_value('linkcheck_retries', 1, '', types=frozenset({int}))
    app.add_config_value('linkcheck_timeout', 30, '', types=frozenset({float, int}))
    app.add_config_value('linkcheck_workers', 5, '', types=frozenset({int}))
    app.add_config_value('linkcheck_anchors', True, '', types=frozenset({bool}))
    # Anchors starting with ! are ignored since they are
    # commonly used for dynamic pages
    app.add_config_value(
        'linkcheck_anchors_ignore', ['^!'], '', types=frozenset({list, tuple})
    )
    app.add_config_value(
        'linkcheck_anchors_ignore_for_url', (), '', types=frozenset({list, tuple})
    )
    app.add_config_value(
        'linkcheck_rate_limit_timeout', 300.0, '', types=frozenset({float, int})
    )
    app.add_config_value(
        'linkcheck_allow_unauthorized', False, '', types=frozenset({bool})
    )
    app.add_config_value(
        'linkcheck_report_timeouts_as_broken', False, '', types=frozenset({bool})
    )

    app.add_event('linkcheck-process-uri')

    # priority 900 to happen after ``check_confval_types()``
    app.connect('config-inited', compile_linkcheck_allowed_redirects, priority=900)

    # FIXME: Disable URL rewrite handler for github.com temporarily.
    # See: https://github.com/sphinx-doc/sphinx/issues/9435
    # app.connect('linkcheck-process-uri', rewrite_github_anchor)

    return {
        'version': 'builtin',
        'parallel_read_safe': True,
        'parallel_write_safe': True,
    }
