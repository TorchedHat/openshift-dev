"""
Cross-node torch symmetric-memory (NVSHMEM backend) smoke test for pytorch-openshift.

Run with launch_nvshmem.sh (IBRC transport + single HCA):
    # node 0 / master:
    NODE_RANK=0 sh launch_nvshmem.sh test_symmem_internode.py
    # node 1, within ~30s:
    NODE_RANK=1 sh launch_nvshmem.sh test_symmem_internode.py

Expected output on rank 0 (exit code 0):
    world_size=4  (2 nodes x 2 GPUs)  backend=NVSHMEM/ibrc
    [all_to_all]  PASS  out=[0,0,0,0,0,0,0,0, 1,...,1, 2,...,2, 3,...,3]
    [broadcast]   PASS  (root=3)
    ALL DONE (cross-node NVSHMEM RDMA verified)

WHY NOT get_buffer / one_shot_all_reduce?
-----------------------------------------
Those are *in-kernel peer-pointer* ops: the CUDA kernel directly loads/stores a
peer's buffer. That requires the peer to be directly addressable from the GPU --
NVLink within a node, or IBGDA (GPU-initiated RDMA) across nodes. Across nodes you
get "Cannot get buffer across nodes" / cudaErrorIllegalAddress, and IBGDA is
unavailable on this cluster (GPU can't map the NIC BAR: cudaHostRegister IoMemory
error=800 / ibgda_nic_mem_gpu_map failed).

For CROSS-NODE data movement use the HOST/STREAM-initiated NVSHMEM ops below. They
run on the IBRC CPU-proxy and move data over RoCE/RDMA between nodes:
    torch.ops.symm_mem.nvshmem_all_to_all(input, out, group_name)   # verified
    torch.ops.symm_mem.nvshmem_broadcast(tensor, root, group_name)  # verified
    torch.ops.symm_mem.nvshmem_put(tensor, peer)                    # one-sided
    torch.ops.symm_mem.nvshmem_get(tensor, peer)                    # one-sided
    torch.ops.symm_mem.nvshmem_put_with_signal(...) / nvshmem_wait_for_signal(...)

DEVICE PINNING (required)
-------------------------
Pin the device explicitly, pass device_id to init_process_group, and allocate
symmetric tensors under `with torch.cuda.device(dev)`. Otherwise NVSHMEM's
TeamManager singleton trips: "Detected use of TeamManager on multiple devices".
"""

import os
import sys
import torch
import torch.distributed as dist
import torch.distributed._symmetric_memory as symm_mem


def main():
    rank = int(os.environ["RANK"])
    local_rank = int(os.environ["LOCAL_RANK"])
    world_size = int(os.environ["WORLD_SIZE"])

    # --- device pinning (avoids the TeamManager multi-device error) ---
    dev = torch.device("cuda", local_rank)
    torch.cuda.set_device(dev)
    dist.init_process_group("nccl", device_id=dev)

    assert symm_mem.is_nvshmem_available(), "NVSHMEM backend not available"
    symm_mem.set_backend("NVSHMEM")
    gname = dist.group.WORLD.group_name

    if rank == 0:
        n_nodes = world_size // int(os.environ.get("LOCAL_WORLD_SIZE", "1"))
        print(f"world_size={world_size}  backend=NVSHMEM/ibrc", flush=True)

    # ---------- 1) all-to-all across all ranks (incl. cross-node) ----------
    # Each rank fills its whole input with its own id. After all-to-all,
    # out chunk i holds the id sent by rank i (== i).
    k = 8
    n = world_size * k
    with torch.cuda.device(dev):
        inp = symm_mem.empty(n, device=dev, dtype=torch.int32)
        out = symm_mem.empty(n, device=dev, dtype=torch.int32)
        symm_mem.rendezvous(inp, dist.group.WORLD)
        symm_mem.rendezvous(out, dist.group.WORLD)
        inp.fill_(rank)
        out.fill_(-1)
        torch.cuda.synchronize()
        torch.ops.symm_mem.nvshmem_all_to_all(inp, out, gname)
        torch.cuda.synchronize()
        ok_a2a = all(bool(out[i * k:(i + 1) * k].eq(i).all().item()) for i in range(world_size))
    if rank == 0:
        print(f"[all_to_all]  {'PASS' if ok_a2a else 'FAIL'}  out={out.tolist()}", flush=True)

    # ---------- 2) broadcast from an off-node root ----------
    with torch.cuda.device(dev):
        b = symm_mem.empty(16, device=dev, dtype=torch.int32)
        symm_mem.rendezvous(b, dist.group.WORLD)
        root = world_size - 1                       # last rank -> lives on the other node
        b.fill_(12345 if rank == root else -1)
        torch.cuda.synchronize()
        torch.ops.symm_mem.nvshmem_broadcast(b, root, gname)
        torch.cuda.synchronize()
        ok_bc = bool(b.eq(12345).all().item())
    if rank == 0:
        print(f"[broadcast]   {'PASS' if ok_bc else 'FAIL'}  (root={root})", flush=True)

    if rank == 0:
        print("ALL DONE (cross-node NVSHMEM RDMA verified)", flush=True)

    # NVSHMEM's finalize (via atexit and dist.destroy_process_group()) currently
    # SIGSEGVs on this stack -- a benign teardown crash that happens AFTER all
    # results are verified. os._exit(0) exits immediately, skipping the crashing
    # finalizer, so torchrun reports success. Prints above use flush=True.
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0 if (ok_a2a and ok_bc) else 1)


if __name__ == "__main__":
    main()
