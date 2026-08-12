/* Device-side (in-kernel) NVSHMEM RDMA over IBGDA — cross-node proof.
 *
 * This is the test the torch host-initiated ops can't cover: a CUDA *kernel* issues the
 * cross-node RDMA itself (`nvshmem_int_p` from device code), which only works if the IBGDA
 * transport is up. torch's symmetric_memory only exposes host/stream-proxy collectives and the
 * NVLink-only peer-pointer ops, so we drop to the raw NVSHMEM device API here.
 *
 * Bootstrap: no MPI/PMI. rank 0 generates the 128-byte NVSHMEM uniqueid and hands it to rank 1
 * over the shared RWX PVC (both lab pods mount /home/devuser). The UID embeds rank 0's bootstrap
 * IP:port; the uid plugin does the TCP rendezvous over the pod network (arbitrary ports OK on the
 * overlay, unlike the firewalled mgmt net). RANK / WORLD_SIZE come from the launch env.
 *
 * Each rank device-writes 1000+mype into the PEER's symmetric buffer, quiets in-kernel, barriers,
 * then verifies its own buffer now holds 1000+peer -> data crossed the node boundary from a kernel.
 *
 * Build (in the pod, H100 = sm_90):
 *   nvcc -rdc=true -ccbin g++ -gencode arch=compute_90,code=sm_90 \
 *     -I/usr/local/nvshmem/include nvshmem_device_put.cu -o nvshmem_device_put \
 *     -L/usr/local/nvshmem/lib -lnvshmem_host -lnvshmem_device \
 *     -L/usr/local/cuda-12.9/lib64 -lcudart -lcuda -lnvidia-ml -Xcompiler -fPIC
 * Run (env set by caller: NVSHMEM_IB_ENABLE_IBGDA=1, NVSHMEM_HCA_LIST=<rail>, RANK, WORLD_SIZE):
 *   RANK=0 WORLD_SIZE=2 ./nvshmem_device_put     # rank 0 pod
 *   RANK=1 WORLD_SIZE=2 ./nvshmem_device_put     # rank 1 pod
 */
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <unistd.h>
#include <cuda_runtime.h>
#include <nvshmem.h>
#include <nvshmemx.h>

#define UIDPATH "/home/devuser/nvshmem_uid.bin"

__global__ void put_kernel(int *dst, int myval, int peer, int n) {
    int i = blockIdx.x * blockDim.x + threadIdx.x;
    if (i < n) nvshmem_int_p(&dst[i], myval, peer);   // device-initiated cross-node RDMA
    __syncthreads();
    if (i == 0) nvshmem_quiet();                       // ensure the puts complete
}

int main() {
    const int rank   = atoi(getenv("RANK"));
    const int nranks = atoi(getenv("WORLD_SIZE"));
    const int peer   = (rank == 0) ? 1 : 0;

    cudaSetDevice(0);

    nvshmemx_uniqueid_t uid = NVSHMEMX_UNIQUEID_INITIALIZER;
    if (rank == 0) {
        if (nvshmemx_get_uniqueid(&uid) != 0) { fprintf(stderr, "get_uniqueid failed\n"); return 2; }
        char tmp[256]; snprintf(tmp, sizeof(tmp), "%s.tmp", UIDPATH);
        FILE *f = fopen(tmp, "wb");
        if (!f) { perror("fopen uid.tmp"); return 2; }
        fwrite(&uid, sizeof(uid), 1, f); fclose(f);
        rename(tmp, UIDPATH);                           // atomic publish
        printf("[dev-put] rank0 published uniqueid\n"); fflush(stdout);
    } else {
        int got = 0;
        for (int t = 0; t < 600 && !got; t++) {         // up to 60s
            FILE *f = fopen(UIDPATH, "rb");
            if (f) { got = (fread(&uid, sizeof(uid), 1, f) == 1); fclose(f); }
            if (!got) usleep(100000);
        }
        if (!got) { fprintf(stderr, "rank%d never got uniqueid\n", rank); return 2; }
        printf("[dev-put] rank%d read uniqueid\n", rank); fflush(stdout);
    }

    nvshmemx_init_attr_t attr = NVSHMEMX_INIT_ATTR_INITIALIZER;
    nvshmemx_set_attr_uniqueid_args(rank, nranks, &uid, &attr);
    nvshmemx_init_attr(NVSHMEMX_INIT_WITH_UNIQUEID, &attr);

    const int mype = nvshmem_my_pe();
    const int npes = nvshmem_n_pes();
    if (rank == 0) printf("[dev-put] NVSHMEM up: mype=%d npes=%d\n", mype, npes);

    const int N = 32;
    int *dst  = (int *) nvshmem_malloc(N * sizeof(int));
    int *hbuf = (int *) malloc(N * sizeof(int));
    for (int i = 0; i < N; i++) hbuf[i] = -1;
    cudaMemcpy(dst, hbuf, N * sizeof(int), cudaMemcpyHostToDevice);
    nvshmem_barrier_all();

    put_kernel<<<1, N>>>(dst, 1000 + mype, peer, N);    // write my id into peer's buffer
    cudaError_t ke = cudaDeviceSynchronize();
    if (ke != cudaSuccess) { fprintf(stderr, "[dev-put] rank%d kernel err: %s\n", rank, cudaGetErrorString(ke)); }
    nvshmem_barrier_all();

    cudaMemcpy(hbuf, dst, N * sizeof(int), cudaMemcpyDeviceToHost);
    const int expect = 1000 + peer;
    int ok = 1;
    for (int i = 0; i < N; i++) if (hbuf[i] != expect) { ok = 0; break; }
    printf("[dev-put] rank=%d mype=%d dst[0]=%d expect=%d  %s\n",
           rank, mype, hbuf[0], expect, ok ? "PASS" : "FAIL");
    fflush(stdout);

    nvshmem_barrier_all();
    nvshmem_free(dst);
    if (rank == 0) unlink(UIDPATH);
    _exit(ok ? 0 : 1);                                  // skip finalize (SIGSEGVs on this stack)
}
