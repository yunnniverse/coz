#!/usr/bin/env python3

import argparse
import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MCOZ_GATE = ROOT / "scripts" / "mcoz_gate.py"
MCOZ_THRIFT_ADAPTER = ROOT / "scripts" / "mcoz_thrift_adapter.py"


def run(cmd, stdin=None, capture_output=False):
    kwargs = {"text": True}
    if stdin is not None:
        kwargs["input"] = stdin
    if capture_output:
        kwargs["capture_output"] = True
    proc = subprocess.run(cmd, **kwargs)
    if proc.returncode != 0:
        raise SystemExit(proc.returncode)
    return proc.stdout if capture_output else ""


def upsert_configmap(namespace, name, files, dry_run=False):
    cmd = ["kubectl", "-n", namespace, "create", "configmap", name]
    for key, path in files.items():
        cmd.append(f"--from-file={key}={path}")
    cmd.extend(["-o", "json", "--dry-run=client"])
    rendered = run(cmd, capture_output=True)
    if dry_run:
        print(f"--- # ConfigMap {namespace}/{name}")
        print(rendered)
        return
    run(["kubectl", "apply", "-f", "-"], stdin=rendered)


def patch_deployment(args):
    patch = {
        "spec": {
            "template": {
                "metadata": {
                    "annotations": {
                        "sidecar.istio.io/inject": "true",
                    }
                },
                "spec": {
                    "volumes": [
                        {
                            "name": "mcoz-gate-script",
                            "configMap": {"name": "mcoz-gate-script"},
                        }
                    ],
                    "containers": [
                        {
                            "name": "mcoz-gate",
                            "image": "python:3.11-slim",
                            "imagePullPolicy": "IfNotPresent",
                            "command": ["python", "-u", "/gate/mcoz_gate.py"],
                            "env": [
                                {
                                    "name": "POD_NAME",
                                    "valueFrom": {
                                        "fieldRef": {"fieldPath": "metadata.name"}
                                    },
                                },
                                {
                                    "name": "POD_NAMESPACE",
                                    "valueFrom": {
                                        "fieldRef": {"fieldPath": "metadata.namespace"}
                                    },
                                },
                                {"name": "MCOZ_CONTAINER", "value": args.container},
                                {"name": "MCOZ_GATE_PORT", "value": str(args.gate_port)},
                                {
                                    "name": "MCOZ_ARM_URL",
                                    "value": args.arm_url,
                                },
                                {"name": "MCOZ_DIRECT_ARM", "value": "true"},
                                {
                                    "name": "MCOZ_DELAY_NS",
                                    "value": str(args.delay_ns),
                                },
                                {"name": "MCOZ_COUNT", "value": str(args.count)},
                                {"name": "MCOZ_MATCH_MODE", "value": args.match_mode},
                                {
                                    "name": "MCOZ_ARM_ACTIVE_DEFAULT",
                                    "value": "false",
                                },
                                {
                                    "name": "MCOZ_FAIL_OPEN_ON_ARM_UNAVAILABLE",
                                    "value": "true",
                                },
                                {
                                    "name": "MCOZ_ARM_SUSPEND_SEC",
                                    "value": str(args.arm_suspend_sec),
                                },
                            ],
                            "ports": [
                                {
                                    "containerPort": int(args.gate_port),
                                    "name": "mcoz-gate",
                                }
                            ],
                            "readinessProbe": {
                                "httpGet": {
                                    "path": "/healthz",
                                    "port": int(args.gate_port),
                                },
                                "initialDelaySeconds": 2,
                                "periodSeconds": 5,
                            },
                            "livenessProbe": {
                                "httpGet": {
                                    "path": "/healthz",
                                    "port": int(args.gate_port),
                                },
                                "initialDelaySeconds": 5,
                                "periodSeconds": 10,
                            },
                            "volumeMounts": [
                                {"name": "mcoz-gate-script", "mountPath": "/gate"}
                            ],
                        }
                    ],
                },
            }
        }
    }

    if args.target_namespace:
        patch["spec"]["template"]["spec"]["containers"][0]["env"].append(
            {"name": "MCOZ_TARGET_NAMESPACE", "value": args.target_namespace}
        )
    if args.target_pod:
        patch["spec"]["template"]["spec"]["containers"][0]["env"].append(
            {"name": "MCOZ_TARGET_POD", "value": args.target_pod}
        )
    if args.target_container:
        patch["spec"]["template"]["spec"]["containers"][0]["env"].append(
            {"name": "MCOZ_TARGET_CONTAINER", "value": args.target_container}
        )

    if args.protocol == "thrift":
        patch["spec"]["template"]["spec"]["volumes"].append(
            {
                "name": "mcoz-thrift-adapter-script",
                "configMap": {"name": "mcoz-thrift-adapter-script"},
            }
        )
        patch["spec"]["template"]["spec"]["containers"].append(
            {
                "name": "mcoz-thrift-adapter",
                "image": "python:3.11-slim",
                "imagePullPolicy": "IfNotPresent",
                "command": ["python", "-u", "/adapter/mcoz_thrift_adapter.py"],
                "env": [
                    {
                        "name": "MCOZ_THRIFT_LISTEN_PORT",
                        "value": str(args.thrift_adapter_port),
                    },
                    {"name": "MCOZ_THRIFT_UPSTREAM_HOST", "value": "127.0.0.1"},
                    {
                        "name": "MCOZ_THRIFT_UPSTREAM_PORT",
                        "value": str(args.app_port),
                    },
                    {"name": "MCOZ_GATE_HOST", "value": "127.0.0.1"},
                    {"name": "MCOZ_GATE_PORT", "value": str(args.gate_port)},
                    {"name": "MCOZ_THRIFT_SERVICE", "value": args.service_name},
                ],
                "readinessProbe": {
                    "tcpSocket": {"port": int(args.thrift_adapter_port)},
                    "initialDelaySeconds": 2,
                    "periodSeconds": 5,
                },
                "livenessProbe": {
                    "tcpSocket": {"port": int(args.thrift_adapter_port)},
                    "initialDelaySeconds": 5,
                    "periodSeconds": 10,
                },
                "volumeMounts": [
                    {
                        "name": "mcoz-thrift-adapter-script",
                        "mountPath": "/adapter",
                    }
                ],
            }
        )

    rendered = json.dumps(patch, separators=(",", ":"))
    if args.dry_run:
        print(f"--- # Deployment patch {args.namespace}/{args.deployment}")
        print(rendered)
        return
    run(
        [
            "kubectl",
            "-n",
            args.namespace,
            "patch",
            "deployment",
            args.deployment,
            "--type",
            "strategic",
            "-p",
            rendered,
        ]
    )


