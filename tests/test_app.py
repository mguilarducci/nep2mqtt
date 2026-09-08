"""Request-path tests: what the inverter sends in, what it gets back.

The inverter treats the response as a time sync, so the contract that matters
is that it always gets fourteen ASCII digits - even when the frame was garbage
or MQTT is broken. Losing a sample costs one data point; failing to answer makes
the inverter retry and drift.
"""
import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from nep2mqtt.app import create_app  # noqa: E402
from nep2mqtt.config import Settings  # noqa: E402

FIXTURE_DIR = pathlib.Path(__file__).parent / "fixtures"
REAL_FRAME = (FIXTURE_DIR / "unit-a-4ch.bin").read_bytes()


class FakePublisher:
    """Stands in for the MQTT publisher; records what it was asked to send."""

    def __init__(self, explode=False):
        self.readings = []
        self.sources = []
        self.explode = explode

    def connect(self):
        pass

    def disconnect(self):
        pass

    def publish(self, reading, source_ip=None):
        if self.explode:
            raise RuntimeError("broker is down")
        self.readings.append(reading)
        self.sources.append(source_ip)


def build_with_upstream(publisher, upstream_body):
    """App whose upstream answers 200 with whatever body is given."""
    settings = Settings(mqtt_host="unused", upstream_ip="192.0.2.1")
    app = create_app(settings, publisher=publisher)

    async def fake_forward(request, body, client, settings):
        return upstream_body

    import nep2mqtt.app as module
    module._forward = fake_forward
    return TestClient(app)


def build(publisher):
    # upstream_ip=None keeps the vendor cloud out of the tests entirely.
    settings = Settings(mqtt_host="unused", upstream_ip=None)
    return TestClient(create_app(settings, publisher=publisher))


class TestRealFrame(unittest.TestCase):
    def setUp(self):
        self.publisher = FakePublisher()
        self.client = build(self.publisher)

    def test_frame_is_published(self):
        with self.client as client:
            client.post("/t.php", content=REAL_FRAME)
        self.assertEqual(len(self.publisher.readings), 1)
        self.assertEqual(self.publisher.readings[0]["serial"], "AA000001")

    def test_response_is_a_timestamp(self):
        with self.client as client:
            response = client.post("/t.php", content=REAL_FRAME)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.content), 14)
        self.assertTrue(response.content.decode().isdigit())

    def test_source_address_is_passed_through(self):
        # The address is not in the frame; it has to come from the connection.
        with self.client as client:
            client.post("/t.php", content=REAL_FRAME)
        self.assertEqual(len(self.publisher.sources), 1)
        self.assertTrue(self.publisher.sources[0])

    def test_forwarded_header_wins_over_the_tcp_peer(self):
        # Behind a proxy the peer is the proxy; the inverter is in the header.
        with self.client as client:
            client.post(
                "/t.php",
                content=REAL_FRAME,
                headers={"X-Forwarded-For": "10.0.210.156, 172.21.0.1"},
            )
        self.assertEqual(self.publisher.sources[0], "10.0.210.156")

    def test_falls_back_to_the_peer_without_the_header(self):
        with self.client as client:
            client.post("/t.php", content=REAL_FRAME)
        self.assertTrue(self.publisher.sources[0])
        self.assertNotIn(",", self.publisher.sources[0])

    def test_any_path_is_accepted(self):
        # Other NEP models are reported to post elsewhere; the payload
        # identifies a frame, not the URL.
        with self.client as client:
            response = client.post("/i.php", content=REAL_FRAME)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(self.publisher.readings), 1)


class TestUpstreamResponses(unittest.TestCase):
    """Whatever upstream does, the inverter still needs its time sync back."""

    def tearDown(self):
        import importlib
        import nep2mqtt.app
        importlib.reload(nep2mqtt.app)

    def test_upstream_body_is_passed_through(self):
        with build_with_upstream(FakePublisher(), b"20260908152902") as client:
            response = client.post("/t.php", content=REAL_FRAME)
        self.assertEqual(response.content, b"20260908152902")

    def test_bodiless_request_is_not_forwarded(self):
        # The healthcheck GETs this service every minute; relaying that would
        # hammer the vendor for nothing.
        forwarded = []

        settings = Settings(mqtt_host="unused", upstream_ip="192.0.2.1")
        app = create_app(settings, publisher=FakePublisher())

        async def spy(request, body, client, settings):
            forwarded.append(body)
            return b"upstream"

        import nep2mqtt.app as module
        module._forward = spy

        with TestClient(app) as client:
            response = client.get("/")
        self.assertEqual(forwarded, [])
        self.assertEqual(len(response.content), 14)

    def test_empty_upstream_body_falls_back_to_our_clock(self):
        # A 200 with nothing in it leaves the inverter with no time at all,
        # which is the same outcome as no answer - so answer it ourselves.
        with build_with_upstream(FakePublisher(), b"") as client:
            response = client.post("/t.php", content=REAL_FRAME)
        self.assertEqual(len(response.content), 14)
        self.assertTrue(response.content.decode().isdigit())


class TestDegradedCases(unittest.TestCase):
    def test_garbage_still_gets_a_timestamp(self):
        publisher = FakePublisher()
        with build(publisher) as client:
            response = client.post("/t.php", content=b"not a frame")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.content), 14)
        self.assertEqual(publisher.readings, [])

    def test_empty_body_publishes_nothing(self):
        publisher = FakePublisher()
        with build(publisher) as client:
            response = client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.content), 14)
        self.assertEqual(publisher.readings, [])

    def test_broker_failure_does_not_cost_the_reply(self):
        # The inverter must still get its time sync when MQTT is down.
        publisher = FakePublisher(explode=True)
        with build(publisher) as client:
            response = client.post("/t.php", content=REAL_FRAME)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.content), 14)


if __name__ == "__main__":
    unittest.main(verbosity=2)
