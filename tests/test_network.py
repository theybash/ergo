"""Network health in pairs, and the one line that names the hop.

The parsing is tested against real output captured from this laptop, which is the only kind worth
testing against: every one of these formats has a quirk that invented text would not have.
"""

from commandcenter import network
from tests.harness import Case

PING_OK = """PING 192.168.0.1 (192.168.0.1) 56(84) bytes of data.

--- 192.168.0.1 ping statistics ---
3 packets transmitted, 3 received, 0% packet loss, time 2004ms
rtt min/avg/max/mdev = 1.213/1.359/1.507/0.120 ms
"""

PING_GONE = """PING 192.168.0.1 (192.168.0.1) 56(84) bytes of data.

--- 192.168.0.1 ping statistics ---
3 packets transmitted, 0 received, 100% packet loss, time 2043ms
"""

WIRELESS = """Inter-| sta-|   Quality        |   Discarded packets               | Missed | WE
 face | tus | link level noise |  nwid  crypt   frag  retry   misc | beacon | 22
wlp0s20f3: 0000   57.  -53.  -256        0      0      0    814    780        0
"""

NMCLI = "no:Neighbour:40:54 Mbit/s\nyes:PATH:71:270 Mbit/s\nno:Other:22:130 Mbit/s"

JOURNAL = """Aug 19 09:00:01 host NetworkManager[905]: <info> manager: NetworkManager state is now CONNECTED_GLOBAL
Aug 19 09:31:44 host NetworkManager[905]: <info> manager: NetworkManager state is now DISCONNECTED
Aug 19 09:32:02 host NetworkManager[905]: <info> manager: NetworkManager state is now CONNECTED_GLOBAL
Aug 19 10:14:07 host NetworkManager[905]: <info> manager: NetworkManager state is now DISCONNECTED
"""


class Parsing(Case):
    def setUp(self):
        super().setUp()
        network.reset()
        self.addCleanup(network.reset)

    def test_a_ping_reply(self):
        got = network._parse_ping(PING_OK)
        self.assertEqual(got, {"avg_ms": 1.4, "max_ms": 1.5, "loss_pct": 0.0})

    def test_total_loss_is_an_answer_not_a_missing_measurement(self):
        got = network._parse_ping(PING_GONE)
        self.assertEqual(got["loss_pct"], 100.0)
        self.assertIsNone(got["avg_ms"])

    def test_the_active_network_is_the_one_reported(self):
        got = network.parse_nmcli(NMCLI)
        self.assertEqual(got, {"ssid": "PATH", "signal_pct": 71, "rate_mbit": 270})

    def test_no_network_at_all(self):
        self.assertIsNone(network.parse_nmcli("no:Other:22:130 Mbit/s")["ssid"])
        self.assertIsNone(network.parse_nmcli("")["ssid"])

    def test_the_retry_counter_which_is_the_useful_one(self):
        got = network.parse_wireless(WIRELESS)
        self.assertEqual(got["retries"], 814)
        self.assertEqual(got["level_dbm"], -53)
        self.assertEqual(got["device"], "wlp0s20f3")

    def test_a_machine_with_no_wireless_says_nothing_rather_than_nought(self):
        got = network.parse_wireless("Inter-| sta-|\n face | tus |\n")
        self.assertIsNone(got["retries"])

    def test_counting_disconnections_in_one_boot(self):
        self.assertEqual(network.count_disconnects(JOURNAL), 2)
        self.assertEqual(network.count_disconnects(""), 0)

    def test_the_gateway_is_read_from_the_route_table(self):
        real = network._run
        network._run = lambda argv, timeout=6.0: (
            "default via 192.168.0.1 dev wlp0s20f3 proto dhcp src 192.168.0.101 metric 600\n"
            "192.168.0.0/24 dev wlp0s20f3 proto kernel scope link\n")
        self.addCleanup(lambda: setattr(network, "_run", real))
        self.assertEqual(network.gateway(), {"address": "192.168.0.1", "device": "wlp0s20f3"})

    def test_no_default_route_says_so(self):
        real = network._run
        network._run = lambda argv, timeout=6.0: "192.168.0.0/24 dev x proto kernel scope link\n"
        self.addCleanup(lambda: setattr(network, "_run", real))
        self.assertIsNone(network.gateway()["address"])


class Verdict(Case):
    """One line naming the hop. A pair is only useful to somebody who reads it as a pair."""

    def line(self, gateway_ms, internet_ms, loss=0.0, retries=None, gateway_loss=0.0):
        return network.verdict({
            "latency": {"gateway": {"avg_ms": gateway_ms, "max_ms": gateway_ms,
                                    "loss_pct": gateway_loss},
                        "internet": {"avg_ms": internet_ms, "max_ms": internet_ms,
                                     "loss_pct": loss}},
            "wifi": {"retries": retries}})

    def test_a_healthy_evening(self):
        self.assertIn("fine", self.line(1.4, 6.2))

    def test_the_router_out_of_memory_which_is_this_laptops_actual_fault(self):
        """100ms to the hall with the line unchanged. Nothing is wrong with the connection."""
        said = self.line(100.0, 23.0)
        self.assertIn("router is the problem", said)
        self.assertIn("100ms", said)

    def test_a_slow_line_with_a_healthy_router(self):
        self.assertIn("line is slow", self.line(2.0, 400.0))

    def test_the_router_answering_but_nothing_beyond_it(self):
        self.assertIn("line beyond it", self.line(2.0, None, loss=100.0))

    def test_the_router_not_answering_at_all(self):
        self.assertIn("not answering", self.line(None, 5.0, gateway_loss=100.0))

    def test_retries_are_mentioned_when_nothing_else_is_wrong(self):
        self.assertIn("retries", self.line(2.0, 8.0, retries=9000))

    def test_retries_do_not_drown_out_a_real_fault(self):
        self.assertIn("router is the problem", self.line(90.0, 8.0, retries=9000))

    def test_nothing_measured_says_nothing_measured(self):
        self.assertEqual(network.verdict({}), "no measurement")


class Sharing(Case):
    def setUp(self):
        super().setUp()
        network.reset()
        self.addCleanup(network.reset)

    def test_the_measurement_is_taken_once_and_shared(self):
        """Three pings per page load per watcher would be traffic on the thing being measured."""
        calls = []
        real = network._run
        network._run = lambda argv, timeout=6.0: calls.append(argv[0]) or (
            "default via 1.2.3.4 dev eth0\n" if argv[0] == "ip" else PING_OK)
        self.addCleanup(lambda: setattr(network, "_run", real))
        first = network.snapshot()
        for _ in range(5):
            network.snapshot()
        self.assertEqual(calls.count("ip"), 1)
        self.assertIn("fine", first["verdict"])

    def test_a_speed_test_is_never_part_of_the_snapshot(self):
        real = network._run
        network._run = lambda argv, timeout=6.0: (
            "default via 1.2.3.4 dev eth0\n" if argv[0] == "ip" else PING_OK)
        self.addCleanup(lambda: setattr(network, "_run", real))
        self.assertIsNone(network.snapshot()["speed"], "it costs bandwidth; it needs a button")
