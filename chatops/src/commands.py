from __future__ import annotations

import asyncio
import base64
import functools
import logging
import time
from collections import deque
from typing import Any

logger = logging.getLogger("chatops")
logger.setLevel(logging.INFO)
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s — %(message)s"))
    logger.addHandler(_handler)
    logger.propagate = False

# Commands that take a password as their last argument — redact it in logs
_PASSWORD_COMMANDS = {"add-nifi", "add-team", "reset-team", "reset-password"}

import httpx
from kubernetes import client as k8s_client
from kubernetes.stream import stream as k8s_stream

from settings import settings

# ── Module-level API clients — set once via init_clients() at startup ──────────
core_v1: k8s_client.CoreV1Api | None = None
apps_v1: k8s_client.AppsV1Api | None = None
networking_v1: k8s_client.NetworkingV1Api | None = None   # for NetworkPolicy
rbac_v1: k8s_client.RbacAuthorizationV1Api | None = None  # for RoleBinding cleanup
custom: k8s_client.CustomObjectsApi | None = None          # Routes, BuildConfigs, Tekton
http_client: httpx.Client | None = None


def init_clients(k8s_module: Any, http: httpx.Client) -> None:
    """Called once from lifespan. k8s_module is the kubernetes.client module."""
    global core_v1, apps_v1, networking_v1, rbac_v1, custom, http_client
    # Set a 10s timeout on all API calls — prevents DNS hangs from blocking Slack responses
    cfg = k8s_module.Configuration.get_default_copy()
    cfg.retries = 1
    k8s_module.Configuration.set_default(cfg)
    core_v1 = k8s_module.CoreV1Api()
    apps_v1 = k8s_module.AppsV1Api()
    networking_v1 = k8s_module.NetworkingV1Api()
    rbac_v1 = k8s_module.RbacAuthorizationV1Api()
    custom = k8s_module.CustomObjectsApi()
    http_client = http


# ── Slack response helper ──────────────────────────────────────────────────────

def post_to_slack(response_url: str, text: str, *, in_channel: bool = True) -> None:
    http_client.post(
        response_url,
        json={
            "response_type": "in_channel" if in_channel else "ephemeral",
            "text": text,
        },
    )


# ── Background task entry point ────────────────────────────────────────────────

def run_command(subcmd: str, args: list[str], response_url: str, channel_id: str, channel_name: str = "unknown") -> None:
    safe_args = args[:-1] + ["***"] if subcmd in _PASSWORD_COMMANDS and args else args
    invocation = " ".join([subcmd] + safe_args)
    logger.info("cmd start  [#%s] %s", channel_name, invocation)
    t0 = time.monotonic()
    try:
        result = dispatch(subcmd, args, channel_id)
        elapsed = time.monotonic() - t0
        logger.info("cmd ok     [#%s] %s (%.1fs)", channel_name, subcmd, elapsed)
        post_to_slack(response_url, f"`{subcmd}` done\n```\n{result}\n```")
    except PermissionError as exc:
        elapsed = time.monotonic() - t0
        logger.warning("cmd denied [#%s] %s — %s (%.1fs)", channel_name, subcmd, exc, elapsed)
        post_to_slack(response_url, f"Not allowed: {exc}", in_channel=False)
    except Exception as exc:
        elapsed = time.monotonic() - t0
        logger.error("cmd error  [#%s] %s — %s (%.1fs)", channel_name, subcmd, exc, elapsed)
        post_to_slack(response_url, f"`{subcmd}` failed: {exc}")


# ── Dispatch ───────────────────────────────────────────────────────────────────

def dispatch(subcmd: str, args: list[str], channel_id: str) -> str:
    is_admin = bool(
        settings.admin_channel_id
        and channel_id == settings.admin_channel_id
    )

    # Non-admin channels: only status is allowed
    if not is_admin:
        if subcmd == "status" and len(args) >= 2:
            return cmd_status(args[0], args[1])
        raise PermissionError(
            "Admin channel only. Contact your instructor."
        )

    match subcmd:
        case "status":           return cmd_status(*args)
        case "status-all":       return cmd_status_all()
        case "add-kafka":        return cmd_add_kafka(*args)
        case "add-nifi":         return cmd_add_nifi(*args)
        case "add-team":         return cmd_add_team(*args)
        case "reset-team":       return cmd_reset_team(*args)
        case "remove-team":      return cmd_remove_team(*args)
        case "remove-kafka":     return cmd_remove_kafka(*args)
        case "remove-nifi":      return cmd_remove_nifi(*args)
        case "remove-all-teams": return cmd_remove_all_teams()
        case "wipe-kafka-data":  return cmd_wipe_kafka_data(*args)
        case "restart-kafka":    return cmd_restart_kafka(*args)
        case "restart-nifi":     return cmd_restart_nifi(*args)
        case "reset-password":   return cmd_reset_password(*args)
        case "force-update-nifi": return cmd_force_update_nifi(*args)
        case "pause-events":     return cmd_pause_events()
        case "resume-events":    return cmd_resume_events()
        case "remove-events":    return cmd_remove_events()
        case "rebuild-events":   return cmd_rebuild_events()
        case "deploy-events":    return cmd_deploy_events()
        case "teardown-all":     return cmd_teardown_all(*args)
        case "reset-all":        return cmd_reset_all()
        case "run-pipeline":     return cmd_run_pipeline()
        case "run-reset":        return cmd_run_reset()
        case "run-cleanup":      return cmd_run_cleanup()
        case "pipeline-status":  return cmd_pipeline_status()
        case "cleanup-runs":     return cmd_cleanup_runs()
        case "export-config":       return cmd_export_config()
        case "deploy-console":      return cmd_deploy_console()
        case "console-status":      return cmd_console_status()
        case "help":                return HELP_TEXT
        case _:                     return f"Unknown command: `{subcmd}`\n\n{HELP_TEXT}"


# ── Namespace discovery ────────────────────────────────────────────────────────

_SYSTEM_NAMESPACES = {"default", "kube-public", "kube-node-lease"}
_SYSTEM_PREFIXES = ("kube-", "openshift-")


def _discover_team_namespaces() -> list[str]:
    """Return all non-system, non-infra namespaces the SA has access to, sorted.

    Uses the OpenShift Projects API (project.openshift.io/v1/projects) instead
    of core list_namespace — this only returns projects the service account has
    a RoleBinding in, so no cluster-admin permission is required.

    Raises RuntimeError with a user-facing message if the API call fails.
    """
    try:
        projects = custom.list_cluster_custom_object(
            "project.openshift.io", "v1", "projects", _request_timeout=10
        )
    except Exception as e:
        raise RuntimeError(
            f"Could not list namespaces — API call failed: {e}\n"
            "This is usually a transient network issue. Try again in a moment."
        ) from e
    result = []
    for proj in projects.get("items", []):
        name = proj["metadata"]["name"]
        if name == settings.infra_namespace:
            continue
        if name in _SYSTEM_NAMESPACES:
            continue
        if any(name.startswith(p) for p in _SYSTEM_PREFIXES):
            continue
        result.append(name)
    return sorted(result)


# ── Private helpers ────────────────────────────────────────────────────────────

def _apply_or_update_service(ns: str, svc: k8s_client.V1Service) -> None:
    """Create service if it doesn't exist; patch it if it does."""
    name = svc.metadata.name
    try:
        core_v1.create_namespaced_service(ns, svc)
    except k8s_client.ApiException as e:
        if e.status == 409:
            core_v1.patch_namespaced_service(name, ns, svc)
        else:
            raise


def _apply_or_update_stateful_set(ns: str, sts: k8s_client.V1StatefulSet) -> None:
    """Create StatefulSet if it doesn't exist; patch it if it does."""
    name = sts.metadata.name
    try:
        apps_v1.create_namespaced_stateful_set(ns, sts)
    except k8s_client.ApiException as e:
        if e.status == 409:
            apps_v1.patch_namespaced_stateful_set(name, ns, sts)
        else:
            raise


def _apply_or_update_pvc(ns: str, pvc: k8s_client.V1PersistentVolumeClaim) -> None:
    """Create PVC if it doesn't exist; skip if it does (PVCs are immutable)."""
    try:
        core_v1.create_namespaced_persistent_volume_claim(ns, pvc)
    except k8s_client.ApiException as e:
        if e.status != 409:
            raise


def _apply_or_update_custom(group: str, version: str, plural: str,
                             ns: str, body: dict) -> None:
    """Create custom resource if it doesn't exist; patch it if it does."""
    name = body["metadata"]["name"]
    try:
        custom.create_namespaced_custom_object(group, version, ns, plural, body)
    except k8s_client.ApiException as e:
        if e.status == 409:
            custom.patch_namespaced_custom_object(group, version, ns, plural, name, body)
        else:
            raise


def _apply_or_update_network_policy(ns: str, np: k8s_client.V1NetworkPolicy) -> None:
    """Create NetworkPolicy if it doesn't exist; patch if it does."""
    name = np.metadata.name
    try:
        networking_v1.create_namespaced_network_policy(ns, np)
    except k8s_client.ApiException as e:
        if e.status == 409:
            networking_v1.patch_namespaced_network_policy(name, ns, np)
        else:
            raise


def _check_namespace(ns: str) -> None:
    """Raise RuntimeError with a clear message if the namespace/project doesn't exist."""
    try:
        custom.get_cluster_custom_object("project.openshift.io", "v1", "projects", ns)
    except k8s_client.ApiException as e:
        if e.status in (404, 403):
            raise RuntimeError(
                f"Namespace '{ns}' does not exist or is not accessible.\n"
                f"Create it first:  oc new-project {ns}"
            )
        raise


def _wait_for_pod_ready(pod_name: str, ns: str, timeout_seconds: int = 1500) -> None:
    """
    Poll until pod exists and is Ready, or timeout.
    First polls for existence (pod may not exist yet while image is pulling).
    Then waits for Ready condition.
    timeout_seconds covers image pull (3.5 GB NiFi image can take 10-15 min).
    """
    deadline = time.time() + timeout_seconds
    # Phase 1: wait for pod to be scheduled
    while time.time() < deadline:
        try:
            core_v1.read_namespaced_pod(pod_name, ns)
            break
        except k8s_client.ApiException as e:
            if e.status == 404:
                time.sleep(10)
            else:
                raise
    else:
        raise RuntimeError(f"Timed out waiting for {pod_name} to be scheduled")

    # Phase 2: wait for Ready condition
    while time.time() < deadline:
        pod = core_v1.read_namespaced_pod(pod_name, ns)
        conditions = pod.status.conditions or []
        for cond in conditions:
            if cond.type == "Ready" and cond.status == "True":
                return
        time.sleep(10)
    raise RuntimeError(f"Timed out waiting for {pod_name} to be Ready")


def _get_last_pipeline_params() -> dict[str, str]:
    """
    Read params from the last successful PipelineRun for deploy-all-teams or
    reset-and-deploy. Used by run-pipeline / run-reset to inherit cluster-specific
    values when re-triggering the full pipeline.
    """
    result = custom.list_namespaced_custom_object(
        group="tekton.dev",
        version="v1",
        plural="pipelineruns",
        namespace=settings.infra_namespace,
    )
    items = sorted(
        result.get("items", []),
        key=lambda r: r["metadata"].get("creationTimestamp", ""),
        reverse=True,
    )
    for run in items:
        pipeline_name = run.get("spec", {}).get("pipelineRef", {}).get("name", "")
        if pipeline_name not in ("deploy-all-teams", "reset-and-deploy"):
            continue
        conditions = run.get("status", {}).get("conditions", [])
        if any(c.get("reason") in ("Succeeded", "Completed") for c in conditions):
            return {
                p["name"]: p["value"]
                for p in run.get("spec", {}).get("params", [])
            }
    raise RuntimeError(
        "No successful PipelineRun found. Run `bash pipeline/setup.sh` first, "
        "then retry."
    )


def _label_selector_list(ns: str, label: str, kinds: list[str]) -> list[str]:
    """List resource names matching a label selector (for status reporting)."""
    lines = []
    for kind in kinds:
        try:
            if kind == "pod":
                items = core_v1.list_namespaced_pod(ns, label_selector=label).items
            elif kind == "service":
                items = core_v1.list_namespaced_service(ns, label_selector=label).items
            elif kind == "persistentvolumeclaim":
                items = core_v1.list_namespaced_persistent_volume_claim(
                    ns, label_selector=label
                ).items
            else:
                items = []
            for item in items:
                phase = ""
                if kind == "pod" and item.status:
                    phase = f"  [{item.status.phase}]"
                lines.append(f"  {kind}: {item.metadata.name}{phase}")
        except Exception:
            pass
    return lines


