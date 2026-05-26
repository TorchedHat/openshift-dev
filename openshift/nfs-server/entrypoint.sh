#!/bin/sh
set -e

# Start rpcbind
rpcbind -w

# Export all entries from /etc/exports
exportfs -arv

# Start NFS server (8 threads)
rpc.nfsd 8

# Start mountd in foreground (keeps container alive)
exec rpc.mountd --no-nfs-version 2 --no-nfs-version 3 --foreground
