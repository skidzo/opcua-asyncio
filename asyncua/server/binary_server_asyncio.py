"""
Socket server forwarding request to internal server
"""
import logging
import asyncio

from ..ua.ua_binary import header_from_binary
from ..common import Buffer, NotEnoughData
from .uaprocessor import UaProcessor

logger = logging.getLogger(__name__)
__all__ = ["BinaryServer"]


class OPCUAProtocol(asyncio.Protocol):
    """
    Instantiated for every connection.
    """

    def __init__(self, iserver, policies, clients):
        self.peer_name = None
        self.transport = None
        self.processor = None
        self._buffer = b''
        self.iserver = iserver
        self.policies = policies
        self.clients = clients
        self.messages = asyncio.Queue()
        self._task = None

    def __str__(self):
        return 'OPCUAProtocol({}, {})'.format(self.peer_name, self.processor.session)

    __repr__ = __str__

    def connection_made(self, transport):
        self.peer_name = transport.get_extra_info('peername')
        logger.info('New connection from %s', self.peer_name)
        self.transport = transport
        self.processor = UaProcessor(self.iserver, self.transport)
        self.processor.set_policies(self.policies)
        self.iserver.asyncio_transports.append(transport)
        self.clients.append(self)
        self._task = self.iserver.loop.create_task(self._process_received_message_loop())

    def connection_lost(self, ex):
        logger.info('Lost connection from %s, %s', self.peer_name, ex)
        self.transport.close()
        self.iserver.asyncio_transports.remove(self.transport)
        self.iserver.loop.create_task(self.processor.close())
        if self in self.clients:
            self.clients.remove(self)
        self.messages.put_nowait((None, None))
        self._task.cancel()

    def data_received(self, data):
        self._buffer += data
        # try to parse the incoming data
        while self._buffer:
            try:
                buf = Buffer(self._buffer)
                try:
                    header = header_from_binary(buf)
                except NotEnoughData:
                    logger.debug('Not enough data while parsing header from client, waiting for more')
                    return
                if len(buf) < header.body_size:
                    logger.debug('We did not receive enough data from client. Need %s got %s', header.body_size,
                                 len(buf))
                    return
                # we have a complete message
                self.messages.put_nowait((header, buf))
                self._buffer = self._buffer[(header.header_size + header.body_size):]
            except Exception:
                logger.exception('Exception raised while parsing message from client')
                return

    async def _process_received_message_loop(self):
        """
        Take message from the queue and try to process it.
        """
        while True:
            header, buf = await self.messages.get()
            if header is None and buf is None:
                # Connection was closed, end task
                break
            try:
                await self._process_one_msg(header, buf)
            except Exception:
                logger.exception()

    async def _process_one_msg(self, header, buf):
        logger.debug('_process_received_message %s %s', header.body_size, len(buf))
        ret = await self.processor.process(header, buf)
        if not ret:
            logger.info('processor returned False, we close connection from %s', self.peer_name)
            self.transport.close()
            return


class BinaryServer:
    def __init__(self, internal_server, hostname, port):
        self.logger = logging.getLogger(__name__)
        self.hostname = hostname
        self.port = port
        self.iserver = internal_server
        self._server = None
        self._policies = []
        self.clients = []

    def set_policies(self, policies):
        self._policies = policies

    def _make_protocol(self):
        """Protocol Factory"""
        return OPCUAProtocol(iserver=self.iserver, policies=self._policies, clients=self.clients)

    async def start(self):
        self._server = await self.iserver.loop.create_server(self._make_protocol, self.hostname, self.port)
        # get the port and the hostname from the created server socket
        # only relevant for dynamic port asignment (when self.port == 0)
        if self.port == 0 and len(self._server.sockets) == 1:
            # will work for AF_INET and AF_INET6 socket names
            # these are to only families supported by the create_server call
            sockname = self._server.sockets[0].getsockname()
            self.hostname = sockname[0]
            self.port = sockname[1]
        self.logger.info('Listening on %s:%s', self.hostname, self.port)

    async def stop(self):
        self.logger.info('Closing asyncio socket server')
        for transport in self.iserver.asyncio_transports:
            transport.close()
        if self._server:
            self.iserver.loop.call_soon(self._server.close)
            await self._server.wait_closed()
