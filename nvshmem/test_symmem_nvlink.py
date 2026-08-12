"""NVLink intra-node device-side symmetric-memory ops (the NVLink half of the goal).

These are the torch in-kernel peer-pointer ops that are NVLink-only by design (they dereference a
peer's raw device pointer inside the CUDA kernel): get_buffer + one_shot / two_shot / multimem
all-reduce. Cross-node they fail (need IBGDA + a custom kernel); intra-node over NVLink they run
directly. Run in a single pod with 2 GPUs on the SAME node (DRA gpu-2 claim):

  torchrun --standalone --nnodes=1 --nproc_per_node=2 test_symmem_nvlink.py

Default symm_mem backend (CUDA IPC + NVLink multicast) is used — NOT the NVSHMEM backend — since
this is the pure intra-node NVLink path.
"""
import os
import sys
import torch
import torch.distributed as dist
import torch.distributed._symmetric_memory as symm_mem


def _reduce_expect(world_size):
    return float(sum(r + 1 for r in range(world_size)))


def main():
    rank = int(os.environ["RANK"])
    local_rank = int(os.environ["LOCAL_RANK"])
    world_size = int(os.environ["WORLD_SIZE"])
    dev = torch.device("cuda", local_rank)
    torch.cuda.set_device(dev)
    dist.init_process_group("nccl", device_id=dev)
    gname = dist.group.WORLD.group_name
    peer = (rank + 1) % world_size
    expect = _reduce_expect(world_size)
    results = {}

    # 1) raw peer-pointer read over NVLink
    with torch.cuda.device(dev):
        t = symm_mem.empty(1024, device=dev, dtype=torch.float32).fill_(float(rank))
        hdl = symm_mem.rendezvous(t, dist.group.WORLD)
        try:
            pbuf = hdl.get_buffer(peer, t.shape, t.dtype)
            v = pbuf[0].item()
            results["get_buffer(peer)"] = ("PASS" if v == float(peer) else "FAIL") + f" (peer{peer} val={v})"
        except Exception as e:
            results["get_buffer(peer)"] = f"FAIL ({type(e).__name__}: {e})"

    # 2) one_shot_all_reduce (returns a new tensor)
    with torch.cuda.device(dev):
        inp = symm_mem.empty(4096, device=dev, dtype=torch.float32).fill_(float(rank + 1))
        symm_mem.rendezvous(inp, dist.group.WORLD)
        try:
            out = torch.ops.symm_mem.one_shot_all_reduce(inp, "sum", gname)
            torch.cuda.synchronize()
            ok = bool(out.eq(expect).all().item())
            results["one_shot_all_reduce"] = ("PASS" if ok else "FAIL") + f" (out[0]={out[0].item()} expect={expect})"
        except Exception as e:
            results["one_shot_all_reduce"] = f"FAIL ({type(e).__name__}: {e})"

    # 3) two_shot_all_reduce_ (in-place)
    with torch.cuda.device(dev):
        inp = symm_mem.empty(4096, device=dev, dtype=torch.float32).fill_(float(rank + 1))
        symm_mem.rendezvous(inp, dist.group.WORLD)
        try:
            torch.ops.symm_mem.two_shot_all_reduce_(inp, "sum", gname)
            torch.cuda.synchronize()
            ok = bool(inp.eq(expect).all().item())
            results["two_shot_all_reduce_"] = ("PASS" if ok else "FAIL") + f" (inp[0]={inp[0].item()} expect={expect})"
        except Exception as e:
            results["two_shot_all_reduce_"] = f"FAIL ({type(e).__name__}: {e})"

    # 4) multimem_all_reduce_ (NVLink SHARP multicast)
    with torch.cuda.device(dev):
        inp = symm_mem.empty(4096, device=dev, dtype=torch.float32).fill_(float(rank + 1))
        symm_mem.rendezvous(inp, dist.group.WORLD)
        try:
            torch.ops.symm_mem.multimem_all_reduce_(inp, "sum", gname)
            torch.cuda.synchronize()
            ok = bool(inp.eq(expect).all().item())
            results["multimem_all_reduce_"] = ("PASS" if ok else "FAIL") + f" (inp[0]={inp[0].item()} expect={expect})"
        except Exception as e:
            results["multimem_all_reduce_"] = f"FAIL ({type(e).__name__}: {e})"

    if rank == 0:
        print("==== NVLink intra-node device-side symmetric-memory ops ====", flush=True)
        for k, v in results.items():
            print(f"  {k:24s} {v}", flush=True)
        allpass = all("PASS" in v for v in results.values())
        print(f"ALL DONE ({'ALL PASS' if allpass else 'SOME FAILED'})", flush=True)

    sys.stdout.flush()
    os._exit(0)


if __name__ == "__main__":
    main()
