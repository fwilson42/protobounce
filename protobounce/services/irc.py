from .. import parser
from ..proto import irc_pb2
from collections import defaultdict
from concurrent import futures
from queue import Queue

import argparse
import grpc
import logging
import os
import os.path
import socket
import ssl
import threading
import time

pending = defaultdict(Queue)
send_pending = Queue()
conn = None

logging.basicConfig(level=logging.DEBUG)

class IRCConnectionServicer(irc_pb2.IRCConnectionServicer):
    def MessageStream(self, request, context):
        connection_id = (max(pending.keys()) + 1) if pending else 1
        queue = pending[connection_id]
        while True:
            data = queue.get()
            if (not request.filter.verbs) or (data.verb in request.filter.verbs):
                yield data
            queue.task_done()

    def SendMessage(self, request, context):
        send_pending.put(request)
        return irc_pb2.SentResponse()

    def DoConnection(self, request, context):
        if not conn.started:
            conn.connect()
            conn.listen()
            return irc_pb2.ConnectionResponse(result=True)
        return irc_pb2.ConnectionResponse(result=False)

class IRCConnection(object):
    def __init__(self, host, port, use_ssl):
        self.handlers = {"PING": self.handle_ping}
        self.host = host
        self.port = port
        self.started = False

        self.s = ssl.SSLSocket(socket.socket()) if use_ssl else socket.socket()

        self.read_thread = threading.Thread(target=self.handle_socket_read)
        self.write_thread = threading.Thread(target=self.handle_socket_write)

    def connect(self):
        self.started = True
        self.s.connect((self.host, self.port))

    def handle_ping(self, writeln, msg):
        writeln("PONG {}".format(msg.arguments[0] if msg.arguments else ""))

    def listen(self):
        self.read_thread.start()
        self.write_thread.start()

    def handle(self, msg):
        if msg.verb in self.handlers:
            self.handlers[msg.verb](self.writeln, msg)

        else:
            for _, q in pending.items():
                q.put(msg)

    def writeln(self, x):
        logging.debug("Send: {}".format(x))

        if not isinstance(x, bytes):
            x = x.encode()
        self.s.send(x + b"\r\n")

    def handle_socket_read(self):
        endl = b"\n"
        buf = b""
        while True:
            while endl in buf:
                msg, buf = buf.split(endl, maxsplit=1)
                msg = msg.strip().decode()
                logging.debug("Recv: {}".format(msg))
                parsed = parser.message_from_str(msg)
                self.handle(parsed)

            buf += self.s.recv(256)

    def handle_socket_write(self):
        while True:
            msg = send_pending.get()
            unparsed = parser.str_from_message(msg)
            self.writeln(unparsed)
            send_pending.task_done()

def irc_start(host, port, use_ssl):
    c = IRCConnection(host, port, use_ssl)
    return c

def create_server(port):
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=10))
    irc_pb2.add_IRCConnectionServicer_to_server(IRCConnectionServicer(), server)
    server.add_insecure_port(port)
    return server

def serve_forever(server):
    server.start()
    while True:
        time.sleep(86400)

if __name__ == '__main__':
    import argparse
    arg_parser = argparse.ArgumentParser(description="Run the protobounce IRC server.")
    arg_parser.add_argument("sockets", help="Directory of protobounce sockets")
    arg_parser.add_argument("host", help="IRC server to connect to")
    arg_parser.add_argument("port", type=int, help="IRC port to use")
    arg_parser.add_argument("--secure", dest="ssl", action="store_true", help="Use SSL/TLS to connect")
    args = arg_parser.parse_args()

    server = create_server("unix:" + os.path.join(args.sockets, "irc.sock"))
    conn = irc_start(args.host, args.port, args.ssl)
    serve_forever(server)
