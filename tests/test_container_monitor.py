import json
import shutil
import socketserver
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse

from container_monitor import monitor
from container_monitor.docker import DockerClient, DockerUnavailable, demux
from container_monitor.models import Alert, Check, Status
from container_monitor.signals import status_changed

GIB = 1024**3


def monitor_settings(**extra):
    return override_settings(
        CONTAINER_MONITOR={"DOCKER_URL": None, "ALERTS": [], **extra}
    )


def fake_proc(memory_available_kb=6 * 1024 * 1024, swap_free_kb=4 * 1024 * 1024):
    proc = Path(tempfile.mkdtemp(prefix="monitor-proc-"))
    (proc / "stat").write_text("cpu  100 0 100 800 0 0 0 0 0 0\n")
    (proc / "meminfo").write_text(
        "MemTotal:        8000000 kB\n"
        f"MemAvailable:    {memory_available_kb} kB\n"
        "SwapTotal:       4194304 kB\n"
        f"SwapFree:        {swap_free_kb} kB\n"
    )
    (proc / "uptime").write_text("90061.50 300000.00\n")
    return proc


def container(
    name, *, state="running", health="", restarts=0, memory_usage=200 * 1024**2
):
    return {
        "summary": {
            "Id": f"id-{name}",
            "Names": [f"/{name}"],
            "Image": "img",
            "State": state,
            "Status": "Up 2 hours" if state == "running" else "Exited (1)",
        },
        "inspect": {
            "State": {
                "OOMKilled": False,
                "ExitCode": 1 if state != "running" else 0,
                **({"Health": {"Status": health}} if health else {}),
            },
            "RestartCount": restarts,
            "HostConfig": {"Memory": GIB, "NanoCpus": 10**9},
        },
        "stats": {
            "cpu_stats": {
                "cpu_usage": {"total_usage": 300},
                "system_cpu_usage": 2000,
                "online_cpus": 4,
            },
            "precpu_stats": {
                "cpu_usage": {"total_usage": 100},
                "system_cpu_usage": 1000,
            },
            "memory_stats": {
                "usage": memory_usage + 10 * 1024**2,
                "stats": {"inactive_file": 10 * 1024**2},
            },
        },
    }


class FakeDocker:
    def __init__(self, *entries, unavailable=""):
        self.entries = {entry["summary"]["Id"]: entry for entry in entries}
        self.unavailable = unavailable

    def containers(self):
        if self.unavailable:
            raise DockerUnavailable(self.unavailable)
        return [entry["summary"] for entry in self.entries.values()]

    def inspect(self, container):
        return self.entries[container]["inspect"]

    def stats(self, container):
        return self.entries[container]["stats"]


class MonitorCase(TestCase):
    def setUp(self):
        self.proc = fake_proc()
        self.addCleanup(shutil.rmtree, self.proc, True)

    def check(self, *entries, unavailable="", proc=None):
        return monitor.run_check(
            client=FakeDocker(*entries, unavailable=unavailable),
            proc_root=str(proc or self.proc),
            sample_seconds=0,
        )


@monitor_settings()
class VerdictTests(MonitorCase):
    def test_a_quiet_host_is_ok(self):
        check = self.check(container("web"), container("db"))
        self.assertEqual(check.status, Status.OK, check.reasons)
        web = next(entry for entry in check.containers if entry["name"] == "web")
        self.assertEqual(web["memory_percent"], round(200 / 1024 * 100, 1))
        self.assertEqual(web["cpu_percent"], 80.0)

    def test_a_stopped_container_is_danger(self):
        check = self.check(container("redis", state="exited"))
        self.assertEqual(check.status, Status.DANGER)
        self.assertIn("redis is exited (exit code 1)", check.reasons)

    def test_only_matching_containers_are_judged(self):
        with monitor_settings(CONTAINERS=["shop-*"]):
            check = self.check(
                container("ci-job", state="exited"), container("shop-web")
            )
        self.assertEqual(check.status, Status.OK)
        self.assertFalse(
            next(e for e in check.containers if e["name"] == "ci-job")["judged"]
        )

    def test_memory_near_the_container_limit(self):
        warning = self.check(container("worker", memory_usage=int(0.9 * GIB)))
        danger = self.check(container("worker", memory_usage=int(0.97 * GIB)))
        self.assertEqual(
            (warning.status, danger.status), (Status.WARNING, Status.DANGER)
        )

    def test_restarts_since_the_last_check_warn(self):
        self.check(container("worker", restarts=1))
        check = self.check(container("worker", restarts=4))
        self.assertIn("worker restarted 3x since the last check", check.reasons)

    def test_unhealthy_is_danger(self):
        self.assertEqual(
            self.check(container("api", health="unhealthy")).status, Status.DANGER
        )

    def test_host_memory_and_swap_are_judged(self):
        check = self.check(
            proc=fake_proc(memory_available_kb=400_000, swap_free_kb=2 * 1024 * 1024)
        )
        self.assertEqual(check.status, Status.DANGER)
        self.assertTrue(check.reasons[0].startswith("host memory 95"))
        self.assertTrue(any(reason.startswith("swap 50") for reason in check.reasons))

    def test_without_docker_the_host_is_still_checked(self):
        check = self.check(unavailable="connection refused")
        self.assertEqual(check.status, Status.WARNING)
        self.assertIn("memory_percent", check.host)


