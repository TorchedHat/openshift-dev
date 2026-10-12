def bucket(n):
    assert n > 0 and n <= 8, "n must be between 1 and 8!"
    cpu_req = max(8, 4 * n)
    mem_req = max(64, 32 * n)
    rct = f"symmetric-gpu-run-{n}"
    return dict(
        gpus=n,
        rct=rct,
        cpu_req=str(cpu_req),
        cpu_lim=str(2 * cpu_req),
        mem_req=f"{mem_req}Gi",
        mem_lim=f"{2 * mem_req}Gi"
    )


def runtime_name(n):
    return f"torch-xnode-{n}gpu"


# bucket -> GPUs per pod (== numProcPerNode; numNodes is always 2) + the DRA claim template it
# references + per-pod CPU/mem sizing. All buckets are 2 nodes (internode RDMA fabric test).
BUCKETS = {n: bucket(n) for n in range(1, 9)}