# ── Team registry helpers ──────────────────────────────────────────────────────

_BOOTSTRAP_TMPL = "kafka-{name}-kafka-bootstrap.{ns}.svc.cluster.local:9092"


def _get_team_registry() -> dict[str, dict[str, str]]:
    """Return {team_name: {namespace, bootstrap}} from team-registry ConfigMap."""
    try:
        cm = core_v1.read_namespaced_config_map(
            settings.team_registry_name, settings.infra_namespace
        )
        result = {}
        for name, value in (cm.data or {}).items():
            entry = dict(kv.split("=", 1) for kv in value.split(",") if "=" in kv)
            result[name] = entry
        return result
    except k8s_client.ApiException:
        return {}


def _upsert_team_registry(name: str, ns: str) -> None:
    """Add or update a team entry in the team-registry ConfigMap."""
    bootstrap = _BOOTSTRAP_TMPL.format(name=name, ns=ns)
    value = f"namespace={ns},bootstrap={bootstrap}"
    try:
        core_v1.patch_namespaced_config_map(
            settings.team_registry_name, settings.infra_namespace,
            {"data": {name: value}}
        )
    except k8s_client.ApiException as e:
        if e.status == 404:
            core_v1.create_namespaced_config_map(
                settings.infra_namespace,
                k8s_client.V1ConfigMap(
                    metadata=k8s_client.V1ObjectMeta(name=settings.team_registry_name),
                    data={name: value}
                )
            )
        else:
            raise


def _remove_from_team_registry(name: str) -> None:
    """Remove a team entry. JSON Merge Patch null = key removal (RFC 7386)."""
    try:
        core_v1.patch_namespaced_config_map(
            settings.team_registry_name, settings.infra_namespace,
            {"data": {name: None}}
        )
    except k8s_client.ApiException:
        pass


def _upsert_team_password(name: str, pwd: str) -> None:
    """Store or update a team's NiFi password in the team-passwords Secret."""
    try:
        core_v1.patch_namespaced_secret(
            settings.team_passwords_name, settings.infra_namespace,
            {"stringData": {name: pwd}}
        )
    except k8s_client.ApiException as e:
        if e.status == 404:
            core_v1.create_namespaced_secret(
                settings.infra_namespace,
                k8s_client.V1Secret(
                    metadata=k8s_client.V1ObjectMeta(name=settings.team_passwords_name),
                    string_data={name: pwd}
                )
            )
        else:
            raise


def _remove_team_password(name: str) -> None:
    """Remove a team's password entry. JSON Merge Patch null = key removal."""
    try:
        core_v1.patch_namespaced_secret(
            settings.team_passwords_name, settings.infra_namespace,
            {"data": {name: None}}
        )
    except k8s_client.ApiException:
        pass


def _patch_event_generator_bootstrap() -> str:
    """Rebuild TEAM_BOOTSTRAP_SERVERS from team-registry and patch the event-generator ConfigMap.

    Always patches the ConfigMap so removed teams are never retried on next EG restart.
    Only rollout-restarts when at least one team remains — avoids crash-loop with no Kafka.
    """
    registry = _get_team_registry()

    bootstrap_str = ",".join(
        f"{name}={entry['bootstrap']}"
        for name, entry in sorted(registry.items())
        if "bootstrap" in entry
    )

    try:
        cms = core_v1.list_namespaced_config_map(
            settings.infra_namespace,
            label_selector=f"app={settings.event_generator_name}"
        ).items
    except Exception:
        cms = []
    if not cms:
        return "event-generator ConfigMap not found — skipping patch"

    for cm in cms:
        core_v1.patch_namespaced_config_map(
            cm.metadata.name, settings.infra_namespace,
            {"data": {"TEAM_BOOTSTRAP_SERVERS": bootstrap_str}}
        )

    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    try:
        apps_v1.patch_namespaced_deployment(
            settings.event_generator_name, settings.infra_namespace,
            {"spec": {"template": {"metadata": {"annotations":
                {"kubectl.kubernetes.io/restartedAt": now}
            }}}}
        )
    except k8s_client.ApiException as e:
        if e.status == 404:
            return (
                "event-generator ConfigMap patched but Deployment is missing — "
                "run `/infra deploy-events` to redeploy."
            )
        raise
    if not registry:
        return "All teams removed — event-generator cleared and restarted (no active clusters)"
    return f"Event-generator patched with {len(registry)} team(s) and restarted"


def _patch_console_clusters() -> str:
    """Rebuild kafkaClusters in the Console CR from team-registry.

    Best-effort — skips silently if the Console CR is not deployed.
    Called after every add-kafka / remove-kafka to keep the Console in sync.
    Never raises — a transient API or network error must not fail the caller.
    """
    try:
        custom.get_namespaced_custom_object(
            "console.streamshub.github.com", "v1alpha1",
            settings.infra_namespace, "consoles", "kafka-console"
        )
    except k8s_client.ApiException as e:
        if e.status == 404:
            return "Console CR not deployed — skipping cluster list update"
        return f"Console CR check failed — {e.reason}"
    except Exception as e:
        return f"Console CR check failed — {e}"

    registry = _get_team_registry()
    kafka_clusters = [
        {"name": f"kafka-{name}", "namespace": entry["namespace"], "listener": "plain"}
        for name, entry in sorted(registry.items())
        if "namespace" in entry
    ]

    try:
        custom.patch_namespaced_custom_object(
            "console.streamshub.github.com", "v1alpha1",
            settings.infra_namespace, "consoles", "kafka-console",
            {"spec": {"kafkaClusters": kafka_clusters}}
        )
    except k8s_client.ApiException as e:
        return f"Console CR patch failed — {e.reason}"
    except Exception as e:
        return f"Console CR patch failed — {e}"

    if not kafka_clusters:
        return "Console CR updated — no active clusters"
    return f"Console CR updated with {len(kafka_clusters)} cluster(s)"


def _remove_all_in_namespace(ns: str) -> None:
    """Fire all delete calls for Kafka/NiFi resources in a team namespace.

    Does NOT wait for pods to terminate — caller is responsible for the wait.
    Separating deletes from the wait allows all namespaces to be cleaned in
    parallel rather than sequentially (15 teams in ~60s instead of ~30 min).
    """
    # Delete Kafka CRs first — the Strimzi operator watches these and will reconcile
    # (recreate) any StatefulSets/Services we delete while the CR still exists.
    try:
        kafkas = custom.list_namespaced_custom_object(
            "kafka.strimzi.io", "v1beta2", ns, "kafkas"
        )
        for k in kafkas.get("items", []):
            try:
                custom.delete_namespaced_custom_object(
                    "kafka.strimzi.io", "v1beta2", ns, "kafkas", k["metadata"]["name"]
                )
            except Exception:
                pass
    except Exception:
        pass

    # Delete KafkaNodePool CRs
    try:
        pools = custom.list_namespaced_custom_object(
            "kafka.strimzi.io", "v1beta2", ns, "kafkanodepools"
        )
        for p in pools.get("items", []):
            try:
                custom.delete_namespaced_custom_object(
                    "kafka.strimzi.io", "v1beta2", ns, "kafkanodepools", p["metadata"]["name"]
                )
            except Exception:
                pass
    except Exception:
        pass

    # StatefulSets (Kafka + NiFi)
    try:
        for sts in apps_v1.list_namespaced_stateful_set(ns).items:
            try:
                apps_v1.delete_namespaced_stateful_set(sts.metadata.name, ns)
            except Exception:
                pass
    except Exception:
        pass

    # Services
    try:
        for svc in core_v1.list_namespaced_service(ns).items:
            if svc.metadata.name == "kubernetes":
                continue
            try:
                core_v1.delete_namespaced_service(svc.metadata.name, ns)
            except Exception:
                pass
    except Exception:
        pass

    # PVCs
    try:
        for pvc in core_v1.list_namespaced_persistent_volume_claim(ns).items:
            try:
                core_v1.delete_namespaced_persistent_volume_claim(pvc.metadata.name, ns)
            except Exception:
                pass
    except Exception:
        pass

    # Routes (OpenShift CRD)
    try:
        routes = custom.list_namespaced_custom_object(
            "route.openshift.io", "v1", ns, "routes"
        )
        for r in routes.get("items", []):
            try:
                custom.delete_namespaced_custom_object(
                    "route.openshift.io", "v1", ns, "routes", r["metadata"]["name"]
                )
            except Exception:
                pass
    except Exception:
        pass

    # NetworkPolicies
    try:
        for np in networking_v1.list_namespaced_network_policy(ns).items:
            try:
                networking_v1.delete_namespaced_network_policy(np.metadata.name, ns)
            except Exception:
                pass
    except Exception:
        pass

    # Per-team monitoring resources
    for cm_name in ["kafka-metrics-config"]:
        try:
            core_v1.delete_namespaced_config_map(cm_name, ns)
        except Exception:
            pass
    for rb_name in ["prometheus-scrape"]:
        try:
            rbac_v1.delete_namespaced_role_binding(rb_name, ns)
        except Exception:
            pass


def _has_pods(ns: str) -> bool:
    """Return True if any pods exist in the namespace (used during teardown wait)."""
    try:
        return bool(core_v1.list_namespaced_pod(ns).items)
    except Exception:
        return False


def _delete_affinity_assistants(ns: str) -> None:
    """Delete Tekton affinity-assistant resources in namespace.

    Handles both StatefulSet-backed (Tekton >= 0.41 / OpenShift Pipelines >= 1.8)
    and standalone Pod implementations. StatefulSet deletion cascades to its pod
    via owner reference. Force-deletes pods to clear any stuck-terminating state
    (e.g. after the backing PVC was deleted first).
    """
    label = "app.kubernetes.io/component=affinity-assistant"
    # StatefulSets (newer Tekton) — cascades to pod deletion
    try:
        sts_list = apps_v1.list_namespaced_stateful_set(ns, label_selector=label)
        for sts in sts_list.items:
            try:
                apps_v1.delete_namespaced_stateful_set(sts.metadata.name, ns)
            except Exception:
                pass
    except Exception:
        pass
    # Pods — standalone (older Tekton) or already-stuck pods after StatefulSet deletion
    try:
        pod_list = core_v1.list_namespaced_pod(ns, label_selector=label)
        for pod in pod_list.items:
            try:
                core_v1.delete_namespaced_pod(pod.metadata.name, ns, grace_period_seconds=0)
            except Exception:
                pass
    except Exception:
        pass


def _cancel_in_flight_runs() -> str:
    """Patch all running PipelineRuns and TaskRuns to cancelled state."""
    cancelled = 0

    # Cancel PipelineRuns
    try:
        pr_list = custom.list_namespaced_custom_object(
            group="tekton.dev", version="v1",
            namespace=settings.infra_namespace, plural="pipelineruns",
        )
        for pr in pr_list.get("items", []):
            conditions = pr.get("status", {}).get("conditions", [])
            if any(c.get("reason") in ("Running", "Started") for c in conditions):
                try:
                    custom.patch_namespaced_custom_object(
                        group="tekton.dev", version="v1",
                        namespace=settings.infra_namespace, plural="pipelineruns",
                        name=pr["metadata"]["name"],
                        body={"spec": {"status": "StoppedRunFinally"}},
                    )
                    cancelled += 1
                except Exception:
                    pass
    except Exception:
        pass

    # Cancel TaskRuns
    try:
        tr_list = custom.list_namespaced_custom_object(
            group="tekton.dev", version="v1",
            namespace=settings.infra_namespace, plural="taskruns",
        )
        for tr in tr_list.get("items", []):
            conditions = tr.get("status", {}).get("conditions", [])
            if any(c.get("reason") == "Running" for c in conditions):
                try:
                    custom.patch_namespaced_custom_object(
                        group="tekton.dev", version="v1",
                        namespace=settings.infra_namespace, plural="taskruns",
                        name=tr["metadata"]["name"],
                        body={"spec": {"status": "TaskRunCancelled"}},
                    )
                    cancelled += 1
                except Exception:
                    pass
    except Exception:
        pass

    _delete_affinity_assistants(settings.infra_namespace)
    return f"Cancelled {cancelled} in-flight run(s)"


