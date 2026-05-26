FROM quay.io/fedora/fedora-minimal:41

RUN microdnf install -y nfs-utils rpcbind && microdnf clean all

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 2049

ENTRYPOINT ["/entrypoint.sh"]
