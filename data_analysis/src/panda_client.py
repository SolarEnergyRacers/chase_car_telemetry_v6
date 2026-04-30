

import logging as lg
from   pathlib import Path
import queue
import threading
import zmq
import zmq.auth


class Panda_client:
    def __init__(self, opt: dict, panda_pipe: queue.Queue):
        print("made new panda :3")
        self.queue = panda_pipe
        keyname = opt['client']['keyname']
        serv_keyname = opt['server']['keyname']
        com_key_path =  Path(__file__).parent.parent.parent / "certificates/"
        mine_key_path = com_key_path / "mine" / f"{keyname}.key_secret"
        serv_key_path = com_key_path / "others" / f"{serv_keyname}.key"
        self.pub_key, self.priv_key = zmq.auth.load_certificate(mine_key_path)
        self.srv_key, *_            = zmq.auth.load_certificate(serv_key_path)

        self.url_ip = f"tcp://{opt['server']['host']}"
        self.url_pub = self.url_ip + f":{opt['server']['port_pub']}"
        self.url_rep = self.url_ip + f":{opt['server']['port_rep']}"

        self.subscriber = None
        self.subscriber_online = True
        self.subscribe_t = threading.Thread(target=self._subscriber)
        self.subscribe_t.start()

        self.requester = None
        self.request_end = threading.Event()
        self.request_t = threading.Thread(target=self._requester)
        self.request_t.start()

    def shutdown(self):
        """shut down servers and free their ports. 
        Returns list of names that failed to terminate"""
        self.subscriber_online = False
        self.request_end.set()
        self.subscribe_t.join(timeout=2.0)
        self.request_t.join(timeout=2.0)
        lose_ends = []
        if self.subscribe_t.is_alive():
            lose_ends.append("subscriber")
        if self.request_t.is_alive():
            lose_ends.append("requester")
        return lose_ends

    def _auth(self, context: zmq.Context, client: zmq.sugar.Socket):
        """Set up CURVE authentication for a server"""
        client.curve_secretkey = self.priv_key
        client.curve_publickey = self.pub_key
        client.curve_serverkey = self.srv_key
        return

    def _subscriber(self):
        try:
            with zmq.Context() as ctx, ctx.socket(zmq.SUB) as client:
                client: zmq.sugar.Socket
                self._auth(ctx, client)
                client.connect(self.url_pub)
                client.setsockopt(zmq.SUBSCRIBE, b'')
                self.subscriber = client
                poller = zmq.Poller()
                poller.register(client, zmq.POLLIN)
                lg.info(f"panda client subscriber online on {self.url_pub}")
                while self.subscriber_online:
                    events = dict(poller.poll(timeout = 1e3))
                    if client in events:
                        rx = client.recv_json() # todo: clean exit see server?
                        if not self.queue.full():
                            # drop data if full for whichever reason
                            self.queue.put(rx)
                client.disconnect(self.url_pub)
                lg.debug(f"panda client publisher unbound from {self.url_pub}")

            lg.info(f"panda client subscriber shut down nominally.")
            return
        except Exception as e:
            lg.error(f"panda client subscriber error: {type(e)} {e}")
            raise e
        finally:
            self.subscriber = None

    def request_datapoints(self, start_t: int, stop_t: int):
        """request all data from start to stop time (unix timestamps)"""
        if self.requester is None:
            raise RuntimeError("No requester service is running")
        try:
            self.requester.send_json({"start_t": start_t, "stop_t": stop_t})
            poller = zmq.Poller()
            poller.register(self.requester, zmq.POLLIN)
            if self.requester in dict(poller.poll(timeout = 10e3)):
                return self.requester.recv_json()
            else:
                threading.Thread(
                    target=self.requester.recv_json, 
                    daemon=True
                ).start()  # absolutely cursed: 
                # Need to rx & discard, otherwise ZMQ hangs up
                raise zmq.error.ZMQError(msg=f"request timed out")
        except zmq.error.ZMQError as e:
            return {
                "metadata": {
                    "errors": [
                        "request_datapoints() failed",
                        f"{type(e)} {e}"
                    ]
                }, 
                "csvstr": ""
            }

    def _requester(self):
        try:
            with zmq.Context() as ctx, ctx.socket(zmq.REQ) as client:
                client: zmq.sugar.Socket
                self._auth(ctx, client)
                client.connect(self.url_rep)
                # client.setsockopt(zmq.SUBSCRIBE, b'')  # todo: ???
                self.requester = client
                lg.info(f"panda client requester online on {self.url_rep}")
                self.request_end.wait()
                client.unbind(self.url_rep)
                lg.debug(f"panda client requester unbound from {self.url_rep}")

            lg.info(f"panda client requester shut down nominally.")
            return
        except Exception as e:
            lg.error(f"panda client requester error: {type(e)} {e}")
            raise e
        finally:
            self.requester = None


if __name__ == "__main__":
    # client test run
    import json
    import time
    pipe = queue.Queue()
    def update_rx(pipe: queue.Queue):
        while True:
            print(f"sub: {pipe.get()}")
    update_rx_t = threading.Thread(target=update_rx, daemon=True, args=(pipe,))
    update_rx_t.start()
    with open("options.json", "r") as opt_file:
        opt = json.load(opt_file)
    client = Panda_client(opt, pipe)
    print("starting requests")
    rx = client.request_datapoints(111, 222)
    print(rx.get('metadata', "metadata missing ._."))
    print(f"-> N csvlen chars = {len(rx.get('csvstr', ''))}")
    rx = client.request_datapoints(0, 0)
    print(rx.get('metadata', "metadata missing ._."))
    print(f"-> N csvlen chars = {len(rx.get('csvstr', ''))}")
    input("press enter to terminate cleanly...")
    lose = client.shutdown()
    if lose:
        print(f"endeded, failed to terminate: {lose}")
    else:
        print("ended all threads successfully.")
