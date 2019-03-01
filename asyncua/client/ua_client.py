"""
Low level binary client
"""
import asyncio
import logging

from asyncua import ua
from ..ua.ua_binary import struct_from_binary, uatcp_to_binary, struct_to_binary, nodeid_from_binary, header_from_binary
from ..ua.uaerrors import UaError, BadTimeout, BadNoSubscription, BadSessionClosed
from ..common import SecureConnection

__all__ = ["UaClient"]


class UASocketProtocol(asyncio.Protocol):
    """
    Handle socket connection and send ua messages.
    Timeout is the timeout used while waiting for an ua answer from server.
    """
    INITIALIZED = 'initialized'
    OPEN = 'open'
    CLOSED = 'closed'

    def __init__(self, timeout=1, security_policy=ua.SecurityPolicy(), loop=None):
        self.logger = logging.getLogger(__name__ + ".UASocketProtocol")
        self.loop = loop or asyncio.get_event_loop()
        self.transport = None
        self.receive_buffer: bytes = None
        self.is_receiving = False
        self.timeout = timeout
        self.authentication_token = ua.NodeId()
        self._request_id = 0
        self._request_handle = 0
        self._callbackmap = {}
        self._connection = SecureConnection(security_policy)
        self.state = self.INITIALIZED

    def connection_made(self, transport: asyncio.Transport):
        self.state = self.OPEN
        self.transport = transport

    def connection_lost(self, exc):
        self.logger.info("Socket has closed connection")
        self.state = self.CLOSED
        self.transport = None

    def data_received(self, data: bytes):
        if self.receive_buffer:
            data = self.receive_buffer + data
            self.receive_buffer = None
        self._process_received_data(data)

    def _process_received_data(self, data: bytes):
        """Try to parse a asyncua message"""
        buf = ua.utils.Buffer(data)
        while True:
            try:
                try:
                    header = header_from_binary(buf)
                except ua.utils.NotEnoughData:
                    self.logger.debug('Not enough data while parsing header from server, waiting for more')
                    self.receive_buffer = data
                    return
                if len(buf) < header.body_size:
                    self.logger.debug('We did not receive enough data from server. Need %s got %s', header.body_size, len(buf))
                    self.receive_buffer = data
                    return
                msg = self._connection.receive_from_header_and_body(header, buf)
                self._process_received_message(msg)
                if not buf:
                    return
            except Exception:
                self.logger.exception('Exception raised while parsing message from server')
                self.disconnect_socket()
                return

    def _process_received_message(self, msg):
        if msg is None:
            pass
        elif isinstance(msg, ua.Message):
            self._call_callback(msg.request_id(), msg.body())
        elif isinstance(msg, ua.Acknowledge):
            self._call_callback(0, msg)
        elif isinstance(msg, ua.ErrorMessage):
            self.logger.fatal("Received an error: %r", msg)
            self._call_callback(0, ua.UaStatusCodeError(msg.Error.value))
        else:
            raise ua.UaError(f"Unsupported message type: {msg}")

    def _send_request(self, request, callback=None, timeout=1000, message_type=ua.MessageType.SecureMessage):
        """
        Send request to server, lower-level method.
        Timeout is the timeout written in ua header.
        Returns future
        """
        request.RequestHeader = self._create_request_header(timeout)
        self.logger.debug('Sending: %s', request)
        try:
            binreq = struct_to_binary(request)
        except Exception:
            # reset request handle if any error
            # see self._create_request_header
            self._request_handle -= 1
            raise
        self._request_id += 1
        future = asyncio.Future()
        if callback:
            future.add_done_callback(callback)
        self._callbackmap[self._request_id] = future
        msg = self._connection.message_to_binary(binreq, message_type=message_type, request_id=self._request_id)
        self.transport.write(msg)
        return future

    async def send_request(self, request, callback=None, timeout=10, message_type=ua.MessageType.SecureMessage):
        """
        Send a request to the server.
        Timeout is the timeout written in ua header.
        Returns response object if no callback is provided.
        """
        future = self._send_request(request, callback, timeout, message_type)
        if not callback:
            await asyncio.wait_for(future, timeout if timeout else None)
            data = future.result()
            self.check_answer(data, " in response to " + request.__class__.__name__)
            return data

    def check_answer(self, data, context):
        data = data.copy()
        typeid = nodeid_from_binary(data)
        if typeid == ua.FourByteNodeId(ua.ObjectIds.ServiceFault_Encoding_DefaultBinary):
            self.logger.warning("ServiceFault from server received %s", context)
            hdr = struct_from_binary(ua.ResponseHeader, data)
            hdr.ServiceResult.check()
            return False
        return True

    def _call_callback(self, request_id, body):
        future = self._callbackmap.pop(request_id, None)
        if future is None:
            raise ua.UaError("No request found for requestid: {0}, callbacks in list are {1}".format(
                request_id, self._callbackmap.keys()))
        future.set_result(body)

    def _create_request_header(self, timeout=1000):
        hdr = ua.RequestHeader()
        hdr.AuthenticationToken = self.authentication_token
        self._request_handle += 1
        hdr.RequestHandle = self._request_handle
        hdr.TimeoutHint = timeout
        return hdr

    def disconnect_socket(self):
        self.logger.info("Request to close socket received")
        if self.transport:
            self.transport.close()
        else:
            self.logger.warning("disconnect_socket was called but transport is None")

    async def send_hello(self, url, max_messagesize=0, max_chunkcount=0):
        hello = ua.Hello()
        hello.EndpointUrl = url
        hello.MaxMessageSize = max_messagesize
        hello.MaxChunkCount = max_chunkcount
        future = asyncio.Future()
        self._callbackmap[0] = future
        binmsg = uatcp_to_binary(ua.MessageType.Hello, hello)
        self.transport.write(binmsg)
        await asyncio.wait_for(future, self.timeout)
        ack = future.result()
        return ack

    async def open_secure_channel(self, params):
        self.logger.info("open_secure_channel")
        request = ua.OpenSecureChannelRequest()
        request.Parameters = params
        future = self._send_request(request, message_type=ua.MessageType.SecureOpen)
        await asyncio.wait_for(future, self.timeout)
        result = future.result()
        # FIXME: we have a race condition here
        # we can get a packet with the new token id before we reach to store it..
        response = struct_from_binary(ua.OpenSecureChannelResponse, result)
        response.ResponseHeader.ServiceResult.check()
        self._connection.set_channel(response.Parameters)
        return response.Parameters

    async def close_secure_channel(self):
        """
        Close secure channel.
        It seems to trigger a shutdown of socket in most servers, so be prepare to reconnect.
        OPC UA specs Part 6, 7.1.4 say that Server does not send a CloseSecureChannel response
        and should just close socket.
        """
        self.logger.info("close_secure_channel")
        request = ua.CloseSecureChannelRequest()
        future = self._send_request(request, message_type=ua.MessageType.SecureClose)
        # don't expect any more answers
        future.cancel()
        self._callbackmap.clear()
        # some servers send a response here, most do not ... so we ignore


