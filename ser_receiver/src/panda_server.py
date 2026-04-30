

import logging as lg
from   pathlib import Path
import threading
import time
from   typing import Callable
import zmq
import zmq.auth
from   zmq.auth.thread import ThreadAuthenticator


class Panda_server:
    def __init__(self, opt: dict, responder_func: Callable[[dict], dict]):
        self.get_response = responder_func
        keyname = opt['server']['keyname']
        com_key_path =  Path(__file__).parent.parent.parent / "certificates/"
        self.others_key_path = com_key_path / "others"
        mine_key_path = com_key_path / "mine" / f"{keyname}.key_secret"
        self.pub_key, self.priv_key = zmq.auth.load_certificate(mine_key_path)

        self.url_ip = f"tcp://{opt['server']['host']}" 
        self.url_pub = self.url_ip + f":{opt['server']['port_pub']}"
        self.url_rep = self.url_ip + f":{opt['server']['port_rep']}"

        self.publisher = None
        self.publish_end = threading.Event()
        self.publish_t = threading.Thread(target=self._publisher)
        self.publish_t.start()

        self.responder = None
        self.responder_online = True
        self.respond_t = threading.Thread(target=self._responder)
        self.respond_t.start()

    def shutdown(self):
        """shut down servers and free their ports. 
        Returns list of names that failed to terminate"""
        self.publish_end.set()
        self.responder_online = False
        self.publish_t.join(2.0)
        self.respond_t.join(2.0)
        lose_ends = []
        if self.publish_t.is_alive():
            lose_ends.append("publisher")
        if self.respond_t.is_alive():
            lose_ends.append("responder")
        return lose_ends

    def _auth(self, context: zmq.Context, server: zmq.sugar.Socket):
        """Set up CURVE authentication for a server"""
        auth = zmq.auth.thread.ThreadAuthenticator(context)
        auth.configure_curve(domain="*", location=self.others_key_path)
        auth.start()  # most important command, otherwise auth is ignored
        server.curve_secretkey = self.priv_key
        server.curve_publickey = self.pub_key
        server.curve_server = True
        return auth

    def publish(self, data: dict):
        """Non-blocking, dropping packages on overload."""
        if not self.publisher is None:
            # pkg_id = PKG_IDs["dp"]
            self.publisher.send_json(data)
            # self.publisher.send(pkg_id + data)
            return True
        return False

    def _publisher(self):
        """Set up server and hold it alive until shutdown"""
        try:
            with zmq.Context() as ctx, ctx.socket(zmq.PUB) as serv:
                serv: zmq.sugar.Socket
                self._auth(ctx, serv)
                serv.bind(self.url_pub)
                self.publisher = serv
                lg.info(f"panda server publisher online on {self.url_pub}")
                self.publish_end.wait()
                serv.unbind(self.url_pub)
                lg.debug(f"panda server publisher unbound from {self.url_pub}")

            lg.info(f"panda server publisher shut down nominally.")
            return
        except Exception as e:
            lg.error(f"panda server publisher error: {type(e)} {e}")
            raise e
        finally:
            self.publisher = None

    def _responder(self):
        """Set up server and hold it alive until shutdown"""
        try:
            with zmq.Context() as ctx, ctx.socket(zmq.REP) as serv:
                serv: zmq.sugar.Socket
                self._auth(ctx, serv)
                serv.bind(self.url_rep)
                self.responder = serv
                poller = zmq.Poller()
                poller.register(serv, zmq.POLLIN)
                lg.info(f"panda server responder online on {self.url_rep}")
                while self.responder_online:
                    events = dict(poller.poll(timeout = 1e3))
                    # poll to allow shutdown.
                    # Cleaner: add 2nd (internal) socket for shutdown command
                    if serv in events:
                        rx = serv.recv_json()
                        serv.send_json(self.get_response(rx))
                serv.unbind(self.url_rep)
                lg.debug(f"panda server responder unbound from {self.url_rep}")

            lg.info(f"panda server responder shut down nominally.")
            return
        except Exception as e:
            lg.error(f"panda server responder error: {type(e)} {e}")
            raise e
        finally:
            self.responder = None 
