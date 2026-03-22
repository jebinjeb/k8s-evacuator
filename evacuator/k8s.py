# k8s.py
import logging
import time
from collections import defaultdict
from kubernetes import client
from kubernetes.client.rest import ApiException


log = logging.getLogger(__name__)
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
def cordon_node(core, node_name, dry_run=False):
    log.info(f"Cordoning node {node_name}")
    if not dry_run:
        core.patch_node(node_name, {"spec": {"unschedulable": True}})

def uncordon_node(core, node_name, dry_run=False):
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

def get_pods_on_node(core, node_name):
    pods = core.list_pod_for_all_namespaces(field_selector=f"spec.nodeName={node_name}").items
    result = []
    for p in pods:
        if is_mirror_pod(p) or is_daemonset_pod(p) or is_terminal_pod(p) or p.metadata.deletion_timestamp:
            continue
        if is_job_pod(p):
            log.info(f"[SKIP] Job pod {p.metadata.name}")
            continue
        result.append(p)
    return result

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

def is_pod_ready(pod):
    if pod.status.phase != "Running" or pod.metadata.deletion_timestamp is not None or not pod.status.conditions:
        return False
    return any(c.type == "Ready" and c.status == "True" for c in pod.status.conditions)

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

def wait_until_desired_state(kind, name, namespace, owner_uid, timeout=300, retries=5):
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

def get_pod_resource_score(pod):
    cpu, memory = 0, 0
    for c in pod.spec.containers or []:
        resources = c.resources.requests or {}
        cpu_val = resources.get("cpu", "0")
        mem_val = resources.get("memory", "0")
        try:
            cpu += int(cpu_val.replace("m", "")) if "m" in cpu_val else int(float(cpu_val) * 1000)
        except Exception:
            pass
        try:
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


from collections import defaultdict, deque

def group_by_spread(pods, batch_size=2):
    """
    Evenly distribute pods across batches using round-robin across workloads.
    Works with:
    - random pod names
    - uneven replicas
    - single/multiple workloads
    """

    owners = defaultdict(list)

    # Step 1: group by owner
    for pod in pods:
        if not pod.metadata.owner_references:
            key = ("orphan", pod.metadata.name, pod.metadata.namespace)
        else:
            owner = pod.metadata.owner_references[0]
            key = (owner.kind, owner.name, pod.metadata.namespace)

        owners[key].append(pod)

    # Step 2: convert to queues (important for popping)
    queues = {k: deque(v) for k, v in owners.items()}

    batches = []

    while any(queues.values()):
        batch = []

        # round-robin pick
        for key in list(queues.keys()):
            if queues[key]:
                batch.append(queues[key].popleft())

                if batch_size and len(batch) >= batch_size:
                    break

        if batch:
            batches.append(batch)

    return batches