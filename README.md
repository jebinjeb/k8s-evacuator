# 🚀 Kubernetes Node Evacuator

![Python](https://img.shields.io/badge/python-3.8%2B-blue)
![Kubernetes](https://img.shields.io/badge/kubernetes-compatible-green)
![License](https://img.shields.io/badge/license-MIT-blue)
![Status](https://img.shields.io/badge/status-active-success)
![Contributions](https://img.shields.io/badge/contributions-welcome-brightgreen)
![Maintenance](https://img.shields.io/badge/maintained-yes-green)

> 🧠 A smarter, workload-aware alternative to kubectl drain using the Kubernetes Eviction API

A Python-based tool to safely evacuate pods from a Kubernetes node, with **intelligent pod ordering, batch support, StatefulSet handling**, optional **Prometheus metrics**, and live CLI progress tracking.

---

## ✨ Why not just `kubectl drain`?

While `kubectl drain` is safe, it is **not workload-aware**:

- Evicts pods in a mostly **flat / unordered way**
- No control over **batching strategies**
- No awareness of **resource impact per workload**
- Limited visibility into **progress and recovery**

👉 This tool solves these problems with **controlled, observable, and intelligent evacuation**.

---

## ⚙️ Eviction Strategy (Production-Safe)

This tool uses the **Kubernetes Eviction API** — the same mechanism as `kubectl drain`.

### ✅ Guarantees

- ✅ Graceful pod eviction (no abrupt termination)  
- ✅ Respects **PodDisruptionBudgets (PDB)**  
- ✅ Waits for **replacement pods to become Ready**  
- ✅ Ensures workloads reach **desired state before continuing**  
- ❌ Does **NOT** force delete pods by default  

---

### ⚙️ Behavior

- Uses **`policy/v1` Eviction API**
- Retries when blocked by PDB (`429 Too Many Requests`)
- Supports configurable fallback strategies:
  - Graceful delete
  - Force delete *(optional, last resort)*

---

> ⚠️ Designed for **zero-downtime or minimal-disruption operations** in production environments.

---
## Features

- **Safe pod evacuation** without modifying Deployment/StatefulSet specs.  
- **Pod-aware batching**: one-by-one, fixed-size batch, or all-at-once.  
- **StatefulSet support**: pods evicted in ordinal order to preserve stability. 
- **Grouping strategies**:
  - owner (default for workloads) → evacuates pods workload by workload.
  - spread → evicts pods from multiple workloads evenly across batches to minimize impact per workload.
    - Supports controlled execution via `--max-batches` (process N batches and exit).
- **Pre and post-checks**: waits for workloads to reach **desired state**.  
- **Excludes**: DaemonSets, Jobs, completed/failed pods, mirror pods.  
- **Optional metrics**: push per-pod progress and status to Prometheus Pushgateway.  
- **Live progress tracking** in CLI.  
- Fully configurable **timeout** and **retry** logic.
- Dry-run mode for safe validation

---

## TODO

- **Rollback / Retry**: If eviction fails mid-way, optionally rollback already evicted pods or retry safely.
- **Advanced Dry-Run Simulation**: Check if pods can actually be scheduled on other nodes before eviction.
  - NodeSelector / Affinity rules
  - Taints & tolerations
  - Available CPU / memory
- Consider per-workload max-unavailable in spread mode to avoid evicting all replicas at once.

## 🚧 Experimental / In-Progress Features

The following features are under development and may evolve:

### 🔹 `--batch-size 0` (Dynamic Batching)
Automatically calculates a safe batch size based on workload size.

- Adapts eviction speed to the number of replicas
- Prevents over-eviction of small workloads
- Designed to maintain minimum availability during evacuation

---

### ⚡ `--evict-all-safe` (Fast Path Eviction)
Evicts all pods in a workload at once when considered safe.

- Useful for stateless or highly replicated workloads
- Skips batching for faster node evacuation
- Automatically avoids unsafe scenarios (e.g., StatefulSets)

---

### 🛡️ `--respect-pdb` (PDB-Aware Eviction)
Adjusts eviction behavior based on PodDisruptionBudgets.

- Uses Kubernetes PDB limits to determine safe eviction count
- Prevents disruption beyond allowed thresholds
- Aligns evacuation strategy with cluster safety policies

---

> ⚠️ These features are experimental and may change in future releases.


## 📦 Installation

### 🔹 Prerequisites

- Python **3.8+**
- Access to a Kubernetes cluster
- `kubeconfig` configured (e.g., `~/.kube/config`)  
  or running inside a cluster (in-cluster config)

---

### 🔹 Install Dependencies

```bash
pip install kubernetes prometheus-client
```

### 🔹 Run the Tool
```bash
python k8s_evacuator.py --node <node_name>
```

## 🏗️ Architecture

```mermaid
flowchart TD
    A[User CLI] --> B[Evacuator Engine]

    B --> C[Cordon Node]
    B --> D[Fetch Pods on Node]

    D --> E[Filter Pods]
    E --> F{Grouping Strategy}

    F -->|owner| G[Group by Workload]
    F -->|spread| H[Spread Across Workloads]

    G --> I[Sort Pods]
    H --> I

    I --> J{Batching Strategy}

    J -->|fixed| K[Static Batching]
    J -->|dynamic| L[Dynamic Batch Calculation]
    J -->|evict-all-safe| M[Fast Path Eviction]

    K --> N[Eviction Loop]
    L --> N
    M --> N

    N --> O[Evict via Eviction API]

    O -->|PDB Block (429)| P[Retry / Backoff]
    P --> O

    O --> Q[Scheduler]
    Q --> R[New Pod Placement]

    R --> S[Replacement Pods]
    S --> T[Readiness Check]

    T --> U[Desired State Validation]

    U -->|Not Ready| N
    U -->|Ready| V[Next Batch]

    B --> W[Prometheus Pushgateway]
    W --> X[Metrics]

    style A fill:#f9f,stroke:#333
    style B fill:#bbf,stroke:#333
    style O fill:#fbb,stroke:#333
```

## 📊 Metrics (Prometheus Pushgateway)

This tool can optionally push per-pod evacuation metrics to a Prometheus Pushgateway if `prometheus_client` is installed and the `--pushgateway` flag is provided.

### Metric

| Metric Name                | Labels                     | Description                                                                 |
|-----------------------------|----------------------------|-----------------------------------------------------------------------------|
| `evacuation_pod_status`     | `pod`, `namespace`, `status` | Tracks the status of each pod during evacuation. `status` can be: `evicted`, `ready`, or `failed`. |

### Status Labels

- **evicted** → Pod eviction has been initiated.  
- **ready** → Replacement pod is running and ready.  
- **failed** → Pod eviction failed or replacement pod did not become ready.  

### Usage

```bash
python k8s_evacuator.py --node <node_name> --pushgateway http://pushgateway.example.com:9091
```

## 📊 Metrics TODO

### 🚀 1. Pod Movement Metric

Tracks how pods move between nodes during evacuation.

#### Metric Definition

| Metric Name | Labels | Description |
|------------|--------|------------|
| `evacuation_pod_movement` | `old_pod`, `old_node`, `new_pod`, `new_node`, `namespace`, `status` | Tracks pod migration from source node to destination node |

#### Label Details

| Label | Description | Example |
|------|------------|--------|
| `old_pod` | Original pod name | `nginx-abc123` |
| `old_node` | Source node (hostname) | `k3s-lab-worker` |
| `new_pod` | New pod name after rescheduling | `nginx-xyz789` |
| `new_node` | Destination node | `k3s-lab-worker-2` |
| `namespace` | Kubernetes namespace | `default` |
| `status` | Movement result | `moved`, `failed` |

#### Examples

| Scenario | Metric |
|--------|--------|
| Deployment Pod | `evacuation_pod_movement{old_pod="nginx-abc", old_node="node1", new_pod="nginx-def", new_node="node2", namespace="default", status="moved"} 1` |
| StatefulSet Pod | `evacuation_pod_movement{old_pod="mysql-0", old_node="node1", new_pod="mysql-0", new_node="node2", namespace="db", status="moved"} 1` |
| Failed Evacuation | `evacuation_pod_movement{old_pod="redis-abc", old_node="node1", new_pod="unknown", new_node="unknown", namespace="cache", status="failed"} 1` |

---

### 📦 2. Pod Status Metric

Tracks lifecycle of pods during evacuation.

#### Metric Definition

| Metric Name | Labels | Description |
|------------|--------|------------|
| `evacuation_pod_status` | `pod`, `namespace`, `status` | Tracks pod state transitions during evacuation |

#### Label Details

| Label | Description | Example |
|------|------------|--------|
| `pod` | Pod name | `nginx-abc123` |
| `namespace` | Kubernetes namespace | `default` |
| `status` | Pod state | `evicted`, `ready`, `failed` |

#### Examples

| Status | Metric |
|-------|--------|
| Evicted | `evacuation_pod_status{pod="nginx-abc", namespace="default", status="evicted"} 1` |
| Ready | `evacuation_pod_status{pod="nginx-def", namespace="default", status="ready"} 1` |

---

### ⏱️ 3. Pod Rescheduling Duration (Optional)

Tracks time taken for pods to become ready after eviction.

#### Metric Definition

| Metric Name | Labels | Description |
|------------|--------|------------|
| `evacuation_pod_reschedule_duration_seconds` | `pod`, `namespace` | Time taken for pod rescheduling |

#### Example

| Metric Type | Example |
|------------|--------|
| Bucket | `evacuation_pod_reschedule_duration_seconds_bucket{pod="nginx", namespace="default", le="5"} 1` |
| Sum | `evacuation_pod_reschedule_duration_seconds_sum{pod="nginx", namespace="default"} 3.2` |
| Count | `evacuation_pod_reschedule_duration_seconds_count{pod="nginx", namespace="default"} 1` |

---

### 🔍 Useful Prometheus Queries

| Use Case | Query |
|--------|------|
| Pods moved from a node | `evacuation_pod_movement{old_node="k3s-lab-worker", status="moved"}` |
| Distribution across nodes | `sum by (new_node) (evacuation_pod_movement{status="moved"})` |
| Failed evacuations | `evacuation_pod_movement{status="failed"}` |
| Total evicted pods | `count(evacuation_pod_status{status="evicted"})` |

---