@monitor_settings(
    ALERTS=[{"BACKEND": "container_monitor.alerts.EmailAlert"}], REPEAT_ALERT_MINUTES=60
)
class AlertTests(MonitorCase):
    def test_alerts_on_change_and_recovery_not_on_every_check(self):
        received = []
        status_changed.connect(
            lambda **kwargs: received.append(kwargs["status"]), weak=False
        )
        self.addCleanup(status_changed.receivers.clear)

        self.check(container("db"))
        self.check(container("db", state="exited"))
        self.check(container("db", state="exited"))
        self.check(container("db"))

        self.assertEqual(
            [message.subject for message in mail.outbox],
            [
                "[Django] [DANGER] server health",
                "[Django] [OK] server health recovered",
            ],
        )
        self.assertEqual(received, ["danger", "ok"])
        self.assertEqual(Alert.objects.filter(delivered=True).count(), 2)

    def test_a_lasting_problem_is_repeated(self):
        self.check(container("db", state="exited"))
        Alert.objects.update(sent_at="2000-01-01T00:00:00Z")
        self.check(container("db", state="exited"))
        self.assertEqual(len(mail.outbox), 2)

    def test_min_status_filters_channels(self):
        with monitor_settings(
            ALERTS=[
                {
                    "BACKEND": "container_monitor.alerts.EmailAlert",
                    "MIN_STATUS": "danger",
                }
            ]
        ):
            self.check(container("worker", memory_usage=int(0.9 * GIB)))
        self.assertEqual(mail.outbox, [])

    def test_a_failing_channel_is_recorded_not_raised(self):
        with monitor_settings(
            ALERTS=[
                {
                    "BACKEND": "container_monitor.alerts.WebhookAlert",
                    "OPTIONS": {"url": "http://127.0.0.1:9/nowhere", "timeout": 1},
                }
            ]
        ):
            self.check(container("db", state="exited"))
        alert = Alert.objects.get()
        self.assertFalse(alert.delivered)
        self.assertTrue(alert.error)

    def test_the_webhook_posts_json(self):
        received = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                received.append(
                    json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                )
                self.send_response(204)
                self.end_headers()

            def log_message(self, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.handle_request, daemon=True).start()
        url = f"http://127.0.0.1:{server.server_port}/hook"
        with monitor_settings(
            ALERTS=[
                {
                    "BACKEND": "container_monitor.alerts.WebhookAlert",
                    "OPTIONS": {"url": url},
                }
            ]
        ):
            self.check(container("db", state="exited"))
        server.server_close()
        self.assertEqual(received[0]["status"], "danger")
        self.assertIn("db is exited", received[0]["text"])


class DockerClientTests(TestCase):
    def test_the_client_speaks_http_over_a_unix_socket(self):
        folder = tempfile.mkdtemp(prefix="docker-sock-")
        self.addCleanup(shutil.rmtree, folder, True)
        socket_path = f"{folder}/docker.sock"
        seen = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                seen.append(self.path)
                body = json.dumps([{"Id": "abc", "Names": ["/web"]}]).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def address_string(self):
                return "unix"

            def log_message(self, *args):
                pass

        server = socketserver.UnixStreamServer(socket_path, Handler)
        threading.Thread(target=server.handle_request, daemon=True).start()
        self.assertEqual(
            DockerClient(f"unix://{socket_path}").containers()[0]["Id"], "abc"
        )
        server.server_close()
        self.assertEqual(seen, ["/containers/json?all=true"])

    def test_an_unreachable_daemon_raises_docker_unavailable(self):
        with self.assertRaises(DockerUnavailable):
            DockerClient("unix:///nonexistent/docker.sock", timeout=1).containers()

    def test_multiplexed_logs_are_joined(self):
        frame = (
            lambda stream, text: bytes([stream, 0, 0, 0])
            + len(text).to_bytes(4, "big")
            + text
        )  # noqa: E731
        self.assertEqual(demux(frame(1, b"out\n") + frame(2, b"err\n")), "out\nerr\n")


@monitor_settings(TOKEN="s3cret")
class EndpointAndAdminTests(MonitorCase):
    def test_the_status_endpoint_needs_the_token_and_reflects_danger(self):
        url = reverse("container_monitor:status")
        self.check(container("db", state="exited"))

        self.assertEqual(self.client.get(url).status_code, 403)
        response = self.client.get(url, headers={"Authorization": "Bearer s3cret"})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["status"], "danger")

    def test_admin_pages_render_and_check_now_runs_a_check(self):
        user = get_user_model().objects.create_superuser("admin", "a@example.com", "pw")
        self.client.force_login(user)
        real = monitor.run_check
        with mock.patch(
            "container_monitor.monitor.run_check",
            lambda: real(
                client=FakeDocker(container("web")),
                proc_root=str(self.proc),
                sample_seconds=0,
            ),
        ):
            response = self.client.post(reverse("admin:container_monitor_check_now"))
        check = Check.objects.get()
        self.assertRedirects(
            response,
            reverse("admin:container_monitor_check_change", args=[check.pk]),
            fetch_redirect_response=False,
        )
        detail = self.client.get(
            reverse("admin:container_monitor_check_change", args=[check.pk])
        )
        self.assertContains(detail, "web")
        self.assertContains(
            detail, reverse("admin:container_monitor_logs", args=["web"])
        )
        with mock.patch("container_monitor.admin.DockerClient") as client:
            client.return_value.logs.return_value = "booted in 2s"
            logs = self.client.get(
                reverse("admin:container_monitor_logs", args=["web"])
            )
        self.assertContains(logs, "booted in 2s")