def build_envoyfilter(args):
    selector_key, selector_value = args.selector.split("=", 1)
    name = f"{args.deployment}-mcoz-trigger"
    if args.protocol == "thrift":
        cluster_name = f"mcoz_thrift_adapter_local_{args.deployment}".replace("-", "_")
        return {
            "apiVersion": "networking.istio.io/v1alpha3",
            "kind": "EnvoyFilter",
            "metadata": {"name": name, "namespace": args.namespace},
            "spec": {
                "workloadSelector": {"labels": {selector_key: selector_value}},
                "configPatches": [
                    {
                        "applyTo": "NETWORK_FILTER",
                        "match": {
                            "context": "SIDECAR_INBOUND",
                            "listener": {
                                "portNumber": int(args.app_port),
                                "filterChain": {
                                    "filter": {
                                        "name": "envoy.filters.network.tcp_proxy"
                                    }
                                },
                            },
                        },
                        "patch": {
                            "operation": "MERGE",
                            "value": {
                                "name": "envoy.filters.network.tcp_proxy",
                                "typed_config": {
                                    "@type": "type.googleapis.com/envoy.extensions.filters.network.tcp_proxy.v3.TcpProxy",
                                    "stat_prefix": f"mcoz_thrift_trigger_{args.deployment}",
                                    "cluster": cluster_name,
                                },
                            },
                        },
                    },
                    {
                        "applyTo": "CLUSTER",
                        "match": {"context": "SIDECAR_INBOUND"},
                        "patch": {
                            "operation": "ADD",
                            "value": {
                                "name": cluster_name,
                                "type": "STATIC",
                                "connect_timeout": "0.25s",
                                "load_assignment": {
                                    "cluster_name": cluster_name,
                                    "endpoints": [
                                        {
                                            "lb_endpoints": [
                                                {
                                                    "endpoint": {
                                                        "address": {
                                                            "socket_address": {
                                                                "address": "127.0.0.1",
                                                                "port_value": int(
                                                                    args.thrift_adapter_port
                                                                ),
                                                            }
                                                        }
                                                    }
                                                }
                                            ]
                                        }
                                    ],
                                },
                            },
                        },
                    },
                ],
            },
        }

    allowed_headers = [
        {"exact": ":method"},
        {"exact": ":path"},
        {"exact": ":authority"},
        {"exact": "content-type"},
        {"exact": "x-request-id"},
        {"exact": "x-mcoz-enable"},
        {"exact": "x-b3-traceid"},
        {"exact": "traceparent"},
        {"exact": "grpc-timeout"},
        {"exact": "te"},
    ]
    cluster_name = f"mcoz_gate_local_{args.deployment}".replace("-", "_")
    return {
        "apiVersion": "networking.istio.io/v1alpha3",
        "kind": "EnvoyFilter",
        "metadata": {"name": name, "namespace": args.namespace},
        "spec": {
            "workloadSelector": {"labels": {selector_key: selector_value}},
            "configPatches": [
                {
                    "applyTo": "HTTP_FILTER",
                    "match": {
                        "context": "SIDECAR_INBOUND",
                        "listener": {
                            "filterChain": {
                                "filter": {
                                    "name": "envoy.filters.network.http_connection_manager",
                                    "subFilter": {
                                        "name": "envoy.filters.http.router"
                                    },
                                }
                            }
                        },
                    },
                    "patch": {
                        "operation": "INSERT_BEFORE",
                        "value": {
                            "name": "envoy.filters.http.ext_authz",
                            "typed_config": {
                                "@type": "type.googleapis.com/envoy.extensions.filters.http.ext_authz.v3.ExtAuthz",
                                "transport_api_version": "V3",
                                "stat_prefix": "mcoz_gate",
                                "failure_mode_allow": True,
                                "with_request_body": {
                                    "max_request_bytes": 1,
                                    "allow_partial_message": True,
                                },
                                "http_service": {
                                    "path_prefix": "/check",
                                    "server_uri": {
                                        "uri": f"http://127.0.0.1:{args.gate_port}",
                                        "cluster": cluster_name,
                                        "timeout": "0.25s",
                                    },
                                    "authorization_request": {
                                        "allowed_headers": {
                                            "patterns": allowed_headers
                                        }
                                    },
                                    "authorization_response": {
                                        "allowed_upstream_headers": {
                                            "patterns": [{"prefix": "x-mcoz-"}]
                                        }
                                    },
                                },
                            },
                        },
                    },
                },
                {
                    "applyTo": "CLUSTER",
                    "match": {"context": "SIDECAR_INBOUND"},
                    "patch": {
                        "operation": "ADD",
                        "value": {
                            "name": cluster_name,
                            "type": "STATIC",
                            "connect_timeout": "0.25s",
                            "load_assignment": {
                                "cluster_name": cluster_name,
                                "endpoints": [
                                    {
                                        "lb_endpoints": [
                                            {
                                                "endpoint": {
                                                    "address": {
                                                        "socket_address": {
                                                            "address": "127.0.0.1",
                                                            "port_value": int(args.gate_port),
                                                        }
                                                    }
                                                }
                                            }
                                        ]
                                    }
                                ],
                            },
                        },
                    },
                },
            ],
        },
    }