class UaClient:
    """
    low level OPC-UA client.

    It implements (almost) all methods defined in asyncua spec
    taking in argument the structures defined in asyncua spec.

    In this Python implementation  most of the structures are defined in
    uaprotocol_auto.py and uaprotocol_hand.py available under asyncua.ua
    """

    def __init__(self, timeout=1, loop=None):
        self.logger = logging.getLogger(__name__ + '.UaClient')
        self.loop = loop or asyncio.get_event_loop()
        self._publish_callbacks = {}
        self._timeout = timeout
        self.security_policy = ua.SecurityPolicy()
        self.protocol: UASocketProtocol = None
        self._sub_cond = asyncio.Condition()
        self._sub_data_queue = []

    def set_security(self, policy: ua.SecurityPolicy):
        self.security_policy = policy

    def _make_protocol(self):
        self.protocol = UASocketProtocol(self._timeout, security_policy=self.security_policy, loop=self.loop)
        return self.protocol

    async def connect_socket(self, host: str, port: int):
        """Connect to server socket."""
        self.logger.info("opening connection")
        await self.loop.create_connection(self._make_protocol, host, port)

    def disconnect_socket(self):
        if self.protocol and self.protocol.state == UASocketProtocol.CLOSED:
            self.logger.warning("disconnect_socket was called but connection is closed")
            return None
        return self.protocol.disconnect_socket()

    async def send_hello(self, url, max_messagesize=0, max_chunkcount=0):
        await self.protocol.send_hello(url, max_messagesize, max_chunkcount)

    async def open_secure_channel(self, params):
        return await self.protocol.open_secure_channel(params)

    async def close_secure_channel(self):
        """
        close secure channel. It seems to trigger a shutdown of socket
        in most servers, so be prepare to reconnect
        """
        if self.protocol and self.protocol.state == UASocketProtocol.CLOSED:
            self.logger.warning("close_secure_channel was called but connection is closed")
            return
        return await self.protocol.close_secure_channel()

    async def create_session(self, parameters):
        self.logger.info("create_session")
        request = ua.CreateSessionRequest()
        request.Parameters = parameters
        data = await self.protocol.send_request(request)
        response = struct_from_binary(ua.CreateSessionResponse, data)
        self.logger.debug(response)
        response.ResponseHeader.ServiceResult.check()
        self.protocol.authentication_token = response.Parameters.AuthenticationToken
        return response.Parameters

    async def activate_session(self, parameters):
        self.logger.info("activate_session")
        request = ua.ActivateSessionRequest()
        request.Parameters = parameters
        data = await self.protocol.send_request(request)
        response = struct_from_binary(ua.ActivateSessionResponse, data)
        self.logger.debug(response)
        response.ResponseHeader.ServiceResult.check()
        return response.Parameters

    async def close_session(self, delete_subscriptions):
        self.logger.info("close_session")
        if self.protocol and self.protocol.state == UASocketProtocol.CLOSED:
            self.logger.warning("close_session was called but connection is closed")
            return
        request = ua.CloseSessionRequest()
        request.DeleteSubscriptions = delete_subscriptions
        data = await self.protocol.send_request(request)
        response = struct_from_binary(ua.CloseSessionResponse, data)
        try:
            response.ResponseHeader.ServiceResult.check()
        except BadSessionClosed:
            # Problem: closing the session with open publish requests leads to BadSessionClosed responses
            #          we can just ignore it therefore.
            #          Alternatively we could make sure that there are no publish requests in flight when
            #          closing the session.
            pass

    async def browse(self, parameters):
        self.logger.info("browse")
        request = ua.BrowseRequest()
        request.Parameters = parameters
        data = await self.protocol.send_request(request)
        response = struct_from_binary(ua.BrowseResponse, data)
        self.logger.debug(response)
        response.ResponseHeader.ServiceResult.check()
        return response.Results

    async def browse_next(self, parameters):
        self.logger.info("browse next")
        request = ua.BrowseNextRequest()
        request.Parameters = parameters
        data = await self.protocol.send_request(request)
        response = struct_from_binary(ua.BrowseNextResponse, data)
        self.logger.debug(response)
        response.ResponseHeader.ServiceResult.check()
        return response.Parameters.Results

    async def read(self, parameters):
        self.logger.info("read")
        request = ua.ReadRequest()
        request.Parameters = parameters
        data = await self.protocol.send_request(request)
        response = struct_from_binary(ua.ReadResponse, data)
        self.logger.debug(response)
        response.ResponseHeader.ServiceResult.check()
        # cast to Enum attributes that need to
        for idx, rv in enumerate(parameters.NodesToRead):
            if rv.AttributeId == ua.AttributeIds.NodeClass:
                dv = response.Results[idx]
                if dv.StatusCode.is_good():
                    dv.Value.Value = ua.NodeClass(dv.Value.Value)
            elif rv.AttributeId == ua.AttributeIds.ValueRank:
                dv = response.Results[idx]
                if dv.StatusCode.is_good() and dv.Value.Value in (-3, -2, -1, 0, 1, 2, 3, 4):
                    dv.Value.Value = ua.ValueRank(dv.Value.Value)
        return response.Results

    async def write(self, params):
        self.logger.info("read")
        request = ua.WriteRequest()
        request.Parameters = params
        data = await self.protocol.send_request(request)
        response = struct_from_binary(ua.WriteResponse, data)
        self.logger.debug(response)
        response.ResponseHeader.ServiceResult.check()
        return response.Results

    async def get_endpoints(self, params):
        self.logger.info("get_endpoint")
        request = ua.GetEndpointsRequest()
        request.Parameters = params
        data = await self.protocol.send_request(request)
        response = struct_from_binary(ua.GetEndpointsResponse, data)
        self.logger.debug(response)
        response.ResponseHeader.ServiceResult.check()
        return response.Endpoints

    async def find_servers(self, params):
        self.logger.info("find_servers")
        request = ua.FindServersRequest()
        request.Parameters = params
        data = await self.protocol.send_request(request)
        response = struct_from_binary(ua.FindServersResponse, data)
        self.logger.debug(response)
        response.ResponseHeader.ServiceResult.check()
        return response.Servers

    async def find_servers_on_network(self, params):
        self.logger.info("find_servers_on_network")
        request = ua.FindServersOnNetworkRequest()
        request.Parameters = params
        data = await self.protocol.send_request(request)
        response = struct_from_binary(ua.FindServersOnNetworkResponse, data)
        self.logger.debug(response)
        response.ResponseHeader.ServiceResult.check()
        return response.Parameters

    async def register_server(self, registered_server):
        self.logger.info("register_server")
        request = ua.RegisterServerRequest()
        request.Server = registered_server
        data = await self.protocol.send_request(request)
        response = struct_from_binary(ua.RegisterServerResponse, data)
        self.logger.debug(response)
        response.ResponseHeader.ServiceResult.check()
        # nothing to return for this service

    async def register_server2(self, params):
        self.logger.info("register_server2")
        request = ua.RegisterServer2Request()
        request.Parameters = params
        data = await self.protocol.send_request(request)
        response = struct_from_binary(ua.RegisterServer2Response, data)
        self.logger.debug(response)
        response.ResponseHeader.ServiceResult.check()
        return response.ConfigurationResults

    async def translate_browsepaths_to_nodeids(self, browse_paths):
        self.logger.info("translate_browsepath_to_nodeid")
        request = ua.TranslateBrowsePathsToNodeIdsRequest()
        request.Parameters.BrowsePaths = browse_paths
        data = await self.protocol.send_request(request)
        response = struct_from_binary(ua.TranslateBrowsePathsToNodeIdsResponse, data)
        self.logger.debug(response)
        response.ResponseHeader.ServiceResult.check()
        return response.Results

    async def create_subscription(self, params, callback):
        self.logger.info("create_subscription")
        request = ua.CreateSubscriptionRequest()
        request.Parameters = params
        data = await self.protocol.send_request(request)
        response = struct_from_binary(
            ua.CreateSubscriptionResponse,
            data
        )
        self.logger.info("create subscription callback")
        self.logger.debug(response)
        response.ResponseHeader.ServiceResult.check()
        self._publish_callbacks[response.Parameters.SubscriptionId] = callback
        return response.Parameters

    async def delete_subscriptions(self, subscription_ids):
        self.logger.info("delete_subscription")
        request = ua.DeleteSubscriptionsRequest()
        request.Parameters.SubscriptionIds = subscription_ids
        data = await self.protocol.send_request(request)
        response = struct_from_binary(
            ua.DeleteSubscriptionsResponse,
            data
        )
        self.logger.info("delete subscriptions callback")
        self.logger.debug(response)
        response.ResponseHeader.ServiceResult.check()
        for sid in subscription_ids:
            self._publish_callbacks.pop(sid)
        return response.Results

    async def publish(self, acks=None):
        self.logger.info("publish")
        if acks is None:
            acks = []
        request = ua.PublishRequest()
        request.Parameters.SubscriptionAcknowledgements = acks
        await self.protocol.send_request(request, self._sub_data_received, timeout=0)

    def _sub_data_received(self, future):
        data = future.result()
        self.loop.create_task(self._call_publish_callback(data))

    """
    # to avoid fire and forget of task we could use a loop:
    def _sub_data_received(self, future):
        data = future.result()
        self.loop.create_task(self._enqueue_sub_data(data))

    async def _enqueue_sub_data(self, data):
        self._sub_data_queue.append(data)
        with self._sub_cond:
            self._sub_cond.notify()

    async def _subscribtion_loop(self):
        while True:
            async with self._sub_cond:
                await self._sub_cond.wait()
                data = self._sub_data_queue.pop(0)
                await self._call_publish_callback(data)
    """

    async def _call_publish_callback(self, data):
        self.logger.info("call_publish_callback")
        try:
            self.protocol.check_answer(data, "while waiting for publish response")
        except BadTimeout:
            # Spec Part 4, 7.28
            await self.publish()
            return
        except BadNoSubscription:  # Spec Part 5, 13.8.1
            # BadNoSubscription is expected after deleting the last subscription.
            #
            # We should therefore also check for len(self._publishcallbacks) == 0, but
            # this gets us into trouble if a Publish response arrives before the
            # DeleteSubscription response.
            #
            # We could remove the callback already when sending the DeleteSubscription request,
            # but there are some legitimate reasons to keep them around, such as when the server
            # responds with "BadTimeout" and we should try again later instead of just removing
            # the subscription client-side.
            #
            # There are a variety of ways to act correctly, but the most practical solution seems
            # to be to just ignore any BadNoSubscription responses.
            self.logger.info("BadNoSubscription received, ignoring because it's probably valid.")
            return
        # parse publish response
        try:
            response = struct_from_binary(ua.PublishResponse, data)
            self.logger.debug(response)
        except Exception:
            # INFO: catching the exception here might be obsolete because we already
            #       catch BadTimeout above. However, it's not really clear what this code
            #       does so it stays in, doesn't seem to hurt.
            self.logger.exception("Error parsing notification from server")
            # send publish request ot server so he does stop sending notifications
            await self.publish([])
            return
        # look for callback
        try:
            callback = self._publish_callbacks[response.Parameters.SubscriptionId]
        except KeyError:
            self.logger.warning("Received data for unknown subscription: %s ", response.Parameters.SubscriptionId)
            return
        # do callback
        try:
            await callback(response.Parameters)
        except Exception:
            # we call client code, catch everything!
            self.logger.exception("Exception while calling user callback: %s")

    async def create_monitored_items(self, params):
        self.logger.info("create_monitored_items")
        request = ua.CreateMonitoredItemsRequest()
        request.Parameters = params
        data = await self.protocol.send_request(request)
        response = struct_from_binary(ua.CreateMonitoredItemsResponse, data)
        self.logger.debug(response)
        response.ResponseHeader.ServiceResult.check()
        return response.Results

    async def delete_monitored_items(self, params):
        self.logger.info("delete_monitored_items")
        request = ua.DeleteMonitoredItemsRequest()
        request.Parameters = params
        data = await self.protocol.send_request(request)
        response = struct_from_binary(ua.DeleteMonitoredItemsResponse, data)
        self.logger.debug(response)
        response.ResponseHeader.ServiceResult.check()
        return response.Results

    async def add_nodes(self, nodestoadd):
        self.logger.info("add_nodes")
        request = ua.AddNodesRequest()
        request.Parameters.NodesToAdd = nodestoadd
        data = await self.protocol.send_request(request)
        response = struct_from_binary(ua.AddNodesResponse, data)
        self.logger.debug(response)
        response.ResponseHeader.ServiceResult.check()
        return response.Results

    async def add_references(self, refs):
        self.logger.info("add_references")
        request = ua.AddReferencesRequest()
        request.Parameters.ReferencesToAdd = refs
        data = await self.protocol.send_request(request)
        response = struct_from_binary(ua.AddReferencesResponse, data)
        self.logger.debug(response)
        response.ResponseHeader.ServiceResult.check()
        return response.Results

    async def delete_references(self, refs):
        self.logger.info("delete")
        request = ua.DeleteReferencesRequest()
        request.Parameters.ReferencesToDelete = refs
        data = await self.protocol.send_request(request)
        response = struct_from_binary(ua.DeleteReferencesResponse, data)
        self.logger.debug(response)
        response.ResponseHeader.ServiceResult.check()
        return response.Parameters.Results

    async def delete_nodes(self, params):
        self.logger.info("delete_nodes")
        request = ua.DeleteNodesRequest()
        request.Parameters = params
        data = await self.protocol.send_request(request)
        response = struct_from_binary(ua.DeleteNodesResponse, data)
        self.logger.debug(response)
        response.ResponseHeader.ServiceResult.check()
        return response.Results

    async def call(self, methodstocall):
        request = ua.CallRequest()
        request.Parameters.MethodsToCall = methodstocall
        data = await self.protocol.send_request(request)
        response = struct_from_binary(ua.CallResponse, data)
        self.logger.debug(response)
        response.ResponseHeader.ServiceResult.check()
        return response.Results

    async def history_read(self, params):
        self.logger.info("history_read")
        request = ua.HistoryReadRequest()
        request.Parameters = params
        data = await self.protocol.send_request(request)
        response = struct_from_binary(ua.HistoryReadResponse, data)
        self.logger.debug(response)
        response.ResponseHeader.ServiceResult.check()
        return response.Results

    async def modify_monitored_items(self, params):
        self.logger.info("modify_monitored_items")
        request = ua.ModifyMonitoredItemsRequest()
        request.Parameters = params
        data = await self.protocol.send_request(request)
        response = struct_from_binary(ua.ModifyMonitoredItemsResponse, data)
        self.logger.debug(response)
        response.ResponseHeader.ServiceResult.check()
        return response.Results
