#!/usr/bin/env python3
import argparse
import logging
from k8s import (
    cordon_node,
    uncordon_node,
    get_pods_on_node,
    group_by_owner,
    group_by_spread,
    evict_pod,
    wait_for_replacement,
)
from evacuator import evacuate_group
from metrics import ProgressTracker
from kube import load_config, get_clients

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 300
DEFAULT_RETRIES = 5

def main():
    parser = argparse.ArgumentParser(description="Advanced K8s Node Evacuator")
    parser.add_argument("--node", required=True)
    parser.add_argument("--batch-size", type=int, default=2)
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT)
    parser.add_argument("--retries", type=int, default=DEFAULT_RETRIES)
    parser.add_argument("--uncordon", action="store_true")
    parser.add_argument("--pushgateway")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--eviction-strategy", choices=["high", "low"], default="high")
    parser.add_argument(
        "--group-strategy",
        choices=["owner", "spread"],
        default="spread",
        help="Grouping strategy for evacuation"
    )
    args = parser.parse_args()

    # Load Kubernetes config FIRST
    load_config()

    # Create API clients AFTER config
    clients = get_clients()
    core = clients["core"]
    policy = clients["policy"]

    # Cordon the node
    cordon_node(core, args.node, dry_run=args.dry_run)

    try:
        pods = get_pods_on_node(core, args.node)
        log.info(f"Found {len(pods)} pods")

        tracker = ProgressTracker(total=len(pods), pushgateway=args.pushgateway)

        if args.group_strategy == "owner":
            grouped = group_by_owner(pods)
            groups = list(grouped.items())

            # Evacuate workload by workload
            for (kind, name, ns), group_pods in groups:
                evacuate_group(
                    kind, name, ns,
                    group_pods,
                    args.batch_size,
                    tracker,
                    args.dry_run,
                    strategy=args.eviction_strategy
                )

        elif args.group_strategy == "spread":
            batches = group_by_spread(pods, args.batch_size)

            # Spread mode: evict pods in batches directly
            for batch in batches:
                log.info(f"[SPREAD-BATCH] Evicting {len(batch)} pods")
                for pod in batch:
                    evict_pod(pod, dry_run=args.dry_run)
                    tracker.evicted += 1
                    tracker.log()

                if not args.dry_run:
                    for pod in batch:
                        wait_for_replacement(pod, pod.spec.node_name)

    finally:
        if args.uncordon:
            uncordon_node(core, args.node, dry_run=args.dry_run)

    log.info("Evacuation completed successfully")

if __name__ == "__main__":
    main()