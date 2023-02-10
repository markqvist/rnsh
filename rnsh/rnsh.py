#!/usr/bin/env python3

# MIT License
#
# Copyright (c) 2016-2022 Mark Qvist / unsigned.io
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

from __future__ import annotations
import functools
from typing import Callable, TypeVar
import termios
import rnslogging
import RNS
import shlex
import time
import sys
import os
import datetime
import base64
import process
import asyncio
import threading
import signal
import retry
from multiprocessing.pool import ThreadPool
from __version import __version__
import logging as __logging

module_logger = __logging.getLogger(__name__)


def _get_logger(name: str):
    global module_logger
    return module_logger.getChild(name)


APP_NAME = "rnsh"
_identity = None
_reticulum = None
_allow_all = False
_allowed_identity_hashes = []
_cmd: [str] = None
DATA_AVAIL_MSG = "data available"
_finished: asyncio.Event | None = None
_retry_timer = retry.RetryThread()
_destination: RNS.Destination | None = None
_pool: ThreadPool = ThreadPool(10)
_loop: asyncio.AbstractEventLoop | None = None

async def _check_finished(timeout: float = 0):
        await process.event_wait(_finished, timeout=timeout)


def _sigint_handler(signal, frame):
    global _finished
    log = _get_logger("_sigint_handler")
    log.debug("SIGINT")
    if _finished is not None:
        _finished.set()
    else:
        raise KeyboardInterrupt()


signal.signal(signal.SIGINT, _sigint_handler)


def _prepare_identity(identity_path):
    global _identity
    log = _get_logger("_prepare_identity")
    if identity_path is None:
        identity_path = RNS.Reticulum.identitypath + "/" + APP_NAME

    if os.path.isfile(identity_path):
        _identity = RNS.Identity.from_file(identity_path)

    if _identity is None:
        log.info("No valid saved identity found, creating new...")
        _identity = RNS.Identity()
        _identity.to_file(identity_path)

def _print_identity(configdir, identitypath, service_name, include_destination: bool):
    _reticulum = RNS.Reticulum(configdir=configdir, loglevel=RNS.LOG_INFO)
    _prepare_identity(identitypath)
    destination = RNS.Destination(_identity, RNS.Destination.IN, RNS.Destination.SINGLE, APP_NAME, service_name)
    print("Identity     : " + str(_identity))
    if include_destination:
        print("Listening on : " + RNS.prettyhexrep(destination.hash))
    exit(0)


