#!/usr/bin/env python3
import asyncio
import logging
import argparse
import ssl
from urllib.parse import urlencode, urlparse, urlunparse
# https://github.com/aaugustin/websockets
import websockets

logger = logging.getLogger(__name__)

class ConnIdleTimeout(Exception):
    pass

class Watchdog:
    def __init__(self, timeout, exc):
        self.timeout = timeout
        self.cnt = 0
        self.exc = exc
    
    def reset(self):
        self.cnt = 0
    
    async def start(self):
        while True:
            await asyncio.sleep(1)
            self.cnt += 1
            if self.cnt == self.timeout:
                raise self.exc

class BaseServer:
    '''
    Handle one client
    Start websocket connection
    Spin up tasks for forwarding traffic
    Shutdown on error
    '''
    def __init__(self, client, f_write_to_transport, f_conn_lost, uri, certfile, client_cert, idle_timeout):
        self.client = client
        self.shutdown = asyncio.get_running_loop().create_future()
        self.que = asyncio.Queue()
        if uri.startswith('wss://'):
            ssl_context = ssl.create_default_context(cafile = certfile)
            ssl_context.minimum_version = ssl.TLSVersion.TLSv1_2
            if client_cert:
                ssl_context.load_cert_chain(client_cert)
            ssl_param = {'ssl': ssl_context}
        else:
            ssl_param = dict()
        asyncio.create_task(self.new_client(uri, ssl_param, f_write_to_transport, f_conn_lost, idle_timeout))
    
    def data_received(self, data):
        self.que.put_nowait(data)
    
    def abort(self):
        self.shutdown.set_result(True)
    
    async def new_client(self, uri, ssl_param, f_write_to_transport, f_conn_lost, idle_timeout):
        tasks = []
        try:
            async with websockets.connect(uri, **ssl_param) as ws:
                if idle_timeout:
                    watchdog = Watchdog(idle_timeout, ConnIdleTimeout(f"Connection {self.client} has idled"))
                    tasks.append(asyncio.create_task(watchdog.start()))
                else:
                    watchdog = None
                tasks.append(asyncio.create_task(self.ws_data_sender(ws, watchdog)))
                tasks.append(asyncio.create_task(self.ws_data_receiver(ws, f_write_to_transport, watchdog)))
                done, _ = await asyncio.wait({self.shutdown, *tasks}, return_when = 'FIRST_COMPLETED')
                exc = done.pop().exception()
                if exc:
                    raise exc
        except ConnIdleTimeout as e:
            logger.info(repr(e))
        except Exception as e:
            logger.error(repr(e))
        finally:
            for t in tasks:
                t.cancel()
            if not self.shutdown.done():
                f_conn_lost(self.client)
    
    async def ws_data_sender(self, ws, watchdog):
        que = self.que
        while True:
            if watchdog:
                watchdog.reset()
            await ws.send(await que.get())
            que.task_done()
    
    async def ws_data_receiver(self, ws, f_write_to_transport, watchdog):
        while True:
            if watchdog:
                watchdog.reset()
            f_write_to_transport(await ws.recv(), self.client)

class UdpServer:
    def __init__(self, uri, certfile, client_cert, idle_timeout):
        self.base_servers = dict()
        self.args = [uri, certfile, client_cert, idle_timeout]
    
    def connection_made(self, transport):
        self.transport = transport
    
    def datagram_received(self, data, addr):
        try:
            base = self.base_servers[addr]
        except KeyError:
            logger.info(f'New UDP connection from {addr}')
            base = self.base_servers[addr] = BaseServer(addr,
                                                        self.transport.sendto,
                                                        self.upstream_lost,
                                                        *self.args
                                                       )
        base.data_received(data)
    
    def write_to_transport(self, data, addr):
        self.transport.sendto(data, addr)
    
    def upstream_lost(self, addr):
        logger.info(f'Upstream connection for UDP client {addr} is gone')
        self.base_servers.pop(addr)

