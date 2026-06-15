from __future__ import annotations

import time
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
        case "status":          return cmd_status(*args)
        case "add-kafka":       return cmd_add_kafka(*args)
        case "add-nifi":        return cmd_add_nifi(*args)
        case "remove-team":     return cmd_remove_team(*args)
        case "remove-kafka":    return cmd_remove_kafka(*args)
        case "remove-nifi":     return cmd_remove_nifi(*args)
        case "wipe-kafka-data":  return cmd_wipe_kafka_data(*args)
        case "restart-kafka":    return cmd_restart_kafka(*args)
        case "restart-nifi":     return cmd_restart_nifi(*args)
        case "reset-password":   return cmd_reset_password(*args)
        case "pause-events":    return cmd_pause_events()
        case "resume-events":   return cmd_resume_events()
        case "remove-events":   return cmd_remove_events()
        case "run-pipeline":    return cmd_run_pipeline()
        case "run-reset":       return cmd_run_reset()
        case "pipeline-status": return cmd_pipeline_status()
        case "cleanup-runs":    return cmd_cleanup_runs()
        case "help":            return HELP_TEXT
        case _:                 return f"Unknown command: `{subcmd}`\n\n{HELP_TEXT}"


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
    """Raise RuntimeError with a clear message if the namespace doesn't exist."""
    try:
        core_v1.read_namespace(ns)
    except k8s_client.ApiException as e:
        if e.status == 404:
            raise RuntimeError(
                f"Namespace '{ns}' does not exist.\n"
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
        "No successful PipelineRun found. Run `bash tekton/setup.sh` first, "
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


# ── Command implementations ────────────────────────────────────────────────────

def cmd_status(name: str, ns: str) -> str:
    lines = [f"Status for {name} in {ns}:"]

    label = f"app in (kafka-{name},nifi-{name})"
    lines += _label_selector_list(ns, label, ["pod", "service", "persistentvolumeclaim"])

    # Route (OpenShift custom resource)
    try:
        routes = custom.list_namespaced_custom_object(
            group="route.openshift.io",
            version="v1",
            plural="routes",
            namespace=ns,
            label_selector=f"app=nifi-{name}",
        )
        for r in routes.get("items", []):
            host = r.get("spec", {}).get("host", "")
            lines.append(f"  route: https://{host}/nifi")
    except Exception:
        pass

    return "\n".join(lines) if len(lines) > 1 else f"No resources found for {name} in {ns}"


def cmd_add_kafka(name: str, ns: str) -> str:
    """
    Deploy Kafka for a single team using the same resource spec as
    kafka/per-team/kafka-per-team-template.yaml. Mirrors ops.sh _do_add_kafka.
    """
    _check_namespace(ns)

    labels = {"app": f"kafka-{name}", "component": "kafka", "team": name}
    selector = {"app": f"kafka-{name}"}
    advertised = (
        f"PLAINTEXT://kafka-{name}-0.kafka-{name}-headless"
        f".{ns}.svc.cluster.local:9092"
    )
    quorum_voters = (
        f"1@kafka-{name}-0.kafka-{name}-headless"
        f".{ns}.svc.cluster.local:9093"
    )

    # ClusterIP service
    _apply_or_update_service(ns, k8s_client.V1Service(
        metadata=k8s_client.V1ObjectMeta(name=f"kafka-{name}", namespace=ns, labels=labels),
        spec=k8s_client.V1ServiceSpec(
            type="ClusterIP",
            ports=[k8s_client.V1ServicePort(port=9092, name="kafka", target_port=9092)],
            selector=selector,
        ),
    ))

    # Headless service
    _apply_or_update_service(ns, k8s_client.V1Service(
        metadata=k8s_client.V1ObjectMeta(
            name=f"kafka-{name}-headless", namespace=ns, labels=labels
        ),
        spec=k8s_client.V1ServiceSpec(
            cluster_ip="None",
            ports=[
                k8s_client.V1ServicePort(port=9092, name="kafka", target_port=9092),
                k8s_client.V1ServicePort(port=9093, name="controller", target_port=9093),
            ],
            selector=selector,
        ),
    ))

    # StatefulSet (mirrors kafka-per-team-template.yaml exactly)
    _apply_or_update_stateful_set(ns, k8s_client.V1StatefulSet(
        metadata=k8s_client.V1ObjectMeta(
            name=f"kafka-{name}", namespace=ns, labels=labels
        ),
        spec=k8s_client.V1StatefulSetSpec(
            service_name=f"kafka-{name}-headless",
            replicas=1,
            selector=k8s_client.V1LabelSelector(match_labels=selector),
            template=k8s_client.V1PodTemplateSpec(
                metadata=k8s_client.V1ObjectMeta(labels=labels),
                spec=k8s_client.V1PodSpec(
                    containers=[k8s_client.V1Container(
                        name="kafka",
                        image="confluentinc/cp-kafka:7.5.0",
                        ports=[
                            k8s_client.V1ContainerPort(
                                container_port=9092, name="kafka", protocol="TCP"
                            ),
                            k8s_client.V1ContainerPort(
                                container_port=9093, name="controller", protocol="TCP"
                            ),
                        ],
                        env=[
                            k8s_client.V1EnvVar(
                                name="CLUSTER_ID", value="MkU3OEVBNTcwNTJENDM2Qk"
                            ),
                            k8s_client.V1EnvVar(name="KAFKA_NODE_ID", value="1"),
                            k8s_client.V1EnvVar(
                                name="KAFKA_PROCESS_ROLES", value="broker,controller"
                            ),
                            k8s_client.V1EnvVar(
                                name="KAFKA_LISTENERS",
                                value="PLAINTEXT://:9092,CONTROLLER://:9093",
                            ),
                            k8s_client.V1EnvVar(
                                name="KAFKA_ADVERTISED_LISTENERS", value=advertised
                            ),
                            k8s_client.V1EnvVar(
                                name="KAFKA_CONTROLLER_LISTENER_NAMES", value="CONTROLLER"
                            ),
                            k8s_client.V1EnvVar(
                                name="KAFKA_LISTENER_SECURITY_PROTOCOL_MAP",
                                value="CONTROLLER:PLAINTEXT,PLAINTEXT:PLAINTEXT",
                            ),
                            k8s_client.V1EnvVar(
                                name="KAFKA_CONTROLLER_QUORUM_VOTERS", value=quorum_voters
                            ),
                            k8s_client.V1EnvVar(
                                name="KAFKA_OFFSETS_TOPIC_REPLICATION_FACTOR", value="1"
                            ),
                            k8s_client.V1EnvVar(
                                name="KAFKA_TRANSACTION_STATE_LOG_REPLICATION_FACTOR",
                                value="1",
                            ),
                            k8s_client.V1EnvVar(
                                name="KAFKA_TRANSACTION_STATE_LOG_MIN_ISR", value="1"
                            ),
                            k8s_client.V1EnvVar(
                                name="KAFKA_DEFAULT_REPLICATION_FACTOR", value="1"
                            ),
                            k8s_client.V1EnvVar(
                                name="KAFKA_MIN_INSYNC_REPLICAS", value="1"
                            ),
                            k8s_client.V1EnvVar(
                                name="KAFKA_AUTO_CREATE_TOPICS_ENABLE", value="true"
                            ),
                            k8s_client.V1EnvVar(
                                name="KAFKA_LOG_DIRS", value="/mnt/kafka-data/logs"
                            ),
                            k8s_client.V1EnvVar(
                                name="KAFKA_LOG_RETENTION_HOURS", value="24"
                            ),
                            k8s_client.V1EnvVar(
                                name="KAFKA_LOG_RETENTION_BYTES", value="104857600"
                            ),
                            k8s_client.V1EnvVar(
                                name="KAFKA_LOG_SEGMENT_BYTES", value="52428800"
                            ),
                            k8s_client.V1EnvVar(
                                name="KAFKA_LOG_CLEANUP_POLICY", value="delete"
                            ),
                            k8s_client.V1EnvVar(
                                name="KAFKA_LOG_RETENTION_CHECK_INTERVAL_MS", value="300000"
                            ),
                            k8s_client.V1EnvVar(
                                name="KAFKA_LOG_SEGMENT_DELETE_DELAY_MS", value="60000"
                            ),
                            k8s_client.V1EnvVar(
                                name="KAFKA_HEAP_OPTS", value="-Xms256m -Xmx512m"
                            ),
                        ],
                        volume_mounts=[
                            k8s_client.V1VolumeMount(
                                name="data", mount_path="/mnt/kafka-data"
                            )
                        ],
                        resources=k8s_client.V1ResourceRequirements(
                            requests={"memory": "512Mi", "cpu": "250m"},
                            limits={"memory": "1Gi", "cpu": "500m"},
                        ),
                        readiness_probe=k8s_client.V1Probe(
                            tcp_socket=k8s_client.V1TCPSocketAction(port=9092),
                            initial_delay_seconds=30,
                            period_seconds=10,
                            timeout_seconds=5,
                            failure_threshold=3,
                        ),
                        liveness_probe=k8s_client.V1Probe(
                            tcp_socket=k8s_client.V1TCPSocketAction(port=9092),
                            initial_delay_seconds=60,
                            period_seconds=30,
                            timeout_seconds=10,
                            failure_threshold=3,
                        ),
                    )]
                ),
            ),
            volume_claim_templates=[
                k8s_client.V1PersistentVolumeClaim(
                    metadata=k8s_client.V1ObjectMeta(name="data", labels=labels),
                    spec=k8s_client.V1PersistentVolumeClaimSpec(
                        access_modes=["ReadWriteOnce"],
                        resources=k8s_client.V1VolumeResourceRequirements(
                            requests={"storage": "2Gi"}
                        ),
                        storage_class_name=settings.storage_class,
                    ),
                )
            ],
        ),
    ))

    bootstrap = (
        f"kafka-{name}-0.kafka-{name}-headless.{ns}.svc.cluster.local:9092"
    )
    return (
        f"Kafka deployed for {name} in {ns}\n"
        f"Bootstrap: {bootstrap}"
    )


def cmd_add_nifi(name: str, ns: str, pwd: str) -> str:
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


def cmd_remove_team(name: str, ns: str) -> str:
    cmd_remove_kafka(name, ns)
    cmd_remove_nifi(name, ns)
    return f"Kafka + NiFi removed for {name} in {ns}"


def cmd_remove_kafka(name: str, ns: str) -> str:
    label = f"app=kafka-{name}"
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
    return f"Kafka removed for {name} in {ns}"


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
            "route.openshift.io", "v1", "routes", ns, label_selector=label
        )
        for r in routes.get("items", []):
            custom.delete_namespaced_custom_object(
                "route.openshift.io", "v1", "routes", ns, r["metadata"]["name"]
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


def cmd_wipe_kafka_data(name: str, ns: str) -> str:
    sts_name = f"kafka-{name}"
    # Verify StatefulSet exists first
    try:
        apps_v1.read_namespaced_stateful_set(sts_name, ns)
    except k8s_client.ApiException as e:
        if e.status == 404:
            raise RuntimeError(
                f"StatefulSet {sts_name} not found in {ns} — Kafka is not deployed for this team."
            )
        raise

    # Scale to 0
    apps_v1.patch_namespaced_stateful_set_scale(
        sts_name, ns, {"spec": {"replicas": 0}}
    )
    # Wait up to 60s for pod to disappear
    deadline = time.time() + 60
    while time.time() < deadline:
        try:
            core_v1.read_namespaced_pod(f"{sts_name}-0", ns)
            time.sleep(3)
        except k8s_client.ApiException as e:
            if e.status == 404:
                break
    # Delete PVCs
    label = f"app=kafka-{name}"
    for pvc in core_v1.list_namespaced_persistent_volume_claim(
        ns, label_selector=label
    ).items:
        core_v1.delete_namespaced_persistent_volume_claim(pvc.metadata.name, ns)
    # Scale back to 1
    apps_v1.patch_namespaced_stateful_set_scale(
        sts_name, ns, {"spec": {"replicas": 1}}
    )
    return f"Kafka data wiped for {name} in {ns}. Pod restarting with fresh storage."


def cmd_restart_kafka(name: str, ns: str) -> str:
    core_v1.delete_namespaced_pod(
        f"kafka-{name}-0", ns, body=k8s_client.V1DeleteOptions()
    )
    return f"kafka-{name}-0 deleted — StatefulSet will restart it"


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

    # NiFi stores the bcrypt hash in conf/login-identity-providers.xml on the PVC.
    # The env var is only read when no credentials file exists yet.
    # Use the nifi.sh CLI to update the hash, then restart the pod to reload it.
    resp = k8s_stream(
        core_v1.connect_get_namespaced_pod_exec,
        pod_name,
        ns,
        command=[
            "/opt/nifi/nifi-current/bin/nifi.sh",
            "set-single-user-credentials",
            name,
            pwd,
        ],
        stderr=True,
        stdin=False,
        stdout=True,
        tty=False,
    )
    if "ERROR" in resp:
        raise RuntimeError(f"nifi.sh set-single-user-credentials failed: {resp.strip()}")

    core_v1.delete_namespaced_pod(pod_name, ns, body=k8s_client.V1DeleteOptions())
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
                group, "v1", plural, ns, label_selector=label
            )
            for item in items.get("items", []):
                custom.delete_namespaced_custom_object(
                    group, "v1", plural, ns, item["metadata"]["name"]
                )
        except Exception:
            pass
    return "Event generator removed"


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


