from __future__ import annotations

import asyncio
import base64
import functools
import time
from collections import deque
from typing import Any

import httpx
from kubernetes import client as k8s_client
from kubernetes.stream import stream as k8s_stream

from settings import settings

# ── Module-level API clients — set once via init_clients() at startup ──────────
core_v1: k8s_client.CoreV1Api | None = None
apps_v1: k8s_client.AppsV1Api | None = None
networking_v1: k8s_client.NetworkingV1Api | None = None   # for NetworkPolicy
custom: k8s_client.CustomObjectsApi | None = None          # Routes, BuildConfigs, Tekton
http_client: httpx.Client | None = None


def init_clients(k8s_module: Any, http: httpx.Client) -> None:
    """Called once from lifespan. k8s_module is the kubernetes.client module."""
    global core_v1, apps_v1, networking_v1, custom, http_client
    # Set a 10s timeout on all API calls — prevents DNS hangs from blocking Slack responses
    cfg = k8s_module.Configuration.get_default_copy()
    cfg.retries = 1
    k8s_module.Configuration.set_default(cfg)
    core_v1 = k8s_module.CoreV1Api()
    apps_v1 = k8s_module.AppsV1Api()
    networking_v1 = k8s_module.NetworkingV1Api()
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

def run_command(subcmd: str, args: list[str], response_url: str, channel_id: str) -> None:
    try:
        result = dispatch(subcmd, args, channel_id)
        post_to_slack(response_url, f"`{subcmd}` done\n```\n{result}\n```")
    except PermissionError as exc:
        post_to_slack(response_url, f"Not allowed: {exc}", in_channel=False)
    except Exception as exc:
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
        case "teardown-all":     return cmd_teardown_all(*args)
        case "reset-all":        return cmd_reset_all()
        case "run-pipeline":     return cmd_run_pipeline()
        case "run-reset":        return cmd_run_reset()
        case "pipeline-status":  return cmd_pipeline_status()
        case "cleanup-runs":     return cmd_cleanup_runs()
        case "export-config":       return cmd_export_config()
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
        if any(c.get("reason") == "Succeeded" for c in conditions):
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

    cms = core_v1.list_namespaced_config_map(
        settings.infra_namespace,
        label_selector=f"app={settings.event_generator_name}"
    ).items
    if not cms:
        return "event-generator ConfigMap not found — skipping patch"

    for cm in cms:
        core_v1.patch_namespaced_config_map(
            cm.metadata.name, settings.infra_namespace,
            {"data": {"TEAM_BOOTSTRAP_SERVERS": bootstrap_str}}
        )

    if not registry:
        return "All teams removed — event-generator ConfigMap cleared, restart skipped"

    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    apps_v1.patch_namespaced_deployment(
        settings.event_generator_name, settings.infra_namespace,
        {"spec": {"template": {"metadata": {"annotations":
            {"kubectl.kubernetes.io/restartedAt": now}
        }}}}
    )
    return f"Event-generator patched with {len(registry)} team(s) and restarted"


def _remove_all_in_namespace(ns: str) -> None:
    """Delete all Kafka/NiFi resources from a team namespace (no label selector)."""
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


def _cancel_in_flight_runs() -> str:
    """Patch all running PipelineRuns and TaskRuns to cancelled state."""
    cancelled = 0

    # Cancel PipelineRuns
    try:
        pr_list = custom.list_namespaced_custom_object(
            "tekton.dev", "v1", "pipelineruns", settings.infra_namespace
        )
        for pr in pr_list.get("items", []):
            conditions = pr.get("status", {}).get("conditions", [])
            if any(c.get("reason") in ("Running", "Started") for c in conditions):
                try:
                    custom.patch_namespaced_custom_object(
                        "tekton.dev", "v1", "pipelineruns", settings.infra_namespace,
                        pr["metadata"]["name"],
                        {"spec": {"status": "StoppedRunFinally"}},
                    )
                    cancelled += 1
                except Exception:
                    pass
    except Exception:
        pass

    # Cancel TaskRuns
    try:
        tr_list = custom.list_namespaced_custom_object(
            "tekton.dev", "v1", "taskruns", settings.infra_namespace
        )
        for tr in tr_list.get("items", []):
            conditions = tr.get("status", {}).get("conditions", [])
            if any(c.get("reason") == "Running" for c in conditions):
                try:
                    custom.patch_namespaced_custom_object(
                        "tekton.dev", "v1", "taskruns", settings.infra_namespace,
                        tr["metadata"]["name"],
                        {"spec": {"status": "TaskRunCancelled"}},
                    )
                    cancelled += 1
                except Exception:
                    pass
    except Exception:
        pass

    return f"Cancelled {cancelled} in-flight run(s)"


