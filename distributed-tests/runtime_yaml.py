build_pytorch_runtime = """\
apiVersion: trainer.kubeflow.org/v1alpha1
kind: TrainingRuntime
metadata:
  name: {name}
  namespace: {ns}
  labels:
    trainer.kubeflow.org/framework: torch
spec:
  mlPolicy:
    numNodes: 2
    torch:
      numProcPerNode: {ngpus}
  template:
    spec:
      ttlSecondsAfterFinished: 1800
      replicatedJobs:
        - name: node
          template:
            metadata:
              labels:
                trainer.kubeflow.org/trainjob-ancestor-step: trainer
            spec:
              backoffLimit: 0
              template:
                metadata:
                  labels:
                    app: {app}
                spec:
                  serviceAccountName: {sa}
                  restartPolicy: Never
                  imagePullSecrets:
                    - name: {pull_secret}
                  securityContext:
                    runAsUser: 1000
                    runAsGroup: 0
                    fsGroup: 0
                    fsGroupChangePolicy: OnRootMismatch
                  affinity:
                    podAntiAffinity:
                      requiredDuringSchedulingIgnoredDuringExecution:
                        - topologyKey: kubernetes.io/hostname
                          labelSelector:
                            matchLabels:
                              app: {app}
                  resourceClaims:
                    - name: gpu
                      resourceClaimTemplateName: {rct}
                  # BUILD PHASE
                  initContainers:
                    - name: build
                      image: {image}
                      imagePullPolicy: Always
                      command: ["/bin/bash", "/opt/build/build-at-run.initcontainer.sh"]
                      env:
                        - name: TORCH_CUDA_ARCH_LIST
                          value: "9.0"
                        - name: TH_REPO
                          value: "https://github.com/skpark-rh/TestHarness.git"
                        - name: TH_REF
                          value: "main"
                        - name: CCACHE_MAXSIZE
                          value: "60G"
                        - name: MAX_JOBS
                          value: "32"
                      resources:
                        requests:
                          cpu: "4"
                          memory: 64Gi
                        limits:
                          cpu: "32"
                          memory: 128Gi
                      volumeMounts:
                        - name: work
                          mountPath: /work
                        - name: cache
                          mountPath: /cache
                        - name: build-script
                          mountPath: /opt/build
                        - name: source
                          mountPath: /opt/src  # optional dev.patch
                  # TEST PHASE
                  containers:
                    - name: node
                      image: {image}
                      imagePullPolicy: Always
                      command:
                        - /bin/bash
                        - -c
                        - 'source /work/venv/bin/activate && exec /opt/rz/entrypoint.sh "$@"'
                        - --
                      securityContext:
                        capabilities:
                          add: ["IPC_LOCK"]
                      env:
                        - name: IB_DEV
                          value: mlx5_0
                      resources:
                        requests:
                          cpu: "{cpu_req}"
                          memory: {mem_req}
                          rdma/rdma_shared_device_a: "1"
                        limits:
                          cpu: "{cpu_lim}"
                          memory: {mem_lim}
                          rdma/rdma_shared_device_a: "1"
                        claims:
                          - name: gpu
                      volumeMounts:
                        - name: work
                          mountPath: /work
                        - name: entrypoint
                          mountPath: /opt/rz
                        - name: dshm
                          mountPath: /dev/shm
                  volumes:
                    - name: work
                      emptyDir:
                        sizeLimit: 80Gi
                    - name: cache
                      persistentVolumeClaim:
                        claimName: pytorch-build-cache
                    - name: build-script
                      configMap:
                        name: torch-cross-node-build-script
                        defaultMode: 0555
                    - name: entrypoint
                      configMap:
                        name: torch-cross-node-entrypoint-podnet
                        defaultMode: 0555
                    - name: source
                      configMap:
                        name: torch-cross-node-dev-patch
                        optional: true
                    - name: dshm
                      emptyDir:
                        medium: Memory
"""

dev_pytorch_runtime = """\
apiVersion: trainer.kubeflow.org/v1alpha1
kind: TrainingRuntime
metadata:
  name: {name}
  namespace: {ns}
  labels:
    trainer.kubeflow.org/framework: torch
spec:
  mlPolicy:
    numNodes: 2
    torch:
      numProcPerNode: {ngpus}
  template:
    spec:
      ttlSecondsAfterFinished: 1800
      replicatedJobs:
        - name: node
          template:
            metadata:
              labels:
                trainer.kubeflow.org/trainjob-ancestor-step: trainer
            spec:
              backoffLimit: 0
              template:
                metadata:
                  labels:
                    app: {app}
                spec:
                  serviceAccountName: {sa}
                  restartPolicy: Never
                  imagePullSecrets:
                    - name: {pull_secret}
                  securityContext:
                    runAsUser: 1000
                    runAsGroup: 0
                    fsGroupChangePolicy: OnRootMismatch
                  # Force the 2 pods onto different nodes
                  affinity:
                    podAntiAffinity:
                      requiredDuringSchedulingIgnoredDuringExecution:
                        - topologyKey: kubernetes.io/hostname
                          labelSelector:
                            matchLabels:
                              app: {app}
                  resourceClaims:
                    - name: gpu
                      resourceClaimTemplateName: {rct}
                  containers:
                    - name: node
                      image: {image}
                      imagePullPolicy: Always
                      command: ["/bin/bash", "/opt/rz/entrypoint.sh"]
                      securityContext:
                        capabilities:
                          add: ["IPC_LOCK"]
                      env:
                        - name: IB_DEV
                          value: mlx5_0
                      resources:
                        requests:
                          cpu: "{cpu_req}"
                          memory: {mem_req}
                          rdma/rdma_shared_device_a: "1"
                        limits:
                          cpu: "{cpu_lim}"
                          memory: {mem_lim}
                          rdma/rdma_shared_device_a: "1"
                        claims:
                          - name: gpu
                      volumeMounts:
                        - name: pytorch-eco-data
                          mountPath: /home/devuser
                        - name: entrypoint
                          mountPath: /opt/rz
                        - name: dshm
                          mountPath: /dev/shm
                  volumes:
                    - name: pytorch-eco-data
                      persistentVolumeClaim:
                        claimName: {pvc}
                    - name: entrypoint
                      configMap:
                        name: {entrypoint_cm}
                        defaultMode: 0555
                    - name: dshm
                      emptyDir:
                        medium: Memory
"""