HELP_TEXT = """\
*Available commands* (`/infra <command> [args]`):

*Status*
  `status <name> <ns>`              Show pods, services, PVCs, and route for a team

*Kafka / NiFi*
  `add-kafka <name> <ns>`           Deploy Kafka for a team (direct k8s API)
  `add-nifi <name> <ns> <pwd>`      Deploy NiFi for a team — waits until Ready
  `remove-team <name> <ns>`         Remove Kafka + NiFi
  `remove-kafka <name> <ns>`        Remove only Kafka
  `remove-nifi <name> <ns>`         Remove only NiFi
  `wipe-kafka-data <name> <ns>`          Delete Kafka PVC (data wiped, pod restarts fresh)
  `restart-kafka <name> <ns>`            Restart Kafka pod
  `restart-nifi <name> <ns>`             Restart NiFi pod
  `reset-password <name> <ns> <pwd>`     Reset NiFi login password (min 12 chars)

*Event generator*
  `pause-events`     Scale to 0 replicas
  `resume-events`    Scale back to 1 replica
  `remove-events`    Delete entire event generator

*Pipeline*
  `run-pipeline`      Trigger deploy-all-teams pipeline
  `run-reset`         Trigger reset-and-deploy pipeline
  `pipeline-status`   Show last 5 PipelineRuns
  `cleanup-runs`      Delete old PipelineRuns (keep newest 3)
"""