def _wipe_tekton_history() -> str:
    """Delete all PipelineRun, TaskRun objects and workspace PVCs from infra namespace.
    Does NOT touch the ChatOps deployment or its resources."""
    ns = settings.infra_namespace
    chatops_name = settings.chatops_name
    deleted = 0

    # Delete all PipelineRuns
    try:
        pr_list = custom.list_namespaced_custom_object(
            group="tekton.dev", version="v1", namespace=ns, plural="pipelineruns"
        )
        for pr in pr_list.get("items", []):
            try:
                custom.delete_namespaced_custom_object(
                    group="tekton.dev", version="v1", namespace=ns,
                    plural="pipelineruns", name=pr["metadata"]["name"],
                )
                deleted += 1
            except Exception:
                pass
    except Exception:
        pass

    # Delete all TaskRuns
    try:
        tr_list = custom.list_namespaced_custom_object(
            group="tekton.dev", version="v1", namespace=ns, plural="taskruns"
        )
        for tr in tr_list.get("items", []):
            try:
                custom.delete_namespaced_custom_object(
                    group="tekton.dev", version="v1", namespace=ns,
                    plural="taskruns", name=tr["metadata"]["name"],
                )
                deleted += 1
            except Exception:
                pass
    except Exception:
        pass

    # Delete workspace PVCs, skip any that belong to ChatOps
    try:
        pvcs = core_v1.list_namespaced_persistent_volume_claim(ns)
        for pvc in pvcs.items:
            pvc_name = pvc.metadata.name
            if chatops_name and chatops_name in pvc_name:
                continue
            try:
                core_v1.delete_namespaced_persistent_volume_claim(pvc_name, ns)
                deleted += 1
            except Exception:
                pass
    except Exception:
        pass

    _delete_affinity_assistants(ns)
    return f"Wiped Tekton history: {deleted} objects deleted (ChatOps preserved)"


def _delete_console() -> str:
    """Delete Kafka Console CR and its operator-managed routes. Best-effort."""
    ns = settings.infra_namespace
    deleted = []
    try:
        custom.delete_namespaced_custom_object(
            "console.streamshub.github.com", "v1alpha1",
            ns, "consoles", "kafka-console"
        )
        deleted.append("Console CR")
    except Exception:
        pass
    try:
        routes = custom.list_namespaced_custom_object(
            "route.openshift.io", "v1", ns, "routes",
            label_selector="app.kubernetes.io/name=console"
        )
        for r in routes.get("items", []):
            try:
                custom.delete_namespaced_custom_object(
                    "route.openshift.io", "v1", ns, "routes", r["metadata"]["name"]
                )
                deleted.append(f"route/{r['metadata']['name']}")
            except Exception:
                pass
    except Exception:
        pass
    return f"Console deleted: {', '.join(deleted)}" if deleted else "Console: nothing to delete"


def _delete_tekton_definitions() -> str:
    """Delete Tekton Task and Pipeline definitions by name (mirrors cleanup.sh step 5)."""
    ns = settings.infra_namespace
    task_names = [
        "deploy-kafka", "deploy-event-generator", "verify-health",
        "teardown-all", "deploy-nifi", "deploy-chatops", "deploy-console",
    ]
    pipeline_names = ["deploy-all-teams", "reset-and-deploy"]
    deleted = 0
    for task_name in task_names:
        try:
            custom.delete_namespaced_custom_object(
                "tekton.dev", "v1", ns, "tasks", task_name
            )
            deleted += 1
        except Exception:
            pass
    for pipeline_name in pipeline_names:
        try:
            custom.delete_namespaced_custom_object(
                "tekton.dev", "v1", ns, "pipelines", pipeline_name
            )
            deleted += 1
        except Exception:
            pass
    return f"Tekton definitions: {deleted} task(s)/pipeline(s) deleted"


def _delete_rbac() -> str:
    """Delete pipeline RBAC from team namespaces, infra namespace, and cluster-scoped (best-effort).

    Mirrors cleanup.sh steps 7-9. Matches label: app=tekton-pipeline,component=rbac.
    """
    label = "app=tekton-pipeline,component=rbac"
    ns = settings.infra_namespace
    deleted = 0

    try:
        team_namespaces = _discover_team_namespaces()
    except Exception:
        team_namespaces = []

    for target_ns in team_namespaces + [ns]:
        try:
            for role in rbac_v1.list_namespaced_role(target_ns, label_selector=label).items:
                try:
                    rbac_v1.delete_namespaced_role(role.metadata.name, target_ns)
                    deleted += 1
                except Exception:
                    pass
        except Exception:
            pass
        try:
            for rb in rbac_v1.list_namespaced_role_binding(target_ns, label_selector=label).items:
                try:
                    rbac_v1.delete_namespaced_role_binding(rb.metadata.name, target_ns)
                    deleted += 1
                except Exception:
                    pass
        except Exception:
            pass

    # Cluster-scoped RBAC — dedicated clusters only, skip silently if no permission
    for delete_fn, name in [
        (rbac_v1.delete_cluster_role_binding, "pipeline-runner-binding"),
        (rbac_v1.delete_cluster_role, "pipeline-runner-role"),
    ]:
        try:
            delete_fn(name)
            deleted += 1
        except Exception:
            pass

    return f"RBAC: {deleted} object(s) deleted"


# ── NiFi deploy core (shared by add-nifi and force-update-nifi) ───────────────

def _do_deploy_nifi(name: str, ns: str, pwd: str) -> str:
    """
    Deploy NiFi for a single team using the same resource spec as the nifi/
    templates (team-pvc-template, team-statefulset-template, team-route-template,
    team-networkpolicy-template). Mirrors ops.sh _do_add_nifi.
    Waits for the NiFi pod to be Ready before returning (may take 20+ min on
    first run while the 3.5 GB image pulls).
    """
    if not settings.nifi_image:
        raise RuntimeError(
            "NIFI_IMAGE env var is not set on the chatops deployment.\n"
            "Add it to chatops/k8s/03-deployment.yaml and re-deploy."
        )
    if not settings.external_domain:
        raise RuntimeError(
            "EXTERNAL_DOMAIN env var is not set on the chatops deployment.\n"
            "Add it to chatops/k8s/03-deployment.yaml and re-deploy."
        )

    _check_namespace(ns)

    nifi_image = settings.nifi_image
    external_domain = settings.external_domain
    storage_class = settings.storage_class
    proxy_host = f"nifi-{name}-{ns}.{external_domain}"
    labels = {"app": f"nifi-{name}"}

    # PVC (1200Mi — matches team-pvc-template.yaml)
    _apply_or_update_pvc(ns, k8s_client.V1PersistentVolumeClaim(
        metadata=k8s_client.V1ObjectMeta(
            name=f"nifi-{name}-data", namespace=ns, labels=labels
        ),
        spec=k8s_client.V1PersistentVolumeClaimSpec(
            access_modes=["ReadWriteOnce"],
            resources=k8s_client.V1VolumeResourceRequirements(
                requests={"storage": "1200Mi"}
            ),
            storage_class_name=storage_class,
        ),
    ))

    # ClusterIP Service (port 8443)
    _apply_or_update_service(ns, k8s_client.V1Service(
        metadata=k8s_client.V1ObjectMeta(
            name=f"nifi-{name}", namespace=ns, labels=labels
        ),
        spec=k8s_client.V1ServiceSpec(
            type="ClusterIP",
            ports=[
                k8s_client.V1ServicePort(
                    port=8443, target_port=8443, protocol="TCP", name="https"
                )
            ],
            selector={"app": f"nifi-{name}"},
        ),
    ))

    # Init container script (mirrors team-statefulset-template.yaml init-config)
    init_script = f"""set -x
echo "Initializing minimal NiFi configuration for {name}..."
mkdir -p /data/conf /data/state /data/flowfile_repository /data/content_repository /data/provenance_repository
if [ ! -f /data/conf/nifi.properties ]; then
  echo "Fresh deployment - copying default configs..."
  cp -r /opt/nifi/nifi-current/conf/* /data/conf/
fi
rm -f /data/conf/keystore.p12
keytool -genkeypair -alias nifi -keyalg RSA -keysize 2048 \\
  -dname "CN={proxy_host}" \\
  -ext "SAN=dns:{proxy_host}" \\
  -keystore /data/conf/keystore.p12 \\
  -storetype PKCS12 \\
  -storepass password -keypass password -validity 365 -noprompt
sed -i 's|^nifi.security.keystorePasswd=.*|nifi.security.keystorePasswd=password|' /data/conf/nifi.properties
sed -i 's|^nifi.security.keyPasswd=.*|nifi.security.keyPasswd=password|' /data/conf/nifi.properties
echo "Custom keystore generated with correct hostname"
echo "Configuration complete"
"""

    # StatefulSet (mirrors team-statefulset-template.yaml)
    _apply_or_update_stateful_set(ns, k8s_client.V1StatefulSet(
        metadata=k8s_client.V1ObjectMeta(
            name=f"nifi-{name}", namespace=ns, labels=labels
        ),
        spec=k8s_client.V1StatefulSetSpec(
            service_name=f"nifi-{name}",
            replicas=1,
            selector=k8s_client.V1LabelSelector(match_labels={"app": f"nifi-{name}"}),
            template=k8s_client.V1PodTemplateSpec(
                metadata=k8s_client.V1ObjectMeta(labels={"app": f"nifi-{name}"}),
                spec=k8s_client.V1PodSpec(
                    security_context=k8s_client.V1PodSecurityContext(),
                    init_containers=[
                        k8s_client.V1Container(
                            name="init-config",
                            image=nifi_image,
                            command=["sh", "-c", init_script],
                            volume_mounts=[
                                k8s_client.V1VolumeMount(name="data", mount_path="/data")
                            ],
                            resources=k8s_client.V1ResourceRequirements(
                                requests={"memory": "128Mi", "cpu": "100m"},
                                limits={"memory": "256Mi", "cpu": "500m"},
                            ),
                        )
                    ],
                    containers=[
                        k8s_client.V1Container(
                            name="nifi",
                            image=nifi_image,
                            image_pull_policy="Always",
                            ports=[
                                k8s_client.V1ContainerPort(
                                    container_port=8443, name="https", protocol="TCP"
                                ),
                                k8s_client.V1ContainerPort(
                                    container_port=11443, name="cluster", protocol="TCP"
                                ),
                                k8s_client.V1ContainerPort(
                                    container_port=10443, name="s2s", protocol="TCP"
                                ),
                            ],
                            env=[
                                k8s_client.V1EnvVar(name="HOME", value="/tmp"),
                                k8s_client.V1EnvVar(
                                    name="NIFI_WEB_HTTPS_HOST", value="0.0.0.0"
                                ),
                                k8s_client.V1EnvVar(
                                    name="NIFI_WEB_HTTPS_PORT", value="8443"
                                ),
                                k8s_client.V1EnvVar(
                                    name="NIFI_WEB_PROXY_HOST", value=proxy_host
                                ),
                                k8s_client.V1EnvVar(
                                    name="SINGLE_USER_CREDENTIALS_USERNAME", value=name
                                ),
                                k8s_client.V1EnvVar(
                                    name="SINGLE_USER_CREDENTIALS_PASSWORD", value=pwd
                                ),
                                k8s_client.V1EnvVar(
                                    name="NIFI_SENSITIVE_PROPS_KEY",
                                    value="YOUR_SENSITIVE_PROPS_KEY",
                                ),
                                k8s_client.V1EnvVar(
                                    name="NIFI_JVM_HEAP_INIT", value="512M"
                                ),
                                k8s_client.V1EnvVar(
                                    name="NIFI_JVM_HEAP_MAX", value="1G"
                                ),
                            ],
                            volume_mounts=[
                                k8s_client.V1VolumeMount(
                                    name="data",
                                    mount_path="/opt/nifi/nifi-current/conf",
                                    sub_path="conf",
                                ),
                                k8s_client.V1VolumeMount(
                                    name="data",
                                    mount_path="/opt/nifi/nifi-current/state",
                                    sub_path="state",
                                ),
                                k8s_client.V1VolumeMount(
                                    name="data",
                                    mount_path="/opt/nifi/nifi-current/flowfile_repository",
                                    sub_path="flowfile_repository",
                                ),
                                k8s_client.V1VolumeMount(
                                    name="data",
                                    mount_path="/opt/nifi/nifi-current/content_repository",
                                    sub_path="content_repository",
                                ),
                                k8s_client.V1VolumeMount(
                                    name="data",
                                    mount_path="/opt/nifi/nifi-current/provenance_repository",
                                    sub_path="provenance_repository",
                                ),
                            ],
                            resources=k8s_client.V1ResourceRequirements(
                                requests={"memory": "512Mi", "cpu": "200m"},
                                limits={"memory": "2Gi", "cpu": "500m"},
                            ),
                            liveness_probe=k8s_client.V1Probe(
                                tcp_socket=k8s_client.V1TCPSocketAction(port=8443),
                                initial_delay_seconds=120,
                                period_seconds=30,
                                timeout_seconds=10,
                                failure_threshold=3,
                            ),
                            readiness_probe=k8s_client.V1Probe(
                                tcp_socket=k8s_client.V1TCPSocketAction(port=8443),
                                initial_delay_seconds=30,
                                period_seconds=10,
                                timeout_seconds=5,
                                failure_threshold=3,
                            ),
                        )
                    ],
                    volumes=[
                        k8s_client.V1Volume(
                            name="data",
                            persistent_volume_claim=k8s_client.V1PersistentVolumeClaimVolumeSource(
                                claim_name=f"nifi-{name}-data"
                            ),
                        )
                    ],
                ),
            ),
        ),
    ))

    # Route (OpenShift CRD — passthrough TLS, no spec.host so OpenShift auto-generates it)
    _apply_or_update_custom(
        "route.openshift.io", "v1", "routes", ns,
        {
            "apiVersion": "route.openshift.io/v1",
            "kind": "Route",
            "metadata": {"name": f"nifi-{name}", "namespace": ns, "labels": labels},
            "spec": {
                "to": {"kind": "Service", "name": f"nifi-{name}"},
                "port": {"targetPort": "https"},
                "tls": {
                    "termination": "passthrough",
                    "insecureEdgeTerminationPolicy": "Redirect",
                },
            },
        },
    )

    # NetworkPolicy (allow ingress from OpenShift router to NiFi pods only)
    _apply_or_update_network_policy(ns, k8s_client.V1NetworkPolicy(
        metadata=k8s_client.V1ObjectMeta(
            name="allow-from-openshift-ingress", namespace=ns
        ),
        spec=k8s_client.V1NetworkPolicySpec(
            pod_selector=k8s_client.V1LabelSelector(
                match_labels={"app": f"nifi-{name}"}
            ),
            ingress=[
                k8s_client.V1NetworkPolicyIngressRule(
                    _from=[
                        k8s_client.V1NetworkPolicyPeer(
                            namespace_selector=k8s_client.V1LabelSelector(
                                match_labels={
                                    "network.openshift.io/policy-group": "ingress"
                                }
                            )
                        )
                    ]
                )
            ],
            policy_types=["Ingress"],
        ),
    ))

    # Wait for pod to be Ready (image pull + init container + NiFi startup)
    _wait_for_pod_ready(f"nifi-{name}-0", ns, timeout_seconds=1500)

    return (
        f"NiFi deployed for {name} in {ns}\n"
        f"UI: https://{proxy_host}/nifi\n"
        f"Username: {name}"
    )


