# 🚀 Kubernetes Node Evacuator

![Python](https://img.shields.io/badge/python-3.8%2B-blue)
![Kubernetes](https://img.shields.io/badge/kubernetes-compatible-green)
![License](https://img.shields.io/badge/license-MIT-blue)
![Status](https://img.shields.io/badge/status-active-success)
![Contributions](https://img.shields.io/badge/contributions-welcome-brightgreen)
![Maintenance](https://img.shields.io/badge/maintained-yes-green)

> Intelligent, resource-aware Kubernetes node evacuation tool (better than `kubectl drain`)

A Python-based tool to safely evacuate pods from a Kubernetes node, with **intelligent pod ordering, batch support, StatefulSet handling**, optional **Prometheus metrics**, and live CLI progress tracking.

---

## Features

- **Safe pod evacuation** without modifying Deployment/StatefulSet specs.  
- **Pod-aware batching**: one-by-one, fixed-size batch, or all-at-once.  
- **StatefulSet support**: pods evicted in ordinal order to preserve stability.  
- **Pre- and post-checks**: waits for workloads to reach **desired state**.  
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



## Installation

```bash
pip install kubernetes prometheus-client
```

## 🏗️ Architecture

```mermaid
flowchart TD
    A[User CLI] --> B[Evacuator Script]

    B --> C[Cordon Node]
    B --> D[Fetch Pods on Node]
    B --> E[Group by Owner]

    E --> F[Sort Pods]
    F -->|Resource-aware| G[Batching Engine]

    G --> H[Evict Pods]
    H --> I[API Server]

    I --> J[Scheduler]
    J --> K[New Node Placement]

    K --> L[Replacement Pods]
    L --> M[Readiness Check]

    M --> N[Desired State Validation]

    B --> O[Prometheus Pushgateway]
    O --> P[Metrics]

    style A fill:#f9f,stroke:#333
    style B fill:#bbf,stroke:#333
    style H fill:#fbb,stroke:#333