async def _listen(configdir, command, identitypath=None, service_name="default", verbosity=0, quietness=0,
                  allowed=None, disable_auth=None, disable_announce=False):
    global _identity, _allow_all, _allowed_identity_hashes, _reticulum, _cmd, _destination
    log = _get_logger("_listen")
    _cmd = command

    targetloglevel = 3 + verbosity - quietness
    _reticulum = RNS.Reticulum(configdir=configdir, loglevel=targetloglevel)
    _prepare_identity(identitypath)
    _destination = RNS.Destination(_identity, RNS.Destination.IN, RNS.Destination.SINGLE, APP_NAME, service_name)

    if disable_auth:
        _allow_all = True
    else:
        if allowed is not None:
            for a in allowed:
                try:
                    dest_len = (RNS.Reticulum.TRUNCATED_HASHLENGTH // 8) * 2
                    if len(a) != dest_len:
                        raise ValueError(
                            "Allowed destination length is invalid, must be {hex} hexadecimal characters ({byte} bytes).".format(
                                hex=dest_len, byte=dest_len // 2))
                    try:
                        destination_hash = bytes.fromhex(a)
                        _allowed_identity_hashes.append(destination_hash)
                    except Exception as e:
                        raise ValueError("Invalid destination entered. Check your input.")
                except Exception as e:
                    log.error(str(e))
                    exit(1)

    if len(_allowed_identity_hashes) < 1 and not disable_auth:
        log.warning("Warning: No allowed identities configured, rnsh will not accept any connections!")

    _destination.set_link_established_callback(_listen_link_established)

    if not _allow_all:
        _destination.register_request_handler(
            path="data",
            response_generator=_listen_request,
            allow=RNS.Destination.ALLOW_LIST,
            allowed_list=_allowed_identity_hashes
        )
    else:
        _destination.register_request_handler(
            path="data",
            response_generator=_listen_request,
            allow=RNS.Destination.ALLOW_ALL,
        )

    await _check_finished()

    log.info("rnsh listening for commands on " + RNS.prettyhexrep(_destination.hash))

    if not disable_announce:
        _destination.announce()

    last = time.time()

    try:
        while True:
            if not disable_announce and time.time() - last > 900:  # TODO: make parameter
                last = datetime.datetime.now()
                _destination.announce()
            await _check_finished(1.0)
    except KeyboardInterrupt:
        log.warning("Shutting down")
        for link in list(_destination.links):
            try:
                proc = ProcessState.get_for_tag(link.link_id)
                if proc is not None and proc.process.running:
                    proc.process.terminate()
            except:
                pass
        await asyncio.sleep(1)
        links_still_active = list(filter(lambda l: l.status != RNS.Link.CLOSED, _destination.links))
        for link in links_still_active:
            if link.status != RNS.Link.CLOSED:
                link.teardown()


class ProcessState:
    _processes: [(any, ProcessState)] = []
    _lock = threading.RLock()

    @classmethod
    def get_for_tag(cls, tag: any) -> ProcessState | None:
        with cls._lock:
            return next(map(lambda p: p[1], filter(lambda p: p[0] == tag, cls._processes)), None)

    @classmethod
    def put_for_tag(cls, tag: any, ps: ProcessState):
        with cls._lock:
            cls.clear_tag(tag)
            cls._processes.append((tag, ps))


    @classmethod
    def clear_tag(cls, tag: any):
        with cls._lock:
            try:
                cls._processes.remove(tag)
            except:
                pass



    def __init__(self,
                 tag: any,
                 cmd: [str],
                 mdu: int,
                 data_available_callback: callable,
                 terminated_callback: callable,
                 term: str | None,
                 loop: asyncio.AbstractEventLoop = None):

        self._log = _get_logger(self.__class__.__name__)
        self._mdu = mdu
        self._loop = loop if loop is not None else asyncio.get_running_loop()
        self._process = process.CallbackSubprocess(argv=cmd,
                                                   term=term,
                                                   loop=loop,
                                                   stdout_callback=self._stdout_data,
                                                   terminated_callback=terminated_callback)
        self._data_buffer = bytearray()
        self._lock = threading.RLock()
        self._data_available_cb = data_available_callback
        self._terminated_cb = terminated_callback
        self._pending_receipt: RNS.PacketReceipt | None = None
        self._process.start()
        self._term_state: [int] = None
        ProcessState.put_for_tag(tag, self)

    @property
    def mdu(self) -> int:
        return self._mdu

    @mdu.setter
    def mdu(self, val: int):
        self._mdu = val

    def pending_receipt_peek(self) -> RNS.PacketReceipt | None:
        return self._pending_receipt

    def pending_receipt_take(self) -> RNS.PacketReceipt | None:
        with self._lock:
            val = self._pending_receipt
            self._pending_receipt = None
            return val

    def pending_receipt_put(self, receipt: RNS.PacketReceipt | None):
        with self._lock:
            self._pending_receipt = receipt

    @property
    def process(self) -> process.CallbackSubprocess:
        return self._process

    @property
    def return_code(self) -> int | None:
        return self.process.return_code

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    def read(self, count: int) -> bytes:
        with self.lock:
            initial_len = len(self._data_buffer)
            take = self._data_buffer[:count]
            self._data_buffer = self._data_buffer[count:].copy()
            self._log.debug(f"read {len(take)} bytes of {initial_len}, {len(self._data_buffer)} remaining")
            return take

    def _stdout_data(self, data: bytes):
        with self.lock:
            self._data_buffer.extend(data)
            total_available = len(self._data_buffer)
        try:
            self._data_available_cb(total_available)
        except Exception as e:
            self._log.error(f"Error calling ProcessState data_available_callback {e}")

    TERMSTATE_IDX_TERM = 0
    TERMSTATE_IDX_TIOS = 1
    TERMSTATE_IDX_ROWS = 2
    TERMSTATE_IDX_COLS = 3
    TERMSTATE_IDX_HPIX = 4
    TERMSTATE_IDX_VPIX = 5

    def _update_winsz(self):
        try:
            self.process.set_winsize(self._term_state[ProcessState.TERMSTATE_IDX_ROWS],
                                     self._term_state[ProcessState.TERMSTATE_IDX_COLS],
                                     self._term_state[ProcessState.TERMSTATE_IDX_HPIX],
                                     self._term_state[ProcessState.TERMSTATE_IDX_VPIX])
        except Exception as e:
            self._log.debug(f"failed to update winsz: {e}")


    REQUEST_IDX_STDIN = 0
    REQUEST_IDX_TERM = 1
    REQUEST_IDX_TIOS = 2
    REQUEST_IDX_ROWS = 3
    REQUEST_IDX_COLS = 4
    REQUEST_IDX_HPIX = 5
    REQUEST_IDX_VPIX = 6

    @staticmethod
    def default_request(stdin_fd: int | None) -> [any]:
        request: list[any] = [
            None,  # 0 Stdin
            None,  # 1 TERM variable
            None,  # 2 termios attributes or something
            None,  # 3 terminal rows
            None,  # 4 terminal cols
            None,  # 5 terminal horizontal pixels
            None,  # 6 terminal vertical pixels
        ].copy()

        if stdin_fd is not None:
            request[ProcessState.REQUEST_IDX_TERM] = os.environ.get("TERM", None)
            request[ProcessState.REQUEST_IDX_TIOS] = termios.tcgetattr(stdin_fd)
            request[ProcessState.REQUEST_IDX_ROWS], \
            request[ProcessState.REQUEST_IDX_COLS], \
            request[ProcessState.REQUEST_IDX_HPIX], \
            request[ProcessState.REQUEST_IDX_VPIX] = process.tty_get_winsize(stdin_fd)
        return request

    def process_request(self, data: [any], read_size: int) -> [any]:
        stdin = data[ProcessState.REQUEST_IDX_STDIN]  # Data passed to stdin
        # term = data[ProcessState.REQUEST_IDX_TERM]  # TERM environment variable
        # tios = data[ProcessState.REQUEST_IDX_TIOS]  # termios attr
        # rows = data[ProcessState.REQUEST_IDX_ROWS]  # window rows
        # cols = data[ProcessState.REQUEST_IDX_COLS]  # window cols
        # hpix = data[ProcessState.REQUEST_IDX_HPIX]  # window horizontal pixels
        # vpix = data[ProcessState.REQUEST_IDX_VPIX]  # window vertical pixels
        # term_state = data[ProcessState.REQUEST_IDX_ROWS:ProcessState.REQUEST_IDX_VPIX+1]
        response = ProcessState.default_response()
        term_state = data[ProcessState.REQUEST_IDX_TIOS:ProcessState.REQUEST_IDX_VPIX+1]

        response[ProcessState.RESPONSE_IDX_RUNNING] = self.process.running
        if self.process.running:
            if term_state != self._term_state:
                self._term_state = term_state
                self._update_winsz()
            if stdin is not None and len(stdin) > 0:
                stdin = base64.b64decode(stdin)
                self.process.write(stdin)
        response[ProcessState.RESPONSE_IDX_RETCODE] = self.return_code

        with self.lock:
            stdout = self.read(read_size)
            response[ProcessState.RESPONSE_IDX_RDYBYTE] = len(self._data_buffer)

        if stdout is not None and len(stdout) > 0:
            response[ProcessState.RESPONSE_IDX_STDOUT] = base64.b64encode(stdout).decode("utf-8")
        return response

    RESPONSE_IDX_RUNNING = 0
    RESPONSE_IDX_RETCODE = 1
    RESPONSE_IDX_RDYBYTE = 2
    RESPONSE_IDX_STDOUT  = 3
    RESPONSE_IDX_TMSTAMP = 4

    @staticmethod
    def default_response() -> [any]:
        response: list[any] = [
            False,        # 0: Process running
            None,         # 1: Return value
            0,            # 2: Number of outstanding bytes
            None,         # 3: Stdout/Stderr
            None,         # 4: Timestamp
        ].copy()
        response[ProcessState.RESPONSE_IDX_TMSTAMP] = time.time()
        return response


def _subproc_data_ready(link: RNS.Link, chars_available: int):
    global _retry_timer
    log = _get_logger("_subproc_data_ready")
    process_state: ProcessState = ProcessState.get_for_tag(link.link_id)

    def send(timeout: bool, tag: any, tries: int) -> any:
        # log.debug("send")
        def inner():
            # log.debug("inner")
            try:
                if link.status != RNS.Link.ACTIVE:
                    _retry_timer.complete(link.link_id)
                    process_state.pending_receipt_take()
                    return

                pr = process_state.pending_receipt_take()
                log.debug(f"send inner pr: {pr}")
                if pr is not None and pr.status == RNS.PacketReceipt.DELIVERED:
                    if not timeout:
                        _retry_timer.complete(tag)
                    log.debug(f"Notification completed with status {pr.status} on link {link}")
                    return
                else:
                    if not timeout:
                        log.info(
                            f"Notifying client try {tries} (retcode: {process_state.return_code} chars avail: {chars_available})")
                        packet = RNS.Packet(link, DATA_AVAIL_MSG.encode("utf-8"))
                        packet.send()
                        pr = packet.receipt
                        process_state.pending_receipt_put(pr)
                    else:
                        log.error(f"Retry count exceeded, terminating link {link}")
                        _retry_timer.complete(link.link_id)
                        link.teardown()
            except Exception as e:
                log.error("Error notifying client: " + str(e))

        _loop.call_soon_threadsafe(inner)
        return link.link_id

    with process_state.lock:
        if not _retry_timer.has_tag(link.link_id):
            _retry_timer.begin(try_limit=15,
                               wait_delay=max(link.rtt * 5 if link.rtt is not None else 1, 1),
                               try_callback=functools.partial(send, False),
                               timeout_callback=functools.partial(send, True),
                               tag=None)
        else:
            log.debug(f"Notification already pending for link {link}")

def _subproc_terminated(link: RNS.Link, return_code: int):
    global _loop
    log = _get_logger("_subproc_terminated")
    log.info(f"Subprocess returned {return_code} for link {link}")
    proc = ProcessState.get_for_tag(link.link_id)
    if proc is None:
        log.debug(f"no proc for link {link}")
        return

    def cleanup():
        def inner():
            log.debug(f"cleanup culled link {link}")
            if link and link.status != RNS.Link.CLOSED:
                try:
                    link.teardown()
                except:
                    pass
                finally:
                    ProcessState.clear_tag(link.link_id)
        _loop.call_later(300, inner)
        _loop.call_soon(_subproc_data_ready, link, 0)
    _loop.call_soon_threadsafe(cleanup)


def _listen_start_proc(link: RNS.Link, term: str, loop: asyncio.AbstractEventLoop) -> ProcessState | None:
    global _cmd
    log = _get_logger("_listen_start_proc")
    try:
        return ProcessState(tag=link.link_id,
                            cmd=_cmd,
                            term=term,
                            mdu=link.MDU,
                            loop=loop,
                            data_available_callback=functools.partial(_subproc_data_ready, link),
                            terminated_callback=functools.partial(_subproc_terminated, link))
    except Exception as e:
        log.error("Failed to launch process: " + str(e))
        _subproc_terminated(link, 255)
    return None


def _listen_link_established(link):
    global _allow_all
    log = _get_logger("_listen_link_established")
    link.set_remote_identified_callback(_initiator_identified)
    link.set_link_closed_callback(_listen_link_closed)
    log.info("Link " + str(link) + " established")


def _listen_link_closed(link: RNS.Link):
    log = _get_logger("_listen_link_closed")
    # async def cleanup():
    log.info("Link " + str(link) + " closed")
    proc: ProcessState | None = ProcessState.get_for_tag(link.link_id)
    if proc is None:
        log.warning(f"No process for link {link}")
    else:
        try:
            proc.process.terminate()
            _retry_timer.complete(link.link_id)
        except Exception as e:
            log.error(f"Error closing process for link {link}: {e}")
    ProcessState.clear_tag(link.link_id)


def _initiator_identified(link, identity):
    global _allow_all, _cmd, _loop
    log = _get_logger("_initiator_identified")
    log.info("Initiator of link " + str(link) + " identified as " + RNS.prettyhexrep(identity.hash))
    if not _allow_all and not identity.hash in _allowed_identity_hashes:
        log.warning("Identity " + RNS.prettyhexrep(identity.hash) + " not allowed, tearing down link", RNS.LOG_WARNING)
        link.teardown()


def _listen_request(path, data, request_id, link_id, remote_identity, requested_at):
    global _destination, _retry_timer, _loop
    log = _get_logger("_listen_request")
    log.debug(f"listen_execute {path} {request_id} {link_id} {remote_identity}, {requested_at}")
    _retry_timer.complete(link_id)
    link: RNS.Link = next(filter(lambda l: l.link_id == link_id, _destination.links), None)
    if link is None:
        raise Exception(f"Invalid request {request_id}, no link found with id {link_id}")
    process_state: ProcessState | None = None
    try:
        term = data[ProcessState.REQUEST_IDX_TERM]
        process_state = ProcessState.get_for_tag(link.link_id)
        if process_state is None:
            log.debug(f"Process not found for link {link}")
            process_state = _listen_start_proc(link, term, _loop)

        # leave significant headroom for metadata and encoding
        result = process_state.process_request(data, link.MDU * 3 // 2)
        return result
        # return ProcessState.default_response()
    except Exception as e:
        log.error(f"Error procesing request for link {link}: {e}")
        try:
            if process_state is not None and process_state.process.running:
                process_state.process.terminate()
        except Exception as ee:
            log.debug(f"Error terminating process for link {link}: {ee}")

    return ProcessState.default_response()


async def _spin(until: Callable | None = None, timeout: float | None = None) -> bool:
    global _pool
    if timeout is not None:
        timeout += time.time()

    while (timeout is None or time.time() < timeout) and not until():
        await _check_finished(0.01)
    if timeout is not None and time.time() > timeout:
        return False
    else:
        return True


_link: RNS.Link | None = None
_remote_exec_grace = 2.0
_new_data: asyncio.Event | None = None
_tr = process.TtyRestorer(sys.stdin.fileno())


def _client_packet_handler(message, packet):
    global _new_data
    log = _get_logger("_client_packet_handler")
    if message is not None and message.decode("utf-8") == DATA_AVAIL_MSG and _new_data is not None:
        log.debug("data available")
        _new_data.set()
    else:
        log.error(f"received unhandled packet")


class RemoteExecutionError(Exception):
    def __init__(self, msg):
        self.msg = msg


def _response_handler(request_receipt: RNS.RequestReceipt):
    pass


async def _execute(configdir, identitypath=None, verbosity=0, quietness=0, noid=False, destination=None,
                   service_name="default", stdin=None, timeout=RNS.Transport.PATH_REQUEST_TIMEOUT):
    global _identity, _reticulum, _link, _destination, _remote_exec_grace, _tr, _new_data
    log = _get_logger("_execute")

    dest_len = (RNS.Reticulum.TRUNCATED_HASHLENGTH // 8) * 2
    if len(destination) != dest_len:
        raise RemoteExecutionError(
            "Allowed destination length is invalid, must be {hex} hexadecimal characters ({byte} bytes).".format(
                hex=dest_len, byte=dest_len // 2))
    try:
        destination_hash = bytes.fromhex(destination)
    except Exception as e:
        raise RemoteExecutionError("Invalid destination entered. Check your input.")

    if _reticulum is None:
        targetloglevel = 2 + verbosity - quietness
        _reticulum = RNS.Reticulum(configdir=configdir, loglevel=targetloglevel)

    if _identity is None:
        _prepare_identity(identitypath)

    if not RNS.Transport.has_path(destination_hash):
        RNS.Transport.request_path(destination_hash)
        log.info(f"Requesting path...")
        if not await _spin(until=lambda: RNS.Transport.has_path(destination_hash), timeout=timeout):
            raise RemoteExecutionError("Path not found")

    if _destination is None:
        listener_identity = RNS.Identity.recall(destination_hash)
        _destination = RNS.Destination(
            listener_identity,
            RNS.Destination.OUT,
            RNS.Destination.SINGLE,
            APP_NAME,
            service_name
        )

    if _link is None or _link.status == RNS.Link.PENDING:
        _link = RNS.Link(_destination)
        _link.did_identify = False

    log.info(f"Establishing link...")
    if not await _spin(until=lambda: _link.status == RNS.Link.ACTIVE, timeout=timeout):
        raise RemoteExecutionError("Could not establish link with " + RNS.prettyhexrep(destination_hash))

    if not noid and not _link.did_identify:
        _link.identify(_identity)
        _link.did_identify = True

    _link.set_packet_callback(_client_packet_handler)

    request = ProcessState.default_request(sys.stdin.fileno())
    request[ProcessState.REQUEST_IDX_STDIN] = (base64.b64encode(stdin) if stdin is not None else None)

    # TODO: Tune
    timeout = timeout + _link.rtt * 4 + _remote_exec_grace

    request_receipt = _link.request(
        path="data",
        data=request,
        timeout=timeout
    )
    timeout += 0.5

    await _spin(
        until=lambda: _link.status == RNS.Link.CLOSED or (
                request_receipt.status != RNS.RequestReceipt.FAILED and request_receipt.status != RNS.RequestReceipt.SENT),
        timeout=timeout
    )

    if _link.status == RNS.Link.CLOSED:
        raise RemoteExecutionError("Could not request remote execution, link was closed")

    if request_receipt.status == RNS.RequestReceipt.FAILED:
        raise RemoteExecutionError("Could not request remote execution")

    await _spin(
        until=lambda: request_receipt.status != RNS.RequestReceipt.DELIVERED,
        timeout=timeout
    )

    if request_receipt.status == RNS.RequestReceipt.FAILED:
        raise RemoteExecutionError("No result was received")

    if request_receipt.status == RNS.RequestReceipt.FAILED:
        raise RemoteExecutionError("Receiving result failed")

    if request_receipt.response is not None:
        try:
            running     = request_receipt.response[ProcessState.RESPONSE_IDX_RUNNING] or True
            return_code = request_receipt.response[ProcessState.RESPONSE_IDX_RETCODE]
            ready_bytes = request_receipt.response[ProcessState.RESPONSE_IDX_RDYBYTE] or 0
            stdout      = request_receipt.response[ProcessState.RESPONSE_IDX_STDOUT]
            timestamp   = request_receipt.response[ProcessState.RESPONSE_IDX_TMSTAMP]
            # log.debug("data: " + (stdout.decode("utf-8") if stdout is not None else ""))
        except Exception as e:
            raise RemoteExecutionError(f"Received invalid response") from e

        _tr.raw()
        if stdout is not None:
            stdout = base64.b64decode(stdout)
            # log.debug(f"stdout: {stdout}")
            os.write(sys.stdout.fileno(), stdout)

        sys.stdout.flush()
        sys.stderr.flush()

        log.debug(f"{ready_bytes} bytes ready on server, return code {return_code}")

        if ready_bytes > 0:
            _new_data.set()

        if (not running or return_code is not None) and (ready_bytes == 0):
            log.debug(f"returning running: {running}, return_code: {return_code}")
            return return_code or 255

        return None


async def _initiate(configdir: str, identitypath: str, verbosity: int, quietness: int, noid: bool, destination: str,
                    service_name: str, timeout: float):
    global _new_data, _finished, _tr
    log = _get_logger("_initiate")
    loop = asyncio.get_running_loop()
    _new_data = asyncio.Event()

    data_buffer = bytearray()

    def sigint_handler():
        log.debug("KeyboardInterrupt")
        data_buffer.extend("\x03".encode("utf-8"))

    def sigwinch_handler():
        # log.debug("WindowChanged")
        if _new_data is not None:
            _new_data.set()

    def stdin():
        data = process.tty_read(sys.stdin.fileno())
        # log.debug(f"stdin {data}")
        if data is not None:
                data_buffer.extend(data)

    process.tty_add_reader_callback(sys.stdin.fileno(), stdin)

    await _check_finished()
    # signal.signal(signal.SIGWINCH, sigwinch_handler)
    loop.add_signal_handler(signal.SIGWINCH, sigwinch_handler)
    first_loop = True
    while True:
        try:
            log.debug("top of client loop")
            stdin = data_buffer.copy()
            data_buffer.clear()
            _new_data.clear()
            log.debug("before _execute")
            return_code = await _execute(
                configdir=configdir,
                identitypath=identitypath,
                verbosity=verbosity,
                quietness=quietness,
                noid=noid,
                destination=destination,
                service_name=service_name,
                stdin=stdin,
                timeout=timeout,
            )
            # signal.signal(signal.SIGINT, sigint_handler)
            if first_loop:
                first_loop = False
                loop.remove_signal_handler(signal.SIGINT)
                loop.add_signal_handler(signal.SIGINT, sigint_handler)
                _new_data.set()

            if return_code is not None:
                log.debug(f"received return code {return_code}, exiting")
                try:
                    _link.teardown()
                except:
                    pass
                return return_code
        except RemoteExecutionError as e:
            print(e.msg)
            return 255

        await process.event_wait(_new_data, 5)


_T = TypeVar("_T")

def _split_array_at(arr: [_T], at: _T) -> ([_T], [_T]):
    try:
        idx = arr.index(at)
        return arr[:idx], arr[idx+1:]
    except ValueError:
        return arr, []

async def main():
    global _tr, _finished, _loop
    import docopt
    log = _get_logger("main")
    _loop = asyncio.get_running_loop()
    rnslogging.set_main_loop(_loop)
    _finished = asyncio.Event()
    _loop.remove_signal_handler(signal.SIGINT)
    _loop.add_signal_handler(signal.SIGINT, functools.partial(_sigint_handler, signal.SIGINT, None))
    usage = '''
Usage:
    rnsh [--config <configdir>] [-i <identityfile>] [-s <service_name>] [-l] -p
    rnsh -l [--config <configfile>] [-i <identityfile>] [-s <service_name>] [-v...] [-q...] [-b] 
         (-n | -a <identity_hash> [-a <identity_hash>]...) [--] <program> [<arg>...]
    rnsh [--config <configfile>] [-i <identityfile>] [-s <service_name>] [-v...] [-q...] [-N] [-m]
         [-w <timeout>] <destination_hash>
    rnsh -h
    rnsh --version

Options:
    --config FILE            Alternate Reticulum config directory to use
    -i FILE --identity FILE  Specific identity file to use
    -s NAME --service NAME   Listen on/connect to specific service name if not default
    -p --print-identity      Print identity information and exit
    -l --listen              Listen (server) mode
    -b --no-announce         Do not announce service
    -a HASH --allowed HASH   Specify identities allowed to connect
    -n --no-auth             Disable authentication
    -N --no-id               Disable identify on connect
    -m --mirror              Client returns with code of remote process
    -w TIME --timeout TIME   Specify client connect and request timeout in seconds
    -v --verbose             Increase verbosity
    -q --quiet               Increase quietness
    --version                Show version
    -h --help                Show this help
    '''

    argv, program_args = _split_array_at(sys.argv, "--")
    if len(program_args) > 0:
        argv.append(program_args[0])
        program_args = program_args[1:]

    args = docopt.docopt(usage, argv=argv[1:], version=f"rnsh {__version__}")
    # json.dump(args, sys.stdout)

    args_service_name = args.get("--service", None) or "default"
    args_listen = args.get("--listen", None) or False
    args_identity = args.get("--identity", None)
    args_config = args.get("--config", None)
    args_print_identity = args.get("--print-identity", None) or False
    args_verbose = args.get("--verbose", None) or 0
    args_quiet = args.get("--quiet", None) or 0
    args_no_announce = args.get("--no-announce", None) or False
    args_no_auth = args.get("--no-auth", None) or False
    args_allowed = args.get("--allowed", None) or []
    args_program = args.get("<program>", None)
    args_program_args = args.get("<arg>", None) or []
    args_program_args.insert(0, args_program)
    args_program_args.extend(program_args)
    args_no_id = args.get("--no-id", None) or False
    args_mirror = args.get("--mirror", None) or False
    args_timeout = args.get("--timeout", None) or RNS.Transport.PATH_REQUEST_TIMEOUT
    args_destination = args.get("<destination_hash>", None)
    args_help = args.get("--help", None) or False

    if args_help:
        return 0

    if args_print_identity:
        _print_identity(args_config, args_identity, args_service_name, args_listen)
        return 0

    if args_listen:
        # log.info("command " + args.command)
        await _listen(
            configdir=args_config,
            command=args_program_args,
            identitypath=args_identity,
            service_name=args_service_name,
            verbosity=args_verbose,
            quietness=args_quiet,
            allowed=args_allowed,
            disable_auth=args_no_auth,
            disable_announce=args_no_announce,
        )

    if args_destination is not None and args_service_name is not None:
        try:
            return_code = await _initiate(
                configdir=args_config,
                identitypath=args_identity,
                verbosity=args_verbose,
                quietness=args_quiet,
                noid=args_no_id,
                destination=args_destination,
                service_name=args_service_name,
                timeout=args_timeout,
            )
            return return_code if args_mirror else 0
        except:
            _tr.restore()
            raise
    else:
        print("")
        print(args)
        print("")


if __name__ == "__main__":
    return_code = 1
    try:
        return_code = asyncio.run(main())
    finally:
        try:
            process.tty_unset_reader_callbacks(sys.stdin.fileno())
        except:
            pass
        _tr.restore()
        _pool.close()
        _retry_timer.close()
    sys.exit(return_code)