def apply_envoyfilter(args):
    manifest = build_envoyfilter(args)
    rendered = json.dumps(manifest)
    if args.dry_run:
        print(f"--- # EnvoyFilter {args.namespace}/{manifest['metadata']['name']}")
        print(rendered)
        return
    run(["kubectl", "apply", "-f", "-"], stdin=rendered)


def main():
    parser = argparse.ArgumentParser(
        description="Apply protocol-aware mcoz trigger adapters to a workload"
    )
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--deployment", required=True)
    parser.add_argument("--selector", required=True, help="label selector key=value")
    parser.add_argument("--container", required=True, help="application container name")
    parser.add_argument("--service-name", default="", help="logical service name for thrift paths")
    parser.add_argument(
        "--protocol", required=True, choices=["http", "grpc", "thrift"]
    )
    parser.add_argument("--app-port", type=int, default=9090)
    parser.add_argument("--gate-port", type=int, default=19093)
    parser.add_argument("--thrift-adapter-port", type=int, default=19094)
    parser.add_argument(
        "--arm-url",
        default="udp://coz-daemon-udp-local.mcoz-system.svc.cluster.local:19090/arm",
    )
    parser.add_argument("--delay-ns", type=int, default=10000000)
    parser.add_argument("--count", type=int, default=1)
    parser.add_argument("--match-mode", default="all")
    parser.add_argument("--arm-suspend-sec", type=float, default=30.0)
    parser.add_argument("--target-namespace", default="")
    parser.add_argument("--target-pod", default="")
    parser.add_argument("--target-container", default="")
    parser.add_argument("--rollout", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    upsert_configmap(
        args.namespace, "mcoz-gate-script", {"mcoz_gate.py": MCOZ_GATE}, dry_run=args.dry_run
    )
    if args.protocol == "thrift":
        upsert_configmap(
            args.namespace,
            "mcoz-thrift-adapter-script",
            {"mcoz_thrift_adapter.py": MCOZ_THRIFT_ADAPTER},
            dry_run=args.dry_run,
        )
    patch_deployment(args)
    apply_envoyfilter(args)
    if args.rollout and not args.dry_run:
        run(
            [
                "kubectl",
                "-n",
                args.namespace,
                "rollout",
                "status",
                f"deployment/{args.deployment}",
                "--timeout=180s",
            ]
        )


if __name__ == "__main__":
    main()