# ── Command implementations ────────────────────────────────────────────────────

def _pod_icon(pod) -> tuple[str, str]:
    """Return (icon, detail) summarising pod health at a glance."""
    phase = pod.status.phase or "Unknown"
    if phase in ("Succeeded", "Completed"):
        return "✓", "Completed"
    if phase == "Failed":
        return "❌", "Failed"
    if phase == "Running":
        for cs in pod.status.container_statuses or []:
            if cs.state and cs.state.waiting:
                reason = cs.state.waiting.reason or ""
                if any(w in reason for w in ("CrashLoop", "Error", "OOMKilled")):
                    return "❌", reason
        all_ready = all(cs.ready for cs in (pod.status.container_statuses or []))
        return ("✅", "Running") if all_ready else ("⚠️", "Not Ready")
    if phase == "Pending":
        for cs in (pod.status.init_container_statuses or []) + (pod.status.container_statuses or []):
            if cs.state and cs.state.waiting and cs.state.waiting.reason:
                return "⚠️", f"Pending ({cs.state.waiting.reason})"
        return "⚠️", "Pending"
    return "⚠️", phase


def _nifi_url(name: str, ns: str) -> str:
    """Return the NiFi route URL for a team, or empty string if not found."""
    try:
        routes = custom.list_namespaced_custom_object(
            "route.openshift.io", "v1", ns, "routes",
            label_selector=f"app=nifi-{name}",
        )
        for r in routes.get("items", []):
            host = r.get("spec", {}).get("host", "")
            if host:
                return f"https://{host}/nifi"
    except Exception:
        pass
    return ""


def cmd_status(name: str, ns: str) -> str:
    """Health summary for one team — all pods, PVCs, and routes in the namespace."""
    lines = [f"*{name}* / `{ns}`"]
    issues = []

    # All active pods (skip completed Tekton/build pods)
    try:
        pods = core_v1.list_namespaced_pod(ns).items
        active = [p for p in pods if p.status.phase not in ("Succeeded",)]
        if active:
            for pod in sorted(active, key=lambda p: p.metadata.name):
                icon, detail = _pod_icon(pod)
                lines.append(f"  {icon}  {pod.metadata.name}  {detail}")
                if icon != "✅":
                    issues.append(f"{pod.metadata.name} is {detail}")
        else:
            lines.append("  ⚠️  no active pods")
            issues.append(f"No pods running in {ns}")
    except Exception as e:
        lines.append(f"  ⚠️  error reading pods: {e}")

    # All PVCs
    try:
        pvcs = core_v1.list_namespaced_persistent_volume_claim(ns).items
        if pvcs:
            pvc_parts = []
            for pvc in pvcs:
                phase = pvc.status.phase or "Unknown"
                icon = "✅" if phase == "Bound" else "❌"
                pvc_parts.append(f"{icon} {pvc.metadata.name}")
                if phase != "Bound":
                    issues.append(f"PVC {pvc.metadata.name} is {phase}")
            lines.append(f"  PVCs     {'   '.join(pvc_parts)}")
        else:
            lines.append("  PVCs     ⚠️  none")
    except Exception:
        pass

    # All routes
    try:
        routes = custom.list_namespaced_custom_object(
            "route.openshift.io", "v1", ns, "routes"
        )
        for r in routes.get("items", []):
            host = r.get("spec", {}).get("host", "")
            rname = r["metadata"]["name"]
            if host:
                lines.append(f"  🔗  {rname}  https://{host}")
    except Exception:
        pass

    if issues:
        lines += ["", "*Action needed:*"]
        for issue in issues:
            lines.append(f"  ❌ {issue}")

    return "\n".join(lines)


def cmd_status_all() -> str:
    """Cluster overview: infra services + last pipeline run + all team health."""
    ns = settings.infra_namespace
    issues: list[str] = []
    lines = ["*Cluster Overview*", ""]

    # ── Infra services — all Deployments + StatefulSets (excludes Tekton/build pods) ──
    lines.append("*Infra*")
    try:
        deployments = apps_v1.list_namespaced_deployment(ns).items
        statefulsets = apps_v1.list_namespaced_stateful_set(ns).items
        workloads = [(d.metadata.name, d.spec.replicas or 1, d.status.ready_replicas or 0)
                     for d in deployments] + \
                    [(s.metadata.name, s.spec.replicas or 1, s.status.ready_replicas or 0)
                     for s in statefulsets]
        if workloads:
            for wname, desired, ready in sorted(workloads):
                if ready == desired:
                    icon = "✅"
                    detail = f"Running ({ready}/{desired})"
                elif ready > 0:
                    icon = "⚠️"
                    detail = f"Degraded ({ready}/{desired} ready)"
                    issues.append(f"infra/{wname}: {detail}")
                else:
                    icon = "❌"
                    detail = f"Down (0/{desired} ready)"
                    issues.append(f"infra/{wname} is down")
                lines.append(f"  {wname:<26} {icon}  {detail}")
        else:
            lines.append("  (no deployments found)")
    except Exception as e:
        lines.append(f"  ⚠️  error reading infra workloads: {e}")

    # Last pipeline run
    try:
        result = custom.list_namespaced_custom_object("tekton.dev", "v1", ns, "pipelineruns")
        runs = sorted(
            result.get("items", []),
            key=lambda r: r["metadata"].get("creationTimestamp", ""),
            reverse=True,
        )
        if runs:
            run = runs[0]
            rname = run["metadata"]["name"]
            conditions = run.get("status", {}).get("conditions", [])
            reason = conditions[0].get("reason", "Unknown") if conditions else "Unknown"
            ts = run["metadata"].get("creationTimestamp", "")[:10]
            icon = "✅" if reason in ("Succeeded", "Completed") else ("❌" if reason in ("Failed", "PipelineRunCancelled") else "⏳")
            short_name = rname if len(rname) <= 30 else rname[-30:]
            lines.append(f"  {'pipeline':<24} {icon}  {reason}  {short_name}  {ts}")
            if icon == "❌":
                issues.append(f"Last pipeline run failed: {rname}")
        else:
            lines.append(f"  {'pipeline':<24} —   no runs found")
    except Exception:
        pass

    # Flag builds only if the LATEST build for a given BuildConfig failed.
    # Old failures superseded by a successful build are noise — ignore them.
    try:
        all_pods = core_v1.list_namespaced_pod(ns).items
        build_pods = [p for p in all_pods if p.metadata.name.endswith("-build")]
        # Group by BuildConfig name (pod name = "<bc-name>-<N>-build" → strip last two segments)
        from collections import defaultdict
        bc_pods: dict[str, list] = defaultdict(list)
        for p in build_pods:
            # e.g. slack-chatops-6-build → bc = slack-chatops
            parts = p.metadata.name.rsplit("-", 2)  # ["slack-chatops", "6", "build"]
            bc_name = parts[0] if len(parts) == 3 else p.metadata.name
            bc_pods[bc_name].append(p)
        for bc_name, pods in bc_pods.items():
            # Sort by creation timestamp — latest last
            pods.sort(key=lambda p: p.metadata.creation_timestamp or "")
            latest = pods[-1]
            if latest.status.phase == "Failed":
                issues.append(
                    f"Latest build failed: {latest.metadata.name}"
                    f" — run: `ops.sh rebuild-chatops` or check logs"
                )
    except Exception:
        pass

    # Console URL
    try:
        routes = custom.list_namespaced_custom_object(
            "route.openshift.io", "v1", ns, "routes",
            label_selector="app.kubernetes.io/name=console"
        )
        items = routes.get("items", [])
        host = items[0].get("spec", {}).get("host", "") if items else ""
        lines.append("")
        if host:
            lines.append(f"*Kafka Console URL:* https://{host}")
        else:
            lines.append("*Kafka Console URL:* not deployed")
    except Exception:
        pass

    # ── Teams ──
    lines.append("")
    lines.append("*Teams*")
    try:
        team_namespaces = _discover_team_namespaces()
    except RuntimeError as e:
        lines.append(f"  ⚠️ {e}")
        team_namespaces = []

    if not team_namespaces:
        lines.append("  (no team namespaces found)")
    else:
        for team_ns in team_namespaces:
            lines.append(f"  *{team_ns}*")

            # All active pods — any app, any naming convention
            try:
                pods = core_v1.list_namespaced_pod(team_ns).items
                active = [p for p in pods if p.status.phase not in ("Succeeded",)]
                if active:
                    pod_parts = []
                    for pod in sorted(active, key=lambda p: p.metadata.name):
                        icon, detail = _pod_icon(pod)
                        pod_parts.append(f"{icon} {pod.metadata.name}")
                        if icon != "✅":
                            issues.append(f"{team_ns}: {pod.metadata.name} is {detail}")
                    lines.append(f"    Pods    {',  '.join(pod_parts)}")
                else:
                    lines.append("    Pods    ⚠️ none running")
                    issues.append(f"{team_ns}: no active pods")
            except Exception:
                lines.append("    Pods    ⚠️ error")

            # All PVCs
            try:
                pvcs = core_v1.list_namespaced_persistent_volume_claim(team_ns).items
                if pvcs:
                    pvc_parts = []
                    for pvc in pvcs:
                        phase = pvc.status.phase or "Unknown"
                        icon = "✅" if phase == "Bound" else "❌"
                        pvc_parts.append(f"{icon} {pvc.metadata.name}")
                        if phase != "Bound":
                            issues.append(f"{team_ns}: PVC {pvc.metadata.name} is {phase}")
                    lines.append(f"    PVCs    {',  '.join(pvc_parts)}")
                else:
                    lines.append("    PVCs    ⚠️ none")
            except Exception:
                lines.append("    PVCs    ⚠️ error")

            # All routes — any app
            try:
                routes = custom.list_namespaced_custom_object(
                    "route.openshift.io", "v1", team_ns, "routes"
                )
                route_items = routes.get("items", [])
                if route_items:
                    for r in route_items:
                        host = r.get("spec", {}).get("host", "")
                        if host:
                            lines.append(f"    Route   https://{host}")
                else:
                    lines.append("    Route   (none)")
            except Exception:
                lines.append("    Route   (none)")

    # ── Issues summary ──
    lines.append("")
    if issues:
        lines.append(f"*Issues ({len(issues)})*")
        for issue in issues:
            lines.append(f"  ❌ {issue}")
    else:
        lines.append("*All systems healthy* ✅")

    return "\n".join(lines)


