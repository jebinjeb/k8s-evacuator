#!/usr/bin/env python3

import time
import argparse
import logging
from collections import defaultdict

from kubernetes import client, config
from kubernetes.client.rest import ApiException

# -----------------------------
# Optional Prometheus Support
# -----------------------------
try:
    from prometheus_client import CollectorRegistry, Gauge, push_to_gateway
    METRICS_AVAILABLE = True
except ImportError:
    METRICS_AVAILABLE = False


# -----------------------------
# Logging
# -----------------------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# -----------------------------
# Load kube config
# -----------------------------
config.load_kube_config()

core = client.CoreV1Api()
apps = client.AppsV1Api()
policy = client.PolicyV1Api()

# -----------------------------
# Defaults
# -----------------------------
DEFAULT_TIMEOUT = 300
DEFAULT_RETRIES = 5
SLEEP_INTERVAL = 5


# -----------------------------
# Node Operations
# -----------------------------
def cordon_node(node_name, dry_run=False):
    log.info(f"Cordoning node {node_name}")
    if not dry_run:
        core.patch_node(node_name, {"spec": {"unschedulable": True}})


def uncordon_node(node_name, dry_run=False):
    log.info(f"Uncordoning node {node_name}")
    if not dry_run:
        core.patch_node(node_name, {"spec": {"unschedulable": False}})


# -----------------------------
# Pod Filters
# -----------------------------
def is_mirror_pod(pod):
    return pod.metadata.annotations and "kubernetes.io/config.mirror" in pod.metadata.annotations


def is_daemonset_pod(pod):
    return pod.metadata.owner_references and any(o.kind == "DaemonSet" for o in pod.metadata.owner_references)


def is_job_pod(pod):
    return pod.metadata.owner_references and any(o.kind == "Job" for o in pod.metadata.owner_references)


def is_terminal_pod(pod):
    return pod.status.phase in ["Succeeded", "Failed"]


# -----------------------------
# Get Pods
# -----------------------------
def get_pods_on_node(node_name):
    pods = core.list_pod_for_all_namespaces(
        field_selector=f"spec.nodeName={node_name}"
    ).items

    result = []

    for p in pods:
        if is_mirror_pod(p):
            continue
        if is_daemonset_pod(p):
            continue
        if is_job_pod(p):
            log.info(f"[SKIP] Job pod {p.metadata.name}")
            continue
        if is_terminal_pod(p):
            log.info(f"[SKIP] Completed pod {p.metadata.name}")
            continue
        if p.metadata.deletion_timestamp:
            continue

        result.append(p)

    return result


# -----------------------------
# Grouping
# -----------------------------
def group_by_owner(pods):
    groups = defaultdict(list)

    for pod in pods:
        if not pod.metadata.owner_references:
            key = ("orphan", pod.metadata.name, pod.metadata.namespace)
        else:
            owner = pod.metadata.owner_references[0]
            key = (owner.kind, owner.name, pod.metadata.namespace)

        groups[key].append(pod)

    return groups


# -----------------------------
# Pod Readiness
# -----------------------------
def is_pod_ready(pod):
    if pod.status.phase != "Running":
        return False

    if pod.metadata.deletion_timestamp is not None:
        return False

    if not pod.status.conditions:
        return False

    return any(c.type == "Ready" and c.status == "True" for c in pod.status.conditions)


# -----------------------------
# Desired State
# -----------------------------
def get_desired_replicas(kind, name, namespace):
    try:
        if kind == "Deployment":
            return apps.read_namespaced_deployment(name, namespace).spec.replicas
        elif kind == "StatefulSet":
            return apps.read_namespaced_stateful_set(name, namespace).spec.replicas
    except Exception:
        pass

    return None


def count_ready_pods(namespace, owner_uid):
    pods = core.list_namespaced_pod(namespace).items
    return sum(
        1 for p in pods
        if p.metadata.owner_references
        and p.metadata.owner_references[0].uid == owner_uid
        and is_pod_ready(p)
    )


