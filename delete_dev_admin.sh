#!/bin/bash

read -p "Enter username: " USERNAME

# remove the namespace's SCC grants (same single file used to create them)
oc delete -f <(sed "s/<username>/$USERNAME/g" scc/user-scc-bindings.yml) --ignore-not-found

# delete the user's namespace (also cascades any remaining namespaced bindings)
oc delete -f <(sed "s/<username>/$USERNAME/g" namespace.yml)
