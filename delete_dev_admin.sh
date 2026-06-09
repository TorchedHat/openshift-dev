#!/bin/bash

read -p "Enter username: " USERNAME

# create namespace for the user
oc delete -f <(sed "s/<username>/$USERNAME/g" namespace.yml)

oc adm policy remove-scc-from-user anyuid -z default -n $USERNAME