def wait_until_desired_state(kind, name, namespace, owner_uid,
                             timeout=DEFAULT_TIMEOUT, retries=DEFAULT_RETRIES):

    desired = get_desired_replicas(kind, name, namespace)

    if desired is None:
        log.warning(f"[WARN] Skipping desired state check for {kind}/{name}")
        return True

    for attempt in range(retries):
        log.info(f"[CHECK] {kind}/{name} attempt {attempt+1}/{retries}")

        start = time.time()

        while time.time() - start < timeout:
            ready = count_ready_pods(namespace, owner_uid)

            log.info(f"[STATUS] {kind}/{name}: {ready}/{desired}")

            if ready == desired:
                return True

            time.sleep(SLEEP_INTERVAL)

    raise TimeoutError(f"{kind}/{name} failed desired state")

# -----------------------------
# Resource-based Sorting
# -----------------------------
def get_pod_resource_score(pod):
    cpu = 0
    memory = 0

    if not pod.spec.containers:
        return 0

    for c in pod.spec.containers:
        resources = c.resources.requests or {}

        cpu_val = resources.get("cpu", "0")
        mem_val = resources.get("memory", "0")

        # CPU → millicores
        try:
            if isinstance(cpu_val, str) and "m" in cpu_val:
                cpu += int(cpu_val.replace("m", ""))
            else:
                cpu += int(float(cpu_val) * 1000)
        except Exception:
            pass

        # Memory → Mi
        try:
            if isinstance(mem_val, str):
                if "Mi" in mem_val:
                    memory += int(mem_val.replace("Mi", ""))
                elif "Gi" in mem_val:
                    memory += int(mem_val.replace("Gi", "")) * 1024
        except Exception:
            pass

    return (cpu * 2) + memory

# -----------------------------
# Eviction
# -----------------------------
def evict_pod(pod, dry_run=False, retries=DEFAULT_RETRIES):
    for attempt in range(retries):
        log.info(f"[EVICT] {pod.metadata.namespace}/{pod.metadata.name} (try {attempt+1})")

        if dry_run:
            return

        eviction = client.V1Eviction(
            metadata=client.V1ObjectMeta(
                name=pod.metadata.name,
                namespace=pod.metadata.namespace
            )
        )

        try:
            policy.create_namespaced_pod_eviction(
                name=pod.metadata.name,
                namespace=pod.metadata.namespace,
                body=eviction
            )
            return

        except ApiException as e:
            if e.status == 429:
                log.warning("PDB blocked eviction, retrying...")
                time.sleep(SLEEP_INTERVAL)
            else:
                raise

    raise RuntimeError(f"Failed to evict {pod.metadata.name}")


# -----------------------------
# Wait for Replacement
# -----------------------------
def wait_for_replacement(pod, source_node,
                         timeout=DEFAULT_TIMEOUT, retries=DEFAULT_RETRIES):

    ns = pod.metadata.namespace

    owner_refs = pod.metadata.owner_references
    if not owner_refs:
        log.warning(f"[SKIP] Orphan pod {pod.metadata.name}")
        return True

    owner_uid = owner_refs[0].uid

    old_pods = core.list_namespaced_pod(ns).items
    old_uids = {p.metadata.uid for p in old_pods if p.metadata.owner_references and p.metadata.owner_references[0].uid == owner_uid}

    for attempt in range(retries):
        log.info(f"[WAIT] Replacement attempt {attempt+1}/{retries}")

        start = time.time()

        while time.time() - start < timeout:
            pods = core.list_namespaced_pod(ns).items

            for p in pods:
                if not p.metadata.owner_references:
                    continue

                if p.metadata.owner_references[0].uid != owner_uid:
                    continue

                if p.metadata.uid in old_uids:
                    continue

                if is_pod_ready(p) and p.spec.node_name != source_node:
                    log.info(f"[READY] Replacement pod {p.metadata.name}")
                    return True

            time.sleep(SLEEP_INTERVAL)

    raise TimeoutError(f"Replacement pod failed for {pod.metadata.name}")