class TcpServer(asyncio.Protocol):
    def __init__(self, uri, certfile, client_cert, idle_timeout):
        self.args = [uri, certfile, client_cert, idle_timeout]
        self.peername = None
        self.base = None
        self.transport = None
        super().__init__()
    
    def connection_made(self, transport):
        peername = self.peername = transport.get_extra_info('peername')
        logger.info(f'New TCP connection from {peername}')
        self.transport = transport
        self.base = BaseServer(peername,
                               self.write_to_transport,
                               self.upstream_lost,
                               *self.args
                              )
    
    def data_received(self, data):
        self.base.data_received(data)
    
    def connection_lost(self, exc):
        logger.info(f'TCP connection from {self.peername} is down: {repr(exc)}')
        self.base.abort()
    
    def write_to_transport(self, data, addr):
        self.transport.write(data)
    
    def upstream_lost(self, peername):
        self.transport.close()

def get_passwd_from_file(path):
    with open(path, 'r') as fi:
        return fi.readline().rstrip('\r\n')

def update_url_with_passwd(url, passwd):
    url = urlparse(url)
    url = url._replace(query = urlencode({'t': passwd}))
    return urlunparse(url)

async def main(listen, uri, passwd_file, certfile, client_cert, idle_timeout):
    protocol, local_addr = listen.split('://', maxsplit=1)
    local_addr = local_addr.split(':', maxsplit=1)
    local_addr = (local_addr[0], int(local_addr[1]))
    if passwd_file:
        uri = update_url_with_passwd(uri, get_passwd_from_file(passwd_file))
    if not uri.startswith('wss://'):
        logger.warning('Secure connection is disabled')
    loop = asyncio.get_running_loop()
    if protocol == 'udp':
        transport, _ = await loop.create_datagram_endpoint(lambda: UdpServer(uri, certfile, client_cert, idle_timeout),
                                                           local_addr = local_addr
                                                          )
        try:
            await loop.create_future() # Serve forever
        finally:
            transport.close()
    else:
        server = await loop.create_server(lambda: TcpServer(uri, certfile, client_cert, idle_timeout),
                                          local_addr[0], local_addr[1]
                                         )
        async with server:
            await server.serve_forever()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Wstunnel client')
    parser.add_argument('--url', type=str, required=True, help='URL')
    parser.add_argument('-l', '--listen', type=str, required=True, help='Listen address')
    parser.add_argument('-p', '--passwd', type=str, metavar='FILE', help='File containing one line of password to authenticate to the proxy server')
    parser.add_argument('-i', '--idle-timeout', type=int, default=120, help='Seconds to wait before an idle UDP connection being killed')
    parser.add_argument('-s', '--ca-certs', type=str, metavar='ca.pem', help="Server CA certificates in PEM format to verify against")
    parser.add_argument('-c', '--client-cert', type=str, metavar='client.pem', help="Client certificate in PEM format with private key")
    parser.add_argument('--log-file', type=str, metavar='FILE', help='Log to FILE')
    parser.add_argument('--log-level', type=str, default="info", choices=['debug', 'info', 'error', 'critical'], help='Log level')
    args = parser.parse_args()
    if args.log_level == 'debug':
        log_level = logging.DEBUG
    elif args.log_level == 'error':
        log_level = logging.ERROR
    elif args.log_level == 'critical':
        log_level = logging.CRITICAL
    else:
        log_level = logging.INFO
    logging_config_param = {'format': '%(levelname)s::%(asctime)s::%(filename)s:%(lineno)d::%(message)s',
                            'datefmt': '%Y-%m-%d %H:%M:%S'
                           }
    if args.log_file:
        logging_config_param['filename'] = args.log_file
    logging.basicConfig(**logging_config_param)
    logging.getLogger(__name__).setLevel(log_level)
    asyncio.run(main(args.listen, args.url, args.passwd, args.ca_certs, args.client_cert, args.idle_timeout))