def cmd_add_kafka(name: str, ns: str) -> str:
    """
    Deploy Kafka for a single team via the Kafka operator.
    Creates KafkaNodePool + Kafka CRs; operator provisions pod, services, PVC.
    Mirrors ops.sh _do_add_kafka.
    """
    _check_namespace(ns)

    # KafkaNodePool CR — defines the broker/controller pod
    node_pool_body = {
        "apiVersion": "kafka.strimzi.io/v1beta2",
        "kind": "KafkaNodePool",
        "metadata": {
            "name": "dual-role",
            "namespace": ns,
            "labels": {"strimzi.io/cluster": f"kafka-{name}"},
        },
        "spec": {
            "replicas": 1,
            "roles": ["controller", "broker"],
            "storage": {
                "type": "persistent-claim",
                "size": "2Gi",
                "deleteClaim": False,
                "class": settings.storage_class,
            },
            "resources": {
                "requests": {"memory": "512Mi", "cpu": "250m"},
                "limits": {"memory": "1Gi", "cpu": "500m"},
            },
        },
    }

    # Kafka CR — declares the cluster configuration
    kafka_body = {
        "apiVersion": "kafka.strimzi.io/v1beta2",
        "kind": "Kafka",
        "metadata": {
            "name": f"kafka-{name}",
            "namespace": ns,
            "annotations": {
                "strimzi.io/node-pools": "enabled",
                "strimzi.io/kraft": "enabled",
            },
        },
        "spec": {
            "kafka": {
                "version": "4.2.0",
                "metadataVersion": "4.2-IV0",
                "listeners": [
                    {"name": "plain", "port": 9092, "type": "internal", "tls": False}
                ],
                "config": {
                    "offsets.topic.replication.factor": 1,
                    "transaction.state.log.replication.factor": 1,
                    "transaction.state.log.min.isr": 1,
                    "default.replication.factor": 1,
                    "min.insync.replicas": 1,
                    "auto.create.topics.enable": "true",
                    "log.retention.hours": 24,
                    "log.retention.bytes": 104857600,
                    "log.segment.bytes": 52428800,
                    "log.cleanup.policy": "delete",
                    "log.retention.check.interval.ms": 300000,
                },
            },
            "entityOperator": {
                "topicOperator": {},
                "userOperator": {},
            },
        },
    }

    # Apply KafkaNodePool CR
    try:
        existing = custom.get_namespaced_custom_object(
            "kafka.strimzi.io", "v1beta2", ns, "kafkanodepools", "dual-role"
        )
        node_pool_body["metadata"]["resourceVersion"] = existing["metadata"]["resourceVersion"]
        custom.replace_namespaced_custom_object(
            "kafka.strimzi.io", "v1beta2", ns, "kafkanodepools", "dual-role", node_pool_body
        )
    except k8s_client.ApiException as e:
        if e.status == 404:
            custom.create_namespaced_custom_object(
                "kafka.strimzi.io", "v1beta2", ns, "kafkanodepools", node_pool_body
            )
        else:
            raise

    # Apply Kafka CR
    try:
        existing = custom.get_namespaced_custom_object(
            "kafka.strimzi.io", "v1beta2", ns, "kafkas", f"kafka-{name}"
        )
        kafka_body["metadata"]["resourceVersion"] = existing["metadata"]["resourceVersion"]
        custom.replace_namespaced_custom_object(
            "kafka.strimzi.io", "v1beta2", ns, "kafkas", f"kafka-{name}", kafka_body
        )
    except k8s_client.ApiException as e:
        if e.status == 404:
            custom.create_namespaced_custom_object(
                "kafka.strimzi.io", "v1beta2", ns, "kafkas", kafka_body
            )
        else:
            raise

    # Wait for Kafka CR to be Ready before restarting EG.
    # EG has 5×3s retries at startup — if Kafka isn't up yet, the new team is
    # permanently skipped (no reconnect). 240s covers operator reconcile time.
    #
    # We check observedGeneration >= metadata.generation so that a re-deploy of
    # an already-Ready CR doesn't break out of the loop immediately — we wait
    # for the operator to process the current spec version first.
    try:
        kafka_cr = custom.get_namespaced_custom_object(
            "kafka.strimzi.io", "v1beta2", ns, "kafkas", f"kafka-{name}"
        )
        target_generation = kafka_cr["metadata"].get("generation", 1)
    except Exception:
        target_generation = 1

    deadline = time.time() + 240
    while time.time() < deadline:
        try:
            kafka_cr = custom.get_namespaced_custom_object(
                "kafka.strimzi.io", "v1beta2", ns, "kafkas", f"kafka-{name}"
            )
            observed_gen = kafka_cr.get("status", {}).get("observedGeneration", 0)
            conditions = kafka_cr.get("status", {}).get("conditions", [])
            if (observed_gen >= target_generation and
                    any(c.get("type") == "Ready" and c.get("status") == "True" for c in conditions)):
                break
        except Exception:
            pass
        time.sleep(5)

    _upsert_team_registry(name, ns)
    eg_result = _patch_event_generator_bootstrap()
    console_result = _patch_console_clusters()
    bootstrap = _BOOTSTRAP_TMPL.format(name=name, ns=ns)
    return (
        f"Kafka deployed for {name} in {ns}\n"
        f"Bootstrap: {bootstrap}\n"
        f"{eg_result}\n{console_result}"
    )


def cmd_add_nifi(name: str, ns: str, pwd: str) -> str:
    """
    Deploy NiFi for a team. Skips if NiFi StatefulSet already exists and has
    ready replicas — use force-update-nifi to override.
    """
    sts_name = f"nifi-{name}"
    try:
        sts = apps_v1.read_namespaced_stateful_set(sts_name, ns)
        ready = sts.status.ready_replicas or 0
        if ready > 0:
            return (
                f"NiFi already deployed and healthy for {name} in {ns} "
                f"({ready} replica ready).\n"
                f"Use `force-update-nifi {name} {ns} <pwd>` to force a redeploy."
            )
    except k8s_client.ApiException as e:
        if e.status != 404:
            raise
    result = _do_deploy_nifi(name, ns, pwd)
    _upsert_team_password(name, pwd)
    return result


def cmd_force_update_nifi(name: str, ns: str, pwd: str) -> str:
    """Force redeploy NiFi regardless of current state (bypasses healthy check)."""
    result = _do_deploy_nifi(name, ns, pwd)
    _upsert_team_password(name, pwd)
    return result


def cmd_add_team(name: str, ns: str, pwd: str) -> str:
    """Deploy Kafka + NiFi for a team. Updates team-registry and event-generator via cmd_add_kafka."""
    kafka_result = cmd_add_kafka(name, ns)   # registry upsert + EG patch included
    _upsert_team_password(name, pwd)
    nifi_result = _do_deploy_nifi(name, ns, pwd)
    return f"{kafka_result}\n{nifi_result}"


def cmd_reset_team(name: str, ns: str, pwd: str) -> str:
    """Remove Kafka + NiFi then redeploy fresh. Registry and EG updated via public commands."""
    cmd_remove_team(name, ns)
    return cmd_add_team(name, ns, pwd)


def cmd_remove_team(name: str, ns: str) -> str:
    """Remove Kafka + NiFi. Updates team-registry and event-generator via cmd_remove_kafka."""
    kafka_result = cmd_remove_kafka(name, ns)   # registry remove + EG patch included
    _remove_team_password(name)
    cmd_remove_nifi(name, ns)
    return f"{kafka_result}\nNiFi removed for {name} in {ns}"


def cmd_remove_kafka(name: str, ns: str) -> str:
    # Delete Kafka CR — operator cascades cleanup of pod, services, PVC
    try:
        custom.delete_namespaced_custom_object(
            "kafka.strimzi.io", "v1beta2", ns, "kafkas", f"kafka-{name}"
        )
    except k8s_client.ApiException as e:
        if e.status != 404:
            raise
    # Delete KafkaNodePool CR
    try:
        custom.delete_namespaced_custom_object(
            "kafka.strimzi.io", "v1beta2", ns, "kafkanodepools", "dual-role"
        )
    except k8s_client.ApiException as e:
        if e.status != 404:
            raise
    # Delete Strimzi-created PVCs — must delete or re-add crashes with cluster.id mismatch
    try:
        for pvc in core_v1.list_namespaced_persistent_volume_claim(
            ns, label_selector=f"strimzi.io/cluster=kafka-{name}"
        ).items:
            core_v1.delete_namespaced_persistent_volume_claim(pvc.metadata.name, ns)
    except Exception:
        pass
    # Delete per-team monitoring resources
    for cm_name in ["kafka-metrics-config"]:
        try:
            core_v1.delete_namespaced_config_map(cm_name, ns)
        except k8s_client.ApiException:
            pass
    for rb_name in ["prometheus-scrape"]:
        try:
            rbac_v1.delete_namespaced_role_binding(rb_name, ns)
        except k8s_client.ApiException:
            pass
    _remove_from_team_registry(name)
    eg_result = _patch_event_generator_bootstrap()
    console_result = _patch_console_clusters()
    return f"Kafka removed for {name} in {ns}\n{eg_result}\n{console_result}"


def cmd_remove_nifi(name: str, ns: str) -> str:
    label = f"app=nifi-{name}"
    try:
        for sts in apps_v1.list_namespaced_stateful_set(ns, label_selector=label).items:
            apps_v1.delete_namespaced_stateful_set(sts.metadata.name, ns)
    except Exception:
        pass
    try:
        for svc in core_v1.list_namespaced_service(ns, label_selector=label).items:
            core_v1.delete_namespaced_service(svc.metadata.name, ns)
    except Exception:
        pass
    try:
        for pvc in core_v1.list_namespaced_persistent_volume_claim(
            ns, label_selector=label
        ).items:
            core_v1.delete_namespaced_persistent_volume_claim(pvc.metadata.name, ns)
    except Exception:
        pass
    # Route
    try:
        routes = custom.list_namespaced_custom_object(
            "route.openshift.io", "v1", ns, "routes", label_selector=label
        )
        for r in routes.get("items", []):
            custom.delete_namespaced_custom_object(
                "route.openshift.io", "v1", ns, "routes", r["metadata"]["name"]
            )
    except Exception:
        pass
    # NetworkPolicy
    try:
        networking_v1.delete_namespaced_network_policy(
            "allow-from-openshift-ingress", ns
        )
    except Exception:
        pass
    return f"NiFi removed for {name} in {ns}"


def cmd_remove_all_teams() -> str:
    """Remove all resources from every non-system, non-infra namespace.

    Two-phase approach: fire all deletes across all namespaces first, then do a
    single consolidated wait. This means 15 teams take the same time as 1 team
    (~30-60s) instead of up to 30 min with sequential per-namespace waits.
    """
    namespaces = _discover_team_namespaces()
    if not namespaces:
        return "No team namespaces found."

    registry = _get_team_registry()
    ns_to_name = {entry.get("namespace"): name for name, entry in registry.items()}

    # Phase 1 — fire all deletes across every namespace (just API calls, no waiting)
    for ns in namespaces:
        try:
            _remove_all_in_namespace(ns)
        except Exception:
            pass
        name = ns_to_name.get(ns)
        if name:
            _remove_from_team_registry(name)
            _remove_team_password(name)

    # Phase 2 — single consolidated wait across all namespaces
    deadline = time.time() + 180
    still_terminating = list(namespaces)
    while time.time() < deadline:
        still_terminating = [
            ns for ns in namespaces
            if _has_pods(ns)
        ]
        if not still_terminating:
            break
        time.sleep(5)

    eg_result = _patch_event_generator_bootstrap()
    console_result = _patch_console_clusters()

    if still_terminating:
        return (
            f"{len(namespaces)} team(s) removed — {len(still_terminating)} namespace(s) still "
            f"terminating after 180s: {', '.join(still_terminating)}\n"
            f"{eg_result}\n{console_result}"
        )
    return (
        f"All {len(namespaces)} team(s) removed and pods confirmed gone.\n"
        f"{eg_result}\n{console_result}"
    )