# -----------------------------
# Progress Tracker
# -----------------------------
class ProgressTracker:
    def __init__(self, total, pushgateway=None):
        self.total = total
        self.evicted = 0
        self.ready = 0
        self.failed = 0
        self.pushgateway = pushgateway

        if pushgateway and METRICS_AVAILABLE:
            self.registry = CollectorRegistry()
            self.metric = Gauge(
                'evacuation_pod_status',
                'Pod evacuation status',
                ['pod', 'namespace', 'status'],
                registry=self.registry
            )
        else:
            self.registry = None

    def log(self):
        log.info(f"[PROGRESS] total={self.total} evicted={self.evicted} ready={self.ready} failed={self.failed}")

    def update_metrics(self, pod, ns, status):
        if self.registry:
            self.metric.labels(pod=pod, namespace=ns, status=status).set(1)
            push_to_gateway(self.pushgateway, job="k8s_evacuator", registry=self.registry)


# -----------------------------
# Evacuation Logic
# -----------------------------
def evacuate_group(kind, name, ns, pods, batch_size, tracker, dry_run, strategy="high"):

    if kind == "orphan":
        log.warning(f"[SKIP] Orphan workload {name}")
        return

    owner_uid = pods[0].metadata.owner_references[0].uid

    log.info(f"[GROUP] {kind}/{name} ({ns}) - {len(pods)} pods")

    wait_until_desired_state(kind, name, ns, owner_uid)


    reverse = True if strategy == "high" else False

    # -----------------------------
    # Sorting Logic
    # -----------------------------
    if kind == "StatefulSet":
        # Preserve strict ordering for StatefulSets
        try:
            pods = sorted(pods, key=lambda p: int(p.metadata.name.split("-")[-1]))
            log.info("[ORDER] StatefulSet ordinal order applied")
        except Exception:
            log.warning("Failed to sort StatefulSet pods")
    else:
        pods = sorted(pods, key=get_pod_resource_score, reverse=reverse)
        log.info(f"[ORDER] Pods sorted by resource usage ({'high→low' if reverse else 'low→high'})")

    # -----------------------------
    # Batching
    # -----------------------------
    if batch_size <= 0:
        batches = [pods]
    elif batch_size == 1:
        batches = [[p] for p in pods]
    else:
        batches = [pods[i:i + batch_size] for i in range(0, len(pods), batch_size)]

    # -----------------------------
    # Evacuation Loop
    # -----------------------------
    for batch in batches:
        log.info(f"[BATCH] Processing {len(batch)} pods")

        for pod in batch:
            evict_pod(pod, dry_run=dry_run)
            tracker.evicted += 1
            tracker.log()

        if not dry_run:
            for pod in batch:
                wait_for_replacement(pod, pod.spec.node_name)

        wait_until_desired_state(kind, name, ns, owner_uid)

# -----------------------------
# Main
# -----------------------------
def main():
    global DEFAULT_TIMEOUT, DEFAULT_RETRIES
    parser = argparse.ArgumentParser(description="Advanced K8s Node Evacuator")
    parser.add_argument("--node", required=True)
    parser.add_argument("--batch-size", type=int, default=2)
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    parser.add_argument("--retries", type=int, default=DEFAULT_RETRIES)
    parser.add_argument("--uncordon", action="store_true")
    parser.add_argument("--pushgateway")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--eviction-strategy", choices=["high", "low"], default="high")

    args = parser.parse_args()

    DEFAULT_TIMEOUT = args.timeout
    DEFAULT_RETRIES = args.retries

    cordon_node(args.node, dry_run=args.dry_run)

    try:
        pods = get_pods_on_node(args.node)
        log.info(f"Found {len(pods)} pods")

        groups = group_by_owner(pods)

        tracker = ProgressTracker(total=len(pods), pushgateway=args.pushgateway)

        for (kind, name, ns), group_pods in groups.items():
            evacuate_group(kind, name, ns, group_pods, args.batch_size, tracker, args.dry_run)

    finally:
        if args.uncordon:
            uncordon_node(args.node, dry_run=args.dry_run)

    log.info("Evacuation completed successfully")


if __name__ == "__main__":
    main()