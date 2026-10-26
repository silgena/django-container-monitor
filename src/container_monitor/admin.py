from django.contrib import admin, messages
from django.http import Http404, HttpResponseNotAllowed
from django.shortcuts import redirect, render
from django.urls import path, reverse
from django.utils.html import format_html, format_html_join

from container_monitor import conf, monitor
from container_monitor.docker import DockerClient, DockerUnavailable
from container_monitor.models import Alert, Check, Status

COLOURS = {Status.OK: "#1a7f37", Status.WARNING: "#9a6700", Status.DANGER: "#cf222e"}


def _badge(status, text=None):
    return format_html(
        '<span style="color:#fff;background:{};padding:1px 7px;border-radius:9px;'
        'font-size:11px;font-weight:600">{}</span>',
        COLOURS.get(status, "#8c959f"),
        text or status,
    )


def _bytes(value):
    if value is None:
        return "—"
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024


@admin.register(Check)
class CheckAdmin(admin.ModelAdmin):
    list_display = (
        "checked_at",
        "verdict",
        "memory",
        "load",
        "disk",
        "containers_summary",
        "why",
    )
    list_filter = ("status", "checked_at")
    date_hierarchy = "checked_at"
    change_list_template = "admin/container_monitor/check/change_list.html"
    fields = (
        "checked_at",
        "verdict",
        "reasons_list",
        "host_table",
        "containers_table",
        "docker_error",
        "duration_ms",
    )
    readonly_fields = fields

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def get_urls(self):
        view = self.admin_site.admin_view
        return [
            path(
                "check-now/",
                view(self.check_now_view),
                name="container_monitor_check_now",
            ),
            path(
                "logs/<str:container>/",
                view(self.logs_view),
                name="container_monitor_logs",
            ),
        ] + super().get_urls()

    def changelist_view(self, request, extra_context=None):
        extra_context = {
            **(extra_context or {}),
            "latest": Check.objects.order_by("-checked_at").first(),
        }
        return super().changelist_view(request, extra_context)

    def check_now_view(self, request):
        if request.method != "POST":
            return HttpResponseNotAllowed(["POST"])
        if not self.has_view_permission(request):
            raise Http404
        check = monitor.run_check()
        messages.info(request, f"Checked: {check.get_status_display()}.")
        return redirect(
            reverse("admin:container_monitor_check_change", args=[check.pk])
        )

    def logs_view(self, request, container):
        if not self.has_view_permission(request):
            raise Http404
        lines = min(int(request.GET.get("lines") or conf.get("LOG_LINES")), 5000)
        try:
            output, error = DockerClient().logs(container, lines), ""
        except DockerUnavailable as exc:
            output, error = "", str(exc)
        return render(
            request,
            "admin/container_monitor/check/logs.html",
            {
                **self.admin_site.each_context(request),
                "opts": self.model._meta,
                "title": f"Logs: {container}",
                "container": container,
                "lines": lines,
                "output": output,
                "error": error,
            },
        )

    # -- columns ------------------------------------------------------------
    @admin.display(description="status", ordering="status")
    def verdict(self, obj):
        return _badge(obj.status, obj.get_status_display())

    @admin.display(description="memory")
    def memory(self, obj):
        percent = obj.host.get("memory_percent")
        return (
            "—"
            if percent is None
            else f"{percent:g}% of {_bytes(obj.host.get('memory_total'))}"
        )

    @admin.display(description="load (5 min)")
    def load(self, obj):
        load = obj.host.get("load_5")
        return "—" if load is None else f"{load:g} / {obj.host.get('cpu_count')} cores"

    @admin.display(description="disk")
    def disk(self, obj):
        return (
            ", ".join(
                f"{disk['path']} {disk['percent']:g}%"
                for disk in obj.host.get("disks", [])
            )
            or "—"
        )

    @admin.display(description="containers")
    def containers_summary(self, obj):
        if obj.docker_error and not obj.containers:
            return "unavailable"
        running = sum(1 for entry in obj.containers if entry["state"] == "running")
        return f"{running}/{len(obj.containers)} running"

    @admin.display(description="why")
    def why(self, obj):
        if not obj.reasons:
            return "—"
        more = f" (+{len(obj.reasons) - 1})" if len(obj.reasons) > 1 else ""
        return f"{obj.reasons[0]}{more}"

    @admin.display(description="reasons")
    def reasons_list(self, obj):
        if not obj.reasons:
            return "Everything is within limits."
        return format_html(
            "<ul style='margin:0'>{}</ul>",
            format_html_join("", "<li>{}</li>", ((reason,) for reason in obj.reasons)),
        )

    @admin.display(description="host")
    def host_table(self, obj):
        metrics = obj.host
        rows = [
            (
                "CPU",
                (
                    f"{metrics['cpu_percent']:g}%"
                    if metrics.get("cpu_percent") is not None
                    else "—"
                ),
            ),
            (
                "Load (1 / 5 / 15 min)",
                f"{metrics.get('load_1')} / {metrics.get('load_5')} / {metrics.get('load_15')}",
            ),
            ("Memory", self.memory(obj)),
            (
                "Swap",
                (
                    f"{metrics.get('swap_percent', 0):g}% of {_bytes(metrics.get('swap_total'))}"
                    if metrics.get("swap_total")
                    else "none"
                ),
            ),
            ("Disk", self.disk(obj)),
        ]
        return format_html(
            "<table>{}</table>",
            format_html_join(
                "", "<tr><th style='padding-right:1em'>{}</th><td>{}</td></tr>", rows
            ),
        )

    @admin.display(description="containers")
    def containers_table(self, obj):
        if not obj.containers:
            return obj.docker_error or "—"
        rows = []
        for entry in sorted(
            obj.containers, key=lambda item: (not item.get("judged"), item["name"])
        ):
            memory = _bytes(entry.get("memory_usage"))
            if limit := entry.get("memory_limit"):
                percent = entry.get("memory_percent", 0)
                memory += f" / {_bytes(limit)} ({percent:g}%)"
            logs = reverse("admin:container_monitor_logs", args=[entry["name"]])
            rows.append(
                (
                    _badge(
                        entry.get("status"),
                        entry.get("status") if entry.get("judged") else "not judged",
                    ),
                    entry["name"],
                    entry.get("status_text") or entry["state"],
                    entry.get("health") or "—",
                    (
                        "—"
                        if entry.get("cpu_percent") is None
                        else f"{entry['cpu_percent']:g}%"
                    ),
                    memory,
                    entry.get("restart_count", 0),
                    "; ".join(entry.get("reasons") or []) or "—",
                    format_html('<a href="{}">logs</a>', logs),
                )
            )
        header = ("", "name", "state", "health", "CPU", "memory", "restarts", "why", "")
        return format_html(
            "<table><thead><tr>{}</tr></thead><tbody>{}</tbody></table>",
            format_html_join("", "<th>{}</th>", ((column,) for column in header)),
            format_html_join("", "<tr>" + "<td>{}</td>" * len(header) + "</tr>", rows),
        )


@admin.register(Alert)
class AlertAdmin(admin.ModelAdmin):
    list_display = (
        "sent_at",
        "channel",
        "previous_status",
        "status",
        "delivered",
        "error",
    )
    list_filter = ("status", "delivered", "channel")
    readonly_fields = (
        "check_run",
        "sent_at",
        "channel",
        "previous_status",
        "status",
        "message",
        "delivered",
        "error",
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False