def cmd_wipe_kafka_data(name: str, ns: str) -> str:
    # Verify Kafka CR exists first
    try:
        custom.get_namespaced_custom_object(
            "kafka.strimzi.io", "v1beta2", ns, "kafkas", f"kafka-{name}"
        )
    except k8s_client.ApiException as e:
        if e.status == 404:
            raise RuntimeError(
                f"Kafka CR kafka-{name} not found in {ns} — Kafka is not deployed for this team."
            )
        raise

    label_selector = f"strimzi.io/cluster=kafka-{name}"

    # Phase 1: Delete KafkaNodePool — operator removes the broker pod
    try:
        custom.delete_namespaced_custom_object(
            "kafka.strimzi.io", "v1beta2", ns, "kafkanodepools", "dual-role"
        )
    except k8s_client.ApiException as e:
        if e.status != 404:
            raise

    # Phase 2: Wait up to 120s for pod to disappear.
    # Force-delete after timeout — safe here since data is intentionally being wiped.
    deadline = time.time() + 120
    while time.time() < deadline:
        pods = core_v1.list_namespaced_pod(ns, label_selector=label_selector).items
        if not pods:
            break
        time.sleep(5)
    else:
        # Pod still running — force delete to unblock PVC release
        pods = core_v1.list_namespaced_pod(ns, label_selector=label_selector).items
        for pod in pods:
            try:
                core_v1.delete_namespaced_pod(
                    pod.metadata.name, ns,
                    grace_period_seconds=0,
                )
            except k8s_client.ApiException:
                pass
        time.sleep(5)

    # Phase 3: Delete PVCs then wait for them to fully disappear.
    # If deleted while pod was still attached, PVC stays in Terminating and the
    # recreated KafkaNodePool cannot claim a new PVC with the same name.
    pvcs = core_v1.list_namespaced_persistent_volume_claim(
        ns, label_selector=label_selector
    ).items
    for pvc in pvcs:
        try:
            core_v1.delete_namespaced_persistent_volume_claim(pvc.metadata.name, ns)
        except k8s_client.ApiException:
            pass

    pvc_deadline = time.time() + 60
    while time.time() < pvc_deadline:
        remaining = core_v1.list_namespaced_persistent_volume_claim(
            ns, label_selector=label_selector
        ).items
        if not remaining:
            break
        time.sleep(3)

    # Phase 4: Recreate KafkaNodePool with fresh storage
    node_pool_body = {
        "apiVersion": "kafka.strimzi.io/v1beta2",
        "kind": "KafkaNodePool",
        "metadata": {
            "name": "dual-role",
            "namespace": ns,
            "labels": {"strimzi.io/cluster": f"kafka-{name}"},
        },
        "spec": {
            "replicas": 1,
            "roles": ["controller", "broker"],
            "storage": {
                "type": "persistent-claim",
                "size": "2Gi",
                "deleteClaim": False,
                "class": settings.storage_class,
            },
            "resources": {
                "requests": {"memory": "512Mi", "cpu": "250m"},
                "limits": {"memory": "1Gi", "cpu": "500m"},
            },
        },
    }
    custom.create_namespaced_custom_object(
        "kafka.strimzi.io", "v1beta2", ns, "kafkanodepools", node_pool_body
    )

    # Phase 5: Wait for broker to be Ready again before returning.
    # KafkaNodePool deletion toggles Kafka CR to NotReady, so checking Ready=True
    # correctly blocks until the new pod is up — no generation check needed here.
    ready_deadline = time.time() + 240
    while time.time() < ready_deadline:
        try:
            kafka_cr = custom.get_namespaced_custom_object(
                "kafka.strimzi.io", "v1beta2", ns, "kafkas", f"kafka-{name}"
            )
            conditions = kafka_cr.get("status", {}).get("conditions", [])
            if any(c.get("type") == "Ready" and c.get("status") == "True" for c in conditions):
                return f"Kafka data wiped and broker Ready for {name} in {ns}."
        except k8s_client.ApiException:
            pass
        time.sleep(5)

    return f"Kafka data wiped for {name} in {ns}. Broker still initialising — check status shortly."


def cmd_restart_kafka(name: str, ns: str) -> str:
    # Use label selector to find the actual pod name (Strimzi naming: kafka-{name}-dual-role-0)
    label_selector = f"strimzi.io/cluster=kafka-{name},strimzi.io/kind=Kafka"
    pods = core_v1.list_namespaced_pod(ns, label_selector=label_selector).items
    if not pods:
        raise RuntimeError(f"No Kafka pod found for kafka-{name} in {ns}")
    pod_name = pods[0].metadata.name
    core_v1.delete_namespaced_pod(pod_name, ns, body=k8s_client.V1DeleteOptions())
    return f"{pod_name} deleted — Strimzi operator will restart it"


def cmd_restart_nifi(name: str, ns: str) -> str:
    core_v1.delete_namespaced_pod(
        f"nifi-{name}-0", ns, body=k8s_client.V1DeleteOptions()
    )
    return f"nifi-{name}-0 deleted — StatefulSet will restart it"


def cmd_reset_password(name: str, ns: str, pwd: str) -> str:
    if len(pwd) < 12:
        raise ValueError("Password must be at least 12 characters (NiFi requirement).")

    pod_name = f"nifi-{name}-0"
    try:
        core_v1.read_namespaced_pod(pod_name, ns)
    except k8s_client.ApiException as exc:
        if exc.status == 404:
            raise RuntimeError(f"Pod {pod_name} not found in {ns} — is NiFi deployed?")
        raise

    # This NiFi image regenerates the bcrypt hash from SINGLE_USER_CREDENTIALS_PASSWORD
    # on every pod start, overwriting anything written by nifi.sh set-single-user-credentials.
    # Patch the env var in the StatefulSet spec, then delete the pod so it restarts
    # with the new password value.
    sts_name = f"nifi-{name}"
    sts = apps_v1.read_namespaced_stateful_set(sts_name, ns)
    containers = sts.spec.template.spec.containers
    for container in containers:
        if container.name == "nifi":
            for env_var in (container.env or []):
                if env_var.name == "SINGLE_USER_CREDENTIALS_PASSWORD":
                    env_var.value = pwd
                    break
            else:
                (container.env or []).append(
                    k8s_client.V1EnvVar(name="SINGLE_USER_CREDENTIALS_PASSWORD", value=pwd)
                )
            break
    apps_v1.patch_namespaced_stateful_set(sts_name, ns, sts)
    core_v1.delete_namespaced_pod(pod_name, ns, body=k8s_client.V1DeleteOptions())
    _upsert_team_password(name, pwd)
    return f"Password reset for {name} in {ns}. Pod restarting — NiFi ready in ~2 min."


def cmd_pause_events() -> str:
    apps_v1.patch_namespaced_deployment_scale(
        settings.event_generator_name,
        settings.infra_namespace,
        {"spec": {"replicas": 0}},
    )
    return "Event generator paused (0 replicas)"


def cmd_resume_events() -> str:
    apps_v1.patch_namespaced_deployment_scale(
        settings.event_generator_name,
        settings.infra_namespace,
        {"spec": {"replicas": 1}},
    )
    return "Event generator resumed (1 replica)"


def cmd_remove_events() -> str:
    ns = settings.infra_namespace
    name = settings.event_generator_name
    label = f"app={name}"
    try:
        apps_v1.delete_namespaced_deployment(name, ns)
    except Exception:
        pass
    try:
        for svc in core_v1.list_namespaced_service(ns, label_selector=label).items:
            core_v1.delete_namespaced_service(svc.metadata.name, ns)
    except Exception:
        pass
    try:
        for cm in core_v1.list_namespaced_config_map(ns, label_selector=label).items:
            core_v1.delete_namespaced_config_map(cm.metadata.name, ns)
    except Exception:
        pass
    # BuildConfig and ImageStream are intentionally kept so rebuild-events can
    # trigger a fresh image build. However, the Deployment is deleted here, so
    # rebuild-events alone will NOT bring the pod back — run deploy-events to
    # redeploy with a fresh ConfigMap and the latest image.
    return "Event generator removed"


def _remove_events_full() -> str:
    """Delete event generator completely including BuildConfig and ImageStream.

    Used by run-cleanup (full infrastructure wipe that requires setup.sh to recover).
    Use cmd_remove_events() for teardown-all where build infrastructure should survive
    so rebuild-events / run-pipeline can recover without re-running setup.sh.
    """
    ns = settings.infra_namespace
    name = settings.event_generator_name
    label = f"app={name}"
    deleted = []

    try:
        apps_v1.delete_namespaced_deployment(name, ns)
        deleted.append("Deployment")
    except Exception:
        pass
    for list_fn, delete_fn, kind in [
        (core_v1.list_namespaced_service, core_v1.delete_namespaced_service, "Service"),
        (core_v1.list_namespaced_config_map, core_v1.delete_namespaced_config_map, "ConfigMap"),
    ]:
        try:
            for obj in list_fn(ns, label_selector=label).items:
                try:
                    delete_fn(obj.metadata.name, ns)
                    deleted.append(kind)
                except Exception:
                    pass
        except Exception:
            pass
    for group, version, plural in [
        ("build.openshift.io", "v1", "buildconfigs"),
        ("image.openshift.io", "v1", "imagestreams"),
    ]:
        try:
            obj_list = custom.list_namespaced_custom_object(
                group=group, version=version, namespace=ns,
                plural=plural, label_selector=label,
            )
            for obj in obj_list.get("items", []):
                try:
                    custom.delete_namespaced_custom_object(
                        group=group, version=version, namespace=ns,
                        plural=plural, name=obj["metadata"]["name"],
                    )
                    deleted.append(plural)
                except Exception:
                    pass
        except Exception:
            pass
    return f"Event generator fully removed ({', '.join(deleted) if deleted else 'nothing found'})"


def _delete_chatops() -> str:
    """Delete the ChatOps deployment and all its resources.

    Called last in run-cleanup so the response_url message is already posted
    to Slack before the pod is terminated by Kubernetes.
    """
    ns = settings.infra_namespace
    name = settings.chatops_name
    label = f"app={name}"
    deleted = []

    try:
        apps_v1.delete_namespaced_deployment(name, ns)
        deleted.append("Deployment")
    except Exception:
        pass
    try:
        for svc in core_v1.list_namespaced_service(ns, label_selector=label).items:
            try:
                core_v1.delete_namespaced_service(svc.metadata.name, ns)
                deleted.append("Service")
            except Exception:
                pass
    except Exception:
        pass
    for group, version, plural in [
        ("build.openshift.io", "v1", "buildconfigs"),
        ("image.openshift.io", "v1", "imagestreams"),
        ("route.openshift.io", "v1", "routes"),
    ]:
        try:
            obj_list = custom.list_namespaced_custom_object(
                group=group, version=version, namespace=ns,
                plural=plural, label_selector=label,
            )
            for obj in obj_list.get("items", []):
                try:
                    custom.delete_namespaced_custom_object(
                        group=group, version=version, namespace=ns,
                        plural=plural, name=obj["metadata"]["name"],
                    )
                    deleted.append(plural)
                except Exception:
                    pass
        except Exception:
            pass
    return f"ChatOps deleted ({', '.join(deleted) if deleted else 'nothing found'})"


