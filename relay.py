import asyncio
import base64
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

import websockets
from dataclasses_json import dataclass_json
from state import AppState


def setup_logger():
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    console_logger = logging.StreamHandler()
    console_logger.setFormatter(
        logging.Formatter(
            "%(asctime)s - [%(levelname)-4.4s] - [%(threadName)-7.7s] - [%(name)-20.20s] - %(message)s"
        )
    )
    logger.addHandler(console_logger)
    return logger


def basic_auth_header(username, password):
    user_pass = f"{username}:{password}"
    basic_credentials = base64.b64encode(user_pass.encode()).decode()
    return ("Authorization", f"Basic {basic_credentials}")


@dataclass_json
@dataclass
class MetaInformation:
    event: Literal["Connection", "Disconnection"]
    payload: dict = None
    timestamp: str = field(
        default_factory=lambda: datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    )


class WebSocketRelay:
    def __init__(self):
        self.internal_queue = asyncio.Queue()
        self.injected_message_ids = []
        self.logger = setup_logger()
        self.csms_url, self.csms_id, self.csms_pass = None, None, None
        self.csms_ws, self.cp_ws = None, None
        self.cp_id, cp_ws_subp = None, None

    async def load_csms_from_redis(self) -> bool:
        state = AppState.instantiate()

        if not state.csms_info:
            self.logger.error("CSMS info not found in Redis (ocpp-relay:csms_info)")
            state.relay_configured = False
            return False

        # state.csms_info may already be a dict because of eval()
        if isinstance(state.csms_info, dict):
            csms_info = state.csms_info
        else:
            try:
                import json
                csms_info = json.loads(state.csms_info)
            except Exception as e:
                self.logger.error(f"Invalid CSMS info JSON in Redis: {e}")
                state.relay_configured = False
                return False

        self.csms_url = csms_info.get("url", "")
        self.csms_id = csms_info.get("id", "")
        self.csms_pass = csms_info.get("pass", "")

        if not self.csms_url:
            self.logger.error("CSMS URL missing in Redis configuration")
            state.relay_configured = False
            return False

        state.relay_configured = True

        self.logger.info(f"Loaded CSMS config from Redis. URL={self.csms_url}")
        return True

    async def _relay(self, source_ws, target_ws, source_name, target_name):
        while True:
            try:
                message = await source_ws.recv()
                json_message = json.loads(message)
                self.internal_queue.put_nowait(message)
                if json_message[1] not in self.injected_message_ids:
                    await target_ws.send(message)
                    self.logger.info(
                        f"Relayed message from {source_name} to {target_name} ({json_message[1]})"
                    )
            except websockets.exceptions.ConnectionClosed:
                self.logger.info(f"{source_name} connection closed.")
                self.internal_queue.put_nowait(
                    str(MetaInformation(event="Disconnection").to_json())
                )
                break

    async def on_connect(self, ws, path):
        path = path.strip("/")
        self.logger.info(f"WebSocket OnConnect for path: {path}")

        if "streamlit" in path:
            _, csms_info_b64 = path.split("/")
            csms_info = json.loads(base64.b64decode(csms_info_b64).decode("ascii"))
            self.csms_url, self.csms_id, self.csms_pass = (
                csms_info["url"],
                csms_info["id"],
                csms_info["pass"],
            )
            self.logger.info(
                f"Relay will connect to CSMS at: {self.csms_url} when it receives a connection from ChargePoint"
            )
            while True:
                message = await self.internal_queue.get()
                await ws.send(message)

        elif "inject" in path:
            _, direction = path.split("/")
            request = await ws.recv()
            json_request = json.loads(request)
            if direction == "csms-cp":
                self.logger.info(f"Injecting CSMS → CP: {request}")
                self.injected_message_ids.append(json_request[1])
                await self.cp_ws.send(request)
                self.internal_queue.put_nowait(request)
                # response is handled by the _relay() method, avoiding two consumers/recv() of the websocket
            elif direction == "cp-csms":
                self.logger.info(f"Injecting CP → CSMS: {request}")
                self.injected_message_ids.append(json_request[1])
                await self.csms_ws.send(request)
                self.internal_queue.put_nowait(request)
                # response is handled by the _relay() method, avoiding two consumers/recv() of the websocket

        else:
            charge_point_id = path.strip("/")
            cp_ws = ws
            try:
                ws_subprotocol = cp_ws.request_headers["Sec-WebSocket-Protocol"]
            except KeyError:
                self.logger.error(
                    "Client didn't specify any sub-protocol. A sub-protocol is required for OCPP. Closing Connection"
                )
                return await cp_ws.close()

            self.logger.info(
                f"Received a new connection from a ChargePoint. {charge_point_id=}"
            )

            # POINT 1: guard
            if not self.csms_url:
                self.logger.error(
                    "CSMS URL not set yet. Rejecting ChargePoint %s", charge_point_id
                )
                await cp_ws.close(code=1011, reason="CSMS not configured")
                return

            # POINT 2: correct URL composition
            csms_uri = f"{self.csms_url}{charge_point_id}"
            self.logger.info(f"Connecting to CSMS at {csms_uri}")

            self.logger.info(f"Connecting to CSMS at {self.csms_url}/{charge_point_id}")
            connection_meta_info = MetaInformation(
                event="Connection",
                payload={
                    "charge_point_id": charge_point_id,
                    "ws_subprotocol": ws_subprotocol,
                },
            )
            self.internal_queue.put_nowait(str(connection_meta_info.to_json()))

            async with websockets.connect(
                f"{self.csms_url}/{charge_point_id}",
                subprotocols=[ws_subprotocol],
                extra_headers=(
                    [basic_auth_header(self.csms_id, self.csms_pass)]
                    if all([self.csms_id, self.csms_pass])
                    else []
                ),
            ) as csms_ws:
                self.csms_ws, self.cp_ws = csms_ws, cp_ws
                await asyncio.gather(
                    self._relay(cp_ws, csms_ws, source_name="CP", target_name="CSMS"),
                    self._relay(csms_ws, cp_ws, source_name="CSMS", target_name="CP"),
                )

    async def start(self, port):
        if not await self.load_csms_from_redis():
            self.logger.error("Relay startup aborted: CSMS not configured")
            return

        server = await websockets.serve(self.on_connect, "0.0.0.0", port)
        self.logger.info(f"Relay server started on {port}")
        await server.wait_closed()


if __name__ == "__main__":
    relay = WebSocketRelay()
    asyncio.run(relay.start(8500))