def _wipe_tekton_history() -> str:
    """Delete all PipelineRun, TaskRun objects and workspace PVCs from infra namespace.
    Does NOT touch the ChatOps deployment or its resources."""
    ns = settings.infra_namespace
    chatops_name = settings.chatops_name
    deleted = 0

    # Delete all PipelineRuns
    try:
        pr_list = custom.list_namespaced_custom_object("tekton.dev", "v1", "pipelineruns", ns)
        for pr in pr_list.get("items", []):
            try:
                custom.delete_namespaced_custom_object(
                    "tekton.dev", "v1", "pipelineruns", ns, pr["metadata"]["name"]
                )
                deleted += 1
            except Exception:
                pass
    except Exception:
        pass

    # Delete all TaskRuns
    try:
        tr_list = custom.list_namespaced_custom_object("tekton.dev", "v1", "taskruns", ns)
        for tr in tr_list.get("items", []):
            try:
                custom.delete_namespaced_custom_object(
                    "tekton.dev", "v1", "taskruns", ns, tr["metadata"]["name"]
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

    return f"Wiped Tekton history: {deleted} objects deleted (ChatOps preserved)"


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
        result = custom.list_namespaced_custom_object("tekton.dev", "v1", "pipelineruns", ns)
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
            icon = "✅" if reason == "Succeeded" else ("❌" if reason in ("Failed", "PipelineRunCancelled") else "⏳")
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
        custom.get_namespaced_custom_object(
            "kafka.strimzi.io", "v1beta2", ns, "kafkanodepools", "dual-role"
        )
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
        custom.get_namespaced_custom_object(
            "kafka.strimzi.io", "v1beta2", ns, "kafkas", f"kafka-{name}"
        )
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
    # permanently skipped (no reconnect). 180s covers operator reconcile time.
    deadline = time.time() + 180
    while time.time() < deadline:
        try:
            kafka_cr = custom.get_namespaced_custom_object(
                "kafka.strimzi.io", "v1beta2", ns, "kafkas", f"kafka-{name}"
            )
            conditions = kafka_cr.get("status", {}).get("conditions", [])
            if any(c.get("type") == "Ready" and c.get("status") == "True" for c in conditions):
                break
        except k8s_client.ApiException:
            pass
        time.sleep(5)

    _upsert_team_registry(name, ns)
    eg_result = _patch_event_generator_bootstrap()
    bootstrap = _BOOTSTRAP_TMPL.format(name=name, ns=ns)
    return (
        f"Kafka deployed for {name} in {ns}\n"
        f"Bootstrap: {bootstrap}\n"
        f"{eg_result}"
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
    _remove_from_team_registry(name)
    eg_result = _patch_event_generator_bootstrap()
    return f"Kafka removed for {name} in {ns}\n{eg_result}"


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
    """Remove all resources from every non-system, non-infra namespace."""
    namespaces = _discover_team_namespaces()
    if not namespaces:
        return "No team namespaces found."
    # Collect team names from registry before removing so we can clean up
    registry = _get_team_registry()
    ns_to_name = {entry.get("namespace"): name for name, entry in registry.items()}
    results = []
    for ns in namespaces:
        try:
            _remove_all_in_namespace(ns)
            results.append(f"  {ns}: removed")
        except Exception as e:
            results.append(f"  {ns}: error — {e}")
        name = ns_to_name.get(ns)
        if name:
            _remove_from_team_registry(name)
            _remove_team_password(name)
    _patch_event_generator_bootstrap()
    return "Removed all teams:\n" + "\n".join(results)


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

    # Delete KafkaNodePool — operator removes pod and PVC, then we recreate fresh
    try:
        custom.delete_namespaced_custom_object(
            "kafka.strimzi.io", "v1beta2", ns, "kafkanodepools", "dual-role"
        )
    except k8s_client.ApiException as e:
        if e.status != 404:
            raise

    # Wait up to 120s for pod to disappear before recreating
    label_selector = f"strimzi.io/cluster=kafka-{name}"
    deadline = time.time() + 120
    while time.time() < deadline:
        pods = core_v1.list_namespaced_pod(ns, label_selector=label_selector).items
        if not pods:
            break
        time.sleep(5)

    # Recreate KafkaNodePool with fresh storage
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
    return f"Kafka data wiped for {name} in {ns}. Operator is recreating broker with fresh storage."


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
    for group, plural in [
        ("build.openshift.io", "buildconfigs"),
        ("image.openshift.io", "imagestreams"),
    ]:
        try:
            items = custom.list_namespaced_custom_object(
                group, "v1", ns, plural, label_selector=label
            )
            for item in items.get("items", []):
                custom.delete_namespaced_custom_object(
                    group, "v1", ns, plural, item["metadata"]["name"]
                )
        except Exception:
            pass
    return "Event generator removed"


def cmd_teardown_all(*args) -> str:
    """
    teardown-all           — remove events + remove all teams
    teardown-all clean     — cancel in-flight runs first, then remove events + teams
    teardown-all wipe      — cancel runs + wipe Tekton history (PipelineRuns/TaskRuns/PVCs)
                             + remove events + teams (ChatOps stays up)
    """
    mode = args[0] if args else ""
    lines = []
    if mode in ("clean", "wipe"):
        lines.append(_cancel_in_flight_runs())
    if mode == "wipe":
        lines.append(_wipe_tekton_history())
    lines.append(cmd_remove_events())
    lines.append(cmd_remove_all_teams())
    return "\n".join(lines)


def cmd_reset_all() -> str:
    """Cancel in-flight runs, teardown all, then trigger reset-and-deploy pipeline."""
    teardown_result = cmd_teardown_all("clean")
    pipeline_result = cmd_run_reset()
    return f"{teardown_result}\n{pipeline_result}"


def cmd_run_pipeline() -> str:
    return _trigger_pipeline("deploy-all-teams", "deploy-all-teams-run")


def cmd_run_reset() -> str:
    return _trigger_pipeline("reset-and-deploy", "reset-all-teams-run")


def _trigger_pipeline(pipeline_name: str, name_prefix: str) -> str:
    try:
        cluster_params = _get_last_pipeline_params()
        params = [{"name": k, "value": v} for k, v in cluster_params.items()]
    except RuntimeError:
        params = []

    body = {
        "apiVersion": "tekton.dev/v1",
        "kind": "PipelineRun",
        "metadata": {
            "generateName": f"{name_prefix}-",
            "namespace": settings.infra_namespace,
            "labels": {"app": "tekton-pipeline"},
        },
        "spec": {
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
    to_delete = items[3:]  # keep newest 3
    for run in to_delete:
        try:
            custom.delete_namespaced_custom_object(
                group="tekton.dev",
                version="v1",
                namespace=settings.infra_namespace,
                plural="pipelineruns",
                name=run["metadata"]["name"],
            )
        except Exception:
            pass
    return f"Kept 3 newest PipelineRuns, deleted {len(to_delete)} old ones."


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
  `remove-all-teams`          Remove Kafka + NiFi from all team namespaces
  `teardown-all`              Remove events + all teams
  `teardown-all clean`        Cancel in-flight runs + remove events + all teams
  `teardown-all wipe`         Cancel runs + wipe Tekton history + remove events + all teams (ChatOps stays up)
  `reset-all`                 teardown-all clean + trigger reset pipeline

*Event generator*
  `pause-events`     Scale to 0 replicas
  `resume-events`    Scale back to 1 replica
  `remove-events`    Delete entire event generator

*Pipeline*
  `run-pipeline`      Trigger deploy-all-teams pipeline
  `run-reset`         Trigger reset-and-deploy pipeline
  `pipeline-status`   Show last 5 PipelineRuns
  `cleanup-runs`      Delete old PipelineRuns (keep newest 3)

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