def cmd_rebuild_events() -> str:
    """Trigger a new BuildConfig build for the event generator.

    BuildConfig and ImageStream survive teardown-all so a fresh image build
    always works. However, if the Deployment does not exist, the new image will
    land in the ImageStream but no pod will roll out — run deploy-events to
    redeploy the Deployment with a fresh ConfigMap.
    If the Deployment still exists, the ImageChange trigger rolls out the new
    pod automatically once the build completes.
    """
    ns = settings.infra_namespace
    name = settings.event_generator_name
    try:
        custom.get_namespaced_custom_object(
            "build.openshift.io", "v1", ns, "buildconfigs", name
        )
    except k8s_client.ApiException as e:
        if e.status == 404:
            raise RuntimeError(
                f"BuildConfig '{name}' not found — run `bash pipeline/setup.sh` first to apply build manifests."
            )
        raise
    build_request = {
        "apiVersion": "build.openshift.io/v1",
        "kind": "BuildRequest",
        "metadata": {"name": name},
    }
    # BuildConfig instantiate is a subresource — must POST directly to the path
    # since create_namespaced_custom_object does not support subresources.
    response = custom.api_client.call_api(
        f"/apis/build.openshift.io/v1/namespaces/{ns}/buildconfigs/{name}/instantiate",
        "POST",
        header_params={"Content-Type": "application/json", "Accept": "application/json"},
        body=build_request,
        response_type=object,
        auth_settings=["BearerToken"],
        _return_http_data_only=True,
    )
    build_name = response.get("metadata", {}).get("name", "unknown") if isinstance(response, dict) else "unknown"
    # Check whether the Deployment still exists before reporting rollout status.
    # Swallow all errors — build is already started, don't fail the response over a status check.
    deployment_exists = True
    try:
        apps_v1.read_namespaced_deployment(name, ns)
    except k8s_client.ApiException as e:
        if e.status == 404:
            deployment_exists = False
    except Exception:
        pass  # unknown error — assume exists, user will see pod status themselves
    if deployment_exists:
        rollout_note = "New pod will roll out automatically when build completes."
    else:
        rollout_note = (
            "Deployment does not exist — new image is being built but no pod will start.\n\n"
            "*What do you want to do next?*\n"
            "• `/infra deploy-events` — redeploy event generator only (fast, uses live team registry)\n"
            "• `/infra run-pipeline` — redeploy everything: Kafka + NiFi + event generator for all teams"
        )
    return f"Build started: `{build_name}`\n{rollout_note}"


def cmd_deploy_events() -> str:
    """Deploy or redeploy the event generator Deployment + ConfigMap.

    Works even when the Deployment was previously deleted (remove-events, teardown-all).
    Builds TEAM_BOOTSTRAP_SERVERS from the live team-registry ConfigMap.
    Reads EVENT_RATE_PER_SEC, TOPIC_PREFIX, TOPIC_SUFFIX, REGIONS from the existing
    EG ConfigMap if it exists, otherwise falls back to defaults.
    Requires ImageStream and BuildConfig to exist (created by setup.sh).
    """
    ns = settings.infra_namespace
    name = settings.event_generator_name

    # Fail fast if image build resources are missing
    for kind, plural in [("ImageStream", "imagestreams"), ("BuildConfig", "buildconfigs")]:
        try:
            custom.get_namespaced_custom_object(
                "image.openshift.io" if kind == "ImageStream" else "build.openshift.io",
                "v1", ns, plural, name
            )
        except k8s_client.ApiException as e:
            if e.status == 404:
                raise RuntimeError(
                    f"{kind} '{name}' not found — run `bash pipeline/setup.sh` first to apply build manifests."
                )
            raise

    # Build TEAM_BOOTSTRAP_SERVERS from live team registry.
    # If empty, Kafka was deployed without chatops add-kafka — user must register teams first.
    registry = _get_team_registry()
    bootstrap_str = ",".join(
        f"{tname}={entry['bootstrap']}"
        for tname, entry in sorted(registry.items())
        if "bootstrap" in entry
    )
    if not bootstrap_str:
        raise RuntimeError(
            "No teams registered. Run `/infra add-kafka <name> <ns>` for each team first, "
            "then retry `deploy-events`."
        )

    # Read existing ConfigMap values (preserved if it exists, defaults if deleted)
    existing_cm_data: dict = {}
    cms = core_v1.list_namespaced_config_map(
        ns, label_selector=f"app={name}"
    ).items
    if cms:
        existing_cm_data = cms[0].data or {}

    event_rate   = existing_cm_data.get("EVENT_RATE_PER_SEC", "10")
    topic_prefix = existing_cm_data.get("TOPIC_PREFIX", "events.")
    topic_suffix = existing_cm_data.get("TOPIC_SUFFIX", ".raw")
    regions      = existing_cm_data.get("REGIONS", "Boston,NYC,Chicago,Seattle,Austin")
    topic        = existing_cm_data.get("TOPIC", "")
    kafka_bs     = existing_cm_data.get("KAFKA_BOOTSTRAP_SERVERS", "")

    # Apply ConfigMap
    cm_body = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": f"{name}-config",
            "namespace": ns,
            "labels": {"app": name},
        },
        "data": {
            "EVENT_RATE_PER_SEC": event_rate,
            "TOPIC_PREFIX": topic_prefix,
            "TOPIC_SUFFIX": topic_suffix,
            "REGIONS": regions,
            "TEAM_BOOTSTRAP_SERVERS": bootstrap_str,
            "TOPIC": topic,
            "KAFKA_BOOTSTRAP_SERVERS": kafka_bs,
        },
    }
    try:
        core_v1.create_namespaced_config_map(ns, cm_body)
    except k8s_client.ApiException as e:
        if e.status == 409:  # already exists — patch
            core_v1.patch_namespaced_config_map(f"{name}-config", ns, cm_body)
        else:
            raise

    # Apply Service (deleted by remove-events — must recreate alongside Deployment)
    svc_body = {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {
            "name": name,
            "namespace": ns,
            "labels": {"app": name},
        },
        "spec": {
            "type": "ClusterIP",
            "selector": {"app": name},
            "ports": [{"port": 8000, "targetPort": 8000, "name": "http"}],
        },
    }
    try:
        core_v1.create_namespaced_service(ns, svc_body)
    except k8s_client.ApiException as e:
        if e.status != 409:  # 409 = already exists, nothing to do
            raise

    # Apply Deployment
    image_ref = (
        f"image-registry.openshift-image-registry.svc:5000"
        f"/{ns}/{name}:latest"
    )
    triggers_annotation = (
        f'[{{"from":{{"kind":"ImageStreamTag","name":"{name}:latest"}},'
        f'"fieldPath":"spec.template.spec.containers[?(@.name==\\"generator\\")].image"}}]'
    )
    deployment_body = {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": name,
            "namespace": ns,
            "labels": {"app": name},
            "annotations": {"image.openshift.io/triggers": triggers_annotation},
        },
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": {"app": name}},
            "template": {
                "metadata": {"labels": {"app": name}},
                "spec": {
                    "containers": [{
                        "name": "generator",
                        "image": image_ref,
                        "imagePullPolicy": "Always",
                        "envFrom": [{"configMapRef": {"name": f"{name}-config"}}],
                        "resources": {
                            "requests": {"memory": "768Mi", "cpu": "200m"},
                            "limits":   {"memory": "2Gi",  "cpu": "500m"},
                        },
                        "livenessProbe": {
                            "httpGet": {"path": "/health", "port": 8000},
                            "initialDelaySeconds": 30,
                            "periodSeconds": 10,
                        },
                        "readinessProbe": {
                            "httpGet": {"path": "/ready", "port": 8000},
                            "initialDelaySeconds": 5,
                            "periodSeconds": 5,
                        },
                    }],
                },
            },
        },
    }
    try:
        apps_v1.create_namespaced_deployment(ns, deployment_body)
        action = "created"
    except k8s_client.ApiException as e:
        if e.status == 409:  # already exists — patch + rollout restart
            now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            apps_v1.patch_namespaced_deployment(
                name, ns,
                {"spec": {"template": {"metadata": {"annotations":
                    {"kubectl.kubernetes.io/restartedAt": now}
                }}}}
            )
            action = "restarted"
        else:
            raise

    team_count = len(registry)
    return (
        f"Event generator {action}.\n"
        f"{team_count} team(s): `{bootstrap_str}`\n"
        f"Pod rolling out in namespace `{ns}`."
    )


def cmd_teardown_all(*args) -> str:
    """
    teardown-all         — cancel in-flight runs + remove events + Console + all teams
    teardown-all --wipe  — same + also wipe Tekton run history (PipelineRuns/TaskRuns/workspace PVCs)
    """
    wipe = "--wipe" in args
    lines = [_cancel_in_flight_runs()]
    if wipe:
        lines.append(_wipe_tekton_history())
    lines.append(cmd_remove_events())
    lines.append(_delete_console())
    lines.append(cmd_remove_all_teams())
    return "\n".join(lines)


def cmd_reset_all() -> str:
    """Cancel in-flight runs, teardown all, then trigger reset-and-deploy pipeline."""
    teardown_result = cmd_teardown_all()
    pipeline_result = cmd_run_reset()
    return f"{teardown_result}\n{pipeline_result}"


def cmd_run_pipeline() -> str:
    return _trigger_pipeline("deploy-all-teams", "deploy-all-teams-run")


def cmd_run_reset() -> str:
    return _trigger_pipeline("reset-and-deploy", "reset-all-teams-run")


def cmd_run_cleanup() -> str:
    """Full infrastructure wipe — equivalent to bash pipeline/cleanup.sh DELETE_CHATOPS=true.

    Deletes everything this repo deployed: Console, event generator (incl. BuildConfig +
    ImageStream), all teams, Tekton run history, Task/Pipeline definitions, RBAC, and
    ChatOps itself. Namespaces are kept.

    ChatOps is deleted last so this response is posted to Slack before the pod terminates.
    Run `bash pipeline/setup.sh` to recover — run-pipeline alone is not enough.
    """
    lines = [
        _cancel_in_flight_runs(),
        _delete_console(),
        _remove_events_full(),
        cmd_remove_all_teams(),
        _wipe_tekton_history(),
        _delete_tekton_definitions(),
        _delete_rbac(),
        _delete_chatops(),
    ]
    lines.append(
        "Full cleanup complete. ChatOps has been deleted.\n"
        "Run `bash pipeline/setup.sh` to recover."
    )
    return "\n".join(lines)


def _trigger_pipeline(pipeline_name: str, name_prefix: str) -> str:
    try:
        cluster_params = _get_last_pipeline_params()
        params = [{"name": k, "value": v} for k, v in cluster_params.items()]
    except RuntimeError as e:
        return f"Cannot trigger pipeline: {e}"

    body = {
        "apiVersion": "tekton.dev/v1",
        "kind": "PipelineRun",
        "metadata": {
            "name": f"{name_prefix}-{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}",
            "namespace": settings.infra_namespace,
            "labels": {"app": "tekton-pipeline"},
        },
        "spec": {
            "taskRunTemplate": {"serviceAccountName": "pipeline"},
            "pipelineRef": {"name": pipeline_name},
            "params": params,
            "workspaces": [
                {
                    "name": "shared-data",
                    "volumeClaimTemplate": {
                        "spec": {
                            "accessModes": ["ReadWriteOnce"],
                            "resources": {"requests": {"storage": "100Mi"}},
                        }
                    },
                }
            ],
        },
    }
    created = custom.create_namespaced_custom_object(
        group="tekton.dev",
        version="v1",
        plural="pipelineruns",
        namespace=settings.infra_namespace,
        body=body,
    )
    run_name = created["metadata"]["name"]
    return f"Started: `{run_name}`\nCheck progress: `/infra pipeline-status`"


def cmd_pipeline_status() -> str:
    result = custom.list_namespaced_custom_object(
        group="tekton.dev",
        version="v1",
        plural="pipelineruns",
        namespace=settings.infra_namespace,
    )
    items = sorted(
        result.get("items", []),
        key=lambda r: r["metadata"].get("creationTimestamp", ""),
        reverse=True,
    )[:5]

    if not items:
        return "No PipelineRuns found."

    lines = ["Last 5 PipelineRuns:"]
    for run in items:
        name = run["metadata"]["name"]
        conditions = run.get("status", {}).get("conditions", [])
        reason = conditions[0].get("reason", "Unknown") if conditions else "Pending"
        ts = run["metadata"].get("creationTimestamp", "")
        lines.append(f"  {name}  [{reason}]  {ts}")
    return "\n".join(lines)


def cmd_cleanup_runs() -> str:
    ns = settings.infra_namespace

    def _delete_old(plural: str, keep: int) -> int:
        result = custom.list_namespaced_custom_object(
            group="tekton.dev", version="v1", plural=plural, namespace=ns
        )
        items = sorted(
            result.get("items", []),
            key=lambda r: r["metadata"].get("creationTimestamp", ""),
            reverse=True,
        )
        to_delete = items[keep:]
        for run in to_delete:
            try:
                custom.delete_namespaced_custom_object(
                    group="tekton.dev", version="v1", namespace=ns,
                    plural=plural, name=run["metadata"]["name"],
                )
            except Exception:
                pass
        return len(to_delete)

    pr_deleted = _delete_old("pipelineruns", keep=3)
    tr_deleted = _delete_old("taskruns", keep=5)

    _delete_affinity_assistants(ns)
    return f"Kept 3 newest PipelineRuns (deleted {pr_deleted}), kept 5 newest TaskRuns (deleted {tr_deleted})."


def cmd_export_config() -> str:
    """Print team-registry + passwords as a config.env block ready to paste."""
    registry = _get_team_registry()
    if not registry:
        return "team-registry is empty. Nothing to export."

    passwords = {}
    try:
        secret = core_v1.read_namespaced_secret(
            settings.team_passwords_name, settings.infra_namespace
        )
        for k, v in (secret.data or {}).items():
            passwords[k] = base64.b64decode(v).decode()
    except k8s_client.ApiException:
        pass  # Secret missing — passwords show as <set-manually>

    lines = [
        "```",
        "# Team config from cluster — paste into config.env",
        "",
    ]
    for i, (name, entry) in enumerate(sorted(registry.items()), start=1):
        ns = entry.get("namespace", "unknown")
        pwd = passwords.get(name, "<set-manually>")
        lines += [
            f"export TEAM{i}_NAME={name}",
            f"export TEAM{i}_NAMESPACE={ns}",
            f"export TEAM{i}_PASSWORD={pwd}",
            "",
        ]
    for i in range(len(registry) + 1, 16):
        lines += [
            f"export TEAM{i}_NAME=skip",
            f"export TEAM{i}_NAMESPACE=skip",
            f"export TEAM{i}_PASSWORD=skip",
            "",
        ]
    bootstrap_str = ",".join(
        f"{name}={entry['bootstrap']}"
        for name, entry in sorted(registry.items())
        if "bootstrap" in entry
    )
    lines += [
        f'export TEAM_BOOTSTRAP_SERVERS="{bootstrap_str}"',
        "```",
    ]
    return "\n".join(lines)


def cmd_deploy_console() -> str:
    """Deploy or update the Kafka Console CR in the infra namespace.

    - If the Console CR does not exist: creates it with hostname + kafkaClusters from registry.
    - If it already exists: patches kafkaClusters to match current registry.
    Skips gracefully if the Console operator CRD is not installed.
    """
    # Check operator is installed
    try:
        custom.list_namespaced_custom_object(
            "console.streamshub.github.com", "v1alpha1",
            settings.infra_namespace, "consoles", _request_timeout=5
        )
    except k8s_client.ApiException as e:
        if e.status == 404:
            return "Console operator not installed — ask cluster admin to install Streams for Apache Kafka Console operator."
        # Other errors (e.g. 403) treated as operator missing
        return f"Console operator check failed ({e.status}) — operator may not be installed."

    registry = _get_team_registry()
    kafka_clusters = [
        {"name": f"kafka-{name}", "namespace": entry["namespace"], "listener": "plain"}
        for name, entry in sorted(registry.items())
        if "namespace" in entry
    ]

    if not kafka_clusters:
        return "No active teams in registry — deploy teams first before deploying Console."

    hostname = f"kafka-console-{settings.infra_namespace}.{settings.external_domain}"

    try:
        custom.get_namespaced_custom_object(
            "console.streamshub.github.com", "v1alpha1",
            settings.infra_namespace, "consoles", "kafka-console"
        )
        # Already exists — patch kafkaClusters only
        custom.patch_namespaced_custom_object(
            "console.streamshub.github.com", "v1alpha1",
            settings.infra_namespace, "consoles", "kafka-console",
            {"spec": {"kafkaClusters": kafka_clusters}}
        )
        return f"Console CR updated with {len(kafka_clusters)} cluster(s)\nURL: https://{hostname}"
    except k8s_client.ApiException as e:
        if e.status != 404:
            raise

    # Does not exist — create with full spec
    console_body = {
        "apiVersion": "console.streamshub.github.com/v1alpha1",
        "kind": "Console",
        "metadata": {
            "name": "kafka-console",
            "namespace": settings.infra_namespace,
            "labels": {"app": "kafka-console"},
        },
        "spec": {
            "hostname": hostname,
            "kafkaClusters": kafka_clusters,
        },
    }
    custom.create_namespaced_custom_object(
        "console.streamshub.github.com", "v1alpha1",
        settings.infra_namespace, "consoles", console_body
    )
    return (
        f"Console CR created with {len(kafka_clusters)} cluster(s)\n"
        f"URL: https://{hostname}\n"
        f"Pod is starting — may take a few minutes on first deploy."
    )


def cmd_console_status() -> str:
    """Check Kafka Console CR status and route URL."""
    try:
        cr = custom.get_namespaced_custom_object(
            "console.streamshub.github.com", "v1alpha1",
            settings.infra_namespace, "consoles", "kafka-console"
        )
    except k8s_client.ApiException as e:
        if e.status == 404:
            return "Console CR not found — run: `/infra deploy-console`"
        return f"Console CR check failed — {e.reason}"

    conditions = cr.get("status", {}).get("conditions", [])
    ready = next((c for c in conditions if c.get("type") == "Ready"), None)
    status_line = "Ready ✅" if ready and ready.get("status") == "True" else \
                  f"Not ready — {ready.get('reason', 'initializing')}" if ready else "No status yet"

    hostname = cr.get("spec", {}).get("hostname", "")
    clusters = cr.get("spec", {}).get("kafkaClusters", [])
    url_line = f"URL: https://{hostname}" if hostname else "URL: not set"
    clusters_line = f"Clusters: {', '.join(c['name'] for c in clusters)}" if clusters else "Clusters: none"

    return f"Console: {status_line}\n{url_line}\n{clusters_line}"


HELP_TEXT = """\
*Available commands* (`/infra <command> [args]`):

*Status*
  `status <name> <ns>`              Show pods, services, PVCs, and route for a team
  `status-all`                      Show all namespaces overview (infra + all teams)

*Kafka / NiFi — single team*
  `add-kafka <name> <ns>`                   Deploy Kafka for a team
  `add-nifi <name> <ns> <pwd>`              Deploy NiFi (skips if already healthy)
  `force-update-nifi <name> <ns> <pwd>`     Force redeploy NiFi regardless of state
  `add-team <name> <ns> <pwd>`              Deploy Kafka + NiFi together
  `reset-team <name> <ns> <pwd>`            Remove then redeploy Kafka + NiFi
  `remove-team <name> <ns>`                 Remove Kafka + NiFi
  `remove-kafka <name> <ns>`                Remove only Kafka
  `remove-nifi <name> <ns>`                 Remove only NiFi
  `wipe-kafka-data <name> <ns>`             Delete Kafka PVC (pod restarts fresh)
  `restart-kafka <name> <ns>`               Restart Kafka pod
  `restart-nifi <name> <ns>`                Restart NiFi pod
  `reset-password <name> <ns> <pwd>`        Reset NiFi login password (min 12 chars)

*Bulk operations*
  `remove-all-teams`            Remove Kafka + NiFi from all team namespaces

  *Safe reset — Tekton tasks/pipelines/RBAC survive, all Slack commands still work after:*
  `teardown-all`                Cancel in-flight runs → remove events + Console + all teams
  `teardown-all --wipe`         Same + also wipe Tekton run history (PipelineRuns/TaskRuns/workspace PVCs)
  `reset-all`                   teardown-all + trigger reset-and-deploy pipeline

  *Nuclear — removes tasks/pipelines/RBAC/Console; run `bash pipeline/setup.sh` to recover:*
  `run-cleanup`                 Wipe everything except ChatOps and namespaces

*Event generator*
  `pause-events`      Stop sending events — keeps deployment, undo with resume-events
  `resume-events`     Resume after pause
  `remove-events`     Delete deployment (BuildConfig/ImageStream kept — image can still be rebuilt)
  `deploy-events`     (Re)deploy from live team registry — use when deployment is missing
  `rebuild-events`    Build fresh image from source — if running, rolls out automatically;
                      if deployment missing, prompts next steps

*Pipeline*
  `run-pipeline`      Trigger deploy-all-teams pipeline
  `run-reset`         Trigger reset-and-deploy pipeline
  `pipeline-status`   Show last 5 PipelineRuns
  `cleanup-runs`      Delete old PipelineRuns (keep newest 3)

*Kafka Console*
  `deploy-console`    Deploy or update Kafka Console CR (creates if missing, patches clusters if exists)
  `console-status`    Show Console CR ready state, URL, and connected clusters

*Config sync*
  `export-config`     Print team registry as config.env block (includes passwords from cluster)
"""

# ── Kafka crash-loop alerting ──────────────────────────────────────────────────

# Tracks the last time an alert was sent per pod (key: "namespace/pod-name").
# Alerts for the same pod are suppressed for 1800s (30 min) to avoid spam during sustained crash-loops.
_alert_sent: dict[str, float] = {}


async def kafka_restart_monitor() -> None:
    """Background loop: alert admin channel when Kafka broker restart count spikes."""
    if not settings.slack_bot_token or not settings.admin_channel_id:
        print("[kafka-monitor] disabled — SLACK_BOT_TOKEN or ADMIN_CHANNEL_ID not set")
        return

    # restart_history[namespace][pod_name] = deque of (timestamp, restart_count) pairs
    restart_history: dict[str, dict[str, deque]] = {}

    while True:
        await asyncio.sleep(settings.alert_poll_interval_seconds)
        try:
            await _check_kafka_restarts(restart_history)
        except Exception as exc:
            print(f"[kafka-monitor] error: {exc}")


async def _check_kafka_restarts(
    restart_history: dict[str, dict[str, deque]]
) -> None:
    """Poll Kafka broker pods across all team namespaces and fire alerts on crash-loops."""
    now = time.time()
    window_seconds = settings.alert_window_minutes * 60

    registry = await asyncio.to_thread(_get_team_registry)

    for team_name, info in registry.items():
        ns = info["namespace"]
        if ns not in restart_history:
            restart_history[ns] = {}

        list_fn = functools.partial(
            core_v1.list_namespaced_pod,
            ns,
            label_selector=f"strimzi.io/cluster=kafka-{team_name},strimzi.io/kind=Kafka",
        )
        try:
            pod_list = await asyncio.to_thread(list_fn)
        except Exception as exc:
            print(f"[kafka-monitor] failed to list pods in {ns}: {exc}")
            continue

        for pod in pod_list.items:
            pod_name = pod.metadata.name
            if pod_name not in restart_history[ns]:
                restart_history[ns][pod_name] = deque()

            history = restart_history[ns][pod_name]

            # Sum restart counts across all containers (handle None container_statuses)
            total_restarts = sum(
                cs.restart_count
                for cs in (pod.status.container_statuses or [])
            )

            history.append((now, total_restarts))

            # Evict entries outside the rolling window
            while history and history[0][0] < now - window_seconds:
                history.popleft()

            if len(history) < 2:
                continue

            oldest_count = history[0][1]
            newest_count = history[-1][1]
            delta = newest_count - oldest_count

            if delta < settings.alert_restart_threshold:
                continue

            alert_key = f"{ns}/{pod_name}"
            if now - _alert_sent.get(alert_key, 0) < 1800:
                continue  # still in cooldown

            alert_text = (
                ":rotating_light: *Kafka broker crash-loop detected*\n"
                f"• *Namespace*: `{ns}`\n"
                f"• *Pod*: `{pod_name}`\n"
                f"• *Restarts*: +{delta} in last {settings.alert_window_minutes} minutes"
                f" (total: {newest_count})\n"
                f"• *Action*: Run `/infra status {team_name} {ns}` to investigate"
            )

            post_fn = functools.partial(
                http_client.post,
                "https://slack.com/api/chat.postMessage",
                headers={"Authorization": f"Bearer {settings.slack_bot_token}"},
                json={"channel": settings.admin_channel_id, "text": alert_text},
            )
            try:
                resp = await asyncio.to_thread(post_fn)
                body = resp.json() if hasattr(resp, "json") else {}
                if body.get("ok"):
                    _alert_sent[alert_key] = now  # only stamp cooldown on success
                    print(f"[kafka-monitor] alert sent for {alert_key} (+{delta} restarts)")
                else:
                    print(f"[kafka-monitor] Slack API error for {alert_key}: {body.get('error', body)}")
            except Exception as exc:
                print(f"[kafka-monitor] failed to post alert for {alert_key}: {exc}")
